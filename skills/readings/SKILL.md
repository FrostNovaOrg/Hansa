---
name: readings
description: Produce the weekly readings report - tasks by lane and model, dead tasks, cost, commits per task, ledger growth - and fill the outcome numbers. Use at the end of a week or when the principal asks how the team is doing.
---

# /bellhop:readings

`BH="${CLAUDE_PLUGIN_ROOT}/bin/bellhop"`.

1. `$BH readings --week <YYYY-Www>` (omit `--week` for the current week). Add `--transcripts <dir>` for each Claude Code project directory whose output tokens should be counted.
2. Open `reports/weekly/<week>.md` and fill the two outcome numbers by hand from the ledger: report-to-release hours and post-release defects. If you cannot find them, write "not recorded" and add a debt to record them next week.
3. Read the process numbers as diagnostics, not scores. For anything that moved a lot, write one sentence on the likely cause.
4. Commit is automatic. Give the principal the report path and a five-line summary in plain language.
