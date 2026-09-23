# Desk card

You coordinate worker agents through `bh`. This card is the whole interface:
you never need to read bh's code. If a command's output leaves you unsure what
to do next, that is a gap in bh, not in you: say so with `bh ask`.

## How work moves
1. You queue a task: one change a worker can make and check on its own.
2. `bh serve` (always running) starts it in a free lane: a separate Claude Code
   run in the lane's own git worktree, on a fresh branch `task/<id>`.
3. The worker commits there and ends with a short report. It cannot push.
4. Meanwhile run `bh wait` in the background (the Bash tool's background option);
   it returns, and you are told, when a task finishes or fails.
5. You review and merge, or send it back with what to change.

## Commands
| command | what it does |
|---|---|
| `bh status` | lanes, running, queued (and why it waits), waiting for review, failed, providers, today's usage |
| `bh add "<task>" [--lane L] [--effort high] [--model M] [--title T]` | queue a task; `--file PATH` reads the text from a file |
| `bh show <id>` | report, changed files, refused commands, usage, and what to do next |
| `bh diff <id> [--stat]` | the task branch against its base |
| `bh merge <id>` | merge the task branch into the lane's base branch |
| `bh retry <id> "<what to change>" [--fresh]` | queue a follow-up on top of that attempt (`--fresh`: start from base) |
| `bh cancel <id>` | stop a queued or running task |
| `bh wait [id ...] [--timeout MIN]` | return when one of those tasks (default: all queued or running) finishes or fails, or the principal answers a request; run it in the background |
| `bh pause "<reason>"` / `bh resume` | stop / restart new work (running tasks finish) |
| `bh explain [lane L \| provider P \| task ID \| states \| server]` | what a setting or state means and where it comes from |
| `bh cost [--days N] [--by day\|seat\|lane\|task\|model]` | tokens and what they are worth at the principal's prices |
| `bh ask "<request>"` | put a request on the principal's page |
| `bh help` | this card |

Add `--json` to `status` or `cost` for machine-readable output.

## Writing a task
Say what to change, where, and how the worker can check it (a command, a file to
look at). Keep one task to one change. If the lane lacks a command the check
needs, `bh explain lane L` shows what is allowed; ask the principal to add it.

## Reading the result
`bh show <id>` gives the worker's report plus facts bh recorded itself: changed
files, commits, commands the lane refused, tokens. A refused command means the
worker could not do something; decide whether the task needs it (then ask) or
should be rewritten.

## What you change, and what only the principal changes
- Yours: tasks and their options; each lane's standing instructions in
  `lanes/<lane>/brief.md` (sent to every worker in that lane); pause/resume.
- The principal's, on the web page: providers, models, endpoints, keys, effort
  defaults, each lane's provider order, allowed commands and environment,
  prices, how many tasks run at once. Ask with `bh ask`; do not edit
  `settings.json`. Keys live in `secrets/`, which you cannot read, by design.

## Facts you can rely on
- A provider that reports it is out of quota rests for a while and the task
  moves to the lane's next provider; `bh status` shows this.
- A worker that runs past its lane's time limit is stopped and marked failed.
- Anything a worker left uncommitted is committed on its branch when it ends.
- Cost figures are Claude Code's own token counts priced with the principal's
  table. With a subscription they show what the tokens are worth, not a bill.
