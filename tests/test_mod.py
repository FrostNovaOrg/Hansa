"""The coordinator's mod (hansa/mod/, a Claude Code plugin) in hansa's suite: `claude plugin validate hansa/mod` and
`claude plugin test hansa/mod`, each one test, run by the claude on the PATH. They skip, saying why, where that claude
cannot run them: none on the PATH, one older than the mod's tests need (no `plugin test`), or a sandbox that refuses
what they need. With HANSA_TESTS_NEED_CLAUDE_PLUGIN=1 (as on GitHub's machines, which install Claude Code) a skip fails
instead, so they cannot quietly stop running there. No model is called, and no login is needed."""
import functools
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
import unittest.mock

ROOT = Path(__file__).resolve().parent.parent
MOD = ROOT / "hansa" / "mod"
# Read as the module loads, before a test of the suite puts a stand-in claude on the PATH or changes the environment
CLAUDE = shutil.which("claude")
ENV = dict(os.environ)
NEED = "HANSA_TESTS_NEED_CLAUDE_PLUGIN"
OLDEST = (2, 1, 306)  # the oldest Claude Code that runs the mod's tests (CONTRIBUTING.md)
# Nothing sent to Anthropic: `plugin validate` asks its API for what it need not have
QUIET = {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_AUTOUPDATER": "1"}
TIMEOUT = 900  # seconds; the tests take a few
# A sandbox's refusal as claude reports it: a file, a process or a socket it was not let make
REFUSAL = re.compile(r"operation not permitted|\bEPERM\b|\bEACCES\b|permission denied", re.I)
# What claude says once it has validated, or run the tests: a failure after it is the mod's, never a refusal
REPORTED = re.compile(r"Validation (?:passed|failed)|^Ran \d+ tests? across", re.M)
ONE_LINE = 300


def plugin(claude, *args, timeout=TIMEOUT):
    """`claude plugin ...` in hansa's folder, its output and errors together."""
    return subprocess.run([claude, "plugin", *args], cwd=ROOT, env={**ENV, **QUIET}, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
                          timeout=timeout)


def refused(output) -> str:
    """The line in which a sandbox refused what claude needed, or "" where none did (or claude got as far as a
    result, which is then the mod's own)."""
    if REPORTED.search(output):
        return ""
    line = next((x.strip() for x in output.splitlines() if REFUSAL.search(x)), "")
    return line[:ONE_LINE]


@functools.lru_cache(maxsize=None)
def unusable(claude) -> str:
    """Why this claude cannot run the mod's validation and tests, or "" where it can; asked once for each."""
    if not claude:
        return "no claude on the PATH"
    try:
        r = subprocess.run([claude, "--version"], env={**ENV, **QUIET}, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", timeout=120)
    except PermissionError as e:
        return f"{claude} cannot be started here ({e}; a sandbox refuses to)"
    why = refused(r.stdout) if r.returncode else ""
    if why:
        return f"a sandbox refused what {claude} --version needs: {why}"
    version = re.search(r"(\d+)\.(\d+)\.(\d+)", r.stdout) if r.returncode == 0 else None
    if version and tuple(map(int, version.groups())) < OLDEST:
        return (f"the claude on the PATH is {version[0]}; the mod's tests need Claude Code "
                f"{'.'.join(map(str, OLDEST))} or later")
    r = plugin(claude, "test", "--help", timeout=120)
    if r.returncode != 0 or "plugin test" not in r.stdout:
        why = refused(r.stdout)
        if why:
            return f"a sandbox refused what {claude} plugin test --help needs: {why}"
        return f"the claude on the PATH ({claude}) has no `claude plugin test`: {last_line(r.stdout)}"
    return ""


def last_line(output) -> str:
    lines = [x.strip() for x in output.splitlines() if x.strip()]
    return lines[-1][:ONE_LINE] if lines else "(it said nothing)"


def said(output, most=80) -> str:
    """What a failed run said, without the tests that passed: its failures and its summary."""
    lines = [x for x in output.splitlines() if not x.startswith("(pass)")]
    return "\n".join(lines[-most:])


class ModTest(unittest.TestCase):
    """The mod's own validation and tests, as `claude plugin validate` and `claude plugin test` give them."""

    claude = CLAUDE

    def skip(self, why):
        if os.environ.get(NEED) == "1":
            self.fail(f"{why}, and {NEED}=1 says the coordinator plugin's validation and tests must run")
        self.skipTest(why)

    def plugin(self, command):
        why = unusable(self.claude)
        if why:
            self.skip(why)
        r = plugin(self.claude, command, str(MOD))
        if r.returncode != 0:
            why = refused(r.stdout)
            if why:
                self.skip(f"a sandbox refused what claude plugin {command} needs: {why}")
            self.fail(f"claude plugin {command} hansa/mod exited with status {r.returncode}:\n{said(r.stdout)}")
        return r

    def test_the_coordinators_mod_validates(self):
        r = self.plugin("validate")
        self.assertIn("Validation passed", r.stdout)

    def test_the_coordinators_mod_passes_its_tests(self):
        r = self.plugin("test")
        ran = re.search(r"^Ran (\d+) tests? across", r.stdout, re.M)
        self.assertIsNotNone(ran, said(r.stdout))
        self.assertGreater(int(ran[1]), 0)
        self.assertRegex(r.stdout, r"(?m)^\s*0 fail$")


class ModRunTest(unittest.TestCase):
    """ModTest with stand-ins for claude: what it skips, saying why, and what it fails, naming it."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dir = Path(folder.name)

    def stand_in(self, version="2.1.321 (Claude Code)", help_status=0, validate="", test=""):
        """A claude whose --version, plugin test --help, plugin validate and plugin test say what is given: a body
        of shell for each command."""
        path = Path(tempfile.mkdtemp(dir=self.dir)) / "claude"  # each its own: a test may make several
        path.write_text(textwrap.dedent(f"""\
            #!/bin/sh
            if [ "$1" = --version ]; then echo '{version}'; exit 0; fi
            if [ "$1 $2 $3" = "plugin test --help" ]; then
              echo 'Usage: claude plugin test [dir]'; exit {help_status}
            fi
            if [ "$1 $2" = "plugin validate" ]; then {validate or "echo '✔ Validation passed'"}; exit $?; fi
            if [ "$1 $2" = "plugin test" ]; then {test or "echo '(pass) a'; echo ' 1 pass'; echo ' 0 fail'; echo 'Ran 1 test across 1 file.'"}; exit $?; fi
            exit 9
            """))
        path.chmod(0o755)
        return str(path)

    def run_case(self, claude, need=""):
        case = type("Case", (ModTest,), {"claude": claude})
        result = unittest.TestResult()
        with unittest.mock.patch.dict(os.environ, {NEED: need}):
            unittest.defaultTestLoader.loadTestsFromTestCase(case).run(result)
        return result

    def outcome(self, result):
        return ([why for _, why in result.skipped], [str(t).split()[0] for t, _ in result.failures + result.errors])

    def test_a_claude_that_can_run_them_passes_both(self):
        result = self.run_case(self.stand_in())
        self.assertEqual((result.testsRun, self.outcome(result)), (2, ([], [])))

    def test_no_claude_none_too_old_or_without_plugin_test_skips_both_saying_why(self):
        for claude, why in [(None, "no claude on the PATH"),
                            (self.stand_in(version="2.1.305 (Claude Code)"), "is 2.1.305; the mod's tests need "
                                                                             "Claude Code 2.1.306 or later"),
                            (self.stand_in(help_status=1), "has no `claude plugin test`")]:
            with self.subTest(why=why):
                skipped, failed = self.outcome(self.run_case(claude))
                self.assertEqual(failed, [])
                self.assertEqual(len(skipped), 2)
                for text in skipped:
                    self.assertIn(why, text)

    def test_a_sandbox_that_refuses_what_they_need_skips_them_saying_so(self):
        claude = self.stand_in(validate="echo 'Error: EPERM: operation not permitted, mkdtemp'; exit 1",
                               test="echo 'Error: EPERM: operation not permitted, mkdtemp'; exit 1")
        skipped, failed = self.outcome(self.run_case(claude))
        self.assertEqual(failed, [])
        self.assertEqual(len(skipped), 2)
        for text in skipped:
            self.assertIn("a sandbox refused", text)
            self.assertIn("EPERM: operation not permitted, mkdtemp", text)

    def test_where_they_must_run_a_skip_fails_instead(self):
        result = self.run_case(None, need="1")
        self.assertEqual(self.outcome(result), ([], ["test_the_coordinators_mod_passes_its_tests",
                                                     "test_the_coordinators_mod_validates"]))
        self.assertIn(f"{NEED}=1", result.failures[0][1])

    def test_a_failing_test_of_the_mod_fails_naming_it_whatever_it_says_after(self):
        claude = self.stand_in(test="echo '(pass) a'; echo '(fail) b: it read the settings'; "
                                    "echo '  Error: EPERM: operation not permitted'; echo ' 1 pass'; "
                                    "echo ' 1 fail'; echo 'Ran 2 tests across 1 file.'; exit 1",
                               validate="echo '✘ Validation failed'; exit 1")
        result = self.run_case(claude)
        self.assertEqual(self.outcome(result), ([], ["test_the_coordinators_mod_passes_its_tests",
                                                     "test_the_coordinators_mod_validates"]))
        failures = dict((str(t).split()[0], text) for t, text in result.failures)
        self.assertIn("(fail) b: it read the settings", failures["test_the_coordinators_mod_passes_its_tests"])
        self.assertNotIn("(pass) a", failures["test_the_coordinators_mod_passes_its_tests"])
        self.assertIn("Validation failed", failures["test_the_coordinators_mod_validates"])


if __name__ == "__main__":
    unittest.main()
