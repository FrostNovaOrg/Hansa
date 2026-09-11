---
name: return
description: Write a return packet - hand one task back to its lane without touching its criteria. Use when a receipt does not meet the packet and the gap is small enough to send back rather than re-scope.
---

# /bellhop:return

`BH="${CLAUDE_PLUGIN_ROOT}/bin/bellhop"`. A return creates a new packet id linked to
the original. The script inherits its criteria, forbids and Required context unchanged.

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

3. Keep the body within the `packet_bytes` guide. Omit `## Acceptance`: the script
   copies the original items. Changed criteria require a newly scoped task, not a return.
4. `$BH dispatch --lane <original-lane> --title "<return title>" --return <id> --body <file>`.
   The command records the new id, bell, in-flight row and card. Read its output and end
   the turn; do not repeat those state writes or edit the original packet.
5. If provenance is refused, run `$BH accept status <id>`. Restore original packet/worker
   evidence from history; never invent missing hashes. If recovery is impossible, scope
   a new task after resolving the lane's existing work through the normal recovery commands.
