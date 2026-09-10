---
name: intake
description: Take in one settled task - read its closing line, decide accept or return, then close it with one command. Use whenever a receipt has landed or a close-out is unread.
---

# /bellhop:intake

`BH="${CLAUDE_PLUGIN_ROOT}/bin/bellhop"`. One task at a time, three steps.

1. **Read the closing line.** `$BH state closings unread` prints the close-outs you
   have not read; each says a task is `settled, awaiting close`. Open
   `receipts/<id>-<slug>.md` and read the numbers in its header block: base, head,
   files changed, the lane check's exit code, cost, turns. Do not re-run the worker's
   checks yourself.
2. **Decide.** `$BH sample <id>`: exit 0 means this one is checked in depth, so run
   `$BH accept <id>` and read `receipts/<id>.acceptance.md`; exit 3 means take it on
   its numbers. The verdict file is what the next step reads, and its last line is
   `VERDICT: pass | return | doubt`. Returning it is a verdict, not a message: make
   the file say `VERDICT: return — <one sentence>` and still run step 3, which
   records the refusal as a close-out and merges nothing.
3. **Close it.** `$BH close-task <id> --next "<what happens next>" --note "<one
   sentence for the ledger>"`. One command, six named steps: evidence, fileset, merge
   with the full board run on the merged result, ledger, state, one commit. It stops
   at the first failure with `🛑 stopped at: <step> — <reason>` and writes
   `receipts/<id>.close.json` saying how far it got.
   - Read the stop line, fix that one thing, run the same command again: it skips the
     steps that already ran.
   - `--dry-run` first if you want to see the ledger entry before it is written.
   - Do not merge, write the ledger, refresh the card or settle debts by hand. Seven
     hand-run steps are what this replaced, and in the records a task closed halfway
     looks exactly like a task closed cleanly.

Ring the next bell if there is one, then end your turn.
