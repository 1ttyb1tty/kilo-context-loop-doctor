# kilo-context-loop-doctor

A local diagnostic for **Kilo Code sessions stuck in the context-overflow regime** — plus a
price-free consumption view for anyone on a plan or allowance.

This is a *user-side workaround and a specification*, not a patch. It exists because the failure it
detects is silent from inside the product.

## The pathology it detects

A session whose serialized transcript crosses the payload threshold (~1.25 MB) never recovers by
itself. Measured on Kilo Code 7.8.15, Windows, 1M-context model:

- the payload-prune path runs on **every step** and reports `payload still large after pruning`;
  the payload only grows — observed 1,254,297 → 3,721,819 bytes over 18 hours, 1,792 warnings
- the compaction meant to reduce it writes a marker part (34 bytes,
  `{"type":"compaction","auto":false}`) and leaves `agent=compaction` messages marked
  `"summary": true` with **0 parts**, 0 tokens, cost 0 — nothing is summarized, nothing removed
- every part of the session survives, including its first hour, so the context cannot shrink
- it ends at the provider's hard refusal (`Input exceeds the context limit (1048568 tokens)`), and
  every turn after that repeats the same failure
- **nothing is shown to the user**: no warning for the over-limit or prune-failed state, and the
  only visible string — `"ui.messagePart.compaction": "Session compacted"` — reports the opposite
  of what happened
- the cost is real but disguised: the prompt cache keeps the headline hit rate at ~97%, while 47
  prefix-invalidating sends re-billed the whole context uncached (29.8 M tokens in one session)
- **it is a trajectory, not a state**: the ratio reads mid-range while the payload climbs. Measured
  2026-10-22 on `ses_ea141659…`: 1,255,085 → 2,026,898 bytes in 2 h 01 (first and last warnings in
  `opencode.log`), 150 failed prunes, 270k tokens of uncached input re-billed — and still under half
  the window when it was flagged. Five sessions in one folder entered this way in ten days

There is no local repair. The session must be retired.

## Install

Nothing to build; Python 3 only. Point it at a Kilo binary via `KILO_BIN` or let it glob the usual
VS Code extension location:

```bash
export KILO_BIN=/path/to/kilo        # optional
python kilo_doctor.py                # detect + report, read-only
```

To use it as an agent skill, copy this directory to `<config>/skills/context-loop-doctor/`
(e.g. `~/.config/kilo/skills/context-loop-doctor/`), which is where `SKILL.md` is discovered from.

## Use

```bash
python kilo_doctor.py                        # every session, ranked by peak context / window
python kilo_doctor.py --predict              # + the trajectory block for every session
python kilo_doctor.py --min-severity 30      # display filter only; never narrows --retire-stuck
python kilo_doctor.py --top 10               # display at most N rows
python kilo_doctor.py --json                 # the same rows, for a script
python kilo_doctor.py --account              # consumption in tokens, by project + burn rate/hour
python kilo_account.py --hours 12            # same thing, measurement half only
python kilo_doctor.py --retire-stuck         # ARCHIVE-ONLY dry run (degraded + still-rising)
python kilo_doctor.py --retire-stuck --apply # archive, then delete
python kilo_doctor.py --force                # ignore the recency guard (never the caller's session)
python kilo_doctor.py --list-archives        # the archive index, reconciled against the files
python kilo_doctor.py --verify-archives      # re-parse every archive, check it against the index
python kilo_doctor.py --reindex-archives     # index archives written before the index existed
python verify_restore.py                     # prove the archives import cleanly
```

`--min-severity` and `--top` are presentation: the retirement selection is always computed over every
session, so a filter cannot change which sessions `--retire-stuck` picks.

`sample-output.txt` is a real run, redacted: home path, project folder names and session ids. The
numbers and timestamps are untouched, and `build_sample.py` regenerates it from a live run with the
redaction counted and asserted (a rule that stops matching fails the build rather than publishing a
sample that leaks).

## Safety model

Every part of this is load-bearing:

- **archive first, validate, then delete.** Each target *and every descendant* is exported to JSON and
  parsed; one failed or empty export aborts the whole run. The descendant walk is breadth-first to
  closure, because the cascade's depth is undocumented.
- **`kilo session delete <id>` cascades to child sessions.** Measured: retiring 4 targets removed
  **20 sessions** — 16 subagent runs went with them, and the CLI help says only "delete a session".
  This tool archives the whole descendant closure for exactly that reason, and prints it before it
  deletes anything.
- **the guard is about the caller, not the clock.** The session running the command (newest store
  session whose directory is the process's cwd) is refused unconditionally — `--force` does not
  override it. Any other session touched within `--active-window` minutes (default 10) is skipped with
  its age and the exact follow-up (`--active-window <age-1>` or `--force`). If no store session matches
  the current directory the caller cannot be identified and the run says so.
- **`--apply` is required to delete.** The default is a dry run: it archives and writes index rows with
  `deleted:false`, which the following `--apply` run supersedes rather than appends to.
- **the index records what happened, not what was intended.** `deleted` is read back from the store
  after the delete, and a descendant removed by the cascade is flagged `cascade_removed`. A failed
  `session delete` cannot leave the index claiming a removal.
- **restore is tested**, not asserted: `verify_restore.py` imports archives into an isolated scratch
  store and compares message/part counts.
- **store totals are printed before and after** the delete, so the record shrink is stated where it
  happens instead of being inferred later.

## What it writes

| path | content |
|---|---|
| `<evidence-dir>/<session>.json` | the export of one archived session (default `~/.local/share/kilo/retired-sessions/`) |
| `<evidence-dir>/index.jsonl` | one row per archived session: role, parent, title, directory, model, archived-at (local with offset), messages, bytes, token totals, peak context, warns, reason, `deleted` |
| `~/.local/share/kilo/context-doctor-state.json` | the per-session warn high-water mark (`warns_seen`, `first_warn`, `last_warn`, `max_size`), unioned on every run |

Both files are written atomically (temp file + rename), and a state file that cannot be *parsed* is
kept and reported rather than replaced by a partial view. Nothing else is written: detection and
reporting are read-only.

## Files

| file | role |
|---|---|
| `kilo_doctor.py` | detect, report, archive-and-retire stuck sessions (single file, stdlib only) |
| `kilo_account.py` | thin wrapper over `--account`: tokens by project, burn rate per hour |
| `verify_restore.py` | imports archives into a scratch store and checks the counts match |
| `build_sample.py` | regenerates `sample-output.txt` from a live run, redacting identity by counted rule |
| `SKILL.md` | the agent-skill form: when to reach for this, thresholds, traps, limits |
| `sample-output.txt` | a real run of three modes, redacted |
| `LICENSE` | MIT — © 2026 Victor Simeonov |

## Reading the report

`sev%` is peak context ÷ window, taken from the store's own per-call usage, so it survives log
rotation. `peak_ctx` ≈ `tokens.input + tokens.cache.read` of the largest call. `cold` counts sends
at <10% cache hit — the signature of a prune rewriting the prompt prefix. `warns` counts failed
prunes from `opencode.log`, **unioned with a persisted high-water mark** so a rotated log cannot make a
known-stuck session look healthy. `cmsg`/markers are compaction evidence. `dup` counts repeated
identical text parts, the re-appended user message symptom.

| sev% | meaning |
|---|---|
| < 34% | below the prune threshold — healthy, unless it is RISING |
| 34%+ with `warns` | pruning has started and cannot win — degraded |
| 85–100% | near or at the wall; the next turn may hard-fail |
| 100% | already over; every turn duplicates the failure |

Two blocks are computed from the same per-call usage:

- **trend** — `first_ctx`/`peak_ctx`, the session's span in hours, `tok/h` (the last hour when the
  session has three calls in it, else a straight line through the whole session; `-` when there is too
  little data), `eta_h` = hours to the assumed window at that rate, and `last_warn_B`/`pay%` = the
  largest warning payload seen ÷ `PRUNE_BYTES_EST` (an estimate, see below).
- **RISING** — sessions below the degraded threshold whose payload is still growing, with the hours
  left. They are part of `--retire-stuck`'s selection: retiring an idle one costs an archive, waiting
  costs the re-billed sends.

The tool prints `marker queries: compaction-parts=N compaction-agent-msgs=N partless-msgs=N` before it
reports "none", because a pass that leaves no marker is possible — measured: a session with 150 failed
prunes on record had 0 of all three. Absence of a marker is not absence of the loop.

## Evidence discipline built into it

- **The store is durable; the logs are not.** Per-session logs are deleted within hours, and
  `opencode.log` does not contain `compaction.chunks` lines at all — its silence about compaction is
  not evidence of absence. Anything authority-bearing is read from the store, and warn marks are
  unioned into `context-doctor-state.json` so the log's ephemerality cannot erase them. Warn
  timestamps are stored as the log writes them (UTC `Z`).
- **Retiring destroys the local usage record.** Deleting a session deletes its tokens from every
  store-based total: measured 46.2 M → 3.85 M input and 1,385 M → 107 M cache reads. So the archive
  JSON is the only local record of those tokens, and `--account` prints the archived total separately
  to make that visible rather than silently under-reporting. `index.jsonl` is that history with an
  index: measured 2026-10-22, 21 archives holding 45.24 M input / 1.76 M output / 1.41 B cache-read
  tokens the store no longer counts.
- **Logs record command text**, so a phrase match is not an event; the parser requires the event
  shape (`level=WARN` plus the exact message) before counting a line, and keys the timestamp on the
  field name (`timestamp=…`) rather than on the token's shape.
- **Two units, and only one is a threshold.** `sev%` is tokens ÷ window; the prune trigger is a payload
  size in bytes (`REQUEST_PRUNE_BYTES`). The `34%` row above is the token ratio at which the path was
  observed to fire on a 1M window, not a definition, which is why the report also carries the payload
  side. `PRUNE_BYTES_EST = 1250000` is the smallest first-warning payload seen on 7.8.15 — a measured
  floor, an estimate, not a value read from the bundle.
- **The archive export renames store columns** (`session.parent_id` arrives as `info.parentID`), so a
  lookup of `parent_id` finds nothing and every reindexed row looks parentless.

## Limits

- It detects and retires; it **cannot repair** a session. Nothing here fixes the underlying defect.
- It reads the local store only — one machine, no cloud.
- `sev%` uses `--window` (default 1,048,576) unless you pass your model's declared window; `eta_h` is
  computed against the same window, so the two agree. The table prints no model column; the index
  records each retired session's model, which is how to say which window applied to it.
- **Rates are straight lines, labelled as such.** A `tok/h` from a session's last hour is a projection,
  not a prediction, and `eta_h` says nothing about growth that is stepwise (a few big pastes rather
  than steady turns). Sessions with too little metered history show `-` rather than an extrapolation.
- There is no CLI archive command even though the `session` table has a `time_archived` column, so
  "retire" here means archive-and-delete.
- Version-tagged: everything measured on 7.8.15. Re-check before trusting a version-sensitive claim.

## Related upstream reports

- [#15165](https://github.com/Kilo-Org/kilocode/issues/15165) — context compaction looping
- [#14454](https://github.com/Kilo-Org/kilocode/issues/14454) — prune cannot win for long sessions
- [#14372](https://github.com/Kilo-Org/kilocode/issues/14372) — overflow accounting and thresholds (v2 slice)
- [#15303](https://github.com/Kilo-Org/kilocode/issues/15303) — usage, cost and telemetry (soft max-cost alert)
- [#15354](https://github.com/Kilo-Org/kilocode/issues/15354) — snapshot cleanup loop fails; refs accumulate
- [#15355](https://github.com/Kilo-Org/kilocode/issues/15355) — `kilo import` is not atomic
- [#15357](https://github.com/Kilo-Org/kilocode/issues/15357) — `session delete` cascades to children

## Licence

MIT — see `LICENSE`. © 2026 Victor Simeonov (contact: Discord `vikos64`).
