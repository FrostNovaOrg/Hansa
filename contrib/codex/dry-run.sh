#!/bin/sh
# One real Codex run through the shim, on a scratch repository, started as hansa starts a worker: the worker's
# command line (with the sandbox hansa would give it in --settings), a clean environment with the switch
# HANSA_CODEX_RUN=1 and the provider's settings, as hansa gives a run on a provider of the kind Codex
# (run.provider_env), the task on stdin. Prints the answer hansa would read, then the scratch worktree's state: hello.txt
# made, and no commit (the sandbox denies writing git's data; hansa commits, never the worker). On macOS srt
# refuses project settings writes, including sub/.claude by mkdir, rename or link. On Linux nested settings
# may be written; hansa's commit check below stages the work and prints what it refuses, making no commit.
# It is also the check of the -c config keys --help does not show:
# the report naming task dry-run shows developer_instructions arrived; refused configuration appears in the answer.
#
# Needs, in the environment (as the provider's Codex program and home; codex.env is not read):
#   HANSA_CODEX_BIN   a codex outside ~/.codex (hansa's sandbox denies reading ~/.codex, srt too)
#   HANSA_CODEX_HOME  its CODEX_HOME, logged in once with:  CODEX_HOME=<it> <codex> login
# and srt on the PATH (or HANSA_CODEX_SRT). HANSA_CODEX_MODEL is passed as hansa passes the provider's model
# (--model; default gpt-6.1-sol); HANSA_CODEX_CA_FILE, HANSA_CODEX_WEB_SEARCH and HANSA_CODEX_DISABLE stand for
# the provider's other settings (defaults: the page's).
# hansa's checkout is the one this script is in (contrib/codex), unless
# HANSA_SRC names another.
set -eu
for need in HANSA_CODEX_BIN HANSA_CODEX_HOME; do
    eval "given=\${$need:-}"
    if [ -z "$given" ]; then
        echo "dry-run.sh: set $need in the environment (the provider's Codex program and home)" >&2
        exit 2
    fi
done
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
from hansa.desk import CODEX_FIELDS
wt = Path.cwd()
box = {"sandbox": {  # hansa's sandbox_settings for a lane with no network list and no write list
    "enabled": True, "failIfUnavailable": True, "autoAllowBashIfSandboxed": True, "allowUnsandboxedCommands": False,
    "filesystem": {"denyRead": hansa_run.full_paths(hansa_run.worker_home_denials()),
                   "denyWrite": hansa_run.git_write_deny(wt)},
    "network": {"allowedDomains": [], "strictAllowlist": True},
    "credentials": {"envVars": [{"name": v, "mode": "deny"} for v in hansa_run.KEY_VARS]}}}
system = hansa_run.PROTOCOL.format(tid="dry-run", lane="codex", tmp=tmp, long="")
argv = [os.path.join(here, "bin", "claude-or-codex"), "-p", "--output-format", "json",
        "--session-id", str(uuid.uuid4()), "--permission-mode", "acceptEdits", "--max-turns", "30",
        "--model", os.environ.get("HANSA_CODEX_MODEL") or "gpt-6.1-sol",
        "--append-system-prompt", system, *hansa_run.isolated(wt, box)]
env = {k: os.environ[k] for k in hansa_run.BASE_ENV if k in os.environ}
# What run.provider_env gives a run on a provider of the kind Codex: the switch and the provider's settings
env.update({"TERM": "dumb", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1", "CLAUDE_CODE_MAX_RETRIES": "8",
            "HANSA_CODEX_RUN": "1", "HANSA_CODEX_BIN": os.environ["HANSA_CODEX_BIN"],
            "HANSA_CODEX_HOME": os.environ["HANSA_CODEX_HOME"],
            "HANSA_CODEX_SRT": os.environ.get("HANSA_CODEX_SRT") or hansa_run.srt_bin(),
            "HANSA_CODEX_WEB_SEARCH": os.environ.get("HANSA_CODEX_WEB_SEARCH") or CODEX_FIELDS["web_search"],
            "HANSA_CODEX_DISABLE": os.environ.get("HANSA_CODEX_DISABLE",
                                                  " ".join(CODEX_FIELDS["disabled_features"]))})
if os.environ.get("HANSA_CODEX_CA_FILE"):
    env["HANSA_CODEX_CA_FILE"] = os.environ["HANSA_CODEX_CA_FILE"]
env.update({"TMPDIR": tmp, "CLAUDE_CODE_TMPDIR": tmp, "HANSA_TASK": "dry-run"})
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
echo "(hello.txt untracked or added, and only the 'init' commit: the worker could not commit.)"
case "$(uname -s)" in
    Linux)
        python3 - "$hansa_src" <<'COMMIT_CHECK'
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from hansa import run as hansa_run
from hansa.desk import Desk
wt = Path.cwd()
desk = Desk(wt.parent)
hansa_run.check_git_file(desk, wt, wt.parent / "repo")
print("hansa's Linux commit check (stages changes; makes no commit):")
try:
    hansa_run.stage_work(desk, wt, hansa_run.wgit(desk, wt, "rev-parse", "HEAD"))
except hansa_run.SandboxPathChanged as e:
    print(e)
else:
    print("No protected changes to refuse at commit (Git does not track empty folders).")
COMMIT_CHECK
        echo "(Top-level .claude/ and .codex/ writes should be refused by srt. Nested paths such as"
        echo " sub/.claude/settings.json or a sub/.claude link may remain on disk: hansa refuses changes to"
        echo " paths with a .claude, .codex or .agents component at any depth at commit. Git ignores empty folders.)"
        ;;
    *)
        echo "(No .claude/, .codex/, sub/.claude/settings.json, nor any sub/.claude"
        echo " (folder, renamed folder or link; sub/x and sub/y may be there): srt refused them.)"
        ;;
esac
echo "(The report names task dry-run: developer_instructions arrived.)"
rm -rf "$tmp"
