# Contributing

- Standard library only.
- Change a task through the desk (`Desk.change`, under the desk lock); the event log is append-only.
- `hansa/card.md` is the coordinator's whole interface: when a change alters what the coordinator can rely on, the card says so. Nobody should need to read the code to use hansa.
- `README.md` is the front page: what hansa is, how to start, where to read more. Keep it short. The details of how hansa behaves go in the matching file under `docs/` (the sandbox, releases and alerts, how a task runs, a Linux server, ...), linked from the README's Documentation table.
- The card is the coordinator's whole interface and must stand alone; `docs/` explains the same behaviour to people. A change to a fact that both state changes both, in the same commit.
- Run `python3 -m unittest tests.test_hansa` before opening a change. It uses a stand-in for `claude`, so no model is called.
- Run the tests as an ordinary user, not as root (in a container, say): as root, `test_a_lane_repository_must_belong_to_the_user_hansa_runs_as` makes hansa switch to another account with `runuser`, which replaces the test process, and the suite stops partway.
- The suite must pass on macOS and on Linux, and on GitHub's machines (`.github/workflows/tests.yml`), which have no git identity and a PATH of their own. A test about one system patches the platform (`account.LINUX`, `sys.platform`) rather than assume it; a test that reads the PATH sets its own; a test repository's `git commit` passes `-c user.name=... -c user.email=...`.
- No real product names, paths, or transcripts in this repository.

## Licensing of contributions

Hansa is dual-licensed (AGPL-3.0-or-later and a commercial license from FrostNova). By submitting a contribution you agree that:

1. you have the right to submit it;
2. it is licensed to the project and to everyone under the AGPL-3.0-or-later; and
3. you grant FrostNova a perpetual, worldwide, royalty-free right to include it in versions of Hansa distributed under the commercial license.

If you cannot agree to point 3, say so in the pull request and we will discuss it before merging.
