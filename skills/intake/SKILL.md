---
name: intake
description: Take in one worker receipt - read its numbers, sample for acceptance, write one ledger entry, refresh the card, merge or reject. Use whenever a receipt has landed.
---

# /bellhop:intake

`BH="${CLAUDE_PLUGIN_ROOT}/bin/bellhop"`. One receipt at a time, four steps, in order.

1. **Read the numbers.** Open `receipts/<id>-<slug>.md`. For each acceptance item, find the command and its output. Read the footer (`bellhop: {...}`): exit code, dead flag, cost, turns. Do not re-run the worker's checks yourself.
2. **Sample.** `$BH sample <id>`. Exit 0 = sampled: run `$BH accept <id>` and read `receipts/<id>.acceptance.md`. Exit 3 = not sampled: take the receipt on its numbers.
3. **Record once.** `$BH ledger add --title "received <id> <short title>" --ref <id> --body "<what was received, what you decided, what is doubtful>"`. Under 1,200 bytes. A dead task's entry says so and names the next step.
4. **Move.** `$BH card --ready yes|no --next "..." --lane <lane> --reason "..."`, then `$BH merge <id>` (add `--verdict "..."` if sampled) or a ledger entry that rejects with the reason and the follow-up packet id.

Ring the next bell if there is one, then end your turn.
