# Changelog

Up to 0.4 this project was called Bellhop, and its command was `bellhop`. The
entries for those versions keep the names of their time. Commits up to 9 October 2026 carry the licence of their time, the AGPL-3.0-or-later with a commercial licence; since then Hansa has been under the Apache License 2.0 alone.

## 0.5.0a1 — alpha, unreleased

Hansa is a rewrite: one directory holds a desk, with a command line for the coordinator,
a web page for the principal (the person directing the work), and task workers. Apache License 2.0.

### Tasks and workers

- Queue work on lanes, each with its own git worktree, providers, instructions and limits.
  Several lanes can work at once; the coordinator sets concurrency and decides what to merge.
  Workers change files and report; hansa commits their work and cleans up processes.
- Retry with a note, updating from the base or moving lanes on the same repository and base; findings carry over.
- Request verification by another worker, optionally on another lane or model. `add --verify`
  and `retry --verify` queue it after a passing check, or with no check, for completed work
  with changes. “Holds, with notes” permits merging; saved notes can be tracked as known gaps.
- Session notifications bring results to the coordinator. `hansa ask` keeps the principal's to-dos;
  answers happen in conversation and the coordinator records the outcome. `hansa wait` waits
  for tasks only. See [task handling](docs/tasks.md).

### The sandbox

- Worker commands run in an operating-system sandbox with lane-specific network hosts and write
  folders. Setup, checks and lane tries use srt; commands cannot fall back to running outside it.
- Runs cannot read the desk's private files, other runs' temporary folders or common credentials.
  Listed desks are kept apart; an unusable account desk list holds runs. The principal can add
  read restrictions; invalid rules stop new runs, while repository or PATH conflicts are reported.
- Lane caches stay separate from installed tools and shared configuration. Lane environment settings
  reach sandboxed commands, not hansa itself. Git metadata and protected paths are checked.
- Runs get private temporary storage; Linux lane commands normally use the desk's disk.
  Java and Maven receive temporary-directory and proxy settings; sandbox startup failures hold workers.
  See [the sandbox guide](docs/sandbox.md).

### Merging and checks

- A task's check runs after its worker. A merge check tests the combined tree when the base has
  changes absent from the task, or a separate command is needed, with its own limit or check lane.
- Merges queue per repository and base. Compatible merges can share a check; a failed batch
  is checked again individually in its original order.
- `hansa merge --recheck` checks again; `--despite-check` can override a check failure.
  Stored keys and conflict markers always block merging. Configured release files require
  a holding verification, including “holds, with notes”.
- **Forbidden words support safe `re:` patterns.** Optional mailbox rules also check outgoing changes
  at merge and release. `hansa lane try` tests a lane without a worker.
- Push checks run on GitHub's Ubuntu machine for tests a sandbox cannot run, plus adapter and plugin
  checks. Pull requests run the full suite on Ubuntu; manual runs add macOS and its real sandbox
  runtime. See [contributing](CONTRIBUTING.md).

### Releases and alerts

- Named targets have commands, time limits and optional dependencies on another target releasing
  the same commit. Requests show outgoing changes and direct commits, which can require approval.
- Approve on the page or pass the one-time code from the principal's alert through the coordinator.
  A target ticked on the page may run without asking, including after merges settle; changes
  to commands, inputs or permissions invalidate approval.
- Release commands run outside the sandbox. Ordinary releases hold their repository; fixed-checkout
  targets let merges continue. Releases can be stopped; results return to the coordinator.
- The principal sets the alert command. Alerts are grouped and rate-limited; closed to-dos are omitted
  and failures shown. Low disk space holds new work until space recovers, with a floor scaled to
  the volume. See [releases and alerts](docs/releases.md).
- `hansa changelog` collects workers' release notes, missing notes and direct commits.

### The web page

- English and Chinese views show the pipeline, active lanes, waiting decisions and searchable tasks.
  Task details show each step, reports, checks and verification. Notes and known gaps are readable.
- Work and cost includes a work calendar, lane timeline, outcomes, token usage and CSV export.
  All attempts count; whole history includes identifiable coordinator and helper sessions, including
  recorded working folders outside the desk. Missing usage and unpriced models are explained.
- Settings group providers, coordinator, lanes, read restrictions, releases, alerts and prices.
  Lanes are read-only; dispatch controls are in Settings. Drafts survive tab changes and failed saves;
  concurrent settings changes are shown before another save.
- Each tab keeps its token in `sessionStorage` and sends `Authorization: Bearer` on API requests;
  changes also need `X-Hansa: 1`. The token is removed from the address; cookies do not authenticate requests.
  See [the desk guide](docs/desk.md).

### Providers and usage limits

- Use Claude's login or compatible providers, with per-provider models, effort and ordered lane
  selection. Known keys are redacted from published text and logs.
- Usage limits requeue work and rest the provider until its reported reset, or a configured rest.
  Desks sharing a login share usage and credit rests; confirming a top-up clears shared credit rests.
- Connection failures hold the provider until a check answers, then resume tasks from safe saved
  commits. Repeated counted failures stop the task; a system-killed worker can restart once.
- `hansa ping` uses the server's recorded Claude program, including an adapter; checks use no tools.

### The terminal menu and `hansa doctor`

- In a terminal, `hansa` or `hansa up` outside a desk opens the desk menu: select desks to start,
  join a coordinator, open the page, pause dispatch or stop a server. `hansa desks` lists desk state;
  `hansa up --all` starts all listed desks, restoring recorded server and coordinator starts.
- Serving without an explicit port finds a free one and remembers it. A symbolic link to `bin/hansa`
  on PATH works. [Getting started](docs/getting-started.md) covers local and SSH use.
- `hansa doctor` checks tools, login and sandbox operation, with fixes, JSON output and English or Chinese.

### The Codex adapter

- The optional macOS adapter runs Codex workers inside srt, translates results and live progress
  for hansa, and passes other calls to Claude. [Adapter setup and limits](contrib/codex/README.md)
  covers its own login and configuration, provider errors, login access inside its sandbox and tool-rule limits.

## 0.4.0 — 2026-09-17

- Bellhop bound acceptance to the packet a worker actually read, with upgrade recovery.
- Added verifier preflight checks, worker health alerts, quota handling and consistent exit codes.
- Supported concurrent lanes, defaulting to one worker.
- Tightened coordinator write gates and isolated the optional provider bridge.

## 0.3.0 — 2026-09-11

- Bellhop added provider adapters, model schedules, permission guards and sandbox profiles.
- Made dispatch, acceptance and closing resumable, with machine-readable receipts and verdicts.
- Ran verification in disposable copies.
- Checked merged trees before moving the base, and added lane recovery commands.
- Added guarded service wake-ups, a principal's brief and resumable release steps.

## 0.2.0

Bellhop became a Claude Code plugin with coordinator slash commands and helpers.

- Added a stdio MCP server and a watchdog that worked while a session was open.
- Added launchd and systemd installers for resident watchdog and sentinel services.
- Session hooks showed desk status and warned about pending work without a watchdog.

## 0.1.0

Bellhop's first release established the principal, coordinator and worker roles.

- A desk template and watchdog sent tasks to fresh workers in lane worktrees.
- Receipts, sampled acceptance and sentinel checks made completed work reviewable.
- Checksums protected scripts and frozen documents; a dry-run demo used stub workers.
