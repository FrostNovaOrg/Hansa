---
name: close
description: Daily close - verify seals, run the sentinel, write the day's one-line summary to the ledger and commit. Use at the end of a working day.
---

# /bellhop:close

`BH="${CLAUDE_PLUGIN_ROOT}/bin/bellhop"`.

1. `$BH close`. It verifies seals, runs the sentinel, and appends a ledger entry with the day's counts.
2. If seals are red because scripts or frozen documents changed deliberately, `$BH seal` and say what changed in a ledger entry. If they changed and nobody meant to, stop and tell the user.
3. Make sure `state/human-todo.md` lists everything only the principal can do, each line with "why only you".
4. End with the day's summary line, nothing more.
