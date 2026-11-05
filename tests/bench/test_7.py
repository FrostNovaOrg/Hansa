"""Management through the CLI and page, with fake Lima/SSH and no network."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from hansa import cli, web
from hansa.bench import vm
from hansa.desk import Desk, default_settings, validate
from tests.bench.test_6 import KEY


class BenchManagementTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.desk = Desk(temporary.name)
        self.calls, self.fail_action, self.status = [], "", "Stopped"

    def run_command(self, command, **options):
        self.calls.append(command)
        self.assertTrue(0 < options["timeout"] <= (3600 if "--name=" + vm.INSTANCE in command else 900))
        if self.fail_action and self.fail_action in command:
            raise subprocess.CalledProcessError(1, command)
        private = Path(options["env"]["HOME"])
        output = b""
        if command[0] == "ssh-keygen":
            (private / "submission").write_text("private fixture")
            (private / "submission").chmod(0o600)
            (private / "submission.pub").write_text(KEY)
        elif command[1] == "start":
            (private / "lima" / vm.INSTANCE).mkdir(exist_ok=True)
            self.status = "Running"
        elif command[1] == "stop":
            self.status = "Stopped"
        elif command[1] == "list":
            output = json.dumps(dict(name=vm.INSTANCE, status=self.status, sshLocalPort=2200)).encode()
        elif "tar" in command:
            from hansa.bench.manage import FILES
            with tarfile.open(fileobj=options["stdin"]) as bundle:
                self.assertEqual(set(bundle.getnames()), {p.name for p in FILES} | {"submission.pub"})
                for path in FILES:
                    self.assertEqual(bundle.extractfile(path.name).read(), path.read_bytes())
        elif "cat" in command:
            output = (KEY + " fixture-comment\n").encode()
        return subprocess.CompletedProcess(command, 0, output)

    def command(self, action, *args):
        initialize = vm.VM.__init__
        factory = lambda machine, private: initialize(machine, private, run=self.run_command, clock=lambda: 0)
        with patch.object(vm.VM, "__init__", factory), patch.object(vm.sys, "platform", "darwin"), \
                patch.object(cli, "_as_desk_account"), patch.object(cli.notify, "session_here", return_value=False), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            cli.main(["--desk", str(self.desk.root), "bench", action, *args])
        return out.getvalue()

    def test_page_preserves_bench(self):
        self.assertFalse(default_settings()["bench"]["enabled"])
        settings = self.desk.settings()
        bench = dict(enabled=False, generation=3, build="failed", recipe="fixture", failure="stopped",
                     acceptance=dict(generation=2, status="passed"))
        self.assertEqual(self.desk.save_settings({**settings, "bench": bench}), [])
        for value in (None, {"enabled": True}, {}):
            result = web._save_settings(self.desk, {"settings": {**settings, "bench": value}})
            self.assertTrue(result["ok"], result)
            self.assertEqual(self.desk.settings()["bench"], bench)
        settings.pop("bench")
        self.assertEqual(self.desk.save_settings(settings, by="page"), [])
        self.assertEqual(self.desk.settings()["bench"], bench)
        for value in (None, {**bench, "enabled": 1}, {**bench, "generation": -1}, {**bench, "acceptance": True}):
            self.assertTrue(any("bench" in p for p in validate({**settings, "bench": value})))

    def test_limactl_failure_messages_are_redacted(self):
        key = 'fixture-private-"provider\\key'
        self.desk.secrets_dir.mkdir()
        self.desk.key_file("fixture").write_text(key)
        run = self.run_command
        def fail(command, **options):
            if command[0] == "limactl" and ("sh" in command or "--version" in command or
                                           (action in ("build", "selftest") and command[1] == "stop")):
                return subprocess.run([sys.executable, "-c", "import sys; sys.stderr.write(" +
                                       repr("Lima refused " + key) + "); sys.exit(1)"], **options)
            return run(command, **options)
        for action in ("build", "rebuild", "selftest"):
            if action == "selftest":
                self.command("rebuild")
            with patch.object(self, "run_command", side_effect=fail):
                if action == "selftest":
                    shown = self.command(action, "--json")
                    self.assertIn("Lima refused", json.loads(shown)["acceptance"]["results"]["stopped"]["error"])
                else:
                    with self.assertRaises(SystemExit) as caught:
                        self.command(action)
                    shown = str(caught.exception)
            failure = self.desk.settings()["bench"]["failure"]
            self.assertIn("Lima refused [key of fixture]", failure)
            self.assertIn("Lima refused [key of fixture]", shown)
            if action == "build":
                self.assertIn("Lima refused [key of fixture]", failure.split("; stop: ")[1])
            self.assertNotIn(key, shown + json.dumps(self.desk.settings()) +
                             (self.desk.state_dir / "settings-history.jsonl").read_text())

    def test_page_bench_guard_independent_of_desk_guard(self):
        settings, save = self.desk.settings(), self.desk.save_settings
        changed = {**settings["bench"], "generation": 7}
        # Isolate the web guard: Desk also independently preserves bench for by="page".
        with patch.object(self.desk, "save_settings", side_effect=lambda new, **kw: save(new)):
            self.assertTrue(web._save_settings(self.desk, {"settings": {**settings, "bench": changed}})["ok"])
        self.assertEqual(self.desk.settings()["bench"], settings["bench"])

    def test_first_build_allows_twenty_minute_download(self):
        now, call, run = [0], vm.VM._call, self.run_command
        def timed(machine, *args, **kwargs):
            machine.clock = lambda: now[0]
            return call(machine, *args, **kwargs)
        def download(command, **options):
            if "--name=" + vm.INSTANCE in command:
                if options["timeout"] < 1200:
                    raise subprocess.TimeoutExpired(command, options["timeout"])
                now[0] += 1200
            return run(command, **options)
        with patch.object(vm.VM, "_call", timed), patch.object(self, "run_command", side_effect=download):
            self.command("build")
        self.assertEqual((now[0], self.desk.settings()["bench"]["build"]), (1200, "ready"))

    def check_redaction_precedes_stderr_cut(self, action, error):
        key = "fixture-secret-" + "k" * 15
        self.desk.secrets_dir.mkdir(exist_ok=True)
        self.desk.key_file("fixture").write_text(key)
        if action in ("selftest", "off"):
            self.command("build")
        fail_action = dict(build="sh", rebuild="sh", selftest="--version", off="stop")[action]
        run = self.run_command
        def fail(command, **options):
            if fail_action in command:
                options["stderr"].write(("prefix " + key + "x" * 4074 + "\nended").encode())
                raise error(command, options["timeout"]) if error is subprocess.TimeoutExpired else error(1, command)
            return run(command, **options)
        with patch.object(self, "run_command", side_effect=fail):
            if action == "selftest":
                shown = self.command(action, "--json")
                self.assertIn("[key of fixture]", json.loads(shown)["acceptance"]["error"])
            else:
                with self.assertRaises(SystemExit) as caught:
                    self.command(action)
                shown = str(caught.exception)
        failure = self.desk.settings()["bench"]["failure"]
        self.assertIn("[key of fixture]", failure)
        self.assertTrue(failure.endswith("ended"))
        self.assertNotIn(key[-15:], shown + json.dumps(self.desk.settings()) +
                         (self.desk.state_dir / "settings-history.jsonl").read_text())

    def test_redaction_precedes_stderr_cut_on_failure_and_timeout(self):
        for action, error in (("build", subprocess.CalledProcessError), ("rebuild", subprocess.TimeoutExpired)):
            self.check_redaction_precedes_stderr_cut(action, error)

    def test_selftest_redaction_precedes_stderr_cut(self):
        self.check_redaction_precedes_stderr_cut("selftest", subprocess.CalledProcessError)

    def test_off_redaction_precedes_stderr_cut(self):
        self.check_redaction_precedes_stderr_cut("off", subprocess.TimeoutExpired)

    def test_serve_broker_redacts_lima_errors_before_cut(self):
        from hansa.run import WorkerBench
        key = "fixture-secret-" + "k" * 15
        self.desk.secrets_dir.mkdir()
        self.desk.key_file("fixture").write_text(key)
        self.desk.state_dir.mkdir()
        with patch.object(vm.sys, "platform", "darwin"):
            bench = WorkerBench(self.desk)
        self.addCleanup(bench.close)
        for error in (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            for padding in ("", "x" * 4074):
                def fail(command, **options):
                    options["stderr"].write(("prefix " + key + padding + "\nended").encode())
                    raise error(command, options["timeout"]) if error is subprocess.TimeoutExpired else error(1, command)
                with self.subTest(error=error.__name__, cut=bool(padding)), \
                        patch.object(bench.broker.vm, "run", side_effect=fail):
                    with self.assertRaises(error) as caught:
                        bench.broker.vm.stop(bench.broker.vm.clock() + 30)
                    shown = str(caught.exception)
                    self.assertIn("[key of fixture]", shown)
                    self.assertTrue(shown.endswith("ended"))
                    self.assertNotIn(key, shown)
                    self.assertNotIn(key[-15:], shown)

    def test_build_failure_recorded(self):
        self.fail_action = "sh"
        with self.assertRaises(SystemExit):
            self.command("build")
        bench = self.desk.settings()["bench"]
        self.assertEqual((bench["enabled"], bench["generation"], bench["build"], bench["acceptance"]),
                         (False, 1, "failed", None))
        self.assertIn("CalledProcessError", bench["failure"])
        self.assertEqual(self.status, "Stopped")
        history = (self.desk.state_dir / "settings-history.jsonl").read_text()
        self.assertIn('"bench.build"', history)
        self.assertIn('"failed"', history)
        self.fail_action = ""
        self.command("rebuild")
        self.assertEqual(self.desk.settings()["bench"]["generation"], 2)
        self.assertEqual(sum(c[0] == "ssh-keygen" for c in self.calls), 2)
        from hansa.bench.manage import exclusive
        with exclusive(self.desk), self.assertRaisesRegex(SystemExit, "busy"):
            self.command("rebuild")

    def test_on_requires_current_pass(self):
        with self.assertRaisesRegex(SystemExit, "selftest"):
            self.command("on")
        self.command("build")
        self.assertEqual(self.status, "Stopped")
        settings = self.desk.settings()
        for acceptance in (None, dict(generation=2, status="passed"), dict(generation=1, status="failed"),
                           dict(generation=1, status="incomplete")):
            settings["bench"]["acceptance"] = acceptance
            self.assertEqual(self.desk.save_settings(settings), [])
            with self.assertRaisesRegex(SystemExit, "selftest"):
                self.command("on")
        from hansa.bench.selftest import mandatory
        settings["bench"]["acceptance"] = dict(generation=1, status="passed", results={
            n: dict(status="passed", checks=dict.fromkeys(fields, "passed")) for n, fields in mandatory().items()})
        self.assertEqual(self.desk.save_settings(settings), [])
        with patch("hansa.bench.manage.recipe_hash", return_value="changed"), self.assertRaisesRegex(SystemExit, "selftest"):
            self.command("on")
        read = Path.read_bytes
        for name in ("broker.py", "channel.py", "snapshot.py", "vm.py", "probes.py", "selftest.py"):
            with self.subTest(changed=name), patch.object(Path, "read_bytes",
                    new=lambda p: read(p) + (b"\n# upgrade\n" if p.name == name else b"")):
                with self.assertRaisesRegex(SystemExit, "selftest"): self.command("on")
        self.assertIn("bench: on", self.command("on"))
        for action in ("build", "rebuild"):
            with self.assertRaisesRegex(SystemExit, "off"):
                self.command(action)
        self.assertTrue(json.loads(self.command("status", "--json"))["enabled"])
        self.fail_action = "stop"
        with self.assertRaises(SystemExit):
            self.command("off")
        self.assertFalse(self.desk.settings()["bench"]["enabled"])
        self.assertIsNone(self.desk.settings()["bench"]["acceptance"])
        self.fail_action = ""
        self.assertIn("bench: off", self.command("off"))
        self.command("rebuild")
        with self.assertRaisesRegex(SystemExit, "selftest"):
            self.command("on")
