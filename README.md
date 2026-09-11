# Bellhop

**A file-based, auditable operations framework for running a team of Claude Code agents.**

Bellhop is the paperwork layer for an AI-run engineering desk. A human principal sets direction, a resident *coordinator* session writes task packets, a watchdog opens one fresh *worker* session per task, a *verifier* judges the result in a session that did not write it, and every step leaves a file behind: the packet, the bell that triggered it, the worker's receipt, the verdict, the coordinator's ledger entry, the principal's decision. Nothing important lives only in a chat window.

It was distilled from a working setup with a coordinator and parallel worker lanes. The names are generic, the example service is fictional, and no part of it depends on a particular product.

The historical source desk used several models; individual model assignments
are incomplete. See [measurement provenance](docs/measurement-provenance.md).
For a new project, start with the [Claude Code workflow contract](docs/replicate-claude-workflow.md).

## The model in one picture

```
 Principal (human)
   │  state/decisions.md (verbatim, append-only) · `bellhop brief` reads it back
   ▼
 Coordinator (resident Claude Code session, one per desk)
   │  `bellhop dispatch` → packet + bell + in-flight row + card, in one command
   ▼
 Watchdog (plain script, no model)
   │  one bell → one fresh worker session in the lane's worktree, at the packet's base
   ▼
 Worker (ephemeral session, any provider)
   │  writes receipts/<id>-<slug>.md and exits; the watchdog writes its machine header
   ▼
 Verifier (fresh read-only session on a detached copy) → receipts/<id>.acceptance.md
   ▼
 `bellhop close-task` → evidence, file set, merge with the full board, ledger, state, one commit
   │
 Sentinel (plain script, on a timer) → one log line per run, wakes the coordinator behind two gates
```

Helpers (Claude Code subagents) exist only for the coordinator's own work: scouting a codebase before writing a packet, walking through a UI, drafting text. They never own a task.

Why each of those exists, with the numbers behind it: [docs/rationale.md](docs/rationale.md).

## What you get

| Piece | What it is |
|---|---|
| `bellhop` CLI | The desk's mechanics: dispatch and ring, watch and archive, headers and their checkers, acceptance, `close-task`, `state`, `board`, `brief`, `readings`, sentinel, seals, providers, daemon |
| `skills/` | Thirteen slash commands for the coordinator: init, dispatch, intake, status, close, readings, daemon, shutdown, resume, return, condense, brief, release |
| `agents/` | Three read-only helpers: `scout`, `walkthrough`, `acceptance-reader` |
| `hooks/` | `SessionStart` prints the reading list and the board; `UserPromptSubmit` logs the principal's words; `PreToolUse` gates hand edits; `Stop` warns about unanswered bells |
| MCP server | Eight desk tools over stdio, plus the session-mode watchdog |
| `template/` | A ready-to-copy desk: directories, state files, rules, coordinator identity, permissions, provider examples |
| `docs/` | Rationale, design, plugin packaging, dispatch, headers, acceptance, close-task, state, hooks, sentinel, providers, metrics, security, operations |
| `examples/` | A fictional `widget-service` with two lanes and a dry-run demo |

Python 3.9+ standard library only. Workers are launched through a provider adapter — `claude` by default, `generic` for any other vendor's CLI ([docs/providers.md](docs/providers.md)).

## Quick start

### As a Claude Code plugin

The repository is also the marketplace, so installing takes two commands:

```
/plugin marketplace add FrostNovaOrg/Hansa
/plugin install bellhop@bellhop
```

Then run `/bellhop:init .` in an empty directory to turn it into a desk. `/bellhop:daemon` makes the watchdog always-on. Details in [docs/plugin.md](docs/plugin.md).

### From a checkout

```sh
git clone <this repo> bellhop && cd bellhop
./bin/bellhop init ../my-desk                    # copy the template, initialise git
cd ../my-desk
$EDITOR bellhop.json                             # point a lane at a repository
../bellhop/bin/bellhop provider check api        # who answers this lane, and with what
../bellhop/bin/bellhop dispatch --lane api --title "Add /health endpoint" --body body.md
../bellhop/bin/bellhop watch --once              # or run it under launchd/systemd
../bellhop/bin/bellhop accept 001                # judge it in a fresh, read-only session
../bellhop/bin/bellhop close-task 001            # evidence, file set, merge, ledger, state, commit
../bellhop/bin/bellhop sentinel
```

To see the whole loop without spending a single API call:

```sh
./examples/demo.sh
```

### Applying it to a new project

In this order, and no faster:

1. **`init`** — make the desk, commit it, read `bellhop.json`.
2. **Point one lane** at the repository, write `rules/lanes/<lane>.md` (build, check, what not to touch), and run `bellhop provider check <lane>` until it exits 0.
3. **Run one real task end to end** by hand: `dispatch` → `watch --once` → `accept` → `close-task`. One task teaches you more about your allowlist and your check command than any amount of configuration.
4. **`readings`** — after a week, read what it cost. Context per turn, full-chain cost per task and repair share are the three that change decisions; choose the coordinator's model from your numbers, not from anyone else's.
5. **Then the daemon.** `bellhop daemon install` last, once the loop works while you are watching. A watchdog answering bells you have not yet seen answered is how a desk runs away from you.

## Commands

The plugin gives the coordinator thirteen slash commands. Each one is a short procedure; the mechanics stay in the CLI.

| Command | Purpose |
|---|---|
| `/bellhop:init` | Turn an empty directory into a desk and configure its lanes |
| `/bellhop:dispatch` | Write a packet body; one command computes the rest, rings, and ends the turn |
| `/bellhop:intake` | Take in one settled task: read the closing line, decide, `close-task` |
| `/bellhop:status` | The board, once: card, in flight, debts, close-outs, bells, heartbeat |
| `/bellhop:return` | Hand one task back to its lane in four fixed sections, criteria unchanged |
| `/bellhop:brief` | The principal's page: what is waiting for them, the queue, today, money, decisions |
| `/bellhop:close` | Daily close: seals, sentinel, one summary line in the ledger |
| `/bellhop:readings` | The weekly report, with the outcome numbers filled in by hand |
| `/bellhop:condense` | Weekly shrink: recount self-catches, retire hot rules, settle stale debts |
| `/bellhop:release` | Run the desk's release steps in order, stop at the first failure, resume from it |
| `/bellhop:shutdown` | Pause the desk: stop dispatch, settle what is running, close, write the resume list |
| `/bellhop:resume` | The reverse, in order: services, sentinel dry run, the readings gap, lift, the card |
| `/bellhop:daemon` | Install, load, unload, check, or remove the resident watchdog and sentinel |

The CLI carries the verbs a skill calls: `dispatch`, `ring`, `watch`, `archive`, `receipt-header`, `check-receipt`, `acceptance-header`, `check-acceptance`, `header-spec`, `accept` (with `record`, `shadow-report`, `status`), `sample`, `merge`, `close-task`, `settle-bell`, `state`, `board`, `brief`, `intake`, `hand`, `stop-dispatch`, `lane`, `provider check`, `provider-guard`, `readings`, `release`, `sentinel`, `seal`, `close`, `coldstart`, `daemon` (`install`, `load`, `unload`, `status`, `uninstall`, `files`), `mcp`, `hook`.

## Design principles

1. **Files, not chat.** Every instruction, result, and approval is a file in a git repository. If it is not in a file, it did not happen.
2. **One task, one fresh session.** Workers start cold, read a short list, do one thing, write one receipt, and exit. No long-lived worker state.
3. **Append, never rewrite.** The ledger and the decisions log only grow. Rewriting state is done by scripts that keep the previous version.
4. **Guardrails at the repository layer.** Workers get a worktree, a pre-push guard, and a scrubbed environment. These are cooperating-agent guardrails, not OS isolation or removal of the OS user's credentials. The model's own tool gate is the second line of defence, not the first.
5. **Machines check what machines can check.** The sentinel, seals, and the machine headers catch drift; rules that a script can enforce are not written as rules.
6. **Gates block once; they never remind.** A hook that only warns changes nothing — one that fired on every edit moved no habit. One hook can refuse a call; nothing else nags.
7. **Measure outcomes; use process numbers only to diagnose.** Time from bug report to release and defects found after release are the score. Commit counts are a symptom.

Read [docs/rationale.md](docs/rationale.md) for why each piece looks the way it does, and [docs/design.md](docs/design.md) for the specification.

## Status

Early. The scripts run and are tested in dry-run mode; the shape of the desk is stable; the Claude Code integration tracks the current CLI. Expect breaking changes before 1.0.

## License

Bellhop is dual-licensed.

- **Open source:** [GNU AGPL v3 or later](LICENSE). Use it, change it, share it; if you run a modified version as a network service or distribute it, the AGPL asks you to share your source under the same terms.
- **Commercial:** [Bellhop Commercial License](LICENSE-COMMERCIAL.md) from FrostNova, for embedding Bellhop in proprietary products or services without AGPL obligations.

`SPDX-License-Identifier: AGPL-3.0-or-later OR LicenseRef-Bellhop-Commercial`

Copyright (c) 2026 FrostNova.
