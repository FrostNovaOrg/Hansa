"""Installation diagnostics with executable fakes, no real login or sandbox."""
import contextlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch

from hansa import cli, doctor
from hansa.desk import Desk, program


class DoctorTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name).resolve()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.calls = self.tmp / "calls.jsonl"
        env = patch.dict(os.environ, {"HOME": str(self.tmp), "PATH": str(self.bin), "LANG": "C",
                                     "TMPDIR": str(self.tmp), "PYTHONDONTWRITEBYTECODE": "1"}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        platform = patch.object(doctor.sys, "platform", "darwin")
        platform.start()
        self.addCleanup(platform.stop)
        # Exercise desk.program's real fallback search, excluding tools on the host.
        fallback = patch.object(doctor, "program", side_effect=lambda name, env:
                                self.local_program(name, env))
        fallback.start()
        self.addCleanup(fallback.stop)
        for name, body in {"git": "print('git version 2.43.0')",
                           "node": "print('v22.12.0')", "tmux": "print('tmux 3.5')",
                           "bwrap": "", "socat": "", "apt-get": ""}.items():
            self.tool(name, body)
        self.claude()
        self.package = self.tmp / "lib/node_modules/@anthropic-ai/sandbox-runtime"
        (self.package / "dist").mkdir(parents=True)
        self.metadata(doctor.SRT_VERSION)
        target = self.tool("srt", "", self.package / "dist/cli.js")
        (self.bin / "srt").symlink_to(target)

    def local_program(self, name, env):
        path = program(name, env)
        return path if path and str(self.tmp) in path else ""

    def tool(self, name, body, path=None):
        path = path or self.bin / name
        path.parent.mkdir(parents=True, exist_ok=True)
        source = f"""
import json, os, sys
from pathlib import Path
record = dict(name={name!r}, args=sys.argv[1:], cwd=os.getcwd(),
              env={{k: v for k, v in os.environ.items() if k.startswith(('CLAUDE', 'AI_AGENT')) or k == 'TMPDIR'}})
if '--settings' in sys.argv:
    record['settings'] = json.loads(Path(sys.argv[sys.argv.index('--settings') + 1]).read_text())
with open({str(self.calls)!r}, 'a') as out:
    out.write(json.dumps(record) + '\\n')
"""
        path.write_text("#!" + sys.executable + "\n" + source + textwrap.dedent(body), encoding="utf-8")
        path.chmod(0o755)
        return path

    def claude(self, version="2.1.306", login=0, path=None):
        return self.tool("claude", f"""
            if sys.argv[1:] == ['auth', 'status']:
                print('private auth stdout')
                print('private auth stderr', file=sys.stderr)
                sys.exit({login})
            print({version!r} + ' (Claude Code)')
        """, path)

    def metadata(self, version):
        (self.package / "package.json").write_text(json.dumps(
            {"name": "@anthropic-ai/sandbox-runtime", "version": version}), encoding="utf-8")

    def check(self, platform="darwin", **kwargs):
        with patch.object(doctor.sys, "platform", platform):
            return doctor.check(**kwargs)

    def rows(self, **kwargs):
        return {r["name"]: r for r in self.check(**kwargs)["rows"]}

    def recorded(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def command(self, *args):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            try:
                code = cli.main(["doctor", *args])
            except SystemExit as e:
                code = e.code
        return code, out.getvalue()

    def test_all_rows_and_commands_on_both_systems(self):
        for platform in ("darwin", "linux"):
            with self.subTest(platform=platform):
                result = self.check(platform)
                self.assertTrue(result["ok"], result)
                expected = ["Python", "Git", "Node", "Claude Code", "Claude login", "srt", "tmux"]
                if platform == "linux":
                    expected += ["bubblewrap", "socat"]
                self.assertEqual([r["name"] for r in result["rows"]], expected + ["Sandbox"])
                for row in result["rows"]:
                    self.assertEqual(set(row), {"name", "state", "found", "need", "fix"})
                    self.assertEqual(row["state"], "ok", row)
                    self.assertTrue(row["found"] and row["need"])
                    self.assertEqual(row["fix"], "")
                self.assertNotIn("private auth", json.dumps(result))
        calls = self.recorded()
        for name, args in (("git", ["--version"]), ("claude", ["--version"]),
                           ("claude", ["auth", "status"]), ("node", ["--version"]), ("tmux", ["-V"])):
            self.assertTrue(any(c["name"] == name and c["args"] == args for c in calls))
        self.assertTrue(all(c["args"][-2:] == ["--", "/usr/bin/true"] for c in calls if c["name"] == "srt"))
        self.assertFalse(any(c["args"] == ["--version"] for c in calls if c["name"] == "srt"))

    def test_python_minimum_and_running_interpreter(self):
        for version, state in (((3, 8, 20), "fails"), ((3, 9, 0), "ok"), ((3, 14, 0), "ok")):
            with self.subTest(version=version), patch.object(doctor.sys, "version_info", version):
                row = self.rows()["Python"]
                self.assertEqual(row["state"], state)
                self.assertIn(sys.executable, row["found"])
                if state == "fails":
                    self.assertEqual(row["fix"], "brew install python")
        with patch.object(doctor.sys, "version_info", (3, 8, 20)):
            self.assertEqual(self.rows(platform="linux")["Python"]["fix"], "sudo apt-get install python3")

    def test_missing_tools_and_platform_specific_fixes(self):
        for path in self.bin.iterdir():
            if path.name != "apt-get":
                path.unlink()
        for platform, prefix in (("darwin", "brew install "), ("linux", "sudo apt-get install ")):
            with self.subTest(platform=platform):
                rows = self.rows(platform=platform)
                for label, package in (("Git", "git"), ("tmux", "tmux")):
                    self.assertEqual(rows[label]["state"], "note" if label == "tmux" else "fails")
                    self.assertEqual(rows[label]["fix"], prefix + package)
                self.assertIn("hansa up", rows["tmux"]["need"])
                self.assertEqual(rows["Claude Code"]["fix"], "npm install -g @anthropic-ai/claude-code@2.1.306")
                self.assertEqual(rows["srt"]["fix"], "npm install -g @anthropic-ai/sandbox-runtime@0.0.79")
                self.assertEqual(rows["srt"]["state"], "fails")
                self.assertEqual(rows["Claude login"]["state"], "fails")
                self.assertIn("claude auth login", rows["Claude login"]["fix"])
                self.assertEqual(rows["Sandbox"]["state"], "skipped")
                self.assertEqual(rows["Sandbox"]["fix"], rows["srt"]["fix"])
                if platform == "linux":
                    for label in ("bubblewrap", "socat"):
                        self.assertEqual(rows[label]["state"], "fails")
                        self.assertEqual(rows[label]["fix"], prefix + label)
        (self.bin / "apt-get").unlink()
        rows = self.rows(platform="linux")
        for label in ("Git", "tmux", "bubblewrap", "socat"):
            self.assertIn("docs/getting-started.md", rows[label]["fix"])

    def test_node_fix_uses_brew_or_node_download_for_missing_and_old_versions(self):
        for platform in ("darwin", "linux", "freebsd"):
            for apt in (True, False):
                if apt:
                    self.tool("apt-get", "")
                else:
                    (self.bin / "apt-get").unlink()
                for version in (None, "v18.19.0", "v20.19.0", "v22.11.9"):
                    with self.subTest(platform=platform, apt=apt, version=version):
                        if version is None:
                            (self.bin / "node").unlink(missing_ok=True)
                        else:
                            self.tool("node", f"print({version!r})")
                        row = self.rows(platform=platform)["Node"]
                        self.assertEqual(row["state"], "fails")
                        if platform == "darwin":
                            self.assertEqual(row["fix"], "brew install node")
                        else:
                            self.assertIn("https://nodejs.org/en/download", row["fix"])
                            self.assertIn("22.12.0 or newer", row["fix"])
                            self.assertNotIn("apt-get", row["fix"])
        row = self.rows(platform="linux", lang="zh")["Node"]
        self.assertIn("https://nodejs.org/en/download", row["fix"])
        self.assertIn("22.12.0", row["fix"])
        self.assertIn("更新", row["fix"])

    def test_guide_paths_follow_language_and_installed_chinese_file(self):
        root = self.tmp / "installation"
        chinese = root / "docs/zh-CN/getting-started.md"
        chinese.parent.mkdir(parents=True)
        for name in ("git", "apt-get"):
            (self.bin / name).unlink()
        self.tool("srt", "raise SystemExit(1)", self.package / "dist/cli.js")
        for present in (True, False):
            if present:
                chinese.write_text("# 上手指南\n", encoding="utf-8")
            else:
                chinese.unlink()
            for lang in ("en", "zh", None):
                guide = "docs/zh-CN/getting-started.md" if present and lang != "en" else "docs/getting-started.md"
                section = "#安装所需工具" if present and lang != "en" else "#install-the-tools"
                args = ["--lang", lang] if lang else []
                with self.subTest(present=present, lang=lang), \
                        patch.object(doctor, "__file__", str(root / "hansa/doctor.py")), \
                        patch.object(doctor.sys, "platform", "linux"), \
                        patch.dict(os.environ, {"LANG": "zh_CN.UTF-8"}):
                    code, output = self.command(*args)
                    self.assertEqual(code, 1)
                    self.assertIn(("See " if lang == "en" else "参阅 ") + guide, output)
                    self.assertIn(guide + section, output)
                    other = "docs/getting-started.md" if present and lang != "en" else "docs/zh-CN/getting-started.md"
                    self.assertNotIn(other, output)
                    with patch.dict(os.environ, {"LANG": "C" if lang == "en" else "zh_CN.UTF-8"}):
                        _, help_text = self.command("--help")
                    self.assertIn(guide, help_text)
                    self.assertNotIn(other, help_text)

    def test_old_unknown_and_new_versions(self):
        for tool, label, versions in (("claude", "Claude Code", ("2.1.305", "unknown", "2.1.306", "2.2.0")),
                                      ("node", "Node", ("v22.11.0", "unknown", "v22.12.0", "v24.22.0"))):
            for index, version in enumerate(versions):
                with self.subTest(tool=tool, version=version):
                    if tool == "claude":
                        self.claude(version)
                    else:
                        self.tool(tool, f"print({version!r})")
                    row = self.rows()[label]
                    self.assertEqual(row["state"], "fails" if index < 2 else "ok")
                    self.assertIn(version, row["found"])

    def test_login_uses_only_exit_code_and_never_exposes_output(self):
        self.claude(login=1)
        code, output = self.command("--json")
        self.assertEqual(code, 1)
        row = next(r for r in json.loads(output)["rows"] if r["name"] == "Claude login")
        self.assertEqual(row["state"], "fails")
        self.assertIn("claude auth login", row["fix"])
        self.assertIn("yourself", row["fix"])
        self.assertNotIn("private auth", output)
        self.assertTrue(any(c["args"] == ["auth", "status"] for c in self.recorded()))

    def test_srt_package_version_notes_do_not_fail(self):
        for version in ("0.0.78", "0.0.80", None):
            with self.subTest(version=version):
                self.metadata(version)
                result = self.check()
                self.assertTrue(result["ok"])
                row = next(r for r in result["rows"] if r["name"] == "srt")
                self.assertEqual(row["state"], "note")
                self.assertIn("@0.0.79", row["fix"])
        for content in ("broken JSON", "[]", '{"name": "another-package", "version": "0.0.79"}'):
            with self.subTest(content=content):
                (self.package / "package.json").write_text(content)
                self.assertEqual(self.rows()["srt"]["state"], "note")

    def test_extra_folder_tools_get_path_notes(self):
        (self.bin / "claude").unlink()
        fallback = self.claude(path=self.tmp / ".local/bin/claude")
        (self.bin / "tmux").unlink()
        tmux = self.tool("tmux", "print('tmux 3.5')", self.tmp / "extra tools/tmux")
        with patch.dict(os.environ, {"HANSA_TMUX": str(tmux)}):
            result = self.check()
        self.assertTrue(result["ok"])
        rows = {r["name"]: r for r in result["rows"]}
        for label, tool in (("Claude Code", fallback), ("tmux", tmux)):
            self.assertEqual(rows[label]["state"], "note")
            self.assertIn("outside PATH", rows[label]["found"])
            self.assertIn("hansa serve", rows[label]["found"])
            self.assertIn(str(tool.parent), rows[label]["fix"])
            self.assertIn("export PATH=", rows[label]["fix"])
        self.claude(version="2.1.1", path=fallback)
        self.assertEqual(self.rows()["Claude Code"]["state"], "fails")

    def test_command_failure_and_cannot_start_do_not_stop_other_rows(self):
        for name, label in (("git", "Git"), ("claude", "Claude Code"), ("node", "Node"), ("tmux", "tmux")):
            path = self.bin / name
            original = path.read_text()
            for body in ("#!/missing/interpreter\n", "#!" + sys.executable + "\nraise SystemExit(4)\n"):
                with self.subTest(name=name, body=body):
                    path.write_text(body)
                    rows = self.rows()
                    self.assertEqual(rows[label]["state"], "fails")
                    self.assertEqual(rows["Sandbox"]["state"], "ok")
                    if name == "claude":
                        self.assertEqual(rows["Claude login"]["state"], "fails")
            path.write_text(original)

    def test_hung_tool_times_out_and_remaining_checks_run(self):
        self.tool("git", """
            import signal, subprocess
            child = subprocess.Popen([sys.executable, '-c',
                                      'import signal; signal.alarm(45); signal.pause()'])
            child.wait()
        """)
        started = time.monotonic()
        rows = self.rows()
        self.assertGreaterEqual(time.monotonic() - started, 15)
        self.assertLess(time.monotonic() - started, 30)
        self.assertEqual(rows["Git"]["state"], "fails")
        self.assertIn("15", rows["Git"]["found"])
        self.assertEqual(rows["Sandbox"]["state"], "ok")

    def test_detached_child_holding_output_does_not_hold_doctor(self):
        ready, stop, stopped = (self.tmp / name for name in ("child-ready", "stop-child", "child-stopped"))
        child_source = textwrap.dedent(f"""
            import signal, time
            from pathlib import Path
            signal.alarm(45)
            Path({str(ready)!r}).touch()
            try:
                while not Path({str(stop)!r}).exists():
                    time.sleep(0.01)
            finally:
                Path({str(stopped)!r}).touch()
        """)
        self.tool("git", f"""
            import subprocess, time
            subprocess.Popen([sys.executable, '-c', {child_source!r}], start_new_session=True)
            deadline = time.monotonic() + 10
            while not Path({str(ready)!r}).exists():
                if time.monotonic() > deadline:
                    raise RuntimeError('child did not start')
                time.sleep(0.01)
        """)
        started = time.monotonic()
        try:
            rows = self.rows()
            self.assertLess(time.monotonic() - started, 30)
            self.assertEqual(rows["Git"]["state"], "fails")
            self.assertIn("15", rows["Git"]["found"])
            self.assertNotIn("cannot start", rows["Git"]["found"])
            self.assertEqual(rows["Sandbox"]["state"], "ok")
        finally:
            stop.touch()
            deadline = time.monotonic() + 10
            while not stopped.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(stopped.exists(), "detached child did not stop")

    def test_sandbox_settings_environment_and_cleanup(self):
        with patch.dict(os.environ, {"CLAUDECODE": "1", "CLAUDE_PID": "42", "AI_AGENT": "agent",
                                     "CLAUDE_CODE_SESSION_ID": "secret", "CLAUDE_CODE_MESSAGING_TOKEN": "secret"}):
            result = self.check()
        self.assertTrue(result["ok"])
        for call in self.recorded():
            self.assertFalse(any(k in call["env"] for k in ("CLAUDECODE", "CLAUDE_PID", "AI_AGENT",
                                                            "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_MESSAGING_TOKEN")))
            if call["name"] == "srt":
                self.assertEqual(call["settings"]["network"]["allowedDomains"], [])
                self.assertEqual(call["settings"]["filesystem"]["allowWrite"], [call["cwd"]])
                self.assertEqual(call["env"]["TMPDIR"], call["cwd"])
                self.assertEqual(call["env"]["CLAUDE_CODE_TMPDIR"], call["cwd"])
                self.assertFalse(Path(call["cwd"]).exists())

    def test_sandbox_failure_includes_last_lines_and_cleans_up(self):
        self.tool("srt", "for i in range(20): print('sandbox-line-' + str(i))\nraise SystemExit(5)\n",
                  self.package / "dist/cli.js")
        row = self.rows()["Sandbox"]
        self.assertEqual(row["state"], "fails")
        self.assertIn("exit 5", row["found"])
        self.assertIn("sandbox-line-19", row["found"])
        self.assertNotIn("sandbox-line-0", row["found"])
        self.assertTrue(row["fix"])
        self.assertFalse(Path(self.recorded()[-1]["cwd"]).exists())

    def test_sandbox_fix_includes_missing_prerequisites(self):
        self.tool("srt", "raise SystemExit(1)", self.package / "dist/cli.js")
        (self.bin / "node").unlink()
        self.assertIn("brew install node", self.rows()["Sandbox"]["fix"])
        (self.bin / "bwrap").unlink()
        (self.bin / "socat").unlink()
        fix = self.rows(platform="linux")["Sandbox"]["fix"]
        self.assertIn("https://nodejs.org/en/download", fix)
        self.assertIn("22.12.0 or newer", fix)
        for package in ("bubblewrap", "socat"):
            self.assertIn("sudo apt-get install " + package, fix)
        self.assertIn("docs/getting-started.md", fix)

    def test_sandbox_timeout_start_and_temporary_folder_errors(self):
        communicate = subprocess.Popen.communicate
        sandbox_folders = []
        drain_timeout = False

        def fail_srt(process, *args, **kwargs):
            if "--settings" in process.args:
                if kwargs.get("timeout") == 15:
                    self.assertEqual(kwargs["timeout"], 15)
                    sandbox_folders.append(Path(process.args[2]).parent)
                    raise subprocess.TimeoutExpired(process.args, 15)
                self.assertGreater(kwargs["timeout"], 0)
                self.assertLessEqual(kwargs["timeout"], 2)
                communicate(process, *args, **kwargs)
                if drain_timeout:
                    raise subprocess.TimeoutExpired(process.args, kwargs["timeout"],
                                                    output=b"sandbox timeout detail\n")
                return "sandbox timeout detail\n", None
            self.assertEqual(kwargs["timeout"], 15)
            return communicate(process, *args, **kwargs)

        for drain_timeout in (False, True):
            with self.subTest(drain_timeout=drain_timeout), \
                    patch.object(doctor.subprocess.Popen, "communicate", fail_srt), \
                    patch.object(doctor.os, "killpg", side_effect=PermissionError("group already exited")):
                row = self.rows()["Sandbox"]
            self.assertEqual(row["state"], "fails")
            self.assertIn("sandbox timeout detail", row["found"])
            self.assertIn("15", row["found"])
        self.assertTrue(sandbox_folders)
        self.assertTrue(all(not folder.exists() for folder in sandbox_folders))
        (self.package / "dist/cli.js").write_text("#!/missing/interpreter\n")
        self.assertEqual(self.rows()["Sandbox"]["state"], "fails")
        with patch.object(doctor.tempfile, "TemporaryDirectory", side_effect=OSError("cannot create temporary folder")):
            self.assertIn("cannot create", self.rows()["Sandbox"]["found"])

    def test_reads_only_package_metadata_and_writes_only_probe_settings(self):
        read, write = Path.read_text, Path.write_text
        writes = []

        def read_metadata(path, *args, **kwargs):
            self.assertEqual(path.name, "package.json", "doctor must not read login or key files")
            return read(path, *args, **kwargs)

        def write_probe(path, *args, **kwargs):
            self.assertEqual(path.name, "settings.json")
            self.assertTrue(path.parent.name.startswith("hansa-doctor-"))
            writes.append(path)
            return write(path, *args, **kwargs)

        with patch.object(Path, "read_text", read_metadata), patch.object(Path, "write_text", write_probe):
            self.assertTrue(self.check()["ok"])
        self.assertEqual(len(writes), 1)
        self.assertFalse(writes[0].parent.exists())

    def test_json_exit_codes_languages_and_no_desk_hooks(self):
        with patch.object(Desk, "find", side_effect=AssertionError("doctor needs no desk")), \
                patch.object(cli, "_as_desk_account", side_effect=AssertionError("no account switch")), \
                patch.object(cli.cost, "note_session", side_effect=AssertionError("no session writes")), \
                patch.object(cli.notify, "session_here", return_value=True):
            code, output = self.command("--json")
        self.assertEqual(code, 0)
        result = json.loads(output)
        self.assertTrue(result["ok"])
        self.assertEqual(set(result), {"ok", "rows"})
        (self.bin / "tmux").unlink()
        self.assertEqual(self.command("--json")[0], 0)
        (self.bin / "git").unlink()
        code, output = self.command("--json")
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(output)["ok"])
        with patch.dict(os.environ, {"LANG": "zh_CN.UTF-8"}):
            self.assertIn("只读检查", self.command()[1])
            self.assertIn("read-only checks", self.command("--lang", "en")[1])
        with patch.dict(os.environ, {"LC_ALL": "en_US.UTF-8", "LANG": "zh_CN.UTF-8"}):
            self.assertIn("read-only checks", self.command()[1])
            self.assertIn("只读检查", self.command("--lang", "zh")[1])
            self.assertIn("未找到", self.command("--json", "--lang", "zh")[1])

    def test_cli_runs_outside_a_desk(self):
        root = Path(__file__).resolve().parent.parent
        result = subprocess.run([sys.executable, "-B", "-m", "hansa", "doctor", "--json"],
                                cwd=self.tmp, env={**os.environ, "PYTHONPATH": str(root)},
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["ok"])
        self.assertFalse((self.tmp / ".hansa").exists())

    def test_guide_table_matches_doctor_versions_and_stays_short(self):
        guide = (Path(__file__).resolve().parent.parent / "docs/getting-started.md").read_text()
        table = dict(re.findall(r"^\| (.+?) \| (.+?) \|$", guide, re.M))
        for tool, version in (("Python", doctor.PYTHON_MIN),
                              ("[Claude Code](https://code.claude.com/docs/en/overview)", doctor.CLAUDE_MIN),
                              ("Node", doctor.NODE_MIN)):
            self.assertEqual(re.search(r"\d+\.\d+(?:\.\d+)?", table[tool]).group(), doctor.version_text(version))
        self.assertEqual(table["srt (`@anthropic-ai/sandbox-runtime`)"], doctor.SRT_VERSION)
        self.assertLess(len(guide.split()), 1500)
        commands = "\n".join(re.findall(r"```sh\n(.*?)```", guide, re.S))
        self.assertRegex(commands, r"(?m)^hansa doctor$")
