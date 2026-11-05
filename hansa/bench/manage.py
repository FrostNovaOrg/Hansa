"""Coordinator management. Keep exclusive builds and generation-bound acceptance
while guest execution is supported; a new recipe always needs fresh acceptance.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile

from ..desk import Desk
from .vm import INSTANCE, VM

RECIPE = Path(__file__).resolve().parents[2] / "contrib/bench"
FILES = [RECIPE / n for n in ("lima.yaml", "provision.sh", "edge.nft", "sysctl.conf")]
FILES += [Path(__file__).with_name(n) for n in ("guest.py", "archive.py")]


def recipe_hash():
    return hashlib.sha256(b"".join(p.name.encode() + b"\0" + p.read_bytes() for p in FILES)).hexdigest()


@contextmanager
def exclusive(desk):
    """Shared with the broker/selftest; never hold the desk lock during VM calls."""
    desk.state_dir.mkdir(exist_ok=True)
    with open(desk.state_dir / "bench.lock", "a") as held:
        try:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("bench busy") from None
        yield


def save(desk, bench, action):
    with desk.lock():
        problems = desk.save_settings({**desk.settings(), "bench": bench}, by="coordinator",
                                      command="hansa bench " + action)
        if problems:
            raise ValueError("; ".join(problems))


def build(machine, deadline):
    """Upload only shipped files and the public submission key through Lima admin."""
    machine.prepare(deadline)
    machine._call(["limactl", "start", "--tty=false", "--name=" + INSTANCE, str(RECIPE / "lima.yaml")], deadline)
    admin = ["limactl", "shell", INSTANCE, "sudo", "-n", "--"]
    bundle = "/var/lib/hansa-bench-bundle"
    machine._call(admin + ["install", "-d", "-m", "0700", bundle], deadline)
    with tempfile.TemporaryFile() as stream:
        with tarfile.open(fileobj=stream, mode="w") as archive:
            for path in FILES + [machine.identity.with_suffix(".pub")]:
                archive.add(path, arcname=path.name, recursive=False)
        stream.seek(0)
        machine._call(admin + ["tar", "-xf", "-", "-C", bundle], deadline, stdin=stream)
    machine._call(admin + ["sh", bundle + "/provision.sh"], deadline)
    key = machine._call(admin + ["cat", "/etc/ssh/ssh_host_ed25519_key.pub"], deadline,
                        stdout=subprocess.PIPE).stdout.decode().split()
    machine.pin(" ".join(key[:2]))
    machine.reset(deadline)


def command(a):
    desk, action = Desk.find(a.desk), a.bench_command
    try:
        if action != "status":
            with exclusive(desk):
                bench = desk.settings()["bench"]
                private = desk.state_dir / "bench"
                if action in ("build", "rebuild"):
                    if bench["enabled"]:
                        raise ValueError("turn bench off before building")
                    if action == "build" and bench["generation"]:
                        raise ValueError("bench already built or attempted; use rebuild")
                    bench.update(generation=bench["generation"] + 1, build="building", acceptance=None,
                                 failure="", recipe="")
                    save(desk, bench, action)
                    machine = None
                    try:
                        machine = VM(private)
                        deadline = machine.clock() + 900
                        if (private / "lima" / INSTANCE).exists():
                            machine.stop(deadline)
                            machine._call(["limactl", "delete", "--force", INSTANCE], deadline)
                        shutil.rmtree(private)
                        machine = VM(private)
                        bench["recipe"] = recipe_hash()
                        build(machine, deadline)
                        bench["build"] = "ready"
                    except BaseException as error:
                        bench.update(build="failed", failure=type(error).__name__)
                        if machine is not None:
                            try:
                                machine.stop(machine.clock() + 30)
                            except BaseException as cleanup:
                                bench["failure"] += "; stop: " + type(cleanup).__name__
                        save(desk, bench, action)
                        raise
                elif action == "on":
                    accepted = bench["acceptance"] or {}
                    if (bench["build"] != "ready" or bench["generation"] < 1
                            or accepted.get("status") != "passed"
                            or accepted.get("generation") != bench["generation"]
                            or bench["recipe"] != recipe_hash()):
                        raise ValueError("bench needs a passing selftest for the current build")
                    VM(private)  # Platform and private-state checks also apply to enablement.
                    bench["enabled"] = True
                elif action == "off":
                    bench["enabled"] = False
                    save(desk, bench, action)  # Close admission even if stopping fails.
                    if (private / "lima" / INSTANCE).exists():
                        machine = VM(private)
                        try:
                            machine.stop(machine.clock() + 30)
                        except Exception as error:
                            bench.update(build="failed", acceptance=None, failure=type(error).__name__)
                            save(desk, bench, action)
                            raise
                save(desk, bench, action)
        bench = desk.settings()["bench"]
        print(json.dumps(bench, sort_keys=True) if getattr(a, "json", False) else
              "bench: " + ("on" if bench["enabled"] else "off") + "; " + json.dumps(bench, sort_keys=True))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        raise SystemExit("hansa bench: " + str(error)) from None
