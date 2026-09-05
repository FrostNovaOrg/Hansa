# Bellhop

**A file-based, auditable operations framework for running a team of Claude Code agents.**

Bellhop is the paperwork layer for an AI-run engineering desk. A human principal sets direction, a resident *coordinator* session writes task packets, a watchdog opens one fresh *worker* session per task, and every step leaves a file behind: the packet, the bell that triggered it, the worker's receipt, the coordinator's ledger entry, the principal's decision. Nothing important lives only in a chat window.

It was distilled from a working setup with a coordinator and parallel worker lanes. The names are generic, the example service is fictional, and no part of it depends on a particular product.

## The model in one picture

```
 Principal (human)
   │  decisions.md  (verbatim, append-only)
   ▼
 Coordinator (resident Claude Code session, one per desk)
   │  writes inbox/<lane>/<id>-<slug>.md   → rings a bell
   ▼
 Watchdog (plain script, no model)
   │  one bell → one fresh `claude -p` worker in the lane's repo
   ▼
 Worker (ephemeral session, any provider)
   │  writes receipts/<id>-<slug>.md, exits
   ▼
 Archive (plain script) → ledger entry, commit, optional push
   │
 Sentinel (plain script, on a timer) → red/green status line, wakes the coordinator
```

Helpers (Claude Code subagents) exist only for the coordinator's own work: scouting a codebase before writing a packet, walking through a UI, drafting text. They never own a task.

## What you get

| Piece | What it is |
|---|---|
| `bellhop` CLI | `init`, `ring`, `watch`, `archive`, `sentinel`, `ledger`, `decide`, `sample`, `accept`, `readings`, `seal`, `close`, `coldstart`, `daemon` |
| `skills/` | Seven slash commands for the coordinator: init, dispatch, intake, status, close, readings, daemon |
| `agents/` | Three read-only helpers: `scout`, `walkthrough`, `acceptance-reader` |
| `hooks/` | `SessionStart` prints the desk's state; `Stop` warns about bells nobody is answering |
| MCP server | Eight desk tools over stdio, plus the session-mode watchdog |
| `template/` | A ready-to-copy desk: directories, state files, rules, coordinator identity, permissions |
| `docs/` | Design, plugin packaging, security model, metrics, daily operations |
| `examples/` | A fictional `widget-service` with two lanes and a dry-run demo |

Python 3.9+ standard library only. Workers are launched through the Claude Code CLI (`claude -p`); other providers plug in through a lane's `worker_cmd`.

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
../bellhop/bin/bellhop ring inbox/api/001-add-health-endpoint.md
../bellhop/bin/bellhop watch --once              # or run it under launchd/systemd
../bellhop/bin/bellhop sentinel
```

To see the whole loop without spending a single API call:

```sh
./examples/demo.sh
```

## Commands

The plugin gives the coordinator seven slash commands. Each one is a short procedure; the mechanics stay in the CLI.

| Command | Purpose |
|---|---|
| `/bellhop:init` | Turn an empty directory into a desk and configure its lanes |
| `/bellhop:dispatch` | Write a task packet for a lane, ring its bell, then end the turn |
| `/bellhop:intake` | Take in one receipt: numbers, sampling, one ledger entry, card, merge or reject |
| `/bellhop:status` | What is running, what is waiting, what is red |
| `/bellhop:close` | Daily close: seals, sentinel, one summary line in the ledger |
| `/bellhop:readings` | The weekly report, with the outcome numbers filled in by hand |
| `/bellhop:daemon` | Install, check, or remove the resident watchdog and sentinel |

## Design principles

1. **Files, not chat.** Every instruction, result, and approval is a file in a git repository. If it is not in a file, it did not happen.
2. **One task, one fresh session.** Workers start cold, read a short list, do one thing, write one receipt, and exit. No long-lived worker state.
3. **Append, never rewrite.** The ledger and the decisions log only grow. Rewriting state is done by scripts that keep the previous version.
4. **Guardrails at the repository layer.** Workers get a worktree, no push rights, and a scrubbed environment. The model's own tool gate is the second line of defence, not the first.
5. **Machines check what machines can check.** The sentinel, seals, and the required-checks list catch drift; rules that a script can enforce are not written as rules.
6. **Measure outcomes; use process numbers only to diagnose.** Time from bug report to release and defects found after release are the score. Commit counts are a symptom.

Read [docs/design.md](docs/design.md) for the full picture.

## Status

Early. The scripts run and are tested in dry-run mode; the shape of the desk is stable; the Claude Code integration tracks the current CLI. Expect breaking changes before 1.0.

## License

Bellhop is dual-licensed.

- **Open source:** [GNU AGPL v3 or later](LICENSE). Use it, change it, share it; if you run a modified version as a network service or distribute it, the AGPL asks you to share your source under the same terms.
- **Commercial:** [Bellhop Commercial License](LICENSE-COMMERCIAL.md) from FrostNova, for embedding Bellhop in proprietary products or services without AGPL obligations.

`SPDX-License-Identifier: AGPL-3.0-or-later OR LicenseRef-Bellhop-Commercial`

Copyright (c) 2026 FrostNova.
