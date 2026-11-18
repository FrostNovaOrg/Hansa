# Codex

hansa can run a worker with OpenAI's Codex command line instead of Claude Code, through the optional
[Codex adapter](../contrib/codex/README.md), whose README holds the details.

## Setting it up

1. Install Codex. Its standard install under `~/.codex/packages` works, including a link such as
   `~/.local/bin/codex`. Other installs must be outside folders workers may not read.
2. On the page (Settings, Providers), add a provider of the kind Codex: its Codex program and Codex home as full
   paths, and as its model the Codex model to run. "More Codex settings" holds the optional certificate file, web
   search and the Codex features turned off. Put the provider in a lane's providers. It has no address and no key,
   and cannot be the coordinator's provider.
   Choosing Codex fills empty program and home fields with paths on the machine running `hansa serve`:
   the program found through `HANSA_CODEX_BIN`, `PATH`, then `~/.local/bin/codex` (keeping links as found),
   and `~/codex-home` expanded to a full path. A program that save would refuse is left blank with the
   refusal's reason. Existing text and providers already saved as Codex are left alone; an older server
   or a page where settings cannot be changed fills nothing. Review the paths and save to keep them.
3. Give this provider a home folder of its own, outside `~/.codex`, the desk and the lanes' repositories, and log
   in once there: `CODEX_HOME=<Codex home> <Codex program> login`. The suggested separate home is `~/codex-home`;
   an existing login there can be reused. Detecting paths creates no folder, reads no login and runs no program.
   Plain `codex login` uses Codex's default home (`~/.codex`) and does not log in this provider.
   The page and `hansa explain provider NAME` give the command. If Ping finds no login, the page gives the same
   command beside the button and in Status, in the page's language. Then `hansa ping NAME`.
4. For a second Codex model on the same program and home, use **Add a model** under that provider's card: a
   name, the model and an effort. The new entry runs with the card's Codex program, home and login; no second
   provider and no second login are needed. Put its name in a lane's providers as any other.

hansa starts the adapter itself for such a provider and passes its settings in the run's environment: no
`HANSA_CLAUDE`, no `codex.env`. It keeps the Codex home from every run but that provider's own Codex runs, so
nothing needs adding to what workers may not read. A lost login rests the provider until it is logged in again and
`hansa ping NAME` answers. Providers on one Codex home share its usage rests, across providers and desks; on one desk they also rest
together for its lost login or its credit (see [Tasks](tasks.md)). A
task's `--model` is not used on a Codex provider, the provider's model is; the task's or provider's effort is
passed on to Codex.

For a program resolving under the account's `~/.codex/packages`, only that provider's worker runs and provider
checks may read that folder, as named and with links resolved. It stays read-only. Every other entry in
`~/.codex` stays closed, including the account's login, settings, rules, instructions, skills, memories,
sessions, history and logs. The provider still uses its separate home and login. The principal's no-read entries
always win, including an entry for packages or anything inside it. Claude workers and verifiers, Codex providers
whose program is elsewhere, and lane setups and checks get no package exception. On macOS the repository's
tests run this rule against the real sandbox, with a stand-in for the program; on Linux the settings handed to
the sandbox are checked, and a run with the real sandbox and the real program has not been done yet. The page refuses other
built-in no-read locations on save, including a program reached through a link, the desk's own folders,
other hidden desks and other providers' homes. These checks prevent saving a program that its runs cannot read.

## Desks set up the old way

A desk may still reach Codex the old way: `hansa serve` started with `HANSA_CLAUDE` pointing at the adapter, a
`compatible` provider with the address `http://codex.invalid` and a made-up key, and the adapter's settings in
`contrib/codex/codex.env`. That keeps working, as the adapter's README describes; there the principal keeps the
Codex home on the list of folders workers may not read. `hansa explain provider NAME` and the provider's card on
the page say that such a provider can be changed.

## Upgrading

For an old compatible provider, choose the kind Codex on the page, fill in the
Codex program and home paths, and save; that save removes its stored made-up key.
Once no provider uses the old way, `hansa serve` needs no `HANSA_CLAUDE`.

After stopping runs started with an earlier adapter, remove their output from
the adapter's former `logs/` folder, or the folder previously selected by
`HANSA_CODEX_LOGS`. Upgrading does not remove existing files.

## Adapter output

The adapter uses temporary output for worker runs and provider checks. Standard
output and error use unnamed, mode 600 regular files, closed when the adapter
exits. The adapter reads events while the run proceeds and uses standard error
when needed for its final answer. Temporary srt settings are in the run's own
folder and removed with it.

The answer still contains the report, thread id and token accounting. Live
progress and hansa's task results remain available through the usual commands
and page. `HANSA_CODEX_LOGS` is accepted for compatibility and has no effect.
