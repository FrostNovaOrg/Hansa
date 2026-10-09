# Contributing

- Standard library only.
- Change a task through the desk (`Desk.change`, under the desk lock); the event log is append-only.
- `hansa/card.md` is the coordinator's whole interface: when a change alters what the coordinator can rely on, the card says so. Nobody should need to read the code to use hansa.
- `README.md` is the front page: what hansa is, how to start, where to read more. Keep it short. The details of how hansa behaves go in the matching file under `docs/` (the sandbox, releases and alerts, how a task runs, a Linux server, ...), linked from the README's Documentation table.
- The card is the coordinator's whole interface and must stand alone; `docs/` explains the same behaviour to people. A change to a fact that both state changes both, in the same commit.
- Run `python3 -m unittest tests.test_hansa` before opening a change. It uses a stand-in for `claude`, so no model is called.
- Run the tests as an ordinary user, not as root (in a container, say): as root, `test_a_lane_repository_must_belong_to_the_user_hansa_runs_as` makes hansa switch to another account with `runuser`, which replaces the test process, and the suite stops partway.
- What runs where: before each push the maintainers run the whole suite on macOS and on Linux; every push to `alpha` or `main` runs on GitHub's macOS machine only the tests a sandboxed run cannot (`HANSA_TESTS_ONLY_UNSANDBOXED=1`); every pull request, and a run started by hand, runs the whole suite on Ubuntu and macOS, with Python 3.9 and the newest.
- A test that needs what a sandbox refuses (a local port, a Unix socket, the real srt) calls `skip_unless_ports(self)` or `skip_in_sandbox(self, why)` in its own body, not in a helper it calls: that skips it in a sandbox and is what puts it among the tests every push runs. Run them alone with `HANSA_TESTS_ONLY_UNSANDBOXED=1 python3 -m unittest tests.test_hansa`; the rest skip, saying so.
- The suite must pass on macOS and on Linux, and on GitHub's machines (`.github/workflows/tests.yml`), which have no git identity and a PATH of their own. A test about one system patches the platform (`account.LINUX`, `sys.platform`) rather than assume it; a test that reads the PATH sets its own; a test repository's `git commit` passes `-c user.name=... -c user.email=...`.
- No real product names, paths, or transcripts in this repository.

## Licensing of contributions

Hansa is licensed under the Apache License 2.0. By submitting a contribution you agree that:

1. you have the right to submit it; and
2. it is licensed under the Apache License 2.0, as section 5 of the license says.
