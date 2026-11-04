"""A worker run on the Codex provider: the codex command, and an answer hansa reads as it reads Claude's."""
import json
import os

from .codexkit import FIXTURES, Scratch, hansa_run, shim


class CodexRun(Scratch):
    def test_success_answer_reads_as_hansa_reads_it(self):
        run, code = self.run_worker("success.jsonl")
        self.assertEqual(code, 0)
        self.assertEqual((run["type"], run["subtype"], run["is_error"]), ("result", "success", False))
        self.assertEqual(run["result"], "Added hello.txt with a greeting; checked it with cat. Nothing left undone.")
        self.assertEqual(run["session_id"], self.sid)
        self.assertEqual(run["num_turns"], 3)  # two commands and a file change
        self.assertEqual((run["total_cost_usd"], run["permission_denials"]), (0, []))
        want = {"inputTokens": 24763 - 24448, "outputTokens": 122, "cacheReadInputTokens": 24448,
                "cacheCreationInputTokens": 0, "reasoningOutputTokens": 0}
        self.assertEqual(run["modelUsage"], {shim.DEFAULT_MODEL: want})
        self.assertEqual(run["usage"], {shim.DEFAULT_MODEL: want})
        self.assertEqual(hansa_run.usage_of(run), {shim.DEFAULT_MODEL: {
            "in": 315, "out": 122, "cache_read": 24448, "cache_write": 0, "claude_code_usd": None, "basis": ""}})
        self.assertEqual(hansa_run.unusable_reason(run, self.err), ("", ""))
        self.assertEqual(hansa_run.network_reason(run, self.err), "")

    def test_codex_command(self):
        self.run_worker("success.jsonl")
        called = self.codex_called()
        argv = called["argv"]
        system = "You are a worker on task t1.\nSay \"done\" when done; C:\\path stays."
        out = argv[argv.index("-o") + 1]
        prefix = called["env"]["TMPPREFIX"]
        self.assertEqual(argv, ["exec", "--json", "--ignore-user-config", "--ignore-rules", "--ephemeral",
                                "--strict-config", "-C", str(self.wt), "-m", shim.DEFAULT_MODEL,
                                "-c", 'model_reasoning_effort="high"',
                                "-c", "developer_instructions=" + json.dumps(system, ensure_ascii=False),
                                "-c", f"shell_environment_policy.set.TMPPREFIX={json.dumps(prefix)}",
                                "-c", "analytics.enabled=false", "-c", 'approval_policy="never"',
                                "-c", 'approvals_reviewer="user"',
                                "-c", "check_for_update_on_startup=false",
                                "-c", 'web_search="disabled"', "--disable", "apps", "--disable", "plugins",
                                "--disable", "browser_use", "--disable", "computer_use",
                                "--disable", "image_generation", "--disable", "multi_agent", "--disable", "hooks",
                                "-s", "danger-full-access", "-o", out, "-"])
        # the value is a TOML basic string; these escapes read the same in JSON
        self.assertEqual(json.loads(argv[argv.index("-c", 12) + 1].split("=", 1)[1]), system)
        self.assertTrue(out.startswith(str(self.tmp) + "/"), "the last message is written in the run's temporary folder")
        self.assertEqual(called["stdin"], "Add hello.txt.\n")
        self.assertEqual(called["cwd"], str(self.wt))
        self.assertEqual(called["env"]["CODEX_HOME"], str(self.home))
        self.assertEqual(called["env"]["FROM_LANE_ENV"], "lane value", "the lane's env, as Claude's commands get it")
        for k in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "HANSA_CODEX_BIN", "CLAUDE_ENV_FILE"):
            self.assertIsNone(called["env"][k], k)
        self.assertFalse(os.path.exists(out), "the run's own files are removed")

    def ran_model(self, run):
        """The model codex was given (-m), and the one the answer's usage is under."""
        called = self.codex_called()["argv"]
        self.assertEqual(list(run["modelUsage"]), list(run["usage"]))
        return called[called.index("-m") + 1], list(run["modelUsage"])

    def test_the_providers_model_runs(self):
        run, code = self.run_worker("success.jsonl", argv=self.worker_argv(model="gpt-6-astra"))
        self.assertEqual(code, 0)
        self.assertEqual(self.ran_model(run), ("gpt-6-astra", ["gpt-6-astra"]))
        self.assertEqual(list(hansa_run.usage_of(run)), ["gpt-6-astra"])

    def test_the_providers_model_over_the_setting(self):
        run, _ = self.run_worker("success.jsonl", argv=self.worker_argv(model="gpt-6-astra"),
                                 HANSA_CODEX_MODEL="gpt-6.1-sol")
        self.assertEqual(self.ran_model(run), ("gpt-6-astra", ["gpt-6-astra"]))

    def test_the_setting_when_hansa_passes_no_model(self):
        run, _ = self.run_worker("success.jsonl", argv=self.worker_argv(model=None), HANSA_CODEX_MODEL="gpt-6-astra")
        self.assertEqual(self.ran_model(run), ("gpt-6-astra", ["gpt-6-astra"]))

    def test_the_default_when_neither(self):
        run, _ = self.run_worker("success.jsonl", argv=self.worker_argv(model=None))
        self.assertEqual(self.ran_model(run), (shim.DEFAULT_MODEL, [shim.DEFAULT_MODEL]))

    def test_the_providers_model_not_available(self):
        run, code = self.run_worker("model_not_found.jsonl", argv=self.worker_argv(model="gpt-6-astra"))
        self.assertEqual((code, run["subtype"], run["api_error_status"]), (1, "error_during_execution", 404))
        self.assertIn("model is not available", run["result"])
        called = self.codex_called()["argv"]
        self.assertEqual(called[called.index("-m") + 1], "gpt-6-astra")

    def test_settings_from_environment_and_effort_from_hansa(self):
        argv = self.worker_argv(effort="low", model=None)
        run, _ = self.run_worker("success.jsonl", argv=argv, HANSA_CODEX_MODEL="gpt-x", HANSA_CODEX_EFFORT="medium")
        called = self.codex_called()["argv"]
        self.assertEqual(called[called.index("-m") + 1], "gpt-x")
        self.assertIn('model_reasoning_effort="low"', called)
        self.assertEqual(list(run["modelUsage"]), ["gpt-x"])
        self.run_worker("success.jsonl", HANSA_CODEX_EFFORT="medium")
        self.assertIn('model_reasoning_effort="medium"', self.codex_called()["argv"])
        for effort in ("low", "medium", "high", "xhigh", "max"):
            self.run_worker("success.jsonl", argv=self.worker_argv(effort=effort))
            self.assertIn(f'model_reasoning_effort="{effort}"', self.codex_called()["argv"])

    def test_logs_kept(self):
        run, _ = self.run_worker("success.jsonl")
        self.assertEqual((self.logs / f"{self.sid}.jsonl").read_text(), (FIXTURES / "success.jsonl").read_text())
        self.assertTrue((self.logs / f"{self.sid}.err").exists())
        thread = "0199f3a2-7c1e-7d40-9b6a-5c2f0e8d4a11"
        self.assertEqual((self.logs / f"{self.sid}.thread").read_text().strip(), thread)
        self.assertEqual(run["codex_thread_id"], thread)

    def test_toml_string(self):
        self.assertEqual(shim.toml_string('a"b\\c\nd\x01\x7fé'), '"a\\"b\\\\c\\nd\\u0001\\u007fé"')
