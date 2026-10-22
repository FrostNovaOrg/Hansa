# The desk: files, keys and notes

## Status and cost on the web page

The browser tab and the caption beside Hansa name the desk by its parent folder
and its own folder, such as `work/desk`. The tab title is `Hansa · work/desk`,
prefixed with the number of releases waiting for approval plus open requests
when that count is nonzero. It updates with each status refresh.

The header summary presents its facts as separate items: desk, dispatch, server,
version, install time and current time, followed by any status notes. Each item
that needs attention, such as paused dispatch or a stopped server, stands out
on its own. On the Chinese page, the desk and version items are labelled
`工作台` and `hansa 版本`; the English current-time item uses `now`, as `hansa status` does.

Status shows the last 24 hours in summary cards, keeps dispatch controls separate
from task tables, and places release decisions and requests near the top. Empty
states explain where new activity will appear. Task details remain open across
refreshes. The page refreshes every five seconds while Status is visible, unless
you are filling in an input. Release approvals, refusals, stops and request replies
use the same actions as before.

Cost offers the last 1, 7 or 30 days, with token usage, estimated value and unpriced
tokens above the six category tables. Unpriced models and unreadable coordinator
usage remain explicit warnings; dollar values are an estimate, not a bill. A
failed range change identifies the old report that remains visible and disables
export until a report loads successfully. Retry requests the selected range.
CSV export keeps the original rows and columns, uses the range selected when the
export started, and reports success or failure. Both pages share the settings
page's native CSS and JavaScript controls and reduced-motion support.

The page keeps navigation for the current browser tab in `sessionStorage`:
`hansa.tab` is written whenever Status, Settings or Cost is shown (including
startup), `hansa.settingsSection` whenever a Settings section is shown, and
`hansa.costPeriod` when the Cost range changes. Reloading restores those choices;
a fresh browser tab starts on Status. An address containing `?t=` (or `&t=`)
always opens Status, where releases and requests wait for the principal.
Unknown saved choices fall back to Status, Providers and 7 days.
Scroll position and expanded Status details are not stored.

The small language selector at the end of the header, after the status summary,
offers `中文` and `English`. Choosing writes `hansa.language` in `localStorage`
(`zh-CN` or `en`) and reloads, keeping navigation while discarding unsaved Settings
text without a draft warning. The choice applies across visits and browser tabs;
without a valid choice, a browser language starting with `zh` selects Chinese,
and other languages select English. The HTML `lang` attribute matches the shown
language. If storage is unavailable or throws, the page still works with its
usual defaults for the choices it cannot read or keep.

## Settings on the web page

Settings are grouped into Providers, Coordinator, Lanes (read only), What workers
may not read, Releases, Alerts and Prices. Select a group in the directory; full
explanations and other read-only lane fields expand in place. Network, write and
allowed-command rules remain visible, as do the warnings needed before saving.
Providers includes the rest minutes that apply to every provider.
The save bar applies to all editable settings groups.
The visible not-read warning says that while the list holds an entry containing
`()[]{}*?\`, hansa starts no worker, verifier or coordinator until it is changed here.

The directory marks groups with unsaved edits. The save bar distinguishes edits,
an operation in progress, success and failure; a failed save says that the draft
is still present. Known validation errors link to the relevant field or group,
with field errors shown beside the control. Narrow screens use a horizontal
group directory. Controls have visible keyboard focus, and brief transitions
follow the browser's reduced-motion preference.

Switching between Status, Settings and Cost, or between settings groups, keeps
unsaved edits. The page marks them and asks the browser to warn before a refresh
or close. Drafts live only in page memory: they do not survive leaving the page.
Save settings writes them; a failed save keeps them. During a save the settings
form is disabled until the saved settings have been reloaded. Discard confirms
before replacing a draft with the desk's latest settings; with no draft it simply
reloads them. If a save meets newer settings, the server refuses it. The page
reloads the latest settings and keeps each top-level setting changed in your
draft, taking the others from the new copy. It names the settings changed
elsewhere and those changed by both using the page's group names in its language,
and shows any problems returned with the new copy alongside the notice. Your
draft value is kept for settings changed by both. Typed
keys remain. Nothing is saved until you press Save again. A failed reload keeps
the draft too. Unsaved markers compare values regardless of object key order;
a blank optional field counts as unset. Names typed for a new provider or price
also count as drafts, even before Add. If a save succeeds but its reload fails,
the page says that the save succeeded.

Keys are saved separately with each provider's Save key button. An unsaved key
input stays in memory across page and group switches, and across a settings save;
it is never included in the settings request or put in browser storage. A
confirmed discard clears those inputs too. Keys already saved are unaffected.
The eye button shows or hides the current key input without saving or changing it.
With no draft, returning to Settings refreshes it from the desk. If you start
editing before that reply arrives, the reply leaves your new draft alone.

## Starting the server and the coordinator again

Run `hansa up` in a desk, or name it with `hansa --desk DIR up` or `HANSA_DESK`.
If the named folder is not a desk, hansa refuses it and starts nothing; it does
not fall back to the account's desk list.
It starts the server as it last ran, then the coordinator's session as it last
started ([below](#the-coordinators-session)). A running server, including a busy or stuck
one, is left running and its pid is shown. It does not arrange startup at boot,
or stop or restart a running server or session.

Each `hansa serve` start saves `.hansa/serve-start.json`: the absolute path of
its installation's `bin/hansa`, `--host`, `--port`, `--every`, and the variables
that were set: `PATH`, `LANG`, every `LC_*` (including `LC_ALL`), `HANSA_CLAUDE`,
`HANSA_TMUX`, `CLAUDE_CONFIG_DIR`, `TMPDIR`, `XDG_RUNTIME_DIR`, `XDG_STATE_HOME`
and `TMUX_TMPDIR`. In tmux it also saves the server and session. A socket in
tmux's own directory is saved by server name (`-L`), so tmux can recreate the
directory after a reboot; a custom socket keeps its path (`-S`). A manual
foreground `hansa serve --port N` works as before and updates this record.
`hansa serve` is the dispatcher and web server that `hansa up` runs.
The command path keeps links to the installation, so switching such a link
makes future starts use the new installation.

The record never includes `ANTHROPIC_*`, other key variables, values holding
one of the desk's keys, or a Claude Code session's identity or messaging
variables. hansa removes the session's identity before recording the start.
The restarted server gets only `HOME`, `USER`, `LOGNAME`, `SHELL`, `TERM` and
the recorded variables, plus tmux's own pane variables when in tmux. `USER` and
`LOGNAME` come from the current account's password entry, regardless of the
calling shell's values. `HOME` keeps the shell's value when set. It does
not inherit the other variables from the shell running `hansa up` or from an
existing tmux server.

With tmux installed (found by hansa's usual lookup, including `HANSA_TMUX`),
`hansa up` starts a detached pane in the saved server and session. If that
session already exists, it adds a window there. Without a saved tmux location,
it uses a server named `hansa-<desk folder name>` (made safe) and session
`serve`. The pane command uses `/bin/sh`, regardless of the account's shell.
Output stays in the pane and is also appended to `.hansa/serve.log`.
Without tmux it starts a background process in its own session, with input
from `/dev/null` and output appended to that log. Before either start, a log
over 20 MB moves to `serve.log.1`, replacing the previous backup.

With no saved start, hansa says so and uses the defaults and the current
shell's allowed launch variables. After starting it waits up to eight seconds
for the server's lock and page to be ready, then prints the pid, page port and
tmux attach command (or a command to follow the log). A failed start shows the
log's last lines. On another account's desk, it starts nothing and gives the
command to run as that account, so the server uses that account's tmux and home.

Each server start also adds or updates one entry for its desk in this account's
`$XDG_STATE_HOME/hansa/desks.json`, or `~/.local/state/hansa/desks.json` when
`XDG_STATE_HOME` is unset. That list holds the desk folder, the absolute hansa
command and when it started. If the list cannot be updated, `hansa serve`
warns on stderr and keeps serving; the desk's own start record is still saved.
Outside any desk, with no `--desk` or `HANSA_DESK`,
`hansa up` visits every listed folder that is still a desk, printing a block
for each. Each is started by its own installation with
`<saved hansa> --desk <folder> up`. If that installation is missing or too old
to have `up`, nothing starts there and hansa prints the last server command
instead. A new desk gets on the list when its server starts once.

### The coordinator's session

Each start of the coordinator's session saves `.hansa/coordinator-start.json`;
the latest start wins.

- `hansa coordinator [words]` saves hansa's own option (`--clear-daily`), the
  words it gave claude, the folder (the desk), the tmux server and session when
  it runs in tmux, and the launch variables listed above.
- A session hansa did not start (in a folder of its owner's, with its own
  `CLAUDE.md` and memory) saves its start when it runs `hansa notify on`: the
  exact argument list of its claude process (the `CLAUDE_PID` process; read
  through sysctl on macOS, `/proc/PID/cmdline` on Linux), that process's
  folder, the tmux server and session (`TMUX` and `TMUX_PANE` reach the command
  from the session), and the launch variables of the command's own
  environment, which it has from the session. `hansa notify on` then adds one
  line: after a restart, `hansa up` starts this session again in FOLDER with
  COMMAND. Run in the session the record says `hansa coordinator` started (the
  same pid and start time), it saves nothing: that start's record stands.
  `HANSA_ROLE` does not decide this: a desk owner's wrapper script commonly sets
  `HANSA_ROLE=coordinator` on every hansa command, so a session it runs
  `hansa notify on` for is recorded like any other.
- Both save the session's process: its pid with its start time, as hansa tells
  sessions apart.

The record never holds a key or a session's identity. The words that pick a
conversation or give an inbox are left out with their values: `-c`,
`--continue`, `-r`/`--resume`, `--session-id`, `--fork-session` and
`--messaging-socket-path`. So a session started with `--resume <its id>` or
`--session-id <its id>` is recorded, and so is one `hansa up`'s launcher
started (it carries its inbox) when it runs `hansa notify on`: it hears from
hansa already, and running it does no harm. When a word that is kept holds
one of the desk's keys or the session's identity, or the argument list or the
folder cannot be read, no command is saved and the start says why. The record
then holds only that and the session's process, so `hansa up` starts no older
command and says why; only a session already recorded keeps its record when a
later `hansa notify on` cannot read it. As root on an account's desk nothing
is saved: root writes nothing there, and `hansa up` runs as the account.

Claude Code loads the card (the desk's `CLAUDE.md`) only for a session started
in the desk or below it, or one whose `CLAUDE.md` imports it. So
`hansa notify on` looks at the `CLAUDE.md` and `.claude/CLAUDE.md` in the
recorded folder F and the folders above it for a line `@path` naming the card
(a relative path read from the file's folder). With none, it writes a copy of
the card to `F/.claude/rules/hansa-card.md`, which every Claude Code session
started in F or a folder below it reads at each start; this session reads it
from its next start, and until then `hansa help card` prints the card. Above
the card the copy has hansa's header: hansa wrote it for this desk, rewrites
it, and removes it, so edits to it are lost; and the session was not started
by `hansa coordinator`, so the helpers (scout, reviewer, operator) and the
coordinator's mod are not in it, and where the card says to ask a helper it
makes a task. The header also says the card is for the session that coordinates
that desk, the one hansa posts its notices to, and any other session started
in that folder or below it, such as a worker a script starts or a session for
other work, must ignore it. The card's import of `notes/PROJECT.md` names that file from
the copy's folder when the desk is inside F; with the desk outside F the copy
holds a pointer to the file in its place, and imports nothing. The copy is
always made from hansa's own card, never from the desk's `CLAUDE.md` as it
stands. hansa rewrites the copy at every `hansa notify on` and wherever it
writes the desk's card: at `hansa init`, at each `hansa serve` start (which
brings the desk's card up to hansa's, as `hansa coordinator` does, so a desk
whose coordinator only runs `hansa notify on` gets an upgraded hansa's card and
copy too) and at `hansa coordinator`'s start, which instead removes a copy in a
folder that holds the desk: the session it starts there would read both.
`hansa notify on --no-card` records the session but removes hansa's copy and
keeps the copy off for this desk, including later plain `notify on` and starts
of `hansa serve` or `hansa coordinator`. `hansa notify on --card` turns it back
on and writes the copy as above; the default is on.
`hansa notify off` removes hansa's copies, and `hansa notify on` from another
folder removes the earlier one; only a file with hansa's header for this desk
that git does not track is removed. When F is in a git work tree, hansa adds
the copy's path to that repository's `info/exclude` (`git rev-parse --git-path
info/exclude`), once, and not when git already ignores it; it never writes a
file git tracks, as a copy committed into a project would reach every session
of everyone who clones it. hansa's own runs leave the copy out: each worker
and verifier run lists, for every folder above its checkout, that folder's
`CLAUDE.md` files and `.claude/rules/**` in its `claudeMdExcludes`.

hansa writes no copy, and says why, when a file without its header is at that
path (it is left), when git tracks the path, or when F cannot be written: it
then gives the line to add to F's `CLAUDE.md`: the card's path relative to F
when the desk is inside it (such as `@desk/CLAUDE.md`), else its absolute path,
which Claude Code loads only once imports from outside the folder are allowed
(it asks once at a start: "Allow external CLAUDE.md file imports?"); hansa
never writes that line, the folder being another project's. Nor does it write
a copy when F's `.claude` is the account's Claude Code folder (the home
folder's, or `CLAUDE_CONFIG_DIR`'s) or F holds the home folder: rules there
reach every session of the account. It then gives no line either, as a
`CLAUDE.md` there reaches every session below it, and says to start the
coordinator in a folder of its own or with `hansa coordinator`. A line already
there with the card outside the folder gets the note on external imports, and
no copy. `hansa status` adds a short note to its `hansa posts to` line (with
the copy in place, that the session reads the card from it), and `hansa explain
notify` the full lines. A session `hansa coordinator` started runs in the desk
and is not checked.

After the server, `hansa up` starts nothing when a session hears from hansa,
and names it (pid and name). It starts nothing either when the recorded
session's process still runs (the same pid, started at the same time), even
one that does not hear from hansa: it says to run `hansa notify on` in it,
since a second session would continue the same conversation. A process name
does not find that process: Claude Code's shows its version as its name. A
record whose words hold `-p` or `--print` is not an interactive session:
nothing starts, and `hansa up` says so.

Otherwise it starts the session in tmux: in the recorded tmux server and
session, or, with none recorded, the server `hansa up` uses for the desk's
server and a session `coordinator`; when that session exists, in a new window.
The pane gets the server's clean environment (above), with the recorded
variables, and runs in the recorded folder:

- a `hansa coordinator` record: `<hansa> --desk <desk> coordinator <recorded words> --continue`;
- a `hansa notify on` record: the recorded argument list, with `--continue`,
  through hansa's launcher (`hansa notify-launch`, not listed in help). The
  launcher starts the follower for its own process, as `hansa coordinator`
  does, records the start again, then execs claude with
  `--messaging-socket-path`, so the session hears from hansa without
  `hansa notify on`.

`--continue` continues the most recent conversation in that folder. hansa's own
`claude -p` runs (`hansa ping`) use a temporary folder, so they leave none
there. A prompt given on the recorded command line is given again.

With no record on a new desk (no task, and nothing under `.hansa/followers/`,
as after `hansa init`), it starts `hansa coordinator` in the desk, without
`--continue`, and says that the first start in a folder asks whether to trust
it: attach and answer. With no record on any other desk, it starts nothing:
that desk had a coordinator before hansa recorded its start, maybe a session
in another folder that ran `hansa notify on`, and a fresh `hansa coordinator`
would be a second coordinator, in the wrong folder, with a new conversation.
It says that the start is not recorded yet, and what to do: start the
coordinator as before and run `hansa notify on` in it, or start it with
`hansa coordinator`; from then on `hansa up` starts it again. After a start, it waits
three seconds and prints that the session started, with the attach command
(`tmux -L NAME attach -t SESSION`, or `tmux attach -t SESSION` for tmux's
default server); if the pane ended, it says that the start failed, with the
command to run by hand. Without tmux it starts no coordinator, and prints the
command to run in a terminal of one's own. On another account's desk it starts
nothing, as for the server. While no session hears from hansa, `hansa status`
says in one line that `hansa up` starts the session again, and how, or why it
does not, including a start not recorded yet and what to do about it, and a
recorded folder that is not there.

## A desk's files

### Where a desk's files are

The desk's files are under its directory; see the top of [`hansa/desk.py`](../hansa/desk.py).
The account's desk list lives outside it, as described [above](#starting-the-server-and-the-coordinator-again).
A write that fails leaves the previous file, with its permissions, and no temporary file.
The event log is only appended to. Readers skip a damaged line, and the next event starts
on a line of its own. Keys live
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

### Where a desk's files grow

| What | Where | How big |
|---|---|---|
| A task's text, record, report and result | `tasks/<id>/task.md`, `meta.json`, `result.md`, `run.json` | a few KB to some tens of KB each; kept whole |
| What a task's processes printed | `tasks/<id>/run.err` (the worker's errors), `setup.log`, `check.log`, `merge-check.log`, `merge-setup.log`, `merge-check.err` | whatever the lane's setup, check and merge check print: not capped while the task is open (a verbose build can print hundreds of MB) |
| A lane's last try | `lanes/<lane>/try.log`, `merge-check-try.log` | at most 4 MB per step (its first and last 2 MB), replaced by the next try |
| The event log | `events.jsonl` | a few hundred bytes per event, append-only; never trimmed |
| Server output from `hansa up` | `.hansa/serve.log`, `serve.log.1` | the log rotates on the next start when over 20 MB; one backup |
| A release's output | `.hansa/releases/<id>.log` | whatever its command prints; kept |
| A running task's output | `.hansa/runs/<id>/` | moved into `tasks/<id>/` when it ends |
| Each lane's worktree | `worktrees/<lane>/` | the repository's checkout, and what builds leave in it (see [Free space](#free-space)) |
| A worker run's temporary folder | `/tmp/hansa-<id>-...` | what the worker's commands put there; removed when the run ends |
| A lane command's TMPDIR, on Linux | `.hansa/tmp/hansa-check-...` | what a setup's, check's, merge check's or try's build writes there; removed when the command ends |

Once a day `hansa serve` cuts merged tasks' passed setup, check and merge check logs over 289 KB to their
first 32 KB and last 256 KB, with a line counting the bytes cut. Failed logs wait 30 days; other logs of
tasks ended (merged, cancelled, failed, replaced) or left at done that long, and ended releases' logs that
old, keep their last 256 KB, under a line saying when and how big they were. Task cuts appear in the record
and `hansa show`, release cuts only in the event; no log is cut twice. Text, records, reports, results,
the event log and all other logs stay whole.

## Hearing from hansa

hansa posts to the coordinator's Claude Code session through the session's
inbox (a local socket every interactive session listens on) when something
needs the coordinator: a task ended (after hansa's own check), failed, was
cancelled, stopped at its ceiling or could not start; a verification's verdict;
a merge check ended; the principal answered or closed a
request (with the answer); the principal answered a release; a release ended
(done, or failed with its exit code and last lines) or became void; a provider
an open request names became ready when it was not as the request was made
(once, looked at on a key or settings save, a `hansa ping`, or its rest after a usage
limit running out; the request may be done: if it is, the coordinator closes it
with `hansa ask --close N "<why>"`); `hansa serve` stopped, or, still running,
finished no round for 5 minutes (busy or stuck), and then that it runs again;
srt not running; free space went under the floor (`hansa disk-floor`), and then
that it is back over it. A server busy for less than that is not news, and
nothing the coordinator did itself and saw is posted to it.
Time the machine sleeps does not count toward a server being busy or stuck.

Each message starts with `[hansa]` and the desk, with one line per event and the
next command; events seconds apart come in one message, and past 10 lines the
rest are counted (`hansa status` has them). A follower does the posting: a small
background process per session and desk, which reads the event log from where it
left off and ends when the session's process does; one that starts after
another ended first posts what was missed meanwhile, as it is now.

Which session hears what: a task or request goes to the session that made it
(`hansa add`, `retry`, `verify`, `merge` and `ask` record it) while
that session has a follower; otherwise to every session hansa posts to, as do
desk-wide events. A cancel or close is not posted to the session whose command
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

When what hansa posts cannot reach the coordinator, the principal hears of it through their alert command (kind
`coordinator`, see [alerts](releases.md#alerts)): when no session hears from hansa while something waits for the
coordinator (the session ended, or notifications were turned off), when the session waits on a permission dialog or
a question with something posted to it, and when a task posted to it waits for review while the session is idle.
The minutes before each are the principal's, on the page, and `hansa status` shows a line while one of them holds,
such as `the coordinator's session pid 4242 (desk-b4) has waited on its window for 14 min`. For the last two a
session's follower runs `claude agents --json` only while something posted to it could still go unanswered, and at
most once a minute counting every run, its lookup of the session's name included (one run gives both).

### The daily clear

A coordinator's session can run for days. Each time its context fills, Claude
Code compacts it into a summary; the next compaction summarizes that summary
again together with everything since. The summary keeps growing, the session
starts each stretch heavier, compaction comes sooner, and early detail drifts.
`hansa coordinator --clear-daily HH-HH` (such as `04-09`) clears the session
once a day while it is idle, so it starts again from the desk's state, not from
a summary of summaries. Automatic compaction keeps a full session going; it does
not do what the daily clear does.

It is off by default. hansa hands the window (local time; `22-03` runs past
midnight) to its mod, for that start only, and the mod clears the session as
`/clear` does, once in each window, when the session has been idle for 5
minutes: no turn (hansa's posts and anyone's messages start one) and no
background work waiting. It skips the window the session started in. Inside the window
idle compaction waits for the clear. Without the mod there is no daily clear,
and the start says why.

After any clear, the session is shown `hansa status`, and hansa records when.
`hansa status` and the page show the window and the last clear, and warn once
two windows have ended since without one; the session started as root on an
account's desk records no clear, so there the last clear is not known.

When the session recorded for `hansa up` is one that ran `hansa notify on`
(kind `notify`), `hansa status` and the page instead say in one line that it was
started outside `hansa coordinator`, so the coordinator's mod and the daily
clear are not in it. They do not describe the earlier coordinator's start or
show its daily clear for this session. They also omit the coordinator's compaction
line: neither the mod's idle setting nor the `--autocompact` option passed by
`hansa coordinator` reaches this session through `hansa up`.

So the coordinator keeps what must survive in the desk (tasks, requests, and
its notes under `notes/`), not only in the conversation. It keeps the notes file
it rereads each time it picks the work up short, since each reading costs it:
the current work and open items only. It moves finished rounds to another file
there that it does not reread, without asking the principal and without typing
them out again, with `hansa notes archive FILE HEADING` (to `notes/archive/FILE`
unless `--to` names another); `hansa notes` lists the files with their sizes and
headings. It does not `mv`, `cp` or `sed` them: a shell command there asks.

### Compaction

`hansa coordinator` loads hansa's mod, a Claude Code plugin, into the coordinator's session only (workers,
verifications never get it). The principal sets two values on the page, next to the
coordinator's model and effort; the coordinator cannot change them:

- **Compact when idle at** (default 400,000 tokens): once a turn has ended with an answer and the session is idle
  (no turn running, and the last `Stop` listed no background tasks and no session crons), the mod compacts after 60
  seconds with no new turn, while the whole context is still in the prompt cache.
- **Compact while working at** (default 550,000 tokens): Claude Code's own auto-compact, given as `--autocompact`.

Both take 100,000 to 1,000,000 tokens. A new turn cancels the wait; after a compact (done, skipped or refused) the
mod asks again only after a new turn. It reads the first in the desk's settings file at each decision (a turn or a
wait ends): a missing value is its default, a file it cannot read or parse means no idle compact. The second applies
from the next start. The mod needs Claude Code 2.1.290 or later; `--autocompact` is given
whenever Claude Code is new enough, whether or not the mod loads. When hansa skips it, the start, `hansa status` and
the page say why. A session whose settings turn hooks off runs without the mod's rules, and hansa cannot see that;
Claude Code's own auto-compact still applies to it.

Below **compact when idle at**, what compacts an idle session is Claude Code's own idle compaction (Claude Code
2.1.299 or later): a conversation of 200,000 tokens or more left idle about 54 minutes is compacted before its
one-hour prompt cache lapses. `"idleCompaction": false` in Claude Code's settings, or its `/config`, turns it off.
hansa neither sets nor reads it.

## Merge checks

For long merge checks and busy task lanes, add a merge lane with `hansa lane add m --like a`, give it its own caches through `write` and `env`, then set `hansa lane set m merge_lane true` (unset or false: ordinary). One per repository and base, it takes no work and uses its own worktree, sandbox, env, setup, merge command and limit, keeping the task lane free and its merge message and identity. The lane is chosen when the check starts; unmarking or removing it while queued restores the task lane. Merge checks still run in order per repository and base, taking a concurrency slot before new work. A starting check takes consecutive compatible queued merges up to this lane’s `merge_batch` (1–10, default 4; `hansa lane set m merge_batch N`, `none` restores the default). Setup and check run once on the last merge commit; each task keeps its own merge commit, message, identity and landing checks. A failed or stopped batch, moved base, landing refusal, or restart that cannot finish it returns its remaining members to their old places, each checked alone. See [merging](tasks.md#merging-the-merge-check-and-conflicts). With every slot busy, a merge check waits for the next task to end anywhere, and that task's lane waits about one merge check. Raising concurrency by one removes the wait if the machine can run a merge check beside the lanes' work.

## Free space

Desks run builds and tests many times a day, and a full disk breaks everything on the machine, the desk's own
records among them. So `hansa serve` keeps a floor of free space. Each round it looks at the free space of each
volume it and its runs write on: the desk's folder, each lane's repository and worktree, the folder worker runs get
their temporary folders in (`/tmp`) and its own TMPDIR (where the setups', checks' and tries' are made; on Linux
their own TMPDIR is in the desk's `.hansa/tmp`, on the desk's volume, which is named as a place too). While any
of them has less free than the floor, it starts no new task, verification, try, merge or merge check, or release. What already runs goes on, a running task's check and a resumed worker among it. It says why:

- `hansa status` has a line naming the volume, the places on it, its free space and the floor (`DISK LOW: ...`),
  and always one with the free space of the desk's volume and the floor, also when nothing is under it;
- the page shows the same in its status line, and the floor next to the free space under Settings, Lanes, General;
- `hansa explain server` lists each volume with its free space;
- a queued task, merge or release says it waits for space;
- the coordinator's session is told once when a volume goes under the floor and once when all are back over it,
  and the principal's alert command (when one is set) runs once, with `HANSA_ALERT_KIND=disk`.

It starts again by itself once every volume has the floor and 1 GB more free, so that it does not stop and start
at every round around the line. A release asked meanwhile waits, approved, and goes once there is space.

If a write fails anyway (a full disk, for example), `hansa serve` goes on, and still stops runs that are
cancelled or over their time limit. Free space: once writes work again, it records the ends it kept, a run
stopped at its limit as timed out. After a restart, a check's end is found without running the check again;
a worker's exit code that could not be recorded stays unknown, as before.

If serve restarts before it can save a failed start's record, ordinary recovery applies: the stopped process is gone, workers and verifications return to the queue, checks and merge checks run again, releases are recorded as lost and tries end.

If a write fails just after git has merged, set work aside or committed, hansa's record or message may not
show what git did: git is right, and `hansa git L log` shows it.

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
