# Contributing

- Standard library only. If a feature needs a dependency, it probably belongs in the adapter layer, not here.
- Every script that changes desk state must be append-only or keep the previous version.
- Run `python3 -m unittest discover tests` and `./examples/demo.sh` before opening a change.
- Keep `template/rules/core.md` under 40 lines. A rule a script can enforce goes in a script.
- No real product names, paths, or transcripts in this repository. The example service is fictional on purpose.

## Licensing of contributions

Bellhop is dual-licensed (AGPL-3.0-or-later and a commercial license from FrostNova). By submitting a contribution you agree that:

1. you have the right to submit it;
2. it is licensed to the project and to everyone under the AGPL-3.0-or-later; and
3. you grant FrostNova a perpetual, worldwide, royalty-free right to include it in versions of Bellhop distributed under the commercial license.

If you cannot agree to point 3, say so in the pull request and we will discuss it before merging.
