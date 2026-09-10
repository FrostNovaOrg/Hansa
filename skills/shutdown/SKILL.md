---
name: shutdown
description: Pause the whole desk - stop dispatch, settle what is running, stop the resident services, close the day, and write the resume list. Use when a quota runs out, before the machine goes down, or when the principal says stop.
---

# /bellhop:shutdown

`BH="${CLAUDE_PLUGIN_ROOT}/bin/bellhop"`. Every check below is a command. A pause held
in your head and a pause written into the files look the same until the desk comes
back; only one of them survives a `/clear`.

1. `$BH stop-dispatch set --reason "<why, and what lifts it>"`. Nothing goes out from
   here on.
2. `$BH board`. For every in-flight line: wait for its receipt, or tag it
   `$BH state inflight tag --task <id> --tag settled-awaiting-close`. An untagged line
   and a finished task look the same tomorrow.
3. `$BH daemon status`. If a resident service is running, stop it with the commands for
   the platform it names - macOS `launchctl bootout gui/$(id -u)/<label>`, Linux
   `systemctl --user stop <label>.service <label>-sentinel.timer`. Do **not** run
   `daemon uninstall`: a pause must not delete the service files, or the resume becomes
   a reinstall.
4. `$BH close`. Seals, sentinel, the day's ledger entry.
5. Write the resume list, one line each, with the command:
   `$BH state own add --text "resume: <what>"`. It must carry at least
   - any shadow or acceptance run that was on, and whether it comes back on or off,
   - the card's dispatchable flag and reason exactly as they stand,
   - every day `bellhop readings` has not covered yet,
   - the stop order's reason, so the resume knows what it is lifting.

   This is the step a hand-written checklist misses; three of those four have been
   missed at once.
6. Verify, each line from its own command and none from memory:
   - `$BH board` - pending bells 0, every in-flight line tagged
   - `$BH seal --verify` - green
   - `git status --short` - clean; `git log --oneline @{u}..HEAD` - the unpushed count
     (with `push: false` a non-zero count is the normal state, so report the number
     rather than calling it red)
   - `$BH stop-dispatch status` - the order stands, with its reason
   - `$BH state own list` - the resume list, read back
7. Print those five readings in five lines. If any is red, say so and do not call the
   desk paused.
