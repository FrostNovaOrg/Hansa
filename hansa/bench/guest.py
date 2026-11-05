"""Disposable forced gateway, installed with archive.py; never enabled by importing.
Wire: four-byte big-endian JSON length, strict UTF-8 JSON, then declared tar bytes.
Request fields below include remaining timeout seconds; replies carry version/id/length.
Keep validation, isolation and the dirty-slot barrier while submissions are untrusted.
An interrupted/uncertain teardown requires the broker's stop-and-purge barrier.
"""
import fcntl
import io
import json
import os
from pathlib import Path
import re
import resource
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from . import archive

SLOT_ROOT = "/var/lib/hansa-bench"


class CleanupError(RuntimeError):
    """Leave the slot poisoned until VM reset; never collect uncertain survivors."""


def _header(source):
    def read(size):
        buffer = io.BytesIO()
        archive._copy(source, buffer, size)
        return buffer.getvalue()

    size = int.from_bytes(read(4), "big")
    if not 0 < size <= archive.HEADER_BYTES:
        raise ValueError("header limit")
    def pairs(items):
        result = dict(items)
        if len(result) != len(items):
            raise ValueError("duplicate field")
        return result
    request = json.loads(read(size).decode("utf-8"), object_pairs_hook=pairs)
    if type(request) is not dict or set(request) != {"version", "id", "script", "cwd", "timeout", "length"}:
        raise ValueError("request fields")
    if (type(request["version"]) is not int or request["version"] != 1
            or not isinstance(request["id"], str) or not re.fullmatch("[0-9a-f]{32}", request["id"])
            or not isinstance(request["script"], str) or "\0" in request["script"]
            or type(request["timeout"]) is not int or not 1 <= request["timeout"] <= 900
            or not isinstance(request["cwd"], str)):
        raise ValueError("request values")
    if request["cwd"] != ".":
        archive._name(request["cwd"])
    return request


def terminate(process):
    """Kill/reap bubblewrap itself: its PID namespace and die-with-parent do teardown."""
    try:
        if process.poll() is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CleanupError("namespace teardown unconfirmed") from error


def run_box(slot, request, source, deadline, clock=time.monotonic):
    command = ["/usr/bin/bwrap", "--unshare-all", "--die-with-parent", "--new-session",
               "--clearenv", "--cap-drop", "ALL", "--ro-bind", "/usr", "/usr",
               "--symlink", "usr/bin", "/bin", "--symlink", "usr/sbin", "/sbin",
               "--symlink", "usr/lib", "/lib", "--ro-bind", "/etc", "/etc",
               "--proc", "/proc", "--dev", "/dev"]
    for name, target in (("work", "/work"), ("out", "/out"), ("tmp", "/tmp"), ("shm", "/dev/shm")):
        command += ["--bind", str(slot / name), target]
    command += ["--ro-bind", str(slot / "run.sh"), "/run.sh", "--setenv", "HOME", "/tmp",
                "--setenv", "PATH", "/usr/bin:/bin", "--chdir", "/work/" + request["cwd"],
                "/bin/sh", "/run.sh"]
    with tempfile.TemporaryFile(dir=slot) as info:
        command[1:1] = ["--info-fd", str(info.fileno())]
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, env={}, close_fds=True, pass_fds=(info.fileno(),),
                                   preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_NPROC, (256, 256)))
        output, retained, truncated, status = [bytearray(), bytearray()], 0, False, None
        pipes = {process.stdout.fileno(): 0, process.stderr.fileno(): 1}
        try:
            while pipes or process.poll() is None:
                remaining = deadline - clock()
                if remaining <= 0:
                    status = "timeout"
                    break
                ready = select.select([source, *pipes], [], [], min(remaining, 0.1))[0]
                if source in ready:
                    status = "cancelled"  # EOF or forbidden trailing input cancels the request.
                    break
                for fd in ready:
                    data = os.read(fd, archive.HEADER_BYTES)
                    if not data:
                        del pipes[fd]
                        continue
                    kept = data[:archive.OUTPUT_BYTES - retained]
                    output[pipes[fd]].extend(kept)
                    retained += len(kept)
                    truncated |= len(kept) != len(data)
        finally:
            try:
                terminate(process)
            finally:
                process.stdout.close()
                process.stderr.close()
        info.seek(0)
        try:
            pid = json.loads(info.read(4096))["child-pid"]
            started = type(pid) is int and pid > 0
        except (ValueError, KeyError, TypeError):
            started = False
    return dict(status=status or ("infrastructure" if not started else "ok" if process.returncode == 0 else "failed"),
                exit=process.returncode, truncated=truncated), *map(bytes, output)


def _expired(signum, frame):
    raise TimeoutError("gateway phase deadline")


def main(source=0, target=1, root=SLOT_ROOT, clock=time.monotonic):
    if os.environ.get("SSH_ORIGINAL_COMMAND") != "bench-v1":
        raise ValueError("literal bench-v1 required")
    started, slot, uncertain = clock(), None, False
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        lock = archive.safe_open(directory, "lock", os.O_RDWR | os.O_CREAT)
    finally:
        os.close(directory)
    previous = signal.signal(signal.SIGALRM, _expired)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if set(os.listdir(root)) != {"lock"}:
            raise CleanupError("dirty slot; VM reset required")
        signal.setitimer(signal.ITIMER_REAL, 900)
        with os.fdopen(os.dup(source), "rb", buffering=0) as incoming:
            request = _header(incoming)
            deadline = started + request["timeout"]
            if deadline <= clock():
                raise TimeoutError("input deadline")
            signal.setitimer(signal.ITIMER_REAL, deadline - clock())
            slot = Path(tempfile.mkdtemp(prefix="slot-", dir=root))
            archive.unpack(incoming, request["length"], slot / "work")
            for name in ("out", "tmp", "shm"):
                (slot / name).mkdir(mode=0o700)
            (slot / "run.sh").write_text(request["script"], encoding="utf-8")
            signal.setitimer(signal.ITIMER_REAL, 0)
            result, stdout, stderr = run_box(slot, request, source, deadline, clock)
        # Bounded teardown/return grace also allows reporting an execution timeout.
        signal.setitimer(signal.ITIMER_REAL, 10)
        out = os.open(slot / "out", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            with archive.collect(out, dict(result, id=request["id"]), stdout, stderr) as returned:
                shutil.rmtree(slot)
                slot = None
                header = json.dumps(dict(version=1, id=request["id"], length=returned.seek(0, 2))).encode()
                returned.seek(0)
                with os.fdopen(os.dup(target), "wb") as outgoing:
                    outgoing.write(len(header).to_bytes(4, "big") + header)
                    shutil.copyfileobj(returned, outgoing)
        finally:
            os.close(out)
    except CleanupError:
        uncertain = True
        raise
    finally:
        signal.setitimer(signal.ITIMER_REAL, 10)
        try:
            if slot is not None and not uncertain:
                shutil.rmtree(slot)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
            os.close(lock)  # Closing releases the slot only after teardown and deletion.


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit("bench: gateway infrastructure failure")
