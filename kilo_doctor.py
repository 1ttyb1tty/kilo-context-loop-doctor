#!/usr/bin/env python3
"""
kilo_doctor.py - detect, report and retire sessions stuck in the context-overflow regime.

Why this exists: a session whose serialized transcript crosses the payload threshold never
recovers on its own. Kilo prunes on every step (`payload still large after pruning`), the
compaction that is supposed to reduce it produces empty summaries and removes nothing, and the
session silently keeps re-sending an oversized payload until the provider refuses it outright -
after which every turn repeats the same failure. Such a session has to be retired by hand.

Evidence sources, and why the order matters:
  * STORE (~/.local/share/kilo/kilo.db) - durable. Session/message/part rows and per-call token
    usage live here. Everything authority-bearing is read from here.
  * LOGS (~/.local/share/kilo/log/) - NOT durable. Per-session logs are deleted within hours;
    opencode.log persists but does not contain compaction lines at all. Log-derived numbers are
    reported as best-effort and never used to prove an absence.
  * STATE (~/.local/share/kilo/context-doctor-state.json) - the union of warn marks ever seen, so a
    session that was stuck before its log rotated still looks stuck.

What the report adds beyond a severity ratio: the payload trajectory (tok/h, hours to the window)
and a RISING block, because every session measured entering this regime was already unrecoverable
when first flagged; the payload side in bytes, because the prune trigger is a size and sev% is a
ratio of tokens; and per-query counts for the compaction-marker probes, because "none" on its own
cannot be audited.

Safety model:
  * detection and reporting are read-only; nothing is written outside the evidence directory, the
    state file and the archive manifest
  * retirement is archive-first: every session (and every child session) is exported to JSON and
    validated before any delete happens; one failed export aborts the whole run
  * the session running the command is identified by directory + recency and is never retired; any
    other session touched within --active-window minutes is skipped with the follow-up printed
    (--active-window/--force), since a recent mtime may be a different, idle session
  * deletion requires --apply; the default is a dry run

Usage:
    python kilo_doctor.py                       # report every session
    python kilo_doctor.py --predict             # trajectory for every session
    python kilo_doctor.py --json                # machine-readable rows
    python kilo_doctor.py --min-severity 60     # display filter only
    python kilo_doctor.py --retire-stuck --apply
    python kilo_doctor.py --retire ses_abc... --apply [--force]
    python kilo_doctor.py --list-archives | --verify-archives | --reindex-archives
    python kilo_doctor.py --evidence-dir D:\\kilo-evidence
"""
import argparse
import glob
import json
import os
import subprocess
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DEFAULT_WINDOW = 1048576          # model context window used for the severity ratio
STUCK_MIN_WARNS = 1               # any failed prune means the payload is over the threshold
SEVERITY_WARN = 60                # % of window at which a session is called degraded
SEVERITY_HINT = 34                # % of window at which the prune path is documented to start
PRUNE_BYTES_EST = 1250000         # ESTIMATE of the payload prune threshold (REQUEST_PRUNE_BYTES):
                                  # the smallest first-warning payload seen on 7.8.15 over five stuck
                                  # sessions was 1,250,350 bytes. A measured floor, not a value read
                                  # out of the bundle - every pay% derived from it is an estimate.
STATE_KEEP = 400                  # persisted warn marks kept (sessions are few, marks are tiny)


def find_kilo():
    env = os.environ.get("KILO_BIN")
    if env and os.path.isfile(env):
        return env
    pats = [
        os.path.expandvars(r"%USERPROFILE%\.vscode\extensions\kilocode.kilo-code-*\bin\kilo.exe"),
        os.path.expandvars(r"%USERPROFILE%\.vscode\extensions\kilo-code*\bin\kilo.exe"),
        "/usr/local/bin/kilo",
        "/usr/bin/kilo",
    ]
    for p in pats:
        hits = sorted(glob.glob(p))
        if hits:
            return hits[-1]
    raise SystemExit("kilo binary not found; set KILO_BIN")


KILO = None


def db(query, timeout=180):
    """Run a read-only query through the CLI. Returns rows as lists of strings."""
    r = subprocess.run([KILO, "db", query], capture_output=True, timeout=timeout)
    out = (r.stdout or b"").decode("utf-8", "replace").strip()
    if r.returncode != 0 or not out:
        return []
    lines = [l for l in out.splitlines() if l.strip()]
    return [l.split("\t") for l in lines[1:]]


def q1sessions():
    rows = db("SELECT id, coalesce(directory,''), coalesce(title,''), tokens_input, "
              "tokens_output, tokens_cache_read, time_updated, coalesce(time_compacting,0), "
              "coalesce(model,'') "
              "FROM session ORDER BY time_updated DESC")
    out = {}
    for r in rows:
        if len(r) < 9:
            continue
        out[r[0]] = {
            "id": r[0], "directory": r[1], "title": r[2],
            "tokens_input": int(r[3] or 0), "tokens_output": int(r[4] or 0),
            "tokens_cache_read": int(r[5] or 0), "time_updated": int(r[6] or 0),
            "time_compacting": int(r[7] or 0), "model": r[8],
        }
    return out


def q2usage():
    """Largest context a session ever sent, its call count, and cold sends.

    tokens.input + tokens.cache.read for a call is approximately the whole context it sent (the
    cached prefix plus the uncached remainder), so the maximum over a session estimates its peak
    payload in tokens without needing the logs.
    """
    rows = db("SELECT session_id, COUNT(*), "
              "MAX(json_extract(data,'$.tokens.input') + json_extract(data,'$.tokens.cache.read')), "
              "SUM(CASE WHEN json_extract(data,'$.tokens.input') > 100000 "
              "AND json_extract(data,'$.tokens.cache.read') < 0.1*json_extract(data,'$.tokens.input') "
              "THEN 1 ELSE 0 END), "
              "SUM(CASE WHEN json_extract(data,'$.tokens.input') > 100000 "
              "AND json_extract(data,'$.tokens.cache.read') < 0.1*json_extract(data,'$.tokens.input') "
              "THEN json_extract(data,'$.tokens.input') ELSE 0 END) "
              "FROM message GROUP BY session_id")
    out = {}
    for r in rows:
        if len(r) < 5:
            continue
        out[r[0]] = {"calls": int(r[1] or 0), "peak_ctx": int(r[2] or 0),
                     "cold_sends": int(r[3] or 0), "uncached_in": int(r[4] or 0)}
    return out


def q3parts():
    rows = db("SELECT session_id, COUNT(*) FROM part GROUP BY session_id")
    return {r[0]: int(r[1]) for r in rows if len(r) > 1}


def q4compaction():
    """Sessions where a compaction pass left evidence, and every query's count.

    Three shapes are probed separately because a reporter that prints only "none" cannot be audited:
    on 7.8.15 the store held 0 compaction parts, 0 compaction-agent messages and 0 partless messages
    for a session that had 150 failed prunes on record - so "a pass always writes a marker" is not
    true of every store, and the counts are what show which shape is missing.
    """
    rows = db("SELECT session_id, COUNT(*) FROM message "
              "WHERE json_extract(data,'$.agent')='compaction' GROUP BY session_id")
    agents = {r[0]: int(r[1]) for r in rows if len(r) > 1}
    rows = db("SELECT id, session_id, time_created, length(data) FROM part "
              "WHERE data LIKE '%\"type\":\"compaction\"%'")
    markers = {}
    for r in rows:
        if len(r) >= 4:
            markers.setdefault(r[1], []).append({"id": r[0], "time": int(r[2]), "bytes": int(r[3])})
    rows = db("SELECT m.session_id, COUNT(*) FROM message m LEFT JOIN part p ON p.message_id=m.id "
              "WHERE p.id IS NULL GROUP BY m.session_id")
    partless = {r[0]: int(r[1]) for r in rows if len(r) > 1}
    return agents, markers, partless


def q5duptext():
    """Repeated identical text parts - the re-appended user message symptom."""
    rows = db("SELECT session_id, COUNT(*) FROM part WHERE json_extract(data,'$.type')='text' "
              "GROUP BY session_id, substr(data,1,400) HAVING COUNT(*) > 1")
    return {r[0]: int(r[1]) for r in rows if len(r) > 1}


def q6descendants(ids):
    """Every descendant of the given sessions, as {descendant_id: parent_id}.

    Breadth-first rather than one level: `kilo session delete` cascades and its help does not say
    how deep, so archiving only direct children can leave a grandchild to be destroyed unarchived.
    """
    found = {}
    frontier = list(ids)
    seen = set(ids)
    while frontier:
        rows = db("SELECT id, parent_id FROM session WHERE parent_id IN (%s)"
                  % ",".join("'%s'" % i for i in frontier))
        frontier = []
        for r in rows:
            if len(r) < 2 or not r[0] or r[0] in seen:
                continue
            seen.add(r[0])
            found[r[0]] = r[1]
            frontier.append(r[0])
    return found


def q7growth(window=DEFAULT_WINDOW):
    """Context over time, per session: first/last and a straight-line rate.

    Severity is a snapshot, and a snapshot cannot tell a session that stopped growing from one that
    is 4 hours from the wall. tokens.input + tokens.cache.read of a call is the whole context it
    sent, so the sequence of those per call is the payload trajectory the store keeps. `window` must
    be the window the caller is reporting against, or `eta_h` disagrees with the stated sev%.
    """
    rows = db("SELECT session_id, time_created, "
              "coalesce(json_extract(data,'$.tokens.input'),0) "
              "+ coalesce(json_extract(data,'$.tokens.cache.read'),0) "
              "FROM message WHERE json_extract(data,'$.tokens.input') IS NOT NULL "
              "ORDER BY session_id, time_created")
    per = {}
    for r in rows:
        if len(r) < 3:
            continue
        try:
            ts, ctx = int(r[1] or 0), int(r[2] or 0)
        except ValueError:
            continue
        if ctx > 0:
            per.setdefault(r[0], []).append((ts, ctx))
    out = {}
    for sid, pts in per.items():
        pts.sort()
        t0, c0 = pts[0]
        t1, c1 = pts[-1]
        hours = (t1 - t0) / 3600000.0
        # a rate needs enough of a session to mean anything: two calls in twelve minutes extrapolate
        # to a confident-looking number that is only the first minute of the ramp
        rate = (c1 - c0) / hours if hours >= 0.25 and len(pts) >= 3 else None
        # the last hour is the trajectory that matters now; fall back to the whole session
        recent = [(t, c) for t, c in pts if t >= t1 - 3600000]
        rrate = None
        if len(recent) >= 3 and (recent[-1][0] - recent[0][0]) > 0:
            rh = (recent[-1][0] - recent[0][0]) / 3600000.0
            rrate = (recent[-1][1] - recent[0][1]) / rh if rh > 0 else None
        use = (rrate if rrate is not None else rate) if hours >= 0.5 else None
        out[sid] = {"first_ctx": c0, "last_ctx": c1, "hours": hours,
                    "points": len(pts), "rate_per_h": rate, "recent_per_h": rrate,
                    "rate_used": use,
                    "eta_h": ((window - c1) / use) if use and use > 0 else None}
    return out


def state_path():
    return os.path.join(os.path.expanduser("~"), ".local", "share", "kilo",
                        "context-doctor-state.json")


def load_state():
    """Returns (state, ok). `ok` is False when an existing file could not be parsed.

    The caller must not persist on a failed read: save_state truncates the file, so treating an
    unreadable state as empty would replace the accumulated marks - the only durable record that a
    session was ever stuck - with a partial view.
    """
    path = state_path()
    try:
        with open(path, encoding="utf-8") as f:
            j = json.load(f)
    except FileNotFoundError:
        return {}, True
    except Exception as e:
        print("WARNING: %s is unreadable (%s) - keeping it, not rewriting the warn marks" % (path, e))
        return {}, False
    if not isinstance(j, dict):
        print("WARNING: %s is not a JSON object - keeping it, not rewriting it" % path)
        return {}, False
    return j, True


def save_state(state):
    """Persist the warn high-water mark per session.

    The store carries no prune warnings and the log self-deletes, so a session that was stuck last
    week looks healthy once its log lines are gone. The union of what has ever been seen does not.
    Written to a temp file and renamed, so an interrupted run cannot truncate the record.
    """
    items = sorted(state.items(), key=lambda kv: kv[1].get("last_warn") or "", reverse=True)
    state = dict(items[:STATE_KEEP])
    path = state_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except OSError as e:
        print("WARNING: could not persist %s (%s) - the warn marks are unchanged on disk" % (path, e))
    return state


def merge_state(state, logs):
    for sid, e in logs.items():
        s = state.setdefault(sid, {})
        s["warns_seen"] = max(s.get("warns_seen", 0), e.get("warns", 0))
        firsts = [x for x in (s.get("first_warn"), e.get("first")) if x]
        lasts = [x for x in (s.get("last_warn"), e.get("last")) if x]
        s["first_warn"] = min(firsts) if firsts else None
        s["last_warn"] = max(lasts) if lasts else None
        s["max_size"] = max(s.get("max_size") or 0, e.get("max") or 0) or None
    return state


def caller_session(sessions):
    """The session running this command: the newest store session in this process's directory.

    The recency guard exists so the doctor never deletes the session it is being run from. Matching
    on a directory-and-recency identity says that directly, instead of treating every session
    touched in the last N minutes as if it were the caller.
    """
    try:
        cwd = os.path.normcase(os.path.realpath(os.getcwd()))
    except OSError:
        return None
    best = None
    for sid, s in sessions.items():
        d = s.get("directory") or ""
        if not d:
            continue
        try:
            rd = os.path.normcase(os.path.realpath(d))
        except OSError:
            continue
        if rd == cwd and (best is None or s["time_updated"] > sessions[best]["time_updated"]):
            best = sid
    return best


def store_totals():
    """Tokens the store still counts, so a cleanup's delta can be stated in the same run."""
    rows = db("SELECT COUNT(*), coalesce(SUM(tokens_input),0), coalesce(SUM(tokens_output),0), "
              "coalesce(SUM(tokens_cache_read),0) FROM session")
    if not rows or len(rows[0]) < 4:
        return {"sessions": 0, "input": 0, "output": 0, "cache": 0}
    r = rows[0]
    return {"sessions": int(r[0] or 0), "input": int(r[1] or 0),
            "output": int(r[2] or 0), "cache": int(r[3] or 0)}


def log_stats():
    """Best-effort log-derived counts. Absence here proves nothing - logs self-delete."""
    log = os.path.join(os.path.expanduser("~"), ".local", "share", "kilo", "log", "opencode.log")
    per = {}
    try:
        f = open(log, encoding="utf-8", errors="replace")
    except OSError:
        return per, None
    with f:
        for line in f:
            # must be a real WARN event: the log also records command text that can contain
            # this phrase (e.g. a grep for it), which is not an event
            if "level=WARN" not in line or 'message="payload still large after pruning"' not in line:
                continue
            sid = size = ts = None
            for tok in line.split():
                if tok.startswith("session.id="):
                    sid = tok.split("=", 1)[1]
                elif tok.startswith("size="):
                    try:
                        size = int(tok.split("=", 1)[1])
                    except ValueError:
                        size = None
                elif tok.startswith("timestamp="):
                    # key on the field name: the value is UTC (Z), and matching on shape alone
                    # (four leading digits) never matched this token, so warn times were always lost
                    ts = tok.split("=", 1)[1]
            if sid:
                e = per.setdefault(sid, {"warns": 0, "first": ts, "last": ts, "max": size})
                e["warns"] += 1
                e["last"] = ts or e["last"]
                if size:
                    e["max"] = size if e["max"] is None else max(e["max"], size)
    return per, log


def human(n):
    if n is None:
        return "-"
    try:
        if n >= 1e9:
            return "%.2fB" % (n / 1e9)
        if n >= 1e6:
            return "%.2fM" % (n / 1e6)
        if n >= 1e3:
            return "%.0fk" % (n / 1e3)
        return "%d" % round(n)
    except (TypeError, ValueError):
        return str(n)


def archive_token_totals(j):
    """Sum an exported session's per-message tokens.

    The one place that reads token data out of an archive object, so --account and --reindex-archives
    cannot drift apart. (A row written by retire() takes the store's session-row totals instead,
    which is the only copy that exists before the delete; the manifest says which writer made it.)
    """
    ti = to = tc = 0
    for m in (j.get("messages") or []):
        tk = ((m.get("info") or {}).get("tokens") or {}) if isinstance(m, dict) else {}
        ca = tk.get("cache") or {}
        ti += tk.get("input", 0) or 0
        to += tk.get("output", 0) or 0
        tc += ca.get("read", 0) or 0
    return ti, to, tc


def archive_totals(evidence_dir):
    """Tokens held in retired-session archives - usage the store no longer counts.

    Deleting a session deletes its messages and parts, so its tokens vanish from every store-based
    total. The archive is the only local record of them, which is why it is worth summing.
    """
    tot = {"sessions": 0, "input": 0, "output": 0, "cache": 0, "bytes": 0, "unreadable": 0}
    if not os.path.isdir(evidence_dir):
        return tot
    for name in sorted(os.listdir(evidence_dir)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(evidence_dir, name)
        try:
            size = os.path.getsize(path)
            with open(path, encoding="utf-8") as f:
                j = json.load(f)
        except Exception:
            tot["unreadable"] += 1
            continue
        tot["sessions"] += 1
        tot["bytes"] += size
        ti, to, tc = archive_token_totals(j if isinstance(j, dict) else {})
        tot["input"] += ti
        tot["output"] += to
        tot["cache"] += tc
    return tot


def account(args):
    """Price-free consumption view: tokens are the currency on a plan or an allowance."""
    rows = db("SELECT coalesce(s.directory,'(none)') AS proj, COUNT(DISTINCT s.id) AS sess, "
              "COUNT(*) AS calls, SUM(json_extract(m.data,'$.tokens.input')) AS inp, "
              "SUM(json_extract(m.data,'$.tokens.output')) AS outp, "
              "SUM(json_extract(m.data,'$.tokens.cache.read')) AS cr, "
              "MAX(json_extract(m.data,'$.tokens.input') + json_extract(m.data,'$.tokens.cache.read')) AS peak "
              "FROM message m JOIN session s ON s.id = m.session_id GROUP BY 1")
    data = []
    for r in rows:
        if len(r) < 7:
            continue
        inp, outp, cr, peak = (int(x or 0) for x in r[3:7])
        data.append({"proj": r[0], "sess": int(r[1] or 0), "calls": int(r[2] or 0),
                     "inp": inp, "outp": outp, "cr": cr, "peak": peak, "total": inp + outp + cr})
    if not data:
        print("no metered messages in the store")
        return
    data.sort(key=lambda d: -d["total"])
    grand = sum(d["total"] for d in data) or 1

    print("=== account: local record, in tokens (no price table needed) ===")
    print("%-44s %5s %6s %10s %9s %11s %11s %7s %7s %10s" % (
        "project", "sess", "calls", "input", "output", "cache_read", "total", "share", "worst%",
        "peak_call"))
    for d in data:
        print("%-44s %5d %6d %10s %9s %11s %11s %6.1f%% %7.1f%% %10s" % (
            tail(d["proj"], 44), d["sess"], d["calls"], human(d["inp"]), human(d["outp"]),
            human(d["cr"]), human(d["total"]), 100.0 * d["total"] / grand,
            100.0 * d["peak"] / args.window if args.window else 0.0, human(d["peak"])))
    ti = sum(d["inp"] for d in data)
    to = sum(d["outp"] for d in data)
    tc = sum(d["cr"] for d in data)
    print("%-44s %5d %6d %10s %9s %11s %11s %7.1f%%" % (
        "TOTAL", sum(d["sess"] for d in data), sum(d["calls"] for d in data),
        human(ti), human(to), human(tc), human(ti + to + tc), 100.0))
    print("shared pot: every session and project draws on the same allowance, so the account line is")
    print("the one that matters for exhaustion - not any single session's total.")
    hot = [d for d in data if args.window and 100.0 * d["peak"] / args.window >= SEVERITY_HINT]
    if hot:
        print("repeat offenders (a session has taken this project past %d%% of the window):" % SEVERITY_HINT)
        for d in hot:
            print("  %s  worst call %s tokens = %.0f%% of window over %d session(s)"
                  % (tail(d["proj"], 60), human(d["peak"]), 100.0 * d["peak"] / args.window, d["sess"]))
        print("  the regime re-enters per session: start a fresh one rather than resuming a long one.")

    print()
    print("=== burn rate: tokens per hour (input + cache read), last %d h ===" % args.hours)
    rows = db("SELECT strftime('%Y-%m-%dT%H', time_created/1000, 'unixepoch') AS h, "
              "SUM(json_extract(data,'$.tokens.input') + json_extract(data,'$.tokens.cache.read')) AS tot, "
              "COUNT(*) AS calls FROM message GROUP BY h ORDER BY h DESC LIMIT " + str(int(args.hours)))
    hours = []
    for r in rows:
        if len(r) >= 3:
            hours.append((r[0], int(r[1] or 0), int(r[2] or 0)))
    for h, tot, calls in hours:
        bar = "#" * min(48, int(48.0 * tot / max(1, max(x[1] for x in hours))))
        print("  %s  %10s  %4d calls  %s" % (h, human(tot), calls, bar))
    if hours:
        busiest = max(hours, key=lambda x: x[1])
        print("  busiest hour: %s with %s tokens in %d calls" % (busiest[0], human(busiest[1]), busiest[2]))

    a = archive_totals(args.evidence_dir)
    print()
    if a["sessions"]:
        atot = a["input"] + a["output"] + a["cache"]
        print("=== retired sessions: archived, NOT counted above ===")
        print("  %d archive(s), %.1f MB, holding %s in + %s out + %s cache read = %s tokens%s"
              % (a["sessions"], a["bytes"] / 1e6, human(a["input"]), human(a["output"]), human(a["cache"]),
                 human(atot), "  (%d unreadable)" % a["unreadable"] if a["unreadable"] else ""))
        print("  the store's record is incomplete by that much; the provider remains the authority on")
        print("  what was actually spent, and on what is left.")
    else:
        print("=== no retired-session archives in %s ===" % args.evidence_dir)
        print("  (after a cleanup, totals here drop because deleted sessions stop being counted -")
        print("   that is the record shrinking, not consumption falling)")



def report(args):
    sessions = q1sessions()
    usage = q2usage()
    growth = q7growth(args.window)
    parts = q3parts()
    agents, markers, partless = q4compaction()
    dups = q5duptext()
    logs, logpath = log_stats()
    state, state_ok = load_state()
    if state_ok:
        state = save_state(merge_state(state, logs))
    now = time.time() * 1000

    rows = []
    for sid, s in sessions.items():
        u = usage.get(sid, {})
        g = growth.get(sid, {})
        lg = logs.get(sid, {})
        hist = state.get(sid, {})
        peak = u.get("peak_ctx", 0)
        sev = 100.0 * peak / args.window if args.window else 0.0
        rows.append({
            "id": sid, "sev": sev, "peak": peak, "calls": u.get("calls", 0),
            "cold": u.get("cold_sends", 0), "uncached": u.get("uncached_in", 0),
            "warns": max(lg.get("warns", 0), hist.get("warns_seen", 0)),
            "warns_log": lg.get("warns", 0), "warns_hist": hist.get("warns_seen", 0),
            # unioned, not preferred: a live log with a smaller max or a later first warning must not
            # overwrite the persisted high-water mark, or pay%/"stuck since" move backwards
            "wmax": max(lg.get("max") or 0, hist.get("max_size") or 0) or None,
            "first_warn": min([x for x in (lg.get("first"), hist.get("first_warn")) if x] or [None]),
            "parts": parts.get(sid, 0), "compact_msgs": agents.get(sid, 0),
            "markers": len(markers.get(sid, [])), "dups": dups.get(sid, 0),
            "partless": partless.get(sid, 0),
            "first_ctx": g.get("first_ctx", 0), "last_ctx": g.get("last_ctx", 0),
            "hours": g.get("hours", 0.0), "points": g.get("points", 0),
            "rate": g.get("rate_used"), "rate_all": g.get("rate_per_h"),
            "rate_1h": g.get("recent_per_h"), "eta": g.get("eta_h"),
            "dir": os.path.basename(s["directory"].replace("\\", "/").rstrip("/")),
            "title": s["title"][:34], "model": s["model"],
            "age_min": (now - s["time_updated"]) / 60000.0 if s["time_updated"] else 1e9,
            "compacting": s["time_compacting"],
        })

    rows.sort(key=lambda r: -r["sev"])
    # selection is computed over every session, never over what the display filter left: a filtered
    # report that also narrows --retire-stuck would silently retire a different set than it showed.
    stuck = [r for r in rows if r["sev"] >= SEVERITY_WARN or r["warns"] >= STUCK_MIN_WARNS]
    harmful = [r for r in rows if r["sev"] >= SEVERITY_HINT]
    risky = [r for r in harmful if r not in stuck and (r["rate"] or 0) > 0 and r["eta"]]

    if args.json:
        keep = ("id", "sev", "peak", "calls", "cold", "warns", "warns_log", "warns_hist",
                "wmax", "parts", "compact_msgs", "markers", "partless", "dups", "first_ctx",
                "last_ctx", "hours", "points", "rate", "rate_all", "rate_1h", "eta", "dir",
                "title", "model", "age_min")
        print(json.dumps([{k: r[k] for k in keep} for r in rows], indent=1))
        return rows, sessions

    disp = [r for r in rows if r["sev"] >= args.min_severity] if args.min_severity else list(rows)
    if args.top:
        disp = disp[:args.top]

    print("kilo_doctor  store=%s" % (db("SELECT 1") is not None and "ok" or "?"))
    print("window assumed: %d tokens | sessions: %d%s | log stats: %s"
          % (args.window, len(rows),
             "" if len(disp) == len(rows) else " (displaying %d)" % len(disp),
             logpath if logs else "unavailable (logs may have been deleted)"))
    print()
    print("%-30s %6s %8s %6s %5s %6s %11s %6s %6s %5s" % (
        "session", "sev%", "peak_ctx", "calls", "cold", "warns", "uncached_in", "parts", "cmsg", "dup"))
    for r in disp:
        print("%-30s %6.1f %8s %6d %5d %6d %11s %6d %6d %5d" % (
            r["id"], r["sev"], human(r["peak"]), r["calls"], r["cold"], r["warns"],
            human(r["uncached"]), r["parts"], r["compact_msgs"], r["dups"]))
    print()
    print("legend: sev% = peak context / window | cold = sends at <10% cache hit (prune rewrote the")
    print("        prefix) | dup = repeated identical text parts | warns = log WARN events plus a")
    print("        persisted high-water mark, so a rotated log cannot hide a known-stuck session")
    print("        (display filters: --min-severity, --top; neither changes what --retire-stuck picks)")
    print()

    if stuck:
        print("DEGRADED (%d) - do not resume these; a fresh session in the same folder costs nothing:" % len(stuck))
        for r in stuck:
            where = r["dir"] or "(no dir)"
            extra = ""
            if r["eta"]:
                extra += "  ~%.1fh to window at the observed rate" % r["eta"]
            if r["first_warn"]:
                extra += "  stuck since %s (UTC)" % r["first_warn"]
            if not r["warns_log"] and r["warns_hist"]:
                extra += "  (warn mark from a log that is gone)"
            print("  %s  %.0f%% of window  %s  last used %.0f min ago%s"
                  % (r["id"], r["sev"], where, r["age_min"], extra))
    else:
        print("no degraded sessions")
    print()
    if risky:
        print("RISING (%d) - below the degraded threshold but the payload is still growing:" % len(risky))
        for r in risky:
            print("  %s  %.0f%% of window  +%s tok/h  ~%.1fh to window  %s"
                  % (r["id"], r["sev"], human(r["rate"]), r["eta"], r["dir"] or "(no dir)"))
        print("  the prune path cannot win from about %d%%; retiring an idle session here costs one" % SEVERITY_HINT)
        print("  archive, and waiting costs the re-billed sends (see the cold column).")
        print()

    trend = rows if args.predict else [r for r in rows if r["id"] in {h["id"] for h in harmful}]
    trend = [r for r in trend if r["calls"]]
    if trend:
        print("trend (the store's own per-call usage; rate = the last hour when the session has three")
        print("       calls in it, else a straight line through the whole session; pay% = est payload")
        print("       at its last recorded warning / %d B; '-' = too little data to extrapolate):" % PRUNE_BYTES_EST)
        print("%-30s %9s %9s %6s %10s %7s %11s %6s" % (
            "session", "first_ctx", "peak_ctx", "hours", "tok/h", "eta_h", "last_warn_B", "pay%"))
        for r in sorted(trend, key=lambda r: (r["eta"] is None, r["eta"] or 0)):
            pay = "%.0f" % (100.0 * r["wmax"] / PRUNE_BYTES_EST) if r["wmax"] else "-"
            # show exactly the rate the eta came from: a rate that was declined as too short-lived is
            # a "-", not a number with no eta beside it
            used = r["rate"]
            rate = "-" if not (used or 0) > 0 else "%s%s" % (human(used), " (1h)" if r["rate_1h"] == used else "")
            print("%-30s %9s %9s %6.1f %10s %7s %11s %6s" % (
                r["id"], human(r["first_ctx"]), human(r["peak"]), r["hours"], rate,
                "%.1f" % r["eta"] if r["eta"] else "-",
                "%d" % r["wmax"] if r["wmax"] else "-", pay))
        print()
    hist_only = [r["id"] for r in rows if not r["warns_log"] and r["warns_hist"]]
    if hist_only:
        print("warns known only from the persisted mark (this log no longer holds them): %s"
              % ", ".join(hist_only))
        print()
    print("marker queries: compaction-parts=%d compaction-agent-msgs=%d partless-msgs=%d "
          "(a pass that left no marker is possible; these counts make 'none' auditable)"
          % (sum(len(v) for v in markers.values()), sum(agents.values()), sum(partless.values())))
    if markers:
        print("compaction markers on record:")
        for sid, ms in markers.items():
            for m in ms:
                print("  %s  %s  %d bytes" % (sid, time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(m["time"] / 1000)), m["bytes"]))
    else:
        print("compaction markers on record: none")
    for r in rows:
        if r["compacting"]:
            print("NOTE: %s has time_compacting set - a compaction may be in flight" % r["id"])
    return rows, sessions


def iso_local(secs):
    """Local time with an explicit offset - a bare clock is not comparable with a log's UTC stamp."""
    t = time.localtime(secs)
    return time.strftime("%Y-%m-%dT%H:%M:%S", t) + time.strftime("%z", t)


def manifest_path(evidence_dir):
    return os.path.join(evidence_dir, "index.jsonl")


def manifest_upsert(evidence_dir, entries):
    """One row per session: a later run's row supersedes the earlier one for the same id.

    A dry run writes rows with deleted=false; the following --apply must replace them rather than
    append, or --list-archives counts the same session's tokens twice. The write is atomic
    (temp + os.replace) so an interrupted run cannot leave a half-written index.
    """
    entries = [e for e in entries if isinstance(e, dict) and e.get("session_id")]
    if not entries:
        return 0
    superseded = {e["session_id"] for e in entries}
    keep = [e for e in read_manifest(evidence_dir) if e.get("session_id") not in superseded]
    path = manifest_path(evidence_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for e in keep + entries:
            f.write(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n")
    os.replace(tmp, path)
    return len(keep) + len(entries)


def read_manifest(evidence_dir):
    entries = []
    path = manifest_path(evidence_dir)
    if not os.path.isfile(path):
        return entries
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError as e:
                print("  manifest line %d unparseable (%s): %s" % (n, e, line[:70]))
                continue
            if not isinstance(row, dict):
                print("  manifest line %d is not an object (%s) - skipped" % (n, type(row).__name__))
                continue
            # the file is user-visible and may have been hand-edited: consumers call .get() and int()
            # on these fields, so coerce here rather than raising three functions later
            for k in ("messages", "bytes", "tokens_input", "tokens_output", "tokens_cache_read",
                      "peak_ctx", "warns", "last_warn_bytes"):
                if k in row and row[k] is not None:
                    try:
                        row[k] = int(row[k])
                    except (TypeError, ValueError):
                        print("  manifest line %d: %s=%r is not a number - ignored" % (n, k, row[k]))
                        row[k] = None
            entries.append(row)
    return entries


def list_archives(args):
    """The archive is the only surviving record of a retired session's tokens, so it needs an index."""
    evid = args.evidence_dir
    entries = read_manifest(evid)
    files = sorted(f for f in os.listdir(evid) if f.endswith(".json")) if os.path.isdir(evid) else []
    known = {e.get("session_id") for e in entries}
    print("=== retired-session archives: %d file(s), %d manifest row(s) in %s ==="
          % (len(files), len(entries), evid))
    if entries:
        print("%-32s %-6s %6s %10s %-22s %9s %9s %9s" % (
            "session", "role", "msgs", "bytes", "archived_at", "in", "out", "cache_read"))
        for e in entries:
            print("%-32s %-6s %6s %10s %-22s %9s %9s %9s" % (
                e.get("session_id", ""), e.get("role", ""), e.get("messages", ""),
                human(e.get("bytes", 0)), e.get("archived_at", ""),
                human(e.get("tokens_input", 0)), human(e.get("tokens_output", 0)),
                human(e.get("tokens_cache_read", 0))))
        ti = sum(e.get("tokens_input", 0) or 0 for e in entries)
        to = sum(e.get("tokens_output", 0) or 0 for e in entries)
        tc = sum(e.get("tokens_cache_read", 0) or 0 for e in entries)
        print("%-32s %-6s %6s %10s %-22s %9s %9s %9s" % (
            "TOTAL (%d rows)" % len(entries), "", "", "",
            "", human(ti), human(to), human(tc)))
    else:
        print("no manifest yet - archives written by an older version have no index row;")
        print("--account still sums them from the files, and --verify-archives checks each one.")
    missing = [e.get("session_id") for e in entries
               if e.get("session_id") and (e["session_id"] + ".json") not in files]
    extra = [f for f in files if f[:-5] not in known]
    if missing:
        print("manifest rows with no file: %s%s" % (brief(missing), ""))
    if extra:
        print("files with no manifest row (pre-manifest): %s" % brief(extra))
        print("  run --reindex-archives to give them rows built from the files themselves")
    print("restore: kilo import %s\\<session>.json" % evid)


def brief(items, n=6):
    if len(items) <= n:
        return ", ".join(items)
    return "%s ... (+%d more)" % (", ".join(items[:n]), len(items) - n)


def tail(s, n):
    """Keep the informative end of a path: a leading wipe makes two projects look alike."""
    return s if len(s) <= n else "..." + s[-(n - 3):]


def verify_archives(args):
    evid = args.evidence_dir
    files = sorted(f for f in os.listdir(evid) if f.endswith(".json")) if os.path.isdir(evid) else []
    by_id = {e.get("session_id"): e for e in read_manifest(evid)}
    n = bad = tot = 0
    for f in files:
        p = os.path.join(evid, f)
        sid = f[:-5]
        n += 1
        try:
            with open(p, encoding="utf-8") as fh:
                j = json.load(fh)
            if not isinstance(j, dict):
                print("  %s: not a session export (%s)" % (f, type(j).__name__))
                bad += 1
                continue
            msgs = len(j.get("messages") or [])
        except Exception as e:
            print("  %s: UNREADABLE %s" % (f, e))
            bad += 1
            continue
        tot += msgs
        m = by_id.get(sid, {}) if isinstance(by_id.get(sid), dict) else {}
        note = ""
        if not msgs:
            note = "  <-- 0 messages: not a usable archive"
            bad += 1
        elif m.get("messages") is not None and m["messages"] != msgs:
            note = "  <-- manifest says %s messages" % m["messages"]
            bad += 1
        print("  %-36s %5d messages  %10d bytes%s" % (sid, msgs, os.path.getsize(p), note))
    print("verified %d archive(s), %d message(s) total, %d problem(s)" % (n, tot, bad))


def reindex_archives(args):
    """Give pre-manifest archives a row, with tokens summed from the files themselves.

    Retiring deletes the store's copy of a session's tokens, so an archive written before the
    manifest existed is the only record of them and is otherwise not represented anywhere.
    """
    evid = args.evidence_dir
    files = sorted(f for f in os.listdir(evid) if f.endswith(".json")) if os.path.isdir(evid) else []
    known = {e.get("session_id") for e in read_manifest(evid)}
    add = []
    for f in files:
        sid = f[:-5]
        if sid in known:
            continue
        p = os.path.join(evid, f)
        try:
            with open(p, encoding="utf-8") as fh:
                j = json.load(fh)
        except Exception as e:
            print("  %s: unreadable (%s) - not indexed" % (f, e))
            continue
        if not isinstance(j, dict):
            print("  %s: not a session export (%s) - not indexed" % (f, type(j).__name__))
            continue
        info = j.get("info") or {}
        if not isinstance(info, dict):
            info = {}
        ti, to, tc = archive_token_totals(j)
        model = info.get("model")
        msgs = len(j.get("messages") or [])
        # the export renames the store's column: session.parent_id arrives as info.parentID, so a
        # lookup of "parent_id" here silently finds nothing and every row looks parentless
        parent = info.get("parentID") or info.get("parent_id") or None
        add.append({
            "session_id": info.get("id") or sid,
            "role": "descendant" if parent else "target", "parent": parent,
            "title": info.get("title", ""), "directory": info.get("directory", ""),
            "model": model.get("id") if isinstance(model, dict) else (model or ""),
            "archived_at": iso_local(os.path.getmtime(p)), "messages": msgs,
            "bytes": os.path.getsize(p), "tokens_input": ti, "tokens_output": to,
            "tokens_cache_read": tc, "peak_ctx": None, "warns": None, "last_warn_bytes": None,
            "sev_pct": None, "reason": "reindexed from the archive file", "deleted": None,
        })
        print("  index %s  %4d msg  in=%s out=%s cache=%s"
              % (sid, msgs, human(ti), human(to), human(tc)))
    manifest_upsert(evid, add)
    print("indexed %d archive(s); manifest now holds %d row(s)" % (len(add), len(known) + len(add)))


def retire(targets, args, sessions, explicit, rows=None):
    stats = {r["id"]: r for r in (rows or [])}
    evid = args.evidence_dir
    os.makedirs(evid, exist_ok=True)
    now = time.time() * 1000
    me = caller_session(sessions)

    # the caller is excluded before any recency test: a session running this command must not be
    # retired even when it is older than --active-window (or when the window is 0), and --force is
    # about idle-but-recent sessions, never about the one you are typing in
    if me and me in targets:
        age = (now - sessions[me]["time_updated"]) / 60000.0
        print("  refusing %s - this is the session running this command (%.2f min old)" % (me, age))
        targets = [t for t in targets if t != me]
    elif me is None:
        print("  WARNING: no store session matches this directory, so the caller could not be")
        print("           identified - check the target list before using --apply")

    recent = {s: (now - sessions[s]["time_updated"]) / 60000.0
              for s in targets if sessions[s]["time_updated"]
              and (now - sessions[s]["time_updated"]) / 60000.0 < args.active_window}
    drop = []
    for sid, age in sorted(recent.items(), key=lambda kv: kv[1]):
        if args.force:
            print("  %s was updated %.1f min ago - retiring anyway (--force)" % (sid, age))
        else:
            print("  skipping %s - updated %.1f min ago, inside --active-window %.1f"
                  % (sid, age, args.active_window))
            print("     the caller's session is %s; if %s is idle, re-run with"
                  " --active-window %.0f or --force"
                  % (me or "unknown (no session in this directory)", sid, max(1.0, age - 0.5)))
            drop.append(sid)
    targets = [t for t in targets if t not in drop]
    if not targets:
        # stdout, not stderr: a native stderr line is wrapped by PowerShell as NativeCommandError
        # noise that reads like a crash rather than the guard doing its job
        print("nothing left to retire after skipping recent sessions%s"
              % ("" if args.force else " (--force retires them)"))
        sys.exit(1)

    before = store_totals()
    print("store before: %d session(s)  input=%s output=%s cache_read=%s"
          % (before["sessions"], human(before["input"]), human(before["output"]), human(before["cache"])))
    for sid in targets:
        s = sessions[sid]
        print("  target %s session row: input=%s output=%s cache_read=%s"
              % (sid, human(s["tokens_input"]), human(s["tokens_output"]),
                 human(s["tokens_cache_read"])))

    children = q6descendants(targets)
    if children:
        print("descendants (archived with the target, then kept - the CLI cascades on delete, its")
        print("depth is undocumented, so every level is walked here):")
        for c, p in sorted(children.items(), key=lambda kv: kv[1] or ""):
            print("  %s  parent=%s  %s" % (c, p, (sessions.get(c, {}).get("title") or "")[:44]))
    todo = list(targets) + sorted(children)
    print("archiving %d session(s): %d target(s) + %d descendant(s)"
          % (len(todo), len(targets), len(children)))
    ok = True
    manifest = []
    for sid in todo:
        out_path = os.path.join(evid, sid + ".json")
        r = subprocess.run([KILO, "export", sid], capture_output=True)
        raw = r.stdout or b""
        if r.returncode != 0 or not raw:
            print("  %s EXPORT FAILED rc=%s" % (sid, r.returncode))
            ok = False
            continue
        with open(out_path, "wb") as f:
            f.write(raw)
        try:
            j = json.loads(open(out_path, encoding="utf-8").read())
        except Exception as e:
            print("  %s export unreadable: %s" % (sid, e))
            ok = False
            continue
        if not isinstance(j, dict):
            print("  %s export is not an object - refusing" % sid)
            ok = False
            continue
        n = len(j.get("messages", []))
        st = stats.get(sid, {})
        s = sessions.get(sid, {})
        # tokens come from the store's session row, which is the only place they exist before the
        # delete; an archive written without the store uses archive_token_totals() instead
        manifest.append({
            "session_id": sid, "role": "target" if sid in targets else "descendant",
            "parent": children.get(sid),
            "title": s.get("title", ""), "directory": s.get("directory", ""),
            "model": s.get("model", ""), "archived_at": iso_local(time.time()),
            "messages": n, "bytes": os.path.getsize(out_path),
            "tokens_input": s.get("tokens_input", 0), "tokens_output": s.get("tokens_output", 0),
            "tokens_cache_read": s.get("tokens_cache_read", 0),
            "peak_ctx": st.get("peak", 0), "warns": st.get("warns", 0),
            "last_warn_bytes": st.get("wmax"), "sev_pct": round(st.get("sev", 0.0), 1),
            "reason": "degraded" if st.get("sev", 0) >= SEVERITY_WARN or st.get("warns", 0) >= STUCK_MIN_WARNS
                      else ("explicit" if explicit else "retire-stuck"),
            "deleted": False,
        })
        print("  %s ok messages=%-4d bytes=%d" % (sid, n, os.path.getsize(out_path)))
        if n == 0:
            ok = False
    if not ok:
        raise SystemExit("an archive failed to validate - nothing deleted")
    print("all archives validated in %s" % evid)

    if not args.apply:
        manifest_upsert(evid, manifest)
        print("manifest: %d row(s) written (deleted=false) to %s" % (len(manifest), manifest_path(evid)))
        print("\ndry run: nothing deleted. Re-run with --apply (its rows supersede these).")
        return

    print("\ndeleting targets (descendants are kept):")
    for sid in targets:
        r = subprocess.run([KILO, "session", "delete", sid], capture_output=True)
        print("  %s rc=%s" % (sid, r.returncode))
    print("\nvalidating:")
    after_rows = q1sessions()
    bad = [s for s in targets if s in after_rows]
    survivors = [c for c in children if c in after_rows]
    orphans = [c for c in children if c not in after_rows]
    print("  targets still present: %s" % (bad if bad else "none - all retired"))
    print("  descendants kept: %d" % len(survivors))
    if orphans:
        print("  descendants REMOVED BY CASCADE: %d (archives exist, see note in the skill)" % len(orphans))
        for c in orphans:
            print("    %s" % c)
    # the del of a row is a fact about the store, not about the flag: only a target that is gone
    # counts as deleted, so a failed `session delete` cannot leave the index claiming a removal
    print("  archive files: %d" % len([f for f in os.listdir(evid) if f.endswith(".json")]))
    for e in manifest:
        e["deleted"] = e["role"] == "target" and e["session_id"] not in after_rows
        e["cascade_removed"] = e["role"] != "target" and e["session_id"] not in after_rows
    manifest_upsert(evid, manifest)
    print("manifest: %d row(s) written to %s (deleted=%d, cascade_removed=%d)"
          % (len(manifest), manifest_path(evid),
             sum(1 for e in manifest if e["deleted"]), sum(1 for e in manifest if e["cascade_removed"])))
    after = store_totals()
    print("store after: %d session(s)  input=%s output=%s cache_read=%s"
          % (after["sessions"], human(after["input"]), human(after["output"]), human(after["cache"])))
    print("store delta: input=%+d output=%+d cache_read=%+d  (record shrinking, not consumption falling;"
          % (after["input"] - before["input"], after["output"] - before["output"],
             after["cache"] - before["cache"]))
    print("the pre-cleanup numbers above and the archives are the only local record of those tokens)")


def main():
    global KILO
    ap = argparse.ArgumentParser(description="detect and retire context-stuck Kilo sessions")
    ap.add_argument("--window", type=int, default=DEFAULT_WINDOW)
    ap.add_argument("--min-severity", type=float, default=0,
                    help="display filter only; it does not narrow --retire-stuck")
    ap.add_argument("--top", type=int, default=0, help="display at most this many rows")
    ap.add_argument("--retire-stuck", action="store_true",
                    help="retire every degraded session, plus any session still rising past 34%% of window")
    ap.add_argument("--retire", nargs="*", default=[], help="explicit session id(s) to retire")
    ap.add_argument("--apply", action="store_true", help="actually delete after archiving")
    ap.add_argument("--active-window", type=float, default=10.0,
                    help="refuse to retire a session touched within this many minutes")
    ap.add_argument("--force", action="store_true",
                    help="retire recent sessions anyway - never the session running this command")
    ap.add_argument("--predict", action="store_true",
                    help="show the trajectory for every session, not only the ones already past 34%%")
    ap.add_argument("--json", action="store_true", help="machine-readable rows instead of tables")
    ap.add_argument("--list-archives", action="store_true",
                    help="read the retired-session manifest and reconcile it with the files")
    ap.add_argument("--verify-archives", action="store_true",
                    help="re-parse every archive and check it against the manifest")
    ap.add_argument("--reindex-archives", action="store_true",
                    help="add manifest rows for archives written before the manifest existed")
    ap.add_argument("--evidence-dir", default=None)
    ap.add_argument("--account", action="store_true",
                    help="price-free consumption view: tokens by project, burn rate by hour, archived total")
    ap.add_argument("--hours", type=int, default=24, help="burn-rate window in hours (with --account)")
    args = ap.parse_args()
    args.evidence_dir = args.evidence_dir or os.path.join(
        os.path.expanduser("~"), ".local", "share", "kilo", "retired-sessions")

    if args.list_archives:
        list_archives(args)
        return
    if args.verify_archives:
        verify_archives(args)
        return
    if args.reindex_archives:
        reindex_archives(args)
        return

    KILO = find_kilo()

    if args.account:
        account(args)
        return

    rows, sessions = report(args)
    if not (args.retire_stuck or args.retire):
        if args.apply:
            print("--apply without --retire/--retire-stuck: nothing to do")
        return
    targets = list(args.retire)
    explicit = bool(args.retire)
    if args.retire_stuck:
        # every row, not just the displayed ones: a filter must not change the selection
        targets += [r["id"] for r in rows if r["sev"] >= SEVERITY_WARN or r["warns"] >= STUCK_MIN_WARNS
                    or r["eta"] is not None and r["sev"] >= SEVERITY_HINT]
    targets = sorted(set(t for t in targets if t in sessions))
    if not targets:
        print("no matching sessions")
        sys.exit(1)
    print("\n=== retire: %s ===" % ", ".join(targets))
    retire(targets, args, sessions, explicit, rows)


if __name__ == "__main__":
    main()
