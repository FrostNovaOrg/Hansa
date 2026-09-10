---
name: daemon
description: Install, check, remove, or bring back after a reboot the resident watchdog and sentinel (launchd on macOS, systemd on Linux) so bells are answered even when no Claude Code session is open. Use when the user wants always-on operation, when the session-start note says there is no fresh heartbeat, after a reboot, or after a Bellhop upgrade.
---

# /bellhop:daemon

`BH="${CLAUDE_PLUGIN_ROOT}/bin/bellhop"`. The install command is deterministic; your job is to run it, read what it says, fix the machine's environment if needed, and verify. Do not write service files by hand.

## Autostart is off by default

`autostart` in `bellhop.json` is `false` unless someone set it. That means the services **do not come back on their own after a reboot**: on macOS `RunAtLoad` and `KeepAlive` are false and the jobs are written disabled; on Linux the units have no `[Install]` section, so `systemctl --user enable` has nothing to hook into. This is deliberate — a watchdog that comes up by itself starts answering bells against whatever state the machine booted into, and the operator finds out from the receipts.

So "not running" after a reboot is the expected state, not a fault. Say so, and offer to start them (below), rather than reporting a broken service. `$BH daemon status` prints the setting on every run.

Turning it on is the user's call, not yours: set `"autostart": true` in `bellhop.json`, then reinstall. `daemon status` warns when the installed service files were written under the other setting.

## Install

1. `$BH daemon status`. If already installed with a fresh heartbeat, say so and stop unless the user wants a reinstall (needed after a Bellhop upgrade: status prints STALE, and after an `autostart` change: status says the files were written under the other setting).
2. Check the environment the service will run in: `which python3`, `which claude` (or the lane's `worker_cmd`). Services get a minimal PATH; the installer builds one from these locations, so they must resolve now.
3. `$BH daemon files` — show the user the service files that will be written and where. Explain in two lines: one service runs the watchdog and restarts it if it dies; one runs the sentinel every N minutes. Both point at a vendored copy of Bellhop inside the desk (`.bellhop/`) so plugin upgrades cannot break them. Also say whether autostart is on, in the user's words: "after a reboot you start these yourself" or "these come back at login".
4. `$BH daemon install` — writes the files and prints the load commands. **Ask the user before loading**: this changes their login services. With a yes, run `$BH daemon install --load`.
5. Verify, in this order: `$BH daemon status` says service running; wait up to 30 seconds and check the heartbeat is fresh; `tail -20 bells/logs/daemon.err.log` is empty of errors.
6. Record: `$BH ledger add --title "resident service installed" --body "<platform>, <label>, autostart <on|off>, verified heartbeat at <time>"`.

## After a reboot

1. `$BH daemon status`. Installed but not running, with autostart off, is normal.
2. `$BH daemon install --load` starts them again (it refreshes the vendored copy and runs the load commands). Ask first if anything about the desk changed while the machine was down.
3. Verify the heartbeat as in step 5 above.

## If verification fails

- Heartbeat never appears: read `bells/logs/daemon.err.log`. `python3: command not found` or `claude: command not found` means the PATH baked into the service is wrong; run `$BH daemon install --python <absolute path>` after `which` shows the right binaries, then load again.
- macOS says the service is already loaded: `launchctl bootout gui/$(id -u)/<label>` then load again.
- macOS says the service is disabled and refuses to bootstrap: that is the autostart-off setting; `$BH daemon install --load` runs `launchctl enable` before bootstrapping, so use it rather than a bare `launchctl bootstrap`.
- Linux `systemctl --user` fails with "Failed to connect to bus": the user session has no systemd user instance; enable lingering (`loginctl enable-linger $USER`) with the user's permission, or fall back to session mode.
- Anything else: stop, show the exact error, and offer session mode.

## Session mode (the fallback)

If the user does not want login services, say that the watchdog runs inside the `bellhop` MCP server while a Claude Code session is open in the desk, and stops when the session closes. Nothing to install.

## Remove

`$BH daemon uninstall` unloads and deletes the service files and the marker. With autostart off it also runs `launchctl disable`, so the job goes back to where the plist says it should be. Confirm with the user first.
