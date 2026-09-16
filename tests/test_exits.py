"""One table of exit codes, and the two ways it goes wrong.

The failure this file exists to prevent is not a crash: it is a number that means one
thing in one verb and another thing in the next. On the desk these tests come from,
`bellhop state inflight` with a bad argument printed usage and exited 128, and three
failures recorded as hard errors turned out to be three typos.

So two guards. Every parser in the CLI is walked with an argument that cannot be valid
and must answer 2, and every `sys.exit(<number>)` in the package must name a code from
the table. The per-verb meanings are checked where those verbs are tested.
"""
import ast
import io
import re
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bellhop import cli, exits  # noqa: E402
from bellhop.util import read  # noqa: E402

BOGUS = "--zzz-not-a-real-flag"


def command_paths(parser, prefix=()):
    """Every `bellhop <verb> [<sub> ...]` path the parser can reach."""
    out = [tuple(prefix)] if prefix else []
    for action in parser._actions:
        choices = getattr(action, "choices", None)
        if not isinstance(choices, dict):
            continue
        for name, sub in choices.items():
            if isinstance(sub, type(parser)):
                out.extend(command_paths(sub, prefix + (name,)))
    return out


class UsageExitTest(unittest.TestCase):
    def run_cli(self, argv):
        """cli.main, with its output swallowed. Returns the exit code it would give."""
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = cli.main(argv)
        except SystemExit as exc:                  # a verb that exits on its own path
            code = exits.normalise_usage_exit(exc) if exc.code in (None, 0, 2) else exc.code
        return code, out.getvalue() + err.getvalue()

    def test_every_parser_answers_a_bad_argument_with_2(self):
        paths = command_paths(cli.build_parser())
        self.assertGreater(len(paths), 40, "the walk found almost no commands; it is not walking")
        for path in paths:
            with self.subTest(command=" ".join(path)):
                code, text = self.run_cli(["--desk", str(ROOT / "no-such-desk"), *path, BOGUS])
                self.assertEqual(code, exits.USAGE,
                                 f"`bellhop {' '.join(path)} {BOGUS}` exited {code}: {text[:300]}")

    def test_an_unknown_verb_is_a_usage_error_and_help_is_not(self):
        self.assertEqual(self.run_cli(["definitely-not-a-verb"])[0], exits.USAGE)
        self.assertEqual(self.run_cli([])[0], exits.USAGE)          # no verb at all
        # `--help` and `--version` leave argparse through the same exception as a bad
        # flag, and they are not failures.
        self.assertEqual(self.run_cli(["--help"])[0], exits.OK)
        self.assertEqual(self.run_cli(["--version"])[0], exits.OK)

    def test_no_desk_is_a_config_error_not_a_usage_error(self):
        code, text = self.run_cli(["--desk", str(ROOT / "no-such-desk"), "card", "--show"])
        self.assertEqual(code, exits.CONFIG, text[:300])
        self.assertIn("bellhop.json", text)

    def test_normalise_usage_exit_covers_what_argparse_can_raise(self):
        self.assertEqual(exits.normalise_usage_exit(SystemExit(None)), exits.OK)
        self.assertEqual(exits.normalise_usage_exit(SystemExit(0)), exits.OK)
        self.assertEqual(exits.normalise_usage_exit(SystemExit(2)), exits.USAGE)
        self.assertEqual(exits.normalise_usage_exit(SystemExit(128)), exits.USAGE)
        self.assertEqual(exits.normalise_usage_exit(SystemExit("bad flag")), exits.USAGE)


class TableTest(unittest.TestCase):
    def test_the_docs_table_is_the_table_from_the_code(self):
        doc = read(ROOT / "docs" / "exit-codes.md")
        self.assertIn(exits.table_rows(), doc,
                      "docs/exit-codes.md is behind bellhop/exits.py")

    def test_every_exit_in_the_package_names_a_code_from_the_table(self):
        allowed = {row[0] for row in exits.TABLE} | {exits.HOOK_BLOCK}
        offenders = []
        for path in sorted((ROOT / "bellhop").glob("*.py")):
            tree = ast.parse(read(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                name = getattr(node.func, "attr", getattr(node.func, "id", ""))
                if name not in ("exit", "_exit"):
                    continue
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, int):
                    if arg.value not in allowed:
                        offenders.append(f"{path.name}:{node.lineno} sys.exit({arg.value})")
        self.assertEqual(offenders, [], "these exits are not in bellhop/exits.py: " + ", ".join(offenders))

    def test_the_modules_that_keep_their_own_names_use_the_table(self):
        from bellhop import closetask, dispatch, headers, state
        self.assertEqual(state.REFUSED, exits.GATE)
        self.assertEqual(state.MALFORMED, exits.USAGE)
        self.assertEqual(state.ENVIRONMENT, exits.NOT_FOUND)
        self.assertEqual(closetask.EXIT_REFUSED, exits.GATE)
        self.assertEqual(closetask.EXIT_MALFORMED, exits.USAGE)
        self.assertEqual(closetask.EXIT_ENVIRONMENT, exits.NOT_FOUND)
        self.assertEqual(dispatch.REFUSED, exits.GATE)
        self.assertEqual(dispatch.BELL_TAKEN, exits.NOT_FOUND)
        self.assertEqual(dispatch.PART_WAY, exits.EXTERNAL)
        self.assertEqual(headers.EXIT_MALFORMED, exits.NOT_FOUND)
        self.assertEqual(headers.EXIT_EXTERNAL, exits.EXTERNAL)

    def test_the_hook_protocol_is_documented_as_the_one_exception(self):
        doc = read(ROOT / "docs" / "exit-codes.md")
        self.assertIn("The hook protocol is not this table", doc)
        self.assertTrue(re.search(r"provider-guard.*hook protocol", doc, re.S | re.I))


if __name__ == "__main__":
    unittest.main()
