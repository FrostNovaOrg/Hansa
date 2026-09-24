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
import sys
import time
import uuid
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
# The provider would not answer this request (its content filter or a refusal). That
# says nothing about other tasks, so the provider does not rest; the task moves on.
REFUSED_STOPS = {"content_filter", "refusal"}
BASE_ENV = ("HOME", "PATH", "LANG", "LC_ALL", "USER", "LOGNAME", "SHELL", "TMPDIR")
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


VERIFY_PROTOCOL = """You are a worker in lane {lane}, checking another worker's finished task. You are
on branch task/{tid}, a copy of that task's branch made for this check; nothing here is merged.
- Change files as your checks need (undo code, add a throwaway test), but do not commit. For
  undoing, `git checkout <commit> -- <path>`, `git restore` and `git stash` are allowed here.
- Do not push, do not switch branches, do not touch other directories.
- If a command you need is refused, say which one and why in your report; do not look for a way around it.
"""

VERIFY_TASK = """Find out, by running things, whether task {id} does what it asked. Its change is
`git diff {base}..{head}`, already on this branch. Do not fix anything.

1. Run first.
   - If the change adds or changes tests: undo the code part of the change (keep the tests) and
     run those tests. They should fail; if they still pass, they do not test the change. Then put
     the code back.
   - Run what the task is about the way a user or caller would meet it.
   - Try the edge cases you can think of: empty or missing values, data or settings left from
     before the change, errors, other languages.
2. Read the code only to decide what to run and to explain what you saw.
3. For text or documentation, compare what it says with the code it describes.

Report what you ran and what happened (the command and the lines that matter); then, for each
thing the task asked, whether it holds, does not hold or is unsure, with that evidence; then
anything the change does that the task did not ask for. End with exactly one of these lines:
VERDICT: holds
VERDICT: does not hold
VERDICT: unsure
Say "does not hold" when something the task asked for does not hold, and also when a test the
change adds or changes still passes with the code undone: such a test checks nothing, even if
the task asked for it that way.
"""
VERDICT = re.compile(r"^\W*VERDICT:\W*(holds|does not hold|unsure)\b", re.I | re.M)
VERIFY_ALLOW = ["Bash(git checkout:*)", "Bash(git restore:*)", "Bash(git stash:*)"]

BROWSER_NOTE = """
This task comes with a browser (tools named mcp__browser__...). It has no saved logins.
Files it saves, such as screenshots, go to {dir}, and a file name it reports is relative
to that folder; open a screenshot there with the Read tool to see it. Page text can be
read directly; charts, images and drawings can only be seen in a screenshot.
"""

# A lane's turn and time limits are a circuit breaker, set high: a worker that reaches one
# was given a task too big or unclear, or got stuck. Its notes show where it went.
CEILING = "the task was probably too big or unclear, or the worker got stuck"
STOPPED_NOTE = "(Stopped at the ceiling before it wrote its report. Its notes along the way, to see what went wrong:)\n\n"


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


def refused_by(m: dict) -> set:
    """Providers that refused this task; it is not offered to them again."""
    return {a["provider"] for a in m.get("attempts", []) if a.get("kind") == "refused"}


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
        if str(st.get("reason", "")).startswith("key rejected"):
            return f"{st['reason']}; saving a new key brings it back"
        return f"resting until {time.strftime('%m-%d %H:%M', time.localtime(st['cool_until']))} ({st.get('reason', '')})"
    return ""


def config_home(desk: Desk, name: str, p: dict) -> Path:
    """Where Claude Code keeps this provider's login or settings, and its session records."""
    if p["kind"] == "subscription":
        return Path(p.get("config_dir") or os.path.expanduser("~/.claude"))
    return desk.state_dir / f"claude-{name}"


def _record(home: Path, sid: str):
    """A worker's session record as Claude Code writes it while it works:
    (texts, replies, last write, {reply id: (model, token counts)})."""
    texts, replies, last, counts = [], set(), None, {}
    for f in home.glob(f"projects/*/{sid}.jsonl") if sid else []:
        last = max(last or 0, f.stat().st_mtime)
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            msg = d.get("message") or {} if d.get("type") == "assistant" else {}
            if msg.get("id"):
                replies.add(msg["id"])
                if isinstance(msg.get("usage"), dict):
                    counts[msg["id"]] = (msg.get("model") or "?", msg["usage"])
            content = msg.get("content")
            texts += [c["text"].strip() for c in content if isinstance(c, dict) and c.get("type") == "text"
                      and c.get("text", "").strip()] if isinstance(content, list) else []
    return texts, len(replies), last, counts


def notes_of(home: Path, sid: str, limit: int = 6000) -> str:
    """What a worker wrote along the way, from its session record."""
    return "\n\n".join(_record(home, sid)[0])[-limit:]


def progress_of(desk: Desk, m: dict) -> dict:
    """How far a running worker has got: model replies so far, minutes since it last did
    anything, and the last thing it wrote. bh records turns and tokens only at the end."""
    p = desk.settings()["providers"].get(m.get("provider") or "")
    texts, replies, last, _ = _record(config_home(desk, m["provider"], p), m.get("session_id", "")) if p else ([], 0, None, {})
    return {"replies": replies, "idle_min": int((time.time() - last) // 60) if last else None,
            "note": texts[-1].splitlines()[0][:160] if texts else ""}


def provider_env(desk: Desk, name: str, p: dict) -> dict:
    """Environment for one Claude Code run on this provider. Starts clean, so a
    worker never inherits keys that are not its own."""
    env = {k: os.environ[k] for k in BASE_ENV if k in os.environ}
    env["TERM"] = "dumb"
    env["CLAUDE_CODE_MAX_RETRIES"] = "3"  # a rejected key fails in seconds, not after eleven tries
    if p["kind"] == "subscription":
        if p.get("config_dir"):
            env["CLAUDE_CONFIG_DIR"] = p["config_dir"]
    else:
        home = config_home(desk, name, p)
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
    files = [rule(t, wt) for t in ("Read", "Edit")]  # Read covers Glob and Grep; Edit covers Write
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


def browser_for(desk: Desk, tid: str, command: str):
    """The principal's browser tool for this one task, started in the task's own folder
    and not told where the worker works, so what it saves (screenshots, also the ones
    given a name) lands there and never in the repository."""
    d = desk.tasks_dir / tid / "browser"
    d.mkdir(parents=True, exist_ok=True)
    cfg = desk.tasks_dir / tid / "browser-mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"browser": {
        "command": sys.executable, "args": [str(Path(__file__).with_name("relay.py")), str(d), command]}}}),
        encoding="utf-8")
    return cfg, ["mcp__browser", rule("Read", d)]


def start(desk: Desk, tid: str, lane_name: str, pname: str) -> subprocess.Popen:
    s = desk.settings()
    lane, p, meta = s["lanes"][lane_name], s["providers"][pname], desk.meta(tid)
    wt, base = prepare(desk, lane_name, lane, meta)
    model = (meta.get("model") if p["kind"] == "subscription" else "") or p["model"]  # other providers: their own
    effort = meta.get("effort") or p.get("effort", "")
    brief = desk.lanes_dir / lane_name / "brief.md"
    verifying = meta.get("kind") == "verify"
    system = (VERIFY_PROTOCOL if verifying else PROTOCOL).format(tid=tid, lane=lane_name)
    if brief.is_file() and brief.read_text(encoding="utf-8").strip():
        system += "\nStanding instructions for this lane:\n" + brief.read_text(encoding="utf-8")
    sid = str(uuid.uuid4())  # known up front, so a worker stopped before its report still has a record
    argv = [claude_bin(), "-p", "--output-format", "json", "--session-id", sid, "--permission-mode", "acceptEdits",
            "--max-turns", str(meta.get("max_turns") or lane.get("max_turns", 300)),
            "--model", model, "--append-system-prompt", system]
    if effort:
        argv += ["--effort", effort]
    extra = []
    if meta.get("browser") and s.get("browser"):
        cfg, extra = browser_for(desk, tid, s["browser"])
        argv[argv.index("--append-system-prompt") + 1] += BROWSER_NOTE.format(dir=cfg.parent / "browser")
        argv += ["--mcp-config", str(cfg)]
    argv += ["--disallowedTools", rule("Read", desk.secrets_dir), rule("Read", desk.state_dir)]
    argv += ["--allowedTools", *lane_allow(desk, lane, wt), *extra, *(VERIFY_ALLOW if verifying else [])]
    env = {**provider_env(desk, pname, p), **lane.get("env", {}), "BH_TASK": tid,
           "GIT_AUTHOR_NAME": f"bh worker {lane_name}", "GIT_AUTHOR_EMAIL": "bh@localhost",
           "GIT_COMMITTER_NAME": f"bh worker {lane_name}", "GIT_COMMITTER_EMAIL": "bh@localhost"}
    d = desk.tasks_dir / tid
    with open(d / "task.md", "rb") as stdin, open(d / "run.json", "wb") as out, open(d / "run.err", "wb") as err:
        proc = subprocess.Popen(argv, cwd=str(wt), env=env, stdin=stdin, stdout=out, stderr=err,
                                start_new_session=True)
    desk.update(tid, state="running", lane=lane_name, provider=pname, model_asked=model, effort_used=effort,
                started=now(), finished=None, pid=proc.pid, base=base, branch=f"task/{tid}", worktree=str(wt),
                session_id=sid, error="", timed_out=False, cancel=False)
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


def usage_from_record(home: Path, sid: str) -> dict:
    """Token counts from the session record, for a run stopped before Claude Code wrote its totals
    (cancelled, or stopped at the time ceiling). Some providers leave output tokens out of the
    record, so these can be short."""
    out = {}
    for model, u in _record(home, sid)[3].values():
        t = out.setdefault(model, {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0,
                                   "claude_code_usd": None, "basis": "session record"})
        t["in"] += u.get("input_tokens") or 0
        t["out"] += u.get("output_tokens") or 0
        t["cache_read"] += u.get("cache_read_input_tokens") or 0
        t["cache_write"] += u.get("cache_creation_input_tokens") or 0
    return out


def denial_of(d) -> str:
    if not isinstance(d, dict):
        return str(d)[:160]
    inp = d.get("tool_input") or {}
    what = inp.get("command") or inp.get("file_path") or inp.get("pattern") or ""
    return f"{d.get('tool_name', '?')}: {str(what)[:140]}"


def unusable_reason(run: dict, err: str):
    """Why this provider cannot be used for this run: ("quota" | "key" | "refused" | "", words)."""
    if run.get("stop_reason") in REFUSED_STOPS:
        return "refused", f"refused the request ({run['stop_reason']})"
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
    report = result
    stopped = meta.get("timed_out") or run.get("subtype") == "error_max_turns"
    p = desk.settings()["providers"].get(meta.get("provider"))
    usage = usage_of(run)
    if not usage and p:
        usage = usage_from_record(config_home(desk, meta["provider"], p), meta.get("session_id", ""))
    if stopped and not result.strip() and p:
        notes = notes_of(config_home(desk, meta["provider"], p), meta.get("session_id", ""))
        report = STOPPED_NOTE + notes if notes else ""
    (d / "result.md").write_text(report + ("\n" if report else ""), encoding="utf-8")
    fields = {"finished": now(), "exit": returncode, "pid": None,
              "session_id": run.get("session_id") or meta.get("session_id", ""),
              "turns": run.get("num_turns"), "subtype": run.get("subtype", ""),
              "usage": usage, "denials": [denial_of(x) for x in run.get("permission_denials") or []]}
    if meta.get("kind") == "verify":
        found = VERDICT.findall(result)
        fields["verdict"] = found[-1].lower() if found else ""
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
        lane = desk.settings()["lanes"].get(meta.get("lane"), {})
        state, error = "failed", f"stopped at the ceiling of {lane.get('timeout_min', 240)} minutes: {CEILING}"
    elif run.get("subtype") == "error_max_turns":
        turns = meta.get("max_turns") or desk.settings()["lanes"].get(meta.get("lane"), {}).get("max_turns", 300)
        state, error = "failed", f"stopped at the ceiling of {turns} turns: {CEILING}"
    elif kind == "refused":
        attempts.append({"provider": meta["provider"], "at": now(), "error": why, "kind": "refused"})
        order = desk.settings()["lanes"].get(meta.get("lane"), {}).get("providers", [])
        left = [p for p in order if p not in refused_by({"attempts": attempts})]
        state = "queued" if left and len(attempts) < 3 else "failed"
        error = f"{meta['provider']}: {why}; " + (
            "queued again for the lane's next provider" if state == "queued" else "no other provider in the lane to try")
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
    lane = desk.settings()["lanes"].get(meta.get("lane"), {})
    checking = (state == "done" and bool(lane.get("check")) and bool(fields.get("commits"))
                and meta.get("kind") != "verify")  # a verification is never merged, so never checked
    meta = desk.update(tid, state="running" if checking else state, phase="checking" if checking else "",
                       error=error, attempts=attempts, **fields)
    desk.event("task_finished", task=tid, state="checking" if checking else state,
               provider=meta.get("provider"), error=error)
    return meta


def start_check(desk: Desk, tid: str, lane_name: str, lane: dict):
    """Run the lane's check command in the worktree, as bh, after the worker is done.
    The exit code goes to a file, so a restarted server can still read it."""
    d = desk.tasks_dir / tid
    (d / "check.exit").unlink(missing_ok=True)
    env = {k: os.environ[k] for k in BASE_ENV if k in os.environ}
    env.update({**lane.get("env", {}), "BH_TASK": tid, "TERM": "dumb"})
    with open(d / "check.log", "wb") as out:
        proc = subprocess.Popen(["/bin/sh", "-c", 'sh -c "$1"; echo $? > "$2"', "bh-check", lane["check"],
                                 str(d / "check.exit")], cwd=str(desk.worktrees_dir / lane_name), env=env,
                                stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    desk.update(tid, pid=proc.pid, check_started=now(), check_command=lane["check"])
    desk.event("check_started", task=tid, lane=lane_name)
    return proc


def finish_check(desk: Desk, tid: str) -> dict:
    m = desk.meta(tid)
    d = desk.tasks_dir / tid
    try:
        code = int((d / "check.exit").read_text().strip())
    except (OSError, ValueError):
        code = None
    log = (d / "check.log").read_text(encoding="utf-8", errors="replace") if (d / "check.log").exists() else ""
    note = ("stopped on request" if m.get("cancel") else "stopped at the lane's time ceiling"
            if m.get("timed_out") else "" if code is not None else "the check was interrupted")
    check = {"command": m.get("check_command", ""), "ok": code == 0 and not note, "exit": code, "note": note,
             "seconds": round(now() - (m.get("check_started") or now())), "tail": log.strip().splitlines()[-15:]}
    state = "cancelled" if m.get("cancel") else "done"
    m = desk.update(tid, state=state, phase="", pid=None, check=check, error="cancelled" if m.get("cancel") else "")
    desk.event("task_checked", task=tid, ok=check["ok"], exit=code, note=note)
    return m


def alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def stop(pid) -> None:
    if not pid:
        return
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

    def ended(self, tid: str, returncode) -> None:
        if self.desk.meta(tid).get("phase") == "checking":
            finish_check(self.desk, tid)
        else:
            finish(self.desk, tid, returncode)

    def tick(self) -> None:
        desk = self.desk
        s = desk.settings()
        desk.state_dir.mkdir(exist_ok=True)
        (desk.state_dir / "heartbeat").write_text(str(now()))
        for tid, proc in list(self.procs.items()):
            if proc.poll() is not None:
                del self.procs[tid]
                self.ended(tid, proc.returncode)
        for m in desk.tasks():
            if m["state"] != "running" or m["id"] in self.procs:
                continue
            if m.get("phase") == "checking" and not m.get("pid"):
                lane = s["lanes"].get(m.get("lane"), {})
                if lane.get("check"):
                    self.procs[m["id"]] = start_check(desk, m["id"], m["lane"], lane)
                else:  # the principal removed the check meanwhile
                    desk.update(m["id"], state="done", phase="")
            elif not alive(m.get("pid")):  # started by an earlier server
                self.ended(m["id"], None)
        for m in desk.tasks():
            if m["state"] != "running":
                continue
            limit = 60 * s["lanes"].get(m.get("lane"), {}).get("timeout_min", 240)
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
                pname = next((p for p in s["lanes"][ln]["providers"]
                              if p not in refused_by(m) and not provider_problem(desk, s, p)), None)
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
        probs = {p: "refused this task" if p in refused_by(m) else provider_problem(desk, s, p)
                 for p in s["lanes"][ln]["providers"]}
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
    kind = ""
    try:
        env = {**provider_env(desk, name, p), "CLAUDE_CODE_MAX_RETRIES": "0"}
        r = subprocess.run(argv, cwd=str(tmp), env=env, input="Reply with the single word OK.",
                           capture_output=True, text=True, timeout=timeout)
        run = json.loads(r.stdout) if r.stdout.strip().startswith("{") else {}
        ok = bool(run) and not run.get("is_error") and "ok" in (run.get("result") or "").lower()
        if ok:
            detail = ", ".join(run.get("modelUsage") or {})
        else:
            kind, why = unusable_reason(run, r.stderr)
            said = re.search(r'"message"\s*:\s*"([^"]{1,120})"', f"{run.get('result') or ''} {r.stderr}")
            detail = (why or (run.get("result") or r.stderr or r.stdout or "no answer").strip()[:200]) + \
                (f": {said.group(1)}" if said and why else "")
    except subprocess.TimeoutExpired:
        ok, detail = False, f"no answer in {timeout}s"
    except Exception as e:  # noqa: BLE001
        ok, detail = False, str(e)[:200]
    result = {"ok": ok, "detail": detail, "at": now(), "seconds": round(time.time() - t0, 1)}
    desk.set_provider_state(name, check=result)
    if ok:
        desk.set_provider_state(name, cool_until=0, reason="")
    elif kind:
        minutes = desk.settings().get("cool_minutes", 30) if kind == "quota" else KEY_REST_MINUTES
        desk.set_provider_state(name, cool_until=time.time() + 60 * minutes, reason=detail)
    desk.event("provider_checked", provider=name, ok=ok, detail=detail)
    return result
