"""The desk: settings, keys, tasks and the event log, as plain files.

Layout (everything under one directory):

    settings.json        settings: the principal's on the web page, the lanes the coordinator's (hansa lane)
    secrets/<name>.key   provider keys; written by the page, never shown
    lanes/<lane>/brief.md  standing instructions sent to every worker in a lane
    tasks/<id>/          task.md, meta.json, result.md, and what its processes wrote, keys removed
    worktrees/<lane>/    one git worktree per lane
    notes/               the coordinator's own notes, kept past a cleared session; it writes there without asking
    notes/PROJECT.md     the project's own rules, loaded with the card (it imports them); hansa never writes it after init
    events.jsonl         append-only record of everything that happened
    asks.json            requests from agents to the principal
    PAUSED               present = no new work starts (holds the reason)
    .hansa/                 runtime state: provider cooling, heartbeat, web token and port, and runs/<id>/
                         (what a task's processes write, until hansa publishes it without keys);
                         releases.json and releases/<id>.log (hansa release: the requests, the
                         principal's answers, and each release's output without keys); on Linux
                         tmp/<run>/ (the TMPDIR of a lane's setup, check or merge check, while it runs)
"""
from __future__ import annotations

import contextlib
import copy
import fcntl
import hashlib
import json
import os
import re
import secrets as _secrets
import shlex
import shutil
import site
import string
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
import unicodedata
from pathlib import Path

NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
ANSWER_WORDS = ("yes", "no")  # hansa release yes|no ID CODE is an answer, so no lane takes these names


def answer_name(name) -> bool:
    return isinstance(name, str) and name.strip().lower() in ANSWER_WORDS


def answer_names(old: dict, new: dict) -> list:
    """A lane newly named yes or no (any case): refused; one a desk had before is left for the principal to remove."""
    had = old.get("lanes") if isinstance(old.get("lanes"), dict) else {}
    lanes = new.get("lanes") if isinstance(new.get("lanes"), dict) else {}
    return [f"lane {n}: yes and no (in any case) are not lane names, as hansa release yes|no ID CODE answers a "
            "release" for n in lanes if answer_name(n) and n not in had]


ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")  # either case: npm_config_cache and http_proxy are ordinary names
# A host a lane's workers may reach during a task: registry.npmjs.org, *.pypi.org, localhost:3000
LABEL = r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
HOST = re.compile(rf"^(\*\.)?{LABEL}(\.{LABEL})*(:(?P<port>[0-9]{{1,5}}))?$")
MAX_HOSTS = 50
MAX_WRITE = 20
MAX_READ = 20
# Characters a folder the coordinator is let read may not have in its path: it goes into a permission rule,
# Edit(//path/**), where these mean something else
# and the backslash, which a pattern reads as "take the next character as it is"
RULE_BREAKERS = "()[]{}*?\\"
MAX_DENY = 50
MAX_WORDS = 200  # the words no release may send out (release_forbidden)
# The project's own rules, the coordinator's and the principal's to keep: the card imports it (@notes/PROJECT.md),
# so Claude Code loads it with the card at every start, after /clear and after compaction. hansa makes it once, at
# hansa init, and rewrites it only when asked (hansa notes archive). A missing one loads nothing and stops nothing.
PROJECT_FILE = "notes/PROJECT.md"
PROJECT_INTRO = ("<!-- This project's own rules for the coordinator: how it builds, what may never go out, who "
                 "decides what. Loaded with the card at every start; hansa changes this file only for hansa notes archive. "
                 "Keep it short. -->\n")
# Past this, hansa status says the file is long: it is part of the conversation's first message, so every turn
# rereads it. 8 KB is some 2,000 tokens, about the 200 lines Claude Code itself advises a memory file to stay under
PROJECT_LONG = 8 * 1024
# Settings only the principal saves, on the page: no command of the coordinator's changes them
PRINCIPAL_ONLY = ("releases_without_asking", "release_forbidden", "alert")
# The events that may call for an alert to the principal (alert.from_event)
ALERTED = ("release_asked", "release_waits", "release_ended", "release_void", "release_forbidden", "release_stopped",
           "ask", "sandbox_held")
# What the principal's list of releases without asking records of each lane's release target, as the page saves
# it: {lane, target, repo, base, command, after, fixed?} (after: the target it must follow, "" for none; fixed: "yes"
# for a target that runs from a fixed checkout, left out for one that runs in the repository); a target goes out without asking only while it still has
# all of them. An entry saved before targets had names holds {lane, repo, base, release}: the lane's unnamed target
# and its command; an entry without after or fixed counts only for a target that must follow none and runs in the
# repository
LISTED_AS = ("repo", "base", "command", "after", "fixed")
LISTED_KEYS = ("lane", "target", *LISTED_AS, "release")
MAX_TARGETS = 20  # named release targets on one lane
MAX_RELEASE_FILES = 100  # paths or globs on one lane's release_files
TARGET_FIELDS = ("command", "timeout_min", "after", "fixed")
# a lane's forms for the messages hansa writes into its repository; unset, hansa's own (task_message, merge_message)
MESSAGE_FIELDS = ("commit_message", "merge_message")
MESSAGE_PLACES = "{task}, {title} and {lane}"
MAX_MESSAGE = 200
# the author and committer of the commits hansa makes for a lane: the lane's identity, else the repository's own
# user.name and user.email (its local config), else hansa's own (run.lane_identity)
HANSA_IDENTITY = ("hansa", "hansa@localhost")
MAX_IDENTITY = 200
RETRY_OF = " (retry of t"  # what hansa retry adds to a task's title
# Files and folders under the home folder of the user hansa runs as that no worker command may read: logins,
# keys and tokens for other services, browsing data and messages. Nothing a build or test tool needs to read
# (not .gitconfig, .m2, .gradle, .cargo/config.toml, .npm); a name that does not exist costs nothing. The
# desk's own secrets/ and .hansa/, every provider's Claude Code folder, ~/.claude and the principal's own list
# (workers_may_not_read) are added per run (run.lane_sandbox); no lane's write list and no folder the
# coordinator is let read may reach any of them. For every system:
HOME_SECRETS = (
    ".ssh", ".aws", ".config/gh", ".claude.json", ".netrc", ".gnupg", ".docker", ".kube", ".npmrc", ".pypirc",
    # Teleport
    ".tsh", ".tbot",
    # other AI tools (.config/anthropic: the settings and keys of Anthropic's own command-line tools)
    ".codex", ".gemini", ".cursor", ".grok", ".qwen", ".continue", ".codeium", ".windsurf", ".aider",
    ".config/github-copilot", ".config/opencode", ".local/share/opencode", ".config/goose", ".config/openai",
    ".openai", ".anthropic", ".config/anthropic", ".cache/huggingface/token", ".huggingface/token", ".kaggle", ".config/kaggle",
    # cloud and hosting tools
    ".config/gcloud", ".azure", ".oci", ".config/doctl", ".config/hcloud", ".config/scw", ".config/linode-cli",
    ".aliyun", ".tccli", ".bluemix", ".fly", ".config/flyctl", ".config/netlify", ".config/vercel",
    ".local/share/com.vercel.cli", ".wrangler", ".config/.wrangler", ".config/heroku", ".railway", ".config/stripe",
    ".config/configstore/firebase-tools.json", ".s3cfg", ".boto", ".config/rclone", ".databrickscfg",
    ".config/snowflake", ".snowsql", ".pulumi/credentials.json", ".terraform.d", ".terraformrc", ".vault-token",
    ".config/sops", ".config/age", ".ngrok2", ".config/ngrok", ".sentryclirc", ".config/helm/registry",
    ".config/containers/auth.json", ".config/tailscale", ".puppetlabs/token", ".chef",
    # git and registry credentials
    ".git-credentials", ".config/git/credentials", ".cargo/credentials", ".cargo/credentials.toml",
    ".gem/credentials", ".config/hub", ".config/glab-cli", ".config/tea", ".composer/auth.json",
    ".config/composer/auth.json", ".pub-cache/credentials.json", ".config/dart/pub-credentials.json",
    ".sbt/.credentials", ".ivy2/.credentials", ".config/pypoetry/auth.toml", ".subversion/auth",
    # Maven's master password, which unlocks the passwords settings.xml holds encrypted (settings.xml itself and
    # ~/.gradle/gradle.properties also hold mirrors and proxies a build needs: the principal lists them if not)
    ".m2/settings-security.xml",
    # proxy clients' settings: they hold the proxy's server passwords or subscription links
    ".config/clash", ".config/clash.meta", ".config/mihomo", ".config/clash-verge",
    ".config/io.github.clash-verge-rev.clash-verge-rev", ".config/sing-box", ".config/v2ray", ".config/xray",
    ".config/nekoray",
    # databases' saved passwords
    ".pgpass", ".my.cnf", ".mylogin.cnf",
    # password stores and keyrings
    ".password-store", ".local/share/keyrings", ".gnome2/keyrings", ".local/share/kwalletd", ".config/op",
    ".config/1Password", ".config/Bitwarden", ".config/Bitwarden CLI", ".config/rbw", ".local/share/rbw",
    ".local/share/gopass", ".config/keepassxc", ".config/keybase", ".local/share/keybase",
    # browsers' profiles, mail and messages (Linux)
    ".mozilla", ".config/google-chrome", ".config/google-chrome-beta", ".config/chromium", ".config/BraveSoftware",
    ".config/microsoft-edge", ".config/vivaldi", ".config/opera", ".librewolf", ".zen", "snap/firefox",
    "snap/chromium", ".thunderbird", ".config/Claude", ".config/Slack", ".config/discord", ".config/Signal",
    ".local/share/TelegramDesktop", ".config/filezilla",
    # shell and REPL histories
    ".bash_history", ".zsh_history", ".zhistory", ".histfile", ".sh_history", ".zsh_sessions", ".bash_sessions",
    ".local/share/fish/fish_history", ".local/share/atuin", ".python_history", ".psql_history", ".mysql_history",
    ".sqlite_history", ".node_repl_history", ".irb_history", ".pry_history", ".rediscli_history", ".dbshell",
    ".mongodb", ".scala_history", ".Rhistory", ".lesshst")
# For macOS only: the keychains, cookies, the Claude desktop app's data, browsers' profiles, messages, mail
# and proxy clients' settings (server passwords, subscription links)
MAC_SECRETS = (
    "Library/Keychains", "Library/Cookies", "Library/HTTPStorages", "Library/Accounts",
    "Library/Application Support/com.apple.TCC", "Library/Application Support/Claude",
    "Library/Application Support/Google/Chrome", "Library/Application Support/Google/Chrome Beta",
    "Library/Application Support/Google/Chrome Canary", "Library/Application Support/Chromium",
    "Library/Application Support/BraveSoftware", "Library/Application Support/Microsoft Edge",
    "Library/Application Support/Arc", "Library/Application Support/Firefox", "Library/Application Support/Vivaldi",
    "Library/Application Support/com.operasoftware.Opera", "Library/Application Support/zen",
    "Library/Safari", "Library/Containers/com.apple.Safari", "Library/Messages", "Library/Mail",
    "Library/Containers/com.apple.mail", "Library/Group Containers/group.com.apple.notes", "Library/Thunderbird",
    "Library/Application Support/Slack", "Library/Application Support/discord", "Library/Application Support/Signal",
    "Library/Application Support/Telegram Desktop", "Library/Group Containers/group.net.whatsapp.WhatsApp.shared",
    "Library/Application Support/1Password", "Library/Group Containers/2BUA8C4S2C.com.1password",
    "Library/Application Support/Bitwarden", "Library/Application Support/Bitwarden CLI",
    "Library/Application Support/Keybase", "Library/Application Support/rclone",
    "Library/Application Support/com.vercel.cli", "Library/Application Support/pypoetry/auth.toml",
    "Library/Application Support/io.github.clash-verge-rev.clash-verge-rev",
    "Library/Application Support/com.nssurge.surge-mac", "Library/Application Support/clash-verge")
PROVIDER_KINDS = ("subscription", "compatible")
EFFORTS = ("", "low", "medium", "high", "xhigh", "max")
PRICE_FIELDS = ("in", "out", "cache_read", "cache_write", "cache_write_1h")
MANAGED_ENV = ("CLAUDE_CONFIG_DIR", "HANSA_TASK")
# A lane's env reaches only what runs in the sandbox (run.in_sandbox, run.env_file): srt, the worker's Claude
# Code and the /bin/sh scripts around a setup or check get hansa's environment. As a second line, what a
# lane's env names is closed to workers, as what hansa's own environment names is, and these names, which
# tell a loader, git, a shell or Claude Code what to run, are not a lane's to set
RUN_ENV_PREFIXES = ("LD_", "DYLD_", "GIT_", "CLAUDE_")
RUN_ENV_NAMES = ("NODE_OPTIONS", "BASH_ENV", "ENV", "SHELL", "ZDOTDIR", "HOME")
# Names sh, bash or zsh keep for themselves, read-only or special: `export UID=0` in the file a worker's Bash
# commands start with (run.env_file) stops the shell, so every command of the lane would fail; zsh's lowercase
# ones are tied to others (path is PATH) or are the shell's own tables.
# Made from the names zsh 5.9 types special or readonly in ${(t)parameters}, bash's `readonly -p` and the names
# bash 5's manual says the shell sets or reads itself, each written as `export NAME=0` in a file sourced by
# /bin/sh, /bin/bash and /bin/zsh: here are every name that stops one of them, every read-only one, and those the
# shell sets for itself and reads each line with (IFS, PWD, BASHOPTS...); the rest (PATH, LANG, TERM, HISTSIZE,
# PS1...) are ordinary values a lane may set
SHELL_ENV_NAMES = (
    "_", "aliases", "ARGC", "argv", "BASH_ALIASES", "BASH_ARGC", "BASH_ARGV", "BASH_ARGV0", "BASH_CMDS",
    "BASH_COMMAND", "BASH_LINENO", "BASH_REMATCH", "BASH_SOURCE", "BASH_SUBSHELL", "BASH_VERSINFO",
    "BASH_VERSION", "BASHOPTS", "BASHPID", "builtins", "cdpath", "commands", "dirstack", "dis_aliases",
    "dis_builtins", "dis_functions", "dis_functions_source", "dis_galiases", "dis_patchars", "dis_reswords",
    "dis_saliases", "EGID", "EPOCHREALTIME", "EPOCHSECONDS", "ERRNO", "EUID", "fignore", "fpath", "funcfiletrace",
    "FUNCNAME", "funcsourcetrace", "funcstack", "functions", "functions_source", "functrace", "galiases", "GID",
    "GROUPS", "HISTCMD", "history", "historywords", "IFS", "jobdirs", "jobstates", "jobtexts", "keymaps",
    "LINENO", "mailpath", "manpath", "module_path", "modules", "nameddirs", "OLDPWD", "OPTARG", "OPTIND",
    "options", "parameters", "patchars", "path", "PIPESTATUS", "pipestatus", "PPID", "PS4", "psvar", "PWD",
    "RANDOM", "reswords", "saliases", "SECONDS", "SHELLOPTS", "SHLVL", "signals", "SRANDOM", "status",
    "sysparams", "termcap", "terminfo", "TTYIDLE", "UID", "userdirs", "usergroups", "widgets", "ZSH_ARGZERO",
    "ZSH_EVAL_CONTEXT", "zsh_eval_context", "ZSH_NAME", "ZSH_PATCHLEVEL", "zsh_scheduled_events", "ZSH_SUBSHELL",
    "ZSH_VERSION")
SHELL_ENV_WHY = ("cannot be set for a lane: sh, bash or zsh keeps it for itself (read-only or special), and setting "
                 "it would stop every command of the lane's workers")
RUN_ENV_WHY = ("cannot be set for a lane: it tells a loader, git, a shell or Claude Code what to run; set it inside "
               "the check or setup command instead, where it runs in the sandbox")
# The variables that exist to point a tool in the sandbox at the lane's own folder: what these name may be on
# the lane's write list (the card names the cache ones)
CACHE_ENV = ("npm_config_cache", "npm_config_store_dir", "PIP_CACHE_DIR", "YARN_CACHE_FOLDER", "CARGO_HOME",
             "GOMODCACHE", "GOCACHE", "GRADLE_USER_HOME", "MAVEN_OPTS", "CCACHE_DIR", "UV_CACHE_DIR",
             "POETRY_CACHE_DIR", "PIPENV_CACHE_DIR", "BUN_INSTALL_CACHE_DIR", "DENO_DIR", "NUGET_PACKAGES",
             "COURSIER_CACHE")
XDG_OPEN = ("XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME")

# Rules every lane's workers, verifiers and design sessions are given without asking: none. A lane's allowed
# commands go to all three; workers run any command inside their sandbox and need no Bash rule, so a lane lists
# what its design sessions run (its tests, say) and the web tools its runs may use (refused_rule).
DEFAULT_ALLOW: list = []


def home_secrets() -> tuple:
    """The built-in names, under the home folder, of what no worker may read on this system."""
    return HOME_SECRETS + (MAC_SECRETS if sys.platform == "darwin" else ())


REST_MINUTES_MOST = 44640  # a provider's own rest after a usage limit: up to 31 days (a monthly allowance)


def default_settings() -> dict:
    return {
        "version": 1,
        "concurrency": 1,
        "cool_minutes": 30,
        "coordinator": {"provider": "claude", "model": "opus", "effort": "high", "audits_cost": False},
        # Design sessions, which the principal works in directly (`hansa design`). Blank provider
        # or model: the coordinator's.
        "design": {"provider": "", "model": "", "effort": ""},
        "providers": {
            "claude": {"kind": "subscription", "model": "opus", "effort": "", "config_dir": ""},
        },
        "lanes": {},
        # The command that starts a browser tool (an MCP server on stdio). The coordinator
        # gives it to one task at a time with `hansa add --browser`. Empty: no browser.
        "browser": "",
        # Folders outside the desk the coordinator (and the helpers in its session) may read without
        # asking, as it reads the lanes' repositories: full paths. The principal's, on the page.
        "coordinator_read": [],
        # Folders and files no worker, verifier, setup or check may read, besides what hansa always keeps from
        # them (desk.HOME_SECRETS and the like): full paths, a machine's own folders of keys, say. Read is
        # denied there to every Claude Code run hansa starts, the coordinator too. The principal's, on the page.
        "workers_may_not_read": [],
        # Release targets whose releases (hansa release) go out without the principal's yes on the page: each one as
        # the page saved it, {lane, target, repo, base, command, after} (target "": the lane's unnamed one, its
        # release command; after: the target it must follow, "" for none); a target that no longer has that
        # repository, base branch, command and after waits for the yes. The principal's, on the page; empty: every
        # release waits for the yes.
        "releases_without_asking": [],
        # Words or phrases no release may send out (any case): a hit in the lines the outgoing commits add or in
        # their messages refuses the release. The principal's, on the page.
        "release_forbidden": [],
        # The command hansa runs when something waits for the principal (hansa/alert.py), with when the page saved
        # it. The principal's, on the page; empty: no alerts.
        "alert": {"command": ""},
        # US dollars per million tokens. Cache prices default to the usual
        # multiples of the input price when left out.
        "prices": {
            "claude-haiku-4-5": {"in": 1, "out": 5},
            "claude-opus-5": {"in": 5, "out": 25},
        },
    }


def default_lane(repo: str, base: str = "main", providers: list | None = None) -> dict:
    return {"repo": repo, "base": base, "providers": providers or ["claude"],
            "allow": list(DEFAULT_ALLOW), "network": [], "env": {}, "max_turns": 300, "timeout_min": 240}


def now() -> float:
    return round(time.time(), 3)


def json_text(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def write_json(path: Path, data) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")  # writers never share one
    tmp.write_text(json_text(data), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _is_int(v, lo: int, hi: int) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi


def validate(s: dict, root=None, page: bool = False) -> list:
    """Every problem with a settings object, in words; empty means valid. root: the desk's folder,
    which no lane's write list may reach into. page: the page's save, which cannot change lanes, so the refused
    rules a lane's allowed commands hold are not its problems (allow_problems shows them there as warnings)."""
    p = []
    if not isinstance(s, dict):
        return ["settings must be an object"]
    known = {"version", "concurrency", "cool_minutes", "coordinator", "design", "providers", "lanes", "prices",
             "browser", "coordinator_read", "workers_may_not_read", "releases_without_asking", "release_forbidden",
             "alert"}
    p += [f"unknown setting: {k}" for k in s if k not in known]
    # which of them may be read is looked at when the page saves a changed list, and when the coordinator
    # starts (read_split): one that went bad later (removed, a link pointed elsewhere) is skipped there and
    # said on the page, and does not keep other settings or the coordinator's lane changes from saving
    given = s.get("coordinator_read", [])
    if not isinstance(given, list) or len(given) > MAX_READ or not all(isinstance(x, str) for x in given):
        p.append(f"the folders the coordinator may read must be a list of up to {MAX_READ} full paths, one per line")
    # as the read list: which of them hold is looked at when the page saves a changed list, and when a run starts
    # (deny_split), where one that no longer passes is left out and said in the task
    kept = s.get("workers_may_not_read", [])
    if not isinstance(kept, list) or len(kept) > MAX_DENY or not all(isinstance(x, str) for x in kept):
        p.append(DENY_LIST_WHY)
        kept = []
    kept = deny_split(kept, root)[0]  # whatever the lanes: a lane's repository gives way to the list, below
    listed = s.get("releases_without_asking", [])
    if not isinstance(listed, list) or len(listed) > 100 or not all(_listed_ok(x) for x in listed):
        p.append("the releases without asking must be a list of lanes and their release targets, each a lane name "
                 "and a target name (lowercase letters, digits and '-'; none for the lane's unnamed target)")
    words = s.get("release_forbidden", [])
    if not isinstance(words, list) or len(words) > MAX_WORDS or not all(
            isinstance(x, str) and x.strip() and len(x) <= 200 and not _has_control(x) for x in words):
        p.append(f"the words no release may send out must be a list of up to {MAX_WORDS} words or phrases, one per "
                 "line, each up to 200 characters")
    al = s.get("alert", {"command": ""})
    if (not isinstance(al, dict) or set(al) - {"command", "by", "at"} or not isinstance(al.get("command", ""), str)
            or len(al.get("command", "")) > 1000 or _has_control(al.get("command", ""))):
        p.append("the alert command must be one shell command line (up to 1000 characters)")
    if not isinstance(s.get("browser", ""), str) or len(s.get("browser", "")) > 1000:
        p.append("browser must be one command line (up to 1000 characters)")
    if not _is_int(s.get("concurrency"), 1, 8):
        p.append("concurrency must be a whole number from 1 to 8")
    if not _is_int(s.get("cool_minutes"), 1, 1440):
        p.append("cool_minutes must be a whole number from 1 to 1440")

    provs = s.get("providers")
    if not isinstance(provs, dict) or not provs:
        p.append("at least one provider is needed")
        provs = {}
    for name, pv in provs.items():
        at = f"provider {name}"
        if not NAME.match(name):
            p.append(f"{at}: use lowercase letters, digits and '-'")
        if not isinstance(pv, dict):
            p.append(f"{at}: must be an object")
            continue
        if {"key", "api_key", "token"} & set(pv):
            p.append(f"{at}: keys are not kept in settings; use the key box on the page")
        extra = set(pv) - {"kind", "model", "effort", "config_dir", "base_url", "note", "rest_minutes", "key", "api_key",
                           "token"}
        if extra:
            p.append(f"{at}: unknown field(s): {', '.join(sorted(extra))}")
        if pv.get("kind") not in PROVIDER_KINDS:
            p.append(f"{at}: kind must be 'subscription' or 'compatible'")
        if not isinstance(pv.get("model"), str) or not pv.get("model", "").strip():
            p.append(f"{at}: model is required")
        if pv.get("effort", "") not in EFFORTS:
            p.append(f"{at}: effort must be one of {', '.join(e or '(blank)' for e in EFFORTS)}")
        url = pv.get("base_url", "")
        if pv.get("kind") == "compatible" and url and not str(url).startswith(("https://", "http://")):
            p.append(f"{at}: the endpoint address must start with https://")
        if pv.get("rest_minutes") is not None and not _is_int(pv["rest_minutes"], 1, REST_MINUTES_MOST):
            p.append(f"{at}: rest_minutes (its rest after a usage limit) must be a whole number from 1 to "
                     f"{REST_MINUTES_MOST}, or empty for the desk's cool_minutes")
        cdir = pv.get("config_dir", "")
        if cdir and not str(cdir).startswith("/"):  # nothing expands ~ for a worker
            p.append(f"{at}: config_dir must be a full path starting with /")

    lanes = s.get("lanes")
    if not isinstance(lanes, dict):
        p.append("lanes must be an object")
        lanes = {}
    for name, ln in lanes.items():
        at = f"lane {name}"
        if not NAME.match(name):
            p.append(f"{at}: use lowercase letters, digits and '-'")
        if not isinstance(ln, dict):
            p.append(f"{at}: must be an object")
            continue
        extra = set(ln) - {"repo", "base", "providers", "allow", "network", "write", "env", "check", "merge_check",
                           "preview", "preview_wait_s", "setup", "release", "targets", "release_files", "max_turns",
                           "timeout_min", "merge_timeout_min", "note", "identity", *MESSAGE_FIELDS}
        if extra:
            p.append(f"{at}: unknown field(s): {', '.join(sorted(extra))}")
        if not isinstance(ln.get("repo"), str) or not ln.get("repo", "").startswith("/"):
            p.append(f"{at}: repo must be an absolute path")
        order = ln.get("providers")
        if not isinstance(order, list) or not order:
            p.append(f"{at}: list at least one provider")
        else:
            p += [f"{at}: unknown provider {x}" for x in order if x not in provs]
        allow = ln.get("allow", [])
        if not isinstance(allow, list):
            p.append(f"{at}: allow must be a list of rules such as Bash(pnpm test:*)")
        elif not page:  # one saved before it was refused is left out of runs; the page shows it as a warning
            p += allow_problems(name, ln)
        hosts = ln.get("network", [])
        if not isinstance(hosts, list) or len(hosts) > MAX_HOSTS or not all(is_host(h) for h in hosts):
            p.append(f"{at}: network must be a list of up to {MAX_HOSTS} host names such as registry.npmjs.org, "
                     "*.pypi.org or localhost:3000")
        if isinstance(ln.get("repo"), str) and ln["repo"].startswith("/"):
            # the coordinator sets a lane's repository: none goes where the principal keeps workers from reading
            p += [f"{at}: repo cannot be {ln['repo']}: it is {d} or inside it, which the principal keeps workers "
                  "from reading (the web page, Settings)" for d in kept if _above(d, ln["repo"])][:1]
        env = ln.get("env", {})
        p += [f"{at}: {x}" for x in write_problems(ln.get("write", []), ln.get("repo"), root, provs, kept, env,
                                                   other_envs(lanes, ln))]
        if not isinstance(env, dict):
            p.append(f"{at}: env must be an object")
        else:
            p += [f"{at}: env {k} {why}" for k, why in env_problems(env, root)]
        for field in ("check", "merge_check", "preview", "setup", "release"):
            if not isinstance(ln.get(field, ""), str) or len(ln.get(field, "")) > 1000:
                p.append(f"{at}: {field} must be one shell command line (up to 1000 characters)")
        p += [f"{at}: {f} {message_problem(ln[f])}" for f in MESSAGE_FIELDS if message_problem(ln.get(f, ""))]
        if identity_problem(ln.get("identity", "")):
            p.append(f"{at}: identity {identity_problem(ln['identity'])}")
        p += [f"{at}: {x}" for x in target_problems(ln)]
        files = ln.get("release_files", [])
        if not isinstance(files, list) or len(files) > MAX_RELEASE_FILES or not all(
                isinstance(x, str) and release_file_ok(x) for x in files):
            p.append(f"{at}: release_files must be a list of up to {MAX_RELEASE_FILES} paths or globs relative to the "
                     "repository, such as pom.xml, **/pom.xml or tools/release.sh (no leading /, no .., no spaces)")
        if not _is_int(ln.get("max_turns", 300), 1, 1000):
            p.append(f"{at}: max_turns must be from 1 to 1000")
        if not _is_int(ln.get("timeout_min", 240), 1, 1440):
            p.append(f"{at}: timeout_min must be from 1 to 1440")
        if "merge_timeout_min" in ln and not _is_int(ln["merge_timeout_min"], 1, 1440):
            p.append(f"{at}: merge_timeout_min must be from 1 to 1440")
        if "preview_wait_s" in ln and not _is_int(ln["preview_wait_s"], 1, 1800):
            p.append(f"{at}: preview_wait_s must be from 1 to 1800")

    co = s.get("coordinator")
    if not isinstance(co, dict):
        p.append("coordinator must be an object")
    else:
        if co.get("provider") not in provs:
            p.append("coordinator: unknown provider")
        if co.get("effort", "") not in EFFORTS:
            p.append("coordinator: effort is not a known level")
        if not isinstance(co.get("audits_cost", False), bool):
            p.append("coordinator: audits_cost must be true or false")
    ds = s.get("design", {})
    if not isinstance(ds, dict) or set(ds) - {"provider", "model", "effort"}:
        p.append("design must be an object with provider, model and effort")
    else:
        if ds.get("provider") and ds["provider"] not in provs:
            p.append("design: unknown provider")
        if not isinstance(ds.get("model", ""), str):
            p.append("design: model must be text")
        if ds.get("effort", "") not in EFFORTS:
            p.append("design: effort is not a known level")

    prices = s.get("prices")
    if not isinstance(prices, dict):
        p.append("prices must be an object")
    else:
        for model, pr in prices.items():
            if not isinstance(pr, dict) or "in" not in pr or "out" not in pr:
                p.append(f"price {model}: needs at least in and out")
                continue
            for k, v in pr.items():
                if k not in PRICE_FIELDS:
                    p.append(f"price {model}: unknown field {k}")
                elif not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0:
                    p.append(f"price {model}: {k} must be a number, 0 or more")
    return p


def env_paths(value, home: str) -> list:
    """The full paths a lane's env value names: its parts between `:`, spaces, `=` and `,` that start with /
    (or with ~/, the account's home folder)."""
    parts = re.split(r"[:=,\s]+", value) if isinstance(value, str) else []
    parts = [home + x[1:] if x == "~" or x.startswith("~/") else x for x in parts]
    return [_plain(x) for x in dict.fromkeys(parts) if x.startswith("/") and "\0" not in x]


def env_problems(env: dict, root=None) -> list:
    """What is wrong with a lane's env: (the name, why), one for each variable that cannot be set."""
    p, home = [], None
    for k, v in env.items():
        if not isinstance(k, str) or not ENV_NAME.match(k) or not isinstance(v, str):
            p.append((k, "must be NAME=text"))
        elif k in MANAGED_ENV or k.startswith("ANTHROPIC_"):
            p.append((k, "is set from the provider, not the lane"))
        elif k.startswith(RUN_ENV_PREFIXES) or k in RUN_ENV_NAMES:
            p.append((k, RUN_ENV_WHY))
        elif k in SHELL_ENV_NAMES:
            p.append((k, SHELL_ENV_WHY))
        else:
            home = home or account_home()
            odd = [d for d in v.split(os.pathsep) if not d.startswith("/")] if k == "PATH" else []
            inside = [d for d in env_paths(v, home) if root and _above(str(root), d)]
            if odd:  # it would be looked for in the worktree, which workers write
                p.append((k, f"cannot have the entry {odd[0]!r}: each folder on a lane's PATH must be a full path "
                             "starting with /, and none may be empty (such an entry is looked for in the "
                             "worktree, which workers write)"))
            elif inside:
                p.append((k, f"cannot name {inside[0]}: it is the desk or inside it (workers write the "
                             "worktrees; name a place in the worktree inside the check or setup command instead)"))
    return p


def other_envs(lanes, lane) -> dict:
    """The env of each other lane on the desk, by the lane's name: what any of them names is closed to this
    lane's workers too (write_problems)."""
    return {n: ln["env"] for n, ln in (lanes.items() if isinstance(lanes, dict) else ())
            if ln is not lane and isinstance(ln, dict) and isinstance(ln.get("env"), dict)}


def lane_env(env, root=None) -> tuple:
    """A lane's env as a run may be given it: (the variables that pass the checks now, a line for each left
    out). The env is checked on saving, but one saved before a check was there would still reach the lane's
    commands."""
    if not isinstance(env, dict):
        return {}, ([] if env is None else ["left out of this run: the whole env: it must be an object of "
                                            "NAME=text (the env is changed with hansa lane set)"])
    bad = dict(env_problems(env, root))
    return ({k: v for k, v in env.items() if k not in bad},
            [f"left out of this run: {k} {why} (the env is changed with hansa lane set)" for k, why in bad.items()])


def key_problem(k: str) -> str:
    """What makes this not look like a bare key; '' when it looks fine."""
    if re.search(r"\s", k):
        return "the key has spaces or line breaks in it; paste only the key"
    if k[:1] in "\"'“‘" or k[-1:] in "\"'”’":
        return "the key is wrapped in quotes; paste it without them"
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", k):
        return "this looks like NAME=key; paste only the part after ="
    if re.search(r"[*•…]|\.\.\.", k):
        return "this looks like a masked key (with * or ...); copy the full key"
    if k.lower().startswith("bearer"):
        return "leave out the word Bearer; paste only the key"
    if len(k) < 8:  # hansa looks for keys in what agents write; one this short would match ordinary text
        return "this is too short to be a key; paste the whole key"
    return ""


def is_host(h) -> bool:
    """A host name for a lane's network list, optionally *. in front and :port behind."""
    m = HOST.match(h) if isinstance(h, str) and len(h) <= 260 else None
    return bool(m) and (m["port"] is None or 0 < int(m["port"]) < 65536)


def _folds(folder: str, st) -> bool:
    """Whether the volume an existing folder is on ignores case: a name there spelled in the other case is
    the same file."""
    dev = st.st_dev
    if dev not in _FOLDS:
        found, at = None, folder
        while found is None:
            up, name = os.path.split(at)
            if up == at:  # the top, however it is spelled
                break
            try:
                if os.stat(at).st_dev != dev:
                    break
                if name.swapcase() != name:
                    found = os.path.samefile(at, os.path.join(up, name.swapcase()))
            except OSError:
                found = False if name.swapcase() != name else None
            at = up
        _FOLDS[dev] = sys.platform in ("darwin", "win32") if found is None else found
    return _FOLDS[dev]


_FOLDS: dict = {}


_LOOK = threading.local()


def _once(fn):
    """A look at a path, made once within one check of a list (_one_look): the fences ask the same of the same
    paths many times over, and the disk does not change under one check."""
    def look(path):
        memo = getattr(_LOOK, "memo", None)
        if memo is None:
            return fn(path)
        key = (fn.__name__, path)
        if key not in memo:
            memo[key] = fn(path)
        return memo[key]
    look.__name__, look.__doc__ = fn.__name__, fn.__doc__
    return look


def _one_look(fn):
    """A check of a whole list, during which each path is looked at once (_once)."""
    def check(*args, **kw):
        if getattr(_LOOK, "memo", None) is not None:
            return fn(*args, **kw)
        _LOOK.memo = {}
        try:
            return fn(*args, **kw)
        finally:
            _LOOK.memo = None
    check.__name__, check.__doc__ = fn.__name__, fn.__doc__
    return check


@_once
def _seen(path: str) -> tuple:
    """A full path as the disk sees it: which file its deepest existing folder is (os.stat, so through links
    and in any spelling), and the names below that do not exist, in one case where the volume ignores case."""
    rest = []
    while True:
        try:
            st = os.stat(path)
            break
        except (OSError, ValueError):
            up, name = os.path.split(path)
            if up == path:  # the top, however it is spelled
                return (0, 0, tuple(reversed(rest)))
            path = up
            rest.append(name)
    if rest and _folds(path, st):
        rest = [unicodedata.normalize("NFD", n).casefold() for n in rest]
    return (st.st_dev, st.st_ino, tuple(reversed(rest)))


# macOS keeps what is not the system's on a volume of its own, mounted here: /System/Volumes/Data/Users/me is
# /Users/me. Its root is another file than /, so going up from a folder spelled from / never meets it
DATA = "/System/Volumes/Data"


def _plain(path: str) -> str:
    """A full path with . and .. and doubled slashes gone: one slash at its start too (normpath keeps two)."""
    return "/" + os.path.normpath(path).lstrip("/")


@_once
def _ups(path: str) -> list:
    """A full path and each folder above it, up to /, as spelled. It ends for any path."""
    out, at = [], _plain(path)
    while True:
        out.append(at)
        up = os.path.dirname(at)
        if up == at or len(up) >= len(at):
            return out
        at = up


def _off_data(path: str) -> str:
    """A full path under macOS's data volume as the path from /; '' for any other."""
    path = _plain(path)
    for at in _ups(path):
        try:
            if os.path.samefile(at, DATA):
                return _plain("/" + path[len(at):])
        except (OSError, ValueError):
            pass
    return ""


def resolved(path: str) -> str:
    """A path with its links resolved; '' when the system does not say where they lead: a folder on the way that
    this account may not look into (/proc/1/root on Linux, but for root: Python before 3.10 raises there), or a
    loop of links."""
    try:
        return os.path.realpath(path)
    except (OSError, ValueError, RuntimeError):
        return ""


def unresolved_why(path: str) -> str:
    """'' for a path whose links can be followed; otherwise, in a line, why hansa refuses it: what the path
    opens cannot be checked."""
    return "" if resolved(path) else ("hansa cannot follow its links to see where it leads (a folder on the way "
                                      "cannot be looked into), so it cannot check what it would open")


@_once
def _forms(path: str) -> set:
    """A full path as given and with links resolved (as given only, when they cannot be followed: a list that
    opens refuses such a path itself, unresolved_why; the deny list keeps it); on macOS, one under the data volume also as the path from /."""
    out = {_plain(w) for w in (path, resolved(path)) if w}
    if sys.platform == "darwin":
        out |= {w for w in map(_off_data, out) if w}
    return out


def _in_library(path: str, lib: str) -> bool:
    """Whether a full path, as given or where its link leads, is ~/Library, above it, or inside it anywhere
    but in its Caches (where build tools keep theirs)."""
    fence, caches = _chain(lib), _seen(os.path.join(lib, "Caches"))
    libs = {_seen(w) for w in _forms(lib)}
    for v in _forms(path):
        ups = {_seen(u) for u in _ups(v)}
        if (_seen(v) in fence or libs & ups) and caches not in ups:
            return True
    return False


def _mac_fence(home: str, write: bool = False) -> list:
    """On macOS: the data volume and the folders above it hold everything, the home folder too; /Volumes
    holds a link to / and /.vol reaches any folder by its number; ~/Library holds the keychains, apps' own
    data and what runs at login. write: for a lane's write list, which takes nothing in ~/Library (its
    Caches hold what runs outside the sandbox: apps' updaters, what brew installs from); the coordinator's
    read list takes its Caches."""
    if sys.platform != "darwin":
        return []
    lib = os.path.join(home, "Library")
    held = f"it would open {lib}, which holds the keychains, apps' own data and what runs at login"
    return [(DATA, _above, f"it would open {DATA}, the volume macOS keeps everything on, the home folder too"),
            ("/Volumes", _above, "it would open /Volumes, which holds every volume, the one with the home folder too "
                                 "(a folder on another volume, such as /Volumes/Build/cache, can be listed)"),
            ("/.vol", _overlaps, "it would open /.vol, where macOS reaches any folder by its number, the home folder too"),
            (lib, _overlaps, f"{held}, and caches shared with what runs outside the sandbox (nothing in it can "
                             f"be listed; {OWN_FOLDER})") if write else
            (lib, _in_library, f"{held} (only {os.path.join(lib, 'Caches')} and folders inside it can be listed)")]


# Other names for the whole disk. Some systems reach any folder under a folder at the top: macOS by path under
# /.nofollow and /.resolve, by number under /.vol, and under /System/Volumes/Data; Linux under /proc/<pid>/root.
# Each is another file than /, so going up from a fenced folder never meets it. The class is refused, not the
# names: a first folder below / that is hidden, and the system's own folders. None is a place for a header
# library or a build cache.
SYSTEM_TOPS = ("/System", "/dev", "/proc", "/sys")
#
# What the fences cannot see: a folder is known as a fenced one (or one above it) when it is the same file, by
# device and number, as a folder met going up from the fenced one's path. The same disk mounted a second time
# somewhere else with a root that is another file (another mount of the volume, a bind mount or a share of it
# that the system shows under another device or number, a subvolume or a snapshot) is not known: a folder
# listed there opens what is under it. The principal, who alone sets these lists for reading, must not list
# one; the coordinator can put a lane's write list there only if such a mount already exists on the machine.


# What a refusal of a cache folder says to list instead
ELSEWHERE = "a cache folder goes elsewhere, such as a tools folder of the desk's, another volume or /tmp"
OWN_FOLDER = ("give the lane a folder of its own, such as ~/hansa-cache/<lane>/<tool>, and point the tool there with "
              "the lane's env: PIP_CACHE_DIR, npm_config_cache, UV_CACHE_DIR, CARGO_HOME, GRADLE_USER_HOME and the like")


def _top_hidden(path: str, _=None) -> bool:
    """Whether the first folder below / of a full path, as given or where its link leads, is hidden."""
    return any(v.startswith("/.") for v in _forms(path))


def _top_fence() -> list:
    """The folders at the top that are other names for the whole disk, or the system's own."""
    out = [("/", _top_hidden, "its first folder below / is hidden (its name starts with a dot): such a folder "
                              "can be another name for the whole disk, as /.vol, /.nofollow and /.resolve are on macOS")]
    out += [(d, _overlaps, f"it is {d} or inside it, the system's own, where the whole disk can be reached under "
                           "another name") for d in SYSTEM_TOPS]
    if sys.platform == "darwin":
        out.append(("/Library/Keychains", _overlaps, "it would open /Library/Keychains, the machine's keychains"))
    return out


# Characters that show as nothing, or as a plain space, and are not format characters (those are refused by
# their class, as are the spaces other than the plain one): the fillers, the blank braille pattern, the
# variation selectors and the others Unicode says to leave out when it has no glyph for them
INVISIBLE = ((0x034F, 0x034F), (0x115F, 0x1160), (0x17B4, 0x17B5), (0x180B, 0x180F), (0x2800, 0x2800),
             (0x3164, 0x3164), (0xFE00, 0xFE0F), (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0x1BCA0, 0x1BCA3),
             (0x1D173, 0x1D17A), (0xE0000, 0xE0FFF))


def _invisible(c: str) -> bool:
    """Whether a character is invisible or changes direction: a Unicode format character (such as U+200B,
    U+202E or U+FEFF), a space other than the plain one (such as the no-break space U+00A0), or a filler."""
    cat = unicodedata.category(c)
    return cat == "Cf" or (cat == "Zs" and c != " ") or any(a <= ord(c) <= b for a, b in INVISIBLE)


def _has_format(path: str) -> bool:
    """Whether a full path, as given or where its link leads, has an invisible or direction-changing
    character: it would not read as what it is."""
    return any(_invisible(c) for v in _forms(path) for c in v)


FORMAT_WHY = ("a name in it (or where its link leads) has an invisible or direction-changing character, so it "
              "does not read as what it is")


def stored(path: str) -> str:
    """A full path in the spelling the disk stores: each name that exists as its folder lists it (the case
    kept where the disk ignores case). Links are not followed."""
    out = "/"
    for name in _plain(path).split("/"):
        if not name:
            continue
        try:
            names = os.listdir(out)
            if name not in names:
                me = os.lstat(os.path.join(out, name))
                fold = lambda n: unicodedata.normalize("NFD", n).casefold()
                for n in [n for n in names if fold(n) == fold(name)] or names:
                    st = os.lstat(os.path.join(out, n))
                    if (st.st_dev, st.st_ino) == (me.st_dev, me.st_ino):
                        name = n
                        break
        except OSError:
            pass
        out = os.path.join(out, name)
    return out


@_once
def _chain(path: str) -> set:
    """A full path and each folder above it, as the disk sees them."""
    return {_seen(u) for v in _forms(path) for u in _ups(v)}


def _above(a: str, b: str) -> bool:
    """Whether full path a is b or a folder above it, as the disk sees them: in any spelling, case the volume
    ignores, or through a link."""
    return any(_seen(v) in _chain(b) for v in _forms(a))


def _overlaps(a: str, b: str) -> bool:
    """Whether one of two full paths is the other or inside it."""
    return _above(a, b) or _above(b, a)


def _as_written(path: str, f: str, why: str) -> str:
    """A fence's refusal of a full path, naming the fenced folder as the path spells it when the fence names it
    through a link: /usr/lib/x is in /lib where /lib is a link to /usr/lib. (Where the path itself goes through
    a link, the fence's name is where it leads.)"""
    pre, fold = f"it would open {f}", lambda n: unicodedata.normalize("NFD", n).casefold()
    if not why.startswith(pre) or not f.startswith("/"):
        return why
    at = _seen(_plain(f))
    u = next((u for u in _ups(_plain(path)) if _seen(u) == at), "")
    if not u or fold(u) == fold(_plain(f)) or _plain(resolved(u) or "/") != u:  # the same name; or u is the link
        return why
    return f"it would open {u} (the same folder as {f}){why[len(pre):]}"


def claude_home(state_dir, name: str, p: dict) -> Path:
    """Where Claude Code keeps a provider's login or settings, and its session records."""
    if p.get("kind") == "subscription":
        return Path(p.get("config_dir") or os.path.expanduser("~/.claude"))
    return Path(state_dir) / f"claude-{name}"


def release_file_ok(pattern: str) -> bool:
    """A release_files entry: a path or glob relative to the repository, on one line, without spaces."""
    parts = pattern.split("/")
    return (bool(pattern) and len(pattern) <= 300 and not pattern.startswith("/") and ".." not in parts
            and not any(c.isspace() for c in pattern) and not _has_control(pattern) and all(parts))


def _glob_re(pattern: str) -> str:
    """A release_files glob as a regular expression on a path relative to the repository: * and ? within one
    folder's name, [...] a class, **/ any folders (none too), /** everything below, ** alone anything."""
    out, i = [], 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:[^/]+/)*")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[" and "]" in pattern[i + 2:]:
            end = pattern.index("]", i + 2)
            body = pattern[i + 1:end].replace("\\", "\\\\").replace("[", "\\[")
            out.append("[" + ("^" + body[1:] if body[0] in "!^" else body) + "]")
            i = end + 1
        else:
            out.append(re.escape(c))
            i += 1
    return "".join(out)


def release_files_in(files, patterns) -> list:
    """The files (paths relative to the repository) that one of a lane's release_files names, in their order."""
    pats = [re.compile(_glob_re(x)) for x in patterns or [] if isinstance(x, str) and x]
    return [f for f in dict.fromkeys(files) if f and any(r.fullmatch(f) for r in pats)]


def lane_targets(ln) -> dict:
    """A lane's release targets, by name: its named ones (targets), or else its release command as its one unnamed
    target, named ""; each {command, timeout_min?, after?}. Empty when it has neither."""
    if not isinstance(ln, dict):
        return {}
    named = ln.get("targets")
    if isinstance(named, dict) and named:
        return {k: v for k, v in named.items() if isinstance(v, dict)}
    cmd = ln.get("release")
    return {"": {"command": cmd}} if isinstance(cmd, str) and cmd.strip() else {}


def target_name(target) -> str:
    """How a release target is named in words: 'target NAME', or '' for a lane's unnamed one."""
    return f"target {target}" if target else ""


def target_problems(ln: dict) -> list:
    """What is wrong with a lane's named release targets, in words."""
    named = ln.get("targets", {})
    if not isinstance(named, dict) or len(named) > MAX_TARGETS:
        return [f"targets must be an object of up to {MAX_TARGETS} release targets by name"]
    p = []
    if named and isinstance(ln.get("release"), str) and ln["release"].strip():
        p.append("has a release command and named release targets: the release command is the lane's one unnamed "
                 "target, so give it a name (hansa lane target L NAME \"<command>\") and take it out (hansa lane set "
                 "L release none)")
    for name, t in named.items():
        at = f"release target {name}"
        if not isinstance(name, str) or not NAME.match(name):
            p.append(f"{at}: use lowercase letters, digits and '-'")
        if not isinstance(t, dict):
            p.append(f"{at}: must be an object with command, and optionally timeout_min, after and fixed")
            continue
        extra = set(t) - set(TARGET_FIELDS)
        if extra:
            p.append(f"{at}: unknown field(s): {', '.join(sorted(map(str, extra)))}")
        cmd = t.get("command")
        if not isinstance(cmd, str) or not cmd.strip() or len(cmd) > 1000:
            p.append(f"{at}: command must be one shell command line (up to 1000 characters)")
        if "timeout_min" in t and not _is_int(t["timeout_min"], 1, 1440):
            p.append(f"{at}: timeout_min must be from 1 to 1440")
        if "fixed" in t and t["fixed"] is not True:
            p.append(f"{at}: fixed must be true (or left out)")
        named_base = t.get("fixed") is True and isinstance(cmd, str) and names_branch(cmd, ln.get("base") or "main")
        if named_base:
            p.append(f"{at}: runs from a fixed checkout of the release's commit, but its command names the base "
                     f"branch ({named_base}), which by the time it runs may have moved past the commit the principal "
                     "saw; name the commit instead: $HANSA_RELEASE_COMMIT (git push origin "
                     f"\"$HANSA_RELEASE_COMMIT:refs/heads/{ln.get('base') or 'main'}\", say), or HEAD")
        after = t.get("after")
        if after is not None and (not isinstance(after, str) or after == name or after not in named):
            p.append(f"{at}: after must name another release target of the same lane")
    for name in named:  # an order that comes back to where it began can never be kept
        seen, at = {name}, named.get(name)
        while isinstance(at, dict) and isinstance(at.get("after"), str) and at["after"] in named:
            if at["after"] in seen:
                p.append(f"release target {name}: the order of targets (after) goes round in a circle")
                break
            seen.add(at["after"])
            at = named.get(at["after"])
    return p


def names_branch(command: str, branch: str) -> str:
    """The word of a command line that names the branch as what to read or send (main, refs/heads/main, the source
    side of main:dest, and any of these followed by git's revision syntax: main~0, main^{commit}, main@{0}), '' for
    none; naming it only as where to send (HEAD:main, $X:refs/heads/main) is not reading it. Only the command line
    itself is read: a variable it expands or a script it runs is not looked into."""
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        words = list(lex)
    except ValueError:
        words = command.split()
    for w in words:
        src = re.split(r"[~^]|@\{", w.lstrip("+").split(":", 1)[0], maxsplit=1)[0]
        if src in (branch, f"refs/heads/{branch}", f"heads/{branch}"):
            return w
    return ""


def _listed_ok(x) -> bool:
    """An entry of releases_without_asking: a lane name (its unnamed target) or {lane, target} as the page sends
    it, or as recorded when the page saved it."""
    if isinstance(x, str):
        return bool(NAME.match(x))
    return (isinstance(x, dict) and set(x) <= set(LISTED_KEYS) and isinstance(x.get("lane"), str)
            and bool(NAME.match(x["lane"])) and isinstance(x.get("target", ""), str)
            and (x.get("target", "") == "" or bool(NAME.match(x["target"])))
            and all(isinstance(x.get(k, ""), str) and len(x.get(k, "")) <= 4000 for k in (*LISTED_AS, "release")))


def listed_as(ln: dict | None, target: str = "") -> dict:
    """What the principal's list of releases without asking records of a lane's release target when the page saves
    it: the lane's repository and base branch, and the target's command and the target it must follow ('' for none;
    all empty when there is no such lane)."""
    if not isinstance(ln, dict) or not isinstance(ln.get("repo"), str):
        return {"repo": "", "base": "", "command": "", "after": ""}
    t = lane_targets(ln).get(target, {})
    return {"repo": str(Path(ln["repo"]).expanduser().resolve()), "base": str(ln.get("base") or "main"),
            "command": str(t.get("command") or ""), "after": str(t.get("after") or ""),
            **({"fixed": "yes"} if t.get("fixed") else {})}


def listed_key(entry) -> tuple:
    """(lane, target) of an entry of the list; an entry without a target stands for the lane's unnamed one."""
    if isinstance(entry, str):
        return entry, ""
    if isinstance(entry, dict):
        return entry.get("lane"), entry.get("target", "")
    return None, None


def record_listed(listed, lanes: dict, before=()) -> list:
    """The page's save of the list: a lane name, or {lane, target}, it sends is recorded with what the lane and the
    target are now; an entry on the list before (before: the list as it was) is kept as it was, a bare lane name
    saved by an older page as much as a recorded entry: either is taken back in only when the page sends it as a
    tick ({lane, target}, as its button to take them back in does)."""
    if not isinstance(listed, list):
        return listed
    kept = [x for x in before if isinstance(x, str)] if isinstance(before, list) else []
    out = []
    for x in listed:
        fresh = (isinstance(x, str) and x not in kept) or (isinstance(x, dict) and set(x) <= {"lane", "target"})
        if fresh and _listed_ok(x):
            lane, target = listed_key(x)
            x = {"lane": lane, "target": target, **listed_as(lanes.get(lane), target)}
        out.append(x)
    return out


def stamp_alert(given, before) -> object:
    """The page's save of the alert command: who saved it and when, as hansa records them (not as the page sends
    them); kept as they were while the command is unchanged."""
    if not isinstance(given, dict) or not isinstance(given.get("command", ""), str):
        return given  # refused by validate
    before = before if isinstance(before, dict) else {}
    cmd = given.get("command", "").strip()
    if cmd == (before.get("command") or "").strip():
        return {"command": cmd, **{k: before[k] for k in ("by", "at") if k in before}}
    return {"command": cmd, "by": "page", "at": now()}


def listed_mismatch(s: dict, entry, zh: bool = False) -> str:
    """'' while a release target on the principal's list of releases without asking is what it was when the page
    saved the list; otherwise what is no longer so (zh: in Chinese, for the page)."""
    lane, target = listed_key(entry)
    # a target no lane has now is said as such first, whenever and however its entry was saved
    ln = s.get("lanes", {}).get(lane)
    if not ln:
        return f"工作线 {lane} 不在桌面上" if zh else f"lane {lane} is not on the desk"
    if target not in lane_targets(ln):
        if zh:
            return f"工作线 {lane} 现在没有发布目标 {target}" if target else f"工作线 {lane} 现在没有不带名字的发布命令"
        if target:
            return f"lane {lane} has no release target {target} now"
        return (f"lane {lane} has no unnamed release command now (its targets have names)" if lane_targets(ln) else
                f"lane {lane} has no release command now")
    if not isinstance(entry, dict):
        return ("保存清单时 hansa 还没记下各工作线的仓库、基础分支和发布命令" if zh else
                "the list was saved before hansa recorded each lane's repository, base branch and release command")
    if not entry.get("repo"):
        return f"保存清单时工作线 {lane} 还不在桌面上" if zh else f"lane {lane} was not on the desk when the list was saved"
    now_as = listed_as(ln, target)
    # an entry saved without after (before hansa recorded it) stands for a target that must follow none
    then = {**entry, "command": entry.get("command", entry.get("release", ""))}
    changed = [what for k, what in (("repo", ("repository", "仓库")), ("base", ("base branch", "基础分支")),
                                    ("command", ("release command", "发布命令")),
                                    ("after", ("order (the target it must follow)", "顺序（它要跟在哪个目标之后）")),
                                    ("fixed", ("way it runs (--fixed)", "运行方式（--fixed）")))
               if then.get(k, "") != now_as.get(k, "")]
    if not changed:
        return ""
    return (f"保存清单之后它的{'和'.join(w[1] for w in changed)}变了" if zh else
            f"its {' and '.join(w[0] for w in changed)} changed since the list was saved")


def listed_targets(s: dict) -> list:
    """(lane, target) of each entry on the principal's list of releases without asking, as the page shows them."""
    listed = s.get("releases_without_asking") or []
    return [listed_key(x) for x in listed if isinstance(x, (str, dict))]


def message_problem(form) -> str:
    """'' for a commit_message or merge_message hansa can use ('' is unset); otherwise why not, in one line that
    names the placeholders."""
    if form == "":
        return ""
    why = ""
    if not isinstance(form, str):
        why = "must be text"
    elif len(form) > MAX_MESSAGE:
        why = f"must be up to {MAX_MESSAGE} characters"
    elif _has_control(form):
        why = "must be one line: it cannot hold a newline"
    else:
        try:
            names = [(f, spec, conv) for _, f, spec, conv in string.Formatter().parse(form) if f is not None]
        except ValueError:
            names, why = [], "has a { or } that opens or closes no placeholder (a brace itself is {{ or }})"
        bad = ["{" + f + (f"!{conv}" if conv else "") + (f":{spec}" if spec else "") + "}" for f, spec, conv in names
               if f not in ("task", "title", "lane") or spec or conv]
        if bad:
            why = f"has an unknown placeholder {bad[0]}"
        elif not why and not {"task", "title"} & {f for f, _, _ in names}:
            why = ("gives every task the same message, or an empty one: it needs {task} or {title}"
                   if form.strip() else "gives an empty message")
    return f"{why}; the placeholders are {MESSAGE_PLACES}" if why else ""


def identity_problem(v) -> str:
    """'' for an identity git can write as a commit's author and committer, Name <email> ('' is unset); otherwise
    why not, in a few words: a name and an email in angle brackets, with no newline and no < or > inside either."""
    if v == "":
        return ""
    if not isinstance(v, str):
        return "must be text: Name <email>"
    if len(v) > MAX_IDENTITY:
        return f"must be up to {MAX_IDENTITY} characters"
    if _has_control(v):
        return "must be one line: it cannot hold a newline or another control character"
    m = re.fullmatch(r"\s*(.*?)\s*<(.*)>\s*", v)
    if not m:
        return "must be a name and an email in angle brackets: Name <email>"
    name, email = m[1], m[2].strip()
    if "<" in name or ">" in name or "<" in email or ">" in email:
        return "cannot hold < or > inside the name or the email: Name <email>"
    if not name:
        return "needs a name before the <email>"
    if not email:
        return "needs an email inside the angle brackets"
    return ""


def parse_identity(v) -> tuple | None:
    """(name, email) of an identity identity_problem passes; None for one unset or bad."""
    if not v or identity_problem(v):
        return None
    m = re.fullmatch(r"\s*(.*?)\s*<(.*)>\s*", v)
    return m[1], m[2].strip()


def own_title(title: str) -> str:
    """A title without the " (retry of t0001)" a retry adds to it (hansa retry, which cuts titles at 80
    characters, perhaps within that), for a form that names no task."""
    found = re.search(r" \(retry of t\d", title)
    if found:
        return title[:found.start()]
    cut = next((n for n in range(len(RETRY_OF), 1, -1) if title.endswith(RETRY_OF[:n])), 0) if len(title) == 80 else 0
    return title[:len(title) - cut]


def _message(ln, field: str, tid: str, title: str, lane: str) -> str | None:
    form = (ln or {}).get(field)
    if not form or message_problem(form):
        return None
    title = title if "{task}" in form.replace("{{", "") else own_title(title)
    return form.format(task=tid, title=title or "work", lane=lane or "")


def title_forms(ln) -> list:
    """The lane's commit_message and merge_message that put a task's title in what they make."""
    return [f for f in MESSAGE_FIELDS if isinstance((ln or {}).get(f), str) and (ln or {})[f]
            and not message_problem(ln[f])
            and any(n == "title" for _, n, _, _ in string.Formatter().parse(ln[f]))]


def message_fallback(ln, lane: str, field: str) -> str:
    """When a lane's commit_message or merge_message is set but cannot be used (written by hand in the settings
    file; hansa lane set refuses it), the line saying so: hansa uses its own message, task number included."""
    form = (ln or {}).get(field)
    if form is None or form == "" or not message_problem(form):
        return ""
    return (f"lane {lane}'s {field} {json.dumps(form, ensure_ascii=False)} {message_problem(form)}; hansa uses its "
            f"own message in its place, with the task number (set it right: hansa lane set {lane} {field} ...)")


def bad_messages(s: dict) -> list:
    """message_fallback of each lane's message forms, for hansa status while the setting is bad."""
    lanes = s.get("lanes") if isinstance(s.get("lanes"), dict) else {}
    return [x for n, ln in sorted(lanes.items()) for f in MESSAGE_FIELDS if (x := message_fallback(ln, n, f))]


def task_message(ln, lane: str, tid: str, title: str) -> str:
    """The message of the commit hansa makes of a task's work: the lane's commit_message, or t0001: <title>."""
    return (_message(ln, "commit_message", tid, title, lane) or f"{tid}: {title or 'work'}")[:100]


def merge_message(ln, lane: str, tid: str, title: str) -> str:
    """The message of the merge of a task into the lane's base: the lane's merge_message, or Merge t0001: <title>."""
    return _message(ln, "merge_message", tid, title, lane) or f"Merge {tid}: {title}"


def update_message(ln, lane: str, tid: str, title: str, base: str) -> str:
    """The message of the merge of the base into a task's branch (retry --update). With a commit_message it is
    named after the task's commit, which comes after it on that branch; else Merge main into task/t0001."""
    if _message(ln, "commit_message", tid, title, lane) is None:
        return f"Merge {base} into task/{tid}"
    return f"Merge {base} into the branch of: {task_message(ln, lane, tid, title)}"


def _has_control(x: str) -> bool:
    """Whether a path has a line break or another control character."""
    return any(unicodedata.category(c) in ("Cc", "Zl", "Zp") for c in x)


def account_home() -> str:
    """The home folder of the user hansa runs as, from the account (the password database), not from $HOME:
    whoever starts a hansa command sets $HOME, and a fence that followed it would stand around another
    folder than the one the logins, the settings and the programs are in."""
    try:
        import pwd
        at = pwd.getpwuid(os.getuid()).pw_dir
    except (ImportError, KeyError):
        at = ""
    return at if at and at.startswith("/") else os.path.abspath(os.path.expanduser("~"))


# The programs hansa, its server and the coordinator run outside the sandbox, besides the python hansa runs in
PROGRAMS = ("claude", "git", "srt", "node", "tmux")


def program_candidates(name: str) -> list:
    """Where hansa looks for a program it runs, in order: the first that can be run is the one."""
    if name == "claude":
        return [os.environ.get("HANSA_CLAUDE"), shutil.which("claude"), os.path.expanduser("~/.local/bin/claude")]
    if name == "tmux":
        return [os.environ.get("HANSA_TMUX"), shutil.which("tmux"), "/opt/homebrew/bin/tmux", "/usr/local/bin/tmux"]
    return [shutil.which(name)]


def program(name: str) -> str:
    """The program as hansa finds it; '' when it is not on this machine."""
    return next((c for c in program_candidates(name) if c and os.access(c, os.X_OK)), "")


def _hops(path: str) -> list:
    """A file's full path and each path its links lead through, the last with every link resolved."""
    out = [_plain(os.path.abspath(path))]
    for _ in range(40):
        try:
            to = os.readlink(out[-1])
        except (OSError, ValueError):
            break
        out.append(_plain(os.path.join(os.path.dirname(out[-1]), to)))
    real = resolved(path)
    return out if not real or _plain(real) in out else out + [_plain(real)]


def _install(path: str) -> list:
    """The folders a program at this path is installed in, when it is in a folder of versions or packages:
    the folder that holds `versions` (~/.local/share/claude), Homebrew's Cellar with the folders beside it
    that its programs load from, the folder of npm packages (node_modules: a package and the ones it needs)."""
    names = path.split("/")
    # a framework or an app: the bundle, which holds the program and what it loads
    out = ["/".join(names[:i + 1]) for i, n in enumerate(names[:-1]) if i > 1 and n.endswith((".framework", ".app"))][:1]
    for mark in ("Cellar", "node_modules", "versions"):
        if mark in names[1:-1]:
            i = names.index(mark, 1)
            top = "/".join(names[:i]) or "/"
            if mark == "versions":
                return out + [top]
            if mark == "Cellar":
                return out + [os.path.join(top, d) for d in ("Cellar", "opt", "lib", "etc", "Frameworks")]
            return out + [os.path.join(top, mark)]
    return out


_GIT: dict = {}


def _git_exec_path(git: str) -> str:
    """Where git keeps its own programs (git --exec-path), asked once for each git; '' when it does not say."""
    key = (git, os.environ.get("GIT_EXEC_PATH"))
    if key not in _GIT:
        try:
            r = subprocess.run([git, "--exec-path"], capture_output=True, text=True, errors="replace",
                               stdin=subprocess.DEVNULL, timeout=30)
            said = r.stdout.strip() if r.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            said = ""
        _GIT[key] = said if said.startswith("/") and "\n" not in said else ""
    return _GIT[key]


# Files and folders in the home folder that a program hansa or the coordinator runs outside the sandbox reads
# when it starts, and that can make it run a command: (what it is, the names)
HOME_STARTS = (
    ("a shell's startup file, which Claude Code takes the coordinator's shell settings from and runs",
     (".bashrc", ".bash_profile", ".bash_login", ".bash_logout", ".bash_aliases", ".profile", ".zshrc", ".zshenv",
      ".zprofile", ".zlogin", ".zlogout", ".kshrc", ".cshrc", ".tcshrc", ".login", ".config/fish", ".oh-my-zsh")),
    ("git's settings, where a hook or a monitor is a command git runs outside the sandbox",
     (".gitconfig", ".config/git")),
    ("tmux's settings, which run commands where the coordinator's and the design sessions' tmux starts",
     (".tmux.conf", ".tmux", ".config/tmux")),
    ("Claude Code's own files (its versions, or what it keeps between runs), which run outside the sandbox",
     (".local/share/claude", ".local/state/claude", ".cache/claude", ".cache/claude-cli-nodejs",
      "Library/Caches/claude-cli-nodejs")),
    ("a folder node loads packages from, outside the sandbox too", (".node_modules", ".node_libraries", "node_modules")),
    ("srt's settings, which say what its sandbox lets through", (".srt-settings.json",)),
    ("hansa's own files in the home folder", (".hansa",)),
)
# The system's folders of settings, libraries and programs' own programs: every program reads them when it starts
SYSTEM_STARTS = ("/etc", "/lib", "/lib64", "/usr/lib", "/usr/lib64", "/usr/libexec", "/usr/local/lib")
MAC_STARTS = ("/Library/LaunchAgents", "/Library/LaunchDaemons", "/Library/Application Support/ClaudeCode")


def _text(x) -> str:
    return x if isinstance(x, str) else ""


def _run_fence(home: str, lane_env=None) -> list:
    """What runs outside the sandbox, which no lane's write list may open: a worker that could write there
    would change what hansa, its server or the coordinator runs later, with their reach. The programs, each
    found as hansa finds it (the PATH hansa runs with, links resolved), the folder each is installed in, and
    the files each reads when it starts that can make it run a command. lane_env: the lane's env, whose
    values are read as hansa's own environment is."""
    out, beside, up = [], [], _chain(home)

    def fence(f, why, to=out):  # the file or folder, all inside it and each folder above it
        f = _plain(f)
        to = beside if f.endswith((".framework", ".app")) else to  # a bundle: after what is named inside it
        wide = len(f.split("/")) < 3 or any(_seen(v) in up for v in _forms(f))  # at the top, or the home folder
        to.append((f, _above if wide else _overlaps, f"it would open {f}, {why}"))

    def installed(name, at):
        hops = _hops(at)
        for hop in hops:  # the folders first: a link is also the file it leads to, and the refusal names the folder
            for d in _install(hop):
                fence(d, f"where {name} is installed, which runs outside the sandbox")
            folder = os.path.dirname(hop)
            if os.path.basename(folder) in ("bin", "sbin"):
                for d in ("lib", "lib64", "libexec"):
                    fence(os.path.join(os.path.dirname(folder), d),
                          f"which holds what {name} loads when it starts, outside the sandbox", beside)
        for hop in reversed(hops):
            fence(hop, f"the program {name}, which runs outside the sandbox")

    for name in PROGRAMS:
        at = program(name)
        if at:
            installed(name, at)
    git = program("git")
    own = _git_exec_path(git) if git else ""
    if own:
        fence(own, "git's own programs, which run outside the sandbox")
        top = os.path.dirname(os.path.dirname(own))
        if own.endswith("/libexec/git-core") and top != "/":
            fence(os.path.join(top, "etc", "gitconfig"), "the settings git reads for the whole system, where a "
                                                         "hook or a monitor is a command git runs")
            fence(os.path.join(top, "share", "git-core"), "git's templates and settings, which hold hooks git runs")
    # the python hansa runs in
    if sys.executable:
        installed("python", sys.executable)
        for d in (os.path.dirname(sys.executable), os.path.dirname(os.path.dirname(sys.executable))):
            fence(os.path.join(d, "pyvenv.cfg"), "which says where the python that runs hansa loads its library from")
    for d in {sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix}:
        if d.startswith("/"):  # the prefix and above; what it holds is named below (other tools may share it)
            for f in _install(os.path.join(_plain(resolved(d) or d), "bin", "python")):  # unless it is a bundle
                fence(f, "where the python that runs hansa is installed")
            out.append((_plain(d), _above, f"it would open {_plain(d)}, the prefix of the python that runs hansa"))
            for sub in ("bin", "lib", "lib64", "libexec", "pyvenv.cfg"):
                fence(os.path.join(d, sub), "part of the python that runs hansa", beside)
    fence(os.path.dirname(os.path.abspath(__file__)), "hansa's own code")
    # the folder that holds the package, whole: bin/hansa is there, which the coordinator runs
    fence(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "the folder hansa is installed in (its "
          "code and bin/hansa, which the coordinator runs outside the sandbox)")
    version = f"{sys.version_info[0]}.{sys.version_info[1]}"
    mods = [x for x in sys.path if x]
    mods += [sysconfig.get_paths().get(k) for k in ("stdlib", "platstdlib", "purelib", "platlib")]
    try:
        mods += [*site.getsitepackages(), site.getusersitepackages()]
    except (AttributeError, OSError, TypeError):  # a python started without its site module
        pass
    mods += [os.path.join(home, ".local", "lib", f"python{version}", "site-packages"),
             os.path.join(home, "Library", "Python", version, "lib", "python", "site-packages")]
    for d in dict.fromkeys(os.path.abspath(x) for x in mods if isinstance(x, str) and x.startswith("/")):
        fence(d, "which the python that runs hansa loads modules from (a site-packages folder's .pth file runs "
                 "in every python that starts)")
    # what the programs read when they start
    for why, names in HOME_STARTS:
        for n in names:
            fence(os.path.join(home, n), why)
    if sys.platform == "darwin":
        fence(os.path.join(home, "lib"), "where macOS looks for a library a program did not find, outside the sandbox too")
    for env in (os.environ.get, *([lane_env.get] if isinstance(lane_env, dict) else [])):
        for var, names, why in (("XDG_CONFIG_HOME", ("git",), HOME_STARTS[1][0]), ("XDG_CONFIG_HOME", ("tmux",), HOME_STARTS[2][0]),
                                ("XDG_CONFIG_HOME", ("fish",), HOME_STARTS[0][0]),
                                ("XDG_CACHE_HOME", ("claude", "claude-cli-nodejs"), HOME_STARTS[3][0]),
                                ("XDG_DATA_HOME", ("claude",), HOME_STARTS[3][0]), ("XDG_STATE_HOME", ("claude",), HOME_STARTS[3][0]),
                                ("ZDOTDIR", (".zshenv", ".zshrc", ".zprofile", ".zlogin", ".zlogout"), HOME_STARTS[0][0])):
            if _text(env(var)).startswith("/"):
                for n in names:
                    fence(os.path.join(env(var), n), why)
        for var, why in (("ENV", HOME_STARTS[0][0]), ("BASH_ENV", HOME_STARTS[0][0]),
                         ("GIT_CONFIG_GLOBAL", HOME_STARTS[1][0]), ("GIT_CONFIG_SYSTEM", HOME_STARTS[1][0])):
            if _text(env(var)).startswith("/"):
                fence(env(var), why)
        for var, why in (("NODE_PATH", "a folder node loads packages from, outside the sandbox too"),
                         ("LD_LIBRARY_PATH", "a folder programs load libraries from, outside the sandbox too"),
                         ("DYLD_LIBRARY_PATH", "a folder programs load libraries from, outside the sandbox too"),
                         ("DYLD_FALLBACK_LIBRARY_PATH", "a folder programs load libraries from, outside the sandbox too")):
            for d in _text(env(var)).split(os.pathsep):
                if d.startswith("/"):
                    fence(d, why)
    for d in SYSTEM_STARTS + (MAC_STARTS if sys.platform == "darwin" else ()):
        out.append((d, _overlaps, f"it would open {d}, the system's own settings, libraries or programs, which "
                                  f"every program reads when it starts, outside the sandbox too ({ELSEWHERE})"))
    return out + beside  # a folder beside a program last: what is named for what it holds says more


# Inside the home folder a lane's write list may hold only plain folders: no name between the home folder and
# the entry starts with a dot, and none is inside ~/Library or ~/Applications. The rest of the home folder is
# closed as a class, not by name: a shell's startup file can read any file there, so no list of the names that
# run outside the sandbox would be complete. A user's caches are closed with it (~/.cache, ~/Library/Caches,
# ~/.m2/repository, ~/.npm/_cacache and the rest): they are shared with what runs outside the sandbox (an
# app's updater, corepack's node, what brew installs from, the user's own builds), so what a worker writes
# there reaches it. A lane gets a cache folder of its own instead.
HOME_APPS = ("Library", "Applications")
# Plain names in the home folder that are a tool's own: what is there is shared with what runs outside the
# sandbox (the user's own builds, a shell that starts conda). These are the common ones, not a complete list:
# hansa cannot see what the user's shell, git or tools name in a plain folder.
TOOL_HOMES = {"go": "Go's default GOPATH, whose pkg/mod is the module cache the user's own builds share",
              "miniconda3": "a conda install, whose programs the user's shell runs",
              "anaconda3": "a conda install, whose programs the user's shell runs",
              "miniforge3": "a conda install, whose programs the user's shell runs",
              "mambaforge": "a conda install, whose programs the user's shell runs",
              "miniconda": "a conda install, whose programs the user's shell runs",
              "anaconda": "a conda install, whose programs the user's shell runs",
              "micromamba": "a conda install, whose programs the user's shell runs",
              "sdk": "where Go keeps the Go versions it downloads, which the user's own builds run"}
# Outside it, the places programs are installed, whole: what is there runs outside the sandbox sooner or later
INSTALL_TOPS = ("/opt/homebrew", "/home/linuxbrew/.linuxbrew", "/opt/local", "/opt/X11", "/usr", "/Applications",
                "/Library", "/nix", "/snap", "/var/lib")


def _home_closed(path: str, home: str) -> bool:
    """Whether a full path, as given or where its link leads, is inside the home folder and not a plain folder
    there."""
    homes = {_seen(w) for w in _forms(home)}
    apps = {_seen(os.path.join(w, n)) for w in _forms(home) for n in HOME_APPS}
    for v in _forms(path):
        ups = _ups(v)
        seen = [_seen(u) for u in ups]
        i = next((i for i, x in enumerate(seen) if x in homes), 0)
        if not i:  # outside the home folder, or the home folder itself (its own fence)
            continue
        names = ups[0][len(ups[i].rstrip("/")):].strip("/").split("/")
        if any(n.startswith(".") for n in names) or apps & set(seen[:i]):
            return True
    return False


def home_rule() -> str:
    """What a lane's write list may hold inside the home folder, in words."""
    return ("plain folders (no name below the home folder starts with a dot, and not inside ~/Library or "
            "~/Applications), such as ~/work/cache: a user's caches (~/.cache, ~/Library/Caches, ~/.m2/repository, "
            "~/.npm and the like) are shared with what runs outside the sandbox")


def _on_path(path: str, folder: str) -> bool:
    """Whether a full path is a folder on the PATH, above it, or inside it: a program put there is one hansa
    may run outside the sandbox. A PATH that names / closes only / (the fences for the rest stand)."""
    return _above(path, folder) or (folder != "/" and _above(folder, path))


def brew_prefixes() -> list:
    """Where Homebrew keeps what it installs, when it is on this machine: what `brew --prefix` gives, read
    from where brew is on the PATH hansa runs with (brew is <prefix>/bin/brew, or a link to
    <prefix>/Homebrew/bin/brew)."""
    out = [os.environ.get("HOMEBREW_PREFIX") or ""]
    brew = shutil.which("brew")
    for hop in _hops(brew) if brew else []:
        top = os.path.dirname(os.path.dirname(hop))
        out.append(top)
        if os.path.basename(top) == "Homebrew":  # the repository inside the prefix
            out.append(os.path.dirname(top))
    return [d for d in dict.fromkeys(_plain(d) for d in out if d.startswith("/")) if len(d.split("/")) > 2]


def _install_fence(home: str) -> list:
    """The places programs are installed, outside the home folder: none may be listed, nor a folder inside or
    above one. A home folder that is itself inside one (an account of the system's, under /var/lib) keeps its
    own rule."""
    homes = {_seen(w) for w in _forms(home)}

    def hit(path, top):
        return any(_overlaps(v, top) and not homes & {_seen(u) for u in _ups(v)[1:]} for v in _forms(path))

    why = f"where programs are installed: what is there runs outside the sandbox ({ELSEWHERE})"
    return [(d, hit, f"it would open {d}, {why}") for d in dict.fromkeys([*brew_prefixes(), *INSTALL_TOPS])]


def _breaks(path: str) -> bool:
    """Whether a name in a full path, as given or where its link leads, has a pattern character, or one that
    turns into one when the name is normalized (NFKC: the look-alike backslashes U+FF3C and U+FE68, the wide
    brackets and stars)."""
    return any(c in w for v in _forms(path) for w in (v, unicodedata.normalize("NFKC", v)) for c in RULE_BREAKERS)


BREAKS_WHY = f"a name in it (or where its link leads) has one of {RULE_BREAKERS} or a look-alike of one"


@_one_look
def write_problems(paths, repo=None, root=None, providers=None, denied=(), env=None, others=None) -> list:
    """What is wrong with a lane's write list: the folders its workers may write besides the worktree.
    None may open what the sandbox otherwise keeps closed, or what hansa runs from outside it. denied: what
    the principal keeps workers from reading (deny_split's good ones). env: the lane's env, which reaches
    the lane's commands in the sandbox: what it names is closed as what hansa's own environment names is, so
    workers cannot write where a lane's commands look. others: the env of each other lane on the desk
    (other_envs), whose PATH folders and named paths are closed the same way."""
    if not isinstance(paths, list) or len(paths) > MAX_WRITE or \
            not all(isinstance(x, str) and x.startswith("/") and "\0" not in x and len(x) <= 1000 for x in paths):
        return [f"write must be a list of up to {MAX_WRITE} full paths starting with /, such as /Users/me/hansa-cache/a/pip"]
    if not paths:
        return []
    home = account_home()
    fenced = [(home, _above, f"it would open {home}, the home folder of the user hansa runs as"),
              *_mac_fence(home, write=True)]
    if root:
        fenced.append((str(root), _overlaps, "it is the desk's, which holds the keys and hansa's state"))
    if isinstance(repo, str) and repo.startswith("/"):
        fenced.append((os.path.join(repo, ".git"), lambda a, b: _above(b, a),  # around it: the repository's
                       "it is the repository's .git folder, whose history hansa keeps"))
        fenced.append((repo, _overlaps, f"it would open {repo}, the lane's repository; workers change code "
                                        "only in their worktree"))
    for name, pv in (providers if isinstance(providers, dict) else {}).items():
        if isinstance(pv, dict) and isinstance(pv.get("config_dir", ""), str) \
                and (root or pv.get("kind") == "subscription"):
            h = str(claude_home(Path(root or "/") / ".hansa", name, pv))
            if h.startswith("/"):  # a relative config_dir is refused above
                fenced.append((h, _overlaps, f"it would open {h}, provider {name}'s Claude Code folder, "
                                             "which workers may not read"))
    mine = os.path.join(home, ".claude")  # whichever folder the providers use: its settings and hooks
    fenced.append((mine, _overlaps, f"it would open {mine}, the machine owner's Claude Code folder"))
    fenced += [(os.path.join(home, f), _overlaps, f"it would open {os.path.join(home, f)}, a login or key "
                                                  "that workers may not read") for f in home_secrets()]
    fenced += [(d, _overlaps, f"it would open {d}, which the principal keeps workers from reading") for d in denied]
    fenced += _top_fence()
    on_path = [_plain(d) for d in os.environ.get("PATH", "").split(os.pathsep) if d.startswith("/")]
    fenced += [(d, _above, f"it would open {d}, a folder on the PATH hansa runs with") for d in on_path]
    env = {k: v for k, v in env.items() if isinstance(k, str) and isinstance(v, str)} if isinstance(env, dict) else {}
    others = {n: {k: v for k, v in e.items() if isinstance(k, str) and isinstance(v, str)}
              for n, e in (others.items() if isinstance(others, dict) else ()) if isinstance(e, dict)}
    envs = [("the lane's", env), *((f"lane {n}'s", e) for n, e in others.items())]
    fenced += [(_plain(d), _on_path, f"it would open {_plain(d)}, a folder on {whose} PATH (its env), is inside it "
                                     "or above it: a lane's commands find their programs there, so a program a "
                                     "worker put there would run in their place (put a tool's bin folder on the "
                                     "PATH inside the check or setup command instead)")
               for whose, e in envs for d in e.get("PATH", "").split(os.pathsep) if d.startswith("/")]
    fenced += _run_fence(home, env)
    # after the programs hansa knows, which say more: whatever else is in a PATH folder, there or not yet
    fenced += [(d, _on_path, f"it is inside {d}, a folder on the PATH hansa runs with: a program there runs "
                             f"outside the sandbox ({ELSEWHERE})") for d in on_path]
    fenced += _install_fence(home)
    # every path the lane's env names, but those of the variables that point a tool at the lane's own folder
    fenced += [(d, _on_path, f"it would open {d}, which {whose} env names in {k} (or it is inside it): what a "
                             "lane's env names is where the lane's commands look, so it is closed to workers "
                             f"(but a cache variable such as {CACHE_ENV[2]}, {CACHE_ENV[0]} or UV_CACHE_DIR, which "
                             "may point at the lane's own folder; set the other inside the check or setup command)")
               for whose, e in envs for k, v in e.items() if k != "PATH" and k not in CACHE_ENV and k not in XDG_OPEN
               for d in env_paths(v, home)]
    fenced += [(os.path.join(home, n), _overlaps, f"it would open {os.path.join(home, n)}, {what}; {OWN_FOLDER}")
               for n, what in TOOL_HOMES.items()]
    fenced.append((home, _home_closed, f"it is inside {home}, the home folder, where a write list may hold only "
                                       f"{home_rule()}; {OWN_FOLDER}"))
    p = []
    for x in paths:
        if _has_control(x):  # as the read list: a line break would split one entry into two where it is written
            p.append(f"write cannot include {json.dumps(x)}: it has a line break or another control character")
            continue
        if _has_format(x):
            p.append(f"write cannot include {json.dumps(x)}: {FORMAT_WHY}")
            continue
        for f, hit, why in fenced:
            if hit(x, f):  # as the disk sees both: as given and with links resolved, in any case it ignores
                p.append(f"write cannot include {x}: {_as_written(x, f, why)}")
                break
        else:
            if unresolved_why(x):  # what it opens cannot be checked
                p.append(f"write cannot include {x}: {unresolved_why(x)}")
            elif _breaks(x):  # the sandbox may read it as a pattern
                p.append(f"write cannot include {x}: {BREAKS_WHY}, which the sandbox may read as a pattern and "
                         "open more than the one folder")
    return p


@_one_look
def read_split(paths, root=None, providers=None, own=(), denied=()) -> tuple:
    """The folders the principal lets the coordinator read without asking: (those it may be given, what is
    wrong with the others, one line each). None may open what hansa keeps from it: the home folder as a
    whole, the desk (its keys and state), a Claude Code folder (a provider's, and `own`: the one its own
    session runs from, and ~/.claude always), a login or key, what the principal keeps workers from reading
    (`denied`: no Claude Code run hansa starts reads it). Each is compared as the disk sees it (_above)."""
    if not isinstance(paths, list) or len(paths) > MAX_READ:
        return [], [f"the folders the coordinator may read must be a list of up to {MAX_READ} full paths, one per line"]
    home = account_home()
    fenced = [("/", _above, "it is the whole machine"),
              (home, _above, f"it would open {home}, the home folder of the user hansa runs as"), *_mac_fence(home)]
    if root:
        fenced.append((str(root), _overlaps, f"it would open {root}, the desk, which holds the keys and hansa's state"))
    for name, pv in (providers if isinstance(providers, dict) else {}).items():
        if isinstance(pv, dict) and isinstance(pv.get("config_dir", ""), str) \
                and (root or pv.get("kind") == "subscription"):
            h = str(claude_home(Path(root or "/") / ".hansa", name, pv))
            if h.startswith("/"):  # a relative config_dir is refused by validate
                fenced.append((h, _overlaps, f"it would open {h}, provider {name}'s Claude Code folder"))
    fenced += [(str(h), _overlaps, f"it would open {h}, the Claude Code folder the coordinator runs from")
               for h in own]
    mine = os.path.join(home, ".claude")  # whichever folder the providers and the session use
    fenced.append((mine, _overlaps, f"it would open {mine}, the machine owner's Claude Code folder"))
    fenced += [(os.path.join(home, f), _overlaps, f"it would open {os.path.join(home, f)}, a login or key")
               for f in home_secrets()]
    fenced += [(d, _overlaps, f"it would open {d}, which is on the list of what workers may not read") for d in denied]
    fenced += _top_fence()
    good, p, seen = [], [], set()
    for x in paths:
        at = f"the coordinator cannot be let read {x if isinstance(x, str) else json.dumps(x)}"
        if not isinstance(x, str) or not x.startswith("/") or _has_control(x) or len(x) > 1000:
            p.append(f"{at}: it must be a full path starting with /, such as /opt/include")
            continue
        if _has_format(x):
            p.append(f"the coordinator cannot be let read {json.dumps(x)}: {FORMAT_WHY}")
            continue
        why = next((_as_written(x, f, why) for f, hit, why in fenced if hit(x, f)), "") or unresolved_why(x)
        if not why and _breaks(x):
            why = f"{BREAKS_WHY}, which a permission rule cannot hold"
        if not why and not os.path.isdir(x):
            why = "it is a file, not a folder" if os.path.exists(x) else "there is no such folder"
        if why:
            p.append(f"{at}: {why}")
        elif _seen(x) not in seen:  # one folder once, however it is spelled
            seen.add(_seen(x))
            good.append(stored(x))  # as the disk spells it: what Claude Code is given, and the Edit denial
    return good, p


DENY_LIST_WHY = (f"what workers may not read must be a list of up to {MAX_DENY} full paths, one per line")
DENY_AT = "workers cannot be kept from reading "
# Folders no entry of workers_may_not_read may be or be above (deny_split): the system's programs and libraries,
# which every command runs on. One that does not exist on this system costs nothing; what is below them may be listed
SYSTEM_FOLDERS = SYSTEM_STARTS + ("/bin", "/sbin", "/lib32", "/libx32", "/usr/bin", "/usr/sbin", "/usr/lib32",
                                  "/usr/local/bin", "/usr/share", "/proc", "/nix/store", "/opt/homebrew/bin",
                                  "/opt/homebrew/lib", "/System/Library", "/Library/Developer/CommandLineTools")
# the folders Homebrew's program links (bin, opt) lead into: a single formula's folder in them may be listed
BREW_FOLDERS = tuple(f"{top}/{d}" for top in ("/opt/homebrew", "/usr/local", "/home/linuxbrew/.linuxbrew")
                     for d in ("Cellar", "opt", "Caskroom"))
# and the folders in the home folder where tools keep their settings and caches (~/Library too, on macOS)
HOME_TOOL_FOLDERS = (".config", ".local", ".cache")


def deny_split(paths, root=None, lanes=None) -> tuple:
    """deny_parts without telling apart the entries that stop runs: (those kept, a line for each of the others)."""
    return deny_parts(paths, root, lanes)[:2]


@_one_look
def deny_parts(paths, root=None, lanes=None, for_lanes=None) -> tuple:
    """The folders and files the principal keeps workers from reading: (those that are kept from them, what is
    wrong with the others, one line each, the lines of those among them that stop every run). None may close
    what a worker needs in order to run: the home folder or a folder above it, the desk or above it, a lane's
    repository or above it, a folder on the PATH or above it. Each is compared as the disk sees it (_above).
    One that does not exist yet is accepted, and so is one whose links cannot be followed (unresolved_why): it
    is denied as given, in every form hansa can work out. Left out of a run, rather than stopping it, is only an
    entry that would close a lane's repository, a worktree or a folder on the PATH, and nothing else: a
    contradiction the coordinator and the principal settle (a lane may be added, a worktree made, the PATH differ
    from where the page was saved). Any other that does not hold now (a pattern character in a name or where a
    link leads, say) was closed by the principal and cannot be applied: no run starts until it is changed.
    `for_lanes`, a list: given the lines of those left out for a lane's repository or worktree (the others are
    left out only for a folder on the PATH)."""
    if not isinstance(paths, list) or len(paths) > MAX_DENY:
        return [], [DENY_LIST_WHY], [DENY_LIST_WHY]
    home = account_home()
    fenced = [("/", _above, "it is the whole machine"),
              (home, _above, f"it is {home}, the home folder of the user hansa runs as, or a folder above it")]
    if sys.platform == "darwin":
        fenced.append((DATA, _above, f"it is {DATA}, the volume macOS keeps everything on, or a folder above it"))
    if root:
        fenced.append((str(root), _above, f"it is {root}, the desk, or a folder above it"))
    settled = set()  # the reasons for which an entry is left out of runs rather than stopping them
    for name, ln in (lanes if isinstance(lanes, dict) else {}).items():
        if isinstance(ln, dict) and isinstance(ln.get("repo"), str) and ln["repo"].startswith("/"):
            fenced.append((ln["repo"], _above, f"it is {ln['repo']}, lane {name}'s repository, or a folder above it"))
            settled.add(fenced[-1][2])
    # said along with any other reason, as which folders are on the PATH differs from machine to machine
    path_fenced = [(d, _above, f"it is {d}, a folder on the PATH hansa runs with, or a folder above it: workers "
                                "could not run their tools")
                   for d in os.environ.get("PATH", "").split(os.pathsep) if d.startswith("/")]
    fenced += [(d, _above, f"it is {d}, where the system keeps its programs and libraries, or a folder above it: "
                           "workers could not run their tools") for d in SYSTEM_FOLDERS]
    fenced += [(d, _above, f"it is {d}, where Homebrew's programs are, or a folder above it: workers could not run "
                           "their tools") for d in BREW_FOLDERS]
    fenced.append(("/dev", _overlaps, "it is /dev or inside it, the system's devices: workers' commands need /dev/null, "
                                      "/dev/tty and /dev/urandom"))
    # these leave what is inside them open to listing, so a folder on the PATH inside them is said as well
    in_parts = [(os.path.join(home, d), _above, f"it is {os.path.join(home, d)}, where tools keep their settings and "
                 "caches in the home folder, or a folder above it: name the files in it instead")
                for d in HOME_TOOL_FOLDERS + (("Library",) if sys.platform == "darwin" else ())]
    fenced += in_parts
    fenced += [(d, _above, f"it is {d}, the temporary folder, or a folder above it: workers' commands and tools "
                           "write and read there") for d in dict.fromkeys(("/tmp", "/var/tmp", tempfile.gettempdir()))]
    if root:
        wts = Path(root) / "worktrees"
        own = {wts / n for x in (lanes if isinstance(lanes, dict) else {}) for n in (x, f"{x}-design")}
        own |= set(wts.iterdir()) if wts.is_dir() else set()
        fenced += [(str(d), _above, f"it is {d}, the desk's worktrees folder or a lane's worktree in it, or a folder "
                                    "above it: workers work there") for d in [wts, *sorted(own)]]
        settled |= {f[2] for f in fenced[-1 - len(own):]}
    good, p, stops, seen = [], [], [], set()
    for x in paths:
        if not isinstance(x, str) or not x.startswith("/") or _has_control(x) or len(x) > 1000:
            p.append(f"{DENY_AT}{x if isinstance(x, str) and not _has_control(x) else json.dumps(x)}: it must be a "
                     "full path starting with /, such as /Users/me/.config/myproject/keys, with no line break or "
                     "other control character")
            stops.append(p[-1])
            continue
        if _has_format(x):
            p.append(f"{DENY_AT}{json.dumps(x)}: {FORMAT_WHY}")
            stops.append(p[-1])
            continue
        # one whose links cannot be followed is kept, denied as given: refused, it would be read
        hits = [(f, why) for f, hit, why in fenced if hit(x, f)]
        paths_in = [(d, why) for d, hit, why in path_fenced if hit(x, d)]
        # the reason said is one that covers every folder on the PATH the entry holds (/, the home folder, /usr/bin
        # for /usr), which is then not said; else the first, and the folder on the PATH with it
        said = next(((f, why) for f, why in hits if paths_in and all(_above(f, d) for d, _ in paths_in)
                     and why not in (w for _, _, w in in_parts)), None)
        on_path = "" if said else next((why for _, why in paths_in), "")
        said = said or next(iter(hits), None)
        why = "; and ".join(w for w in (said[1] if said else "", on_path) if w)
        breaks = _breaks(x)  # said first: it is what keeps the entry from being written, whatever else it would close
        if breaks:
            why = "; and ".join(w for w in (f"{BREAKS_WHY}, which the sandbox and a permission rule may read as a "
                                            "pattern", why) if w)
        if why:
            p.append(f"{DENY_AT}{x}: {why}")
            if breaks or any(w not in settled for _, w in hits):
                stops.append(p[-1])
            elif hits and for_lanes is not None:  # left out for a lane's repository or worktree, not the PATH alone
                for_lanes.append(p[-1])
        elif _seen(x) not in seen:  # one path once, however it is spelled
            seen.add(_seen(x))
            good.append(stored(x))
    return good, p, stops


def kept_from_workers(s: dict, root=None) -> tuple:
    """deny_split for a desk's settings: (what the principal keeps workers from reading, a line for each entry
    of the list that is not applied)."""
    return deny_split(s.get("workers_may_not_read", []), root, s.get("lanes"))


def no_read_parts(s: dict, root=None) -> tuple:
    """deny_parts for a desk's settings: (what the principal keeps workers from reading, a line for each entry
    left out of runs, a line for each entry that stops them)."""
    lane_held = []
    good, said, stops = deny_parts(s.get("workers_may_not_read", []), root, s.get("lanes"), lane_held)
    return good, [x + (LEFT_OUT_NOTE if x in lane_held else PATH_LEFT_OUT_NOTE) for x in said if x not in stops], stops


LEFT_OUT_NOTE = ("; left out of every run, so that folder is open to the runs of that lane and every other (workers, "
                 "verifiers, setups, checks, design sessions, the coordinator) until the principal and the coordinator "
                 "settle it")
# for one left out only as it covers a folder on the PATH: no lane is nearer to it than another
PATH_LEFT_OUT_NOTE = ("; left out of every run, so that folder is open to every run (workers, verifiers, setups, "
                      "checks, design sessions, the coordinator) until the principal and the coordinator settle it")


NO_READ_STOP = ("hansa starts no worker, verifier, design session or coordinator while an entry of what workers may "
                "not read cannot be applied, as that folder would be open to them: {lines}. The principal changes "
                "or removes it on the web page, Settings")


def no_read_stop(s: dict, root=None) -> str:
    """'' when every entry of what workers may not read can be applied or is only left out (no_read_parts);
    otherwise what to say when hansa refuses to start a run, naming each entry and why."""
    return stop_text(no_read_parts(s, root)[2])


def stop_text(stops: list) -> str:
    """What hansa says when it refuses to start a run for these entries of what workers may not read."""
    return NO_READ_STOP.format(lines="; ".join(x.removeprefix(DENY_AT) for x in stops)) if stops else ""
# Claude Code's own tools that reach the network from its own process, outside the sandbox's network list:
# WebFetch only as WebFetch(domain:HOST), WebSearch as it is. The MCP tools reach servers that run outside the
# sandbox too, and no rule holds them to listed sites: no lane lists them (hansa gives a task its browser itself).
MCP_TOOLS = ("ListMcpResourcesTool", "ReadMcpResourceTool")
WEB_FETCH_HOW = ("list each site as WebFetch(domain:HOST), with HOST a public site's name as the network list writes "
                 "it, such as WebFetch(domain:docs.python.org) or WebFetch(domain:*.python.org)")
# Names meant for this machine or a local or private network: WebFetch, outside the sandbox, would reach them
LOCAL_SUFFIXES = ("localhost", "local", "internal", "lan", "home.arpa", "arpa", "test", "invalid", "example",
                  "localdomain")
# Well-known names that come back as an address written in them (127.0.0.1.nip.io) or as this machine (lvh.me),
# with every name under them
LOOPBACK_NAMES = ("nip.io", "sslip.io", "xip.io", "traefik.me", "localtest.me", "lvh.me", "vcap.me", "lacolhost.com")
# Common suffixes under which every name belongs to someone else: a wildcard over one opens all of them
SHARED_SUFFIXES = frozenset((
    "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "com.au", "net.au", "org.au", "co.jp", "ne.jp", "or.jp",
    "com.cn", "net.cn", "org.cn", "com.hk", "com.tw", "com.sg", "co.kr", "co.nz", "co.in", "co.za", "com.br",
    "com.mx", "com.ar", "com.tr", "co.il", "co.id", "com.my", "com.ph", "com.vn", "com.ru",
    "github.io", "gitlab.io", "pages.dev", "workers.dev", "vercel.app", "netlify.app", "herokuapp.com",
    "appspot.com", "web.app", "firebaseapp.com", "blogspot.com", "cloudfront.net", "amazonaws.com",
    "azurewebsites.net", "ngrok.io", "ngrok-free.app", "onrender.com", "fly.dev", "glitch.me", "repl.co",
    "s3.amazonaws.com", "r2.dev", "surge.sh", "readthedocs.io"))


def split_rules(text: str) -> list:
    """The rules Claude Code reads out of one value of --allowedTools or --disallowedTools, split as it splits
    them: on commas and spaces outside parentheses, where being inside is an on/off flag that every ( turns on and
    every ) turns off, not a depth. So Bash(echo $(true) WebFetch ok) reaches it as Bash(echo $(true), WebFetch
    and ok); Bash(echo $(date):*) and Read(//x/Application Support/**) as themselves."""
    out, cur, inside = [], "", False
    for c in text:
        if c in "()":
            inside, cur = c == "(", cur + c
        elif c in ", " and inside:
            cur += c
        elif c == ",":
            out, cur = out + [cur.strip()] if cur.strip() else out, ""
        elif c == " ":
            if cur.strip():
                out, cur = out + [cur.strip()], ""
        elif cur or c.strip():
            cur += c
    if cur.strip():
        out.append(cur.strip())
    return out


def one_rule(rule) -> bool:
    """Whether a rule reaches Claude Code as itself and nothing else (split_rules): a tool name, then at most one
    parenthesised part that closes at the rule's last character, and no control characters."""
    if not isinstance(rule, str) or _has_control(rule):
        return False
    m = re.match(r"[A-Za-z_][A-Za-z0-9_]*", rule)
    rest = rule[m.end():] if m else None
    if rest is None or rest and (len(rest) < 3 or rest[0] != "(" or rest[-1] != ")"):
        return False
    return split_rules(rule) == [rule]


def path_rule_problem(path) -> str:
    """Why a folder cannot be closed or opened to Claude Code's tools by a rule hansa writes for it
    (Read(//PATH/**), Read(//PATH)): the rule would reach Claude Code as several (split_rules), and a deny rule
    split so leaves the folder open; '' for a path that makes one rule. Such a path must move: hansa never drops
    the rule and runs on."""
    p = str(path)
    if all(one_rule(f"Read(//{p.lstrip('/')}{end})") for end in ("", "/**")):
        return ""
    return (f"the path {p} would make rules for Claude Code's tools (such as Read(//{p.lstrip('/')}/**)) that it "
            f"reads as more than one, as after a ')' it splits them on commas and spaces, so a rule closing that "
            f"folder would not close it: move it to a path with no ')' followed later by a space or a comma")


# Characters srt's denyRead and Claude Code's Read rules read as a pattern in a path, not as themselves: a rule for
# a/[1]/desk/secrets matches a/1/desk/secrets and leaves the real folder open. { } and ( ) match as themselves
# there (a ')' that would split a rule is path_rule_problem's)
PATTERN_CHARS = "[]*?\\"


def pattern_chars(path) -> str:
    """The PATTERN_CHARS a full path has, as given or where its links lead, or as a look-alike of one (the wide
    brackets, star and question mark, the look-alike backslashes); '' for none."""
    forms = [w for v in _forms(str(path)) for w in (v, unicodedata.normalize("NFKC", v))]
    return "".join(c for c in PATTERN_CHARS if any(c in w for w in forms))


def pattern_path_problem(path, what: str, closes: str) -> str:
    """Why hansa may not write rules under `path` (`what` it is, the rules that would then close nothing:
    `closes`): it has one of PATTERN_CHARS (pattern_chars); '' for a path with none. Such a path must move: hansa
    escapes nothing and starts nothing with such a rule."""
    found = pattern_chars(path)
    if not found:
        return ""
    real = resolved(str(path)) or str(path)
    at = str(path) if real == str(path) else f"{path} (where its links lead: {real})"
    return (f"{what} {at} has {' '.join(found)} in it, which the sandbox's deny list and Claude Code's Read rules "
            f"read as a pattern, not as itself, so the rules that close {closes} would match another folder and "
            f"leave it open: move it to a path with none of {' '.join(PATTERN_CHARS)}")


def desk_paths_problem(desk, homes=(), repos=()) -> str:
    """pattern_path_problem for the folders hansa writes deny rules under for every run and the coordinator: the
    desk's (as given and where its links lead), each home folder the logins list is made in, and the lanes'
    repositories (`repos`: (lane, path)); '' for none."""
    for x in dict.fromkeys([str(desk.given), str(desk.root)]):  # as given first: it says where its links lead
        why = pattern_path_problem(x, "the desk", "its secrets/, .hansa/, notes/ and CLAUDE.md")
        if why:
            return why
    for h in dict.fromkeys(str(h) for h in homes if h):
        why = pattern_path_problem(h, "the home folder", "the logins and keys in it that no run may read")
        if why:
            return why
    for name, r in repos:
        why = pattern_path_problem(r, f"lane {name}'s repository", "git's data in it")
        if why:
            return why
    return ""


def lane_repos(s: dict) -> list:
    """(lane, repository) for each lane of a desk's settings that names one."""
    lanes = s.get("lanes") if isinstance(s.get("lanes"), dict) else {}
    return [(n, ln["repo"]) for n, ln in lanes.items() if isinstance(ln, dict) and isinstance(ln.get("repo"), str)]


def run_homes(env=None) -> list:
    """The home folders hansa makes the logins list in: the account's (account_home) and $HOME."""
    env = os.environ if env is None else env
    return list(dict.fromkeys(h for h in (account_home(), env.get("HOME") or os.path.expanduser("~")) if h))


def rules_problem(rules) -> str:
    """Why hansa may not start Claude Code with these rules: one would reach it as several (one_rule), named with
    the path it was made for (path_rule_problem); '' when each reaches it as itself."""
    for r in rules:
        if one_rule(r):
            continue
        m = re.match(r"^[A-Za-z_]+\(//(.*?)(/\*\*)?\)$", r) if isinstance(r, str) else None
        if m and path_rule_problem("/" + m.group(1)):
            return path_rule_problem("/" + m.group(1))
        return f"the rule {r} would reach Claude Code as more than one rule, as it splits them on commas and spaces"
    return ""


def argv_rules_problem(argv) -> str:
    """rules_problem for the rules of a Claude Code command line: the values after --allowedTools and
    --disallowedTools, up to the next option."""
    rules, on = [], False
    for x in argv:
        if x in ("--allowedTools", "--disallowedTools"):
            on = True
        elif x.startswith("--"):
            on = False
        elif on:
            rules.append(x)
    return rules_problem(rules)


ONE_RULE_WHY = ("would reach Claude Code as more than one rule, as it splits the list on commas and spaces outside "
                "parentheses, and every ')' closes them: give each rule as its own value, a tool name with at most "
                "one (...) part that closes at the end, with no ')' before a later space or comma")


def web_host_problem(host: str) -> str:
    """Why WebFetch may not be held to host, as it is not one public site's name: an address, this machine, a
    local or private network, or a wildcard over a suffix others share; '' for one it may."""
    name = host.lower()
    wild = name.startswith("*.")
    bare = name[2:] if wild else name
    last = bare.rstrip(".").rsplit(".", 1)[-1]
    if ("[" in name or ":" in name and re.fullmatch(r"[0-9a-f:.*]+", name)
            or re.fullmatch(r"0x[0-9a-f]*|[0-9]+", last.split(":")[0])):  # as a URL reads an IP address
        return ("is an address, not a site's name: WebFetch runs in Claude Code's own process, outside the sandbox, "
                "where an address can reach this machine or a private network; " + WEB_FETCH_HOW)
    if not is_host(host) or name.endswith(".") or ":" in name:
        return "is not a site's name alone; " + WEB_FETCH_HOW + " (no port)"
    local = next((x for x in LOCAL_SUFFIXES if bare == x or bare.endswith("." + x)), "")
    if local == "localhost":
        return ("names this machine, which WebFetch would reach from Claude Code's own process, outside the "
                "sandbox; " + WEB_FETCH_HOW)
    if local:
        return (f"is a name under .{local}, meant for a local or private network, which WebFetch would reach from "
                f"Claude Code's own process, outside the sandbox; " + WEB_FETCH_HOW)
    back = next((x for x in LOOPBACK_NAMES if bare == x or bare.endswith("." + x)), "")
    if back:
        return (f"is a name under {back}, which comes back as the address written in it or as this machine, which "
                f"WebFetch would reach from Claude Code's own process, outside the sandbox; " + WEB_FETCH_HOW)
    if not wild and "." not in bare:
        return ("is a name with no dot, which a resolver may complete to one on a local or private network, which "
                "WebFetch would reach from Claude Code's own process, outside the sandbox; " + WEB_FETCH_HOW)
    if wild and ("." not in bare or bare in SHARED_SUFFIXES):
        return (f"is a wildcard over the shared suffix .{bare}: every name under it belongs to someone else; "
                + WEB_FETCH_HOW)
    return ""


def web_rule_problem(rule: str) -> str:
    """Why an allowed-command rule cannot be on a lane's list, as it reaches the network outside the sandbox
    without being held to listed public sites; '' for one that may."""
    m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)(?:\((.*)\))?$", rule.strip())
    tool, arg = (m.group(1), m.group(2)) if m else ("", None)
    if tool == "WebFetch":
        host = arg[len("domain:"):] if arg and arg.startswith("domain:") else None
        if host is None or not host.strip("*.") or host in ("*", "*.*"):
            return ("lets a worker fetch any site from Claude Code's own process, outside the sandbox's network "
                    "list; " + WEB_FETCH_HOW)
        return web_host_problem(host)
    if tool.startswith("mcp__") or tool in MCP_TOOLS:
        return ("reaches an MCP server, which runs outside the sandbox and is not held to listed sites; a task that "
                "needs a browser gets one with hansa add --browser")
    return ""


GIT_RULE_WHY = ("runs git, and git is handled by hansa (workers can already look at status, diffs and history, and "
                "hansa commits for them)")


def refused_rule(rule) -> str:
    """Why a rule cannot be on a lane's allowed commands: it is not one rule (one_rule), runs git, or reaches the
    network unheld (web_rule_problem); '' for one that may. Runs leave out one saved before it was refused."""
    if not isinstance(rule, str) or not one_rule(rule):
        return ONE_RULE_WHY
    return GIT_RULE_WHY if is_git_rule(rule) else web_rule_problem(rule)


def allow_fix(name: str, allow: list) -> str:
    """The coordinator's command that leaves a lane's allowed commands without the rules refused_rule refuses."""
    rest = [r for r in dict.fromkeys(r for r in allow if isinstance(r, str)) if not refused_rule(r)]
    return f"hansa lane set {name} allow " + (" ".join(shlex.quote(r) for r in rest) or "none")


def allow_problems(name: str, ln: dict) -> list:
    """The refused rules a lane's allowed commands hold (refused_rule), as one line naming each and the coordinator's
    command that removes them; [] for none. On the page it is a warning: only the coordinator changes lanes."""
    allow = ln.get("allow", []) if isinstance(ln, dict) else []
    if not isinstance(allow, list):
        return []
    bad = [(r, refused_rule(r)) for r in allow if refused_rule(r)]
    if not bad:
        return []
    fix = allow_fix(name, allow)
    return [f"lane {name}: " + "; ".join(f"allow {r if isinstance(r, str) else json.dumps(r)} {why}" for r, why in bad)
            + f"; runs leave {'it' if len(bad) == 1 else 'them'} out, and the coordinator removes "
            f"{'it' if len(bad) == 1 else 'them'} with: {fix}"]


def lane_warnings(s: dict) -> list:
    """What the page shows of the lanes' refused rules (allow_problems): it saves the rest all the same."""
    lanes = s.get("lanes") if isinstance(s.get("lanes"), dict) else {}
    return [x for name, ln in lanes.items() for x in allow_problems(name, ln)]


def web_sites(allow: list) -> tuple:
    """What a lane's allowed commands give of Claude Code's web tools: (the sites WebFetch reaches, whether
    WebSearch is given). Rules refused_rule refuses give nothing."""
    sites = [r[len("WebFetch(domain:"):-1] for r in allow
             if isinstance(r, str) and r.startswith("WebFetch(domain:") and not refused_rule(r)]
    return sites, any(isinstance(r, str) and re.match(r"^WebSearch(\(.*\))?$", r) and not refused_rule(r)
                      for r in allow)


def is_git_rule(rule: str) -> bool:
    """Whether an allowed-command rule runs git anywhere in it: also behind env, sudo, a variable,
    a path or a shell's -c."""
    m = re.match(r"^Bash\((.*)\)$", rule.strip())
    return any(w.strip("'\"").rsplit("/", 1)[-1].split(":", 1)[0] == "git" for w in (m.group(1).split() if m else []))


class Desk:
    def __init__(self, root) -> None:
        self.root = Path(root).resolve()
        self.given = Path(os.path.abspath(os.path.expanduser(str(root))))  # as it was spelled, links not followed
        self.settings_path = self.root / "settings.json"
        self.secrets_dir = self.root / "secrets"
        self.tasks_dir = self.root / "tasks"
        self.lanes_dir = self.root / "lanes"
        self.worktrees_dir = self.root / "worktrees"
        self.notes_dir = self.root / "notes"
        self.project_path = self.root / PROJECT_FILE
        self.state_dir = self.root / ".hansa"
        self.events_path = self.root / "events.jsonl"
        self.asks_path = self.root / "asks.json"
        self.paused_path = self.root / "PAUSED"
        self._mutex = threading.RLock()  # the desk lock, within this process
        self._held = 0
        self._lock_file = None

    @classmethod
    def find(cls, explicit: str | None = None) -> "Desk":
        if explicit or os.environ.get("HANSA_DESK"):
            return cls(explicit or os.environ["HANSA_DESK"])
        here = Path.cwd().resolve()
        for d in (here, *here.parents):
            if (d / "settings.json").is_file() and (d / "tasks").is_dir():
                return cls(d)
        raise SystemExit("hansa: no desk here. Run inside a desk, pass --desk DIR, or set HANSA_DESK.")

    # -- settings ---------------------------------------------------------
    def settings(self) -> dict:
        s = default_settings()
        loaded = read_json(self.settings_path, {})
        if isinstance(loaded, dict):
            s.update(loaded)
        for ln in (s["lanes"].values() if isinstance(s.get("lanes"), dict) else []):
            # a merge_check of only spaces, written by hand, is unset, as hansa lane set makes it: never a command
            if isinstance(ln, dict) and isinstance(ln.get("merge_check"), str) and not ln["merge_check"].strip():
                del ln["merge_check"]
        return s

    def settings_rev(self) -> str:
        """Changes whenever settings.json does, so a page saves only over the settings it loaded."""
        try:
            return hashlib.sha256(self.settings_path.read_bytes()).hexdigest()[:16]
        except OSError:
            return ""

    def save_settings(self, new: dict, by: str = "", rev: str | None = None) -> list:
        """rev: the settings_rev() the change was made on; a save over newer settings is refused."""
        with self.lock():
            if rev is not None and rev != self.settings_rev():
                return ["the settings changed since this page loaded them (another tab or a command); "
                        "press Reload, then make the change again"]
            old = self.settings()
            if by != "page":  # the principal's alone: a command that changes them changes nothing
                theirs = [k for k in PRINCIPAL_ONLY if new.get(k, []) != old.get(k, [])]
                if theirs:
                    return [f"{k} is the principal's, set only on the web page (Settings); ask with hansa ask"
                            for k in theirs]
            else:  # each lane the page lists, recorded as it is now: it goes out without asking only while it still is
                lanes = new.get("lanes") if isinstance(new.get("lanes"), dict) else {}
                new = {**new, "releases_without_asking": record_listed(new.get("releases_without_asking", []), lanes,
                                                                       old.get("releases_without_asking")),
                       "alert": stamp_alert(new.get("alert", old.get("alert")), old.get("alert"))}
            # the page cannot change lanes: a refused rule one holds is the coordinator's to remove, not the page's
            page = by == "page" and new.get("lanes") == old.get("lanes")
            problems = (validate(new, self.root, page=page) + self.keys_in_release(old, new)
                        + self.keys_in_alert(old, new) + answer_names(old, new))
            if problems:
                return problems
            changed = sorted(k for k in set(old) | set(new) if old.get(k) != new.get(k))
            write_json(self.settings_path, new)
        self.event("settings_saved", by=by, changed=changed)
        self.ready_news()
        return []

    def keys_in_release(self, old: dict, new: dict) -> list:
        """A new or changed release command that holds a key the desk knows: refused, as the command is stored and
        shown as written (in the settings, on the page, in hansa release)."""
        lanes, before = new.get("lanes"), old.get("lanes") or {}
        out = []
        for name, ln in (lanes.items() if isinstance(lanes, dict) else []):
            was = lane_targets(before.get(name))
            for target, t in lane_targets(ln).items():
                cmd = t.get("command")
                if not isinstance(cmd, str) or cmd == (was.get(target) or {}).get("command"):
                    continue
                found = self.keys_in(cmd)
                if found:
                    out.append(f"lanes.{name}: the release command{' of target ' + target if target else ''} holds "
                               f"the key of {', '.join(found)}, and a release command is stored and shown as written; "
                               "have the command read the key from where the tool keeps it (its own login or "
                               "configuration file, or a file the command reads) instead")
        return out

    def keys_in_alert(self, old: dict, new: dict) -> list:
        """A new or changed alert command that holds a key the desk knows: refused, as the command is stored and
        shown as written (in the settings, on the page, in hansa explain)."""
        cmd = (new.get("alert") or {}).get("command") if isinstance(new.get("alert"), dict) else None
        if not isinstance(cmd, str) or cmd == ((old.get("alert") or {}).get("command")):
            return []
        found = self.keys_in(cmd)
        return [f"the alert command holds the key of {', '.join(found)}, and it is stored and shown as written; have "
                "the command read the key from a file instead"] if found else []

    # -- keys -------------------------------------------------------------
    def key_file(self, provider: str) -> Path:
        if not NAME.match(provider):
            raise ValueError(f"bad provider name: {provider!r}")
        return self.secrets_dir / f"{provider}.key"

    def has_key(self, provider: str) -> bool:
        f = self.key_file(provider)
        return f.is_file() and f.stat().st_size > 0

    def read_key(self, provider: str) -> str:
        return self.key_file(provider).read_text(encoding="utf-8").strip()

    def write_key(self, provider: str, value: str, by: str = "") -> None:
        f = self.key_file(provider)
        self.secrets_dir.mkdir(mode=0o700, exist_ok=True)
        os.chmod(self.secrets_dir, 0o700)
        if not value.strip():
            with self.lock():
                f.unlink(missing_ok=True)
            self.event("key_removed", provider=provider, by=by)
            self.ready_news()
            return
        problem = key_problem(value.strip())
        if problem:
            raise ValueError(problem)
        with self.lock():  # a provider check compares and records under the same lock
            tmp = f.with_name(f.name + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(value.strip() + "\n")
            os.replace(tmp, f)
            self.set_provider_state(provider, cool_until=0, reason="")  # a new key ends any rest
            self.scrub(value.strip(), provider)
        self.event("key_set", provider=provider, by=by)
        self.ready_news()

    PUBLISHED = ("task.md", "result.md", "run.json", "run.err", "setup.log", "check.log", "meta.json", "merge-check.log",
                 "merge-setup.log", "merge-check.err")

    def scrub(self, key: str, provider: str) -> None:
        """Take a newly saved key out of what hansa has already written down (text that was not a key
        when it was written): the task folders, the requests and the lanes' kept try output. The event
        log only ever grows, so it keeps it."""
        tasks = self.tasks_dir.iterdir() if self.tasks_dir.is_dir() else []
        lanes = list(self.lanes_dir.iterdir()) if self.lanes_dir.is_dir() else []
        for f in [self.asks_path, *(d / name for d in tasks for name in self.PUBLISHED),
                  *(d / name for d in lanes for name in ("try.log", "merge-check-try.log"))]:
            try:
                text = f.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if key in text:
                text = text.replace(key, f"[key of {provider}]")
                write_json(f, json.loads(text)) if f.name in ("meta.json", "asks.json") \
                    else f.write_text(text, encoding="utf-8")

    def raw_dir(self, tid: str) -> Path:
        """Where a task's processes write before hansa removes keys from what they wrote: under
        .hansa/, which no agent may read. hansa publishes it into the task's folder when they end."""
        d = self.state_dir / "runs" / tid
        d.mkdir(parents=True, exist_ok=True)
        return d

    def keys(self) -> dict:
        """Every stored key by provider name, for checking text against; never shown."""
        out = {}
        for f in sorted(self.secrets_dir.glob("*.key")) if self.secrets_dir.is_dir() else []:
            try:
                k = f.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if len(k) >= 8:  # shorter strings would match ordinary text
                out[f.stem] = k
        return out

    def keys_in(self, text: str) -> list:
        """Names of the providers whose key appears in this text."""
        return sorted(n for n, k in self.keys().items() if k in text)

    def redact(self, text: str) -> str:
        for n, k in self.keys().items():
            text = text.replace(k, f"[key of {n}]")
        return text

    # -- log, lock, pause -------------------------------------------------
    def event(self, kind: str, **fields) -> None:
        def clean(v):
            return self.redact(v) if isinstance(v, str) else [clean(x) for x in v] if isinstance(v, list) else v
        line = json.dumps({"ts": now(), "event": kind, **{k: clean(v) for k, v in fields.items()}}, ensure_ascii=False)
        with open(self.events_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        if kind in ALERTED:  # what waits for the principal, for the principal's own alert command (hansa/alert.py)
            from .alert import from_event
            try:
                from_event(self, kind, fields)
            except Exception as e:  # noqa: BLE001 - an alert never keeps an event from being recorded
                with open(self.events_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"ts": now(), "event": "alert_error", "error": self.redact(str(e))[:300]},
                                        ensure_ascii=False) + "\n")

    @contextlib.contextmanager
    def lock(self):
        """The desk lock: one holder at a time across processes and threads; a holder may take it again."""
        with self._mutex:
            if not self._held:
                if os.environ.get("HANSA_MERGING"):  # waiting would never end: the merge running the hook holds it
                    raise SystemExit("hansa: this runs in a git hook of a hansa merge, which holds the desk until the merge "
                                     "ends; commands that change the desk cannot run here")
                self.state_dir.mkdir(exist_ok=True)
                self._lock_file = open(self.state_dir / "lock", "w")
                fcntl.flock(self._lock_file, fcntl.LOCK_EX)
            self._held += 1
            try:
                yield
            finally:
                self._held -= 1
                if not self._held:
                    fcntl.flock(self._lock_file, fcntl.LOCK_UN)
                    self._lock_file.close()
                    self._lock_file = None

    def paused(self) -> str | None:
        try:
            return self.paused_path.read_text(encoding="utf-8").strip() or "paused"
        except FileNotFoundError:
            return None

    def pause(self, reason: str, by: str = "") -> None:
        self.paused_path.write_text(reason.strip() + "\n", encoding="utf-8")
        self.event("paused", reason=reason, by=by)

    def resume(self, by: str = "") -> None:
        self.paused_path.unlink(missing_ok=True)
        self.event("resumed", by=by)

    # -- tasks ------------------------------------------------------------
    def new_task(self, text: str, **opts) -> str:
        text = text.strip()
        if not text:
            raise ValueError("the task text is empty")
        with self.lock():  # a key saved meanwhile is either known here or scrubbed from the task after
            text = self.redact(text)  # a worker never gets a key in its task
            self.tasks_dir.mkdir(exist_ok=True)
            ids = [int(d.name[1:]) for d in self.tasks_dir.iterdir() if d.name[1:].isdigit()]
            tid = f"t{max(ids, default=0) + 1:04d}"
            d = self.tasks_dir / tid
            d.mkdir()
            (d / "task.md").write_text(text + "\n", encoding="utf-8")
            title = self.redact(opts.pop("title", "")) or text.splitlines()[0][:80]
            meta = {"id": tid, "title": title, "state": "queued", "created": now(),
                    "lane": "", "effort": "", "model": "", "max_turns": 0, "attempts": []}
            meta.update({k: v for k, v in opts.items() if v not in (None, "")})
            own = self.settings()["lanes"].get(meta["lane"]) if meta["lane"] else None
            if isinstance(own, dict) and own.get("repo") and not meta.get("repo"):  # its lane's repository now:
                meta["repo"] = resolved(own["repo"]) or own["repo"]  # a lane made again elsewhere is not the task's
            write_json(d / "meta.json", meta)
        self.event("task_added", task=tid, lane=meta["lane"], **({"browser": True} if meta.get("browser") else {}))
        return tid

    def meta(self, tid: str) -> dict:
        m = read_json(self.tasks_dir / tid / "meta.json")
        if not isinstance(m, dict):
            raise SystemExit(f"hansa: no task {tid}")
        return m

    def update(self, tid: str, **fields) -> dict:
        return self.change(tid, lambda m: fields)

    def change(self, tid: str, decide) -> dict | None:
        """Read a task, decide and write in one step under the lock. decide(meta) returns the
        fields to set, or None to leave the task as it is (then this returns None)."""
        with self.lock():
            m = self.meta(tid)
            fields = decide(m)
            if fields is None:
                return None
            m.update(fields)
            write_json(self.tasks_dir / tid / "meta.json", m)
        return m

    def transition(self, tid: str, states, **fields) -> dict | None:
        """Set these fields only while the task is in one of these states; None when it has moved on."""
        return self.change(tid, lambda m: fields if m["state"] in states else None)

    def tasks(self) -> list:
        if not self.tasks_dir.is_dir():
            return []
        out = []
        for d in sorted(self.tasks_dir.iterdir()):
            m = read_json(d / "meta.json")
            if isinstance(m, dict):
                out.append(m)
        return out

    # -- provider state, asks, web token ------------------------------------
    def provider_state(self) -> dict:
        return read_json(self.state_dir / "providers.json", {}) or {}

    def set_provider_state(self, name: str, **fields) -> None:
        with self.lock():
            st = self.provider_state()
            st.setdefault(name, {}).update(fields)
            self.state_dir.mkdir(exist_ok=True)
            write_json(self.state_dir / "providers.json", st)

    def asks(self) -> list:
        return read_json(self.asks_path, []) or []

    def ready_news(self) -> None:
        """Tell the open requests' askers of the providers they name that became ready (views.ready_news)."""
        from .views import ready_news
        ready_news(self)

    def add_ask(self, text: str, by: str = "", session: dict | None = None) -> int:
        """session: the Claude Code session that asked, which hears the answer (hansa/notify.py). It records the
        providers ready then; any other name (one not set up yet too) was not, and a request naming it may be done
        once it is (views.ask_note)."""
        from .views import ready_now
        ready_then = ready_now(self)
        with self.lock():
            asks = self.asks()
            n = max((a["id"] for a in asks), default=0) + 1
            asks.append({"id": n, "text": self.redact(text.strip()), "by": by, "created": now(), "closed": None,
                         "answer": "", "ready_then": ready_then, **({"session": session} if session else {})})
            write_json(self.asks_path, asks)
        self.event("ask", id=n, by=by)
        return n

    def close_ask(self, n: int, answer: str = "", by: str = "") -> str:
        """Answers an open request; one no longer open (closed meanwhile by the coordinator, say, while the page
        still showed it) is left as it was: what was refused is returned, "" when it was answered."""
        with self.lock():
            asks = self.asks()
            a = next((a for a in asks if a.get("id") == n), None)
            if not a:
                return f"there is no request #{n}; nothing was answered"
            if a.get("closed"):
                return (f"request #{n} was already closed"
                        + (f" by the coordinator ({a['reason']})" if a.get("closed_by") == "coordinator" else "")
                        + "; your answer was not taken")
            a["closed"], a["answer"] = now(), self.redact(answer.strip())
            write_json(self.asks_path, asks)
        self.event("ask_closed", id=n, by=by)
        return ""

    def close_ask_as_coordinator(self, n: int, reason: str, by: str = "") -> None:
        """The coordinator closes an open request of this desk itself (hansa ask --close), saying why; the page
        shows it as closed by the coordinator, and nobody is posted: the coordinator knows."""
        reason = self.redact(reason.strip())
        if not reason:
            raise SystemExit("hansa: say why the request is closed: hansa ask --close N \"why\"")
        with self.lock():
            asks = self.asks()
            a = next((a for a in asks if a.get("id") == n), None)
            if not a:
                raise SystemExit(f"hansa: there is no request #{n} on this desk (hansa status lists the open ones)")
            if a.get("closed"):
                raise SystemExit(f"hansa: request #{n} is already closed"
                                 + (f" by the coordinator: {a['reason']}" if a.get("closed_by") == "coordinator" else
                                    f": {a['answer']}" if a.get("answer") else "")
                                 + "; nothing changed")
            a.update(closed=now(), answer="", closed_by="coordinator", reason=reason)
            write_json(self.asks_path, asks)
        self.event("ask_closed", id=n, by=by, coordinator=True, reason=reason)

    def web_token(self) -> str:
        f = self.state_dir / "web-token"
        if not f.is_file():
            self.state_dir.mkdir(exist_ok=True)
            fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(_secrets.token_urlsafe(18))
        return f.read_text().strip()


def public_settings(desk: Desk) -> dict:
    """Settings plus which keys are present, with no key values."""
    rev = desk.settings_rev()  # read first: if the file changes meanwhile, a save is refused, never lost
    s = copy.deepcopy(desk.settings())
    keys = {name: desk.has_key(name) for name, p in s["providers"].items() if p.get("kind") == "compatible"}
    # saved folders the coordinator is no longer given (removed, or a link now leading where it may not read)
    kept, not_kept, stops = no_read_parts(s, desk.root)
    skipped = read_split(s.get("coordinator_read", []), desk.root, s["providers"], denied=kept)[1]
    # saved entries of what workers may not read that no run applies now (a lane's repository under one, say)
    # lanes on the principal's list of lanes released without asking that are no longer what the list recorded:
    # their releases wait for the yes until the list is saved again
    listed = s.get("releases_without_asking") if isinstance(s.get("releases_without_asking"), list) else []
    # gone: a target no lane has now, which the page shows among the ticks with what it was, to be unticked
    unmatched = [{"lane": lane, "target": target, "why": why, "why_zh": listed_mismatch(s, x, True),
                  "gone": target not in lane_targets(s.get("lanes", {}).get(lane)),
                  "was": str(x.get("command", x.get("release", ""))) if isinstance(x, dict) else ""}
                 for x in listed if isinstance(x, (str, dict)) for why in [listed_mismatch(s, x)] if why
                 for lane, target in [listed_key(x)]]
    return {"settings": s, "keys": keys, "problems": validate(s, desk.root, page=True), "rev": rev,
            "read_skipped": skipped, "deny_skipped": not_kept, "deny_stops": stops, "no_read_stop": stop_text(stops),
            "release_unmatched": unmatched, "lane_warnings": lane_warnings(s)}
