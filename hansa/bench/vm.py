"""Coordinator-only transport; private must be beneath the worker-denied desk state.
One exclusive owner per instance. New owners must reset before start/connect: never
adopt uncertain execution. Keep this barrier and strict pins while guests run code.
Build calls prepare, then pin with the trusted two-field Ed25519 public key;
rebuild uses a fresh private directory. No scanning or automatic key replacement.
"""
import base64
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import stat
import subprocess
import sys
import time

INSTANCE = "hansa-bench"
PURGE = ("import os, shutil; p='/var/lib/hansa-bench'; shutil.rmtree(p); "
         "os.mkdir(p, 0o700); shutil.chown(p, 'bench', 'bench')")


def _private(path, directory=False):
    if directory:
        path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if (not kind(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)
            or (not directory and info.st_nlink != 1)):
        raise ValueError("bench identity must be private, owned and unlinked")


class VM:
    def __init__(self, private, *, run=subprocess.run, clock=time.monotonic):
        if sys.platform != "darwin":
            raise RuntimeError("bench requires macOS and Lima")
        self.private, self.run, self.clock = Path(private).absolute(), run, clock
        self.clean, self.port = False, None
        _private(self.private, True)
        _private(self.private / "lima", True)
        self.identity, self.known_hosts = self.private / "submission", self.private / "known_hosts"
        self.env = dict(HOME=str(self.private), LIMA_HOME=str(self.private / "lima"),
                        PATH=os.environ.get("PATH", os.defpath), LC_ALL="C")

    def _call(self, command, deadline, **streams):
        remaining = deadline - self.clock()
        if not math.isfinite(remaining) or remaining <= 0:
            raise TimeoutError("bench deadline")
        return self.run(command, check=True, timeout=min(remaining, 900), env=self.env,
                        **dict(dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL), **streams))

    def prepare(self, deadline):
        if not os.path.lexists(self.identity):
            self._call(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "",
                        "-f", str(self.identity)], deadline)
        _private(self.identity)

    def pin(self, public_key):
        fields = public_key.split()
        if (len(fields) != 2 or fields[0] != "ssh-ed25519"
                or base64.b64decode(fields[1], validate=True)[:19] != b"\0\0\0\x0bssh-ed25519\0\0\0 "
                or len(base64.b64decode(fields[1], validate=True)) != 51):
            raise ValueError("invalid Ed25519 host key")
        fd = os.open(self.known_hosts, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(INSTANCE + " " + " ".join(fields) + "\n")

    def _info(self, deadline):
        data = self._call(["limactl", "list", "--json", INSTANCE], deadline, stdout=subprocess.PIPE).stdout
        if len(data) > 65536:
            raise ValueError("Lima status limit")
        info = json.loads(data)
        if not isinstance(info, dict) or info.get("name") != INSTANCE:
            raise ValueError("unexpected Lima instance")
        return info

    def _boot(self, deadline):
        self._call(["limactl", "start", "--tty=false", INSTANCE], deadline)
        info = self._info(deadline)
        port = info.get("sshLocalPort")
        if info.get("status") != "Running" or type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("Lima is not ready")
        return port

    @contextmanager
    def _failure(self):
        try:
            yield
        except BaseException:
            self.clean = False
            self.stop(self.clock() + 30)  # Independent, bounded teardown grace after request expiry.
            raise

    def start(self, deadline):
        if not self.clean:
            raise RuntimeError("bench closed; reset required")
        self.port = None
        with self._failure():
            self.port = self._boot(deadline)
        return self.port

    def stop(self, deadline):
        self.port = None
        try:
            self._call(["limactl", "stop", "--force", INSTANCE], deadline)
            if self._info(deadline).get("status") != "Stopped":
                raise RuntimeError("VM stop unconfirmed")
        except BaseException:
            self.clean = False
            raise

    def reset(self, deadline):
        """Stop first to kill survivors; boot only for admin purge, then confirm stopped."""
        self.clean, self.port = False, None
        try:
            self.stop(deadline)
            self._boot(deadline)
            self._call(["limactl", "shell", INSTANCE, "sudo", "-n", "--", "python3", "-I", "-c", PURGE], deadline)
        finally:
            self.stop(self.clock() + 30)
        self.clean = True

    def connect(self, source, target, deadline):
        """Blocking executor call. Broker supplies a live input pipe (EOF cancels),
        and drains/caps the output pipe. subprocess.run kills/reaps on timeout.
        """
        if not self.clean or self.port is None:
            raise RuntimeError("bench closed; start after reset required")
        with self._failure():
            _private(self.identity)
            _private(self.known_hosts)
            hosts = str(self.known_hosts).replace("\\", "\\\\").replace('"', '\\"')
            options = ["IdentitiesOnly=yes", "IdentityAgent=none", 'UserKnownHostsFile="' + hosts + '"',
                       "GlobalKnownHostsFile=/dev/null", "BatchMode=yes", "StrictHostKeyChecking=yes",
                       "HostKeyAlias=" + INSTANCE, "UpdateHostKeys=no", "CheckHostIP=no",
                       "ClearAllForwardings=yes", "ForwardAgent=no", "ControlPath=none",
                       "HostKeyAlgorithms=ssh-ed25519", "ConnectTimeout=10"]
            command = ["ssh", "-F", "/dev/null", "-T", "-i", str(self.identity)]
            for option in options:
                command += ["-o", option]
            self._call(command + ["-p", str(self.port), "bench@127.0.0.1", "bench-v1"],
                       deadline, stdin=source, stdout=target)
