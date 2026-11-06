"""The Codex adapter's tests, in hansa's suite: tests/run_parallel.py runs them with tests.test_hansa, and
`python3 -m unittest contrib.codex.tests` runs them alone. Fakes stand in for codex, srt and claude: no real Codex,
no network."""
import importlib
import sys
from pathlib import Path

# The suite's git settings (tests/git_env.py), as tests.test_hansa gives them, for a run of these alone too.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tests import git_env  # noqa: E402

git_env.apply()


def load_tests(loader, tests, pattern):
    for f in sorted(Path(__file__).resolve().parent.glob("test_*.py")):
        tests.addTests(loader.loadTestsFromModule(importlib.import_module(f".{f.stem}", __name__)))
    return tests
