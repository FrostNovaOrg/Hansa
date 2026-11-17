"""The standard install opens only packages, for the provider that needs its program there."""
import json
import shutil
import subprocess
from unittest import mock

from .codexkit import FAKES, REPO, WRAPPER, Scratch, hansa_run, shim


class Packages(Scratch):
    def setUp(self):
        super().setUp()
        self.account_real = self.root / "account"
        self.account_real.mkdir()
        self.account = self.root / "account-link"
        self.account.symlink_to(self.account_real, target_is_directory=True)
        self.codex = self.account / ".codex"
        self.packages = self.codex / "packages"
        version = self.packages / "standalone" / "v1"
        (version / "bin").mkdir(parents=True)
        self.program = version / "bin" / "codex"
        shutil.copy2(FAKES / "codex", self.program)
        (version.parent / "current").symlink_to("v1", target_is_directory=True)
        self.link = self.account / ".local" / "bin" / "codex"
        self.link.parent.mkdir(parents=True)
        self.link.symlink_to(self.packages / "standalone" / "current" / "bin" / "codex")
        fs = self.box["sandbox"]["filesystem"]
        fs["denyRead"] = shim.both([self.secret, self.codex])
        fs["allowRead"] = shim.both([self.wt, self.tmp])

    def call(self, check=False, program=None, denied=(), legacy=False):
        env = self.env("check_ok.jsonl", HOME=str(self.account), HANSA_CODEX_RUN="1",
                       HANSA_CODEX_BIN=str(program or self.link),
                       HANSA_CODEX_READ_POLICY=json.dumps({"home": str(self.account), "denied": list(map(str, denied))}))
        argv = ["-p", "--output-format", "json"] if check else self.worker_argv()
        wrapper = WRAPPER
        if legacy:
            # A real codex.env read by a copied adapter, never the checkout's settings file.
            adapter = self.root / "adapter"
            for folder in ("bin", "shim"):
                (adapter / folder).mkdir(parents=True, exist_ok=True)
            wrapper = adapter / "bin" / WRAPPER.name
            shutil.copy2(WRAPPER, wrapper)
            shutil.copy2(REPO / "shim" / "hansa_codex_shim.py", adapter / "shim" / "hansa_codex_shim.py")
            (adapter / "codex.env").write_text(f"HANSA_CODEX_BIN='{self.link}'\n")
            env.pop("HANSA_CODEX_BIN")
            env.pop("HANSA_CODEX_RUN")
        p = subprocess.run([str(wrapper), *argv], cwd=self.wt, env=env, input="Reply OK.",
                           capture_output=True, text=True, timeout=60)
        self.assertTrue(p.stdout, p.stderr)
        return json.loads(p.stdout), p.returncode

    def success(self, **kw):
        run, code = self.call(**kw)
        self.assertEqual((code, run["subtype"]), (0, "success"), run["result"])
        return self.srt_called()["settings"]["filesystem"]

    def refused(self, **kw):
        run, code = self.call(**kw)
        self.assertEqual((code, run["subtype"]), (1, "error_during_execution"))
        self.assertFalse(self.srt_record.exists(), "refusal precedes srt")
        return run["result"]

    def test_standard_install_runs_and_checks_with_only_packages_opened(self):
        for check in (False, True):
            with self.subTest(check=check):
                fs = self.success(check=check)
                expected = shim.both([self.packages])
                self.assertNotEqual(expected[0], expected[1], "exercise both spellings")
                self.assertEqual(fs["allowRead"], expected if check else self.box["sandbox"]["filesystem"]["allowRead"] + expected)
                for path in shim.both([self.codex]):
                    self.assertIn(path, fs["denyRead"])
                    self.assertIn(path, fs["denyWrite"])
                self.assertIn(str(self.link), self.srt_called()["command"])
                self.assertIsNone(self.codex_called()["env"]["HANSA_CODEX_READ_POLICY"])

    def test_every_other_codex_entry_stays_closed_for_workers_and_checks(self):
        names = ("auth.json", "config.toml", "environments.toml", "hooks.json", "rules", "AGENTS.md",
                 "AGENTS.override.md", "skills", "memories", "sessions", "history.jsonl", "logs", "unknown-entry")
        for check in (False, True):
            fs = self.success(check=check)
            for name in names:
                for path in shim.both([self.codex / name]):
                    with self.subTest(check=check, path=path):
                        self.assertTrue(any(shim.under(path, d) for d in fs["denyRead"]))
                        self.assertFalse(any(shim.under(path, a) for a in fs["allowRead"]))
                        self.assertTrue(any(shim.under(path, d) for d in fs["denyWrite"]))

    def test_program_elsewhere_in_codex_is_refused_without_opening_packages(self):
        program = self.codex / "other-bin" / "codex"
        for check in (False, True):
            with self.subTest(check=check):
                self.assertIn(str(self.codex), self.refused(check=check, program=program))
                fs = self.settings_for(program, check)
                for path in shim.both([self.codex]):
                    self.assertIn(path, fs["denyRead"])
                self.assertFalse(any(shim.under(a, str(self.codex.resolve())) or shim.under(a, str(self.codex))
                                     for a in fs["allowRead"]))

    def settings_for(self, program, check=False, denied=()):
        args = (str(program), str(self.account), list(map(str, denied)))
        if check:
            return shim.check_settings(str(self.tmp), str(self.home),
                                       shim.check_deny_read(str(self.account), str(self.wt)), *args)["filesystem"]
        return shim.srt_settings(self.box, str(self.wt), str(self.tmp), str(self.home), *args)["filesystem"]

    def test_program_in_another_no_read_folder_is_still_refused(self):
        for check in (False, True):
            with self.subTest(check=check):
                program = self.account / ".ssh" / "codex" if check else self.secret / "codex"
                self.assertIn(str(program.parent), self.refused(check=check, program=program))

    def test_principals_entries_win_and_are_named_for_workers_and_checks(self):
        alias = self.root / "packages-alias"
        alias.symlink_to(self.packages, target_is_directory=True)
        for entry in (self.codex, self.packages, self.packages / "codex-resources", self.program, alias):
            for check in (False, True):
                with self.subTest(entry=entry, check=check):
                    why = self.refused(check=check, denied=[entry])
                    self.assertIn(str(entry), why)
                    self.assertIn("HANSA_CODEX_BIN", why)
                    fs = self.settings_for(self.link, check, [entry])
                    self.assertFalse(any(p in fs["allowRead"] for p in shim.both([self.packages])))

    def test_other_runs_keep_packages_closed(self):
        for check in (False, True):
            with self.subTest(check=check):
                fs = self.success(check=check, program=FAKES / "codex")
                for path in shim.both([self.codex]):
                    self.assertIn(path, fs["denyRead"])
                self.assertFalse(any(p in fs["allowRead"] for p in shim.both([self.packages])))
        # Hansa's shared builder remains closed for Claude workers, verifiers, setups and lane checks.
        with mock.patch.object(hansa_run, "account_home", return_value=str(self.account)):
            for path in shim.both([self.codex]):
                self.assertIn(path, hansa_run.full_paths(hansa_run.worker_home_denials()))

    def test_legacy_settings_file_standard_install_runs_and_checks(self):
        for check in (False, True):
            with self.subTest(check=check):
                fs = self.success(check=check, legacy=True)
                for path in shim.both([self.packages]):
                    self.assertIn(path, fs["allowRead"])

    def test_refused_link_names_typed_path_and_resolved_destination(self):
        target = self.codex / "other" / "codex"
        link = self.account / ".local" / "bin" / "other"
        link.symlink_to(target)
        for check in (False, True):
            with self.subTest(check=check):
                why = self.refused(check=check, program=link)
                for part in (str(link), "resolves to", str(target.resolve()), str(self.codex)):
                    self.assertIn(part, why)

    def test_package_links_cannot_open_another_no_read_folder(self):
        # A packages link into a different built-in secret must not re-allow that secret.
        elsewhere = self.account / ".ssh"
        elsewhere.mkdir()
        shutil.move(str(self.packages / "standalone"), elsewhere / "standalone")
        self.packages.rmdir()
        self.packages.symlink_to(elsewhere, target_is_directory=True)
        self.box["sandbox"]["filesystem"]["denyRead"] += shim.both([elsewhere])
        for check in (False, True):
            with self.subTest(check=check):
                self.assertIn(str(elsewhere), self.refused(check=check))

    def test_resolved_package_root_stays_read_only_on_a_lane_write_list(self):
        target = self.cache / "codex-packages"
        shutil.move(str(self.packages), target)
        self.packages.symlink_to(target, target_is_directory=True)
        for check in (False, True):
            with self.subTest(check=check):
                fs = self.success(check=check)
                for path in shim.both([self.packages]):
                    self.assertIn(path, fs["allowRead"])
                    self.assertIn(path, fs["denyWrite"])
                self.assertTrue(shim.denied(str(self.program.resolve()), fs["denyWrite"]))

    def test_packages_link_cannot_open_codex_parent_or_another_codex_entry(self):
        shutil.move(str(self.packages), self.root / "original-packages")
        for target in (self.codex, self.account, self.codex / "private"):
            target.mkdir(exist_ok=True)
            self.packages.symlink_to(target, target_is_directory=True)
            for check in (False, True):
                with self.subTest(target=target, check=check):
                    self.assertIn(str(self.codex), self.refused(check=check, program=target / "codex"))
                    fs = self.settings_for(target / "codex", check)
                    self.assertFalse(any(p in fs["allowRead"] for p in shim.both([self.packages])))
            self.packages.unlink()

    def test_worker_denial_inside_packages_cannot_be_overridden_without_internal_policy(self):
        entry = self.packages / "codex-resources"
        self.box["sandbox"]["filesystem"]["denyRead"] += shim.both([entry])
        self.assertIn(str(entry), self.refused())

    def test_internal_read_policy_is_validated_and_is_not_an_adapter_setting(self):
        self.assertNotIn(shim.READ_POLICY, shim.SETTINGS)
        for policy in ("{", "[]", '{"denied":"/private"}', '{"home":42}', '{"denied":[42]}'):
            with self.subTest(policy=policy):
                run, code = self.run_worker(HANSA_CODEX_READ_POLICY=policy)
                self.assertEqual((code, run["subtype"]), (1, "error_during_execution"))
                self.assertIn("read policy", run["result"])
                self.assertFalse(self.srt_record.exists())

    def test_standard_install_without_internal_policy_uses_environment_home(self):
        run, code = self.run_worker(HOME=str(self.account), HANSA_CODEX_BIN=str(self.link))
        self.assertEqual((code, run["subtype"]), (0, "success"), run["result"])
        self.assertEqual([p for p in self.srt_called()["settings"]["filesystem"]["allowRead"]
                          if p not in self.box["sandbox"]["filesystem"]["allowRead"]], shim.both([self.packages]))

    def test_real_srt_starts_linked_package_and_refuses_account_login(self):
        srt = shutil.which("srt")
        if not srt:
            self.skipTest("no real srt here")
        auth = self.codex / "auth.json"
        auth.write_text("scratch-login-secret\n")
        self.program.write_text('#!/bin/sh\necho package-started\nif cat "$1"; then exit 9; fi\n'
                                'echo auth-read-refused\nif echo changed > "$2"; then exit 10; fi\n'
                                'echo package-write-refused\n')
        self.home.mkdir()
        probe_settings = self.root / "probe-srt.json"
        probe_settings.write_text(json.dumps({"filesystem": {"allowWrite": shim.both([self.tmp]),
                                                             "denyRead": [], "denyWrite": []},
                                              "network": {"allowedDomains": [], "deniedDomains": []}}))
        probe = subprocess.run([srt, "--settings", str(probe_settings), "--", "/bin/echo", "srt-ready"],
                               cwd=self.wt, capture_output=True, text=True, timeout=60)
        if probe.returncode:
            self.skipTest(f"real srt cannot start here: {probe.stderr.strip()[:300]}")
        for check in (False, True):
            settings = self.root / "real-srt.json"
            fs = self.settings_for(self.link, check)
            settings.write_text(json.dumps({"filesystem": fs, "network": {"allowedDomains": [], "deniedDomains": []}}))
            p = subprocess.run([srt, "--settings", str(settings), "--", str(self.link), str(auth), str(self.program)],
                               cwd=self.wt, capture_output=True, text=True, timeout=60)
            self.assertEqual(p.returncode, 0, p.stderr)
            for part in ("package-started", "auth-read-refused", "package-write-refused"):
                self.assertIn(part, p.stdout)
            self.assertNotIn("scratch-login-secret", p.stdout)
