# How a task runs

Time limits and resumes, retries after a dropped connection or a provider's usage limit, a check's output,
effort, merging, what work costs, and the states a task goes through. The
[README](../README.md#how-it-works) has the overview of a task's life.

## Time limits, and work left running in the background

New lanes stop at 200 turns; saved lanes keep their limits (`hansa lane set L max_turns 200` lowers one).

A run stopped at a ceiling before writing its report has its notes along the way as its report. At the
time ceiling, a line follows them: the last tool call the session record shows the worker starting, whether
or not it ended, and how many minutes before the stop it started (a Bash call's command on one line, cut
to 300 characters; another tool's name and its file path or pattern). Many minutes there points to one
long command, not a task too big. No line when the record holds no call or the provider keeps none.

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
and tokens stay on the task's record of its tries.

## Dropped connections and usage limits

After a dropped connection or usage limit, hansa carries on new commits without keys or markers, unless
the run began with update conflicts. Otherwise it keeps earlier carried work, or repeats the original
start and update. `hansa show` says where and why.
Time in the queue while a provider is unreachable or resting does not use the run's time ceiling.

A worker or verification the system killed (SIGKILL: exit -9, or 137 through a wrapper) after the lane's
setup, before it wrote its result, is queued again once the same way, without using a try, whatever its
stderr says of the provider. `hansa status` gives the kill as its last run while it waits; what the killed run
reported, if anything, is not kept: the next run's report replaces it. A second kill in the task fails it: the
machine may be short of memory, so fewer runs at once may help (`hansa concurrency`).
On Linux, the run's record, `hansa show` and its end notice give any rise in the kernel's `oom_kill`
count while it ran, including both runs' known counts after two kills. This counts processes killed on the
whole machine, not just this task; a missing or unreadable count changes nothing. A setup is covered by the
run it belongs to.

### Retries after a dropped connection

A run that ends on a dropped connection queues again and its provider becomes unreachable: nothing
else starts on it until it answers a check. `hansa serve` sends the same one-line request as
`hansa ping` every 3 minutes, with a 90-second timeout, without holding up other lanes.
The outage's start, reason and check times stay with the provider's state across server restarts.
`hansa status` and the page give those details, the last check's result and how many tasks wait for it.
When a check answers, those tasks carry on from their last commits as above, on the same provider.

A drop while the provider is already marked unreachable uses no try and does not count toward a
task's two drops. A second counted drop, after the provider answered a check in between, fails the
task with "the connection to the provider failed twice, so hansa stopped trying". Verifications follow
the same rule. This prevents one continuing outage from failing tasks; if the provider answers checks
but its long runs keep dropping, the task still fails after two drops. A report written before the
connection went ends as before. Usage limits, key or login problems and system kills keep their own rules.

hansa reads only Claude Code's own error lines for a dropped connection: one that begins `API Error`
(a 5xx, a connection error or reset, a request that timed out, an overloaded
provider, unable to connect, but not a certificate error), one that begins
with a connection reset, refused or closed, a socket hang up, a fetch that failed or "Connection lost while your
computer was asleep", one that ends `(ENETUNREACH)`, and the API's
`overloaded_error`. A test that
timed out, or a log line saying "overloaded", is the task's own failure.

### When a provider hits a usage limit

When a provider answers with a usage limit (HTTP 429, or the wording of one), it rests: no task
starts on it until the rest ends, and a task stopped by the limit moves to the next ready provider of its
lane, or waits for one.

- **Until the reset time, when the error gives one**, and a minute, for at most 31 days. hansa reads a
  clock time (`You've hit your session limit · resets 3pm (Europe/Berlin)`, as Claude Code prints a
  subscription's limit), a date and time (`resets Oct 9, 3pm (Europe/Berlin)`, `resets 2026-08-08 00:00
  UTC`), a Unix time (`usage limit reached|1760000000`, `resets_at`), a `retry-after` value given in the
  message, and "in N minutes" or "in N hours" (`Please try again in 20 minutes`). A time without a zone is
  the machine's local time; a month and day without a year is in the year that puts it nearest to now.
  A form hansa is unsure of, a time already past, or a line it cannot read gets the fixed length below.
- **Else for the desk's `cool_minutes`**, 30 by default.
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

Credit errors (`credit balance is too low`, HTTP/API Error 402, `insufficient balance`,
`insufficient_balance`, `insufficient_quota`, `余额不足`) rest indefinitely, surviving server restarts.
Only the principal ends this rest: press **Topped up** on that provider's row in **Settings, Providers**,
or save a new key. The coordinator cannot end it, even with `hansa ping`.
The task moves to the lane's next provider without using a try; if all rest, it waits.
Once per credit rest, hansa posts a line to the coordinator and runs the principal's alert command,
if set. `status`, `show`, `explain provider P`, and the provider's page row say why and since when.

After `hansa lane set L providers ...`, queued tasks, including those waiting for a resting provider,
use the new order: first ready provider excluding prior refusals; otherwise they wait. A
dropped-connection retry keeps its provider if still listed. Running work keeps its provider.
`hansa retry` uses its destination lane's current order. Verification on L uses L's current order;
on a verify lane, that lane's order. Neither inherits the parent's provider; readiness decides which runs.

## A check's output

`hansa show ID` gives a failed check's failing tests and its last lines. `hansa show ID --check` gives the task's
last check and its last merge check apart, each under a line of its own (`== check ...`, `== merge check ...`):
how it ended, the file in the task's folder with its whole output (`check.log`, `merge-check.log`, or
`merge-setup.log` when the lane's setup failed before the merge check) with its lines and bytes, and its last 20
lines. Its first line says which of the two follow and how to print each whole: `hansa show ID --check-log` and
`hansa show ID --merge-check-log` print one output whole, keys removed, under a line that names it, its length
and its file.
For setups and checks, a command killed by a signal exits with 128 plus the signal number on both Linux
and macOS. On Linux, a failed check or merge check runs once more only when hansa could read `oom_kill`
at both ends and its count rose, and hansa did not stop the run itself. Any failing exit, or no exit code,
can qualify; exit 137 or a missing exit alone cannot. Without a readable, rising count it ends as before.
A passing check keeps its count and passes. Cancellation, time ceilings and other stops made by hansa
never cause this retry. A merge check whose lane setup fails is not run again and shows the setup's
output as before. The rerun gets the lane's full time ceiling; a merge check keeps its place in line and
merges if it passes.
`hansa show ID --check` gives the first run's exit, last lines and the machine-wide evidence. The first run's
note records only what happened; hansa says it ran again only after the rerun starts. The first
run stays recorded if the base then moves and another merge check runs. If the rerun fails while the count
rises again, it says "failed twice while the out-of-memory killer ran" with both counts and a memory hint
(`hansa concurrency`); a rerun that fails without a rising count ends as an ordinary failed check.
Lane tries are requested by hand and
releases never restart: both only keep the count.

## Effort

Each run is started with an effort, and hansa records where it came from: a task's own `--effort` (`hansa add`,
`hansa retry`, `hansa verify`), else its provider's default effort (set on the page), else none, which leaves
it to claude's own default. Lanes have no effort setting. `hansa show ID` gives
it beside the provider and model (`claude (opus, effort high from the task's --effort)`), `hansa status`
gives it for each running task (and `hansa status --json` as `effort` and `effort_from`), and an earlier try
started with another model or effort names its own. On a provider whose endpoint is not Claude's own (kind
`compatible`: any endpoint that speaks the Anthropic API) they add that the endpoint may ignore effort: hansa
passes it on, but some such endpoints do not act on it.

## Task text and options

Give text inline or with `--file PATH`; add screenshots, patches or logs with repeatable `--attach PATH`.
Copies are saved under `.hansa/` when queued, so later source edits change nothing. Retry inherits them
and accepts more with `--attach`; verification inherits them too. Each run gets copies under its
temporary `attach/`, named in its instructions by full path. `hansa show ID` lists names and sizes.
To keep private data closed, folders, missing files, worker read denials (following links) and files
holding provider keys are refused. Limits: 20 files, 20 MB total, distinct names, including inherited files.

## Writing a task a verification can judge

A verification judges a change against the task's text, so most rounds of sending work back start in
that text. What holds up:

- Ask for the use that is needed. For a rare case (a lane removed and made again elsewhere, a cancel
  between two steps, records from an older hansa), say what the change does with it: refuse it with a
  way out ("give the work again with `hansa add`"), or accept it as it is. Do not ask for it to be
  handled.
- Give the cases of ordinary use as a table up front, one row each, with what each should do. A rare
  case gets a row only to say "refused" or "accepted as it is".
- Avoid "never", "any" and "all" in what you ask: each is a promise about every state.
- Ask for behaviour built on facts hansa or the program has (an exit code, a count it read), not on
  guesses about what another program did.
- When you ask for a test, say which failure a user would meet that it catches. Tests of test helpers
  or fixtures are not needed.
- In what a change stores or shows, ask for what happened, not a forecast: something coming in
  between makes a forecast wrong.
- Write the verification's text from the task's: the same rows, nothing stricter, and the list of
  what is accepted as it is.
- Send work back to narrow it. Removing a condition can widen what a change does; widening is a new
  task.
- Check the technical facts a task states on the machine, with the version the desk runs, before you
  queue it: an option's spelling, what a version does, where a file is.
- Check the names a task quotes from the code on the base branch as it is now. Give a line number
  only beside the name, as a guide.
- Put what workers cannot read into the task in full. A worker does not stop when a file the task
  names is closed to it: it works from what the task says.
- A task that changes how something starts, or the environment it starts in, lists each way the real
  desk starts that thing, and asks for a test that goes through one of those ways (its wrapper, its
  temporary folder), not only a call of the function.
- The numbers in a report are the ones this run produced, not copied from earlier text; workers are
  told so.

### Which provider for which task

Advice, not a rule hansa enforces:

- A task about a tool's own behaviour (its options, settings, hooks, plugins) goes to a model made for
  that tool or that knows it, and the task pastes in the parts of the tool's documentation it needs.
- A task a provider's content filter may refuse (probing a sandbox from inside, say) goes to another
  provider.
- With a provider that tends to write long, give long text tasks a word limit.

## Merging, the merge check and conflicts

`hansa verify ID` chooses a verify lane listing the task's lane: free first, then fewest queued,
then lanes' order; otherwise its own lane. `--lane L` overrides this.

A verification ends with `VERDICT: holds`, `VERDICT: holds, with notes`,
`VERDICT: does not hold` or `VERDICT: unsure`.
It says "does not hold" when ordinary use misses something the task asked for
or breaks something that worked before; in any state, when the change merges
into the wrong branch, loses work, shows a key, or reports a check passed that
did not pass; or when the only tests for a named defect still pass with the
code undone.

"Holds, with notes" means everything holds in ordinary use and the gaps found
show only in rare combinations of states: something coming between two steps
(a cancel, a restart, a failed write), old records or settings, or a setting
made by hand. The failures above still mean "does not hold". The report lists
the gaps under "Notes", each with what it would take to meet it. This verdict
counts as "holds" for merging, including release files, and for the suggested
next step. Show, the page and the merge command name the verification with
the notes. Only "does not hold" counts toward the advice to rethink below.

New verifications keep each top-level list item under the last Notes heading
(any level), including sub-items, as `VID.N`, with the checked task, title and
time. Missing or unlisted Notes keeps one report pointer. Other verdicts and
past verifications add nothing. `hansa gaps` lists unmarked notes oldest first
(160-character previews); `--all` includes marks and reasons. `hansa gaps show
ID` prints one whole. `hansa gaps mark ID fixed|later|wontfix "why"` requires
a reason; marking again replaces it, recording who and when. Records persist
in `tasks/VID/meta.json`. Posts give the count; status counts unmarked notes.

`hansa merge ID` merges a task's branch into its lane's base branch. A failed
check blocks it unless the coordinator says `--despite-check` (recorded), and
so does a verification that does not hold. That flag skips the merge check
even when the base moved; it never overrides release files or a key.

A [merge lane](desk.md) serves its repository and base's checks with its own worktree, sandbox, env, setup, command and limit; otherwise they use the task's lane as below. With a merge lane, a command of its own means its merge command differs from the task lane's check, which the task already passed. For long merge checks and busy task lanes: `hansa lane add m --like a`, its own caches through `write` and `env`, then `hansa lane set m merge_lane true`. One per repository and base, it takes no work (a lane with work queued or running cannot become one) and holds only itself, keeping the task lane free; it keeps the task lane's merge message and identity. It is chosen when the check starts: unmarking or removing it while queued restores the task lane, and a check already running keeps the lane it recorded. With every concurrency slot busy, a merge check takes the next slot before new work when a task ends anywhere, and that task's lane waits about one merge check; raising concurrency by one removes the wait if the machine can run the check beside the lanes' work.

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
  the base moves again while it is checked alone, it is checked once more on the new
  tip; after a second move it stops and says so. `--despite-check` merges at
  once without it (recorded).
- **One at a time, or a batch.** Merges into one repository and base branch keep
  the order they were asked. A check starting with others queued behind it takes
  the next ones too, up to its check lane's `merge_batch` (default 4, including
  itself). Set `hansa lane set L merge_batch N` to 1–10; `none` restores 4 and
  1 checks each task separately. This is the merge lane's setting when one serves
  the base, otherwise the task lane's. It stops before an `alone` task, a merge
  needing no check (`plain`), a different check lane, or a conflict. An `alone`
  first task takes none. Each member gets its own merge commit, message and
  identity, on top of the preceding member; setup and check run once on the last.
  After a pass, each member's branch, checkout, keys and forbidden words in what
  its own merge adds are checked before the base moves to the last commit. All
  land together, each recorded with its own commit, or none land.
  Any failed or stopped batch, moved base, landing refusal, or restart that cannot
  finish it returns every still-merging member to its old place marked `alone`.
  It stays alone until merged or sent back to review; members with their own
  landing refusal go back to review saying why. A failed batch is not divided:
  its extra check is followed by individual checks in merge order.
  `hansa status`, `hansa show` and the page name the members and whose check runs;
  each member's show points to that check's output and keeps its companions after
  merging. Events record the members at check start and on each merge.
  Merges also wait while a release runs in that repository. A plain merge waits
  its turn and goes in without a check if the base has still not moved, or with
  a check as above if it has.
- **Cancelling.** `hansa cancel ID` on a `merging` task stops its merge check
  and puts the task back in review. For a batch member it stops the shared check;
  only that member goes back to review, and the others keep their old places,
  each checked alone.
- **Closing.** `hansa close ID` closes a finished task without merging, or a finished verification
  while keeping its verdict. `cancel` on finished work refuses and points to `close`.
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

### Sending a task back

`--lane L` sends the follow-up to a work lane on the same repository and current
base branch as the task's own lane; that own lane must still be on the desk and,
if the task recorded its repository, still on that repository, or give the work
again with `hansa add --lane L`.

`hansa retry ID "<what to change>"` queues a follow-up with a new id from the
earlier task's last commit; the earlier task becomes replaced. Its title keeps the earlier
task's own title whole, followed by ` (retry of ID)`, naming only the immediately
earlier task; another follow-up replaces that suffix. The follow-up's worker gets the
original task, one line per earlier round (its id, how it ended, the start of
its note), this round's note, the previous report and the latest
verification's report, with its verdict. Earlier notes are not carried, so
what still holds of them is said again in the new note. Each round's note is
kept on its own beside its task, as `note.md` in the task's folder (keys
removed, as from what else hansa writes there).

A verification of a follow-up judges each ask in the original task together with
this round's note, which restates what still holds of earlier rounds' notes and
takes precedence where they differ, against the whole change.

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
A verification's scratch changes are kept under `refs/hansa/scratch/ID`, leaving its worktree clean
and the shared stash alone; `hansa show ID` gives the `hansa git` command to read them, and the daily
trim removes the ref 30 days after it ends.

## What work costs

Cost figures are Claude Code's own token counts, priced with the principal's
table of prices (on the page). With a subscription they show what the tokens
are worth, not a bill. `hansa cost` gives them (`--days N` or `--since HH:MM`;
`--by day`, `seat`, `lane`, `task`, `model` or `provider`; `--json`, with
`--rows` for every row). A task's turns and tokens are recorded when it ends.

To say what one job cost, note the time `hansa status` shows when it starts,
and run `hansa cost --since HH:MM --by seat` when it is done. Spend no turns on cost.

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
