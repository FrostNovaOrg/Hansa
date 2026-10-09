# The desk: files, keys and notes

Everything is under the desk directory; see the top of [`hansa/desk.py`](../hansa/desk.py). Keys live
in `secrets/` (mode 600) and are never shown or sent anywhere but to the
provider's endpoint, as that worker's environment. hansa replaces a key with
`[key of <provider>]` wherever it would otherwise show, and refuses to merge a
change that holds one. The coordinator's permission rules are hansa's: they are
in the desk's `.claude/settings.json` and come up to date whenever the
coordinator starts. Rules of your own go in `.claude/settings.local.json`.
The coordinator creates and edits files under the desk's `notes/` without
asking (its plan and findings, kept past a cleared session); everything else it
edits asks as before, and the closed files stay closed. `hansa notes archive FILE HEADING`
moves a finished section from a notes file to `notes/archive/FILE` without the
coordinator typing it out again.

The project's own rules (how it builds, what may never go out, who decides what)
go in the desk's `notes/PROJECT.md`. The card imports it with a line of its own,
`@notes/PROJECT.md` (Claude Code's import syntax for memory files), so Claude Code
loads it with the card at every start, after `/clear` and after compaction, and the
import survives every card refresh. `hansa init` makes the file once, holding only
an HTML comment that says what it is for (Claude Code drops such comments, so it
costs nothing until something is written there); after that hansa rewrites it
only when the coordinator asks, with `hansa notes archive PROJECT.md HEADING`. A
missing file loads nothing and stops nothing. The coordinator and you keep it; the
coordinator edits it without asking, as anything under `notes/`. Workers do not
see it: rules for a lane's workers go in its brief. `hansa status` shows its size,
and calls it long past 8 KB: it is part of the conversation's first message, so
every turn rereads it, and 8 KB (some 2,000 tokens) is about the 200 lines Claude
Code advises a memory file to stay under.

The coordinator's session does not load the claude.ai connectors of the Claude
account it logs in with (Claude Code's `disableClaudeAiConnectors`): a connector
is a way to publish outside the machine, and it adds its tools to every turn.
MCP servers you set up on the machine stay. hansa also turns off Claude Code's
guessed next message in the coordinator's input box (prompt suggestions); in a
tmux capture without colors that grey guess looked typed. If you run an older
Claude Code where a grey line still shows there, do not press Enter on what
looks like an empty box: it may send the guess as if you wrote it.

## Free space

Desks run builds and tests many times a day, and a full disk breaks everything on the machine, the desk's own
records among them. So `hansa serve` keeps a floor of free space. Each round it looks at the free space of each
volume it and its runs write on: the desk's folder, each lane's repository and worktree, the folder worker runs get
their temporary folders in (`/tmp`) and its own TMPDIR (where the setups', checks' and tries' are made). While any
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

Each place is looked at on its own. One that cannot be looked at (a lane's repository in a folder the account
cannot read, say) is left out, and only it: the others are measured and hold as before, and `hansa status`, the
page and `hansa explain server` say which place cannot be looked at, since when and why, and that the free space
of its volume is not known and holds nothing back. If the look fails as a whole, `hansa serve` holds nothing back
while it fails, and `hansa status` says so with what the last look that worked measured and when, rather than a
`DISK LOW` that no longer holds. Either is an event (`disk_look_failed`, and `disk_look_ok` once it can look again)
when it changes, not each round, and the coordinator's session hears it.

The floor is the coordinator's, as concurrency is: `hansa disk-floor` shows it with the free space of the desk's
volume, `hansa disk-floor 20` sets it to 20 GB (default 10; 0 turns it off). The page shows it and cannot change
it.

Free space rather than lower the floor. A lane whose builds leave large outputs in its worktree (`target/`,
`build/`, `node_modules/`, a compiler's cache), or fill a cache of its own (a Maven or pip cache on its write
list), should clean them in its setup, which runs before each task: `mvn -q clean` or `rm -rf build`, or a cache
pruned past a size, keeps the worktree from growing with every task.

## Where a desk's files grow

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
| Each lane's worktree | `worktrees/<lane>/`, `worktrees/<lane>-design/` | the repository's checkout, and what builds leave in it (see above) |
| A worker run's temporary folder | `/tmp/hansa-<id>-...` | what the worker's commands put there; removed when the run ends |

Since a task's logs are not capped, once a day `hansa serve` trims those of every task that ended (merged,
cancelled, failed or replaced) more than 30 days ago: each of the logs above (not the text, record, report or
result, nor the screenshots) is cut to its last 256 KB, from the start of a line, under a first line that says
it was trimmed, when, and how big it was. The task's record notes it, and `hansa show` says which logs were
trimmed, when, and their size before. A task still open, or one that ended less than 30 days ago, keeps all of
its logs; the event log is not touched.
