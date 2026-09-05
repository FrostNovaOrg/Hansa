---
name: dispatch
description: Write and ring a task packet for a lane. Use when there is a ready item and an idle lane.
---

# Dispatch

1. If the change touches code you have not read this session, ask the `scout` helper for a brief first. Do not read the repository yourself.
2. `bellhop packet new --lane <lane> --title "<what, in six words>" --base <commit>`.
3. Fill the packet: what to do in plain words; what not to touch; acceptance items that a command can check (a URL that returns 200, a test that passes, a file that contains a line). Three to five items.
4. `bellhop ring inbox/<lane>/<id>-<slug>.md`.
5. Add one line to `state/in-flight.md`? No: the watchdog does that. Refresh the card if it changed (`bellhop card ...`).
6. End your turn. The sentinel wakes you when the receipt lands.

Packets under 1,500 bytes. Cite `rules/lanes/<lane>.md` instead of repeating it.
