"""Offline Lima/SSH contract: injected commands and clock, no VM or network."""
import base64
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

KEY = "ssh-ed25519 " + base64.b64encode(b"\0\0\0\x0bssh-ed25519\0\0\0 " + b"x" * 32).decode()


class BenchVMTest(unittest.TestCase):
    def setUp(self):
        from hansa.bench import vm
        self.module = vm
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "private state"
        self.calls, self.status, self.port, self.now = [], "Stopped", 2200, 0
        self.fail_action, self.key, self.timeout = "", KEY, False
        with patch.object(vm.sys, "platform", "darwin"):
            self.vm = vm.VM(self.root, run=self.run_command, clock=lambda: self.now)
        self.vm.prepare(100)
        self.vm.pin(KEY)

    def run_command(self, command, **options):
        self.calls.append(command)
        self.assertTrue(0 < options["timeout"] <= 100)
        self.assertEqual(options["env"]["LIMA_HOME"], str(self.root / "lima"))
        self.assertEqual(set(options["env"]), {"HOME", "PATH", "LC_ALL", "LIMA_HOME"})
        action = command[1] if command[0] == "limactl" else command[0]
        if action == self.fail_action:
            raise subprocess.CalledProcessError(1, command)
        output = b""
        if action == "ssh-keygen":
            path = Path(command[command.index("-f") + 1])
            path.write_text("fixture private key")
            path.chmod(0o600)
        elif action == "start":
            self.status, self.port = "Running", self.port + 1
        elif action == "stop":
            self.status = "Stopped"
        elif action == "list":
            output = json.dumps(dict(name=self.module.INSTANCE, status=self.status, sshLocalPort=self.port)).encode()
        elif action == "shell":
            # Execute the actual fixed purge on a fake guest filesystem.
            guest = self.root / "slots"
            guest.mkdir(exist_ok=True)
            (guest / "dirty").mkdir()
            outside = self.root / "sentinel"
            outside.write_text("keep")
            (guest / "link").symlink_to(outside)
            with patch("shutil.chown") as owner, patch("os.sync"), patch("os.path.ismount", return_value=True):
                exec(command[-1].replace(repr("/var/lib/hansa-bench"), repr(str(guest))))
            owner.assert_called_once_with(str(guest), "bench", "bench")
            self.assertEqual(list(guest.iterdir()), [])
            self.assertEqual(outside.read_text(), "keep")
        elif action == "ssh":
            flags = dict(shlex.split(command[i + 1])[0].split("=", 1)
                         for i, value in enumerate(command[:-1]) if value == "-o")
            for k, v in dict(StrictHostKeyChecking="yes", HostKeyAlias=self.module.INSTANCE,
                            IdentitiesOnly="yes", IdentityAgent="none", BatchMode="yes",
                            GlobalKnownHostsFile="/dev/null", UpdateHostKeys="no",
                            ClearAllForwardings="yes", ForwardAgent="no").items():
                self.assertEqual(flags[k], v)
            self.assertEqual(command[1:3], ["-F", "/dev/null"])
            self.assertEqual(command[-2:], ["bench@127.0.0.1", "bench-v1"])
            self.assertEqual(int(command[command.index("-p") + 1]), self.port)
            code = ("import os, select, signal, sys\n"
                    "if open(sys.argv[1]).read() != sys.argv[2]: sys.exit(255)\n"
                    "if sys.argv[3]:\n"
                    "    open(sys.argv[3], 'w').write(str(os.getpid())); signal.pause()\n"
                    "os.write(1, os.read(0, 4))\nassert not select.select([0], [], [], 0)[0]\n")
            return subprocess.run([sys.executable, "-c", code, flags["UserKnownHostsFile"],
                                   self.module.INSTANCE + " " + self.key + "\n",
                                   str(self.root / "pid") if self.timeout else ""], **options)
        return subprocess.CompletedProcess(command, 0, output)

    def test_purge_preserves_mount_and_sync(self):
        guest = self.root / "purge-slots"
        code = self.module.PURGE.replace(repr("/var/lib/hansa-bench"), repr(str(guest)))
        with patch("os.path.ismount", return_value=False), patch("shutil.chown"), patch("os.sync"), \
                self.assertRaisesRegex(RuntimeError, "not mounted"):
            exec(code)
        self.assertFalse(guest.exists())
        guest.mkdir()
        for _ in range(2):
            (guest / "dirty").write_text("discard")
            with patch("shutil.chown") as owner, patch("os.sync") as sync, \
                    patch("os.path.ismount", return_value=True), patch("os.rmdir", side_effect=AssertionError("mount removed")):
                def synced():
                    owner.assert_called_once_with(str(guest), "bench", "bench")
                    self.assertEqual(list(guest.iterdir()), [])
                    self.assertEqual(guest.stat().st_mode & 0o777, 0o700)
                sync.side_effect = synced
                exec(code)
                sync.assert_called_once_with()

    def test_limactl_error_tail(self):
        data = b"discarded\n" + b"x" * 4096 + b"\xff\nLima failed"
        self.vm.run = lambda command, **options: subprocess.run(
            [sys.executable, "-c", "import os; os.write(2, " + repr(data) + "); raise SystemExit(7)"], **options)
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            self.vm._call(["limactl", "start"], 100)
        self.assertEqual(caught.exception.stderr, data[-4096:])
        self.assertIn("\ufffd\nLima failed", str(caught.exception))
        self.assertNotIn("discarded", str(caught.exception))

    def test_other_calls_keep_900_second_cap(self):
        def run(command, **options):
            self.assertEqual(options["timeout"], 900)
            return subprocess.CompletedProcess(command, 0)
        self.vm.run = run
        for command in (["limactl", action] for action in ("start", "stop", "list", "shell", "delete")):
            self.vm._call(command, 3600)
        for command in (["ssh"], ["ssh-keygen"]):
            self.vm._call(command, 3600)

    def test_status_size_name_running_and_port_checks(self):
        info = dict(name=self.module.INSTANCE, status="Running", sshLocalPort=2200)
        encoded = json.dumps(info).encode()
        for data in (encoded + b" " * (65537 - len(encoded)),
                     json.dumps({**info, "name": "another-instance"}).encode()):
            with patch.object(self.vm, "_call", return_value=subprocess.CompletedProcess([], 0, data)), \
                    self.assertRaises(ValueError):
                self.vm._info(100)
        for change in ({"status": "Stopped"}, *({"sshLocalPort": p} for p in (0, 65536, True, "2200"))):
            with self.subTest(change=change), patch.object(self.vm, "_info", return_value={**info, **change}), \
                    self.assertRaisesRegex(ValueError, "not ready"):
                self.vm._boot(100)

    def test_platform_stop_and_ssh_options(self):
        with patch.object(self.module.sys, "platform", "linux"), self.assertRaisesRegex(RuntimeError, "macOS"):
            self.module.VM(self.root)
        self.vm.reset(100)
        self.assertEqual([c for c in self.calls if c[:2] == ["limactl", "stop"]],
                         [["limactl", "stop", "--force", self.module.INSTANCE]] * 2)
        self.vm.start(100)
        with patch.object(self.vm, "_call") as call:
            self.vm.connect(None, None, 100)
        ssh = call.call_args[0][0]
        for option in ("ControlPath=none", "HostKeyAlgorithms=ssh-ed25519", "ConnectTimeout=10", "CheckHostIP=no"):
            self.assertIn(option, [ssh[i + 1] for i, arg in enumerate(ssh[:-1]) if arg == "-o"])

    def test_timeout_keeps_stderr(self):
        def run(command, **options):
            options["stderr"].write(b"download stalled")
            raise subprocess.TimeoutExpired(command, options["timeout"])
        self.vm.run = run
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            self.vm._call(["limactl", "start"], 100)
        self.assertEqual(caught.exception.stderr, b"download stalled")
        self.assertIn("download stalled", str(caught.exception))

    def test_stderr_flood_never_grows_a_disk_spool(self):
        def redact(text):
            self.assertLessEqual(len(text), 65536)
            return text
        self.vm.redact = redact
        def run(command, **options):
            for _ in range(128):
                options["stderr"].write(b"x" * 8192)
                self.assertLessEqual(os.fstat(options["stderr"].fileno()).st_size, 65536)
            options["stderr"].write(b"final diagnostic")
            raise subprocess.CalledProcessError(7, command)
        self.vm.run = run
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            self.vm._call(["limactl", "start"], 100)
        self.assertEqual(len(caught.exception.stderr), 4096)
        self.assertTrue(caught.exception.stderr.endswith(b"final diagnostic"))

    def test_stderr_completion_races_keep_final_bytes(self):
        import threading
        for stalled in ("select", "read"):
            with self.subTest(stalled=stalled):
                waiting, finished = threading.Event(), threading.Event()
                select, read, join = self.module.select.select, os.read, threading.Thread.join
                # A full Linux pipe can refill after a read but before done.
                chunks = iter([b"x" * 8192] * 7 + [b"final diagnostic", b""])
                def pause(*args):
                    if not waiting.is_set():
                        waiting.set()
                        self.assertTrue(finished.wait(10))
                        return ([], [], []) if stalled == "select" else b"x" * 8192
                    return select(*args) if stalled == "select" else next(chunks)
                def finish(thread):
                    finished.set()  # _stderr_tail has set done before joining.
                    join(thread)
                with patch.object(self.module.select, "select", side_effect=(
                        pause if stalled == "select" else lambda *args: ([0], [], []))), \
                        patch.object(os, "read", side_effect=read if stalled == "select" else pause), \
                        patch.object(threading.Thread, "join", finish):
                    with self.module._stderr_tail() as (errors, tail):
                        self.assertTrue(waiting.wait(10))
                        errors.write(b"final diagnostic")
                    self.assertTrue(tail.endswith(b"final diagnostic"))

    def connect(self, deadline=100):
        reader, writer = os.pipe()
        with os.fdopen(reader, "rb") as source, os.fdopen(writer, "wb", buffering=0) as sender, \
                tempfile.TemporaryFile() as target:
            sender.write(b"wire")  # Keep stdin open through completion, as the broker must.
            self.vm.connect(source, target, deadline)
            target.seek(0)
            return target.read()

    def test_changed_key_refused(self):
        self.vm.reset(100)
        self.vm.start(100)
        pinned = self.vm.known_hosts.read_bytes()
        self.key = KEY.replace("eHh4", "eXl5")
        with self.assertRaises(subprocess.CalledProcessError):
            self.connect()
        self.assertEqual(self.vm.known_hosts.read_bytes(), pinned)
        self.assertFalse(self.vm.clean)
        self.assertEqual(self.status, "Stopped")
        with self.assertRaises(FileExistsError):
            self.vm.pin(KEY)
        for key in ("", "ssh-rsa AAAA", "ssh-ed25519 AAAA", "ssh-ed25519 !!!", KEY + "\nother key"):
            with self.assertRaises(ValueError):
                self.vm.pin(key)
        self.vm.reset(100)
        self.vm.start(100)
        self.vm.identity.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "private"):
            self.connect()

    def test_port_rediscovered(self):
        self.vm.prepare(100)
        self.vm.reset(100)
        ports = []
        for _ in range(2):
            ports.append(self.vm.start(100))
            self.assertEqual(self.connect(), b"wire")
            self.vm.stop(100)
        self.assertNotEqual(*ports)
        self.assertEqual(sum(c[0] == "ssh-keygen" for c in self.calls), 1)
        for path, mode in ((self.root, 0o700), (self.root / "lima", 0o700),
                           (self.vm.identity, 0o600), (self.vm.known_hosts, 0o600)):
            self.assertEqual(path.stat().st_mode & 0o777, mode)

    def test_reset_failure_keeps_closed(self):
        with self.assertRaisesRegex(RuntimeError, "closed"):
            self.vm.start(100)
        for self.fail_action in ("stop", "start", "shell", "list"):
            self.calls.clear()
            with self.assertRaises(subprocess.CalledProcessError):
                self.vm.reset(100)
            self.assertFalse(self.vm.clean)
            with self.assertRaisesRegex(RuntimeError, "closed"):
                self.connect()
            if self.fail_action == "stop":
                self.assertFalse(any(c[1] in ("start", "shell") for c in self.calls))
        self.fail_action = ""
        self.calls.clear()
        self.vm.reset(100)
        self.assertEqual([c[1] for c in self.calls], ["stop", "list", "start", "list", "shell", "stop", "list"])
        self.assertTrue(self.vm.clean)
        self.assertEqual(self.status, "Stopped")
        self.vm.start(100)
        self.timeout = True
        with self.assertRaises(subprocess.TimeoutExpired):
            self.connect(5)
        with self.assertRaises(ChildProcessError):
            os.waitpid(int((self.root / "pid").read_text()), os.WNOHANG)
        self.assertFalse(self.vm.clean)
        self.assertEqual(self.status, "Stopped")
        for deadline in (self.now, float("nan"), float("inf")):
            with self.assertRaises(TimeoutError):
                self.vm._call(["unused"], deadline)
