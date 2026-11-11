#!/usr/bin/env python3
"""A stand-in for `claude` that runs OpenAI's Codex (`codex exec`) as a hansa worker: hansa's optional Codex adapter.

hansa finds claude through HANSA_CLAUDE; a desk that uses the adapter points it at bin/claude-or-codex, which runs
this. A worker run on the Codex provider (-p, --output-format json, --session-id, and ANTHROPIC_BASE_URL set to
PLACEHOLDER) runs `codex exec` in srt, with the sandbox hansa gave the worker in --settings, and answers on
stdout in the shape hansa reads from `claude -p --output-format json`. hansa's provider check on it (the same, but
no --session-id) runs `codex exec` once, the same way, in a new empty folder and a sandbox of the shim's own making,
and answers the same way. Every other call is the real claude's:
this process execs it, with the same arguments, stdin and environment.

Python 3.9, standard library only. It never imports hansa: the tests do, to check the answers against hansa's
own readings (unusable_reason, network_reason, usage_of, reset_time).
"""
import base64
import errno
import json
import os
import re
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

PLACEHOLDER = "http://codex.invalid"  # the Codex provider's base URL (a `compatible` provider): a Codex worker run
# What the sandbox lets through besides the lane's list: what a ChatGPT login and Codex's API need
CODEX_HOSTS = ("chatgpt.com", "auth.openai.com", "api.openai.com")
DEFAULT_MODEL = "gpt-6.1-sol"
DEFAULT_EFFORT = "high"
DEFAULT_MAX_TURNS = 300  # hansa always gives --max-turns; its own default
SETTINGS = ("HANSA_CODEX_BIN", "HANSA_CODEX_HOME", "HANSA_CODEX_MODEL", "HANSA_CODEX_EFFORT", "HANSA_CODEX_LOGS",
            "HANSA_CODEX_SRT", "HANSA_REAL_CLAUDE", "HANSA_CODEX_CA_FILE", "HANSA_CODEX_WEB_SEARCH",
            "HANSA_CODEX_DISABLE")
# The certificates Codex checks TLS against (CODEX_CA_CERTIFICATE): macOS's own check, its default, does not
# complete inside srt. HANSA_CODEX_CA_FILE, else this when it exists; set but empty, none is given.
CA_SETTING = "HANSA_CODEX_CA_FILE"
DEFAULT_CA_FILE = "/etc/ssl/cert.pem"
# Two ways to the web that run on OpenAI's side, where srt cannot stop them: Codex's own web search (on by default
# in 0.162.1), and, with a ChatGPT login, the codex_apps tools (search_service.web_run opens any page). Off by
# default, as hansa's workers have no web tools: -c web_search=..., and --disable for each feature named.
WEB_SEARCH_SETTING = "HANSA_CODEX_WEB_SEARCH"
WEB_SEARCH_VALUES = ("disabled", "cached", "live")
DISABLE_SETTING = "HANSA_CODEX_DISABLE"  # Codex features, space-separated; set but empty, none
# multi_agent: a sub-agent's tokens are not in turn.completed, so they would go uncounted; hooks: none of a task's
DEFAULT_DISABLE = ("apps", "plugins", "browser_use", "computer_use", "image_generation", "multi_agent", "hooks")
# Every codex exec runs on its own configuration (0.162.1): not $CODEX_HOME/config.toml (notify, mcp_servers,
# providers, project and hook trust) nor environments.toml (commands sent to another machine), not the rules, no
# session files; an unknown -c key fails at start. Auth still comes from CODEX_HOME.
ISOLATION_FLAGS = ("--ignore-user-config", "--ignore-rules", "--ephemeral", "--strict-config")
FIXED_CONFIG = ("analytics.enabled=false", 'approval_policy="never"', 'approvals_reviewer="user"')
# Never carry memory between runs sharing CODEX_HOME, whatever HANSA_CODEX_DISABLE says.
FIXED_DISABLE = ("memories", "external_agent_memory_import")
# In the Codex home, what Codex reads on every run whatever the flags, or what the flags above leave unread: a run
# may not write it (srt's denyWrite), and Codex never writes it during exec
HOME_DENY = ("AGENTS.md", "AGENTS.override.md", "config.toml", "environments.toml", "hooks.json", "rules")
HOME_INSTRUCTIONS = ("AGENTS.md", "AGENTS.override.md")  # read on every run: a run refuses while one exists
HOME_SKILLS, SYSTEM_SKILLS = "skills", ".system"  # skills/ may hold Codex's own .system, nothing else
# What a provider check may not read in the user's home (hansa gives a check no sandbox, so no no-read list): the
# logins of Claude, Codex and other AI tools, and the common credentials (a part of hansa's HOME_SECRETS)
CHECK_HOME_SECRETS = (".ssh", ".aws", ".config/gh", ".claude", ".claude.json", ".netrc", ".gnupg", ".docker",
                      ".kube", ".npmrc", ".pypirc", ".codex", ".gemini", ".config/openai", ".openai", ".anthropic",
                      ".config/anthropic", ".git-credentials", ".config/git/credentials", ".config/gcloud", ".azure")
EMPTY_MEANS_NONE = (CA_SETTING, DISABLE_SETTING)  # the settings where set but empty is not unset
# For the tests only: no watchdog (so the shim's own handling of a signal is what stops srt), and a pause, with
# srt's pid written to the file this names, between srt's start and Run.proc being set
NO_WATCHDOG = "HANSA_CODEX_TEST_NO_WATCHDOG"
PAUSE_AFTER_START = "HANSA_CODEX_TEST_PAUSE"
STATUS_TIMEOUT_SETTING = "HANSA_CODEX_TEST_STATUS_TIMEOUT"  # for the tests only: seconds for `codex login status`
STATUS_TIMEOUT = 20  # seconds `codex login status` may take, after a refresh collision
# What hansa gives Claude Code for the provider, which Codex must not get: the placeholder's key, among them
DROP_VARS = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL",
             "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL",
             "CLAUDE_CODE_SUBAGENT_MODEL", "CLAUDE_CONFIG_DIR", "CLAUDE_ENV_FILE", "CLAUDE_CODE_MAX_RETRIES",
             "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "CODEX_HOME", "CODEX_CA_CERTIFICATE", NO_WATCHDOG, PAUSE_AFTER_START,
             STATUS_TIMEOUT_SETTING, *SETTINGS)
COUNTED_ITEMS = ("command_execution", "file_change")  # what counts against --max-turns
STOP_GRACE = 3  # seconds between SIGTERM and SIGKILL; hansa waits 10 on a stop, 5 on a sweep
POLL = 0.2  # seconds between looks at Codex's log, while it writes nothing new
WATCHDOG = "--hansa-codex-watchdog"
STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
# The project folders at the top of the worktree Codex may not write, whole: Claude Code's (.claude) and Codex's
# (.codex, .agents) settings, hooks, rules and skills, which a later run on the branch would obey, its hooks outside
# any sandbox, once hansa has committed them. Claude Code's own sandbox denies some of .claude; srt alone, only
# .claude/commands and .claude/agents.
PROJECT_CONFIG_DIRS = (".claude", ".codex", ".agents")
# Below the top, as srt's macOS globs (**/ is any folders, or none): the project folders' names themselves, so
# that srt's move-blocking refuses to make an entry so named, by mkdir, link or rename; and what Claude Code's
# sandbox protects in a .claude folder. srt on Linux drops globs from denyWrite (sandbox-manager's
# stripWriteGlobs), so there only the top folders hold.
NESTED_CONFIG = (*PROJECT_CONFIG_DIRS,
                 ".claude/settings.json", ".claude/settings.local.json", ".claude/hooks", ".claude/skills")
# hansa's worker deny list in git's folders (run.py: GIT_COMMON_DENY, GIT_OWN_DENY, GIT_OPS); the tests hold
# these to hansa's own
GIT_COMMON_DENY = ("objects", "refs", "packed-refs", "HEAD", "config", "hooks", "info", "logs", "config.worktree")
GIT_OPS = ("MERGE_HEAD", "MERGE_MODE", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "sequencer")
GIT_OWN_DENY = ("HEAD", "logs", "commondir", "gitdir", "config.worktree", *GIT_OPS)
# zsh makes a here-document's temporary file under TMPPREFIX (default /tmp/zsh), not TMPDIR, and srt lets the run
# write only in the worktree, TMPDIR and the Codex home: `cat > f <<'EOF'` failed with "can't create temp file for
# here document". Claude Code sets it to $TMPDIR/zsh for its own commands; Codex's get it in the shim's folder
# there, through its environment and shell_environment_policy.set.
ZSH_PREFIX = "TMPPREFIX"
REPO = Path(__file__).resolve().parent.parent
CODEX_ENV = REPO / "codex.env"  # the settings, for a worker: hansa gives it a clean environment
OLD_ENV = REPO / "trial.env"  # its older name: read when there is no codex.env, so that an older desk keeps working

# Run inside srt: CODEX_CA_CERTIFICATE when one is given, then the lane's env and sandbox prelude (hansa's
# CLAUDE_ENV_FILE, copied where the sandbox can read it), whose own CODEX_CA_CERTIFICATE wins, then Codex, with
# its CODEX_HOME set after whatever the lane's env said.
INNER = ('hansa_codex_f=$1; hansa_codex_h=$2; hansa_codex_c=$3; shift 3; '
         'if [ -n "$hansa_codex_c" ]; then CODEX_CA_CERTIFICATE=$hansa_codex_c; export CODEX_CA_CERTIFICATE; fi; '
         'if [ -n "$hansa_codex_f" ]; then . "$hansa_codex_f"; rm -f "$hansa_codex_f"; fi; '
         'CODEX_HOME=$hansa_codex_h; export CODEX_HOME; unset hansa_codex_f hansa_codex_h hansa_codex_c; exec "$@"')


# ---- settings and the call -------------------------------------------------------------------------------

def settings_text() -> str:
    """codex.env, else OLD_ENV when there is no codex.env; '' when neither can be read."""
    for f in (CODEX_ENV, OLD_ENV):
        try:
            return f.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except OSError:
            break
    return ""


def settings(env) -> dict:
    """The adapter's settings: from the environment, else from codex.env (KEY=VALUE lines) beside bin/. Empty in
    the environment is unset, except for EMPTY_MEANS_NONE, where empty means none."""
    out = {}
    for line in settings_text().splitlines():
        k, eq, v = line.strip().partition("=")
        if eq and k.strip() in SETTINGS:
            out[k.strip()] = os.path.expanduser(v.strip().strip("'\""))
    out.update({k: env[k] for k in SETTINGS if env.get(k) or (k in EMPTY_MEANS_NONE and k in env)})
    return out


def ca_file(conf: dict, default: str = DEFAULT_CA_FILE) -> str:
    """The certificate file for Codex's CODEX_CA_CERTIFICATE: the setting (empty: none), else `default` when it
    exists, else none ('')."""
    if CA_SETTING in conf:
        return conf[CA_SETTING]
    return default if os.path.isfile(default) else ""


def web_options(conf: dict):
    """(web_search's value, the features to disable) from the settings; a str when one is not usable."""
    search = conf.get(WEB_SEARCH_SETTING) or WEB_SEARCH_VALUES[0]
    if search not in WEB_SEARCH_VALUES:
        return f"{WEB_SEARCH_SETTING} ({search}) is not one of {', '.join(WEB_SEARCH_VALUES)}"
    off = conf[DISABLE_SETTING].split() if DISABLE_SETTING in conf else list(DEFAULT_DISABLE)
    bad = [f for f in off if not re.fullmatch(r"[A-Za-z0-9_-]+", f) or f.startswith("-")]
    if bad:
        return f"{DISABLE_SETTING} names {bad[0]!r}, which is not a Codex feature name"
    return search, off


VALUE_OPTS = {"--session-id", "--resume", "-r", "--max-turns", "--model", "--append-system-prompt", "--effort",
              "--settings", "--setting-sources", "--permission-mode", "--output-format", "--input-format",
              "--system-prompt", "--fallback-model", "--tools"}


def parse(argv: list) -> dict:
    """What this shim reads of claude's arguments. Variadic lists (--allowedTools, say) hold rules, never these."""
    out, i = {"print": False}, 0
    while i < len(argv):
        a = argv[i]
        name, eq, val = a.partition("=")
        if a in ("-p", "--print"):
            out["print"] = True
        elif eq and name in VALUE_OPTS and name.startswith("--"):
            out[name] = val
        elif a in VALUE_OPTS and i + 1 < len(argv):
            out[a] = argv[i + 1]
            i += 1
        elif a in ("--resume", "-r", "--continue", "-c"):
            out["--resume"] = ""
        i += 1
    if "-r" in out:
        out["--resume"] = out.pop("-r")
    return out


def call_kind(argv: list, env) -> str:
    """"codex" (a worker run on the Codex provider), "check" (hansa's provider check on it: no session id; its
    --tools, any value, the empty one too, is not looked at), "resume" (a resume on it), or "pass" (the real
    claude's)."""
    url = (env.get("ANTHROPIC_BASE_URL") or "").rstrip("/")
    if url != PLACEHOLDER:
        return "pass"
    a = parse(argv)
    if "--resume" in a:
        kind = "resume"
    elif a["print"] and a.get("--output-format") == "json":
        kind = "codex" if a.get("--session-id") else "check"
    else:
        return "pass"  # an interactive session, --version
    return kind


def real_claude(env: dict, conf: dict) -> str:
    """The real claude: HANSA_REAL_CLAUDE, else the first claude on PATH that is not this shim, else
    ~/.local/bin/claude."""
    me = {os.path.realpath(__file__), os.path.realpath(REPO / "bin" / "claude-or-codex")}
    path = env.get("PATH", "").split(os.pathsep)
    for c in [conf.get("HANSA_REAL_CLAUDE"), *(os.path.join(d, "claude") for d in path if d),
              os.path.expanduser("~/.local/bin/claude")]:
        if c and os.path.isfile(c) and os.access(c, os.X_OK) and os.path.realpath(c) not in me:
            return c
    raise SystemExit("hansa-codex-shim: the real claude was not found; set HANSA_REAL_CLAUDE in codex.env")


def pass_through(argv: list, env: dict, conf: dict) -> None:
    c = real_claude(env, conf)
    os.execve(c, [c, *argv], env)  # same arguments, stdin, environment; claude's exit code is the caller's


# ---- the sandbox -------------------------------------------------------------------------------------------

def both(paths) -> list:
    """Each path as given and with links resolved (/tmp is /private/tmp on macOS), once (hansa's full_paths)."""
    return list(dict.fromkeys(x for p in paths if p for x in (str(p), os.path.realpath(str(p)))))


def under(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder.rstrip("/") + "/")


def git_folders(wt: str):
    """(the worktree's .git, its own git folder, the repository's common one), read as files (no git runs here);
    (None, None, None) without a .git, the folders None when it leads nowhere readable. A main checkout's own
    folder is the common one."""
    dot = os.path.join(wt, ".git")
    if os.path.isdir(dot):
        return dot, dot, dot
    try:
        text = Path(dot).read_text(encoding="utf-8").strip()
    except OSError:
        return None, None, None
    if not text.startswith("gitdir:"):
        return dot, None, None
    own = os.path.normpath(os.path.join(wt, text[len("gitdir:"):].strip()))
    try:
        common = os.path.join(own, Path(own, "commondir").read_text(encoding="utf-8").strip())
    except OSError:
        return dot, None, None
    return dot, own, os.path.normpath(common)


def glob_regex(pattern: str):
    """A denyWrite glob as srt's macOS sandbox reads it (globToRegex, with the subtree a deny covers): ** across
    folders, * and ? within one, [...] as given; paths without regard to case, as macOS matches them."""
    out = re.sub(r"[.^$+{}()|\\]", lambda m: "\\" + m.group(0), pattern)
    out = re.sub(r"\[([^\]]*?)$", r"\\[\1", out)
    out = out.replace("**/", "\0S").replace("**", "\0G").replace("*", "[^/]*").replace("?", "[^/]")
    return re.compile("^" + out.replace("\0S", "(.*/)?").replace("\0G", ".*") + "(/.*)?$", re.I)


def denied(path: str, deny_write: list) -> bool:
    """Whether `path`, as given or with links resolved, is in deny_write: at or under a path or a glob there."""
    spellings = both([path])
    for d in deny_write:
        if any(c in d for c in "*?["):
            if any(glob_regex(d).match(s) for s in spellings):
                return True
        elif any(under(s, d) for s in spellings):
            return True
    return False


def git_required(wt: str):
    """What in git's folders hansa's worker settings deny for the worktree `wt` (run.py's git_write_deny), and so
    what srt's must, as (named, others): named, each by its path: the worktree's .git, the common folder's
    GIT_COMMON_DENY and, for a linked worktree, its own folder's GIT_OWN_DENY (on Linux, GIT_OPS only where
    present, as hansa); others, by its path or a pattern (what was made after hansa listed them): every other
    entry now in the common folder and in its worktrees/. A str: why it cannot be told."""
    dot, own, common = git_folders(wt)
    if dot is None:
        return f"the worktree {wt} has no .git"
    if common is None:
        return f"the git folder the worktree's {dot} leads to could not be read"
    linked = os.path.realpath(own) != os.path.realpath(common)
    named = [dot, *(os.path.join(common, n) for n in GIT_COMMON_DENY)]
    others = []
    linux = sys.platform.startswith("linux")
    if linked:
        named += [os.path.join(own, n) for n in GIT_OWN_DENY
                  if not (linux and n in GIT_OPS and not os.path.lexists(os.path.join(own, n)))]
        for folder, keep in ((common, "worktrees"), (os.path.join(common, "worktrees"), os.path.basename(own))):
            try:
                others += [os.path.join(folder, n) for n in sorted(os.listdir(folder)) if n != keep]
            except OSError:
                pass
    return named, [x for x in dict.fromkeys(others) if x not in named]


def config_deny(wt: str) -> list:
    """srt's denyWrite for the project folders: PROJECT_CONFIG_DIRS at the top, NESTED_CONFIG below it (and, the
    globs matching no folders for **/, at the top too)."""
    return both([os.path.join(wt, d) for d in PROJECT_CONFIG_DIRS]) + [
        f"{w}/**/{n}" for w in both([wt]) for n in NESTED_CONFIG]


def home_deny(codex_home: str) -> list:
    """srt's denyWrite in the Codex home: HOME_DENY, as given and with links resolved."""
    return both([os.path.join(codex_home, n) for n in HOME_DENY]) if codex_home else []


def home_problem(codex_home: str) -> str:
    """What in the Codex home a run left for later runs, which Codex would read: an AGENTS.md or
    AGENTS.override.md, or an entry in skills/ other than Codex's own .system. '' when none. Only names and
    existence are looked at; no file is read."""
    for n in HOME_INSTRUCTIONS:
        p = os.path.join(codex_home, n)
        if os.path.lexists(p):
            return f"{p} exists in the Codex home, and Codex would follow it on every run; remove it"
    skills = os.path.join(codex_home, HOME_SKILLS)
    if os.path.islink(skills) or (os.path.lexists(skills) and not os.path.isdir(skills)):
        return f"{skills} is not a folder of the Codex home's own; remove it"
    try:
        names = sorted(os.listdir(skills))
    except FileNotFoundError:
        return ""
    except OSError as e:
        return f"{skills} could not be listed: {e}"
    for n in names:
        if n != SYSTEM_SKILLS:
            return (f"{os.path.join(skills, n)} is in the Codex home's skills, and Codex would load it on every run;"
                    " remove it")
    return ""


def home_read_open(deny_read, codex_home: str):
    """(denyRead without the entries that are the Codex home or inside it, those entries): a desk may list the
    Codex home among the folders its workers may not read, so that other lanes' Claude workers cannot read the
    login, and a Codex run needs it. An entry above the home stays (sandbox_problem refuses it): never more
    than the home is opened."""
    homes = both([codex_home])
    kept, left = [], []
    for d in deny_read or []:
        inside = bool(d) and any(under(x, h) for x in both([str(d)]) for h in homes)
        (left if inside else kept).append(d)
    return kept, left


def sandbox_problem(fs: dict, wt: str, codex_home: str, codex_bin: str, ca: str = "") -> str:
    """Why srt with these settings (its filesystem part) is not the boundary hansa means; '' when it is. Codex
    runs with its own sandbox off, so srt alone keeps it out of git's data (hansa commits, never the worker),
    the project folders other runs obey, what in the Codex home later runs read, and the no-read list. Only on
    macOS: srt on Linux drops the nested denies."""
    if sys.platform != "darwin":
        return (f"Codex runs only on macOS for now ({sys.platform}): srt there does not hold the nested write "
                "denies")
    deny_write = [str(x) for x in fs.get("denyWrite") or []]
    if any(c in wt for c in "*?[]"):  # srt would read the worktree's path in its patterns as a glob
        return f"the worktree's path {wt} holds a glob character"
    need = git_required(wt)
    if isinstance(need, str):
        return need
    named, others = need
    for p in named:
        if not any(x in deny_write for x in both([p])):
            return f"the sandbox settings hansa gave do not deny writing {p}"
    for p in others:
        if not denied(p, deny_write):
            return f"the sandbox settings hansa gave do not deny writing {p}"
    for d in config_deny(wt):
        if d not in deny_write:
            return f"srt's settings do not deny writing {d}"
    return paths_problem(fs, wt, codex_home, codex_bin, ca)


def paths_problem(fs: dict, wt: str, codex_home: str, codex_bin: str, ca: str = "") -> str:
    """What a worker run and a check share of the pre-run check: HOME_DENY in the Codex home denied; codex, its
    home and the certificate file readable (srt would refuse codex the read), the home outside `wt`. '' when
    none."""
    deny_write = [str(x) for x in fs.get("denyWrite") or []]
    for d in home_deny(codex_home):
        if d not in deny_write:
            return f"srt's settings do not deny writing {d}"
    if ca and not os.path.isfile(ca):
        return f"{CA_SETTING} ({ca}) is not a file; set it to Codex's certificate file, or empty for none"
    for what, p in (("HANSA_CODEX_HOME", codex_home), ("HANSA_CODEX_BIN", codex_bin), (CA_SETTING, ca)):
        if not p:
            continue
        for d in fs.get("denyRead") or []:
            if any(under(x, str(d)) for x in both([p])):
                above = (" (the shim opens the Codex home itself to a Codex run, never a folder above it)"
                         if p == codex_home else "")
                return f"{what} ({p}) is under {d}, which the sandbox may not read{above}; put it elsewhere"
    if any(under(x, w) for x in both([codex_home]) for w in both([wt])):
        return f"HANSA_CODEX_HOME ({codex_home}) is in the worktree"
    return ""


def own_git_write(wt: str) -> list:
    """A linked worktree's own git folder, where git keeps the worktree's index (git checkout <commit> -- <file>
    takes index.lock there): Claude Code opens the common folder to a Claude worker, all of it but this folder
    kept by hansa's denyWrite, so srt opens this one folder to a Codex run, hansa's denies in it (HEAD, logs and
    the rest of GIT_OWN_DENY) still above it. [] for a main checkout, whose .git hansa denies whole, for a .git
    leading anywhere but a folder in the common folder's worktrees/, and for a folder whose path, as given or
    resolved, holds a glob character (srt would read the grant as a pattern, over other worktrees' folders)."""
    dot, own, common = git_folders(wt)
    if own is None or common is None:
        return []
    if os.path.dirname(os.path.realpath(own)) != os.path.join(os.path.realpath(common), "worktrees"):
        return []
    if any(c in p for p in both([own]) for c in "*?[]"):
        return []
    return [own]


def srt_settings(box: dict, wt: str, tmp: str, codex_home: str) -> dict:
    """srt's settings for the Codex run, from the sandbox hansa gave Claude Code in --settings: writes in the
    worktree, its own git folder (own_git_write), its temporary folder, the lane's write list and
    HANSA_CODEX_HOME; git's data, the project folders (config_deny) and HOME_DENY in the Codex home never
    written; the no-read list never read, but for the Codex home and what is inside it (home_read_open), and what
    hansa re-allows in it (allowRead: the worktree, the run's temporary folder); the lane's network list and
    CODEX_HOSTS. (hansa's srt_settings needs its desk.)"""
    sb = box.get("sandbox") or {}
    fs, net = sb.get("filesystem") or {}, sb.get("network") or {}
    hosts = list(dict.fromkeys([*(net.get("allowedDomains") or []), *CODEX_HOSTS]))
    network = {"allowedDomains": hosts, "deniedDomains": []}  # srt refuses settings without deniedDomains
    if net.get("allowLocalBinding"):
        network["allowLocalBinding"] = True
    return {"network": network, "filesystem": {
        "allowWrite": both([wt, *own_git_write(wt), tmp, codex_home]) + [x for x in fs.get("allowWrite") or [] if x],
        "denyRead": home_read_open(fs.get("denyRead"), codex_home)[0],
        "allowRead": [x for x in fs.get("allowRead") or [] if x],
        "denyWrite": list(dict.fromkeys([*(fs.get("denyWrite") or []), *config_deny(wt), *home_deny(codex_home)]))}}


def check_deny_read(home: str, cwd: str) -> list:
    """srt's denyRead for a provider check, which hansa gives no sandbox: CHECK_HOME_SECRETS in the user's home,
    the folder hansa ran the check in and, when that is hansa's <desk>/.hansa/check, the desk's .hansa (its keys
    and state)."""
    paths = [os.path.join(home, n) for n in CHECK_HOME_SECRETS] if home else []
    paths.append(cwd)
    if os.path.basename(cwd) == "check" and os.path.basename(os.path.dirname(cwd)) == ".hansa":
        paths.append(os.path.dirname(cwd))
    return both(paths)


def check_settings(folder: str, codex_home: str, deny_read: list) -> dict:
    """srt's settings for a provider check: writes in its own new folder and the Codex home alone (Codex keeps its
    login there, and a refreshed token not written back would end the login), HOME_DENY in the home never; no
    reads of deny_read but for the Codex home (home_read_open); the network, CODEX_HOSTS alone."""
    return {"network": {"allowedDomains": list(CODEX_HOSTS), "deniedDomains": []}, "filesystem": {
        "allowWrite": both([folder, codex_home]),
        "denyRead": home_read_open(deny_read, codex_home)[0],
        "denyWrite": home_deny(codex_home)}}


def toml_string(s: str) -> str:
    """s as a TOML basic string, for codex's -c (which reads its value as TOML)."""
    named = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r"}
    return '"' + "".join(named.get(c) or (f"\\u{ord(c):04x}" if ord(c) < 0x20 or ord(c) == 0x7f else c)
                         for c in s) + '"'


def codex_argv(codex: str, wt: str, model: str, effort: str, system: str, last: str,
               web_search: str = WEB_SEARCH_VALUES[0], disable=DEFAULT_DISABLE, zsh_prefix: str = "",
               no_repo: bool = False) -> list:
    """codex exec's command line; `no_repo` (a check, whose folder is no git repository): --skip-git-repo-check."""
    return [codex, "exec", "--json", *ISOLATION_FLAGS, *(("--skip-git-repo-check",) if no_repo else ()),
            "-C", wt, "-m", model,
            "-c", f"model_reasoning_effort={toml_string(effort)}",
            "-c", f"developer_instructions={toml_string(system)}",
            # set reaches every command whatever the policy inherits (all, core or none)
            *(("-c", f"shell_environment_policy.set.{ZSH_PREFIX}={toml_string(zsh_prefix)}") if zsh_prefix else ()),
            *(x for c in FIXED_CONFIG for x in ("-c", c)),
            "-c", "check_for_update_on_startup=false",
            "-c", f"web_search={toml_string(web_search)}",
            *(x for f in (*disable, *FIXED_DISABLE) for x in ("--disable", f)),
            "-s", "danger-full-access", "-o", last, "-"]


# ---- processes ---------------------------------------------------------------------------------------------

def _sysctl(*mib):
    import ctypes
    import ctypes.util
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    name = (ctypes.c_int * len(mib))(*mib)
    for _ in range(5):
        size = ctypes.c_size_t(0)
        if libc.sysctl(name, len(mib), None, ctypes.byref(size), None, 0) != 0:
            return b""
        size.value += 648 * 16
        buf = ctypes.create_string_buffer(size.value)
        if libc.sysctl(name, len(mib), buf, ctypes.byref(size), None, 0) == 0:
            return buf.raw[:size.value]
        if ctypes.get_errno() != errno.ENOMEM:
            return b""
    return b""


def process_table() -> dict:
    """{pid: (parent pid, start)} of the running processes; {} when it cannot be read. /proc on Linux; on macOS
    sysctl, as hansa does (its sandbox will not start ps): a kinfo_proc is 648 bytes, its start at 0, state at
    36, pid at 40, parent at 560."""
    out = {}
    if os.path.isdir("/proc"):
        for p in os.listdir("/proc"):
            try:
                if p.isdigit():
                    with open(f"/proc/{p}/stat") as fh:
                        f = fh.read().rsplit(")", 1)[1].split()
                    if f[0] != "Z":
                        out[int(p)] = (int(f[1]), f[19])
            except (OSError, IndexError, ValueError):
                pass
        return out
    try:
        data = _sysctl(1, 14, 0, 0)  # CTL_KERN, KERN_PROC, KERN_PROC_ALL
    except (OSError, AttributeError, ImportError, TypeError, ValueError):
        return {}
    for i in range(0, len(data) - 647, 648):
        k = data[i:i + 648]
        pid = struct.unpack_from("@i", k, 40)[0]
        if pid > 0 and k[36] != 5:  # not a zombie
            out[pid] = (struct.unpack_from("@i", k, 560)[0], "%d.%06d" % struct.unpack_from("@li", k, 0))
    return out


def descendants(top: int, seen: dict) -> dict:
    """seen ({pid: start}) with each process now running below `top` or below one seen, whatever its group."""
    table = process_table()
    if top in table:
        seen.setdefault(top, table[top][1])
    grew = True
    while grew:
        grew = False
        for pid, (ppid, start) in table.items():
            if pid not in seen and ppid in seen and table[ppid][1] == seen[ppid]:
                seen[pid], grew = start, True
    return seen


def stop_tree(top: int, proc=None, grace: float = STOP_GRACE, seen=None) -> None:
    """Stop `top` (srt, the leader of its own process group), its group and every process below it: SIGTERM,
    up to `grace` seconds, then SIGKILL. A process that left the group (codex's commands may) is found by its
    parent, looked up now and while it ran (`seen`, {pid: start})."""
    mine = {os.getpid(), os.getppid()}
    seen = descendants(top, dict(seen or {}))

    def send(sig):
        try:
            os.killpg(top, sig)
        except OSError:
            pass
        table = process_table()
        for pid, start in list(seen.items()):
            if pid not in mine and (not table or table.get(pid, (0, None))[1] == start):
                try:
                    os.kill(pid, sig)
                except OSError:
                    pass
    send(signal.SIGTERM)
    end = time.time() + grace
    while time.time() < end:
        if proc is not None and proc.poll() is None:
            descendants(top, seen)
        alive = process_table()
        if not any(p in alive and alive[p][1] == s for p, s in seen.items() if p not in mine):
            break
        time.sleep(0.1)
    send(signal.SIGKILL)


def watchdog(shim: int, top: int) -> None:
    """Watches the shim from a session of its own: when the shim dies without stopping srt and what runs below
    it (SIGKILL), stop them."""
    seen = {}
    while True:
        time.sleep(0.5)
        if os.getppid() != shim:
            stop_tree(top, seen=seen)
            return
        table = process_table()
        if table and top not in table:  # srt ended: the shim cleans up after it
            return
        descendants(top, seen)


# ---- what Codex said ---------------------------------------------------------------------------------------

# Codex 0.162.1's own messages (several with ’, U+2019, where ' is read too), each matched in the whole text
# A provider limit: hansa rests the provider (until the reset a usage limit gives)
CREDIT_TEXT = re.compile(r"Your workspace is out of credits|"
                         r"To use Codex with your ChatGPT plan, upgrade to Plus", re.I)
LIMIT_TEXT = re.compile(r"You[’']ve hit your usage limit|You hit your spend cap|"
                        r"Quota exceeded\. Check your plan and billing details\.", re.I)
# Overloaded, try later: as Claude's overloaded (529)
OVERLOADED_TEXT = re.compile(r"Selected model is at capacity|We[’']re currently experiencing high demand", re.I)
# The provider refused the request: not a dropped connection, though it starts as one does
REFUSAL_TEXT = re.compile(r"stream disconnected before completion: Incomplete response returned, reason: "
                          r"content_filter", re.I)
DROPPED_TEXT = re.compile(r"stream disconnected before completion: ", re.I)  # with any other reason
# The login failed: not retried; the provider cannot be used until someone signs in again
LOGIN_FAILED_TEXT = re.compile(r"Your access token could not be refreshed|unexpected status 401 Unauthorized", re.I)
# One of those: another run on the same Codex home refreshed at the same instant (Codex refreshes only when the
# auth on disk is unchanged, manager.rs), and the login on disk may be fine; `codex login status` tells
REFRESH_USED_TEXT = re.compile(r"refresh token was already used", re.I)
LOGGED_IN_LINE = re.compile(r"Logged in\b")  # the first line of `codex login status` when it is
NO_MODEL_TEXT = re.compile(r"unexpected status 404 Not Found", re.I)  # the model is not available
# --strict-config's refusal of a -c key, or --disable's of a feature name: on stderr only, before the start
CONFIG_TEXT = re.compile(r"Error loading config\.toml|unknown configuration field|Unknown feature flag", re.I)
# A retry, not the final error: "Reconnecting... 2/5 (M)", "Reconnecting... waiting for network (M)"
RETRY = re.compile(r"^\s*Reconnecting\.\.\.\s*(?:\d+/\d+|waiting for network)\s*\((.*)\)\s*$", re.S)
# Older or other wordings, after Codex's own
QUOTA_WORDS = re.compile(r"usage limit|usage_limit|rate limit|rate_limit|quota|too many requests|\b429\b", re.I)
LOGIN_WORDS = re.compile(r"not logged in|no (?:auth|credentials)|login required|please (?:log|sign) ?in|"
                         r"codex login|token data is not available", re.I)
AUTH_WORDS = re.compile(r"\b40[13]\b|unauthori[sz]ed|forbidden|token[ _](?:expired|revoked)|"
                        r"refresh token|sign(?:ing)? in again|log(?:ging)? ?in again|invalid_grant", re.I)
SERVER_STATUS = re.compile(r"(?:status:?|status code:?|HTTP)\s*(5\d\d)\b|\b(5\d\d) (?:Internal|Bad Gateway|"
                           r"Service Unavailable|Gateway Time)", re.I)
STREAM_WORDS = re.compile(r"stream (?:disconnected|closed|ended|error)|disconnected before completion|"
                          r"unexpected eof|idle timeout", re.I)
CONNECT_WORDS = re.compile(r"error sending request|connection (?:reset|refused|closed|error|aborted)|"
                           r"workspace routing discovery failed|"
                           r"timed? ?out|dns error|failed to lookup|network (?:is )?unreachable|broken pipe", re.I)


def reset_at(text: str, at=None):
    """When a usage limit resets, as Codex's message says: resets_at / resets_in_seconds, "try again in 2 hours
    5 minutes", or "try again at 3:05 PM" / "at Oct 12th, 2026 3:05 PM" (local time). None when it gives none."""
    at = time.time() if at is None else at
    if m := re.search(r"resets?_?at\"?\s*[:=]\s*\"?(\d{10})", text, re.I):
        return int(m.group(1))
    if m := re.search(r"resets?_in_seconds\"?\s*[:=]\s*\"?(\d+)", text, re.I):
        return int(at + int(m.group(1)))
    if m := re.search(r"try again in ((?:\s*(?:and\s+)?\d+\s*(?:days?|hours?|hrs?|minutes?|mins?|seconds?|secs?|"
                      r"[dhms])\b,?)+)", text, re.I):
        unit = {"d": 86400, "h": 3600, "m": 60, "s": 1}
        return int(at + sum(int(n) * unit[u[0].lower()]
                            for n, u in re.findall(r"(\d+)\s*([a-z]+)", m.group(1), re.I)))
    if m := re.search(r"try again at ([A-Z][a-z]{2} \d{1,2})(?:st|nd|rd|th)?,? (\d{4}),? (\d{1,2}:\d{2} ?[AP]M)",
                      text, re.I):
        try:
            when = datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3).replace(' ', '')}", "%b %d %Y %I:%M%p")
        except ValueError:
            return None
        return int(when.timestamp())
    if m := re.search(r"try again at (\d{1,2}:\d{2} ?[AP]M)", text, re.I):
        try:
            clock = datetime.strptime(m.group(1).replace(" ", "").upper(), "%I:%M%p")
        except ValueError:
            return None
        now = datetime.fromtimestamp(at)
        when = now.replace(hour=clock.hour, minute=clock.minute, second=0, microsecond=0)
        if when.timestamp() < at - 60:
            when += timedelta(days=1)
        return int(when.timestamp())
    return None


def one_line(text: str, n: int = 400) -> str:
    """text on one line, for display: cut at n characters, with … where it was cut."""
    whole = " ".join(str(text).split())
    return whole if len(whole) <= n else whole[:n - 1] + "…"


def login_failure(said: str, home: str, codex: str) -> dict:
    """The adapter owns the login remedy; hansa keeps login_hint when resting the provider."""
    login = (f"CODEX_HOME={shlex.quote(home)} " if home else "") + f"{shlex.quote(codex)} login"
    hint = f"sign in again with `{login}`"
    return {"result": f"Not logged in (Codex: {said}); {hint}", "login_hint": hint}


def classify(text: str, home: str = "", codex: str = "codex") -> dict:
    """Codex's failure as hansa reads Claude Code's: the fields of an error answer ("result", maybe
    "api_error_status" or "stop_reason"), worded so hansa's unusable_reason and network_reason class it as they
    would Claude's. Classed on the whole text; only what is shown is cut."""
    whole = " ".join(str(text).split())
    said = one_line(whole)
    if REFUSAL_TEXT.search(whole):
        return {"result": f"Codex: the provider refused the request (content_filter): {said}",
                "stop_reason": "content_filter"}
    if CREDIT_TEXT.search(whole):
        return {"result": f"API Error: 402 credit balance too low (Codex: {said})", "api_error_status": 402}
    if LIMIT_TEXT.search(whole) or QUOTA_WORDS.search(whole):
        reset = reset_at(whole)
        return {"result": f"API Error: 429 usage limit reached (Codex: {said})"
                          + (f" · resets_at: {reset}" if reset else ""), "api_error_status": 429}
    if LOGIN_FAILED_TEXT.search(whole):
        return login_failure(said, home, codex)
    if NO_MODEL_TEXT.search(whole):
        return {"result": f"API Error: 404 the model is not available (Codex: {said})", "api_error_status": 404}
    if OVERLOADED_TEXT.search(whole):
        return {"result": f"API Error: Repeated 529 Overloaded errors (Codex: {said})", "api_error_status": 529}
    if DROPPED_TEXT.search(whole):
        return {"result": f"API Error: Connection dropped (Codex: {said})"}
    if CONFIG_TEXT.search(whole):
        return {"result": f"hansa-codex-shim: Codex refused its configuration (a -c key or --disable feature this "
                          f"codex does not know): {said}"}
    if LOGIN_WORDS.search(whole) and not re.search(r"\b40[13]\b", whole):
        return login_failure(said, home, codex)
    if AUTH_WORDS.search(whole):
        status = 403 if re.search(r"\b403\b|forbidden", whole, re.I) else 401
        return {"result": f"API Error: {status} authentication_error (Codex: {said})", "api_error_status": status}
    if m := SERVER_STATUS.search(whole):
        status = int(m.group(1) or m.group(2))
        return {"result": f"API Error: {status} (Codex: {said})", "api_error_status": status}
    if STREAM_WORDS.search(whole):
        return {"result": f"API Error: Connection dropped (Codex: {said})"}
    if CONNECT_WORDS.search(whole):
        return {"result": f"API Error: Connection error (Codex: {said})"}
    return {"result": f"Codex failed: {said or 'no message'}"}


def answer(subtype: str, result: str, sid: str, turns: int = 0, model: str = "", usage=None, **extra) -> dict:
    u = {model: usage} if usage else {}
    return {"type": "result", "subtype": subtype, "is_error": subtype != "success", "result": result,
            "num_turns": turns, "session_id": sid, "usage": u, "modelUsage": u, "total_cost_usd": 0,
            "permission_denials": [], **extra}


def usage_of(u: dict) -> dict:
    """turn.completed's usage as Claude Code's modelUsage: Codex counts cached input and cache writes inside input,
    and reasoning inside output (passed on alone as reasoningOutputTokens, never added again)."""
    def n(k):
        try:
            return max(int(u.get(k) or 0), 0)
        except (TypeError, ValueError):
            return 0
    cached, written = n("cached_input_tokens"), n("cache_write_input_tokens")
    return {"inputTokens": max(n("input_tokens") - cached - written, 0), "outputTokens": n("output_tokens"),
            "cacheReadInputTokens": cached, "cacheCreationInputTokens": written,
            "reasoningOutputTokens": n("reasoning_output_tokens")}


def login_status(codex: str, home: str, env: dict, timeout: float = STATUS_TIMEOUT):
    """(logged in, how it was told) from `codex login status` with CODEX_HOME=home, run here, outside the sandbox,
    within `timeout` seconds. Only its exit code and first line are looked at, and neither is passed on: the
    line may name the login (an API key's first characters). Not logged in, a failure or no answer: False."""
    senv = {k: v for k, v in env.items() if k not in DROP_VARS}
    senv["CODEX_HOME"] = home
    with tempfile.TemporaryFile() as out:  # a file, not a pipe: a child left behind cannot hold it open
        try:
            proc = subprocess.Popen([codex, "login", "status"], stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                                    env=senv, start_new_session=True)
        except OSError as e:
            return False, f"could not start ({e.strerror or e})"
        Run.proc, Run.seen = proc, {}  # a signal now stops it too
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            stop_tree(proc.pid, proc, grace=0)
            proc.wait()
            return False, f"did not answer within {timeout:g} s"
        finally:
            Run.proc = None
        out.seek(0)
        lines = out.read(4096).decode("utf-8", "replace").splitlines()
    first = lines[0].strip() if lines else ""
    if code == 0 and LOGGED_IN_LINE.match(first):
        return True, "logged in"
    if code != 0 and re.match(r"Not logged in\b", first, re.I):
        return False, "not logged in"
    return False, f"failed (exit {code})"


# ---- the run -----------------------------------------------------------------------------------------------

class Run:
    proc = None
    seen: dict = {}  # what ran below srt, as last looked up
    mine = None  # the run's own folder, removed however it ends


def killed_by(code: int):
    """The signal srt's exit code tells of: srt itself ended by one (a negative code), or 128 + its number, as a
    shell reports a killed child. None for any other code."""
    n = -code if code < 0 else code - 128 if code > 128 else 0
    try:
        return signal.Signals(n) if n > 0 else None
    except ValueError:
        return None


def emit(ans: dict) -> int:
    sys.stdout.write(json.dumps(ans) + "\n")
    sys.stdout.flush()
    return 0 if ans["subtype"] == "success" else 1


class Progress:
    """Best-effort progress for hansa, replaced whole while Codex events arrive."""

    def __init__(self, home: str, sid: str):
        self.path = Path(home) / "progress" / f"{sid}.json" if home else None
        self.changed, self.wrote = None, None

    def event(self, replies: int, said: str):
        if self.path is None:
            return
        last = time.time()
        said = said.strip()
        note = said.splitlines()[0] if said else ""
        if self.changed == (replies, note) and last - self.wrote < 10:
            return
        self.changed, self.wrote = (replies, note), last
        tmp = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             prefix=f".{self.path.name}.", delete=False) as f:
                tmp = Path(f.name)
                json.dump({"replies": replies, "last": last, "note": note}, f)
            os.replace(tmp, self.path)
        except (OSError, UnicodeError):
            pass  # progress must never fail a task (a full disk, for example)
        finally:
            if tmp is not None:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass


def run_codex(argv: list, env: dict, conf: dict, check: bool = False) -> int:
    """A worker run on the Codex provider, or (`check`) hansa's provider check on it: codex exec once, in srt,
    answered as Claude Code answers. A check runs in a new empty folder of its own, which is its working folder
    and, with the Codex home, its only writable place; its network is CODEX_HOSTS alone. The folder is removed
    afterwards."""
    a = parse(argv)
    sid = str(uuid.uuid4()) if check else a["--session-id"]
    name = f"check-{sid}" if check else sid  # the log files'
    # the provider's model, which hansa passes; HANSA_CODEX_MODEL and then DEFAULT_MODEL only when it passes none
    model = a.get("--model") or conf.get("HANSA_CODEX_MODEL") or DEFAULT_MODEL
    effort = a.get("--effort") or conf.get("HANSA_CODEX_EFFORT") or DEFAULT_EFFORT
    try:
        max_turns = int(a.get("--max-turns") or DEFAULT_MAX_TURNS)
    except ValueError:
        max_turns = DEFAULT_MAX_TURNS
    codex = conf.get("HANSA_CODEX_BIN") or ""
    home = conf.get("HANSA_CODEX_HOME") or ""
    logs = Path(conf.get("HANSA_CODEX_LOGS") or REPO / "logs")
    srt = conf.get("HANSA_CODEX_SRT") or shutil.which("srt", path=env.get("PATH")) or ""
    ca = ca_file(conf)

    def failed(why):
        if Run.mine:
            shutil.rmtree(Run.mine, ignore_errors=True)
        return emit(answer("error_during_execution", f"hansa-codex-shim: {why}", sid, model=model))
    for what, v in (("HANSA_CODEX_BIN", codex), ("HANSA_CODEX_HOME", home), ("srt (HANSA_CODEX_SRT)", srt)):
        if not v:
            return failed(f"{what} is not set")
    web = web_options(conf)
    if isinstance(web, str):
        return failed(web)
    tmp = env.get("TMPDIR") or tempfile.gettempdir()
    if check:  # hansa gives a check no sandbox: its own, in a new empty folder
        try:
            Run.mine = mine = wt = os.path.realpath(tempfile.mkdtemp(prefix="codex-check-", dir=tmp))
        except OSError as e:
            return failed(str(e))
        deny_read = check_deny_read(env.get("HOME") or "", os.getcwd())
        box_srt = check_settings(mine, home, deny_read)
        why = paths_problem(box_srt["filesystem"], wt, home, codex, ca)
    else:
        wt = os.getcwd()
        try:
            box = json.loads(a.get("--settings") or "{}")
        except ValueError:
            box = {}
        if not (box.get("sandbox") or {}).get("enabled"):
            return failed("no sandbox in the --settings hansa gave; Codex runs only in one")
        deny_read = (box["sandbox"].get("filesystem") or {}).get("denyRead")
        box_srt = srt_settings(box, wt, tmp, home)
        why = sandbox_problem(box_srt["filesystem"], wt, home, codex, ca)
    why = why or home_problem(home)
    if why:
        return failed(why)
    opened = home_read_open(deny_read, home)[1]
    if opened:
        sys.stderr.write(f"hansa-codex-shim: left out of srt's denyRead for this Codex run, as the Codex home or "
                         f"inside it: {', '.join(map(str, opened))}\n")
    try:
        logs.mkdir(parents=True, exist_ok=True)
        Path(home).mkdir(parents=True, exist_ok=True)
        if not check:
            # in the run's temporary folder: srt may write it
            Run.mine = mine = tempfile.mkdtemp(prefix="codex-", dir=tmp)
    except OSError as e:
        return failed(str(e))
    last = os.path.join(mine, "last-message.txt")
    lane_env = ""
    # hansa's lane env and prelude, which the sandbox may not read where it is (a check has none)
    if env.get("CLAUDE_ENV_FILE") and not check:
        try:
            lane_env = os.path.join(mine, "lane-env.sh")
            fd = os.open(lane_env, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as out:
                out.write(Path(env["CLAUDE_ENV_FILE"]).read_bytes())
        except OSError as e:
            return failed(f"the lane's env could not be read: {e}")
    sfile = logs / f"{name}.srt.json"
    sfile.write_text(json.dumps(box_srt, indent=1), encoding="utf-8")
    cenv = {k: v for k, v in env.items() if k not in DROP_VARS}
    if check:
        cenv["TMPDIR"] = mine  # its only writable place
    zsh_prefix = cenv[ZSH_PREFIX] = os.path.join(mine, "zsh")  # in TMPDIR, which srt lets the run write
    command = [srt, "--settings", str(sfile), "--", "/bin/sh", "-c", INNER, "hansa-codex", lane_env, home, ca,
               *codex_argv(codex, wt, model, effort, a.get("--append-system-prompt") or "", last, *web,
                          zsh_prefix=zsh_prefix, no_repo=check)]

    # Codex's stdout is the log file itself, never a pipe: a write to a regular file does not fail with EAGAIN,
    # whoever sets O_NONBLOCK on it (codex's print panics on that when a pipe is full). The shim follows the file.
    jsonl, err = open(logs / f"{name}.jsonl", "wb"), open(logs / f"{name}.err", "wb")
    follow = open(logs / f"{name}.jsonl", "rb", buffering=0)
    seen, said, errors, failure, usage, thread, done, over = set(), "", [], None, None, "", False, False
    progress = Progress(env.get("CLAUDE_CONFIG_DIR", "") if not check else "", sid)
    retries, warnings = [], []
    partial = b""

    def read_events(ended, stopping=False):
        nonlocal partial, said, failure, usage, thread, done, over
        if follow.closed:
            return False
        chunk = follow.read()
        lines = (partial + chunk).split(b"\n")
        partial = lines.pop()  # what follows the last newline: a line not yet complete
        if ended and partial:
            lines.append(partial)  # a last line without its newline, parsed if it is complete JSON
            partial = b""
        for line in lines:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            t, item = ev.get("type"), ev.get("item") if isinstance(ev.get("item"), dict) else {}
            if t == "thread.started":
                thread = str(ev.get("thread_id") or "")
                (logs / f"{name}.thread").write_text(thread + "\n", encoding="utf-8")
            elif t in ("item.started", "item.completed") and item.get("type") in COUNTED_ITEMS:
                seen.add(item.get("id") or f"#{len(seen)}")
                if len(seen) > max_turns:
                    over = True
            elif (t == "item.completed" and item.get("type") == "agent_message"
                  and isinstance(item.get("text"), str) and item["text"]):
                said = item["text"]
            elif t == "item.completed" and item.get("type") == "error" and item.get("message"):
                warnings.append(str(item["message"]))  # a model warning, not a failure
            elif t == "turn.completed":
                done = True
                if isinstance(ev.get("usage"), dict):
                    usage = usage_of(ev["usage"])
            elif t == "turn.failed":
                failure = str((ev.get("error") or {}).get("message") or ev.get("error") or "")
            elif t == "error":
                m = str(ev.get("message") or "")
                (retries if RETRY.match(m) else errors).append(m)
            if not stopping:
                progress.event(len(seen), said)
                if over:
                    break
        return bool(chunk)

    def on_signal(sig, _frame):  # hansa's ceiling, or a cancel: srt, codex and their commands go too
        for s in STOP_SIGNALS:
            signal.signal(s, signal.SIG_IGN)  # another stop must not restart the three-second grace
        if Run.proc is not None:
            stop_tree(Run.proc.pid, Run.proc, seen=Run.seen)
        read_events(ended=True, stopping=True)  # include events written just before or during the stop
        if seen or said or usage:
            extra = {"codex_thread_id": thread} if thread else {}
            emit(answer("error_during_execution", said, sid, len(seen), model, usage, **extra))
        shutil.rmtree(mine, ignore_errors=True)
        os._exit(128 + sig)
    for s in STOP_SIGNALS:
        signal.signal(s, on_signal)

    # A signal held back until Run.proc is set and the watchdog runs, then handled once: srt is never left behind.
    # The children start with the mask as it was.
    was = signal.pthread_sigmask(signal.SIG_BLOCK, STOP_SIGNALS)

    def unmasked():
        signal.pthread_sigmask(signal.SIG_SETMASK, was)
    try:
        try:
            proc = subprocess.Popen(command, cwd=wt, env=cenv, stdin=sys.stdin, stdout=jsonl, stderr=err,
                                    start_new_session=True, preexec_fn=unmasked)
        except OSError as e:
            follow.close()
            return failed(f"srt could not start: {e}")
        if env.get(PAUSE_AFTER_START):
            Path(env[PAUSE_AFTER_START]).write_text(str(proc.pid), encoding="utf-8")
            time.sleep(1)
        Run.proc = proc
        dog = None
        if not env.get(NO_WATCHDOG):
            try:  # in a session of its own, so that hansa's kill of this one's group leaves it to clean up
                dog = subprocess.Popen([sys.executable, "-I", os.path.abspath(__file__), WATCHDOG, str(os.getpid()),
                                        str(proc.pid)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, start_new_session=True, preexec_fn=unmasked)
            except OSError:
                pass
    finally:
        unmasked()
    sys.stderr.write(f"hansa-codex-shim: Codex's output is kept in {logs}/{name}.jsonl and .err\n")
    looked, ended = 0.0, False
    # Complete lines as they arrive; a partial last one is kept until its newline does
    while not (ended or over):
        if time.time() - looked > 2:
            looked = time.time()
            Run.seen = descendants(proc.pid, dict(Run.seen))
        ended = proc.poll() is not None  # before the read: what srt's codex wrote is then all in the file
        # The handler drains this same reader. Defer it until the offset, partial line and counts agree.
        was_reading = signal.pthread_sigmask(signal.SIG_BLOCK, STOP_SIGNALS)
        try:
            changed = read_events(ended)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, was_reading)
        if over:
            stop_tree(proc.pid, proc, seen=Run.seen)
        if not changed and not ended:
            time.sleep(POLL)
    follow.close()
    code = proc.wait()
    stop_tree(proc.pid, None, grace=0, seen=Run.seen)  # what codex left running
    if dog is not None:
        dog.kill()
        dog.wait()
    jsonl.close()
    err.close()
    try:
        report = Path(last).read_text(encoding="utf-8").strip() or said
    except OSError:
        report = said
    shutil.rmtree(mine, ignore_errors=True)
    extra = {"codex_thread_id": thread} if thread else {}
    warnings = list(dict.fromkeys(warnings))
    warned = "".join(f"\n\nCodex warned: {one_line(w)}" for w in warnings)
    if warnings:
        extra["codex_warnings"] = warnings
    if over:
        return emit(answer("error_max_turns", said + warned, sid, len(seen), model, usage, **extra))
    # killed from outside (the system's out-of-memory killer, say): no answer, and the exit of a killed process, as
    # a killed claude leaves, so that hansa runs it again
    if sig := killed_by(code):
        sys.stderr.write(f"hansa-codex-shim: the run was killed by {sig.name} (exit {code}); no answer\n")
        return 128 + sig
    if failure is None and done and code == 0:
        return emit(answer("success", report + warned, sid, len(seen), model, usage, **extra))
    # the final error: turn.failed's, else the last error event that is not a retry, else codex's stderr (a failure
    # before the start is there alone), else what the last retry said
    text = failure or (errors[-1] if errors else "")
    if not text:
        try:
            text = (logs / f"{name}.err").read_text(encoding="utf-8", errors="replace")[-4000:].strip()
        except OSError:
            text = ""
    if not text and retries:
        text = RETRY.match(retries[-1]).group(1)
    text = text or f"codex ended (exit {code}) without finishing its turn"
    classified = classify(text, home, codex)
    if REFRESH_USED_TEXT.search(text):  # another run's refresh at the same instant, or a login that is gone
        try:
            limit = float(env.get(STATUS_TIMEOUT_SETTING) or STATUS_TIMEOUT)
        except ValueError:
            limit = STATUS_TIMEOUT
        good, how = login_status(codex, home, env, limit)
        sys.stderr.write(f"hansa-codex-shim: the refresh token was already used; codex login status: {how}\n")
        if good:
            classified = {"result": f"API Error: Connection dropped (Codex: another run on the same Codex home refreshed "
                              f"the login at the same moment, and `codex login status` says the login is still "
                              f"good; run the task again: {one_line(text)})"}
        else:
            classified["result"] += f" (codex login status: {how})"
    return emit(answer("error_during_execution", classified.pop("result") + warned.replace("\n\n", " · "), sid,
                       len(seen), model, usage, **classified, **extra))


def given_env() -> dict:
    """The environment bin/claude-or-codex was given, from its snapshot (HANSA_SHIM_ENV0: NAME=VALUE, a NUL after
    each, base64): python adds to its own (LC_CTYPE; macOS's /usr/bin/python3 SDKROOT, CPATH and more). Without
    one, ours."""
    snap = os.environ.pop("HANSA_SHIM_ENV0", None)
    if snap:
        try:
            pairs = (x.partition(b"=") for x in base64.b64decode(snap).split(b"\0") if b"=" in x)
            return {os.fsdecode(k): os.fsdecode(v) for k, _, v in pairs}
        except ValueError:
            pass
    return dict(os.environ)


def main(argv: list) -> int:
    if argv[:1] == [WATCHDOG]:
        watchdog(int(argv[1]), int(argv[2]))
        return 0
    env = given_env()
    conf = settings(env)
    kind = call_kind(argv, env)
    if kind == "pass":
        pass_through(argv, env, conf)
    if kind == "resume":
        return emit(answer("error_during_execution", "hansa-codex-shim: resume is not supported on the Codex "
                                                     "provider", parse(argv).get("--resume") or ""))
    return run_codex(argv, env, conf, check=kind == "check")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
