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

Safety model:
  * detection and reporting are read-only; nothing is written outside the evidence directory
  * retirement is archive-first: every session (and every child session) is exported to JSON and
    validated before any delete happens; one failed export aborts the whole run
  * a session modified within --active-window minutes is never retired (that is you, right now)
  * deletion requires --apply; the default is a dry run

Usage:
    python kilo_doctor.py                       # report every session
    python kilo_doctor.py --min-severity 60     # only the degraded ones
    python kilo_doctor.py --retire-stuck --apply
    python kilo_doctor.py --retire ses_abc... --apply
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
              "tokens_output, tokens_cache_read, time_updated, coalesce(time_compacting,0) "
              "FROM session ORDER BY time_updated DESC")
    out = {}
    for r in rows:
        if len(r) < 8:
            continue
        out[r[0]] = {
            "id": r[0], "directory": r[1], "title": r[2],
            "tokens_input": int(r[3] or 0), "tokens_output": int(r[4] or 0),
            "tokens_cache_read": int(r[5] or 0), "time_updated": int(r[6] or 0),
            "time_compacting": int(r[7] or 0),
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
    """Sessions where a compaction pass ran, and how many empty summary messages it left."""
    rows = db("SELECT session_id, COUNT(*) FROM message "
              "WHERE json_extract(data,'$.agent')='compaction' GROUP BY session_id")
    agents = {r[0]: int(r[1]) for r in rows if len(r) > 1}
    rows = db("SELECT id, session_id, time_created, length(data) FROM part "
              "WHERE data LIKE '%\"type\":\"compaction\"%'")
    markers = {}
    for r in rows:
        if len(r) >= 4:
            markers.setdefault(r[1], []).append({"id": r[0], "time": int(r[2]), "bytes": int(r[3])})
    return agents, markers


def q5duptext():
    """Repeated identical text parts - the re-appended user message symptom."""
    rows = db("SELECT session_id, COUNT(*) FROM part WHERE json_extract(data,'$.type')='text' "
              "GROUP BY session_id, substr(data,1,400) HAVING COUNT(*) > 1")
    return {r[0]: int(r[1]) for r in rows if len(r) > 1}


def q6children(ids):
    if not ids:
        return []
    rows = db("SELECT id FROM session WHERE parent_id IN (%s)"
              % ",".join("'%s'" % i for i in ids))
    return [r[0] for r in rows if r]


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
                elif tok[:4].isdigit() and "T" in tok and tok.endswith("Z"):
                    ts = tok
            if sid:
                e = per.setdefault(sid, {"warns": 0, "first": ts, "last": ts, "min": size, "max": size})
                e["warns"] += 1
                e["last"] = ts or e["last"]
                if size:
                    e["min"] = size if e["min"] is None else min(e["min"], size)
                    e["max"] = size if e["max"] is None else max(e["max"], size)
    return per, log


def human(n):
    if n >= 1e9:
        return "%.2fB" % (n / 1e9)
    if n >= 1e6:
        return "%.2fM" % (n / 1e6)
    if n >= 1e3:
        return "%.0fk" % (n / 1e3)
    return str(n)


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
        for m in j.get("messages", []):
            tk = ((m.get("info") or {}).get("tokens") or {})
            ca = tk.get("cache") or {}
            tot["input"] += tk.get("input", 0) or 0
            tot["output"] += tk.get("output", 0) or 0
            tot["cache"] += ca.get("read", 0) or 0
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
    print("%-44s %5s %6s %10s %9s %11s %11s %8s %10s" % (
        "project", "sess", "calls", "input", "output", "cache_read", "total", "share", "peak_call"))
    for d in data:
        print("%-44s %5d %6d %10s %9s %11s %11s %7.1f%% %10s" % (
            d["proj"][-44:], d["sess"], d["calls"], human(d["inp"]), human(d["outp"]),
            human(d["cr"]), human(d["total"]), 100.0 * d["total"] / grand, human(d["peak"])))
    ti = sum(d["inp"] for d in data)
    to = sum(d["outp"] for d in data)
    tc = sum(d["cr"] for d in data)
    print("%-44s %5d %6d %10s %9s %11s %11s %7.1f%%" % (
        "TOTAL", sum(d["sess"] for d in data), sum(d["calls"] for d in data),
        human(ti), human(to), human(tc), human(ti + to + tc), 100.0))
    print("shared pot: every session and project draws on the same allowance, so the account line is")
    print("the one that matters for exhaustion - not any single session's total.")

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
    parts = q3parts()
    agents, markers = q4compaction()
    dups = q5duptext()
    logs, logpath = log_stats()
    now = time.time() * 1000

    rows = []
    for sid, s in sessions.items():
        u = usage.get(sid, {})
        lg = logs.get(sid, {})
        peak = u.get("peak_ctx", 0)
        sev = 100.0 * peak / args.window if args.window else 0.0
        rows.append({
            "id": sid, "sev": sev, "peak": peak, "calls": u.get("calls", 0),
            "cold": u.get("cold_sends", 0), "uncached": u.get("uncached_in", 0),
            "warns": lg.get("warns", 0), "wmin": lg.get("min"), "wmax": lg.get("max"),
            "parts": parts.get(sid, 0), "compact_msgs": agents.get(sid, 0),
            "markers": len(markers.get(sid, [])), "dups": dups.get(sid, 0),
            "dir": os.path.basename(s["directory"].replace("\\", "/").rstrip("/")),
            "title": s["title"][:34], "updated": s["time_updated"],
            "age_min": (now - s["time_updated"]) / 60000.0 if s["time_updated"] else 1e9,
            "compacting": s["time_compacting"],
        })

    rows.sort(key=lambda r: -r["sev"])
    if args.min_severity:
        rows = [r for r in rows if r["sev"] >= args.min_severity]

    print("kilo_doctor  store=%s" % (db("SELECT 1") is not None and "ok" or "?"))
    print("window assumed: %d tokens | sessions: %d | log stats: %s"
          % (args.window, len(rows), logpath if logs else "unavailable (logs may have been deleted)"))
    print()
    print("%-30s %6s %8s %6s %5s %6s %11s %6s %6s %5s" % (
        "session", "sev%", "peak_ctx", "calls", "cold", "warns", "uncached_in", "parts", "cmsg", "dup"))
    for r in rows:
        print("%-30s %6.1f %8s %6d %5d %6d %11s %6d %6d %5d" % (
            r["id"], r["sev"], human(r["peak"]), r["calls"], r["cold"], r["warns"],
            human(r["uncached"]), r["parts"], r["compact_msgs"], r["dups"]))
    print()
    print("legend: sev% = peak context / window | cold = sends at <10% cache hit (prune rewrote the")
    print("        prefix) | cmsg = agent=compaction messages | dup = repeated identical text parts")
    print()

    stuck = [r for r in rows if r["sev"] >= SEVERITY_WARN or r["warns"] >= STUCK_MIN_WARNS]
    if stuck:
        print("DEGRADED (%d) - do not resume these; a fresh session in the same folder costs nothing:" % len(stuck))
        for r in stuck:
            where = r["dir"] or "(no dir)"
            print("  %s  %.0f%% of window  %s  last used %.0f min ago" % (r["id"], r["sev"], where, r["age_min"]))
    else:
        print("no degraded sessions")
    print()
    if markers:
        print("compaction markers on record (a pass always writes one):")
        for sid, ms in markers.items():
            for m in ms:
                print("  %s  %s  %d bytes" % (sid, time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(m["time"] / 1000)), m["bytes"]))
    else:
        print("compaction markers on record: none")
    for r in rows:
        if r["compacting"]:
            print("NOTE: %s has time_compacting set - a compaction may be in flight" % r["id"])
    return rows, sessions


def retire(targets, args, sessions, explicit):
    evid = args.evidence_dir
    os.makedirs(evid, exist_ok=True)
    now = time.time() * 1000
    active = {s: (now - sessions[s]["time_updated"]) / 60000.0
              for s in targets if sessions[s]["time_updated"]
              and (now - sessions[s]["time_updated"]) / 60000.0 < args.active_window}
    for sid, age in active.items():
        if explicit:
            raise SystemExit("refusing: %s was active %.1f min ago and was named explicitly"
                             % (sid, age))
        print("  skipping %s - active %.1f min ago (live session)" % (sid, age))
    targets = [t for t in targets if t not in active]
    if not targets:
        raise SystemExit("nothing left to retire after skipping active sessions")
    children = q6children(targets)
    todo = list(targets) + children
    print("archiving %d session(s): %d target(s) + %d child(ren)" % (len(todo), len(targets), len(children)))
    ok = True
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
        n = len(j.get("messages", []))
        print("  %s ok messages=%-4d bytes=%d" % (sid, n, os.path.getsize(out_path)))
        if n == 0:
            ok = False
    if not ok:
        raise SystemExit("an archive failed to validate - nothing deleted")
    print("all archives validated in %s" % evid)

    if not args.apply:
        print("\ndry run: nothing deleted. Re-run with --apply.")
        return
    print("\ndeleting targets (children are kept):")
    for sid in targets:
        r = subprocess.run([KILO, "session", "delete", sid], capture_output=True)
        print("  %s rc=%s" % (sid, r.returncode))
    print("\nvalidating:")
    after = q1sessions()
    bad = [s for s in targets if s in after]
    survivors = [c for c in children if c in after]
    orphans = [c for c in children if c not in after]
    print("  targets still present: %s" % (bad if bad else "none - all retired"))
    print("  children kept: %d" % len(survivors))
    if orphans:
        print("  children REMOVED BY CASCADE: %d (archives exist, see note in the skill)" % len(orphans))
        for c in orphans:
            print("    %s" % c)
    print("  archive files: %d" % len([f for f in os.listdir(evid) if f.endswith(".json")]))


def main():
    global KILO
    ap = argparse.ArgumentParser(description="detect and retire context-stuck Kilo sessions")
    ap.add_argument("--window", type=int, default=DEFAULT_WINDOW)
    ap.add_argument("--min-severity", type=float, default=0, help="only report sessions at/above this %% of window")
    ap.add_argument("--retire-stuck", action="store_true", help="retire every degraded session")
    ap.add_argument("--retire", nargs="*", default=[], help="explicit session id(s) to retire")
    ap.add_argument("--apply", action="store_true", help="actually delete after archiving")
    ap.add_argument("--active-window", type=float, default=10.0,
                    help="refuse to retire a session touched within this many minutes")
    ap.add_argument("--evidence-dir", default=None)
    ap.add_argument("--account", action="store_true",
                    help="price-free consumption view: tokens by project, burn rate by hour, archived total")
    ap.add_argument("--hours", type=int, default=24, help="burn-rate window in hours (with --account)")
    args = ap.parse_args()
    args.evidence_dir = args.evidence_dir or os.path.join(
        os.path.expanduser("~"), ".local", "share", "kilo", "retired-sessions")
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
        targets += [r["id"] for r in rows if r["sev"] >= SEVERITY_WARN or r["warns"] >= STUCK_MIN_WARNS]
    targets = sorted(set(t for t in targets if t in sessions))
    if not targets:
        raise SystemExit("no matching sessions")
    print("\n=== retire: %s ===" % ", ".join(targets))
    retire(targets, args, sessions, explicit)


if __name__ == "__main__":
    main()
