"""Workers: start one Claude Code run per task, record what came back, and
keep the queue moving.

A worker is `claude -p` in the lane's git worktree, on a branch of its own
(task/<id>). The prompt goes in on stdin; the JSON result lands in
.bh/runs/<id>/run.json, and bh publishes it into tasks/<id>/ without keys when the
worker ends. Nothing here needs the worker to know about bh.
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

from .desk import Desk, is_git_rule, now, read_json

# Only the wording providers use for "out of quota", so a task that is itself
# about rate limits does not rest a provider when it fails for another reason.
QUOTA = re.compile(r"API Error: (402|429)|rate_limit_error|usage limit reached|credit balance is too low|"
                   r"insufficient[_ ](quota|balance)|quota exceeded|too many requests|余额不足|额度不足|额度已用", re.I)
QUOTA_STATUS = {402, 429}
AUTH = re.compile(r"authentication_error|invalid (x-)?api[ -]key|API Error: 40[13]\b|permission_error", re.I)
AUTH_STATUS = {401, 403}
KEY_VARS = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")  # where a provider's key reaches Claude Code
SETUP_FAILED = re.compile(r"^bh: the lane's setup command failed \(exit (\d+)\)$", re.M)
# The lane's setup command runs first in the worker's own process, then the worker replaces it:
# bh serve never waits on it, and stopping the task stops it too. It never sees a provider's key.
# What it changes in the repository (a lock file, say) is set aside, so it is not part of the task;
# <log>.done says it got that far (a setup stopped midway leaves its changes uncommitted).
# sh -c SETUP_SCRIPT bh-setup <setup> <log> <error file> <worker command...>
SETUP_SCRIPT = ("env " + " ".join(f"-u {v}" for v in KEY_VARS) + """ /bin/sh -c "$1" </dev/null >"$2" 2>&1 || {
  rc=$?
  [ -z "$(git status --porcelain)" ] || git stash push --include-untracked -q -m "bh: what the lane's failed setup changed" >>"$2" 2>&1
  echo "bh: the lane's setup command failed (exit $rc)" >>"$3"; exit 97; }
if [ -n "$(git status --porcelain)" ]; then
  git stash push --include-untracked -q -m "bh: what the lane's setup changed" >>"$2" 2>&1
  echo "bh: the setup changed files in the repository; they were set aside (git stash), so they are not part of the task" >>"$2"
fi
: > "$2.done"
shift 3; exec "$@"
""")
KEY_REST_MINUTES = 24 * 60  # until the principal changes the key (saving a key clears the rest)
# The provider would not answer this request (its content filter or a refusal). That
# says nothing about other tasks, so the provider does not rest; the task moves on.
REFUSED_STOPS = {"content_filter", "refusal"}
BASE_ENV = ("HOME", "PATH", "LANG", "LC_ALL", "USER", "LOGNAME", "SHELL", "TMPDIR")
GIT_ID = {"GIT_AUTHOR_NAME": "bh", "GIT_AUTHOR_EMAIL": "bh@localhost",
          "GIT_COMMITTER_NAME": "bh", "GIT_COMMITTER_EMAIL": "bh@localhost"}
PROTOCOL = """You are a worker on task {tid} in lane {lane}. You work only in this directory,
on branch task/{tid}, which was made for this task.
- Do not commit: when you end, bh commits everything you changed here. To look at your
  work, git status, git diff, git log and git show are allowed.
- Do not push, do not switch branches, do not touch other directories.
- To search, run read-only commands such as grep -rn, find and sed -n on paths in this
  directory; they are allowed here.
- If a command you need is refused, say which one and why you need it in your report;
  do not look for a way around the refusal.
- End with a short report: what you changed, how you checked it, and anything left undone.
"""


VERIFY_PROTOCOL = """You are a worker in lane {lane}, checking another worker's finished task. You are
on branch task/{tid}, a copy of that task's branch made for this check; nothing here is merged.
- Change files as your checks need (undo code, add a throwaway test), but do not commit; bh sets
  your changes aside when you end. For undoing, `git checkout <commit> -- <path>` and
  `git restore` are allowed here.
- Do not push, do not switch branches, do not touch other directories.
- To search, run read-only commands such as grep -rn, find and sed -n on paths in this directory.
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
VERIFY_ALLOW = ["Bash(git checkout:*)", "Bash(git restore:*)"]

BROWSER_NOTE = """
This task comes with a browser (tools named mcp__browser__...). It has no saved logins.
Files it saves, such as screenshots, go to {dir}, and a file name it reports is relative
to that folder; open a screenshot there with the Read tool to see it. Page text can be
read directly; charts, images and drawings can only be seen in a screenshot.
"""

DESIGN_PROTOCOL = """You are a design session for task {tid} in lane {lane}. The principal, who owns the
product, works with you here directly, in this terminal, to shape one feature by looking at it.
You work only in this directory, on branch task/{tid}, which was made for this session.
- The principal decides how it looks and works; follow their calls and ask when unsure. They
  may not read code: talk in plain words, in the language they write in, and show rather than tell.
- Change the real code; there is no separate prototype. Keep to the part of the code the brief
  names, and ask the principal before going beyond it.
- Show your work: after each change, take screenshots of what changed. The principal sees them
  on their page, newest first; name each file after what it shows.
- Keep the lane's checks passing{check}. Commit each step the principal is happy with, starting
  each commit message with "{tid}:".
- Do not push, do not switch branches, do not touch other directories. If a command you need is
  refused, say which one and why; do not look for a way around it.
- When the principal says it is done: bring in the latest {base} (git merge {base}) and settle any
  conflict here, run the checks, commit, stop the app if you started it, then write your hand-in
  report as one message (what changed, how it was checked, anything left) and ask the principal
  to type /exit. bh then hands the work to the coordinator, who checks it and merges it.
"""
DESIGN_START = """The coordinator's brief for this session:

{brief}
Before the principal joins: read the code the brief names and take screenshots of how it looks
now. Then say in a few lines what you would change first, and wait for the principal.
"""
PREVIEW_NOTE = """
To look at the app, start it with `{preview}` (the Bash tool's background option) and open
the address it prints in the browser.
"""
NO_BROWSER_NOTE = """
No browser is set up on this desk, so you cannot take screenshots: tell the principal, and say
in words what changed and where they can see it.
"""
DESIGN_NOTE = "(A design session: the principal shaped this directly with it. Its last message:)\n\n"
# The principal is in the session, so it commits each step itself and brings in the base.
DESIGN_ALLOW = ["Bash(git add:*)", "Bash(git commit:*)", "Bash(git merge:*)"]

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


def tmux_bin() -> str:
    for c in (os.environ.get("BH_TMUX"), shutil.which("tmux"), "/opt/homebrew/bin/tmux", "/usr/local/bin/tmux"):
        if c and os.access(c, os.X_OK):
            return c
    raise RuntimeError("tmux was not found; a design session runs in tmux so the principal can join it")


def tmux_socket(desk: Desk) -> str:
    """One tmux server per desk, so session names never meet another desk's."""
    import hashlib
    return "bh-" + hashlib.sha1(str(desk.root).encode()).hexdigest()[:6]


def join_command(m: dict) -> str:
    sock, name = m.get("tmux") or ["", ""]
    return f"tmux -u -L {sock} attach -t {name}"


def git(*args, cwd, env=None) -> str:
    e = {**os.environ, **GIT_ID, **(env or {})}
    r = subprocess.run(["git", *args], cwd=str(cwd), env=e, capture_output=True, text=True, errors="replace")
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
            "note": desk.redact(texts[-1].splitlines()[0][:160]) if texts else ""}


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
    """The worktree's files plus the lane's own commands. Git rules a lane still lists from before
    are left out: bh does the committing, and read-only git needs no rule."""
    files = [rule(t, wt) for t in ("Read", "Edit")]  # Edit covers Write
    return files + ["TodoWrite"] + [r for r in lane.get("allow", []) if not is_git_rule(r)]


def command_rules(command: str) -> list:
    """One allow rule per command in a shell line such as `pnpm test && pnpm typecheck`; quoted
    text and pipelines stay whole."""
    parts, cur, quote, i = [], "", "", 0
    while i < len(command):
        c = command[i]
        if quote:
            cur += c
            if c == quote:
                quote = ""
            elif c == "\\" and quote == '"' and i + 1 < len(command):
                cur, i = cur + command[i + 1], i + 1
        elif c in "'\"":
            cur, quote = cur + c, c
        elif c == "\\" and i + 1 < len(command):
            cur, i = cur + c + command[i + 1], i + 1
        elif command.startswith(("&&", "||"), i):
            parts, cur, i = parts + [cur], "", i + 1
        elif c in ";\n":  # a pipeline stays one rule: its stages alone (| sh, say) would allow far more
            parts, cur = parts + [cur], ""
        else:
            cur += c
        i += 1
    return [f"Bash({p.strip()}:*)" for p in parts + [cur] if p.strip()]


def keys_in_change(desk: Desk, cwd, base: str, head: str) -> list:
    """Providers whose stored key is in a change: in its files, binary ones too, or its commit messages."""
    r = subprocess.run(["git", "log", "-p", "-m", "--text", "--format=%B", f"{base}..{head}"], cwd=str(cwd),
                       capture_output=True)  # -m: what a merge commit changes itself counts too
    return desk.keys_in(r.stdout.decode("utf-8", "replace"))


def publish(desk: Desk, tid: str, *names: str) -> None:
    """Move what a task's processes wrote from .bh/runs/<id>/ into its folder, keys removed."""
    raw, d = desk.state_dir / "runs" / tid, desk.tasks_dir / tid
    with desk.lock():  # a key saved meanwhile is either known here or scrubbed from it after (Desk.scrub)
        for name in names:
            src = raw / name if (raw / name).exists() else d / name  # in d: started before bh kept them apart
            if src.exists():
                (d / name).write_text(desk.redact(src.read_text(encoding="utf-8", errors="replace")),
                                      encoding="utf-8")
                if src.parent == raw:
                    src.unlink()
        try:
            raw.rmdir()
        except OSError:
            pass


def pids_with_arg(token: str) -> list:
    """Processes whose command line holds this word: bh puts one on each process it starts, so a
    restarted server can find one whose number it never recorded."""
    if not token:
        return []
    found = []
    if Path("/proc").is_dir():
        for p in Path("/proc").iterdir():
            try:
                if p.name.isdigit() and token.encode() in (p / "cmdline").read_bytes().split(b"\0"):
                    found.append(int(p.name))
            except OSError:
                pass
    else:
        r = subprocess.run(["ps", "-axww", "-o", "pid=,args="], capture_output=True, text=True, errors="replace",
                           timeout=30)
        found = [int(x.split(None, 1)[0]) for x in r.stdout.splitlines() if token in x.split()[1:]]
    return [p for p in found if p != os.getpid()]


def leader(pids: list):
    """The one of these that leads its own process group, as each process bh starts does."""
    for p in pids:
        try:
            if os.getpgid(p) == p:
                return p
        except OSError:
            pass
    return None


def prepare(desk: Desk, lane_name: str, lane: dict, meta: dict, wt: Path | None = None):
    """Put the lane's worktree (or the one given) on a fresh task branch. Returns (worktree, base sha)."""
    repo = Path(lane["repo"])
    tid = meta["id"]
    base = meta.get("base") or git("rev-parse", lane.get("base") or "main", cwd=repo)
    start = meta.get("start_from") or base
    wt = wt or desk.worktrees_dir / lane_name
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


def with_setup(argv: list, lane: dict, raw: Path) -> list:
    """The worker command, preceded by the lane's setup command when it has one (see SETUP_SCRIPT)."""
    if not lane.get("setup"):
        return argv
    (raw / "setup.log.done").unlink(missing_ok=True)  # an earlier try's mark says nothing about this one
    return ["/bin/sh", "-c", SETUP_SCRIPT, "bh-setup", lane["setup"], str(raw / "setup.log"), str(raw / "run.err"),
            *argv]


def cancelled_before_start(desk: Desk, tid: str) -> bool:
    """A task cancelled while bh prepared it ends there, and its worker never starts."""
    m = desk.change(tid, lambda x: {"state": "cancelled", "phase": "", "error": "cancelled before it started",
                                    "finished": now()} if x.get("cancel") else None)
    if m:
        desk.event("task_finished", task=tid, state="cancelled", provider=m.get("provider"), error=m["error"])
    return bool(m)


def not_started(desk: Desk, tid: str, e: Exception) -> None:
    """Record why a task could not start; one cancelled meanwhile stays cancelled."""
    why = desk.redact(f"could not start: {e}")[:300]
    desk.change(tid, lambda m: None if m["state"] not in ("queued", "running") else
                {"state": "cancelled", "phase": "", "error": "cancelled before it started", "finished": now()}
                if m.get("cancel") else {"state": "failed", "phase": "", "error": why, "finished": now()})
    desk.event("task_not_started", task=tid, error=why)


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


def start(desk: Desk, tid: str, lane_name: str, pname: str):
    """Claim a queued task and start its worker. None when the task was no longer queued, or was
    cancelled while bh prepared it."""
    meta = desk.transition(tid, ("queued",), state="running", phase="starting", lane=lane_name, provider=pname,
                           started=now(), finished=None, pid=None, pid_start="", error="", timed_out=False,
                           cancel=False)
    if not meta:
        return None
    s = desk.settings()
    lane, p = s["lanes"][lane_name], s["providers"][pname]
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
    d, raw = desk.tasks_dir / tid, desk.raw_dir(tid)
    with desk.lock():  # a cancel lands before this (the worker never starts) or after it (it is stopped)
        if cancelled_before_start(desk, tid):
            return None
        # the session id is on the worker's command line: a server that stops right after starting it
        # can still find it when it comes back
        desk.update(tid, launch=sid, session_id=sid, base=base, branch=f"task/{tid}", worktree=str(wt),
                    setup_ran=bool(lane.get("setup")))
        with open(d / "task.md", "rb") as stdin, open(raw / "run.json", "wb") as out, \
                open(raw / "run.err", "wb") as err:
            proc = subprocess.Popen(with_setup(argv, lane, raw), cwd=str(wt), env=env, stdin=stdin, stdout=out,
                                    stderr=err, start_new_session=True)
        desk.update(tid, phase="", model_asked=model, effort_used=effort, pid=proc.pid,
                    pid_start=proc_start(proc.pid))
    desk.event("task_started", task=tid, lane=lane_name, provider=pname, model=model)
    return proc


def design_command(desk: Desk, tid: str):
    """The interactive Claude Code run for a design session: (argv, env, directory).
    Built inside the session's own terminal (bh.session), so no key is on a command line."""
    s, meta = desk.settings(), desk.meta(tid)
    lane_name = meta["lane"]
    lane, pname = s["lanes"][lane_name], meta["provider"]
    p, wt = s["providers"][pname], Path(meta["worktree"])
    system = DESIGN_PROTOCOL.format(tid=tid, lane=lane_name, base=lane.get("base") or "main",
                                    check=f" ({lane['check']})" if lane.get("check") else "")
    brief = desk.lanes_dir / lane_name / "brief.md"
    if brief.is_file() and brief.read_text(encoding="utf-8").strip():
        system += "\nStanding instructions for this lane:\n" + brief.read_text(encoding="utf-8")
    start = DESIGN_START.format(brief=(desk.tasks_dir / tid / "task.md").read_text(encoding="utf-8"))
    argv = [claude_bin(), start, "--session-id", meta["session_id"], "--model", meta["model_asked"],
            "--permission-mode", "dontAsk"]
    if meta.get("effort_used"):
        argv += ["--effort", meta["effort_used"]]
    extra = []
    if s.get("browser"):
        cfg, extra = browser_for(desk, tid, s["browser"])
        system += BROWSER_NOTE.format(dir=cfg.parent / "browser").replace("This task comes", "This session comes")
        argv += ["--mcp-config", str(cfg)]
    else:
        system += NO_BROWSER_NOTE
    if lane.get("preview"):
        system += PREVIEW_NOTE.format(preview=lane["preview"])
        extra.append(f"Bash({lane['preview']}:*)")
    if lane.get("check"):  # it is told to keep the checks passing, so it may run them
        extra += command_rules(lane["check"])
    argv += ["--append-system-prompt", system,
             "--disallowedTools", rule("Read", desk.secrets_dir), rule("Read", desk.state_dir),
             "--allowedTools", *lane_allow(desk, lane, wt), *extra, *DESIGN_ALLOW]
    env = {**provider_env(desk, pname, p), **lane.get("env", {}), "BH_TASK": tid,
           "TERM": os.environ.get("TERM") or "xterm-256color",  # a terminal the principal types into
           "GIT_AUTHOR_NAME": f"bh design {lane_name}", "GIT_AUTHOR_EMAIL": "bh@localhost",
           "GIT_COMMITTER_NAME": f"bh design {lane_name}", "GIT_COMMITTER_EMAIL": "bh@localhost"}
    return with_setup(argv, lane, desk.raw_dir(tid)), env, wt


def start_design(desk: Desk, tid: str, lane_name: str) -> None:
    """Open a design session: an interactive Claude Code run in tmux, in the lane's design
    worktree, which the principal joins. It ends when they leave it with /exit."""
    s = desk.settings()
    lane = s["lanes"][lane_name]
    ds, co = s.get("design") or {}, s["coordinator"]
    pname = ds.get("provider") or co["provider"]
    p = s["providers"][pname]
    prob = provider_problem(desk, s, pname)
    if prob:
        raise RuntimeError(f"the design provider {pname} cannot be used now: {prob}")
    tmux = tmux_bin()
    if p["kind"] == "subscription":
        model = ds.get("model") or (co.get("model") if pname == co["provider"] else "") or p["model"]
    else:
        model = p["model"]
    sock, name = tmux_socket(desk), f"design-{tid}"
    meta = desk.transition(
        tid, ("queued",), state="running", phase="starting", lane=lane_name, provider=pname, model_asked=model,
        effort_used=ds.get("effort") or (co.get("effort", "") if pname == co["provider"] else "") or p.get("effort", ""),
        started=now(), finished=None, pid=None, pid_start="", error="", timed_out=False, cancel=False,
        tmux=[sock, name], setup_ran=bool(lane.get("setup")))
    if not meta:
        return
    wt, base = prepare(desk, lane_name, lane, meta, wt=desk.worktrees_dir / f"{lane_name}-design")
    desk.update(tid, base=base, branch=f"task/{tid}", worktree=str(wt), session_id=str(uuid.uuid4()))
    if cancelled_before_start(desk, tid):
        return
    env = {k: v for k, v in os.environ.items() if k != "TMUX"}
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
    subprocess.run([tmux, "-L", sock, "new-session", "-d", "-s", name, "-x", "200", "-y", "50", "-c", str(wt),
                    sys.executable, "-m", "bh.session", str(desk.root), tid],
                   env=env, check=True, capture_output=True, timeout=30)
    r = subprocess.run([tmux, "-L", sock, "display-message", "-p", "-t", name, "#{pane_pid}"],
                       capture_output=True, text=True, timeout=30)
    pid = int(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip().isdigit() else None  # None: over already
    meta = desk.update(tid, phase="", pid=pid, pid_start=proc_start(pid))
    ask = desk.add_ask(f"Design session {tid} is waiting for you: {meta['title']}. Join it on this machine with "
                       f"`{join_command(meta)}`; leave it running with Ctrl-b then d, end it with /exit (if it "
                       f"then asks about background work, press Enter: the first choice stops it). The first "
                       f"session in a new folder asks whether you trust it: choose \"Yes, I trust this folder\" "
                       f"(the default, No, ends the session). "
                       f"Its screenshots show under {tid} on this page.", by="bh")
    desk.update(tid, ask=ask)
    desk.event("design_started", task=tid, lane=lane_name, provider=pname, model=model)


def end_design(desk: Desk, meta: dict) -> None:
    """Close what a design session left open: its tmux session and its request to the principal."""
    sock, name = meta.get("tmux") or ["", ""]
    if name:
        try:
            subprocess.run([tmux_bin(), "-L", sock, "kill-session", "-t", name], capture_output=True, timeout=30)
        except (RuntimeError, OSError, subprocess.SubprocessError):
            pass
    if meta.get("ask") and any(a["id"] == meta["ask"] and not a["closed"] for a in desk.asks()):
        desk.close_ask(meta["ask"], "the design session ended", by="bh")


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
    done = desk.state_dir / "runs" / tid / "setup.log.done"
    set_up = not meta.get("setup_ran") or done.exists()  # else the worker never started: nothing is its work
    done.unlink(missing_ok=True)
    publish(desk, tid, "run.json", "run.err", "setup.log")
    run = read_json(d / "run.json", {}) or {}
    err = (d / "run.err").read_text(encoding="utf-8", errors="replace")[-3000:] if (d / "run.err").exists() else ""
    setup = SETUP_FAILED.search(err)  # then the worker never started: nothing here is the provider's doing
    result = run.get("result") or ""
    report = result
    stopped = meta.get("timed_out") or run.get("subtype") == "error_max_turns"
    p = desk.settings()["providers"].get(meta.get("provider"))
    usage = usage_of(run)
    if not usage and p:
        usage = usage_from_record(config_home(desk, meta["provider"], p), meta.get("session_id", ""))
    designing = meta.get("kind") == "design"
    if stopped and not result.strip() and p:
        notes = notes_of(config_home(desk, meta["provider"], p), meta.get("session_id", ""))
        report = STOPPED_NOTE + notes if notes else ""
    if designing:
        texts, replies, _, _ = _record(config_home(desk, meta["provider"], p), meta.get("session_id", "")) if p \
            else ([], 0, None, {})
        report = DESIGN_NOTE + texts[-1] if texts else ""
        run = {"num_turns": replies}
        end_design(desk, meta)
    report = desk.redact(report)
    (d / "result.md").write_text(report + ("\n" if report else ""), encoding="utf-8")
    fields = {"finished": now(), "exit": returncode, "pid": None,
              "session_id": run.get("session_id") or meta.get("session_id", ""),
              "turns": run.get("num_turns"), "subtype": run.get("subtype", ""),
              "usage": usage, "denials": [desk.redact(denial_of(x)) for x in run.get("permission_denials") or []]}
    if meta.get("kind") == "verify":
        found = VERDICT.findall(result)
        fields["verdict"] = found[-1].lower() if found else ""
    wt = Path(meta["worktree"]) if meta.get("worktree") else None
    try:
        if wt is None:
            raise RuntimeError("the task never got a worktree")
        if git("status", "--porcelain", cwd=wt) and set_up and not setup:  # a setup's leftovers are nobody's work
            if meta.get("kind") == "verify":  # its scratch changes are evidence, not work: kept aside
                git("stash", "push", "--include-untracked", "-m", f"bh: {tid} scratch changes", cwd=wt)
            else:
                git("add", "-A", cwd=wt)
                git("commit", "-q", "--no-verify", "-m", f"{tid}: {meta.get('title') or 'work'}"[:100], cwd=wt)
        head = git("rev-parse", "HEAD", cwd=wt)
        fields.update(head=head, commits=int(git("rev-list", "--count", f"{meta['base']}..{head}", cwd=wt) or 0),
                      files=git("diff", "--name-only", meta["base"], head, cwd=wt).splitlines(),
                      stat=git("diff", "--shortstat", meta["base"], head, cwd=wt))
        fields["leaked"] = keys_in_change(desk, wt, meta["base"], head)  # it would travel with the merge
        if fields["leaked"]:
            desk.event("key_in_change", task=tid, providers=fields["leaked"])
    except (RuntimeError, KeyError, OSError) as e:
        fields.update(head="", commits=0, files=[], stat="", git_error=str(e)[:300])

    attempts = list(meta.get("attempts", []))
    if setup:
        fields["setup_exit"] = int(setup.group(1))
    kind, why = ("", "") if meta.get("cancel") or setup else unusable_reason(run, err)
    if meta.get("cancel"):
        state, error = "cancelled", "cancelled"
    elif setup:
        state, error = "failed", (f"the lane's setup command failed (exit {setup.group(1)}), so the worker never "
                                  f"started; bh show {tid} has the setup's last lines")
    elif designing:
        lines = err.strip().splitlines()
        state, error = ("done", "") if report or not lines else ("failed", f"the design session did not start: {lines[-1][:200]}")
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

    def decide(now_meta):
        final, message = ("cancelled", "cancelled") if now_meta.get("cancel") else (state, error)
        checking = (final == "done" and bool(lane.get("check")) and bool(fields.get("commits"))
                    and meta.get("kind") != "verify")  # a verification is never merged, so never checked
        return {"state": "running" if checking else final, "phase": "checking" if checking else "",
                "error": desk.redact(message), "attempts": attempts, "pid_start": "", **fields}
    meta = desk.change(tid, decide)
    desk.event("task_finished", task=tid, state="checking" if meta.get("phase") == "checking" else meta["state"],
               provider=meta.get("provider"), error=meta["error"])
    return meta


def start_check(desk: Desk, tid: str, lane_name: str, lane: dict):
    """Run the lane's check command in the worktree, as bh, after the worker is done; None when
    the task was cancelled first. The exit code goes to a file, so a restarted server can still
    read it."""
    raw = desk.raw_dir(tid)
    (raw / "check.exit").unlink(missing_ok=True)
    env = {k: os.environ[k] for k in BASE_ENV if k in os.environ}
    env.update({**lane.get("env", {}), "BH_TASK": tid, "TERM": "dumb"})
    token = f"bh-check-{tid}-{uuid.uuid4().hex[:8]}"  # on its command line, for a restarted server
    with desk.lock():  # a cancel lands before this (no check) or after it (the check is stopped)
        m = desk.change(tid, lambda x: None if x.get("cancel") else
                        {"launch": token, "check_started": now(), "check_command": lane["check"]})
        if m is None:
            return None
        with open(raw / "check.log", "wb") as out:
            proc = subprocess.Popen(["/bin/sh", "-c", 'sh -c "$1"; echo $? > "$2"', token, lane["check"],
                                     str(raw / "check.exit")], cwd=m.get("worktree") or str(desk.worktrees_dir / lane_name),
                                    env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                    start_new_session=True)
        desk.update(tid, pid=proc.pid, pid_start=proc_start(proc.pid))
    desk.event("check_started", task=tid, lane=lane_name)
    return proc


def finish_check(desk: Desk, tid: str) -> dict:
    m = desk.meta(tid)
    raw, d = desk.state_dir / "runs" / tid, desk.tasks_dir / tid
    done = raw / "check.exit" if (raw / "check.exit").exists() else d / "check.exit"
    try:
        code = int(done.read_text().strip())
    except (OSError, ValueError):
        code = None
    done.unlink(missing_ok=True)
    publish(desk, tid, "check.log")  # what the check printed can hold a key the change holds
    log = (d / "check.log").read_text(encoding="utf-8", errors="replace") if (d / "check.log").exists() else ""
    note = ("stopped on request" if m.get("cancel") else "stopped at the lane's time ceiling"
            if m.get("timed_out") else "" if code is not None else "the check was interrupted")
    check = {"command": m.get("check_command", ""), "ok": code == 0 and not note, "exit": code, "note": note,
             "seconds": round(now() - (m.get("check_started") or now())), "tail": log.strip().splitlines()[-15:]}
    m = desk.change(tid, lambda now_meta: {"state": "cancelled" if now_meta.get("cancel") else "done", "phase": "",
                                           "pid": None, "pid_start": "", "check": check,
                                           "error": "cancelled" if now_meta.get("cancel") else ""})
    desk.event("task_checked", task=tid, ok=check["ok"], exit=code, note=note)
    return m


def proc_start(pid) -> str:
    """When a process started, to tell it from a later one given the same number; '' if unknown."""
    if not pid:
        return ""
    try:
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return boot + ":" + Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        pass
    try:
        return subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                              timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def alive(pid, start: str = "") -> bool:
    """Whether this process is still running, and, when its start is known, still the same one."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    seen = proc_start(pid) if start else ""
    return not seen or seen == start  # when its start cannot be read now, assume it is the same process


def _state(pid) -> str:
    """A process's state letter (Z: ended, waiting to be reaped); '' if unknown."""
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        pass
    try:
        return subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True,
                              timeout=10).stdout.strip()[:1]
    except (OSError, subprocess.SubprocessError):
        return ""


def has_ended(proc) -> bool:
    """Whether bh's child has ended, found without reaping it: until it is reaped, its number,
    and so its process group's, cannot pass to another process."""
    if proc.returncode is not None:
        return True
    try:
        os.kill(proc.pid, 0)
    except OSError:  # gone altogether
        proc.poll()
        return True
    return _state(proc.pid) == "Z"


def group_running(pgid) -> bool:
    """Whether anything in this process group still runs (one that only waits to be reaped does not)."""
    if Path("/proc").is_dir():
        for p in Path("/proc").iterdir():
            try:
                if p.name.isdigit():
                    f = (p / "stat").read_text().rsplit(")", 1)[1].split()
                    if f[0] != "Z" and int(f[2]) == pgid:
                        return True
            except (OSError, IndexError, ValueError):
                pass
        return False
    r = subprocess.run(["ps", "-axo", "pgid=,stat="], capture_output=True, text=True, timeout=30)
    return any(x.split()[0] == str(pgid) and not x.split()[1].startswith("Z")
               for x in r.stdout.splitlines() if len(x.split()) >= 2)


def stop(pid, start: str = "", proc=None) -> None:
    """Stop the process group bh started, politely and then for good, until nothing in it runs,
    also when its leader has ended already. proc: the Popen, when this process started the
    leader; it is reaped only at the end, so the group's number cannot pass to another process
    meanwhile. Without it, a number that now belongs to another process is left alone."""
    if not pid or (proc is not None and proc.returncode is not None) or (proc is None and not alive(pid, start)):
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except OSError:
            break
        end = time.time() + 10
        while group_running(pid) and time.time() < end:
            time.sleep(0.5)
        if not group_running(pid):
            break
    if proc is not None:
        proc.poll()


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

    def recover(self, m: dict) -> None:
        """A task an earlier server claimed but did not record as started: a worker it did start
        is taken over; otherwise the task goes back to the queue, or ends there if cancelled."""
        found = leader(pids_with_arg(m.get("launch", ""))) if m.get("kind") != "design" else None
        if found:
            self.desk.update(m["id"], phase="", pid=found, pid_start=proc_start(found))
            return
        if m.get("tmux"):  # a design session it may have opened, which nobody was asked to join yet
            try:
                subprocess.run([tmux_bin(), "-L", m["tmux"][0], "kill-session", "-t", m["tmux"][1]],
                               capture_output=True, timeout=30)
            except (OSError, RuntimeError, subprocess.SubprocessError):
                pass
        self.desk.change(m["id"], lambda x: None if x["state"] != "running" or x.get("phase") != "starting" else
                         {"state": "cancelled", "phase": "", "error": "cancelled before it started",
                          "finished": now()} if x.get("cancel") else
                         {"state": "queued", "phase": "", "pid": None, "pid_start": "", "tmux": None})

    def check(self, m: dict, s: dict) -> None:
        """Run the lane's check on a task whose worker is done: or take over the one an earlier
        server started, or finish the one it left finished."""
        desk, tid = self.desk, m["id"]
        found = leader(pids_with_arg(m.get("launch", "")))
        if found:
            desk.update(tid, pid=found, pid_start=proc_start(found))
            return
        if (desk.state_dir / "runs" / tid / "check.exit").exists():
            finish_check(desk, tid)
            return
        lane = s["lanes"].get(m.get("lane"), {})
        proc = start_check(desk, tid, m["lane"], lane) if lane.get("check") else None
        if proc:
            self.procs[tid] = proc
            return
        publish(desk, tid, "check.log")  # cancelled first, or the principal removed the check meanwhile
        desk.change(tid, lambda x: {"state": "cancelled", "phase": "", "error": "cancelled", "finished": now()}
                    if x.get("cancel") else {"state": "done", "phase": ""})

    def tick(self) -> None:
        desk = self.desk
        s = desk.settings()
        desk.state_dir.mkdir(exist_ok=True)
        (desk.state_dir / "heartbeat").write_text(str(now()))
        for tid, proc in list(self.procs.items()):
            if has_ended(proc):
                del self.procs[tid]
                stop(proc.pid, "", proc)  # what it left running in its group (a server, say) ends with it
                self.ended(tid, proc.returncode)
        for m in desk.tasks():
            if m["state"] != "running" or m["id"] in self.procs:
                continue
            if m.get("phase") == "starting":  # an earlier server stopped while it prepared this task
                self.recover(m)
            elif m.get("phase") == "checking" and not m.get("pid"):
                self.check(m, s)
            elif not alive(m.get("pid"), m.get("pid_start", "")):  # started by an earlier server
                self.ended(m["id"], None)
        for m in desk.tasks():
            if m["state"] != "running":
                continue
            limit = 60 * s["lanes"].get(m.get("lane"), {}).get("timeout_min", 240)
            over = time.time() - m.get("started", time.time()) > limit and m.get("kind") != "design"
            if m.get("cancel") or over:  # a design session waits on the principal, so it has no time ceiling
                if not m.get("cancel"):
                    desk.update(m["id"], timed_out=True)
                stop(m.get("pid"), m.get("pid_start", ""), self.procs.get(m["id"]))
        if desk.paused():
            return
        designing = {m["lane"] for m in desk.tasks() if m["state"] == "running" and m.get("kind") == "design"}
        for m in desk.tasks():  # a design session has its own worktree and waits on the principal
            if m["state"] == "queued" and m.get("kind") == "design" and m.get("lane") in s["lanes"] \
                    and m["lane"] not in designing:
                try:
                    start_design(desk, m["id"], m["lane"])
                except Exception as e:  # noqa: BLE001 - any failure to start is recorded on the task
                    not_started(desk, m["id"], e)
                else:
                    designing.add(m["lane"])
        running = [m for m in desk.tasks() if m["state"] == "running" and m.get("kind") != "design"]
        free = s["concurrency"] - len(running)
        busy = {m["lane"] for m in running}
        for m in desk.tasks():
            if free <= 0:
                break
            if m["state"] != "queued" or m.get("kind") == "design":
                continue
            for ln in ([m["lane"]] if m.get("lane") else sorted(s["lanes"])):
                if ln in busy or ln not in s["lanes"]:
                    continue
                pname = next((p for p in s["lanes"][ln]["providers"]
                              if p not in refused_by(m) and not provider_problem(desk, s, p)), None)
                if not pname:
                    continue
                try:
                    proc = start(desk, m["id"], ln, pname)
                except Exception as e:  # noqa: BLE001 - any failure to start is recorded on the task
                    not_started(desk, m["id"], e)
                else:
                    if proc:
                        self.procs[m["id"]] = proc
                        busy.add(ln)
                        free -= 1
                break


def server_up(desk: Desk) -> bool:
    """Whether bh serve has looked at the desk in the last half minute."""
    return time.time() - (read_json(desk.state_dir / "heartbeat", 0) or 0) < 30


def waiting_reason(desk: Desk, s: dict, m: dict, busy: set) -> str:
    """Why a queued task is not running yet, in one line."""
    if m.get("kind") == "design":
        other = [x["id"] for x in desk.tasks() if x.get("kind") == "design" and x["state"] == "running"
                 and x.get("lane") == m.get("lane")]
        return (f"lane {m.get('lane')} already has design session {other[0]} open" if other
                else f"lane {m.get('lane')} is not in settings" if m.get("lane") not in s["lanes"]
                else "opens when `bh serve` next looks (within seconds)" if server_up(desk)
                else "opens once `bh serve` runs; `bh serve` is not running")
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
            return (f"ready for lane {ln} on {ready[0]}; starts when `bh serve` next looks (within seconds)"
                    if server_up(desk) else f"ready for lane {ln} on {ready[0]}, but `bh serve` is not running")
        why.append(f"lane {ln} has no provider ready ({'; '.join(f'{p}: {x}' for p, x in probs.items())})")
    return "; ".join(why) if why else "waiting for a free slot"


def key_stamp(desk: Desk, name: str) -> tuple:
    """Which key file is there now (saving a key always makes a new file); () if none."""
    try:
        st = desk.key_file(name).stat()
    except (OSError, ValueError):
        return ()
    return st.st_ino, st.st_mtime_ns


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
    stamp = (key_stamp(desk, name), json.dumps(p, sort_keys=True))
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
    detail = desk.redact(detail)
    result = {"ok": ok, "detail": detail, "at": now(), "seconds": round(time.time() - t0, 1)}
    with desk.lock():  # keys and settings are saved under this lock too, so nothing lands in between
        s = desk.settings()
        if (key_stamp(desk, name), json.dumps(s["providers"].get(name), sort_keys=True)) != stamp:
            return {**result, "ok": False, "detail": "the provider's key or settings changed while this check "
                                                     "ran, so it says nothing about them now; check again"}
        desk.set_provider_state(name, check=result)
        if ok:
            desk.set_provider_state(name, cool_until=0, reason="")
        elif kind:
            minutes = s.get("cool_minutes", 30) if kind == "quota" else KEY_REST_MINUTES
            desk.set_provider_state(name, cool_until=time.time() + 60 * minutes, reason=detail)
    desk.event("provider_checked", provider=name, ok=ok, detail=detail)
    return result
