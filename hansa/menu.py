"""The principal's terminal screen. Reading rows never starts a process or writes a desk."""
import contextlib
import os
from pathlib import Path
import select
import shlex
import shutil
import signal
import subprocess
import sys
import termios
import time
import unicodedata

from . import account, notify, release, session_vars, up, views
from .desk import Desk, listed_desks, program, read_json
from .run import server_state


def available():
    return (sys.stdin.isatty() and sys.stdout.isatty() and not session_vars()
            and not os.environ.get("HANSA_ROLE") and not os.environ.get("HANSA_TASK"))


def language(lang=None):
    locale = next((os.environ[k] for k in ("LC_ALL", "LC_MESSAGES", "LANG") if os.environ.get(k)), "")
    return lang or ("zh" if locale.lower().startswith("zh") else "en")


# Shared nouns and states follow ui.html's Chinese strings.
WORDS = {
    "headers": ("Tick|Desk|Server|Coordinator|Paused|Work|For you", "勾选|工作台|后台|协调席|已暂停|任务|等主理人答复"),
    "title": ("hansa · choose a desk · refreshes every 5 seconds", "hansa · 选择工作台 · 每 5 秒刷新"),
    "keys1": ("↑↓ choose  Space tick  a all  s start  Enter enter  w page",
              "↑↓ 选择  空格 勾选  a 全选  s 启动  回车 进入  w 页面"),
    "keys2": ("x stop  p pause/resume  r refresh  q quit", "x 停止  p 暂停/恢复派活  r 刷新  q 退出"),
    "start": ("Press s to start this desk; Space ticks several.", "按 s 启动这个工作台；空格可勾选多个。"),
    "enter": ("Press Enter for its coordinator; or on this machine: {attach}",
              "按回车进入协调席；或在本机运行：{attach}"),
    "open_session": ("Press s to open its coordinator.", "按 s 打开协调席。"),
    "started": ("{desk}: started; {attach}", "{desk}：已启动；{attach}"),
    "not_started": ("{desk}: not started; see the lines above.", "{desk}：未启动；请看上面的输出。"),
    "empty": ("No desks listed. hansa init makes a desk; its first server start lists it. q quits.",
              "没有工作台。hansa init 创建工作台；首次启动后台后列入清单。q 退出。"),
    "gone": ("folder is gone; nothing to do", "文件夹已不存在；没有可做的操作"),
    "unreadable": ("folder cannot be read as a desk; nothing to do", "无法读取工作台；没有可做的操作"),
    "up": ("running", "运行中"), "busy": ("busy", "在忙"), "stuck": ("stuck", "卡住了"),
    "stopped": ("stopped", "已停止"), "open": ("open", "已打开"), "closed": ("not open", "未打开"),
    "yes": ("yes", "已暂停"), "no": ("no", "否"),
    "work": ("{running} running, {review} review", "{running} 运行中，{review} 做完待审"),
    "principal": ("{releases} releases, {todos} to-dos", "{releases} 发布，{todos} 待办"),
    "return": ("Press any key to return to the menu.", "按任意键返回菜单。"),
    "session": ("The coordinator's session is not open. Press s to start it.", "协调席未打开。按 s 启动。"),
    "no_tmux": ("Its coordinator is not in tmux; Enter cannot reach it.", "协调席不在 tmux 中，回车无法进入。"),
    "install_tmux": ("tmux is missing; install it to enter the session.", "找不到 tmux；安装后才能进入协调席。"),
    "leave_tmux": ("Leave tmux first (Ctrl+B, then D), then run: ", "请先离开 tmux（Ctrl+B，再按 D），然后运行："),
    "detach": ("Inside, press Ctrl+B, then D to return; the coordinator keeps running.",
               "进入后，按 Ctrl+B，再按 D 返回菜单；协调席会继续运行。"),
    "server": ("The server is stopped. Press s to start it.", "后台已停止。按 s 启动。"),
    "tunnel": ("The computer with the browser needs a tunnel: ssh -L {port}:127.0.0.1:{port} HOST",
               "浏览器所在的电脑需要隧道：ssh -L {port}:127.0.0.1:{port} HOST"),
    "confirm": ("Stop this desk's server? Press y. The coordinator's session stays open.",
                "停止这个工作台的后台？按 y 确认。协调席保持打开。"),
    "working": ("Still running: {work}. Cannot stop the server yet. p pauses new work; stop it once this ends.",
                "仍在运行：{work}。暂时不能停止后台。按 p 暂停派活，等这些结束后再停止后台。"),
    "task": ("tasks", "任务"), "verify": ("verifications", "核对活"),
    "merge": ("merge checks", "合并验收"), "release": ("releases", "发布"), "try": ("lane tries", "工作线试跑"),
    "ended": ("Server stopped.", "后台已停止。"),
    "timeout": ("Server did not stop within 30 seconds (pid {pid}).", "后台在 30 秒内没有停止（pid {pid}）。"),
}


def words(lang, key, **values):
    return WORDS[key][lang == "zh"].format(**values)


def rows():
    state = views.desks()
    saved = {str(root): rec for root, rec in listed_desks(include_missing=True)}
    for row in state["desks"]:
        row["saved"] = saved.get(row["desk"], {})
        if not row.get("problem"):
            desk = Desk(row["desk"])
            try:
                row.update(coordinator=bool(notify.names(desk) or up.still_runs(up.read_coordinator(desk))),
                           paused=desk.paused() is not None)
            except (OSError, UnicodeError):
                row["problem"] = "folder cannot be read as a desk"
    return state


def attachment(desk, env):
    rec = up.read_coordinator(desk)
    saved, launch = rec.get("tmux") or {}, rec.get("env") or {}
    server, session = saved.get("server"), saved.get("session")
    if launch.get("TMUX_TMPDIR"):
        env["TMUX_TMPDIR"] = launch["TMUX_TMPDIR"]
    else:
        env.pop("TMUX_TMPDIR", None)
    tmux = program("tmux") or program("tmux", env=launch)
    attach = up.attach_command(tmux or "tmux", server, session, env) if server and session else ""
    return tmux, server, session, attach


def guide(row, lang):
    if row.get("problem"):
        return []
    if row["server"] == "gone":
        return [words(lang, "start")]
    if not row["coordinator"]:
        return [words(lang, "open_session")]
    attach = attachment(Desk(row["desk"]), dict(os.environ))[3]
    return ([words(lang, "enter", attach=attach), words(lang, "detach")] if attach
            else [words(lang, "no_tmux")])


def command(row, key, lang):
    """(argv, environment, message): decide without running tmux or changing the desk."""
    desk, env = Desk(row["desk"]), dict(os.environ)
    base = [row["saved"].get("hansa") or "", "--desk", row["desk"]]
    if key in ("p", "w") and not up.installation_up(base[0]):
        return [], env, f"Desk {row['desk']}:\n" + up.listed_problem(desk, row["saved"])
    if key == "p":
        return base + (["resume"] if row["paused"] else ["pause", "hansa menu"]), env, ""
    if key == "w":
        if row["server"] == "gone":
            return [], env, words(lang, "server")
        note = words(lang, "tunnel", port=views.web_address(desk)[1]) if env.get("SSH_CONNECTION") else ""
        return base + ["web"], env, note
    if not row["coordinator"]:
        return [], env, words(lang, "session")
    tmux, server, session, attach = attachment(desk, env)
    if not server or not session:
        return [], env, words(lang, "no_tmux")
    if not tmux:
        return [], env, words(lang, "install_tmux")
    if os.environ.get("TMUX"):
        socket = (Path(server[1]) if server[0] == "-S" else
                  Path(env.get("TMUX_TMPDIR", "/tmp")) / f"tmux-{os.getuid()}" / server[1])
        if socket.resolve() != Path(os.environ["TMUX"].rsplit(",", 2)[0]).resolve():
            return [], env, words(lang, "leave_tmux") + attach
        return [tmux, *server, "switch-client", "-t", session], env, ""
    return shlex.split(attach), env, words(lang, "detach")


def busy_counts(desk):
    tasks = desk.tasks()
    return {
        "task": sum(m.get("state") == "running" and m.get("kind") != "verify" for m in tasks),
        "verify": sum(m.get("state") == "running" and m.get("kind") == "verify" for m in tasks),
        "merge": sum(m.get("state") == "merging" and (m.get("merge_check") or {}).get("phase")
                     in ("starting", "running") for m in tasks),
        "release": sum(r.get("state") in ("starting", "running") for r in release.releases(desk)),
        "try": sum((read_json(f, {}) or {}).get("phase") == "running"
                   for f in (desk.state_dir / "tries").glob("*.json")),
    }


def stop_server(desk, lang):
    """Called only after y. Read again then: work may have begun during the question."""
    theirs = account.desk_account(desk.root)
    if theirs:
        return f"This desk belongs to {theirs}; run as {theirs}."
    counts = busy_counts(desk)
    if any(counts.values()):
        work = ", ".join(f"{n} {words(lang, k)}" for k, n in counts.items() if n)
        return words(lang, "working", work=work)
    state = server_state(desk)
    if state["state"] == "gone":
        return words(lang, "ended")
    pid = state["pid"]
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return words(lang, "ended")
    end = time.monotonic() + 30
    while True:
        state = server_state(desk)
        if state["state"] == "gone" or state["pid"] != pid:
            return words(lang, "ended")
        if time.monotonic() >= end:
            return words(lang, "timeout", pid=pid)
        time.sleep(0.1)


def cells(text):
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def cut(text, width):
    out = ""
    for c in text:
        c = "?" if unicodedata.category(c).startswith("C") else c
        if cells(out + c) > width:
            break
        out += c
    return out


def screen(state, chosen, ticks, lang, width, height):
    width, height = width or 80, height or 24
    lines = [words(lang, "title")]
    table = [words(lang, "headers").split("|")]
    for i, row in enumerate(state["desks"]):
        name = "/".join(Path(row["desk"]).parts[-2:])
        values = [(">" if i == chosen else " ") + ("[x]" if row["desk"] in ticks else "[ ]"), name]
        if row.get("problem"):
            values += [words(lang, "gone" if row["problem"] == "folder is gone" else "unreadable"), "", "", "", ""]
        else:
            values += [words(lang, "stopped" if row["server"] == "gone" else row["server"]),
                       words(lang, "open" if row["coordinator"] else "closed"),
                       words(lang, "yes" if row["paused"] else "no"), words(lang, "work", **row),
                       words(lang, "principal", **row)]
        table.append(values)
    sizes = [max(cells(cut(r[i], 200)) for r in table) for i in range(7)]
    rendered = ["  ".join(cut(v, 200) + " " * (sizes[i] - cells(cut(v, 200))) for i, v in enumerate(r)) for r in table]
    tail = [state["problem"] or words(lang, "empty")] if state["problem"] or not state["desks"] else []
    tail += guide(state["desks"][chosen], lang) if state["desks"] else []
    tail += [words(lang, "keys1"), words(lang, "keys2")]
    room = max(0, height - 2 - len(tail))
    offset = max(0, chosen - room + 1)
    lines += rendered[:1] + rendered[1 + offset:1 + offset + room]
    lines = lines[:max(0, height - len(tail))] + tail
    return "\x1b[H\x1b[2J" + "\r\n".join(cut(line.rstrip(), width) for line in lines[:height])


class Terminal:
    def write(self, text):
        sys.stdout.buffer.write(text.encode("utf-8", errors="replace"))
        sys.stdout.buffer.flush()

    def raw(self):
        attrs = termios.tcgetattr(0)
        attrs[3] &= ~(termios.ECHO | termios.ICANON)
        attrs[6][termios.VMIN], attrs[6][termios.VTIME] = 1, 0
        termios.tcsetattr(0, termios.TCSANOW, attrs)

    def display(self):
        self.raw()
        self.write("\x1b[?1049h\x1b[?25l")

    def restore(self):
        try:
            termios.tcsetattr(0, termios.TCSANOW, self.old)
        finally:
            self.write("\x1b[?25h\x1b[?1049l")

    @contextlib.contextmanager
    def opened(self):
        self.old = termios.tcgetattr(0)
        try:
            self.display()
            yield self
        finally:
            self.restore()

    @contextlib.contextmanager
    def outside(self):
        self.restore()
        try:
            yield
        finally:
            self.display()

    def key(self, timeout=None):
        if not select.select([0], [], [], timeout)[0]:
            return None
        key = os.read(0, 1)
        if not key:
            raise EOFError
        if key == b"\x1b":
            while select.select([0], [], [], 0.1)[0] and len(key) < 3:
                more = os.read(0, 1)
                if not more:
                    raise EOFError
                key += more
        return key.decode("utf-8", errors="replace")


def perform(key, row, targets, terminal, lang):
    with terminal.outside():
        wait = True
        try:
            if key == "s":
                for target in targets:
                    if target.get("problem"):
                        print(target["desk"] + ": " + words(lang, "gone" if target["problem"] == "folder is gone" else "unreadable"))
                    else:
                        up.listed_desk_up(Path(target["desk"]), target["saved"], stream=True)
                fresh = {r["desk"]: r for r in rows()["desks"]}
                opened = False
                for target in targets:
                    current = fresh.get(target["desk"], {})
                    opened |= bool(current.get("coordinator"))
                    attach = (attachment(Desk(target["desk"]), dict(os.environ))[3]
                              if current.get("coordinator") and not current.get("problem")
                              and current.get("server", "gone") != "gone" else "")
                    print(words(lang, "started" if attach else "not_started", desk=target["desk"], attach=attach))
                if opened:
                    print(words(lang, "detach"))
            elif row.get("problem"):
                print(words(lang, "gone" if row["problem"] == "folder is gone" else "unreadable"))
            elif key == "x":
                print(words(lang, "confirm"), flush=True)
                terminal.raw()
                if terminal.key() == "y":
                    print(stop_server(Desk(row["desk"]), lang))
            else:
                argv, env, note = command(row, key, lang)
                if note:
                    print(note, flush=True)
                if argv:
                    wait = subprocess.call(argv, env=env) != 0 or key == "p"
        except KeyboardInterrupt:
            wait = False
        except (OSError, subprocess.SubprocessError) as e:
            print(str(e))
        if wait:
            print(words(lang, "return"), flush=True)
            terminal.raw()
            try:
                terminal.key()
            except KeyboardInterrupt:
                pass


def run(lang=None):
    lang, chosen, ticks = language(lang), 0, set()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # our messages too; child output inherits the fd untouched
    try:
        with Terminal().opened() as terminal:
            while True:
                state = rows()
                desks = state["desks"]
                chosen = min(chosen, max(0, len(desks) - 1))
                ticks.intersection_update(r["desk"] for r in desks)
                try:
                    size = os.get_terminal_size(1)  # a stale COLUMNS must not make lines wrap after a resize
                except OSError:
                    size = shutil.get_terminal_size()
                terminal.write(screen(state, chosen, ticks, lang, size.columns, size.lines))
                key = terminal.key(5)
                if key in ("q", "\x03"):
                    return
                if not desks:
                    continue
                row = desks[chosen]
                if key in ("\x1b[A", "\x1bOA", "\x1b[B", "\x1bOB"):
                    chosen = (chosen + (-1 if key[-1] == "A" else 1)) % len(desks)
                elif key == " ":
                    ticks.symmetric_difference_update([row["desk"]])
                elif key == "a":
                    ticks = set() if len(ticks) == len(desks) else {r["desk"] for r in desks}
                elif key in ("s", "\r", "\n", "w", "x", "p"):
                    targets = [r for r in desks if r["desk"] in ticks] if ticks else [row]
                    try:
                        perform(key, row, targets, terminal, lang)
                    finally:
                        if key == "s":
                            ticks.clear()
    except (KeyboardInterrupt, EOFError, OSError, termios.error):
        pass
