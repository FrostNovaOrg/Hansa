---
name: resume
description: Bring the desk back after a pause - services, sentinel, the readings gap, the stop order, the card - in that order. Use at the start of the first session after a /bellhop:shutdown, or after a machine came back up.
---

# /bellhop:resume

`BH="${CLAUDE_PLUGIN_ROOT}/bin/bellhop"`. The reverse of `/bellhop:shutdown`, in order.
Do not skip a step because the last session "was fine": the resume list is the record,
your recollection is not.

1. `$BH state own list`. Read the resume list before anything else. Every `resume:`
   line is one of the steps below, or a step you add to them.
2. `$BH daemon install --load` if this desk runs resident - it refreshes the vendored
   copy and runs the load commands. `$BH daemon status` afterwards: the heartbeat must
   be fresh within 30 seconds. Session mode needs nothing.
3. `$BH sentinel --dry-run`. What it would do - wake, daily clear, compaction - before
   it does any of it on its timer. Read those three lines.
4. `$BH readings --week <YYYY-Www> --transcripts <path or dir>`, once per week the
   pause covered. A week nobody read is a permanent hole: no later run recomputes it.
5. `$BH stop-dispatch lift --note "resumed <date>"`. It writes the close-out line that
   says how long the desk stood paused.
6. `$BH state card set --dispatchable yes --next "<what>" --lane <lane>`, or
   `--dispatchable no --reason "<what we are waiting on, and who wakes us>"`. Put it
   back where the resume list says it stood, not where you would like it to be.
7. The two flags the list named:
   - the daily-clear marker, `ls state/.daily-clear-*` - no marker for today is normal
     before the window opens, and says nothing about the pause;
   - any shadow or acceptance run - back to whatever the resume list recorded, on or
     off. A shadow run left on writes to the shadow registers and the live ones go
     quiet, which reads exactly like a desk with nothing to do.
8. `$BH board`, then `$BH state own settle --match "resume: ..."` for each line you
   actually did. Ring the first bell only after the board is clean.
