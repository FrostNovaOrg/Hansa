# Changelog

Up to 0.4 this project was called Bellhop, and its command was `bellhop`. The
entries for those versions keep the names of their time. Commits up to 9 October 2026 carry the licence of their time, the AGPL-3.0-or-later with a commercial licence; from then until 3 November 2026, the Apache License 2.0; since then, the MIT License.

## 0.5.0a1 — alpha, unreleased

Hansa is a rewrite: one directory holds a desk, with a command line for the coordinator,
a web page for the principal (the person directing the work), and task workers. MIT License.

### Tasks and workers

- Queue work on lanes, each with its own git worktree, providers, instructions and limits.
  Several lanes can work at once; the coordinator sets concurrency and decides what to merge.
  Workers change files and report; hansa commits their work and cleans up processes.
  Workers try changes as their user would and report what a page or command showed before and after.
  They leave alone what the task says to leave.
  Leftover processes are also found by their working folder and stopped before the commit and check.
- Workers and verifiers have auto memory off, whatever repository settings say; their CLAUDE.md still loads.
- `hansa add --after T` queues work that starts only after T is merged into its lane's base.
  See [queuing after another task](docs/tasks.md#queuing-after-another-task).
- When hansa cannot inspect a process, its task keeps running and its lane stays taken.
- Retry with a note, updating from the base or moving lanes on the same repository and base; findings carry over.
- Cancelling a follow-up before it starts restores the task it replaced, including its verification.
- Retrying a follow-up the machine kept from starting preserves the earlier work, report and standing verification report.
- Closed and cancelled tasks can be replaced only once; another retry points to their follow-up.
- `hansa close ID` also puts away failed tasks and verifications, keeping why they failed in `hansa show`.
  Status lists only failures not closed, shows how many more there are and gives `failed_count` in JSON;
  goals no longer count closed failed work as failed.
- Once a prerequisite is merged, later follow-ups do not hold tasks queued with `--after`.
- Follow-ups of merged work count as new work in goals; the merged task keeps its releases and evidence.
- Request verification by another worker, optionally on another lane or model. `add --verify`
  and `retry --verify` queue it after a passing check, or with no check, for completed work
  with changes. “Holds, with notes” permits merging; saved notes can be tracked as known gaps.
  `hansa gaps` gives each unmarked note its marking command.
  Notes under a plain `Notes:` line are split into separate [known gaps](docs/tasks.md#merging-the-merge-check-and-conflicts).
- Session notifications bring results to the coordinator. `hansa ask` keeps the principal's to-dos;
  answers happen in conversation and the coordinator records the outcome. `hansa wait` waits
  for tasks only. See [task handling](docs/tasks.md).
  `hansa notify on` returns only when its follower is ready to hear new events.
  Lines refused by the coordinator's session are posted once it accepts again.
  Continued refusals alert the principal.
  When API errors keep ending turns, hansa reposts unhandled work and alerts after 30 minutes by default,
  or at once for errors only the principal can fix; after an alert, a normal turn sends a recovery notice.
  Server startup writes missing events for saved task ends even after a long stop, without writing any twice.
- Posts to the coordinator include tasks cancelled or closed elsewhere and survive titles with bytes that are not UTF-8.
  Long posts keep the newest ten lines whole and summarize each earlier result.
  After a session's `/restart`, `hansa notify on` from that session restores posting and delivers what was missed.
- Missing Claude Code, an unusable `HANSA_CLAUDE`, a missing repository folder or git failing to prepare a worktree
  holds tasks in the queue without using a try. Status gives the reason; tasks start by themselves once it is fixed.
  For a task that failed to start, `hansa show` says what to fix before retrying and omits empty model details.
- `hansa add --lane` refuses an unknown lane and names the desk's lanes. Queuing says why work cannot start now,
  including a pause or a disk or memory floor; with the server stopped, it gives the command to start it.
- Status, show and the page name a task's wait for a concurrency slot, with the occupied and allowed counts.
  While a lane's setup runs, status and show name it and its elapsed minutes instead of saying the model has not replied.
- `hansa wait` returns at once with no work to watch, a stopped server, or only queued tasks held by a pause
  or a disk or memory floor; it keeps waiting for running and merging work under those holds.
  A session that starts hearing hansa while the server is down gets that notice once, with the start command.
- A lane whose repository folder moved or was renamed gives the recovery commands in order: cancel or close its tasks,
  remove the lane, add it at the new folder and queue the tasks again. Lane removal moves an unreachable worktree aside
  within the desk (`--discard` deletes it); adding a lane sets aside a worktree left from another or unreachable repository
  so work starts fresh, saying when the other repository still needs `git worktree prune`. Set the re-added lane's check
  and other settings again; it starts with defaults.
- `hansa goal` and `hansa decide` record the principal's goals and reasons for product choices.
  Tasks tie to goals.
  `hansa goal show` lists each goal's checks, verdicts, merges, releases and what is missing.
  `hansa cost --by goal` shows each goal's cost. See [goals and decisions](docs/tasks.md#goals-and-product-decisions).

### The sandbox

- On Linux, leftover processes are stopped and listed by `hansa show` even when the kernel cut a process name
  inside a multi-byte character, which previously left the process running and logged a decoding error.
- Folders hansa makes for desks, new projects and the coordinator's mod are private whatever the umask.
  For an existing folder others can write in, hansa gives the `chmod go-w` fix.
- Worker commands run in an operating-system sandbox with lane-specific network hosts and write
  folders. Setup, checks and lane tries use srt; commands cannot fall back to running outside it.
- Runs cannot read the desk's private files, other runs' temporary folders or common credentials.
  Listed desks are kept apart; an unusable account desk list holds runs. The principal can add
  read restrictions; invalid rules stop new runs, while repository or PATH conflicts are reported.
- Lane caches stay separate from installed tools and shared configuration. Lane environment settings
  reach sandboxed commands, not hansa itself. Git metadata and protected paths are checked.
- A lane's write list refuses every lane's repository on the desk, folders inside it and folders above it, through links too.
  Runs leave out such entries saved earlier; adding a lane refuses a repository overlapping an existing write entry.
- Runs get private temporary storage; Linux lane commands normally use the desk's disk.
  Java and Maven receive temporary-directory and proxy settings; sandbox startup failures hold workers.
  See [the sandbox guide](docs/sandbox.md).
- The optional macOS [VM bench](docs/bench.md) is off by default; `hansa bench build` prepares a fixed Debian image.
  `hansa bench selftest` checks isolation and shutdown before `hansa bench on`; bench code upgrades need fresh acceptance.
  With the bench on, workers use `hansa bench run` for Linux, browser and terminal checks, with output and artifacts.
  Each hand-in starts fresh with only its own loopback, no downloads or host dependencies, and 4 GiB across
  `/work`, `/out`, `/tmp` and `/dev/shm`. Timeouts keep output and files; teardown finishes before commit.
  `hansa bench off` cancels hand-ins; failures leave task handling running. Valid artifacts return if others are rejected.
  `hansa bench status` explains holds; `hansa serve` prints a bench failure reason only when it changes.

### Merging and checks

- File names from git keep non-ASCII characters, spaces, quotes and backslashes in task records,
  release file matching and lists, text-only detection, word scans and conflict reports;
  `hansa show`, `hansa diff` and `hansa git` show them correctly.
- A task's check runs after its worker. A merge check tests the combined tree when the base has
  changes absent from the task, or a separate command is needed, with its own limit or check lane.
- A merge check waiting for a concurrency slot says so in status, show, the page and the merge reply,
  including when it uses the task's own lane.
- When git cannot commit a worker's work, the task fails with git's words and keeps the work under
  `refs/hansa/kept/ID`; `hansa show` gives the error and the command to read the work.
- `hansa show` names the stash holding worktree leftovers found before a task starts, the last task that ran there
  and commands to read the changes and new files.
- A verification whose scratch changes cannot be kept says so in `hansa show`, with git's words; its verdict stands.
- Merges queue per repository and base. Compatible merges can share a check; a failed batch
  is checked again individually in its original order.
- Lane copies omit release targets; merge lanes refuse target settings and releases and show how to remove old targets.
- `hansa merge --recheck` checks again; `--despite-check` can override a check failure.
  Stored keys and conflict markers always block merging. Configured release files require
  a holding verification, including “holds, with notes”.
- The last definite verification verdict stands until another definite verdict replaces it.
  An `unsure`, cancelled or failed verification, or one without a verdict, does not clear it;
  `hansa show`, the page and goals show the standing verdict.
- Lane base changes refuse running or merging tasks and finished work waiting for review.
  `hansa merge` refuses a base that no longer holds the task's starting commit, counts the extra history
  and points to `hansa retry --fresh`; `--despite-check` cannot override this.
- After a task check failed or could not run, `hansa merge --recheck` reruns it before the merge check and stops at the first failure.
  Identical commands run once; `hansa show` names each check, whether it ran and its result.
- **Forbidden words support safe `re:` patterns.** Optional mailbox rules also check outgoing changes
  at merge and release. `hansa lane try` tests a lane without a worker.
- Push checks run on GitHub's Ubuntu machine for tests a sandbox cannot run, plus adapter and plugin
  checks. Pull requests run the full suite on Ubuntu; manual runs add macOS and its real sandbox
  runtime. See [contributing](CONTRIBUTING.md).
- GitHub's checks run the documentation tests.

### Releases and alerts

- Named targets have commands, time limits and optional dependencies on another target releasing
  the same commit. Requests show outgoing changes and direct commits, which can require approval.
- Release requests and lists show at most ten direct commits, their count and a command to see the full range.
  The instruction to wait for the principal follows the head; void, replaced and stopped requests take one line each.
  Outside-path warnings appear in lists and `--show` only while a request waits for approval.
- `hansa explain lane L` gives each target short lines for its command, approval and last successful release,
  with the latest request separately. `hansa lane target L` shows every target's approval tick;
  both commands and a release request say when a tick lapsed and why.
- A release command's local `git push` destination is described as where it sends, without advice to move it into the repository.
- `hansa release` refuses an unknown target word even on a lane with only one target;
  name the target before a one-word note, or give a quoted note with spaces.
- A repeated release request names the waiting request it replaces and says its code no longer works.
  At the last release's commit, it first says there is nothing new and the release was asked to run again.
- A target's first release with several remotes counts, shows and scans every commit missing from at least one remote,
  including for fixed-checkout targets; the request and page show how many each remote lacks.
- Approve on the page or pass the one-time code from the principal's alert through the coordinator.
  A target ticked on the page may run without asking, including after merges settle; changes
  to commands, inputs or permissions invalidate approval.
  Settings shows each target's last release time and outcome; missing descriptions prompt `--about` there and in `hansa lane target`.
- Release commands run outside the sandbox. Ordinary releases hold their repository; fixed-checkout
  targets let merges continue. Releases can be stopped; results return to the coordinator.
- After a push, a target's `--watch` command follows CI without holding other work and posts whether it passed.
  The [CI watch example](docs/releases.md#watching-ci-after-a-push) keeps waiting through network errors until the run ends.
- `hansa release --show ID` shows a request and all retained release and watch output, keys removed, including output
  so far while running. Failed release lists and posts point to it for more; the whole log stays the principal's.
- `hansa release --stop ID` and Stop on the page also end a running watch, keeping the release done and recording
  who stopped the watch and why. A stopped watch stays stopped after a server restart and its log is trimmed like others;
  a stop after the command ended keeps its result. Release stops name the coordinator, the principal or a terminal command in words.
- A malformed one-time code uses none of the principal's wrong-code tries, for either `hansa release yes` or `no`.
- Watch commands warn about forbidden words when saved and refuse releases containing them.
- The principal sets the alert command. Alerts are grouped and rate-limited; closed to-dos are omitted
  and failures shown. Low disk space holds new work until space recovers, with a floor scaled to
  the volume. See [releases and alerts](docs/releases.md).
- Long alert batches keep every alert's start, urgent ones first. Release alerts put the request, code and what to do
  before the target's description; requests that no longer wait are left out, while their results still go out.
- A successful alert-command run clears an earlier failure warning. A waiting release whose code-bearing alert failed
  says the code may not have arrived and points to the page; status also says to use the page when no waiting release has a live code.
- `hansa status` says how many alerts the hourly limit holds and until when, leaving out answered requests
  and excluding test alerts from the held count.
- Recovery from low disk space, a lane's sandbox failure or a stuck server sends an all-clear with the trouble's
  start and end times. A sandbox failure alerts again after a passing try cleared the earlier hold, even for the same reason.
- Alerts about no session hearing hansa count only work that still waits and no longer fire for a server starting again.
- A coordinator that handles a post and then stops on a permission dialog still alerts after the waiting time.
- Work waiting for review still alerts after a session or server restart, from whichever session watches it.
  A queued or running verification suspends that wait; the review status line appears only while the coordinator is idle.
- Alerts and status describe a failed request the principal typed as that request failing at its time, with advice to type it again.
  Login, billing and other errors only the principal can fix give that remedy first.
- After reposting work, an API-error alert waits for the turn it started to finish, or for the time allowed for it to start.
- Problem alerts give a next step the principal can take, including how to restore a session or ask for review;
  every coordinator API error has advice. Chinese alerts use Chinese throughout, quoting task titles and programs' own words
  and naming the provider or lanes concerned. `hansa explain` and the page also list alerts for failed turns and provider credit.
- Status shows the configured minutes for each coordinator alert, or that it is off.
- Alerts use English or Chinese, carry attention levels and keep the release's description.
  Push addresses and the alert command send independently.
  Save up to ten private push addresses on the page; paste an address to recognize its service, or choose it.
  Switch each on or off, send a test push and see its last result. A saved Bark key moves into the list.
  An old Bark setting with no key to move is cleared instead of reported.
  Use Bark (also self-hosted), ntfy, Gotify, PushDeer, Server酱, WeCom, DingTalk, Feishu/Lark,
  Telegram, Discord, Slack or a JSON webhook; bots support signing secrets. See [push services](docs/push-services.md).
  Choosing Bark for another service's address is refused, naming that service.
  Secret parts stay out of settings, events, status and the page and go only to their own service.
  They stay hidden through unreadable entries, saves cut short and address files saved by older versions.
  An unreadable entry does not stop the others from sending.
  Deleted Discord webhooks are named as such in `hansa status` and on the page.
  `hansa help push-services` explains setup.
- Discord and Slack mentions remain text and ping no users or channels; Slack alerts use plain text.
- Bark entries can encrypt alerts with the key set in the app and on the page, using AES-128, -192 or -256 in CBC mode.
  Bark's server and Apple see ciphertext; entries without a key send as before. The page and status mark encrypted entries,
  and test pushes explain key mismatches. English and Chinese guides cover setup and privacy.
- A Bark encryption key made invalid by a file edit sends nothing and carries no release code; the page and status
  say to set it again. Bark encryption examples may include links to other sites. Other services' tokens and signing
  secrets still work when their files contain extra blank lines.
- Encrypted Bark entries receive the same one-time release code as the alert command; other push entries ask for approval on the page.
  Code delivery status counts every eligible channel, and saved results omit service responses for pushes carrying codes.
  If making a code fails, the whole alert batch still goes out, asking for an answer on the page.
- `hansa changelog` collects workers' release notes, missing notes and direct commits.
- With no earlier release or remote-tracking history, `hansa changelog` leaves out history from before hansa managed
  the repository, using the same cutoff as release requests.

### The web page

- English and Chinese views show the pipeline, active lanes, waiting decisions and searchable tasks.
  Task details show each step, reports, checks and verification. Notes and known gaps are readable.
  Tabs wrap on phones and narrow windows so every tab stays visible.
- Name the desk on the page; alerts, push titles, the page, `hansa status`, `hansa desks` and the menu use it.
  Status, the desk list and the menu also show the folder.
- The Goals tab shows each goal's decisions, its work's checks, merges and releases, what is missing and its cost.
  See [goals on the page](docs/desk.md#status-work-and-cost-on-the-web-page).
- Work and cost includes a work calendar, lane timeline, outcomes, token usage and CSV export.
  All attempts count; whole history includes identifiable coordinator and helper sessions, including
  recorded working folders outside the desk. Missing usage and unpriced models are explained.
- Settings group providers, coordinator, lanes, read restrictions, releases, alerts and prices.
  Lanes are read-only; dispatch controls are in Settings. Drafts survive tab changes and failed saves;
  concurrent settings changes are shown before another save.
- Push addresses show the recognized service while typing. Bare Bark keys need at least eight characters;
  Bark and ntfy refuse each other's official addresses and name the service to choose.
- Saving a duplicate push address names the entry that already has it. Older duplicates leave unrelated settings saves
  working, and status names the entries sharing an address.
- Before saving an ntfy topic, the page warns that hansa hides it wherever it appears
  and recommends a topic that is hard to guess.
- Push-service refusals show the service's code and what to check. Malformed addresses get a specific reason;
  save refusals on the Chinese page are in Chinese.
- Replacing a push address clears its old delivery result.
- Clear a saved extra secret while keeping the push address. Clearing a Bark encryption key first warns that later
  pushes will be unencrypted and carry no release codes; a blank replacement field still keeps the saved secret.
- “Send a test alert” reports every configured channel, and a failure in any channel makes the test fail.
- Emptying a coordinator-alert minutes box restores the default; saving says which default applies.
- `hansa explain history` and Settings show who changed each setting, when and old → new; filter by lane or provider. Keys stay hidden.
- The page is compressed and cached by the browser when unchanged.
  Polls stay small and back off on connection loss.
  Reconnecting keeps the last received status visible.
- Each tab keeps its token in `sessionStorage` and sends `Authorization: Bearer` on API requests;
  changes also need `X-Hansa: 1`. The token is removed from the address; cookies do not authenticate requests.
  See [the desk guide](docs/desk.md).

### Providers and usage limits

- Use Claude's login or compatible providers, with per-provider models, effort and ordered lane
  selection. Known keys are redacted from published text and logs.
- Usage limits requeue work and rest the provider until its reported reset, or a configured rest.
  Desks sharing a login share usage and credit rests; confirming a top-up clears shared credit rests.
- Connection failures hold the provider until a check answers, then resume tasks from safe saved
  commits. Repeated counted failures stop the task; a system-killed worker can restart once.
- `hansa ping` uses the server's recorded Claude program, including an adapter; checks use no tools.
- A `HANSA_CLAUDE` naming a missing or unrunnable file gives hansa's own one-line error in `hansa coordinator`,
  a failed `hansa ping` and provider Check, and a failed Claude Code row in `hansa doctor`, with the remedy.
  No other `claude` runs in its place; details are in the task guide.

### The terminal menu and `hansa doctor`

- `hansa init` and `hansa lane add` create a repository with an empty first commit for a new or empty project.
  A folder already holding files is refused, with the commands to commit them.
- `hansa init --repo` describes the lane it made and warns when it has no check, with the command to set one,
  as `hansa lane add` does.
- `hansa init` refuses a desk folder that already holds files and gives a command using a separate folder.
  It also refuses lane names that `hansa lane add` refuses. A desk inside a git repository is kept out of its commits,
  both at init and when adding that repository as a lane.
- In a terminal, `hansa` or `hansa up` outside a desk opens the desk menu: select desks to start,
  join a coordinator, open the page, pause dispatch or stop a server. `hansa desks` lists desk state;
  `hansa up --all` starts all listed desks, restoring recorded server and coordinator starts.
- Outside a desk without a terminal, `hansa up` starts nothing and lists the account's desks with
  `hansa --desk DIR up` and `hansa up --all`. Stopped-server notices to sessions outside the desk give the `--desk` form.
- `--desk` or `HANSA_DESK` naming a missing folder or one that is not a desk is refused before anything is written,
  pointing to `hansa init DIR` instead of treating it as an empty desk or printing a traceback.
- A second interactive `hansa coordinator` is refused with the running session's pid, name and tmux attach command
  when available. Runs with `-p`, help or version leave the interactive session's saved start alone.
- A failed `hansa serve` bind gives one line with host, port and the system's reason and keeps the last successful
  start for `hansa up`. Simultaneous `hansa up` calls wait for the first start, then report the server running.
- `hansa up` starts a coordinator on a desk that never had one, including with tasks added from a shell.
  It resumes the coordinator's own conversation by session id, or starts a new one when none remains, and says which;
  older records still continue the folder's latest conversation until its id is recorded.
- When the coordinator fails to start, `hansa up` reports its last output and the command to run by hand;
  a failed resume also gives the command for a new conversation. Only starts ending within their first minute
  leave the pane's last screen on disk.
- `hansa desks` shows no page address for a desk never started, points to `hansa init` for an empty list
  and gives `hansa desks --forget PATH` for a desk whose folder is gone.
- Serving without an explicit port finds a free one and remembers it.
  Hansa starts no second server when it cannot inspect the one holding the desk.
  A symbolic link to `bin/hansa` on PATH works. [Getting started](docs/getting-started.md) covers local and SSH use.
- `hansa doctor` checks tools, login and sandbox operation, with fixes, JSON output and English or Chinese.
- The installer and `hansa doctor` point Chinese readers to the Chinese getting-started guide.
- `hansa status` shows how the coordinator's last turn ended, or that its turns are not recorded.
- Under the coordinator's prompt, hansa shows the desk name, model and effort, problems, waiting decisions, work,
  24-hour cost and page address without its key, refreshed by `hansa serve`, also in `hansa up` sessions; a person's own line stays.
- The mod and `hansa doctor` require Claude Code 2.1.306 or later; older starts suggest `claude update`. `--autocompact` is passed from 2.1.290.
- [SETUP.md](SETUP.md) guides an AI agent through installing and setting up Hansa with you, one question at a time.

### Documentation

- The Chinese README links to Simplified Chinese guides for getting started, concepts,
  how a desk is run and push services. Tests keep each guide's headings and code blocks in step with English.
- The [backup instructions](docs/desk.md#backing-up-and-moving-a-desk) keep goals and decisions.

### The Codex adapter

- The optional macOS adapter runs Codex workers inside srt, translates results and live progress
  for hansa, and passes other calls to Claude. [Adapter setup and limits](contrib/codex/README.md)
  covers its own login and configuration, provider errors, login access inside its sandbox and tool-rule limits.
- The adapter now runs the model set for its provider on the page; `HANSA_CODEX_MODEL` is only a fallback.
  Existing providers with placeholder model names must now name the Codex model.
- Codex runs disable memories and external agent memory import.

## 0.4.0 — 2026-09-17

- Bellhop bound acceptance to the packet a worker actually read, with upgrade recovery.
- Added verifier preflight checks, worker health alerts, quota handling and consistent exit codes.
- Supported concurrent lanes, defaulting to one worker.
- Tightened coordinator write gates and isolated the optional provider bridge.

## 0.3.0 — 2026-09-11

- Bellhop added provider adapters, model schedules, permission guards and sandbox profiles.
- Made dispatch, acceptance and closing resumable, with machine-readable receipts and verdicts.
- Ran verification in disposable copies.
- Checked merged trees before moving the base, and added lane recovery commands.
- Added guarded service wake-ups, a principal's brief and resumable release steps.

## 0.2.0

Bellhop became a Claude Code plugin with coordinator slash commands and helpers.

- Added a stdio MCP server and a watchdog that worked while a session was open.
- Added launchd and systemd installers for resident watchdog and sentinel services.
- Session hooks showed desk status and warned about pending work without a watchdog.

## 0.1.0

Bellhop's first release established the principal, coordinator and worker roles.

- A desk template and watchdog sent tasks to fresh workers in lane worktrees.
- Receipts, sampled acceptance and sentinel checks made completed work reviewable.
- Checksums protected scripts and frozen documents; a dry-run demo used stub workers.
