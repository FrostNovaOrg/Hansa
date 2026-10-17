"""HANSA_CURSOR_ENABLE, the Cursor backend's switch, off unless a desk turns it on (README, "The Cursor backend").
While it is unset, or set to anything but `on`, a call on the Cursor provider is the real claude's, exactly, though
every other Cursor setting is set; with it on, the same call is the Cursor backend's. Calls on the Codex provider and
on other providers do not look at it."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from .codexkit import FAKES, WRAPPER, shim
from .test_cursor import CURSOR_URL, ONLY_MACOS, VERSION, CursorScratch, status

# The switch unset, empty (unset, as every setting is when empty), and set to values other than `on`
OFF = (None, "", "off", "1", "true", "yes", "ON")
# The Cursor provider's calls as hansa makes them: a worker run (no sandbox in its --settings, so that the backend
# refuses it before it makes anything; SwitchOnAWorktree runs one on a worktree), hansa's provider check, a resume
WORKER = ["-p", "--output-format", "json", "--session-id", "s1", "--permission-mode", "acceptEdits", "--max-turns", "5",
          "--model", "cursor", "--append-system-prompt", "line one\nline \"two\"", "--settings", "{\"a\": 1}"]
CHECK = ["-p", "--output-format", "json", "--max-turns", "1", "--model", "cursor", "--setting-sources",
         "project,local", "--tools", ""]
RESUME = ["-p", "--output-format", "json", "--resume", "s1", "--max-turns", "3"]
STDIN = b"Reply with the single word OK.\n\x00\xff binary \xe2\x9c\x93\n"


def recorded(folder: Path):
    """What the fake real claude recorded in `folder`: argv, stdin, pid and environment; None when it was not called."""
    if not (folder / "argv").exists():
        return None
    return {"argv": [os.fsdecode(a) for a in (folder / "argv").read_bytes().split(b"\0")[:-1]],
            "stdin": (folder / "stdin").read_bytes(), "pid": int((folder / "pid").read_text()),
            "env": dict(os.fsdecode(x).partition("=")[::2] for x in (folder / "env").read_bytes().split(b"\0") if x)}


class Switch(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="cursor-switch-")).resolve()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.record, self.logs, self.tmp, self.work = (self.dir / n for n in ("claude-record", "logs", "tmp", "work"))
        for d in (self.record, self.logs, self.tmp, self.work):
            d.mkdir()
        self.cursor_record = self.dir / "cursor-record.json"

    def call(self, argv, enable, base_url=CURSOR_URL):
        """bin/claude-or-codex called as hansa calls claude, with every Cursor setting but the switch set, the switch
        as `enable` says (None: unset), and the fakes for the real claude (exit 7) and Cursor."""
        env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/"), "ANTHROPIC_BASE_URL": base_url,
               "ANTHROPIC_AUTH_TOKEN": "placeholder-key", "TMPDIR": str(self.tmp),
               "HANSA_REAL_CLAUDE": str(FAKES / "claude"), "FAKE_CLAUDE_RECORD": str(self.record),
               "FAKE_CLAUDE_EXIT": "7", "HANSA_CURSOR_BIN": str(FAKES / "cursor-agent"),
               "HANSA_CURSOR_VERSION": VERSION, "HANSA_CURSOR_MODEL": "grok-4.7", "HANSA_CURSOR_LOGS": str(self.logs),
               "FAKE_CURSOR_RECORD": str(self.cursor_record), "FAKE_CURSOR_RESULT": "ok", "FAKE_CURSOR_TOOLS": "0",
               "HANSA_CODEX_TEST_NO_WATCHDOG": "1", "PYTHONDONTWRITEBYTECODE": "1", "ODD": "a b\nc'\"$x"}
        if enable is not None:
            env["HANSA_CURSOR_ENABLE"] = enable
        p = subprocess.Popen([str(WRAPPER), *argv], cwd=str(self.work), env=env, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = p.communicate(STDIN, timeout=60)
        return p, out, err.decode(errors="replace"), env

    def assert_real_claudes(self, p, out, err, env, argv):
        got = recorded(self.record)
        self.assertIsNotNone(got, f"the real claude was not called: {out!r} {err}")
        self.assertEqual(got["argv"], argv)
        self.assertEqual(got["stdin"], STDIN)
        self.assertEqual(p.returncode, 7)
        self.assertEqual(got["pid"], p.pid, "exec: nothing stays between hansa and claude")
        self.assertEqual(got["env"], env, "the environment as given: nothing added, nothing taken out")

    def test_off_the_calls_are_the_real_claudes(self):
        for argv in (WORKER, CHECK, RESUME):
            for enable in OFF:
                with self.subTest(argv=argv[:6], enable=enable):
                    shutil.rmtree(self.record)
                    self.record.mkdir()
                    self.assert_real_claudes(*self.call(argv, enable), argv)
                    self.assertFalse(self.cursor_record.exists(), "Cursor never ran")
                    self.assertEqual(list(self.logs.iterdir()) + list(self.tmp.iterdir()), [], "nothing made")

    def test_on_a_check_runs_cursor(self):
        if sys.platform != "darwin":
            self.skipTest(ONLY_MACOS)
        p, out, err, _ = self.call(CHECK, "on")
        self.assertIsNone(recorded(self.record), "the real claude was not called")
        run = json.loads(out)
        self.assertEqual((p.returncode, run["subtype"], run["result"]), (0, "success", "ok"), err)
        self.assertEqual(list(run["modelUsage"]), ["grok-4.7-high"])
        self.assertEqual(json.loads(self.cursor_record.read_text())["argv"][-1], "Reply with the single word: ok")
        self.assertEqual(list(self.tmp.iterdir()), [], "the check's folder is removed")

    def test_on_a_resume_and_a_worker_run_are_the_backends(self):
        for argv, said in ((RESUME, "hansa-cursor: resume is not supported on the Cursor provider"),
                           (WORKER, "hansa-cursor: no sandbox in the --settings hansa gave")):
            with self.subTest(argv=argv[:6]):
                p, out, err, _ = self.call(argv, "on")
                self.assertIsNone(recorded(self.record), "the real claude was not called")
                run = json.loads(out)
                self.assertEqual((p.returncode, run["subtype"], run["session_id"]),
                                 (1, "error_during_execution", "s1"), err)
                self.assertIn(said, run["result"])
        self.assertFalse(self.cursor_record.exists())

    def test_the_switch_from_codex_env(self):
        # hansa gives a worker a clean environment: the switch comes from codex.env (trial.env while there is no
        # codex.env), the environment first
        kept = shim.CODEX_ENV, shim.OLD_ENV
        shim.CODEX_ENV, shim.OLD_ENV = self.dir / "codex.env", self.dir / "trial.env"
        self.addCleanup(lambda: (setattr(shim, "CODEX_ENV", kept[0]), setattr(shim, "OLD_ENV", kept[1])))
        worker = ["-p", "--output-format", "json", "--session-id", "x"]
        for files, given, want in (
                ({}, {}, "pass"),
                ({"codex.env": "HANSA_CURSOR_BIN=/opt/cursor-agent\nHANSA_CURSOR_VERSION=1\n"}, {}, "pass"),
                ({"codex.env": "HANSA_CURSOR_ENABLE=on\n"}, {}, "cursor"),
                ({"codex.env": "HANSA_CURSOR_ENABLE='on'\n"}, {}, "cursor"),
                ({"codex.env": "HANSA_CURSOR_ENABLE=yes\n"}, {}, "pass"),
                ({"codex.env": "HANSA_CURSOR_ENABLE=off\n"}, {"HANSA_CURSOR_ENABLE": "on"}, "cursor"),
                ({"codex.env": "HANSA_CURSOR_ENABLE=on\n"}, {"HANSA_CURSOR_ENABLE": "off"}, "pass"),
                ({"trial.env": "HANSA_CURSOR_ENABLE=on\n"}, {}, "cursor"),
                ({"codex.env": "HANSA_CODEX_MODEL=gpt-y\n", "trial.env": "HANSA_CURSOR_ENABLE=on\n"}, {}, "pass")):
            with self.subTest(files=files, given=given):
                for name in ("codex.env", "trial.env"):
                    (self.dir / name).unlink(missing_ok=True)
                for name, text in files.items():
                    (self.dir / name).write_text(text)
                env = {"ANTHROPIC_BASE_URL": CURSOR_URL, **given}
                self.assertEqual(shim.call_kind(worker, env), want)
                self.assertEqual(shim.call_kind(worker, env, shim.settings(env)), want, "as main asks it")

    def test_codex_and_other_providers_whatever_the_switch(self):
        worker = ["-p", "--output-format", "json", "--session-id", "x"]
        for enable in ("on", None, "off"):
            with self.subTest(enable=enable):
                switch = {} if enable is None else {"HANSA_CURSOR_ENABLE": enable}
                codex = {"ANTHROPIC_BASE_URL": shim.PLACEHOLDER, **switch}
                self.assertEqual(shim.call_kind(worker, codex), "codex")
                self.assertEqual(shim.call_kind(["-p", "--output-format", "json"], codex), "check")
                self.assertEqual(shim.call_kind(RESUME, codex), "resume")
                self.assertEqual(shim.call_kind(worker, {"ANTHROPIC_BASE_URL": "https://api.example.com", **switch}),
                                 "pass")
                self.assertEqual(shim.call_kind(worker, switch), "pass")
        # another provider's worker run, through the wrapper with the switch on: the real claude's, exactly
        self.assert_real_claudes(*self.call(WORKER, "on", base_url="https://api.example.com"), WORKER)
        self.assertFalse(self.cursor_record.exists())


class SwitchOnAWorktree(CursorScratch):
    """A worker run on a worktree, as hansa starts one: the real claude's with the switch unset; with it on, the
    Cursor backend's, whose copy's change comes back."""

    def test_off_then_on(self):
        record = self.root / "claude-record"
        record.mkdir()
        argv, script, task = self.worker_argv(), "printf 'new\\n' > new.txt", "Add new.txt.\n"
        env = self.cursor_env(script, FAKE_CLAUDE_RECORD=str(record), FAKE_CLAUDE_EXIT="7")
        del env["HANSA_CURSOR_ENABLE"]
        p = self.start(argv, env, task=task)
        out, err = p.communicate(timeout=60)
        got = recorded(record)
        self.assertIsNotNone(got, f"the real claude was not called: {out!r} {err!r}")
        self.assertEqual((got["argv"], got["stdin"], got["env"], got["pid"], p.returncode),
                         (argv, task.encode(), env, p.pid, 7))
        self.assertFalse(self.cursor_record.exists(), "Cursor never ran")
        self.assertEqual(status(self.wt), [])
        shutil.rmtree(record)
        record.mkdir()
        run, code = self.run_cursor(script, argv=argv, task=task, FAKE_CLAUDE_RECORD=str(record))
        self.assertEqual((code, run["subtype"]), (0, "success"), run["result"])
        self.assertIsNone(recorded(record), "the real claude was not called")
        self.assertEqual(self.cursor_called()["script_exit"], 0)
        self.assertEqual(status(self.wt), ["?? new.txt"])
