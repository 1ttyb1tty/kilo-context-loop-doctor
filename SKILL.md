---
name: context-loop-doctor
description: Detect, report and retire Kilo sessions stuck in the context-overflow regime — payload past the prune threshold, compaction that removes nothing, silent re-sends until the provider refuses, then the same failure on every turn. Reports each session's trajectory (tokens/hour, hours to the wall) and keeps a durable warn mark, an archive index and a manifest. Use when a session keeps erroring with "Input exceeds the context limit", when the UI says "Session compacted" but the context never shrank, when "payload still large after pruning" repeats, or when asked to find expensive/stuck sessions or clean them up.
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
python ~/.config/kilo/skills/context-loop-doctor/kilo_doctor.py --predict     # trend for every session
python ~/.config/kilo/skills/context-loop-doctor/kilo_doctor.py --json        # machine-readable rows
python ~/.config/kilo/skills/context-loop-doctor/kilo_doctor.py --min-severity 30 --top 10
```

`sev%` = peak context ÷ window (from the store's own per-call usage, so it is durable);
`peak_ctx` ≈ `tokens.input + tokens.cache.read` of the largest call; `cold` = sends at <10% cache hit,
i.e. the prune rewrote the prefix and the whole context was re-billed uncached; `warns` = failed prunes
seen in `opencode.log` **unioned with a persisted high-water mark**, so a rotated log cannot make a
known-stuck session look healthy; `cmsg` / markers = evidence a compaction pass ran; `dup` = identical
repeated text parts, the re-appended user message symptom.

Two further blocks:

- **trend** — `first_ctx`/`peak_ctx`, the session's span in hours, `tok/h` (the last hour when the
  session has three calls in it, else a straight line through the whole session; `-` when there is too
  little data), `eta_h` = hours to the assumed window at that rate, and `last_warn_B` / `pay%` = the
  largest warning payload seen ÷ `PRUNE_BYTES_EST`.
- **RISING** — sessions below the degraded threshold whose payload is *still growing*, with the hours
  left. This is the early-warning block: severity alone is a snapshot.

`--json` prints the same rows for scripts. `--min-severity` and `--top` are **display filters only**;
they never change what `--retire-stuck` selects (verified: `--min-severity 100 --retire-stuck` shows an
empty table and still selects the degraded sessions).

Reading it:

| sev% | meaning |
|---|---|
| < 34% | below the prune threshold, healthy — unless it is RISING |
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
python .../kilo_doctor.py --retire ses_abc... --apply --force   # ignore the recency guard
```

`--retire-stuck` selects every degraded session **and every session still rising past 34% of the
window** — the RISING block is part of the selection, not only the report.

Safety model, all of it load-bearing:

- **archive first, validate, then delete** — every target *and every descendant* is exported to JSON
  and parsed; one failed or empty export aborts the whole run. The descendant walk is **breadth-first
  to closure** (`parent_id` at each level), because `kilo session delete` cascades and its help does
  not say how deep: archiving only direct children can leave a grandchild to be destroyed unarchived
- archives go to `~/.local/share/kilo/retired-sessions/` (`--evidence-dir` to change), and each run
  writes **one manifest row per session** to `index.jsonl` (id, role, parent, title, directory, model,
  archived-at local with offset, messages, bytes, token totals, peak context, warn count, reason)
- the manifest is **upserted, not appended**: a dry run's rows (`deleted:false`) are replaced by the
  following `--apply` run's rows, so `--list-archives` cannot count one session's tokens twice. The
  write is atomic (temp file + `os.replace`)
- **`deleted` is read back from the store, not from the flag**: after deleting, a target that is still
  present is recorded `deleted:false`, and a descendant removed by the cascade is recorded
  `cascade_removed:true`. A failed `session delete` therefore cannot leave the index claiming a removal
- **the guard is about the caller**, not a blanket clock: the tool identifies the session running the
  command (newest store session whose directory is the process's cwd) and refuses to retire *that* one
  — unconditionally, before any recency test, and `--force` does not override it. Any other session
  touched inside `--active-window` (default 10 min) is skipped with its age and the exact follow-up
  (`--active-window <age-1>` or `--force`)
- if no store session matches the current directory the caller cannot be identified: the run says so
  and you must check the target list before `--apply`
- **explicit `--retire <id>` gets the same guard** as `--retire-stuck` — naming a session does not
  override recency by itself; `--force` is what does
- deletion needs `--apply`
- the run prints store totals before and after deletion, so the record shrink is stated where the
  deletion happened instead of being measured later

Before deleting, the run lists every descendant it will archive with the target, with its parent.

### The guard blocked the right session once

Measured 2026-10-22, `--retire-stuck` on a session 4.2 min old: the old guard printed
`skipping ... - active 4.2 min ago (live session)` and exited, although the *live* session was a
different, 0.2-min-old one. The working sequence was a second run with a narrower window:

```
python .../kilo_doctor.py --retire-stuck                    # skipped: target 4.2 min, window 10
python .../kilo_doctor.py --retire-stuck --active-window 2  # retired it
```

With the caller-session guard the first command now names the caller, gives the target's age and
suggests the exact window, so one run plus one `--active-window`/`--force` is enough.

## Predict, don't just measure

Severity is a snapshot; the loop is a trajectory, and every session retired from this project was
already unrecoverable when first flagged. Measured on `ses_ea141659…` (2026-10-21/22): payload
1,255,085 → 2,026,898 bytes in 2 h01 (first and last warnings in `opencode.log`), 150 failed prunes,
270k tokens of uncached input re-billed, and it was still under half the window when flagged. Its four
predecessors in the same folder each ran the same climb (1.25 MB → 2.3–3.7 MB, 285–672 warnings).

So: read the **RISING** block, and retire an idle session that is past ~34% and still climbing, before
the provider wall. Waiting costs the cold re-sends (`cold`, `uncached_in`) and ends in a hard failure
that repeats on every turn.

## Two units, and only one of them is a threshold

`sev%` compares **tokens** against the assumed window; the prune trigger is a **payload size in bytes**
(`REQUEST_PRUNE_BYTES`). The `34%` in the table above is the *token* ratio at which the path was
observed to start firing on a 1M window, not a definition. The tool therefore also reports the payload
side: `last_warn_B` from the log and `pay%` = that ÷ `PRUNE_BYTES_EST` (1250000 — the smallest
first-warning payload seen on 7.8.15, a measured floor, an **estimate**, not a value read from the
bundle). `last_warn_B` and `pay%` are **unioned with the persisted mark**, never replaced by it: after
a log rotation a live log with a smaller maximum or a later first warning cannot move the reported
peak backwards, and `warns_seen` / `max_size` from the state file are what keep `pay%` meaningful once
the log lines are gone. Only when neither source has a size is the column `-`.

## Marker detection is audited, not asserted

The tool prints `marker queries: compaction-parts=N compaction-agent-msgs=N partless-msgs=N` before it
says "none", because a pass that leaves no marker is possible: on 7.8.15 a session with 150 failed
prunes on record had **0** compaction parts, **0** `agent=compaction` messages and **0** partless
messages. Absence of a marker is not absence of the loop — the prune warnings and the token
trajectory are the evidence that survives.

## Archives: index, reindex, verify

The archive is the only local record of a retired session's tokens, so it is also usage history, and a
directory of JSON files is not usable as history without an index.

```
python .../kilo_doctor.py --list-archives     # manifest vs files, with token totals per session
python .../kilo_doctor.py --reindex-archives  # give pre-manifest archives a row built from the file
python .../kilo_doctor.py --verify-archives   # re-parse every archive, check it against the manifest
```

Measured 2026-10-22: 21 archives, 3888 messages, 0 problems, holding 45.24 M input / 1.76 M output /
1.41 B cache-read tokens that the store no longer counts. `--list-archives` reconciles manifest rows
against files in both directions (rows with no file, files with no row).

Token provenance differs by writer and the manifest says which: a row written by `retire()` takes the
store's session-row totals (the only copy that exists before the delete), while a row written by
`--reindex-archives` sums the archive's own per-message tokens. Both go through the same
`archive_token_totals()` reader on the archive side; `--verify-archives` re-parses every file and
compares message counts against the manifest. Malformed rows and non-export JSON files are reported and
skipped rather than raising.

`--reindex-archives` also infers `role` and `parent` from the archive itself: the export carries
`info.parentID` (the store's `session.parent_id`, renamed), so a session with a parent was carried
along as a descendant and one without was the target. Measured on the 21 archives of 2026-10-22: 5
targets and 16 descendants under 3 parents — which is the same 4-target/16-child cascade the earlier
retirement recorded. Such a row's `deleted` is `null`: the file does not say whether the session was
removed, and the index does not guess.

## Durable warn marks

`~/.local/share/kilo/context-doctor-state.json` keeps, per session, `warns_seen`, `first_warn`,
`last_warn` and `max_size`, unioned from every run. The log self-deletes and the store carries no prune
warnings, so without this a session that was stuck last week reports a clean zero. Warn timestamps are
stored **as the log writes them (UTC `Z`)**; say so when comparing them with anything local.

The file is rewritten atomically (temp + `os.replace`), and a run that cannot **parse** an existing
state file keeps it and warns instead of persisting a partial view — treating an unreadable state as
empty would replace the accumulated marks with one run's log, which is the only durable record there is.

## One investigation per session

The regime re-enters per session, not per project: five sessions in this folder were stuck between
2026-10-12 and 2026-10-22 (285, 325, 520, 672 and 150 failed prunes), and each was retired only after
the loop had started. Start a fresh session before a long investigation passes ~35% of the window, and
treat `--account`'s `worst%` column and its "repeat offenders" list as the project-level warning: a
project whose worst call is past 34% will do this again.

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
  numbers as best-effort. Warn marks are unioned into `context-doctor-state.json` precisely so the
  log's ephemerality cannot erase them.
- The store may be open in WAL mode; read it with `kilo db "<query>"` or from a copy, and never assume a
  missing row means a missing event without checking the source can hold it.
- `kilo db` output is TSV with a header row. Open a session JSON with `kilo export <id>` (add
  `--sanitize` before sharing anything).

## Traps that have produced wrong answers here

- **A guard nested in a filter is not a guard.** The caller-session exclusion used to sit inside the
  `--active-window` loop, so a caller older than the window (or a run with `--active-window 0`) was
  never compared against it and the session running the command could be archived and deleted. The
  exclusion now happens first and unconditionally; exclude-by-identity must not depend on the same
  condition it is meant to override. Verified in an isolated harness, including the case where
  `kilo session delete` reports success while the session is still present.
- **Write the observed outcome, not the intent, into a record.** `deleted` used to be derived from
  `--apply` before the delete ran, so a failed delete left the index asserting a removal; and a dry run
  plus a later `--apply` left two rows for one session, which `--list-archives` then summed twice.
- **A snapshot is not a trajectory.** `sev%` at 45% reads as mid-range and was 2 h from the wall with
  the payload climbing ~385 KB/h. Report and read the rate and `eta_h`, not only the ratio.
- **A ratio must be computed against the window you state.** `eta_h` used the default window while
  `sev%` used `--window`, so with `--window 400000` the DEGRADED lines gave "hours to window" against a
  window the report had not assumed.
- **A filter that also selects is a silent scope change.** `--min-severity` used to be applied to the
  rows the retirement step then chose from, so `--min-severity 61 --retire-stuck` retired nothing while
  printing a plausible table. Selection is now computed over every session; the filter is presentation.
- **Key a log field by its name, not its shape.** Warn timestamps were parsed by "four leading digits
  and an `Z`", which never matched `timestamp=2026-…Z` — so `first_warn`/`last_warn` were silently
  `None` for every session (found 2026-10-22). Match `tok.startswith("timestamp=")`. Same rule as the
  table-parsing one in the machine conventions: locate by content, then validate.
- **An export renames store columns.** `session.parent_id` arrives in the archive's `info` as
  `parentID`, so a lookup of `parent_id` finds nothing and every reindexed row silently came out
  parentless (`role: unknown`) even though 16 of 21 archives did carry a parent. Two names for one
  column is worth a comment at the read site.
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
(`--window 1000000`) so the two agree. The table prints no model column yet; the manifest does record
each retired session's model, which is the honest way to say which window applied to it.

Rates are straight lines and they are labelled as such: a `tok/h` derived from a session's last hour is
a projection, not a prediction, and `eta_h` says nothing about a session whose growth is stepwise (a
few big pastes rather than steady turns).

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

