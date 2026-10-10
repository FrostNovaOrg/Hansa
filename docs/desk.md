# The desk: files, keys and notes

## A desk's files

### Where a desk's files are

Everything is under the desk directory; see the top of [`hansa/desk.py`](../hansa/desk.py). Keys live
in `secrets/` (mode 600) and are never shown or sent anywhere but to the
provider's endpoint, as that worker's environment. hansa replaces a key with
`[key of <provider>]` wherever it would otherwise show, and refuses to merge a
change that holds one. The coordinator's permission rules are hansa's: they are
in the desk's `.claude/settings.json` and come up to date whenever the
coordinator starts. The principal's own rules go in `.claude/settings.local.json`.
The coordinator creates and edits files under the desk's `notes/` without
asking (its plan and findings; how it keeps them short is at the end of
[The daily clear](#the-daily-clear)); everything else it edits asks as before,
and the closed files stay closed.

The project's own rules (how it builds, what may never go out, who decides what)
go in the desk's `notes/PROJECT.md`. The card imports it with a line of its own,
`@notes/PROJECT.md` (Claude Code's import syntax for memory files), so Claude Code
loads it with the card at every start, after `/clear` and after compaction, and the
import survives every card refresh. `hansa init` makes the file once, holding only
an HTML comment that says what it is for (Claude Code drops such comments, so it
costs nothing until something is written there); after that hansa rewrites it
only when the coordinator asks, with `hansa notes archive PROJECT.md HEADING`. A
missing file loads nothing and stops nothing. The coordinator and the principal keep it; the
coordinator edits it without asking, as anything under `notes/`. Workers do not
see it: rules for a lane's workers go in its standing instructions. `hansa status` shows its size,
and calls it long past 8 KB: it is part of the conversation's first message, so
every turn rereads it, and 8 KB (some 2,000 tokens) is about the 200 lines Claude
Code advises a memory file to stay under.

The coordinator's session does not load the claude.ai connectors of the Claude
account it logs in with (Claude Code's `disableClaudeAiConnectors`): a connector
is a way to publish outside the machine, and it adds its tools to every turn.
MCP servers set up on the machine stay. hansa also turns off Claude Code's
guessed next message in the coordinator's input box (prompt suggestions); in a
tmux capture without colors that grey guess looked typed. With an older
Claude Code, where a grey line still shows there, the principal should not press
Enter on what looks like an empty box: it may send the guess as if they wrote it.

### Whole files and event lines

Hansa writes a replacement beside its destination and renames it only after the write and close succeed.
An `OSError` during writing, closing or replacement leaves the previous file intact (or no file when it was
new), and removes the temporary file. Replacements keep the destination's permission bits, including `0600`;
new files keep their previous defaults under the umask, and keys, tokens and the lane environment stay `0600`.
Publishing a run's output removes its raw source only after the whole copy has been installed.

Event appends hold a lock on `events.jsonl` across checking its last byte and writing the next event. If the
previous append left a line without a newline, the next append first supplies one, in the same write as the
new event. Hansa never truncates the log to undo a failed append. A damaged line stays there and readers skip
it; a complete event missing only its newline remains readable. This also applies to the fallback
`alert_error` append. The follower (`notify.read_log`), the page's latest and filtered events (`web._events`),
and lane history (`views.lane_origin`) all skip invalid JSON, invalid UTF-8 and lines that are not objects.
Successful writes keep the same contents and number of payload writes; no extra event or record is written
each round. This concerns file writes, rather than what a command or server round does after an I/O error.

The write inventory below groups callers that use the same writer. The failure column describes the tests
in `tests.test_hansa.FailedWriteTest`: replacement rows check old bytes, no leftover temporary, and a file
starting at `0600`. New-only rows check that the destination stays absent and a retry works.

| Desk file and writer | Write fails part way |
|---|---|
| JSON records through `desk.write_json`: settings, task records, requests, provider state, heartbeat and runtime records (disk, sandbox, server, followers, alert and release state) | Whole replacement; JSON row in `test_partial_replacement_matrix` |
| `secrets/<provider>.key`, `Desk.write_key` | Whole replacement at `0600`; private-files matrix |
| `PAUSED`, `Desk.pause` | Whole replacement; replacement matrix |
| `tasks/<id>/task.md`, `Desk.new_task` | Whole creation; failed-creation matrix |
| Published `run.json`, `run.err`, `setup.log`, `check.log`, `merge-check.log`, `merge-setup.log`, `merge-check.err`, `preview.log`, `preview-before-resume.log`, `run.publish` | One replacement row per name; raw source is also checked intact |
| Published task text/results/logs and lanes' `try.log`, `merge-check-try.log`, `Desk.scrub` | Whole replacement; private-files matrix covers each text name; JSON scrubs use `write_json` |
| Initial `lanes/<lane>/brief.md`, `CLAUDE.md`, `notes/PROJECT.md`, `cli.init_desk` | Whole writes; other-sites matrix, three rows |
| Follow-up `tasks/<id>/note.md`, `cli._retry` | Whole write; other-sites matrix |
| `.hansa/web-port` and `web-host`, `cli.cmd_serve` | Whole writes; other-sites matrix, two rows |
| Refreshed card, helpers and their timestamped `.hansa/` backups, `cli._bring` | Whole writes; other-sites matrix covers both destination and backup expressions |
| Copied, new and edited lane briefs, and `brief-before-*.md`, `cli.cmd_lane` | Whole writes; other-sites matrix, four rows |
| `.hansa/runs/<id>/lane-env.sh`, `run.env_file` | Whole replacement at `0600`; private-files matrix |
| Task `browser-mcp.json`, `run.browser_for` | Whole replacement; other-sites matrix |
| `.hansa/runs/<id>/worker-argv.json`, `run.start` | Whole replacement with the existing JSON format; other-sites matrix |
| Task `result.md`, `run.finish` | Whole replacement; other-sites matrix |
| Lane `try.log` and `merge-check-try.log`, `run.keep_try_log` | Whole replacements; private-files matrix also checks the writer's failure return |
| Design startup `run.err`, `session.main` | Whole replacement; other-sites matrix |
| `.hansa/web-token`, `Desk.web_token` | Whole creation at `0600`; failed-creation matrix checks retry |
| `secrets/release-code.hmac`, `release._code_key` | Whole creation at `0600`; failed-creation matrix checks retry |
| Finished `.hansa/releases/<id>.log`, `release.finish` | Whole replacement after redaction; other-sites matrix |
| Raw preview state, `preview.main.save` | Whole replacement with the existing JSON format; other-sites matrix |
| Git evidence `what-was-found.txt`, `run.git_file_changed` (two branches) | Whole writes; other-sites matrix, two rows |
| Git evidence `config` and files under `hooks/`, `desk.copy_file` used directly and by `copytree` | Whole copies, including copied metadata before replacement; private-files matrix covers both paths |
| Check and merge-check `.exit`, `run.CHECK_SCRIPT` | Whole writes through the shared writer; shell-completion matrix |
| Try `<step>.exit`, `run.TRY_SCRIPT` | Whole write, with the same exit code and elapsed seconds; shell-completion matrix |
| Release `<id>.exit`, `release.SCRIPT` | Whole write; shell-completion matrix |
| Notes source and archive destination, `notes._write` | Already whole replacements with cleanup and fsync; private-files matrix covers both paths |
| Ended tasks' trimmed logs, `disk._trim_one` | Whole replacement, now preserving permissions too; private-files matrix checks the failure return |
| Command text and git-open probe script in a run's temporary folder, `run.around_note` and `run._srt_writes` | Whole writes; other-sites matrix covers both; these inputs normally live outside the desk |
| `events.jsonl`, primary and `alert_error` appends | Append-only; failed-append tests keep the damaged prefix, then retain the next event; the two-process test retains every good event |

For the larger command functions, the other-sites matrix executes their actual write expressions with
representative inputs, so it exercises each destination without starting a server, worker, release or git
operation. Additional tests cover close, chmod and rename errors, successful contents and permission bits
under a restrictive umask, one payload write per replacement or append, and successful short event writes.

The following writes intentionally remain in place:

| Place | Why it can stay |
|---|---|
| Worker/design stdout and stderr in raw `run.json` and `run.err`; setup, check, merge-check and try logs; the release's running log (`run` wrappers, `release._launch`) | These are live process output streams. Their published or finished copies use whole replacement. |
| Raw preview output, `preview.Kept` | This is a live process log, with its capped tail rewritten while output arrives; it is published whole after the preview ends. |
| Follower `.log`, `notify.spawn`, and the optional `HANSA_NOTIFY_CAPTURE` file, `notify.post` | Appended process output; capture is a test facility, not the desk event log. |
| `.hansa/lock`, `alert.lock` and follower `.lock` files | Empty lock files have no payload to be part-written. Their inode must remain stable for holders and waiters. |
| `.hansa/serve.lock`, `cli.hold_server_lock` | Its PID text is attached to the inode that the server locks. Replacing it would let another server lock a different inode and start. It remains a lock file, not a replaceable record. |
| Empty lane brief creation on a page save, `web._save_settings` (`touch`); setup `.done` marker, `SETUP_SCRIPT` | Neither writes a payload; an existing empty file cannot become half-written. |
| Sandbox commands' scratch Maven settings under their own TMPDIR, `JAVA_PRELUDE` | Private, disposable command input, never a published desk record; its command's temporary directory is removed when the command ends. On Linux that TMPDIR is under `.hansa/tmp/`. |
| Git's temporary config/excludes in the Linux prelude, missing empty git-deny placeholders, git-open probes, and worktree git-file repairs | Git and sandbox scratch, including the worktree's git state, are outside this change. These do not replace the desk's settings, task records or published output. |

### Where a desk's files grow

| What | Where | How big |
|---|---|---|
| A task's text, record, report and result | `tasks/<id>/task.md`, `meta.json`, `result.md`, `run.json` | a few KB to some tens of KB each; kept whole |
| What a task's processes printed | `tasks/<id>/run.err` (the worker's errors), `setup.log`, `check.log`, `merge-check.log`, `merge-setup.log`, `merge-check.err` | whatever the lane's setup, check and merge check print: not capped while the task is open (a verbose build can print hundreds of MB) |
| A preview's output | `tasks/<id>/preview.log`, `preview-before-resume.log` | at most 4 MB each, its first and last 2 MB |
| A browser task's screenshots | `tasks/<id>/browser/` | as many as the worker takes; kept whole |
| A lane's last try | `lanes/<lane>/try.log`, `merge-check-try.log` | at most 4 MB per step (its first and last 2 MB), replaced by the next try |
| The event log | `events.jsonl` | a few hundred bytes per event, append-only; never trimmed |
| A release's output | `.hansa/releases/<id>.log` | whatever its command prints; kept |
| A running task's output | `.hansa/runs/<id>/` | moved into `tasks/<id>/` when it ends |
| Each lane's worktree | `worktrees/<lane>/`, `worktrees/<lane>-design/` | the repository's checkout, and what builds leave in it (see [Free space](#free-space)) |
| A worker run's temporary folder | `/tmp/hansa-<id>-...` | what the worker's commands put there; removed when the run ends |
| A lane command's TMPDIR, on Linux | `.hansa/tmp/hansa-check-...` | what a setup's, check's, merge check's or try's build writes there; removed when the command ends |

Since a task's logs are not capped, once a day `hansa serve` trims those of every task that ended (merged,
cancelled, failed or replaced) more than 30 days ago: each of the logs above (not the text, record, report or
result, nor the screenshots) is cut to its last 256 KB, from the start of a line, under a first line that says
it was trimmed, when, and how big it was. The task's record notes it, and `hansa show` says which logs were
trimmed, when, and their size before. A task still open, or one that ended less than 30 days ago, keeps all of
its logs; the event log is not touched.

## Hearing from hansa

hansa posts to the coordinator's Claude Code session through the session's
inbox (a local socket every interactive session listens on) when something
needs the coordinator: a task ended (after hansa's own check), failed, was
cancelled, stopped at its ceiling or could not start; a verification's verdict;
a design session ended; a merge check ended; the principal answered or closed a
request (with the answer); the principal answered a release; a release ended
(done, or failed with its exit code and last lines) or became void; a provider
an open request names became ready when it was not as the request was made
(once, looked at on a key or settings save, a `hansa check`, or its rest after a usage
limit running out; the request may be done: if it is, the coordinator closes it
with `hansa ask --close N "<why>"`); `hansa serve` stopped, or, still running,
finished no round for 5 minutes (busy or stuck), and then that it runs again;
srt not running; free space went under the floor (`hansa disk-floor`), and then
that it is back over it. A server busy for less than that is not news, and
nothing the coordinator did itself and saw is posted to it.

Each message starts with `[hansa]` and the desk, with one line per event and the
next command; events seconds apart come in one message, and past 10 lines the
rest are counted (`hansa status` has them). A follower does the posting: a small
background process per session and desk, which reads the event log from where it
left off and ends when the session's process does; one that starts after
another ended first posts what was missed meanwhile, as it is now.

Which session hears what: a task or request goes to the session that made it
(`hansa add`, `retry`, `verify`, `merge`, `design` and `ask` record it) while
that session has a follower; otherwise to every session hansa posts to, as do
desk-wide events. A cancel is not posted to the session whose `hansa cancel`
made it. A session started with `hansa coordinator` is posted to by itself;
any other session runs `hansa notify on` once (`hansa notify off` stops it).
`hansa notify` lists the sessions hansa posts to, and `hansa explain notify`
says all of this.

While hansa posts to a session, that session does not keep `hansa wait`
running in the background; `hansa wait` is for a session hansa does not post
to. Since hansa posts when a task, a verification or a merge ends and when the
principal answers, the coordinator needs no scheduled wakeups and no polling to
find out: it ends its turn and waits. Each wakeup or poll is a turn that rereads
the whole conversation, and costs that much even when it finds nothing to do.

These lines come from hansa, not from the principal, and approve nothing. A
session whose Claude Code settings hold or refuse messages from other sessions
(`crossSessionInbound`) does not get them; only the principal can change that.

### The daily clear

A coordinator's session can run for days. Each time its context fills, Claude
Code compacts it into a summary; the next compaction summarizes that summary
again together with everything since. The summary keeps growing, the session
starts each stretch heavier, compaction comes sooner, and early detail drifts.
`--clear-daily HH-HH` (`hansa coordinator --clear-daily 04-09`, or `hansa notify
on --clear-daily 04-09`) clears the session once a day while it is idle, so it
starts again from the desk's state, not from a summary of summaries. A session
started with `hansa coordinator` is shown `hansa status` after the clear.
Automatic compaction keeps a full session going; it does not do what the daily
clear does.

It is off by default and needs the session to run in tmux. Once a day, in that
window of hours (local time; `22-03` runs past midnight), the follower types
`/clear` into the session's tmux pane, only when `claude agents --json` shows
the session idle on two readings a minute apart, hansa posted nothing to it for
5 minutes, no key was pressed in a terminal on that tmux session for 90
seconds, and the input box is empty; it presses Enter only when the box then
reads exactly `/clear` and the session is still idle. Otherwise it takes the
`/clear` back only when the box ends in it, and tries again later; when the box
cannot be read, holds something else, or tmux fails, it leaves the box as it is
and counts the day as tried, and so it does when the session's id does not
change within 15 seconds of Enter. Anything it cannot read counts as busy. A
session that started inside the day's window, after it opened, counts as
cleared already: its next clear is the next day. `hansa notify` shows each
session's window and the day's state (cleared at a time, held and why, the
window passed without a clear, or off and why); the follower's log has each
reason it held.

So the coordinator keeps what must survive in the desk (tasks, requests, and
its notes under `notes/`), not only in the conversation. It keeps the notes file
it rereads each time it picks the work up short, since each reading costs it:
the current work and open items only. It moves finished rounds to another file
there that it does not reread, without asking the principal and without typing
them out again, with `hansa notes archive FILE HEADING` (to `notes/archive/FILE`
unless `--to` names another); `hansa notes` lists the files with their sizes and
headings. It does not `mv`, `cp` or `sed` them: a shell command there asks.

## Free space

Desks run builds and tests many times a day, and a full disk breaks everything on the machine, the desk's own
records among them. So `hansa serve` keeps a floor of free space. Each round it looks at the free space of each
volume it and its runs write on: the desk's folder, each lane's repository and worktree, the folder worker runs get
their temporary folders in (`/tmp`) and its own TMPDIR (where the setups', checks' and tries' are made; on Linux
their own TMPDIR is in the desk's `.hansa/tmp`, on the desk's volume, which is named as a place too). While any
of them has less free than the floor, it starts no new task, verification, try, merge or merge check, design
session or release. What already runs goes on, a running task's check and a resumed worker among it. It says why:

- `hansa status` has a line naming the volume, the places on it, its free space and the floor (`DISK LOW: ...`),
  and always one with the free space of the desk's volume and the floor, also when nothing is under it;
- the page shows the same in its status line, and the floor next to the free space under Settings, General;
- `hansa explain server` lists each volume with its free space;
- a queued task, merge or release says it waits for space;
- the coordinator's session is told once when a volume goes under the floor and once when all are back over it,
  and the principal's alert command (when one is set) runs once, with `HANSA_ALERT_KIND=disk`.

It starts again by itself once every volume has the floor and 1 GB more free, so that it does not stop and start
at every round around the line. A release asked meanwhile waits, approved, and goes once there is space.

The floor holds for each volume by its own size. A volume smaller than four times the floor has a quarter of its
size as its floor: on a Linux server that mounts `/tmp` as a tmpfs (systemd's default size is half the memory), an
8 GB machine has a 3.9 GB `/tmp`, which under the default floor of 10 GB holds back only under about 1 GB free,
not always. The margin it needs before `hansa serve` starts again is scaled with it, to no more than a quarter of
that volume's floor (about 0.1 GB there). A volume of four times the floor or more (40 GB under the default) keeps
the floor as set, and so does a volume whose total size is not known. Where a volume's floor is not the setting,
the `DISK LOW` line, what a queued task, merge or release says, `hansa explain server` and the page give the one
that applies to it (`floor 1.0 GB for this 3.9 GB volume`), and say how much each volume needs before it starts
again.

Each place is looked at on its own. One that cannot be looked at (a lane's repository in a folder the account
cannot read, say) is left out, and only it: the others are measured and hold as before, and `hansa status`, the
page and `hansa explain server` say which place cannot be looked at, since when and why, and that the free space
of its volume is not known and holds nothing back. If the look fails as a whole, `hansa serve` holds nothing back
while it fails, and `hansa status` says so with what the last look that worked measured and when, rather than a
`DISK LOW` that no longer holds. Either is an event (`disk_look_failed`, and `disk_look_ok` once it can look again)
when it changes, not each round, and the coordinator's session hears it.

The floor is the coordinator's, as concurrency is: `hansa disk-floor` shows it with the free space of the desk's
volume, `hansa disk-floor 20` sets it to 20 GB (default 10; 0 turns it off for every volume). It is one number:
a small volume's own floor follows from it, as above. The page shows it and cannot change it.

Free space rather than lower the floor. A lane whose builds leave large outputs in its worktree (`target/`,
`build/`, `node_modules/`, a compiler's cache), or fill a cache of its own (a Maven or pip cache on its write
list), should clean them in its setup, which runs before each task: `mvn -q clean` or `rm -rf build`, or a cache
pruned past a size, keeps the worktree from growing with every task.
