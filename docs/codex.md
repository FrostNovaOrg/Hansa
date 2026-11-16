# Codex

hansa can run a worker with OpenAI's Codex command line instead of Claude Code, through the optional
[Codex adapter](../contrib/codex/README.md), whose README holds the details.

## Setting it up

1. Install Codex in a folder of its own, outside `~/.codex` and every folder workers may not read.
2. On the page (Settings, Providers), add a provider of the kind Codex: its Codex program and Codex home as full
   paths, and as its model the Codex model to run. "More Codex settings" holds the optional certificate file, web
   search and the Codex features turned off. Put the provider in a lane's providers. It has no address and no key,
   and cannot be the coordinator's provider.
3. Log in once with Codex's own login on that home: `CODEX_HOME=<Codex home> <Codex program> login`. The page and
   `hansa explain provider NAME` give the command. Then `hansa ping NAME`.

hansa starts the adapter itself for such a provider and passes its settings in the run's environment: no
`HANSA_CLAUDE`, no `codex.env`. It keeps the Codex home from every run but that provider's own Codex runs, so
nothing needs adding to what workers may not read. A lost login rests the provider until it is logged in again and
`hansa ping NAME` answers. Providers on one Codex home share its usage rests, across providers and desks. A
task's `--model` is not used on a Codex provider, the provider's model is; the task's or provider's effort is
passed on to Codex.

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
