"""Coordinator management. Keep exclusive builds and generation-bound acceptance
while guest execution is supported; a new recipe always needs fresh acceptance.
"""
from contextlib import contextmanager, nullcontext
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
    files = FILES + [Path(__file__).with_name(n) for n in
                    ("broker.py", "channel.py", "snapshot.py", "vm.py", "probes.py", "selftest.py")]
    return hashlib.sha256(b"".join(p.name.encode() + b"\0" + p.read_bytes() for p in files)).hexdigest()


def acceptance_problem(bench):
    """CLI and serve share the gate; an upgrade cannot reuse stale isolation evidence."""
    from .selftest import complete
    accepted = bench["acceptance"] or {}
    if (bench["build"] != "ready" or bench["generation"] < 1
            or accepted.get("status") != "passed" or not complete(accepted)
            or accepted.get("generation") != bench["generation"]
            or bench["recipe"] != recipe_hash()):
        return "bench needs a passing selftest for the current build"
    return ""


class Busy(ValueError):
    """Temporary contention for VM ownership, rather than a VM failure."""


@contextmanager
def exclusive(desk):
    """Shared with the broker/selftest; never hold the desk lock during VM calls."""
    desk.state_dir.mkdir(exist_ok=True)
    with open(desk.state_dir / "bench.lock", "a") as held:
        try:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Busy("bench busy") from None
        yield


def save(desk, bench, action):
    with desk.lock():
        problems = desk.save_settings({**desk.settings(), "bench": bench}, by="coordinator",
                                      command="hansa bench " + action)
        if problems:
            raise ValueError("; ".join(problems))


def managed_vm(desk, private):
    machine = VM(private)
    machine.redact = desk.redact  # Redact the bounded Lima tail before its final 4 KiB cut.
    return machine


def build(machine, deadline):
    """Upload only shipped files and the public submission key through Lima admin."""
    machine.prepare(deadline)
    machine._call(["limactl", "start", "--tty=false", "--name=" + INSTANCE, str(RECIPE / "lima.yaml")], deadline, limit=3600)
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
            with (nullcontext() if action == "off" else exclusive(desk)):
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
                        machine = managed_vm(desk, private)
                        deadline = machine.clock() + 3600
                        if (private / "lima" / INSTANCE).exists():
                            machine.stop(deadline)
                            machine._call(["limactl", "delete", "--force", INSTANCE], deadline)
                        shutil.rmtree(private)
                        machine = managed_vm(desk, private)
                        bench["recipe"] = recipe_hash()
                        build(machine, deadline)
                        bench["build"] = "ready"
                    except BaseException as error:
                        bench.update(build="failed", failure=desk.redact(f"{type(error).__name__}: {error}"))
                        if machine is not None:
                            try:
                                machine.stop(machine.clock() + 30)
                            except BaseException as cleanup:
                                bench["failure"] += "; stop: " + desk.redact(f"{type(cleanup).__name__}: {cleanup}")
                        save(desk, bench, action)
                        raise
                elif action == "selftest":
                    from .selftest import run
                    if bench["enabled"] or bench["build"] != "ready" or bench["recipe"] != recipe_hash():
                        raise ValueError("selftest requires a disabled, current ready build")
                    bench["acceptance"] = dict(generation=bench["generation"], status="incomplete")
                    save(desk, bench, action)  # Invalidate any earlier pass before touching the VM.
                    report = run(managed_vm(desk, private))
                    for result in (report, *report.get("results", {}).values()):
                        if "error" in result: result["error"] = desk.redact(result["error"])
                    bench["acceptance"].update(report)
                    bench["failure"] = "; ".join(r["error"] for r in (report, *report.get("results", {}).values()) if "error" in r)
                elif action == "on":
                    problem = acceptance_problem(bench)
                    if problem:
                        raise ValueError(problem)
                    managed_vm(desk, private)  # Platform and private-state checks also apply to enablement.
                    bench["enabled"] = True
                elif action == "off":
                    bench["enabled"] = False
                    save(desk, bench, action)  # Close admission even if stopping fails.
                    if (private / "lima" / INSTANCE).exists():
                        try:
                            with exclusive(desk):
                                machine = managed_vm(desk, private)
                                machine.stop(machine.clock() + 30)
                        except Busy:
                            pass  # The broker sees disabled settings and cancels its hand-in.
                        except Exception as error:
                            bench.update(build="failed", acceptance=None, failure=desk.redact(f"{type(error).__name__}: {error}"))
                            save(desk, bench, action)
                            raise
                save(desk, bench, action)
        bench = desk.settings()["bench"]
        problem = ""
        if action == "status" and not getattr(a, "json", False) and bench["enabled"]:
            problem = acceptance_problem(bench)
        print(json.dumps(bench, sort_keys=True) if getattr(a, "json", False) else
              "bench: " + ("on" if bench["enabled"] else "off") +
              (", not in use: " + problem if problem else "") + "; " + json.dumps(bench, sort_keys=True))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        raise SystemExit("hansa bench: " + desk.redact(str(error))) from None
