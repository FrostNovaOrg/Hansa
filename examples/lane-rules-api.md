# Lane: api (widget-service)

## Build & test
- `python3 -m unittest discover -s tests -q` from the repository root.
- `./check.sh` is the required-checks script; it must print `check.sh: all green`.

## Do not touch
- `check.sh` itself (changes to checks are their own packet).
- Anything outside `widget/` and `tests/` unless the packet says so.

## Conventions
- One commit per task on the lane branch, message `task <id>: <title>`.
- New behaviour comes with a test in `tests/`.

## Release
- The coordinator tags releases on `main` after `bellhop merge`; workers never tag.
