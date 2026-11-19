"""The run's folders hansa names to a worker's commands (HANSA_PICTURES_DIR, HANSA_BENCH_DIR and its epoch) reach
Codex's commands: in Codex's environment, and by shell_environment_policy.set, which holds whatever the policy in
the Codex home inherits (`inherit = "core"` would drop them otherwise)."""
import json

from .codexkit import Scratch, shim

GIVEN = {"HANSA_PICTURES_DIR": "/tmp/hansa-t0001-x/claude-501/pictures",
         "HANSA_BENCH_DIR": "/tmp/hansa-t0001-x/claude-501/bench", "HANSA_BENCH_EPOCH": "e1"}


class CommandEnv(Scratch):
    def test_a_workers_folders_reach_codex_and_every_command(self):
        run, code = self.run_worker("success.jsonl", **GIVEN)
        self.assertEqual((code, run["subtype"]), (0, "success"), run["result"])
        called = self.codex_called()
        for k, v in GIVEN.items():
            with self.subTest(var=k):
                self.assertEqual(called["env"].get(k), v)
                self.assertIn(f"shell_environment_policy.set.{k}={json.dumps(v)}", called["argv"])
        # and none set when hansa gave none
        run, code = self.run_worker("success.jsonl")
        self.assertEqual(code, 0, run["result"])
        self.assertFalse([a for a in self.codex_called()["argv"]
                          if any(a.startswith(f"shell_environment_policy.set.{k}=") for k in GIVEN)])

    def test_the_command_line_sets_each_as_toml(self):
        argv = shim.codex_argv("codex", "/work", "m", "high", "", "/last", commands_env={"HANSA_PICTURES_DIR": 'a "b"'})
        self.assertIn('shell_environment_policy.set.HANSA_PICTURES_DIR="a \\"b\\""', argv)
