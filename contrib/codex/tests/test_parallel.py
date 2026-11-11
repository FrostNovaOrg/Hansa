"""A desk that also runs Claude workers, with several Codex runs at once: the Codex home on the no-read list is
opened to a Codex run alone, and a refresh collision between two runs is a dropped connection, not a lost login."""
import copy
import json
import os
import subprocess
import time

from .codexkit import FAKES, WRAPPER, Scratch, alive, hansa_run, shim


class HomeOnTheNoReadList(Scratch):
    def box_with(self, *paths):
        """hansa's settings for the worktree with `paths` added to the no-read list, as hansa lists them."""
        box = copy.deepcopy(self.box)
        box["sandbox"]["filesystem"]["denyRead"] += hansa_run.full_paths(paths)
        return box

    def test_the_home_itself_is_left_out(self):
        box = self.box_with(self.home)
        run, _ = self.run_worker("success.jsonl", argv=self.worker_argv(box=box))
        self.assertEqual(run["subtype"], "success", run["result"])
        self.assertEqual(self.srt_called()["settings"]["filesystem"]["denyRead"], self.box["sandbox"]["filesystem"]
                         ["denyRead"], "only the home's entry left out; the rest as hansa gave it")
        self.assertIn("left out of srt's denyRead", self.err)
        self.assertIn(str(self.home), self.err)

    def test_an_entry_inside_the_home_is_left_out(self):
        inside = [self.home / "auth.json", self.home / "sessions"]
        box = self.box_with(*inside)
        run, _ = self.run_worker("success.jsonl", argv=self.worker_argv(box=box))
        self.assertEqual(run["subtype"], "success", run["result"])
        deny = self.srt_called()["settings"]["filesystem"]["denyRead"]
        for p in inside:
            self.assertNotIn(str(p), deny)
            self.assertIn(str(p), self.err)

    def test_an_entry_above_the_home_is_refused(self):
        home = self.secret / "codex"
        box = self.box_with(home)  # the home's own entry is left out; the folder above it is not
        run, code = self.run_worker("success.jsonl", argv=self.worker_argv(box=box), HANSA_CODEX_HOME=str(home))
        self.assertEqual((code, run["subtype"]), (1, "error_during_execution"))
        self.assertIn(f"HANSA_CODEX_HOME ({home}) is under {self.secret}", run["result"])
        self.assertIn("never a folder above it", run["result"])
        self.assertFalse(self.codex_record.exists(), "codex never started")
        self.assertEqual(hansa_run.unusable_reason(run, self.err), ("", ""))

    def test_unrelated_entries_are_kept(self):
        other = self.root / "codex-home-old"  # a name that starts as the home's does is not inside it
        deny = self.box["sandbox"]["filesystem"]["denyRead"] + hansa_run.full_paths([self.home, other])
        kept, left = shim.home_read_open(deny, str(self.home))
        self.assertEqual(kept, [*self.box["sandbox"]["filesystem"]["denyRead"], str(other)])
        self.assertEqual(left, [str(self.home)])
        for p in (self.secret, os.path.expanduser("~/.codex"), other):
            self.assertIn(str(p), kept)
        self.assertEqual(shim.home_read_open(deny, ""), (deny, []))

    def test_through_a_link(self):
        # the home given through a link, listed by hansa as given and resolved: both spellings are left out
        link = self.root / "home-link"
        self.home.mkdir()
        link.symlink_to(self.home)
        deny = hansa_run.full_paths([link, self.secret])
        self.assertEqual(shim.home_read_open(deny, str(link)), ([str(self.secret)], [str(link), str(self.home)]))
        self.assertEqual(shim.home_read_open(deny, str(self.home)), ([str(self.secret)], [str(link), str(self.home)]))

    def test_a_claude_run_keeps_every_entry(self):
        box = self.box_with(self.home, self.home / "auth.json")
        record = self.root / "claude-record"
        record.mkdir()
        # another provider's worker; an interactive session on the Codex provider
        for base_url in ("https://api.example.com", shim.PLACEHOLDER):
            with self.subTest(base_url=base_url):
                argv = self.worker_argv(box=box)
                if base_url == shim.PLACEHOLDER:
                    argv = [a for a in argv if a != "-p"]
                env = self.env(ANTHROPIC_BASE_URL=base_url, FAKE_CLAUDE_RECORD=str(record))
                p = subprocess.run([str(WRAPPER), *argv], cwd=str(self.wt), env=env, input=b"",
                                   capture_output=True, timeout=60)
                got = [os.fsdecode(a) for a in (record / "argv").read_bytes().split(b"\0")[:-1]]
                self.assertEqual(got, argv)
                given = json.loads(got[got.index("--settings") + 1])
                self.assertEqual(given["sandbox"]["filesystem"]["denyRead"],
                                 box["sandbox"]["filesystem"]["denyRead"])
                self.assertNotIn(b"left out", p.stderr)
                self.assertFalse(self.srt_record.exists())


class RefreshCollision(Scratch):
    def collided(self, status, fixture="refresh_token_used.jsonl", **extra):
        self.status_record = self.root / "status-record.json"
        run, code = self.run_worker(fixture, FAKE_CODEX_STATUS=status,
                                    FAKE_CODEX_STATUS_RECORD=str(self.status_record), **extra)
        self.assertEqual((code, run["subtype"], run["is_error"]), (1, "error_during_execution", True))
        return run

    def assert_failed_login(self, run, how):
        self.assertEqual(hansa_run.unusable_reason(run, self.err), ("login", f"not logged in; {run['login_hint']}"))
        self.assertIn(f"codex login status: {how}", run["result"])

    def test_logged_in_is_a_dropped_connection(self):
        run = self.collided("in")
        self.assertEqual(hansa_run.unusable_reason(run, self.err), ("", ""), run["result"])
        self.assertEqual(hansa_run.network_reason(run, self.err),
                         "the connection to the provider failed (Connection dropped)")
        self.assertIn("another run on the same Codex home refreshed the login at the same moment", run["result"])
        # run here, outside srt (whose fake records only the exec), on the run's own Codex home
        status = json.loads(self.status_record.read_text())
        self.assertEqual(status["argv"], ["login", "status"])
        self.assertEqual(status["codex_home"], str(self.home))
        self.assertEqual(self.srt_called()["command"][8], "exec")
        # nothing from the status command is passed on
        self.assertNotIn("using ChatGPT", run["result"] + self.err)

    def test_not_logged_in_is_a_failed_login(self):
        self.assert_failed_login(self.collided("out"), "not logged in")

    def test_a_status_that_fails_is_a_failed_login(self):
        run = self.collided("fail")
        self.assert_failed_login(run, "failed (exit 2)")
        self.assertNotIn("auth store", run["result"] + self.err)

    def test_a_status_that_hangs_is_a_failed_login(self):
        began = time.time()
        run = self.collided("hang", **{shim.STATUS_TIMEOUT_SETTING: "1"})
        self.assertLess(time.time() - began, 15)
        self.assert_failed_login(run, "did not answer within 1 s")
        status = json.loads(self.status_record.read_text())
        deadline = time.time() + 5
        while time.time() < deadline and (alive(status["pid"]) or alive(status["child"])):
            time.sleep(0.1)
        self.assertFalse(alive(status["pid"]) or alive(status["child"]), "the status command and its child stopped")

    def test_the_default_time_limit(self):
        self.assertEqual(shim.STATUS_TIMEOUT, 20)
        self.assertIn(shim.STATUS_TIMEOUT_SETTING, shim.DROP_VARS)

    def test_other_refresh_failures_ask_nothing(self):
        run = self.collided("in", fixture="refresh_token_expired.jsonl")
        self.assertEqual(hansa_run.unusable_reason(run, self.err), ("login", f"not logged in; {run['login_hint']}"))
        self.assertFalse(self.status_record.exists(), "no status command for another form")
        self.assertNotIn("codex login status", run["result"])

    def test_status_reads_no_file(self):
        # the status command alone: logged in only on exit 0 and a first line that says so
        env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/")}
        for how, want in (("in", (True, "logged in")), ("out", (False, "not logged in")),
                          ("fail", (False, "failed (exit 2)"))):
            with self.subTest(how=how):
                got = shim.login_status(str(FAKES / "codex"), str(self.home), {**env, "FAKE_CODEX_STATUS": how})
                self.assertEqual(got, want)
        self.assertEqual(shim.login_status(str(self.root / "no-codex"), str(self.home), env)[0], False)
        self.assertFalse(self.home.exists(), "the Codex home is not touched")
