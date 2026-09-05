---
name: intake
description: Take in one worker receipt - read its numbers, sample for acceptance, write one ledger entry, refresh the card, merge or reject. Use whenever a receipt has landed.
---

# Intake

One receipt at a time. Four steps, in order.

1. **Read the numbers.** Open `receipts/<id>-<slug>.md`. For each acceptance item, find the command and its output. Read the footer line (`bellhop: {...}`): exit code, dead flag, cost, turns. Do not re-run the worker's checks yourself.
2. **Sample.** Run `bellhop sample <id>`. Exit 0 means sampled: run `bellhop accept <id>` and read `receipts/<id>.acceptance.md`. Exit 3 means not sampled: take the receipt on its numbers.
3. **Record once.** `bellhop ledger add --title "received <id> <short title>" --ref <id> --body "<what was received, what you decided, what is doubtful>"`. Under 1,200 bytes. If the task is dead, the entry must say so and name the next step.
4. **Move.** `bellhop card --ready yes|no --next "..." --lane <lane> --reason "..."`, then either `bellhop merge <id>` (add `--verdict "..."` if sampled) or a ledger entry that rejects with the reason and the follow-up packet id.

Then ring the next bell if there is one, and end your turn.
