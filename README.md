# Hansa

**A small team of Claude Code agents, organized like a company.**

A coordinator session queues tasks; workers (one `claude -p` run per task, each
in its own git worktree) do them; hansa commits their changes, runs the lane's
check and keeps the record; and a person (the principal) owns the settings,
keys and budget through a local web page. Python standard library only; no
build step.

This branch holds an alpha rewrite that is much smaller than 0.4. Up to 0.4 the
project was called Bellhop; that version is on the `main` branch.

## Set up
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
endpoint that speaks the Anthropic API, with its key), lanes, allowed
commands, prices. Then start the coordinator with `hansa coordinator`.

The first time Claude Code opens in a new desk, it asks whether you trust the
folder: choose yes. Claude Code ignores the allowed commands of a folder it does
not trust, and the coordinator could not run `hansa`. The same question comes up
once for each lane's design worktree, in the first design session.

## Who changes what
- The principal, on the page: providers, models, endpoints, keys, effort,
  each lane's provider order, allowed commands, environment, check and setup
  commands, prices, concurrency, whether the coordinator audits cost.
- The coordinator, with `hansa`: tasks, each lane's standing instructions
  (`lanes/<lane>/brief.md`), pause and resume. Its whole interface is
  `hansa/card.md`, copied into the desk as `CLAUDE.md`.
- Workers change files in their worktree and report. They do not commit:
  hansa commits each task's changes on its own branch, and merges only what the
  coordinator merges.
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
- Allowed commands are Claude Code permission rules. They keep an agent from
  doing what it was not asked to do; they are not operating-system isolation.
  Code a worker runs (tests, the lane's check and setup commands) runs as the
  desk's user. Run a desk as a user, or on a machine, whose files you are
  content for that code to reach.
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
