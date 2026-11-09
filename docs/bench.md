# The Linux test bench

The bench runs offline Linux server, browser and terminal checks handed in by
workers on a Mac. It is optional and off by default. It needs macOS on Apple silicon,
Lima 2.2.1 or newer, 2 CPUs, 2 GiB RAM and a 20 GiB VM disk. The first build needs
downloads; worker checks run without external network access.

To set it up for the first time, run these commands from the desk's folder, or
give its path with the global `--desk` option:

1. [Install Lima](https://lima-vm.io/docs/installation/) and make sure `limactl`
   is on `hansa serve`'s `PATH`.
2. Run `hansa bench build`. It prepares the VM and leaves the bench off.
3. Run `hansa bench selftest`. Check that the saved acceptance `status` is
   `passed`; `hansa bench selftest --json` gives the same result as JSON.
   Failed or incomplete checks must be resolved before continuing.
4. Run `hansa bench on` to enable worker hand-ins.
5. Run `hansa bench status` to confirm it is on and see its build and acceptance.
   Keep `hansa serve` running to handle hand-ins.

Its fixed Debian 13 VM uses Apple's Virtualization framework (VZ), without host
shares or application forwarding. Claude Code and Codex workers receive access
and instructions when started while it is enabled. A worker started while it is off gets the bench folder
and epoch if resumed after it is turned on, but its pinned system prompt still has
no bench instructions. Codex's macOS-only sandbox guard still applies.

Coordinator commands accept global `--desk`: `hansa bench build`, `rebuild`,
`on`, `off`, `status [--json]`, and `selftest [--json]`. Build, rebuild and selftest
require exclusive access while disabled; rebuilding changes the generation and invalidates acceptance.
`off` records disabled settings even during a hand-in; the broker then cancels and
stops. Temporary management lock contention delays the broker until a later poll.
`on` and serve require complete passing acceptance for the current recipe and generation.
An upgrade that changes the bench's code needs a rebuild and a new selftest;
the recipe covers guest files and the host broker, channel, snapshot, VM, probes and selftest.
Serve treats stale or incomplete acceptance as off and reports the reason once;
`status` also reports why an enabled bench is not in use. The
selftest checks local HTTP, headless Chromium, a terminal and tmux, sandbox filesystem
and network restrictions, account restrictions, the firewall's UID rules and detached
descendant cleanup after completion, timeout, disconnect and gateway death. It also
checks disk, output and artifact limits, malformed requests, DHCP renewal, SSH host
keys, queue cancellation, restart cleanup, idle stop and memory return.
Missing evidence is incomplete. Selftest runs shipped code while disabled,
records every probe and timing across two boots, and requires confirmed VM stop.
Failed or incomplete results keep up to 4 KiB of evidence, with stored provider keys
redacted before the cut; artifacts retain names and sizes, never bytes. Survivor
evidence includes each process's PID and command name.
Saved errors include their class and message, with provider keys redacted. Each
boot must connect and retain its host key; reusing an SSH port is allowed.
Before each boot's Mac/LAN denial probes, root must reach both controlled listeners
through temporary rules limited to root and each listener's address and TCP port.
Each rule is removed in `finally` and its absence checked before denial probes run.
Any control or cleanup failure aborts selftest and stops the VM; rules are never persisted.
Keep these controls while denial probes must distinguish blocking from an unreachable path.
Missing facilities leave acceptance incomplete; `on` checks every required result.
Malformed probe evidence and unexpected sandbox artifacts fail acceptance.
Memory return is checked by the exit of the VM's observed host processes after idle stop.
Do not manufacture an acceptance record.

Provisioning, reset's purge and each gateway slot deletion sync guest writes before
the VM can be force-stopped. Reset still force-stops first to kill hand-in survivors.
Provisioning masks `user@<bench uid>.service`: the bench needs no systemd user
manager, whose delayed shutdown would otherwise leave processes after SSH logout.
Build and rebuild allow one hour, including the first cloud-image download; their
initial Lima start may use that hour. Other calls retain their 900-second cap.
`/work`, `/out`, `/tmp` and `/dev/shm` share a 4 GiB disk limit, including the
snapshot and filesystem overhead. Further writes fail with "No space left on device".
A separate fixed-size ext4 image bounds peak disk growth while leaving the VM root
and gateway's result spool usable. Its `discard` mount releases backing-file blocks
when slots are deleted. Purge preserves the mount and refuses an unmounted store;
the underlying directory is root-only. Selftest fills all four paths past the shared
limit on both boots and requires ENOSPC evidence returned through the gateway.
Keep this bound while hand-ins are untrusted; rebuild existing benches to install it.
Build, rebuild and selftest save and print failure messages with stored provider keys
redacted before the final 4 KiB stderr cut, including on timeout. Lima stderr is
continuously drained into a 64 KiB memory tail so floods cannot fill temporary storage.

Use the enabled bench for Linux server, browser or terminal checks. Write one
shell script that starts everything, waits for readiness, exercises it, and
saves evidence in `/out`. Submit it with:

```sh
hansa bench run --script run.sh --cwd . --timeout 300 --wait 60
```

The worktree copy lands at `/work`; the script starts there, with `--cwd` relative
to it. `--cwd` defaults to `.`.

The command prints a request ID. Wait for `ID.reply` in `HANSA_BENCH_DIR`, without
editing source: copying happens when admitted. Read the reply's `result`; when
it includes `report`, read that JSON file in the same folder. It holds the guest
result, stdout, stderr and an artifact-name mapping. Artifacts appear there
automatically under broker-chosen names. There is no fetch command. These files
last only for the worker run; include findings in the report before ending.

Artifacts have no extensions; the report's artifact mapping gives each original
name. To view an image, first copy it to a name with its extension.

Each hand-in starts fresh with only its own loopback. Downloads, host dependencies,
host environment and caches are unavailable. Git data, credentials and agent
configuration are excluded. Links, special files and detected source changes
are refused. Edit source on the Mac and report Linux-specific limits. Guest state
is deleted after every run. Chromium keeps its own sandbox; do not disable it.

Source change detection compares file identity, mode, link count, owner, size and
modification/change timestamps before and after packing, then scans again. Timestamp
fields expressed in nanoseconds do not guarantee a clock with nanosecond resolution.
A same-size in-place write within one timestamp tick can leave every compared field
unchanged. The bench can then run an older copy or, if a write overlaps a read, a
mixture of contents. These checks do not provide an atomic filesystem snapshot;
leave source unchanged while a submission is admitted and copied.

Comparing content hashes from scans and the bytes packed would catch persistent
same-size changes despite timestamp collisions, at the cost of extra full reads and
hashing. It would still miss a write restored between observations. A guaranteed
stable copy needs all writers excluded while copying (a cooperative lock is not
enough for worker-controlled files), or an atomic filesystem snapshot/clone with
platform support, storage and cleanup costs; per-file clones alone do not make
the whole tree atomic. The mutation test advances mtime explicitly and also runs
with a simulated clock that never ticks, so it tests the metadata guard without
depending on the host clock advancing. Keep that fixture while seals use timestamps.

The guest has Debian 13, Python 3.13, git, headless Chromium, tmux and only DejaVu
fonts, so pages render with different fonts than on a Mac. The copy has no git
data; make a fresh repository if a command needs one.

One request runs, four wait, and each worker may have one outstanding request;
excess requests receive `busy`. Total time defaults to 300 seconds (maximum 900),
queue wait to 60 (maximum 300), bounded by the worker's remaining allowance.
New requests while the bench is off receive `timeout` with a reason saying it is off.
Malformed or unreadable requests receive one `refused` reply and are consumed for
the epoch, so they cannot interrupt polling or repeatedly delay other workers.
The guest gets the request's timeout minus a 30-second allowance for SSH startup,
teardown and return, so a timeout must be longer than the allowance.
Queue, startup and transfer consume the deadline. Existing resource holds prevent
admission. The VM stops after 60 idle seconds to return its memory.

Snapshots are limited to 256 MiB and 50,000 entries. Combined output is capped at
1 MiB; artifacts at 16 MiB each, 64 MiB per run and per worker, and 1,000 entries.
`/work`, `/out`, `/tmp` and `/dev/shm` share a 4 GiB disk limit, including the snapshot and filesystem overhead.
Read script exit status, timeout, truncation, rejected-artifact and infrastructure
notices separately. Partial transfers cannot report success.
Once delivery and its report are complete, the reply retains that report even if
the deadline passed during delivery; this does not invalidate the bench.
Artifact names must be valid UTF-8. Unsupported names set the rejected-artifact
flag; output and supported artifacts still return. Keep this validation while
guest filenames cross the archive boundary.

Serve supervises the broker without waiting on VM operations in its polling loop.
An error in the bench step leaves it off for that round so task handling continues;
serve prints each new reason once. This keeps an optional bench failure from idling the desk.
Worker completion, cancellation and temporary-folder cleanup revoke access before
commit. Pending folder cleanup is retried after revoke completes, without waiting
under the desk lock; the task cannot start again until its earlier cleanup finishes.
A serve restart discards bench work; new submissions use a fresh epoch and
must cross the stop-and-purge barrier. Uncertain cleanup closes the bench. A killed
serve has no watchdog; restart it to recover. `hansa bench on` requires a ready
build and complete passing selftest results for its current recipe and generation.
The selftest exercises the real VM; unit tests use fakes and cannot establish isolation.
