# Contributing

- Standard library only. If a feature needs a dependency, it probably belongs in the adapter layer, not here.
- Every script that changes desk state must be append-only or keep the previous version.
- Run `python3 -m unittest discover tests` and `./examples/demo.sh` before opening a change.
- Keep `template/rules/core.md` under 40 lines. A rule a script can enforce goes in a script.
- No real product names, paths, or transcripts in this repository. The example service is fictional on purpose.
