---
name: context-loop-doctor
description: Detect, report and retire Kilo sessions stuck in the context-overflow regime — payload past the prune threshold, compaction that removes nothing, silent re-sends until the provider refuses, then the same failure on every turn. Use when a session keeps erroring with "Input exceeds the context limit", when the UI says "Session compacted" but the context never shrank, when "payload still large after pruning" repeats, or when asked to find expensive/stuck sessions or clean them up.
---

# Context loop doctor

## The pathology

A session whose serialized transcript crosses the payload threshold (~1.25 MB) never recovers by
itself. Measured on 7.8.15:

- the payload-prune path runs on **every step** and reports `payload still large after pruning`; the
  payload only grows (observed 1,254,297 → 3,721,819 bytes over 18 hours, 1,792 warnings)
- the compaction that is supposed to reduce it writes a marker part (`{"type":"compaction","auto":false}`,
  34 bytes) and leaves `agent=compaction` messages marked `"summary": true` with **0 parts**, 0 tokens
  and cost 0 — nothing is summarized and nothing is removed
- every one of the session's thousands of parts survives, including its first hour, so the context
  cannot shrink
- it ends at the provider's hard refusal (`Input exceeds the context limit (1048568 tokens)`), and from
  then on every turn repeats the same failure
- **nothing is shown to the user**: no warning for the over-limit or prune-failed state, and the only
  visible string (`"ui.messagePart.compaction": "Session compacted"`) reports the opposite of what happened

Re-entry is the loop: the requested compaction pass runs again on the next turn (+1 chunk each time,
`chunks=673 → 674 → 675`). An earlier auto-trigger does not help — the prune path was already firing
from ~34% of a 1M window and reducing nothing.

There is no local fix. **Retire the session.**

## Detect

```
python ~/.config/kilo/skills/context-loop-doctor/kilo_doctor.py
python ~/.config/kilo/skills/context-loop-doctor/kilo_doctor.py --min-severity 30
```

Columns: `sev%` peak context ÷ window (from the store's own per-call usage, so it is durable);
`peak_ctx` ≈ `tokens.input + tokens.cache.read` of the largest call; `cold` = sends at <10% cache hit,
i.e. the prune rewrote the prefix and the whole context was re-billed uncached; `warns` = failed prunes
seen in `opencode.log` (best-effort, see below); `cmsg` / markers = evidence a compaction pass ran;
`dup` = identical repeated text parts, the re-appended user message symptom.

Reading it:

| sev% | meaning |
|---|---|
| < 34% | below the prune threshold, healthy |
| 34%+ with `warns` | pruning has started and cannot win — degraded |
| ~85–100% | near or at the wall; next turn may hard-fail |
| 100% | already over; every turn duplicates the failure |

Cross-check the CLI's own totals with `kilo stats --models`.

## Retire

```
# dry run (default): archive everything, delete nothing
python .../kilo_doctor.py --retire-stuck
# archive, then delete
python .../kilo_doctor.py --retire-stuck --apply
python .../kilo_doctor.py --retire ses_abc... --apply
```

Safety model, all of it load-bearing:

- **archive first, validate, then delete** — every target *and every child session* is exported to JSON
  and parsed; one failed or empty export aborts the whole run
- archives go to `~/.local/share/kilo/retired-sessions/` (`--evidence-dir` to change)
- a session touched in the last `--active-window` minutes (default 10) is skipped, so the live session
  is never the one you retire
- deletion needs `--apply`; explicit `--retire <id>` on an active session is refused outright

After retiring: **do not resume the session, and do not press Compact on it** — a fresh session in the
same folder costs nothing and starts below the threshold. Never rely on compaction or pruning to
recover a session that is already in this state.

## Deleting a session cascades to its children — archive them first

`kilo session delete <id>` also deletes sessions whose `parent_id` is that session. Measured: retiring
4 targets removed **20 sessions** — 16 subagent runs (review tracks, porting tasks) went with them, and
the CLI help ("delete a session") says nothing about it.

The tool therefore archives children too, even though it only deletes the parents. Restore is verified:

```
kilo import ~/.local/share/kilo/retired-sessions/<session>.json
```

Tested against an isolated scratch store: 928 messages / 3799 parts and 12 / 58 restored exactly, and
the compaction marker came back with them.

## Retiring destroys the local usage record

Deleting a session also deletes its messages and parts, so the store stops counting its tokens.
`kilo stats` and this tool's totals therefore **drop after a cleanup** — measured on 7.8.15: recorded
input went 46.2M → 3.85M and cache reads 1,385M → 107M after retiring 20 sessions. That is the record
shrinking, not consumption falling.

Consequences worth stating to whoever asks for the numbers:

- the pre-retirement **archive JSON is the only local record of those tokens** — keep it, and treat
  `retired-sessions/*.json` as usage history, not just as a restore point
- for a prepaid balance, the provider's own dashboard remains the authority; a local report after a
  cleanup cannot be used to estimate remaining allowance
- when quoting consumption, say whether it is pre- or post-cleanup, and give the session count with it

## Evidence discipline

- **The store is durable; the logs are not.** `~/.local/share/kilo/kilo.db` keeps sessions, messages,
  parts and per-call usage. Per-session logs are deleted within hours, and `opencode.log` — which does
  persist — **does not contain `compaction.chunks` lines at all**. Its silence about compaction is not
  evidence of absence; prefer store queries for anything authority-bearing and describe log-derived
  numbers as best-effort.
- The store may be open in WAL mode; read it with `kilo db "<query>"` or from a copy, and never assume a
  missing row means a missing event without checking the source can hold it.
- `kilo db` output is TSV with a header row. Open a session JSON with `kilo export <id>` (add
  `--sanitize` before sharing anything).

## Traps that have produced wrong answers here

- **A string in the bundle is not a running code path.** Read the callee at the call site. A truncated
  window in a minified bundle cuts the method name, and a route that exists can be a 503 stub.
- **PowerShell:** `\"` inside a double-quoted string ends the string — quoting failures look like syntax
  errors pointing at the code you passed. Use single quotes for SQL and file-tool edits for source.
- **SQLite:** `""` is an identifier, not an empty string; use `''` or drop the coalesce.
- **Logs record command text**, so a phrase match is not an event. Require the event shape
  (`level=WARN` plus the exact message) before counting a line.
- **Self-referential counts:** searching a transcript for a string that your own investigation keeps
  writing into it inflates the count. Exclude the session doing the search.
- **`"auto": false` on a compaction part does not mean "explicitly requested"** — the editor's Compact
  button omits `auto`, so it defaults to false.

## Related, already filed

- #15165 — context compaction looping (the loop, the empty summaries, the false "Session compacted")
- #14454 — prune cannot win for long sessions (`REQUEST_PRUNE_BYTES`); auto-compaction never fires on
  1M-context models
- #15147 — build the compaction request from the session's own prefix so it reads the prompt cache
  (the cold-send cost)
- #15354 — snapshot cleanup loop fails; snapshot refs accumulate per message part
- #15355 — `kilo import` is not atomic; a validation failure leaves a partial session
- #14770 — compaction needs intermediate state and a configurable retry under rate limiting

## Limits of this tool

It detects and retires; it cannot repair. There is no CLI archive command even though the `session`
table has a `time_archived` column, so "retire" here means archive-and-delete. `sev%` compares peak
context against `--window` (default 1,048,576) rather than the model's declared limit, so state the
window you used when quoting a percentage — and if the model declares its own window, pass it
(`--window 1000000`) so the two agree.

## Account view: consumption and burn rate, in tokens

```
python ~/.config/kilo/skills/context-loop-doctor/kilo_doctor.py --account
python ~/.config/kilo/skills/context-loop-doctor/kilo_doctor.py --account --hours 12
```

Denominated in **tokens**, so it needs no price table and works on a plan or allowance — where the
currency is consumption, not dollars (a plan provider's catalog entry is priced `0` on purpose, so
any cost-based alarm is inapplicable to it).

Three sections:

- **by project** — sessions, calls, input/output/cache-read, total, share of the account, and the
  largest single call (`peak_call`). A pot is shared by every session and project, so the account
  line is the one that matters for exhaustion, not one session's total.
- **burn rate per hour** with a bar — this is where a spike becomes visible. On this machine the
  hours of one investigation read 18–32 M tokens/hour against ~3 M in ordinary hours; the bar makes
  a 10× anomaly obvious at a glance, which is the warning that did not exist.
- **retired sessions** — the token total held in the archives, with the reminder that store-based
  totals exclude it. Measured: 20 archives holding 1.38 B tokens that the store no longer counts.

## Verify what a limit declaration did, and did not, do

`kilo debug config` prints the **resolved** configuration, so it shows whether an edit to
`kilo.jsonc` was picked up (e.g. a model's `limit` block) without running a turn:

```powershell
& "$env:USERPROFILE\.vscode\extensions\kilocode.kilo-code-7.8.15-win32-x64\bin\kilo.exe" debug config |
  Select-String -Pattern "<model-id>" -Context 0,6
```

`kilo config check` reports warnings and errors. `kilo debug skill` lists discovered skills — worth
knowing that a new skill is picked up at runtime, and `kilo models <provider>` shows the model
inventory.

A model entry with **no `limit`** means the harness has no declared window for it and falls back to
a default; the catalog's plan entries declare `context` and `output`, and the config schema requires
both when `limit` is present. Declaring a limit pins the overflow accounting to a stated value
instead of a default — it does not by itself make a runaway session recover.

**State the layer a value describes.** `kilo debug config` shows what actually took effect, which is
how to catch a field that is accepted but ignored: a `cost.cache: {read, write}` block in a model
entry validates with no warnings yet is absent from the resolved config, where only
`cost.{input, output}` survives. Write the file to match what takes effect. A plan provider is
priced `0` deliberately — "allowance, not meter" — so do not add prices to one; measure tokens
instead (see the account view).

