# How a task runs

Time limits and resumes, retries after a dropped connection or a provider's usage limit, a check's output,
effort, merging, what work costs, and the states a task goes through. The
[README](../README.md#how-it-works) has the overview of a task's life.

`hansa status`, `hansa show ID` and the page show replies, idle minutes and the last note while a worker runs.
Claude Code's session record wins; otherwise hansa reads `progress/<session-id>.json` in the provider's Claude
home (`.hansa/claude-<provider>` for compatible providers): `{"replies": 3, "last": 1790000000, "note": "Checking tests."}`.
`last` is epoch seconds. Missing, unreadable or malformed progress means no reply yet. Notes are redacted and
cut to 160 characters. Codex counts commands and file changes as replies.

## Goals and product decisions

Record each goal the principal gives and each product decision as said. Hansa keeps the records;
the coordinator supplies the text, reason and who took the decision. After a clear or compaction,
read `hansa goal list` and `hansa goal show G` before going on. `hansa help intent-records` prints
this section.

The principal's page has a read-only [Goals tab](desk.md#status-work-and-cost-on-the-web-page)
with the same counts, decisions, evidence and lifetime cost.

| Command | What it records or shows |
|---|---|
| `hansa goal add "<title>" [--why "<what the principal asked, in their words>"]` | a new open goal; prints `g1`, `g2`, … |
| `hansa goal list [--all] [--json]` | one line per open goal: id, title, task counts, merged chains missing evidence, released chains and open to-do count; `--all` includes done and dropped goals with their outcome |
| `hansa goal show G [--json]` | the goal, current decisions newest first (reason, who, time, task), replaced decisions naming their replacement, latest task in each retry chain with state, rounds and evidence, total cost and rounds, and open to-dos |
| `hansa cost --by goal [--days N \| --since "YYYY-MM-DD HH:MM"]` | tokens and worth per goal in the same window as `hansa cost`; tasks without a goal and coordinator/helper usage are under `(no goal)` |
| `hansa goal done G "<outcome>"` | ends an open goal as done, keeping its outcome |
| `hansa goal drop G "<why>"` | ends an open goal as dropped, keeping the reason |
| `hansa decide "<decision>" --why "<reason>" [--goal G] [--by principal\|coordinator\|assumption] [--task T] [--replaces D]` | a decision; prints `d1`, `d2`, …; `--by` defaults to `coordinator`; `assumption` means still to be confirmed |
| `hansa decisions [--goal G] [--grep TEXT] [--all]` | current decisions newest first; `--all` includes replaced ones; `--grep` matches decision or reason, ignoring case |
| `hansa add "<task>" --goal G` | queues a task tied to the goal; `hansa retry` and `hansa verify`, including automatic verification, keep its goal |
| `hansa ask "<what>" --goal G` | ties a new principal to-do to the goal; closing it removes it from the goal's open to-dos |

Task counts use the latest task in each retry chain: queued/running, waiting for review (`done`
or `merging`), merged and failed. A merged task stays a chain of its own, with its merge, releases
and evidence; a follow-up sent on top of it is a new chain, `(retry of ID)` in its title, counting
only its own rounds. A follow-up cancelled before it started counts as one cancelled chain and
leaves the task it was sent back from as it was.
Cancelled tasks stay in `show` but do not add to these counts.
Verifications appear as verifications of their task, without counting the product work twice.
`hansa status` shows at most five open goals with the task and to-do counts and points to `hansa goal list`
for the rest. With no goals it adds no goal lines. Ended goals remain readable with `show`,
`list --all` and `decisions --goal G`.

Under each chain, `goal show` adds the evidence recorded for its latest task:

- `check:` passed, failed, could not run, or none, from the lane's check after that task.
- `verification:` holds, holds with notes (printed as `holds, with notes`), does not hold,
  unsure, running (including queued), or none; it names the verification task when present.
- `merge:` merged after its merge check, merged without one, merged despite a failed check
  or verdict, or not merged. A lane check and a merge check are distinct evidence.
- `release #N: TARGET · STATE` for each release whose outgoing commits include the merge,
  including releases from another lane or base. One release can appear under several chains.
  Pending and failed releases stay visible; only a `done` release makes the chain count as
  released in `goal list`. With none, it prints `releases: none`.
- For merged chains only, `missing:` repeats the merge's saved unverified note, and names
  any recorded merge despite a failed check or verdict or merge not checked. Older tasks
  without a saved note use the same `unverified_note` rule as `hansa merge`, including its
  exemption for text-only changes and its explanation of earlier retry verdicts.
  `missing: nothing` means none of those notes or marks applies; it does not imply a release
  happened or a separate merge check was needed. `goal list` counts each merged chain with
  any such note once as `merged missing evidence`.

Release links use the saved outgoing commit list, not just the release's tip commit. When
that list was capped, hansa reads the recorded commit range from the repository; if that
history is no longer available, it can only link the commits still in the saved list.
These views read evidence; they do not change what the merge gate checks.

The final `cost:` line is the goal's lifetime token count and dollar worth, including every
run of its tasks, follow-ups and verifications, with unpriced tokens called out. `rounds:`
is the sum of its chains' rounds; verifications add no chains or rounds. An empty goal has
zero cost and rounds. `cost --by goal` uses the usual prices, `--days` (default 7), `--since`
(also accepts `HH:MM`) and `--json`. Its rows add up to the ordinary cost total for that
window, including coordinator and helper usage in `(no goal)`; a desk without goals has
only that row.

`goal show --json` returns the goal record plus `decisions`, `chains`, `verifications`,
`todos`, `cost` and `rounds`. Each chain carries its latest task's id, title, state, lane,
rounds, check, verification and verification task id, merge description and commit, merge
marks, releases (id, lane, target, state, commit and times), and `missing`: `null` before
merging, an empty list when nothing is missing, otherwise a list of the same notes printed
as text. `goal list --json` returns an array of goal records with `counts`: `active`,
`review`, `merged`, `missing`, `released`, `failed` and `todos`; `--all` keeps its usual meaning.

An explicit `--goal G` on `hansa add`, `hansa ask` or `hansa decide` requires an open goal.
Retries and verifications, including automatic verification, keep their task's goal even when
the goal is done or dropped. `--task` requires an existing task.
`--replaces D` requires a current decision; if it was already replaced,
the refusal names its replacement. A decision is never edited or deleted: record a change of mind
as a new decision with `--replaces D`. Pass `--goal G` on the new decision to keep it under that goal.
These reference checks prevent recording new work under a missing or ended goal or overwriting a change
of mind; they remain needed while these commands accept references.

The append-only entries live under `.hansa/intent/`, separate from `notes/`. Each new goal,
decision, goal ending and decision replacement is a new entry, published atomically under the desk
lock; earlier entries are unchanged. Hansa supplies the time and writing session, as for tasks.
Text holding a known provider key is refused before anything is recorded, to keep keys out of
the record; this check remains needed while the coordinator can supply text. Goals and decisions
are internal records and do not undergo the release word scan. Goal evidence is read from
existing task, merge and release records; reports and checks remain on their tasks.

## Time limits, and work left running in the background

New lanes stop at 200 turns or 240 minutes; saved lanes keep their limits.
Change them with `hansa lane set L max_turns N` and `hansa lane set L timeout_min N`.

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

A worker's report ends with `Changelog: SENTENCE` for users, or `Changelog: none`.
Follow-ups describe the whole change on their branch. Verifications need no changelog line.

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
  A time up to an hour past gives about a minute's rest, and a clock time further past means tomorrow's;
  a form hansa is unsure of, a dated time more than an hour past, or a line it cannot read gets the length below.
- **Else for the desk's `cool_minutes`**, 30 by default.
- **No lost tries.** A task whose run ended on its provider's usage limit goes back to the queue without
  using one of its 3 tries, 5 times; after that, each such end counts as a try, so a provider that hits its
  limit again right after each rest cannot hold a task forever. Any other failure counts as before.
  `hansa show ID` says until when the task waits; `hansa status` and `hansa explain provider P` say until
  when each provider rests and why (the reset time it read, or its fixed length), and its rest length.
- **Other desks on the same account learn the rest too.** Desks sharing a subscription's Claude Code
  folder, or a key provider's exact endpoint and key, hold new runs until the same time. `show` and
  `status` say when another desk found the limit. Different folders or keys do not share rests;
  expired rests are ignored. Providers on one desk sharing that login also share usage rests.
  A good `hansa ping NAME` or page check ends a shared usage rest when the checked provider has the
  same configured model as the one that found the limit. Other desks become ready on their next
  provider read, including the desk that recorded the limit. A good check of a different model frees
  only that provider until a new usage limit is recorded; the original model may still be limited.
  Older records without a model count as the same model. A failed check does not clear a rest.
  The account's `provider-rests.json`, beside `desks.json` in
  `$XDG_STATE_HOME/hansa` (default `~/.local/state/hansa`), holds SHA-256 login, source-desk,
  source-provider and configured-model hashes, usage-rest start/end and clearing times, and credit
  rest/top-up times, with no key, endpoint, path, model name or error text in clear.
  Workers, setups and checks cannot read that folder. If the file is missing, unreadable or damaged,
  each desk uses its own rests as before.
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
A **Topped up** confirmation clears that login's credit rest on every desk on the account.
Saving a different key lets this desk use that key; it leaves the old login's shared rest in place.
The task moves to the lane's next provider without using a try; if all rest, it waits.
Once per credit rest, hansa posts a line to the coordinator and runs the principal's alert command,
if set. `status`, `show`, `explain provider P`, and the provider's page row say why and since when.

A lost login is not the task's doing either. When a run says it is not logged in (`Please run /login`), or a
subscription provider's run ends with HTTP 401 because its login ran out (`OAuth token has expired`), the
provider rests for a day, until a `hansa ping` of it answers, and the task moves to the lane's next provider
without using a try. A subscription provider's line says to log in with `claude` (with its `CLAUDE_CONFIG_DIR`
when it has one) and then `hansa ping NAME`. A key a compatible provider's endpoint rejects (HTTP 401 or 403)
still uses a try, and saving a new key brings that provider back.

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

Effort tells the model how much reasoning to spend on a run. The values are `low`, `medium`,
`high`, `xhigh` and `max`; a provider may not support every value.

Each run is started with an effort, and hansa records where it came from: a task's own `--effort` (`hansa add`,
`hansa retry`, `hansa verify`), else its provider's default effort (set on the page), else none, which leaves
it to claude's own default. Lanes have no effort setting. `hansa show ID` gives
it beside the provider and model (`claude (opus, effort high from the task's --effort)`), `hansa status`
gives it for each running task (and `hansa status --json` as `effort` and `effort_from`), and an earlier try
started with another model or effort names its own. On a provider whose endpoint is not Claude's own (kind
`compatible`: any endpoint that speaks the Anthropic API) they add that the endpoint may ignore effort: hansa
passes it on, but some such endpoints do not act on it.

## Queuing after another task

`hansa add` refuses a lane absent from settings and lists the desk's lanes, so a typo cannot leave a task
waiting indefinitely. Its queued reply includes the wait reason when the task cannot start now, including a
paused desk or a disk or memory floor. If `hansa serve` is down, it gives the command to start that desk and
does not promise a session post.

`hansa add "<task>" --after T` queues work now and starts it only when T has been merged into
the new task's lane's base branch. Repeat `--after T` for several prerequisites; every one must
be merged. A prerequisite already merged adds no wait. While waiting, the task takes no lane
or concurrency slot, and unrelated queued work can start. `hansa status`, `hansa show ID` and
the page name only the prerequisites still outstanding. For these tasks, `hansa show ID` uses
the same waiting or held reason as `hansa status` in its `meaning:` line.
`hansa help queuing` gives these rules.

Each T must already exist and have a lane on the same repository and base branch as the new
task's lane. Without `--lane`, hansa may choose any work lane on the prerequisites' repository
and base. An unassigned prerequisite is accepted when its eligible lanes all have the same
repository and base (or an earlier `--after` already fixed its target). A missing task, a
verification, an ambiguous target, or a different repository or base is refused before anything
is queued. This check prevents starting work without the changes it needs; it remains necessary
while `--after` accepts task references. The target is saved with the task, so changing a lane
cannot silently send it to another repository or base.

When T is sent back with `hansa retry`, the task waits for its latest follow-up, through any
number of retries. Once a task in that chain is merged, the dependency is met: a later follow-up of
merged work adds no wait and holds nothing, whether it waits, is cancelled or is closed.
If the latest task is closed, cancelled or fails without being sent back, the
dependent task stays queued but held. Hansa posts the reason to its coordinator once for that
dependency and attempt, naming both tasks and the choices: `hansa cancel ID` to drop the waiting
task, or `hansa retry T` (the latest follow-up when there is one). A later retry and merge clears
the hold automatically. Dependencies and notice records survive a server restart. Cancelling
the waiting task works as for any queued task; a retry of it keeps its dependencies.

## Task text and options

Give text inline or with `--file PATH`. Text files, including a regular file redirected to `--file -`,
follow the same worker read permissions as new attachment sources, following symbolic links and checking
private hard links. The check applies to `add`, `retry` and `lane brief`, including before handing text
to the desk's account. A refusal changes nothing. Inline text, pipes and here-documents remain usable.

Add screenshots, patches or logs with repeatable `--attach PATH`. Copies are saved under `.hansa/`
when queued, so later source edits change nothing. Retry inherits them
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
- When a task changes behaviour that existing tests pin, say which tests may change with it, or allow
  assertions of the old behaviour to change. Ask the worker to report each test it changed. Saying
  "keep every existing test" alone leaves it to fail its check or bend the change around an old test.
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

File names read from git are kept as written, including non-ASCII characters, spaces, quotes and
backslashes, regardless of the repository's or account's `core.quotePath` setting. This applies to
new task file records, release file matching, conflict reports and the files given to an `--update`
worker. `hansa show` uses the recorded names; `hansa diff --stat` and `hansa git` show non-ASCII
names as written. Names already saved by an older hansa are left as saved.

`hansa verify ID` chooses a verify lane listing the task's lane: free first, then fewest queued,
then lanes' order; otherwise its own lane. `--lane L` overrides this.

`hansa add` and `hansa retry` accept `--verify` to queue this verification after a passing check
(or with no check), if the task ends done with changes. `--verify-lane L`, `--verify-effort E`
and `--verify-note "text"` give its lane, effort and note; each implies `--verify`.
These options apply only to that task, never its follow-ups. The verdict goes to the task's
session without posting the task first; if queueing is refused, the task stays done and is posted
with the reason, so you can run `hansa verify` yourself. Hansa never merges by itself.
When these options lead to a verification, the review alert starts when the verification ends by
itself. After a verification is cancelled, no review alert fires; the task shows in `hansa status`.
A verification queued by hand leaves the review alert unchanged for tasks queued without these options.

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
time. A plain `Notes:` line (bold `**Notes:**` too) also starts the notes when
no Notes heading follows it: its list ends at the next heading or at the next
non-list line at the left margin, such as `VERDICT: ...`. Missing or unlisted
Notes keeps one report pointer. Other verdicts and past verifications add
nothing. `hansa gaps` lists unmarked notes oldest first (160-character
previews); `--all` includes marks and reasons. `hansa gaps show ID` prints one
whole. Each unmarked note, in the list and in show, ends with
`next: hansa gaps mark ID fixed|later|wontfix "why"` to copy.
`hansa gaps mark ID fixed|later|wontfix "why"` requires a reason; marking
again replaces it, recording who and when. Records persist in
`tasks/VID/meta.json`. Posts give the count; status counts unmarked notes.

`hansa merge ID` merges a task's branch into its lane's base branch. A failed
check blocks it unless the coordinator says `--despite-check` (recorded), and
so does a verification that does not hold. That flag skips the merge check
even when the base moved; it never overrides release files or a key.

The last definite verdict (`holds`, `holds, with notes`, or `does not hold`)
stands until a later verification gives another definite verdict. A cancelled
or failed verification, one ending without a verdict, or `unsure` takes nothing
back. This keeps a second opinion that gave no answer from clearing a refusal;
it is needed while verification can end without a definite verdict.
`hansa show` names the standing verification and the newest attempt when they
differ. The page's task list, goal evidence, the count of rounds that did not
hold, and the verification report given to a follow-up use that standing verdict.
A newest verification still queued or running continues to hold the merge.

The base must still hold the commit the task started from. If the lane was
repointed on an older desk, or its history rewritten, hansa refuses the merge,
names that starting commit, and counts the commits beside the task's own that
would come with it. `--despite-check` cannot override this: use
`hansa retry ID --fresh "<the change on the current base>"`. This guard prevents
bringing old-base history into another base; it is needed while tasks can
outlive base changes or history rewrites. `hansa lane set L base NEW` refuses
while that lane has running or merging tasks, or finished work waiting for
review: merge or close them first (cancel running work first). Queued tasks that
have not started do not hold a base change.

A [merge lane](desk.md) serves its repository and base's checks with its own worktree, sandbox, env, setup, command and limit; otherwise they use the task's lane as below. With a merge lane, a command of its own means its merge command differs from the task lane's check, which the task already passed. For long merge checks and busy task lanes: `hansa lane add m --like a` (the copy has no release targets: a merge lane releases nothing), its own caches through `write` and `env`, then `hansa lane set m merge_lane true`. One per repository and base, it takes no work (a lane with work queued or running cannot become one) and holds only itself, keeping the task lane free; it keeps the task lane's merge message and identity. It is chosen when the check starts: unmarking or removing it while queued restores the task lane, and a check already running keeps the lane it recorded. With every concurrency slot busy, a merge check takes the next slot before new work when a task ends anywhere, and that task's lane waits about one merge check; raising concurrency by one removes the wait if the machine can run the check beside the lanes' work.

- **When it merges at once.** When the base's tip is already in the task's
  recorded head and the lane has no merge check of its own (below), when the
  task has no commits, or when the lane has neither a check nor a `merge_check`,
  `hansa merge` merges with no check; it still waits its turn behind other
  merges into that repository and base, or while a release runs there. A lane
  without a check has its tasks merged untested: give it one with `hansa lane
  set L check "<command>"`.
- **When the base has moved.** When the base has moved (another task was merged
  meanwhile, say) and the lane has a check or a `merge_check`, the two together were never
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
  needing no check (`plain`), a failed task's `--recheck`, a different check lane,
  or a conflict. An `alone`
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
  It also puts away a failed task or verification, whatever stopped it and even when its lane
  has since been removed: the task becomes `cancelled`, closed without merging as one closed from
  review is, and `hansa show` keeps why it failed; a verification stays `failed` with a closed note,
  and the task it checked is unchanged. Closed, neither is listed under `failed (latest)` in
  `hansa status` (which shows the latest five failed that are not closed, and how many more there
  are; `--json` gives the count of all of them as `failed_count`), nor counted failed by a goal.
  A task queued `--after` it is held as for a prerequisite closed without merging; `hansa retry`
  goes as for any closed task. `cancel` on a failed task refuses and points to `close`.
- **`--recheck`.** `hansa merge ID --recheck` goes the same way whether or not
  the base has moved: the task merged onto the base's tip, the lane's setup and
  merge check run there when the lane is free, and exactly that merged if what
  ran passed. If the task's check failed or could not run, its lane's check runs
  there too, before the merge check, using the same worktree, sandbox and env
  (the merge lane's when one serves it). Identical commands run once. A failed
  command stops the sequence and nothing merges. This prevents a different
  merge command from bypassing a failed task check; it is needed while the two
  commands can differ. A failed task's recheck runs separately so each task keeps
  the results of its own repeated check. A passed task check needs only the merge
  check, as before.
  Use it once the machine problem is fixed. `hansa show` and the event log keep
  the commands and each exit; the event log also records who asked.
  With `--despite-check`, the checks are skipped and the override recorded.
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
its note), this round's note, the previous report and the standing
verification's report, with its verdict. Earlier notes are not carried, so
what still holds of them is said again in the new note. Each round's note is
kept on its own beside its task, as `note.md` in the task's folder (keys
removed, as from what else hansa writes there).

A done, failed, closed or cancelled task becomes replaced, so it can be sent back once; a second `hansa retry` of it is
refused and names the follow-up to send back instead. A merged task stays merged: each
`hansa retry` of it is new work on top of that merge, and it can be sent back more than once.

A follow-up cancelled while still queued, before it ever started, gives the task it replaced back
as it was (waiting for review, failed, or closed), and `hansa cancel` says so; `hansa retry` of
the cancelled follow-up is then refused and points to that task. A follow-up the machine kept from
starting (`next:` says `hansa retry` it once the machine is fixed) has no commit of its own; its
retry starts where it would have started, the earlier task's last commit, and is given that
earlier task's report and, if the never-started round has no standing verification, the
earlier task's standing verification report. Closing that verification because the earlier
task was replaced does not discard its verdict or report.

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
trim removes the ref 30 days after it ends. When git cannot keep them (the ref is locked, say), the
verdict stands and `hansa show ID` says the scratch changes could not be kept, with git's words.

When hansa cannot commit a worker's work as the run ends (a locked git index, or any other git error
there), the task fails: "hansa could not commit the work: " and git's first line. `hansa show ID` gives
all git said and the command that reads the work, which hansa keeps under `refs/hansa/kept/ID` (made
without the worktree's index, so a locked one does not stop it). The lane's next task starts clean:
if git could not clear the worktree then, that task clears it, as the work is kept. `hansa retry ID`
works as for any failed task: it starts from the base, since the work was never committed.

Anything else a task finds in its lane's worktree when it starts is set aside in `git stash`, named
"hansa: left over before ID". That task's `hansa show` says so: the stash commit, the last task that
ran in that worktree, and the `hansa git` commands that show the changes and the new files.

## What work costs

Cost figures are Claude Code's own token counts, priced with the principal's
table of prices (on the page). With a subscription they show what the tokens
are worth, not a bill. `hansa cost` gives them (`--days N` or `--since HH:MM`;
`--by day`, `seat`, `lane`, `task`, `goal`, `model` or `provider`; `--json`, with
`--rows` for every row). A task's turns and tokens are recorded when it ends.

When the table has no entry for a Claude model, workers use Claude Code's
recorded list-price total for the run. This fallback follows the model even if
its provider was renamed, removed or changed kind. A worker's run without a
recorded figure stays unpriced. The coordinator's own usage and its helpers need
an entry in the table: their session records have no recorded dollar figure.
Other models without an entry stay unpriced; a third-party model's placeholder
dollars are never used. The principal's table takes precedence, including a
price of zero.

`hansa status` shows the worth of the priced worker tokens in the last 24 hours
and counts any tokens with no price yet, pointing to the principal's prices on
the page. If none have a price, it shows no dollar value. `hansa cost` shows
`no price` in place of dollars for a wholly unpriced row; a partly priced row
shows its priced worth and how many of its total tokens have no price.
The JSON fields stay the same: an unpriced usage row has `usd: null` and
`priced_by: "no price"`.

To say what one job cost, note the time `hansa status` shows when it starts,
and run `hansa cost --since HH:MM --by seat` when it is done. Spend no turns on cost.

The coordinator's own session, with its helpers, is counted only when it runs
with the workers' Claude login (the same config folder), because only then are
its session records where hansa reads them. What the coordinator costs is
everything spent in its working folder. Every session started in the desk's
own folder counts, as `hansa coordinator` starts it there. For a coordinator
started in another folder (one `hansa up` started there, or `hansa notify on`
recorded), each hansa command run in the session the start record names notes
the id Claude Code gives the command, and the login folder it runs with, in
`.hansa/coordinator-sessions.json`. A compaction keeps the id; after a `/clear`
the session's next hansa command notes the new one. The folder the noted
sessions ran in, found by where their records are, is the working folder: every
session recorded there under the workers' login counts as the coordinator's,
noted or not, its helpers as helpers, for its turns since the desk began (the
desk's first event); sessions in other folders do not. When the noted sessions
move to another folder, the old folder counts up to when hansa noted the first
session in the new one, and the new one from then on; a noted session counts
whole since the desk began, wherever its records are. Until a session is noted
(the desk's own folder counts all the same), and when the coordinator runs
with another login, as another user (root on a Linux server: [On a Linux server](linux-server.md)),
or its records cannot be read or found, `hansa cost`, `hansa cost --turns` and
the page say it is not counted, and why.

The coordinator has no lane or task, so in `--by
lane` and `--by task` its usage is a row of its own, `(coordinator)`: in every
view the rows add up to the total. `hansa cost --turns` shows the
coordinator's own usage turn by turn (what started each turn, its steps, the
conversation's length at its first step, its worth and the tools it called;
then totals by what started them; no message text), which shows where its
money goes. A turn's time runs from its first step in the window to its last;
`hansa cost` gives the coordinator's turns and that time over the window.

## Task states

`hansa wait [ID ...]` returns when a watched task ends. With no IDs, it watches all queued,
running and merging tasks. When dispatch is paused or under a disk or memory floor, it returns
at once with the reason only if every watched task is queued; running and merging tasks are
still waited on, even if queued tasks are watched alongside them. With no tasks to watch, or
with `hansa serve` not running, it returns at once. For a stopped server, it gives the command
to start that desk.

While the lane's setup runs, status and show name it and show its elapsed minutes instead of saying the
model has not replied. Once setup completes, model progress is shown as above.

A queued task with an available lane and provider still needs a concurrency slot. Status, show and the page
say how many slots are occupied and name `hansa concurrency`; status JSON uses `waiting_code: slot`.
A merge check also waits for a slot, including when it uses the task's own lane; status and the merge reply
say so.

`hansa help states` prints this section.

| State | What it means |
|---|---|
| `queued` | waiting for a free lane and a ready provider |
| `running` | a worker is on it, or hansa is running the lane's check on its work |
| `done` | the worker finished; waiting for the coordinator's review (`hansa show` / `hansa merge` / `hansa retry`) |
| `merging` | `hansa merge` is checking it, as the base's tip is not in the task's recorded head or the lane has a `merge_check` of its own: merged onto the base as it is now, with that `merge_check` (else the lane's check) run there; it merges if that passes. Or it needs no check (a task with no commits never does) and waits behind other merges on the same repository and base, or a release running there |
| `failed` | the run ended with an error; `hansa show` says why. `hansa retry` sends it back; `hansa close` puts it away (a task becomes cancelled, a verification stays failed), and `hansa show` still says why it failed |
| `merged` | merged into the lane's base branch |
| `cancelled` | stopped or closed on request |
| `replaced` | a follow-up task took its place |

A process hansa cannot look at is not taken as ended: only a lookup that says there is no such process (`ESRCH`), or a start time that reads differently from the one hansa saved, counts as gone. `EPERM`, `EACCES`, `EIO` and other errors leave the run as it is. A task an earlier server left `starting` fails after five minutes if hansa still cannot read the process list, as before; its lane then takes no other task until a look that works finds no process of that run. `hansa status` says why on the lane's line, and a queued task waiting for that lane says the same.

A task that cannot start for a reason of the machine or of its lane, not its own, stays `queued` (no try used), and the lane waits, as for a sandbox that cannot start: `hansa status` shows the lane as `UNABLE TO RUN` with the reason in one line, and the queued tasks waiting for it say the same; `hansa show` (the task that met it) and `hansa explain server` give the whole reason. The coordinator is told once for each lane and reason. The tasks start by themselves once the cause is gone, with no `hansa retry`:

| Cause | Lanes that wait | When the tasks start |
|---|---|---|
| no `claude` where `hansa serve` looks | every lane that takes work | once `hansa serve` finds it (install Claude Code on its PATH; `HANSA_CLAUDE` is read as `hansa serve` starts, so after setting it, restart `hansa serve`) |
| `HANSA_CLAUDE` names a file that does not exist or cannot be run | every lane that takes work; no other `claude` runs in its place | once that file can be run, or after `hansa serve` is restarted with `HANSA_CLAUDE` set right or unset |
| the lane's repository folder is not there (moved or removed) | that lane | once the folder is there again; if it was moved or renamed, cancel or close the lane's tasks (`hansa cancel`, `hansa close`), then `hansa lane remove L`, then `hansa lane add L --repo NEW_PATH`, then queue the tasks again: they do not carry over, and start in a fresh worktree ([a lane's repository moved or renamed](desk.md#a-lanes-repository-moved-or-renamed)) |
| git cannot prepare the lane's worktree (a file in it git cannot read, say) | that lane; the others go on | `hansa serve` tries again every minute; they start once git can prepare it |

If a nonempty `HANSA_CLAUDE` names a missing or unrunnable file, `hansa doctor`
reports a failed Claude Code row with the value and remedy; the same reason
appears in `hansa status`, in `hansa coordinator`'s `hansa: ...` error (exit 1),
and in failed `hansa ping` and page provider Check results, without substituting
another `claude`.
Empty or unset `HANSA_CLAUDE` keeps the usual PATH lookup.

A task that cannot start for a reason of its own (a file it was given is gone, say) fails, as before; `hansa show` then says to fix what kept it from starting first, then `hansa retry ID`.
