---
name: return
description: Write a return packet - hand one task back to its lane without touching its criteria. Use when a receipt does not meet the packet and the gap is small enough to send back rather than re-scope.
---

# /bellhop:return

`BH="${CLAUDE_PLUGIN_ROOT}/bin/bellhop"`. A return is not a new packet. The criteria
already exist; rewriting them while returning is how a task ends up accepted against a
standard nobody agreed to. Returns with no fixed form produced
a different document each time.

1. Read the receipt first (`/bellhop:intake` if it has not been taken in). A return
   written without the receipt's numbers is an opinion.
2. Write the body to a file, with exactly these four sections, in this order and with
   these headings:

   ```
   ## Criteria (unchanged)
   Cite the packet - `inbox/<lane>/<id>-<slug>.md`, by item number. Do not restate them,
   and do not add one.

   ## Why returned
   One sentence: the criterion that is not met, and the line of the receipt that shows it.

   ## Change
   What to do. Files and commands, not adjectives.

   ## Do not change
   What already passed and must survive the second attempt.
   ```

3. Keep the body under the config cap `packet_bytes`. Over it, cut prose - never a
   section. Four sections and three sections are different documents.
4. `$BH dispatch --return <id> --body <file>`. That is the only entry point; do not
   write a fresh packet and do not edit the original one.
5. Record it once:
   `$BH state closings add --task <id> --line "returned: <the one sentence>"` so it
   shows on the principal's page, then `$BH state ledger add --file -` with what came
   back, which criterion, and what was sent out again.
6. `$BH state card set --dispatchable no --reason "waiting on the return of <id>"`,
   then end your turn. The watchdog answers the bell.
