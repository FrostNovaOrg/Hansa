"""Read-only installation checks, independent of any desk or saved credentials."""
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile

from . import without_session
from .desk import program
from .menu import language


PYTHON_MIN = (3, 9)
CLAUDE_MIN = (2, 1, 306)
NODE_MIN = (22, 12, 0)
SRT_VERSION = "0.0.79"
TIMEOUT = 15
GUIDE = "docs/getting-started.md"


def guide_path(lang=None):
    chinese = "docs/zh-CN/getting-started.md"
    if language(lang) == "zh" and (Path(__file__).resolve().parent.parent / chinese).is_file():
        return chinese
    return GUIDE


def version_text(version):
    return ".".join(map(str, version))


def package_version(executable):
    """Follow npm's executable link to its own package, never srt's CLI version."""
    try:
        for folder in Path(executable).resolve().parents:
            package = folder / "package.json"
            if package.is_file():
                data = json.loads(package.read_text(encoding="utf-8"))
                if data.get("name") == "@anthropic-ai/sandbox-runtime":
                    value = data.get("version")
                    return value if isinstance(value, str) else ""
    except (OSError, ValueError, RuntimeError, AttributeError):
        pass
    return ""


def check(lang=None):
    lang = language(lang)
    guide = guide_path(lang)
    env = without_session(os.environ)
    rows = []

    def tr(en, zh):
        return zh if lang == "zh" else en

    def which(name):
        return shutil.which(name, path=env.get("PATH", os.defpath)) or ""

    def fix(name):
        if name == "claude":
            return "npm install -g @anthropic-ai/claude-code@" + version_text(CLAUDE_MIN)
        if name == "srt":
            return "npm install -g @anthropic-ai/sandbox-runtime@" + SRT_VERSION
        if sys.platform == "darwin":
            return "brew install " + name
        if name == "node":
            minimum = version_text(NODE_MIN)
            return tr(f"Install Node {minimum} or newer: https://nodejs.org/en/download",
                      f"安装 Node {minimum} 或更新版本：https://nodejs.org/en/download")
        if which("apt-get"):
            return "sudo apt-get install " + {"python": "python3"}.get(name, name)
        return tr("See ", "参阅 ") + guide

    def row(name, state, found, need, repair=""):
        rows.append(dict(name=name, state=state, found=found, need=need, fix=repair if state != "ok" else ""))

    def run(argv, *, quiet=False, cwd=None, child_env=None):
        try:
            with subprocess.Popen(argv, env=child_env or env, cwd=cwd, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL if quiet else subprocess.PIPE,
                                  stderr=subprocess.DEVNULL if quiet else subprocess.STDOUT,
                                  text=True, errors="replace", start_new_session=True) as process:
                try:
                    output, _ = process.communicate(timeout=TIMEOUT)
                except subprocess.TimeoutExpired:
                    # srt starts children; kill the group, then bound the drain too:
                    # a child can leave the group while keeping the output pipe open.
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        process.kill()
                    try:
                        output, _ = process.communicate(timeout=1)
                    except subprocess.TimeoutExpired as e:
                        output = e.output or b""
                        if isinstance(output, bytes):
                            output = output.decode("utf-8", errors="replace")
                    return False, tr(f"no answer in {TIMEOUT} s", f"{TIMEOUT} 秒内未响应"), output or ""
            output = (output or "").strip()
            if process.returncode:
                return False, tr(f"exit {process.returncode}", f"退出码 {process.returncode}"), output
            return True, "", output
        except OSError as e:
            return False, tr(f"cannot start: {e}", f"无法启动：{e}"), ""

    def located(name):
        path = which(name)
        return path or program(name, env), not bool(path)

    def path_note(path):
        return tr(f"; {path} is outside PATH; hansa serve needs it on its PATH",
                  f"；{path} 不在 PATH 中；请把这个工具所在目录加入 hansa serve 的 PATH")

    def path_fix(path):
        return "export PATH=" + shlex.quote(str(Path(path).absolute().parent)) + ':"$PATH"'

    current = tuple(sys.version_info[:3])
    row("Python", "ok" if current >= PYTHON_MIN else "fails", f"{version_text(current)} ({sys.executable})",
        ">= " + version_text(PYTHON_MIN), fix("python"))

    claude = ""
    for name, label, args, minimum in (("git", "Git", ["--version"], None),
                                       ("node", "Node", ["--version"], NODE_MIN),
                                       ("claude", "Claude Code", ["--version"], CLAUDE_MIN),
                                       ("tmux", "tmux", ["-V"], None)):
        path, extra = located(name)
        if name == "claude":
            claude = path
        need = ">= " + version_text(minimum) if minimum else tr("present", "已安装")
        if name == "tmux":
            need = tr("recommended; hansa up needs it to start the coordinator",
                      "建议安装；hansa up 需要它来启动协调员")
        if not path:
            found = tr("missing", "未找到")
            if name == "tmux":
                found += tr("; hansa up cannot start the coordinator without tmux",
                            "；没有 tmux，hansa up 无法启动协调员")
            row(label, "note" if name == "tmux" else "fails", found, need, fix(name))
            continue
        ok, error, output = run([path, *args])
        state = "ok" if ok else "fails"
        found = f"{output} ({path})" if ok else f"{path}: {error}"
        if ok and minimum:
            match = re.search(r"(?<!\d)(\d+)\.(\d+)\.(\d+)\b", output)
            if not match or tuple(map(int, match.groups())) < minimum:
                state = "fails"
        repair = fix(name)
        if extra:
            found += path_note(path)
            if state == "ok":
                state = "note"
                repair = path_fix(path)
            else:
                repair += "; " + path_fix(path)
        row(label, state, found, need, repair)

    login_fix = tr("Run claude auth login yourself (the principal).",
                   "请负责人亲自运行 claude auth login。")
    if claude:
        ok, error, _ = run([claude, "auth", "status"], quiet=True)
        row("Claude login", "ok" if ok else "fails", tr("logged in", "已登录") if ok else error,
            tr("logged in", "已登录"), login_fix)
    else:
        row("Claude login", "fails", tr("Claude Code missing", "未找到 Claude Code"),
            tr("logged in", "已登录"), fix("claude") + "; " + login_fix)

    srt = which("srt")
    need = tr(f"present; package {SRT_VERSION} recommended", f"已安装；建议使用软件包 {SRT_VERSION}")
    if srt:
        version = package_version(srt)
        row("srt", "ok" if version == SRT_VERSION else "note",
            f"{version or tr('package version unknown', '软件包版本未知')} ({srt})", need, fix("srt"))
    else:
        row("srt", "fails", tr("missing", "未找到"), need, fix("srt"))

    if sys.platform.startswith("linux"):
        for name, executable in (("bubblewrap", "bwrap"), ("socat", "socat")):
            path = which(executable)
            row(name, "ok" if path else "fails", path or tr("missing", "未找到"),
                tr("present on Linux", "Linux 上必须安装"), fix(name))

    need = tr("srt runs /usr/bin/true in a sandbox", "srt 在沙箱中运行 /usr/bin/true")
    if not srt:
        row("Sandbox", "skipped", tr("srt missing", "未找到 srt"), need, fix("srt"))
    else:
        prerequisites = [r["fix"] for r in rows if r["state"] == "fails"
                         and r["name"] in ("Node", "bubblewrap", "socat")]
        section = "#安装所需工具" if guide.startswith("docs/zh-CN/") else "#install-the-tools"
        repair = "; ".join([*prerequisites, tr("See ", "参阅 ") + guide + section])
        try:
            with tempfile.TemporaryDirectory(prefix="hansa-doctor-") as temporary:
                folder = Path(temporary).resolve()
                settings = folder / "settings.json"
                settings.write_text(json.dumps({"network": {"allowedDomains": [], "deniedDomains": []},
                                                "filesystem": {"allowWrite": [str(folder)], "denyWrite": [],
                                                               "denyRead": []}}), encoding="utf-8")
                ok, error, output = run([srt, "--settings", str(settings), "--", "/usr/bin/true"], cwd=str(folder),
                                        child_env={**env, "TMPDIR": str(folder), "CLAUDE_CODE_TMPDIR": str(folder)})
                found = tr("ran /usr/bin/true", "成功运行 /usr/bin/true") if ok else error
                if not ok and output:
                    found += "\n" + "\n".join(output.splitlines()[-8:])
                row("Sandbox", "ok" if ok else "fails", found, need, repair)
        except OSError as e:
            row("Sandbox", "fails", str(e), need, repair)

    # Keep installation order stable for both people and agents.
    order = ["Python", "Git", "Node", "Claude Code", "Claude login", "srt", "tmux", "bubblewrap", "socat", "Sandbox"]
    rows.sort(key=lambda r: order.index(r["name"]))
    return {"ok": not any(r["state"] == "fails" for r in rows), "rows": rows}


def text(result, lang=None):
    zh = language(lang) == "zh"
    states = {"ok": "正常", "note": "提示", "fails": "失败", "skipped": "跳过"} if zh else {}
    lines = ["hansa doctor · 只读检查" if zh else "hansa doctor · read-only checks"]
    for row in result["rows"]:
        lines.append(f"[{states.get(row['state'], row['state'])}] {row['name']}: {row['found']}")
        lines.append(f"  {'需要' if zh else 'Need'}: {row['need']}")
        if row["fix"]:
            lines.append(f"  {'修复' if zh else 'Fix'}: {row['fix']}")
    return "\n".join(lines)
