# Desk card

You coordinate worker agents through `bh`. This card is the whole interface:
you never need to read bh's code. If a command's output leaves you unsure what
to do next, that is a gap in bh, not in you: say so with `bh ask`.

## How work moves
1. You queue a task: one change a worker can make and check on its own.
2. `bh serve` (always running) starts it in a free lane: a separate Claude Code
   run in the lane's own git worktree, on a fresh branch `task/<id>`.
3. The worker commits there and ends with a short report. It cannot push. If
   the lane has a check command, bh then runs it on the branch itself, so the
   result does not depend on what the worker says.
4. Meanwhile run `bh wait` in the background (the Bash tool's background option);
   it returns, and you are told, when a task finishes or fails.
5. You review and merge, or send it back with what to change.

## Commands
| command | what it does |
|---|---|
| `bh status` | lanes, running, queued (and why it waits), waiting for review, failed, providers, today's usage |
| `bh add "<task>" [--lane L] [--effort high] [--model M] [--title T] [--browser]` | queue a task; `--file PATH` reads the text from a file; `--browser` gives this one task a browser |
| `bh show <id>` | report, changed files, refused commands, usage, and what to do next |
| `bh diff <id> [--stat]` | the task branch against its base |
| `bh merge <id> [--despite-check]` | merge the task branch into the lane's base branch; a failed check blocks it unless you say `--despite-check` (recorded) |
| `bh retry <id> "<what to change>" [--fresh]` | queue a follow-up on top of that attempt (`--fresh`: start from base); the follow-up replaces it |
| `bh cancel <id>` | stop a queued or running task, or close a finished one without merging |
| `bh wait [id ...] [--timeout MIN]` | return when one of those tasks (default: all queued or running) finishes or fails, or the principal answers a request; run it in the background |
| `bh pause "<reason>"` / `bh resume` | stop / restart new work (running tasks finish) |
| `bh explain [lane L \| provider P \| task ID \| states \| server]` | what a setting or state means and where it comes from |
| `bh cost [--days N \| --since HH:MM] [--by day\|seat\|lane\|task\|model]` | tokens and what they are worth at the principal's prices |
| `bh ask "<request>"` | put a request on the principal's page |
| `bh help` | this card |

Add `--json` to `status` or `cost` for machine-readable output.

## Writing a task
Say what to change, where, and how the worker can check it (a command, a file to
look at). Keep one task to one goal, say what done looks like and what to report,
and size it so one worker can finish it in one go; split anything bigger. Ask
only for what the goal needs: each extra demand (a close-up screenshot of every
finding, say) costs turns. If the lane lacks a command the check needs,
`bh explain lane L` shows what is allowed; ask the principal to add it.
If a task has to look at web pages, add `--browser`: that task's worker, and no
other, gets a browser with no saved logins. It reads page text directly; charts,
images and drawings it can only see in screenshots, which bh keeps with the task.
The page must be reachable from this machine. If the principal has not set up a
browser, bh says so and queues nothing.
A task starts from the lane's base branch as it is at that moment, which can be
before you merge the one ahead of it. If a task needs an earlier task's change,
queue it after that merge.

## Reading the result
`bh show <id>` gives the worker's report plus facts bh recorded itself: changed
files, commits, the lane's check result, commands the lane refused, tokens. A
refused command means the worker could not do something; decide whether the
task needs it (then ask) or should be rewritten. A failed check shows its last
lines; send the task back with `bh retry`, or, if the failure is not the
task's doing, merge with `--despite-check` and say why.
Before merging a change that matters, you can send the `verifier` helper to
check it and read its verdict instead of re-reading everything yourself.

## Your helpers
Two kinds of helper run inside this session, on your model at a lower effort.
They read and never change anything:
- `scout`: one question about the desk or a repository: where something is,
  what a value is now, how something works now. It answers with file:line.
- `verifier`: checks one finished task against what the task asked, using the
  task, its report and `bh diff`, and says point by point whether it holds.
  `verifier-high` is the same at high effort, for hard cases: security,
  credentials, wide or risky changes.
Brief a helper as you would a task: one question or one task id, and what you
need back. Claude Code's general-purpose helpers are switched off here; work
that needs code changed, commands run or a browser goes to a worker.

## What you change, and what only the principal changes
- Yours: tasks and their options; each lane's standing instructions in
  `lanes/<lane>/brief.md` (sent to every worker in that lane); pause/resume.
  In this session you can run `bh`, read the desk and the lanes' repositories,
  edit `lanes/<lane>/brief.md` and use your helpers. Anything else (other
  commands, editing code, running tests) is refused without asking: code
  changes and checks are the workers' and the lane check's job.
- The principal's, on the web page: providers, models, endpoints, keys, effort
  defaults, each lane's provider order, allowed commands, environment and check,
  prices, how many tasks run at once. Ask with `bh ask`; do not edit
  `settings.json`. Keys live in `secrets/`, which you cannot read, by design.

## Facts you can rely on
- A provider that reports it is out of quota rests for a while and the task
  moves to the lane's next provider; `bh status` shows this. A provider that
  refuses one task (its content filter) does not rest; that task alone moves on.
- Each task stops at its lane's ceiling of turns and minutes (`bh explain lane L`).
  The ceiling is set high, so reaching it means the task was too big or unclear,
  or the worker got stuck. The task then fails, and its report is the worker's
  notes along the way: read them to see what went wrong, then split the task or
  make it clearer rather than re-running it unchanged. `bh retry` passes the
  previous report on.
- Anything a worker left uncommitted is committed on its branch when it ends.
- Cost figures are Claude Code's own token counts priced with the principal's
  table. With a subscription they show what the tokens are worth, not a bill.
  To say what one job cost, note the time `bh status` shows when you start it
  and run `bh cost --since HH:MM --by seat` when it is done.
