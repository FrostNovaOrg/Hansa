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
2. **Accept every task.** Run `$BH accept <id>` and read
   `receipts/<id>.acceptance.md`. `$BH sample <id>` selects additional review
   (exit 0 selected, exit 3 not selected); it never permits skipping acceptance.
   The verdict file is what the next step reads, with `VERDICT: pass | return | doubt`.
   If it says `return` or `doubt`, run step 3 to record the refusal and stop; it
   merges nothing. A stalled run needs diagnosis through `$BH accept status <id>`;
   leftover verdict text from a stalled run is not approval.
   For packet provenance failures, restore the original evidence from history or scope
   a new task. `--force` reruns a verifier; it does not approve changed task inputs.
3. **Close it.** `$BH close-task <id> --next "<what happens next>" --note "<one
   sentence for the ledger>"`. One command, six named steps: evidence, fileset, merge
   with the full board run on the merged result, ledger, state, one commit. It stops
   at the first failure with `🛑 stopped at: <step> — <reason>` and writes
   `receipts/<id>.close.json` saying how far it got.
   - Read the stop line, fix that one thing, run the same command again: it skips the
     steps that already ran.
   - Missing/stale acceptance binding needs a fresh `$BH accept <id> --force` after
     worker provenance is valid. Do not manufacture metadata or change the executed packet.
   - `--dry-run` first if you want to see the ledger entry before it is written.
   - Do not merge, write the ledger, refresh the card or settle debts by hand. Seven
     hand-run steps are what this replaced, and in the records a task closed halfway
     looks exactly like a task closed cleanly.

Ring the next bell if there is one, then end your turn.
