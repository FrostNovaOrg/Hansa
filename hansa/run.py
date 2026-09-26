"""Workers: start one Claude Code run per task, record what came back, and
keep the queue moving.

A worker is `claude -p` in the lane's git worktree, on a branch of its own
(task/<id>). The prompt goes in on stdin; the JSON result lands in
.hansa/runs/<id>/run.json, and hansa publishes it into tasks/<id>/ without keys when the
worker ends. Nothing here needs the worker to know about hansa.
"""
from __future__ import annotations

import errno
import json
import os
import re
import shlex
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from . import account
from .desk import HOME_SECRETS, Desk, claude_home, is_git_rule, now, read_json, write_json

# Only the wording providers use for "out of quota", so a task that is itself
# about rate limits does not rest a provider when it fails for another reason.
QUOTA = re.compile(r"API Error: (402|429)|rate_limit_error|usage limit reached|credit balance is too low|"
                   r"insufficient[_ ](quota|balance)|quota exceeded|too many requests|余额不足|额度不足|额度已用", re.I)
QUOTA_STATUS = {402, 429}
AUTH = re.compile(r"authentication_error|invalid (x-)?api[ -]key|API Error: 40[13]\b|permission_error", re.I)
AUTH_STATUS = {401, 403}
LOGIN = re.compile(r"Not logged in|Please run /login", re.I)  # Claude Code itself has no login for this provider
KEY_VARS = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")  # where a provider's key reaches Claude Code
MODEL_VARS = ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
              "ANTHROPIC_DEFAULT_HAIKU_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL")
# Every variable provider_env sets for the provider itself; the rest of its environment is the caller's
PROVIDER_VARS = ("CLAUDE_CODE_MAX_RETRIES", "CLAUDE_CONFIG_DIR", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
                 "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", *MODEL_VARS)
SETUP_FAILED = re.compile(r"^hansa: the lane's setup command failed \(exit (\d+)\)$", re.M)
# The lane's setup command runs first in the worker's own process, then the worker replaces it:
# hansa serve never waits on it, and stopping the task stops it too. It never sees a provider's key,
# and runs in srt with the lane's sandbox (see in_srt); its temporary folder is removed when it ends.
# What it changes in the repository (a lock file, say) is set aside, so it is not part of the task;
# Its output goes into a pipe, which cat (outside srt) copies to <log>: on macOS, a sandbox that hides .hansa
# also refuses some of what a command does with its own output file there (python dies at once), not a pipe.
# Its exit code comes back on fd 3, since sh has no pipefail.
# <log>.done says it got that far (a setup stopped midway leaves its changes uncommitted).
# Its git commands run only while the worktree's .git file still holds the line git wrote for it (see
# check_git_file); otherwise the worker does not start, and hansa's finish says what happened.
# sh -c SETUP_SCRIPT hansa-setup <setup> <log> <error file> <.git line> <temporary folder> <srt> <srt settings>
#   <worker command...>
SETUP_SCRIPT = ("""ok() { [ -f .git ] && [ ! -L .git ] && [ "$(cat .git)" = "$line" ]; }
line=$4
rc=$( { { env """ + " ".join(f"-u {v}" for v in KEY_VARS) + """ TMPDIR="$5" CLAUDE_CODE_TMPDIR="$5" "$6" --settings "$7" -- /bin/sh -c "$1" </dev/null 2>&1 3>&-
  echo $? >&3; } | cat >"$2"; } 3>&1 )
[ "$rc" = 0 ] || {
  rm -rf "$5"
  ! ok || [ -z "$(git status --porcelain)" ] || git stash push --include-untracked -q -m "hansa: what the lane's failed setup changed" >>"$2" 2>&1
  echo "hansa: the lane's setup command failed (exit $rc)" >>"$3"; exit 97; }
ok || { echo "hansa: the worktree's .git file was changed during the lane's setup; hansa ran no git there" >>"$3"; exit 96; }
if [ -n "$(git status --porcelain)" ]; then
  git stash push --include-untracked -q -m "hansa: what the lane's setup changed" >>"$2" 2>&1
  echo "hansa: the setup changed files in the repository; they were set aside (git stash), so they are not part of the task" >>"$2"
fi
rm -rf "$5"
: > "$2.done"
shift 7; exec "$@"
""")
# A follow-up made with `hansa retry --update` whose merge of the base conflicts: hansa merges the base again
# here, after the lane's setup (which sets aside what it changes, and could not with a merge half done), and
# leaves the conflict markers in the files for the worker. Its git runs only while the worktree's .git file
# still holds the line git wrote for it, and never sees a provider's key.
# sh -c UPDATE_SCRIPT hansa-update <base commit> <.git line> <error file> <worker command...>
UPDATE_SCRIPT = ("""[ -f .git ] && [ ! -L .git ] && [ "$(cat .git)" = "$2" ] || {
  echo "hansa: the worktree's .git file was changed before hansa merged the base in; hansa ran no git there" >>"$3"; exit 96; }
env """ + " ".join(f"-u {v}" for v in KEY_VARS) + """ git merge --no-ff --no-commit --no-verify "$1" >/dev/null 2>&1
shift 3; exec "$@"
""")
# The lane's check, in srt; its exit code goes to a file, so a restarted server can still read it.
# Its output goes into a pipe, and cat (outside srt) copies it to the log the script writes to (see SETUP_SCRIPT).
# sh -c CHECK_SCRIPT <token> <check> <exit file> <temporary folder> <srt> <srt settings>
CHECK_SCRIPT = '{ "$4" --settings "$5" -- sh -c "$1" 2>&1; echo $? > "$2"; } | cat; rm -rf "$3"'
# `hansa lane try`: the lane's setup, then its check, each in srt as for a task; the first that fails ends it.
# Each writes <folder>/<name>.log (through a pipe, see SETUP_SCRIPT) and <folder>/<name>.exit: its exit code
# and the seconds it took. Each step's temporary folder is removed after it, or when it does not run.
# sh -c TRY_SCRIPT <token> <folder> <srt> [<name> <command> <srt settings> <temporary folder>]...
TRY_SCRIPT = ("""d=$1 srt=$2
run() {
  t0=$(date +%s)
  rc=$( { { env """ + " ".join(f"-u {v}" for v in KEY_VARS) + """ TMPDIR="$4" CLAUDE_CODE_TMPDIR="$4" "$srt" --settings "$3" -- /bin/sh -c "$2" </dev/null 2>&1 3>&-
    echo $? >&3; } | cat >"$d/$1.log"; } 3>&1 )
  rm -rf "$4"
  echo "$rc $(( $(date +%s) - t0 ))" >"$d/$1.exit"
  [ "$rc" = 0 ]
}
shift 2
while [ $# -ge 4 ]; do
  run "$1" "$2" "$3" "$4" || break
  shift 4
done
while [ $# -ge 4 ]; do rm -rf "$4"; shift 4; done
""")
KEY_REST_MINUTES = 24 * 60  # until the principal changes the key (saving a key clears the rest)
# or logs in (then a check that answers clears it)
# The provider would not answer this request (its content filter or a refusal). That
# says nothing about other tasks, so the provider does not rest; the task moves on.
REFUSED_STOPS = {"content_filter", "refusal"}
BASE_ENV = ("HOME", "PATH", "LANG", "LC_ALL", "USER", "LOGNAME", "SHELL", "TMPDIR")
GIT_ID = {"GIT_AUTHOR_NAME": "hansa", "GIT_AUTHOR_EMAIL": "hansa@localhost",
          "GIT_COMMITTER_NAME": "hansa", "GIT_COMMITTER_EMAIL": "hansa@localhost"}
PROTOCOL = """You are a worker on task {tid} in lane {lane}. You work only in this directory,
on branch task/{tid}, which was made for this task.
- Do not commit: when you end, hansa commits everything you changed here. To look at your
  work, git status, git diff, git log and git show are allowed.
- If git status lists untracked .bashrc, .gitconfig, .vscode, .mcp.json, .claude/ and the like that
  you did not make: they are the sandbox's placeholders, never committed; leave them and do not report them.
- Do not push, do not switch branches, do not touch other directories.
- You work in a sandbox; inside it any command runs. Your temporary folder ($TMPDIR) is yours
  alone for this run and is removed when it ends: put scratch files there, not in this directory.
- If a command you need is refused, say which one and why you need it in your report;
  do not look for a way around the refusal.
- End with a short report: what you changed, how you checked it, and anything left undone.
  Stop or wait for any background work you started before you write it: your last message
  is taken as your report.
- Nobody will wake you later or answer questions: wait for your own work before you report.
"""


VERIFY_PROTOCOL = """You are a worker in lane {lane}, checking another worker's finished task. You are
on branch task/{tid}, a copy of that task's branch made for this check; nothing here is merged.
- Change files as your checks need (undo code, add a throwaway test), but do not commit; hansa sets
  your changes aside when you end. For undoing, `git checkout <commit> -- <path>` and
  `git restore` are allowed here.
- If git status lists untracked .bashrc, .gitconfig, .vscode, .mcp.json, .claude/ and the like that
  nobody made: they are the sandbox's placeholders, never committed; leave them and do not report them.
- Do not push, do not switch branches, do not touch other directories.
- You work in a sandbox; inside it any command runs. Your temporary folder ($TMPDIR) is yours
  alone for this run and is removed when it ends: put scratch files there, not in this directory.
- If a command you need is refused, say which one and why in your report; do not look for a way around it.
- Stop or wait for any background work you started before you write your report: your last
  message is taken as your report.
- Nobody will wake you later or answer questions: wait for your own work before you report.
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
# A task or verification worker may run any command: its sandbox (sandbox_settings) holds what the command
# can reach, and the deny rules, weighed before this, still hold. Without it Claude Code asks about commands
# with ${...} or a heredoc holding braces and quotes, and nobody answers. This is safe only while the sandbox
# never lets a command out (allowUnsandboxedCommands false, no excludedCommands).
WORKER_ALLOW = ["Bash"]
WORKER_TMP_ROOT = "/tmp"  # where each worker run's own temporary folder is made (make_worker_tmp)
# Git history is hansa's. Inside the sandbox any command runs without a rule, so a git command
# that changes history or branches is kept out by name. Verifications keep VERIFY_ALLOW's two.
GIT_DENY = [f"Bash(git {c}:*)" for c in ("commit", "push", "merge", "rebase", "reset", "tag", "cherry-pick",
                                         "revert", "am", "stash", "switch", "branch", "checkout", "restore")]
# What no worker command may write in git's own folders, so history stays hansa's whatever runs git
# (git -c k=v commit, a build script): in the repository's common folder, and in the worktree's own.
GIT_COMMON_DENY = ("objects", "refs", "packed-refs", "HEAD", "config", "hooks", "info", "logs")
GIT_OWN_DENY = ("HEAD", "logs", "commondir", "gitdir")
GIT_DENY_DIRS = ("objects", "refs", "hooks", "info", "logs")  # the folders among them; the rest are files
# A worker is one `claude -p` run: nothing wakes it later, nobody answers or is notified. Design sessions keep them.
INTERACTIVE_TOOLS = ["ScheduleWakeup", "CronCreate", "CronDelete", "CronList", "RemoteTrigger", "PushNotification",
                     "AskUserQuestion", "Monitor"]
LOCAL_HOSTS = ("localhost", "127.0.0.1")

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
  to type /exit. hansa then hands the work to the coordinator, who checks it and merges it.
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
UPDATE_NOTE = """
Before you started, hansa merged {base} as it is now into this branch, which holds the previous attempt's
work. The merge conflicts in these files, which now hold git's conflict markers (<<<<<<< this branch,
======= then {base}, >>>>>>> end): {files}. Settle each conflict by editing the file so it keeps what
both sides need, and remove every marker. Do not run git merge or commit: when you end, hansa's commit
completes the merge. Then do what the task says.
"""
UPDATE_CLEAN_NOTE = """
Before you started, hansa merged {base} as it is now into this branch, which holds the previous attempt's
work; it merged without conflicts.
"""
DESIGN_NOTE = "(A design session: the principal shaped this directly with it. Its last message:)\n\n"
# The principal is in the session, so it commits each step itself and brings in the base.
DESIGN_ALLOW = ["Bash(git add:*)", "Bash(git commit:*)", "Bash(git merge:*)"]

# A lane's turn and time limits are a circuit breaker, set high: a worker that reaches one
# was given a task too big or unclear, or got stuck. Its notes show where it went.
CEILING = "the task was probably too big or unclear, or the worker got stuck"
STOPPED_NOTE = "(Stopped at the ceiling before it wrote its report. Its notes along the way, to see what went wrong:)\n\n"
LATE_NOTE = "\n\n(Written after that, when a background notice came in:)\n\n"


def claude_bin() -> str:
    for c in (os.environ.get("HANSA_CLAUDE"), shutil.which("claude"),
              os.path.expanduser("~/.local/bin/claude")):
        if c and os.access(c, os.X_OK):
            return c
    raise RuntimeError("claude was not found; install Claude Code or set HANSA_CLAUDE")


SRT_MISSING = ("hansa needs srt (Anthropic's sandbox runtime) to run a lane's setup and check commands in a "
               "sandbox, and srt is not on the PATH. Install it with `npm install -g @anthropic-ai/sandbox-runtime` "
               "(the coordinator can do this). A lane with neither a setup nor a check command needs no srt.")


def srt_bin() -> str:
    """srt, which runs a lane's setup and check in the lane's sandbox; hansa never runs them without it."""
    c = shutil.which("srt")
    if not c:
        raise RuntimeError(SRT_MISSING)
    return c


SRT_TRY_SECONDS = 60


class SrtCannotRun(RuntimeError):
    """srt was not found, or found but could not run `true` in a sandbox: the machine's problem, not the task's.
    str(): what srt printed (or that it is missing)."""


def srt_record(desk: Desk) -> dict:
    """What hansa serve last saw of srt: {found, path, ran, said, at}; {} when no server has tried it yet."""
    return read_json(desk.state_dir / "srt.json", {}) or {}


def try_srt(desk: Desk, settings_file, tmp: str, cwd, env: dict) -> str:
    """Run `true` in srt with these settings and environment, as a setup or check would run, and record what
    happened in the desk's state (srt_record). Returns srt's path; raises SrtCannotRun when it is not there
    or does not run: srt is a node script, so a PATH without node finds srt and still cannot start it."""
    path = shutil.which("srt", path=env.get("PATH", os.defpath)) or shutil.which("srt")
    rec = {"found": bool(path), "path": path or "", "ran": False, "said": "", "at": now()}
    if path:
        try:
            r = subprocess.run([path, "--settings", str(settings_file), "--", "true"], cwd=str(cwd),
                               env={**env, "TMPDIR": tmp, "CLAUDE_CODE_TMPDIR": tmp}, stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, errors="replace", timeout=SRT_TRY_SECONDS)
            rec["ran"] = r.returncode == 0
            said = " ".join((r.stdout + r.stderr).split())[-500:]
            rec["said"] = "" if rec["ran"] else f"{said or 'nothing'} (exit {r.returncode})"
        except subprocess.TimeoutExpired:
            rec["said"] = f"no answer in {SRT_TRY_SECONDS} s"
        except OSError as e:
            rec["said"] = str(e)[:300]
    else:
        rec["said"] = SRT_MISSING
    desk.state_dir.mkdir(exist_ok=True)
    write_json(desk.state_dir / "srt.json", rec)
    if not rec["ran"]:
        raise SrtCannotRun(rec["said"] if not path else f"srt ({path}) could not start: {rec['said']}")
    return path


def try_srt_at_start(desk: Desk) -> dict:
    """hansa serve's try of srt when it starts: `true` in a sandbox that may write only a temporary folder
    (before each setup or check it tries again, with the lane's settings). Returns the record."""
    tmp = tempfile.mkdtemp(prefix="hansa-srt-")
    try:
        f = Path(tmp) / "settings.json"
        write_json(f, {"network": {"allowedDomains": [], "deniedDomains": []},
                       "filesystem": {"allowWrite": full_paths([tmp]), "denyWrite": [],
                                      "denyRead": full_paths([desk.secrets_dir, desk.state_dir])}})
        try_srt(desk, f, tmp, tmp, {k: os.environ[k] for k in BASE_ENV if k in os.environ})
    except SrtCannotRun:
        pass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return srt_record(desk)


def srt_when(rec: dict) -> str:
    """When hansa serve tried srt, as month-day hour:minute; '' when the record has no time."""
    return time.strftime("%m-%d %H:%M", time.localtime(rec["at"])) if rec.get("at") else ""


def srt_needed(s: dict) -> bool:
    """Whether some lane has a setup or check command, which run in srt."""
    return any(ln.get("setup") or ln.get("check") for ln in s["lanes"].values())


def srt_problem(desk: Desk, s: dict) -> str:
    """What stops a lane's setup and check from running, as hansa serve last saw srt; before any server has
    tried it, as this process's PATH sees it, and said so. '' when nothing does, or no lane needs srt."""
    if not srt_needed(s):
        return ""
    rec = srt_record(desk)
    if not rec:
        return "" if shutil.which("srt") else (
            "srt MISSING (on the PATH of this command; hansa serve has not tried srt yet, and its PATH may differ): "
            + SRT_MISSING)
    if rec.get("ran"):
        return ""
    when = f", {srt_when(rec)}" if srt_when(rec) else ""
    if not rec.get("found"):
        return f"srt MISSING (as hansa serve saw it{when}): {SRT_MISSING}"
    return (f"srt CANNOT RUN (as hansa serve saw it{when}): it is at {rec.get('path')}, but a sandboxed `true` "
            f"failed: {rec.get('said')}. Setups and checks do not run until this is fixed (hansa explain server)")


def tmux_bin() -> str:
    for c in (os.environ.get("HANSA_TMUX"), shutil.which("tmux"), "/opt/homebrew/bin/tmux", "/usr/local/bin/tmux"):
        if c and os.access(c, os.X_OK):
            return c
    raise RuntimeError("tmux was not found; a design session runs in tmux so the principal can join it")


def tmux_socket(desk: Desk) -> str:
    """One tmux server per desk, so session names never meet another desk's."""
    import hashlib
    return "hansa-" + hashlib.sha1(str(desk.root).encode()).hexdigest()[:6]


def tmux_socket_path(sock: str) -> str:
    """The full path of the socket tmux makes for `-L sock`: in $TMUX_TMPDIR or /tmp, under tmux-<uid>."""
    return os.path.join(os.environ.get("TMUX_TMPDIR") or "/tmp", f"tmux-{os.getuid()}", sock)


def join_command(m: dict) -> str:
    """By the socket's full path, so root, as a principal on a server usually is, can join a session
    the desk's account runs: tmux admits its owner and root."""
    sock, name = m.get("tmux") or ["", ""]
    return f"tmux -u -S {tmux_socket_path(sock)} attach -t {name}"


def git(*args, cwd, env=None) -> str:
    e = {**os.environ, **GIT_ID, **(env or {})}
    r = subprocess.run(["git", *args], cwd=str(cwd), env=e, capture_output=True, text=True, errors="replace")
    if r.returncode:
        refused = account.git_refusal(r.stderr or "", cwd)
        if refused:
            raise RuntimeError(refused)
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
        if str(st.get("reason", "")).startswith("not logged in"):
            if p["kind"] == "compatible":  # no login of its own: the saved key did not arrive
                return f"{st['reason']}; saving a new key brings it back, or `hansa check {name}` once it arrives"
            # start Claude Code exactly as the worker does: on macOS a login made with
            # CLAUDE_CONFIG_DIR set, even to ~/.claude, may land in another keychain entry
            login = f"CLAUDE_CONFIG_DIR={shlex.quote(p['config_dir'])} claude" if p.get("config_dir") else "claude"
            how = f"`{login}`"
            if account.LINUX and account.uid() != 0:  # a principal on a server usually works as root
                who = account.name_of(account.uid())
                as_root = f"env {login}" if p.get("config_dir") else login
                how += f" as {who} (from root: `runuser -u {who} -- {as_root}`)"
            return f"{st['reason']}; log in with {how}, then `hansa check {name}` brings it back"
        return f"resting until {time.strftime('%m-%d %H:%M', time.localtime(st['cool_until']))} ({st.get('reason', '')})"
    return ""


def config_home(desk: Desk, name: str, p: dict) -> Path:
    """Where Claude Code keeps this provider's login or settings, and its session records."""
    return claude_home(desk.state_dir, name, p)


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


def _rows(home: Path, sid: str):
    """A session record's rows, in order; None when it cannot be read."""
    rows = []
    try:
        for f in home.glob(f"projects/*/{sid}.jsonl") if sid else []:
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if isinstance(d, dict):
                    rows.append(d)
    except OSError:
        return None
    return rows


def _strings(x):
    """Every string inside a row, however deep."""
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for v in x.values():
            yield from _strings(v)
    elif isinstance(x, list):
        for v in x:
            yield from _strings(v)


# Only when a result row carries no id in structured form: the id comes right after the colon. An agent's
# result also says "the agentId below", which is not an id.
BASH_ID = re.compile(r"running in background with ID:\s*([A-Za-z0-9_-]+)", re.I)
AGENT_ID = re.compile(r"\bagentId:\s*([A-Za-z0-9_-]+)")
NOTICE = re.compile(r"<task-notification>(.*?)(?:</task-notification>|$)", re.S)
NOTICE_ID = re.compile(r"<task-id>\s*([A-Za-z0-9_-]+)\s*</task-id>")
NOTICE_STATUS = re.compile(r"<status>\s*([^<]*?)\s*</status>")
ENDED = re.compile(r"\b(completed|complete|done|finished|failed|killed|stopped|cancell?ed|exited|timed[ _]out|timeout|error)\b",
                   re.I)


def background_left(home: Path, sid: str) -> list:
    """What a worker started in the background (a Bash command or an agent) and had no notice of
    ending by the end of its session record, by description: it was still running when the worker
    ended, so the worker may have written its report without it. Empty when the record cannot be read."""
    rows = _rows(home, sid) or []
    started, ended = {}, set()  # background id: description
    calls = {}  # tool use id: (tool name, input)
    for d in rows:
        content = (d.get("message") or {}).get("content")
        for c in content if isinstance(content, list) else []:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "tool_use" and isinstance(c.get("input"), dict):
                calls[c.get("id")] = (c.get("name"), c["input"])
                if c.get("name") in ("TaskStop", "KillShell", "KillBash"):  # the worker stopped it itself
                    ended.update(str(v) for k, v in c["input"].items() if k in ("task_id", "shell_id", "id"))
            elif c.get("type") == "tool_result" and c.get("tool_use_id") in calls:
                name, given = calls[c["tool_use_id"]]
                said = "\n".join(_strings(c.get("content")))
                result = d.get("toolUseResult") if isinstance(d.get("toolUseResult"), dict) else {}
                bid = None
                if name == "Bash" and given.get("run_in_background"):
                    bid = result.get("backgroundTaskId")
                    if not bid and "backgroundTaskId" not in result:
                        found = BASH_ID.search(said)
                        bid = found and found.group(1)
                elif name in ("Agent", "Task"):
                    if result.get("agentId") or result.get("status"):
                        bid = result.get("status") == "async_launched" and result.get("agentId")
                    elif given.get("run_in_background") or re.search(r"async|background", said, re.I):
                        found = AGENT_ID.search(said)
                        bid = found and found.group(1)
                if bid:
                    what = given.get("description") or given.get("command") or given.get("prompt") or name
                    started[str(bid)] = " ".join(str(what).split())[:100]
        for s in _strings(d):
            for n in NOTICE.findall(s) if "<task-notification>" in s else []:
                tid = NOTICE_ID.search(n)
                status = NOTICE_STATUS.search(n)
                if tid and ENDED.search(status.group(1) if status else n):
                    ended.add(tid.group(1))
    return [what for bid, what in started.items() if bid not in ended]


def report_after_notices(home: Path, sid: str):
    """(report, what it wrote after) when the session ended on turns a background notice opened
    (a watch it started timing out, say): Claude Code then takes its answer to the notice as the
    result, and the report is its last message before the first of those notices. None when the
    session did not end so, or its record cannot be read."""
    rows = _rows(home, sid)
    if rows is None:
        return None
    rows = [d for d in rows if d.get("type") in ("user", "assistant")]

    def blocks(d, kind):
        content = (d.get("message") or {}).get("content") if d.get("type") == "assistant" else None
        return [c for c in content if isinstance(c, dict) and c.get("type") == kind] if isinstance(content, list) else []

    def texts(d):
        return [c["text"].strip() for c in blocks(d, "text") if isinstance(c.get("text"), str) and c["text"].strip()]

    # A turn opens on a user row with turnOrigin; tool results are user rows without it. A notice
    # that came in while the worker was busy, or after which it went on working, is part of the work:
    # only a notice that opened a turn after the worker had stopped, and that it only answered, is after the report.
    openers = [i for i, d in enumerate(rows) if d.get("type") == "user" and d.get("turnOrigin")]
    cut, end = None, len(rows)
    while openers and rows[openers[-1]]["turnOrigin"] == "task_notification":
        i = openers.pop()
        stopped = i > 0 and rows[i - 1].get("type") == "assistant" and not blocks(rows[i - 1], "tool_use")
        if not stopped or any(blocks(d, "tool_use") for d in rows[i:end]):
            break
        cut, end = i, i
    if cut is None:
        return None
    said = [(i, (rows[i].get("message") or {}).get("id")) for i in range(cut) if texts(rows[i])]
    if not said:
        return None
    last_id = said[-1][1]
    ids = [i for i, mid in said if mid == last_id] if last_id else [said[-1][0]]
    report = "\n\n".join(t for i in ids for t in texts(rows[i]))
    later = "\n\n".join(t for d in rows[cut:] for t in texts(d))
    return report, later


def progress_of(desk: Desk, m: dict) -> dict:
    """How far a running worker has got: model replies so far, minutes since it last did
    anything, and the last thing it wrote. hansa records turns and tokens only at the end."""
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
        for v in MODEL_VARS:
            env[v] = p["model"]
    return env


def isolated(cwd, settings: dict | None = None) -> list:
    """What keeps a Claude Code run hansa starts to its checkout and hansa's own instructions: no settings,
    plugins, hooks or CLAUDE.md of the user it runs as, no MCP servers but those hansa gives it, and no
    CLAUDE.md from the folders above the checkout (the desk's card, the coordinator's, among them).
    `settings` go into the same --settings object (a worker's sandbox, say)."""
    above = []
    for d in dict.fromkeys([*Path(cwd).parents, *Path(cwd).resolve().parents]):
        above += [str(d / "CLAUDE.md"), str(d / "CLAUDE.local.md"), str(d / ".claude" / "CLAUDE.md")]
    return ["--setting-sources", "project,local", "--strict-mcp-config",
            "--settings", json.dumps({"claudeMdExcludes": above, **(settings or {})})]


def full_paths(paths) -> list:
    """Each path as given and with links resolved (/tmp is /private/tmp on macOS), once."""
    return list(dict.fromkeys(str(x) for q in paths for x in (Path(q), Path(q).resolve())))


def git_write_deny(wt: Path) -> list:
    """The paths in git's folders for the worktree `wt` that a sandboxed worker may not write, worked
    out now: Claude Code opens a linked worktree's shared git folder for writing. The worktree's own
    .git file is among them: pointed at a git folder of the worker's making, it would run the worker's
    hooks and settings the next time hansa runs git there, outside the sandbox."""
    return full_paths([*_git_folder_deny(wt), Path(wt) / ".git"])


def _git_folder_deny(wt: Path) -> list:
    """The paths of git_write_deny in git's folders: the repository's common folder and the worktree's own.
    A main checkout has no folder of its own (and no commondir or gitdir): its folder is the common one."""
    common = Path(wt) / git("rev-parse", "--git-common-dir", cwd=wt)  # relative to wt, or absolute
    own = Path(git("rev-parse", "--absolute-git-dir", cwd=wt))
    return [*(common / f for f in GIT_COMMON_DENY),
            *(own / f for f in GIT_OWN_DENY if own.resolve() != common.resolve())]


def make_git_write_deny(wt: Path) -> None:
    """Create each path of git_write_deny that is missing, empty. On Linux the sandbox (bwrap) puts a device
    nobody can open where a listed path does not exist, and git then fails reading it: packed-refs, say,
    which a repository made with git init has only once refs are packed. Git takes an empty packed-refs
    as none. A path that exists is left as it is."""
    for f in _git_folder_deny(wt):
        if os.path.lexists(f):
            continue
        try:
            if f.name in GIT_DENY_DIRS:
                f.mkdir()
            else:
                os.close(os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644))
        except FileExistsError:  # made meanwhile, by git say
            pass


# A linked worktree's .git is a one-line file (gitdir: <folder>) that tells git where the worktree's
# history and settings live. Whatever runs in the worktree could point it at a git folder with hooks
# or settings of its own, which the next git command hansa runs there would obey, outside any sandbox.
# So hansa reads it as a plain file before each git command it runs in a worktree, against the line
# it took from the repository when it made the worktree (.hansa/worktrees.json), and runs no git
# there when they differ.

class GitFileChanged(RuntimeError):
    """The worktree's .git file is not the one git wrote for it. str(): what the principal is told."""

    def __init__(self, wt: Path, found: str, expected: str) -> None:
        self.wt, self.found, self.expected = Path(wt), found, expected
        super().__init__(f"the worktree's .git file was changed: {found} instead of "
                         f"{expected.split(':', 1)[-1].strip()}; hansa ran no git in {wt}")


def _known_git_files(desk: Desk) -> dict:
    return read_json(desk.state_dir / "worktrees.json", {}) or {}


def _remember_git_file(desk: Desk, wt: Path, line) -> None:
    with desk.lock():
        known = _known_git_files(desk)
        if line:
            known[str(wt)] = line
        elif str(wt) not in known:
            return
        else:
            del known[str(wt)]
        desk.state_dir.mkdir(exist_ok=True)
        write_json(desk.state_dir / "worktrees.json", known)


def git_admin_dirs(repo: Path, wt: Path) -> list:
    """The folders under the repository's worktrees/ whose gitdir file names this worktree's .git:
    the worktree's own git folder, found from the repository and not from the worktree."""
    common = Path(repo) / git("rev-parse", "--git-common-dir", cwd=repo)  # relative to repo, or absolute
    target, found = Path(wt).resolve() / ".git", []
    for d in sorted((common / "worktrees").iterdir()) if (common / "worktrees").is_dir() else []:
        try:
            named = Path((d / "gitdir").read_text(encoding="utf-8").strip())
        except OSError:
            continue
        if (named if named.is_absolute() else d / named).resolve() == target:
            found.append(d.resolve())
    return found


def _read_git_file(wt: Path):
    """(line, what was found) for the worktree's .git, read as a plain file and never through git;
    line is None when it is not a regular file."""
    dot = Path(wt) / ".git"
    try:
        st = os.lstat(dot)
    except FileNotFoundError:
        return None, "it is gone"
    if stat.S_ISLNK(st.st_mode):
        return None, f"it is now a link to {os.readlink(dot)}"
    if stat.S_ISDIR(st.st_mode):
        return None, "it is now a folder"
    if not stat.S_ISREG(st.st_mode):
        return None, "it is no longer a plain file"
    try:
        with os.fdopen(os.open(dot, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)), "rb") as fh:
            line = fh.read(4096).decode("utf-8", "replace").strip()
    except OSError as e:
        return None, f"it cannot be read ({e.strerror})"
    where = line[len("gitdir:"):].strip() if line.startswith("gitdir:") else ""
    return line, f"it now points to {where}" if where else f"it now holds {line[:100]!r}"


def expected_git_line(desk: Desk, wt: Path, repo=None):
    """The gitdir line git wrote for this worktree: remembered when hansa made it, or else found from the
    lane's repository (a worktree made before hansa remembered them) and remembered then. None: unknown."""
    line = _known_git_files(desk).get(str(wt))
    if line or repo is None:
        return line
    try:
        dirs = git_admin_dirs(Path(repo), Path(wt))
    except (RuntimeError, OSError):
        return None
    if len(dirs) != 1:  # none, or another folder names it too: which one is git's cannot be told
        return None
    line, now_line = f"gitdir: {dirs[0]}", _read_git_file(Path(wt))[0]
    if now_line and now_line.startswith("gitdir:"):  # the same folder written another way (relative, say)
        written = Path(now_line[len("gitdir:"):].strip())
        if (written if written.is_absolute() else Path(wt) / written).resolve() == dirs[0]:
            line = now_line
    _remember_git_file(desk, Path(wt), line)
    return line


def check_git_file(desk: Desk, wt: Path, repo=None) -> None:
    """Raise GitFileChanged unless the worktree's .git is still the regular file holding the line git
    wrote for it. hansa runs it before each git command it runs in a worktree."""
    wt = Path(wt)
    line = expected_git_line(desk, wt, repo)
    if not line:
        raise RuntimeError(f"hansa does not know which git folder {wt} belongs to, so it runs no git there")
    found, what = _read_git_file(wt)
    if found != line:
        raise GitFileChanged(wt, what, line)


def git_file_changed(desk: Desk, tid: str, e: GitFileChanged) -> str:
    """After a worktree's .git was found changed: keep what was found in the task's raw folder, put the
    right file back so the lane works again, record it, and say it in plain words."""
    dot = e.wt / ".git"
    keep = desk.raw_dir(tid) / f"worktree-git-{int(time.time() * 1000)}"
    keep.mkdir(parents=True, exist_ok=True)
    (keep / "what-was-found.txt").write_text(f"{dot}: {e.found}\nexpected: {e.expected}\n", encoding="utf-8")
    if e.found.startswith("it now points to "):  # the settings and hooks hansa's git would have run
        target = Path(e.found[len("it now points to "):])
        target = target if target.is_absolute() else e.wt / target
        for name in ("config", "hooks"):
            try:
                if (target / name).is_dir():
                    shutil.copytree(target / name, keep / name, symlinks=True)
                elif (target / name).is_file():
                    shutil.copy2(target / name, keep / name)
            except OSError:
                pass
    if os.path.lexists(dot):
        shutil.move(str(dot), str(keep / "found.git"))
    dot.write_text(e.expected + "\n", encoding="utf-8")
    desk.event("worktree_git_changed", task=tid, worktree=str(e.wt), found=e.found, expected=e.expected)
    return (f"{e}. What was found is kept in .hansa/runs/{tid}/{keep.name}; the right file is back, "
            f"so the lane's next task runs normally")


def wgit(desk: Desk, wt: Path, *args) -> str:
    """git in a worktree, run only after its .git file is checked."""
    check_git_file(desk, wt)
    return git(*args, cwd=wt)


def sandbox_leftovers(desk: Desk, wt: Path) -> list:
    """Untracked files a worker's sandbox left under .claude/ in the worktree: its .cc-writes and the empty
    placeholders for what it protects. Nobody's work, so never committed."""
    out = wgit(desk, wt, "ls-files", "--others", "--exclude-standard", "-z", "--", ".claude")
    return [f for f in out.split("\0") if f and (Path(f).name == ".cc-writes" or (wt / f).is_file()
                                                  and not (wt / f).is_symlink() and (wt / f).stat().st_size == 0)]


def commit_work(desk: Desk, wt: Path, message: str) -> None:
    """Commit everything changed in the worktree but what a sandbox left there; nothing when that is all."""
    left = sandbox_leftovers(desk, wt)  # untracked until the add
    wgit(desk, wt, "add", "-A")
    if left:
        wgit(desk, wt, "rm", "-q", "--cached", "--", *left)
    if wgit(desk, wt, "diff", "--cached", "--name-only"):
        wgit(desk, wt, "commit", "-q", "--no-verify", "-m", message)


def is_local(host: str) -> bool:
    return host.lower().split(":")[0] in LOCAL_HOSTS


def lane_sandbox(desk: Desk, lane: dict, pname: str, p: dict, wt: Path) -> tuple:
    """What a lane's sandbox lets through, for its workers (Claude Code's sandbox) and for its setup and
    check (srt): (the hosts it may reach, {"denyRead": the desk's keys and state, the provider's login and
    credential files, "denyWrite": git's data, "allowWrite": the lane's write list}). Each adds the
    worktree and a temporary folder to what may be written."""
    make_git_write_deny(wt)  # before anything starts in the sandbox
    home = Path.home()
    deny = full_paths([desk.secrets_dir, desk.state_dir, config_home(desk, pname, p),
                       *(home / f for f in HOME_SECRETS)])
    # the write list holds build or test tools' cache folders; denyRead and denyWrite still win within them
    return list(lane.get("network", [])), {"denyRead": deny, "denyWrite": git_write_deny(wt),
                                           "allowWrite": full_paths(lane.get("write", []))}


def sandbox_settings(desk: Desk, lane: dict, pname: str, p: dict, wt: Path) -> dict:
    """Claude Code's Bash sandbox for a task or verification worker, enforced by the operating system:
    any command, but files changed only in the worktree (and the lane's write list) and never git's
    history, no reading of keys and logins, no network but the lane's list, and no keys in the environment. It never falls back to
    running unsandboxed."""
    hosts, fs = lane_sandbox(desk, lane, pname, p, wt)
    network = {"allowedDomains": hosts, "strictAllowlist": True}
    if any(is_local(h) for h in hosts):
        network["allowLocalBinding"] = True  # a worker may run a local server and reach it
    if not fs["allowWrite"]:
        del fs["allowWrite"]
    return {"sandbox": {
        "enabled": True,
        "failIfUnavailable": True,
        "autoAllowBashIfSandboxed": True,
        "allowUnsandboxedCommands": False,
        "filesystem": fs,
        "network": network,
        "credentials": {"envVars": [{"name": v, "mode": "deny"} for v in KEY_VARS]},
    }}


def srt_settings(desk: Desk, lane: dict, pname: str, p: dict, wt: Path, tmp: str) -> dict:
    """srt's settings for a lane's setup or check: the sandbox of the lane's workers, writing in the
    worktree, the temporary folder `tmp` and the lane's write list."""
    hosts, fs = lane_sandbox(desk, lane, pname, p, wt)
    network = {"allowedDomains": hosts, "deniedDomains": []}  # srt refuses settings without deniedDomains
    if any(is_local(h) for h in hosts):
        network["allowLocalBinding"] = True
    return {"network": network, "filesystem": {"allowWrite": full_paths([wt, tmp]) + fs["allowWrite"],
                                               "denyRead": fs["denyRead"], "denyWrite": fs["denyWrite"]}}


def in_srt(desk: Desk, tid: str, name: str, lane: dict, pname: str, p: dict, wt: Path) -> tuple:
    """What a lane's setup or check (`name`) runs in: (srt, its settings file in the task's raw folder,
    a temporary folder made for this run, for the command's TMPDIR). srt ignores the TMPDIR it is given
    and sets its own from CLAUDE_CODE_TMPDIR (else /tmp/claude), so the caller sets both to the folder.
    Whoever runs the command removes the folder after it; finish and finish_check remove one left behind.
    srt is tried first with these settings (try_srt): when it cannot run, SrtCannotRun, and nothing runs."""
    try:
        check_git_file(desk, wt)  # lane_sandbox asks git in the worktree
    except GitFileChanged as e:
        raise RuntimeError(git_file_changed(desk, tid, e)) from None
    tmp = tempfile.mkdtemp(prefix=f"hansa-{name}-")
    f = desk.raw_dir(tid) / f"{name}-sandbox.json"
    write_json(f, srt_settings(desk, lane, pname, p, wt, tmp))
    desk.change(tid, lambda m: {"sandbox_tmp": [*m.get("sandbox_tmp", []), tmp]})
    env = {**{k: os.environ[k] for k in BASE_ENV if k in os.environ}, **lane.get("env", {})}
    try:
        srt = try_srt(desk, f, tmp, wt, env)
    except SrtCannotRun:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return srt, str(f), tmp


def remove_sandbox_tmp(desk: Desk, tid: str) -> None:
    """The srt settings of a task's setup and check, once they have ended, and their temporary folders
    still there (one stopped midway leaves its own), and the worker's own temporary folder (worker_tmp)."""
    for name in ("setup", "check"):
        (desk.state_dir / "runs" / tid / f"{name}-sandbox.json").unlink(missing_ok=True)
    m = desk.meta(tid)
    for t in [*m.get("sandbox_tmp", []), *([m["worker_tmp"]] if m.get("worker_tmp") else [])]:
        shutil.rmtree(t, ignore_errors=True)
    desk.update(tid, sandbox_tmp=[], worker_tmp="")


def make_worker_tmp(desk: Desk, tid: str) -> str:
    """A temporary folder for one worker run, its alone (mode 700), recorded as the task's worker_tmp so
    remove_sandbox_tmp removes it however the run ends. Claude Code gets it as CLAUDE_CODE_TMPDIR, makes its
    per-user folder (the worker's TMPDIR) and its sockets in it, so its path is kept short: under /tmp,
    not the server's TMPDIR (a long path under /var/folders on macOS)."""
    tmp = tempfile.mkdtemp(prefix=f"hansa-{tid}-", dir=WORKER_TMP_ROOT)
    desk.update(tid, worker_tmp=tmp)
    return tmp


def lane_allow(desk: Desk, lane: dict, wt: Path) -> list:
    """The worktree's files plus the lane's own commands. Git rules a lane still lists from before
    are left out: hansa does the committing, and read-only git needs no rule."""
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


MARKER_LINE = re.compile(r"^(.*):(\d+): leftover conflict marker$", re.M)


def markers_in_change(cwd, base: str, head: str) -> dict:
    """Git's conflict markers a change adds, as {file: [line numbers at head]}: the lines `git diff --check`
    calls a leftover conflict marker, less a ======= (or |||||||) line outside a <<<<<<< ... >>>>>>> block,
    such as a Markdown heading's underline."""
    r = subprocess.run(["git", "-c", "core.quotePath=false", "diff", "--check", base, head], cwd=str(cwd),
                       capture_output=True, text=True, errors="replace")
    flagged = {}
    for f, n in MARKER_LINE.findall(r.stdout):
        flagged.setdefault(f, []).append(int(n))
    out = {}
    for f, lines in flagged.items():
        shown = subprocess.run(["git", "show", f"{head}:{f}"], cwd=str(cwd), capture_output=True)
        text = shown.stdout.decode("utf-8", "replace").splitlines()
        blocks, top = [], None
        for n, x in enumerate(text, 1):
            if x.startswith("<<<<<<<"):
                top = n
            elif x.startswith(">>>>>>>") and top is not None:
                blocks.append((top, n))
                top = None
        found = [n for n in lines if n <= len(text) and (text[n - 1][:1] in "<>"
                                                         or any(a < n < b for a, b in blocks))]
        if found:
            out[f] = found
    return out


MARKERS_ON_PURPOSE = ("If those lines are there on purpose (a test fixture, say), mark the files with "
                      "conflict-marker-size in the base's .gitattributes (`path/to/fixture.txt "
                      "conflict-marker-size=100`), which git's check honours, and merge again.")


def markers_text(markers: dict) -> str:
    """Where a change's conflict markers are, in words: `a.txt lines 1, 3, 5; b.txt line 2`."""
    return "; ".join(f"{f} line{'s' if len(ns) > 1 else ''} {', '.join(map(str, ns[:12]))}"
                     + (" ..." if len(ns) > 12 else "") for f, ns in markers.items())


def publish(desk: Desk, tid: str, *names: str) -> None:
    """Move what a task's processes wrote from .hansa/runs/<id>/ into its folder, keys removed."""
    raw, d = desk.state_dir / "runs" / tid, desk.tasks_dir / tid
    with desk.lock():  # a key saved meanwhile is either known here or scrubbed from it after (Desk.scrub)
        for name in names:
            src = raw / name if (raw / name).exists() else d / name  # in d: started before hansa kept them apart
            if src.exists():
                (d / name).write_text(desk.redact(src.read_text(encoding="utf-8", errors="replace")),
                                      encoding="utf-8")
                if src.parent == raw:
                    src.unlink()
        try:
            raw.rmdir()
        except OSError:
            pass


# macOS has no /proc, and Claude Code's sandbox there will not start ps: these ask the kernel with
# sysctl instead. A struct kinfo_proc is 648 bytes; at offset 0 is its start time (a timeval: a long
# of seconds, then an int of microseconds at 8), at 36 its state p_stat (a char), at 40 its pid, and
# at 392 its real user id, at 420 its effective user id (e_ucred.cr_uid). The sandbox refuses a list by
# user (KERN_PROC_UID), not the full list.
_CTL_KERN, _KERN_PROC, _KERN_PROCARGS2 = 1, 14, 49
_KERN_PROC_ALL, _KERN_PROC_PID, _KERN_PROC_PGRP = 0, 1, 2
_KINFO_PROC_SIZE, _SZOMB = 648, 5
_MAC_STATES = {1: "I", 2: "R", 3: "S", 4: "T", _SZOMB: "Z"}


_ON_MAC = sys.platform == "darwin"


def _sysctl(*mib: int, slack: int = 0):
    """What sysctl gives for this name, or None when it cannot be read (also where libc has no
    sysctl, as glibc 2.32 and later)."""
    try:
        import ctypes
        import ctypes.util
    except ImportError:
        return None
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        name = (ctypes.c_int * len(mib))(*mib)
        for _ in range(5):  # on a busy machine the list can outgrow the slack between the two calls: ask again
            size = ctypes.c_size_t(0)
            if libc.sysctl(name, len(mib), None, ctypes.byref(size), None, 0) != 0:
                return None
            size.value += slack  # room for a list that grows between the two calls
            buf = ctypes.create_string_buffer(size.value)
            if libc.sysctl(name, len(mib), buf, ctypes.byref(size), None, 0) == 0:
                return buf.raw[:size.value]
            if ctypes.get_errno() != errno.ENOMEM:
                return None
        return None
    except (OSError, AttributeError, TypeError, ValueError, ctypes.ArgumentError):
        return None


def _mac_procs(*mib: int) -> list:
    """The kinfo_proc records sysctl lists for this KERN_PROC query; [] when there are none."""
    data = _sysctl(_CTL_KERN, _KERN_PROC, *mib, slack=_KINFO_PROC_SIZE * 16) or b""
    return [data[i:i + _KINFO_PROC_SIZE] for i in range(0, len(data) - _KINFO_PROC_SIZE + 1, _KINFO_PROC_SIZE)]


def _mac_proc(pid: int):
    """This process's kinfo_proc, or None when there is no such process."""
    found = _mac_procs(_KERN_PROC_PID, int(pid))
    return found[0] if found else None


def _mac_start(pid) -> str:
    k = _mac_proc(pid)
    if k is None:
        return ""
    sec, usec = struct.unpack_from("@li", k, 0)
    return f"{sec}.{usec:06d}"


def _mac_state(pid) -> str:
    k = _mac_proc(pid)
    return "" if k is None else _MAC_STATES.get(k[36], "?")


def _mac_group_running(pgid) -> bool:
    return any(k[36] != _SZOMB for k in _mac_procs(_KERN_PROC_PGRP, int(pgid)))


def _mac_procargs(pid: int):
    """A process's arguments and environment, as two lists; None when they cannot be read (a process
    of another account, one in the middle of an exec, one that ended meanwhile)."""
    data = _sysctl(_CTL_KERN, _KERN_PROCARGS2, pid)
    if not data or len(data) < 4:
        return None
    argc = struct.unpack_from("@i", data, 0)[0]
    rest = data[4:]
    end = rest.find(b"\0")  # the executable's path, then NULs up to the first argument
    if end < 0:
        return None
    words = rest[end:].lstrip(b"\0").split(b"\0")
    env = words[argc:]  # the environment ends at an empty string; the kernel's own strings follow it
    return words[:argc], env[:env.index(b"")] if b"" in env else env


class CannotLook(Exception):
    """The process list, or the arguments of a process that may be the one looked for, could not be
    read: what is looked for may be running all the same."""


LOOK_TRIES = 5  # reads of the process list, and of a process's arguments, before hansa says it could not look
LOOK_LIMIT = 300  # seconds a task waits, while hansa cannot look for what an earlier server started, before it fails


def _may_lead(pid: int) -> bool:
    """Whether this process leads its own process group, or may: its group cannot be read."""
    try:
        return os.getpgid(pid) == pid
    except ProcessLookupError:
        return False
    except OSError:
        return True


def _mac_pids_with_arg(token: str, since=None) -> list:
    """As pids_with_arg. A process of this account whose arguments cannot be read is read again a few
    times; one that still cannot be, and leads its own process group as hansa's processes do, may be
    the one looked for: CannotLook. Not read at all: one that started before `since` (the launch word
    was made then), and one whose effective user is not its real user (a setuid program such as sudo,
    which hansa never starts, and whose arguments cannot be read). (One that does not lead its group
    is not the one looked for either: some of the account's processes never let their arguments be read.)"""
    for i in range(LOOK_TRIES):
        data = _sysctl(_CTL_KERN, _KERN_PROC, _KERN_PROC_ALL, 0, slack=_KINFO_PROC_SIZE * 16)
        if data:
            break
        time.sleep(0.05 * (i + 1))
    else:
        raise CannotLook("the process list could not be read")
    found, unread = [], []
    for i in range(0, len(data) - _KINFO_PROC_SIZE + 1, _KINFO_PROC_SIZE):
        k = data[i:i + _KINFO_PROC_SIZE]
        pid = struct.unpack_from("@i", k, 40)[0]
        if pid <= 0 or k[36] == _SZOMB or struct.unpack_from("@I", k, 392)[0] != os.getuid() or \
                struct.unpack_from("@I", k, 420)[0] != os.getuid():
            continue
        if since and struct.unpack_from("@l", k, 0)[0] < since - 2:  # 2 s for the rounding of both clocks
            continue
        got = _mac_procargs(pid)
        if got is None:
            unread += [pid] if _may_lead(pid) else []
        elif token.encode() in got[0]:
            found.append(pid)
    for i in range(LOOK_TRIES):  # in the middle of an exec, say: a moment later it can be read, or is gone
        if not unread:
            break
        time.sleep(0.05 * (i + 1))
        again = []
        for pid in unread:
            got = _mac_procargs(pid)
            if got is not None:
                found += [pid] if token.encode() in got[0] else []
            elif _mac_state(pid) not in ("", "Z") and _may_lead(pid):
                again.append(pid)
        unread = again
    if unread:
        raise CannotLook(f"the arguments of process {unread[0]} could not be read")
    return found


def pids_with_arg(token: str, since=None) -> list:
    """Processes whose command line holds this word: hansa puts one on each process it starts, so a
    restarted server can find one whose number it never recorded; `since`, when given, is the time
    the word was made, and no older process is looked at. CannotLook when the process list cannot be
    read: not the same as an empty list, which says the word is on no process."""
    if not token:
        return []
    found = []
    if Path("/proc").is_dir():
        try:
            entries = list(Path("/proc").iterdir())
        except OSError as e:
            raise CannotLook(f"the process list could not be read: {e}") from None
        for p in entries:
            try:  # one that ends meanwhile is not there, which is right
                if p.name.isdigit() and token.encode() in (p / "cmdline").read_bytes().split(b"\0"):
                    found.append(int(p.name))
            except OSError:
                pass
    else:
        found = _mac_pids_with_arg(token, since)
    return [p for p in found if p != os.getpid()]


def leader(pids: list):
    """The one of these that leads its own process group, as each process hansa starts does.
    CannotLook when the group of one that still runs cannot be read."""
    for p in pids:
        try:
            if os.getpgid(p) == p:
                return p
        except ProcessLookupError:  # ended meanwhile
            pass
        except OSError as e:
            raise CannotLook(f"the process group of process {p} could not be read: {e}") from None
    return None


def task_processes(tid: str) -> list:
    """This account's processes whose environment gives HANSA_TASK as this task, and no other task:
    [(pid, arguments)]. One that cleared its environment is not found."""
    want, found = f"HANSA_TASK={tid}".encode(), []

    def ours(env: list) -> bool:
        named = [x for x in env if x.startswith(b"HANSA_TASK=")]
        return bool(named) and all(x == want for x in named)
    if Path("/proc").is_dir():
        for p in Path("/proc").iterdir():
            try:
                if p.name.isdigit() and p.stat().st_uid == os.getuid() and \
                        ours((p / "environ").read_bytes().split(b"\0")):
                    found.append((int(p.name), (p / "cmdline").read_bytes().rstrip(b"\0").split(b"\0")))
            except OSError:
                pass
    else:
        for k in _mac_procs(_KERN_PROC_ALL, 0):
            pid = struct.unpack_from("@i", k, 40)[0]
            if pid > 0 and k[36] != _SZOMB and struct.unpack_from("@I", k, 392)[0] == os.getuid():
                args, env = _mac_procargs(pid) or ([], [])
                if ours(env):
                    found.append((pid, args))
    return [(p, [a.decode(errors="replace") for a in args]) for p, args in found]


SWEEP_GRACE = 5  # seconds between the polite signal and the one that cannot be refused


def sweep(desk: Desk, tid: str) -> list:
    """Stop what a task's processes left running once nothing of the task should run any more: a server or
    watcher that left the process group hansa stops, in a session of its own. Never hansa serve itself, nor
    the command doing this. What was stopped goes on the task (left_running) and in the event log."""
    keep = {os.getpid(), os.getppid()}
    try:
        keep.add(int((desk.state_dir / "serve.lock").read_text().split()[0]))
    except (OSError, ValueError, IndexError):
        pass

    def running(p, start):
        return alive(p, start) and _state(p) != "Z"
    found = [(p, args, proc_start(p)) for p, args in task_processes(tid) if p not in keep]
    found = [(p, args, s) for p, args, s in found if running(p, s)]
    if not found:
        return []
    for sig, grace in ((signal.SIGTERM, SWEEP_GRACE), (signal.SIGKILL, 2)):
        for p, _, start in found:
            if running(p, start):
                try:
                    os.kill(p, sig)
                except OSError:
                    pass
        end = time.time() + grace
        while any(running(p, s) for p, _, s in found) and time.time() < end:
            time.sleep(0.1)
        if not any(running(p, s) for p, _, s in found):
            break
    stopped = [{"pid": p, "command": desk.redact(" ".join(args))[:120]} for p, args, s in found if not running(p, s)]
    if stopped:
        desk.change(tid, lambda x: {"left_running": [*(x.get("left_running") or []), *stopped]})
        desk.event("left_running_stopped", task=tid, processes=[f"{x['pid']} {x['command']}" for x in stopped])
    return stopped


def ready_worktree(desk: Desk, repo: Path, wt: Path, tid: str, start: str) -> None:
    """Make the worktree if it is not there yet, check its .git file, and set aside what was left in it."""
    if not wt.exists():
        _remember_git_file(desk, wt, None)  # removed by hand: a new one gets its own line
    if not os.path.lexists(wt / ".git") and not expected_git_line(desk, wt):
        wt.parent.mkdir(parents=True, exist_ok=True)
        git("worktree", "add", "--detach", str(wt), start, cwd=repo)
    try:
        check_git_file(desk, wt, repo)  # also finds and remembers the line of a worktree just made
    except GitFileChanged as e:  # changed since the last task ended: this one does not start, the next one can
        raise RuntimeError(git_file_changed(desk, tid, e)) from None
    if merge_in_progress(desk, wt):  # a merge left half done cannot be set aside
        wgit(desk, wt, "merge", "--abort")
    if wgit(desk, wt, "status", "--porcelain"):
        wgit(desk, wt, "stash", "push", "--include-untracked", "-m", f"hansa: left over before {tid}")


def merge_in_progress(desk: Desk, wt: Path) -> bool:
    try:
        return bool(wgit(desk, wt, "rev-parse", "-q", "--verify", "MERGE_HEAD"))
    except GitFileChanged:
        raise
    except RuntimeError:
        return False


def conflicted_files(run) -> list:
    """The files a failed merge left unmerged; run: git in the checkout where it failed."""
    try:
        return run("diff", "--name-only", "--diff-filter=U").splitlines()
    except GitFileChanged:
        raise
    except RuntimeError:
        return []


def conflict_places(cwd: Path, files: list, most_files: int = 10, most_lines: int = 16) -> str:
    """Where the two sides of a failed merge meet, read from its files before the merge is aborted: for each
    file, the lines of each conflict with their numbers."""
    out = []
    for f in files[:most_files]:
        p = Path(cwd) / f
        try:  # a link is not followed: what it points to is not the change's
            text = p.read_bytes()[:1_000_000].decode("utf-8", "replace").splitlines() \
                if stat.S_ISREG(os.lstat(p).st_mode) else []
        except OSError:
            text = []
        blocks, top = [], None
        for n, x in enumerate(text, 1):
            if x.startswith("<<<<<<<") and top is None:
                top = n
            elif x.startswith(">>>>>>>") and top is not None:
                blocks.append((top, n))
                top = None
        if not blocks:
            out.append(f"  {f}: no conflict markers in it (one side deleted or renamed it, or it is not text)")
        for a, b in blocks:
            out.append(f"  {f}, lines {a}-{b}:")
            shown = list(enumerate(text[a - 1:b], a))
            if len(shown) > most_lines:
                shown = shown[:most_lines - 1] + [(None, f"... {len(shown) - most_lines + 1} more lines")]
            out += [f"    {'' if n is None else n:>5}  {x[:160]}" for n, x in shown]
    if len(files) > most_files:
        out.append(f"  and {len(files) - most_files} more file(s): {', '.join(files[most_files:])}")
    return "\n".join(out)


def conflict_advice(tid: str, base: str, places: str, error: str = "") -> str:
    """What the coordinator is told when a task's merge conflicts with the base."""
    where = (f" Where the two sides meet (<<<<<<< {base} as it is now, >>>>>>> {tid}):\n{places}\n" if places
             else f" ({error})\n" if error else "\n")
    return (f"hansa: {tid} conflicts with {base}; nothing was merged.{where}"
            f"Next: hansa retry {tid} --update \"<how to settle the conflict>\" (a worker continues from {tid}'s "
            f"branch with the current {base} merged into it, and settles the conflict by editing the files); "
            f"or, to redo the whole change: hansa retry {tid} --fresh \"<the same change on the current {base}>\"")


def update_branch(desk: Desk, wt: Path, tid: str, name: str, tip: str) -> list:
    """For `hansa retry --update`: merge the base's current tip into the task's fresh branch. Merged at once
    when it merges cleanly; else aborted here and merged again just before the worker starts, after the
    lane's setup (UPDATE_SCRIPT). Returns the files that conflict."""
    try:
        wgit(desk, wt, "merge", "--no-ff", "--no-verify", "-m", f"Merge {name} into task/{tid}", tip)
        return []
    except GitFileChanged:
        raise
    except RuntimeError as e:
        files = conflicted_files(lambda *a: wgit(desk, wt, *a))
        if merge_in_progress(desk, wt):
            wgit(desk, wt, "merge", "--abort")
        if not files:
            raise RuntimeError(f"could not merge the current {name} into task/{tid}: {e}") from None
        return files


def prepare(desk: Desk, lane_name: str, lane: dict, meta: dict, wt: Path | None = None):
    """Put the lane's worktree (or the one given) on a fresh task branch. Returns (worktree, base sha).
    A follow-up made with --update gets the base's current tip merged into it (update_branch)."""
    repo = Path(lane["repo"])
    tid = meta["id"]
    updating = bool(meta.get("update") and meta.get("start_from"))
    base = git("rev-parse", lane.get("base") or "main", cwd=repo) if updating or not meta.get("base") else meta["base"]
    start = meta.get("start_from") or base
    wt = wt or desk.worktrees_dir / lane_name
    ready_worktree(desk, repo, wt, tid, start)
    branch = f"task/{tid}"
    if wgit(desk, wt, "branch", "--list", branch):
        # an earlier try of this task: keep its commits under another name
        n = len(meta.get("attempts", []))
        wgit(desk, wt, "branch", "-m", branch, f"{branch}-try{n}")
    wgit(desk, wt, "checkout", "-q", "-B", branch, start)
    if updating:
        desk.update(tid, update_conflicts=update_branch(desk, wt, tid, lane.get("base") or "main", base))
    return wt, base


def with_setup(desk: Desk, tid: str, argv: list, lane: dict, pname: str, p: dict, wt: Path) -> list:
    """The worker command, preceded by the lane's setup command when it has one (see SETUP_SCRIPT)."""
    if not lane.get("setup"):
        return argv
    raw = desk.raw_dir(tid)
    (raw / "setup.log.done").unlink(missing_ok=True)  # an earlier try's mark says nothing about this one
    srt, settings, tmp = in_srt(desk, tid, "setup", lane, pname, p, wt)
    line = expected_git_line(desk, wt) or "(unknown, so no git runs here)"
    return ["/bin/sh", "-c", SETUP_SCRIPT, "hansa-setup", lane["setup"], str(raw / "setup.log"), str(raw / "run.err"),
            line, tmp, srt, settings, *argv]


def cancelled_before_start(desk: Desk, tid: str) -> bool:
    """A task cancelled while hansa prepared it ends there, and its worker never starts."""
    m = desk.change(tid, lambda x: {"state": "cancelled", "phase": "", "error": "cancelled before it started",
                                    "finished": now()} if x.get("cancel") else None)
    if m:
        desk.event("task_finished", task=tid, state="cancelled", provider=m.get("provider"), error=m["error"])
    return bool(m)


def not_started(desk: Desk, tid: str, e: Exception) -> None:
    """Record why a task could not start; one cancelled meanwhile stays cancelled."""
    machine = isinstance(e, SrtCannotRun)  # the lane's setup could not run: not the task's doing
    why = desk.redact(f"could not start: the lane's setup could not run: {e}" if machine else f"could not start: {e}")[:600]
    desk.change(tid, lambda m: None if m["state"] not in ("queued", "running") else
                {"state": "cancelled", "phase": "", "error": "cancelled before it started", "finished": now()}
                if m.get("cancel") else {"state": "failed", "phase": "", "error": why, "finished": now(),
                                         "machine_problem": machine})
    remove_sandbox_tmp(desk, tid)
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
    cancelled while hansa prepared it."""
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
    conflicts = desk.meta(tid).get("update_conflicts") if meta.get("update") else None
    if conflicts is not None:
        note = UPDATE_NOTE if conflicts else UPDATE_CLEAN_NOTE
        argv[argv.index("--append-system-prompt") + 1] += note.format(base=lane.get("base") or "main",
                                                                       files=", ".join(conflicts))
    try:
        check_git_file(desk, wt)  # sandbox_settings asks git in the worktree
    except GitFileChanged as e:
        raise RuntimeError(git_file_changed(desk, tid, e)) from None
    argv += isolated(wt, sandbox_settings(desk, lane, pname, p, wt))
    tmp = make_worker_tmp(desk, tid)
    argv += ["--disallowedTools", rule("Read", desk.secrets_dir), rule("Read", desk.state_dir),
             *[r for r in GIT_DENY if not (verifying and r in VERIFY_ALLOW)], *INTERACTIVE_TOOLS]
    argv += ["--allowedTools", *lane_allow(desk, lane, wt), *extra, *WORKER_ALLOW,
             *[rule(t, x) for x in full_paths([tmp]) for t in ("Read", "Edit")], *(VERIFY_ALLOW if verifying else [])]
    env = {**provider_env(desk, pname, p), **lane.get("env", {}), "HANSA_TASK": tid, "CLAUDE_CODE_TMPDIR": tmp,
           "GIT_AUTHOR_NAME": f"hansa worker {lane_name}", "GIT_AUTHOR_EMAIL": "hansa@localhost",
           "GIT_COMMITTER_NAME": f"hansa worker {lane_name}", "GIT_COMMITTER_EMAIL": "hansa@localhost"}
    d, raw = desk.tasks_dir / tid, desk.raw_dir(tid)
    if conflicts:  # merged again after the setup, with the markers left in (UPDATE_SCRIPT)
        argv = ["/bin/sh", "-c", UPDATE_SCRIPT, "hansa-update", base,
                expected_git_line(desk, wt) or "(unknown, so no git runs here)", str(raw / "run.err"), *argv]
    command = with_setup(desk, tid, argv, lane, pname, p, wt)
    with desk.lock():  # a cancel lands before this (the worker never starts) or after it (it is stopped)
        if cancelled_before_start(desk, tid):
            remove_sandbox_tmp(desk, tid)
            return None
        # the session id is on the worker's command line: a server that stops right after starting it
        # can still find it when it comes back
        desk.update(tid, launch=sid, session_id=sid, base=base, branch=f"task/{tid}", worktree=str(wt),
                    setup_ran=bool(lane.get("setup")))
        with open(d / "task.md", "rb") as stdin, open(raw / "run.json", "wb") as out, \
                open(raw / "run.err", "wb") as err:
            proc = subprocess.Popen(command, cwd=str(wt), env=env, stdin=stdin, stdout=out,
                                    stderr=err, start_new_session=True)
        desk.update(tid, phase="", model_asked=model, effort_used=effort, pid=proc.pid,
                    pid_start=proc_start(proc.pid))
    desk.event("task_started", task=tid, lane=lane_name, provider=pname, model=model)
    return proc


def design_command(desk: Desk, tid: str):
    """The interactive Claude Code run for a design session: (argv, env, directory).
    Built inside the session's own terminal (hansa.session), so no key is on a command line."""
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
    argv += isolated(wt)
    argv += ["--append-system-prompt", system,
             "--disallowedTools", rule("Read", desk.secrets_dir), rule("Read", desk.state_dir),
             "--allowedTools", *lane_allow(desk, lane, wt), *extra, *DESIGN_ALLOW]
    env = {**provider_env(desk, pname, p), **lane.get("env", {}), "HANSA_TASK": tid,
           "TERM": os.environ.get("TERM") or "xterm-256color",  # a terminal the principal types into
           "GIT_AUTHOR_NAME": f"hansa design {lane_name}", "GIT_AUTHOR_EMAIL": "hansa@localhost",
           "GIT_COMMITTER_NAME": f"hansa design {lane_name}", "GIT_COMMITTER_EMAIL": "hansa@localhost"}
    return with_setup(desk, tid, argv, lane, pname, p, wt), env, wt


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
    if lane.get("setup"):
        srt_bin()  # the session runs the lane's setup in srt, so it could not start without it
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
    # -P prints the pane's process id once the session exists: tmux may exit 0 without making it
    # (when it cannot create its socket, say), and a session that ends at once still printed it
    r = subprocess.run([tmux, "-L", sock, "new-session", "-d", "-P", "-F", "#{pane_pid}", "-s", name,
                        "-x", "200", "-y", "50", "-c", str(wt), sys.executable, "-m", "hansa.session", str(desk.root), tid],
                       env=env, capture_output=True, text=True, timeout=30)
    if r.returncode or not r.stdout.strip().isdigit():
        said = " ".join((r.stdout + r.stderr).split())[:300] or "nothing"
        raise RuntimeError(f"tmux did not open the design session; it printed: {said}")
    pid = int(r.stdout.strip())
    meta = desk.update(tid, phase="", pid=pid, pid_start=proc_start(pid))
    ask = desk.add_ask(f"Design session {tid} is waiting for you: {meta['title']}. Join it on this machine with "
                       f"`{join_command(meta)}`; leave it running with Ctrl-b then d, end it with /exit (if it "
                       f"then asks about background work, press Enter: the first choice stops it). The first "
                       f"session in a new folder asks whether you trust it: choose \"Yes, I trust this folder\" "
                       f"(the default, No, ends the session). "
                       f"Its screenshots show under {tid} on this page.", by="hansa")
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
        desk.close_ask(meta["ask"], "the design session ended", by="hansa")


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


def refusal_reasons(home: Path, sid: str) -> dict:
    """What Claude Code told the worker when it refused a call, from the session record: {tool use id: the
    error text of its tool result} ("Contains expansion", say). Empty when the record cannot be read."""
    out = {}
    for d in _rows(home, sid) or []:
        content = (d.get("message") or {}).get("content") if d.get("type") == "user" else None
        for c in content if isinstance(content, list) else []:
            if not (isinstance(c, dict) and c.get("type") == "tool_result" and c.get("is_error") and c.get("tool_use_id")):
                continue
            said = c.get("content")
            if isinstance(said, list):
                said = " ".join(x.get("text", "") for x in said if isinstance(x, dict))
            said = " ".join(str(said or "").replace("<tool_use_error>", "").replace("</tool_use_error>", "").split())
            if said:
                out[c["tool_use_id"]] = said[:300]
    return out


def unusable_reason(run: dict, err: str):
    """Why this provider cannot be used for this run: ("quota" | "key" | "login" | "refused" | "", words)."""
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
    m = AUTH.search(text)  # before LOGIN: "Invalid API key · Please run /login" is a key problem
    if m:
        return "key", f"key rejected ({m.group(0)})"
    if LOGIN.search(text):
        return "login", "not logged in (Please run /login)"
    return "", ""


def finish(desk: Desk, tid: str, returncode) -> dict:
    meta = desk.meta(tid)
    d = desk.tasks_dir / tid
    done = desk.state_dir / "runs" / tid / "setup.log.done"
    set_up = not meta.get("setup_ran") or done.exists()  # else the worker never started: nothing is its work
    done.unlink(missing_ok=True)
    remove_sandbox_tmp(desk, tid)
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
    late = report_after_notices(config_home(desk, meta["provider"], p), meta.get("session_id", "")) \
        if p and not designing and not stopped else None
    if late:
        report = late[0] + (LATE_NOTE + late[1] if late[1] else "")
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
    denied = run.get("permission_denials") or []
    reasons = refusal_reasons(config_home(desk, meta["provider"], p), meta.get("session_id", "")) \
        if p and denied else {}
    (d / "result.md").write_text(report + ("\n" if report else ""), encoding="utf-8")
    fields = {"finished": now(), "exit": returncode, "pid": None,
              "session_id": run.get("session_id") or meta.get("session_id", ""),
              "turns": run.get("num_turns"), "subtype": run.get("subtype", ""),
              "usage": usage, "denials": [desk.redact(denial_of(x)) for x in denied],
              "denial_reasons": [desk.redact(reasons.get(x.get("tool_use_id"), "") if isinstance(x, dict) else "")
                                 for x in denied],
              "background_left": [desk.redact(x) for x in background_left(config_home(desk, meta["provider"], p),
                                                                          meta.get("session_id", ""))]
              if p and not designing else []}
    if meta.get("kind") == "verify":
        found = VERDICT.findall(report if late else result)
        fields["verdict"] = found[-1].lower() if found else ""
    wt = Path(meta["worktree"]) if meta.get("worktree") else None
    changed = ""  # what the principal is told when the worktree's .git was changed: then the task fails
    try:
        if wt is None:
            raise RuntimeError("the task never got a worktree")
        check_git_file(desk, wt, desk.settings()["lanes"].get(meta.get("lane"), {}).get("repo"))
        if wgit(desk, wt, "status", "--porcelain") and set_up and not setup:  # a setup's leftovers are nobody's work
            if meta.get("kind") == "verify":  # its scratch changes are evidence, not work: kept aside
                wgit(desk, wt, "stash", "push", "--include-untracked", "-m", f"hansa: {tid} scratch changes")
            else:
                commit_work(desk, wt, f"{tid}: {meta.get('title') or 'work'}"[:100])
        head = wgit(desk, wt, "rev-parse", "HEAD")
        fields.update(head=head, commits=int(wgit(desk, wt, "rev-list", "--count", f"{meta['base']}..{head}") or 0),
                      files=wgit(desk, wt, "diff", "--name-only", meta["base"], head).splitlines(),
                      stat=wgit(desk, wt, "diff", "--shortstat", meta["base"], head))
        check_git_file(desk, wt)  # keys_in_change runs git there too
        fields["leaked"] = keys_in_change(desk, wt, meta["base"], head)  # it would travel with the merge
        if fields["leaked"]:
            desk.event("key_in_change", task=tid, providers=fields["leaked"])
        fields["markers"] = markers_in_change(wt, meta["base"], head)  # a conflict left unsettled
        if fields["markers"]:
            desk.event("conflict_markers_in_change", task=tid, files=sorted(fields["markers"]))
    except GitFileChanged as e:
        changed = git_file_changed(desk, tid, e)
        fields.update(head="", commits=0, files=[], stat="")
    except (RuntimeError, KeyError, OSError) as e:
        fields.update(head="", commits=0, files=[], stat="", git_error=str(e)[:300])

    attempts = list(meta.get("attempts", []))
    if setup:
        fields["setup_exit"] = int(setup.group(1))
    kind, why = ("", "") if meta.get("cancel") or setup or changed else unusable_reason(run, err)
    if changed:
        state, error = "failed", changed
    elif meta.get("cancel"):
        state, error = "cancelled", "cancelled"
    elif setup:
        state, error = "failed", (f"the lane's setup command failed (exit {setup.group(1)}), so the worker never "
                                  f"started; hansa show {tid} has the setup's last lines")
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
        final, message = ("cancelled", "cancelled") if now_meta.get("cancel") and not changed else (state, error)
        checking = (final == "done" and bool(lane.get("check")) and bool(fields.get("commits"))
                    and meta.get("kind") != "verify")  # a verification is never merged, so never checked
        return {"state": "running" if checking else final, "phase": "checking" if checking else "",
                "error": desk.redact(message), "attempts": attempts, "pid_start": "", **fields}
    meta = desk.change(tid, decide)
    desk.event("task_finished", task=tid, state="checking" if meta.get("phase") == "checking" else meta["state"],
               provider=meta.get("provider"), error=meta["error"])
    if meta.get("phase") != "checking":  # else after its check, which carries the same id
        sweep(desk, tid)
        meta = desk.meta(tid)
    return meta


def start_check(desk: Desk, tid: str, lane_name: str, lane: dict):
    """Run the lane's check command in the worktree, in srt with the lane's sandbox, after the worker is
    done; None when the task was cancelled first. The exit code goes to a file, so a restarted server
    can still read it."""
    raw = desk.raw_dir(tid)
    (raw / "check.exit").unlink(missing_ok=True)
    token = f"hansa-check-{tid}-{uuid.uuid4().hex[:8]}"  # on its command line, for a restarted server
    with desk.lock():  # a cancel lands before this (no check) or after it (the check is stopped)
        meta = desk.meta(tid)
        if meta.get("cancel"):
            return None
        wt, pname = Path(meta.get("worktree") or desk.worktrees_dir / lane_name), meta.get("provider", "")
        srt, settings, tmp = in_srt(desk, tid, "check", lane, pname, desk.settings()["providers"].get(pname) or {}, wt)
        env = {k: os.environ[k] for k in BASE_ENV if k in os.environ}
        env.update({**lane.get("env", {}), "HANSA_TASK": tid, "TERM": "dumb", "TMPDIR": tmp, "CLAUDE_CODE_TMPDIR": tmp})
        desk.update(tid, launch=token, check_started=now(), check_command=lane["check"])
        with open(raw / "check.log", "wb") as out:
            proc = subprocess.Popen(["/bin/sh", "-c", CHECK_SCRIPT, token, lane["check"], str(raw / "check.exit"), tmp,
                                     srt, settings], cwd=str(wt), env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
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
    remove_sandbox_tmp(desk, tid)
    publish(desk, tid, "check.log")  # what the check printed can hold a key the change holds
    log = (d / "check.log").read_text(encoding="utf-8", errors="replace") if (d / "check.log").exists() else ""
    note = ("stopped on request" if m.get("cancel") else "stopped at the lane's time ceiling"
            if m.get("timed_out") else "" if code is not None else "the check was interrupted")
    changed = ""  # the check's sandbox keeps the worktree's .git file, and hansa makes sure
    if m.get("worktree") and expected_git_line(desk, Path(m["worktree"])):
        try:
            check_git_file(desk, Path(m["worktree"]))
        except GitFileChanged as e:
            changed = git_file_changed(desk, tid, e)
    check ={"command": m.get("check_command", ""), "ok": code == 0 and not note, "exit": code, "note": note,
             "seconds": round(now() - (m.get("check_started") or now())), "tail": log.strip().splitlines()[-15:]}
    m = desk.change(tid, lambda now_meta: {"state": "failed" if changed else "cancelled" if now_meta.get("cancel")
                                           else "done", "phase": "", "pid": None, "pid_start": "", "check": check,
                                           "error": changed or ("cancelled" if now_meta.get("cancel") else "")})
    desk.event("task_checked", task=tid, ok=check["ok"], exit=code, note=note)
    sweep(desk, tid)
    return desk.meta(tid)


# A merge check: `hansa merge` of a task whose base has moved since it was made, on a lane with a check,
# or `hansa merge --recheck` of any task on such a lane (merge_check {recheck: True, by: who asked}).
# The task is "merging" meanwhile, with merge_check {phase: queued | starting | running, ...}; hansa serve
# runs it when the lane is free, in the lane's worktree, and it ends in "merged", or back in "done" with
# merge_check_result saying why nothing was merged.
MERGE_FILES = ("merge-check.log", "merge-setup.log", "merge-check.err")
MERGE_MOVES = 2  # the base moved this often while its merge was being checked: hansa stops trying


def base_moved(repo, tip: str, head: str) -> bool:
    """Whether the base (at tip) has commits the task's branch (at head) does not."""
    r = subprocess.run(["git", "merge-base", "--is-ancestor", tip, head], cwd=str(repo), capture_output=True)
    return r.returncode != 0


def lane_occupied(m: dict) -> bool:
    """Whether this task holds its lane's worktree now: a worker or check on it, or its merge check."""
    return (m["state"] == "running" and m.get("kind") != "design") or \
        (m["state"] == "merging" and (m.get("merge_check") or {}).get("phase") in ("starting", "running"))


def close_verification(desk: Desk, m: dict, why: str, by=None) -> None:
    """A verification belongs to the task it checks, and ends with it. by: the session whose hansa cancel closed
    it, which hears nothing of it."""
    if not m.get("verify"):
        return
    v = desk.meta(m["verify"])
    if v["state"] in ("queued", "done"):
        desk.update(v["id"], state="cancelled", error=why, cancel_by=by)
    elif v["state"] == "running":  # hansa serve stops it within seconds
        desk.update(v["id"], cancel=True, cancel_by=by)


TEXT_SUFFIXES = (".md", ".markdown", ".txt", ".rst", ".adoc", ".org")
TEXT_NAMES = ("LICENSE", "LICENCE", "README", "CHANGELOG", "NOTICE", "AUTHORS", "COPYING", "CONTRIBUTING")


def text_only(path: str) -> bool:
    """A file of text or documentation, which a verification has nothing to run in: by its kind, wherever it is
    (a .py under docs/ is code)."""
    p = Path(path)
    suffix = p.suffix.lower()
    return suffix in TEXT_SUFFIXES or (not suffix and p.name.upper() in TEXT_NAMES)

def unverified_note(desk: Desk, m: dict) -> str:
    """For a merge: that nothing verified a change beyond text and documentation, without the leading 'note: ';
    '' when its verification holds, or it changed only text."""
    def holds(t):
        v = desk.meta(t["verify"]) if t and t.get("verify") else None
        return v if v and v.get("verdict") == "holds" else None
    code = [f for f in m.get("files") or [] if not text_only(f)]
    if not code or holds(m):
        return ""
    what = f"{m['id']} changed {', '.join(code[:3])}" + (f" and {len(code) - 3} more" if len(code) > 3 else "")
    try:
        pm = desk.meta(m["parent"]) if m.get("parent") else None
    except SystemExit:
        pm = None
    pv = holds(pm)
    if pv:
        return (f"{what}, and no verification of {m['id']} held; {pv['id']} verified {pm['id']}, the attempt it "
                f"retries, not this one")
    return (f"nothing verified this change: {what}, and "
            + (f"neither it nor {pm['id']}, the attempt it retries, has a verification that holds" if pm
               else "no verification of it holds"))


def end_merge_check(desk: Desk, tid: str, result: dict | None) -> dict | None:
    """Nothing was merged: the task goes back to waiting for review, with the result saying why."""
    m = desk.change(tid, lambda x: None if x["state"] != "merging" else
                    {"state": "done", "merge_check": None, "merge_check_result": result})
    if m:
        desk.event("merge_check_ended", task=tid, ok=False, note=(result or {}).get("note", "cancelled"))
    return m


def start_merge_check(desk: Desk, tid: str, lane_name: str, lane: dict):
    """Merge a task onto the base's current tip in the lane's worktree, then run the lane's setup (if it has
    one) and check there, in srt, as after a task. Returns the process; None when it did not start
    (cancelled meanwhile), or ended at once (a conflict; or no check any more, and so merged)."""
    raw = desk.raw_dir(tid)
    for f in ("merge-check.exit", "merge-setup.log.done", *MERGE_FILES):
        (raw / f).unlink(missing_ok=True)
    token = f"hansa-merge-{tid}-{uuid.uuid4().hex[:8]}"  # on its command line, for a restarted server
    with desk.lock():  # a cancel lands before this (no check) or after it (the check is stopped)
        m = desk.meta(tid)
        mc = m.get("merge_check") or {}
        if m["state"] != "merging" or mc.get("phase") != "queued":
            return None
        repo, name = Path(lane["repo"]), lane.get("base") or "main"
        wt = desk.worktrees_dir / lane_name
        tip = git("rev-parse", name, cwd=repo)
        ready_worktree(desk, repo, wt, tid, tip)
        try:
            wgit(desk, wt, "checkout", "-q", "--detach", tip)
            wgit(desk, wt, "merge", "--no-ff", "--no-verify", "-m", f"Merge {tid}: {m['title']}", m["head"])
        except GitFileChanged as e:
            raise RuntimeError(git_file_changed(desk, tid, e)) from None
        except RuntimeError as e:
            files = conflicted_files(lambda *a: wgit(desk, wt, *a))
            places = desk.redact(conflict_places(wt, files)) if files else ""
            if merge_in_progress(desk, wt):
                wgit(desk, wt, "merge", "--abort")
            end_merge_check(desk, tid, {"ok": False, "onto": tip, "at": now(), "files": files,
                                        "note": f"conflicts with {name} as it is now",
                                        "conflict": desk.redact(conflict_advice(tid, name, places, str(e)[:300]))})
            return None
        commit = wgit(desk, wt, "rev-parse", "HEAD")
        if not lane.get("check"):  # the principal took the check away meanwhile: nothing to run
            land_merge(desk, tid, {"ok": True, "onto": tip, "commit": commit, "command": "", "note": "",
                                   "at": now(), "seconds": 0, "tail": []})
            return None
        pname = m.get("provider", "")
        p = desk.settings()["providers"].get(pname) or {}
        srt, settings, tmp = in_srt(desk, tid, "check", lane, pname, p, wt)
        argv = ["/bin/sh", "-c", CHECK_SCRIPT, token, lane["check"], str(raw / "merge-check.exit"), tmp, srt, settings]
        if lane.get("setup"):
            ssrt, ssettings, stmp = in_srt(desk, tid, "setup", lane, pname, p, wt)
            argv = ["/bin/sh", "-c", SETUP_SCRIPT, token, lane["setup"], str(raw / "merge-setup.log"),
                    str(raw / "merge-check.err"), expected_git_line(desk, wt) or "(unknown, so no git runs here)",
                    stmp, ssrt, ssettings, *argv]
        env = {k: os.environ[k] for k in BASE_ENV if k in os.environ}
        env.update({**lane.get("env", {}), "HANSA_TASK": tid, "TERM": "dumb", "TMPDIR": tmp, "CLAUDE_CODE_TMPDIR": tmp})
        mc = {**mc, "phase": "starting", "launch": token, "onto": tip, "commit": commit, "command": lane["check"],
              "setup": lane.get("setup", ""), "started": now(), "worktree": str(wt)}
        desk.update(tid, merge_check=mc)
        with open(raw / "merge-check.log", "wb") as out:
            proc = subprocess.Popen(argv, cwd=str(wt), env=env, stdin=subprocess.DEVNULL, stdout=out,
                                    stderr=subprocess.STDOUT, start_new_session=True)
        desk.update(tid, merge_check={**mc, "phase": "running", "pid": proc.pid, "pid_start": proc_start(proc.pid)})
    desk.event("merge_check_started", task=tid, lane=lane_name, onto=tip)
    return proc


def finish_merge_check(desk: Desk, tid: str) -> dict | None:
    """After a merge check's process ended: merge, check again, or send the task back for review."""
    m = desk.meta(tid)
    mc = m.get("merge_check") or {}
    if m["state"] != "merging" or mc.get("phase") not in ("starting", "running"):
        return None
    raw, d = desk.state_dir / "runs" / tid, desk.tasks_dir / tid
    try:
        code = int((raw / "merge-check.exit").read_text().strip())
    except (OSError, ValueError):
        code = None
    (raw / "merge-check.exit").unlink(missing_ok=True)
    (raw / "merge-setup.log.done").unlink(missing_ok=True)
    remove_sandbox_tmp(desk, tid)
    publish(desk, tid, *MERGE_FILES)  # what the check printed can hold a key the change holds
    sweep(desk, tid)  # what the merge check left running

    def read(name):
        return (d / name).read_text(encoding="utf-8", errors="replace") if (d / name).exists() else ""
    err = read("merge-check.err").strip()
    setup = SETUP_FAILED.search(err)
    note = ("stopped on request" if mc.get("cancel") else "stopped at the lane's time ceiling" if mc.get("timed_out")
            else f"the lane's setup failed (exit {setup.group(1)}), so the check did not run" if setup
            else "" if code is not None else (err.splitlines()[-1] if err else "the check was interrupted"))
    if mc.get("worktree") and expected_git_line(desk, Path(mc["worktree"])):
        try:
            check_git_file(desk, Path(mc["worktree"]))
        except GitFileChanged as e:
            note = git_file_changed(desk, tid, e)
    result = {"ok": code == 0 and not note, "exit": code, "note": note, "command": mc.get("command", ""),
              "onto": mc.get("onto", ""), "commit": mc.get("commit", ""), "at": now(),
              "seconds": round(now() - (mc.get("started") or now())),
              "tail": read("merge-setup.log" if setup else "merge-check.log").strip().splitlines()[-15:]}
    desk.event("merge_checked", task=tid, ok=result["ok"], exit=code, note=note,
               **({"recheck": True, "by": mc.get("by", "")} if mc.get("recheck") else {}))
    if mc.get("cancel"):
        return end_merge_check(desk, tid, None)
    if not result["ok"]:
        return end_merge_check(desk, tid, result)
    return land_merge(desk, tid, result)


def land_merge(desk: Desk, tid: str, result: dict) -> dict | None:
    """Move the base to exactly the merge commit that passed, and only if the base is still where it was merged
    onto; otherwise check once more on the new tip, and after the base's second move, stop."""
    with desk.lock():  # a merge, a cancel or a key saved meanwhile lands before this or after it
        m = desk.meta(tid)
        mc = m.get("merge_check") or {}
        if m["state"] != "merging":
            return None
        lane = desk.settings()["lanes"].get(m.get("lane"))
        if not lane:
            return end_merge_check(desk, tid, {**result, "ok": False, "note": f"lane {m.get('lane')} is no longer in "
                                                                              "settings; nothing was merged"})
        repo, name = lane["repo"], lane.get("base") or "main"
        tip = git("rev-parse", name, cwd=repo)
        if tip != result["onto"]:
            moves = mc.get("moves", 0) + 1
            desk.event("merge_check_base_moved", task=tid, onto=result["onto"], now=tip, moves=moves)
            if moves >= MERGE_MOVES:
                return end_merge_check(desk, tid, {**result, "ok": False, "note": (
                    f"its merge passed the check twice, but {name} moved each time before hansa could merge it; "
                    f"nothing was merged. Merge it again when {name} is quiet: hansa merge {tid}")})
            desk.update(tid, merge_check={"phase": "queued", "queued": mc.get("queued"), "moves": moves,
                                          "passed_onto": result["onto"],
                                          **({"recheck": True, "by": mc.get("by", "")} if mc.get("recheck") else {})})
            return desk.meta(tid)
        why = ""
        current = git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo)
        if current != name:
            why = f"the checkout at {repo} is on {current}, not {name}; the principal needs to switch it back"
        elif git("status", "--porcelain", "--untracked-files=no", cwd=repo):
            why = f"the checkout at {repo} has uncommitted changes"
        else:
            leaked = keys_in_change(desk, repo, m["base"], m["head"])  # a key saved meanwhile counts too
            if leaked:
                desk.update(tid, leaked=leaked)
                why = f"the change holds the key of {', '.join(leaked)}"
        if not why:
            try:
                git("merge", "--ff-only", "-q", result["commit"], cwd=repo, env={"HANSA_MERGING": "1"})
            except RuntimeError as e:
                why = f"moving {name} to the checked merge failed ({str(e)[:200]})"
        if why:
            return end_merge_check(desk, tid, {**result, "ok": False, "note": f"its merge passed the check, but {why}; "
                                                                              "nothing was merged"})
        m = desk.update(tid, state="merged", merged=time.time(), merge_commit=result["commit"], merge_check=None,
                        merge_check_result=result, unverified=unverified_note(desk, m) or None)
        close_verification(desk, m, f"closed: {tid} was merged")
    desk.event("task_merged", task=tid, into=name, merge_checked=True, onto=result["onto"],
               **({"recheck": True, "by": mc.get("by", "")} if mc.get("recheck") else {}))
    return m


# A try: `hansa lane try L` runs the lane's setup and check once, with no worker, to see that they work where
# tasks run. hansa serve runs it (with the server's environment), when the lane is free, in the lane's worktree
# detached at the base's tip, and puts the worktree back clean at that tip after. .hansa/tries/<lane>.json holds
# the try: {id, phase: queued | running | done, ...}, and when done its result, which the command waits for.
TRY_STEPS = ("setup", "check")


def try_file(desk: Desk, lane_name: str) -> Path:
    return desk.state_dir / "tries" / f"{lane_name}.json"


def read_try(desk: Desk, lane_name: str) -> dict:
    return read_json(try_file(desk, lane_name), {}) or {}


def write_try(desk: Desk, lane_name: str, t: dict) -> None:
    try_file(desk, lane_name).parent.mkdir(parents=True, exist_ok=True)
    write_json(try_file(desk, lane_name), t)


def tried_lanes(desk: Desk) -> set:
    """Lanes with a try queued or running: no task or merge check starts there meanwhile."""
    d = desk.state_dir / "tries"
    return {f.stem for f in (d.glob("*.json") if d.is_dir() else [])
            if (read_json(f, {}) or {}).get("phase") in ("queued", "running")}


def lane_holder(desk: Desk, lane_name: str):
    """The task holding the lane's worktree now, if any."""
    return next((m for m in desk.tasks() if m.get("lane") == lane_name and lane_occupied(m)), None)


def end_try(desk: Desk, lane_name: str, result: dict) -> dict:
    t = {**read_try(desk, lane_name), "phase": "done", "at": now(), **result}
    for k in ("pid", "pid_start", "tmp", "launch"):
        t.pop(k, None)
    write_try(desk, lane_name, t)
    desk.event("lane_tried", lane=lane_name, ok=t.get("ok", False), note=t.get("note", ""), onto=t.get("onto", ""),
               **{s: {"exit": x.get("exit"), "seconds": x.get("seconds")} for s, x in t.get("steps", {}).items()})
    return t


def start_try(desk: Desk, lane_name: str, lane: dict):
    """Start a queued try of the lane; None when there is none, or it ended at once (the lane is busy)."""
    d = desk.state_dir / "tries" / lane_name
    tid = f"try-{lane_name}"  # where a changed .git file found here is kept (git_file_changed)
    with desk.lock():  # no task starts on the lane meanwhile
        t = read_try(desk, lane_name)
        if t.get("phase") != "queued":
            return None
        holder = lane_holder(desk, lane_name)
        if holder:
            end_try(desk, lane_name, {"ok": False, "busy": holder["id"], "steps": {},
                                      "note": f"lane {lane_name} is busy with {holder['id']}; nothing was run"})
            return None
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True)
        repo, name = Path(lane["repo"]), lane.get("base") or "main"
        wt = desk.worktrees_dir / lane_name
        tip = git("rev-parse", name, cwd=repo)
        ready_worktree(desk, repo, wt, tid, tip)
        try:
            wgit(desk, wt, "checkout", "-q", "--detach", tip)
        except GitFileChanged as e:
            raise RuntimeError(git_file_changed(desk, tid, e)) from None
        s = desk.settings()
        pname = (lane.get("providers") or [""])[0]  # the sandbox of the lane's first provider, as a task's
        p = s["providers"].get(pname) or {}
        token, args, tmps = f"hansa-try-{lane_name}-{uuid.uuid4().hex[:8]}", [], []
        for step in TRY_STEPS:
            if lane.get(step):
                tmp = tempfile.mkdtemp(prefix=f"hansa-{step}-")
                write_json(d / f"{step}-sandbox.json", srt_settings(desk, lane, pname, p, wt, tmp))
                args += [step, lane[step], str(d / f"{step}-sandbox.json"), tmp]
                tmps.append(tmp)
        env = {k: os.environ[k] for k in BASE_ENV if k in os.environ}
        env.update({**lane.get("env", {}), "TERM": "dumb"})
        try:  # srt first, with the first step's settings (as in_srt): when it cannot run, nothing runs
            srt = try_srt(desk, args[2], args[3], wt, env) if args else srt_bin()
        except SrtCannotRun as e:
            for x in tmps:
                shutil.rmtree(x, ignore_errors=True)
            shutil.rmtree(d, ignore_errors=True)
            end_try(desk, lane_name, {"ok": False, "could_not_run": True, "steps": {}, "onto": tip,
                                      "note": desk.redact(f"could not run: {e}")[:600]})
            return None
        t = {**t, "phase": "running", "launch": token, "onto": tip, "worktree": str(wt), "started": now(),
             "tmp": tmps, **{step: lane.get(step, "") for step in TRY_STEPS}}
        write_try(desk, lane_name, t)
        with open(d / "try.err", "wb") as err:
            proc = subprocess.Popen(["/bin/sh", "-c", TRY_SCRIPT, token, str(d), srt, *args], cwd=str(wt), env=env,
                                    stdin=subprocess.DEVNULL, stdout=err, stderr=subprocess.STDOUT,
                                    start_new_session=True)
        write_try(desk, lane_name, {**t, "pid": proc.pid, "pid_start": proc_start(proc.pid)})
    desk.event("lane_try_started", lane=lane_name, onto=tip)
    return proc


def finish_try(desk: Desk, lane_name: str) -> dict:
    """After a try's process ended: read what each step did, keys removed, and put the worktree back clean at
    the base's tip it ran on."""
    t = read_try(desk, lane_name)
    d = desk.state_dir / "tries" / lane_name
    steps, failed = {}, ""
    for step in TRY_STEPS:
        if not t.get(step):
            continue
        try:
            code, secs = map(int, (d / f"{step}.exit").read_text().split())
        except (OSError, ValueError):
            code = secs = None
        log = (d / f"{step}.log").read_text(encoding="utf-8", errors="replace") if (d / f"{step}.log").exists() else ""
        ran = code is not None or (d / f"{step}.log").exists()
        steps[step] = {"command": t[step], "ran": ran, "exit": code, "seconds": secs,
                       "tail": desk.redact(log).strip().splitlines()[-15:]}
        if ran and code != 0 and not failed:
            failed = step
    for x in t.get("tmp", []):
        shutil.rmtree(x, ignore_errors=True)
    note = ("stopped at the lane's time ceiling" if t.get("timed_out") else
            f"the {failed} was interrupted" if failed and steps[failed]["exit"] is None else "")
    wt, clean = Path(t.get("worktree") or desk.worktrees_dir / lane_name), False
    try:
        wgit(desk, wt, "reset", "-q", "--hard", t.get("onto") or "HEAD")
        wgit(desk, wt, "clean", "-fdq")
        clean = True
    except GitFileChanged as e:
        note = git_file_changed(desk, f"try-{lane_name}", e)
    except RuntimeError as e:
        note = note or desk.redact(f"the worktree could not be put back clean: {e}")[:300]
    shutil.rmtree(d, ignore_errors=True)
    ok = bool(steps) and all(x["exit"] == 0 for x in steps.values()) and not note
    return end_try(desk, lane_name, {"ok": ok, "steps": steps, "note": note, "clean": clean})


def proc_start(pid) -> str:
    """When a process started, to tell it from a later one given the same number; '' if unknown."""
    if not pid:
        return ""
    try:
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return boot + ":" + Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        pass
    return _mac_start(pid) if _ON_MAC else ""


def alive(pid, start: str = "") -> bool:
    """Whether this process is still running, and, when its start is known, still the same one."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    if any(c.isspace() for c in start):  # ps's start text, saved before hansa read it with sysctl
        start = ""
    seen = proc_start(pid) if start else ""
    return not seen or seen == start  # when its start cannot be read now, assume it is the same process


def _state(pid) -> str:
    """A process's state letter (Z: ended, waiting to be reaped); '' if unknown."""
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        pass
    return _mac_state(pid) if _ON_MAC else ""


def has_ended(proc) -> bool:
    """Whether hansa's child has ended, found without reaping it: until it is reaped, its number,
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
    return _mac_group_running(pgid)


def stop(pid, start: str = "", proc=None) -> None:
    """Stop the process group hansa started, politely and then for good, until nothing in it runs,
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
        self.merge_procs = {}  # merge checks this server started
        self.try_procs = {}  # tries (hansa lane try) this server started, by lane

    def ended(self, tid: str, returncode) -> None:
        if self.desk.meta(tid).get("phase") == "checking":
            finish_check(self.desk, tid)
        else:
            finish(self.desk, tid, returncode)

    def look(self, tid: str, launch: str, launched, since, what: str):
        """The process an earlier server started with this launch word, made at the time `launched`:
        (pid or None, None) when hansa could look; (None, since) when it could not, with since the time
        it first could not, which the caller keeps. Each time it could not is in the event log."""
        try:
            return leader(pids_with_arg(launch, launched)), None
        except CannotLook as e:
            since = since or now()
            self.desk.event("process_lookup_failed", task=tid, looking_for=what, error=str(e),
                            seconds=round(time.time() - since))
            return None, since

    def gave_up(self, since) -> str:
        """Once hansa has not been able to look for LOOK_LIMIT seconds: why the task ends; '' before."""
        if time.time() - since < LOOK_LIMIT:
            return ""
        return (f"hansa could not read the process list for {LOOK_LIMIT // 60} minutes, so it could not tell "
                "whether what an earlier server started still runs; it was not started a second time")

    def recover(self, m: dict) -> None:
        """A task an earlier server claimed but did not record as started: a worker it did start
        is taken over; otherwise the task goes back to the queue, or ends there if cancelled. While
        hansa cannot look for that worker, the task stays as it is, and fails after LOOK_LIMIT."""
        found, since = self.look(m["id"], m.get("launch", ""), m.get("started"), m.get("lookup_failing_since"),
                                  "worker") \
            if m.get("kind") != "design" else (None, None)
        if since:
            why = self.gave_up(since)
            if self.desk.change(m["id"], lambda x: None if x["state"] != "running" or x.get("phase") != "starting" else
                                {"lookup_failing_since": since} if not why else
                                {"state": "failed", "phase": "", "error": why, "finished": now(),
                                 "lookup_failing_since": None}) and why:
                remove_sandbox_tmp(self.desk, m["id"])
            return
        if found:
            self.desk.update(m["id"], phase="", pid=found, pid_start=proc_start(found), lookup_failing_since=None)
            return
        if m.get("tmux"):  # a design session it may have opened, which nobody was asked to join yet
            try:
                subprocess.run([tmux_bin(), "-L", m["tmux"][0], "kill-session", "-t", m["tmux"][1]],
                               capture_output=True, timeout=30)
            except (OSError, RuntimeError, subprocess.SubprocessError):
                pass
        if self.desk.change(m["id"], lambda x: None if x["state"] != "running" or x.get("phase") != "starting" else
                            {"state": "cancelled", "phase": "", "error": "cancelled before it started",
                             "finished": now()} if x.get("cancel") else
                            {"state": "queued", "phase": "", "pid": None, "pid_start": "", "tmux": None,
                             "lookup_failing_since": None}):
            remove_sandbox_tmp(self.desk, m["id"])  # what it made for a worker that never started

    def check(self, m: dict, s: dict) -> None:
        """Run the lane's check on a task whose worker is done: or take over the one an earlier
        server started, or finish the one it left finished."""
        desk, tid = self.desk, m["id"]
        found, since = self.look(tid, m.get("launch", ""), m.get("check_started"), m.get("lookup_failing_since"),
                                "check")
        if since:  # not run a second time beside one that may still run: looked for again next tick
            why = self.gave_up(since)
            desk.change(tid, lambda x: None if x["state"] != "running" or x.get("phase") != "checking" or x.get("pid")
                        else {"lookup_failing_since": since} if not why else
                        {"state": "failed", "phase": "", "error": why, "lookup_failing_since": None})
            return
        if m.get("lookup_failing_since"):
            desk.update(tid, lookup_failing_since=None)
        if found:
            desk.update(tid, pid=found, pid_start=proc_start(found))
            return
        if (desk.state_dir / "runs" / tid / "check.exit").exists():
            finish_check(desk, tid)
            return
        lane = s["lanes"].get(m.get("lane"), {})
        try:
            proc = start_check(desk, tid, m["lane"], lane) if lane.get("check") else None
        except SrtCannotRun as e:  # the machine's problem: the task is not judged on a check that never ran
            said = desk.redact(str(e))[:600]
            check = {"command": lane.get("check", ""), "ok": None, "could_not_run": True, "exit": None,
                     "note": f"could not run: {said}", "seconds": 0, "tail": []}
            remove_sandbox_tmp(desk, tid)
            publish(desk, tid, "check.log")
            desk.change(tid, lambda x: {"state": "cancelled", "phase": "", "pid": None, "pid_start": "", "error": "cancelled",
                                        "finished": now()} if x.get("cancel") else
                        {"state": "done", "phase": "", "pid": None, "pid_start": "", "check": check})
            desk.event("task_checked", task=tid, ok=None, exit=None, note=check["note"], could_not_run=True)
            sweep(desk, tid)
            return
        except Exception as e:  # noqa: BLE001 - say: the check cannot run, so the task fails
            why = desk.redact(f"the lane's check could not run: {e}")[:600]
            remove_sandbox_tmp(desk, tid)
            publish(desk, tid, "check.log")
            desk.change(tid, lambda x: {"state": "failed", "phase": "", "pid": None, "pid_start": "", "error": why})
            desk.event("task_checked", task=tid, ok=False, exit=None, note=why)
            sweep(desk, tid)
            return
        if proc:
            self.procs[tid] = proc
            return
        publish(desk, tid, "check.log")  # cancelled first, or the principal removed the check meanwhile
        desk.change(tid, lambda x: {"state": "cancelled", "phase": "", "error": "cancelled", "finished": now()}
                    if x.get("cancel") else {"state": "done", "phase": ""})
        sweep(desk, tid)

    def merge_checks(self, s: dict) -> None:
        """Finish merge checks that ended, stop those cancelled or over the lane's time, take over or finish
        those an earlier server left, and start queued ones on free lanes (also while dispatch is paused:
        they use no provider)."""
        desk = self.desk
        for tid, proc in list(self.merge_procs.items()):
            if has_ended(proc):
                del self.merge_procs[tid]
                stop(proc.pid, "", proc)
                finish_merge_check(desk, tid)
        for m in desk.tasks():
            mc = m.get("merge_check") or {}
            if m["state"] != "merging" or m["id"] in self.merge_procs or mc.get("phase") not in ("starting", "running"):
                continue
            found, since = self.look(m["id"], mc.get("launch", ""), mc.get("started"),
                                     mc.get("lookup_failing_since"), "merge check")
            if since and mc.get("phase") == "starting":  # not started a second time: looked for again next tick
                why = self.gave_up(since)
                if why:
                    end_merge_check(desk, m["id"], {"ok": False, "at": now(), "note": why})
                else:
                    desk.change(m["id"], lambda x: {"merge_check": {**x["merge_check"], "lookup_failing_since": since}}
                                if x["state"] == "merging" and (x.get("merge_check") or {}).get("phase") == "starting"
                                else None)
                continue
            if found and mc.get("phase") == "starting":  # an earlier server stopped right after starting it
                desk.update(m["id"], merge_check={**mc, "phase": "running", "pid": found, "pid_start": proc_start(found),
                                                  "lookup_failing_since": None})
            elif not found and not alive(mc.get("pid"), mc.get("pid_start", "")):
                if mc.get("phase") == "starting":  # it never started: once more from the start
                    desk.change(m["id"], lambda x: {"merge_check": {**x["merge_check"], "phase": "queued",
                                                                    "lookup_failing_since": None}}
                                if x["state"] == "merging" else None)
                else:
                    finish_merge_check(desk, m["id"])
        for m in desk.tasks():
            mc = m.get("merge_check") or {}
            if m["state"] != "merging" or mc.get("phase") != "running":
                continue
            limit = 60 * s["lanes"].get(m.get("lane"), {}).get("timeout_min", 240)
            if not mc.get("cancel") and time.time() - (mc.get("started") or time.time()) > limit:
                desk.update(m["id"], merge_check={**mc, "timed_out": True})
                stop(mc.get("pid"), mc.get("pid_start", ""), self.merge_procs.get(m["id"]))
        tasks = desk.tasks()
        busy = {m.get("lane") for m in tasks if lane_occupied(m)} | tried_lanes(desk)
        free = s["concurrency"] - sum(map(lane_occupied, tasks))
        for m in tasks:
            if m["state"] != "merging" or (m.get("merge_check") or {}).get("phase") != "queued":
                continue
            ln = m.get("lane")
            if ln not in s["lanes"]:
                end_merge_check(desk, m["id"], {"ok": False, "at": now(), "note": f"lane {ln} is no longer in settings"})
                continue
            if ln in busy or free <= 0:
                continue
            try:
                proc = start_merge_check(desk, m["id"], ln, s["lanes"][ln])
            except SrtCannotRun as e:  # the machine's problem: nothing is merged, and nothing ran
                remove_sandbox_tmp(desk, m["id"])
                end_merge_check(desk, m["id"], {"ok": False, "at": now(), "could_not_run": True,
                                                "command": s["lanes"][ln].get("check", ""), "tail": [],
                                                "note": desk.redact(f"could not run: {e}")[:600]})
                continue
            except Exception as e:  # noqa: BLE001 - say: nothing is merged, and the task says why
                remove_sandbox_tmp(desk, m["id"])
                end_merge_check(desk, m["id"], {"ok": False, "at": now(),
                                                "note": desk.redact(f"the merge check could not run: {e}")[:600]})
                continue
            if proc:
                self.merge_procs[m["id"]] = proc
                busy.add(ln)
                free -= 1

    def tries(self, s: dict) -> None:
        """Finish tries that ended, stop those over the lane's time, finish those an earlier server left, and
        start queued ones (also while dispatch is paused: they use no provider)."""
        desk = self.desk
        for ln, proc in list(self.try_procs.items()):
            if has_ended(proc):
                del self.try_procs[ln]
                stop(proc.pid, "", proc)
                finish_try(desk, ln)
        d = desk.state_dir / "tries"
        for ln in sorted(f.stem for f in d.glob("*.json")) if d.is_dir() else []:
            t = read_try(desk, ln)
            if t.get("phase") == "running" and ln not in self.try_procs:
                if not alive(t.get("pid"), t.get("pid_start", "")):  # started by an earlier server
                    finish_try(desk, ln)
                    continue
            if t.get("phase") == "running" and not t.get("timed_out"):
                if time.time() - (t.get("started") or time.time()) > 60 * s["lanes"].get(ln, {}).get("timeout_min", 240):
                    write_try(desk, ln, {**t, "timed_out": True})
                    stop(t.get("pid"), t.get("pid_start", ""), self.try_procs.get(ln))
            if t.get("phase") != "queued":
                continue
            if ln not in s["lanes"]:
                end_try(desk, ln, {"ok": False, "steps": {}, "note": f"lane {ln} is no longer in settings"})
                continue
            try:
                proc = start_try(desk, ln, s["lanes"][ln])
            except Exception as e:  # noqa: BLE001 - srt missing, say: the try says why it did not run
                for x in read_try(desk, ln).get("tmp", []):
                    shutil.rmtree(x, ignore_errors=True)
                end_try(desk, ln, {"ok": False, "steps": {}, "note": desk.redact(f"the try could not run: {e}")[:600]})
                continue
            if proc:
                self.try_procs[ln] = proc

    def tick(self) -> None:
        desk = self.desk
        s = desk.settings()
        desk.state_dir.mkdir(exist_ok=True)
        beat(desk)
        for tid, proc in list(self.procs.items()):
            if has_ended(proc):
                del self.procs[tid]
                stop(proc.pid, "", proc)  # what it left running in its group (a server, say) ends with it
                self.ended(tid, proc.returncode)
                beat(desk)
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
        self.tries(s)
        self.merge_checks(s)
        beat(desk)  # what came before can take long: stopping a worker waits for its group, finishing sweeps
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
        running = [m for m in desk.tasks() if lane_occupied(m)]
        free = s["concurrency"] - len(running)
        busy = {m["lane"] for m in running} | tried_lanes(desk)
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


ROUND_QUIET = 30  # seconds without a finished round after which hansa serve counts as busy
STUCK_AFTER = 300  # seconds without a finished round after which it is said to be busy or stuck


def server_holder(desk: Desk) -> tuple:
    """The hansa serve that serve.lock names (pid, start); (None, '') when it names none."""
    try:
        words = (desk.state_dir / "serve.lock").read_text().split()
        return int(words[0]), " ".join(words[1:])
    except (OSError, ValueError, IndexError):
        return None, ""


def _exists(pid, start: str) -> bool:
    """alive(), but a process of another account (not ours to signal) counts as there."""
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return alive(pid, start)


def beat(desk: Desk) -> None:
    """hansa serve got through a round, or a step of one: the heartbeat server_state reads."""
    desk.state_dir.mkdir(exist_ok=True)
    (desk.state_dir / "heartbeat").write_text(str(now()))


def pulse(desk: Desk, every: float = 5.0) -> None:
    """hansa serve's process answers: written from a thread while it runs, apart from the rounds, so that a
    server whose rounds stopped coming is still told from one whose process stopped (server_state)."""
    import threading

    def run():
        while True:
            try:
                write_json(desk.state_dir / "pulse", {"pid": os.getpid(), "at": time.time()})
            except OSError:
                pass
            time.sleep(every)
    threading.Thread(target=run, name="hansa-pulse", daemon=True).start()


def server_state(desk: Desk) -> dict:
    """One rule for whether hansa serve runs. gone: the process serve.lock names is no longer alive, or it
    names none. up: alive, and it finished a round in the last ROUND_QUIET seconds. busy: alive, quiet for
    longer (a round waiting on a worker to stop, say). stuck: alive, quiet for STUCK_AFTER seconds or more.
    quiet: seconds since its last round; answers: whether its process still wrote its pulse lately (a
    process stopped with Ctrl-Z, say, does not)."""
    pid, start = server_holder(desk)
    if not pid or not _exists(pid, start):
        return {"state": "gone", "pid": None, "quiet": None, "answers": False}
    quiet = max(0, int(time.time() - (read_json(desk.state_dir / "heartbeat", 0) or 0)))
    pulse = read_json(desk.state_dir / "pulse", {}) or {}
    answers = pulse.get("pid") == pid and time.time() - (pulse.get("at") or 0) < ROUND_QUIET
    return {"state": "up" if quiet < ROUND_QUIET else "busy" if quiet < STUCK_AFTER else "stuck",
            "pid": pid, "quiet": quiet, "answers": answers}


def server_up(desk: Desk) -> bool:
    """Whether hansa serve runs: its process is alive, busy or not (server_state)."""
    return server_state(desk)["state"] != "gone"


def server_text(st: dict) -> str:
    """What server_state says, in words, for hansa status and hansa explain server."""
    if st["state"] == "gone":
        return "server NOT running: start it with `hansa serve` (see hansa explain server)"
    if st["state"] == "up":
        return "server up"
    mins = st["quiet"] // 60
    quiet = f"{mins} minute{'s' if mins != 1 else ''}" if mins else f"{st['quiet']} seconds"
    return (f"server up (pid {st['pid']}) but busy: no round finished for {quiet}" if st["state"] == "busy" else
            f"hansa serve (pid {st['pid']}) has not finished a round for {quiet}: busy or stuck"
            + ("" if st["answers"] else "; its process does not answer either (stopped with Ctrl-Z?)")
            + " · next: hansa explain server")


def waiting_reason(desk: Desk, s: dict, m: dict, busy: set) -> str:
    """Why a queued task is not running yet, in one line."""
    if m.get("kind") == "design":
        other = [x["id"] for x in desk.tasks() if x.get("kind") == "design" and x["state"] == "running"
                 and x.get("lane") == m.get("lane")]
        return (f"lane {m.get('lane')} already has design session {other[0]} open" if other
                else f"lane {m.get('lane')} is not in settings" if m.get("lane") not in s["lanes"]
                else "opens when `hansa serve` next looks (within seconds)" if server_up(desk)
                else "opens once `hansa serve` runs; `hansa serve` is not running")
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
            return (f"ready for lane {ln} on {ready[0]}; starts when `hansa serve` next looks (within seconds)"
                    if server_up(desk) else f"ready for lane {ln} on {ready[0]}, but `hansa serve` is not running")
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
    argv += isolated(tmp)
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
