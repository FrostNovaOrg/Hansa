# Contributing

- Standard library only.
- Change a task through the desk (`Desk.change`, under the desk lock); the event log is append-only.
- `hansa/card.md` is the coordinator's whole interface: when a change alters what the coordinator can rely on, the card says so. Nobody should need to read the code to use hansa.
- `README.md` is the front page: what hansa is, how to start, where to read more. Keep it short. The details of how hansa behaves go in the matching file under `docs/` (the sandbox, releases and alerts, how a task runs, a Linux server, ...), linked from the README's Documentation table.
- The card is the coordinator's whole interface and must stand alone; `docs/` explains the same behaviour to people. A change to a fact that both state changes both, in the same commit.
- Run `python3 -m unittest tests.test_hansa` before opening a change. It uses a stand-in for `claude`, so no model is called.
- Run the tests as an ordinary user, not as root (in a container, say): as root, `test_a_lane_repository_must_belong_to_the_user_hansa_runs_as` makes hansa switch to another account with `runuser`, which replaces the test process, and the suite stops partway.
- The suite must pass on macOS and on Linux, and on GitHub's machines (`.github/workflows/tests.yml`), which have no git identity and a PATH of their own. A test about one system patches the platform (`account.LINUX`, `sys.platform`) rather than assume it; a test that reads the PATH sets its own; a test repository's `git commit` passes `-c user.name=... -c user.email=...`.
- The suite also runs as a lane's check, inside hansa's sandbox on Linux, and must pass there too. There the prelude has already given git an excludes file and a global config, and srt its proxy settings: the suite takes those variables out when it starts (`setUpModule`, keeping `TMPDIR`), so a test that makes its own repository meets what it meets on GitHub's machines. `TMPDIR` is itself a `hansa-tmpfs` there, and srt refuses Unix sockets (a sandbox may refuse ports too): a test that needs one calls `skip_unless_unix_sockets` or `skip_unless_ports`, which skip where they are refused and fail on GitHub's machines (`HANSA_TESTS_NEED_PORTS`, `HANSA_TESTS_NEED_UNIX_SOCKETS`). A test about where python is installed takes it from the python itself.
- No real product names, paths, or transcripts in this repository.

## Licensing of contributions

Hansa is licensed under the Apache License 2.0. By submitting a contribution you agree that:

1. you have the right to submit it; and
2. it is licensed under the Apache License 2.0, as section 5 of the license says.
