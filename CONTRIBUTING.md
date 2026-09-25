# Contributing

- Standard library only.
- Change a task through the desk (`Desk.change`, under the desk lock); the event log is append-only.
- `bh/card.md` is the coordinator's whole interface: when a change alters what the coordinator can rely on, the card says so. Nobody should need to read the code to use bh.
- Run `python3 -m unittest tests.test_bh` before opening a change. It uses a stand-in for `claude`, so no model is called.
- No real product names, paths, or transcripts in this repository.

## Licensing of contributions

Bellhop is dual-licensed (AGPL-3.0-or-later and a commercial license from FrostNova). By submitting a contribution you agree that:

1. you have the right to submit it;
2. it is licensed to the project and to everyone under the AGPL-3.0-or-later; and
3. you grant FrostNova a perpetual, worldwide, royalty-free right to include it in versions of Bellhop distributed under the commercial license.

If you cannot agree to point 3, say so in the pull request and we will discuss it before merging.
