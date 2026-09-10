---
name: readings
description: Produce the weekly readings report - tasks by lane and model, dead tasks, context per turn, full-chain cost per task, repair share, verifier sessions, intake duplicates - and fill the outcome numbers. Use at the end of a week or when the principal asks how the team is doing or where the money went.
---

# /bellhop:readings

`BH="${CLAUDE_PLUGIN_ROOT}/bin/bellhop"`.

1. `$BH readings --week <YYYY-Www>` (omit `--week` for the current week). Add `--transcripts <path or dir>` for each Claude Code transcript, or directory of them, that belongs to this desk - the coordinator's own project directory first. Without it every context and cost number reads `n/a`.
2. Open `reports/weekly/<week>.md` and fill the two outcome numbers by hand from the ledger: report-to-release hours and post-release defects. If you cannot find them, write "not recorded" and add a debt to record them next week.
3. Read "Cost and context" before the process numbers. Three questions it answers: what share of turns sat above the long-context line and what share of the money they took; what one task really cost end to end (coordinator + worker + verifier); and how much of the week the desk spent repairing itself.
4. Read "Where the coordinator's turns went" only if the context median is high. It names the source filling the window - one tool's results, hooks, file reads - which is what a fix has to target.
5. Read the process numbers as diagnostics, not scores. For anything that moved a lot, write one sentence on the likely cause.
6. `n/a` is a reading, not a gap to fill in. It means the desk has no file that can answer: no transcripts given, a model with no price, no acceptance result, no intake book. Say which, and do not substitute an estimate.
7. Commit is automatic; `state/readings.md` gains one line per day and is committed with the report. Give the principal both paths and a five-line summary in plain language.

Formulas, sources and the config keys: `docs/metrics.md`.
