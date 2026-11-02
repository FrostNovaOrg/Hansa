# Concepts

The [README](../README.md) has the overview.

## Key terms

| Term | What it is |
|---|---|
| **Principal** | The person the desk works for. On the web page they set providers, keys, models and prices, read their to-dos and approve or refuse releases. They answer to-dos in the conversation, and the coordinator records each outcome. The principal can also answer a release request in the conversation with the one-time code in their alert. |
| **Coordinator** | An interactive Claude Code session in the desk (`hansa coordinator`). It plans, queues, reviews and merges work through `hansa`, and sets up the lanes. It runs outside the sandbox, so it can install what a lane needs; Claude Code asks the principal before each command not already allowed. Its interface is [`hansa/card.md`](../hansa/card.md), with `hansa help` covering the rest. |
| **Helpers** | Three subagents in the coordinator's session: `scout` (looks something up) and `reviewer` (reads a high-stakes change) only read; `operator` does one hands-on job on the machine, such as installing software. |
| **Worker** | One `claude -p` run per task, or per verification (`hansa verify`). It may run any command inside Claude Code's sandbox, but can write only in its worktree and the folders its lane allows and reach only its lane's hosts. It never commits. |
| **Desk** | One folder that holds a team's settings, keys, tasks and an append-only event log. The command line, the web page and the agents all read the same files. |
| **Lane** | A git checkout for workers: its base branch, a worktree of its own, its provider order, the hosts and folders its workers may reach, and its setup, check and release commands. A lane runs one task at a time; lanes on the same repository run tasks side by side, up to `hansa concurrency`. |
| **Provider** | Where a run gets its model: a Claude Code login (a subscription), or any endpoint that speaks the Anthropic API, with its key. A provider that hits its usage limit rests, and its task moves to the lane's next ready provider, or waits for one. |
| **Check** | A lane's test command. hansa runs it on each task's branch, in the lane's sandbox; a failed check blocks the merge unless the coordinator overrides it, which is recorded. |
| **Release** | Sending a lane's base branch out through one of its named release targets, after the principal's yes. It runs outside the sandbox, as the desk's account. |

## The desk

A desk is one folder of plain files:

```text
desk/
├── settings.json          the principal's settings (set on the page) and the lanes (the coordinator's)
├── secrets/               provider keys; written by the page, never shown
├── CLAUDE.md              the card, a copy of hansa/card.md
├── .claude/               the coordinator's permission rules and helpers (your own rules: settings.local.json)
├── lanes/<lane>/brief.md  standing instructions sent to every worker in a lane
├── tasks/<id>/            each task's text, record and report, and what its processes wrote, keys removed
├── worktrees/<lane>/      one git worktree per lane
├── notes/                 the coordinator's notes; notes/PROJECT.md holds the project's own rules
├── events.jsonl           append-only record of everything that happened
├── asks.json              requests from the agents to the principal
├── PAUSED                 present: no new work starts
└── .hansa/                runtime state: providers' rests, the heartbeat, the page's token and port, releases
```

More: [The desk: files, keys and notes](desk.md).

## macOS and Linux

| | macOS | Linux |
|---|---|---|
| Workers' sandbox | Built in | Needs `bubblewrap` and `socat`; AppArmor on Ubuntu 24.04 and later can stop it |
| `localhost` on a lane's network list | Needed for workers' own local servers, but opens every service on the machine's loopback: list it only when nothing there must be kept from workers | Leave it off: each sandbox has a loopback of its own, and it would only open the machine's loopback through the sandbox's proxy |
| Builds in the sandbox | `mktemp` ignores `TMPDIR` unless given a template there, and is refused: [Java and Maven in a lane](java-and-maven.md) | The mount table can call a writable folder read-only, and git there leaves out the sandbox's placeholder files: [On a Linux server](linux-server.md#what-a-lanes-commands-meet-in-linuxs-sandbox) |
| As root | — | The coordinator may run as root, with the desk owned by an ordinary account: [On a Linux server](linux-server.md) |

## Limits

- Alpha: settings, commands and files may still change between versions.
- A worker's commands and the lane's setup and check run in the operating system's sandbox. The coordinator
  runs as the desk's user (or as root on a Linux server). Run a desk only with a user account or on a machine
  whose files that code may reach.
- With the [Codex adapter](../contrib/codex/README.md), a Codex run's commands can read the Codex login: Codex and
  the commands it runs share one sandbox, and every Codex run, on any lane, uses that one login. Claude workers
  cannot read it, and a Codex run's network reaches only the lane's hosts and OpenAI's.
- A release's command runs outside the sandbox, as the desk's account. Anything it builds and starts from the base
  branch also runs outside the sandbox as that account, so list a lane's build and release files as its `release_files`.
- The coordinator's rules against pushing and publishing catch only the forms they name. If a command takes another
  form, it waits for the principal only when Claude Code asks before running it.
- hansa replaces the principal's keys wherever it shows or records text, and will not merge a change that
  holds one. It cannot see into compressed files, and the event log keeps what it recorded before a key was
  saved.
- When a run ends, hansa stops what it left running, but a process that moved out of every marker between
  two of `hansa serve`'s rounds, and whose parent ended before the next one, can escape: see
  [what hansa stops when a run ends](sandbox.md#what-hansa-stops-when-a-run-ends).
- A lane repository's merge hooks run while `hansa merge` holds the desk, so `hansa serve` waits for them.
- One `hansa serve` per desk, on one machine.

## Everyday commands

The coordinator runs these; you can too. Every coordinator command is in
[`hansa/card.md`](../hansa/card.md), which `hansa help card` prints. `hansa help`
lists commands and topics, and `hansa help <topic>` prints one from `docs/`.
`hansa --help` lists every command, including the principal's `init`, `serve`,
`web`, `coordinator` and `ping`; `hansa <command> --help` or
`hansa help <command>` shows its options.

| Command | What it does |
|---|---|
| `hansa`, `hansa menu` | The principal's terminal menu: choose, start, stop and enter this account's desks |
| `hansa status` | Whether `hansa serve` runs, the page's address, the lanes, what is running, waiting or failed, the providers, the last 24 hours |
| `hansa add "<task>" [--lane L]` | Queue a task |
| `hansa show ID`, `hansa diff ID` | A task's report, its check, the files it changed, what to do next; its changes against the base |
| `hansa verify ID` | Have a worker run a finished task's change to see whether it holds; never merged |
| `hansa merge ID` | Merge a finished task into its lane's base branch |
| `hansa retry ID "<what to change>"` | Queue a follow-up on top of the earlier task (`--fresh`: from the base; `--update`: with the base merged in) |
| `hansa cancel ID`, `hansa close ID` | Stop queued, running or merging work; close finished work without merging |
| `hansa release L [TARGET] ["<note>"]` | Ask for a release of lane L's base branch; it runs after the principal's yes (a target ticked to go out without asking needs none) |
| `hansa release yes\|no ID CODE` | Pass on the principal's answer to a release, given in the conversation with their alert's one-time code |
| `hansa lane ...`, `hansa concurrency [N]` | List, add, change or remove lanes; how many tasks run at once |
| `hansa ask "<what>"` | Record a to-do for the principal; close it with `--close N "<outcome>"` |
| `hansa cost [--by day\|seat\|lane\|task\|model\|provider]` | Tokens, and what they are worth at the principal's prices |
| `hansa explain [lane L \| provider P \| server \| notify]` | This desk's current settings and state, and where each comes from |

## Upgrading and going back

**Upgrading.** In the desk, run `hansa pause "upgrade"` to keep new work from starting while
running work finishes. `hansa status` lists what runs; wait until nothing runs.

In hansa's clone, record `git rev-parse HEAD` so you can go back, then run `git fetch origin` and
check out the version you want. `git checkout --detach COMMIT` runs one fixed commit.

Restart `hansa serve` to run the new code. If `hansa up` started the server, stop it with
`kill PID`, using the pid it printed, then run `hansa up` in the desk. It leaves running
servers alone. If you started it by hand, press Ctrl-C and rerun its `hansa serve` command
with the same options.

Server startup updates the card. The coordinator reads it when a session starts, after `/clear`
or after compaction; restart the session after changing its permissions, helpers, mod or launch
options. See [Starting the server and the coordinator again](desk.md#starting-the-server-and-the-coordinator-again).
Then run `hansa resume`. The first line of `hansa status` shows the version and when it was installed.

**Going back.** Follow the same steps with the commit you ran before. Newer commands may
write settings older code does not know: `hansa status` and `hansa add` can still work, but
saving settings is refused with `unknown setting: NAME` for each unfamiliar setting.
Settings set with newer commands are best undone before going back.

**What changed:** in the clone, run `git log --oneline OLD..NEW`.
