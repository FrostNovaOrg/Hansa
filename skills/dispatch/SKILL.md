---
name: dispatch
description: Write and ring a task packet for a lane. Use when there is a ready item and an idle lane.
---

# /bellhop:dispatch

`BH="${CLAUDE_PLUGIN_ROOT}/bin/bellhop"`.

1. If the change touches code you have not read this session, ask the `scout` subagent for a brief first. Do not read the repository yourself.
2. `$BH packet new --lane <lane> --title "<what, in six words>" --base <commit>`.
3. Fill the packet: what to do in plain words; what not to touch; three to five acceptance items a command can check (a URL that returns 200, a test that passes, a file that contains a line). Cite `rules/lanes/<lane>.md` instead of repeating it. Under 1,500 bytes.
4. `$BH ring inbox/<lane>/<id>-<slug>.md`.
5. `$BH card --ready no --reason "waiting on receipt <id>"` (or `--ready yes --next ...` if another packet is ready).
6. **End your turn.** The watchdog answers the bell; the sentinel or the receipt wakes you. Never poll logs in a loop.
