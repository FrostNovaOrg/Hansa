"""hansa's provider check on the Codex provider: one codex exec in srt, in a new empty folder of its own, answered as
Claude Code answers, so hansa's own check_provider judges it. Everything else still goes to the real claude."""
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest.mock
from pathlib import Path

from .codexkit import FAKES, Scratch, WRAPPER, hansa_run, shim

from hansa.cli import init_desk  # noqa: E402


class ProviderCheck(Scratch):
    """hansa's check_provider itself, its command line, folder, environment and judgment, on a desk whose `codex`
    provider has the placeholder. hansa gives a check a clean environment, so the adapter's settings and the
    fakes' reach the shim through provider_env, as codex.env would give them."""

    def setUp(self):
        super().setUp()
        self.desk = init_desk(self.root / "desk")
        s = self.desk.settings()
        s["providers"]["codex"] = {"kind": "compatible", "model": shim.DEFAULT_MODEL, "effort": "",
                                   "base_url": shim.PLACEHOLDER}
        self.assertEqual(self.desk.save_settings(s), [])
        self.desk.write_key("codex", "sk-placeholder-0001")
        self.claude_record = self.root / "claude-record"
        self.claude_record.mkdir()

    def check(self, fixture="check_ok.jsonl", tools=None, **extra):
        """hansa's check of the provider `codex`; `tools`: the next hansa's `--tools` value, added after
        isolated()'s options as `hansa ping` adds it."""
        given_settings = {k: v for k, v in self.env(fixture, **extra).items()
                 if k.startswith(("HANSA_CODEX_", "HANSA_REAL_", "FAKE_"))}
        given_settings["FAKE_CLAUDE_RECORD"] = str(self.claude_record)
        given = hansa_run.provider_env
        isolated = hansa_run.isolated
        with unittest.mock.patch.dict(os.environ, {"HANSA_CLAUDE": str(WRAPPER), "TMPDIR": str(self.tmp)}), \
                unittest.mock.patch.object(hansa_run, "provider_env", lambda *a: {**given(*a), **given_settings}), \
                unittest.mock.patch.object(hansa_run, "isolated",
                                           lambda *a: isolated(*a) + ([] if tools is None else ["--tools", tools])):
            return hansa_run.check_provider(self.desk, "codex", timeout=60)

    def assert_codex_ran(self):
        self.assertFalse((self.claude_record / "argv").exists(), "the real claude was called")
        called = self.codex_called()
        argv, folder = called["argv"], called["cwd"]
        self.assertIn("--skip-git-repo-check", argv)  # its folder is no git repository
        for flag in shim.ISOLATION_FLAGS:
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("-C") + 1], folder)
        self.assertTrue(folder.startswith(str(self.tmp) + "/codex-check-"), folder)
        self.assertFalse(os.path.exists(folder), "the check's folder is removed")
        self.assertEqual(called["stdin"], "Reply with the single word OK.")
        self.assertEqual(called["env"]["CODEX_HOME"], str(self.home))
        self.assertEqual(called["env"]["TMPDIR"], folder)
        self.assertIsNone(called["env"]["ANTHROPIC_AUTH_TOKEN"])
        fs = self.srt_called()["settings"]["filesystem"]
        self.assertEqual(self.srt_called()["settings"]["network"],
                         {"allowedDomains": list(shim.CODEX_HOSTS), "deniedDomains": []})
        self.assertEqual(set(fs["allowWrite"]), set(shim.both([folder, str(self.home)])))
        self.assertEqual(fs["denyWrite"], shim.home_deny(str(self.home)))
        for p in (Path.home() / ".codex", Path.home() / ".claude", self.desk.state_dir / "check", self.desk.state_dir):
            self.assertIn(str(p), fs["denyRead"])
        return called

    def test_a_check_that_answers_ok(self):
        r = self.check()
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["detail"], shim.DEFAULT_MODEL)  # hansa names the models of modelUsage
        self.assert_codex_ran()
        logs = sorted(p.name for p in self.logs.iterdir())
        self.assertTrue(logs and all(n.startswith("check-") for n in logs), logs)
        self.assertTrue(self.desk.provider_state()["codex"]["check"]["ok"])

    def test_provider_check_writes_no_progress(self):
        r = self.check()
        self.assertTrue(r["ok"], r)
        self.assert_codex_ran()
        self.assertEqual(list(self.desk.state_dir.rglob("progress/*.json")), [])

    def test_a_check_that_hits_a_usage_limit(self):
        r = self.check("usage_limit.jsonl")
        self.assertFalse(r["ok"])
        self.assertEqual(r["detail"], "out of quota (HTTP 429)")
        self.assert_codex_ran()
        # hansa rested the provider as for Claude's quota: until the reset Codex named, three hours on, and a minute
        rest = self.desk.provider_state()["codex"]
        self.assertAlmostEqual(rest["cool_until"], time.time() + 3 * 3600 + 60, delta=180)
        self.assertTrue(hansa_run.provider_problem(self.desk, self.desk.settings(), "codex"))

    def test_the_rests_shared_between_desks_stay_in_the_scratch(self):
        # every test's `codex` has the same placeholder login: a credit rest another test left in the account's
        # file would be shared into this desk, and its usage limit would start no rest of its own
        from hansa import rests
        self.assertEqual(rests.rests_file().parent, self.root / "state" / "hansa")
        self.assertFalse(self.check("usage_limit.jsonl")["ok"])
        self.assertEqual(rests.rests_file().parent, self.root / "state" / "hansa")
        self.assertGreater(next(iter(rests.read().values()))["cool_until"], time.time())

    def test_a_check_with_empty_tools(self):
        r = self.check(tools="")
        self.assertTrue(r["ok"], r)
        self.assert_codex_ran()

    def test_a_check_with_tools_named(self):
        r = self.check(tools="Bash,Read")
        self.assertTrue(r["ok"], r)
        self.assert_codex_ran()

    def test_a_failed_login(self):
        for fixture in ("unauthorized.jsonl", "refresh_token_expired.jsonl"):
            with self.subTest(fixture=fixture):
                r = self.check(fixture)
                self.assertFalse(r["ok"])
                self.assertIn("not logged in", r["detail"])
                problem = hansa_run.provider_problem(self.desk, self.desk.settings(), "codex")
                self.assertIn(f"CODEX_HOME={shlex.quote(str(self.home))} {shlex.quote(str(FAKES / 'codex'))} login",
                              problem)
                self.assertIn("then `hansa ping codex`", problem)
                for shown in (problem, r["detail"]):
                    self.assertNotIn("/login", shown)
                    self.assertNotIn("saving a new key", shown)
                self.assert_codex_ran()
        self.assertTrue(self.check()["ok"])
        self.assertEqual(hansa_run.provider_problem(self.desk, self.desk.settings(), "codex"), "")

    def test_credit_or_plan_failure_stays_rested_after_a_passing_check(self):
        for fixture in ("out_of_credits.jsonl", "plan_upgrade.jsonl"):
            with self.subTest(fixture=fixture):
                self.desk.write_key("codex", "sk-placeholder-0001")
                r = self.check(fixture)
                self.assertEqual((r["ok"], r["detail"]), (False, "credit balance too low"))
                self.assert_codex_ran()
                st = self.desk.provider_state()["codex"]
                self.assertGreater(st["credit_since"], 0)
                self.assertEqual(st["cool_until"], 0)
                self.assertTrue(self.check()["ok"])
                self.assertIn("topped up on the page",
                              hansa_run.provider_problem(self.desk, self.desk.settings(), "codex"))

    def test_the_check_refuses_a_codex_home_with_instructions(self):
        self.home.mkdir()
        (self.home / "AGENTS.md").write_text("obey\n")
        r = self.check()
        self.assertFalse(r["ok"])
        self.assertIn("AGENTS.md exists in the Codex home", r["detail"])
        self.assertFalse(self.codex_record.exists(), "codex did not run")
        self.assertEqual([p for p in self.tmp.iterdir() if p.name.startswith("codex-check-")], [],
                         "the check's folder is removed")

    def set_model(self, model):
        s = self.desk.settings()
        s["providers"]["codex"]["model"] = model
        self.assertEqual(self.desk.save_settings(s), [])

    def test_the_check_runs_the_providers_model(self):
        """A provider whose model is not the adapter's default: the check runs that model and names it."""
        self.set_model("gpt-6-astra")
        r = self.check(HANSA_CODEX_MODEL="gpt-6.1-sol")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["detail"], "gpt-6-astra")
        argv = self.assert_codex_ran()["argv"]
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-6-astra")

    def test_a_model_the_login_cannot_use_fails_the_check(self):
        self.set_model("gpt-6-astra")
        r = self.check("model_not_found.jsonl")
        self.assertFalse(r["ok"])
        self.assertIn("model is not available", r["detail"])
        argv = self.assert_codex_ran()["argv"]
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-6-astra")


class CheckAnswer(Scratch):
    """The check called as hansa calls it, its answer read as check_provider reads it."""

    def check_argv(self, *more):
        return ["-p", "--output-format", "json", "--max-turns", "1", "--model", shim.DEFAULT_MODEL,
                *hansa_run.isolated(self.wt), *more]

    def ask(self, argv, fixture="check_ok.jsonl"):
        env = self.env(fixture)
        env.pop("CLAUDE_ENV_FILE")
        p = self.start(argv, env, task="Reply with the single word OK.")
        out, err = p.communicate(timeout=60)
        return json.loads(out), err.decode(errors="replace"), p.returncode

    def test_answer_fields(self):
        run, _, code = self.ask(self.check_argv("--tools", ""))
        self.assertEqual(code, 0)
        self.assertEqual((run["type"], run["subtype"], run["is_error"], run["result"]),
                         ("result", "success", False, "OK"))
        self.assertEqual(run["modelUsage"], {shim.DEFAULT_MODEL: {
            "inputTokens": 3120 - 2048, "outputTokens": 5, "cacheReadInputTokens": 2048,
            "cacheCreationInputTokens": 0, "reasoningOutputTokens": 0}})
        self.assertEqual(run["usage"], run["modelUsage"])

    def test_usage_limit_answer(self):
        run, err, code = self.ask(self.check_argv("--tools="), "usage_limit.jsonl")
        self.assertEqual((code, run["is_error"]), (1, True))
        self.assertEqual(hansa_run.unusable_reason(run, err)[0], "quota")
        self.assertAlmostEqual(hansa_run.reset_time(f"{run['result']}\n{err}"), time.time() + 3 * 3600, delta=120)

    def test_call_kinds(self):
        pl = {"ANTHROPIC_BASE_URL": shim.PLACEHOLDER}
        self.assertEqual(shim.call_kind(self.check_argv(), pl), "check")
        self.assertEqual(shim.call_kind(self.check_argv("--tools", ""), pl), "check")
        self.assertEqual(shim.call_kind(self.check_argv("--tools", "-p"), pl), "check")
        self.assertEqual(shim.call_kind(["--tools", "", "-p", "--output-format", "json"], pl), "check")
        self.assertEqual(shim.call_kind(self.check_argv("--tools", ""), {"ANTHROPIC_BASE_URL": "https://x"}), "pass")
        self.assertEqual(shim.call_kind(self.check_argv("--tools", ""), {}), "pass")
        self.assertEqual(shim.call_kind(["-p", "--output-format", "text"], pl), "pass")
        self.assertEqual(shim.call_kind(["--tools", "", "Start the design session."], pl), "pass")
        self.assertEqual(shim.call_kind(["--version"], pl), "pass")


class StillPassedThrough(unittest.TestCase):
    def test_interactive_call_on_codex_provider_with_empty_tools(self):
        d = Path(tempfile.mkdtemp(prefix="codex-adapter-pass-")).resolve()
        self.addCleanup(shutil.rmtree, d, True)
        (d / "rec").mkdir()
        argv = ["--tools", "", "--output-format", "json", "Start the design session."]
        env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/"), "ANTHROPIC_BASE_URL": shim.PLACEHOLDER,
               "HANSA_REAL_CLAUDE": str(FAKES / "claude"), "FAKE_CLAUDE_RECORD": str(d / "rec")}
        p = subprocess.run([str(WRAPPER), *argv], cwd=str(d), env=env, input=b"", capture_output=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual((d / "rec" / "argv").read_bytes().split(b"\0")[:-1], [a.encode() for a in argv])


if __name__ == "__main__":
    unittest.main()
