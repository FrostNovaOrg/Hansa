"""The extension point, and the one property that matters: it cannot take the desk down.

Each failing case here is a real way an add-on breaks - it does not exist, it raises
while being imported, it raises while answering, it calls `sys.exit`, it returns
nonsense. Every one of them has to come out as a single red line with the extension's
name on it, next to a full set of the framework's own checks. The negative control is
that the other checks are still there and `bellhop board` still prints a page: a
sentinel that stopped running and a desk with nothing wrong look alike from outside,
and that is the confusion this whole file exists to prevent.

No extension is named by the core. The fixtures below are written into the sandbox.
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bellhop import board as board_mod  # noqa: E402
from bellhop import extensions as extensions_mod  # noqa: E402
from bellhop import sentinel as sentinel_mod  # noqa: E402
from bellhop.config import DESK_DEFAULTS, Desk  # noqa: E402

WORKING = '''
def checks(desk):
    return [("queue", False, "2 jobs waiting")]


def board_rows(desk):
    return [("H", "== local queue =="), ("S", "2 waiting, 1 unread")]


def fingerprint_parts(desk):
    return ["job-7"]
'''

RAISES_ON_IMPORT = 'raise RuntimeError("the add-on could not start")\n'

RAISES_WHEN_CALLED = '''
def checks(desk):
    raise ValueError("no queue file")


def board_rows(desk):
    raise ValueError("no queue file")


def fingerprint_parts(desk):
    raise ValueError("no queue file")
'''

EXITS = '''
import sys


def checks(desk):
    sys.exit(3)


def board_rows(desk):
    sys.exit(3)
'''

BAD_SHAPES = '''
def checks(desk):
    return ["not a triple"]


def board_rows(desk):
    return ["not a pair"]
'''


class ExtensionCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bellhop-ext-test.")).resolve()
        self.root = self.tmp / "desk"
        (self.root / "state").mkdir(parents=True)
        extensions_mod._loaded.clear()

    def tearDown(self):
        extensions_mod._loaded.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def desk(self, *names):
        return Desk(self.root, {"extensions": list(names)})

    def write_ext(self, name, body):
        d = self.root / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "bellhop_ext.py").write_text(body)
        return d

    # -- the quiet default -------------------------------------------------
    def test_a_desk_with_no_extensions_gets_nothing(self):
        self.assertEqual(DESK_DEFAULTS["extensions"], [])
        desk = Desk(self.root, {})
        self.assertEqual(extensions_mod.checks(desk), [])
        self.assertEqual(extensions_mod.board_rows(desk), [])
        self.assertEqual(extensions_mod.fingerprint_parts(desk), [])

    # -- the working case --------------------------------------------------
    def test_a_working_extension_reaches_checks_board_and_fingerprint(self):
        self.write_ext("addon", WORKING)
        desk = self.desk("addon")
        self.assertEqual(extensions_mod.checks(desk), [("ext:addon:queue", False, "2 jobs waiting")])
        self.assertIn(("S", "2 waiting, 1 unread"), extensions_mod.board_rows(desk))
        self.assertEqual(extensions_mod.fingerprint_parts(desk), ["job-7"])
        self.assertIn("2 waiting, 1 unread", board_mod.board_text(desk, cap=100000))

    def test_an_absolute_path_outside_the_desk_also_loads(self):
        outside = self.tmp / "elsewhere"
        outside.mkdir()
        (outside / "bellhop_ext.py").write_text(WORKING)
        self.assertEqual(extensions_mod.fingerprint_parts(self.desk(str(outside))), ["job-7"])

    # -- every failure is one red line -------------------------------------
    def test_missing_directory_is_one_red_line(self):
        found = extensions_mod.checks(self.desk("nowhere"))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][0], "ext:nowhere")
        self.assertFalse(found[0][1])
        self.assertIn("bellhop_ext.py", found[0][2])

    def test_import_failure_call_failure_exit_and_bad_shape_are_each_one_red_line(self):
        for name, body in (("boom", RAISES_ON_IMPORT), ("angry", RAISES_WHEN_CALLED),
                           ("quitter", EXITS), ("sloppy", BAD_SHAPES)):
            with self.subTest(extension=name):
                extensions_mod._loaded.clear()
                self.write_ext(name, body)
                found = extensions_mod.checks(self.desk(name))
                self.assertEqual(len(found), 1, found)
                self.assertEqual(found[0][0].split(":")[1], name)
                self.assertFalse(found[0][1])
                rows = extensions_mod.board_rows(self.desk(name))
                self.assertEqual(len(rows), 1, rows)
                self.assertTrue(rows[0][1].startswith("RED: extension " + name))

    def test_a_failing_extension_never_moves_the_fingerprint(self):
        self.write_ext("angry", RAISES_WHEN_CALLED)
        desk = self.desk("angry")
        self.assertEqual(extensions_mod.fingerprint_parts(desk), [])
        self.assertEqual(sentinel_mod.board_fingerprint(desk), sentinel_mod.board_fingerprint(Desk(self.root, {})))

    # -- the sentinel and the board survive it -----------------------------
    def test_the_sentinel_keeps_its_own_checks_beside_a_broken_extension(self):
        self.write_ext("boom", RAISES_ON_IMPORT)
        plain = sentinel_mod.run_checks(Desk(self.root, {}), persist=False)
        broken = sentinel_mod.run_checks(self.desk("boom"), persist=False)
        self.assertEqual([c[0] for c in broken][:len(plain)], [c[0] for c in plain])
        self.assertEqual([c[0] for c in broken][len(plain):], ["ext:boom"])

    def test_the_board_still_prints_when_an_extension_fails(self):
        self.write_ext("quitter", EXITS)
        page = board_mod.board_text(self.desk("quitter"), cap=100000)
        self.assertIn("RED: extension quitter", page)
        self.assertIn("== in flight (0) ==", page)


if __name__ == "__main__":
    unittest.main()
