# Contributing

Open an issue to ask a question or discuss a contribution. For security reports,
follow [SECURITY.md](SECURITY.md).

- Standard library only.
- `CHANGELOG.md` is written once per release, from the lines `hansa changelog` collects; a change does not add to it. The union driver in `.gitattributes` keeps two edits from conflicting.
- Change a task through the desk (`Desk.change`, under the desk lock); the event log is append-only.
- `hansa/card.md` and `hansa help` are the coordinator's interface. Document changes to what the coordinator can rely on in the relevant `hansa help` topic. Change the card only when the coordinator must follow the change on every turn, as the next bullet explains. Most changes leave the card alone. Nobody should need to read the code to use hansa.
- `README.md` is the front page: what hansa is, how to start, where to read more. Keep it short. The details of how hansa behaves go in the matching file under `docs/` (the sandbox, releases and alerts, how a task runs, a Linux server, ...), linked from the README's Documentation table.
- The coordinator carries the whole card on every turn, so keep only standing instructions there. Put details once in `docs/`, reached by `hansa help <topic>`, with a pointer in the card (`More: hansa help TOPIC`). If the card and a document state the same fact, change both in the same commit.
- Document each new check or rule in hansa with the kind of problem it guards against and when it may be removed. A check that would need a check of its own belongs in the tests instead.
- Keep private identities, deployment details and real transcripts out of this repository. Supported tools, ordinary platform paths and invented protocol fixtures are fine.

## Tests

### Running the tests here

- Run `python3 tests/run_parallel.py` (or `python3 -m unittest tests.test_hansa`) before opening a pull request; both use a stand-in for `claude`, so no model is called. The runner spreads the tests over processes (`--jobs N`, by default half the CPUs, at most 8), keeps unittest's output, and lists the slowest with `--slowest K`.
- The runner also checks local Markdown links; see [Documentation tests](docs/testing.md) for its scope and the focused command.
- The coordinator's mod (`hansa/mod/`, a Claude Code plugin) has tests of its own, in Claude Code's test kit: after a change there, run `claude plugin validate hansa/mod` and `claude plugin test hansa/mod` (Claude Code 2.1.290 or later; no model is called).
- The optional Codex adapter (`contrib/codex`) has tests of its own, which `tests/run_parallel.py` runs after `tests.test_hansa` (alone: `python3 -m unittest contrib.codex.tests`). Fakes stand in for codex, srt and claude; a test that starts a Codex worker run skips off macOS, where the adapter refuses to run one.
- Run the tests as an ordinary user, not as root (in a container, say): as root, `test_a_lane_repository_must_belong_to_the_user_hansa_runs_as` makes hansa switch to another account with `runuser`, which replaces the test process, and the suite stops partway.
- Before each push the maintainers run the whole suite on macOS and on Linux.

### What GitHub's checks run

- Every CI job also runs the adapter's suite and `claude plugin validate hansa/mod` and `claude plugin test hansa/mod`, with no model or login. Adapter worker runs skip on Ubuntu; the manual macOS jobs exercise them with fakes. The pinned tool baseline and upgrade checks are in [Tools and upgrades](docs/sandbox.md#tools-and-upgrades).
- On GitHub's Ubuntu machine, every push to `alpha` or `main` runs only the main tests that cannot run in a sandbox: those that listen on the loopback or make a Unix socket (`HANSA_TESTS_ONLY_UNSANDBOXED=1`; not the real srt, which GitHub's Ubuntu machines cannot start), plus the adapter and plugin checks above.
- Every pull request runs the whole suite on Ubuntu, with Python 3.9 and the latest version.
- GitHub counts macOS minutes ten times, so macOS runs only in a manually started workflow (Actions, tests, Run workflow): the whole suite on Ubuntu and macOS with both Python versions. That is the only run that tests the real srt on macOS; start it before a release whose changes touch the sandbox (the settings hansa gives srt or a worker's sandbox) or the web page.

### Sandbox tests

- When a sandbox refuses something a test needs (a local port, a Unix socket, the real srt), the test calls `skip_unless_ports(self)` or `skip_in_sandbox(self, why)` in its own body, not in a helper it calls: this skips it in a sandbox and includes it among the tests every push runs. Run them alone with `HANSA_TESTS_ONLY_UNSANDBOXED=1 python3 tests/run_parallel.py` (or `HANSA_TESTS_ONLY_UNSANDBOXED=1 python3 -m unittest tests.test_hansa`); the rest skip, saying so.
- The suite must pass on macOS and Linux, including on GitHub's machines (`.github/workflows/tests.yml`), which have no git identity and a PATH of their own. A test about one system patches the platform (`account.LINUX`, `sys.platform`) rather than assuming it; a test that reads the PATH sets its own; a test repository's `git commit` passes `-c user.name=... -c user.email=...`.
- The suite also runs as a lane's check inside hansa's sandbox on Linux and must pass there. The prelude has already set git's excludes file and global config, and srt's proxy settings. At startup the suite removes those variables (`setUpModule`, keeping `TMPDIR`), so a test that creates a repository sees the same environment as on GitHub's machines. `TMPDIR` is the check's own folder in the desk's `.hansa/tmp`; srt refuses Unix sockets, and a sandbox may refuse ports too. A test needing a Unix socket asks `unix_sockets_refused()` and, if it returns a reason, calls `skip_in_sandbox(self, why, *NEED_UNIX_SOCKETS)`; one needing a port calls `skip_unless_ports(self)`. Both skip where they are refused and fail on GitHub's machines (`HANSA_TESTS_NEED_UNIX_SOCKETS`, `HANSA_TESTS_NEED_PORTS`). A test that needs Python's install location takes it from Python itself.

## Licensing of contributions

Hansa is licensed under the Apache License 2.0. By submitting a contribution you agree that:

1. you have the right to submit it; and
2. it is licensed under the Apache License 2.0, as section 5 of the license says.
