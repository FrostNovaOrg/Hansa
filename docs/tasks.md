# How a task runs

Time limits and resumes, retries after a dropped connection or a provider's usage limit, a check's output,
effort, tasks with a browser, merging, what work costs, and the states a task goes through. The
[README](../README.md#how-it-works) has the overview of a task's life.

## Time limits, and work left running in the background

One Bash call of a task or verification worker may run as long as the lane's
check may, plus 5 minutes, but never past the worker's own limit: hansa sets
Claude Code's `BASH_MAX_TIMEOUT_MS` to that (without it a call may run at most
10 minutes). The check's limit is the lane's `merge_timeout_min`, else its
`timeout_min`; the worker's is `timeout_min`. A call that asks for no timeout
still stops after 10 minutes (`BASH_DEFAULT_TIMEOUT_MS`; less when the most is
less), so a command that hangs does not hold it for the whole limit. Workers
are told these limits in minutes, to give the Bash tool's timeout (in
milliseconds, up to the most) for a long build or suite, and to run it in the
foreground, in one Bash call, never in the background while they end their
turn.

A failed write in `hansa serve` does not keep it from stopping runs at their time limit: see
[Free space](desk.md#free-space).

A worker ends with a short report: its last message before a notice from its
own background work (a watch it started timing out, say) opened another turn
after it had stopped. Text it wrote answering such a notice follows the
report, under a line saying it was written after, when a background notice
came in. A worker that does end its turn while
background work it started still runs is resumed once, in the same session,
with a short message: that work was stopped with it, so it runs it again in the
foreground, waits for it, then reports; the report kept is the one it writes
after the resume. A second time it is not resumed; `hansa
show` then says what was still running. The time limit counts from the task's
start, across both runs. When the task is queued again for a new try (after a
dropped connection, say), that try starts with nothing of the earlier try's
resume: it may be resumed once in its own right, and the earlier try's turns
and tokens stay on the task's record of its tries. For a browser task whose lane has a
preview, the first run's preview is stopped when that run ends, before hansa
decides on the resume, and the resumed run gets one started again from the same
worktree: never two at once. The first one's output is kept beside the second's
(`preview-before-resume.log`, which `hansa show` names).

## Dropped connections and usage limits

A run cut off by a dropped connection is tried again by itself; a provider that hits a usage limit rests,
and its tasks move on or wait.

### Retries after a dropped connection

A run that ended on a dropped connection is tried once more by itself. hansa
reads only Claude Code's own error lines for this: one that begins `API Error`
(a 5xx, a connection error or reset, a request that timed out, an overloaded
provider, unable to connect, but not a certificate error), one that begins
with a connection reset, refused or closed, a socket hang up, a fetch that failed or "Connection lost while your
computer was asleep", one that ends `(ENETUNREACH)`, and the API's
`overloaded_error`. A test that
timed out, or a log line saying "overloaded", is the task's own failure.

### When a provider hits a usage limit

When a provider answers with a usage limit (HTTP 402 or 429, or the wording of one), it rests: no task
starts on it until the rest ends, and a task stopped by the limit moves to the next ready provider of its
lane, or waits for one.

- **Until the reset time, when the error gives one**, and a minute, for at most 31 days. hansa reads a
  clock time (`You've hit your session limit · resets 3pm (Europe/Berlin)`, as Claude Code prints a
  subscription's limit), a date and time (`resets Oct 9, 3pm (Europe/Berlin)`, `resets 2026-08-08 00:00
  UTC`), a Unix time (`usage limit reached|1760000000`, `resets_at`), a `retry-after` value given in the
  message, and "in N minutes" or "in N hours" (`Please try again in 20 minutes`). A time without a zone is
  the machine's local time; a month and day without a year is in the year that puts it nearest to now.
  A form hansa is unsure of, a time already past, or a line it cannot read gets the fixed length below.
- **Else for the provider's own rest**, `rest_minutes` on the page (1 to 44640, a month), or the desk's
  `cool_minutes` (30 by default) when it is empty. Fit it to the provider: a subscription's window is a
  few hours, an API key's limit may be per minute, a plan's allowance may be monthly.
- **No lost tries.** A task whose run ended on its provider's usage limit goes back to the queue without
  using one of its 3 tries, 5 times; after that, each such end counts as a try, so a provider that hits its
  limit again right after each rest cannot hold a task forever. Any other failure counts as before.
  `hansa show ID` says until when the task waits; `hansa status` and `hansa explain provider P` say until
  when each provider rests and why (the reset time it read, or its fixed length), and its rest length.
- **A refusal is not a limit.** A provider that refuses one task (its content filter) does not rest: that
  task alone moves to the lane's next provider.
- **Every try costs, and counts.** When a task goes back to the queue for another try (a usage limit, a
  dropped connection, a provider that refused it), that run's tokens, turns and worth are kept on the
  task's record of its tries; a run an earlier `hansa serve` started and lost track of is read from its session record
  as the task goes back. A run hansa resumed counts with the run before it, the resumed part read from
  the session record after the resume only. `hansa cost` (also `--by task`, which shows each task's runs
  and turns), `hansa show`, and the page's cost and task view count every try once, by when each run ended.

## A check's output

`hansa show ID` gives a failed check's failing tests and its last lines. `hansa show ID --check` gives the task's
last check and its last merge check apart, each under a line of its own (`== check ...`, `== merge check ...`):
how it ended, the file in the task's folder with its whole output (`check.log`, `merge-check.log`, or
`merge-setup.log` when the lane's setup failed before the merge check) with its lines and bytes, and its last 20
lines. Its first line says which of the two follow and how to print each whole: `hansa show ID --check-log` and
`hansa show ID --merge-check-log` print one output whole, keys removed, under a line that names it, its length
and its file.

## Effort

Each run is started with an effort, and hansa records where it came from: a task's own `--effort` (`hansa add`,
`hansa retry`, `hansa verify`), else its provider's default effort (set on the page), else none, which leaves
it to claude's own default. A design session takes the desk's design setting, then the coordinator's when it
runs on the coordinator's provider, then the provider's. Lanes have no effort setting. `hansa show ID` gives
it beside the provider and model (`claude (opus, effort high from the task's --effort)`), `hansa status`
gives it for each running task (and `hansa status --json` as `effort` and `effort_from`), and an earlier try
started with another model or effort names its own. On a provider whose endpoint is not Claude's own (kind
`compatible`: any endpoint that speaks the Anthropic API) they add that the endpoint may ignore effort: hansa
passes it on, but some such endpoints do not act on it.

## Browser tasks and the preview

A task or verification with a browser (`hansa add --browser`, `hansa retry
--browser`, `hansa verify --browser`) gets a browser for that task's worker
alone, and no other, through the browser tool the principal sets on the page,
with no saved logins. The worker reads a page's text directly; charts, images
and drawings it can see only in screenshots, which hansa keeps with the task
(`tasks/<id>/browser/`). The page it looks at must be reachable from this
machine. If the principal has not set up a browser, hansa says so and queues
nothing.

Such a task on a lane with a `preview` (the command that starts the app, such
as `npm run dev`) gets the app started for it. On macOS hansa starts the
preview itself before the worker: in the lane's worktree, after the lane's
setup, in `srt` with the lane's sandbox (network and write lists, env, the
prelude), with local binding allowed for that one process; the worker's own
sandbox stays as the lane sets it. hansa waits up to the lane's
`preview_wait_s` (default 120 seconds) for the first `http://localhost:PORT`
or `http://127.0.0.1:PORT` address the preview prints, or a port it names
that answers, and puts the address in the worker's instructions; a preview
not ready by then is stopped, and the worker is told it did not start, with
its last lines, and goes on without it. hansa stops the preview with its
whole process group when the worker ends, however it ends (done, failed,
cancelled, stopped at the ceiling, or ended while `hansa serve` was
restarted). It stops the processes the preview started that left that group
too: while the preview runs, hansa looks every half second for what runs
below it and records each one (its number and start time), and at the end
stops each recorded one still running, whatever its group or environment,
and any orphan (a process whose parent is the system's first process, as a
daemon that forked twice is) started since whose working folder is in the
worktree or the preview's temporary folder. A process with a living parent
that the preview did not start is not touched, so a shell, an editor or a
file watcher a person starts in the worktree while a browser task runs there is
left alone. But a process started there with `nohup` (or an editor that
detaches itself from the shell that started it) becomes an orphan once that
shell ends, and is stopped at the end of the task. What can still escape on macOS: a process that forks twice,
leaves the group, clears its environment and changes folder all between two
looks (half a second). It keeps running after the task, with the
preview's sandbox, local binding included: it can reach the machine's
loopback, write in the worktree and the lane's write list, and reach the
hosts on the lane's network list, until it ends or the machine restarts. A
worker's own processes are recorded too, on each round of `hansa serve`
(see [what hansa stops when a run ends](sandbox.md#what-hansa-stops-when-a-run-ends)). Its output is kept in the task's folder as
`preview.log`, keys removed, at most its first and last 2 MB on disk with a
line saying how much was left out between them (a byte that is not UTF-8 is
kept as one `?`; where a cut falls inside a line, 4 KB more on each side of
it are left out, so no key is kept in part, and the line says so; if
removing keys made it longer, it is cut again after; hansa reads the whole output as
it comes, so a preview that prints without end never waits on it and never
fills the disk); `hansa show` says where and what became of it. **On macOS such a
preview can reach every service on this machine's loopback while it runs**,
as a lane with `localhost` on its network list can: that is the price of
looking at the app in a browser on a Mac. **On Linux hansa does not start
it**: `srt` runs a command there in a network namespace of its own
(bubblewrap's `--unshare-net`; local binding is a macOS setting), so a
server bound inside listens on the sandbox's own loopback, and the browser,
which runs outside the sandbox, cannot reach it. The worker is told so. A
design session starts its own preview, as before.

## Merging, the merge check and conflicts

`hansa merge ID` merges a task's branch into its lane's base branch. A failed
check blocks it unless the coordinator says `--despite-check` (recorded), and
so does a verification that does not hold.

A [merge lane](desk.md) serves its repository and base's checks with its own worktree, sandbox, env, setup, command and limit; otherwise they use the task's lane as below. With a merge lane, a command of its own means its merge command differs from the task lane's check, which the task already passed.

- **When it merges at once.** When the lane's base has not moved since the task
  was made and the lane has no merge check of its own (below), or when the lane
  has neither a check nor a `merge_check`, `hansa merge` merges at once. A lane
  without a check has its tasks merged untested: give it one with `hansa lane
  set L check "<command>"`.
- **When the base has moved.** When the base has moved (another task was merged
  meanwhile, say) and the lane has a check, the two together were never
  checked, and two changes that each pass can fail together. Then `hansa merge`
  does not merge at once: the task becomes `merging`, and its merge check waits
  for the lane to be free. hansa merges the task onto the base as it is then, in
  the lane's worktree, runs the lane's setup and its merge check there, and if
  that passes moves the base to exactly that merge commit. The coordinator is
  told when it ends: the task ends `merged`, or back in review with `hansa show
  ID` saying why nothing was merged (the check's last lines, or a conflict). If
  the base moves again while it is checked, it is checked once more on the new
  tip; after a second move it stops and says so. `--despite-check` merges at
  once without it (recorded).
- **One at a time.** Merges into one base branch go one at a time, in the order
  they were asked: a merge asked while a merge check runs or waits on the same
  repository and base branch waits its turn behind it, as `merging`, and so
  does one asked while a release runs in that repository. When its turn comes it
  merges without a check if the base has still not moved, and with a merge
  check as above if it has.
- **Cancelling.** `hansa cancel ID` on a `merging` task stops its merge check
  and puts the task back in review.
- **`--recheck`.** `hansa merge ID --recheck` goes the same way whether or not
  the base has moved: the task merged onto the base's tip, the lane's setup and
  merge check run there when the lane is free, and exactly that merged if it
  passes. Use it for a task whose check failed or could not run for the
  machine's sake, once that is fixed. The event log records that the check was
  run again, and by whom. Not with `--despite-check`.
- **`merge_check` and `merge_timeout_min`.** A lane's merge check runs its check
  unless the lane has a command of its own for it: `hansa lane set L
  merge_check "<command>"`, for what is too slow to run after every task (the
  whole build, packaging, the slow suites). Its time limit is the lane's
  `merge_timeout_min` (`hansa lane set L merge_timeout_min N`; none takes it
  out), else its `timeout_min`, whichever command it runs. A merge check of its
  own runs exactly where the check would (the lane's worktree and sandbox, after
  its setup, on the merged tree), but before every merge of a task that changed
  something, whether or not the base has moved, since the task's own tree never
  ran it; the check after each task still runs the check. A lane with only a
  `merge_check` checks nothing after a task, and runs it before each merge.
  `hansa show`, `hansa status` and what the coordinator is told name the
  command that ran.
- **Workers are told.** Workers and verifiers on a lane with a merge check of
  its own are told that it runs before merging, not after their task.
- **Trying it.** `hansa lane try L --merge-check` runs the lane's setup and the
  merge check's command once where tasks run, under its time limit, with no
  worker, and prints how each ended; `hansa lane try L --merge-check --log`
  prints each step's whole output of the last merge-check try, keys removed.
- **Conflicts.** A conflict lists each conflicting file and the lines where the
  two sides meet. Settle it with `hansa retry ID --update "<how to settle
  it>"`: the follow-up continues from the task's branch with the current base
  merged in; where that conflicts, the worker finds the conflict markers in the
  files it is told about and settles them by editing, and hansa's commit
  completes the merge. Redo the whole change with `--fresh` only when the old
  work is no use.

## Sending a task back

`hansa retry ID "<what to change>"` sends a task back: a follow-up takes its
place, and is the one to review and merge. The follow-up's worker gets the
original task, one line per earlier round (its id, how it ended, the start of
its note), this round's note, the previous report and the latest
verification's report, with its verdict. Earlier notes are not carried, so
what still holds of them is said again in the new note. Each round's note is
kept on its own beside its task, as `note.md` in the task's folder (keys
removed, as from what else hansa writes there).

`hansa show` of a follow-up says which round it is, the earlier rounds' ids,
oldest first, and how many of them a verification found do not hold. When a
verification of a follow-up finds that it does not hold, and that is the second
such verdict in its rounds or more, `hansa show` and what the coordinator is
told add: "This task has not held N times. Before sending it back again,
rethink it: make it smaller, ask for less, or start again from the base."

The worker is told that hansa runs the lane's check after it ends, with the
command, so it runs only the tests related to its change. A verification is
told the command of the task's last check and how it ended, and that the whole
check need not be run again unless it needs to.

## What work costs

Cost figures are Claude Code's own token counts, priced with the principal's
table of prices (on the page). With a subscription they show what the tokens
are worth, not a bill. `hansa cost` gives them (`--days N` or `--since HH:MM`;
`--by day`, `seat`, `lane`, `task`, `model` or `provider`; `--json`, with
`--rows` for every row).

To say what one job cost, note the time `hansa status` shows when it starts,
and run `hansa cost --since HH:MM --by seat` when it is done.

The coordinator's own session, with its helpers, is counted only when it runs
with the workers' Claude login (the same config folder), because only then are
its session records where hansa reads them. It has no lane or task, so in `--by
lane` and `--by task` its usage is a row of its own, `(coordinator)`: in every
view the rows add up to the total. `hansa cost --turns` shows the
coordinator's own usage turn by turn (what started each turn, its steps, the
conversation's length at its first step, its worth and the tools it called;
then totals by what started them; no message text), which shows where its
money goes. A coordinator run as root on a Linux server is not counted
([On a Linux server](linux-server.md)).

When `hansa status` says cost audit on, the principal wants the cost in every
report: the coordinator ends each report with what that round of work cost (as
above), and points out anything unusual, such as one task costing far more than
the others, or a seat that grew. When it does not say so, the coordinator
spends no turns on cost.

## Task states

`hansa help states` prints this section.

| State | What it means |
|---|---|
| `queued` | waiting for a free lane and a ready provider |
| `running` | a worker is on it, or hansa is running the lane's check on its work |
| `done` | the worker finished; waiting for the coordinator's review (`hansa show` / `hansa merge` / `hansa retry`) |
| `merging` | `hansa merge` is checking it together with what the base gained since it was made: merged onto the base as it is now, with the lane's merge check (its check, unless it has a `merge_check` of its own) run there; it merges if that passes. Or it needs no check and waits its turn behind a merge check on the same base |
| `failed` | the run ended with an error; `hansa show` says why |
| `merged` | merged into the lane's base branch |
| `cancelled` | stopped or closed on request |
| `replaced` | a follow-up task took its place |
