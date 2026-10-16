"""The Codex adapter's tests, in hansa's suite: tests/run_parallel.py runs them with tests.test_hansa, and
`python3 -m unittest contrib.codex.tests` runs them alone. Fakes stand in for codex, srt and claude: no real Codex,
no network."""
import importlib
from pathlib import Path


def load_tests(loader, tests, pattern):
    for f in sorted(Path(__file__).resolve().parent.glob("test_*.py")):
        tests.addTests(loader.loadTestsFromModule(importlib.import_module(f".{f.stem}", __name__)))
    return tests
