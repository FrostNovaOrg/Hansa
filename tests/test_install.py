"""Account installer against disposable local repositories; no network or real HOME."""
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parent.parent / "install.sh"
GIT = shutil.which("git")


class InstallTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name).resolve()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.tools = self.tmp / "tools"
        self.tools.mkdir()
        self.install = self.home / ".local/share/hansa"
        self.link = self.home / ".local/bin/hansa"
        self.source = self.tmp / "source"
        # Isolate Git from the host's identity, templates, config and lane prelude.
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("GIT_", "HANSA_", "LC_"))}
        self.env.update(HOME=str(self.home), HANSA_SOURCE=str(self.source),
                        LANG="C", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_TEMPLATE_DIR="", PATH=str(self.tools))
        for name in ("mkdir", "rm", "ln"):
            (self.tools / name).symlink_to(shutil.which(name))
        (self.tools / "git").symlink_to(GIT)
        (self.tools / "python3").symlink_to(sys.executable)
        self.git("-c", "init.defaultBranch=fixture-default", "init", str(self.source))
        (self.source / "bin").mkdir()
        (self.source / "bin/hansa").write_text("#!/bin/sh\nprintf 'fixture hansa\\n'\n")
        (self.source / "bin/hansa").chmod(0o755)
        (self.source / "docs").mkdir()
        (self.source / "docs/getting-started.md").write_text("fixture guide\n")
        (self.source / "docs/zh-CN").mkdir()
        (self.source / "docs/zh-CN/getting-started.md").write_text("中文版指南\n", encoding="utf-8")
        self.commit(self.source, "initial")
        for name in (".zshrc", ".bashrc", ".profile", "personal-file"):
            (self.home / name).write_text("keep me\n")

    def git(self, *args, cwd=None):
        result = subprocess.run([GIT, *args], cwd=cwd, env=self.env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def commit(self, repo, message):
        self.git("add", ".", cwd=repo)
        self.git("-c", "user.name=Installer Test", "-c", "user.email=installer@example.invalid",
                 "commit", "-m", message, cwd=repo)

    def run_install(self, *, pipe=False, **env):
        # input for sh -s is the script itself, like curl | sh; any read would steal code.
        result = subprocess.run(["/bin/sh", "-s"] if pipe else ["/bin/sh", str(SCRIPT)],
                                input=SCRIPT.read_text() if pipe else "must not be read\n",
                                env=dict(self.env, **env), cwd=self.tmp,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, timeout=30)
        self.output = result.stdout + result.stderr
        return result

    def snapshot(self, root=None):
        root = root or self.home
        return {str(p.relative_to(root)): ("link", os.readlink(p)) if p.is_symlink() else
                ("dir",) if p.is_dir() else ("file", p.read_bytes())
                for p in root.rglob("*")}

    def assert_success(self, result):
        self.assertEqual(result.returncode, 0, self.output)
        self.assertTrue(self.link.is_symlink())
        self.assertEqual(self.link.resolve(), self.install / "bin/hansa")

    def tool(self, name, body):
        path = self.tools / name
        path.unlink(missing_ok=True)
        path.write_text("#!" + sys.executable + " -B\n" + body)
        path.chmod(0o755)

    def test_missing_git_missing_python_and_old_python_write_nothing(self):
        before = self.snapshot()
        for missing in ("git", "python3"):
            with self.subTest(missing=missing):
                tool = self.tools / missing
                target = tool.readlink()
                tool.unlink()
                try:
                    result = self.run_install()
                    self.assertEqual(result.returncode, 1, self.output)
                    self.assertIn(missing, self.output)
                    self.assertEqual(self.snapshot(), before)
                finally:
                    tool.symlink_to(target)
        # Run the actual version expression against a simulated older interpreter.
        self.tool("python3", "import sys\nsys.version_info = (3, 8, 20)\n"
                  "exec(sys.argv[sys.argv.index('-c') + 1])\n")
        result = self.run_install()
        self.assertEqual(result.returncode, 1, self.output)
        self.assertIn("3.9", self.output)
        self.assertEqual(self.snapshot(), before)

    def test_fresh_install_default_branch_and_only_expected_files(self):
        before = self.snapshot()
        self.assert_success(self.run_install(PATH=str(self.link.parent) + ":" + str(self.tools)))
        self.assertIn("Installed", self.output)
        self.assertEqual(self.git("branch", "--show-current", cwd=self.install), "fixture-default")
        self.assertEqual(self.git("remote", "get-url", "origin", cwd=self.install), str(self.source))
        after = self.snapshot()
        for name, content in before.items():
            self.assertEqual(after[name], content)
        for name in after.keys() - before.keys():
            self.assertTrue(name in (".local", ".local/bin", ".local/share", ".local/bin/hansa")
                            or name.startswith(".local/share/hansa"), name)
        self.assertNotIn('export PATH=', self.output)
        self.assert_next_steps("hansa")

    def test_missing_or_relative_home_is_rejected_without_writes(self):
        before = self.snapshot()
        for home in ("", "relative-home"):
            with self.subTest(home=home):
                self.assertEqual(self.run_install(HOME=home).returncode, 1, self.output)
                self.assertIn("HOME", self.output)
                self.assertEqual(self.snapshot(), before)
                self.assertFalse((self.tmp / "relative-home").exists())

    def assert_next_steps(self, command):
        commands = [command + " doctor", command + " init ~/shop/desk --repo ~/shop/repo", command]
        lines = self.output.splitlines()
        indices = [next(i for i, line in enumerate(lines) if line.startswith(cmd + " "))
                   for cmd in commands[:2]]
        indices.append(next(i for i, line in enumerate(lines) if line.startswith(command + " ")
                            and "opens the menu" in line))
        ssh = "ssh -t -L 8765:127.0.0.1:8765 YOUR-HOST " + shlex.quote(shlex.quote(str(self.link)))
        indices += [lines.index(ssh), next(i for i, line in enumerate(lines)
                                         if str(self.install / "docs/getting-started.md") in line)]
        self.assertEqual(indices, sorted(indices))
        for words in ("checks the tools", "what is missing", "creates a desk", "s starts it",
                      "Enter opens its coordinator"):
            self.assertIn(words, self.output)

    def test_pipe_install_and_missing_path_print_full_commands(self):
        self.assert_success(self.run_install(pipe=True))
        self.assert_next_steps(shlex.quote(str(self.link)))
        next_steps = self.output.split("\n\n", 1)[1]
        self.assertTrue(next_steps.startswith('export PATH="$HOME/.local/bin:$PATH"\n'), self.output)

    def test_path_matches_a_whole_entry_and_accepts_trailing_slash(self):
        self.assert_success(self.run_install(PATH=str(self.link.parent) + "-other:" + str(self.tools)))
        self.assertIn('export PATH=', self.output)
        self.assert_success(self.run_install(PATH=str(self.tools) + ":" + str(self.link.parent) + "/"))
        self.assertNotIn('export PATH=', self.output)
        self.assert_next_steps("hansa")

    def test_home_with_shell_characters_has_copyable_local_and_ssh_commands(self):
        self.home = self.tmp / "home with 'quotes' and $cash"
        self.home.mkdir()
        self.env["HOME"] = str(self.home)
        self.install = self.home / ".local/share/hansa"
        self.link = self.home / ".local/bin/hansa"
        self.assert_success(self.run_install())
        self.assert_next_steps(shlex.quote(str(self.link)))
        local = next(line for line in self.output.splitlines() if " doctor " in line)
        self.assertEqual(shlex.split(local)[:2], [str(self.link), "doctor"])
        ssh = next(line for line in self.output.splitlines() if line.startswith("ssh "))
        remote = shlex.split(ssh)[-1]
        self.assertEqual(shlex.split(remote), [str(self.link)])

    def test_update_fast_forwards_and_recreates_missing_or_relative_link(self):
        self.assert_success(self.run_install())
        for relative in (False, True):
            with self.subTest(relative_link=relative):
                self.link.unlink()
                if relative:
                    self.link.symlink_to("../share/hansa/bin/hansa")
                (self.source / "new-file").write_text(str(relative))
                self.commit(self.source, "update")
                self.assert_success(self.run_install())
                self.assertIn("Updated", self.output)
                self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.install),
                                 self.git("rev-parse", "HEAD", cwd=self.source))
                self.assert_next_steps(shlex.quote(str(self.link)))

    def test_failed_update_preserves_head_worktree_and_link(self):
        self.assert_success(self.run_install())
        (self.install / "local-file").write_text("local commit\n")
        self.commit(self.install, "diverge")
        (self.source / "remote-file").write_text("remote commit\n")
        self.commit(self.source, "diverge remotely")
        head = self.git("rev-parse", "HEAD", cwd=self.install)
        files = {k: v for k, v in self.snapshot().items() if "/.git/" not in k}
        result = self.run_install()
        self.assertEqual(result.returncode, 1, self.output)
        self.assertIn("Update failed", self.output)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.install), head)
        self.assertEqual({k: v for k, v in self.snapshot().items() if "/.git/" not in k}, files)
        self.assertNotIn(" doctor ", self.output)

    def test_failed_update_with_local_edits_preserves_them(self):
        self.assert_success(self.run_install())
        launcher = self.install / "bin/hansa"
        launcher.write_text("local changes\n")
        (self.source / "bin/hansa").write_text("remote changes\n")
        self.commit(self.source, "changed launcher")
        head = self.git("rev-parse", "HEAD", cwd=self.install)
        self.assertEqual(self.run_install().returncode, 1, self.output)
        self.assertIn("Update failed", self.output)
        self.assertEqual(launcher.read_text(), "local changes\n")
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.install), head)

    def test_existing_invalid_installation_is_untouched(self):
        for kind in ("file", "folder", "clone-without-launcher", "nested-in-clone", "broken-link"):
            with self.subTest(kind=kind):
                self.install.parent.mkdir(parents=True, exist_ok=True)
                if kind == "file":
                    self.install.write_text("keep\n")
                elif kind == "broken-link":
                    self.install.symlink_to(self.tmp / "absent")
                else:
                    self.install.mkdir()
                    if kind == "clone-without-launcher":
                        self.git("init", str(self.install))
                    else:
                        (self.install / "bin").mkdir()
                        (self.install / "bin/hansa").write_text("keep\n")
                        if kind == "nested-in-clone":
                            self.git("init", str(self.install.parent))
                before = self.snapshot()
                self.assertEqual(self.run_install().returncode, 1, self.output)
                self.assertIn("not a hansa clone", self.output)
                self.assertEqual(self.snapshot(), before)
                if self.install.is_dir() and not self.install.is_symlink():
                    shutil.rmtree(self.install)
                else:
                    self.install.unlink()

    def test_existing_foreign_launcher_is_untouched_before_clone_or_update(self):
        for installed in (False, True):
            with self.subTest(installed=installed):
                if installed:
                    self.assert_success(self.run_install())
                    self.link.unlink()
                self.link.parent.mkdir(parents=True, exist_ok=True)
                for kind in ("file", "folder", "foreign-link", "dangling-link"):
                    with self.subTest(kind=kind):
                        if kind == "file":
                            self.link.write_text("keep\n")
                        elif kind == "folder":
                            self.link.mkdir()
                        else:
                            self.link.symlink_to(self.source / "bin/hansa" if kind == "foreign-link"
                                                 else self.tmp / "missing")
                        before = self.snapshot()
                        self.assertEqual(self.run_install().returncode, 1, self.output)
                        self.assertIn("not a link", self.output)
                        self.assertEqual(self.snapshot(), before)
                        if self.link.is_dir() and not self.link.is_symlink():
                            self.link.rmdir()
                        else:
                            self.link.unlink()

    def test_failed_clone_shows_git_error_and_removes_partial_folder(self):
        result = self.run_install(HANSA_SOURCE=str(self.tmp / "missing-source"))
        self.assertEqual(result.returncode, 1, self.output)
        self.assertIn("does not exist", self.output)
        self.assertFalse(self.install.exists())
        self.assertFalse(self.link.exists())
        # Git normally cleans up itself; also exercise cleanup when it leaves a folder.
        self.tool("git", """import pathlib, sys
assert sys.argv[1] == 'clone'
target = pathlib.Path(sys.argv[-1])
target.mkdir()
(target / 'partial').write_text('incomplete')
print('fixture git clone failed', file=sys.stderr)
sys.exit(42)
""")
        self.assertEqual(self.run_install().returncode, 1, self.output)
        self.assertIn("fixture git clone failed", self.output)
        self.assertFalse(self.install.exists())

    def test_locale_precedence_and_chinese_or_english_next_steps(self):
        cases = [({}, False), ({"LANG": "zh_CN.UTF-8"}, True),
                 ({"LANG": "zh_TW.UTF-8", "LC_MESSAGES": "en_US.UTF-8"}, False),
                 ({"LANG": "en_US.UTF-8", "LC_MESSAGES": "zh_TW.UTF-8"}, True),
                 ({"LC_MESSAGES": "zh_CN", "LC_ALL": "C"}, False),
                 ({"LC_MESSAGES": "C", "LC_ALL": "zh_HK.UTF-8"}, True),
                 ({"LC_ALL": "", "LC_MESSAGES": "", "LANG": "zh"}, True)]
        for env, chinese in cases:
            with self.subTest(env=env):
                self.assert_success(self.run_install(**env))
                self.assertEqual("检查" in self.output, chinese)
                self.assertEqual("creates a desk" in self.output, not chinese)
                guide = "docs/zh-CN/getting-started.md" if chinese else "docs/getting-started.md"
                label = "上手指南：" if chinese else "Getting started: "
                self.assertIn(label + str(self.install / guide), self.output)
                other = "docs/getting-started.md" if chinese else "docs/zh-CN/getting-started.md"
                self.assertNotIn(str(self.install / other), self.output)
                self.assertEqual((self.install / "docs/zh-CN/getting-started.md").read_text(encoding="utf-8"),
                                 "中文版指南\n")
                for command in ("doctor", "init ~/shop/desk --repo ~/shop/repo", "YOUR-HOST"):
                    self.assertIn(command, self.output)
                if chinese:
                    for words in ("工作台", "协调员", "上手指南", "Enter", "s"):
                        self.assertIn(words, self.output)

    def test_chinese_next_steps_fall_back_when_installed_chinese_guide_is_missing(self):
        self.assert_success(self.run_install(LANG="zh_CN.UTF-8"))
        (self.install / "docs/zh-CN/getting-started.md").unlink()
        self.assert_success(self.run_install(LANG="zh_CN.UTF-8"))
        self.assertIn("上手指南：" + str(self.install / "docs/getting-started.md"), self.output)
        self.assertNotIn("docs/zh-CN/", self.output)

    def test_the_last_next_step_sends_claude_code_to_setup_in_either_language(self):
        for env, words in (({}, ("Open Claude Code", "set up Hansa with me")),
                           ({"LANG": "zh_CN.UTF-8"}, ("打开 Claude Code", "和我一起装好 Hansa"))):
            with self.subTest(env=env):
                result = self.run_install(**env)
                self.assert_success(result)
                last = result.stdout.splitlines()[-1]
                self.assertIn("~/.local/share/hansa/SETUP.md", last)
                for word in words:
                    self.assertIn(word, last)

    def test_interrupted_clone_removes_partial_folder(self):
        self.tool("git", """import os, pathlib, signal, sys
target = pathlib.Path(sys.argv[-1])
target.mkdir()
(target / 'partial').write_text('incomplete')
os.kill(os.getppid(), signal.SIGTERM)
""")
        self.assertEqual(self.run_install().returncode, 1, self.output)
        self.assertFalse(self.install.exists())
        self.assertFalse(self.link.exists())

    def test_default_source_and_noninteractive_git_without_network(self):
        # Redirect the default URL to the fixture while inspecting the real clone invocation.
        self.env.pop("HANSA_SOURCE")
        self.tool("git", f"""import os, sys
assert os.environ['GIT_TERMINAL_PROMPT'] == '0'
assert 'BatchMode=yes' in os.environ['GIT_SSH_COMMAND']
assert sys.argv[1:] == ['clone', '--', 'https://github.com/FrostNovaOrg/Hansa.git', {str(self.install)!r}]
assert sys.stdin.read() == ''
os.execv({GIT!r}, [{GIT!r}, 'clone', '--', {str(self.source)!r}, sys.argv[-1]])
""")
        self.assert_success(self.run_install())


if __name__ == "__main__":
    unittest.main()
