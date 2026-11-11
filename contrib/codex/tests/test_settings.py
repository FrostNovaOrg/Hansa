"""Bad codex.env entries fail before starting Codex, with enough context to fix the file."""
import json
import shutil
import subprocess
from unittest import mock

from hansa.desk import EFFORTS

from .codexkit import REPO, Scratch, shim


class SettingsFile(Scratch):
    def setUp(self):
        super().setUp()
        self.adapter = self.root / "adapter"
        for folder in ("bin", "shim"):
            (self.adapter / folder).mkdir(parents=True)
        self.wrapper = self.adapter / "bin" / "claude-or-codex"
        shutil.copy2(REPO / "bin" / "claude-or-codex", self.wrapper)
        shutil.copy2(shim.__file__, self.adapter / "shim" / "hansa_codex_shim.py")
        self.config = self.adapter / "codex.env"
        self.legacy = self.adapter / "trial.env"

    def read_settings(self, env=None):
        with mock.patch.object(shim, "CODEX_ENV", self.config), mock.patch.object(shim, "OLD_ENV", self.legacy):
            return shim.settings(env or {})

    def run_adapter(self, argv=None, **extra):
        # Startup errors precede the macOS-only worker sandbox check, so exercise them on Linux too.
        proc = subprocess.run([str(self.wrapper), *(self.worker_argv() if argv is None else argv)],
                              env=self.env(**extra), cwd=self.wt, input=b"Reply with OK.\n",
                              capture_output=True, timeout=60)
        self.assertTrue(proc.stdout, proc.stderr.decode(errors="replace"))
        return json.loads(proc.stdout), proc.returncode

    def refused(self, **extra):
        run, code = self.run_adapter(**extra)
        self.assertEqual((code, run["subtype"], run["is_error"]), (1, "error_during_execution", True))
        self.assertEqual(run["session_id"], self.sid)
        self.assertEqual(len(run["result"].splitlines()), 1)
        self.assertFalse(self.codex_record.exists(), "Codex must not start for bad settings")
        self.assertFalse(self.srt_record.exists(), "srt must not start for bad settings")
        self.assertFalse(self.logs.exists(), "validation must precede creating run logs")
        return run["result"]

    def test_exported_bin_is_read_and_runs(self):
        self.config.write_text(f"export HANSA_CODEX_BIN={self.env()['HANSA_CODEX_BIN']}\n")
        self.assertEqual(self.read_settings()["HANSA_CODEX_BIN"], self.env()["HANSA_CODEX_BIN"])
        run, code = self.run_adapter(argv=["-p", "--output-format", "json"], HANSA_CODEX_BIN="")
        self.assertEqual((code, run["subtype"]), (0, "success"), run["result"])
        self.assertTrue(self.codex_record.exists())

    def test_unknown_adapter_key_names_file_and_known_keys(self):
        for prefix in ("", "export "):
            with self.subTest(prefix=prefix):
                self.config.write_text(f"{prefix}HANSA_CODEX_HOMEE=/some/home\n")
                why = self.refused()
                for word in (str(self.config), "HANSA_CODEX_HOMEE", "unknown", *shim.SETTINGS):
                    self.assertIn(word, why)

    def test_bad_effort_names_value_and_accepted_values(self):
        for bad in ("hgih", "High", "none", "minimal", "ultra"):
            with self.subTest(bad=bad):
                self.config.write_text(f"HANSA_CODEX_EFFORT={bad}\n")
                why = self.refused()
                for word in ("HANSA_CODEX_EFFORT", bad, "low, medium, high, xhigh, max"):
                    self.assertIn(word, why)

    def test_effort_from_environment_and_hansa_is_checked_too(self):
        for extra in ({"HANSA_CODEX_EFFORT": "hgih"}, {"argv": self.worker_argv(effort="hgih")}):
            with self.subTest(extra=extra):
                why = self.refused(**extra)
                self.assertIn("hgih", why)
                self.assertIn("low, medium, high, xhigh, max", why)

    def test_file_and_hansa_accept_the_same_efforts(self):
        self.assertEqual(shim.EFFORT_VALUES, EFFORTS[1:])
        for effort in EFFORTS[1:]:
            for source in ("file", "hansa"):
                with self.subTest(effort=effort, source=source):
                    self.config.write_text(f"HANSA_CODEX_EFFORT={effort}\n")
                    argv = ["-p", "--output-format", "json"]
                    if source == "hansa":
                        argv += ["--effort", effort]
                        self.config.write_text("HANSA_CODEX_EFFORT=medium\n")
                    run, code = self.run_adapter(argv=argv)
                    self.assertEqual((code, run["subtype"]), (0, "success"), run["result"])
                    self.assertIn(f'model_reasoning_effort="{effort}"', self.codex_called()["argv"])

    def test_example_note_is_rejected_for_each_setting(self):
        for key in shim.SETTINGS:
            with self.subTest(key=key):
                self.config.write_text(f"{key}=/some/value   (default: example)\n")
                why = self.refused()
                for word in (str(self.config), key, "example's note"):
                    self.assertIn(word, why)

    def test_missing_setting_names_the_file_read(self):
        for config in (self.config, self.legacy):
            with self.subTest(config=config):
                config.write_text("# No program configured.\n")
                why = self.refused(HANSA_CODEX_BIN="")
                self.assertIn("HANSA_CODEX_BIN is not set", why)
                self.assertIn(str(config), why)
                config.unlink()

    def test_missing_home_and_srt_name_the_file_read(self):
        self.config.write_text("# Required settings are absent.\n")
        why = self.refused(HANSA_CODEX_HOME="")
        self.assertIn("HANSA_CODEX_HOME is not set", why)
        self.assertIn(str(self.config), why)
        why = self.refused(HANSA_CODEX_SRT="", PATH=str(self.root / "empty-path"))
        self.assertIn("srt (HANSA_CODEX_SRT) is not set", why)
        self.assertIn(str(self.config), why)

    def test_comments_blanks_other_keys_and_environment_override_still_work(self):
        self.config.write_text("\n  # HANSA_CODEX_HOMEE=ignored\nOTHER=value   (default: ignored)\n"
                               "export OTHER=also ignored\nHANSA_CODEX_MODEL='model-a'\n"
                               "export\t HANSA_CODEX_EFFORT=low\nHANSA_CODEX_CA_FILE=\n")
        self.assertEqual(self.read_settings({"HANSA_CODEX_MODEL": "model-b"}),
                         {"HANSA_CODEX_MODEL": "model-b", "HANSA_CODEX_EFFORT": "low", "HANSA_CODEX_CA_FILE": ""})

    def test_example_optional_settings_can_be_uncommented(self):
        lines = (REPO / "codex.env.example").read_text().splitlines()
        optional = [line[2:] for line in lines if line.startswith("# HANSA_")]
        self.config.write_text("\n".join(optional))
        got = self.read_settings()
        for key, value in got.items():
            self.assertNotIn("(default:", value, key)
