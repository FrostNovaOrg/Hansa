"""HANSA_CODEX_WEB_SEARCH and HANSA_CODEX_DISABLE: the ways to the web on OpenAI's side, which srt cannot stop."""
from .codexkit import Scratch, hansa_run, shim


class WebRoutes(Scratch):
    def codex_options(self, **extra):
        """What codex got between check_for_update_on_startup and -s."""
        run, code = self.run_worker("success.jsonl", **extra)
        self.assertEqual((code, run["subtype"]), (0, "success"), run["result"])
        argv = self.codex_called()["argv"]
        return argv[argv.index("check_for_update_on_startup=false") + 1:argv.index("-s")]

    def test_defaults_reach_codex(self):
        self.assertEqual(self.codex_options(), [
            "-c", 'web_search="disabled"', "--disable", "apps", "--disable", "plugins", "--disable", "browser_use",
            "--disable", "computer_use", "--disable", "image_generation", "--disable", "multi_agent",
            "--disable", "hooks"])
        # empty in the environment is unset for the web search: the default
        self.assertEqual(self.codex_options(HANSA_CODEX_WEB_SEARCH="")[:2], ["-c", 'web_search="disabled"'])

    def test_empty_disable_gives_none(self):
        self.assertEqual(self.codex_options(HANSA_CODEX_DISABLE=""), ["-c", 'web_search="disabled"'])
        self.assertEqual(self.codex_options(HANSA_CODEX_DISABLE="  "), ["-c", 'web_search="disabled"'])

    def test_live_and_a_list_of_its_own(self):
        self.assertEqual(self.codex_options(HANSA_CODEX_WEB_SEARCH="live"), [
            "-c", 'web_search="live"', "--disable", "apps", "--disable", "plugins", "--disable", "browser_use",
            "--disable", "computer_use", "--disable", "image_generation", "--disable", "multi_agent",
            "--disable", "hooks"])
        # independent: web search on leaves apps on only when the list does not name it
        self.assertEqual(self.codex_options(HANSA_CODEX_WEB_SEARCH="cached", HANSA_CODEX_DISABLE="plugins"),
                         ["-c", 'web_search="cached"', "--disable", "plugins"])

    def test_read_from_trial_env(self):
        # codex.env's older name, read while there is no codex.env, as from codex.env
        for name in ("trial.env", "codex.env"):
            with self.subTest(name=name):
                f = self.root / name
                f.write_text("HANSA_CODEX_WEB_SEARCH=live\nHANSA_CODEX_DISABLE=\n")
                kept = shim.CODEX_ENV, shim.OLD_ENV
                shim.CODEX_ENV, shim.OLD_ENV = self.root / "codex.env", self.root / "trial.env"
                try:
                    self.assertEqual(shim.web_options(shim.settings({})), ("live", []))
                    self.assertEqual(shim.web_options(shim.settings({"HANSA_CODEX_DISABLE": "apps"})),
                                     ("live", ["apps"]))
                    self.assertEqual(shim.web_options(shim.settings({"HANSA_CODEX_WEB_SEARCH": "cached"})),
                                     ("cached", []))
                finally:
                    shim.CODEX_ENV, shim.OLD_ENV = kept
                    f.unlink()

    def refused(self, **extra):
        run, code = self.run_worker("success.jsonl", **extra)
        self.assertEqual((code, run["subtype"], run["is_error"]), (1, "error_during_execution", True))
        self.assertFalse(self.codex_record.exists(), "codex never started")
        self.assertEqual(hansa_run.unusable_reason(run, self.err), ("", ""))
        return run["result"]

    def test_bad_web_search_refused(self):
        for bad in ("on", "Live", "true"):
            why = self.refused(HANSA_CODEX_WEB_SEARCH=bad)
            self.assertIn("HANSA_CODEX_WEB_SEARCH", why)
            self.assertIn("disabled, cached, live", why)

    def test_bad_feature_name_refused(self):
        why = self.refused(HANSA_CODEX_DISABLE="apps -s")
        self.assertIn("HANSA_CODEX_DISABLE", why)
