"""Workers: start one Claude Code run per task, record what came back, and
keep the queue moving.

A worker is `claude -p` in the lane's git worktree, on a branch of its own
(task/<id>). The prompt goes in on stdin; the JSON result lands in
.hansa/runs/<id>/run.json, and hansa publishes it into tasks/<id>/ without keys when the
worker ends. Nothing here needs the worker to know about hansa.
"""
from __future__ import annotations

import atexit
import contextlib
import contextvars
import errno
import fcntl
import functools
import hashlib
import json
import math
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
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from . import account, build, installed, installed_text, session_vars, without_session
from .desk import (DENY_AT, HANSA_IDENTITY, event_record, desk_paths_problem, pattern_path_problem, run_homes, no_read_parts, stop_text, other_envs, Desk, account_home, claude_home, home_secrets, identity_problem,
                   refused_rule, argv_rules_problem, path_rule_problem, pattern_chars, kept_from_workers, desks_file, desks_list_problem, other_desks, holding_desks_parts, path_spellings, deny_split, lane_repos, _above, now, merge_message, message_fallback, parse_identity, program_candidates,
                   WRITE_FILE, copy_file, read_json, resolved, task_message, update_message, work_lane_problem, write_bytes, write_json, write_text, write_problems)
from .desk import lane_env as checked_env
from .srt_output import protected

SRT_OUTPUT = shlex.join(protected([]))

# Set the child's score before srt can hide /proc; its children inherit it. A shell wrapper
# is safe when serve has threads (preexec_fn is not), and a refused write must not stop a run.
OOM_SCRIPT = '{ printf "500\\n" > /proc/self/oom_score_adj; } 2>/dev/null || :\nexec "$@"'
OOM_VMSTAT = Path("/proc/vmstat")  # replaceable in tests; read by hansa, never through srt


def oom_kill():
    """The machine's count since boot (Linux 4.13+), or unknown. Counting must never prevent a run."""
    if account.LINUX:
        try:
            for line in OOM_VMSTAT.read_text().splitlines():
                words = line.split()
                if len(words) == 2 and words[0] == "oom_kill":
                    value = int(words[1])
                    return value if value >= 0 else None
        except (OSError, ValueError, UnicodeError):
            pass
    return None


def oom_result(start, end=None) -> dict:
    end = oom_kill() if end is None else end
    return {"oom_kills": end - start} if start is not None and end is not None and end > start else {}


def oom_begin(record: dict, prefix: str = "") -> dict:
    """Clear a previous run's counts under its record's lock. Unknown means no count fields at all."""
    for field in ("oom_start", "oom_kills"):
        record.pop(prefix + field, None)
    start = oom_kill()
    return {prefix + "oom_start": start} if start is not None else {}


def oom_text(record: dict) -> str:
    n = record.get("oom_kills")
    return (f"the system's out-of-memory killer killed {n} {'process' if n == 1 else 'processes'} while it ran "
            "(a count for the whole machine)") if n else ""


def oom_lines(record: dict, what: str) -> list:
    out = [f"{what}: {oom_text(record)}"] if oom_text(record) else []
    first = record.get("retried") or record.get("previous")
    if first:
        out += oom_lines(first, f"{what}'s first run")
    return out


def with_oom_score(argv: list, cwd=None, env=None) -> list:
    """Prefer workers and lane commands when Linux's kernel has to kill a process for memory. A program
    Popen would not find (as it looks: by path from cwd, else in env's PATH) starts without the wrapper,
    so Popen still raises and hansa says so, where the shell would print "not found" and exit 127."""
    prog = argv[0]
    dirs = [""] if os.path.dirname(prog) else os.get_exec_path(env)
    found = any(os.path.isfile(p) and os.access(p, os.X_OK)
                for p in (os.path.join(cwd or "", d, prog) for d in dirs))
    return ["/bin/sh", "-c", OOM_SCRIPT, "hansa-oom", *argv] if account.LINUX and found else argv


# Only the wording providers use for "out of quota", so a task that is itself
# about rate limits does not rest a provider when it fails for another reason.
CREDIT = re.compile(r"credit balance is too low|API Error: 402\b|insufficient[_ ]balance|insufficient_quota|余额不足", re.I)
QUOTA = re.compile(r"API Error: 429|rate_limit_error|usage limit reached|"
                   r"you(?:'|’)ve hit your [\w' ]{0,40}limit|"  # Claude Code's own line for a subscription's limit
                   r"insufficient quota|quota exceeded|too many requests|额度不足|额度已用", re.I)
QUOTA_STATUS = {429}
AUTH = re.compile(r"authentication_error|invalid (x-)?api[ -]key|API Error: 40[13]\b|permission_error", re.I)
AUTH_STATUS = {401, 403}
LOGIN = re.compile(r"Not logged in|Please run /login", re.I)  # Claude Code itself has no login for this provider
# The connection to the provider failed, or the provider did (overloaded, a server error): nothing the task
# did, and nothing that says the provider's key or quota is wrong. The provider waits for a passing check.
# Only on Claude Code's own error lines: one that begins "API Error" (a 5xx, a connection or timeout, the provider
# overloaded, unable to connect), one that begins with a connection reset, refused, closed or lost while the
# computer was asleep, a socket hang up or a fetch that failed (after an "Error:" maybe), one that ends
# "(ENETUNREACH)", or the API's own overloaded error. A test that timed out, or a log line saying
# "overloaded", is the task's output, not a dropped connection. The phrase found is the one named group that matched.
NETWORK = re.compile(r"^[ \t]*(?:[\w.]*Error:[ \t]+)?(?P<server>API Error:?[ \t]*5\d\d)\b|"
                     r"^[ \t]*(?:[\w.]*Error:[ \t]+)?API Error:?[^\n]{0,80}?(?P<api>Repeated 529 Overloaded errors|"
                     r"ECONNRESET|ECONNREFUSED|ECONNABORTED|ETIMEDOUT|EPIPE|EAI_AGAIN|ENOTFOUND|ENETUNREACH|"
                     r"EHOSTUNREACH|Unable to connect to API(?![ \t]*:)|"  # not "...: SSL certificate ...": no drop
                     r"Connection (?:dropped|error|reset|closed|refused|timed out|lost)|socket hang up|fetch failed|"
                     r"(?:request|connection) tim(?:ed[ -]?out|eout)|overloaded_error|\boverloaded\b)|"
                     r"^[ \t]*(?:[\w.]*Error:[ \t]+)?(?:(?:read|write|connect)[ \t]+)?(?P<line>ECONNRESET|ECONNREFUSED|"
                     r"Connection (?:reset|refused|closed|lost while your computer was asleep)|socket hang up|"
                     r"fetch failed)|"
                     r"\((?P<unreach>ENETUNREACH)\)[ \t.]*$|"  # the network itself out of reach, at a line's end
                     r"\"type\"[ \t]*:[ \t]*\"(?P<json>overloaded_error)\"", re.I | re.M)
OVERLOADED_STATUS = 529
PROBE_MINUTES = 3  # unreachable providers: one check per interval, outside serve's loop
# A run the system killed (SIGKILL, as Linux's kernel kills the largest process when memory runs short): its exit as
# Popen gives it, and as a shell passes it on
KILLED_EXITS = (-9, 137)
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
#   <its own temporary folder> <worker command...> (the two folders of run_tmp, both removed after it)
SETUP_SCRIPT = ("""ok() { [ -f .git ] && [ ! -L .git ] && [ "$(cat .git)" = "$line" ]; }
line=$4
rc=$( { { env """ + " ".join(f"-u {v}" for v in KEY_VARS) + """ TMPDIR="$5" CLAUDE_CODE_TMPDIR="$5" "$6" --settings "$7" -- """ + SRT_OUTPUT + """ /bin/sh -c "$1" </dev/null 2>&1 3>&-
  echo $? >&3; } | cat >"$2"; } 3>&1 )
[ "$rc" = 0 ] || {
  rm -rf "$5" "$8"
  ! ok || [ -z "$(git status --porcelain)" ] || git stash push --include-untracked -q -m "hansa: what the lane's failed setup changed" >>"$2" 2>&1
  echo "hansa: the lane's setup command failed (exit $rc)" >>"$3"; exit 97; }
ok || {
  echo "hansa: the worktree's .git file was changed during the lane's setup; hansa ran no git there" >>"$3"; exit 96; }
if [ -n "$(git status --porcelain)" ]; then
  git stash push --include-untracked -q -m "hansa: what the lane's setup changed" >>"$2" 2>&1
  echo "hansa: the setup changed files in the repository; they were set aside (git stash), so they are not part of the task" >>"$2"
fi
rm -rf "$5" "$8"
: > "$2.done"
shift 8; exec "$@"
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
# sh -c CHECK_SCRIPT <token> <check> <exit file> <temporary folder> <srt> <srt settings> <its own temporary folder>
CHECK_SCRIPT = WRITE_FILE + '{ "$4" --settings "$5" -- ' + SRT_OUTPUT + ' sh -c "$1" 2>&1; write_file "$2" "$?"; } | cat; rm -rf "$3" "$6"'
# `hansa lane try`: the lane's setup, then its check, each in srt as for a task; the first that fails ends it.
# Each writes <folder>/<name>.log (through a pipe, see SETUP_SCRIPT) and <folder>/<name>.exit: its exit code
# and the seconds it took. Each step's temporary folder is removed after it, or when it does not run.
# sh -c TRY_SCRIPT <token> <folder> <srt> [<name> <command> <srt settings> <temporary folder> <its own>]...
TRY_SCRIPT = (WRITE_FILE + """d=$1 srt=$2
run() {
  t0=$(date +%s)
  rc=$( { { env """ + " ".join(f"-u {v}" for v in KEY_VARS) + """ TMPDIR="$4" CLAUDE_CODE_TMPDIR="$4" "$srt" --settings "$3" -- """ + SRT_OUTPUT + """ /bin/sh -c "$2" </dev/null 2>&1 3>&-
    echo $? >&3; } | cat >"$d/$1.log"; } 3>&1 )
  rm -rf "$4" "$5"
  write_file "$d/$1.exit" "$rc $(( $(date +%s) - t0 ))"
  [ "$rc" = 0 ]
}
shift 2
while [ $# -ge 5 ]; do
  run "$1" "$2" "$3" "$4" "$5" || break
  shift 5
done
while [ $# -ge 5 ]; do rm -rf "$4" "$5"; shift 5; done
""")
KEY_REST_MINUTES = 24 * 60  # until the principal changes the key (saving a key clears the rest)
# or logs in (then a check that answers clears it)
TRIES = 3  # a task's tries before it fails
# A run that ends on its provider's usage limit puts the task back in the queue without using a try, this
# many times; after that each such end counts as a try, so a provider that hits its limit again right after
# each rest does not hold one task forever.
LIMIT_RETURNS = 5
REST_CAP = 31 * 86400  # the longest rest a reset time read from an error gives (a monthly allowance)
MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
# The zone after a time: "(Europe/Berlin)", as Claude Code prints the subscription's reset; UTC, GMT, Z; an offset.
_ZONE = r"(?:\s*\(([A-Za-z][\w+-]*(?:/[\w+-]+)*)\)|\s*\b(UTC|GMT)\b|(Z)\b|\s*([+-]\d{2}:?\d{2})\b)?"
# The forms of a limit's reset time in an error, each read by reset_time:
#   a Unix time: Claude Code's "Claude AI usage limit reached|1760000000", the API's
#     anthropic-ratelimit-unified-reset, Claude Code's resets_at
RESET_UNIX = re.compile(r"(?:limit reached\||ratelimit-unified-reset\"?\s*[:=]\s*\"?|resets_?at\"?\s*[:=]\s*\"?)"
                        r"(\d{10})(?!\d)", re.I)
#   seconds from now: a retry-after header, when a message passes it on
RESET_AFTER = re.compile(r"retry[-_ ]after\"?\s*[:=]\s*\"?(\d{1,7})(?![\d.])", re.I)
#   "in N minutes" or "in N hours" (seconds too) after words that say the limit ends or to try again then
RESET_IN = re.compile(r"(?:reset|try|retry|again|available|wait)\w*\b[^.\n]{0,30}?\bin\s+(\d{1,5}(?:\.\d+)?)\s*"
                      r"(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h)\b", re.I)
#   a date and time: "2026-08-08 00:00 UTC" (a gateway's spend limit), ISO 8601
RESET_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})[T ](\d{1,2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?" + _ZONE)
#   Claude Code's subscription reset more than a day out: "resets Oct 9, 3pm (Europe/Berlin)",
#   "resets Jan 2, 2027, 3:30pm (Europe/Berlin)"
RESET_DAY = re.compile(r"\b(" + "|".join(MONTHS) + r")[a-z]*\.?\s+(\d{1,2})(?:,\s*(\d{4}))?,?\s+(?:at\s+)?"
                       r"(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)" + _ZONE, re.I)
#   a clock time: Claude Code's "You've hit your session limit · resets 3pm (Europe/Berlin)", "resets at 15:00"
RESET_CLOCK = re.compile(r"\bresets?\s+(?:at\s+)?(\d{1,2})(?!\d)(?::(\d{2}))?\s*([ap]\.?m\.?)?" + _ZONE, re.I)
# The provider would not answer this request (its content filter or a refusal). That
# says nothing about other tasks, so the provider does not rest; the task moves on.
REFUSED_STOPS = {"content_filter", "refusal"}
BASE_ENV = ("HOME", "PATH", "LANG", "LC_ALL", "USER", "LOGNAME", "SHELL", "TMPDIR")
RUN_TMP_VARS = ("TMPDIR", "CLAUDE_CODE_TMPDIR")  # hansa's for each run (make_worker_tmp, in_srt), not a lane's
GIT_ID = {"GIT_AUTHOR_NAME": "hansa", "GIT_AUTHOR_EMAIL": "hansa@localhost",
          "GIT_COMMITTER_NAME": "hansa", "GIT_COMMITTER_EMAIL": "hansa@localhost"}
PROTOCOL = """You are a worker on task {tid} in lane {lane}. You work only in this directory,
on branch task/{tid}, which was made for this task.
- Do not commit: when you end, hansa commits everything you changed here. To look at your
  work, git status, git diff, git log and git show are allowed.
- On Linux the sandbox puts empty files where the paths it protects under .claude/ are missing, so
  while you work git status may list untracked files there that you did not make. Leave them: do not delete
  them or report them as a change. The sandbox removes them and hansa never commits them.
- Do not push, do not switch branches, do not touch other directories.
- You work in a sandbox; inside it any command runs. Your temporary folder, {tmp}, is yours
  alone for this run and is removed when it ends: put scratch files there, not in this directory.
  Write its path out in full in commands, not as $TMPDIR: Claude Code cannot see what a variable
  stands for, and refuses a command whose paths it cannot place.
- To try a line taken from a build or other script (one that holds rm, say), write it to a script
  file in your temporary folder and run that with sh: `sh {tmp}/probe.sh`, not `bash -c "..."`.
  Claude Code refuses a shell -c it cannot check, such as one that runs rm; a script file runs.
- If a command you need is refused, say which one and why you need it in your report;
  do not look for a way around the refusal.
- `git stash` is refused, `git stash list` too, and one refused part refuses the whole command
  line. To see a file as it was before your changes: `git show HEAD:path`.
- A later command cannot stop a background process an earlier one started (kill is refused):
  stop background work within the command that started it, or wait for it there.
- In tests you write: wait for the thing itself (a file, a line, a state), not a fixed few seconds,
  and give any time limit seconds of room; check what the code does and the words that matter,
  not whole sentences; skip a test the sandbox cannot run (one that binds a local port, say) with
  the reason in the skip, rather than leave it failing or delete it.
{long}- End with a short report: what you changed, how you checked it, and anything left undone.
  Its last line is `Changelog: ` and one sentence on the change for the project's users,
  or `Changelog: none` when they would not notice it. A follow-up writes that line for the
  whole change as it stands on its branch, not only for this round.
  The report's numbers come from this run, not from earlier text.
  Stop or wait for any background work you started before you write the report: your last message
  is taken as your report.
- Nobody will wake you later or answer questions: wait for your own work before you report.
"""


VERIFY_PROTOCOL = """You are a worker in lane {lane}, checking another worker's finished task. You are
on branch task/{tid}, a copy of that task's branch made for this check; nothing here is merged.
- Change files as your checks need (undo code, add a throwaway test), but do not commit; hansa sets
  your changes aside when you end. For undoing, `git checkout <commit> -- <path>` and
  `git restore` are allowed here.
- On Linux the sandbox puts empty files where the paths it protects under .claude/ are missing, so
  while you work git status may list untracked files there that nobody made. Leave them: do not delete
  them or report them as a change. The sandbox removes them and hansa never commits them.
- Do not push, do not switch branches, do not touch other directories.
- You work in a sandbox; inside it any command runs. Your temporary folder, {tmp}, is yours
  alone for this run and is removed when it ends: put scratch files there, not in this directory.
  Write its path out in full in commands, not as $TMPDIR: Claude Code cannot see what a variable
  stands for, and refuses a command whose paths it cannot place.
- To try a line taken from a build or other script (one that holds rm, say), write it to a script
  file in your temporary folder and run that with sh: `sh {tmp}/probe.sh`, not `bash -c "..."`.
  Claude Code refuses a shell -c it cannot check, such as one that runs rm; a script file runs.
- If a command you need is refused, say which one and why in your report; do not look for a way around it.
- `git stash` is refused, `git stash list` too, and one refused part refuses the whole command
  line. To see a file as it was before your changes: `git show HEAD:path` (before the task's, the
  base commit the task names: `git show <base>:path`).
- A later command cannot stop a background process an earlier one started (kill is refused):
  stop background work within the command that started it, or wait for it there.
{long}- Stop or wait for any background work you started before you write your report: your last
  message is taken as your report.
- Nobody will wake you later or answer questions: wait for your own work before you report.
"""

LONG_RUNS = """- Run a long build or test suite in the foreground, in one Bash call with a timeout long enough
  for it: give the Bash tool's timeout, in milliseconds, up to {bash_ms} ({bash_min} minutes, the most
  one call may run here). A call that gives none stops after {default_min} minutes. The lane's check
  may run {check_min} minutes, so a command near that is long. Never start one in the background and
  end your turn while it runs: when your turn ends, your run ends, and what it was waiting for is lost.
"""

VERIFY_TASK = """Find out, by running things, whether task {id} does what it asked. Its change is
`git diff {base}..{head}`, already on this branch. Do not fix anything.

1. Run first.
   - If the change adds or changes tests: undo the code part of the change (keep the tests) and
     run only the tests it adds or changes, selected by name (one test, a class, or a name pattern,
     as the project's test runner allows), not their whole file or suite. Also undo each part the task
     asks for, in turn, run only the tests that concern that part, selected the same way, and name
     each part no test fails without. Then put the code back. Each defect the task names needs a
     test that fails with the code undone; one that still passes does not test the change. A test
     the task asks for to cover behaviour that already worked (coverage) may pass either way:
     judge it on whether it tests what its name says.
   - Run what the task is about the way a user or caller would meet it.
   - Try the edge cases you can think of: empty or missing values, data or settings left from
     before the change, errors, other languages.
   - Run the project's whole check at most once, at the end, and only if you need to.
2. Read the code only to decide what to run and to explain what you saw.
3. For text or documentation, compare what it says with the code it describes.

Report what you ran and what happened (the command and the lines that matter); then, for each
thing the task asked, whether it holds, holds with notes, does not hold or is unsure, with that evidence; then
anything the change does that the task did not ask for and each part no test watches. End with
exactly one of these lines:
VERDICT: holds, with notes
VERDICT: holds
VERDICT: does not hold
VERDICT: unsure
Write it in English exactly as shown, whatever the report's language: hansa reads only those words.
Say "does not hold" when any of these is true:
- In ordinary use, something the task asked for does not hold, or something that worked before
  no longer does.
- In any state, the change merges into the wrong branch, loses work, shows a key, or reports
  a check passed that did not pass.
- The only tests for a defect the task names still pass with the code undone: such a test checks
  nothing, even if the task asked for it that way.
Say "holds, with notes" when everything holds in ordinary use and the gaps found show only in
rare combinations of states, except for the failures above that always mean "does not hold".
Examples: something coming between two steps (a cancel, a restart, a failed write); records or
settings left from before the change; a setting made by hand. List these gaps under "Notes",
each with what it would take to meet it.
A coverage test the task asked for that passes either
way does not count against the change; a test whose name promises one case and tests another
does, coverage or not.
"""
VERDICT = re.compile(r"^\W*VERDICT:\W*(holds, with notes|holds|does not hold|unsure)\b", re.I | re.M)
HOLDING_VERDICTS = ("holds", "holds, with notes")
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
# Claude Code opens the whole common folder for writing, so the sandbox also keeps every other entry in it
# (git_write_deny): of the common folder only the worktree's own folder under worktrees/ is left, not
# another worktree's folder (its commondir or gitdir would lead hansa's git there to hooks of a worker's making).
GIT_COMMON_DENY = ("objects", "refs", "packed-refs", "HEAD", "config", "hooks", "info", "logs", "config.worktree")
# config.worktree: with extensions.worktreeConfig on (git sparse-checkout sets it), git takes settings from it,
# core.fsmonitor among them, which runs a command at the next git status there, hansa's own outside the sandbox
# What says a merge, cherry-pick, revert or rebase is under way in a worktree's own folder: git commit takes a
# MERGE_HEAD there as a second parent, so one a worker wrote would put a commit nobody reviewed into the history
# of hansa's commit. Workers and verifiers never merge (hansa does), so they may not write them. hansa makes none of them when missing (an empty MERGE_HEAD says a merge is under way), so on Linux, where
# the sandbox keeps only what is there when a command starts, the check before hansa commits (check_no_operation)
# is what holds.
GIT_OPS = ("MERGE_HEAD", "MERGE_MODE", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "sequencer")
GIT_OWN_DENY = ("HEAD", "logs", "commondir", "gitdir", "config.worktree", *GIT_OPS)
GIT_DENY_DIRS = ("objects", "refs", "hooks", "info", "logs")  # the folders among them; the rest are files
# What hansa clears from a worktree's own folder once it has committed there: what was left of a merge it made the
# commit of (commit_work)
GIT_MERGE_LEFT = ("MERGE_HEAD", "MERGE_MODE", "MERGE_MSG", "AUTO_MERGE")
OID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
# The characters a deny pattern keeps in a [...] class (others_than, as ranges: name_classes), with -: those of the names hansa and
# git give branches, worktrees' folders and git's own files. A name made later with another character is not
# kept by the patterns (one there when the run starts is, by name), and hansa never uses one. macOS's sandbox
# matches paths without regard to case, so a letter is left out of a class in both cases, it reads a - just
# before ] as a range up to the end of the character set, so - goes first, and it drops a class of more than
# CLASS_MAX elements, so a long one is split (name_classes). hansa's own names are small letters, digits, . _
# and -: on a case-insensitive volume a capital is the same name, and on a case-sensitive one a name with a
# capital is kept only when it is there as a run starts, as one with another character is.
NAME_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz._"
# The most elements (a character, \. among them, a leading - too, or a range) a [...] class may hold in macOS's
# sandbox, as measured with srt: with 15 a class keeps each of its characters wherever it stands; with 16 the
# whole class matches nothing, silently, and the profile still applies ([c + 15 more]* kept neither cx nor 0x).
# name_classes splits a longer class into patterns of at most this many, which act together.
CLASS_MAX = 15
# What a worker's sandbox on Linux (bubblewrap, in Claude Code and srt alike) mounts an empty file over at the top
# of the worktree when it is missing, so it cannot be made: its fixed list of files a program may run or obey
# (.gitconfig to .mcp.json) and the folders .vscode and .idea. It removes them once no sandbox of its run is
# still up; while one is, git in the sandbox does not list them (LINUX_PRELUDE). Under .claude/ it does the same
# for the settings, skills, hooks and the like it protects there, which git in the sandbox still lists. The macOS sandbox (Seatbelt) makes none; Claude Code's own
# .claude/.cc-writes there is an empty folder, which git does not list. sandbox_leftovers keeps them all out.
SANDBOX_PLACEHOLDERS = (".gitconfig", ".gitmodules", ".bashrc", ".bash_profile", ".zshrc", ".zprofile", ".profile",
                        ".ripgreprc", ".mcp.json", ".vscode", ".idea")
# sandbox-runtime's fixed protected files and folders, plus all of .claude: Claude Code protects settings,
# commands, agents, skills and hooks there, and its exact list changes between versions. Match from the
# repository's top, without regard to case and by whole path segments, when committing the index.
SANDBOX_PROTECTED_PATHS = (*SANDBOX_PLACEHOLDERS, ".claude")
# A worker is one `claude -p` run: nothing wakes it later, nobody answers or is notified.
INTERACTIVE_TOOLS = ["ScheduleWakeup", "CronCreate", "CronDelete", "CronList", "RemoteTrigger", "PushNotification",
                     "AskUserQuestion", "Monitor"]
LOCAL_HOSTS = ("localhost", "127.0.0.1")

UPDATE_NOTE = """
Before you started, hansa merged {base} as it is now into this branch, which holds the earlier task's
work. The merge conflicts in these files, which now hold git's conflict markers (<<<<<<< this branch,
======= then {base}, >>>>>>> end): {files}. Settle each conflict by editing the file so it keeps what
both sides need, and remove every marker. Do not run git merge or commit: when you end, hansa's commit
completes the merge. Then do what the task says.
"""
UPDATE_CLEAN_NOTE = """
Before you started, hansa merged {base} as it is now into this branch, which holds the earlier task's
work; it merged without conflicts.
"""
# A lane's setup or check command up to this long (the page takes up to 1000 characters) is in a worker's
# instructions whole; a longer one (saved some other way) is whole in a
# file in the run's folder, which the instructions name (around_note)
COMMAND_SHOWN = 4000
# A lane's turn and time limits are a circuit breaker, set high: a worker that reaches one
# was given a task too big or unclear, or got stuck. Its notes show where it went.
CEILING = "the task was probably too big or unclear, or the worker got stuck"
STOPPED_NOTE = "(Stopped at the ceiling before it wrote its report. Its notes along the way, to see what went wrong:)\n\n"
LATE_NOTE = "\n\n(Written after that, when a background notice came in:)\n\n"


def claude_bin() -> str:
    for c in program_candidates("claude"):
        if c and os.access(c, os.X_OK):
            return c
    raise RuntimeError("claude was not found; install Claude Code or set HANSA_CLAUDE")


SRT_MISSING = ("hansa needs srt (Anthropic's sandbox runtime) to run a lane's setup and check commands in a "
               "sandbox, and srt is not on the PATH. Install it with `npm install -g @anthropic-ai/sandbox-runtime@0.0.79` "
               "with Node 22.12.0 or newer on that PATH (the coordinator can do this). "
               "A lane with neither a setup nor a check command needs no srt.")


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


def base_env() -> dict:
    """hansa's own environment, as what it runs outside the sandbox is given it (srt, the scripts around a
    setup or check and their git, Claude Code): never a lane's env, which reaches only what runs in the
    sandbox (in_sandbox, env_file)."""
    return {k: os.environ[k] for k in BASE_ENV if k in os.environ}


def try_srt(desk: Desk, settings_file, tmp: str, cwd) -> str:
    """Run `true` in srt with these settings, as a setup or check would run, and record what happened in the
    desk's state (srt_record). srt is found on hansa's own PATH and run with hansa's environment, never a
    lane's. Returns srt's path; raises SrtCannotRun when it is not there or does not run: srt is a node
    script, so a PATH without node finds srt and still cannot start it. After a relay failure, try plain
    `true` to distinguish srt itself not starting from the relay not running inside the sandbox."""
    env = base_env()
    path = shutil.which("srt")
    rec = {"found": bool(path), "path": path or "", "ran": False, "said": "", "at": now()}
    srt_started = False
    if path:
        for relay in (True, False):
            command = protected(["true"]) if relay else ["true"]
            try:
                r = subprocess.run([path, "--settings", str(settings_file), "--", *command], cwd=str(cwd),
                                   env={**env, "TMPDIR": tmp, "CLAUDE_CODE_TMPDIR": tmp}, stdin=subprocess.DEVNULL,
                                   capture_output=True, text=True, errors="replace", timeout=SRT_TRY_SECONDS)
                if r.returncode == 0:
                    srt_started = True
                    rec["ran"] = relay
                    break
                said = " ".join((r.stdout + r.stderr).split())[-500:]
                rec["said"] = f"{said or 'nothing'} (exit {r.returncode})"
            except subprocess.TimeoutExpired:
                rec["said"] = f"no answer in {SRT_TRY_SECONDS} s"
            except OSError as e:
                rec["said"] = str(e)[:300]
    else:
        rec["said"] = SRT_MISSING
    desk.state_dir.mkdir(exist_ok=True)
    write_json(desk.state_dir / "srt.json", rec)
    if not rec["ran"]:
        if not path:
            raise SrtCannotRun(rec["said"])
        if not srt_started:
            raise SrtCannotRun(f"srt ({path}) could not start with these sandbox settings: {rec['said']}")
        prefix = f"prefix {sys.prefix}" + (f", base prefix {sys.base_prefix}" if sys.base_prefix != sys.prefix else "")
        raise SrtCannotRun(f"srt ({path}) could not run hansa's output relay and true with these sandbox settings: {rec['said']}. "
                           f"Check whether an entry on 'workers may not read' hides the Python hansa runs with "
                           f"({sys.executable}, {prefix}) or hansa's folder ({Path(__file__).resolve().parent}).")
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
        try_srt(desk, f, tmp, tmp)
    except SrtCannotRun:
        pass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return srt_record(desk)


# Before hansa starts a worker or a verifier it tries the run's sandbox: srt with the same
# filesystem and network settings Claude Code is given, on /usr/bin/true, then a write of what git needs open
# there (git_needs_open), since a sandbox that starts may still keep it, as patterns read otherwise than meant
# would (name_classes), and of what it must keep (git_needs_kept), since a pattern may as silently match nothing;
# sandbox_guard. Claude Code carries its
# own copy of the sandbox library, which may differ from the srt installed, so a try that passes is a guard, not
# proof: hansa also watches the run's first Bash calls (sandbox_start_failure). A try that fails holds the task
# (not failed, no try used) and the lane's workers and verifiers: hansa status says why. Kept by a
# fingerprint of the settings in this process alone, so a lane's tasks do not each pay for it; a restart tries
# again, the held lanes' at once (sandbox_retry).
SANDBOX_HELD = "sandbox-held.json"  # in the desk's state: {"lanes": {lane: {group: {why, part, kind, at, task}}},
#                                     "told": [[lane, why]]} (sandbox_held)
SANDBOX_TRIES = {}  # fingerprint of the settings: what the try found (try_sandbox)
# Each part of the settings, tried alone when the whole is refused, to name what the sandbox refuses
SANDBOX_PARTS = (("the exact deny-write paths", lambda fs, net: {"denyWrite": [x for x in fs["denyWrite"] if not _glob(x)]}),
                 ("the deny-write patterns", lambda fs, net: {"denyWrite": [x for x in fs["denyWrite"] if _glob(x)]}),
                 ("the deny-read list", lambda fs, net: {"denyRead": fs["denyRead"], "allowRead": fs["allowRead"]}),
                 ("the write list", lambda fs, net: {"allowWrite": fs["allowWrite"]}),
                 ("the network", lambda fs, net: {"network": net}))
# What a try leaves out on Linux, said once in hansa status and hansa explain server: srt there drops every deny entry
# holding * ? [ and keeps only paths there when a command starts, so new names in git's shared folder are not kept
LINUX_TRY_NOTE = ("on Linux, new names in git's shared folder are left to hansa's checks before it runs git (its "
                  ".git file, the main checkout, no merge or rebase under way, commondir and config.worktree): srt "
                  "there keeps only paths that are there when a command starts, so the sandbox try checks only what "
                  "stays open for git and the paths there it must keep (the shared config, other worktrees' HEAD)")
# What a lane's commands find in the sandbox on Linux, said in hansa explain lane there (docs/linux-server.md).
LINUX_SANDBOX_NOTES = (
    "on Linux, inside the sandbox, what the mount table says (Java's FileStore.isReadOnly, findmnt, df) can call a "
    "writable folder read-only: the worktree, the lane's write list and a worker's temporary folder are writable "
    "binds inside the read-only / (or /tmp) of the same file system, and Java takes the mount of the folder above "
    "them; trust the error of the write itself. The TMPDIR of each of the lane's commands hansa runs in srt (setup, "
    "check, merge check) is a folder of its own on the desk's disk, in .hansa/tmp, which no other run can read "
    "and which hansa removes when the command ends; srt mounts it on its own, so Java reads it as writable, and "
    "findmnt and df name the desk's file system for it",
    "git in the sandbox (in the lane's commands and the workers') does not list the empty "
    "files the sandbox puts at the top of the worktree for what it protects (" + ", ".join(SANDBOX_PLACEHOLDERS)
    + "): its excludes file there adds them, anchored at the top, to the account's own, through a global config "
    "that includes the account's (git 2.32 or later); a tracked file of such a name is still listed, and what hansa "
    "commits does not change. A repository's own core.excludesFile, or one a command sets (git -c, GIT_CONFIG_*), "
    "wins over it, and then they are listed: add them there")
# The name of the script a try runs in srt to write what git needs open and what it must keep (_srt_writes)
GIT_OPEN_TRY = "hansa-git-open-try.sh"
# What a Bash result begins with when the sandbox could not start: macOS's sandbox-exec, Linux's bubblewrap
SANDBOX_START_FAILED = ("sandbox-exec: ", "bwrap: ")


def _glob(x: str) -> bool:
    return any(c in x for c in "*?[")


class SandboxRefused(RuntimeError):
    """srt ran, and refused a run's sandbox settings: the run does not start, and its task is held.
    str(): what the principal is told."""

    def __init__(self, said: str, part: str = "", kind: str = "", why: str = "") -> None:
        self.said, self.part, self.kind = said, part, kind
        super().__init__(why or f"the sandbox could not start: {said}" + (f" (refused: {part})" if part else ""))


# what git_folders found within one start (git_folders_kept), by _git_folders_key; None outside one
_GIT_FOLDERS: contextvars.ContextVar = contextvars.ContextVar("git_folders", default=None)


def git_folders_kept(fn):
    """`fn`, one start of a run (worker, verifier, setup and check, merge check, lane try): it builds
    the sandbox several times, and each build asks git_folders several times for the same two folders, so they
    are worked out once there. The .git file is still checked before each git command (check_git_file)."""
    @functools.wraps(fn)
    def kept(*args, **kwargs):
        if _GIT_FOLDERS.get() is not None:  # within a start already
            return fn(*args, **kwargs)
        token = _GIT_FOLDERS.set({})
        try:
            return fn(*args, **kwargs)
        finally:
            _GIT_FOLDERS.reset(token)
    return kept


def sandbox_try_settings(box: dict, wt: Path, tmp: str) -> dict:
    """srt's settings for a try of the sandbox Claude Code gets (`box`, the "sandbox" of sandbox_settings): its filesystem and network, the worktree and the temporary folder `tmp` written, and, in a
    linked worktree, the common git folder, which Claude Code opens for writing too. `tmp` stands for the run's
    own temporary folder in allowRead (a run's folder is closed to the others, run_tmp_patterns), so the try's
    fingerprint is the same for each run of a lane."""
    fs, net = box["filesystem"], box["network"]
    common, own = git_folders(wt)
    opened = [common] if own.resolve() != common.resolve() else []
    network = {"allowedDomains": list(net.get("allowedDomains", [])), "deniedDomains": []}
    if net.get("allowLocalBinding"):
        network["allowLocalBinding"] = True
    return {"network": network, "filesystem": {"allowWrite": full_paths([wt, tmp, *opened]) + list(fs.get("allowWrite", [])),
                                               "denyRead": list(fs.get("denyRead", [])),
                                               "allowRead": list(dict.fromkeys([*(x for x in fs.get("allowRead", [])
                                                                                  if not is_run_tmp(x)),
                                                                                *full_paths([tmp])])),
                                               "denyWrite": list(fs.get("denyWrite", []))}}


def git_needs_open(wt: Path) -> list:
    """What a run's commands must be able to write in git's folders, for git add and git status: the worktree's
    own index and a new file in its own git folder."""
    common, own = git_folders(wt)
    out = [own / "index", own / ".hansa-try"]
    return out


def git_needs_kept(wt: Path) -> list:
    """What a run's sandbox must keep in git's folders, to see that its deny list keeps it. On macOS, new names
    only a pattern keeps, to see that its patterns match: a new file at the top of the common folder and, next to
    the worktree's own folder under worktrees/, its name with an x after it. On Linux, where srt
    drops every pattern and keeps only paths there when a command starts (LINUX_TRY_NOTE), what is there: the
    shared config and each other worktree's HEAD. None in a main checkout: there the patterns keep nothing."""
    common, own = git_folders(wt)
    if own.resolve() == common.resolve():
        return []
    if account.LINUX:
        try:
            others = sorted(n for n in os.listdir(common / "worktrees") if n != own.name)
        except OSError:
            others = []
        return [x for x in [common / "config", *(common / "worktrees" / n / "HEAD" for n in others)] if x.is_file()]
    out = [common / ".hansa-try-kept", common / "worktrees" / f"{own.name}x"]
    return out


def git_open_try(paths, kept=()) -> str:
    """A shell script writing each of `paths`, then of `kept`, as git would: one that is there opened to append,
    writing nothing; one that is not made and removed again. It prints "refused: <path>" for each of `paths` it
    could not write, and "not kept: <path>" for each of `kept` it could (and removes what it made). It exits 0
    once it has run to its end, whatever it found: another exit is srt's or the sandbox's failure."""
    return ('needs_open() { if [ -e "$1" ]; then ( : >> "$1" ) 2>/dev/null\n'
            '  else ( : > "$1" ) 2>/dev/null && rm -f "$1" 2>/dev/null\n'
            '  fi || echo "refused: $1"; return 0; }\n'
            'needs_kept() { if [ -e "$1" ]; then ( : >> "$1" ) 2>/dev/null && echo "not kept: $1"\n'
            '  elif ( : > "$1" ) 2>/dev/null; then rm -f "$1" 2>/dev/null; echo "not kept: $1"\n'
            '  fi; return 0; }\n'
            + "".join(f"needs_open {shlex.quote(str(x))}\n" for x in paths)
            + "".join(f"needs_kept {shlex.quote(str(x))}\n" for x in kept)
            + "exit 0\n")


def _srt_writes(path: str, settings: dict, tmp: str, cwd, paths, kept=()) -> tuple:
    """srt with these settings writing `paths` and `kept` (git_open_try): (the first of `paths` refused, or "",
    the first of `kept` written, or "", srt's first line when it failed otherwise)."""
    f = Path(tmp) / GIT_OPEN_TRY
    write_text(f, git_open_try(paths, kept))
    ran, said, out = _srt_true(path, settings, tmp, cwd, ["/bin/sh", str(f)])
    lines = out.splitlines()
    refused = [x.removeprefix("refused: ") for x in lines if x.startswith("refused: ")]
    written = [x.removeprefix("not kept: ") for x in lines if x.startswith("not kept: ")]
    return (refused[0] if refused else "", written[0] if written else "", "" if ran or refused or written else said)


def _srt_true(path: str, settings: dict, tmp: str, cwd, argv=("/usr/bin/true",)) -> tuple:
    """srt with these settings on /usr/bin/true (or `argv`): (whether it ran, srt's first line, or why it did not,
    and what the command printed)."""
    f = Path(tmp) / "try-settings.json"
    write_json(f, settings)
    try:
        r = subprocess.run([path, "--settings", str(f), "--", *argv], cwd=str(cwd),
                           env={**base_env(), "TMPDIR": tmp, "CLAUDE_CODE_TMPDIR": tmp}, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, errors="replace", timeout=SRT_TRY_SECONDS)
    except subprocess.TimeoutExpired:
        return False, f"no answer in {SRT_TRY_SECONDS} s", ""
    except OSError as e:
        return False, str(e)[:300], ""
    if r.returncode == 0:
        return True, "", r.stdout
    lines = [x.strip() for x in (r.stderr + "\n" + r.stdout).splitlines() if x.strip()]
    return False, f"{lines[0][:300] if lines else 'nothing'} (exit {r.returncode})", r.stdout


def try_sandbox(box: dict, wt: Path) -> dict:
    """Try the sandbox `box` (sandbox_try_settings) with srt, once for each fingerprint of its settings in this
    process: {"ok": True}; {"ok": False, "said": srt's first line, "part": what of the settings it refuses, found
    by trying each part alone (SANDBOX_PARTS)}; or {"ok": None, "said": why} when srt is not there or does not run
    even with nothing in its settings: then Claude Code's own sandbox (failIfUnavailable) is all there is. A
    sandbox that starts then writes what git needs open there (git_needs_open) and
    what it must keep (git_needs_kept): one that keeps the first or lets the second through is refused too, with
    "why". This fails closed: a sandbox whose patterns do not keep what they should runs no one."""
    path = shutil.which("srt")
    if not path:
        return {"ok": None, "said": SRT_MISSING}
    tmp = tempfile.mkdtemp(prefix="hansa-try-")
    try:
        full = sandbox_try_settings(box, wt, tmp)
        needs = [str(x) for x in git_needs_open(wt)]
        kept = [str(x) for x in git_needs_kept(wt)]
        if account.LINUX:  # only what an exact entry of the deny list keeps: srt there drops the patterns
            exact = [Path(x).resolve() for x in full["filesystem"]["denyWrite"] if not _glob(x)]
            kept = [x for x in kept if any(e == Path(x).resolve() or e in Path(x).resolve().parents for e in exact)]
        key = hashlib.sha256(json.dumps([path, full, needs, kept], sort_keys=True).replace(tmp, "\0tmp").encode()).hexdigest()
        if key in SANDBOX_TRIES:
            return SANDBOX_TRIES[key]
        ran, said, _ = _srt_true(path, full, tmp, wt)
        bare = {"network": {"allowedDomains": [], "deniedDomains": []},
                "filesystem": {"allowWrite": full_paths([tmp]), "denyRead": [], "denyWrite": []}}
        if ran:
            refused, written, said = _srt_writes(path, full, tmp, wt, needs, kept)
            out = {"ok": True}
            if refused or written or said:
                part = (f"the deny-write patterns keep what git needs open: {refused}" if refused
                        else f"the deny-write patterns do not keep {written}" if written
                        else "the write of what git needs open failed")
                out = {"ok": False, "part": part,
                       "said": said or (f"git cannot write {refused}" if refused else f"a command could write {written}"),
                       "why": f"the sandbox starts, but {part}" + (f": {said}" if said else "")}
        elif not _srt_true(path, bare, tmp, wt)[0]:
            out = {"ok": None, "said": said}  # srt does not run at all: not these settings' doing
        else:
            part = ""
            for name, pick in SANDBOX_PARTS:
                alone = pick(full["filesystem"], full["network"])
                if not any(alone.values()):
                    continue
                fs = {**bare["filesystem"], **{k: v for k, v in alone.items() if k != "network"}}
                if "allowWrite" in alone:
                    fs["allowWrite"] = bare["filesystem"]["allowWrite"] + alone["allowWrite"]
                if not _srt_true(path, {"network": alone.get("network", bare["network"]), "filesystem": fs}, tmp, wt)[0]:
                    part = name
                    break
            out = {"ok": False, "said": said, "part": part or "the settings as a whole (no part alone)"}
        SANDBOX_TRIES[key] = out
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def sandbox_guard(desk: Desk, lane_name: str, kind: str, box: dict, wt: Path) -> None:
    """Try the sandbox a worker or a verifier (`kind`) of this lane is
    about to get (try_sandbox): raise SandboxRefused when srt refuses it. A try that passes clears what an
    earlier one held the lane's runs for."""
    r = try_sandbox(box, wt)
    if r["ok"] is False:
        raise SandboxRefused(r["said"], r["part"], kind, r.get("why", ""))
    if r["ok"]:
        sandbox_clear(desk, lane_name, "work")


SANDBOX_GROUPS = {"work": "its workers and verifiers"}


def sandbox_held(desk: Desk) -> dict:
    """The lanes whose runs' sandbox could not start, as last tried: {lane: {group: {why, part, kind, at, task}}},
    a group as in SANDBOX_GROUPS. A record written before groups ({lane: {why, ...}}) is read by its kind."""
    rec = read_json(desk.state_dir / SANDBOX_HELD, {}) or {}
    lanes = rec.get("lanes") or {} if isinstance(rec, dict) else {}
    out = {}
    for ln, v in lanes.items() if isinstance(lanes, dict) else []:
        if not isinstance(v, dict):
            continue
        groups = {"work": v} if "why" in v else \
            {g: h for g, h in v.items() if g in SANDBOX_GROUPS and isinstance(h, dict)}
        if groups:
            out[ln] = groups
    return out


def sandbox_held_text(h: dict) -> str:
    """Why a lane's runs are held (an entry of sandbox_held), which of them, and what starts them again, in a
    clause."""
    why = str(h.get("why") or "?")
    return (f"its {h.get('kind') or 'run'}'s "
            + (why.removeprefix("the ") if why.startswith("the sandbox starts, but ") else
               "sandbox could not start: " + why.removeprefix("the sandbox could not start: "))
            + f"; this holds {SANDBOX_GROUPS['work']}, which start by themselves once a "
              "try of the sandbox passes (after a fix, restart hansa serve; hansa explain server)")


def sandbox_clear(desk: Desk, lane_name: str, group: str) -> None:
    """A try of the sandbox of a lane's runs of this group passed: they are no longer held."""
    with desk.lock():
        held = sandbox_held(desk)
        was = group in held.get(lane_name, {})
        if was:
            held[lane_name].pop(group)
            if not held[lane_name]:
                held.pop(lane_name)
            rec = read_json(desk.state_dir / SANDBOX_HELD, {}) or {}
            write_json(desk.state_dir / SANDBOX_HELD, {**(rec if isinstance(rec, dict) else {}), "lanes": held})
    if was:
        desk.event("sandbox_ready", lane=lane_name, group=group)


def _hold_lane(desk: Desk, lane_name: str, entry: dict) -> bool:
    """Record a lane's runs of the entry's kind as held (sandbox_held); whether the principal's alert is to be
    told, once for each lane and reason."""
    with desk.lock():
        rec = read_json(desk.state_dir / SANDBOX_HELD, {}) or {}
        rec = rec if isinstance(rec, dict) else {}
        told = [x for x in rec.get("told") or [] if isinstance(x, list)]  # [lane, why]; a reason alone was before lanes
        new = [lane_name, entry["why"]] not in told
        held = sandbox_held(desk)
        held.setdefault(lane_name, {})["work"] = entry
        desk.state_dir.mkdir(exist_ok=True)
        write_json(desk.state_dir / SANDBOX_HELD, {"lanes": held, "told": [*told, [lane_name, entry["why"]]] if new else told})
    return new


def hold(desk: Desk, tid: str, lane_name: str, why: str, part: str = "", kind: str = "") -> None:
    """A run whose sandbox could not start: its task goes back to the queue, held (no try used), and its lane's
    workers and verifiers are recorded as unable to run, for hansa status. The principal's alert is
    told once for each lane and reason."""
    why = desk.redact(why)[:600]
    held = {"why": why, "part": part, "kind": kind, "lane": lane_name, "at": now()}
    desk.change(tid, lambda m: None if m["state"] not in ("queued", "running") else
                {"state": "cancelled", "phase": "", "error": "cancelled before it started", "finished": now()}
                if m.get("cancel") else
                {"state": "queued", "phase": "", "pid": None, "pid_start": "", "held": held,
                 "error": why, "sandbox_failed": None})
    remove_sandbox_tmp(desk, tid)
    new = _hold_lane(desk, lane_name, {**{k: held[k] for k in ("why", "part", "kind", "at")}, "task": tid})
    desk.event("sandbox_held", task=tid, lane=lane_name, run=kind, why=why, part=part, alert=new)


@git_folders_kept
def sandbox_retry(desk: Desk, s: dict) -> list:
    """As hansa serve starts: try again the sandbox of each lane's held runs (sandbox_held), a worker's in the
    lane's worktree, so a hold made by earlier code or settings
    does not stay until a task of the lane makes a new try. A try that passes clears the hold; one that fails
    keeps it with the new reason, on the record and on the held tasks. What cannot be tried now (no worktree,
    no srt) is left as it is: a task's start tries it. Returns the (lane, group) still held by a try made here."""
    out = []
    for ln, groups in sandbox_held(desk).items():
        lane = s["lanes"].get(ln)
        for g, h in groups.items() if lane else []:
            kind = h.get("kind") or "worker"
            try:
                pname = next((x for x in lane["providers"] if x in s["providers"]), "")
                wt = desk.worktrees_dir / ln
                p = s["providers"].get(pname)
                if not p or not wt.is_dir():
                    continue
                check_git_file(desk, wt)  # the sandbox's settings ask git in the worktree
                box = sandbox_settings(desk, lane, pname, p, wt)["sandbox"]
                r = try_sandbox(box, wt)
            except Exception as e:  # noqa: BLE001 - left held as it is: the next start of a task tries it
                desk.event("sandbox_retry_failed", lane=ln, group=g, error=desk.redact(str(e))[:300])
                continue
            if r["ok"]:
                sandbox_clear(desk, ln, g)
            elif r["ok"] is False:
                e = SandboxRefused(r["said"], r["part"], kind, r.get("why", ""))
                why = desk.redact(str(e))[:600]
                entry = {"why": why, "part": e.part, "kind": kind, "at": now(), "task": h.get("task", "")}
                new = _hold_lane(desk, ln, entry)
                for m in desk.tasks():
                    if m["state"] == "queued" and m.get("held") and (m["held"].get("lane") or m.get("lane")) == ln \
                            and "work" == g:
                        desk.change(m["id"], lambda x: None if x["state"] != "queued" or not x.get("held") else
                                    {"held": {**x["held"], **{k: entry[k] for k in ("why", "part", "at")}},
                                     "error": why})
                desk.event("sandbox_held", task="", lane=ln, run=kind, why=why, part=e.part, alert=new)
                out.append((ln, g))
    return out


def _result_text(content) -> str:
    """The text of a tool result's content: a string, or a list of blocks, each {"type": "text", "text": ...}."""
    if isinstance(content, str):
        return content
    texts = [c.get("text") if isinstance(c, dict) else c for c in content] if isinstance(content, list) else []
    return "\n".join(x for x in texts if isinstance(x, str))


def _bash_says(c: dict, d: dict) -> list:
    """What a Bash call's result said, as texts to look at the start of: its stderr and stdout where the row
    has them, and the result's text (_result_text) without the "Exit code N" line Claude Code puts first."""
    result = d.get("toolUseResult") if isinstance(d.get("toolUseResult"), dict) else {}
    said = _result_text(c.get("content")).lstrip()
    said = re.sub(r"^(Error: )?Exit code \d+\s*\n", "", said).lstrip()
    return [x.lstrip() for x in (result.get("stderr"), result.get("stdout"), said) if isinstance(x, str)]


def sandbox_start_failure(home: Path, sid: str):
    """From a run's session record: (the line, None) when a Bash result says the sandbox could not start
    (SANDBOX_START_FAILED) before any Bash call succeeded; (None, True) once one has succeeded, after which
    there is nothing to look for; (None, False) while neither is in the record yet."""
    calls = set()
    for d in _rows(home, sid) or []:
        content = (d.get("message") or {}).get("content")
        for c in content if isinstance(content, list) else []:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "tool_use" and c.get("name") == "Bash":
                calls.add(c.get("id"))
            elif c.get("type") == "tool_result" and c.get("tool_use_id") in calls:
                if not c.get("is_error"):
                    return None, True
                for x in _bash_says(c, d):
                    if x.startswith(SANDBOX_START_FAILED):
                        return x.splitlines()[0][:300], None
    return None, False


def srt_when(rec: dict) -> str:
    """When hansa serve tried srt, as month-day hour:minute; '' when the record has no time."""
    return time.strftime("%m-%d %H:%M", time.localtime(rec["at"])) if rec.get("at") else ""


def srt_needed(s: dict) -> bool:
    """Whether some lane has a setup, check or merge check command, which run in srt."""
    return any(ln.get("setup") or ln.get("check") or ln.get("merge_check") for ln in s["lanes"].values())


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


_NO_HOOKS = []  # the empty folder hansa's git in a worktree takes its hooks from, made once (no_hooks)
NO_HOOKS_PREFIX = "hansa-no-hooks-"
TMP_SWEEP_AGE = 3600  # seconds: an empty folder of hansa's this old in the temporary folder is a leftover (sweep_tmp)


def no_hooks() -> str:
    """Made once a process, and removed as it exits (_remove_no_hooks); one a process left (it exec'd another
    program, or was killed) is swept later (sweep_tmp)."""
    if not _NO_HOOKS:
        _NO_HOOKS.append(tempfile.mkdtemp(prefix=NO_HOOKS_PREFIX))  # 0700, the desk's account's alone
        atexit.register(_remove_no_hooks)
    if not os.path.isdir(_NO_HOOKS[0]):  # removed meanwhile (a /tmp cleaner): a new one
        _NO_HOOKS[0] = tempfile.mkdtemp(prefix=NO_HOOKS_PREFIX)
    return _NO_HOOKS[0]


def _remove_no_hooks() -> None:
    if _NO_HOOKS:
        with contextlib.suppress(OSError):
            os.rmdir(_NO_HOOKS[0])  # empty, as git never writes there; one that is not stays


def sweep_tmp(*bases: str) -> list:
    """Empty folders hansa made in the temporary folders (bases: tempfile's and /tmp) that a process left, older than
    TMP_SWEEP_AGE and this account's: the hansa-no-hooks-* of a process that exec'd another program or was killed
    (no_hooks), and the hansa-<uid> of session inboxes no session uses now (notify.socket_path). Only empty ones
    are removed, so a folder in use stays. hansa serve sweeps as it starts and each hour. Returns what it removed."""
    gone, mine = [], f"hansa-{os.getuid()}"
    for base, n in [(b, n) for b in dict.fromkeys(bases or (tempfile.gettempdir(), "/tmp")) for n in _names(b)]:
        if not (n.startswith(NO_HOOKS_PREFIX) or n == mine):
            continue
        f = os.path.join(base, n)
        try:
            st = os.lstat(f)
            if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or time.time() - st.st_mtime < TMP_SWEEP_AGE \
                    or (_NO_HOOKS and f == _NO_HOOKS[0]):
                continue
            os.rmdir(f)
            gone.append(f)
        except OSError:  # not empty, or another's: left as it is
            continue
    return gone


def _names(d: str) -> list:
    try:
        return os.listdir(d)
    except OSError:
        return []


def worktree_git_settings() -> list:
    """What hansa runs git with in a linked worktree, whatever a setting there says: no fsmonitor (a command git
    runs at status, diff and add), hooks from an empty folder (hansa commits and merges with --no-verify, but a
    checkout, a merge's post-merge and the like still run hooks), and no signing program for its commits and
    tags. The lane's own checkout runs its hooks as before: hansa's merge into the base relies on them."""
    return ["-c", "core.fsmonitor=false", "-c", f"core.hooksPath={no_hooks()}",
            "-c", "commit.gpgSign=false", "-c", "tag.gpgSign=false"]


def git(*args, cwd, env=None) -> str:
    check_main_checkout(cwd)  # a linked worktree's git folder is checked with its .git file (check_git_file)
    e = {**without_session(os.environ), **GIT_ID, **(env or {})}
    linked = os.path.isfile(os.path.join(str(cwd), ".git")) and not os.path.islink(os.path.join(str(cwd), ".git"))
    r = subprocess.run(["git", *(worktree_git_settings() if linked else ()), *args], cwd=str(cwd), env=e,
                       capture_output=True, text=True, errors="replace")
    if r.returncode:
        refused = account.git_refusal(r.stderr or "", cwd)
        if refused:
            raise RuntimeError(refused)
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout.strip()


def repo_identity(repo) -> tuple:
    """(user.name, user.email, why not) as the repository's own config sets them: '' for each it does not or that git
    could not write, and for each of those it sets, why it is not usable. Read from its local config alone (git
    config --local, nothing it includes, no global or system config), never the account's own identity."""
    out, bad = [], []
    for key in ("user.name", "user.email"):
        try:
            v = git("config", "--local", "--no-includes", "--get", key, cwd=repo,
                    env={"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
        except (RuntimeError, OSError):
            v = ""
        why = identity_problem(f"{v} <x>" if key == "user.name" else f"x <{v}>") if v else ""
        if why:
            # the problem of the whole Name <email> form, said of the one part
            why = why.replace(" inside the name or the email: Name <email>", "").replace(": Name <email>", "")
            bad.append(f"its {key} {json.dumps(v[:80], ensure_ascii=False)} is not usable: it {why}")
        out.append("" if why else v)
    return (*out, "; ".join(bad))


def lane_identity(lane) -> tuple:
    """(name, email, where it comes from) of the author and committer of the commits hansa makes for a lane: the
    lane's identity, else the repository's own user.name and user.email (each hansa's own where it sets only the
    other, or sets one git could not write), else hansa's own."""
    lane = lane or {}
    own = parse_identity(lane.get("identity"))
    if own:
        return (*own, "the lane's identity")
    name, email, bad = repo_identity(lane["repo"]) if lane.get("repo") else ("", "", "")
    bad = f" ({bad})" if bad else ""
    if name or email:
        missing = "name" if not name else "email" if not email else ""
        return (name or HANSA_IDENTITY[0], email or HANSA_IDENTITY[1],
                "the repository's own config (git config --local user.name and user.email)"
                + (f"; hansa's own {missing} for what it {'cannot use' if bad else 'leaves out'}{bad}"
                   if missing else ""))
    if bad:
        return (*HANSA_IDENTITY, f"hansa's own: the lane sets none and the repository's config sets none that is "
                                 f"usable{bad}")
    return (*HANSA_IDENTITY, "hansa's own: neither the lane nor the repository's config sets one")


def identity_env(lane) -> dict:
    """lane_identity as git's author and committer variables, for a command that writes a commit."""
    name, email, _ = lane_identity(lane)
    return {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email}


def rule(tool: str, path: Path) -> str:
    return f"{tool}(//{str(path).lstrip('/')}/**)"


def no_read_rules(paths) -> list:
    """Rules that deny Claude Code's Read tool each path, a file or a folder with all under it, as given and
    where its link leads."""
    return [r for x in full_paths(paths) for r in (f"Read(//{x.lstrip('/')})", rule("Read", x))]


def _zone(m, first: int):
    """The zone a reset time names (groups first..first+3 of _ZONE): a tzinfo; None for the machine's local
    time, when it names none; False when it names one hansa does not know."""
    from datetime import timedelta, timezone
    name, utc, z, offset = m.group(first, first + 1, first + 2, first + 3)
    if utc or z:
        return timezone.utc
    if offset:
        sign, digits = (-1 if offset[0] == "-" else 1), offset[1:].replace(":", "")
        return timezone(sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:])))
    if name:
        if name.upper() in ("UTC", "GMT"):
            return timezone.utc
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name)
        except Exception:  # noqa: BLE001 - not a zone (or no zone data): unsure of the time
            return False
    return None


def _hour(h: str, ampm) -> int:
    h = int(h)
    if not ampm:
        return h
    return -1 if not 1 <= h <= 12 else h % 12 + (12 if ampm[0].lower() == "p" else 0)


def reset_time(text: str, at: float | None = None):
    """When a provider's usage limit resets, as its error says (see RESET_UNIX and the forms after it): a Unix
    time, or None when the error gives none, or hansa is unsure of the one it gives. A time without a zone is the
    machine's local time; a clock time alone is its next coming."""
    try:
        return _reset_time(text, at)
    except Exception:  # noqa: BLE001 - a line hansa cannot read (an impossible offset, say): no reset time
        return None


def _reset_time(text, at):
    from datetime import datetime, timedelta
    at = time.time() if at is None else at
    text = str(text or "").replace(" ", " ").replace("\xa0", " ").replace("\xb7", " ")

    def when(y, mo, d, h, mi, s, tz):
        if tz is False or not (0 <= h <= 23 and 0 <= mi <= 59 and 0 <= s <= 59):
            return None
        try:
            return datetime(y, mo, d, h, mi, s, tzinfo=tz).timestamp()
        except (ValueError, OverflowError):
            return None

    found = None
    if m := RESET_UNIX.search(text):
        found = float(m.group(1))
    elif m := RESET_AFTER.search(text):
        found = at + int(m.group(1))
    elif m := RESET_IN.search(text):
        unit = m.group(2).lower()
        found = at + float(m.group(1)) * (3600 if unit.startswith("h") else 60 if unit.startswith("m") else 1)
    elif m := RESET_ISO.search(text):
        found = when(*map(int, m.group(1, 2, 3, 4, 5)), int(m.group(6) or 0), _zone(m, 7))
    elif m := RESET_DAY.search(text):
        tz = _zone(m, 7)
        args = (MONTHS.index(m.group(1).lower()[:3]) + 1, int(m.group(2)), _hour(m.group(4), m.group(6)),
                int(m.group(5) or 0), 0, tz)
        if m.group(3):
            found = when(int(m.group(3)), *args)
        else:  # the year that puts it nearest to now: Jan 2 read on Dec 31 is next year's, Dec 31 read on Jan 1 last
            year = datetime.fromtimestamp(at, tz or None).year
            near = [t for t in (when(y, *args) for y in (year - 1, year, year + 1)) if t is not None]
            found = min(near, key=lambda t: abs(t - at)) if near else None
    elif (m := RESET_CLOCK.search(text)) and (m.group(2) or m.group(3)):  # "resets 3" alone is no time
        tz = _zone(m, 4)
        if tz is not False:
            day = datetime.fromtimestamp(at, tz or None)
            found = when(day.year, day.month, day.day, _hour(m.group(1), m.group(3)), int(m.group(2) or 0), 0, tz)
            if found is not None and found < at - 3600:  # that time today has passed: tomorrow's
                found = (datetime.fromtimestamp(found, tz or None) + timedelta(days=1)).timestamp()
    if found is None or found < at - 3600:  # long past: not this limit's reset
        return None
    return found


def rest_provider(desk: Desk, s: dict, name: str, kind: str, why: str, text: str = "") -> float | None:
    """Rest an unusable provider. Credit has no end time; usage limits use their reset time and a minute
    (at most REST_CAP), else cool_minutes. Record and announce a credit rest once, across task finishes."""
    from .rests import record
    with desk.lock():
        if (desk.provider_state().get(name) or {}).get("credit_since"):
            return None  # neither another failure nor a coordinator's check can end a credit rest
        t = time.time()
        if kind == "credit":
            desk.set_provider_state(name, credit_since=t, cool_until=0, reason=desk.redact(why),
                                    rest_how="credit", rest_minutes=None, unreachable_since=0, unreachable_reason="")
            record(desk, name, credit_since=t, cool_until=0)
            line = (f"provider {name}: credit balance too low at {time.strftime('%H:%M', time.localtime(t))}; "
                    "it rests until you say on the page that it is topped up")
            desk.event("provider_credit", provider=name, since=t, text=line)
            return None
        if kind != "quota":
            until = t + 60 * KEY_REST_MINUTES
            desk.set_provider_state(name, cool_until=until, reason=why, rest_how="", rest_minutes=None)
            return until
        try:
            reset = reset_time(text, t)
        except Exception as e:  # noqa: BLE001 - only a detail: the rest falls back to the fixed length
            desk.event("detail_unread", provider=name, what="reset time", error=str(e)[:300])
            reset = None
        if reset is not None:
            until = min(max(reset, t) + 60, t + REST_CAP)
            desk.set_provider_state(name, cool_until=until, reason=why, rest_how="reset", rest_minutes=None)
        else:
            minutes = s.get("cool_minutes", 30)
            until = t + 60 * minutes
            desk.set_provider_state(name, cool_until=until, reason=why, rest_how="fixed", rest_minutes=minutes)
        record(desk, name, cool_until=until)
        return until


def credit_text(st: dict) -> str:
    """The principal alone ends a credit rest; the same words on the page and in the command views."""
    since = st.get("credit_since")
    return ("rests until the principal says it is topped up on the page; credit balance too low since "
            + time.strftime('%m-%d %H:%M', time.localtime(since))
            + ("; another desk found it" if st.get("shared_rest") else "")) if since else ""


def rest_text(st: dict) -> str:
    """Why a resting provider rests that long (its state, provider_state), or ''."""
    if st.get("rest_how") == "reset":
        return "the reset time its error gave, and a minute"
    if st.get("rest_how") == "fixed" and st.get("rest_minutes"):
        return f"no reset time in its error, so its rest of {st['rest_minutes']} minutes"
    return ""


def tries_used(attempts: list) -> int:
    """Earlier runs except free usage-limit returns, system kills and drops during a known outage."""
    return sum(1 for a in attempts if a.get("kind") not in ("killed", "credit")
               and (a.get("kind") != "limit" or a.get("counted"))
               and (a.get("kind") != "network" or a.get("counted", True)))


def task_providers(s: dict, lane: str, m: dict) -> list:
    """A dropped run waits for its own provider; other tasks keep their lane's provider order."""
    order = s["lanes"][lane]["providers"]
    last = (m.get("attempts") or [{}])[-1]
    return [last["provider"]] if last.get("kind") == "network" and last.get("provider") in order else order


def unreachable_text(st: dict) -> str:
    since = st.get("unreachable_since")
    if not since:
        return ""
    text = (f"unreachable since {time.strftime('%H:%M', time.localtime(since))} "
            f"({st.get('unreachable_reason', '')}); checked every {PROBE_MINUTES:g} minutes")
    check = st.get("check") or {}
    if check.get("at", 0) >= since:
        text += f"; last at {time.strftime('%H:%M', time.localtime(check['at']))}: {check['detail']}"
    return text


def refused_by(m: dict) -> set:
    """Providers that refused this task; it is not offered to them again."""
    return {a["provider"] for a in m.get("attempts", []) if a.get("kind") == "refused"}


def provider_problem(desk: Desk, s: dict, name: str) -> str:
    """Why this provider cannot take work right now; '' when it can."""
    p = s["providers"].get(name)
    if not p:
        return "not in settings"
    st = desk.provider_state().get(name, {})
    if st.get("credit_since"):
        return credit_text(st)
    if p["kind"] == "compatible":
        if not p.get("base_url"):
            return "no endpoint address"
        if not desk.has_key(name):
            return "no key"
    if st.get("cool_until", 0) > time.time():
        if str(st.get("reason", "")).startswith("key rejected"):
            return f"{st['reason']}; saving a new key brings it back"
        if str(st.get("reason", "")).startswith("not logged in"):
            if p["kind"] == "compatible":  # no login of its own: the saved key did not arrive
                return f"{st['reason']}; saving a new key brings it back, or `hansa ping {name}` once it arrives"
            # start Claude Code exactly as the worker does: on macOS a login made with
            # CLAUDE_CONFIG_DIR set, even to ~/.claude, may land in another keychain entry
            login = f"CLAUDE_CONFIG_DIR={shlex.quote(p['config_dir'])} claude" if p.get("config_dir") else "claude"
            how = f"`{login}`"
            if account.LINUX and account.uid() != 0:  # a principal on a server usually works as root
                who = account.name_of(account.uid())
                as_root = f"env {login}" if p.get("config_dir") else login
                how += f" as {who} (from root: `runuser -u {who} -- {as_root}`)"
            return f"{st['reason']}; log in with {how}, then `hansa ping {name}` brings it back"
        how = rest_text(st)
        return (f"resting until {time.strftime('%m-%d %H:%M', time.localtime(st['cool_until']))} ({st.get('reason', '')})"
                + (f": {how}" if how else ""))
    return unreachable_text(st)


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


def last_call(home: Path, sid: str, stopped: float) -> str:
    """The last tool call a session record shows the worker starting, whether or not it holds its result, and
    how many minutes before `stopped` it started: for a run stopped at the time ceiling. Empty when the record
    has no call or cannot be read."""
    found = None
    for d in _rows(home, sid) or []:
        content = (d.get("message") or {}).get("content") if d.get("type") == "assistant" else None
        for c in content if isinstance(content, list) else []:
            if isinstance(c, dict) and c.get("type") == "tool_use":
                found = (c, d.get("timestamp"))
    if not found:
        return ""
    c, stamp = found
    args = c.get("input") if isinstance(c.get("input"), dict) else {}
    name = str(c.get("name") or "?")
    if name == "Bash" and isinstance(args.get("command"), str):
        what = " ".join(args["command"].split())
        what = f"Bash: {what[:300]}" + ("…" if len(what) > 300 else "")
    else:
        where = next((args[k] for k in ("file_path", "notebook_path", "path", "pattern") if isinstance(args.get(k), str)), "")
        what = f"{name}: {where}" if where else name
    when = ""
    try:
        began = datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
        when = f", {max(0, round((stopped - began) / 60))} minutes before the stop"
    except ValueError:
        pass
    return f"The last call it started{when}: {what}"


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


def background_left(home: Path, sid: str, after: int = 0) -> list:
    """What a worker started in the background (a Bash command or an agent) and had no notice of
    ending by the end of its session record, by description: it was still running when the worker
    ended, so the worker may have written its report without it. Empty when the record cannot be read.
    Only what was started in the record's rows from `after` on (a resumed run's own: see resume)."""
    rows = _rows(home, sid) or []
    started, ended = {}, set()  # background id: description
    calls = {}  # tool use id: (tool name, input)
    for i, d in enumerate(rows):
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
                if bid and i >= after:
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
    """(report, [what it wrote in each later turn]) when the session ended on turns a background notice
    opened (a watch it started timing out, say): Claude Code then takes its answer to the last notice as
    the result, and the report is its last message before the first of those notices. Whether or not it
    used tools in those turns, what it wrote there is kept, in order: a correction, or the real report
    of a worker that ended a turn waiting. None when the session did not end so, or its record cannot be read."""
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
    # that came in while the worker was busy is part of the work, and the walk back goes past it: only a notice
    # that opened a turn after the worker had stopped is after the report.
    openers = [i for i, d in enumerate(rows) if d.get("type") == "user" and d.get("turnOrigin")]
    cuts = []  # the turns those notices opened, by their first row
    while openers and rows[openers[-1]]["turnOrigin"] == "task_notification":
        i = openers.pop()
        if i > 0 and rows[i - 1].get("type") == "assistant" and not blocks(rows[i - 1], "tool_use"):
            cuts.insert(0, i)
    if not cuts:
        return None
    said = [(i, (rows[i].get("message") or {}).get("id")) for i in range(cuts[0]) if texts(rows[i])]
    if not said:
        return None
    last_id = said[-1][1]
    ids = [i for i, mid in said if mid == last_id] if last_id else [said[-1][0]]
    report = "\n\n".join(t for i in ids for t in texts(rows[i]))
    later = ["\n\n".join(t for d in rows[i:j] for t in texts(d)) for i, j in zip(cuts, cuts[1:] + [len(rows)])]
    return report, [x for x in later if x]


def progress_of(desk: Desk, m: dict) -> dict:
    """How far a running worker has got: model replies so far, minutes since it last did
    anything, and the last thing it wrote. hansa records turns and tokens only at the end."""
    p = desk.settings()["providers"].get(m.get("provider") or "")
    home = config_home(desk, m["provider"], p) if p else None
    sid = m.get("session_id", "")
    texts, replies, last, _ = _record(home, sid) if home else ([], 0, None, {})
    note = texts[-1].splitlines()[0] if texts else ""
    if last is None and home and sid:
        try:
            d = json.loads((home / "progress" / f"{sid}.json").read_text(encoding="utf-8"))
            if (not isinstance(d, dict) or type(d.get("replies")) is not int
                    or type(d.get("last")) not in (int, float) or not math.isfinite(d["last"])
                    or not isinstance(d.get("note"), str)):
                raise ValueError("malformed progress")
            replies, last, note = d["replies"], d["last"], d["note"]
        except (OSError, ValueError, UnicodeError, OverflowError):
            pass
    return {"replies": replies, "idle_min": int((time.time() - last) // 60) if last is not None else None,
            "note": desk.redact(note[:160])}


def provider_env(desk: Desk, name: str, p: dict) -> dict:
    """Environment for one Claude Code run on this provider. Starts clean, so a
    worker never inherits keys that are not its own."""
    env = {k: os.environ[k] for k in BASE_ENV if k in os.environ}
    env["TERM"] = "dumb"
    # 8 tries, with waits that double: Claude Code rides out a network drop of a minute or two inside
    # the run (its retries continue the same conversation; with 3 it gave up after about 20 seconds and
    # the run's work was lost). The cost: a rejected key takes about a minute longer to fail.
    # check_provider, which must answer at once, sets 0.
    env["CLAUDE_CODE_MAX_RETRIES"] = "8"
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
    CLAUDE.md or .claude/rules from the folders above the checkout (the desk's card, the coordinator's, among them,
    and the copy of it hansa notify on puts in a session's folder that holds the desk).
    `settings` go into the same --settings object (a worker's sandbox, say)."""
    above = []
    for d in dict.fromkeys([*Path(cwd).parents, *Path(cwd).resolve().parents]):
        above += [str(d / "CLAUDE.md"), str(d / "CLAUDE.local.md"), str(d / ".claude" / "CLAUDE.md"),
                  str(d / ".claude" / "rules" / "**")]
    return ["--setting-sources", "project,local", "--strict-mcp-config",
            "--settings", json.dumps({"claudeMdExcludes": above, **(settings or {})})]


def full_paths(paths) -> list:
    """Each path as given and with links resolved (/tmp is /private/tmp on macOS), once; as given only where
    its links cannot be followed."""
    return list(dict.fromkeys(str(x) for q in paths for x in (Path(q), Path(resolved(str(q)) or q))))


def git_write_deny(wt: Path) -> list:
    """The paths in git's folders for the worktree `wt` that a sandboxed worker may not write, worked
    out now: Claude Code opens a linked worktree's shared git folder for writing. The worktree's own
    .git file is among them: pointed at a git folder of the worker's making, it would run the worker's
    hooks and settings the next time hansa runs git there, outside the sandbox. Then patterns
    (others_than) for what is made in those folders later, while the run lasts."""
    dot = [Path(wt) / ".git"]
    kept = full_paths([*_git_folder_deny(wt), *_git_others(wt), *dot])
    return kept + [p for p in _git_patterns(wt) if p not in kept]


def git_folders(wt: Path) -> tuple:
    """(the repository's common git folder, the worktree's own). A main checkout has no folder of its own
    (and no commondir or gitdir): its folder is the common one. Within one start (git_folders_kept) asked of
    git once for each state of the worktree's .git and the commondir it leads to (_git_folders_key)."""
    kept = _GIT_FOLDERS.get()
    key = _git_folders_key(wt) if kept is not None else None
    if key is not None and key in kept:
        return kept[key]
    common = Path(wt) / git("rev-parse", "--git-common-dir", cwd=wt)  # relative to wt, or absolute
    out = common, Path(git("rev-parse", "--absolute-git-dir", cwd=wt))
    if key is not None and key == _git_folders_key(wt):  # not changed while git was asked
        kept[key] = out
    return out


def _git_folders_key(wt: Path):
    """What git's answer to git_folders rests on, read as plain files and never through git: the worktree's path,
    its .git (a file's bytes, a folder's identity) and the commondir of the git folder it leads to. None when
    one cannot be read: then git is asked."""
    wt = Path(wt).absolute()
    dot = wt / ".git"
    try:
        st = os.lstat(dot)
        if stat.S_ISREG(st.st_mode):
            with os.fdopen(os.open(dot, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)), "rb") as fh:
                held = fh.read(4097)
            text = held.decode("utf-8", "replace").strip()
            admin = Path(text[len("gitdir:"):].strip()) if text.startswith("gitdir:") else None
            admin = wt / admin if admin is not None and not admin.is_absolute() else admin
        elif stat.S_ISDIR(st.st_mode):
            held, admin = ("folder", st.st_dev, st.st_ino), dot
        else:
            return None
        common = admin / "commondir" if admin is not None else None
        there = common.read_bytes() if common is not None and common.is_file() else b""
    except OSError:
        return None
    return str(wt), held, there


def _git_folder_deny(wt: Path) -> list:
    """The named paths of git_write_deny in git's folders: the repository's common folder and the worktree's
    own. make_git_write_deny makes the missing ones."""
    common, own = git_folders(wt)
    linked = own.resolve() != common.resolve()
    in_common, in_own = GIT_COMMON_DENY, GIT_OWN_DENY
    # GIT_OPS on Linux only when there:
    # the sandbox there would put a device where one is missing, and git would take that for a merge under way
    return [*(common / f for f in in_common),
            *(own / f for f in in_own if linked and not (account.LINUX and f in GIT_OPS and not os.path.lexists(own / f)))]


def _git_open(wt: Path) -> list:
    """What of the common folder git_write_deny does not keep whole, as (folder, the names in it left open):
    the worktree's own folder under worktrees/."""
    common, own = git_folders(wt)
    if own.resolve() == common.resolve():
        return []
    out = [(common, ("worktrees",)), (common / "worktrees", (own.name,))]
    return out


def _git_others(wt: Path) -> list:
    """Every entry there is now in a folder of _git_open but the names left open: other worktrees' folders,
    other refs and tags, other branches' logs, and what else git keeps in the common folder."""
    out = []
    for folder, names in _git_open(wt):
        try:
            out += [folder / n for n in sorted(os.listdir(folder)) if n not in names]
        except OSError:  # not there (no tags yet, say): nothing in it to keep
            pass
    return out


def others_than(folder: Path, names) -> list:
    """Patterns (globs, as the sandbox reads them) for each name in `folder` but `names`, made there now or
    later: for each start of one of `names`, a name going on with another character (NAME_CHARS) and, when
    it is not one of them, that start itself. None when a name has a character outside NAME_CHARS and -."""
    if any(c not in NAME_CHARS + "-" for n in names for c in n):
        return []
    out = []

    def walk(start, rest):
        nexts = sorted({s[0] for s in rest if s})
        out.extend(f"{folder}/{start}[{c}]*" for c in name_classes(nexts))
        if start and "" not in rest:
            out.append(f"{folder}/{start}")
        for c in nexts:
            walk(start + c, [s[1:] for s in rest if s and s[0] == c])
    walk("", list(names))
    return out


def name_classes(left_out) -> list:
    """What goes between [ and ] for the characters of NAME_CHARS and - but `left_out`, as macOS's sandbox reads
    a class: - first when kept, then digits, capitals and small letters each as runs (0-9A-Za-np-z), then . and _;
    one class, or several of at most CLASS_MAX elements each, which together hold them all: then the small letters
    go in one of their own (for o, r, l and w: -0-9A-KMNPQS-VX-Z._ and a-kmnpqs-vx-z), each its own pattern.
    Three facts of that reading shape it. It matches paths without regard to case, so [A] holds a and [B-Z] holds
    c: a letter left out is left out in both cases (for ("a",): -0-9B-Zb-z._). A - just before ] is a range up
    to the end of the character set ([_-] holds every small letter), while a - first is a plain -. And a class
    of more than CLASS_MAX elements matches nothing. macOS also refuses a sandbox whose profile holds two or more
    classes listing some sixty characters one by one; ranges it takes. A negated class cannot stand in: srt
    escapes ^ (so [^w] holds ^ and w), and [!w] holds ! and w."""
    gone = {c.lower() for c in left_out} | {c.upper() for c in left_out}

    def runs(group):  # the elements of a class for the characters of `group` it keeps
        kept, out, i = [c in NAME_CHARS + "-" and c not in gone for c in group], [], 0
        while i < len(group):
            if not kept[i]:
                i += 1
                continue
            j = i
            while j + 1 < len(group) and kept[j + 1]:
                j += 1
            out += [group[i]] if i == j else [group[i], group[j]] if j == i + 1 else [f"{group[i]}-{group[j]}"]
            i = j + 1
        return out
    dash, digits, capitals, small = runs("-"), runs("0123456789"), runs("ABCDEFGHIJKLMNOPQRSTUVWXYZ"), runs("abcdefghijklmnopqrstuvwxyz")
    rest = [c for c in "._" if c in NAME_CHARS and c not in gone]
    if len(dash + digits + capitals + small + rest) <= CLASS_MAX:
        return ["".join(dash + digits + capitals + small + rest)] if dash + digits + capitals + small + rest else []
    out = []
    for group in (dash + digits + capitals + rest, small):
        out += ["".join(group[i:i + CLASS_MAX]) for i in range(0, len(group), CLASS_MAX)]
    return [x for x in out if x]


def _git_patterns(wt: Path) -> list:
    """others_than for each folder of _git_open, as given and with links resolved: what is made there while
    the run lasts (another task's branch, another worktree's folder, git's commondir) is kept too."""
    return [p for folder, names in _git_open(wt) for f in full_paths([folder])
            for p in others_than(Path(f), names)]


def make_git_write_deny(wt: Path) -> None:
    """Create each path of git_write_deny that is missing, empty. On Linux the sandbox (bwrap) puts a device
    nobody can open where a listed path does not exist, and git then fails reading it: packed-refs, say,
    which a repository made with git init has only once refs are packed. Git takes an empty packed-refs
    as none. A path that exists is left as it is."""
    for f in _git_folder_deny(wt):
        if os.path.lexists(f) or f.name in GIT_OPS:  # an empty MERGE_HEAD would say a merge is under way
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


class GitFolderChanged(GitFileChanged):
    """What the worktree's .git file leads to is not what git made: its git folder's commondir or gitdir (the
    folder of the repository's settings and hooks, and the worktree's own .git), or a commondir in the common
    folder. str(): what the principal is told. `right`: what the file held, None where there was none. `aside`:
    what was found may be moved aside (and `right` written there) whatever its name: a branch's ref, a merge
    under way (git_file_changed)."""

    def __init__(self, wt: Path, path: Path, found: str, right, aside: bool = False) -> None:
        self.wt, self.path, self.found, self.right, self.aside = Path(wt), Path(path), found, right, aside
        self.expected = f"{path}: {'none' if right is None else right}"
        RuntimeError.__init__(self, f"the worktree's git folder was changed: {path} {found}; "
                                    f"hansa ran no git in {wt}")


def _plain_text(path: Path):
    """(what the file holds, stripped, or None, what was found), read as a plain file, never through a link."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None, "is gone"
    if not stat.S_ISREG(st.st_mode):
        return None, "is no longer a plain file"
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)), "rb") as fh:
            return fh.read(4096).decode("utf-8", "replace").strip(), ""
    except OSError as e:
        return None, f"cannot be read ({e.strerror})"


def check_git_folder(wt: Path, line: str) -> None:
    """Raise GitFolderChanged unless the git folder the .git line `line` names is still as git made it: a
    folder under the common one's worktrees/, whose commondir leads back to that common folder and whose
    gitdir names this worktree's .git, and a common folder with no commondir of its own. Whatever writes
    there could otherwise lead hansa's git to settings and hooks of its making."""
    wt = Path(wt)
    admin = Path(line[len("gitdir:"):].strip())
    admin = admin if admin.is_absolute() else wt / admin
    try:
        st = os.lstat(admin)
    except OSError:
        st = None
    if st is None or not stat.S_ISDIR(st.st_mode):
        raise GitFolderChanged(wt, admin, "is gone" if st is None else "is no longer a folder", None)
    common = admin.resolve().parent.parent
    for name, right, wanted in (("commondir", "../..", common), ("gitdir", str(wt.resolve() / ".git"), wt.resolve() / ".git")):
        text, what = _plain_text(admin / name)
        if text is None:
            raise GitFolderChanged(wt, admin / name, what, right)
        if not text or (admin / text).resolve() != wanted:
            raise GitFolderChanged(wt, admin / name, f"now holds {text[:200]!r}", right)
    if os.path.lexists(common / "commondir"):  # git would take the repository's settings and hooks from there
        raise GitFolderChanged(wt, common / "commondir", "was made", None)
    # settings of this worktree's own, which git obeys once extensions.worktreeConfig is on: hansa makes it
    # empty (make_git_write_deny), and no worker may write it
    if os.path.lexists(admin / "config.worktree"):
        text, what = _plain_text(admin / "config.worktree")
        if text is None or text:
            raise GitFolderChanged(wt, admin / "config.worktree", what or f"now holds {text[:200]!r}", "")


def worktree_config_on(common: Path) -> bool:
    """Whether the repository's settings turn extensions.worktreeConfig on: git then also obeys each worktree's
    config.worktree. Read by git from that one file, with nothing it includes, and no repository around it."""
    try:
        with open(Path(common) / "config", "rb") as fh:
            if b"worktreeconfig" not in fh.read().lower():  # git spells names in any case; not there, not on
                return False
    except OSError:
        return False
    r = subprocess.run(["git", "config", "--file", str(Path(common) / "config"), "--type=bool", "--get",
                        "extensions.worktreeConfig"], cwd="/", capture_output=True, text=True, errors="replace",
                       env={**without_session(os.environ), "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
    return r.stdout.strip() == "true"


def check_git_settings(desk: Desk, wt: Path, line: str) -> None:
    """Raise GitFolderChanged when the repository's settings for worktrees are not as hansa first saw them (it
    remembers them, in .hansa/git-settings.json, the first time it runs git in one of its worktrees):
    extensions.worktreeConfig turned on since, or the common folder's config.worktree (the main checkout's,
    which git then obeys there) changed and not empty, as hansa makes it."""
    admin = Path(line[len("gitdir:"):].strip())
    common = (admin if admin.is_absolute() else Path(wt) / admin).resolve().parent.parent
    on = worktree_config_on(common)
    cw, cw_what = _plain_text(common / "config.worktree") if os.path.lexists(common / "config.worktree") else ("", "")
    first = (read_json(desk.state_dir / "git-settings.json", {}) or {}).get(str(common))
    if first is None:
        with desk.lock():
            seen = read_json(desk.state_dir / "git-settings.json", {}) or {}
            first = seen.setdefault(str(common), {"worktree_config": on, "config_worktree": cw})
            desk.state_dir.mkdir(exist_ok=True)
            write_json(desk.state_dir / "git-settings.json", seen)
    if on and not first.get("worktree_config"):
        raise GitFolderChanged(wt, common / "config", "now turns extensions.worktreeConfig on, which hansa did not "
                                                      "see on before (git then obeys each worktree's config.worktree)", None)
    if cw is None or cw and cw != first.get("config_worktree"):
        raise GitFolderChanged(wt, common / "config.worktree", cw_what or f"now holds {cw[:200]!r}",
                               first.get("config_worktree") or "")


def check_head(desk: Desk, wt: Path, branch: str) -> None:
    """Raise GitFolderChanged unless the worktree's HEAD names `branch`, the one hansa made for its run: hansa
    commits and merges on whatever HEAD names, so a HEAD pointed at the base would move the base. Read as a
    plain file from the worktree's own git folder, after check_git_file."""
    check_git_file(desk, wt)
    head = own_git_folder(desk, wt) / "HEAD"
    right = f"ref: refs/heads/{branch}"
    text, what = _plain_text(head)
    if text != right:
        raise GitFolderChanged(wt, head, what or f"now holds {text[:200]!r}, not the task's branch {branch}", right)


def own_git_folder(desk: Desk, wt: Path) -> Path:
    """The worktree's own git folder, as the .git line hansa remembers for it names it (after check_git_file)."""
    admin = Path(expected_git_line(desk, Path(wt))[len("gitdir:"):].strip())
    return admin if admin.is_absolute() else Path(wt) / admin


def check_branch_ref(desk: Desk, wt: Path, branch: str) -> str:
    """The id `branch`'s ref holds, read as a plain file and never through a link: a plain commit id, or, when there
    is none, the one packed-refs (which nothing sandboxed may write) holds. Raise GitFolderChanged otherwise: a ref
    holding 'ref: refs/heads/main' would lead git, which follows it, from the run's branch onto the base, while HEAD
    still names the run's branch (check_head). After check_git_file.
    Put right (git_file_changed) to the last id the branch's log names, where it names one."""
    check_git_file(desk, wt)
    common = own_git_folder(desk, wt).resolve().parent.parent
    ref = common / "refs" / "heads" / branch
    if os.path.lexists(ref):
        text, what = _plain_text(ref)
        if text is not None and OID.fullmatch(text):
            return text
        raise GitFolderChanged(wt, ref, what or f"now holds {text[:200]!r}, not a commit id",
                               _last_logged(common, branch), aside=True)
    packed = _packed(common, f"refs/heads/{branch}")
    if not packed:
        raise GitFolderChanged(wt, ref, "is gone, and packed-refs does not hold it either",
                               _last_logged(common, branch), aside=True)
    return packed


def _packed(common: Path, ref: str) -> str:
    """The id packed-refs holds for `ref`, or ''."""
    try:
        if not stat.S_ISREG(os.lstat(common / "packed-refs").st_mode):
            return ""
        text = (common / "packed-refs").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    for line in text.splitlines():
        oid, _, name = line.partition(" ")
        if name == ref and OID.fullmatch(oid):
            return oid
    return ""


def _ref_now(common: Path, branch: str):
    """(the id `branch`'s ref holds, or None, what was found), read as check_branch_ref reads it: the ref file's own
    contents, never through a link, or packed-refs when there is no ref file."""
    ref = common / "refs" / "heads" / branch
    if os.path.lexists(ref):
        text, what = _plain_text(ref)
        return (text, "") if text is not None and OID.fullmatch(text) else (
            None, what or f"now holds {text[:200]!r}, not a commit id")
    packed = _packed(common, f"refs/heads/{branch}")
    return (packed, "") if packed else (None, "is gone, and packed-refs does not hold it either")


def branch_holds(repo, m: dict) -> str:
    """'' when the task's branch still holds `head`, the commit hansa recorded as its work when its run ended
    (finish): what is verified, merged, continued from and shown is that commit, never whatever the branch's name
    leads to now. Else the line saying what the branch holds instead. The ref is read in the repository's common
    folder as a plain file, never through a link (_ref_now)."""
    branch = m.get("branch") or f"task/{m['id']}"
    try:
        common = Path(repo) / git("rev-parse", "--git-common-dir", cwd=repo)  # relative to repo, or absolute
    except (RuntimeError, OSError) as e:
        return f"{branch} cannot be read in {repo} ({str(e)[:200]})"
    text, what = _ref_now(common.resolve(), branch)
    if text == m.get("head"):
        return ""
    return (f"{branch} no longer holds {(m.get('head') or '')[:12]}, the commit hansa recorded as {m['id']}'s work "
            f"when its run ended: its ref {what or 'now holds ' + text}")


def _last_logged(common: Path, branch: str):
    """The id the branch's log last moved it to, or None."""
    log = common / "logs" / "refs" / "heads" / branch
    try:
        if not stat.S_ISREG(os.lstat(log).st_mode):
            return None
        with os.fdopen(os.open(log, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)), "rb") as fh:
            fh.seek(max(0, os.fstat(fh.fileno()).st_size - 65536))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    new = lines[-1].split(" ")[1] if lines and len(lines[-1].split(" ")) > 1 else ""
    return new if OID.fullmatch(new) else None


def check_no_operation(desk: Desk, wt: Path, merging: str = "") -> str:
    """Raise GitFolderChanged when the worktree's own folder says a merge, cherry-pick, revert or rebase is under
    way (GIT_OPS): hansa starts none in a worker's or a verification's worktree but the one below, and git commit
    would take a MERGE_HEAD a worker wrote for a second parent. `merging`: the base's tip a `retry --update` run's
    merge left there (UPDATE_SCRIPT): that MERGE_HEAD, and its MERGE_MODE, are hansa's. Returns `merging` when that
    merge is under way, else ''. What was found is moved aside (git_file_changed). After check_git_file."""
    check_git_file(desk, wt)
    own, merged = own_git_folder(desk, wt), ""
    for name in GIT_OPS:
        p = own / name
        if not os.path.lexists(p):
            continue
        text, what = _plain_text(p)
        if merging and name == "MERGE_HEAD" and text == merging:
            merged = merging
            continue
        if merging and name == "MERGE_MODE" and text is not None:
            continue
        raise GitFolderChanged(wt, p, f"was made ({what or 'it holds ' + repr(text[:200])}): it says a merge, "
                                      f"cherry-pick, revert or rebase is under way, which hansa did not start "
                                      f"there", None, aside=True)
    if not merged and os.path.lexists(own / "MERGE_MODE"):  # a MERGE_MODE kept for a MERGE_HEAD that is not there
        raise GitFolderChanged(wt, own / "MERGE_MODE", "was made without hansa's merge", None, aside=True)
    return merged


def check_main_checkout(repo: Path) -> None:
    """For git in a checkout whose .git is a folder (the lane's own): raise RuntimeError when that folder holds
    a commondir, which git would follow to settings and hooks elsewhere. Git makes none there."""
    dot = Path(repo) / ".git"
    try:
        is_dir = stat.S_ISDIR(os.lstat(dot).st_mode)
    except OSError:
        return
    if is_dir and os.path.lexists(dot / "commondir"):
        raise RuntimeError(f"{dot / 'commondir'} was made, which git does not make there: it would lead git to "
                           f"another folder's settings and hooks, so hansa runs no git in {repo}; look at it and "
                           f"remove it to go on")


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
    check_git_folder(wt, line)
    check_git_settings(desk, wt, line)


def git_file_changed(desk: Desk, tid: str, e: GitFileChanged) -> str:
    """After a worktree's .git was found changed: keep what was found in the task's raw folder, put the
    right file back so the lane works again, record it, and say it in plain words."""
    dot = e.wt / ".git"
    keep = desk.raw_dir(tid) / f"worktree-git-{int(time.time() * 1000)}"
    keep.mkdir(parents=True, exist_ok=True)
    if isinstance(e, GitFolderChanged):  # a commondir or gitdir: what was found is kept, git's line put back
        write_text(keep / "what-was-found.txt", f"{e.path}: {e.found}\nexpected: {e.expected}\n")
        text = _plain_text(e.path)[0]
        target = e.path.parent / text if text and e.path.name == "commondir" else None
        for name in ("config", "hooks") if target else ():  # the settings and hooks hansa's git would have run
            try:
                if (target / name).is_dir():
                    shutil.copytree(target / name, keep / name, symlinks=True, copy_function=copy_file)
                elif (target / name).is_file():
                    copy_file(target / name, keep / name)
            except OSError:
                pass
        fixable = e.path.parent.is_dir() and (
            e.aside and (os.path.lexists(e.path) or e.right is not None)
            or e.path.name in ("commondir", "gitdir", "config.worktree", "HEAD") and not os.path.isdir(e.path))
        if fixable:
            if os.path.lexists(e.path):
                shutil.move(str(e.path), str(keep / f"found.{e.path.name}"))
            if e.right is not None:
                e.path.write_text(e.right + "\n" if e.right else "", encoding="utf-8")
        desk.event("worktree_git_changed", task=tid, worktree=str(e.wt), found=f"{e.path} {e.found}",
                   expected=e.expected)
        return (f"{e}. What was found is kept in .hansa/runs/{tid}/{keep.name}; " +
                ("the right file is back, so the lane's next task runs normally" if fixable else
                 "it is left as it was found, and hansa runs no git there until it is put right"))
    write_text(keep / "what-was-found.txt", f"{dot}: {e.found}\nexpected: {e.expected}\n")
    if e.found.startswith("it now points to "):  # the settings and hooks hansa's git would have run
        target = Path(e.found[len("it now points to "):])
        target = target if target.is_absolute() else e.wt / target
        for name in ("config", "hooks"):
            try:
                if (target / name).is_dir():
                    shutil.copytree(target / name, keep / name, symlinks=True, copy_function=copy_file)
                elif (target / name).is_file():
                    copy_file(target / name, keep / name)
            except OSError:
                pass
    if os.path.lexists(dot):
        shutil.move(str(dot), str(keep / "found.git"))
    dot.write_text(e.expected + "\n", encoding="utf-8")
    desk.event("worktree_git_changed", task=tid, worktree=str(e.wt), found=e.found, expected=e.expected)
    return (f"{e}. What was found is kept in .hansa/runs/{tid}/{keep.name}; the right file is back, "
            f"so the lane's next task runs normally")


def wgit(desk: Desk, wt: Path, *args, env=None) -> str:
    """git in a worktree, run only after its .git file is checked."""
    check_git_file(desk, wt)
    return git(*args, cwd=wt, env=env)


def sandbox_leftovers(desk: Desk, wt: Path) -> list:
    """Untracked files a worker's sandbox left in the worktree: under .claude/ its .cc-writes and the empty
    placeholders for what it protects, and at the top the empty SANDBOX_PLACEHOLDERS. Nobody's work, so
    never committed."""
    out = wgit(desk, wt, "ls-files", "--others", "--exclude-standard", "-z", "--", ".claude",
               *(f":(top,literal){n}" for n in SANDBOX_PLACEHOLDERS))
    return [f for f in out.split("\0") if f and (f.startswith(".claude/") or f in SANDBOX_PLACEHOLDERS)
            and (Path(f).name == ".cc-writes" or (wt / f).is_file() and not (wt / f).is_symlink()
                 and (wt / f).stat().st_size == 0)]


class SandboxPathChanged(RuntimeError):
    """The index changes a path the worker's sandbox protects on disk."""


def sandbox_protected(path: str) -> bool:
    return path.split("/", 1)[0].lower() in SANDBOX_PROTECTED_PATHS


def stage_work(desk: Desk, wt: Path, old: str, merged=()) -> list:
    """Stage work, leave out placeholders and refuse protected changes before any commit or lane removal."""
    left = sandbox_leftovers(desk, wt)  # untracked until the add
    wgit(desk, wt, "add", "-A")
    # A worker's git add may already have staged the sandbox's empty placeholders. Inspect the index,
    # not the disk: skip-worktree can hide an entry whose blob was put there with update-index.
    held = set(wgit(desk, wt, "ls-tree", "-r", "--name-only", "-z", old).split("\0"))
    for entry in wgit(desk, wt, "ls-files", "--stage", "-z").split("\0"):
        if not entry:
            continue
        info, path = entry.split("\t", 1)
        mode, blob, stage = info.split()
        if sandbox_protected(path) and path not in held and stage == "0" and mode in ("100644", "100755") \
                and wgit(desk, wt, "cat-file", "-s", blob) == "0":
            left.append(path)
    if left:
        wgit(desk, wt, "update-index", "--force-remove", "--", *dict.fromkeys(left))
    paths = [p for p in wgit(desk, wt, "diff", "--cached", "--name-only", "--no-renames", "-z", old).split("\0") if p]
    refused = {p for p in paths if sandbox_protected(p)}
    for base in merged:  # content AND mode (including absence) equal to the merged base came from it
        different = set(wgit(desk, wt, "diff", "--cached", "--name-only", "--no-renames", "-z", base).split("\0"))
        refused.intersection_update(different)
    if refused:
        raise SandboxPathChanged(
            f"refusing to commit changes to protected paths: {', '.join(sorted(refused))}; the workers' sandbox "
            "does not let them write these paths, so the changes can only have come through git's index; "
            "nothing was committed; the work is left staged. To change these paths, ask the principal: "
            "they change them outside a task")
    return paths


def commit_work(desk: Desk, wt: Path, message: str, branch: str, merged=(), ident=None) -> str:
    """Commit everything changed in the worktree but what a sandbox left there on `branch`, the one hansa made for
    the run; nothing when that is all and nothing is `merged`. The commit is made from the index with the branch's
    tip (and `merged`, a merge hansa made there) for parents, never with a MERGE_HEAD git would take from the
    worktree's folder, and exactly that branch is moved, from that tip to it, with update-ref's check of the old id
    and without following a link. The tip is the id the branch's ref file (or packed-refs) holds, read by
    check_branch_ref, never through a link: one made in the meantime fails update-ref's check. Afterwards the ref
    must hold the new commit, whose first parent is that tip; otherwise GitFolderChanged, the ref is put back to
    the tip (git_file_changed) and the work is left staged. What a merge left there is cleared then. Returns the
    commit the branch then holds: the new one, or the tip when nothing was committed (finish records it). ident:
    the author and committer (identity_env of the lane)."""
    old = check_branch_ref(desk, wt, branch)
    paths = stage_work(desk, wt, old, merged)
    if not paths and not merged:
        return old
    ref = f"refs/heads/{branch}"
    new = wgit(desk, wt, "commit-tree", wgit(desk, wt, "write-tree"), "-p", old,
               *(a for m in merged for a in ("-p", m)), "-m", message.strip(), env=ident)
    common = own_git_folder(desk, wt).resolve().parent.parent
    path = common / "refs" / "heads" / branch
    try:
        wgit(desk, wt, "update-ref", "--no-deref", "-m", f"commit: {(message.strip().splitlines() or [''])[0]}",
             ref, new, old)
    except RuntimeError as e:
        text, what = _plain_text(path)
        if text == old or what == "is gone" and _packed(common, ref) == old:
            raise  # the ref is as hansa read it: git failed for another reason
        raise GitFolderChanged(wt, path, f"changed while hansa committed: it {what or 'now holds ' + repr(text[:200])}"
                                         f", not {old}; the work is left staged", old, aside=True) from e
    text, what = _plain_text(path)
    parent = wgit(desk, wt, "rev-list", "--parents", "-n", "1", new).split()[1:2]
    if text != new or parent != [old]:
        raise GitFolderChanged(wt, path, f"changed while hansa committed: it {what or 'now holds ' + repr(text[:200])}"
                                         f", not {new}, a commit on {old}; the work is left staged", old, aside=True)
    own = own_git_folder(desk, wt)
    for name in GIT_MERGE_LEFT:
        if os.path.isfile(own / name) and not os.path.islink(own / name):
            (own / name).unlink()
    return new


def is_local(host: str) -> bool:
    return host.lower().split(":")[0] in LOCAL_HOSTS


def write_list(desk: Desk, lane: dict) -> tuple:
    """A lane's write list as a run may be given it: (the folders that pass the fences now, a line for each
    left out). The list is checked on saving, but one saved before a fence was there, or a link pointed
    elsewhere since, would still open what the sandbox keeps closed."""
    good, left = [], []
    given = lane.get("write", [])
    if not isinstance(given, list) or not all(isinstance(x, str) and x.startswith("/") for x in given):
        return [], ["left out of this run: the whole write list: it must be a list of full paths starting with / "
                    "(the write list is changed with hansa lane set)"]
    s = desk.settings()
    kept = kept_from_workers(s, desk.root)[0]
    for x in given:
        said = write_problems([x], lane.get("repo"), desk.root, s["providers"], kept, lane.get("env"),
                              other_envs(s["lanes"], lane))
        if said:
            left.append(f"left out of this run: {said[0].removeprefix('write cannot include ')} "
                        "(the write list is changed with hansa lane set)")
        else:
            good.append(x)
    return good, left


def lane_env(desk: Desk, lane: dict) -> tuple:
    """A lane's env as a run may be given it: (the variables that pass the checks now, a line for each left
    out). It reaches only what runs in the sandbox (in_sandbox, env_file); the checks are a second line, so
    one saved before a check was there is not passed on."""
    return checked_env(lane.get("env", {}), desk.root)


def sandbox_env(desk: Desk, lane: dict) -> dict:
    """The lane's env as the commands in the sandbox get it: what passes the checks, without the temporary
    folder, which is each run's own."""
    return {k: v for k, v in lane_env(desk, lane)[0].items() if k not in RUN_TMP_VARS}


# What runs in the sandbox before a lane's setup, check and merge check (in_sandbox), and in front of each of a
# worker's Bash commands (env_file), after the lane's env is set: Java and Maven made to work there
# (docs/java-and-maven.md). The sandbox's proxy wants a user and password, which it puts only in HTTPS_PROXY and
# HTTP_PROXY, and Maven reads neither: so a Maven global settings file in $TMPDIR names that proxy, for https and
# http, and MAVEN_ARGS gets `-gs <it>`. Java ignores TMPDIR, and on macOS the sandbox does not let it write its
# own: so JAVA_TOOL_OPTIONS, which every JVM reads, gets -Djava.io.tmpdir. What was there is kept, and nothing is
# added twice. A part that cannot be done (no TMPDIR, no proxy, a path with a space) is skipped, never failing the
# run; it prints nothing, and the password goes only into the file (by printf, a shell builtin, and awk's stdin:
# on no command line). POSIX sh, that bash and zsh read alike; its names start with hansa_ and are unset after it.
JAVA_PRELUDE = r"""hansa_x() { printf '%s' "$1" | LC_ALL=C awk 'BEGIN { RS = "\001"; h = "0123456789abcdef" }
{ s = $0; o = ""
  while ((i = index(s, "%")) > 0) {
    a = index(h, tolower(substr(s, i + 1, 1))); b = index(h, tolower(substr(s, i + 2, 1)))
    if (a && b) { o = o substr(s, 1, i - 1) sprintf("%c", (a - 1) * 16 + b - 1); s = substr(s, i + 3) }
    else { o = o substr(s, 1, i); s = substr(s, i + 1) } }
  s = o s; gsub(/&/, "\\&amp;", s); gsub(/</, "\\&lt;", s); gsub(/>/, "\\&gt;", s)
  gsub(/"/, "\\&quot;", s); gsub(/'\''/, "\\&apos;", s); printf "%s", s }' 2>/dev/null; }
hansa_t=${TMPDIR:-}
case $hansa_t in *[[:space:]]*) hansa_t= ;; esac
[ -z "$hansa_t" ] || [ -d "$hansa_t" ] || hansa_t=
if [ -n "$hansa_t" ]; then
  case " ${JAVA_TOOL_OPTIONS:-} " in
    *" -Djava.io.tmpdir=$hansa_t "*) ;;
    *) JAVA_TOOL_OPTIONS="${JAVA_TOOL_OPTIONS:+$JAVA_TOOL_OPTIONS }-Djava.io.tmpdir=$hansa_t"; export JAVA_TOOL_OPTIONS ;;
  esac
fi
hansa_p=${HTTPS_PROXY:-${HTTP_PROXY:-}}
hansa_r=${hansa_p#*://}
hansa_r=${hansa_r%%/*}
hansa_u= hansa_w= hansa_h= hansa_o=
case $hansa_r in
  *@*) hansa_c=${hansa_r%@*}; hansa_r=${hansa_r##*@}; hansa_u=${hansa_c%%:*}
       case $hansa_c in *:*) hansa_w=${hansa_c#*:} ;; esac ;;
esac
case $hansa_r in
  *:*) hansa_h=${hansa_r%:*}; hansa_o=${hansa_r##*:} ;;
  *) hansa_h=$hansa_r; case $hansa_p in https://*) hansa_o=443 ;; *) hansa_o=80 ;; esac ;;
esac
case $hansa_o in ''|*[!0-9]*) hansa_h= ;; esac
if [ -n "$hansa_t" ] && [ -n "$hansa_p" ] && [ -n "$hansa_h" ]; then
  hansa_f=${hansa_t%/}/hansa-maven-settings.xml
  hansa_h=$(hansa_x "$hansa_h") hansa_u=$(hansa_x "$hansa_u") hansa_w=$(hansa_x "$hansa_w")
  if (umask 077
      { printf '<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0">\n  <proxies>\n'
        for hansa_q in https http; do
          printf '    <proxy>\n      <id>hansa-%s</id>\n      <active>true</active>\n      <protocol>%s</protocol>\n' "$hansa_q" "$hansa_q"
          printf '      <host>%s</host>\n      <port>%s</port>\n' "$hansa_h" "$hansa_o"
          [ -z "$hansa_u" ] || printf '      <username>%s</username>\n      <password>%s</password>\n' "$hansa_u" "$hansa_w"
          printf '      <nonProxyHosts>localhost|127.0.0.1</nonProxyHosts>\n    </proxy>\n'
        done
        printf '  </proxies>\n</settings>\n'; } >"$hansa_f") 2>/dev/null; then
    case " ${MAVEN_ARGS:-} " in
      *" -gs $hansa_f "*) ;;
      *) MAVEN_ARGS="${MAVEN_ARGS:+$MAVEN_ARGS }-gs $hansa_f"; export MAVEN_ARGS ;;
    esac
  fi
fi
unset hansa_t hansa_p hansa_r hansa_c hansa_u hansa_w hansa_h hansa_o hansa_f hansa_q; unset -f hansa_x
"""
# Where, on Linux, a setup's, check's or merge check's TMPDIR is: a folder of its own on disk, in the desk's
# .hansa/RUN_TMP_DIR (run_tmp), linked from the run's temporary folder as RUN_TMP_LINK. The run's folder, in the
# system's temporary folder, stays srt's (its sockets want a short path) and the command's CLAUDE_CODE_TMPDIR.
# Java must read TMPDIR as writable: FileStore finds a folder's mount by walking up while the device stays the
# same, so a writable bind of a folder whose parent is the same file system is read-only to it (it takes the
# read-only / or /tmp the bind sits in). Every run's sandbox hides .hansa (desk_private) under an empty tmpfs, and
# srt binds a write-list entry inside a hidden folder back over it, from the folder on disk: so this one is a
# mount of its own, writable in the mount table, whose parent is another device (that tmpfs), and no other run's
# sandbox can read it. It used to be a tmpfs srt mounted in the run's folder: the same answer for Java, but in
# memory, up to half the machine's, which a build's temporary files filled.
RUN_TMP_DIR = "tmp"
RUN_TMP_LINK = "hansa-tmp"
# What runs in the sandbox on Linux before JAVA_PRELUDE (sandbox_prelude). First, the command's TMPDIR moves to
# where the run folder's RUN_TMP_LINK leads when that is a folder it may write (srt bound it); otherwise it stays
# the run's folder. Then git there is given an excludes file in $TMPDIR, hansa-git-excludes-*: the
# excludes git would read with no repository and no command's settings (core.excludesFile from the system's and the
# account's config, else $XDG_CONFIG_HOME/git/ignore or ~/.config/git/ignore), then each of SANDBOX_PLACEHOLDERS
# anchored at the top, so git status there does not list the empty files the sandbox mounts (a build script that
# refuses a tree with untracked files would refuse every one). It is only an ignore: a tracked file of such a name
# is still listed, and git outside the sandbox (what hansa commits, the main checkout) reads none of it. Not the
# repository's info/exclude, which every worktree and the main checkout share, and which is git's own folder, kept
# from workers. Not on macOS, whose sandbox mounts no such files.
# How git is told: with git 2.32 or later, GIT_CONFIG_GLOBAL names hansa-gitconfig-* in $TMPDIR, which includes the
# account's own global config ($GIT_CONFIG_GLOBAL if set, else $XDG_CONFIG_HOME/git/config or ~/.config/git/config,
# then ~/.gitconfig, as git reads them; one there that cannot be read is left out, as git leaves it, since an
# include of it would stop git) and then sets core.excludesFile. So everything there still applies, a
# command's own GIT_CONFIG_COUNT does not remove it, and a repository's own core.excludesFile, or one a lane's env
# or a command sets (git -c, GIT_CONFIG_KEY_n), wins as git means it to: the placeholders show there again. What
# `git config --global` writes there goes to that file, not the account's, and a `--global` read follows no
# include. An older git gets core.excludesFile as GIT_CONFIG_COUNT, GIT_CONFIG_KEY_n and GIT_CONFIG_VALUE_n after
# those set already, which a command setting its own replaces. Commands may run side by side in one TMPDIR, with the
# same $$ (srt gives each a PID namespace of its own), and with other HOME, XDG_CONFIG_HOME or GIT_CONFIG_GLOBAL: so
# each file is written under a mktemp name of its own, then moved to a name ending in a checksum of what it holds
# (hansa_mv). No command reads a file half written or made for other settings; one pair per set of settings stays
# until TMPDIR goes. Nothing is added twice (HANSA_GIT_EXCLUDES, in this TMPDIR), what cannot be done is skipped, and
# nothing is printed; POSIX sh, as JAVA_PRELUDE.
LINUX_PRELUDE = r"""hansa_t=${TMPDIR:-}
if [ -n "$hansa_t" ] && [ -L "${hansa_t%/}/""" + RUN_TMP_LINK + r"""" ]; then
  hansa_t=$(cd -P "${hansa_t%/}/""" + RUN_TMP_LINK + r"""" 2>/dev/null && pwd -P) || hansa_t=
  if [ -n "$hansa_t" ] && [ -d "$hansa_t" ] && [ -w "$hansa_t" ]; then TMPDIR=$hansa_t; export TMPDIR; fi
fi
hansa_t=${TMPDIR:-}
case ${HANSA_GIT_EXCLUDES:-} in "${hansa_t%/}"/hansa-git-excludes-*) hansa_t= ;; esac
hansa_mv() {
  hansa_k=$(cksum <"$1" | tr -cs 0-9 -) && hansa_k=${hansa_k%-} && [ -n "$hansa_k" ] &&
    mv -f -- "$1" "$2-$hansa_k" && printf '%s' "$2-$hansa_k" || { rm -f -- "$1"; return 1; }
}
if [ -n "$hansa_t" ] && [ -d "$hansa_t" ]; then
  hansa_g=$( (unset GIT_CONFIG_COUNT GIT_CONFIG_PARAMETERS GIT_DIR GIT_WORK_TREE; cd / &&
              git config --includes --path --get core.excludesFile) 2>/dev/null) || hansa_g=
  [ -n "$hansa_g" ] || hansa_g=${XDG_CONFIG_HOME:-${HOME:-}/.config}/git/ignore
  hansa_e=
  hansa_w=$(mktemp "${hansa_t%/}/hansa-part.XXXXXXXX" 2>/dev/null) &&
    { { cat -- "$hansa_g" 2>/dev/null; printf '\n'; printf '/%s\n' """ + " ".join(SANDBOX_PLACEHOLDERS) + r"""; } \
        >"$hansa_w" 2>/dev/null ||
      { rm -f -- "$hansa_w"; false; }; } &&
    hansa_e=$(hansa_mv "$hansa_w" "${hansa_t%/}/hansa-git-excludes" 2>/dev/null) || hansa_e=
  if [ -n "$hansa_e" ]; then
    hansa_v=$(git version 2>/dev/null) || hansa_v=
    hansa_v=${hansa_v#git version }
    hansa_m=${hansa_v%%.*} hansa_v=${hansa_v#*.}
    hansa_v=${hansa_v%%[!0-9]*}
    case $hansa_m in ''|*[!0-9]*) hansa_m=0 ;; esac
    hansa_c=
    if [ "$hansa_m" -gt 2 ] || { [ "$hansa_m" -eq 2 ] && [ "${hansa_v:-0}" -ge 32 ]; }; then
      hansa_w=$(mktemp "${hansa_t%/}/hansa-part.XXXXXXXX" 2>/dev/null) &&
        { (if [ -n "${GIT_CONFIG_GLOBAL:-}" ]; then hansa_x=$GIT_CONFIG_GLOBAL hansa_y=
           elif [ -n "${HOME:-}" ] || [ -n "${XDG_CONFIG_HOME:-}" ]; then
             hansa_x=${XDG_CONFIG_HOME:-$HOME/.config}/git/config hansa_y=${HOME:+$HOME/.gitconfig}
           else hansa_x= hansa_y=; fi
           for hansa_f in "$hansa_x" "$hansa_y"; do
             [ -z "$hansa_f" ] || { [ -e "$hansa_f" ] && [ ! -r "$hansa_f" ]; } ||
               git config -f "$hansa_w" --add include.path "$hansa_f" || exit 1
           done
           git config -f "$hansa_w" core.excludesFile "$hansa_e") 2>/dev/null || { rm -f -- "$hansa_w"; false; }; } &&
        hansa_c=$(hansa_mv "$hansa_w" "${hansa_t%/}/hansa-gitconfig" 2>/dev/null) || hansa_c=
    fi
    if [ -n "$hansa_c" ]; then
      GIT_CONFIG_GLOBAL=$hansa_c HANSA_GIT_EXCLUDES=$hansa_e; export GIT_CONFIG_GLOBAL HANSA_GIT_EXCLUDES
    else
      hansa_n=${GIT_CONFIG_COUNT:-0}
      case $hansa_n in ''|*[!0-9]*) hansa_n=0 ;; esac
      eval "GIT_CONFIG_KEY_$hansa_n=core.excludesFile GIT_CONFIG_VALUE_$hansa_n=\$hansa_e"
      export "GIT_CONFIG_KEY_$hansa_n" "GIT_CONFIG_VALUE_$hansa_n"
      GIT_CONFIG_COUNT=$((hansa_n + 1)) HANSA_GIT_EXCLUDES=$hansa_e; export GIT_CONFIG_COUNT HANSA_GIT_EXCLUDES
    fi
  fi
fi
unset hansa_t hansa_e hansa_g hansa_n hansa_v hansa_m hansa_c hansa_x hansa_y hansa_f hansa_w hansa_k; unset -f hansa_mv
"""


def sandbox_prelude(linux: bool | None = None) -> str:
    """What runs in the sandbox before a lane's commands and in front of each of a worker's Bash commands:
    LINUX_PRELUDE on Linux (`linux`, by default this machine's), then JAVA_PRELUDE."""
    return (LINUX_PRELUDE if (account.LINUX if linux is None else linux) else "") + JAVA_PRELUDE


def with_prelude(command: str) -> str:
    """A command for /bin/sh -c with sandbox_prelude run first, in the same sandbox: the command itself then runs
    in a shell of its own, as it would alone, with what the prelude exported."""
    return sandbox_prelude() + "exec /bin/sh -c " + shlex.quote(command)


def in_sandbox(desk: Desk, lane: dict, command: str) -> str:
    """A lane's command (setup, check) as srt is given it to run with /bin/sh -c: with the lane's env set
    inside the sandbox, by env there (`exec /usr/bin/env K=V ... /bin/sh -c COMMAND`), then sandbox_prelude
    (with_prelude). srt itself, the script around it and the git it runs get hansa's environment, so nothing a
    lane's env names is looked up, loaded or read outside the sandbox."""
    env = sandbox_env(desk, lane)
    if not env:
        return with_prelude(command)
    return ("exec /usr/bin/env " + " ".join(shlex.quote(f"{k}={v}") for k, v in env.items())
            + " /bin/sh -c " + shlex.quote(with_prelude(command)))


def env_file(desk: Desk, tid: str, lane: dict) -> dict:
    """How a worker's Bash commands get the lane's env and sandbox_prelude: {"CLAUDE_ENV_FILE": a file of
    `export K='V'` lines, then the prelude}. Claude Code reads the file itself (it is in the task's raw folder,
    .hansa, which no worker can write or read) and runs its content in front of each Bash command, in the same
    shell, inside its sandbox, where the sandbox's proxy variables and the command's TMPDIR are set. Claude
    Code starts without the lane's env, so its own lookups (git, the sandbox's helpers, its shell, MCP servers)
    use hansa's environment."""
    env = sandbox_env(desk, lane)
    f = desk.raw_dir(tid) / "lane-env.sh"
    write_text(f, "".join(f"export {k}={shlex.quote(v)}\n" for k, v in env.items()) + sandbox_prelude(), mode=0o600)
    return {"CLAUDE_ENV_FILE": str(f)}


def no_read(desk: Desk) -> tuple:
    """What the principal keeps workers from reading (settings, workers_may_not_read), as a run may be given
    it: (the paths that pass the checks now, a line for each left out). The list is checked on saving, but a
    lane's repository, a worktree or a folder on the PATH may have come under one since: that one is left out.
    One that cannot be applied for any other reason (a pattern character in a name, or where a link now leads)
    stops the run: RuntimeError, saying which entry and why (no_read_stop); so does a desk or home folder whose
    path has a pattern character (desk_paths_problem), as the rules for the desk's folders and the logins would
    close other folders; and so does an account's list of desks that is there but cannot be read
    (desks_list_problem), as the other desks on it could not be hidden from the run."""
    moved = desk_paths_problem(desk, run_homes()) or desks_list_problem()
    if moved:  # the deny rules for the desk's own folders and the logins would close other folders
        raise RuntimeError(f"hansa starts no run (worker, verifier, setup or check): {moved}")
    good, said, stops = no_read_parts(desk.settings(), desk.root)
    if stops:
        raise RuntimeError(stop_text(stops))
    return good, [f"left out of this run: {x.removeprefix(DENY_AT)} (the principal changes the list on the web "
                  "page, Settings)" for x in said]


def desk_private(desk: Desk) -> list:
    """What of the desk no worker or verifier may read: its keys and state, and the
    coordinator's notes (notes/) and card (CLAUDE.md), the project's and the principal's working knowledge,
    which a task does not need."""
    from .up import desks_file
    return [desk.secrets_dir, desk.state_dir, desk.notes_dir, desk.root / "CLAUDE.md", desks_file().parent]


SESSION_FILES = (".claude", "CLAUDE.md", "CLAUDE.local.md")  # a coordinator's settings, memory and copy of the card


def session_private(desk: Desk) -> list:
    """What of the folder hansa notify on or notify-launch recorded for the coordinator's session (up.recorded_folders)
    no worker or verifier, setup or check may read: SESSION_FILES there. Built in, as notes/ is. Left out, stopping
    nothing: all of them when the folder is the desk or in it, or a lane's repository or in one; and each the
    principal's list would refuse (deny_split) or that would make a rule Claude Code splits (path_rule_problem)."""
    from .up import recorded_folders
    s = desk.settings()
    lanes = s.get("lanes") if isinstance(s.get("lanes"), dict) else {}
    repos = [r for _, r in lane_repos(s) if r.startswith("/")]
    out = []
    for f in recorded_folders(desk):
        if any(_above(x, f) for x in [str(desk.root), *repos]):
            continue
        given = [os.path.join(f, n) for n in SESSION_FILES]
        out += [x for x in deny_split(given, desk.root, lanes)[0] if not path_rule_problem(x)]
    return out


def paths_problem(desk: Desk, wt) -> str:
    """path_rule_problem for the folders hansa writes rules for when it starts a worker or verifier: the desk's private ones (desk_private), the worktree, what the principal keeps from workers; '' for
    none. One that would split is never dropped: nothing starts until it moves."""
    for x in full_paths([*desk_private(desk), wt, *no_read(desk)[0]]):
        why = path_rule_problem(x)
        if why:
            return why
    return ""


def desk_read_rules(desk: Desk) -> list:
    """desk_private as rules that deny Claude Code's Read tool: each folder with all under it, the card itself."""
    return [rule("Read", d) for d in desk_private(desk)[:3]] + [f"Read(//{str(desk.root / 'CLAUDE.md').lstrip('/')})"]


def _entries(folder) -> set:
    """The names in `folder`; none where it cannot be read."""
    try:
        return {e.name for e in os.scandir(folder)}
    except OSError:
        return set()


def _all_but(folder: str, keep: list, extra=()) -> list:
    """The paths in `folder` (its entries, and the names `extra` there or not) with all under them, but `keep`: an
    entry that holds one of them is gone into, so only what is beside it is given."""
    out = []
    for n in sorted({*_entries(folder), *extra}):
        x = os.path.join(folder, n)
        if any(_above(k, x) for k in keep):
            continue
        out += _all_but(x, keep) if any(_above(x, k) for k in keep) else [x]
    return out


def desk_fence_rules(desk: Desk, wt: Path, tmp: str) -> list:
    """desk_fence as rules that deny Claude Code's file tools, which srt's denyRead does not hold. A deny rule there
    wins over any allow rule, so none for the whole desk nor for every run's temporary folder: one for each of the
    desk's entries but worktrees/ (those there now and the names hansa makes, cli.DESK_FILES), each lane's worktree
    but the run's own `wt`, and each run's temporary folder there now but its own `tmp`. What holds `wt` or its git
    folders (a lane's repository inside the desk) is gone into, not closed. A desk path whose rule Claude Code would
    split stops the run, as the other rules' do (argv_rules_problem); a temporary folder's is not one hansa made
    (path_rule_problem), and is left out."""
    from .cli import DESK_FILES, desk_spellings
    lanes = desk.settings().get("lanes")
    keep = [str(wt), *map(str, git_folders(wt))]
    trees = str(desk.worktrees_dir)
    inside = [*_all_but(str(desk.root), [*keep, trees], DESK_FILES),
              *_all_but(trees, keep, lanes if isinstance(lanes, dict) else ())]
    root = str(desk.root)
    paths = [d + x[len(root):] for x in inside for d in desk_spellings(desk)]  # a rule matches the path as written
    runs = [os.path.join(r, n) for r in full_paths(dict.fromkeys([WORKER_TMP_ROOT, tempfile.gettempdir()]))
            for n in sorted(_entries(r)) if n.startswith(RUN_TMP_PATTERN[:-1])]
    runs = [x for x in runs if not any(_above(x, k) or _above(k, x) for k in [*keep, *([tmp] if tmp else [])])
            and not path_rule_problem(x)]
    return no_read_rules(list(dict.fromkeys(paths + runs)))


def worker_read_denials(desk: Desk) -> list:
    """The read fence shared by worker sandboxes and file content admission."""
    home = Path(account_home())
    homes = [config_home(desk, n, p) for n, p in desk.settings()["providers"].items()
             if isinstance(p, dict) and isinstance(p.get("config_dir", ""), str)]
    return full_paths([*desk_private(desk), *(h for h in homes if h.is_absolute()), home / ".claude",
                       *(home / f for f in home_secrets()), *no_read(desk)[0], *session_private(desk),
                       *other_desks_hidden(desk)])


def other_desks_hidden(desk: Desk) -> list:
    """The other desks on the account's list (desk.other_desks), each in every spelling, and the list itself:
    hidden from every run, as its own desk is. A desk that holds this one or one of its lanes' repositories (git's
    folders there are the run's) is hidden but for what holds them (holding_desks_parts). Left out: a path with a
    pattern character or that would make a rule Claude Code splits (no run of that desk starts either:
    desk_paths_problem)."""
    repos = [r for _, r in lane_repos(desk.settings()) if r.startswith("/")]
    paths = [x for d in other_desks(desk.root) if not any(_above(d, r) for r in repos) for x in path_spellings(d)]
    return [x for x in full_paths([*paths, *holding_desks_parts(desk.root, repos), desks_file()])
            if not path_rule_problem(x) and not pattern_chars(x)]


RUN_TMP_PATTERN = "hansa-*"  # the name of every run's temporary folder: make_worker_tmp's, run_tmp's, a try's


def run_tmp_patterns() -> list:
    """srt's patterns for every run's temporary folder: RUN_TMP_PATTERN in WORKER_TMP_ROOT (a worker's) and in the
    system's temporary folder (a setup's, check's or try's run folder), each spelling; none in a folder whose path
    srt would read as a pattern itself."""
    roots = full_paths(dict.fromkeys([WORKER_TMP_ROOT, tempfile.gettempdir()]))
    return [os.path.join(r, RUN_TMP_PATTERN) for r in roots if not _glob(r)]


def is_run_tmp(path: str) -> bool:
    """Whether `path` is one of the runs' temporary folders run_tmp_patterns covers."""
    return any(os.path.dirname(path) == os.path.dirname(x) and os.path.basename(path).startswith(RUN_TMP_PATTERN[:-1])
               for x in run_tmp_patterns())


def desk_fence(desk: Desk, wt: Path, mine, closed) -> tuple:
    """What keeps a run to its own of the desk and of the runs' temporary folders: (denyRead: the desk in each of
    its spellings (cli.desk_spellings) and every run's temporary folder (run_tmp_patterns); allowRead: the worktree
    `wt`, its git folders where they are in the desk (a lane's repository inside it), and the run's own temporary
    folders `mine`, on Linux a setup's or check's TMPDIR in .hansa among them (run_tmp)). srt's allowRead wins over
    a denyRead it is in, so the worktree and git's folders are re-allowed only outside what stays closed
    (`closed`, the rest of the run's denyRead); `mine` are made for this run alone."""
    from .cli import desk_spellings
    spellings = full_paths(desk_spellings(desk))
    common, own = git_folders(wt)
    git = [x for x in dict.fromkeys([common, own]) if any(_above(d, str(x)) for d in spellings)]
    allow = []
    for x in full_paths([wt, *git]):  # what is in the desk, under each spelling the desk is denied by
        rest = next((x[len(d):] for d in spellings if x == d or x.startswith(d.rstrip("/") + "/")), None)
        allow += [x] if rest is None else [d + rest for d in spellings]
    allow = [x for x in allow if not any(_above(c, x) for c in closed)]
    return spellings + run_tmp_patterns(), list(dict.fromkeys(allow + full_paths([m for m in mine if m])))


def lane_sandbox(desk: Desk, lane: dict, pname: str, p: dict, wt: Path, mine=()) -> tuple:
    """What a lane's sandbox lets through, for its workers (Claude Code's sandbox) and for its setup and
    check (srt): (the hosts it may reach, {"denyRead": the desk's keys, state, notes and card (desk_private), the recorded
    coordinator's folder's Claude Code files (session_private), every provider's Claude
    Code folder (the login and the session records of whoever runs on it) and ~/.claude, whichever provider
    this run uses, the built-in logins and keys, what the principal keeps from workers, and the rest of the
    desk and the other runs' temporary folders (desk_fence), "allowRead": what of those this run reads (its
    worktree, its own temporary folders `mine`), "denyWrite": git's data, "allowWrite": the lane's write
    list}). Each adds the worktree and a temporary folder to what may be written."""
    moved = next((w for x, what in ((wt, "the worktree"), (lane.get("repo"), "the lane's repository")) if x
                  for w in [pattern_path_problem(x, what, "git's data in it")] if w), "")
    if moved:  # git_write_deny's rules there would close another folder
        raise RuntimeError(f"hansa starts no run (worker, verifier, setup or check): {moved}")
    make_git_write_deny(wt)  # before anything starts in the sandbox
    deny = full_paths([*desk_private(desk), config_home(desk, pname, p), *worker_read_denials(desk)])
    fence, opened = desk_fence(desk, wt, mine, deny)
    deny += [x for x in fence if x not in deny]
    # the write list holds build or test tools' cache folders; denyRead and denyWrite still win within them
    allow = full_paths(write_list(desk, lane)[0])
    return list(lane.get("network", [])), {"denyRead": deny, "allowRead": opened, "denyWrite": git_write_deny(wt),
                                           "allowWrite": allow}


def sandbox_settings(desk: Desk, lane: dict, pname: str, p: dict, wt: Path, tmp: str = "") -> dict:
    """Claude Code's Bash sandbox for a task or verification worker, enforced by the operating system:
    any command, but files changed only in the worktree (and the lane's write list) and never git's
    history, no reading of keys and logins, nor of the desk but the worktree or of other runs' temporary folders
    but its own (`tmp`, make_worker_tmp), no network but the lane's list, and no keys in the environment. It never
    falls back to running unsandboxed."""
    hosts, fs = lane_sandbox(desk, lane, pname, p, wt, [tmp])
    network = {"allowedDomains": hosts, "strictAllowlist": True}
    if any(is_local(h) for h in hosts):
        # macOS: a worker may bind ports and reach the whole loopback (Seatbelt allows outbound to localhost:*,
        # every service there). Linux ignores it: bubblewrap gives each sandbox a loopback of its own.
        network["allowLocalBinding"] = True
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


def srt_settings(desk: Desk, lane: dict, pname: str, p: dict, wt: Path, tmp: str,
                 own: str = "") -> dict:
    """srt's settings for a lane's setup or check: the sandbox of the lane's workers, writing in the
    worktree, the temporary folder `tmp`, the command's own on disk (`own`, run_tmp) and the lane's write list.
"""
    hosts, fs = lane_sandbox(desk, lane, pname, p, wt, [tmp, own])
    network = {"allowedDomains": hosts, "deniedDomains": []}  # srt refuses settings without deniedDomains
    if any(is_local(h) for h in hosts):
        network["allowLocalBinding"] = True
    mine = [wt, tmp, *([own] if own and own != tmp else [])]
    return {"network": network, "filesystem": {"allowWrite": full_paths(mine) + fs["allowWrite"],
                                               "denyRead": fs["denyRead"], "allowRead": fs["allowRead"],
                                               "denyWrite": fs["denyWrite"]}}


def run_tmp(desk: Desk, name: str) -> tuple:
    """The temporary folders of one lane command (`name`: setup, check), each made for it alone (mode 700):
    (the run's folder, in the system's temporary folder, for srt and the command's CLAUDE_CODE_TMPDIR; the folder
    for its TMPDIR). On Linux the second is on disk, in the desk's .hansa/RUN_TMP_DIR, which every run's sandbox
    hides, and the run's folder links to it as RUN_TMP_LINK, which LINUX_PRELUDE follows (see RUN_TMP_DIR); both
    are removed when the command ends. Elsewhere, and where the desk's path holds a space (a TMPDIR many build
    tools break on, and JAVA_PRELUDE leaves alone), both are the run's folder."""
    tmp = tempfile.mkdtemp(prefix=f"hansa-{name}-")
    if not account.LINUX or re.search(r"\s", str(desk.state_dir)):
        return tmp, tmp
    try:
        root = desk.state_dir / RUN_TMP_DIR
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        own = tempfile.mkdtemp(prefix=f"hansa-{name}-", dir=root)
        os.symlink(own, Path(tmp) / RUN_TMP_LINK)
    except OSError:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return tmp, own


def in_srt(desk: Desk, tid: str, name: str, lane: dict, pname: str, p: dict, wt: Path) -> tuple:
    """What a lane's setup or check (`name`) runs in: (srt, its settings file in the task's raw folder,
    a temporary folder made for this run, the command's own (run_tmp: on Linux on disk, the TMPDIR LINUX_PRELUDE
    gives it; elsewhere the same folder)). srt ignores the TMPDIR it is given and sets its own from
    CLAUDE_CODE_TMPDIR (else /tmp/claude), so the caller sets both to the run's folder. Whoever runs the command
    removes both after it; finish and finish_check remove what is left behind (sandbox_tmp); both stay on the
    task as run_folders, which outlives the run, so sweep finds what the command left by them. srt is tried first
    with these settings (try_srt): when it cannot run, SrtCannotRun, and nothing runs."""
    try:
        check_git_file(desk, wt)  # lane_sandbox asks git in the worktree
    except GitFileChanged as e:
        raise RuntimeError(git_file_changed(desk, tid, e)) from None
    tmp, own = run_tmp(desk, name)
    f = desk.raw_dir(tid) / f"{name}-sandbox.json"
    desk.change(tid, lambda m: {"sandbox_tmp": [*m.get("sandbox_tmp", []), *dict.fromkeys([tmp, own])],
                                "run_folders": [*(m.get("run_folders") or []), *dict.fromkeys([tmp, own])]})
    write_json(f, srt_settings(desk, lane, pname, p, wt, tmp, own=own))
    try:
        srt = try_srt(desk, f, tmp, wt)
    except SrtCannotRun:
        for x in (tmp, own):
            shutil.rmtree(x, ignore_errors=True)
        raise
    return srt, str(f), tmp, own


def remove_sandbox_tmp(desk: Desk, tid: str) -> None:
    """The srt settings of a task's setup and check, once they have ended, the lane's env file, and their temporary folders
    still there (one stopped midway leaves its own), and the worker's own temporary folder (worker_tmp)."""
    for name in ("setup", "check"):
        (desk.state_dir / "runs" / tid / f"{name}-sandbox.json").unlink(missing_ok=True)
    (desk.state_dir / "runs" / tid / "lane-env.sh").unlink(missing_ok=True)  # the ended worker's (env_file)
    m = desk.meta(tid)
    for t in [*m.get("sandbox_tmp", []), *([m["worker_tmp"]] if m.get("worker_tmp") else [])]:
        shutil.rmtree(t, ignore_errors=True)
    gone = {t: not os.path.lexists(t) for t in [m.get("worker_tmp")] if t}
    desk.change(tid, lambda x: {"sandbox_tmp": [], "worker_tmp": "", "worker_tmps": [
        {**r, "removed": gone[r["path"]]} if r.get("path") in gone else r for r in x.get("worker_tmps") or []]})


def make_worker_tmp(desk: Desk, tid: str) -> str:
    """A temporary folder for one worker run, its alone (mode 700), recorded as the task's worker_tmp so
    remove_sandbox_tmp removes it however the run ends, and in worker_tmps, which outlives the run: each run's
    path, and whether it was removed (hansa show says). Claude Code gets it as CLAUDE_CODE_TMPDIR, makes its
    per-user folder (the TMPDIR of the worker's commands) and its sockets in it; and as TMPDIR, where its own
    sandbox makes its proxy sockets (srt-mux-*.sock, claude-http-*.sock) and, on Linux, the empty folders it
    mounts over paths it hides (claude-empty-*), which it leaves when it ends. So its path is kept short:
    under /tmp, not the server's TMPDIR (a long path under /var/folders on macOS)."""
    tmp = tempfile.mkdtemp(prefix=f"hansa-{tid}-", dir=WORKER_TMP_ROOT)
    mark = run_mark(desk, tid)  # the run's HANSA_RUN, given with its folder (worker_env)
    desk.change(tid, lambda x: {"worker_tmp": tmp, "worker_tmps": [*(x.get("worker_tmps") or []),
                                                                   {"path": tmp, "removed": None, "mark": mark}]})
    attachments = desk.meta(tid).get("attachments", [])
    if attachments:
        folder = Path(tmp) / "attach"
        folder.mkdir()
        for a in attachments:
            copy_file(desk.attachment_dir(tid) / a["name"], folder / a["name"])
    return tmp


def run_mark(desk: Desk, tid: str) -> str:
    """A marker made for one run, that no other run has (task ids repeat from desk to desk, and one account may
    run several desks): HANSA_RUN=<it> in the environment of what hansa starts for the run, recorded on the task
    (run_marks, which outlives the run), so sweep finds what the run left by it and never another desk's."""
    mark = uuid.uuid4().hex
    desk.change(tid, lambda x: {"run_marks": [*(x.get("run_marks") or []), mark]})
    return mark


def tmp_mark(desk: Desk, tid: str, tmp: str) -> str:
    """The run marker recorded with a worker run's folder (make_worker_tmp)."""
    return next((r.get("mark") or "" for r in desk.meta(tid).get("worker_tmps") or [] if r.get("path") == tmp), "")


def commands_tmp(tmp: str) -> str:
    """The TMPDIR a worker's commands see, given the run's folder (make_worker_tmp) as CLAUDE_CODE_TMPDIR:
    the per-user folder Claude Code makes in it, claude-<uid>, on macOS and Linux alike. Workers are told it
    in full, since Claude Code refuses a command whose paths it cannot place, as one after `cd "$TMPDIR/x"`."""
    return os.path.join(tmp, f"claude-{os.getuid()}")


def lane_allow(desk: Desk, lane: dict, wt: Path) -> list:
    """The worktree's files plus the lane's own commands, for its workers and verifiers alike.
    Git rules a lane still lists from before are left out: hansa does the committing, and read-only git needs
    no rule. So are the other rules saved before they were refused (refused_rule): a bare WebFetch, one that would
    reach Claude Code as several."""
    files = [rule(t, wt) for t in ("Read", "Edit")]  # Edit covers Write
    return files + ["TodoWrite"] + list(dict.fromkeys(r for r in lane.get("allow", []) if not refused_rule(r)))


def words_in_merge(desk: Desk, repo, tip: str, commit: str, message: str = "", lane=None, lane_name: str = "") -> list:
    """Where a task's merge into the base would hold one of the principal's release_forbidden words, matched as a
    release matches them (release.words_in): the commits from commit that the base (at tip) does not hold, their
    messages, identities, file names and added lines; and, for a merge not made yet (message given), its message
    and the lane's identity, which hansa gives it. Also checks every added line when release_mailboxes is on.
    [] when the list is empty and the mailbox rule is off."""
    settings = desk.settings()
    from .release import lane_words, words_in
    words = lane_words(settings, lane_name)
    mailboxes = settings.get("release_mailboxes", False)
    if not words and not mailboxes:
        return []
    texts = []
    if message:
        name, email, _ = lane_identity(lane)
        texts = [("merge message", message), ("merge author and committer", f"{name} <{email}>")]
    return words_in(repo, [commit, f"^{tip}", "--"], words, texts, mailboxes,
                    settings.get("release_mailbox_allow") or [])[0]


def words_text(tid: str, hits: list) -> str:
    """What hansa says when a task's merge holds forbidden words: the words, the places, and what to do."""
    from .release import hits_text, mailbox_refusal
    mailbox = mailbox_refusal(hits, tid)
    if mailbox:
        return f"the change in {tid} is not merged: {mailbox}"
    if any(h.get("kind") == "pattern_limit" for h in hits):
        return (f"the change in {tid} is not merged:\n{hits_text(hits)}\n"
                f"For words found in the history, take them out with hansa retry {tid} --fresh \"...\", "
                "or ask the principal (hansa ask). More: hansa help forbidden-words")
    return (f"the change in {tid} holds words the principal keeps from going out (release_forbidden), so it is not "
            f"merged: once on the base branch, its history would keep them\n{hits_text(hits)}\nTake them out with "
            f"hansa retry {tid} --fresh \"...\" (a retry that continues from {tid} keeps its commits; a word in the "
            f"title or in the lane's merge_message or identity: add the task again with another --title, or change "
            f"the lane), or ask the principal about the words (hansa ask). More: hansa help forbidden-words")


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


OUTPUT_MARGIN = 4096  # bytes dropped around a cut inside a line, longer than any key


def margin_said(margin: bool) -> str:
    """What the line saying how much output was left out adds when a cut fell inside a line (OUTPUT_MARGIN)."""
    return (f", with {OUTPUT_MARGIN} bytes on each side of a cut inside a line, so that no key is left in part"
            if margin else "")


def read_bounded(path: Path, head: int, tail: int) -> str:
    """A file's text, or of a longer one its first `head` and last `tail` bytes, cut at whole lines, with a line
    in between saying how much was left out: never more of it read than that. A cut that falls inside a line drops
    OUTPUT_MARGIN bytes more on its side, so no key is left there in part (desk.redact replaces whole keys only).
    Bytes that are not UTF-8 stay one character each (surrogateescape): written back with errors="replace", each is
    one byte again."""
    with open(path, "rb") as f:
        size = os.fstat(f.fileno()).st_size
        if size <= head + tail:
            return f.read().decode("utf-8", errors="surrogateescape")
        first = f.read(head)
        f.seek(size - tail)
        last = f.read(tail)
    a, b = first.rfind(b"\n"), last.find(b"\n")
    first = first[:a + 1] if a >= 0 else first[:max(0, len(first) - OUTPUT_MARGIN)]
    last = last[b + 1:] if b >= 0 else last[OUTPUT_MARGIN:]
    return (first.decode("utf-8", errors="surrogateescape")
            + f"\n[hansa: {size - len(first) - len(last)} bytes left out here{margin_said(a < 0 or b < 0)}]\n"
            + last.decode("utf-8", errors="surrogateescape"))


# What a failed step's output says about the machine or the build script rather than the change: (the pattern,
# the line hansa adds). Matched line by line, so the words must be on one line.
# mktemp refused where it may not write: macOS's (mkdtemp failed on /var/folders/...) or Linux's (failed to create
# ...), only for Operation not permitted, not for a full disk or other errors.
MKTEMP_REFUSED = re.compile(r"(mkdtemp failed on|mktemp:).*Operation not permitted")
# Maven's own words for a download that failed, on the line that says 407: Maven 3.9's Apache binary prints
# "status code: 407, reason phrase: Proxy Authentication Required (407)", a distribution's "proxy authentication
# failed for ..., status: 407 Proxy Authentication Required". A 407 or the words inside a path do not count.
MAVEN_407 = re.compile(r"(?<![\w/.-])407(?![\w/.-])")
PROXY_AUTH = re.compile(r"(?<![\w/.-])proxy\s+authentication(?![\w/-])", re.I)
MAVEN_WORDS = re.compile(r"Could not transfer artifact|Could not resolve|Failed to read artifact descriptor"
                         r"|Plugin \S+ .*could not be resolved")
NOT_MAVEN = re.compile(r"npm ERR!|npm error|^\s*curl:|Received status code|Could not (GET|HEAD) '")  # npm, curl, Gradle
MKTEMP_HINT = ("mktemp was refused: macOS's mktemp -d, mktemp -t PREFIX and a bare mktemp ignore TMPDIR and write "
               "under /var/folders, which a lane may not write; give it a template in TMPDIR instead: "
               "mktemp -d \"${TMPDIR:-/tmp}/name.XXXXXX\" or mktemp -d -p \"$TMPDIR\". More: hansa help java")
MKTEMP_OUTSIDE_HINT = ("mktemp was refused: the folder it writes in is outside what the lane may write; give it a "
                       "template in TMPDIR: mktemp -d \"${TMPDIR:-/tmp}/name.XXXXXX\". More: hansa help java")
MAVEN_407_HINT = ("Maven got 407 Proxy Authentication Required: hansa gives Maven the sandbox's proxy through "
                  "MAVEN_ARGS (-gs <file>), and a build script that sets MAVEN_ARGS itself drops it (in bash "
                  "MAVEN_ARGS=(-B) makes an array, which is not exported); keep it: MAVEN_ARGS=(-B ${MAVEN_ARGS:-}). "
                  "More: hansa help java")


def maven_407(line: str) -> bool:
    """Whether the line is Maven saying a download got 407 from the proxy."""
    return bool(MAVEN_407.search(line) and PROXY_AUTH.search(line) and MAVEN_WORDS.search(line)
                and not NOT_MAVEN.search(line))


def output_hints(text: str, platform: str = "") -> list:
    """The lines hansa adds to a failed check's, merge check's or try step's report when its output shows a
    known cause that is not the change itself: mktemp refused (macOS's, on macOS or under /var/folders), or Maven
    without the sandbox's proxy. platform: sys.platform's word for the machine the step ran on."""
    lines = text.splitlines()
    mac = (platform or sys.platform) == "darwin"
    out = []
    refused = [x for x in lines if MKTEMP_REFUSED.search(x)]
    if any(mac or "/var/folders/" in x for x in refused):
        out.append(MKTEMP_HINT)
    elif refused:
        out.append(MKTEMP_OUTSIDE_HINT)
    if any(maven_407(x) for x in lines):
        out.append(MAVEN_407_HINT)
    return out


def cut_redacted(data: bytes, most: int) -> bytes:
    """Text with keys already removed, cut again to at most `most` bytes when a key's longer [key of NAME] label
    made it longer: its start and end, at whole lines where there are some, with a line in between saying how much
    was left out. No margin is needed here: no key is left to split, and a character a cut splits is dropped."""
    if len(data) <= most:
        return data
    half = max(0, most // 2 - 128)  # 128: room for the line in between
    first, last = data[:half], data[len(data) - half:]
    a, b = first.rfind(b"\n"), last.find(b"\n")
    first, last = (first[:a + 1] if a >= 0 else first), (last[b + 1:] if b >= 0 else last)
    note = f"\n[hansa: {len(data) - len(first) - len(last)} bytes left out here, after keys were removed]\n"
    return (first.decode("utf-8", errors="ignore") + note + last.decode("utf-8", errors="ignore")).encode("utf-8")


def publish(desk: Desk, tid: str, *names: str) -> None:
    """Move what a task's processes wrote from .hansa/runs/<id>/ into its folder, keys removed."""
    raw, d = desk.state_dir / "runs" / tid, desk.tasks_dir / tid
    with desk.lock():  # a key saved meanwhile is either known here or scrubbed from it after (Desk.scrub)
        for name in names:
            src = raw / name if (raw / name).exists() else d / name  # in d: started before hansa kept them apart
            if src.exists():
                text = src.read_text(encoding="utf-8", errors="replace")
                data = desk.redact(text).encode("utf-8", errors="replace")
                write_bytes(d / name, data)
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


def _mac_parse_args(data):
    """What KERN_PROCARGS2 gave, as (arguments, environment, whole); None when it holds nothing. The
    executable's path comes first, then NULs up to the next multiple of 8 bytes, then the arguments. Read
    in the middle of an exec it can be half written: all NULs, or a path with a NUL in it, so that the end
    of the path is taken for the first argument and its padding for empty ones. Then `whole` is False."""
    if not data or len(data) < 4:
        return None
    argc = struct.unpack_from("@i", data, 0)[0]
    rest = data[4:]
    end = rest.find(b"\0")  # the executable's path, then NULs up to the first argument
    if end < 0:
        return None
    after = rest[end:].lstrip(b"\0")
    words = after.split(b"\0")
    env = words[argc:]  # the environment ends at an empty string; the kernel's own strings follow it
    return words[:argc], env[:env.index(b"")] if b"" in env else env, \
        end > 0 and len(rest) - len(after) == (end // 8 + 1) * 8


def _mac_procargs(pid: int):
    """A process's arguments and environment, as two lists, and whether they were read whole (_mac_parse_args);
    None when they cannot be read (a process of another account, one in the middle of an exec, one that
    ended meanwhile)."""
    return _mac_parse_args(_sysctl(_CTL_KERN, _KERN_PROCARGS2, pid))


# What listens on the machine's loopback, read as lsof does, through libproc: a process's descriptors
# (PROC_PIDLISTFDS, 8 bytes each: the descriptor, then its type), and for a socket its struct
# socket_fdinfo (PROC_PIDFDSOCKETINFO, 792 bytes): at 256 soi_kind, at 264 its in_sockinfo (the local
# port at 4, network order; the version flag at 24; the local address at 48, an IPv4 one in its last 4
# bytes), and at 344 the TCP state. The kinfo_proc's p_comm is at 243. The sandbox refuses the kernel's
# socket lists (net.inet.tcp.pcblist_n, what netstat reads), and libproc reads other accounts'
# processes only as root.
_PROC_PIDLISTFDS, _PROC_PIDFDSOCKETINFO, _PROX_FDTYPE_SOCKET = 1, 3, 2
_SOCKET_FDINFO_SIZE, _SOCKINFO_TCP, _TCPS_LISTEN = 792, 2, 1


def _loopback_address(vflag: int, raw: bytes) -> str:
    """The local address of a socket, when a connection to the loopback reaches it: '127.0.0.1', '::1'
    or '*' (every address); '' when it listens elsewhere only."""
    import ipaddress
    try:
        ip = ipaddress.ip_address(raw[12:16] if vflag & 1 else raw)
    except ValueError:
        return ""
    if ip.is_unspecified:
        return "*"
    return (f"[{ip}]" if ip.version == 6 else str(ip)) if ip.is_loopback else ""


def mac_loopback_listeners():
    """The TCP servers on this Mac that a connection to its loopback reaches, as (listeners, unread):
    listeners a sorted list of (program, pid, account, address, port), address '*' for one listening on
    every address; unread how many processes of other accounts could not be looked at (all of them
    unless hansa runs as root). None when they cannot be read at all."""
    try:
        import ctypes
        import pwd
        lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    except (ImportError, OSError):
        return None
    data = _sysctl(_CTL_KERN, _KERN_PROC, _KERN_PROC_ALL, 0, slack=_KINFO_PROC_SIZE * 16)
    if not data:
        return None
    found, unread, me = set(), 0, os.getuid()
    info = ctypes.create_string_buffer(_SOCKET_FDINFO_SIZE)
    for i in range(0, len(data) - _KINFO_PROC_SIZE + 1, _KINFO_PROC_SIZE):
        k = data[i:i + _KINFO_PROC_SIZE]
        pid, uid = struct.unpack_from("@i", k, 40)[0], struct.unpack_from("@I", k, 392)[0]
        if pid <= 0 or k[36] == _SZOMB:
            continue
        n = lib.proc_pidinfo(pid, _PROC_PIDLISTFDS, 0, None, 0)
        buf = ctypes.create_string_buffer(max(n, 0) + 8 * 32)  # room for descriptors opened meanwhile
        n = lib.proc_pidinfo(pid, _PROC_PIDLISTFDS, 0, buf, len(buf)) if n > 0 else n
        if n <= 0:
            unread += uid != me
            continue
        for j in range(0, n - 7, 8):
            fd, kind = struct.unpack_from("@iI", buf.raw, j)
            if kind != _PROX_FDTYPE_SOCKET or \
                    lib.proc_pidfdinfo(pid, fd, _PROC_PIDFDSOCKETINFO, info, _SOCKET_FDINFO_SIZE) != _SOCKET_FDINFO_SIZE:
                continue
            raw = info.raw
            if struct.unpack_from("@i", raw, 256)[0] != _SOCKINFO_TCP or \
                    struct.unpack_from("@i", raw, 344)[0] != _TCPS_LISTEN:
                continue
            addr = _loopback_address(raw[288], raw[312:328])
            if not addr:
                continue
            try:
                account = pwd.getpwuid(uid).pw_name
            except KeyError:
                account = str(uid)
            port = int.from_bytes(raw[268:270], "big")  # the low 2 bytes of an int, in network order
            found.add((k[243:260].split(b"\0")[0].decode(errors="replace"), pid, account, addr, port))
    return sorted(found, key=lambda x: (x[4], x[0], x[1], x[3])), unread


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
    the one looked for: CannotLook. One read half written (_mac_parse_args) that leads its group is
    read again too, and taken as it reads once two reads in a row agree (an exec is not half way through
    twice alike: a process whose first argument is empty reads so every time); one never read whole is
    not the one looked for. Not read at all: one that started
    before `since` (the launch word was made then), and one whose effective user is not its real user
    (a setuid program such as sudo, which hansa never starts, and whose arguments cannot be read). (One
    that does not lead its group is not the one looked for either: some of the account's processes never
    let their arguments be read.)"""
    for i in range(LOOK_TRIES):
        data = _sysctl(_CTL_KERN, _KERN_PROC, _KERN_PROC_ALL, 0, slack=_KINFO_PROC_SIZE * 16)
        if data:
            break
        time.sleep(0.05 * (i + 1))
    else:
        raise CannotLook("the process list could not be read")
    found, unsure = [], {}  # unsure: pid -> its last read, None when unread
    for i in range(0, len(data) - _KINFO_PROC_SIZE + 1, _KINFO_PROC_SIZE):
        k = data[i:i + _KINFO_PROC_SIZE]
        pid = struct.unpack_from("@i", k, 40)[0]
        if pid <= 0 or k[36] == _SZOMB or struct.unpack_from("@I", k, 392)[0] != os.getuid() or \
                struct.unpack_from("@I", k, 420)[0] != os.getuid():
            continue
        if since and struct.unpack_from("@l", k, 0)[0] < since - 2:  # 2 s for the rounding of both clocks
            continue
        got = _mac_procargs(pid)
        if got is not None and token.encode() in got[0]:
            found.append(pid)
        elif (got is None or not got[2]) and _may_lead(pid):  # as each process hansa starts does
            unsure[pid] = got
    for i in range(LOOK_TRIES):  # in the middle of an exec, say: a moment later it is read whole, or is gone
        if not unsure:
            break
        time.sleep(0.05 * (i + 1))
        again = {}
        for pid, before in unsure.items():
            got = _mac_procargs(pid)
            if got is not None and token.encode() in got[0]:
                found.append(pid)
            elif (got is None and _mac_state(pid) not in ("", "Z") or got is not None and not got[2] and got != before) \
                    and _may_lead(pid):
                again[pid] = got
        unsure = again
    unread = [pid for pid, got in unsure.items() if got is None]  # one still half written is not the one looked for
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


def _names_folder(folders: list):
    """A test of one argument, or a TMPDIR's value: whether it names one of these folders, or a path inside one,
    as a whole path part (/tmp/hansa-t1-abc names neither /tmp/hansa-t1-abc2 nor /x/tmp/hansa-t1-abc). A folder
    counts under its own path and its real one (on macOS /tmp is /private/tmp)."""
    paths = sorted({os.path.normpath(x) for f in folders if f for x in (f, os.path.realpath(f))}, key=len, reverse=True)
    if not paths:
        return lambda arg: False, lambda value: False
    word = re.compile((r"(?<![\w.~/-])(?:" + "|".join(map(re.escape, paths)) + r")(?![^/\s'\";:,=)])").encode())

    def in_tmpdir(value: bytes) -> bool:
        v = os.path.normpath(value.decode(errors="replace")) if value else ""
        return any(v == x or v.startswith(x + "/") for x in paths)
    return lambda arg: bool(word.search(arg)), in_tmpdir


def task_processes(marks: list, folders: list = ()) -> list:
    """This account's processes whose environment gives HANSA_RUN as one of `marks` (the task's runs' own,
    run_marks), and no other, or that name one of `folders` (the task's own run folders, worker_tmps) in an
    argument or as their TMPDIR, whatever their environment says: [(pid, arguments)]. HANSA_TASK alone finds
    nothing: task ids repeat from desk to desk. One that cleared its environment, and names no such folder, is
    not found."""
    want, found = {f"HANSA_RUN={x}".encode() for x in marks if x}, []
    in_args, in_tmpdir = _names_folder(list(folders))

    def ours(args: list, env: list) -> bool:
        named = [x for x in env if x.startswith(b"HANSA_RUN=")]
        return (bool(named) and all(x in want for x in named)) or any(map(in_args, args)) or \
            any(in_tmpdir(x[7:]) for x in env if x.startswith(b"TMPDIR="))
    if Path("/proc").is_dir():
        for p in Path("/proc").iterdir():
            try:
                if p.name.isdigit() and p.stat().st_uid == os.getuid():
                    args = (p / "cmdline").read_bytes().rstrip(b"\0").split(b"\0")
                    try:
                        env = (p / "environ").read_bytes().split(b"\0")
                    except OSError:  # its arguments may still name a folder
                        env = []
                    if ours(args, env):
                        found.append((int(p.name), args))
            except OSError:
                pass
    else:
        for k in _mac_procs(_KERN_PROC_ALL, 0):
            pid = struct.unpack_from("@i", k, 40)[0]
            if pid > 0 and k[36] != _SZOMB and struct.unpack_from("@I", k, 392)[0] == os.getuid():
                args, env, _ = _mac_procargs(pid) or ([], [], True)
                if ours(args, env):
                    found.append((pid, args))
    return [(p, [a.decode(errors="replace") for a in args]) for p, args in found]


# A process's parent and controlling terminal in its kinfo_proc (e_ppid, e_tdev), on macOS; its working folder
# through libproc (PROC_PIDVNODEPATHINFO: a struct proc_vnodepathinfo of 2352 bytes, the folder's path at 152).
_KP_PPID, _KP_TDEV, _PROC_PIDVNODEPATHINFO, _VNODEPATHINFO_SIZE = 560, 572, 9, 2352


def process_table() -> dict:
    """This account's running processes: {pid: (parent pid, start as proc_start gives it, start in seconds since
    the epoch)}. {} when the list cannot be read."""
    out = {}
    if Path("/proc").is_dir():
        try:
            boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            btime = next(int(x.split()[1]) for x in Path("/proc/stat").read_text().splitlines() if x.startswith("btime "))
            hz = os.sysconf("SC_CLK_TCK")
            entries = list(Path("/proc").iterdir())
        except (OSError, StopIteration, ValueError):
            return {}
        for p in entries:
            try:
                if p.name.isdigit() and p.stat().st_uid == os.getuid():
                    f = (p / "stat").read_text().rsplit(")", 1)[1].split()
                    if f[0] != "Z":
                        out[int(p.name)] = (int(f[1]), f"{boot}:{f[19]}", btime + int(f[19]) / hz)
            except (OSError, IndexError, ValueError):
                pass
        return out
    for k in _mac_procs(_KERN_PROC_ALL, 0):
        pid = struct.unpack_from("@i", k, 40)[0]
        if pid > 0 and k[36] != _SZOMB and struct.unpack_from("@I", k, 392)[0] == os.getuid():
            sec, usec = struct.unpack_from("@li", k, 0)
            out[pid] = (struct.unpack_from("@i", k, _KP_PPID)[0], f"{sec}.{usec:06d}", sec + usec / 1e6)
    return out


def record_descendants(top: int, seen: dict, table: dict | None = None) -> bool:
    """Add to `seen` ({pid: start}) the processes now running below `top` (a process hansa started), or below one
    seen before: whatever their process group, session or environment, so long as their parent is still known
    at a look. `table`: process_table() read already. Whether anything was added."""
    table, added = process_table() if table is None else table, False
    if top in table and seen.get(top) != table[top][1]:
        seen[top], added = table[top][1], True
    grew = True
    while grew:
        grew = False
        for pid, (ppid, start, _) in table.items():
            if seen.get(pid) != start and ppid in seen and table.get(ppid, (0, ""))[1] == seen[ppid]:
                seen[pid], grew, added = start, True, True
    return added


def working_in(folders: list, since: float = 0, table: dict | None = None) -> dict:
    """This account's processes without a controlling terminal whose working folder is one of `folders` or
    inside one, started at `since` or later: {pid: start}. Never this process or its parent. A terminal's shell
    or an editor the principal opened there is left alone, even with its input and output redirected.
    The caller also excludes hansa's own processes and what it started itself, from the same `table`
    (process_table() read already) when given."""
    paths = {os.path.normpath(x) for f in folders if f for x in (f, os.path.realpath(f))}
    if not paths:
        return {}
    lib = buf = None
    if not Path("/proc").is_dir():
        try:
            import ctypes
            lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            buf = ctypes.create_string_buffer(_VNODEPATHINFO_SIZE)
        except (ImportError, OSError):
            return {}
    out = {}
    for pid, (_, start, when) in (process_table() if table is None else table).items():
        if pid in (os.getpid(), os.getppid()) or when < since - 2:  # 2 s for both clocks' rounding
            continue
        try:
            if lib is None:
                fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
                if int(fields[4]) != 0:  # tty_nr: the controlling terminal, regardless of open file descriptors
                    continue
                cwd = os.readlink(f"/proc/{pid}/cwd")
            else:
                k = _mac_proc(pid)
                if k is None or struct.unpack_from("@i", k, _KP_TDEV)[0] != -1:  # NODEV
                    continue
                if lib.proc_pidinfo(pid, _PROC_PIDVNODEPATHINFO, 0, buf, _VNODEPATHINFO_SIZE) != _VNODEPATHINFO_SIZE:
                    continue
                cwd = buf.raw[152:152 + 1024].split(b"\0")[0].decode(errors="replace")
        except (OSError, IndexError, ValueError):
            continue
        cwd = os.path.normpath(cwd) if cwd else ""
        if cwd and any(cwd == x or cwd.startswith(x + "/") for x in paths):
            out[pid] = start
    return out


def stop_recorded(found: dict, grace: float = 5) -> list:
    """Stop these processes ({pid: start}) that still run and are still the same process (their start matches),
    politely and then for good, one by one, whatever their group. The ones that were running."""
    keep = {os.getpid(), os.getppid()}

    def running(p, start):
        return p not in keep and alive(p, start) and _state(p) != "Z"
    hit = [(int(p), s) for p, s in found.items() if s and running(int(p), s)]
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 2)):
        for p, s in hit:
            if running(p, s):
                try:
                    os.kill(p, sig)
                except OSError:
                    pass
        end = time.time() + wait
        while any(running(p, s) for p, s in hit) and time.time() < end:
            time.sleep(0.1)
        if not any(running(p, s) for p, s in hit):
            break
    return [p for p, _ in hit]


SWEEP_GRACE = 5  # seconds between the polite signal and the one that cannot be refused


def group_members(pgid) -> dict:
    """What still runs in this process group: {pid: start}."""
    out = {}
    if Path("/proc").is_dir():
        for p in Path("/proc").iterdir():
            try:
                if p.name.isdigit():
                    f = (p / "stat").read_text().rsplit(")", 1)[1].split()
                    if f[0] != "Z" and int(f[2]) == int(pgid):
                        out[int(p.name)] = proc_start(int(p.name))
            except (OSError, IndexError, ValueError):
                pass
        return out
    for k in _mac_procs(_KERN_PROC_PGRP, int(pgid)):
        if k[36] != _SZOMB:
            sec, usec = struct.unpack_from("@li", k, 0)
            out[struct.unpack_from("@i", k, 40)[0]] = f"{sec}.{usec:06d}"
    return out


def stop_group(pgid) -> None:
    """Stop what still runs in this process group, politely and then for good. A group with a process in it
    keeps its number, so one that still runs is the one recorded."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except OSError:
            break
        end = time.time() + SWEEP_GRACE
        while group_running(pgid) and time.time() < end:
            time.sleep(0.1)
        if not group_running(pgid):
            break


def run_recorded(m: dict) -> dict:
    """What a task's runs started, as hansa serve recorded it while they ran (record_run): {pid: start}."""
    return {int(p): st for p, st in (m.get("started_procs") or {}).items() if str(p).isdigit()}


def record_run(desk: Desk, m: dict, table: dict) -> None:
    """Record on a running task what its run (worker, verifier, or its check) now runs below
    it, also what left its process group, cleared its environment or works in another folder (as a command's
    is: record_descendants), and the group its own process leads, with that process's start: on the task, so
    that a restarted hansa serve still stops them at the run's end (sweep). Only while the run's process is
    still the one started (pid_start): once its number may be another's, nothing more is added. One that ended
    is let go, so the record stays as long as what runs. `table`: process_table() read for this round."""
    pid, start, was = m.get("pid"), m.get("pid_start") or "", run_recorded(m)
    if not pid or not table:  # a list that could not be read lets nothing go
        return
    seen = {p: st for p, st in was.items() if table.get(p, (0, ""))[1] == st}
    ours = bool(start) and not any(c.isspace() for c in start) and table.get(pid, (0, ""))[1] == start
    group = {}
    if ours:
        record_descendants(pid, seen, table)
        try:
            if os.getpgid(pid) == pid:
                group = {str(pid): start}
        except OSError:
            pass
    groups = m.get("run_groups") or {}
    if seen == was and (not group or isinstance(groups, dict) and group.items() <= groups.items()):
        return
    desk.change(m["id"], lambda x: None if x["state"] != "running" or x.get("pid") != pid
                or (x.get("pid_start") or "") != start else
                {"started_procs": {str(p): st for p, st in seen.items()},
                 "run_groups": {**(x.get("run_groups") if isinstance(x.get("run_groups"), dict) else {}), **group}})


def _args(pid) -> list:
    """A process's arguments; [] when they cannot be read."""
    try:
        return [a.decode(errors="replace") for a in Path(f"/proc/{pid}/cmdline").read_bytes().rstrip(b"\0").split(b"\0")]
    except OSError:
        pass
    got = _mac_procargs(pid) if _ON_MAC else None
    return [a.decode(errors="replace") for a in got[0]] if got else []


def proc_args(pid):
    """A process's exact argument list, empty words kept: what started it, to start it again (up.py). None when it
    cannot be read whole, or holds a word that is not UTF-8."""
    try:
        data = Path(f"/proc/{pid}/cmdline").read_bytes()
        words = (data[:-1] if data.endswith(b"\0") else data).split(b"\0") if data else []
    except OSError:
        got = _mac_procargs(pid) if _ON_MAC else None
        words = got[0] if got and got[2] else []
    try:
        return [w.decode("utf-8") for w in words] or None
    except UnicodeDecodeError:
        return None


def proc_cwd(pid) -> str:
    """A process's working folder; '' when it cannot be read. On macOS through libproc (as working_in), else lsof."""
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        if not _ON_MAC:
            return ""
    try:
        import ctypes
        lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        buf = ctypes.create_string_buffer(_VNODEPATHINFO_SIZE)
        if lib.proc_pidinfo(int(pid), _PROC_PIDVNODEPATHINFO, 0, buf, _VNODEPATHINFO_SIZE) == _VNODEPATHINFO_SIZE:
            return buf.raw[152:152 + 1024].split(b"\0")[0].decode()
    except (ImportError, OSError, ValueError, UnicodeDecodeError):
        pass
    try:
        r = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], capture_output=True, text=True,
                           timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return next((x[1:] for x in r.stdout.splitlines() if x.startswith("n")), "")


def sweep(desk: Desk, tid: str, *, run: dict | None = None) -> list:
    """Stop what a task's processes left running once nothing of the task should run any more: a server or
    watcher that left the process group hansa stops, in a session of its own. Never hansa serve itself, nor
    the command doing this. First what hansa serve recorded below the task's runs while they ran (record_run):
    each run's process group, then each recorded process that still runs and is still the same process (its
    start); then what it finds now by the marker of one of the task's runs (HANSA_RUN, run_marks) in their
    environment, or by one of the task's run folders (worker_tmps, run_folders) in their arguments or TMPDIR: a process
    started without the marker still names its run's folder, and one started between two recordings is found so.
    Finally processes working in the ended run's worktree, started since the run, without a controlling terminal:
    also finds an unmarked process never observed below the run. Hansa's own processes and their descendants
    are excluded from this last look. `run` supplies that worktree and started time for a check; by default,
    the task's worker or verifier run. Never fall back to an earlier run's folder or time. The record is cleared.
    What was stopped goes on the task (left_running) and in the event log."""
    servers = {os.getpid()}
    try:
        servers.add(int((desk.state_dir / "serve.lock").read_text().split()[0]))
    except (OSError, ValueError, IndexError):
        pass
    keep = servers | {os.getppid()}

    def running(p, start):
        return alive(p, start) and _state(p) != "Z"
    m = desk.meta(tid)
    run = m if run is None else run
    seen = {p: st for p, st in run_recorded(m).items() if p not in keep}
    groups = m.get("run_groups") or {}
    if not isinstance(groups, dict):  # recorded before a group kept its leader's start
        groups = {g: seen.get(int(g), "") for g in groups}
    in_groups = []  # what was stopped with its group
    for g, st in groups.items():
        g = int(g)
        if g in keep or not group_running(g):
            continue
        members = group_members(g)
        if alive(g):  # its leader still runs: the one recorded, not a later process of its number
            ours = bool(st) and not any(c.isspace() for c in st) and proc_start(g) == st
        else:  # its leader ended, so the number is still the group's: one recorded still in it
            ours = any(s2 and seen.get(p) == s2 for p, s2 in members.items())
        if ours and not keep & set(members):
            members = [(p, _args(p), s2) for p, s2 in members.items()]
            stop_group(g)
            in_groups += [x for x in members if not running(x[0], x[2])]
    done = {x[0] for x in in_groups}
    found = [(p, _args(p), st) for p, st in seen.items() if st and p not in done and running(p, st)]
    folders = [*(r["path"] for r in m.get("worker_tmps") or [] if r.get("path")), *(m.get("run_folders") or [])]
    found += [(p, args, proc_start(p)) for p, args in task_processes(m.get("run_marks") or [], folders)
              if p not in keep and p not in done | {x[0] for x in found}]
    if run.get("worktree") and run.get("started"):
        ours, table = {}, process_table()
        for pid in servers:
            record_descendants(pid, ours, table)
        excluded = keep | set(ours) | done | {x[0] for x in found}
        found += [(p, _args(p), st) for p, st in working_in([run["worktree"]], since=run["started"], table=table).items()
                  if p not in excluded]
    found = [(p, args, s) for p, args, s in found if running(p, s)]
    if m.get("started_procs") or m.get("run_groups"):
        desk.change(tid, lambda x: {"started_procs": {}, "run_groups": {}})
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
    stopped = [{"pid": p, "command": desk.redact(" ".join(args))[:120]}
               for p, args, s in in_groups + [x for x in found if not running(x[0], x[2])]]
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


CONFLICT_FILE_BYTES = 64 * 1024 * 1024


def conflict_places(cwd: Path, files: list, most_files: int = 10, most_lines: int = 16) -> str:
    """Where the two sides of a failed merge meet, read from its files before the merge is aborted: for each
    file, the lines of each conflict with their numbers."""
    out = []
    for f in files[:most_files]:
        p = Path(cwd) / f
        blocks, top, shown = [], None, []
        try:  # a link is not followed: what it points to is not the change's
            info = os.lstat(p)
            if stat.S_ISREG(info.st_mode):
                if info.st_size > CONFLICT_FILE_BYTES:
                    out.append(f"  {f}: too big to look into (over 64 MB)")
                    continue
                with p.open("r", encoding="utf-8", errors="replace") as text:
                    for n, x in enumerate(text, 1):
                        x = x.rstrip("\r\n")
                        if x.startswith("<<<<<<<") and top is None:
                            top, shown = n, []
                        if top is not None and len(shown) < most_lines:
                            shown.append((n, x[:160]))
                        if x.startswith(">>>>>>>") and top is not None:
                            blocks.append((top, n, shown))
                            top = None
        except FileNotFoundError:
            pass  # one side deleted or renamed it
        except OSError:
            out.append(f"  {f}: could not look into it")
            continue
        if not blocks:
            out.append(f"  {f}: no conflict markers in it (one side deleted or renamed it, or it is not text)")
        for a, b, shown in blocks:
            out.append(f"  {f}, lines {a}-{b}:")
            if b - a + 1 > most_lines:
                shown = shown[:most_lines - 1] + [(None, f"... {b - a - most_lines + 2} more lines")]
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
            f"or, to redo the whole change: hansa retry {tid} --fresh \"<the same change on the current {base}>\". "
            "More: hansa help merge-check")


def update_branch(desk: Desk, wt: Path, tid: str, name: str, tip: str, message: str = "", ident=None) -> list:
    """For `hansa retry --update`: merge the base's current tip into the task's fresh branch. Merged at once
    when it merges cleanly; else aborted here and merged again just before the worker starts, after the
    lane's setup (UPDATE_SCRIPT). Returns the files that conflict."""
    try:
        check_head(desk, wt, f"task/{tid}")
        check_branch_ref(desk, wt, f"task/{tid}")
        check_no_operation(desk, wt)
        wgit(desk, wt, "merge", "--no-ff", "--no-verify", "-m", message or f"Merge {name} into task/{tid}", tip,
             env=ident)
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


def prepare(desk: Desk, lane_name: str, lane: dict, meta: dict):
    """Put the lane's worktree on a fresh task branch. Returns (worktree, base sha).
    A follow-up made with --update gets the base's current tip merged into it (update_branch)."""
    repo = Path(lane["repo"])
    tid = meta["id"]
    desk.update(tid, repo=resolved(str(repo)) or str(repo))  # where its branch is, whatever the lane is later
    restart = meta.get("restart") if meta.get("kind") != "verify" else None
    carrying = bool(restart and restart.get("source") == "earlier run's commit")
    updating = bool(meta.get("update") and meta.get("start_from") and not carrying)
    base = git("rev-parse", lane.get("base") or "main", cwd=repo) if updating or not meta.get("base") else meta["base"]
    start = restart["commit"] if carrying else meta.get("start_from") or base
    wt = desk.worktrees_dir / lane_name
    ready_worktree(desk, repo, wt, tid, start)
    branch = f"task/{tid}"
    if wgit(desk, wt, "branch", "--list", branch):
        # an earlier try of this task: keep its commits under another name
        n = len(meta.get("attempts", []))
        name, k = f"{branch}-try{n}", 2
        while wgit(desk, wt, "branch", "--list", name):  # a try that did not count (hold) kept one under it
            name, k = f"{branch}-try{n}-{k}", k + 1
        wgit(desk, wt, "branch", "-m", branch, name)
    wgit(desk, wt, "checkout", "-q", "-B", branch, start)
    if updating:
        desk.update(tid, update_conflicts=update_branch(
            desk, wt, tid, lane.get("base") or "main", base,
            update_message(lane, lane_name, tid, meta.get("title") or "", lane.get("base") or "main"),
            identity_env(lane)))
    desk.update(tid, run_start=restart if carrying else {
        "commit": wgit(desk, wt, "rev-parse", "HEAD"),
        "source": "earlier task's commit" if meta.get("start_from") else "base",
        "why": (restart or {}).get("why", "")})
    return wt, base


def with_setup(desk: Desk, tid: str, argv: list, lane: dict, pname: str, p: dict, wt: Path) -> list:
    """The worker command, preceded by the lane's setup command when it has one (see SETUP_SCRIPT)."""
    if not lane.get("setup"):
        return argv
    raw = desk.raw_dir(tid)
    (raw / "setup.log.done").unlink(missing_ok=True)  # an earlier try's mark says nothing about this one
    srt, settings, tmp, own = in_srt(desk, tid, "setup", lane, pname, p, wt)
    line = expected_git_line(desk, wt) or "(unknown, so no git runs here)"
    return ["/bin/sh", "-c", SETUP_SCRIPT, "hansa-setup", in_sandbox(desk, lane, lane["setup"]), str(raw / "setup.log"), str(raw / "run.err"),
            line, tmp, srt, settings, own, *argv]


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
    desk.failed_starts.pop(("task", tid, desk.meta(tid).get("launch")), None)


def around_note(desk: Desk, lane: dict, meta: dict, lane_name: str = "", tmp: str = "") -> str:
    """What hansa runs around a task or verification worker, for its instructions: the lane's setup and
    check as hansa runs them, and its merge check when that runs another command (the lane's env is set for the
    worker's commands too), keys hidden, each whole. One longer than COMMAND_SHOWN is written whole to a file in
    the run's folder `tmp` (make_worker_tmp), which the worker may read, and named. A verifier
    is told how the task's own run of the check ended only on the task's own lane, named by lane_name."""
    def shown(command: str, name: str) -> str:
        text = desk.redact(command)
        if len(text) <= COMMAND_SHOWN or not tmp:
            return f"`{text}`"
        f = Path(tmp) / f"lane-{name}-command.txt"
        write_text(f, text + "\n")
        return (f"`{text[:200]} ...` (the command is {len(text)} characters long; it is whole in "
                f"{f}, which you may read)")
    out = []
    if lane.get("setup"):
        out.append(f"- Before you started, hansa ran the lane's setup in this worktree: {shown(lane['setup'], 'setup')}")
    if not lane.get("check"):
        out.append("- This lane has no check command: hansa runs nothing here after you end.")
    elif meta.get("kind") == "verify":
        parent = read_json(desk.tasks_dir / meta.get("parent", "-") / "meta.json") or {}
        ran = parent.get("check") or {}
        if parent.get("lane") != lane_name or ran.get("command") != lane["check"]:
            ran = {}  # another lane's run (its env, its sandbox), or an older check: not this command's result
        went = (" hansa could not run it for the task itself." if ran.get("could_not_run") else
                f" The task's own run of it {'passed' if ran['ok'] else 'failed'}."
                if ran.get("ok") in (True, False) else "")
        out.append("- hansa judges a task here by the lane's check, run in its worktree in this sandbox: "
                   f"{shown(lane['check'], 'check')}{went} You may run the same command yourself.")
    else:
        out.append("- After you end, hansa runs the lane's check here, in the same sandbox, and judges the "
                   f"task by it: {shown(lane['check'], 'check')}")
        out.append("- So run yourself only the tests related to your change, not the whole check.")
    if own_merge_check(lane):
        out.append("- Before it merges a task, not after the task ends, hansa runs the lane's merge check on the task "
                   "merged with the base branch, in this worktree and the same sandbox; a merge check that fails "
                   f"merges nothing: {shown(lane['merge_check'], 'merge-check')}")
    return "".join(x + "\n" for x in out)


@git_folders_kept
def start(desk: Desk, tid: str, lane_name: str, pname: str):
    """Claim a queued task and start its worker. None when the task was no longer queued, or was
    cancelled while hansa prepared it."""
    with desk.lock():  # lane roles may have changed since dispatch chose it
        queued = desk.meta(tid)
        if work_lane_problem(desk.settings(), lane_name,
                             verifying=queued.get("kind") == "verify" and bool(queued.get("lane"))):
            return None
        meta = desk.transition(tid, ("queued",), state="running", phase="starting", lane=lane_name, provider=pname,
                               started=now(), finished=None, pid=None, pid_start="", error="", timed_out=False,
                               cancel=False, held=None, sandbox_failed=None)
    if not meta:
        return None
    no_read(desk)  # before anything is prepared: an entry that cannot be applied fails the run (not_started)
    s = desk.settings()
    lane, p = s["lanes"][lane_name], s["providers"][pname]
    wt, base = prepare(desk, lane_name, lane, meta)
    meta = desk.meta(tid)
    model = (meta.get("model") if p["kind"] == "subscription" else "") or p["model"]  # other providers: their own
    effort = meta.get("effort") or p.get("effort", "")
    effort_from = "task" if meta.get("effort") else "provider" if effort else ""  # views.effort_text says it
    brief = desk.lanes_dir / lane_name / "brief.md"
    verifying = meta.get("kind") == "verify"
    tmp = make_worker_tmp(desk, tid)  # removed by not_started too, should the start fail on the way
    long = LONG_RUNS.format(bash_ms=60000 * bash_minutes(lane), bash_min=bash_minutes(lane),
                            default_min=bash_default_minutes(lane), check_min=merge_minutes(lane))
    system = (VERIFY_PROTOCOL if verifying else PROTOCOL).format(tid=tid, lane=lane_name, tmp=commands_tmp(tmp),
                                                                 long=long)
    system += around_note(desk, lane, meta, lane_name, tmp)
    if meta.get("attachments"):
        folder = str(Path(tmp) / "attach")
        names = ", ".join(json.dumps(a["name"], ensure_ascii=False) for a in meta["attachments"])
        system += (f"Attachments: {json.dumps(folder, ensure_ascii=False)}; files: {names}. "
                   "Write this folder's path out in full, not as $TMPDIR.\n")
    carrying = (meta.get("run_start") or {}).get("source") == "earlier run's commit"
    if carrying:
        system += ("\nYou carry on an interrupted run of this same task, from that run's commits "
                   f"({meta['run_start']['commit'][:12]}).\n")
    if brief.is_file() and brief.read_text(encoding="utf-8").strip():
        system += "\nStanding instructions for this lane:\n" + brief.read_text(encoding="utf-8")
    sid = str(uuid.uuid4())  # known up front, so a worker stopped before its report still has a record
    argv = [claude_bin(), "-p", "--output-format", "json", "--session-id", sid, "--permission-mode", "acceptEdits",
            "--max-turns", str(meta.get("max_turns") or lane.get("max_turns", 300)),
            "--model", model, "--append-system-prompt", system]
    if effort:
        argv += ["--effort", effort]
    conflicts = desk.meta(tid).get("update_conflicts") if meta.get("update") and not carrying else None
    if conflicts is not None:
        note = UPDATE_NOTE if conflicts else UPDATE_CLEAN_NOTE
        argv[argv.index("--append-system-prompt") + 1] += note.format(base=lane.get("base") or "main",
                                                                       files=", ".join(conflicts))
    try:
        check_git_file(desk, wt)  # sandbox_settings asks git in the worktree
    except GitFileChanged as e:
        raise RuntimeError(git_file_changed(desk, tid, e)) from None
    box = sandbox_settings(desk, lane, pname, p, wt, tmp)
    sandbox_guard(desk, lane_name, "verifier" if verifying else "worker", box["sandbox"], wt)  # else held (hold)
    argv += isolated(wt, box)
    left = write_list(desk, lane)[1]  # what the sandbox (the setup's and the check's too) was not given
    desk.update(tid, write_left_out=left)
    for line in left:
        desk.event("write_left_out", task=tid, lane=lane_name, why=line)
    env_left = lane_env(desk, lane)[1]  # what the worker, the setup and the check were not given
    desk.update(tid, env_left_out=env_left)
    for line in env_left:
        desk.event("env_left_out", task=tid, lane=lane_name, why=line)
    kept, not_kept = no_read(desk)  # what the principal keeps from workers: the sandbox's denyRead, and Read
    desk.update(tid, no_read_left_out=not_kept)
    for line in not_kept:
        desk.event("no_read_left_out", task=tid, lane=lane_name, why=line)
    argv += ["--disallowedTools", *desk_read_rules(desk), *desk_fence_rules(desk, wt, tmp), *no_read_rules(kept),
             *no_read_rules(session_private(desk)), *no_read_rules(other_desks_hidden(desk)),
             *[r for r in GIT_DENY if not (verifying and r in VERIFY_ALLOW)], *INTERACTIVE_TOOLS]
    argv += ["--allowedTools", *lane_allow(desk, lane, wt), *WORKER_ALLOW,
             *[rule(t, x) for x in full_paths([tmp]) for t in ("Read", "Edit")], *(VERIFY_ALLOW if verifying else [])]
    unsplit = argv_rules_problem(argv)
    if unsplit:  # a deny rule Claude Code would split leaves its folder open: no worker starts with one
        raise RuntimeError(f"hansa started no {'verifier' if verifying else 'worker'}: {unsplit}")
    env = worker_env(desk, tid, lane_name, lane, pname, p, tmp)
    d, raw = desk.tasks_dir / tid, desk.raw_dir(tid)
    # No key in it: keys are in env only; a resume reuses this command.
    write_text(raw / RESUME_ARGV, json.dumps(argv))
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
        desk.change(tid, lambda x: {"launch": sid, "session_id": sid, "base": base, "branch": f"task/{tid}",
                                   "worktree": str(wt), "setup_ran": bool(lane.get("setup")), "resumed": None,
                                   "check_retry": None, **oom_begin(x)})
        with open(d / "task.md", "rb") as stdin, open(raw / "run.json", "wb") as out, \
                open(raw / "run.err", "wb") as err:
            proc = subprocess.Popen(with_oom_score(command, str(wt), env), cwd=str(wt), env=env, stdin=stdin, stdout=out,
                                    stderr=err, start_new_session=True)
        with recording_start(desk, ("task", tid, sid), proc.pid, proc):
            desk.update(tid, phase="", model_asked=model, effort_used=effort, effort_from=effort_from, pid=proc.pid,
                        pid_start=proc_start(proc.pid))
    desk.event("task_started", task=tid, lane=lane_name, provider=pname, model=model)
    return proc


def worker_env(desk: Desk, tid: str, lane_name: str, lane: dict, pname: str, p: dict, tmp: str) -> dict:
    """The environment of a task or verification worker's Claude Code, for its start and its one resume."""
    return {**provider_env(desk, pname, p), **env_file(desk, tid, lane), **bash_limits(lane), "HANSA_TASK": tid,
            "HANSA_RUN": tmp_mark(desk, tid, tmp), "CLAUDE_CODE_TMPDIR": tmp, "TMPDIR": tmp,  # the server's TMPDIR is not the run's (make_worker_tmp)
            "GIT_AUTHOR_NAME": f"hansa worker {lane_name}", "GIT_AUTHOR_EMAIL": "hansa@localhost",
            "GIT_COMMITTER_NAME": f"hansa worker {lane_name}", "GIT_COMMITTER_EMAIL": "hansa@localhost"}


# A worker that ended its turn while background work it started still ran (background_left) has its session
# resumed once, with RESUME_NOTE, so the run is not lost: hansa stopped that work with the worker, so it runs
# it again in the foreground and then reports. Only once (meta "resumed"): a second time it ends as it is,
# and the task says what was left running. The worker's own command line, without the lane's setup around it,
# is kept in the run's raw folder (RESUME_ARGV) for this; its time limit still counts from the task's start.
RESUME_ARGV = "worker-argv.json"
RESUME_NOTE = """Your turn ended while work you started in the background was still running: {left}.
hansa stopped it when your turn ended. Run what you still need of it again in the foreground, in one
Bash call with a long enough timeout (the Bash tool's timeout, in milliseconds, up to {bash_ms}: {bash_min}
minutes), and wait for it to finish. Then
write your report, as your instructions say. This is the only time hansa resumes you.
"""


def resume(desk: Desk, tid: str):
    """Resume a task or verification worker whose run ended well while background work it started was still
    running, once (see RESUME_ARGV): its new process, or None when it is not resumed."""
    meta = desk.meta(tid)
    if meta.get("resumed") or meta.get("cancel") or meta.get("timed_out"):
        return None
    s = desk.settings()
    lane_name, pname = meta.get("lane") or "", meta.get("provider") or ""
    lane, p = s["lanes"].get(lane_name), s["providers"].get(pname)
    raw = desk.state_dir / "runs" / tid
    run = read_json(raw / "run.json", {}) or {}
    sid, tmp = meta.get("session_id") or "", meta.get("worker_tmp") or ""
    if not (lane and p and sid and tmp and os.path.isdir(tmp) and (raw / RESUME_ARGV).is_file()) \
            or run.get("is_error") or run.get("subtype") != "success":
        return None
    home = config_home(desk, pname, p)
    left = read_detail(desk, tid, "background work left", [], background_left, home, sid)
    if not left or not meta.get("worktree"):
        return None
    wt = Path(meta["worktree"])
    try:
        check_git_file(desk, wt)
        argv = json.loads((raw / RESUME_ARGV).read_text(encoding="utf-8"))
        argv[argv.index("--session-id")] = "--resume"
    except (GitFileChanged, OSError, ValueError):  # finish says what happened
        return None
    sweep(desk, tid)  # what the first run left out of its process group: it is run again, not met halfway
    note = RESUME_NOTE.format(left="; ".join(left), bash_ms=60000 * bash_minutes(lane), bash_min=bash_minutes(lane))
    env = worker_env(desk, tid, lane_name, lane, pname, p, tmp)
    with desk.lock():  # a cancel or another end lands before this (no resume, its logs as they are) or after it
        now_meta = desk.meta(tid)
        if now_meta.get("cancel") or now_meta.get("resumed") or now_meta.get("timed_out") \
                or now_meta.get("state") != "running" or now_meta.get("session_id") != sid:
            return None
        desk.update(tid, resumed={"at": now(), "left": [desk.redact(x) for x in left],
                                  "rows": len(_rows(home, sid) or []),
                                  "usage": usage_of(run) or read_detail(desk, tid, "usage", {}, usage_from_record, home, sid),
                                  "turns": run.get("num_turns")})
        with tempfile.TemporaryFile() as stdin, open(raw / "run.json", "wb") as out, open(raw / "run.err", "ab") as err:
            stdin.write(note.encode("utf-8"))
            stdin.seek(0)
            proc = subprocess.Popen(with_oom_score(argv, str(wt), env), cwd=str(wt), env=env, stdin=stdin, stdout=out, stderr=err,
                                    start_new_session=True)
        with recording_start(desk, None, proc.pid, proc):
            desk.update(tid, pid=proc.pid, pid_start=proc_start(proc.pid))
    desk.event("task_resumed", task=tid, lane=lane_name, provider=pname, left=[desk.redact(x) for x in left])
    return proc


def add_usage(a: dict, b: dict) -> dict:
    """Two runs' usage (usage_of) as one: the tokens added up by model, and the cost where both say it."""
    out = {m: dict(u) for m, u in a.items()}
    for m, u in b.items():
        t = out.get(m)
        if t is None:
            out[m] = dict(u)
            continue
        for k in ("in", "out", "cache_read", "cache_write"):
            t[k] = (t.get(k) or 0) + (u.get(k) or 0)
        cost = (t.get("claude_code_usd"), u.get("claude_code_usd"))
        t["claude_code_usd"] = None if None in cost else cost[0] + cost[1]
    return out


def usage_of(run: dict) -> dict:
    out = {}
    for model, u in (run.get("modelUsage") or {}).items():
        out[model] = {"in": u.get("inputTokens", 0), "out": u.get("outputTokens", 0),
                      "cache_read": u.get("cacheReadInputTokens", 0),
                      "cache_write": u.get("cacheCreationInputTokens", 0),
                      "claude_code_usd": u.get("costUSD"), "basis": u.get("costBasis", "")}
    return out


def usage_from_record(home: Path, sid: str, after: int = 0) -> dict:
    """Token counts from the session record, for a run stopped before Claude Code wrote its totals
    (cancelled, or stopped at the time ceiling). Some providers leave output tokens out of the
    record, so these can be short. Only the record's rows from `after` on: a resumed run's own (see resume),
    as the run before the resume has its numbers already."""
    if after:
        counts = {}
        for d in (_rows(home, sid) or [])[after:]:
            msg = d.get("message") or {} if d.get("type") == "assistant" else {}
            if msg.get("id") and isinstance(msg.get("usage"), dict):
                counts[msg["id"]] = (msg.get("model") or "?", msg["usage"])
    else:
        counts = _record(home, sid)[3]
    out = {}
    for model, u in counts.values():
        t = out.setdefault(model, {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0,
                                   "claude_code_usd": None, "basis": "session record"})
        t["in"] += u.get("input_tokens") or 0
        t["out"] += u.get("output_tokens") or 0
        t["cache_read"] += u.get("cache_read_input_tokens") or 0
        t["cache_write"] += u.get("cache_creation_input_tokens") or 0
    return out


DENIAL_KEPT = 20000  # characters of a refused call kept on the task: whole, but for a file written by a heredoc


def denial_of(d) -> str:
    """A refused call as the task keeps it: whole, so that hansa show --full can say which part was refused
    (hansa show alone gives its first line, cut)."""
    if not isinstance(d, dict):
        what = str(d)
    else:
        inp = d.get("tool_input") or {}
        what = f"{d.get('tool_name', '?')}: {inp.get('command') or inp.get('file_path') or inp.get('pattern') or ''}"
    return what if len(what) <= DENIAL_KEPT else what[:DENIAL_KEPT] + f" ... (cut at {DENIAL_KEPT} characters)"


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
                out[c["tool_use_id"]] = said[:DENIAL_KEPT]
    return out


def unusable_reason(run: dict, err: str):
    """Why this provider cannot be used: ("credit" | "quota" | "key" | "login" | "refused" | "", words)."""
    if run.get("stop_reason") in REFUSED_STOPS:
        return "refused", f"refused the request ({run['stop_reason']})"
    status = run.get("api_error_status")
    text = f"{run.get('result') or ''} {err}" if (run.get("is_error") or not run) else ""
    if status == 402 or CREDIT.search(text):
        return "credit", "credit balance too low"
    if status in QUOTA_STATUS:
        return "quota", f"out of quota (HTTP {status})"
    if status in AUTH_STATUS:
        return "key", f"key rejected (HTTP {status})"
    m = QUOTA.search(text)
    if m:
        return "quota", f"out of quota ({m.group(0)})"
    m = AUTH.search(text)  # before LOGIN: "Invalid API key · Please run /login" is a key problem
    if m:
        return "key", f"key rejected ({m.group(0)})"
    if LOGIN.search(text):
        return "login", "not logged in (Please run /login)"
    return "", ""


def network_reason(run: dict, err: str) -> str:
    """How the connection to the provider, or the provider itself, failed when the run ended on that and not
    on the task: "the connection to the provider failed (ECONNRESET)", say. '' when it ended otherwise."""
    if not run.get("is_error"):
        return ""
    status = run.get("api_error_status")
    if status == OVERLOADED_STATUS:
        return f"the provider was overloaded (HTTP {status})"
    if isinstance(status, int) and 500 <= status <= 599:
        return f"the provider had a server error (HTTP {status})"
    m = NETWORK.search(str(run.get("result") or "") or err)
    if not m:
        return ""
    found = next(x for x in m.groups() if x)
    if re.search(r"overloaded|529", found, re.I):
        return f"the provider was overloaded ({found})"
    if re.fullmatch(r"API Error: 5\d\d", found, re.I):
        return f"the provider had a server error ({found})"
    return f"the connection to the provider failed ({found})"


def read_detail(desk: Desk, tid: str, what: str, default, fn, *args):
    """fn(*args), a reading that only adds detail to a run's end; on an error, default, with the error in the
    event log, so the run still ends."""
    try:
        return fn(*args)
    except Exception as e:  # noqa: BLE001 - any error: the run ends on the plainest reading
        desk.event("detail_unread", task=tid, what=what, error=f"{type(e).__name__}: {e}"[:300])
        return default


def interrupted_restart(desk: Desk, wt: Path, meta: dict, fields: dict) -> dict:
    """When a run is cut off by a dropped connection, the provider's limit or the system, and hansa queues it again:
    1. The run's own work is the commits from where it started (run_start) to its head. A run started
       by an older hansa, with no run_start, counts from its start_from, else from its base.
    2. Carry on from its head when that work is not empty, holds no key and no conflict markers,
       and the run did not begin with an update's conflicts to settle.
    3. Otherwise, if that run was itself carried on, the next run carries on from where that run
       started: the earlier run's head, checked when that run ended. Record why the newest work
       was not carried.
    4. Otherwise, the next run starts as before this change, recording why: an ordinary task from
       its base; a follow-up from start_from, with its --update merge and that update's note again.

    A run began with an update's conflicts to settle when its preparation merged the base in and
    left conflicts (update_conflicts not empty). Such a run is never carried on, whatever git left
    in the files: a modify/delete, rename or binary conflict leaves no markers.
    Verifications and other stops keep their existing retry behavior.
    """
    run_start = meta.get("run_start") or {}
    start = run_start.get("commit") or meta.get("start_from") or meta["base"]
    head = fields.get("head")
    work = fields if head else {}
    if head and start != meta["base"]:
        work = {"commits": int(wgit(desk, wt, "rev-list", "--count", f"{start}..{head}") or 0),
                "leaked": keys_in_change(desk, wt, start, head),
                "markers": markers_in_change(wt, start, head)}
    reasons = []
    if not head or not work.get("commits"):
        reasons.append("the interrupted run left no commits")
    if work.get("leaked"):
        reasons.append("the interrupted run's commits hold a key")
    if work.get("markers"):
        reasons.append("the interrupted run's commits hold conflict markers")
    if meta.get("update_conflicts") and not work.get("markers"):
        reasons.append("the interrupted run began with an update's conflicts to settle")
    if not reasons:
        return {"commit": head, "source": "earlier run's commit", "why": ""}
    if run_start.get("source") == "earlier run's commit":
        return {"commit": start, "source": "earlier run's commit", "why": "; ".join(reasons)}
    return {"commit": meta.get("start_from") or meta["base"],
            "source": "earlier task's commit" if meta.get("start_from") else "base", "why": "; ".join(reasons)}


def finish(desk: Desk, tid: str, returncode) -> dict:
    meta = desk.meta(tid)
    memory = oom_result(meta.get("oom_start"))
    d = desk.tasks_dir / tid
    done = desk.state_dir / "runs" / tid / "setup.log.done"
    set_up = not meta.get("setup_ran") or done.exists()  # else the worker never started: nothing is its work
    done.unlink(missing_ok=True)
    (done.parent / RESUME_ARGV).unlink(missing_ok=True)
    remove_sandbox_tmp(desk, tid)
    publish(desk, tid, "run.json", "run.err", "setup.log")
    run = read_json(d / "run.json", {}) or {}
    # killed by the system once the lane's setup had finished, before the worker wrote its result; hansa's own stops
    # (a changed .git file, cancel, sandbox_failed, timed_out) keep their branches, which come first.
    # Then the kill, before its stderr is read for the provider's quota, key, login or refusal (unusable_reason)
    killed = not run and set_up and returncode in KILLED_EXITS
    err = (d / "run.err").read_text(encoding="utf-8", errors="replace")[-3000:] if (d / "run.err").exists() else ""
    setup = SETUP_FAILED.search(err)  # then the worker never started: nothing here is the provider's doing
    result = run.get("result") or ""
    report = result
    stopped = meta.get("timed_out") or run.get("subtype") == "error_max_turns"
    p = desk.settings()["providers"].get(meta.get("provider"))
    usage = read_detail(desk, tid, "usage", {}, usage_of, run)
    if not usage and p:
        usage = read_detail(desk, tid, "usage", {}, lambda: usage_from_record(
            config_home(desk, meta["provider"], p), meta.get("session_id", ""), (meta.get("resumed") or {}).get("rows", 0)))
    resumed = meta.get("resumed") or {}
    if resumed.get("usage"):  # the first run's, before hansa resumed it
        usage = add_usage(resumed["usage"], usage)
    late = read_detail(desk, tid, "report after notices", None, report_after_notices,
                       config_home(desk, meta["provider"], p), meta.get("session_id", "")) \
        if p and not stopped else None
    if late:
        report = late[0] + "".join(LATE_NOTE + x for x in late[1])
    if stopped and not result.strip() and p:
        notes = read_detail(desk, tid, "notes", "", notes_of, config_home(desk, meta["provider"], p),
                            meta.get("session_id", ""))
        report = STOPPED_NOTE + notes if notes else ""
        if meta.get("timed_out"):  # what it was doing when the time ran out: a long command, say
            call = read_detail(desk, tid, "last call", "", last_call, config_home(desk, meta["provider"], p),
                               meta.get("session_id", ""), now())
            report += ("\n\n" if report else "") + call if call else ""
    report = desk.redact(report)
    denied = run.get("permission_denials") or []
    reasons = read_detail(desk, tid, "refusal reasons", {}, refusal_reasons, config_home(desk, meta["provider"], p),
                          meta.get("session_id", "")) if p and denied else {}
    write_text(d / "result.md", report + ("\n" if report else ""))
    fields = {"finished": now(), "exit": returncode, "pid": None, **memory,
              "session_id": run.get("session_id") or meta.get("session_id", ""),
              "turns": run.get("num_turns"), "subtype": run.get("subtype", ""),
              "usage": usage, "denials": [desk.redact(denial_of(x)) for x in denied],
              "denial_reasons": [desk.redact(reasons.get(x.get("tool_use_id"), "") if isinstance(x, dict) else "")
                                 for x in denied],
              "background_left": [desk.redact(x) for x in read_detail(
                  desk, tid, "background work left", [], background_left, config_home(desk, meta["provider"], p),
                  meta.get("session_id", ""), resumed.get("rows", 0))] if p else []}
    if resumed.get("turns") and isinstance(fields["turns"], int):
        fields["turns"] += resumed["turns"]
    if meta.get("kind") == "verify":
        found = VERDICT.findall(report if late else result)
        fields["verdict"] = found[-1].lower() if found else ""
    sweep(desk, tid)  # nothing the run left running may write in the worktree or its branch while hansa commits
    wt = Path(meta["worktree"]) if meta.get("worktree") else None
    changed = ""  # what the principal is told when the worktree's .git was changed: then the task fails
    recorded = False
    restart = None
    try:
        if wt is None:
            raise RuntimeError("the task never got a worktree")
        check_git_file(desk, wt, desk.settings()["lanes"].get(meta.get("lane"), {}).get("repo"))
        branch = meta.get("branch") or f"task/{tid}"
        check_head(desk, wt, branch)  # what hansa commits or stashes on is the task's
        head = check_branch_ref(desk, wt, branch)  # and that branch's ref leads nowhere else
        # A run may hold only the merge hansa made (retry --update).
        merged = check_no_operation(
            desk, wt, meta.get("base", "") if meta.get("update") and meta.get("update_conflicts") else "")
        if wgit(desk, wt, "status", "--porcelain") and set_up and not setup:  # a setup's leftovers are nobody's work
            if meta.get("kind") == "verify":  # its scratch changes are evidence, not work: kept aside
                wgit(desk, wt, "add", "-A")
                tree = wgit(desk, wt, "write-tree")
                scratch = wgit(desk, wt, "commit-tree", tree, "-p", head, "-m", f"hansa: {tid} scratch changes")
                ref = f"refs/hansa/scratch/{tid}"
                wgit(desk, wt, "update-ref", ref, scratch)
                fields["scratch_ref"] = ref
                # Only clean after the snapshot has a ref; neither the task branch nor refs/stash moves.
                wgit(desk, wt, "reset", "--hard", head)
                wgit(desk, wt, "clean", "-fd")
            else:
                ln = desk.settings()["lanes"].get(meta.get("lane"))
                head = commit_work(desk, wt, task_message(ln, meta.get("lane") or "", tid, meta.get("title") or ""),
                                   branch, [merged] if merged else [], identity_env(ln))
                fields["commit_message_note"] = message_fallback(ln, meta.get("lane") or "", "commit_message") or None
        # the task's work is the commit read or made here, by its id: the branch is never read again through a
        # link (a leftover the sweep could not find may still write there), and from now on hansa verifies,
        # merges, continues from and shows exactly this commit (branch_holds)
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
        restart = interrupted_restart(desk, wt, meta, fields)
        recorded = True  # the work is recorded: a ref changed now fails the task, which keeps it
        common = own_git_folder(desk, wt).resolve().parent.parent
        now_at, what = _ref_now(common, branch)
        if now_at != head:
            raise GitFolderChanged(wt, common / "refs" / "heads" / branch,
                                   f"changed after hansa committed: it {what or 'now holds ' + now_at}, not {head}, "
                                   f"the task's work, which is kept: the ref is put back to it", head, aside=True)
    except SandboxPathChanged as e:
        changed = str(e)  # full path list
        fields.update(head="", commits=0, files=[], stat="")
        restart = None
    except GitFileChanged as e:
        changed = git_file_changed(desk, tid, e)
        if not recorded:
            fields.update(head="", commits=0, files=[], stat="")
    except (RuntimeError, KeyError, OSError) as e:
        fields.update(head="", commits=0, files=[], stat="", git_error=str(e)[:300])
        restart = None

    attempts = list(meta.get("attempts", []))
    if setup:
        fields["setup_exit"] = int(setup.group(1))
    kind, why = ("", "") if meta.get("cancel") or setup or changed or killed else unusable_reason(run, err)
    # a report written before the connection went is the worker's finished work: that run ends as it did
    net = "" if kind or late else network_reason(run, err)
    if changed:
        state, error = "failed", changed
    elif meta.get("cancel"):
        state, error = "cancelled", "cancelled"
    elif meta.get("sandbox_failed"):  # stopped by sandbox_watch: held, without using a try
        state, error = "queued", f"the sandbox could not start: {meta['sandbox_failed']}"
    elif setup:
        state, error = "failed", (f"the lane's setup command failed (exit {setup.group(1)}), so the worker never "
                                  f"started; hansa show {tid} has the setup's last lines")
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
        state = "queued" if left and tries_used(attempts) < TRIES else "failed"
        error = f"{meta['provider']}: {why}; " + (
            "queued again for the lane's next provider" if state == "queued" else "no other provider in the lane to try")
    elif kind in ("credit", "quota"):
        until = rest_provider(desk, desk.settings(), meta["provider"], kind, why, f"{run.get('result') or ''}\n{err}")
        counted = kind != "credit" and sum(a.get("kind") == "limit" for a in attempts) >= LIMIT_RETURNS
        attempts.append({"provider": meta["provider"], "at": now(), "error": why,
                         "kind": "credit" if kind == "credit" else "limit", "counted": counted,
                         "rest_until": round(until) if until is not None else None})
        state = "queued" if tries_used(attempts) < TRIES else "failed"
        error = f"{meta['provider']}: {why}; " + (
            f"gave up after {TRIES} tries" if state == "failed" else
            f"queued again; this one used a try, since the task had already gone back {LIMIT_RETURNS} times for a "
            "provider's limit" if counted else
            "queued again without using a try; it starts again when a provider of its lane is ready")
    elif kind:
        rest_provider(desk, desk.settings(), meta["provider"], kind, why)
        attempts.append({"provider": meta["provider"], "at": now(), "error": why})
        state = "queued" if tries_used(attempts) < TRIES else "failed"
        error = f"{meta['provider']}: {why}; " + (
            "queued again for the lane's next provider" if state == "queued" else f"gave up after {TRIES} tries")
    elif net:
        with desk.lock():  # mark and count together: later finishes in this outage use no try
            st = desk.provider_state().get(meta["provider"], {})
            counted = not st.get("unreachable_since")
            if counted:
                desk.set_provider_state(meta["provider"], unreachable_since=now(), unreachable_reason=desk.redact(net),
                                        probe_at=0)
        again = counted and any(a.get("kind") == "network" and a.get("counted", True) for a in attempts)
        attempts.append({"provider": meta["provider"], "at": now(), "error": net, "kind": "network", "counted": counted})
        state = "failed" if again else "queued"
        retry = (f"hansa verify {meta['parent']}" if meta.get("kind") == "verify" and meta.get("parent")
                 else f'hansa retry {tid} "try again"')
        error = f"{meta['provider']}: {net}; " + (
            ("queued again; waits until the provider answers a check" if counted else
             "queued again without using a try; the provider is still unreachable") if state == "queued" else
            "the connection to the provider failed twice, so hansa stopped trying. This is the machine's or the "
            f"provider's problem, not the task's: when the connection is back, {retry}")
    elif killed:  # queued again once, as after a dropped connection, without using a try; each cause counts its own
        before = [a for a in attempts if a.get("kind") == "killed"]
        how = f"killed by the system (exit {returncode})"
        state = "failed" if before else "queued"
        retry = (f"hansa verify {meta['parent']}" if meta.get("kind") == "verify" and meta.get("parent")
                 else f"hansa retry {tid}")
        error = f"{how}; queued again once, without using a try" if state == "queued" else (
            f"killed by the system twice (exit {before[0].get('exit')}, then exit {returncode}), with no word from "
            f"the {'verifier' if meta.get('kind') == 'verify' else 'worker'}: the machine may be short of memory, "
            f"so fewer runs at once may help (hansa concurrency). Next: {retry}")
        if state == "failed":
            counts = [f"run {i}: {oom_text(a)}" for i, a in enumerate([before[0], memory], 1) if oom_text(a)]
            if counts:
                error += "; " + "; ".join(counts)
        attempts.append({"provider": meta["provider"], "at": now(), "kind": "killed", "exit": returncode,
                         "error": error if state == "queued" else how, **memory})
        if isinstance(resumed.get("turns"), int) and not isinstance(fields["turns"], int):  # its first process's
            fields["turns"] = resumed["turns"]  # turns are known, even 0; its killed continuation's own are not
            attempts[-1]["turns_unknown_after_resume"] = True
        if meta.get("kind") == "verify":  # a killed run gives no verdict, whatever its record said before the kill
            fields["verdict"] = ""
    elif run.get("is_error") or run.get("subtype") not in ("success", None) or not run:
        state = "failed"
        lines = (result or err).strip().splitlines()
        subtype = run.get("subtype") or "error"  # Claude Code says "success" of a run that ended on an error
        error = (f"{'error' if subtype == 'success' else subtype}: "
                 f"{lines[-1][:200] if lines else f'no output (exit {returncode})'}")
    else:
        state, error = "done", ""
    lane = desk.settings()["lanes"].get(meta.get("lane"), {})

    def decide(now_meta):
        final, message = ("cancelled", "cancelled") if now_meta.get("cancel") and not changed else (state, error)
        checking = (final == "done" and bool(lane.get("check")) and bool(fields.get("commits"))
                    and meta.get("kind") != "verify")  # a verification is never merged, so never checked
        tries, kept = attempts, {}
        if final == "queued" and len(attempts) > len(meta.get("attempts", [])):
            # queued for another try: this run's numbers go with its attempt record, and the task's own fields are
            # left for the next run, so each try counts once (cost.task_runs). What this try's resume recorded
            # (resumed) is keyed to its session, and its usage is in this run's: a new try starts without it
            tries = [*attempts[:-1], {**attempts[-1], **run_numbers(meta, fields)}]
            kept = {"usage": {}, "turns": None}
        again = {"resumed": None, "restart": None} if final == "queued" else {}
        if (final == "queued" and len(attempts) > len(meta.get("attempts", []))
                and meta.get("kind") != "verify"
                and (kind in ("quota", "credit") or net or attempts[-1].get("kind") == "killed")):
            again["restart"] = restart or interrupted_restart(desk, wt, meta, fields)
        if final == "queued" and meta.get("sandbox_failed"):  # its numbers still count (cost), as a run of the task
            kept = {"usage": {}, "turns": None, "earlier_runs": [*(now_meta.get("earlier_runs") or []), {
                "provider": meta.get("provider", ""), "at": now(), **run_numbers(meta, fields),
                "why": f"stopped: the sandbox could not start ({meta['sandbox_failed']}); the task was held"}]}
        if meta.get("kind") == "verify" and final != "queued" and fields.get("verdict") == "holds, with notes" \
                and "verification_notes" not in now_meta:
            from .gaps import collect
            kept["verification_notes"] = collect(desk, now_meta, report, fields["finished"])
        return {"state": "running" if checking else final, "phase": "checking" if checking else "",
                "error": desk.redact(message), "attempts": tries, "pid_start": "", **fields, **kept, **again}
    meta = desk.change(tid, decide)
    if meta["state"] == "queued" and meta.get("sandbox_failed"):
        hold(desk, tid, meta.get("lane") or "", meta["error"], "",
             {"verify": "verifier"}.get(meta.get("kind"), "worker"))
        meta = desk.meta(tid)
    desk.event("task_finished", task=tid, state="checking" if meta.get("phase") == "checking" else meta["state"],
               provider=meta.get("provider"), error=meta["error"])
    if meta.get("phase") != "checking":  # else after its check, which carries the same id
        sweep(desk, tid)
        meta = desk.meta(tid)
    return meta


def run_numbers(meta: dict, fields: dict) -> dict:
    """What one run of a task used, as kept on its attempt record when the task is queued for another try."""
    return {"usage": fields.get("usage") or {}, "turns": fields.get("turns"), "session_id": fields.get("session_id", ""),
            "launch": meta.get("launch", ""), "ran_from": meta.get("started"), "ended": fields.get("finished") or now(),
            "model": meta.get("model_asked", ""), "effort": meta.get("effort_used", ""),
            "effort_from": meta.get("effort_from", ""), "run_start": meta.get("run_start"),
            **({"oom_kills": fields["oom_kills"]} if fields.get("oom_kills") else {})}


def keep_lost_run(desk: Desk, m: dict) -> dict:
    """For a task an earlier server started that goes back to the queue: the numbers of the run it may have started
    meanwhile, from its session record, to keep in earlier_runs; {} when there is none, or it is already kept.
    Kept by launch, as each start of a task has its own; of a session resumed (resumed), the part before the resume
    is in resumed's numbers and only the part after it is read from the record."""
    sid, launch = m.get("session_id") or "", m.get("launch") or ""
    if not sid or launch != sid:
        return {}
    if any(x.get("launch", x.get("session_id")) == launch  # records made before launch was kept: their session
           for x in [*(m.get("attempts") or []), *(m.get("earlier_runs") or [])] if isinstance(x, dict)):
        return {}
    p = desk.settings()["providers"].get(m.get("provider") or "")
    resumed = m.get("resumed") or {}
    usage = read_detail(desk, m["id"], "usage", {}, lambda: usage_from_record(
        config_home(desk, m["provider"], p), sid, resumed.get("rows", 0))) if p else {}
    if resumed.get("usage"):
        usage = add_usage(resumed["usage"], usage)
    if not usage:
        return {}
    return {"earlier_runs": [*(m.get("earlier_runs") or []), {
        "provider": m.get("provider", ""), "at": now(), "usage": usage, "turns": resumed.get("turns"),
        "session_id": sid, "launch": launch,
        "ran_from": m.get("started"), "ended": now(), "model": m.get("model_asked", ""),
        "why": "hansa serve stopped while it started this run; the task went back to the queue"}]}


@git_folders_kept
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
        srt, settings, tmp, own = in_srt(desk, tid, "check", lane, pname, desk.settings()["providers"].get(pname) or {}, wt)
        env = {**base_env(), "HANSA_TASK": tid, "HANSA_RUN": run_mark(desk, tid), "TERM": "dumb", "TMPDIR": tmp,
               "CLAUDE_CODE_TMPDIR": tmp}
        desk.change(tid, lambda x: {"launch": token, "check_started": now(), "check_command": lane["check"],
                                   **oom_begin(x, "check_")})
        with open(raw / "check.log", "wb") as out:
            proc = subprocess.Popen(with_oom_score(["/bin/sh", "-c", CHECK_SCRIPT, token, in_sandbox(desk, lane, lane["check"]), str(raw / "check.exit"), tmp,
                                                   srt, settings, own], str(wt), env), cwd=str(wt), env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                    start_new_session=True)
        with recording_start(desk, ("check", tid, token), proc.pid, proc):
            desk.update(tid, pid=proc.pid, pid_start=proc_start(proc.pid),
                        **({"check": {**meta["check_retry"], "rerun_started": True}} if meta.get("check_retry") else {}))
    desk.event("check_started", task=tid, lane=lane_name)
    return proc


def check_not_started(desk: Desk, tid: str, e: Exception) -> None:
    m = desk.meta(tid)
    why = desk.redact(f"the lane's check could not run: {e}")[:600]
    remove_sandbox_tmp(desk, tid)
    publish(desk, tid, "check.log")
    desk.change(tid, lambda x: {"state": "failed", "phase": "", "pid": None, "pid_start": "", "error": why})
    desk.event("task_checked", task=tid, ok=False, exit=None, note=why)
    sweep(desk, tid, run={"worktree": m.get("worktree"), "started": m.get("check_started")})
    desk.failed_starts.pop(("check", tid, desk.meta(tid).get("launch")), None)


def verification_due(m: dict) -> bool:
    """A queue-time request ready for serve to handle, including after a restart."""
    chk = m.get("check") or {}
    return bool(m.get("verify_after") and m.get("kind") != "verify" and m.get("state") == "done"
                and m.get("commits") and not m.get("verify") and not m.get("verify_after_error")
                and (not chk or chk.get("ok")) and not chk.get("could_not_run"))


def verification_takes_over(m: dict) -> bool:
    """The requested verification (or a coordinator's action) replaces the task's completion post."""
    return bool(m.get("verify_after") and (verification_due(m) or m.get("verify")
                or m.get("state") in ("merged", "merging", "replaced")
                or m.get("error") == "closed without merging"))


def finish_check(desk: Desk, tid: str) -> dict:
    m = desk.meta(tid)
    ended = {"worktree": m.get("worktree"), "started": m.get("check_started")}
    memory = oom_result(m.get("check_oom_start"))
    raw, d = desk.state_dir / "runs" / tid, desk.tasks_dir / tid
    done = raw / "check.exit" if (raw / "check.exit").exists() else d / "check.exit"
    try:
        code = int(done.read_text().strip())
    except (OSError, ValueError):
        code = None
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
    check = {"command": m.get("check_command", ""), "ok": code == 0 and not note, "exit": code, "note": note,
             **memory, **({"retried": m["check_retry"]} if m.get("check_retry") else {}),
             "seconds": round(now() - (m.get("check_started") or now())), "tail": log.strip().splitlines()[-15:]}
    stopped = m.get("cancel") or m.get("timed_out") or changed
    if check_killed(check, stopped):
        if not check.get("retried"):
            check["note"] = check_retry_reason(check)
            queued = desk.change(tid, lambda x: None if x.get("cancel") or x.get("timed_out") else
                                 {"check_retry": check, "check": check, "pid": None, "pid_start": "", "launch": "",
                                  "check_started": None, "lookup_failing_since": None})
            if queued:
                done.unlink(missing_ok=True)
                sweep(desk, tid, run=ended)
                desk.event("check_killed", task=tid, exit=code, **memory)
                return desk.meta(tid)
            latest = desk.meta(tid)  # a stop arrived while the output was being published
            check["note"] = "stopped on request" if latest.get("cancel") else "stopped at the lane's time ceiling"
        elif check["retried"].get("oom_kills"):
            check["note"] = killed_twice(check["retried"], check)
    m = desk.change(tid, lambda now_meta: {"state": "failed" if changed else "cancelled" if now_meta.get("cancel")
                                           else "done", "phase": "", "pid": None, "pid_start": "", "check": check,
                                           "error": changed or ("cancelled" if now_meta.get("cancel") else "")})
    done.unlink(missing_ok=True)  # the exit remains recoverable until the end is recorded
    desk.event("task_checked", task=tid, ok=check["ok"], exit=code, note=check["note"])
    sweep(desk, tid, run=ended)
    return desk.meta(tid)


def check_killed(result: dict, stopped) -> bool:
    return account.LINUX and not stopped and not result["ok"] and bool(result.get("oom_kills"))


def check_retry_reason(result: dict) -> str:
    n = result.get("oom_kills")
    if not n:  # a retry recorded by an older hansa, before the count was required
        return "its first run failed"
    return (f"it failed while the system's out-of-memory killer killed {n} "
            f"{'process' if n == 1 else 'processes'} on this machine")


def exit_text(result: dict) -> str:
    return "no exit code" if result.get("exit") is None else f"exit {result['exit']}"


def killed_twice(first: dict, last: dict) -> str:
    return (f"failed twice while the out-of-memory killer ran ({first['oom_kills']}, then {last['oom_kills']}): "
            "the machine may be short of memory, so fewer runs at once may help (hansa concurrency)")


# A merge check: `hansa merge` of a task whose base has moved since it was made, on a lane with a check or a
# merge_check, or of any task on a lane whose merge_check differs from its check (merge_needs_check), or
# `hansa merge --recheck` of any task on such a lane (merge_check {recheck: True, by: who asked}). It runs the
# lane's merge_check if it has one, else its check (merge_command), under merge_timeout_min if set, else
# timeout_min (merge_minutes).
# The task is "merging" meanwhile, with merge_check {phase: queued | starting | running, ...}; hansa serve
# runs it when the lane is free, in the lane's worktree, and it ends in "merged", or back in "done" with
# merge_check_result saying why nothing was merged. One check runs at a time per repository and base branch,
# taking consecutive queued tasks up to the execution lane's merge_batch. Followers have phase batched and
# leader naming the task that owns the process and output; members records their order. Unsuccessful batches
# return to their original merge_check.queued positions with alone=True. A merge that
# needs no check, asked while one runs or waits there, joins the same line (merge_check {plain: True}, plain_merge).
# A configured merge_lane on that repository and base takes the checks instead. It is selected at start and
# recorded as merge_check.lane: only that lane is held, and its commands and sandbox run the check, while the
# task lane still supplies the merge message and commit identity.
MERGE_FILES = ("merge-check.log", "merge-setup.log", "merge-check.err")
MERGE_MOVES = 2  # the base moved this often while its merge was being checked: hansa stops trying


def base_moved(repo, tip: str, head: str) -> bool:
    """Whether the base (at tip) has commits the task's branch (at head) does not."""
    r = subprocess.run(["git", "merge-base", "--is-ancestor", tip, head], cwd=str(repo), capture_output=True)
    return r.returncode != 0


def merge_command(lane: dict) -> str:
    """The command a lane's merge check runs: its merge_check, else its check; '' when it has neither."""
    return lane.get("merge_check") or lane.get("check") or ""


def merge_minutes(lane: dict) -> int:
    """A lane's merge check's time limit in minutes: its merge_timeout_min, else its timeout_min."""
    return lane.get("merge_timeout_min") or lane.get("timeout_min", 240)


BASH_MARGIN_MIN = 5  # beyond the check's own limit, for one Bash call that runs the lane's check


def bash_minutes(lane: dict) -> int:
    """How long one Bash call of a task or verification worker may run: as long as the lane's check may
    (merge_minutes) plus BASH_MARGIN_MIN, but never past the worker's own limit, the lane's timeout_min."""
    return min(merge_minutes(lane) + BASH_MARGIN_MIN, lane.get("timeout_min", 240))


BASH_DEFAULT_MIN = 10  # one Bash call that asks for no timeout: a command that hangs holds it no longer


def bash_default_minutes(lane: dict) -> int:
    """How long one Bash call that asks for no timeout may run: BASH_DEFAULT_MIN, or bash_minutes if less."""
    return min(BASH_DEFAULT_MIN, bash_minutes(lane))


def bash_limits(lane: dict) -> dict:
    """Claude Code's own limits on one Bash call (it stops a call after 2 minutes unless the call asks for more,
    and allows at most 10): a call that asks for a timeout may run bash_minutes, so a long suite runs in one call
    in the foreground instead of moving to the background while the worker ends its turn; one that asks for
    none stops after bash_default_minutes, so a command that hangs does not hold it for the whole limit."""
    return {"BASH_DEFAULT_TIMEOUT_MS": str(60000 * bash_default_minutes(lane)),
            "BASH_MAX_TIMEOUT_MS": str(60000 * bash_minutes(lane))}


def own_merge_check(lane: dict, task_lane: dict | None = None) -> bool:
    """Whether the lane's merge check runs a command other than its check after each task, which the task's
    own tree has therefore never passed."""
    if task_lane is not None:
        return bool(merge_command(lane)) and merge_command(lane) != task_lane.get("check")
    return bool(lane.get("merge_check")) and lane["merge_check"] != lane.get("check")


def merge_needs_check(lane: dict, m: dict, repo, tip: str, check_lane: dict | None = None) -> str:
    """Why merging this task needs a merge check first: "moved" when the base has commits its branch does not
    (the two together were never checked), "own" when the lane's merge check runs its own command, which ran on
    neither; '' when it needs none (no change, or no command to run)."""
    command_lane = lane if check_lane is None else check_lane
    if not merge_command(command_lane) or not m.get("commits"):
        return ""
    if base_moved(repo, tip, m["head"]):
        return "moved"
    return "own" if own_merge_check(command_lane, lane if check_lane is not None else None) else ""


def lane_occupied(m: dict) -> bool:
    """Whether this task holds its lane's worktree now: a worker or check on it, its merge check, or a task
    that failed while hansa could not look for its worker, until that process is known to be gone."""
    return (m["state"] == "running" or bool(m.get("lane_wait_since")) or
            (m["state"] == "merging" and (m.get("merge_check") or {}).get("phase") in ("starting", "running")))


def lane_wait_text(m: dict) -> str:
    """Why a lane waits for a task that failed while hansa could not look for its worker; '' when it does not."""
    return ("its worker's process is not known to be gone (hansa could not look for it for "
            f"{LOOK_LIMIT // 60} minutes)") if m.get("lane_wait_since") else ""


def occupied_lane(m: dict):
    """The lane a task holds; a running merge check holds its recorded lane, even if unmarked meanwhile."""
    return ((m.get("merge_check") or {}).get("lane") or m.get("lane")) if m["state"] == "merging" else m.get("lane")


def merge_target(s: dict, lane_name) -> tuple | None:
    """The repository and base branch a lane's merges move; None for a lane not in settings."""
    lane = s["lanes"].get(lane_name)
    if not lane:
        return None
    return str(Path(lane["repo"]).expanduser().resolve()), lane.get("base") or "main"


def merge_lane(s: dict, lane_name):
    """The configured merge lane for a task lane's repository and base, else the task lane itself."""
    at = merge_target(s, lane_name)
    return next((n for n, ln in s["lanes"].items() if ln.get("merge_lane") and at is not None
                 and merge_target(s, n) == at), lane_name)


def merge_check_lane(s: dict, m: dict):
    """Where a merge check runs now, or would start now if queued. Plain merges keep their task lane."""
    mc = m.get("merge_check") or {}
    if mc.get("phase") in ("starting", "running", "batched"):
        return occupied_lane(m)
    return m.get("lane") if mc.get("plain") else merge_lane(s, m.get("lane"))


def merge_order(m: dict) -> tuple:
    """A merge check's place in line: when its merge was asked (kept when it is checked again)."""
    return (m.get("merge_check") or {}).get("queued") or 0, m["id"]


def merge_check_ahead(s: dict, m: dict, tasks: list):
    """The merge check a queued one waits for: one at a time runs per repository and base branch, in the order
    the merges were asked, so each is checked on the base as the one before left it. The one running there, or
    else the one asked earlier that is still queued; None when this one is first."""
    if m["state"] != "merging" or (m.get("merge_check") or {}).get("phase", "queued") != "queued":
        return None
    at = merge_target(s, m.get("lane"))
    if not at:
        return None
    others = [x for x in tasks if x["id"] != m["id"] and x["state"] == "merging" and merge_target(s, x.get("lane")) == at]
    running = [x for x in others if (x.get("merge_check") or {}).get("phase") in ("starting", "running")]
    earlier = sorted((x for x in others if (x.get("merge_check") or {}).get("phase", "queued") == "queued"
                      and merge_order(x) < merge_order(m)), key=merge_order)
    return (running or earlier or [None])[0]


def close_verification(desk: Desk, m: dict, why: str, by=None) -> None:
    """A verification belongs to the task it checks: one still queued or running is stopped with it. One that has
    ended keeps its state and verdict, with why as a plain note (closed), not an error. by: the session whose
    hansa cancel closed it, which hears nothing of it."""
    if not m.get("verify"):
        return
    v = desk.meta(m["verify"])
    if v["state"] == "queued":
        desk.update(v["id"], state="cancelled", error=why, cancel_by=by)
    elif v["state"] == "running":  # hansa serve stops it within seconds
        desk.update(v["id"], cancel=True, cancel_by=by)
    elif not v.get("closed"):  # closed already (hansa cancel of the verification itself): the first note stays
        desk.update(v["id"], closed=why)


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
    '' when its verification holds, or it changed only text. It looks back along the whole chain of attempts this
    one retries (parent, its parent, ...) and names the nearest verdict there, whether it held or not: an older
    one that held says nothing of the code after a later one that did not."""
    def verdict(t):
        v = desk.meta(t["verify"]) if t and t.get("verify") else None
        return v if v and v.get("verdict") else None

    def found(v):
        return {"does not hold": "found it does not hold",
                "unsure": "could not tell whether it holds"}.get(v["verdict"], f"said: {v['verdict']}")
    code = [f for f in m.get("files") or [] if not text_only(f)]
    own = verdict(m)
    if not code or (own and own["verdict"] in HOLDING_VERDICTS):
        return ""
    what = f"{m['id']} changed {', '.join(code[:3])}" + (f" and {len(code) - 3} more" if len(code) > 3 else "")
    if own:
        return f"{what}, and its verification {own['id']} {found(own)}"
    earlier, seen, at = [], {m["id"]}, m  # the attempts it retries, nearest first
    while at.get("parent") and at["parent"] not in seen and at.get("kind") != "verify":
        try:
            at = desk.meta(at["parent"])
        except SystemExit:
            break
        if at.get("kind") == "verify":
            break
        seen.add(at["id"])
        earlier.append(at)
        pv = verdict(at)
        if pv:
            which = "the earlier task it retries" if len(earlier) == 1 else "an earlier task of this change"
            if pv["verdict"] not in HOLDING_VERDICTS:
                return (f"{what}, and no verification of {m['id']} held; {pv['id']} verified {at['id']}, {which}, "
                        f"and {found(pv)}")
            return (f"{what}, and no verification of {m['id']} held; {pv['id']} verified {at['id']}, {which}, not "
                    f"this one: what came after that verification (a conflict settled, code changed) is not the code "
                    f"it checked")
    if not earlier:
        return f"nothing verified this change: {what}, and no verification of it holds"
    if len(earlier) == 1:
        return (f"nothing verified this change: {what}, and neither it nor {earlier[0]['id']}, the earlier task "
                f"it retries, has a verification that holds")
    return (f"nothing verified this change: {what}, and neither it nor any earlier task it retries "
            f"({', '.join(x['id'] for x in earlier)}) has a verification that holds")


RETHINK = ("This task has not held {n} times. Before sending it back again, rethink it: make it smaller, ask for "
           "less, or start again from the base.")


def rounds_of(desk: Desk, m: dict) -> list:
    """The rounds of the task m (no verification) is the latest of, oldest first, m last: each follow-up's parent
    is the attempt hansa retry sent back. Only those hansa still has: when the earliest left is itself a follow-up
    (it has a parent), the rounds before it are gone."""
    rounds, seen = [m], {m["id"]}
    while rounds[0].get("parent") and rounds[0]["parent"] not in seen:
        try:
            r = desk.meta(rounds[0]["parent"])
        except SystemExit:
            break
        if r.get("kind") == "verify":
            break
        seen.add(r["id"])
        rounds.insert(0, r)
    return rounds


def _latest_verdict(desk: Desk, m: dict) -> str:
    """The verdict of a task's latest verification; '' when it has none, or it is gone."""
    try:
        return (desk.meta(m["verify"]).get("verdict") or "") if m.get("verify") else ""
    except SystemExit:
        return ""


def rounds_line(desk: Desk, m: dict) -> str:
    """For hansa show of a follow-up: which round it is, the earlier rounds' ids, oldest first, and how many of
    them their latest verification found do not hold, counting only what hansa still has. '' for a task that is
    no follow-up."""
    if m.get("kind") == "verify" or not m.get("parent"):
        return ""
    rounds = rounds_of(desk, m)
    earlier = rounds[:-1]
    which = (f"round {len(rounds)}" if not rounds[0].get("parent") else
             f"round {len(rounds) + 1} or later (the rounds before {rounds[0]['id']} are gone)")
    n = sum(_latest_verdict(desk, r) == "does not hold" for r in earlier)
    return (f"rounds: this is {which}; earlier rounds, oldest first: "
            f"{', '.join(r['id'] for r in earlier) or 'none left'}; a verification judged {n} of them "
            f"\"does not hold\"")


def rethink_text(desk: Desk, m: dict) -> str:
    """RETHINK, for a follow-up in review (or the verification of one, the latest) whose latest verification
    found it does not hold, when its rounds now have two or more such verdicts; '' otherwise."""
    if m.get("kind") == "verify":
        try:
            parent = desk.meta(m.get("parent") or "-")
        except SystemExit:
            return ""
        return rethink_text(desk, parent) if parent.get("verify") == m["id"] else ""
    if not m.get("parent") or m.get("state") != "done" or _latest_verdict(desk, m) != "does not hold":
        return ""
    n = sum(_latest_verdict(desk, r) == "does not hold" for r in rounds_of(desk, m))
    return RETHINK.format(n=n) if n >= 2 else ""


def merge_result_lane(m: dict, lane_name: str | None) -> dict:
    """Record the execution lane only when it differs from the task's own lane."""
    return {"lane": lane_name} if lane_name and lane_name != m.get("lane") else {}


def end_merge_check(desk: Desk, tid: str, result: dict | None) -> dict | None:
    """Nothing was merged: the task goes back to waiting for review, with the result saying why. A merge that
    needed no check and waited its turn (plain) says so in its result (waited): no check ran for it."""
    meta = desk.meta(tid)
    mc = meta.get("merge_check") or {}
    if len(mc.get("members", [])) > 1:
        return split_merge_batch(desk, mc.get("leader", tid), result)
    plain, check_lane = mc.get("plain"), mc.get("lane")
    if result:
        if mc.get("retried") and not result.get("retried"):
            saved = meta.get("merge_check_result") or {}
            field = "retried" if saved.get("rerun_started") or saved.get("retried") else "previous"
            result = {**result, field: mc["retried"]}
        result = {**result, **merge_result_lane(desk.meta(tid), check_lane)}
    if plain and result:
        result = {**result, "waited": True}
    m = desk.change(tid, lambda x: None if x["state"] != "merging" else
                    {"state": "done", "merge_check": None, "merge_check_result": result})
    if m:
        desk.event("merge_check_ended", task=tid, ok=False, note=(result or {}).get("note", "cancelled"),
                   **({"waited": True} if plain else {}),
                   **({"command": result["command"]} if (result or {}).get("command") else {}))
    return m


def split_merge_batch(desk: Desk, tid: str, result: dict | None) -> dict:
    """Keep every surviving member's place, but check it alone from now on. A cancellation ends only its task."""
    with desk.lock():
        leader = desk.meta(tid)
        mc = leader.get("merge_check") or {}
        for member in mc.get("members", [tid]):
            m = desk.meta(member)
            own = m.get("merge_check") or {}
            if m["state"] != "merging":
                continue
            queued = {k: own[k] for k in ("queued", "recheck", "by") if k in own}
            desk.update(member, merge_check={**queued, "phase": "queued", "moves": 0, "alone": True},
                        merge_check_result={**(result or {}), "batch": mc.get("members", []), "output_task": tid})
            if own.get("cancel"):
                end_merge_check(desk, member, None)
        desk.event("merge_batch_split", task=tid, members=mc.get("members", [tid]),
                   note=(result or {}).get("note", "stopped on request"))
        return desk.meta(tid)


def plain_merge(desk: Desk, tid: str, lane: dict) -> str | None:
    """A merge that needed no check when it was asked, but waited its turn behind a merge check on the same
    repository and base branch (merge_check {plain: True}), now first in line: merged without a check if the
    base has still not moved (or its lane has no check, or it was asked --despite-check). "check" when it now
    needs a merge check (merge_needs_check: the base has moved, or the lane's merge check was set meanwhile):
    it is now a merge check like the others, still first in line; None otherwise (merged, or back for review
    saying why nothing was merged)."""
    with desk.lock():  # a cancel or a key saved meanwhile lands before this or after it
        m = desk.meta(tid)
        mc = m.get("merge_check") or {}
        if m["state"] != "merging" or mc.get("phase") != "queued" or not mc.get("plain"):
            return None
        repo, name = lane["repo"], lane.get("base") or "main"
        tip = git("rev-parse", name, cwd=repo)
        s = desk.settings()
        check_name = merge_lane(s, m["lane"])
        check_lane = lane if check_name == m["lane"] else s["lanes"][check_name]
        unchecked = bool(merge_needs_check(lane, m, repo, tip, check_lane))
        if unchecked and not mc.get("despite"):
            desk.update(tid, merge_check={"phase": "queued", "queued": mc.get("queued"), "moves": 0})
            desk.event("merge_check_queued", task=tid, onto=tip, waited=True, command=merge_command(check_lane))
            return "check"
        why = branch_holds(repo, m)  # what is merged is the commit recorded as the task's work, still its branch's
        current = git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo)
        if why:
            pass
        elif current != name:
            why = f"the checkout at {repo} is on {current}, not {name}; the principal needs to switch it back"
        elif git("status", "--porcelain", "--untracked-files=no", cwd=repo):
            why = f"the checkout at {repo} has uncommitted changes"
        else:
            leaked = keys_in_change(desk, repo, m["base"], m["head"])  # a key saved meanwhile counts too
            words = [] if leaked else words_in_merge(desk, repo, tip, m["head"],
                                                     merge_message(lane, m["lane"], tid, m["title"]), lane, m["lane"])
            if leaked:
                desk.update(tid, leaked=leaked)
                why = f"the change holds the key of {', '.join(leaked)}"
            elif words:
                desk.event("merge_forbidden", task=tid, found=len(words))
                why = desk.redact(words_text(tid, words))
        if why:
            end_merge_check(desk, tid, {"ok": False, "onto": tip, "at": now(), "note": f"{why}; nothing was merged"})
            return None
        try:
            git("merge", "--no-ff", "-m", merge_message(lane, m["lane"], tid, m["title"]), m["head"], cwd=repo,
                env={"HANSA_MERGING": "1", **identity_env(lane)})
        except RuntimeError as e:
            files = conflicted_files(lambda *x: git(*x, cwd=repo))
            places = desk.redact(conflict_places(Path(repo), files)) if files else ""  # read before the abort
            try:
                git("merge", "--abort", cwd=repo)
            except RuntimeError:
                pass
            end_merge_check(desk, tid, {"ok": False, "onto": tip, "at": now(), "files": files,
                                        "note": f"conflicts with {name} as it is now" if files
                                        else desk.redact(f"merging it into {name} failed ({str(e)[:200]})"),
                                        "conflict": desk.redact(conflict_advice(tid, name, places, str(e)[:300]))})
            return None
        m = desk.update(tid, state="merged", merged=now(), merge_commit=git("rev-parse", "HEAD", cwd=repo),
                        merge_repo=str(Path(repo).resolve()), merge_base=name,
                        merge_check=None, merge_check_result=None, unverified=unverified_note(desk, m) or None,
                        merge_message_note=message_fallback(lane, m["lane"], "merge_message") or None)
        close_verification(desk, m, f"closed: {tid} was merged")
    desk.event("task_merged", ts=m["merged"], task=tid, into=name, waited=True, onto=tip,
               **({"despite_check": True} if mc.get("despite_check") or unchecked else {}),
               **({"merge_not_checked": True} if unchecked else {}))
    return None


@git_folders_kept
def start_merge_check(desk: Desk, tid: str, lane_name: str, lane: dict):
    """Merge a task onto the base's current tip in the lane's worktree, then run the lane's setup (if it has
    one) and merge check command (merge_command) there, in srt, as the check after a task. Returns the process;
    None when it did not start (cancelled meanwhile), or ended at once (a conflict; or no command any more, and
    so merged)."""
    raw = desk.raw_dir(tid)
    for f in ("merge-check.exit", "merge-setup.log.done", *MERGE_FILES):
        (raw / f).unlink(missing_ok=True)
    (raw / "merge-check.err").touch()  # publish an empty error over the previous run's setup failure
    token = f"hansa-merge-{tid}-{uuid.uuid4().hex[:8]}"  # on its command line, for a restarted server
    with desk.lock():  # a cancel lands before this (no check) or after it (the check is stopped)
        m = desk.meta(tid)
        mc = m.get("merge_check") or {}
        if m["state"] != "merging" or mc.get("phase") != "queued":
            return None
        s = desk.settings()
        if merge_lane(s, m["lane"]) != lane_name:  # settings changed since this round chose a free lane
            return None
        task_lane = lane if lane_name == m["lane"] else s["lanes"][m["lane"]]
        if lane_name != m["lane"]:
            lane = s["lanes"][lane_name]
        mc = {**mc, "lane": lane_name}
        desk.update(tid, merge_check=mc)
        repo, name = Path(lane["repo"]), lane.get("base") or "main"
        wt = desk.worktrees_dir / lane_name
        tip = git("rev-parse", name, cwd=repo)
        held = branch_holds(repo, m)  # what is merged is the commit recorded as the task's work, still its branch's
        if held:
            end_merge_check(desk, tid, {"ok": False, "onto": tip, "at": now(), "note": f"{held}; nothing was merged"})
            return None
        ready_worktree(desk, repo, wt, tid, tip)
        try:
            wgit(desk, wt, "checkout", "-q", "--detach", tip)
            wgit(desk, wt, "merge", "--no-ff", "--no-verify", "-m", merge_message(task_lane, m["lane"], tid, m["title"]), m["head"],
                 env=identity_env(task_lane))
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
        said = message_fallback(task_lane, m["lane"], "merge_message")  # the message this merge commit was given
        command = merge_command(lane)
        entries = [{"task": tid, "onto": tip, "commit": commit, "message_note": said}]
        if command and not mc.get("alone"):
            waiting = sorted((x for x in desk.tasks() if x["state"] == "merging"
                              and merge_target(s, x.get("lane")) == merge_target(s, m["lane"])
                              and merge_order(x) > merge_order(m)), key=merge_order)
            for other in waiting[:lane.get("merge_batch", 4) - 1]:
                omc = other.get("merge_check") or {}
                if (omc.get("phase") != "queued" or omc.get("alone") or omc.get("plain")
                        or merge_check_lane(s, other) != lane_name or branch_holds(repo, other)):
                    break
                other_lane = s["lanes"][other["lane"]]
                before = entries[-1]["commit"]
                try:
                    wgit(desk, wt, "merge", "--no-ff", "--no-verify", "-m",
                         merge_message(other_lane, other["lane"], other["id"], other["title"]), other["head"],
                         env=identity_env(other_lane))
                except GitFileChanged:
                    raise
                except RuntimeError:
                    if merge_in_progress(desk, wt):
                        wgit(desk, wt, "merge", "--abort")
                    break  # this task gets the usual conflict report when its own turn comes
                entries.append({"task": other["id"], "onto": before,
                                "commit": wgit(desk, wt, "rev-parse", "HEAD"),
                                "message_note": message_fallback(other_lane, other["lane"], "merge_message")})
        if len(entries) > 1:
            members = [e["task"] for e in entries]
            mc = {**mc, "members": members, "leader": tid, "batch_commits": entries, "phase": "starting"}
            desk.update(tid, merge_check=mc)  # publish membership before anything that can fail to launch
            for member in members[1:]:
                omc = desk.meta(member)["merge_check"]
                desk.update(member, merge_check={**omc, "phase": "batched", "leader": tid,
                                                 "members": members, "lane": lane_name})
            commit = entries[-1]["commit"]
        if not command:  # the check and merge check were taken away meanwhile: nothing to run
            land_merge(desk, tid, {"ok": True, "onto": tip, "commit": commit, "command": "", "note": "",
                                   "at": now(), "seconds": 0, "tail": [], "message_note": said,
                                   **({"previous": mc["retried"]} if mc.get("retried") else {}),
                                   **merge_result_lane(m, lane_name)})
            return None
        pname = m.get("provider", "")
        p = desk.settings()["providers"].get(pname) or {}
        srt, settings, tmp, own = in_srt(desk, tid, "check", lane, pname, p, wt)
        argv = ["/bin/sh", "-c", CHECK_SCRIPT, token, in_sandbox(desk, lane, command), str(raw / "merge-check.exit"), tmp, srt,
                settings, own]
        if lane.get("setup"):
            ssrt, ssettings, stmp, sown = in_srt(desk, tid, "setup", lane, pname, p, wt)
            argv = ["/bin/sh", "-c", SETUP_SCRIPT, token, in_sandbox(desk, lane, lane["setup"]), str(raw / "merge-setup.log"),
                    str(raw / "merge-check.err"), expected_git_line(desk, wt) or "(unknown, so no git runs here)",
                    stmp, ssrt, ssettings, sown, *argv]
        env = {**base_env(), "HANSA_TASK": tid, "HANSA_RUN": run_mark(desk, tid), "TERM": "dumb", "TMPDIR": tmp,
               "CLAUDE_CODE_TMPDIR": tmp}
        memory = oom_begin(mc)
        mc = {**mc, "phase": "starting", "launch": token, "onto": tip, "commit": commit, "command": command,
              "own": own_merge_check(lane, task_lane), "message_note": said,
              "setup": lane.get("setup", ""), "started": now(), "worktree": str(wt),
              **memory}
        desk.update(tid, merge_check=mc)
        with open(raw / "merge-check.log", "wb") as out:
            proc = subprocess.Popen(with_oom_score(argv, str(wt), env), cwd=str(wt), env=env, stdin=subprocess.DEVNULL, stdout=out,
                                    stderr=subprocess.STDOUT, start_new_session=True)
        with recording_start(desk, ("merge", tid, token), proc.pid, proc):
            desk.update(tid, merge_check={**mc, "phase": "running", "pid": proc.pid, "pid_start": proc_start(proc.pid)},
                        **({"merge_check_result": {**mc["retried"], "rerun_started": True}} if mc.get("retried") else {}))
    desk.event("merge_check_started", task=tid, lane=lane_name, onto=tip, command=command,
               members=mc.get("members", [tid]))
    return proc


def merge_not_started(desk: Desk, tid: str, e: Exception) -> None:
    key = ("merge", tid, (desk.meta(tid).get("merge_check") or {}).get("launch"))
    remove_sandbox_tmp(desk, tid)
    end_merge_check(desk, tid, {"ok": False, "at": now(),
                              "note": desk.redact(f"the merge check could not run: {e}")[:600]})
    desk.failed_starts.pop(key, None)


def finish_merge_check(desk: Desk, tid: str) -> dict | None:
    """After a merge check's process ended: merge, check again, or send the task back for review."""
    m = desk.meta(tid)
    mc = m.get("merge_check") or {}
    if m["state"] != "merging" or mc.get("phase") not in ("starting", "running"):
        return None
    raw, d = desk.state_dir / "runs" / tid, desk.tasks_dir / tid
    memory = oom_result(mc.get("oom_start"))
    try:
        code = int((raw / "merge-check.exit").read_text().strip())
    except (OSError, ValueError):
        code = None
    (raw / "merge-setup.log.done").unlink(missing_ok=True)
    remove_sandbox_tmp(desk, tid)
    publish(desk, tid, *MERGE_FILES)  # what the check printed can hold a key the change holds
    sweep(desk, tid, run=mc)  # the merge check's lane may differ, and the task's lane may now run another task

    def read(name):
        return (d / name).read_text(encoding="utf-8", errors="replace") if (d / name).exists() else ""
    err = read("merge-check.err").strip()
    setup = SETUP_FAILED.search(err)
    note = ("stopped on request" if mc.get("cancel") or mc.get("batch_cancel") else "stopped at the lane's time ceiling" if mc.get("timed_out")
            else f"the lane's setup failed (exit {setup.group(1)}), so the check did not run" if setup
            else "" if code is not None else (err.splitlines()[-1] if err else "the check was interrupted"))
    changed = False
    if mc.get("worktree") and expected_git_line(desk, Path(mc["worktree"])):
        try:
            check_git_file(desk, Path(mc["worktree"]))
        except GitFileChanged as e:
            note = git_file_changed(desk, tid, e)
            changed = True
    result = {"ok": code == 0 and not note, "exit": code, "note": note, "command": mc.get("command", ""),
              **memory,
              **({"retried": mc["retried"]} if mc.get("retried") else {}),
              **merge_result_lane(m, mc.get("lane")),
              **({"own": True} if mc.get("own") else {}),
              "onto": mc.get("onto", ""), "commit": mc.get("commit", ""), "at": now(),
              "seconds": round(now() - (mc.get("started") or now())),
              "tail": read("merge-setup.log" if setup else "merge-check.log").strip().splitlines()[-15:]}
    if len(mc.get("members", [])) > 1:
        result.update(batch=mc["members"], output_task=tid, batch_commits=mc["batch_commits"],
                      checked_commit=mc.get("commit"))
        desk.event("merge_checked", task=tid, ok=result["ok"], exit=code, note=note, command=result["command"],
                   members=mc["members"],
                   **({"recheck": True, "by": mc.get("by", "")} if mc.get("recheck") else {}))
        if result["ok"]:
            try:
                return land_merge_batch(desk, tid, result)
            finally:
                ended = desk.meta(tid)
                if ended["state"] != "merging" or (ended.get("merge_check") or {}).get("phase") == "queued":
                    (raw / "merge-check.exit").unlink(missing_ok=True)
        (raw / "merge-check.exit").unlink(missing_ok=True)
        return split_merge_batch(desk, tid, result)
    stopped = mc.get("cancel") or mc.get("timed_out") or changed or setup
    if check_killed(result, stopped):
        if not result.get("retried"):
            result["note"] = check_retry_reason(result)
            def retry(x):
                current = x.get("merge_check") or {}
                if x["state"] != "merging" or current.get("cancel") or current.get("timed_out"):
                    return None
                return {"merge_check": {**current, "phase": "queued", "pid": None, "pid_start": "",
                                        "launch": "", "started": None, "retried": result},
                        "merge_check_result": result}
            if desk.change(tid, retry):
                (raw / "merge-check.exit").unlink(missing_ok=True)
                desk.event("merge_check_killed", task=tid, exit=code, **memory)
                return desk.meta(tid)
            mc = desk.meta(tid).get("merge_check") or {}
            result["note"] = "stopped on request" if mc.get("cancel") else "stopped at the lane's time ceiling"
        elif result["retried"].get("oom_kills"):
            result["note"] = killed_twice(result["retried"], result)
    note = result["note"]
    desk.event("merge_checked", task=tid, ok=result["ok"], exit=code, note=note, command=result["command"],
               **({"recheck": True, "by": mc.get("by", "")} if mc.get("recheck") else {}))
    try:
        if mc.get("cancel"):
            return end_merge_check(desk, tid, None)
        if not result["ok"]:
            return end_merge_check(desk, tid, result)
        return land_merge(desk, tid, result)
    finally:
        ended = desk.meta(tid)
        if ended["state"] != "merging" or (ended.get("merge_check") or {}).get("phase") not in ("starting", "running"):
            (raw / "merge-check.exit").unlink(missing_ok=True)


def merge_landing_problem(desk: Desk, m: dict, repo, name: str, onto: str, commit: str) -> str:
    """Each task's own landing checks, against only what its merge adds."""
    why = branch_holds(repo, m)
    current = git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo)
    if why:
        return why
    if current != name:
        return f"the checkout at {repo} is on {current}, not {name}; the principal needs to switch it back"
    if git("status", "--porcelain", "--untracked-files=no", cwd=repo):
        return f"the checkout at {repo} has uncommitted changes"
    leaked = keys_in_change(desk, repo, m["base"], m["head"])
    words = [] if leaked else words_in_merge(desk, repo, onto, commit, lane_name=m["lane"])
    if leaked:
        desk.update(m["id"], leaked=leaked)
        return f"the change holds the key of {', '.join(leaked)}"
    if words:
        desk.event("merge_forbidden", task=m["id"], found=len(words))
        return desk.redact(words_text(m["id"], words))
    return ""


def record_batch_landing(desk: Desk, saved: dict, journal: Path) -> dict:
    """The base already holds the checked commit. Finish metadata after an interrupted landing too."""
    result, tid = saved["result"], saved["leader"]
    for entry in result["batch_commits"]:
        member = entry["task"]
        m = desk.meta(member)
        mc = m.get("merge_check") or {}
        if m["state"] == "merged" and m.get("merge_commit") == entry["commit"]:
            continue
        own = {**result, "onto": entry["onto"], "commit": entry["commit"]}
        m = desk.update(member, state="merged", merged=now(), merge_commit=entry["commit"], merge_check=None,
                        merge_repo=saved["repo"], merge_base=saved["base"], merge_check_result=own,
                        unverified=unverified_note(desk, m) or None, merge_message_note=entry["message_note"] or None)
        close_verification(desk, m, f"closed: {member} was merged")
        desk.event("task_merged", ts=m["merged"], task=member, into=saved["base"], merge_checked=True, onto=entry["onto"],
                   command=result["command"], members=result["batch"], check_task=tid,
                   **({"recheck": True, "by": mc.get("by", "")} if mc.get("recheck") else {}))
    journal.unlink(missing_ok=True)
    return desk.meta(tid)


def land_merge_batch(desk: Desk, tid: str, result: dict) -> dict:
    """Check every member before advancing the base once. Journal the passed batch before the git move."""
    with desk.lock():
        s = desk.settings()
        members = result["batch"]
        if any((desk.meta(x).get("merge_check") or {}).get("cancel") for x in members):
            return split_merge_batch(desk, tid, {**result, "ok": False, "note": "stopped on request"})
        lane = s["lanes"].get(desk.meta(tid).get("lane"))
        if not lane:
            return split_merge_batch(desk, tid, {**result, "ok": False, "note": "the task lane was removed"})
        repo, name = lane["repo"], lane.get("base") or "main"
        tip = git("rev-parse", name, cwd=repo)
        if tip != result["onto"]:
            desk.event("merge_check_base_moved", task=tid, onto=result["onto"], now=tip, members=members)
            return split_merge_batch(desk, tid, {**result, "note": "the base moved before landing"})
        failures = {}
        for entry in result["batch_commits"]:
            m = desk.meta(entry["task"])
            if m.get("lane") not in s["lanes"]:
                why = f"lane {m.get('lane')} is no longer in settings"
            else:
                why = merge_landing_problem(desk, m, repo, name, entry["onto"], entry["commit"])
            if why:
                failures[m["id"]] = {**result, "ok": False, "onto": entry["onto"], "commit": entry["commit"],
                                       "note": f"its merge passed the check, but {why}; nothing was merged"}
        if failures:
            split_merge_batch(desk, tid, result)
            for member, failure in failures.items():
                end_merge_check(desk, member, failure)
            return desk.meta(tid)
        saved = {"leader": tid, "repo": str(Path(repo).resolve()), "base": name, "result": result}
        journal = desk.state_dir / "merge-batches" / f"{tid}.json"
        journal.parent.mkdir(parents=True, exist_ok=True)
        write_json(journal, saved)
        try:
            git("merge", "--ff-only", "-q", result["commit"], cwd=repo, env={"HANSA_MERGING": "1"})
        except RuntimeError as e:
            journal.unlink(missing_ok=True)
            return split_merge_batch(desk, tid, {**result, "ok": False,
                                               "note": f"moving {name} to the checked merge failed ({str(e)[:200]})"})
        return record_batch_landing(desk, saved, journal)


def recover_merge_batches(desk: Desk, tasks: list) -> None:
    """A durable landing proves exactly which commit passed, including after git moved but metadata did not."""
    with desk.lock():
        for journal in sorted((desk.state_dir / "merge-batches").glob("*.json")):
            saved = read_json(journal)
            result = saved["result"]
            tip = git("rev-parse", saved["base"], cwd=saved["repo"])
            if not base_moved(saved["repo"], result["checked_commit"], tip):
                record_batch_landing(desk, saved, journal)
            else:
                split_merge_batch(desk, saved["leader"], {**result, "note": "the server stopped before landing"})
                journal.unlink(missing_ok=True)
        for m in tasks:
            mc = m.get("merge_check") or {}
            if m["state"] != "merging" or mc.get("phase") != "batched":
                continue
            m = desk.meta(m["id"])  # a journal above may just have finished this member
            mc = m.get("merge_check") or {}
            if m["state"] != "merging" or mc.get("phase") != "batched":
                continue
            leader = desk.meta(mc["leader"])
            lmc = leader.get("merge_check") or {}
            if leader["state"] != "merging" or m["id"] not in lmc.get("members", []):
                desk.update(m["id"], merge_check={"phase": "queued", "queued": mc.get("queued"),
                                                 "alone": True, "moves": 0})
                if mc.get("cancel"):
                    end_merge_check(desk, m["id"], None)


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
                                          **({"alone": True} if mc.get("alone") else {}),
                                          "passed_onto": result["onto"],
                                          **({"retried": result["retried"]} if result.get("retried") else {}),
                                          **({"recheck": True, "by": mc.get("by", "")} if mc.get("recheck") else {})})
            return desk.meta(tid)
        why = merge_landing_problem(desk, m, repo, name, tip, result["commit"])
        if not why:
            try:
                git("merge", "--ff-only", "-q", result["commit"], cwd=repo, env={"HANSA_MERGING": "1"})
            except RuntimeError as e:
                why = f"moving {name} to the checked merge failed ({str(e)[:200]})"
        if why:
            return end_merge_check(desk, tid, {**result, "ok": False, "note": f"its merge passed the check, but {why}; "
                                                                              "nothing was merged"})
        m = desk.update(tid, state="merged", merged=now(), merge_commit=result["commit"], merge_check=None,
                        merge_repo=str(Path(repo).resolve()), merge_base=name,
                        merge_check_result=result, unverified=unverified_note(desk, m) or None,
                        merge_message_note=result.get("message_note") or mc.get("message_note") or None)
        close_verification(desk, m, f"closed: {tid} was merged")
    desk.event("task_merged", ts=m["merged"], task=tid, into=name, merge_checked=True, onto=result["onto"],
               **({"command": result["command"]} if result.get("command") else {}),
               **({"recheck": True, "by": mc.get("by", "")} if mc.get("recheck") else {}))
    return m


# A try: `hansa lane try L` runs the lane's setup and check once, with no worker, to see that they work where
# tasks run; `hansa lane try L --merge-check` runs its setup and merge check command (merge_command) instead,
# under the merge check's time limit (try {merge: True}). hansa serve runs it (with the server's environment), when the lane is free, in the lane's worktree
# detached at the base's tip, and puts the worktree back clean at that tip after. .hansa/tries/<lane>.json holds
# the try: {id, phase: queued | running | done, ...}, and when done its result, which the command waits for.
TRY_STEPS = ("setup", "check", "merge_check")
TRY_KEEP = 4 * 2 ** 20  # bytes of each step's output kept, its first and last half (try_log)


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
    return next((m for m in desk.tasks() if occupied_lane(m) == lane_name and lane_occupied(m)), None)


def end_try(desk: Desk, lane_name: str, result: dict) -> dict:
    t = {**read_try(desk, lane_name), "phase": "done", "ended": now(), **result}  # at: the commit --at asked for
    for k in ("pid", "pid_start", "tmp", "launch"):
        t.pop(k, None)
    write_try(desk, lane_name, t)
    desk.event("lane_tried", lane=lane_name, ok=t.get("ok", False), note=t.get("note", ""), onto=t.get("onto", ""),
               **{s: {"exit": x.get("exit"), "seconds": x.get("seconds")} for s, x in t.get("steps", {}).items()})
    return t


@git_folders_kept
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
        if t.get("at") and t["at"] != tip:  # asked with --at: it answers for that commit only
            shutil.rmtree(d, ignore_errors=True)
            end_try(desk, lane_name, {"ok": False, "moved": True, "steps": {}, "onto": tip,
                                      "note": f"{name}'s tip moved from {t['at'][:10]} to {tip[:10]} before the try "
                                              f"started; nothing was run"})
            return None
        ready_worktree(desk, repo, wt, tid, tip)
        try:
            wgit(desk, wt, "checkout", "-q", "--detach", tip)
        except GitFileChanged as e:
            raise RuntimeError(git_file_changed(desk, tid, e)) from None
        s = desk.settings()
        pname = (lane.get("providers") or [""])[0]  # the sandbox of the lane's first provider, as a task's
        p = s["providers"].get(pname) or {}
        token, args, tmps = f"hansa-try-{lane_name}-{uuid.uuid4().hex[:8]}", [], []
        steps = {"setup": lane.get("setup", ""),
                 **({"merge_check": merge_command(lane)} if t.get("merge") else {"check": lane.get("check", "")})}
        for step, command in steps.items():
            if command:
                tmp, own = run_tmp(desk, step)
                tmps += dict.fromkeys([tmp, own])
                write_json(d / f"{step}-sandbox.json", srt_settings(desk, lane, pname, p, wt, tmp, own=own))
                args += [step, in_sandbox(desk, lane, command), str(d / f"{step}-sandbox.json"), tmp, own]
        env = {**base_env(), "TERM": "dumb"}
        try:  # srt first, with the first step's settings (as in_srt): when it cannot run, nothing runs
            srt = try_srt(desk, args[2], args[3], wt) if args else srt_bin()
        except SrtCannotRun as e:
            for x in tmps:
                shutil.rmtree(x, ignore_errors=True)
            shutil.rmtree(d, ignore_errors=True)
            end_try(desk, lane_name, {"ok": False, "could_not_run": True, "steps": {}, "onto": tip,
                                      "note": desk.redact(f"could not run: {e}")[:600]})
            return None
        memory = oom_begin(t)
        t = {**t, "phase": "running", "launch": token, "onto": tip, "worktree": str(wt), "started": now(), **memory,
             "tmp": tmps, **steps}
        write_try(desk, lane_name, t)
        with open(d / "try.err", "wb") as err:
            proc = subprocess.Popen(with_oom_score(["/bin/sh", "-c", TRY_SCRIPT, token, str(d), srt, *args], str(wt), env), cwd=str(wt), env=env,
                                    stdin=subprocess.DEVNULL, stdout=err, stderr=subprocess.STDOUT,
                                    start_new_session=True)
        with recording_start(desk, ("try", lane_name, token), proc.pid, proc):
            write_try(desk, lane_name, {**t, "pid": proc.pid, "pid_start": proc_start(proc.pid)})
    desk.event("lane_try_started", lane=lane_name, onto=tip)
    return proc


def try_not_started(desk: Desk, lane_name: str, e: Exception) -> None:
    t = read_try(desk, lane_name)
    for x in t.get("tmp", []):
        shutil.rmtree(x, ignore_errors=True)
    end_try(desk, lane_name, {"ok": False, "steps": {}, "note": desk.redact(f"the try could not run: {e}")[:600]})
    desk.failed_starts.pop(("try", lane_name, t.get("launch")), None)


def finish_try(desk: Desk, lane_name: str) -> dict:
    """After a try's process ended: read what each step did, keys removed, and put the worktree back clean at
    the base's tip it ran on."""
    t = read_try(desk, lane_name)
    memory = oom_result(t.get("oom_start"))
    d = desk.state_dir / "tries" / lane_name
    steps, failed, whole = {}, "", []
    for step in TRY_STEPS:
        if not t.get(step):
            continue
        try:
            code, secs = map(int, (d / f"{step}.exit").read_text().split())
        except (OSError, ValueError):
            code = secs = None
        f = d / f"{step}.log"
        log = desk.redact(read_bounded(f, TRY_KEEP // 2, TRY_KEEP // 2)).encode("utf-8", errors="replace").decode(
            "utf-8") if f.exists() else ""  # a byte that is not UTF-8 is one "?"
        ran = code is not None or f.exists()
        steps[step] = {"command": t[step], "ran": ran, "exit": code, "seconds": secs,
                       "tail": log.strip().splitlines()[-15:]}
        if ran and code != 0:
            steps[step]["hints"] = output_hints(log)
        if ran:
            whole.append((step, steps[step], log))
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
    ok = bool(steps) and all(x["exit"] == 0 for x in steps.values()) and not note
    log = keep_try_log(desk, lane_name, t, whole)
    try:
        return end_try(desk, lane_name, {"ok": ok, "steps": steps, "note": note, "clean": clean, **memory,
                                         **({"log": str(log)} if log else {})})
    finally:
        if read_try(desk, lane_name).get("phase") == "done":
            shutil.rmtree(d, ignore_errors=True)


def try_log(desk: Desk, lane_name: str, merge: bool = False) -> Path:
    """Where the whole output of the lane's last try is kept (of its last --merge-check try: merge)."""
    return desk.lanes_dir / lane_name / ("merge-check-try.log" if merge else "try.log")


def keep_try_log(desk: Desk, lane_name: str, t: dict, whole: list):
    """Write each step's output (read_bounded: its first and last TRY_KEEP / 2 bytes, keys removed) in place of
    the last try's; its path, or None when no step ran or it could not be written."""
    if not whole:
        return None
    out = [f"hansa lane try {lane_name}{' --merge-check' if t.get('merge') else ''} at {(t.get('onto') or '')[:12]}, "
           f"started {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(t.get('started') or time.time()))}; "
           f"each step's whole output, keys removed\n"]
    for step, x, log in whole:
        how = "interrupted" if x["exit"] is None else f"exit {x['exit']}"
        out.append(f"\n===== {step.replace('_', ' ')}: {x['command']} ({how}"
                   + (f" in {x['seconds']}s" if x["seconds"] is not None else "") + ") =====\n")
        out.append(log if not log or log.endswith("\n") else log + "\n")
    f = try_log(desk, lane_name, bool(t.get("merge")))
    with desk.lock():  # a key saved meanwhile is either known here or scrubbed from it after (Desk.scrub)
        data = desk.redact("".join(out)).encode("utf-8", errors="replace")
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            write_bytes(f, cut_redacted(data, TRY_KEEP * len(whole) + 4096))
        except OSError:
            return None
    return f


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
    """Whether this process is still running, and, when its start is known, still the same one. Only a
    lookup that says there is no such process (ESRCH) counts as gone: one denied for another reason
    (EPERM, EACCES, EIO) leaves it as it is, unless its start reads differently from the one saved."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:  # no such process
        return False
    except OSError:  # denied, not ended: only its start can say it is another process
        pass
    return same_start(pid, start)


def same_start(pid, start: str) -> bool:
    """Whether a process still has the start saved for it; True when either cannot be read."""
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
    and so its process group's, cannot pass to another process. A lookup denied for a reason other
    than that there is no such process does not say that it ended."""
    if proc.returncode is not None:
        return True
    try:
        os.kill(proc.pid, 0)
    except ProcessLookupError:  # gone altogether
        proc.poll()
        return True
    except OSError:  # denied, not ended
        return False
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


@contextlib.contextmanager
def recording_start(desk: Desk, key, pid, proc=None):
    """A launched process may run only if its pid can be recorded. Keep a failed start for later
    when even that record cannot be written; the key includes its launch, so a later run is left alone."""
    try:
        yield
    except Exception as e:
        if key is not None:
            desk.failed_starts[key] = e  # mark BEFORE stopping, even when no disk write is possible
        stop(pid, proc=proc)
        if proc is None:
            try:
                os.waitpid(pid, os.WNOHANG)  # stop already waited; normally another server's child
            except ChildProcessError:
                pass
        raise


def retry_start_failure(desk: Desk, key, record) -> bool:
    """Retry only the failed-start record, never its stopped process."""
    error = desk.failed_starts.get(key)
    if error is None:
        return False
    record(error)
    desk.failed_starts.pop(key, None)
    return True


SERVE_ERRORS = contextvars.ContextVar("serve_errors", default=None)


@contextlib.contextmanager
def serve_step():
    """A write failure holds back only this operation; serve reports it after the rest of the round.
    Outside a serve round, callers still get the error immediately."""
    try:
        yield
    except OSError as e:
        errors = SERVE_ERRORS.get()
        if errors is None:
            raise
        errors.append(e)


def save_stopped(pending: dict, key, run: dict, save, fields=None) -> None:
    """Keep a stop this server made until its mark can be saved, only for this launch. No inference after
    a restart: these are in memory, and an already ended process never creates a mark."""
    identity = (run.get("launch"), run.get("started"))
    if fields is not None:
        kept = pending.get(key)
        if kept and kept[0] == identity:
            fields = {**kept[1], **fields}
        pending[key] = (identity, fields)
    kept = pending.get(key)
    if kept is None:
        return
    if kept[0] == identity:
        save(kept[1])
    pending.pop(key, None)


def run_running(run: dict, proc=None) -> bool:
    """An end awaiting its record is still finished, even if the round is now past its limit."""
    return not has_ended(proc) if proc is not None else alive(run.get("pid"), run.get("pid_start", ""))


class Dispatcher:
    """Starts queued tasks while lanes are free, and finishes the ones that ended."""

    def __init__(self, desk: Desk) -> None:
        self.desk = desk
        self.procs = {}
        self.merge_procs = {}  # merge checks this server started
        self.try_procs = {}  # tries (hansa lane try) this server started, by lane
        self.release_procs = {}  # releases (hansa release) this server started, by id
        self.release_watch_procs = {}  # CI watches, outside the repository's merge and release queue
        self.pending_stops = {}  # stop marks whose writes failed, by kind/id and launch
        self.resting = None  # the providers resting at the last round; None before the first
        self.probes = {}  # at most one background check per unreachable provider
        self.held = {}  # (lane, group) whose runs' sandbox could not start: the desk's settings then (sandbox_waits)
        self.sandbox_seen = set()  # runs (by session) with a Bash call that succeeded: nothing more to look for
        self.retried = False  # whether the held lanes' sandbox was tried again as this server started (sandbox_retry)
        self.ends_recovered = False  # saved task ends whose event an earlier server did not get to write
        self.swept = 0.0  # when it last swept hansa's leftover empty folders in the temporary folder (sweep_tmp)
        self.trimmed = 0.0  # when this server last trimmed what old tasks kept (disk.trim)
        self.look_error = None  # the error of its look of free space while that fails as a whole (disk.look_failed)
        self.memory_error = None  # a failed available-memory reading, recorded once per change
        self.read = None  # (desk.writes, the tasks) as this round last read them; None at the start of a round

    def tasks(self) -> list:
        """The desk's tasks, read once a round and again only after this process changed one (Desk.writes): a
        change writes what it read again under the lock (Desk.change), so a list read earlier in the round cannot
        make it wrong."""
        if self.read is None or self.read[0] != self.desk.writes:
            self.read = (self.desk.writes, self.desk.tasks())
        return self.read[1]

    def recover_ends(self) -> None:
        """Repair the save/event gap once at startup. Only the hour before the log's newest event (and later)
        needs recovery; read its tail once, rather than the whole history for every ended task. The saved end's
        time keeps alerts and the page from treating a restart as the time the task ended."""
        desk = self.desk
        latest, newest = {}, 0
        try:
            source = open(desk.events_path, "rb")
        except FileNotFoundError:
            source = None
        if source:
            with source:
                pos, pending, past = source.seek(0, os.SEEK_END), b"", False
                while pos and not past:
                    size = min(pos, 65536)
                    pos -= size
                    source.seek(pos)
                    lines = (source.read(size) + pending).split(b"\n")
                    pending = lines.pop(0) if pos else b""
                    for line in reversed(lines):
                        e = event_record(line)
                        if not e or not isinstance(e.get("ts"), (int, float)) or not math.isfinite(e["ts"]):
                            continue
                        newest = max(newest, e["ts"])
                        if e["ts"] < newest - 3600:
                            past = True
                            break
                        if e.get("event") == "task_finished" and e.get("state") in ("queued", "checking"):
                            continue  # a run handed to another try or its check has not ended the task
                        key = (e.get("task"), e.get("event"))
                        latest[key] = max(latest.get(key, 0), e["ts"])

        def missing(m, kind, at, **fields):
            if at is None or at < newest - 3600:
                return
            kinds = ("task_finished", "task_checked", "task_not_started") \
                if kind in ("task_finished", "task_checked", "task_not_started") else (kind,)
            if kind == "task_finished" and m["state"] == "cancelled":
                kinds += ("task_cancel",)  # a queued cancel already recorded this end without task_finished
            recorded = max(latest.get((m["id"], k), -1) for k in kinds)
            # Older merges saved an unrounded clock value; their events used millisecond precision.
            # Keep accepting those until no such saved end is in the recovery window.
            if recorded < (round(at, 3) if kind == "task_merged" else at):
                desk.event(kind, ts=at, task=m["id"], **fields)

        for m in self.tasks():
            if m["state"] in ("done", "failed", "cancelled"):
                check = m.get("check") or {}
                if check:
                    missing(m, "task_checked", m.get("finished"),
                            **{k: check.get(k) for k in ("ok", "exit", "note")},
                            **({"could_not_run": True} if check.get("could_not_run") else {}))
                elif (m.get("error") or "").startswith("could not start:"):
                    missing(m, "task_not_started", m.get("finished"), error=m["error"])
                else:
                    missing(m, "task_finished", m.get("finished"), state=m["state"],
                            provider=m.get("provider"), error=m.get("error", ""))
            result = m.get("merge_check_result") or {}
            if m["state"] == "done" and not m.get("merge_check") and result:
                missing(m, "merge_check_ended", result.get("at"), ok=False, note=result.get("note", "cancelled"),
                        **({"waited": True} if result.get("waited") else {}),
                        **({"command": result["command"]} if result.get("command") else {}))
            elif m["state"] == "merged" and result:
                missing(m, "task_merged", m.get("merged"), into=m.get("merge_base"), merge_checked=True,
                        onto=result.get("onto", ""),
                        **({"command": result["command"]} if result.get("command") else {}),
                        **({"members": result["batch"], "check_task": result.get("output_task")}
                           if result.get("batch") else {}))
        self.ends_recovered = True

    def sandbox_waits(self, ln: str, s: dict) -> bool:
        """Whether a lane's tasks wait for their sandbox: held by this server (hold)
        with the settings as they are. Changed settings, or a restart, try it again: the task starts once a try
        passes."""
        return self.held.get((ln, "work")) == json.dumps(s, sort_keys=True, default=str)

    def hold(self, tid: str, ln: str, s: dict, e: SandboxRefused) -> None:
        hold(self.desk, tid, ln, str(e), e.part, e.kind)
        self.held[(ln, "work")] = json.dumps(s, sort_keys=True, default=str)

    def sandbox_watch(self, s: dict) -> None:
        """Stop a run whose session record says its sandbox could not start before any of its Bash calls succeeded
        (sandbox_start_failure): Claude Code's own copy of the sandbox can refuse what srt's try let through. Its
        task is held when it ends (finish), without using a try."""
        for m in self.tasks():
            with serve_step():
                sid = m.get("session_id") or ""
                if m["state"] != "running" or m.get("phase") or not m.get("pid") or m.get("sandbox_failed") \
                        or not sid or sid in self.sandbox_seen or m.get("cancel"):
                    continue
                p = s["providers"].get(m.get("provider") or "")
                if not p:
                    continue
                line, settled = sandbox_start_failure(config_home(self.desk, m["provider"], p), sid)
                if settled:
                    self.sandbox_seen.add(sid)
                if not line:
                    continue
                self.desk.update(m["id"], sandbox_failed=self.desk.redact(line))
                self.held[(m.get("lane"), "work")] = json.dumps(s, sort_keys=True, default=str)
                self.desk.event("sandbox_failed", task=m["id"], lane=m.get("lane"), line=self.desk.redact(line))
                stop(m.get("pid"), m.get("pid_start", ""), self.procs.get(m["id"]))

    def ended(self, tid: str, returncode) -> None:
        save_stopped(self.pending_stops, ("task", tid), self.desk.meta(tid),
                     lambda fields: self.desk.update(tid, **fields))
        if self.desk.meta(tid).get("phase") == "checking":
            finish_check(self.desk, tid)
        else:
            try:
                proc = resume(self.desk, tid)
            except Exception as e:  # noqa: BLE001 - the run ends as it did, and the event log says why
                self.desk.event("resume_failed", task=tid, error=self.desk.redact(f"{type(e).__name__}: {e}")[:300])
                proc = None
            if proc:
                self.procs[tid] = proc
                return
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
        hansa cannot look for that worker, the task stays as it is, and fails after LOOK_LIMIT; its
        lane then waits until its process is known to be gone (lanes_waiting)."""
        key = ("task", m["id"], m.get("launch"))
        if retry_start_failure(self.desk, key, lambda e: not_started(self.desk, m["id"], e)):
            return
        found, since = self.look(m["id"], m.get("launch", ""), m.get("started"), m.get("lookup_failing_since"),
                                  "worker")
        if since:
            why = self.gave_up(since)
            if why:
                at = now()
                fields = {"state": "failed", "phase": "", "error": why, "finished": at,
                          "lookup_failing_since": None, "lane_wait_since": at}
            else:
                fields = {"lookup_failing_since": since}
            if self.desk.change(m["id"], lambda x: None if x["state"] != "running" or x.get("phase") != "starting"
                                else fields) and why:
                remove_sandbox_tmp(self.desk, m["id"])
            return
        if found:
            try:
                with recording_start(self.desk, key, found):
                    self.desk.update(m["id"], phase="", pid=found, pid_start=proc_start(found), lookup_failing_since=None)
            except Exception as e:
                not_started(self.desk, m["id"], e)
            return
        lost = keep_lost_run(self.desk, m)  # read before the lock: a session record can be long
        if self.desk.change(m["id"], lambda x: None if x["state"] != "running" or x.get("phase") != "starting" else
                            {"state": "cancelled", "phase": "", "error": "cancelled before it started",
                             "finished": now(), **lost} if x.get("cancel") else
                            {"state": "queued", "phase": "", "pid": None, "pid_start": "",
                             "lookup_failing_since": None, **lost}):
            remove_sandbox_tmp(self.desk, m["id"])  # what it made for a worker that never started

    def lanes_waiting(self) -> None:
        """A task that failed while hansa could not look for its worker keeps its lane: free it once a
        look that works finds no process of that run (the empty list says the word is on no process)."""
        for m in self.tasks():
            with serve_step():
                if not m.get("lane_wait_since"):
                    continue
                try:
                    found = leader(pids_with_arg(m.get("launch", ""), m.get("started")))
                except CannotLook:
                    continue  # still no look that works: the lane waits
                if found is not None:
                    continue  # the worker's process is there still: not known to be gone
                self.desk.change(m["id"], lambda x: {"lane_wait_since": None} if x.get("lane_wait_since") else None)

    def check(self, m: dict, s: dict) -> None:
        """Run the lane's check on a task whose worker is done: or take over the one an earlier
        server started, or finish the one it left finished."""
        desk, tid = self.desk, m["id"]
        ended = {"worktree": m.get("worktree"), "started": m.get("check_started")}
        key = ("check", tid, m.get("launch"))
        if retry_start_failure(desk, key, lambda e: check_not_started(desk, tid, e)):
            return
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
            try:
                with recording_start(desk, key, found):
                    desk.update(tid, pid=found, pid_start=proc_start(found))
            except Exception as e:
                check_not_started(desk, tid, e)
            return
        if ((desk.state_dir / "runs" / tid / "check.exit").exists()
                and not (m.get("check_retry") and m.get("check_started") is None)):
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
            sweep(desk, tid, run=ended)
            return
        except Exception as e:  # noqa: BLE001 - say: the check cannot run, so the task fails
            check_not_started(desk, tid, e)
            return
        if proc:
            self.procs[tid] = proc
            return
        publish(desk, tid, "check.log")  # cancelled first, or the principal removed the check meanwhile
        desk.change(tid, lambda x: {"state": "cancelled", "phase": "", "error": "cancelled", "finished": now()}
                    if x.get("cancel") else {"state": "done", "phase": ""})
        sweep(desk, tid, run=ended)

    def merge_checks(self, s: dict, starting: bool = True) -> None:
        """Finish merge checks that ended, stop those cancelled or over the lane's time, take over or finish
        those an earlier server left, and start queued ones on free lanes (also while dispatch is paused:
        they use no provider; neither they nor a merge without a check while starting is False, with free space
        under the floor)."""
        desk = self.desk
        recover_merge_batches(desk, self.tasks())
        for tid, proc in list(self.merge_procs.items()):
            with serve_step():
                if has_ended(proc):
                    stop(proc.pid, "", proc)
                    if desk.meta(tid)["state"] != "merging":
                        del self.merge_procs[tid]
                        continue
                    mc = self.desk.meta(tid).get("merge_check") or {}
                    save_stopped(self.pending_stops, ("merge", tid), mc,
                                 lambda fields: desk.update(tid, merge_check={**mc, **fields}))
                    finish_merge_check(desk, tid)
                    del self.merge_procs[tid]
        for m in self.tasks():
            with serve_step():
                mc = m.get("merge_check") or {}
                if m["state"] != "merging" or m["id"] in self.merge_procs or mc.get("phase") not in ("starting", "running"):
                    continue
                key = ("merge", m["id"], mc.get("launch"))
                if retry_start_failure(desk, key, lambda e: merge_not_started(desk, m["id"], e)):
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
                    try:
                        with recording_start(desk, key, found):
                            desk.update(m["id"], merge_check={**mc, "phase": "running", "pid": found, "pid_start": proc_start(found),
                                                              "lookup_failing_since": None})
                    except Exception as e:
                        merge_not_started(desk, m["id"], e)
                elif not found and not alive(mc.get("pid"), mc.get("pid_start", "")):
                    if mc.get("members") and mc.get("phase") == "starting":
                        split_merge_batch(desk, m["id"], {"ok": False, "note": "the server could not finish the batch"})
                    elif mc.get("phase") == "starting":  # it never started: once more from the start
                        desk.change(m["id"], lambda x: {"merge_check": {**x["merge_check"], "phase": "queued",
                                                                        "lookup_failing_since": None}}
                                    if x["state"] == "merging" else None)
                    else:
                        save_stopped(self.pending_stops, ("merge", m["id"]), mc,
                                     lambda fields: desk.update(m["id"], merge_check={**mc, **fields}))
                        finish_merge_check(desk, m["id"])
        for m in self.tasks():
            with serve_step():
                mc = m.get("merge_check") or {}
                if m["state"] != "merging" or mc.get("phase") != "running":
                    continue
                limit = 60 * merge_minutes(s["lanes"].get(occupied_lane(m), {}))
                if not mc.get("cancel") and time.time() - (mc.get("started") or time.time()) > limit:
                    proc = self.merge_procs.get(m["id"])
                    if not run_running(mc, proc):
                        continue
                    try:
                        save_stopped(self.pending_stops, ("merge", m["id"]), mc,
                                     lambda fields: desk.update(m["id"], merge_check={**mc, **fields}), {"timed_out": True})
                    finally:
                        stop(mc.get("pid"), mc.get("pid_start", ""), proc)
                elif (mc.get("cancel") or mc.get("batch_cancel")) and run_running(mc, self.merge_procs.get(m["id"])):
                    stop(mc.get("pid"), mc.get("pid_start", ""), self.merge_procs.get(m["id"]))
        if not starting:
            return
        from .release import holds_repo, releases
        tasks = self.tasks()
        busy = {occupied_lane(m) for m in tasks if lane_occupied(m)} | tried_lanes(desk)
        free = s["concurrency"] - sum(map(lane_occupied, tasks))
        # one merge check at a time per repository and base branch, in the order the merges were asked: one
        # that ran beside another would be checked on a base that the other's merge then moves
        held = {merge_target(s, m.get("lane")) for m in tasks
                if m["state"] == "merging" and (m.get("merge_check") or {}).get("phase") in ("starting", "running")}
        # and none, nor a merge, where a release runs: it sends out the commit the principal saw (one that runs
        # from a fixed checkout of that commit holds nothing)
        releasing = {r.get("repo") for r in releases(desk) if holds_repo(r)}
        for m in sorted(tasks, key=merge_order):
            with serve_step():
                m = desk.meta(m["id"])  # an earlier start may have taken this task into its batch
                if m["state"] != "merging" or (m.get("merge_check") or {}).get("phase") != "queued":
                    continue
                ln = m.get("lane")
                if ln not in s["lanes"]:
                    end_merge_check(desk, m["id"], {"ok": False, "at": now(), "note": f"lane {ln} is no longer in settings"})
                    continue
                at = merge_target(s, ln)
                if at in held or at[0] in releasing:
                    continue
                if (m.get("merge_check") or {}).get("plain"):  # its turn: it needs neither the lane nor a slot
                    try:
                        if plain_merge(desk, m["id"], s["lanes"][ln]) != "check":
                            continue
                    except Exception as e:  # noqa: BLE001 - say: nothing is merged, and the task says why
                        end_merge_check(desk, m["id"], {"ok": False, "at": now(),
                                                        "note": desk.redact(f"the merge could not be made: {e}")[:600]})
                        continue
                    m = desk.meta(m["id"])  # a merge check now, first in line
                ln = merge_check_lane(s, m)
                if ln in busy or free <= 0:
                    held.add(at)  # first in line: the ones after it wait for it
                    continue
                try:
                    proc = start_merge_check(desk, m["id"], ln, s["lanes"][ln])
                except SrtCannotRun as e:  # the machine's problem: nothing is merged, and nothing ran
                    remove_sandbox_tmp(desk, m["id"])
                    end_merge_check(desk, m["id"], {"ok": False, "at": now(), "could_not_run": True,
                                                    "command": merge_command(s["lanes"][ln]), "tail": [],
                                                    "note": desk.redact(f"could not run: {e}")[:600]})
                    continue
                except Exception as e:  # noqa: BLE001 - say: nothing is merged, and the task says why
                    merge_not_started(desk, m["id"], e)
                    continue
                if proc:
                    self.merge_procs[m["id"]] = proc
                    busy.add(ln)
                    held.add(at)
                    free -= 1
                elif (desk.meta(m["id"]).get("merge_check") or {}).get("phase") == "queued":
                    held.add(at)  # merged at once onto a base that had moved: checked again, still first in line

    def tries(self, s: dict, starting: bool = True) -> None:
        """Finish tries that ended, stop those over the lane's time, finish those an earlier server left, and
        start queued ones (also while dispatch is paused: they use no provider; not while starting is False, with
        free space under the floor)."""
        desk = self.desk
        for ln, proc in list(self.try_procs.items()):
            with serve_step():
                if has_ended(proc):
                    stop(proc.pid, "", proc)
                    t = read_try(desk, ln)
                    if t.get("phase") != "running":  # its end was saved before its event failed
                        del self.try_procs[ln]
                        continue
                    save_stopped(self.pending_stops, ("try", ln), t, lambda fields: write_try(desk, ln, {**t, **fields}))
                    finish_try(desk, ln)
                    del self.try_procs[ln]
        d = desk.state_dir / "tries"
        for ln in sorted(f.stem for f in d.glob("*.json")) if d.is_dir() else []:
            with serve_step():
                t = read_try(desk, ln)
                if retry_start_failure(desk, ("try", ln, t.get("launch")), lambda e: try_not_started(desk, ln, e)):
                    continue
                if t.get("phase") == "running" and ln not in self.try_procs:
                    if not alive(t.get("pid"), t.get("pid_start", "")):  # started by an earlier server
                        save_stopped(self.pending_stops, ("try", ln), t, lambda fields: write_try(desk, ln, {**t, **fields}))
                        finish_try(desk, ln)
                        continue
                if t.get("phase") == "running":
                    ln_s = s["lanes"].get(ln, {})
                    limit = merge_minutes(ln_s) if t.get("merge") else ln_s.get("timeout_min", 240)
                    if t.get("timed_out") or time.time() - (t.get("started") or time.time()) > 60 * limit:
                        proc = self.try_procs.get(ln)
                        if not run_running(t, proc):
                            continue
                        try:
                            if not t.get("timed_out"):
                                save_stopped(self.pending_stops, ("try", ln), t,
                                             lambda fields: write_try(desk, ln, {**t, **fields}), {"timed_out": True})
                        finally:
                            stop(t.get("pid"), t.get("pid_start", ""), proc)
                if t.get("phase") != "queued" or not starting:
                    continue
                if ln not in s["lanes"]:
                    end_try(desk, ln, {"ok": False, "steps": {}, "note": f"lane {ln} is no longer in settings"})
                    continue
                try:
                    proc = start_try(desk, ln, s["lanes"][ln])
                except Exception as e:  # noqa: BLE001 - srt missing, say: the try says why it did not run
                    try_not_started(desk, ln, e)
                    continue
                if proc:
                    self.try_procs[ln] = proc

    def rests(self, s: dict) -> None:
        """A provider whose rest ran out is ready again with no save or check to say so: tell the open requests
        that name it, as those do (desk.ready_news), under the same once-only rule, so a rest that starts is
        looked at too. Also once as the server starts, for rests that ran out while no server ran."""
        st = self.desk.provider_state()
        resting = {n for n in s["providers"] if (st.get(n) or {}).get("cool_until", 0) > time.time()
                   or (st.get(n) or {}).get("credit_since") or (st.get(n) or {}).get("unreachable_since")}
        if resting != self.resting:
            self.desk.ready_news()
        self.resting = resting

    def probe_providers(self, s: dict) -> None:
        """Check unreachable providers without holding up a round, even for the check's full timeout.
        The last start is kept with provider state so restarting serve does not shorten the interval."""
        for name in s["providers"]:
            worker = self.probes.get(name)
            if worker and worker.is_alive():
                continue
            with self.desk.lock():
                st = self.desk.provider_state().get(name) or {}
                if st.get("credit_since") or not st.get("unreachable_since") or st.get("cool_until", 0) > time.time() or time.time() - max(
                        st["unreachable_since"], st.get("probe_at", 0)) < 60 * PROBE_MINUTES:
                    continue
                self.desk.set_provider_state(name, probe_at=time.time())

            def probe(name=name):
                try:
                    check_provider(self.desk, name)
                except Exception as e:  # noqa: BLE001 - failed writes must not stop future checks or serve
                    with serve_step():
                        self.desk.event("provider_probe_failed", provider=name,
                                        error=self.desk.redact(f"{type(e).__name__}: {e}")[:300])

            worker = threading.Thread(target=probe, name=f"hansa-probe-{name}", daemon=True)
            self.probes[name] = worker
            worker.start()

    def disk(self, s: dict) -> list:
        """The volumes under the floor of free space (disk.look), for which nothing new starts this round; and,
        once a day, the trimming of what old tasks kept (disk.trim). A place that cannot be looked at is left out
        (disk.measure); a look that fails as a whole holds nothing back, and is recorded once, not each round."""
        from . import disk
        if time.time() - self.trimmed >= disk.TRIM_EVERY:
            self.trimmed = time.time()
            try:
                disk.trim(self.desk)
            except Exception as e:  # noqa: BLE001 - keep serving; the error is on record
                self.desk.event("trim_failed", error=self.desk.redact(f"{type(e).__name__}: {e}")[:300])
        try:
            rec = disk.look(self.desk, s)
        except Exception as e:  # noqa: BLE001 - keep serving; the error is on record, once (disk.look_failed)
            error = self.desk.redact(f"{type(e).__name__}: {e}")[:300]
            if error != self.look_error:
                self.look_error = error
                try:
                    disk.look_failed(self.desk, error)
                except Exception:  # noqa: BLE001 - its record cannot be written either
                    pass
            return []
        self.look_error = None
        return rec["low"]

    def memory(self, s: dict) -> bool:
        """The independent memory hold; a failed reading holds nothing back."""
        from . import memory
        try:
            rec = memory.look(self.desk, s)
        except Exception as e:  # noqa: BLE001 - keep serving, as with a failed disk look
            error = self.desk.redact(f"{type(e).__name__}: {e}")[:300]
            if error != self.memory_error:
                self.memory_error = error
                try:
                    memory.look_failed(self.desk, error)
                except Exception:  # noqa: BLE001 - its record cannot be written either
                    pass
            return False
        self.memory_error = None
        return rec["low"]

    def record(self, tasks: list) -> None:
        """Record what each running worker, verifier and check runs below it (record_run), once
        each round: what it starts that leaves every marker sweep finds is still stopped at its end."""
        running = [m for m in tasks if m["state"] == "running" and m.get("pid") and m.get("phase") != "starting"]
        table = process_table() if running else {}
        for m in running:
            with serve_step():
                record_run(self.desk, m, table)

    def tick(self) -> None:
        errors = []
        token = SERVE_ERRORS.set(errors)
        try:
            self._tick()
        finally:
            SERVE_ERRORS.reset(token)
        if errors:
            raise errors[0]

    def _tick(self) -> None:
        desk = self.desk
        self.read = None  # what another process changed since the last round is read again
        s = desk.settings()
        with serve_step():
            desk.state_dir.mkdir(exist_ok=True)
            beat(desk)
        with serve_step():
            if not self.ends_recovered:
                self.recover_ends()
        with serve_step():
            self.rests(s)
        with serve_step():
            self.probe_providers(s)
        with serve_step():
            if not self.retried:  # a hold an earlier server made, perhaps on earlier code: tried again, once
                self.retried = True
                for key in sandbox_retry(desk, s):
                    self.held[key] = json.dumps(s, sort_keys=True, default=str)
                beat(desk)
        with serve_step():
            if time.time() - self.swept >= TMP_SWEEP_AGE:
                self.swept = time.time()
                sweep_tmp()
        low = []
        with serve_step():
            low = self.disk(s)
        memory_low = False
        with serve_step():
            memory_low = self.memory(s)
        low = bool(low or memory_low or desks_list_problem())  # an unreadable list of desks holds new runs too
        with serve_step():
            self.record(self.tasks())
        for tid, proc in list(self.procs.items()):
            with serve_step():
                if has_ended(proc):
                    if desk.meta(tid).get("pid") != proc.pid:  # its end was saved before a later write failed
                        del self.procs[tid]
                        continue
                    stop(proc.pid, "", proc)  # what it left running in its group (a server, say) ends with it
                    self.ended(tid, proc.returncode)
                    if self.procs.get(tid) is proc:  # a resume replaced it with its own process
                        del self.procs[tid]
                    beat(desk)
        for m in self.tasks():
            with serve_step():
                if m["state"] != "running" or m["id"] in self.procs:
                    continue
                if m.get("phase") == "starting":  # an earlier server stopped while it prepared this task
                    self.recover(m)
                elif m.get("phase") == "checking" and not m.get("pid"):
                    self.check(m, s)
                elif not alive(m.get("pid"), m.get("pid_start", "")):  # started by an earlier server
                    self.ended(m["id"], None)
        with serve_step():
            self.lanes_waiting()
        for m in self.tasks():
            with serve_step():
                if verification_due(m):
                    self.verify_after(m["id"])
        for m in self.tasks():
            with serve_step():
                if m["state"] != "running":
                    continue
                limit = 60 * s["lanes"].get(m.get("lane"), {}).get("timeout_min", 240)
                began = ((m.get("check_started") or time.time()) if m.get("phase") == "checking" and m.get("check_retry")
                         else m.get("started", time.time()))
                over = time.time() - began > limit
                if m.get("cancel") or over:
                    proc = self.procs.get(m["id"])
                    if not run_running(m, proc):
                        continue  # an end whose record failed, or one just before this observation
                    try:
                        if not m.get("cancel"):
                            save_stopped(self.pending_stops, ("task", m["id"]), m,
                                         lambda fields: desk.update(m["id"], **fields), {"timed_out": True})
                    finally:
                        stop(m.get("pid"), m.get("pid_start", ""), proc)
        with serve_step():
            self.sandbox_watch(s)
        with serve_step():
            self.tries(s, starting=not low)
        from .release import tick as releases
        with serve_step():  # before merge checks: a yes waits for none only queued
            releases(desk, s, self.release_procs, starting=not low, tasks=self.tasks, pending=self.pending_stops,
                     watch_procs=self.release_watch_procs)
        with serve_step():
            self.merge_checks(s, starting=not low)
        with serve_step():
            beat(desk)  # what came before can take long: stopping a worker waits for its group, finishing sweeps
        if desk.paused() or low:  # disk or memory under its floor: existing work goes on
            return
        running = [m for m in self.tasks() if lane_occupied(m)]
        free = s["concurrency"] - len(running)
        busy = {occupied_lane(m) for m in running} | tried_lanes(desk)
        for m in self.tasks():
            with serve_step():
                if free <= 0:
                    break
                if m["state"] != "queued":
                    continue
                for ln in ([m["lane"]] if m.get("lane") else sorted(s["lanes"])):
                    if (ln in busy or ln not in s["lanes"] or work_lane_problem(s, ln,
                            verifying=m.get("kind") == "verify" and bool(m.get("lane"))) or self.sandbox_waits(ln, s)):
                        continue
                    pname = next((p for p in task_providers(s, ln, m)
                                  if p not in refused_by(m) and not provider_problem(desk, s, p)), None)
                    if not pname:
                        continue
                    try:
                        proc = start(desk, m["id"], ln, pname)
                    except SandboxRefused as e:  # held, not failed: it starts once a try passes
                        self.hold(m["id"], ln, s, e)
                    except Exception as e:  # noqa: BLE001 - any failure to start is recorded on the task
                        not_started(desk, m["id"], e)
                    else:
                        if proc:
                            self.procs[m["id"]] = proc
                            busy.add(ln)
                            free -= 1
                    break

    def verify_after(self, tid: str) -> None:
        """Queue once from the current metadata, before the pause gate. Commands share this lock."""
        from .cli import queue_verification  # cli imports run; never import it at module load
        desk = self.desk
        with desk.lock():
            m = desk.meta(tid)
            if not verification_due(m):
                return
            # A server may have stopped after creating the child but before recording the pointer.
            children = [t for t in desk.tasks() if t.get("kind") == "verify" and t.get("parent") == tid]
            if children:
                desk.update(tid, verify=children[-1]["id"])
                return
            try:
                queue_verification(desk, tid, **m["verify_after"], session=m.get("session"))
            except (SystemExit, Exception) as e:  # a refusal ends this request, never the completed task
                # A failed write after creating the child must not queue it twice on the next tick.
                children = [t for t in desk.tasks() if t.get("kind") == "verify" and t.get("parent") == tid]
                if children:
                    desk.update(tid, verify=children[-1]["id"])
                    return
                why = desk.redact(str(e) or type(e).__name__)
                desk.update(tid, verify_after_error=why)
                desk.event("task_finished", task=tid, state="done", verification_error=why)


ROUND_QUIET = 30  # seconds without a finished round after which hansa serve counts as busy
STUCK_AFTER = 300  # seconds without a finished round after which it is said to be busy or stuck


def server_holder(desk: Desk) -> tuple:
    """The hansa serve that serve.lock names (pid, start); (None, '') when it names none."""
    try:
        words = (desk.state_dir / "serve.lock").read_text().split()
        return int(words[0]), " ".join(words[1:])
    except (OSError, ValueError, IndexError):
        return None, ""


def lock_held(desk: Desk):
    """Whether a hansa serve holds serve.lock, asked of the lock itself (a non-blocking shared try): whatever
    process its number now names, only a live server holds it. None when the lock cannot be asked."""
    try:
        f = open(desk.state_dir / "serve.lock", "r")
    except FileNotFoundError:
        return False
    except OSError:
        return None
    with f:  # closing it lets the try go at once
        try:
            fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError:
            return None
        return False


def _exists(pid, start: str) -> bool:
    """Whether the process serve.lock names still runs, by alive()'s rule: a process of another account (not ours
    to signal) counts as there while its start, where it can be read (/proc on Linux, for any process), is the one
    serve.lock names, and a lookup that fails for another reason is not its end."""
    return alive(pid, start)


def _server_clock():
    """Seconds awake since boot, shared across processes (unlike monotonic on macOS Python < 3.12)."""
    if sys.platform == "darwin":
        name = "CLOCK_UPTIME_RAW"
    elif sys.platform.startswith("linux"):
        name = "CLOCK_MONOTONIC"
    else:
        return None
    clock = getattr(time, name, None)
    if clock is not None:
        try:
            return time.clock_gettime(clock)
        except (AttributeError, OSError, ValueError):
            pass  # unavailable: write and read wall time only
    return None


def beat(desk: Desk) -> None:
    """hansa serve got through a round, or a step of one: the heartbeat server_state reads."""
    desk.state_dir.mkdir(exist_ok=True)
    record = {"at": now()}
    clock = _server_clock()
    if clock is not None:
        record["monotonic"] = clock
    write_json(desk.state_dir / "heartbeat", record)  # replaced whole


def pulse(desk: Desk, every: float = 5.0) -> None:
    """hansa serve's process answers: written from a thread while it runs, apart from the rounds, so that a
    server whose rounds stopped coming is still told from one whose process stopped (server_state)."""
    import threading

    def run():
        while True:
            try:
                record = {"pid": os.getpid(), "at": time.time()}
                clock = _server_clock()
                if clock is not None:
                    record["monotonic"] = clock
                write_json(desk.state_dir / "pulse", record)
            except OSError:
                pass
            time.sleep(every)
    threading.Thread(target=run, name="hansa-pulse", daemon=True).start()


def last_round(desk: Desk):
    """When the hansa serve that holds serve.lock last finished a round, by its heartbeat; when it started (the
    time serve.lock was written) where the heartbeat is missing, unreadable or older than this server, which
    it then says nothing about. None when neither is there."""
    return _last_round(desk)[0]


def _last_round(desk: Desk) -> tuple:
    """The last round's wall and monotonic times; a start from serve.lock has only a wall time."""
    try:
        start = (desk.state_dir / "serve.lock").stat().st_mtime  # written once, as the server takes the lock
    except OSError:
        start = None
    hb = read_json(desk.state_dir / "heartbeat")
    mono = hb.get("monotonic") if isinstance(hb, dict) else None
    hb = hb.get("at") if isinstance(hb, dict) else hb  # older servers wrote a bare number
    if isinstance(hb, bool) or not isinstance(hb, (int, float)) or hb <= 0:
        hb = None
    if hb is None or start is not None and hb < start:
        return start, None
    return hb, mono


def _server_elapsed(at, mono=None) -> float:
    """Elapsed time without sleep; old records and monotonic times from before a reboot use wall time."""
    current = _server_clock()
    if current is not None and not isinstance(mono, bool) and isinstance(mono, (int, float)) and 0 <= mono <= current:
        return current - mono
    return time.time() - (at or 0)


def server_state(desk: Desk) -> dict:
    """One rule for whether hansa serve runs. gone: no hansa serve holds serve.lock (lock_held), whatever
    process the number in it names now; where the lock cannot be asked, the process it names is gone or is
    another one started since, or it names none. up: alive, and it finished a round in the last ROUND_QUIET seconds. busy: alive, quiet for
    longer (a round waiting on a worker to stop, say). stuck: alive, quiet for STUCK_AFTER seconds or more.
    quiet: seconds since its last round, excluding sleep where its heartbeat records monotonic time;
    answers: whether its process still wrote its pulse lately (a
    process stopped with Ctrl-Z, say, does not)."""
    pid, start = server_holder(desk)
    last, mono = _last_round(desk)
    quiet = max(0, int(_server_elapsed(last, mono))) if last else 0  # none: no lock either, so gone below
    held = lock_held(desk)
    if held is None:  # the lock cannot be asked: the process it names, and when that one started
        held = bool(pid) and _exists(pid, start) and (bool(start) or quiet < ROUND_QUIET)  # a number alone:
        # written by a server before the start was, so its rounds say whether it runs, as they did then
    if not held:
        return {"state": "gone", "pid": None, "quiet": None, "answers": False}
    pulse = read_json(desk.state_dir / "pulse", {}) or {}
    answers = pulse.get("pid") == pid and _server_elapsed(pulse.get("at"), pulse.get("monotonic")) < ROUND_QUIET
    return {"state": "up" if quiet < ROUND_QUIET else "busy" if quiet < STUCK_AFTER else "stuck",
            "pid": pid, "quiet": quiet, "answers": answers}


def record_serve_code(desk: Desk) -> None:
    """hansa serve, as it starts: which code it runs and when that was installed, as serve_code reads it."""
    write_json(desk.state_dir / "serve-code", {"pid": os.getpid(), "build": build(), "installed": installed()})


LOCALE_ENV = ("LANG", "LC_ALL")  # the locale variables of BASE_ENV: what of hansa's own locale its runs get


def is_locale_var(k: str) -> bool:
    return k in LOCALE_ENV or k.startswith("LC_")


def record_serve_locale(desk: Desk) -> None:
    """hansa serve, as it starts: the locale its lanes' commands and workers get from it (base_env), which
    hansa explain lane reads from here rather than from the shell it is run in."""
    write_json(desk.state_dir / "serve-locale", {"pid": os.getpid(),
                                                 "env": {k: os.environ[k] for k in LOCALE_ENV if k in os.environ}})


def lane_locale(desk: Desk, lane: dict, st: dict = None) -> str:
    """The locale a lane's setup, check, merge check and workers get, and where it comes from, in a
    line: the lane's env when it sets one, else hansa serve's environment (as it recorded it when it started),
    else, when serve does not run, this shell's (which a serve started here would give them). hansa passes on
    only LANG and LC_ALL of its own; a lane's env may set any LC_ variable, and LC_ALL, when set and not
    empty, wins over the rest (an empty one counts as not set)."""
    own = {k: v for k, v in lane_env(desk, lane)[0].items() if is_locale_var(k)}
    emptied = {k for k, v in own.items() if not v}  # set empty by the lane: as if not set
    own = {k: v for k, v in own.items() if v}
    st = st or server_state(desk)
    if st["state"] != "gone":
        rec = read_json(desk.state_dir / "serve-locale", {})
        if isinstance(rec, dict) and rec.get("pid") == st["pid"] and isinstance(rec.get("env"), dict):
            outer, whose = {k: v for k, v in rec["env"].items() if k in LOCALE_ENV and isinstance(v, str)}, \
                "hansa serve's environment"
        else:
            outer, whose = None, "hansa serve's environment (it did not record it: restart it to see it here)"
    else:
        outer, whose = ({k: os.environ[k] for k in LOCALE_ENV if k in os.environ},
                        "this shell's environment (hansa serve does not run, and nothing runs without it: the"
                        " commands will take the locale of the environment hansa serve is started from, this"
                        " shell's if started here)")
    if "LC_ALL" in own:
        return f"LC_ALL={own['LC_ALL']}, from the lane's env"
    got = {k: v for k, v in (outer or {}).items() if k not in emptied and v}
    got.update(own)
    if "LC_ALL" in got and own:
        got = {"LC_ALL": got["LC_ALL"]}  # it wins over what the lane sets
        own = {}
    said = ", ".join(f"{k}={v}" for k, v in sorted(got.items()))
    if own and outer:
        return (f"{said}: {', '.join(sorted(own))} from the lane's env, "
                f"{', '.join(sorted(k for k in got if k not in own)) or 'nothing'} from {whose}")
    if own:
        return f"{said}, from the lane's env" + ("" if outer is not None else f" (and whatever {whose} adds)")
    if outer is None:
        return f"whatever is in {whose}"
    return f"{said or 'none set (the C locale)'}, from {whose}"


def record_serve_session(desk: Desk) -> list:
    """hansa serve, as it starts: the Claude Code session's variables it was started with (hansa/__init__.py), which
    it takes out of its environment, so nothing it starts gets them; recorded for hansa status and hansa explain
    server. Returns their names: none when it was started from a plain shell."""
    names = session_vars()
    for k in names:
        os.environ.pop(k, None)
    write_json(desk.state_dir / "serve-session", {"pid": os.getpid(), "dropped": names})
    return names


def serve_session_text(names) -> str:
    return (f"hansa serve was started inside a Claude Code session; it took that session's {', '.join(names)} out of "
            "its environment, so nothing it starts gets them. Better start it from a plain shell (a terminal or tmux "
            "of your own, not one Claude Code opened)")


def serve_session(desk: Desk, st: dict = None) -> str:
    """'' unless the hansa serve that runs was started inside a Claude Code session; then that, in a line."""
    st = st or server_state(desk)
    if st["state"] == "gone":
        return ""
    rec = read_json(desk.state_dir / "serve-session", {})
    if not isinstance(rec, dict) or rec.get("pid") != st["pid"] or not isinstance(rec.get("dropped"), list):
        return ""
    names = [k for k in rec["dropped"] if isinstance(k, str)]
    return serve_session_text(names) if names else ""


def serve_code(desk: Desk, st: dict = None) -> str:
    """'' unless hansa serve runs other code than is installed now (installed since, but not restarted); then
    what to do about it, in a line. A server that did not record its code (started on a hansa before this
    record) is told by when it started: before the code now installed was."""
    st = st or server_state(desk)
    if st["state"] == "gone":
        return ""
    now_build, now_at = build(), installed()
    rec = read_json(desk.state_dir / "serve-code", {})
    if isinstance(rec, dict) and rec.get("pid") == st["pid"] and isinstance(rec.get("build"), str):
        at = rec.get("installed") if isinstance(rec.get("installed"), (int, float)) else 0
        if (rec["build"], at) == (now_build, now_at):
            return ""
        runs = f"{rec['build']} ({installed_text(at)})" if at else rec["build"]
        return f"hansa serve runs {runs}, but {now_build} was {installed_text(now_at)}: restart it"
    try:
        start = (desk.state_dir / "serve.lock").stat().st_mtime  # written once, as the server takes the lock
    except OSError:
        return ""
    if not now_at or now_at <= start:
        return ""
    since = time.strftime("%Y-%m-%d %H:%M", time.localtime(start))
    return f"hansa serve started {since}, before {now_build} was {installed_text(now_at)}: restart it"


def server_up(desk: Desk) -> bool:
    """Whether hansa serve runs: its process is alive, busy or not (server_state)."""
    return server_state(desk)["state"] != "gone"


def quiet_text(seconds: int) -> str:
    mins = seconds // 60
    return f"{mins} minute{'s' if mins != 1 else ''}" if mins else f"{seconds} seconds"


def not_looking(desk: Desk, st: dict | None = None) -> str:
    """Why hansa serve will not look at the desk within seconds, in a clause; '' when it will (up or busy).
    st: server_state, when the caller has it already."""
    st = server_state(desk) if st is None else st
    return ("`hansa serve` is not running" if st["state"] == "gone" else
            f"`hansa serve` is busy or stuck: no round finished for {quiet_text(st['quiet'])}"
            if st["state"] == "stuck" else "")


def server_text(st: dict) -> str:
    """What server_state says, in words, for hansa status and hansa explain server."""
    if st["state"] == "gone":
        return "server NOT running: start it with `hansa up` (see hansa explain server)"
    if st["state"] == "up":
        return "server up"
    quiet = quiet_text(st["quiet"])
    return (f"server up (pid {st['pid']}) but busy: no round finished for {quiet}" if st["state"] == "busy" else
            f"hansa serve (pid {st['pid']}) has not finished a round for {quiet}: busy or stuck"
            + ("" if st["answers"] else "; its process does not answer either (stopped with Ctrl-Z?)")
            + " · next: hansa explain server")


def disk_short(desk: Desk) -> str:
    """Why new work waits for disk space or memory; disk takes precedence if both hold."""
    from . import disk, memory
    low = disk.held(desk)
    if low:
        return disk.short_text(desk, low)
    return memory.short_text(desk) if memory.held(desk) else ""


def waiting_looks(desk: Desk, s: dict, tasks: list | None = None, server: dict | None = None) -> dict:
    """What waiting_reason reads once for all queued tasks in a status response."""
    from . import disk, memory
    server = server_state(desk) if server is None else server
    low = disk.held(desk)
    resource = None
    if low:
        fl = disk.floor(s)
        resource = {"code": "disk", "values": {"volumes": [
            {"mount": v.get("mount"), "free": disk.gb(v.get("free") or 0),
             "floor": disk.floor_text(disk.vol_floor(fl, v)), "again": disk.gb(disk.vol_bar(fl, v))} for v in low]}}
    elif memory.held(desk):
        rec = memory.record(desk)
        fl = memory.machine_floor(memory.floor(s), rec)
        resource = {"code": "memory", "values": {"available": disk.gb(rec["available"]),
                    "floor": disk.floor_text(fl), "again": disk.gb((fl + memory.MARGIN) * disk.GB)}}
    tasks = desk.tasks() if tasks is None else tasks
    return {"tasks": tasks, "paused": desk.paused(),
            "disk": disk.short_text(desk, low) if low else memory.short_text(desk) if resource else "",
            "resource": resource, "server": server, "held": sandbox_held(desk),
            "lane_wait": {occupied_lane(x): x for x in tasks if x.get("lane_wait_since")},
            "not_looking": not_looking(desk, server), "desks": desks_list_problem(),
            "problems": {p: provider_problem(desk, s, p) for ln in s["lanes"].values() for p in ln["providers"]},
            "provider_state": desk.provider_state()}


def waiting_detail(desk: Desk, s: dict, m: dict, busy: set, looks: dict | None = None) -> dict:
    """The unchanged command sentence alongside codes and values for the page's dictionaries.
    Nested reasons carry their own English fallback, so newer codes remain readable by older pages."""
    lk = looks or waiting_looks(desk, s)

    def reason(code, text, **values):
        return {"code": code, "values": values, "text": text}

    def result(code, text, **values):
        return {"waiting": text, "waiting_code": code, "waiting_values": values}

    if lk["paused"]:
        return result("paused", "dispatch is paused")
    if lk.get("desks"):
        return result("desks_list", lk["desks"])
    if lk["disk"]:
        resource = lk["resource"]
        return result(resource["code"], lk["disk"], **resource["values"])
    lanes = [m["lane"]] if m.get("lane") else sorted(n for n in s["lanes"] if not work_lane_problem(s, n))
    if not lanes:
        return (result("no_work_lanes", "no lanes that take work in settings") if s["lanes"] else
                result("no_lanes", "no lanes in settings"))
    why = []
    held = lk["held"]
    for ln in lanes:
        if ln not in s["lanes"]:
            why.append(reason("missing_lane", f"lane {ln} is not in settings", lane=ln))
            continue
        problem = work_lane_problem(s, ln, verifying=m.get("kind") == "verify" and bool(m.get("lane")))
        if problem:
            why.append(reason("merge_lane" if s["lanes"][ln].get("merge_lane") else "verify_lane", problem, lane=ln))
            continue
        if "work" in held.get(ln, {}):
            h = held[ln]["work"]
            why.append(reason("sandbox", f"HELD: lane {ln} cannot run, {sandbox_held_text(h)}",
                              lane=ln, kind=h.get("kind") or "run", why=h.get("why") or "?"))
            continue
        if ln in busy:
            waiter = (lk.get("lane_wait") or {}).get(ln)
            why.append(reason("busy", f"lane {ln} is busy" + (
                f": {waiter['id']}'s worker's process is not known to be gone (hansa could not look for it "
                f"for {LOOK_LIMIT // 60} minutes)" if waiter else ""), lane=ln))
            continue
        probs = {p: "refused this task" if p in refused_by(m) else lk["problems"][p]
                 for p in task_providers(s, ln, m)}
        ready = [p for p, prob in probs.items() if not prob]
        if ready:
            why_not = lk["not_looking"]
            text = (f"ready for lane {ln} on {ready[0]}, but {why_not}" if why_not else
                    f"ready for lane {ln} on {ready[0]}; starts when `hansa serve` next looks (within seconds)")
            return result("ready", text, lane=ln, provider=ready[0], server=lk["server"]["state"],
                          seconds=lk["server"].get("quiet"))
        providers = []
        for p, problem in probs.items():
            st = lk["provider_state"].get(p, {})
            code = ({"refused this task": "refused", "not in settings": "missing_provider",
                     "no endpoint address": "no_endpoint", "no key": "no_key"}.get(problem)
                    or ("credit" if st.get("credit_since") else "rest" if st.get("cool_until", 0) > time.time()
                        else "unreachable" if st.get("unreachable_since") else "unknown"))
            providers.append(reason(code, problem, provider=p, since=st.get("credit_since") or st.get("unreachable_since"),
                                    until=st.get("cool_until"), minutes=st.get("rest_minutes"),
                                    how=st.get("rest_how"),
                                    why=(st.get("unreachable_reason") if code == "unreachable" else st.get("reason")) or ""))
        why.append(reason("providers", f"lane {ln} has no provider ready ({'; '.join(f'{p}: {x}' for p, x in probs.items())})",
                          lane=ln, providers=providers))
    return (result("lanes", "; ".join(r["text"] for r in why), reasons=why) if why else
            result("slot", "waiting for a free slot"))


def waiting_reason(desk: Desk, s: dict, m: dict, busy: set, looks: dict | None = None) -> str:
    """Why a queued task is not running yet, in one line, for the command views."""
    return waiting_detail(desk, s, m, busy, looks)["waiting"]


def key_stamp(desk: Desk, name: str) -> tuple:
    """Which key file is there now (saving a key always makes a new file); () if none."""
    try:
        st = desk.key_file(name).stat()
    except (OSError, ValueError):
        return ()
    return st.st_ino, st.st_mtime_ns


def check_provider(desk: Desk, name: str, timeout: int = 90, claude: str = "") -> dict:
    """Send one tiny request through a provider and record whether it answered, through `claude` when it is given
    (the program a recorded hansa serve start runs), else through this process's."""
    s = desk.settings()
    p = s["providers"].get(name)
    if not p:
        return {"ok": False, "detail": "not in settings"}
    prob = provider_problem(desk, s, name)
    if prob in ("no key", "no endpoint address"):
        return {"ok": False, "detail": prob}
    tmp = desk.state_dir / "check"
    tmp.mkdir(parents=True, exist_ok=True)
    # no tools: a model behind a compatible endpoint often calls one before it answers, and one turn ends there
    argv = [claude or claude_bin(), "-p", "--output-format", "json", "--max-turns", "1", "--model", p["model"], "--tools", ""]
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
        kind, why = ("", "") if ok else unusable_reason(run, r.stderr)
        # the model answered with a tool call and the one turn ran out: it is reachable all the same, unless
        # hansa's own readings (an API error status, a usage limit, a refused key) found something first
        stopped = not ok and not kind and not run.get("api_error_status") and \
            (run.get("subtype"), run.get("stop_reason"), run.get("terminal_reason")) == \
            ("error_max_turns", "tool_use", "max_turns") and bool(run.get("modelUsage")) and not run.get("result")
        if ok or stopped:
            ok, detail = True, ", ".join(run.get("modelUsage") or {})
            detail += ": answered, but stopped at the turn limit" if stopped else ""
        else:
            said =re.search(r'"message"\s*:\s*"([^"]{1,120})"', f"{run.get('result') or ''} {r.stderr}")
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
        if ok and not (desk.provider_state().get(name) or {}).get("credit_since"):
            desk.set_provider_state(name, cool_until=0, reason="", unreachable_since=0, unreachable_reason="")
        elif kind:
            desk.set_provider_state(name, unreachable_since=0, unreachable_reason="")
            rest_provider(desk, s, name, kind, detail, f"{run.get('result') or ''}\n{r.stderr}")
    desk.event("provider_checked", provider=name, ok=ok, detail=detail)
    desk.ready_news()
    return result
