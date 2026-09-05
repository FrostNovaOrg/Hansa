---
name: status
description: Show the desk's state - sentinel checks, dispatch card, in-flight, pending bells, watchdog heartbeat. Use when asked "what is going on" or before dispatching.
---

# /bellhop:status

`BH="${CLAUDE_PLUGIN_ROOT}/bin/bellhop"`.

1. `$BH sentinel --no-wake` — print it as is. Red lines name what to fix; fix through the proper command (`card`, `ledger add`, `seal`), never by editing state files by hand.
2. `$BH daemon status` — if installed but no fresh heartbeat, say so and point to `/bellhop:daemon`.
3. `$BH card --show`, then `state/in-flight.md`.
4. Answer the user's question in five lines or fewer: what is running, what is waiting, what is red, what you will do next.
