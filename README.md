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
python kilo_doctor.py --min-severity 30      # only degraded ones
python kilo_doctor.py --account              # consumption in tokens, by project + burn rate/hour
python kilo_account.py --hours 12            # same thing, measurement half only
python kilo_doctor.py --retire-stuck         # ARCHIVE-ONLY dry run
python kilo_doctor.py --retire-stuck --apply # archive, then delete
python verify_restore.py                     # prove the archives import cleanly
```

`sample-output.txt` is a real run, with the username and session ids redacted.

## Safety model

Every part of this is load-bearing:

- **archive first, validate, then delete.** Each target *and every child session* is exported to
  JSON and parsed; one failed or empty export aborts the whole run.
- **`kilo session delete <id>` cascades to child sessions.** Measured: retiring 4 targets removed
  **20 sessions** — 16 subagent runs went with them, and the CLI help says only "delete a session".
  This tool archives children for exactly that reason.
- **a session touched within `--active-window` minutes (default 10) is skipped**, so the live
  session is never the one retired; an explicitly named active session is refused outright.
- **`--apply` is required to delete.** Default is a dry run that archives nothing and prints the plan.
- **restore is tested**, not asserted: `verify_restore.py` imports archives into an isolated scratch
  store and compares message/part counts.

## Files

| file | role |
|---|---|
| `kilo_doctor.py` | detect, report, archive-and-retire stuck sessions (single file, stdlib only) |
| `kilo_account.py` | thin wrapper over `--account`: tokens by project, burn rate per hour |
| `verify_restore.py` | imports archives into a scratch store and checks the counts match |
| `SKILL.md` | the agent-skill form: when to reach for this, thresholds, traps, limits |
| `sample-output.txt` | a real run of both modes, redacted |
| `LICENSE` | MIT (fill in the copyright holder) |

## Reading the report

`sev%` is peak context ÷ window, taken from the store's own per-call usage, so it survives log
rotation. `peak_ctx` ≈ `tokens.input + tokens.cache.read` of the largest call. `cold` counts sends
at <10% cache hit — the signature of a prune rewriting the prompt prefix. `warns` counts failed
prunes from `opencode.log` (best-effort; see below). `cmsg`/markers are compaction evidence. `dup`
counts repeated identical text parts, the re-appended user message symptom.

| sev% | meaning |
|---|---|
| < 34% | below the prune threshold — healthy |
| 34%+ with `warns` | pruning has started and cannot win — degraded |
| 85–100% | near or at the wall; the next turn may hard-fail |
| 100% | already over; every turn duplicates the failure |

## Evidence discipline built into it

- **The store is durable; the logs are not.** Per-session logs are deleted within hours, and
  `opencode.log` does not contain `compaction.chunks` lines at all — its silence about compaction is
  not evidence of absence. Anything authority-bearing is read from the store.
- **Retiring destroys the local usage record.** Deleting a session deletes its tokens from every
  store-based total: measured 46.2 M → 3.85 M input and 1,385 M → 107 M cache reads. So the archive
  JSON is the only local record of those tokens, and `--account` prints the archived total separately
  to make that visible rather than silently under-reporting.
- **Logs record command text**, so a phrase match is not an event; the parser requires the event
  shape (`level=WARN` plus the exact message) before counting a line.

## Limits

- It detects and retires; it **cannot repair** a session. Nothing here fixes the underlying defect.
- It reads the local store only — one machine, no cloud.
- `sev%` uses `--window` (default 1,048,576) unless you pass your model's declared window.
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
