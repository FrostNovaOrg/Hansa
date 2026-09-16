---
name: status
description: Show the desk's state - card, in flight, own list, debts, close-outs, intake, sentinel, bells, watchdog. Use when asked "what is going on" or before dispatching.
---

# /bellhop:status

`BH="${CLAUDE_PLUGIN_ROOT}/bin/bellhop"`.

1. `$BH board` — **once**. One page, quoted from the files and byte-capped: card, in flight,
   own list, debts, unread close-outs, the last intake titles, hot rules, the sentinel line,
   pending bells, watchdog heartbeat.
2. Do not open `state/*.md`, and do not run `card --show`, `sentinel` or `daemon status` to
   confirm what the board just said. The board is the read. If it says a file is not on disk,
   that is a finding, not a reason to go and look.
3. Only if one number on the board is disputed, run the command that owns it:
   `$BH sentinel --no-wake`, `$BH daemon status`, `$BH state closings unread`.
4. For a task with stalled or disputed acceptance, `$BH accept status <id>` checks current
   packet provenance and reports the last run. Use its recovery reason, not old PASS text.
5. **Answer in five lines or fewer**: what is running, what is waiting, what is red, what
   you will do next. Keep any actionable diagnostic inside those lines. No task ids or file
   paths unless the principal used them first. Go longer only when the principal asks for
   more detail.

Fix a red line through the command that owns the file (`state`, `card`, `ledger add`, `seal`),
never by hand. A last line reading `(trimmed N bytes)` means rows were dropped, in this order:
oldest debts, then intake titles, then close-outs; the card and in flight are never dropped.
