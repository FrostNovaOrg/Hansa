"""Workers: start one Claude Code run per task, record what came back, and
keep the queue moving.

A worker is `claude -p` in the lane's git worktree, on a branch of its own
(task/<id>). The prompt goes in on stdin; the JSON result lands in
tasks/<id>/run.json. Nothing here needs the worker to know about bh.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

from .desk import Desk, now, read_json

# Only the wording providers use for "out of quota", so a task that is itself
# about rate limits does not rest a provider when it fails for another reason.
QUOTA = re.compile(r"API Error: (402|429)|rate_limit_error|usage limit reached|credit balance is too low|"
                   r"insufficient[_ ](quota|balance)|quota exceeded|too many requests|余额不足|额度不足|额度已用", re.I)
QUOTA_STATUS = {402, 429}
AUTH = re.compile(r"authentication_error|invalid (x-)?api[ -]key|API Error: 40[13]\b|permission_error", re.I)
AUTH_STATUS = {401, 403}
KEY_REST_MINUTES = 24 * 60  # until the principal changes the key (saving a key clears the rest)
GIT_ID = {"GIT_AUTHOR_NAME": "bh", "GIT_AUTHOR_EMAIL": "bh@localhost",
          "GIT_COMMITTER_NAME": "bh", "GIT_COMMITTER_EMAIL": "bh@localhost"}
PROTOCOL = """You are a worker on task {tid} in lane {lane}. You work only in this directory,
on branch task/{tid}, which was made for this task.
- Commit your work on this branch; start each commit message with "{tid}:".
- Do not push, do not switch branches, do not touch other directories.
- If a command you need is refused, say which one and why you need it in your report;
  do not look for a way around the refusal.
- End with a short report: what you changed, how you checked it, and anything left undone.
"""


def claude_bin() -> str:
    for c in (os.environ.get("BH_CLAUDE"), shutil.which("claude"),
              os.path.expanduser("~/.local/bin/claude")):
        if c and os.access(c, os.X_OK):
            return c
    raise RuntimeError("claude was not found; install Claude Code or set BH_CLAUDE")


def git(*args, cwd, env=None) -> str:
    e = {**os.environ, **GIT_ID, **(env or {})}
    r = subprocess.run(["git", *args], cwd=str(cwd), env=e, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout.strip()


def rule(tool: str, path: Path) -> str:
    return f"{tool}(//{str(path).lstrip('/')}/**)"


def provider_problem(desk: Desk, s: dict, name: str) -> str:
    """Why this provider cannot take work right now; '' when it can."""
    p = s["providers"].get(name)
    if not p:
        return "not in settings"
    if p["kind"] == "compatible":
        if not p.get("base_url"):
            return "no endpoint address"
        if not desk.has_key(name):
            return "no key"
    st = desk.provider_state().get(name, {})
    if st.get("cool_until", 0) > time.time():
        return f"resting until {time.strftime('%H:%M', time.localtime(st['cool_until']))} ({st.get('reason', '')})"
    return ""


def provider_env(desk: Desk, name: str, p: dict) -> dict:
    """Environment for one Claude Code run on this provider. Starts clean, so a
    worker never inherits keys that are not its own."""
    env = {k: os.environ[k] for k in ("HOME", "PATH", "LANG", "LC_ALL", "USER", "LOGNAME", "SHELL", "TMPDIR")
           if k in os.environ}
    env["TERM"] = "dumb"
    if p["kind"] == "subscription":
        if p.get("config_dir"):
            env["CLAUDE_CONFIG_DIR"] = p["config_dir"]
    else:
        home = desk.state_dir / f"claude-{name}"
        home.mkdir(parents=True, exist_ok=True)
        env.update({
            "CLAUDE_CONFIG_DIR": str(home),
            "ANTHROPIC_BASE_URL": p["base_url"],
            "ANTHROPIC_AUTH_TOKEN": desk.read_key(name),
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        })
        for v in ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                  "ANTHROPIC_DEFAULT_HAIKU_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL"):
            env[v] = p["model"]
    return env


def lane_allow(desk: Desk, lane: dict, wt: Path) -> list:
    files = [rule(t, wt) for t in ("Read", "Edit", "Write", "Glob", "Grep")]
    return files + ["TodoWrite"] + list(lane.get("allow", []))


def prepare(desk: Desk, lane_name: str, lane: dict, meta: dict):
    """Put the lane's worktree on a fresh task branch. Returns (worktree, base sha)."""
    repo = Path(lane["repo"])
    tid = meta["id"]
    base = meta.get("base") or git("rev-parse", lane.get("base") or "main", cwd=repo)
    start = meta.get("start_from") or base
    wt = desk.worktrees_dir / lane_name
    if not (wt / ".git").exists():
        wt.parent.mkdir(parents=True, exist_ok=True)
        git("worktree", "add", "--detach", str(wt), start, cwd=repo)
    if git("status", "--porcelain", cwd=wt):
        git("stash", "push", "--include-untracked", "-m", f"bh: left over before {tid}", cwd=wt)
    branch = f"task/{tid}"
    if git("branch", "--list", branch, cwd=wt):
        # an earlier try of this task: keep its commits under another name
        n = len(meta.get("attempts", []))
        git("branch", "-m", branch, f"{branch}-try{n}", cwd=wt)
    git("checkout", "-q", "-B", branch, start, cwd=wt)
    return wt, base


def start(desk: Desk, tid: str, lane_name: str, pname: str) -> subprocess.Popen:
    s = desk.settings()
    lane, p, meta = s["lanes"][lane_name], s["providers"][pname], desk.meta(tid)
    wt, base = prepare(desk, lane_name, lane, meta)
    model = meta.get("model") or p["model"]
    effort = meta.get("effort") or p.get("effort", "")
    brief = desk.lanes_dir / lane_name / "brief.md"
    system = PROTOCOL.format(tid=tid, lane=lane_name)
    if brief.is_file() and brief.read_text(encoding="utf-8").strip():
        system += "\nStanding instructions for this lane:\n" + brief.read_text(encoding="utf-8")
    argv = [claude_bin(), "-p", "--output-format", "json", "--permission-mode", "acceptEdits",
            "--max-turns", str(meta.get("max_turns") or lane.get("max_turns", 80)),
            "--model", model, "--append-system-prompt", system]
    if effort:
        argv += ["--effort", effort]
    argv += ["--disallowedTools", rule("Read", desk.secrets_dir), rule("Read", desk.state_dir)]
    argv += ["--allowedTools", *lane_allow(desk, lane, wt)]
    env = {**provider_env(desk, pname, p), **lane.get("env", {}), "BH_TASK": tid,
           "GIT_AUTHOR_NAME": f"bh worker {lane_name}", "GIT_AUTHOR_EMAIL": "bh@localhost",
           "GIT_COMMITTER_NAME": f"bh worker {lane_name}", "GIT_COMMITTER_EMAIL": "bh@localhost"}
    d = desk.tasks_dir / tid
    with open(d / "task.md", "rb") as stdin, open(d / "run.json", "wb") as out, open(d / "run.err", "wb") as err:
        proc = subprocess.Popen(argv, cwd=str(wt), env=env, stdin=stdin, stdout=out, stderr=err,
                                start_new_session=True)
    desk.update(tid, state="running", lane=lane_name, provider=pname, model_asked=model, effort_used=effort,
                started=now(), finished=None, pid=proc.pid, base=base, branch=f"task/{tid}", worktree=str(wt),
                error="", timed_out=False, cancel=False)
    desk.event("task_started", task=tid, lane=lane_name, provider=pname, model=model)
    return proc


def usage_of(run: dict) -> dict:
    out = {}
    for model, u in (run.get("modelUsage") or {}).items():
        out[model] = {"in": u.get("inputTokens", 0), "out": u.get("outputTokens", 0),
                      "cache_read": u.get("cacheReadInputTokens", 0),
                      "cache_write": u.get("cacheCreationInputTokens", 0),
                      "claude_code_usd": u.get("costUSD"), "basis": u.get("costBasis", "")}
    return out


def denial_of(d) -> str:
    if not isinstance(d, dict):
        return str(d)[:160]
    inp = d.get("tool_input") or {}
    what = inp.get("command") or inp.get("file_path") or inp.get("pattern") or ""
    return f"{d.get('tool_name', '?')}: {str(what)[:140]}"


def unusable_reason(run: dict, err: str):
    """Why this provider cannot be used right now: ("quota" | "key" | "", words)."""
    status = run.get("api_error_status")
    if status in QUOTA_STATUS:
        return "quota", f"out of quota (HTTP {status})"
    if status in AUTH_STATUS:
        return "key", f"key rejected (HTTP {status})"
    text = f"{run.get('result') or ''} {err}" if (run.get("is_error") or not run) else ""
    m = QUOTA.search(text)
    if m:
        return "quota", f"out of quota ({m.group(0)})"
    m = AUTH.search(text)
    if m:
        return "key", f"key rejected ({m.group(0)})"
    return "", ""


def finish(desk: Desk, tid: str, returncode) -> dict:
    meta = desk.meta(tid)
    d = desk.tasks_dir / tid
    run = read_json(d / "run.json", {}) or {}
    err = (d / "run.err").read_text(encoding="utf-8", errors="replace")[-3000:] if (d / "run.err").exists() else ""
    result = run.get("result") or ""
    (d / "result.md").write_text(result + ("\n" if result else ""), encoding="utf-8")
    fields = {"finished": now(), "exit": returncode, "pid": None, "session_id": run.get("session_id", ""),
              "turns": run.get("num_turns"), "subtype": run.get("subtype", ""),
              "usage": usage_of(run), "denials": [denial_of(x) for x in run.get("permission_denials") or []]}
    wt = Path(meta["worktree"]) if meta.get("worktree") else None
    try:
        if wt is None:
            raise RuntimeError("the task never got a worktree")
        if git("status", "--porcelain", cwd=wt):
            git("add", "-A", cwd=wt)
            git("commit", "-q", "--no-verify", "-m", f"{tid}: changes left uncommitted at exit", cwd=wt)
        head = git("rev-parse", "HEAD", cwd=wt)
        fields.update(head=head, commits=int(git("rev-list", "--count", f"{meta['base']}..{head}", cwd=wt) or 0),
                      files=git("diff", "--name-only", meta["base"], head, cwd=wt).splitlines(),
                      stat=git("diff", "--shortstat", meta["base"], head, cwd=wt))
    except (RuntimeError, KeyError, OSError) as e:
        fields.update(head="", commits=0, files=[], stat="", git_error=str(e)[:300])

    attempts = list(meta.get("attempts", []))
    kind, why = ("", "") if meta.get("cancel") else unusable_reason(run, err)
    if meta.get("cancel"):
        state, error = "cancelled", "cancelled"
    elif meta.get("timed_out"):
        state, error = "failed", "stopped: ran past the lane's time limit"
    elif kind:
        minutes = desk.settings().get("cool_minutes", 30) if kind == "quota" else KEY_REST_MINUTES
        desk.set_provider_state(meta["provider"], cool_until=time.time() + 60 * minutes, reason=why)
        attempts.append({"provider": meta["provider"], "at": now(), "error": why})
        state = "queued" if len(attempts) < 3 else "failed"
        error = f"{meta['provider']}: {why}; " + (
            "queued again for the lane's next provider" if state == "queued" else "gave up after 3 tries")
    elif run.get("is_error") or run.get("subtype") not in ("success", None) or not run:
        state = "failed"
        lines = (result or err).strip().splitlines()
        error = f"{run.get('subtype') or 'error'}: {lines[-1][:200] if lines else f'no output (exit {returncode})'}"
    else:
        state, error = "done", ""
    meta = desk.update(tid, state=state, error=error, attempts=attempts, **fields)
    desk.event("task_finished", task=tid, state=state, provider=meta.get("provider"), error=error)
    return meta


def alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def stop(pid) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except OSError:
            return
        for _ in range(20):
            if not alive(pid):
                return
            time.sleep(0.5)


class Dispatcher:
    """Starts queued tasks while lanes are free, and finishes the ones that ended."""

    def __init__(self, desk: Desk) -> None:
        self.desk = desk
        self.procs = {}

    def tick(self) -> None:
        desk = self.desk
        s = desk.settings()
        desk.state_dir.mkdir(exist_ok=True)
        (desk.state_dir / "heartbeat").write_text(str(now()))
        for tid, proc in list(self.procs.items()):
            if proc.poll() is not None:
                del self.procs[tid]
                finish(desk, tid, proc.returncode)
        for m in desk.tasks():
            if m["state"] != "running" or m["id"] in self.procs:
                continue
            if not alive(m.get("pid")):  # started by an earlier server
                finish(desk, m["id"], None)
        for m in desk.tasks():
            if m["state"] != "running":
                continue
            limit = 60 * s["lanes"].get(m.get("lane"), {}).get("timeout_min", 60)
            if m.get("cancel") or time.time() - m.get("started", time.time()) > limit:
                if not m.get("cancel"):
                    desk.update(m["id"], timed_out=True)
                stop(m.get("pid"))
        if desk.paused():
            return
        running = [m for m in desk.tasks() if m["state"] == "running"]
        free = s["concurrency"] - len(running)
        busy = {m["lane"] for m in running}
        for m in desk.tasks():
            if free <= 0:
                break
            if m["state"] != "queued":
                continue
            for ln in ([m["lane"]] if m.get("lane") else sorted(s["lanes"])):
                if ln in busy or ln not in s["lanes"]:
                    continue
                pname = next((p for p in s["lanes"][ln]["providers"] if not provider_problem(desk, s, p)), None)
                if not pname:
                    continue
                try:
                    self.procs[m["id"]] = start(desk, m["id"], ln, pname)
                except Exception as e:  # noqa: BLE001 - any failure to start is recorded on the task
                    desk.update(m["id"], state="failed", error=f"could not start: {e}"[:300])
                    desk.event("task_not_started", task=m["id"], error=str(e)[:300])
                else:
                    busy.add(ln)
                    free -= 1
                break


def waiting_reason(desk: Desk, s: dict, m: dict, busy: set) -> str:
    """Why a queued task is not running yet, in one line."""
    if desk.paused():
        return "dispatch is paused"
    lanes = [m["lane"]] if m.get("lane") else sorted(s["lanes"])
    if not lanes:
        return "no lanes in settings"
    why = []
    for ln in lanes:
        if ln not in s["lanes"]:
            why.append(f"lane {ln} is not in settings")
            continue
        if ln in busy:
            why.append(f"lane {ln} is busy")
            continue
        probs = {p: provider_problem(desk, s, p) for p in s["lanes"][ln]["providers"]}
        ready = [p for p, prob in probs.items() if not prob]
        if ready:
            hb = read_json(desk.state_dir / "heartbeat", 0) or 0
            return (f"ready for lane {ln} on {ready[0]}; starts when `bh serve` next looks (within seconds)"
                    if time.time() - hb < 30 else f"ready for lane {ln} on {ready[0]}, but `bh serve` is not running")
        why.append(f"lane {ln} has no provider ready ({'; '.join(f'{p}: {x}' for p, x in probs.items())})")
    return "; ".join(why) if why else "waiting for a free slot"


def check_provider(desk: Desk, name: str, timeout: int = 90) -> dict:
    """Send one tiny request through a provider and record whether it answered."""
    s = desk.settings()
    p = s["providers"].get(name)
    if not p:
        return {"ok": False, "detail": "not in settings"}
    prob = provider_problem(desk, s, name)
    if prob in ("no key", "no endpoint address"):
        return {"ok": False, "detail": prob}
    tmp = desk.state_dir / "check"
    tmp.mkdir(parents=True, exist_ok=True)
    argv = [claude_bin(), "-p", "--output-format", "json", "--max-turns", "1", "--model", p["model"]]
    t0 = time.time()
    try:
        r = subprocess.run(argv, cwd=str(tmp), env=provider_env(desk, name, p), input="Reply with the single word OK.",
                           capture_output=True, text=True, timeout=timeout)
        run = json.loads(r.stdout) if r.stdout.strip().startswith("{") else {}
        ok = bool(run) and not run.get("is_error") and "ok" in (run.get("result") or "").lower()
        detail = ", ".join(run.get("modelUsage") or {}) if ok else \
            (unusable_reason(run, r.stderr)[1] or (run.get("result") or r.stderr or r.stdout or "no answer").strip()[:200])
    except subprocess.TimeoutExpired:
        ok, detail = False, f"no answer in {timeout}s"
    except Exception as e:  # noqa: BLE001
        ok, detail = False, str(e)[:200]
    result = {"ok": ok, "detail": detail, "at": now(), "seconds": round(time.time() - t0, 1)}
    desk.set_provider_state(name, check=result)
    if ok:
        desk.set_provider_state(name, cool_until=0, reason="")
    desk.event("provider_checked", provider=name, ok=ok, detail=detail)
    return result
