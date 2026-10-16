#!/bin/sh
# One real Codex run through the shim, on a scratch repository, started as hansa starts a worker: the worker's
# command line (with the sandbox hansa would give it in --settings), a clean environment with the placeholder
# base URL, the task on stdin. Prints the answer hansa would read, then the scratch worktree's state: hello.txt
# made, and no commit (the sandbox denies writing git's data; hansa commits, never the worker), and none of the
# project settings Codex was asked to make (sub/.claude among them, by mkdir, by renaming a folder into place and
# as a link). It is also the check of the -c config keys --help does not show:
# the report naming task dry-run shows developer_instructions arrived; for model_reasoning_effort and
# check_for_update_on_startup, look in the JSONL log and codex's stderr (<logs>/<session-id>.jsonl, .err).
#
# Needs, in codex.env beside this script (or in the environment):
#   HANSA_CODEX_BIN   a codex outside ~/.codex (hansa's sandbox denies reading ~/.codex, srt too)
#   HANSA_CODEX_HOME  its CODEX_HOME, logged in once with:  CODEX_HOME=<it> <codex> login
# and srt on the PATH (or HANSA_CODEX_SRT). hansa's checkout is the one this script is in (contrib/codex), unless
# HANSA_SRC names another.
set -eu
here=$(cd "$(dirname "$0")" && pwd -P)
hansa_src=${HANSA_SRC:-$(cd "$here/../.." && pwd -P)}
scratch=$(mktemp -d "${TMPDIR:-/tmp}/codex-dry-run.XXXXXX")
tmp=$(mktemp -d "${TMPDIR:-/tmp}/hansa-dry-run.XXXXXX")  # the worker's temporary folder, as hansa makes one
g() { git -c user.name=dry-run -c user.email=dry-run@localhost "$@"; }
g init -q -b main "$scratch/repo"
echo "# scratch" > "$scratch/repo/README.md"
g -C "$scratch/repo" add README.md
g -C "$scratch/repo" commit -q -m init
g -C "$scratch/repo" worktree add -q "$scratch/wt" -b task/dry-run

cd "$scratch/wt"
python3 - "$here" "$tmp" "$hansa_src" <<'EOF'
import json, os, subprocess, sys, uuid
from pathlib import Path
here, tmp, hansa_src = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, hansa_src)
from hansa import run as hansa_run
from hansa.desk import home_secrets

wt, home = Path.cwd(), Path.home()
box = {"sandbox": {  # hansa's sandbox_settings for a lane with no network list and no write list
    "enabled": True, "failIfUnavailable": True, "autoAllowBashIfSandboxed": True, "allowUnsandboxedCommands": False,
    "filesystem": {"denyRead": hansa_run.full_paths([home / ".claude", *(home / f for f in home_secrets())]),
                   "denyWrite": hansa_run.git_write_deny(wt)},
    "network": {"allowedDomains": [], "strictAllowlist": True},
    "credentials": {"envVars": [{"name": v, "mode": "deny"} for v in hansa_run.KEY_VARS]}}}
system = hansa_run.PROTOCOL.format(tid="dry-run", lane="codex", tmp=tmp, long="")
argv = [os.path.join(here, "bin", "claude-or-codex"), "-p", "--output-format", "json",
        "--session-id", str(uuid.uuid4()), "--permission-mode", "acceptEdits", "--max-turns", "30",
        "--model", "codex", "--append-system-prompt", system, *hansa_run.isolated(wt, box)]
env = {k: os.environ[k] for k in hansa_run.BASE_ENV if k in os.environ}
env.update({k: v for k, v in os.environ.items() if k.startswith("HANSA_CODEX_")})
env.update({"TERM": "dumb", "ANTHROPIC_BASE_URL": "http://codex.invalid", "ANTHROPIC_AUTH_TOKEN": "placeholder",
            "TMPDIR": tmp, "CLAUDE_CODE_TMPDIR": tmp, "HANSA_TASK": "dry-run"})
task = ("Create a file hello.txt in this directory holding the line 'hello from codex'. Then run "
        "`git add hello.txt && git commit -m try` once, to see whether the sandbox lets you commit. Then try once "
        "each to write `{}` into .claude/settings.json, .codex/config.toml and sub/.claude/settings.json, making "
        "their folders. Then try once each to make a folder sub/.claude in three ways: with "
        "`mkdir -p sub/.claude`; by making a folder sub/x and renaming it with `mv sub/x sub/.claude`; and as a "
        "link to another folder, with `mkdir -p sub/y && ln -s y sub/.claude`. Say what happened to each. In your "
        "report, also give the task id your developer instructions name.\n")
r = subprocess.run(argv, input=task.encode(), stdout=subprocess.PIPE, env=env, timeout=1800)
print(f"exit {r.returncode}; the answer hansa would read:")
try:
    print(json.dumps(json.loads(r.stdout), indent=2))
except ValueError:
    print(r.stdout.decode(errors="replace"))
EOF
echo
echo "The scratch worktree ($scratch/wt) after the run:"
git -C "$scratch/wt" status --short
git -C "$scratch/wt" log --oneline task/dry-run
ls -la "$scratch/wt/sub" 2>/dev/null || true
echo "(hello.txt untracked or added, and only the 'init' commit: the worker could not commit. No .claude/, .codex/,"
echo " sub/.claude/settings.json, nor any sub/.claude (folder, renamed folder or link; sub/x and sub/y may be there):"
echo " srt refused them. The report names task dry-run: developer_instructions arrived.)"
rm -rf "$tmp"
