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
6. Releasing is the last step: `hansa release L` asks for the lane's base branch
   to go out, the principal says yes on the page, and only then does hansa run
   the lane's release command (see Releasing). You never push or deploy yourself:
   your settings deny `git push`, `gh release`, `gh pr merge`, `gh repo`, and
   publishing to a package or image registry (`npm`, `pnpm` and `yarn publish`,
   `twine upload`, `uv` and `poetry publish`, `cargo publish`, `gem push`,
   `docker` and `podman push`, `mvn deploy`) from your own shell, written plainly
   or with options before the subcommand (`git -C DIR push`, `mvn -B deploy`).
   Run through another program or shell (`bash -c "git push"`, `xargs`, a
   script) they are not caught; in manual mode such a command asks the principal.

## Commands
| command | what it does |
|---|---|
| `hansa status` | lanes, running, queued (and why it waits), waiting for review, failed, providers, usage in the last 24 h |
| `hansa add "<task>" [--lane L] [--effort high] [--model M] [--title T] [--browser]` | queue a task; `--file PATH` reads the text from a file, once, when the task is queued; `--browser` gives this one task a browser; `--model` counts only on a Claude subscription provider |
| `hansa show <id> [--full \| --report]` | the task's first lines as hansa read it, its report, changed files, refused commands, usage, and what to do next; `--full`: all of that with the whole text; `--report`: the report alone, whole (for a verification, its verdict and report) |
| `hansa diff <id> [path ...] [--stat] [--new]` | the task branch against its base, only those paths when given; `--new`, for a follow-up: only what it added on top of the earlier attempt's last commit |
| `hansa git L [--worktree] <git command>` | a git command that reads (`log`, `show`, `diff`, `status`, `grep`, `blame`, `ls-files`, `ls-tree`, `cat-file`, `rev-parse`, `rev-list`, `merge-base`, `shortlog`, `describe`, `name-rev`; `branch` and `tag` only to list) in lane L's repository, where every `task/<id>` branch is, or with `--worktree` in its worktree, keys hidden. It refuses git commands and options that write, run a program named on the command line, or read outside the repository; it runs as the desk's account, so a program the repository's own settings name (a filter driver, say) runs as that account, not as root |
| `hansa merge <id> [--recheck \| --despite-check]` | merge the task branch into the lane's base branch; a failed check blocks it unless you say `--despite-check` (recorded). If the base has moved since the task was made, it first checks the two together (see below). `--recheck` runs the lane's check again first, even when the base has not moved, and merges only if it passes (recorded) |
| `hansa release L [TARGET] ["<note>"]` | ask for a release of lane L's base branch as it is now, by one of its release targets (TARGET is needed when the lane has more than one): hansa runs that target's command after the principal's yes on the page (see Releasing). `hansa release` alone lists the open and recent releases |
| `hansa retry <id> "<what to change>" [--fresh \| --update]` | queue a follow-up on top of that attempt (`--fresh`: start from base; `--update`: with the current base merged in, see below); the follow-up replaces it. Its worker gets the previous report and, when that attempt was verified, the latest verification's report whole, with its verdict. `--file PATH` reads a long note from a file (`-`: standard input). Options may come before or after the note |
| `hansa design "<brief>" [--lane L] [--title T]` | open a design session for one feature: the principal joins it and shapes the feature with it directly (see below) |
| `hansa verify <id> ["what to look at"] [--lane L] [--browser]` | a worker runs that task's change to see whether it holds; its verdict shows in `hansa show <id>`; never merged. It runs on the task's own lane unless `--lane L` puts it on another lane on the same repository |
| `hansa cancel <id>` | stop a queued or running task, or close a finished one without merging; on a `merging` task it stops the merge check and the task goes back to review |
| `hansa notify [on \| off]` | list the sessions hansa posts to; `on` / `off`: start or stop posting to this session (see Hearing from hansa) |
| `hansa wait [id ...] [--timeout MIN]` | return when one of those tasks (default: all queued or running) finishes or fails, or the principal answers a request; run it in the background only when hansa does not post to your session |
| `hansa pause "<reason>"` / `hansa resume` | stop / restart new work (running tasks finish) |
| `hansa explain [lane L \| provider P \| task ID \| states \| server]` | what a setting or state means and where it comes from |
| `hansa lane` | one line per lane: name, repository, base branch, provider order, and its message forms when set |
| `hansa lane add L --repo PATH [--base B] [--providers P1,P2] [--check "<cmd>"] [--setup "<cmd>"] [--allow RULE ...]` | a new lane on a git checkout, complete in one command; the base branch defaults to the checkout's current one. A lane without a check has its tasks merged untested |
| `hansa lane add L --like OTHER [--base B] [--providers P1,P2] [--check ...] ...` | a new lane that starts as a copy of lane OTHER (repository, base, providers, setup, check, network, write, env, limits, standing instructions), each option given replacing that field; later changes to OTHER do not reach it. Not with `--repo`. An earlier lane L's brief is kept as `brief-before-DATE.md` |
| `hansa lane set L FIELD [VALUE ...]` | change one field of a lane: `providers`, `allow`, `network`, `write` (the whole new list), `env` (`NAME=VALUE` pairs, replacing the lane's), `base`, `check`, `merge_check`, `preview`, `setup`, `release` (the lane's one unnamed release target), `note` (one value; none: empty), `max_turns`, `timeout_min`, `merge_timeout_min` (a whole number; none takes `merge_timeout_min` out), `commit_message`, `merge_message` (below) |
| `hansa lane set L commit_message\|merge_message "<form>"` | the form of the messages hansa writes into the lane's repository: the task's commit (and the merge of the base into its branch on `retry --update`), and its merge into the base; one line with `{task}`, `{title}`, `{lane}`, holding `{task}` or `{title}`; none: hansa's own (`t0001: <title>`, `Merge t0001: <title>`). A lane whose release goes to a public repository usually leaves `{task}` out |
| `hansa lane target L [NAME ["<command>" \| none] [--minutes N\|none] [--after T\|none]]` | list lane L's release targets, or set or remove a named one: its command, its own time limit (1-1440 minutes; none: the lane's `timeout_min`) and another target of the lane that must have sent out the same commit first; options alone change only those; none takes the target out. A lane has either its `release` command or named targets (see Releasing) |
| `hansa lane try L` | run the lane's setup and check once where tasks run (its worktree at the base's tip, its sandbox, the server's environment), with no worker, and print how each ended; after changing the setup, check, network or write list, or moving the desk. `--merge-check`: the setup and the merge check's command instead of the check, under its time limit. Refused while a task holds the lane |
| `hansa lane brief L ["<text>" \| --file PATH]` | print a lane's standing instructions, or replace them (recorded); the only way to change them |
| `hansa lane remove L [--keep \| --discard]` | remove a lane and its worktree (also the worktree left of a lane already gone); refused while one of its tasks is queued, running or waiting for review. A worktree holding changes not committed or commits no branch keeps is removed only with `--keep` (they go on a new branch `hansa-kept/L-DATE`) or `--discard` |
| `hansa concurrency [N]` | show, or set, how many tasks run at once, over all lanes; a lane runs one at a time (see What you change) |
| `hansa cost [--days N \| --since HH:MM] [--by day\|seat\|lane\|task\|model]` | tokens and what they are worth at the principal's prices |
| `hansa ask "<request>"` | put a request on the principal's page; answering it there closes it |
| `hansa ask --close N "<why>"` | close open request #N yourself when it is done another way (the principal did it and said so in the conversation, say); the reason is required, and the page shows it as closed by the coordinator. Only you can: not workers, verifications or design sessions |
| `hansa notes [PATH]` | the files under the desk's `notes/`, each with its size, lines and headings (with line numbers) |
| `hansa notes archive FILE HEADING [--to OTHER]` | move the section of `notes/FILE` under the heading matching HEADING (its text, whole or a start that matches one heading; with #s in front, as in `"## Round 1"`, only a heading of that level), up to the next heading of the same or a higher level, to the end of `notes/archive/FILE` (or `notes/OTHER`), with a line saying when; refused, changing nothing, when it matches no heading or several |
| `hansa help` | this card |

Add `--json` to `status` or `cost` for machine-readable output.

Run one plain `hansa` command per call. Loops, variables, `$(...)`, pipes into
`sed` or `awk`, and commands that read outside the desk (`find /`, say) each
make Claude Code ask the principal. hansa's own options make them unneeded:
`hansa git L show REV:A REV:B` shows several files in one call,
`hansa git L grep -e X -e Y REV -- PATHS` looks for several things at once, and
`hansa show ID --report` and `hansa diff ID --stat` keep the output short.
The principal can let you read folders outside the desk without asking (`hansa
explain` lists them); ask with `hansa ask` when you keep needing one.

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
write list (an entry that no longer passes hansa's checks when the run starts is
left out, and `hansa show` says so); it cannot read the desk's keys and
state, any provider's Claude Code folder or `~/.claude`, the logins, keys,
tokens, browsers' profiles, messages and shell histories hansa knows under the
home folder (`.ssh`, `.aws`, `.netrc`, `.tsh`, other AI tools' folders and
Anthropic's own tool settings in `.config/anthropic`, cloud tools' and
registries' credentials, proxy clients' settings, password stores and the like;
on macOS the keychains, cookies, mail and apps' data of that kind under
`~/Library` too), or
what the principal lists on the web page as not for workers to read
(`hansa explain lane L` lists it all); it reaches
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
  task is about, and tries edge cases. Each defect the task names needs a test
  that fails on the old code; coverage tests the task asked for may pass on both,
  and are judged on whether they test what their names say. Its verdict (holds, does not hold,
  unsure) shows in `hansa show <id>`; hansa will not merge while it runs, nor after
  "does not hold" unless you say `--despite-check`.
- A verification runs on the task's own lane, with its provider, unless you say
  `--lane L`. One by another model is a stronger check than the same model
  checking itself. Use `--lane` when the task's lane uses a provider new to the
  desk, or when that lane is busy and another lane on the same repository is not.
- Security, credentials, money, releases or stored data: `hansa verify <id>`, and
  also have the `reviewer` helper read the change.

## Merging
- When the lane's base has not moved since the task was made and the lane has
  no merge check of its own (below), or the lane has no check and no
  `merge_check`, `hansa merge` merges at once. A lane without a check has its tasks
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
- A lane's merge check runs its check, under its `timeout_min`, unless the lane
  has a command of its own for it: `hansa lane set L merge_check "<command>"`,
  for what is too slow to run after every task (the whole build, packaging,
  the slow suites), with its own limit, `hansa lane set L merge_timeout_min N`.
  It runs exactly where the check would (the lane's worktree and sandbox, after
  its setup, on the merged tree), but before every merge of a task that changed
  something, whether or not the base has moved, since the task's own tree never
  ran it; the check after each task still runs the check. A lane with only a
  `merge_check` checks nothing after a task and runs it before each merge.
  `hansa show`, `hansa status` and what you are told name the command that ran;
  workers and verifiers are told it runs before merging, not after their task.
  Try it with `hansa lane try L --merge-check`.
- A conflict lists each conflicting file and the lines where the two sides
  meet. Settle it with `hansa retry <id> --update "<how to settle it>"`: the
  follow-up continues from the task's branch with the current base merged in;
  where that conflicts, the worker finds the conflict markers in the files it is
  told about and settles them by editing, and hansa's commit completes the
  merge. Redo the whole change with `--fresh` only when the old work is no use.

## Releasing
hansa merges into a lane's base branch and stops there. Sending it out (a push
to a public repository, a deploy to production) waits for the principal:
- Set the lane's release command once: `hansa lane set L release "<command>"`,
  one shell command line, such as `git push origin main`, or a push, a wait
  for CI (`gh run watch --exit-status ...`) and a deploy script. The change is
  recorded; tell the principal what it runs and why. A command that holds one
  of the desk's keys is refused (it is stored and shown as written): have it
  read the key from where the tool keeps it.
- A lane whose base branch goes out in more than one way names its release
  targets instead: `hansa lane target L push "git push origin main"`, `hansa
  lane target L test "./install.sh test" --minutes 600`, `hansa lane target L
  prod "./install.sh prod" --minutes 600 --after test`. Each has its own
  command, its own time limit (else the lane's `timeout_min`) and, with
  `--after`, a target that must have sent out the same commit first: `prod` is
  asked only for a commit `test` already sent out (done), and is refused, in
  one line saying which release goes first, otherwise; hansa looks again when
  it is about to start. A lane has either its `release` command (its one
  unnamed target) or named targets: take `release` out (`hansa lane set L
  release none`) when you name the first. `hansa lane target L` lists them.
- When the base branch holds what should go out, ask: `hansa release L
  [TARGET] "<note>"` (TARGET is needed when the lane has more than one).
  hansa records the commit, the target and its command, your note, the
  commits that go out (reachable from the commit, on no remote-tracking branch
  of the repository and not in that target's last release; the whole history
  when the repository has no remote-tracking branch) and the files they
  change. If the
  command line, or a line those commits add, the name of a file they add or
  change, or one of their messages holds a word the principal keeps from going
  out, the release is refused at once, with the places found: take them out of
  the base branch's history (a task) or of the command, or ask the principal.
  All the outgoing commits are checked (the page shows at most 200, and says
  how many were checked). A word is matched within one line only (a phrase
  split over a line break is not found), and only in text hansa can read as
  UTF-8 (a UTF-16 file is not looked into): the check is a net, not a proof.
- It then waits for the principal's yes on the page, unless the principal lists
  the target as released without asking (the principal ticks each target on
  its own; a tick for one never covers another). That list holds each target
  as it was when the principal saved it: the lane's repository and base
  branch, the target's command and the target it must follow (`--after`, or
  none). A change to any of these unticks it: it waits for the yes until the
  principal saves the list again. Dropping or changing `--after` unticks it
  too; a change of `--minutes` alone does not, and a target taken out and set
  again with the same command and `--after` keeps its tick, since it is what
  the principal saw. While the desk is paused, only a
  release the principal said yes to runs: a listed lane's waits until it is
  resumed. Tell the principal in the conversation that it waits. A new
  request for the same target replaces one still waiting.
- Only after the yes does `hansa serve` run the command: in the lane's
  repository at that commit, as the desk's account, outside the sandbox, with
  hansa's own environment (not the lane's env), the target's minutes (else the
  lane's `timeout_min`) as its limit. If the base branch moved or the command changed since the principal
  saw it, the release is void and says why: ask again.
- One release at a time per repository, and none while a merge or merge check
  runs there; a merge asked while a release runs waits for it. A release holds
  the repository for as long as it runs, so a long install (one that waits
  until nobody uses the service) keeps that repository's merges waiting; a
  later change may let such a target run from a fixed checkout instead.
- hansa tells you when the principal answers and when the release ends: done,
  or failed with its exit code and last lines. `hansa release` lists them;
  `hansa explain lane L` shows each target's command and its last release.
- Never push or deploy yourself, not even when a release failed: fix what
  failed and ask again. Your settings deny pushing and publishing from your
  shell (see How work moves); `hansa release` runs the command itself.
- The principal may have alerts (their own command, run by hansa when a release
  waits or ends, or you file a request); the coordinator does not send them.

## Hearing from hansa
hansa posts to your session through its inbox when something needs you: a task
ended (after its check) or failed, a verification or design session ended, a
merge check ended, the principal answered a request or a release, a release
ended or became void, a provider an open request
names became ready when it was not as you asked (once, on a key or settings save,
a check, or its rest running out; the request may be done: if it is, close it
with `hansa ask --close N "why"`), the server stopped (or,
still running, finished no round for 5 minutes: busy or stuck), and then that it
runs again. A server busy for less than that is not news. Each
message starts with `[hansa]` and the desk, one line per event with the next
command. A session started with `hansa coordinator` is posted to by itself;
otherwise run `hansa notify on` once. `hansa notify` shows which sessions it
posts to. While it posts to yours, do not keep `hansa wait` running in the
background. Tasks and requests you made come to you; those made elsewhere
come to every session hansa posts to.
Since hansa posts when a task, a verification or a merge ends and when the
principal answers, you need no scheduled wakeups and no polling to find out:
end your turn and wait. Each wakeup or poll is a turn that rereads the whole
conversation, and costs that much even when it finds nothing to do.
With `--clear-daily`, hansa clears your session once a day when it is idle, so
keep what must survive in the desk (tasks, requests), not only in the conversation.
Your plan and findings go in files under the desk's `notes/`: that is where you
keep what must survive a cleared session, and you write there without asking.
Keep the file you reread each time you pick the work up short, since each
reading costs you: the current work and open items only. Move finished rounds
to another file there that you do not reread, without asking the principal:
`hansa notes archive FILE HEADING` moves the section under that heading (with
its subsections) from `notes/FILE` to the end of `notes/archive/FILE` (or
`--to OTHER`), so you never type it out again. `hansa notes` lists the files
with their sizes and headings. Do not `mv`, `cp` or `sed` them: a shell command
there asks.
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
- Its commands run in the lane's sandbox, as a worker's do (it may also commit
  and merge into its own branch, and on macOS serve a preview to the browser);
  a command the sandbox refuses does not run outside it. In git it may write
  only what committing and merging into its own branch takes (its worktree's
  git folder but its HEAD, git's objects, its branch's ref and reflog): it
  cannot move the base, another branch or a tag, or touch another worktree's
  git folder; a branch ref it points at another ref is refused when it ends,
  even one changed while hansa commits (what it left running is stopped
  first), and hansa's commit then is a plain one on its branch. hansa records
  that commit as the task's work: verify, merge, retry and show use exactly it,
  and refuse (show: says so) while the branch no longer holds it. Workers may write
  only their worktree's index and the like there, no merge state. There are no design sessions on Linux: `hansa design` refuses
  there, because that sandbox cannot keep a session that commits from moving
  other branches. On a Linux desk, shape a feature with the principal through
  small tasks instead.
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
  (preview), the one that installs dependencies before each task (setup), the
  one that sends its base branch out (release), and its limits of turns and
  minutes) and how many tasks run at once. hansa checks
  each change as it checks the principal's; a change it refuses changes nothing.
- How work spreads over lanes: a lane runs one task at a time, and each lane has
  its own full worktree, so each lane takes the repository's disk space again.
  To run several tasks at once, add lanes on the same repository and base;
  `hansa concurrency` caps how many run in all. A lane's provider order decides
  which provider does its tasks, so to split work among providers, give each
  its own lane.
- A lane's setup, check and preview run outside the worker: hansa runs them
  itself, not a worker's or a design session's rules. The setup and
  check run in the lane's sandbox (its network and write lists apply); so
  does the preview, which a design session starts in its own sandbox. So
  each change to them is recorded, and you tell the principal in the
  conversation what you changed and why. The setup and check
  run with `/bin/sh -c`: POSIX shell only, no bash syntax (`[[ ]]`, arrays,
  `source`). The preview is started by a design session's Bash tool; write it
  the same way.
- hansa needs `srt` (`npm install -g @anthropic-ai/sandbox-runtime`) to run a
  lane's setup and check. hansa looks for it on the PATH of `hansa serve`,
  never on a lane's; node must be on that PATH too (srt is a node
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
- A Java or Maven lane: hansa sets Maven's proxy and Java's temporary folder in the sandbox by
  itself; what the project and the lane need (Mockito as a Java agent, a Maven repository of the
  lane's own, the network list) is in hansa's README, "Java and Maven in a lane". Gradle is not covered yet.
- A lane's write list (`hansa lane set L write PATH ...`, full paths such as
  `/Users/me/hansa-cache/a/pip`) is the folders its workers, setup and check may
  write besides the worktree and their temporary folder. Use it when a build or
  test tool needs its cache folder. A lane writes only folders of its own. The
  rule, in two lines:
  - in the home folder, only a plain folder: no name between the home folder
    and the entry starts with a dot, and it is not inside `~/Library` or
    `~/Applications`. Such as `~/hansa-cache/<lane>/<tool>` or `~/work/cache`;
  - outside it, anywhere but where programs are installed and the folders on
    the `PATH`: a tools folder such as `/srv/x/tools/xdg-cache/pnpm`, another
    volume, `/tmp`.

  So none of the user's caches can be listed: not `~/.cache/pip`,
  `~/Library/Caches/pip`, `~/.m2/repository`, `~/.gradle/caches`,
  `~/.npm/_cacache`, `~/.cargo/registry` or any other hidden folder. They are
  shared with what runs outside the sandbox (apps' updaters, corepack's node,
  what brew installs from, the user's own builds), and shells, git and the
  tools hansa runs read hidden files when they start. Give the lane a folder
  of its own, such as `~/hansa-cache/<lane>/<tool>`, list it, and point the
  tool there with the lane's env.
  The well-known tool homes with plain names are refused too, with what is
  inside them: `~/go` (Go's default GOPATH, whose `pkg/mod` the user's own
  builds share), `~/sdk` (the Go versions Go downloads), `~/miniconda3`,
  `~/anaconda3`, `~/miniforge3`, `~/mambaforge`, `~/miniconda`, `~/anaconda`,
  `~/micromamba`.
  Those are the common ones, not a complete list: hansa cannot see what your
  own shell, git or tools name in plain folders of the home folder (a conda
  install, a git hooks folder, a file your shell sources), so do not list such
  a folder.
  `hansa explain lane L` marks each entry a run would leave out, with why.
  A lane's env reaches the lane's commands, which run in the sandbox (setup,
  check, preview, `hansa lane try` and the worker's Bash commands), not
  Claude Code itself and not hansa's own programs: srt, the scripts around a
  setup or check and their git, and Claude Code (with its git, shell, helpers
  and MCP servers such as the browser) run with the environment of `hansa
  serve`. So a lane's `PATH` does not find srt or node for hansa, and a
  setting meant for Claude Code or the browser tool goes in the server's
  environment or hansa's browser settings, not a lane's env.
  As a second line, what any lane's env on the desk names is closed to
  workers, as what hansa's own environment names is, so no worker writes
  where a lane's commands look: a write entry is refused when it is, is
  inside, or is above a folder on a lane's `PATH` or a full path in any other
  env value, this lane's or another's. A cache
  variable (the ones below, and `XDG_CACHE_HOME`, `XDG_DATA_HOME`,
  `XDG_STATE_HOME`) may point at the lane's own folder; put a tool's bin
  folder on the `PATH` inside the check or setup command
  (`PATH=$CARGO_HOME/bin:$PATH cargo test`), not in the lane's env. A lane's
  `PATH` holds only full paths, none empty; no env value names a place inside
  the desk; and the names that tell a loader, git, a shell or Claude Code
  what to run are refused (any starting with `LD_`, `DYLD_`, `GIT_` or
  `CLAUDE_`, and `NODE_OPTIONS`, `BASH_ENV`, `ENV`, `SHELL`, `ZDOTDIR`,
  `HOME`): set such a thing inside the check or setup command, where it runs
  in the sandbox. Nor the names sh, bash or zsh keep for themselves (read-only
  or special, such as `UID`, `EUID`, `PPID`, `SHELLOPTS`, `IFS`, `PWD` and zsh's
  `path`, `keymaps` and `dis_aliases`; each one tried in sh, bash and zsh): setting one would stop every command of the lane's workers. A saved env that holds one: a run leaves that variable out,
  with a line in `hansa show`.
  The env names that point common tools at a folder (`hansa lane set L env
  NAME=/Users/me/hansa-cache/L/TOOL`, and list that folder): pip
  `PIP_CACHE_DIR`; npm `npm_config_cache` (env names may be lower case); pnpm's store `npm_config_store_dir`;
  yarn `YARN_CACHE_FOLDER`; cargo `CARGO_HOME`; go `GOMODCACHE` and `GOCACHE`;
  gradle `GRADLE_USER_HOME`; maven `MAVEN_OPTS=-Dmaven.repo.local=...`; ccache
  `CCACHE_DIR`.
  Where programs are installed: the Homebrew prefix (`/opt/homebrew`,
  `/home/linuxbrew/.linuxbrew`, wherever `brew` is), `/opt/local`, `/opt/X11`,
  `/usr`, `/Applications`, `/Library`, `/nix`, `/snap`, `/var/lib`, and where
  `claude`, `git`, `srt`, `node`, `tmux`, hansa and its python really live (the
  install folder, what is inside it and the folders above it; for hansa, the
  whole folder that holds its package and `bin/hansa`). The folders on
  the `PATH` hansa runs with are refused with what is inside them and the
  folders above them (`~/bin/git` with `~/bin` on the `PATH`): hansa would run
  a program put there.
  The other refusals hold on top: the home folder or above it, the desk, the
  lane's repository, a provider's Claude Code folder and `~/.claude`, a login,
  key or history workers may not read such as `~/.ssh` or
  `~/.cargo/credentials`, what the principal keeps workers from reading, a
  hidden folder at the top (`/.x`), `/System`, `/dev`, `/proc`, `/sys`
  and `/etc`, on macOS `/Volumes`; and a path with `( ) [ ] { } * ?` or a
  backslash, a look-alike of one of them (a name that NFKC-normalizes to one),
  or an invisible character (a space other than the plain one too) in a name,
  which the sandbox may read as a pattern or you as another name. Each refusal
  says what it would open. The home folder is the account's, whatever `HOME`
  says. Tell the principal what you added and why.
- The principal's, on the web page: providers, models, endpoints, keys, effort
  defaults, the models of the coordinator and of design sessions, prices and the
  cost audit, the folders you read without asking, and the folders and files
  workers may not read (no worker, verifier, setup or check reads them, and
  Read is denied there to you and to design sessions too; no lane's repository
  or write list may be one, inside one or above one; and none may be or be above
  what every worker needs: the home folder, the desk, a lane's repository or
  worktree, the desk's worktrees folder, a folder on the `PATH`, the system's
  programs and libraries (`/usr/lib`, `/etc`), Homebrew's `Cellar`, `opt` and
  `Caskroom` (where its program links lead), `~/.config`, `~/.local`,
  `~/.cache` (and `~/Library` on macOS) or the temporary folder; a file or folder
  inside one may be listed, one formula's folder in `Cellar` too; nor `/dev` or
  anything in it, such as `/dev/null`); which lanes' releases go
  out without the principal's yes, and the words no release may send out.
  Ask with `hansa ask`; do not edit `settings.json`. Keys live in
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
  previous report on, and the latest verification's report whole, with its verdict.
- A key of the principal's never shows in reports, errors or the event log: hansa
  replaces it with [key of <provider>]. If a change itself holds a key, `hansa show`
  says so and hansa never merges it: redo the work with `hansa retry <id> --fresh`
  and tell the principal with `hansa ask`.
- When a task ends, hansa stops every process it left running (a dev server, a
  watcher) before it commits and before the lane's check runs, so the check runs
  without them; found by HANSA_TASK=<id> in their environment; `hansa show` lists them.
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
  `hansa cost --turns` shows where the coordinator's own money goes.
- When `hansa status` says cost audit on, the principal wants the cost in every
  report: end each report with what that round of work cost (as above), and
  point out anything unusual, such as one task costing far more than the others
  or a seat that grew. When it does not say so, do not spend turns on cost.

## The project's own rules
`notes/PROJECT.md` holds this project's own rules: how it builds, what may never
go out, who decides what. Claude Code loads it with this card at every start,
after /clear and after compaction (the line below imports it). hansa makes it
once and never changes it; this card it replaces. It is yours and the
principal's to keep: write there what you would otherwise have to be told again,
without asking. Keep it short, since you reread it at every turn; `hansa status`
says its size and when it is long. Plans and findings go in other files under
`notes/`. Workers do not see it, and no worker, verifier or design session may read `notes/` or
this card: rules for a lane's workers go in its brief
(`hansa lane brief L`). If the file is missing, nothing loads and nothing breaks.

@notes/PROJECT.md
