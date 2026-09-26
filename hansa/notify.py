"""hansa tells a coordinator's Claude Code session when something needs it, through the session's inbox.

Every interactive Claude Code session listens on a local Unix socket, its inbox: one line of JSON posted
there arrives in the session as a message from another session. A follower is a small background process
per session and desk. It reads the desk's event log from where it left off, picks what the coordinator
waits for (a task ended, a verification or design session ended, a queued merge check ended, the principal
answered a request, the server stopped), and posts it to that session's inbox. It ends when the session's
process does.

    `hansa coordinator`   starts one for the session it launches (--messaging-socket-path, a path it chose)
    `hansa notify on`     starts one for the session it is run from (CLAUDE_PID, CLAUDE_CODE_MESSAGING_SOCKET)
    `hansa notify off`    stops that one; `hansa notify` lists the desk's followers

Where things are kept:
    .hansa/followers/<key>.json   the desk's register: one record per follower (session, socket, read
                                  position, when it was last seen, its last post); written by the desk's owner
    <private>/<key>.state.json    the follower's own: its pid, and the nonce it was started with
    <private>/<key>.lock          held while it runs: one follower per session and desk
    <private>/<key>.log           what it printed
<private> is .hansa/followers/ too, except for root on an account's desk (see account.py): root neither reads
nor writes in the desk, so its follower asks the account's hansa for the log (`hansa notify-feed`) and keeps
its own files under root's home.

A task or request goes to the session that made it (`hansa add`, `retry`, `verify`, `merge`, `design` and
`ask` record it: its pid and start time), if that session has a live follower; otherwise to every follower
of the desk. Desk-wide events go to every follower.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import signal
import socket
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import account
from .desk import Desk, now, read_json, write_json
from .run import alive, claude_bin, proc_start, server_up, srt_needed

LIVE_SECONDS = 30  # a follower not seen for this long is not live (it polls every second or two)
MOST_LINES = 10  # past this many events in one message, the rest are counted, and hansa status has them
POST_WAIT = 60  # seconds a post is tried again while the inbox is not there yet
KEEP_DAYS = 7  # ended followers' records are kept this long
KEY = re.compile(r"^[0-9]+-[0-9a-f]{8}$")


# -- the session a command runs in ---------------------------------------------------------------------
def session_here():
    """The Claude Code session this command was run from: {pid, start}, or None outside one (a shell, the
    page, a worker). The start time tells the session from a later process given the same pid."""
    pid = os.environ.get("CLAUDE_PID", "")
    if not pid.isdigit() or os.environ.get("HANSA_TASK"):
        return None
    return {"pid": int(pid), "start": proc_start(int(pid))}


def recorded(sess) -> dict:
    """The fields that record the session in a task or request; none outside a session."""
    return {"session": sess} if sess else {}


def key_of(sess: dict) -> str:
    return f"{int(sess['pid'])}-{hashlib.sha1(str(sess.get('start') or '').encode()).hexdigest()[:8]}"


def same(a, b) -> bool:
    """Whether two recorded sessions are one: the same pid, started at the same time (when both are known)."""
    if not isinstance(a, dict) or not isinstance(b, dict) or a.get("pid") != b.get("pid"):
        return False
    return not a.get("start") or not b.get("start") or a["start"] == b["start"]


def gone(sess: dict) -> bool:
    return not alive(sess.get("pid"), sess.get("start") or "")


# -- the desk's side: the register and the log ----------------------------------------------------------
def register_dir(desk: Desk) -> Path:
    return desk.state_dir / "followers"


def records(desk: Desk) -> list:
    d = register_dir(desk)
    out = []
    for f in sorted(d.glob("*.json")) if d.is_dir() else []:
        r = read_json(f)
        if isinstance(r, dict) and KEY.match(str(r.get("key", ""))):
            out.append(r)
    return out


def is_live(r: dict) -> bool:
    return not r.get("ended") and time.time() - (r.get("seen") or 0) < LIVE_SECONDS


def live_records(desk: Desk) -> list:
    return [r for r in records(desk) if is_live(r)]


def log_end(desk: Desk) -> int:
    """Where the log's last whole line ends."""
    try:
        with open(desk.events_path, "rb") as f:
            size = f.seek(0, os.SEEK_END)
            f.seek(max(0, size - 65536))
            tail = f.read()
    except FileNotFoundError:
        return 0
    return size - (len(tail) - tail.rfind(b"\n") - 1) if b"\n" in tail else max(0, size - len(tail))


def read_log(desk: Desk, pos: int, most: int = 8 << 20):
    """The events after pos, whole lines only, and where they end."""
    try:
        with open(desk.events_path, "rb") as f:
            f.seek(pos)
            data = f.read(most)
    except FileNotFoundError:
        return [], pos
    cut = data.rfind(b"\n") + 1
    events = []
    for line in data[:cut].splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict):
            events.append(e)
    return events, pos + cut


def _next_step(desk: Desk, tid: str) -> str:
    """What hansa show would suggest next for this task, with hansa show first."""
    from .views import show_text
    try:
        nxt = next((x[6:] for x in show_text(desk, tid).splitlines() if x.startswith("next: ")), "")
    except (SystemExit, OSError, ValueError, KeyError):
        nxt = ""
    nxt = nxt.replace("read the report above", f"read hansa show {tid}")
    return nxt if "hansa show" in nxt else f"hansa show {tid}" + (f" · {nxt}" if nxt else "")


def task_line(desk: Desk, m: dict) -> str:
    """A task that ended, as it is now, in one line with the next command."""
    tid, st = m["id"], m["state"]
    if m.get("kind") == "verify":
        head = (f"{tid}, the verification of {m.get('parent')}, ended: "
                + (f"verdict: {m['verdict']}" if m.get("verdict") else f"{st}, without a verdict"))
    elif m.get("kind") == "design":
        head = f"design session {tid} ended ({st})"
    else:
        head = f"{tid} {st}"
    head += f": {m.get('title', '')}"
    chk = m.get("check") or {}
    if st == "done" and m.get("kind") != "verify":
        head += (" (no changes)" if not m.get("commits") else
                 " (its check could not run: the machine's problem, not the task's)" if chk.get("could_not_run") else
                 " (check passed)" if chk.get("ok") else
                 f" (check FAILED{': ' + chk['note'] if chk.get('note') else ''})" if chk else "")
    elif st in ("failed", "cancelled") and m.get("error") and m.get("error") != st:
        head += f" ({str(m['error'])[:200]})"
    elif st == "merging":
        head += " (its merge check is queued or running)"
    return f"{head} · next: {_next_step(desk, tid)}"


def _item(desk: Desk, e: dict):
    """What one event tells the coordinator: (key, line, recorded session, desk-wide), or None."""
    kind, tid = e.get("event"), e.get("task")
    if kind in ("task_finished", "task_checked", "task_not_started", "merge_check_ended", "task_merged"):
        if kind == "task_finished" and e.get("state") in ("checking", "queued"):  # not ended yet / moved on
            return None
        if kind == "merge_check_ended" and e.get("note") == "cancelled":  # the coordinator cancelled it
            return None
        if kind == "task_merged" and not e.get("merge_checked"):  # a merge the coordinator ran and saw
            return None
        try:
            m = desk.meta(tid)
        except SystemExit:
            return None
        if kind == "merge_check_ended":
            key = ("merge", tid)
            line = (f"{tid}'s merge check ended; nothing was merged: {e.get('note') or 'the check failed'}: "
                    f"{m.get('title', '')} · next: {_next_step(desk, tid)}")
        elif kind == "task_merged":
            key, line = ("merge", tid), f"{tid} merged into {e.get('into')} after its merge check passed: {m.get('title', '')}"
        else:
            key, line = ("task", tid), task_line(desk, m)
        return key, line, m.get("session"), False
    if kind == "ask_closed" and e.get("by") != "hansa":  # hansa closes a design session's request itself
        x = next((a for a in desk.asks() if a.get("id") == e.get("id")), None)
        if not x:
            return None
        return (("ask", x["id"]), f"request #{x['id']} answered: {x.get('answer') or '(closed without a note)'}"
                                  f" (you asked: {str(x.get('text', ''))[:120]})", x.get("session"), False)
    if kind == "server_stopped":
        return ("server",), ("hansa serve stopped: no task starts or ends until it runs again · next: "
                             "hansa explain server"), None, True
    if kind == "srt_tried" and not e.get("ran") and srt_needed(desk.settings()):
        said = ("found, but it could not run: " + str(e.get("said") or "")[:200]) if e.get("found") \
            else "not found on its PATH"
        return ("srt",), (f"hansa serve could not run srt ({said}), so the lanes' setups and checks cannot run "
                          "· next: hansa explain server"), None, True
    return None


def items_for(desk: Desk, events: list, me: dict) -> list:
    """What these events tell this follower's session, one line per task, request or desk-wide event."""
    others = [r.get("session") for r in live_records(desk) if r.get("key") != me.get("key")]
    out = {}
    for e in events:
        it = _item(desk, e)
        if not it:
            continue
        key, line, sess, desk_wide = it
        if not (desk_wide or not sess or same(sess, me.get("session")) or not any(same(sess, o) for o in others)):
            continue  # its own session hears it
        out.pop(key, None)  # the latest line of a task comes last
        out[key] = {"key": list(key), "line": desk.redact(line)}
    return list(out.values())


def _write_record(desk: Desk, key: str, rec: dict) -> None:
    register_dir(desk).mkdir(parents=True, exist_ok=True)
    write_json(register_dir(desk) / f"{key}.json", rec)


def handle(desk: Desk, req: dict) -> dict:
    """The desk's part of a follower's work, done as the desk's owner: start, feed, end, list."""
    op = req.get("op")
    if op == "list":
        return {"followers": live_records(desk)}
    me = req.get("me") or {}
    key = str(me.get("key", ""))
    if not KEY.match(key):
        raise ValueError(f"not a follower's key: {key!r}")
    old = read_json(register_dir(desk) / f"{key}.json", {}) or {}
    if op == "start":
        end = log_end(desk)
        mine = [r for r in records(desk) if r.get("uid") == me.get("uid") and not is_live(r)]
        for r in mine:  # long ended: forgotten
            if time.time() - (r.get("seen") or 0) > KEEP_DAYS * 86400:
                (register_dir(desk) / f"{r['key']}.json").unlink(missing_ok=True)
        prev = max(mine, key=lambda r: r.get("seen") or 0, default=None)
        summary = []
        if prev and int(prev.get("pos") or 0) < end:  # it ended before reading to the end: what was missed
            events, _ = read_log(desk, int(prev.get("pos") or 0), end - int(prev.get("pos") or 0))
            seen = set()
            for x in items_for(desk, events, me):  # one line per task or request, as it is now
                k = ("task", x["key"][1]) if x["key"][0] in ("task", "merge") else tuple(x["key"])
                if k in seen:
                    continue
                seen.add(k)
                try:
                    summary.append(desk.redact(task_line(desk, desk.meta(k[1]))) if k[0] == "task" else x["line"])
                except SystemExit:  # a task folder gone meanwhile
                    continue
        _write_record(desk, key, {**me, "pos": end, "since": me.get("since") or now(), "seen": now(), "ended": None})
        return {"pos": end, "summary": summary, "missed_since": (prev or {}).get("seen") if summary else None,
                "server_up": server_up(desk)}
    if op == "feed":
        events, pos = read_log(desk, int(req.get("pos") or 0))
        items = items_for(desk, events, me)
        _write_record(desk, key, {**old, **me, "pos": int(req.get("done") or 0), "seen": now(), "ended": None})
        return {"pos": pos, "items": items, "server_up": server_up(desk)}
    if op == "end":
        _write_record(desk, key, {**old, **me, "pos": int(req.get("done") or old.get("pos") or 0), "seen": now(),
                                  "ended": now()})
        return {}
    raise ValueError(f"unknown request {op!r}")


def cmd_feed(desk: Desk) -> None:
    """`hansa notify-feed`: handle one request from standard input for a follower of root's."""
    print(json.dumps(handle(desk, json.loads(sys.stdin.read()))))


def source(desk: Desk):
    """How this process reaches the desk's part: directly, or as root through the account's hansa."""
    theirs = account.desk_account(desk.root)
    if not theirs:
        return lambda req: handle(desk, req)

    def via_account(req):
        r = account.run(theirs, ["notify-feed"], desk.root, input=json.dumps(req), capture_output=True, text=True,
                        timeout=120)
        if r.returncode:
            raise RuntimeError(f"the account's hansa notify-feed ended with {r.returncode}: {r.stderr.strip()[-300:]}")
        return json.loads(r.stdout)
    return via_account


def private_dir(desk: Desk) -> Path:
    """Where this account's followers of the desk keep their own files."""
    if account.desk_account(desk.root):
        d = Path.home() / ".hansa" / "followers" / hashlib.sha1(str(desk.root).encode()).hexdigest()[:12]
    else:
        d = register_dir(desk)
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


# -- posting --------------------------------------------------------------------------------------------
def post(path: str, text: str) -> None:
    """Post one message to a session's inbox: one line of JSON, then close. HANSA_NOTIFY_CAPTURE names a
    file that gets what would be sent instead (the tests' sandbox cannot make a Unix socket)."""
    line = json.dumps({"type": "user", "message": {"role": "user", "content": text}}, ensure_ascii=False) + "\n"
    capture = os.environ.get("HANSA_NOTIFY_CAPTURE")
    if capture:
        with open(capture, "a", encoding="utf-8") as f:
            f.write(json.dumps({"socket": path, "line": line}, ensure_ascii=False) + "\n")
        return
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(30)
        s.connect(path)
        s.sendall(line.encode("utf-8"))


def message(desk_root: str, lines: list, head: str = "") -> str:
    shown = lines if len(lines) <= MOST_LINES else lines[:MOST_LINES]
    more = [f"- and {len(lines) - len(shown)} more: hansa status"] if len(lines) > len(shown) else []
    return "\n".join([f"[hansa] desk {desk_root}" + (f": {head}" if head else "")] + [f"- {x}" for x in shown] + more)


# -- the follower ---------------------------------------------------------------------------------------
def session_name(pid: int) -> str:
    """The session's name as `claude agents --json` lists it; '' when it cannot say."""
    try:
        r = subprocess.run([claude_bin(), "agents", "--json"], capture_output=True, text=True, timeout=15,
                           stdin=subprocess.DEVNULL)
        data = json.loads(r.stdout)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        return ""
    rows = data if isinstance(data, list) else next((v for v in data.values() if isinstance(v, list)), []) \
        if isinstance(data, dict) else []
    return next((str(x.get("name") or "")[:80] for x in rows if isinstance(x, dict) and str(x.get("pid")) == str(pid)), "")


def _say(text: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + text, flush=True)


def follow(desk: Desk, key: str, sess: dict, sock: str, nonce: str, name: str = "") -> int:
    """`hansa notify-follow`: run the follower of one session until that session's process ends."""
    priv = private_dir(desk)
    held = open(priv / f"{key}.lock", "a")
    for _ in range(10):
        try:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            time.sleep(0.2)
    else:
        _say("another follower already runs for this session; this one ends")
        return 0
    me_proc = {"pid": os.getpid(), "start": proc_start(os.getpid())}
    write_json(priv / f"{key}.state.json", {**me_proc, "nonce": nonce, "session": sess, "socket": sock,
                                             "since": now()})
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    ask = source(desk)
    via_account = bool(account.desk_account(desk.root))
    poll = float(os.environ.get("HANSA_NOTIFY_POLL") or (2.0 if via_account else 1.0))
    gather = float(os.environ.get("HANSA_NOTIFY_GATHER") or 3.0)
    me = {"key": key, "session": sess, "socket": sock, "name": name, "follower": me_proc, "uid": os.getuid(),
          "user": account.name_of(os.getuid()), "since": now(), "last_post": None}
    done = 0
    try:
        r = ask({"op": "start", "me": me})
        pos = done = r["pos"]
        was_up, told_down = r.get("server_up"), False
        _say(f"following desk {desk.root} for session {sess['pid']} at {sock}, from byte {pos}")
        if r.get("summary"):
            since = time.strftime("%H:%M", time.localtime(r["missed_since"])) if r.get("missed_since") else "earlier"
            text = message(str(desk.root), r["summary"],
                           f"no session heard from hansa since {since}; what changed meanwhile, as it is now")
            if not _deliver(sock, text, sess, me):
                return 0
        pending, first, last, errors, named = [], 0.0, 0.0, 0, time.time() + 5
        while True:
            if gone(sess):
                _say("the session's process ended; so does this follower")
                return 0
            if time.time() >= named:
                me["name"] = session_name(sess["pid"]) or me["name"]
                named = time.time() + 300
            try:
                r = ask({"op": "feed", "me": me, "pos": pos, "done": done})
                errors = 0
            except Exception as e:  # noqa: BLE001 - the desk may be busy or moving; try again, then give up
                errors += 1
                _say(f"could not read the desk: {e}")
                if errors >= 60:
                    return 1
                time.sleep(poll)
                continue
            pos, items = r["pos"], r["items"]
            if any(x["key"] == ["server"] for x in items):
                told_down = True
            up = r.get("server_up")
            if was_up and not up and not told_down:  # stopped without saying so: its heartbeat went quiet
                items.append({"key": ["server"], "line": "hansa serve is not running (it stopped without a word): "
                                                         "no task starts or ends until it runs again · next: "
                                                         "hansa explain server"})
                told_down = True
            elif up and was_up is False:
                told_down = False
            was_up = up
            if items:
                pending += [x["line"] for x in items]
                last = time.time()
                first = first or last
            if pending and (time.time() - last >= gather or time.time() - first >= 20):
                if not _deliver(sock, message(str(desk.root), pending), sess, me):
                    return 0
                pending, first, done = [], 0.0, pos
            time.sleep(poll)
    finally:
        try:
            ask({"op": "end", "me": me, "done": done})
        except Exception as e:  # noqa: BLE001 - the register then shows it as gone quiet, which it is
            _say(f"could not record the end: {e}")
        write_json(priv / f"{key}.state.json", {**read_json(priv / f"{key}.state.json", {}), "ended": now()})


def _deliver(sock: str, text: str, sess: dict, me: dict) -> bool:
    """Post, trying again for a minute while the inbox is not there yet. False when the session is gone."""
    end = time.time() + POST_WAIT
    while True:
        try:
            post(sock, text)
            me["last_post"] = {"at": now(), "text": text.splitlines()[1][2:][:160] if "\n" in text else text[:160]}
            _say("posted: " + text.replace("\n", " | ")[:500])
            return True
        except OSError as e:
            if gone(sess):
                _say(f"the session's process ended before the post ({e}); so does this follower")
                return False
            if time.time() >= end:
                me["last_post"] = {"at": now(), "text": f"could not post: {e}", "failed": True}
                _say(f"gave up posting after {POST_WAIT} s ({e}): " + text.replace("\n", " | ")[:500])
                return True
            time.sleep(2)


# -- starting and stopping one ------------------------------------------------------------------------------
def spawn(desk: Desk, args: list, log: Path) -> None:
    """Start `hansa ARGS` detached: a session of its own, not the caller's child, keeping none of the caller's
    files open (so the caller's tool call ends at once), its output going to log."""
    env = dict(os.environ)
    here = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = here + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    argv = [sys.executable, "-m", "hansa", "--desk", str(desk.root), *args]
    out = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    child = os.fork()
    if child == 0:  # pragma: no cover - runs in the forked process
        try:
            os.setsid()
            if os.fork():
                os._exit(0)
            null = os.open(os.devnull, os.O_RDONLY)
            os.dup2(null, 0)
            os.dup2(out, 1)
            os.dup2(out, 2)
            os.closerange(3, min(os.sysconf("SC_OPEN_MAX"), 65536) if os.sysconf("SC_OPEN_MAX") > 0 else 65536)
            os.chdir("/")
            os.execve(argv[0], argv, env)
        finally:
            os._exit(127)
    os.close(out)
    os.waitpid(child, 0)


def running(desk: Desk, key: str):
    """The follower of this session, when one runs: its state, else None."""
    st = read_json(private_dir(desk) / f"{key}.state.json", {}) or {}
    return st if st.get("pid") and not st.get("ended") and alive(st["pid"], st.get("start", "")) else None


def start(desk: Desk, sess: dict, sock: str, name: str = "", wait: bool = True) -> str:
    """Start the follower of this session: 'started', 'already' (one runs), or 'unknown' (it has not said yet)."""
    key = key_of(sess)
    if running(desk, key):
        return "already"
    nonce = uuid.uuid4().hex
    spawn(desk, ["notify-follow", "--key", key, "--pid", str(sess["pid"]), "--start", sess.get("start") or "",
                 "--socket", sock, "--nonce", nonce, "--name", name], private_dir(desk) / f"{key}.log")
    end = time.time() + (10 if wait else 0)
    while time.time() < end:
        st = running(desk, key)
        if st and st.get("nonce") == nonce:
            return "started"
        if st:
            return "already"
        time.sleep(0.1)
    return "unknown"


def socket_path(pid: int) -> str:
    """A short inbox path for a session hansa starts, in a folder only this account can open."""
    for base in (os.environ.get("XDG_RUNTIME_DIR"), os.environ.get("TMPDIR"), "/tmp"):
        if not base or not os.path.isabs(base):
            continue
        d = os.path.join(base.rstrip("/"), f"hansa-{os.getuid()}")
        if len(d) > 80:  # a socket's path stays under 104 bytes on macOS
            continue
        try:
            os.mkdir(d, 0o700)
        except FileExistsError:
            pass
        except OSError:
            continue
        st = os.lstat(d)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            continue
        if stat.S_IMODE(st.st_mode) & 0o077:
            os.chmod(d, 0o700)
        path = os.path.join(d, f"c{pid}.sock")
        try:
            if stat.S_ISSOCK(os.lstat(path).st_mode):  # left by an earlier process with this pid
                os.unlink(path)
        except FileNotFoundError:
            pass
        return path
    raise RuntimeError("there is no short folder only this account can open for the session's inbox")


def turn_on(desk: Desk) -> str:
    sess, sock = session_here(), os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET", "")
    if os.environ.get("HANSA_TASK"):
        raise SystemExit("hansa: notify is for the coordinator's session, not a worker's; nothing was started")
    if not sess or not sock:
        raise SystemExit("hansa: notify on starts hansa posting to the Claude Code session it is run from, and this "
                         "is not run from one (CLAUDE_PID and CLAUDE_CODE_MESSAGING_SOCKET are not set); nothing was "
                         "started. Run it with the coordinator session's Bash tool")
    how = start(desk, sess, sock)
    if how == "already":
        return (f"hansa already notifies this session (pid {sess['pid']}); nothing was started. hansa notify lists "
                "the desk's followers")
    if how == "unknown":
        raise SystemExit(f"hansa: the follower did not say it started within 10 s; see "
                         f"{private_dir(desk) / (key_of(sess) + '.log')}")
    return (f"hansa now posts to this session (pid {sess['pid']}) through its inbox: its own tasks and requests, and "
            "those no other session hears. Do not keep hansa wait running in the background meanwhile. "
            "Stop it with hansa notify off")


def turn_off(desk: Desk) -> str:
    sess = session_here()
    if not sess:
        raise SystemExit("hansa: notify off stops the follower of the Claude Code session it is run from, and this "
                         "is not run from one; nothing was stopped. hansa notify lists the desk's followers")
    st = running(desk, key_of(sess))
    if not st:
        return "hansa does not notify this session; nothing to stop"
    try:
        os.kill(st["pid"], signal.SIGTERM)
    except OSError:
        pass
    end = time.time() + 10
    while time.time() < end and alive(st["pid"], st.get("start", "")):
        time.sleep(0.1)
    return ("hansa no longer posts to this session. To hear when tasks end, run hansa wait in the background, "
            "or hansa notify on again")


def when(t) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(t)) if t else "?"


def list_text(desk: Desk) -> str:
    rows = source(desk)({"op": "list"})["followers"]
    if not rows:
        return ("hansa notifies no session of this desk. From the coordinator's session: hansa notify on (a session "
                "started with hansa coordinator is notified by itself)")
    out = [f"hansa notifies {len(rows)} session(s) of desk {desk.root} through their inbox:"]
    for r in rows:
        lp = r.get("last_post") or {}
        out.append(f"  {r.get('name') or 'session'} · pid {(r.get('session') or {}).get('pid')} · since "
                   f"{when(r.get('since'))} · last post: " + (f"{when(lp.get('at'))} {lp.get('text', '')}" if lp
                                                               else "none yet"))
    return "\n".join(out)


def names(desk: Desk) -> list:
    """The sessions hansa notifies, for hansa status."""
    return [f"{r.get('name') or 'session'} (pid {(r.get('session') or {}).get('pid')})" for r in live_records(desk)]
