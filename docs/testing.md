# Running the tests

Run from the checkout, as an ordinary user:

```sh
python3 tests/run_parallel.py
python3 tests/run_parallel.py --jobs 4 --slowest 20
python3 tests/run_parallel.py --jobs 2 tests.test_hansa.ParallelRunnerTest
```

The standard-library runner supports Python 3.9 and newer on macOS and Linux. It defaults to
`tests.test_hansa`, with half the machine's CPUs as workers (at least one, at most eight). Names and file
paths are loaded by unittest. Each subprocess takes the next test when it finishes; tests run on the
worker's main thread, so signal handlers work. Module and class fixtures follow unittest's lifecycle
within each worker. Parallel tests must use their own paths and names, and cannot rely on another test's
state. `--jobs 1` preserves unittest's test order and fixture lifecycle.

Progress, failure tracebacks and the final counts use unittest's format, including skips, subtests and
expected failures. This lets `hansa show` extract failures from check logs. `--slowest K` adds timings
after the summary. A worker's unexpected exit becomes an error naming its assigned test; a replacement
takes the remaining tests. INT or TERM reports the tests in progress and stops worker process groups
and their children; the exit status is nonzero. Descendants that start separate sessions are found
through the system process list; when a sandbox refuses that list, cleanup reaches only the worker
process groups. The runner inherits the environment, including
`HANSA_TESTS_ONLY_UNSANDBOXED=1`.

The original command remains available:

```sh
python3 -m unittest tests.test_hansa
```

Both commands use stand-ins for Claude Code; neither calls a model. See [CONTRIBUTING.md](../CONTRIBUTING.md)
for sandbox and platform requirements. Choosing this runner changes no desk's settings or check commands.
