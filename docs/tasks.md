# How a task runs

Time limits and resumes, retries after a dropped connection or a provider's usage limit, and tasks with a
browser. The [README](../README.md#how-it-works) has the overview of a task's life.

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
turn. A worker that does end its turn while
background work it started still runs is resumed once, in the same session,
with a short message: that work was stopped with it, so it runs it again in the
foreground, waits for it, then reports. A second time it is not resumed; `hansa
show` then says what was still running. The time limit counts from the task's
start, across both runs. When the task is queued again for a new try (after a
dropped connection, say), that try starts with nothing of the earlier try's
resume: it may be resumed once in its own right, and the earlier try's turns
and tokens stay in its attempt record. For a browser task whose lane has a
preview, the first run's preview is stopped when that run ends, before hansa
decides on the resume, and the resumed run gets one started again from the same
worktree: never two at once. The first one's output is kept beside the second's
(`preview-before-resume.log`, which `hansa show` names).

## Retries after a dropped connection

A run that ended on a dropped connection is tried once more by itself. hansa
reads only Claude Code's own error lines for this: one that begins `API Error`
(a 5xx, a connection error or reset, a request that timed out, an overloaded
provider, unable to connect, but not a certificate error), one that begins
with a connection reset, refused or closed, a socket hang up, a fetch that failed or "Connection lost while your
computer was asleep", one that ends `(ENETUNREACH)`, and the API's
`overloaded_error`. A test that
timed out, or a log line saying "overloaded", is the task's own failure.

## When a provider hits a usage limit
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
- **Every try costs, and counts.** When a task goes back to the queue for another try (a usage limit, a
  dropped connection, a provider that refused it), that run's tokens, turns and worth are kept on its
  attempt record; a run an earlier `hansa serve` started and lost track of is read from its session record
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

A task or verification with a browser (`hansa add --browser`, `hansa verify
--browser`) on a lane with a `preview` (the command that starts the app, such
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
file watcher you start in the worktree while a browser task runs there is
left alone. But a process you start there with `nohup` (or an editor that
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
