"""A Codex home must not grant writes and reads across an account or worktree."""
import json
from unittest import mock

from .codexkit import Scratch, shim


class HomeScope(Scratch):
    def setUp(self):
        super().setUp()
        self.account_home = self.root / "account-home"
        self.account_home.mkdir()

    def refused(self, home, reason, check=False, switch="0"):
        env = self.env("check_ok.jsonl", HANSA_CODEX_RUN=switch, HANSA_CODEX_HOME=str(home),
                       HOME=str(self.account_home))
        argv = ["-p", "--output-format", "json"] if check else self.worker_argv()
        proc = self.start(argv, env)
        out, err = proc.communicate(timeout=60)
        self.assertTrue(out, err.decode(errors="replace"))
        run = json.loads(out)
        self.assertEqual((proc.returncode, run["subtype"]), (1, "error_during_execution"))
        for word in ("HANSA_CODEX_HOME", str(home), reason):
            self.assertIn(word, run["result"])
        self.assertFalse(self.srt_record.exists(), "home validation must precede srt")
        self.assertFalse(self.codex_record.exists(), "home validation must precede Codex")

    def test_root_account_home_and_ancestors_are_refused_for_runs_and_checks(self):
        for home, reason in (("/", "root"), (self.account_home, "account"), (self.root, "account")):
            for switch in ("0", "1"):
                for check in (False, True):
                    with self.subTest(home=home, switch=switch, check=check):
                        self.refused(home, reason, check=check, switch=switch)

    def test_worktree_and_ancestors_are_refused_for_workers(self):
        for home in (self.wt, self.root):
            for switch in ("0", "1"):
                with self.subTest(home=home, switch=switch):
                    # Keep the account home elsewhere so this specifically tests the worktree rule.
                    self.account_home = self.make_root()
                    self.refused(home, "worktree", switch=switch)

    def test_home_and_protected_paths_are_compared_as_given_and_resolved(self):
        account_link = self.root / "account-link"
        account_link.symlink_to(self.account_home, target_is_directory=True)
        wt_link = self.root / "wt-link"
        wt_link.symlink_to(self.wt, target_is_directory=True)
        for home, account, wt in ((account_link, self.account_home, ""),
                                  (self.account_home, account_link, ""),
                                  (wt_link, "", self.wt), (self.wt, "", wt_link)):
            with self.subTest(home=home, account=account, wt=wt):
                why = shim.home_scope_problem(str(home), str(account), str(wt))
                self.assertIn(str(home), why)
                self.assertIn("HANSA_CODEX_HOME", why)
        # An ancestor of a symlink's given path is too wide even if its target is elsewhere.
        elsewhere = self.make_root()
        outside_account = elsewhere / "account"
        outside_account.mkdir()
        alias = self.root / "outside-account"
        alias.symlink_to(outside_account, target_is_directory=True)
        self.assertIn("account", shim.home_scope_problem(str(self.root), str(alias), ""))
        self.assertIn("account", shim.home_scope_problem(str(elsewhere), str(alias), ""))
        root_link = self.root / "root-link"
        root_link.symlink_to("/", target_is_directory=True)
        self.assertIn("root", shim.home_scope_problem(str(root_link), "", ""))

    def test_dedicated_home_below_account_home_still_runs_in_both_modes(self):
        home = self.account_home / "codex-home"
        for switch in ("0", "1"):
            for check in (False, True):
                with self.subTest(switch=switch, check=check):
                    argv = ["-p", "--output-format", "json"] if check else self.worker_argv()
                    run, code = self.run_worker("check_ok.jsonl", argv=argv, HANSA_CODEX_RUN=switch,
                                                HANSA_CODEX_HOME=str(home), HOME=str(self.account_home))
                    self.assertEqual((code, run["subtype"]), (0, "success"), run["result"])
                    self.assertTrue(home.is_dir())

    def test_scope_check_precedes_both_sandbox_validators(self):
        env = self.env(HANSA_CODEX_HOME=str(self.root), HOME=str(self.account_home))
        with mock.patch.object(shim, "sandbox_problem", side_effect=AssertionError("sandbox checked first")), \
                mock.patch.object(shim, "paths_problem", side_effect=AssertionError("paths checked first")), \
                mock.patch.object(shim, "emit", return_value=1):
            for check in (False, True):
                with self.subTest(check=check):
                    self.assertEqual(shim.run_codex(self.worker_argv(), env, shim.settings(env), check=check), 1)

    def test_unknown_account_home_does_not_add_a_new_error(self):
        self.assertEqual(shim.home_scope_problem(str(self.home), "", ""), "")
        run, code = self.run_worker(HOME="")
        self.assertEqual((code, run["subtype"]), (0, "success"), run["result"])
