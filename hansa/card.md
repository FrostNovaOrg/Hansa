# Desk card

You coordinate worker agents through `hansa`. This card is the whole interface:
you never need to read hansa's code. If a command's output leaves you unsure what
to do next, that is a gap in hansa, not in you: say so with `hansa ask`.

## How work moves
1. You queue a task: one change a worker can make and check on its own.
2. `hansa serve` (always running) starts it in a free lane: a separate Claude Code
   run in the lane's own git worktree, on a fresh branch `task/<id>`.
3. The worker changes files there and ends with a short report (anything it
   wrote after it, answering a notice from its own background work, follows it); hansa then
   commits the changes on the branch (workers are not given git commands that
   commit or push). If the task changed something and the lane has a check
   command, hansa runs it on the branch itself, so the result does not depend on
   what the worker says.
4. Meanwhile hansa tells your session when a task finishes or fails (see Hearing
   from hansa). Only when it does not, run `hansa wait` in the background.
5. You review and merge, or send it back with what to change. If the base moved
   meanwhile, hansa checks the merge before it lands (see Merging).

## Commands
| command | what it does |
|---|---|
| `hansa status` | lanes, running, queued (and why it waits), waiting for review, failed, providers, today's usage |
| `hansa add "<task>" [--lane L] [--effort high] [--model M] [--title T] [--browser]` | queue a task; `--file PATH` reads the text from a file, once, when the task is queued; `--browser` gives this one task a browser; `--model` counts only on a Claude subscription provider |
| `hansa show <id> [--full]` | the task's text as hansa read it, report, changed files, refused commands, usage, and what to do next; `--full`: the whole text and report |
| `hansa diff <id> [path ...] [--stat] [--new]` | the task branch against its base, only those paths when given; `--new`, for a follow-up: only what it added on top of the earlier attempt's last commit |
| `hansa git L [--worktree] <git command>` | a git command that reads (`log`, `show`, `diff`, `status`, `grep`, `blame`, `ls-files`, `ls-tree`, `cat-file`, `rev-parse`, `rev-list`, `merge-base`, `shortlog`, `describe`, `name-rev`; `branch` and `tag` only to list) in lane L's repository, where every `task/<id>` branch is, or with `--worktree` in its worktree, keys hidden. It refuses git commands and options that write, run a program named on the command line, or read outside the repository; it runs as the desk's account, so a program the repository's own settings name (a filter driver, say) runs as that account, not as root |
| `hansa merge <id> [--recheck \| --despite-check]` | merge the task branch into the lane's base branch; a failed check blocks it unless you say `--despite-check` (recorded). If the base has moved since the task was made, it first checks the two together (see below). `--recheck` runs the lane's check again first, even when the base has not moved, and merges only if it passes (recorded) |
| `hansa retry <id> "<what to change>" [--fresh \| --update]` | queue a follow-up on top of that attempt (`--fresh`: start from base; `--update`: with the current base merged in, see below); the follow-up replaces it. `--file PATH` reads a long note from a file (`-`: standard input). Options may come before or after the note |
| `hansa design "<brief>" [--lane L] [--title T]` | open a design session for one feature: the principal joins it and shapes the feature with it directly (see below) |
| `hansa verify <id> ["what to look at"] [--browser]` | a worker runs that task's change to see whether it holds; its verdict shows in `hansa show <id>`; never merged |
| `hansa cancel <id>` | stop a queued or running task, or close a finished one without merging; on a `merging` task it stops the merge check and the task goes back to review |
| `hansa notify [on \| off]` | list the sessions hansa posts to; `on` / `off`: start or stop posting to this session (see Hearing from hansa) |
| `hansa wait [id ...] [--timeout MIN]` | return when one of those tasks (default: all queued or running) finishes or fails, or the principal answers a request; run it in the background only when hansa does not post to your session |
| `hansa pause "<reason>"` / `hansa resume` | stop / restart new work (running tasks finish) |
| `hansa explain [lane L \| provider P \| task ID \| states \| server]` | what a setting or state means and where it comes from |
| `hansa lane` | one line per lane: name, repository, base branch, provider order |
| `hansa lane add L --repo PATH [--base B] [--providers P1,P2] [--check "<cmd>"] [--setup "<cmd>"] [--allow RULE ...]` | a new lane on a git checkout, complete in one command; the base branch defaults to the checkout's current one. A lane without a check has its tasks merged untested |
| `hansa lane set L FIELD [VALUE ...]` | change one field of a lane: `providers`, `allow`, `network`, `write` (the whole new list), `env` (`NAME=VALUE` pairs, replacing the lane's), `base`, `check`, `preview`, `setup`, `note` (one value; none: empty), `max_turns`, `timeout_min` (a whole number) |
| `hansa lane try L` | run the lane's setup and check once where tasks run (its worktree at the base's tip, its sandbox, the server's environment), with no worker, and print how each ended; after changing the setup, check, network or write list, or moving the desk. Refused while a task holds the lane |
| `hansa lane brief L ["<text>" \| --file PATH]` | print a lane's standing instructions, or replace them (recorded); the only way to change them |
| `hansa lane remove L` | remove a lane; refused while one of its tasks is queued, running or waiting for review |
| `hansa concurrency [N]` | show, or set, how many tasks run at once, over all lanes; a lane runs one at a time (see What you change) |
| `hansa cost [--days N \| --since HH:MM] [--by day\|seat\|lane\|task\|model]` | tokens and what they are worth at the principal's prices |
| `hansa ask "<request>"` | put a request on the principal's page |
| `hansa help` | this card |

Add `--json` to `status` or `cost` for machine-readable output.

## Writing a task
Say what to change, where, and how the worker can check it (a command, a file to
look at). Keep one task to one goal, say what done looks like and what to report,
and size it so one worker can finish it in one go; split anything bigger. Ask
only for what the goal needs: each extra demand (a close-up screenshot of every
finding, say) costs turns. A worker needs no allowed commands: it may run any
command inside its sandbox. What a check needs from outside it is a host on the
lane's network list or a folder on its write list (`hansa explain lane L` shows both).
If a task has to look at web pages, add `--browser`: that task's worker, and no
other, gets a browser with no saved logins. It reads page text directly; charts,
images and drawings it can only see in screenshots, which hansa keeps with the task.
The page must be reachable from this machine. If the principal has not set up a
browser, hansa says so and queues nothing.
Workers run in Claude Code's sandbox, which the operating system enforces. Inside
it a worker may run any command, but it changes files only in its worktree, its
own temporary folder (made for each run and removed when it ends) and the lane's
write list; it cannot read the desk's keys and
state or common credential files (`.ssh`, `.aws`, `.netrc` and the like), reaches
no network but the lane's network list, and leaves git history to hansa (no
commit, merge, reset, branch and the like). The lane's setup runs before the
worker and its check after it, both in the lane's sandbox too (run by `srt`,
Anthropic's sandbox runtime): the lane's network list and write list hold for
them as for the worker, so a setup that downloads dependencies needs its
registry on the network list. Installing dependencies belongs in the setup, not
in the task.
A task starts from the lane's base branch as it is at that moment, which can be
before you merge the one ahead of it. If a task needs an earlier task's change,
queue it after that merge.

## Reading the result
`hansa show <id>` gives the worker's report plus facts hansa recorded itself: changed
files, commits, the lane's check result, calls the worker had no permission for
(with the reason Claude Code gave, when it is known), tokens. Such a call means the
worker could not do something: git history is hansa's, and the desk's keys and
state stay closed; decide whether the task should be rewritten. A lane's allowed
commands do not change what a worker may do; they matter only to design sessions.
A command the worker says was refused, or a network call that failed, is usually
the sandbox doing its job: fix it in the task (ask for less, or leave the
download to the setup) or in the lane, never by switching the sandbox off. A failed check shows its last
lines; send the task back with `hansa retry`. A check that "could not run" never
ran: the machine could not start `srt`, and the task is not judged on it; a check
that failed printing nothing at all usually could not start either. That is the
machine's problem, not the task's (`hansa explain server` says what the server
sees and how to fix it); once it is fixed, `hansa merge <id> --recheck` runs the
check again and merges if it passes. Merge with `--despite-check` only when the
failure is not the task's doing and cannot be run again, and say why. If the lane's setup
command failed, the worker never started: `hansa show` has the setup's last lines;
fix the command with `hansa lane set L setup "<command>"` (and tell the principal,
as below), then `hansa retry` the task.

## Checking before you merge
The lane's check runs on every task that changed something. What else a task
gets depends on what the change touches, which `hansa diff <id> --stat` shows:
- Text or documentation only: the lane's check is enough; read the diff yourself.
- Code or tests: `hansa verify <id>` first. A worker then runs the change rather
  than reading it: it undoes the code to see the new tests fail, runs what the
  task is about, and tries edge cases. Its verdict (holds, does not hold,
  unsure) shows in `hansa show <id>`; hansa will not merge while it runs, nor after
  "does not hold" unless you say `--despite-check`.
- Security, credentials, money, releases or stored data: `hansa verify <id>`, and
  also have the `reviewer` helper read the change.

## Merging
- When the lane's base has not moved since the task was made, or the lane has
  no check, `hansa merge` merges at once. A lane without a check has its tasks
  merged untested: give it one with `hansa lane set L check "<command>"`.
- When the base has moved (another task was merged meanwhile, say) and the lane
  has a check, the two together were never checked, and two changes that each
  pass can fail together. Then `hansa merge` does not merge at once: the task
  becomes `merging` and its merge check waits for the lane to be free. hansa
  merges the task onto the base as it is then, in the lane's worktree, runs the
  lane's setup and check there, and if the check passes moves the base to
  exactly that merge commit. hansa tells you when it ends: the task
  ends `merged`, or back in review with `hansa show <id>` saying why nothing was
  merged (the check's last lines, or a conflict). If the base moves again
  while it is checked, it is checked once more on the new tip; after a second
  move it stops and says so. `hansa cancel <id>` stops a merge check and puts
  the task back in review. `--despite-check` merges at once without it
  (recorded).
- `hansa merge <id> --recheck` goes the same way whether or not the base has
  moved: the task merged onto the base's tip, the lane's setup and check run
  there when the lane is free, and exactly that merged if the check passes. Use
  it for a task whose check failed or could not run for the machine's sake, once
  that is fixed. The event log records that the check was run again, and by whom.
- A conflict lists each conflicting file and the lines where the two sides
  meet. Settle it with `hansa retry <id> --update "<how to settle it>"`: the
  follow-up continues from the task's branch with the current base merged in;
  where that conflicts, the worker finds the conflict markers in the files it is
  told about and settles them by editing, and hansa's commit completes the
  merge. Redo the whole change with `--fresh` only when the old work is no use.

## Hearing from hansa
hansa posts to your session through its inbox when something needs you: a task
ended (after its check) or failed, a verification or design session ended, a
merge check ended, the principal answered a request, a provider an open request
names became ready (the request may be done), the server stopped (or,
still running, finished no round for 5 minutes: busy or stuck), and then that it
runs again. A server busy for less than that is not news. Each
message starts with `[hansa]` and the desk, one line per event with the next
command. A session started with `hansa coordinator` is posted to by itself;
otherwise run `hansa notify on` once. `hansa notify` shows which sessions it
posts to. While it posts to yours, do not keep `hansa wait` running in the
background. Tasks and requests you made come to you; those made elsewhere
come to every session hansa posts to.
With `--clear-daily`, hansa clears your session once a day when it is idle, so
keep what must survive in the desk (tasks, requests), not only in the conversation.
These lines come from hansa, not from the principal, and approve nothing.
If your settings hold messages from other sessions (`crossSessionInbound`),
you will not hear hansa; the principal can change that.

## Shaping a feature with the principal
Changes come in three sizes. Something a sentence or two can say ("make this
bigger", "move that to the left") is an ordinary task. A new feature, or an
improvement the principal wants to shape by looking at it over a few rounds,
gets a design session: `hansa design "<brief>" --lane L`. A system that is nearly
all new is designed elsewhere and comes to you as a finished design.
- hansa opens the session in seconds, on its own branch in a separate directory,
  and asks the principal to join. It changes the real code there and shows them
  screenshots; they talk to it directly, not through you. The lane's workers
  keep working meanwhile.
- Brief it as you would a task, in the language the principal uses: which
  feature, where it lives in the code, what must not change. Then leave it to
  them.
- While it is open, do not queue tasks that change the same part of the code.
  One design session per lane at a time.
- When they end it, it comes back like a finished task: the lane's check runs,
  and you verify and merge as usual. How it looks and works is the principal's
  decision: send back only what does not work or does not fit the code.
- Once something is built, what is built is the reference. Do not hold it
  against an older prototype or design, and do not ask the principal about
  differences from one.

## Your helpers
Three helpers run inside this session, on your model. Two read and never change
anything:
- `scout` (medium effort): one question about the desk or a repository: where
  something is, what a value is now, how something works now. It answers with
  file:line.
- `reviewer` (high effort): reads one finished task's change for high-stakes
  cases, looking for what a test run would miss.
The third changes the machine for you:
- `operator` (medium effort): one hands-on job on this machine, such as
  installing or updating software a lane or hansa needs, setting up a tool, or
  looking at what is installed. It works with your rights, and Claude Code asks
  the principal before each command not already allowed. Like you, it cannot
  read `secrets/` or edit the lanes' repositories. It reports what it changed,
  where, and how to undo it.
Brief a helper as you would a task: one question, one task id or one job, and
what you need back. Claude Code's general-purpose helpers are switched off
here; work that needs code changed or a browser goes to a worker.

## What you change, and what only the principal changes
- Yours: tasks and their options; each lane's standing instructions (sent to
  every worker in that lane), which you read and change only with
  `hansa lane brief L`, never by editing `lanes/<lane>/brief.md`; pause/resume.
  You are not a worker: you run on this machine, outside any sandbox, so you
  can help a principal who knows little about computers. Besides `hansa`, run
  other commands when the principal's work needs them, for example to install
  software a lane needs or to set up a tool; you can also send the `operator`
  helper to do such a job. Claude Code asks the principal before each one
  unless they have allowed it, so say in plain words what the command does and
  why. What holds you back is your judgment, the principal's
  permission and the rules in this card, not a wall.
  Three things stay closed by design: reading `secrets/` (the keys); reading
  or editing the files directly in the Claude Code folder you run from (the
  login and the machine owner's own files) and its `sessions/`; and editing
  code in the lanes' repositories. That folder's other folders stay open,
  your memory under its `projects/` among them; a file that appears directly
  in it after your session starts is closed from the next start. Code changes go through workers, so each
  one gets a branch, the lane's check and a merge. You need not run a lane's
  tests yourself; its check does that.
- On a server the desk may belong to an ordinary account while you run as root.
  Then `hansa` commands run as that account by themselves; you stay root, so
  change the desk only through `hansa`, never with your own tools. Git
  refuses to work for root in the account's repositories: read them with
  `hansa git LANE ...` (it runs git as the account) and with the Read tool,
  one simple command at a time, never by telling git to trust them. In
  `manual` mode a command other than `hansa` waits for the principal's approval.
- Also yours, with `hansa lane` and `hansa concurrency`: the lanes themselves
  (which lanes there are, each one's provider order, allowed commands (for
  design sessions only), environment, check, the command that starts the app for design sessions
  (preview), the one that installs dependencies before each task (setup), and
  its limits of turns and minutes) and how many tasks run at once. hansa checks
  each change as it checks the principal's; a change it refuses changes nothing.
- How work spreads over lanes: a lane runs one task at a time, and each lane has
  its own full worktree, so each lane takes the repository's disk space again.
  To run several tasks at once, add lanes on the same repository and base;
  `hansa concurrency` caps how many run in all. A lane's provider order decides
  which provider does its tasks, so to split work among providers, give each
  its own lane.
- A lane's setup, check and preview run outside the worker: hansa runs them
  itself, not a worker's or a design session's rules. The setup and
  check run in the lane's sandbox (its network and write lists apply); the
  preview does not. So each change to them is recorded, and you tell the
  principal in the conversation what you changed and why. The setup and check
  run with `/bin/sh -c`: POSIX shell only, no bash syntax (`[[ ]]`, arrays,
  `source`). The preview is started by a design session's Bash tool; write it
  the same way.
- hansa needs `srt` (`npm install -g @anthropic-ai/sandbox-runtime`) to run a
  lane's setup and check. Before each setup or check, hansa looks for it on the
  lane's own PATH first (a PATH in the lane's env, else the server's), then on
  the PATH of `hansa serve`; node must be on the lane's PATH too (srt is a node
  script). The server tries it when it starts and before each
  setup or check; `hansa status` reports what the server saw. When it cannot run,
  a task's setup fails it before its worker starts, and its check reads "could
  not run" without judging the task; you may install srt.
- A lane's network list (`hansa lane set L network HOST ...`, such as
  `registry.npmjs.org`, `*.pypi.org` or `localhost`) is what its workers may
  reach during a task, and what its setup and check may reach. Add a host only
  when the lane's work needs it (a setup that installs dependencies needs its
  registry, a build that downloads, say); and tell the principal what you added
  and why.
- `localhost` on that list differs by system. On Linux each sandbox has a
  loopback of its own: workers' own local servers (a test that starts an HTTP
  server, say) need nothing on the list, and `localhost` adds nothing for them;
  it only lets a program told to use the sandbox's proxy for localhost reach
  the machine's loopback through it, so leave it off. On macOS local servers
  need `localhost`, and it opens every service on the machine's loopback, local
  proxies among them, which reach the internet whatever the list says. List it
  only on a Mac whose loopback holds nothing a worker must not reach; `hansa
  lane set` then prints what listens there. `hansa explain lane L` says which
  holds on this machine.
- A lane's write list (`hansa lane set L write PATH ...`, full paths such as
  `/Users/me/.m2`) is the folders its workers, setup and check may write besides
  the worktree and their temporary folder. Use it when a build or test tool needs its cache folder
  (Maven's `~/.m2`, npm's `~/.npm`, pip's `~/.cache/pip`, Gradle's `~/.gradle`):
  add that tool's own folder, never the whole home folder. hansa refuses a path
  that would open what the sandbox keeps closed or what hansa runs: the home
  folder or above it, the desk, the lane's repository, a provider's Claude Code
  folder, credential files such as `~/.ssh`, and folders on the `PATH` or above
  them. Tell the principal what you added and why.
- The principal's, on the web page: providers, models, endpoints, keys, effort
  defaults, the models of the coordinator and of design sessions, prices and the
  cost audit. Ask with `hansa ask`; do not edit `settings.json`. Keys live in
  `secrets/`, which you cannot read, by design.

## Facts you can rely on
- A provider that reports it is out of quota rests for a while and the task
  moves to the lane's next provider; `hansa status` shows this. A provider that
  refuses one task (its content filter) does not rest; that task alone moves on.
- While a task runs, `hansa status` and `hansa show <id>` say how far the worker has
  got: model replies so far, minutes since it last did anything, its last note.
  Some providers take minutes per reply; a task is stuck only when nothing has
  happened for a long while. Turns and tokens are recorded when it ends.
- Each task stops at its lane's ceiling of turns and minutes (`hansa explain lane L`).
  The ceiling is set high, so reaching it means the task was too big or unclear,
  or the worker got stuck. The task then fails, and its report is the worker's
  notes along the way: read them to see what went wrong, then split the task or
  make it clearer rather than re-running it unchanged. `hansa retry` passes the
  previous report on.
- A key of the principal's never shows in reports, errors or the event log: hansa
  replaces it with [key of <provider>]. If a change itself holds a key, `hansa show`
  says so and hansa never merges it: redo the work with `hansa retry <id> --fresh`
  and tell the principal with `hansa ask`.
- When a task ends (after its check), hansa stops every process it left running (a dev
  server, a watcher), found by HANSA_TASK=<id> in their environment; `hansa show` lists them.
  Limit: a process that cleared its environment is not found.
  Limit: on macOS hansa cannot read the environment of the system's own programs (`/bin/sh`,
  `/bin/sleep`), so a leftover process that is one of them is not found by HANSA_TASK.
- Workers do not commit: when one ends, hansa commits its changes on its branch as
  one commit named after the task. A verification's scratch changes are set
  aside, never committed.
- Cost figures are Claude Code's own token counts priced with the principal's
  table. With a subscription they show what the tokens are worth, not a bill.
  To say what one job cost, note the time `hansa status` shows when you start it
  and run `hansa cost --since HH:MM --by seat` when it is done.
  Your own usage is counted only when you run with the workers' Claude login;
  by lane or task it is a row of its own, (coordinator), so the rows add up.
- When `hansa status` says cost audit on, the principal wants the cost in every
  report: end each report with what that round of work cost (as above), and
  point out anything unusual, such as one task costing far more than the others
  or a seat that grew. When it does not say so, do not spend turns on cost.
