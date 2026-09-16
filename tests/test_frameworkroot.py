"""One framework root, and one seal answer whichever entry point asks.

Three entry points reach this package: `bin/bellhop` in a checkout, the plugin's
`bellhop_main.py` (what MCP and the hooks run), and the copy the daemon vendors into
`<desk>/.bellhop/`. On a production desk `bellhop seal --verify` answered "seals hold"
from one and named the skills, the hooks and the plugin manifest as changed from
another, against the same desk in the same minute, because each worked the root out for
itself from `__file__` and nothing wrote down which one it had picked.

So: the verdict has to match across entry points, the scope each one could see has to be
printed, and the root has to be on the sentinel's page.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bellhop import config, daemon, desk_ops, exits, seal as seal_mod, sentinel  # noqa: E402
from bellhop.config import Desk  # noqa: E402
from bellhop.util import BellhopError, read, read_json, write  # noqa: E402


class ResolverTest(unittest.TestCase):
    """The resolver on its own: what it picks and what it says about it."""

    def test_the_running_package_is_the_default_and_it_is_a_checkout(self):
        root = config.resolve_framework_root({})
        self.assertEqual(root.path, ROOT)
        self.assertEqual(root.kind, config.CHECKOUT)
        self.assertEqual(root.source, "package")
        self.assertEqual(root.covers, config.COVERS_ALL)

    def test_an_explicit_override_wins_and_says_so(self):
        root = config.resolve_framework_root({config.ENV_FRAMEWORK: str(ROOT)})
        self.assertEqual(root.path, ROOT)
        self.assertEqual(root.source, config.ENV_FRAMEWORK)

    def test_an_override_that_is_not_a_framework_is_a_config_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(BellhopError) as caught:
                config.resolve_framework_root({config.ENV_FRAMEWORK: tmp})
            self.assertEqual(caught.exception.code, exits.CONFIG)
            self.assertIn("bellhop/cli.py", str(caught.exception))

    def test_a_plugin_root_pointing_somewhere_else_is_carried_as_a_note(self):
        """Two entry points running two checkouts is worth a person's attention."""
        quiet = config.resolve_framework_root({config.PLUGIN_ENV: str(ROOT)})
        self.assertEqual(quiet.note, "")
        loud = config.resolve_framework_root({config.PLUGIN_ENV: "/somewhere/else"})
        self.assertIn("not the code that is running", loud.note)
        self.assertIn("/somewhere/else", loud.line())

    def test_the_resolver_answers_once_per_process(self):
        self.assertIs(config.framework_root(), config.framework_root())
        self.assertEqual(config.FRAMEWORK_ROOT, config.framework_root().path)


class SameAnswerFromEveryEntryPointTest(unittest.TestCase):
    """The field bug: one desk, three entry points, one verdict."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bellhop-root."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        os.environ["GIT_AUTHOR_NAME"] = os.environ["GIT_COMMITTER_NAME"] = "test"
        os.environ["GIT_AUTHOR_EMAIL"] = os.environ["GIT_COMMITTER_EMAIL"] = "test@example.invalid"
        self.desk_dir = self.tmp / "desk"
        desk_ops.init_desk(self.desk_dir)
        self.desk = Desk(self.desk_dir, read_json(self.desk_dir / "bellhop.json"))
        seal_mod.seal(self.desk)                       # sealed from this checkout
        self.vendored = daemon.vendor(self.desk)

    def entry_points(self):
        return {
            "bin/bellhop": [str(ROOT / "bin" / "bellhop")],
            "plugin": [sys.executable, str(ROOT / "bellhop_main.py")],
            "vendored": [sys.executable, str(self.vendored / "bellhop_main.py")],
        }

    def verify(self, argv):
        env = {k: v for k, v in os.environ.items() if k != config.ENV_FRAMEWORK}
        return subprocess.run([*argv, "--desk", str(self.desk_dir), "seal", "--verify"],
                              capture_output=True, text=True, env=env)

    def verdicts(self):
        return {name: self.verify(argv) for name, argv in self.entry_points().items()}

    def test_every_entry_point_gives_the_same_verdict_on_an_untouched_desk(self):
        out = self.verdicts()
        for name, r in out.items():
            self.assertEqual(r.returncode, exits.OK, f"{name}: {r.stdout}{r.stderr}")
            self.assertIn("seals hold", r.stdout, name)

    def test_every_entry_point_names_the_same_changed_file(self):
        write(self.desk_dir / "rules" / "acceptance-terms.md", "edited behind your back\n")
        out = self.verdicts()
        named = {}
        for name, r in out.items():
            self.assertEqual(r.returncode, exits.RED, f"{name}: {r.stdout}{r.stderr}")
            named[name] = sorted(l.strip() for l in r.stdout.splitlines() if l.startswith("  "))
        self.assertEqual(len(set(map(tuple, named.values()))), 1, named)
        self.assertIn("desk:rules/acceptance-terms.md", next(iter(named.values())))

    def test_each_entry_point_prints_the_root_it_used_and_what_it_could_see(self):
        out = self.verdicts()
        self.assertIn(f"checkout at {ROOT}", out["bin/bellhop"].stdout)
        self.assertIn(f"checkout at {ROOT}", out["plugin"].stdout)
        self.assertIn("vendored at", out["vendored"].stdout)
        self.assertIn("covers package", out["vendored"].stdout)
        self.assertIn("were not checked", out["vendored"].stdout)

    def test_the_vendored_copy_still_catches_a_change_to_the_code_it_runs(self):
        """Narrowing the scope must not be a way of never being red."""
        target = self.vendored / "bellhop" / "ring.py"
        write(target, read(target) + "\n# smuggled in\n")
        r = self.verify(self.entry_points()["vendored"])
        self.assertEqual(r.returncode, exits.RED, r.stdout + r.stderr)
        self.assertIn("framework:bellhop/ring.py", r.stdout)

    def test_the_manifest_records_the_root_and_the_scope(self):
        head = read(seal_mod.manifest_path(self.desk)).splitlines()[:3]
        self.assertIn(f"framework={ROOT}", head[0])
        self.assertIn("root: checkout via package", head[1])
        self.assertIn("covers: all", head[2])

    def test_a_manifest_written_before_the_header_is_read_from_its_contents(self):
        """An upgrade must not turn every sealed desk red for one run."""
        p = seal_mod.manifest_path(self.desk)
        body = [l for l in read(p).splitlines() if not l.startswith("#")]
        write(p, "# sealed 2026-01-01T00:00:00+0800 framework=/old/checkout\n" + "\n".join(body) + "\n")
        report = seal_mod.verify_report(self.desk)
        self.assertEqual(report.changed, [])
        self.assertEqual(report.scope, config.COVERS_ALL)
        self.assertEqual(report.sealed_from, "/old/checkout")

    def test_the_sentinel_page_says_which_root_answered(self):
        line = [c for c in sentinel.run_checks(self.desk, persist=False) if c[0] == "framework"]
        self.assertEqual(len(line), 1, "the sentinel does not record the framework root")
        self.assertTrue(line[0][1], line[0][2])
        self.assertIn(str(ROOT), line[0][2])


if __name__ == "__main__":
    unittest.main()
