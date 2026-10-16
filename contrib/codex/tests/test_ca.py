"""HANSA_CODEX_CA_FILE: the certificate file Codex gets as CODEX_CA_CERTIFICATE inside srt."""
import os

from .codexkit import Scratch, hansa_run, shim


class CaFile(Scratch):
    def setUp(self):
        super().setUp()
        self.ca = self.root / "certs" / "ca.pem"
        self.ca.parent.mkdir()
        self.ca.write_text("-----BEGIN CERTIFICATE-----\n")

    def codex_ca(self, **extra):
        run, code = self.run_worker("success.jsonl", **extra)
        self.assertEqual((code, run["subtype"]), (0, "success"), run["result"])
        return self.codex_called()["env"]["CODEX_CA_CERTIFICATE"]

    def test_setting_reaches_codex(self):
        self.assertEqual(self.codex_ca(HANSA_CODEX_CA_FILE=str(self.ca)), str(self.ca))

    def test_default_when_it_exists(self):
        default = shim.DEFAULT_CA_FILE if os.path.isfile(shim.DEFAULT_CA_FILE) else None
        self.assertEqual(self.codex_ca(), default)
        # either way, the rule itself
        self.assertEqual(shim.ca_file({}, str(self.ca)), str(self.ca))
        self.assertEqual(shim.ca_file({}, str(self.root / "none.pem")), "")
        self.assertEqual(shim.ca_file({shim.CA_SETTING: str(self.ca)}, str(self.root / "none.pem")), str(self.ca))

    def test_empty_setting_leaves_it_out(self):
        self.assertIsNone(self.codex_ca(HANSA_CODEX_CA_FILE=""))
        self.assertIsNone(self.codex_ca(HANSA_CODEX_CA_FILE="", CODEX_CA_CERTIFICATE=str(self.ca)))
        self.assertEqual(shim.ca_file({shim.CA_SETTING: ""}, str(self.ca)), "")
        # set but empty in codex.env too, unless the environment gives one
        f = self.root / "codex.env"
        f.write_text("HANSA_CODEX_CA_FILE=\n")
        kept, shim.CODEX_ENV = shim.CODEX_ENV, f
        try:
            self.assertEqual(shim.settings({}), {shim.CA_SETTING: ""})
            self.assertEqual(shim.settings({shim.CA_SETTING: str(self.ca)}), {shim.CA_SETTING: str(self.ca)})
        finally:
            shim.CODEX_ENV = kept

    def test_lane_env_wins(self):
        self.lane_env.write_text("export FROM_LANE_ENV='lane value'\nexport CODEX_CA_CERTIFICATE=/lane/ca.pem\n")
        self.assertEqual(self.codex_ca(HANSA_CODEX_CA_FILE=str(self.ca)), "/lane/ca.pem")
        self.assertEqual(self.codex_called()["env"]["FROM_LANE_ENV"], "lane value")

    def refused(self, **extra):
        run, code = self.run_worker("success.jsonl", **extra)
        self.assertEqual((code, run["subtype"], run["is_error"]), (1, "error_during_execution", True))
        self.assertFalse(self.codex_record.exists(), "codex never started")
        self.assertEqual(hansa_run.unusable_reason(run, self.err), ("", ""))
        return run["result"]

    def test_under_a_no_read_path(self):
        ca = self.secret / "ca.pem"
        ca.write_text("-----BEGIN CERTIFICATE-----\n")
        why = self.refused(HANSA_CODEX_CA_FILE=str(ca))
        self.assertIn("HANSA_CODEX_CA_FILE", why)
        self.assertIn("may not read", why)

    def test_missing_file(self):
        why = self.refused(HANSA_CODEX_CA_FILE=str(self.root / "none.pem"))
        self.assertIn("HANSA_CODEX_CA_FILE", why)
        self.assertIn("is not a file", why)
