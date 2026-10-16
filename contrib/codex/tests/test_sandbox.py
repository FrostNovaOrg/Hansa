"""srt's settings for the Codex run: the sandbox hansa gave Claude Code, plus HANSA_CODEX_HOME and CODEX_HOSTS."""
import copy
import os
from pathlib import Path
from unittest import mock

from .codexkit import FAKES, Scratch, git, hansa_run, shim


class SrtSettings(Scratch):
    def test_settings_given_to_srt(self):
        run, _ = self.run_worker("success.jsonl")
        self.assertEqual(run["subtype"], "success")
        called = self.srt_called()
        fs, net = called["settings"]["filesystem"], called["settings"]["network"]
        box = self.box["sandbox"]["filesystem"]
        # the no-read list, whole
        self.assertEqual(fs["denyRead"], box["denyRead"])
        for p in (self.secret, Path.home() / ".codex"):
            self.assertIn(str(p), fs["denyRead"])
        # never written: hansa's own deny list for this worktree (git's data), then the project folders
        wt = str(self.wt)
        self.assertEqual(fs["denyWrite"], [
            *hansa_run.git_write_deny(self.wt), f"{wt}/.claude", f"{wt}/.codex", f"{wt}/.agents",
            f"{wt}/**/.claude", f"{wt}/**/.codex", f"{wt}/**/.agents",
            f"{wt}/**/.claude/settings.json", f"{wt}/**/.claude/settings.local.json", f"{wt}/**/.claude/hooks",
            f"{wt}/**/.claude/skills",
            *(f"{self.home}/{n}" for n in ("AGENTS.md", "AGENTS.override.md", "config.toml", "environments.toml",
                                           "hooks.json", "rules"))])
        self.assertIn(str(self.wt / ".git"), fs["denyWrite"])
        common = str(self.root / "repo" / ".git")
        for name in ("objects", "refs", "HEAD", "config", "hooks"):
            self.assertIn(f"{common}/{name}", fs["denyWrite"])
        self.assertIn(f"{common}/worktrees/wt/HEAD", fs["denyWrite"])
        # writes: the worktree, its own git folder (its index), the run's temporary folder, Codex's home, the
        # lane's write list; nothing else
        self.assertEqual(fs["allowWrite"], [str(self.wt), f"{common}/worktrees/wt", str(self.tmp), str(self.home),
                                            str(self.cache)])
        for p in (self.home, self.wt):
            self.assertFalse(any(str(p) == d or str(p).startswith(d + "/") for d in fs["denyRead"]), p)
        self.assertEqual(net["allowedDomains"], ["pypi.org", *shim.CODEX_HOSTS])
        self.assertEqual(net["deniedDomains"], [])
        self.assertEqual(called["command"][:3], ["/bin/sh", "-c", shim.INNER])
        self.assertEqual(called["command"][7:9], [str(FAKES / "codex"), "exec"])

    def test_own_git_folder_only_for_a_linked_worktree(self):
        # a main checkout: hansa denies its .git whole, so nothing of it is opened
        main = self.root / "repo"
        fs = shim.srt_settings(self.box, str(main), str(self.tmp), str(self.home))["filesystem"]
        self.assertFalse([x for x in fs["allowWrite"] if "/.git" in x], fs["allowWrite"])
        # a .git leading to a folder outside the common folder's worktrees/: nothing opened
        elsewhere, odd = self.root / "elsewhere", self.root / "odd"
        elsewhere.mkdir()
        odd.mkdir()
        (elsewhere / "commondir").write_text("../repo/.git\n")
        (odd / ".git").write_text(f"gitdir: {elsewhere}\n")
        self.assertEqual(shim.own_git_write(str(odd)), [])
        # a folder in worktrees/ whose name srt would read as a pattern over its neighbours: nothing opened
        globbed, other = main / ".git" / "worktrees" / "w[t]", self.root / "other"
        globbed.mkdir()
        other.mkdir()
        (globbed / "commondir").write_text("../..\n")
        (other / ".git").write_text(f"gitdir: {globbed}\n")
        self.assertEqual(shim.own_git_write(str(other)), [])
        self.assertEqual(shim.own_git_write(str(self.wt)), [str(main / ".git" / "worktrees" / "wt")])

    def test_local_binding_carried_over(self):
        box = copy.deepcopy(self.box)
        box["sandbox"]["network"]["allowLocalBinding"] = True
        got = shim.srt_settings(box, str(self.wt), str(self.tmp), str(self.home))
        self.assertTrue(got["network"]["allowLocalBinding"])
        self.assertNotIn("allowLocalBinding", shim.srt_settings(self.box, str(self.wt), str(self.tmp),
                                                                str(self.home))["network"])

    def refused(self, box=None, **extra):
        run, code = self.run_worker("success.jsonl", argv=self.worker_argv(box=box), **extra)
        self.assertEqual((code, run["subtype"], run["is_error"]), (1, "error_during_execution", True))
        self.assertFalse(self.codex_record.exists(), "codex never started")
        self.assertEqual(hansa_run.unusable_reason(run, self.err), ("", ""), "the run's problem, not the provider's")
        self.assertEqual(hansa_run.network_reason(run, self.err), "")
        return run["result"]

    def test_no_git_write_deny_no_run(self):
        box = copy.deepcopy(self.box)
        box["sandbox"]["filesystem"]["denyWrite"] = []
        self.assertIn("do not deny writing", self.refused(box))

    def test_dot_git_only_is_not_enough(self):
        box = copy.deepcopy(self.box)
        box["sandbox"]["filesystem"]["denyWrite"] = hansa_run.full_paths([self.wt / ".git"])
        self.assertIn("do not deny writing", self.refused(box))

    def without(self, *paths):
        """hansa's settings for the worktree with `paths` left out of denyWrite."""
        box = copy.deepcopy(self.box)
        fs = box["sandbox"]["filesystem"]
        gone = hansa_run.full_paths(paths)
        self.assertTrue(set(gone) & set(fs["denyWrite"]), paths)
        fs["denyWrite"] = [x for x in fs["denyWrite"] if x not in gone]
        return box

    def test_each_git_entry_hansa_denies_is_needed(self):
        common, own = self.root / "repo" / ".git", self.root / "repo" / ".git" / "worktrees" / "wt"
        for p in (common / "objects", common / "refs", common / "HEAD", common / "config", common / "hooks",
                  common / "packed-refs", own / "HEAD", own / "gitdir", own / "commondir", own / "MERGE_HEAD"):
            with self.subTest(p=p):
                self.codex_record.unlink(missing_ok=True)
                self.assertIn(f"do not deny writing {p}", self.refused(self.without(p)))

    def test_git_entries_named_by_hansa_are_required(self):
        # the shim's lists are hansa's
        self.assertEqual(shim.GIT_COMMON_DENY, hansa_run.GIT_COMMON_DENY)
        self.assertEqual(shim.GIT_OWN_DENY, hansa_run.GIT_OWN_DENY)
        self.assertEqual(shim.GIT_OPS, hansa_run.GIT_OPS)
        named, others = shim.git_required(str(self.wt))
        for p in hansa_run._git_folder_deny(self.wt) + [self.wt / ".git"]:
            self.assertTrue(any(x in named for x in hansa_run.full_paths([p])), p)
        for p in hansa_run._git_others(self.wt):
            self.assertTrue(any(x in named + others for x in hansa_run.full_paths([p])), p)

    def test_other_git_entries_need_a_path_or_a_pattern(self):
        # the common folder's index: named in hansa's list, and under its patterns for the common folder
        common = self.root / "repo" / ".git"
        box = self.without(common / "index")
        self.assertEqual(self.run_worker("success.jsonl", argv=self.worker_argv(box=box))[0]["subtype"], "success")
        fs = box["sandbox"]["filesystem"]
        fs["denyWrite"] = [x for x in fs["denyWrite"] if not x.startswith(f"{common}/[")]
        self.codex_record.unlink()
        self.assertIn(f"do not deny writing {common / 'index'}", self.refused(box))

    def test_made_in_git_folder_since_hansa_started(self):
        # hansa's patterns hold what is made after it wrote the settings: another task's worktree, a FETCH_HEAD
        (self.root / "repo" / ".git" / "worktrees" / "t9").mkdir()
        (self.root / "repo" / ".git" / "FETCH_HEAD").write_text("")
        run, _ = self.run_worker("success.jsonl")
        self.assertEqual(run["subtype"], "success", run["result"])

    def test_project_folders_required(self):
        fs = shim.srt_settings(self.box, str(self.wt), str(self.tmp), str(self.home))["filesystem"]
        with mock.patch.object(shim.sys, "platform", "darwin"):  # where the shim runs a worker
            self.assertEqual(shim.sandbox_problem(fs, str(self.wt), str(self.home), str(FAKES / "codex")), "")
            for d in shim.config_deny(str(self.wt)) + shim.home_deny(str(self.home)):
                with self.subTest(d=d):
                    less = {**fs, "denyWrite": [x for x in fs["denyWrite"] if x != d]}
                    self.assertIn(f"do not deny writing {d}",
                                  shim.sandbox_problem(less, str(self.wt), str(self.home), str(FAKES / "codex")))

    def test_project_folder_patterns_as_srt_reads_them(self):
        wt = str(self.wt)
        deny = shim.config_deny(wt)
        for p in (".claude", ".claude/settings.json", ".codex/config.toml", ".agents/skills/x/SKILL.md",
                  "a/.claude/settings.json", "a/b/.claude/settings.local.json", "a/.claude/hooks/run.sh",
                  "a/.CLAUDE/Skills/x/SKILL.md", ".claude/hooks"):
            self.assertTrue(shim.denied(f"{wt}/{p}", deny), p)
        for p in ("a/claude/settings.json", "src/settings.json", "a/.claudex/settings.json", ".claudex/x",
                  "a/x.claude", "a/.claude-old/hooks", "a/codex/config.toml", "a/agents"):
            self.assertFalse(shim.denied(f"{wt}/{p}", deny), p)

    def test_nested_project_folder_cannot_be_made(self):
        # srt's move-blocking denies file-write-create on whatever a denyWrite pattern matches, so an entry named
        # .claude, .codex or .agents anywhere below the top can be made neither by mkdir, nor as a link, nor by
        # renaming a folder into place: each makes the entry at that path
        wt = str(self.wt)
        deny = shim.config_deny(wt)
        for p in ("sub/.claude", "sub/deeper/.claude", "sub/.codex", "sub/.agents", "sub/.CLAUDE",
                  "sub/.claude/anything", "sub/.codex/config.toml", "sub/.agents/skills/x/SKILL.md"):
            self.assertTrue(shim.denied(f"{wt}/{p}", deny), p)
        # the gap they close: by the four file patterns alone, the folder itself could be made, then filled
        files_only = [d for d in deny if not any(d.endswith(f"/**/{n}") for n in shim.PROJECT_CONFIG_DIRS)]
        self.assertEqual(len(deny) - len(files_only), len(shim.PROJECT_CONFIG_DIRS))
        self.assertFalse(shim.denied(f"{wt}/sub/.claude", files_only))

    def test_worktree_path_with_a_glob_character(self):
        # srt would read [ ] * ? in the worktree's path, inside the shim's patterns, as a glob
        for i, name in enumerate(("wt[1]", "wt*", "wt?")):
            with self.subTest(name=name):
                wt = self.root / name
                git("worktree", "add", "-q", str(wt), "-b", f"task/glob{i}", cwd=self.root / "repo")
                box = copy.deepcopy(self.box)
                box["sandbox"]["filesystem"]["denyWrite"] = hansa_run.git_write_deny(wt)
                self.wt = wt
                self.codex_record.unlink(missing_ok=True)
                self.assertIn("holds a glob character", self.refused(box))

    def test_codex_home_files_never_written(self):
        fs = shim.srt_settings(self.box, str(self.wt), str(self.tmp), str(self.home))["filesystem"]
        for n in ("AGENTS.md", "AGENTS.override.md", "config.toml", "environments.toml", "hooks.json", "rules",
                  "rules/default.rules"):
            self.assertTrue(shim.denied(str(self.home / n), fs["denyWrite"]), n)
        # what Codex itself writes during exec stays open
        for n in ("auth.json", "tmp/x", "installation_id", "state_5.sqlite", "models_cache.json",
                  "skills/.system/x", "shell_snapshots/x"):
            self.assertFalse(shim.denied(str(self.home / n), fs["denyWrite"]), n)

    def test_left_in_codex_home_refuses_the_run(self):
        self.home.mkdir()
        (self.home / "skills" / ".system").mkdir(parents=True)
        (self.home / "auth.json").write_text("{}")
        self.assertEqual(self.run_worker("success.jsonl")[0]["subtype"], "success")
        for made in ("AGENTS.md", "AGENTS.override.md", "skills/mine"):
            with self.subTest(made=made):
                self.codex_record.unlink(missing_ok=True)
                p = self.home / made
                p.mkdir() if made.startswith("skills/") else p.write_text("obey me\n")
                why = self.refused()
                self.assertIn(str(p), why)
                self.assertEqual(hansa_run.unusable_reason({"is_error": True, "result": why}, ""), ("", ""))
                p.rmdir() if p.is_dir() else p.unlink()
        self.codex_record.unlink(missing_ok=True)
        (self.home / "skills" / ".system").rmdir()
        (self.home / "skills").rmdir()
        (self.home / "skills").symlink_to(self.cache)
        self.assertIn(str(self.home / "skills"), self.refused())

    def test_home_check_reads_no_file(self):
        # names and existence only: an unreadable AGENTS.md is still found, and nothing is opened
        self.home.mkdir()
        p = self.home / "AGENTS.md"
        p.write_text("x")
        p.chmod(0)
        self.addCleanup(p.chmod, 0o600)
        self.assertIn(str(p), shim.home_problem(str(self.home)))
        self.assertEqual(shim.home_problem(str(self.root / "no-such-home")), "")

    def test_only_on_macos(self):
        fs = shim.srt_settings(self.box, str(self.wt), str(self.tmp), str(self.home))["filesystem"]
        for platform in ("linux", "win32", "freebsd14"):
            with mock.patch.object(shim.sys, "platform", platform):
                self.assertIn("only on macOS", shim.sandbox_problem(fs, str(self.wt), str(self.home),
                                                                     str(FAKES / "codex")))
        with mock.patch.object(shim.sys, "platform", "darwin"):
            self.assertEqual(shim.sandbox_problem(fs, str(self.wt), str(self.home), str(FAKES / "codex")), "")

    def test_no_sandbox_no_run(self):
        self.assertIn("no sandbox", self.refused({}))

    def test_codex_home_in_the_no_read_list(self):
        self.assertIn("HANSA_CODEX_HOME", self.refused(HANSA_CODEX_HOME=str(self.secret / "codex")))

    def test_codex_program_in_the_no_read_list(self):
        fake = self.secret / "codex"
        fake.symlink_to(FAKES / "codex")
        self.assertIn("HANSA_CODEX_BIN", self.refused(HANSA_CODEX_BIN=str(fake)))

    def test_codex_home_in_the_worktree(self):
        self.assertIn("in the worktree", self.refused(HANSA_CODEX_HOME=str(self.wt / "codex-home")))

    def test_settings_unset(self):
        self.assertIn("HANSA_CODEX_BIN is not set", self.refused(HANSA_CODEX_BIN=""))

    def settings_from(self, codex_env="", trial_env="", env=None):
        """shim.settings(env) with these files as codex.env and trial.env beside it; '' leaves one out."""
        kept = shim.CODEX_ENV, shim.OLD_ENV
        shim.CODEX_ENV, shim.OLD_ENV = self.root / "codex.env", self.root / "trial.env"
        try:
            for f, text in ((shim.CODEX_ENV, codex_env), (shim.OLD_ENV, trial_env)):
                if text:
                    f.write_text(text)
            return shim.settings(env or {})
        finally:
            for f in (shim.CODEX_ENV, shim.OLD_ENV):
                f.unlink(missing_ok=True)
            shim.CODEX_ENV, shim.OLD_ENV = kept

    def test_settings_from_codex_env_file(self):
        # hansa gives a worker a clean environment: the adapter's settings come from codex.env, the environment first
        got = self.settings_from(codex_env="# the desk's\nHANSA_CODEX_BIN=/opt/codex/bin/codex\n"
                                           "HANSA_CODEX_HOME='~/example-codex-home'\n"
                                           "HANSA_CODEX_MODEL=gpt-y\nOTHER=x\n",
                                 env={"HANSA_CODEX_MODEL": "gpt-z"})
        self.assertEqual(got, {"HANSA_CODEX_BIN": "/opt/codex/bin/codex",
                               "HANSA_CODEX_HOME": os.path.expanduser("~/example-codex-home"),
                               "HANSA_CODEX_MODEL": "gpt-z"})
        self.assertEqual(shim.CODEX_ENV, Path(shim.__file__).resolve().parent.parent / "codex.env")

    def test_settings_from_trial_env_file(self):
        # a desk set up before codex.env: its trial.env is read while there is no codex.env, and never beside one
        old = "HANSA_CODEX_BIN=/opt/codex/bin/codex\nHANSA_CODEX_MODEL=gpt-y\n"
        self.assertEqual(self.settings_from(trial_env=old, env={"HANSA_CODEX_MODEL": "gpt-z"}),
                         {"HANSA_CODEX_BIN": "/opt/codex/bin/codex", "HANSA_CODEX_MODEL": "gpt-z"})
        self.assertEqual(self.settings_from(codex_env="HANSA_CODEX_EFFORT=low\n", trial_env=old),
                         {"HANSA_CODEX_EFFORT": "low"})
        self.assertEqual(self.settings_from(), {})
        self.assertEqual(shim.OLD_ENV, Path(shim.__file__).resolve().parent.parent / "trial.env")
