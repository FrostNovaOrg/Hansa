"""Start a lane's preview for one task or verification worker, wait until it is ready, run the worker, and
stop the preview when the worker ends.

    python3 preview.py <log> <state file> <seconds> <srt> <srt settings> <temporary folder> <task> <desk>
                       <command> <ready note> <not ready note> -- <worker command...>

It runs in the worker's own process, after the lane's setup (hansa.run.with_preview): hansa serve never waits
on it. The preview runs in srt with the lane's sandbox and local binding, in a process group of its own, with
hansa's environment (never a provider's key), HANSA_TASK and its own temporary folder. Its output goes through
a pipe this reads to its end, so the preview never waits on it; <log> keeps its first and last
hansa.run.PREVIEW_KEEP / 2 bytes, with a line between them saying how much was left out. It is ready at the first
http://localhost:PORT or http://127.0.0.1:PORT address it prints, or once a port it names answers; it has
<seconds> to be. One not ready by then is stopped. The worker is then started with <ready note> (@ADDRESS@
replaced by the address) or <not ready note> (@WHY@ and @LINES@ replaced, the desk's keys removed from them)
added to its --append-system-prompt. The preview's process group is stopped when the worker ends, and when this
is told to stop (hansa stops the worker's process group); so is every process seen below the preview (looked
for every hansa.run.PREVIEW_LOOK_S seconds), whatever its group or environment. <state file> names the group and
those processes, so hansa stops them too if this could not (hansa.run.end_preview). This prints nothing of its
own on stdout, which is the worker's result.
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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hansa import run  # noqa: E402
from hansa.desk import Desk  # noqa: E402

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


class Kept:
    """What <log> keeps of the preview's output: all of it up to `half` bytes; past that, its start cut at a
    line, a line saying how much was left out, and its last `half` bytes (from a line's start), written again at
    most every half second, and at the end. A cut with no line to fall at drops hansa.run.PREVIEW_MARGIN bytes more
    on its side, so no key is kept in part, and the line says so."""

    def __init__(self, path: str, half: int) -> None:
        self.out, self.half = open(path, "wb"), half
        self.total = self.written = self.line_end = 0  # line_end: in the file, just after its last newline
        self.cut, self.cut_in_line = None, False  # where the start ends, once the output outgrew it
        self.tail, self.flushed, self.lock = bytearray(), 0.0, threading.Lock()

    def add(self, chunk: bytes) -> None:
        with self.lock:
            self.total += len(chunk)
            if self.cut is None:
                room = self.half - self.written
                part = chunk[:room]
                if b"\n" in part:
                    self.line_end = self.written + part.rfind(b"\n") + 1
                self.out.write(part)
                self.written += len(part)
                if len(chunk) <= room:
                    self.out.flush()
                    return
                # the start ends at its last whole line; with none, inside the line, and a margin before the cut
                # goes too: desk.redact replaces whole keys only, and no key is left there in part
                self.cut = self.line_end or max(0, self.written - run.PREVIEW_MARGIN)
                self.cut_in_line, chunk = not self.line_end, chunk[room:]
            self.tail += chunk
            if len(self.tail) > self.half:
                del self.tail[:len(self.tail) - self.half]
            if time.time() - self.flushed >= 0.5:
                self._write()

    def _write(self) -> None:
        if self.cut is None:
            return
        shown, margin = self.tail, self.cut_in_line
        if self.total - len(self.tail) > self.cut:  # output left out before the end: it is shown from a line's start,
            line = self.tail.find(b"\n")  # or with none, without a margin after the cut
            shown, margin = (self.tail[line + 1:], margin) if line >= 0 else (self.tail[run.PREVIEW_MARGIN:], True)
        left = self.total - self.cut - len(shown)
        self.out.seek(self.cut)
        if left:
            self.out.write(f"\n[hansa: {left} bytes of the preview's output left out here{run.margin_said(margin)}]\n"
                           .encode())
        self.out.write(shown)
        self.out.truncate()
        self.out.flush()
        self.flushed = time.time()

    def close(self) -> None:
        with self.lock:
            if not self.out.closed:
                self._write()
                self.out.close()


def main() -> int:
    cut = sys.argv.index("--")
    log, state, seconds, srt, settings, tmp, tid, root, command, ready_note, not_ready_note = sys.argv[1:cut]
    worker = sys.argv[cut + 1:]
    env = {k: os.environ[k] for k in BASE_ENV if k in os.environ}
    env.update(HANSA_TASK=tid, TERM="dumb", TMPDIR=tmp, CLAUDE_CODE_TMPDIR=tmp)
    kept = Kept(log, run.PREVIEW_KEEP // 2)
    try:
        proc = subprocess.Popen([srt, "--settings", settings, "--", "/bin/sh", "-c", command], env=env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                start_new_session=True)
    except OSError as e:
        proc, said = None, f"srt could not be started: {e}"
    rec = {"pgid": proc.pid if proc else None, "started": time.time(), "ready": False, "cwd": os.getcwd(),
           "tmp": tmp, "seen": {}}
    seen_procs, rec_lock = {}, threading.Lock()

    def save():  # whole or not at all: hansa may read it meanwhile
        with rec_lock:
            with open(state + ".new", "w") as f:
                json.dump({**rec, "seen": {str(p): s for p, s in seen_procs.items()}}, f)
            os.replace(state + ".new", state)
    save()  # before anything else: hansa can stop the group even if this is stopped first
    stopping, done = [], threading.Event()

    def on_signal(sig, _frame):
        stopping.append(sig)
        if proc:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except OSError:
                pass
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, on_signal)
    seen, lock, looking = [""], threading.Lock(), [True]
    first, ports, part = [""], {}, [""]  # in its whole lines as they come, also in a flood: its address, ports it names

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
            kept.add(chunk)
            if looking[0]:  # its address, and its last lines for the worker's note
                piece = chunk.decode(errors="replace").replace("\r", "\n")
                text = part[0] + piece
                whole = ANSI.sub("", text[:text.rfind("\n") + 1])
                part[0] = text[text.rfind("\n") + 1:][-4000:]
                found = ADDRESS.search(whole)
                with lock:
                    if found and not first[0]:
                        first[0] = found.group(0)
                    for m in NAMED_PORT.finditer(whole):
                        if len(ports) < 64:
                            ports.setdefault(int(m.group(1) or m.group(2)), 0)
                    seen[0] = (seen[0] + ANSI.sub("", piece))[-200000:]

    def recorder():  # what the preview starts, also what leaves its group or clears its environment
        while not done.wait(run.PREVIEW_LOOK_S):
            look()

    def look():
        try:
            if run.record_descendants(proc.pid, seen_procs):
                save()
        except Exception:  # noqa: BLE001 - a look that fails is tried again at the next
            pass
    if proc:
        drainer = threading.Thread(target=drain, daemon=True)
        drainer.start()
        look()
        threading.Thread(target=recorder, daemon=True).start()

    def stop_all():
        if done.is_set():  # once: the leader is reaped, and its number may pass to another process
            return
        done.set()
        look()  # the last ones it started
        stop_group(proc)
        run.stop_recorded(seen_procs, GRACE)
        drainer.join(timeout=2)  # its last lines; a process that kept the pipe open does not hold this up
        kept.close()
    address, why, tried = "", "", {}
    end = time.time() + float(seconds)
    while proc and not stopping:
        with lock:
            text, named = seen[0][-4000:], list(ports)  # its last line, perhaps not ended yet, too
            found = first[0] or (ADDRESS.search(text) or [""])[0]
        if found:
            address = found.rstrip(".,;:)]}")
            break
        for port in named + [int(m.group(1) or m.group(2)) for m in NAMED_PORT.finditer(text)]:
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
    looking[0] = False
    if stopping:
        if proc:
            stop_all()
        return 128 + stopping[0]
    if not proc:
        why = said
    elif not address:
        stop_all()
    with lock:
        text = seen[0]
    lines = [x for x in text.strip().splitlines() if x.strip()][-LINES:]
    keys = [os.environ[k] for k in KEY_VARS if os.environ.get(k)]
    desk = Desk(root)

    def clean(x):
        for k in keys:
            x = x.replace(k, "[key]")
        return desk.redact(x)  # the desk's keys too, as in the kept log
    note = (ready_note.replace("@ADDRESS@", address) if address else
            not_ready_note.replace("@WHY@", why).replace(
                "@LINES@", "".join(f"    {clean(x)[:300]}\n" for x in lines) or "    (nothing)\n"))
    rec.update(ready=bool(address), address=address, why=why, waited=round(time.time() - rec["started"]))
    save()
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
        stop_all()
    return rc if rc >= 0 else 128 - rc


if __name__ == "__main__":
    sys.exit(main())
