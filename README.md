# Hansa Agents

**A small team of Claude Code agents, organized like a company.**

A coordinator session queues tasks; workers (one `claude -p` run per task, each
in its own git worktree) do them; hansa commits their changes, runs the lane's
check and keeps the record; and a person (the principal) owns the settings,
keys and budget through a local web page. Python standard library only; no
build step.

This branch holds an alpha rewrite that is much smaller than 0.4. Up to 0.4 the
project was called Bellhop; that version is on the `main` branch.

## Set up
Needs Python 3, git and Claude Code. Workers run in Claude Code's sandbox and do
not start without it: on macOS it is built in; on Linux it needs `bubblewrap`
and `socat`, and on Ubuntu 24.04 and later the system's AppArmor rules can stop
it (see Claude Code's sandboxing documentation). A lane with a setup or check
command also needs `srt`, Anthropic's sandbox runtime, which runs them in the
lane's sandbox: `npm install -g @anthropic-ai/sandbox-runtime`. Without it,
tasks in such a lane fail, and `hansa status` says so.
```sh
bin/hansa init /path/to/desk --repo /path/to/checkout   # first lane is "a"
cd /path/to/desk && hansa serve                          # keep it running (tmux, systemd, ...)
```
From your own computer, open the page through an SSH tunnel:
```sh
ssh -L 8765:127.0.0.1:8765 HOST /path/to/bin/hansa --desk /path/to/desk web
```
It prints an address with a token; open it in the browser and keep the
terminal open. On the page: add providers (a Claude Code login, or any
endpoint that speaks the Anthropic API, with its key), models, effort
defaults, prices. Then start the coordinator with `hansa coordinator`; it sets
up the lanes, their allowed commands and how many tasks run at once with
`hansa lane` and `hansa concurrency`.

The first time Claude Code opens in a new desk, it asks whether you trust the
folder: choose yes. Claude Code ignores the allowed commands of a folder it does
not trust, and the coordinator could not run `hansa`. The same question comes up
once for each lane's design worktree, in the first design session.

## Who changes what
- The principal, on the page: providers, models, endpoints, keys, effort
  defaults, the models of the coordinator and of design sessions, prices,
  whether the coordinator audits cost. The page also shows what goes on.
- The coordinator, with `hansa`: tasks; the lanes (`hansa lane`: each one's
  provider order, allowed commands, environment, check, preview and setup
  commands, limits) and how many tasks run at once (`hansa concurrency`);
  each lane's standing instructions (`lanes/<lane>/brief.md`); pause and resume.
  Setup, check and preview run outside the worker, so each change to them is
  recorded and the coordinator tells the principal about it. It is not a
  worker: it runs on this machine outside any sandbox, and besides `hansa` it
  may run other commands the principal's work needs, such as installing
  software a lane needs or setting up a tool. Claude Code asks the principal
  before each one unless they have allowed it, and the coordinator says in
  plain words what the command does and why. It has three helpers: `scout`
  and `reviewer` only read; `operator` does one hands-on job on the machine
  (installing software, setting up a tool) with the coordinator's rights, each
  command with the principal's permission. Two things stay closed by design:
  reading `secrets/` (the keys) and editing code in the lanes' repositories;
  code changes go through workers, so each gets a branch, the lane's check and
  a merge. Its whole interface is `hansa/card.md`, copied into the desk as
  `CLAUDE.md`.
- Workers (task and verification workers) change files in their worktree and
  report, inside Claude Code's sandbox, which the operating system enforces: any
  command, but files changed only in the worktree (and its temporary folder), no
  reading of the desk's keys or of common credential files, no network but the
  lane's network list (`hansa lane set L network ...`), and no git command that
  changes history. They do not commit: hansa commits each task's changes on its
  own branch, and merges only what the coordinator merges. The lane's setup,
  before the worker, and its check, after it, run in the same sandbox (with
  `srt`): the lane's network and write lists hold for them too, so a setup that
  downloads needs its registry on the network list. Design sessions, which the
  principal works in directly, and the coordinator run outside it.
- Workers see their checkout's own CLAUDE.md and settings, and hansa's instructions: not the desk's card,
  and not the settings, plugins, hooks or MCP servers of the account they run as.
- Nobody reads hansa's code to use it. If the card or a command's output is not
  enough, fix the card or the output.

## Files
Everything is under the desk directory; see the top of `hansa/desk.py`. Keys live
in `secrets/` (mode 600) and are never shown or sent anywhere but to the
provider's endpoint, as that worker's environment. hansa replaces a key with
`[key of <provider>]` wherever it would otherwise show, and refuses to merge a
change that holds one. The coordinator's permission rules are hansa's: they are
in the desk's `.claude/settings.json` and come up to date whenever the
coordinator starts. Rules of your own go in `.claude/settings.local.json`.

## Current limits
- Alpha: settings, commands and files may still change between versions.
- A worker's commands, and the lane's setup and check commands, run in the
  operating system's sandbox; the lane's preview command, design sessions and
  the coordinator do not, and run as the desk's user. Run a desk as a user, or on a machine, whose files you
  are content for that code to reach.
- hansa replaces the principal's keys wherever it shows or records text, and will not
  merge a change that holds one. It cannot see into compressed files, and the
  event log keeps what it recorded before a key was saved.
- What a worker leaves running is stopped when the worker ends, unless `hansa serve`
  was restarted in between.
- A lane repository's merge hooks run while `hansa merge` holds the desk, so
  `hansa serve` waits for them.
- One `hansa serve` per desk, on one machine.

## Tests
`python3 -m unittest tests.test_hansa` (uses a stand-in for `claude`; no model is called).

## License

Hansa is dual-licensed.

- **Open source:** [GNU AGPL v3 or later](LICENSE). Use it, change it, share it; if you run a modified version as a network service or distribute it, the AGPL asks you to share your source under the same terms.
- **Commercial:** [Hansa Commercial License](LICENSE-COMMERCIAL.md) from FrostNova, for embedding Hansa in proprietary products or services without AGPL obligations.

`SPDX-License-Identifier: AGPL-3.0-or-later OR LicenseRef-Hansa-Commercial`

Copyright (c) 2026 FrostNova.
