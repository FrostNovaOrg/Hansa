"""Every call that is not a worker run on the Codex provider is the real claude's, exactly."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from .codexkit import FAKES, WRAPPER, shim


class PassThrough(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="codex-adapter-pass-")).resolve()
        self.record = self.dir / "claude-record"
        self.record.mkdir()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def call(self, argv, base_url="", stdin=b"", code=0, **extra):
        env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/"), "HANSA_REAL_CLAUDE": str(FAKES / "claude"),
               "FAKE_CLAUDE_RECORD": str(self.record), "FAKE_CLAUDE_EXIT": str(code), "ODD": "a b\nc'\"$x", **extra}
        if base_url:
            env["ANTHROPIC_BASE_URL"] = base_url
        p = subprocess.Popen([str(WRAPPER), *argv], cwd=str(self.dir), env=env, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = p.communicate(stdin, timeout=120)
        self.assertTrue((self.record / "argv").exists(), f"the real claude was not called: {out!r} {err!r}")
        rec = {"argv": [os.fsdecode(a) for a in (self.record / "argv").read_bytes().split(b"\0")[:-1]],
               "stdin": (self.record / "stdin").read_bytes(), "pid": int((self.record / "pid").read_text()),
               "env": dict(os.fsdecode(x).partition("=")[::2] for x in (self.record / "env").read_bytes().split(b"\0") if x)}
        return p, rec, env

    def assert_exact(self, argv, base_url="", code=0):
        stdin = b"Reply with OK.\n\x00\xff binary \xe2\x9c\x93\n"
        p, rec, env = self.call(argv, base_url, stdin, code)
        self.assertEqual(rec["argv"], argv)
        self.assertEqual(rec["stdin"], stdin)
        self.assertEqual(p.returncode, code)
        self.assertEqual(rec["pid"], p.pid, "exec: nothing stays between hansa and claude")
        for k, v in env.items():
            self.assertEqual(rec["env"].get(k), v, k)
        self.assertEqual(set(rec["env"]) - set(env), set(), "nothing added to the environment")

    def test_other_providers_worker(self):
        self.assert_exact(["-p", "--output-format", "json", "--session-id", "s1", "--max-turns", "5",
                           "--append-system-prompt", "line one\nline \"two\"", "--settings", "{\"a\": 1}", "", "ü ✓"],
                          base_url="https://api.example.com", code=7)

    def test_subscription_worker(self):
        self.assert_exact(["-p", "--output-format", "json", "--session-id", "s2"], code=3)

    def test_version_on_codex_provider(self):
        self.assert_exact(["--version"], base_url=shim.PLACEHOLDER)

    def test_provider_check_on_another_provider(self):
        # hansa's check_provider (-p, json, no session id) on a provider that is not the placeholder; on the
        # placeholder it is the shim's (test_check)
        self.assert_exact(["-p", "--output-format", "json", "--max-turns", "1", "--model", "codex",
                           "--setting-sources", "project,local", "--tools", ""],
                          base_url="https://api.example.com", code=1)

    def test_interactive_session_on_codex_provider(self):
        self.assert_exact(["Start the design session.", "--session-id", "s3", "--permission-mode", "dontAsk"],
                          base_url=shim.PLACEHOLDER)

    def test_text_output_on_codex_provider(self):
        self.assert_exact(["-p", "--output-format", "text", "--session-id", "s4"], base_url=shim.PLACEHOLDER)


class Resume(unittest.TestCase):
    def test_resume_on_codex_provider_is_an_error_result(self):
        env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/"), "ANTHROPIC_BASE_URL": shim.PLACEHOLDER,
               "HANSA_REAL_CLAUDE": "/nonexistent"}
        p = subprocess.run([str(WRAPPER), "-p", "--output-format", "json", "--resume", "sid-9", "--max-turns", "3"],
                           env=env, input=b"go on", capture_output=True, timeout=60)
        run = json.loads(p.stdout)
        self.assertEqual(p.returncode, 1)
        self.assertEqual((run["type"], run["subtype"], run["is_error"], run["session_id"]),
                         ("result", "error_during_execution", True, "sid-9"))
        self.assertIn("resume is not supported on the Codex provider", run["result"])

    def test_call_kinds(self):
        pl = {"ANTHROPIC_BASE_URL": shim.PLACEHOLDER + "/"}
        worker = ["-p", "--output-format", "json", "--session-id", "x"]
        self.assertEqual(shim.call_kind(worker, pl), "codex")
        self.assertEqual(shim.call_kind(["--print", "--output-format=json", "--session-id=x"], pl), "codex")
        self.assertEqual(shim.call_kind(worker, {"ANTHROPIC_BASE_URL": "https://other"}), "pass")
        self.assertEqual(shim.call_kind(worker, {}), "pass")
        self.assertEqual(shim.call_kind(["-p", "--output-format", "json", "--resume", "x"], pl), "resume")
        # a system prompt that says -p is a value, not the option
        self.assertEqual(shim.call_kind(["--append-system-prompt", "-p", "--output-format", "json",
                                         "--session-id", "x"], pl), "pass")


if __name__ == "__main__":
    unittest.main()
