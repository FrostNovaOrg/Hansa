"""Start a lane's preview for one task or verification worker, wait until it is ready, run the worker, and
stop the preview when the worker ends.

    python3 preview.py <log> <state file> <seconds> <srt> <srt settings> <temporary folder> <task> <command>
                       <ready note> <not ready note> -- <worker command...>

It runs in the worker's own process, after the lane's setup (hansa.run.with_preview): hansa serve never waits
on it. The preview runs in srt with the lane's sandbox and local binding, in a process group of its own, with
hansa's environment (never a provider's key), HANSA_TASK and its own temporary folder. Its output goes through
a pipe this reads, into <log>. It is ready at the first http://localhost:PORT or http://127.0.0.1:PORT address it
prints, or once a port it names answers; it has <seconds> to be. One not ready by then is stopped. The worker is
then started with <ready note> (@ADDRESS@ replaced by the address) or <not ready note> (@WHY@ and @LINES@ replaced)
added to its --append-system-prompt. The preview's process group is stopped when the worker ends, and when this
is told to stop (hansa stops the worker's process group); <state file> names the group, so hansa stops it too if
this could not (hansa.run.end_preview). This prints nothing of its own on stdout, which is the worker's result.
"""
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time

BASE_ENV = ("HOME", "PATH", "LANG", "LC_ALL", "USER", "LOGNAME", "SHELL")  # as hansa.run.BASE_ENV, without TMPDIR
KEY_VARS = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")  # the worker's own key, never shown in its instructions
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
ADDRESS = re.compile(r"\bhttps?://(?:localhost|127\.0\.0\.1):\d{1,5}(?:/[^\s\"'<>`]*)?")
NAMED_PORT = re.compile(r"\bport\s*[:=]?\s*(\d{2,5})\b|(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1?\]|\*):(\d{2,5})\b", re.I)
GRACE = 5  # seconds between the polite signal and the one that cannot be refused
LINES = 15  # the preview's last lines a worker is told when it is not ready


def answers(port: int) -> bool:
    for host in ("127.0.0.1", "::1"):
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            pass
    return False


def stop_group(proc) -> None:
    """Stop the preview's process group, politely and then for good; its leader, this one's child, is reaped."""
    for sig, grace in ((signal.SIGTERM, GRACE), (signal.SIGKILL, 2)):
        try:
            os.killpg(proc.pid, sig)
        except OSError:
            break
        end, gone = time.time() + grace, False
        while time.time() < end and not gone:
            proc.poll()
            try:
                os.killpg(proc.pid, 0)
                time.sleep(0.1)
            except OSError:
                gone = True
        if gone:
            break
    try:
        proc.wait(timeout=2)  # reaped, so nothing takes it for a running preview
    except subprocess.TimeoutExpired:
        pass


def main() -> int:
    cut = sys.argv.index("--")
    log, state, seconds, srt, settings, tmp, tid, command, ready_note, not_ready_note = sys.argv[1:cut]
    worker = sys.argv[cut + 1:]
    env = {k: os.environ[k] for k in BASE_ENV if k in os.environ}
    env.update(HANSA_TASK=tid, TERM="dumb", TMPDIR=tmp, CLAUDE_CODE_TMPDIR=tmp)
    out = open(log, "wb")
    try:
        proc = subprocess.Popen([srt, "--settings", settings, "--", "/bin/sh", "-c", command], env=env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                start_new_session=True)
    except OSError as e:
        proc, said = None, f"srt could not be started: {e}"
    rec = {"pgid": proc.pid if proc else None, "started": time.time(), "ready": False}
    with open(state, "w") as f:  # before anything else: hansa can stop the group even if this is stopped first
        json.dump(rec, f)
    stopping = []

    def on_signal(sig, _frame):
        stopping.append(sig)
        if proc:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except OSError:
                pass
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, on_signal)
    seen, lock = [""], threading.Lock()

    def drain():
        while True:
            try:
                chunk = os.read(proc.stdout.fileno(), 65536)
            except InterruptedError:
                continue
            except OSError:
                break
            if not chunk:
                break
            out.write(chunk)
            out.flush()
            with lock:
                seen[0] = (seen[0] + ANSI.sub("", chunk.decode(errors="replace").replace("\r", "\n")))[-200000:]
    if proc:
        threading.Thread(target=drain, daemon=True).start()
    address, why, tried = "", "", {}
    end = time.time() + float(seconds)
    while proc and not stopping:
        with lock:
            text = seen[0]
        found = ADDRESS.search(text)
        if found:
            address = found.group(0).rstrip(".,;:)]}")
            break
        for m in NAMED_PORT.finditer(text):
            port = int(m.group(1) or m.group(2))
            if 0 < port < 65536 and time.time() >= tried.get(port, 0):
                tried[port] = time.time() + 0.5
                if answers(port):
                    address = f"http://localhost:{port}"
                    break
        if address:
            break
        if proc.poll() is not None:
            time.sleep(0.2)  # its last lines are read, for the worker's note
            why = f"it ended (exit {proc.returncode}) before it was ready"
            break
        if time.time() >= end:
            why = f"it was not ready within {seconds} seconds"
            break
        time.sleep(0.2)
    if stopping:
        if proc:
            stop_group(proc)
        return 128 + stopping[0]
    if not proc:
        why = said
    elif not address:
        stop_group(proc)
    with lock:
        text = seen[0]
    lines = [x for x in text.strip().splitlines() if x.strip()][-LINES:]
    keys = [os.environ[k] for k in KEY_VARS if os.environ.get(k)]

    def clean(x):
        for k in keys:
            x = x.replace(k, "[key]")
        return x
    note = (ready_note.replace("@ADDRESS@", address) if address else
            not_ready_note.replace("@WHY@", why).replace(
                "@LINES@", "".join(f"    {clean(x)[:300]}\n" for x in lines) or "    (nothing)\n"))
    rec.update(ready=bool(address), address=address, why=why, waited=round(time.time() - rec["started"]))
    with open(state, "w") as f:
        json.dump(rec, f)
    if "--append-system-prompt" in worker:
        i = worker.index("--append-system-prompt") + 1
        worker[i] += note
    child = subprocess.Popen(worker)  # stdin, stdout and stderr are the worker's: task.md, run.json, run.err
    while True:
        try:
            rc = child.wait()
            break
        except InterruptedError:
            continue
    if proc:
        stop_group(proc)
    return rc if rc >= 0 else 128 - rc


if __name__ == "__main__":
    sys.exit(main())
