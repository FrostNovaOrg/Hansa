"""hansa can explicitly select the adapter without the legacy placeholder address."""
import json

from .codexkit import Scratch, shim


class ExplicitSwitch(Scratch):
    def setUp(self):
        super().setUp()
        self.claude_record = self.root / "claude-record"
        self.claude_record.mkdir()

    def ask(self, argv, switch="1", base_url=None, **extra):
        env = self.env("check_ok.jsonl", HANSA_CODEX_RUN=switch,
                       FAKE_CLAUDE_RECORD=str(self.claude_record), **extra)
        if base_url is None:
            env.pop("ANTHROPIC_BASE_URL")
        else:
            env["ANTHROPIC_BASE_URL"] = base_url
        proc = self.start(argv, env, task="Reply with OK.")
        out, err = proc.communicate(timeout=60)
        self.assertTrue(out, err.decode(errors="replace"))
        return json.loads(out), proc.returncode

    def test_switch_selects_worker_regardless_of_address(self):
        for base_url in (None, "", "https://other.example", shim.PLACEHOLDER):
            with self.subTest(base_url=base_url):
                run, code = self.ask(self.worker_argv(), base_url=base_url)
                self.assertEqual((code, run["subtype"], run["session_id"]), (0, "success", self.sid))
                self.assertEqual(self.codex_called()["cwd"], str(self.wt))
                self.assertNotIn("--skip-git-repo-check", self.codex_called()["argv"])
                self.assertFalse((self.claude_record / "argv").exists())

    def test_switch_selects_check_regardless_of_address(self):
        for base_url in (None, "", "https://other.example", shim.PLACEHOLDER):
            with self.subTest(base_url=base_url):
                run, code = self.ask(["-p", "--output-format", "json"], base_url=base_url)
                self.assertEqual((code, run["subtype"], run["result"]), (0, "success", "OK"))
                self.assertIn("--skip-git-repo-check", self.codex_called()["argv"])
                self.assertNotEqual(self.codex_called()["cwd"], str(self.wt))
                self.assertFalse((self.claude_record / "argv").exists())

    def test_switch_resume_aliases_get_the_legacy_error(self):
        for flag in ("--resume", "-r", "--continue", "-c"):
            with self.subTest(flag=flag):
                run, code = self.ask([flag, "sid-9"])
                legacy, legacy_code = self.ask([flag, "sid-9"], switch="0", base_url=shim.PLACEHOLDER)
                self.assertEqual((code, run), (legacy_code, legacy))
                self.assertEqual((code, run["subtype"]), (1, "error_during_execution"))
                self.assertIn("resume", run["result"])
                self.assertIn("not supported", run["result"])
                self.assertFalse((self.claude_record / "argv").exists())
                self.assertFalse(self.srt_record.exists())
                self.assertFalse(self.codex_record.exists())

    def test_switch_rejects_calls_other_than_runs_and_checks(self):
        for argv in ([], ["Start the design session."], ["--version"],
                     ["-p", "--output-format", "text"],
                     ["--append-system-prompt", "-p", "--output-format", "json"]):
            for base_url in (None, shim.PLACEHOLDER):
                with self.subTest(argv=argv, base_url=base_url):
                    run, code = self.ask(argv, base_url=base_url)
                    self.assertEqual((code, run["subtype"]), (1, "error_during_execution"))
                    for word in ("adapter", "neither", "run", "check"):
                        self.assertIn(word, run["result"])
                    self.assertFalse((self.claude_record / "argv").exists())
                    self.assertFalse(self.srt_record.exists())
                    self.assertFalse(self.codex_record.exists())

    def test_switch_is_dropped_from_codex_environment_in_both_modes(self):
        for switch in ("1", "0"):
            for argv in (self.worker_argv(), ["-p", "--output-format", "json"]):
                with self.subTest(switch=switch, argv=argv):
                    run, code = self.ask(argv, switch=switch, base_url=shim.PLACEHOLDER)
                    self.assertEqual((code, run["subtype"]), (0, "success"))
                    self.assertIsNone(self.codex_called()["env"]["HANSA_CODEX_RUN"])

    def test_only_one_turns_the_switch_on(self):
        worker = self.worker_argv()
        for switch in ("", "0", "true", "yes", "01", "1 "):
            with self.subTest(switch=switch):
                self.assertEqual(shim.call_kind(worker, {"HANSA_CODEX_RUN": switch}), "pass")
                self.assertEqual(shim.call_kind(worker, {"HANSA_CODEX_RUN": switch,
                                                        "ANTHROPIC_BASE_URL": shim.PLACEHOLDER}), "codex")
