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
        self.fail, self.key, self.timeout = "", KEY, False
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
        if action == self.fail:
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
        for self.fail in ("stop", "start", "shell", "list"):
            self.calls.clear()
            with self.assertRaises(subprocess.CalledProcessError):
                self.vm.reset(100)
            self.assertFalse(self.vm.clean)
            with self.assertRaisesRegex(RuntimeError, "closed"):
                self.connect()
            if self.fail == "stop":
                self.assertFalse(any(c[1] in ("start", "shell") for c in self.calls))
        self.fail = ""
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
