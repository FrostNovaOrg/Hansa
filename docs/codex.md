# Codex adapter output

The optional [Codex adapter](../contrib/codex/README.md) uses temporary output for
worker runs and provider checks. Standard output and error use unnamed, mode 600
regular files, closed when the adapter exits. The adapter reads events while the
run proceeds and uses standard error when needed for its final answer. Temporary
srt settings are in the run's own folder and removed with it.

The answer still contains the report, thread id and token accounting. Live
progress and hansa's task results remain available through the usual commands
and page. `HANSA_CODEX_LOGS` is accepted for compatibility and has no effect.

## Upgrading

After stopping runs started with an earlier adapter, remove their output from
the adapter's former `logs/` folder, or the folder previously selected by
`HANSA_CODEX_LOGS`. Upgrading does not remove existing files.
