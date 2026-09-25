# bh (alpha)

A small desk for a team of Claude Code agents: a coordinator session queues
tasks, workers (one `claude -p` run per task, each in its own git worktree)
do them, and a person (the principal) owns the settings, keys and budget
through a local web page. Python standard library only; no build step.

## Set up
```sh
bin/bh init /path/to/desk --repo /path/to/checkout   # first lane is "a"
cd /path/to/desk && bh serve                          # keep it running (tmux, systemd, ...)
```
From your own computer, open the page through an SSH tunnel:
```sh
ssh -L 8765:127.0.0.1:8765 HOST /path/to/bin/bh --desk /path/to/desk web
```
It prints an address with a token; open it in the browser and keep the
terminal open. On the page: add providers (a Claude Code login, or any
endpoint that speaks the Anthropic API, with its key), lanes, allowed
commands, prices. Then start the coordinator with `bh coordinator`.

The first time Claude Code opens in a new desk, it asks whether you trust the
folder: choose yes. Claude Code ignores the allowed commands of a folder it does
not trust, and the coordinator could not run `bh`. The same question comes up
once for each lane's design worktree, in the first design session.

## Who changes what
- The principal, on the page: providers, models, endpoints, keys, effort,
  each lane's provider order, allowed commands and environment, prices,
  concurrency, whether the coordinator audits cost.
- The coordinator, with `bh`: tasks, each lane's standing instructions
  (`lanes/<lane>/brief.md`), pause and resume. Its whole interface is
  `card.md`, copied into the desk as `CLAUDE.md`.
- Nobody reads bh's code to use it. If the card or a command's output is not
  enough, fix the card or the output.

## Files
Everything is under the desk directory; see the top of `desk.py`. Keys live in
`secrets/` (mode 600) and are never shown or sent anywhere but to the
provider's endpoint, as that worker's environment.

## Tests
`python3 -m unittest tests.test_bh` (uses a stand-in for `claude`; no model is called).
