---
name: init
description: Turn the current directory (or a named one) into a Bellhop desk - the coordination repository with inbox, bells, receipts, state files, and rules. Use when starting a new desk.
---

# /bellhop:init

`BH="${CLAUDE_PLUGIN_ROOT}/bin/bellhop"` (or `bellhop` if it is on PATH).

1. Run `$BH init <dir>` (use `.` for the current directory if it is empty). It copies the template, initialises git, and makes the first commit.
2. Open `bellhop.json` and set at least one lane: `repo` (path to the product repository), `model`, `permission_mode`. Explain each field to the user in one line; do not invent values for `repo`.
3. Write `rules/lanes/<lane>.md` from what the repository's README or build files say: build and test commands, the required-checks script, what must not be touched.
4. If the user has standing rules (what they always want or never want), record them with `$BH decide "<their words>" --standing`.
5. Run `$BH seal` and `$BH sentinel --no-wake`. Both should print green.
6. Tell the user: the desk is ready; `/bellhop:dispatch` writes the first packet; `/bellhop:daemon` makes the watchdog always-on.

Do not write packets in this skill.
