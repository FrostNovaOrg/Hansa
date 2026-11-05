# The Linux test bench

The bench is off by default. It requires macOS and Lima 2.2.1. Its fixed Debian 13
VM uses VZ, 2 CPUs, 2 GiB RAM and a 20 GiB disk, without host shares or application
forwarding. Claude Code and Codex workers receive access and instructions when
started while it is enabled. A worker started while it is off gets the bench folder
and epoch if resumed after it is turned on, but its pinned system prompt still has
no bench instructions. Codex's macOS-only sandbox guard still applies.

Coordinator commands accept global `--desk`: `hansa bench build`, `rebuild`,
`on`, `off`, `status [--json]`, and `selftest [--json]`. Build, rebuild and selftest
require exclusive access while disabled; rebuilding changes the generation and invalidates acceptance.
`on` requires passing acceptance for the current recipe and generation. The
shipped acceptance fixtures cover sandbox capabilities, account restrictions,
the UID-first edge and detached descendants after completion, timeout, disconnect
and gateway death. Missing evidence is incomplete; these isolation gates remain
mandatory while guest execution is supported. Selftest runs shipped code while disabled,
records every probe and timing across two boots, and requires confirmed VM stop.
Saved errors include their class and message, with provider keys redacted. Each
boot must connect and retain its host key; reusing an SSH port is allowed.
Missing facilities leave acceptance incomplete; `on` checks every required result.
Malformed probe evidence and unexpected sandbox artifacts fail acceptance.
Memory return is checked by the exit of the VM's observed host processes after idle stop.
Do not manufacture an acceptance record.

Use the enabled bench for Linux server, browser or terminal checks. Write one
shell script that starts everything, waits for readiness, exercises it, and
saves evidence in `/out`. Submit it with:

```sh
hansa bench run --script run.sh --cwd . --timeout 300 --wait 60
```

The command prints a request ID. Wait for `ID.reply` in `HANSA_BENCH_DIR`, without
editing source: copying happens when admitted. Read the reply's `result`; when
it includes `report`, read that JSON file in the same folder. It holds the guest
result, stdout, stderr and an artifact-name mapping. Artifacts appear there
automatically under broker-chosen names. There is no fetch command. These files
last only for the worker run; include findings in the report before ending.

Each hand-in starts fresh with only its own loopback. Downloads, host dependencies,
host environment and caches are unavailable. Git data, credentials and agent
configuration are excluded. Links, special files and files changing while copied
are refused. Edit source on the Mac and report Linux-specific limits. Guest state
is deleted after every run. Chromium keeps its own sandbox; do not disable it.

One request runs, four wait, and each worker may have one outstanding request;
excess requests receive `busy`. Total time defaults to 300 seconds (maximum 900),
queue wait to 60 (maximum 300), bounded by the worker's remaining allowance.
New requests while the bench is off receive `timeout` with a reason saying it is off.
The guest gets the request's timeout minus a 30-second allowance for SSH startup,
teardown and return, so a timeout must be longer than the allowance.
Queue, startup and transfer consume the deadline. Existing resource holds prevent
admission. The VM stops after 60 idle seconds to return its memory.

Snapshots are limited to 256 MiB and 50,000 entries. Combined output is capped at
1 MiB; artifacts at 16 MiB each, 64 MiB per run and per worker, and 1,000 entries.
Read script exit status, timeout, truncation, rejected-artifact and infrastructure
notices separately. Partial transfers cannot report success.

Serve supervises the broker without waiting on VM operations in its polling loop.
An error in the bench step leaves it off for that round so task handling continues;
serve prints each new reason once. This keeps an optional bench failure from idling the desk.
Worker completion, cancellation and temporary-folder cleanup revoke access before
commit. A serve restart discards bench work; new submissions use a fresh epoch and
must cross the stop-and-purge barrier. Uncertain cleanup closes the bench. A killed
serve has no watchdog; restart it to recover. Security review and real-VM acceptance
remain required before `hansa bench on`; a disabled build may be prepared first.
Unit tests use fakes and cannot establish isolation.
