"""The desk: settings, keys, tasks and the event log, as plain files.

Layout (everything under one directory):

    settings.json        settings: the principal's on the web page, the lanes the coordinator's (hansa lane)
    secrets/<name>.key   provider keys; written by the page, never shown
    lanes/<lane>/brief.md  standing instructions sent to every worker in a lane
    tasks/<id>/          task.md, meta.json, result.md, and what its processes wrote, keys removed
    worktrees/<lane>/    one git worktree per lane
    notes/               the coordinator's own notes, kept past a cleared session; it writes there without asking
    events.jsonl         append-only record of everything that happened
    asks.json            requests from agents to the principal
    PAUSED               present = no new work starts (holds the reason)
    .hansa/                 runtime state: provider cooling, heartbeat, web token and port, and runs/<id>/
                         (what a task's processes write, until hansa publishes it without keys)
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
import sys
import threading
import time
import unicodedata
from pathlib import Path

NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
RULE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\(.+\))?$")
# A host a lane's workers may reach during a task: registry.npmjs.org, *.pypi.org, localhost:3000
LABEL = r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
HOST = re.compile(rf"^(\*\.)?{LABEL}(\.{LABEL})*(:(?P<port>[0-9]{{1,5}}))?$")
MAX_HOSTS = 50
MAX_WRITE = 20
MAX_READ = 20
# Characters a folder the coordinator is let read may not have in its path: it goes into a permission rule,
# Edit(//path/**), where these mean something else
RULE_BREAKERS = "()[]{}*?"
# Files under the home folder of the user hansa runs as that no worker command may read: logins and
# keys for other services. The desk's own secrets/ and .hansa/ and the provider's Claude Code folder
# are added per run (run.sandbox_settings); no lane's write list may reach any of them.
HOME_SECRETS = (".ssh", ".aws", ".config/gh", ".claude.json", ".netrc", ".gnupg", ".docker", ".kube", ".npmrc",
                ".pypirc")
PROVIDER_KINDS = ("subscription", "compatible")
EFFORTS = ("", "low", "medium", "high", "xhigh", "max")
PRICE_FIELDS = ("in", "out", "cache_read", "cache_write", "cache_write_1h")
MANAGED_ENV = ("CLAUDE_CONFIG_DIR", "HANSA_TASK")

# Bash commands a lane's design sessions may run without asking, for every lane: none. A lane's
# allowed commands matter only to its design sessions, which run outside the sandbox: workers run
# any command inside their sandbox and need none. A lane lists only its own, such as its tests.
DEFAULT_ALLOW: list = []


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


def validate(s: dict, root=None) -> list:
    """Every problem with a settings object, in words; empty means valid. root: the desk's folder,
    which no lane's write list may reach into."""
    p = []
    if not isinstance(s, dict):
        return ["settings must be an object"]
    known = {"version", "concurrency", "cool_minutes", "coordinator", "design", "providers", "lanes", "prices",
             "browser", "coordinator_read"}
    p += [f"unknown setting: {k}" for k in s if k not in known]
    # which of them may be read is looked at when the page saves a changed list, and when the coordinator
    # starts (read_split): one that went bad later (removed, a link pointed elsewhere) is skipped there and
    # said on the page, and does not keep other settings or the coordinator's lane changes from saving
    given = s.get("coordinator_read", [])
    if not isinstance(given, list) or len(given) > MAX_READ or not all(isinstance(x, str) for x in given):
        p.append(f"the folders the coordinator may read must be a list of up to {MAX_READ} full paths, one per line")
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
        extra = set(pv) - {"kind", "model", "effort", "config_dir", "base_url", "note", "key", "api_key", "token"}
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
        extra = set(ln) - {"repo", "base", "providers", "allow", "network", "write", "env", "check", "preview", "setup",
                           "max_turns", "timeout_min", "note"}
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
        if not isinstance(allow, list) or not all(isinstance(r, str) and RULE.match(r) for r in allow):
            p.append(f"{at}: allow must be a list of rules such as Bash(pnpm test:*)")
        elif any(is_git_rule(r) for r in allow):
            p.append(f"{at}: git is handled by hansa (workers can already look at status, diffs and history, "
                     "and hansa commits for them); remove the rules that run git")
        hosts = ln.get("network", [])
        if not isinstance(hosts, list) or len(hosts) > MAX_HOSTS or not all(is_host(h) for h in hosts):
            p.append(f"{at}: network must be a list of up to {MAX_HOSTS} host names such as registry.npmjs.org, "
                     "*.pypi.org or localhost:3000")
        p += [f"{at}: {x}" for x in write_problems(ln.get("write", []), ln.get("repo"), root, provs)]
        env = ln.get("env", {})
        if not isinstance(env, dict):
            p.append(f"{at}: env must be an object")
        else:
            for k, v in env.items():
                if not ENV_NAME.match(k) or not isinstance(v, str):
                    p.append(f"{at}: env {k} must be NAME=text")
                elif k in MANAGED_ENV or k.startswith("ANTHROPIC_"):
                    p.append(f"{at}: env {k} is set from the provider, not the lane")
        for field in ("check", "preview", "setup"):
            if not isinstance(ln.get(field, ""), str) or len(ln.get(field, "")) > 1000:
                p.append(f"{at}: {field} must be one shell command line (up to 1000 characters)")
        if not _is_int(ln.get("max_turns", 300), 1, 1000):
            p.append(f"{at}: max_turns must be from 1 to 1000")
        if not _is_int(ln.get("timeout_min", 240), 1, 1440):
            p.append(f"{at}: timeout_min must be from 1 to 1440")

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


def _forms(path: str) -> set:
    """A full path as given and with links resolved; on macOS, one under the data volume also as the path
    from /."""
    out = {_plain(path), _plain(os.path.realpath(path))}
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


def _mac_fence(home: str) -> list:
    """On macOS: the data volume and the folders above it hold everything, the home folder too; /Volumes
    holds a link to / and /.vol reaches any folder by its number; ~/Library holds the keychains, apps' own
    data and what runs at login."""
    if sys.platform != "darwin":
        return []
    lib = os.path.join(home, "Library")
    return [(DATA, _above, f"it would open {DATA}, the volume macOS keeps everything on, the home folder too"),
            ("/Volumes", _above, "it would open /Volumes, which holds every volume, the one with the home folder too "
                                 "(a folder on another volume, such as /Volumes/Build/cache, can be listed)"),
            ("/.vol", _overlaps, "it would open /.vol, where macOS reaches any folder by its number, the home folder too"),
            (lib, _in_library, f"it would open {lib}, which holds the keychains, apps' own data and what runs at "
                               f"login (only {os.path.join(lib, 'Caches')} and folders inside it can be listed)")]


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


def claude_home(state_dir, name: str, p: dict) -> Path:
    """Where Claude Code keeps a provider's login or settings, and its session records."""
    if p.get("kind") == "subscription":
        return Path(p.get("config_dir") or os.path.expanduser("~/.claude"))
    return Path(state_dir) / f"claude-{name}"


def _has_control(x: str) -> bool:
    """Whether a path has a line break or another control character."""
    return any(unicodedata.category(c) in ("Cc", "Zl", "Zp") for c in x)


def write_problems(paths, repo=None, root=None, providers=None) -> list:
    """What is wrong with a lane's write list: the folders its workers may write besides the worktree.
    None may open what the sandbox otherwise keeps closed, or what hansa runs from outside it."""
    if not isinstance(paths, list) or len(paths) > MAX_WRITE or \
            not all(isinstance(x, str) and x.startswith("/") and "\0" not in x and len(x) <= 1000 for x in paths):
        return [f"write must be a list of up to {MAX_WRITE} full paths starting with /, such as /Users/me/.m2"]
    home = os.path.expanduser("~")
    fenced = [(home, _above, f"it would open {home}, the home folder of the user hansa runs as"), *_mac_fence(home)]
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
                                                  "that workers may not read") for f in HOME_SECRETS]
    fenced += [(d, _above, f"it would open {d}, a folder on the PATH hansa runs with")
               for d in os.environ.get("PATH", "").split(os.pathsep) if d.startswith("/")]
    p = []
    for x in paths:
        if _has_control(x):  # as the read list: a line break would split one entry into two where it is written
            p.append(f"write cannot include {json.dumps(x)}: it has a line break or another control character")
            continue
        for f, hit, why in fenced:
            if hit(x, f):  # as the disk sees both: as given and with links resolved, in any case it ignores
                p.append(f"write cannot include {x}: {why}")
                break
        else:
            if any(c in v for v in _forms(x) for c in RULE_BREAKERS):  # the sandbox may read it as a pattern
                p.append(f"write cannot include {x}: a name in it (or where its link leads) has one of "
                         f"{RULE_BREAKERS}, which the sandbox may read as a pattern and open more than the one folder")
    return p


def read_split(paths, root=None, providers=None, own=()) -> tuple:
    """The folders the principal lets the coordinator read without asking: (those it may be given, what is
    wrong with the others, one line each). None may open what hansa keeps from it: the home folder as a
    whole, the desk (its keys and state), a Claude Code folder (a provider's, and `own`: the one its own
    session runs from, and ~/.claude always), a login or key. Each is compared as the disk sees it (_above)."""
    if not isinstance(paths, list) or len(paths) > MAX_READ:
        return [], [f"the folders the coordinator may read must be a list of up to {MAX_READ} full paths, one per line"]
    home = os.path.expanduser("~")
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
               for f in HOME_SECRETS]
    good, p, seen = [], [], set()
    for x in paths:
        at = f"the coordinator cannot be let read {x if isinstance(x, str) else json.dumps(x)}"
        if not isinstance(x, str) or not x.startswith("/") or _has_control(x) or len(x) > 1000:
            p.append(f"{at}: it must be a full path starting with /, such as /opt/include")
            continue
        why = next((why for f, hit, why in fenced if hit(x, f)), "")
        if not why and any(c in v for v in _forms(x) for c in RULE_BREAKERS):
            why = f"a name in it (or where its link leads) has one of {RULE_BREAKERS}, which a permission rule cannot hold"
        if not why and not os.path.isdir(x):
            why = "it is a file, not a folder" if os.path.exists(x) else "there is no such folder"
        if why:
            p.append(f"{at}: {why}")
        elif _seen(x) not in seen:  # one folder once, however it is spelled
            seen.add(_seen(x))
            good.append(stored(x))  # as the disk spells it: what Claude Code is given, and the Edit denial
    return good, p


def is_git_rule(rule: str) -> bool:
    """Whether an allowed-command rule runs git anywhere in it: also behind env, sudo, a variable,
    a path or a shell's -c."""
    m = re.match(r"^Bash\((.*)\)$", rule.strip())
    return any(w.strip("'\"").rsplit("/", 1)[-1].split(":", 1)[0] == "git" for w in (m.group(1).split() if m else []))


class Desk:
    def __init__(self, root) -> None:
        self.root = Path(root).resolve()
        self.settings_path = self.root / "settings.json"
        self.secrets_dir = self.root / "secrets"
        self.tasks_dir = self.root / "tasks"
        self.lanes_dir = self.root / "lanes"
        self.worktrees_dir = self.root / "worktrees"
        self.notes_dir = self.root / "notes"
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
            problems = validate(new, self.root)
            if problems:
                return problems
            old = self.settings()
            changed = sorted(k for k in set(old) | set(new) if old.get(k) != new.get(k))
            write_json(self.settings_path, new)
        self.event("settings_saved", by=by, changed=changed)
        self.ready_news()
        return []

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
        when it was written): the task folders and the requests. The event log only ever grows, so
        it keeps it."""
        tasks = self.tasks_dir.iterdir() if self.tasks_dir.is_dir() else []
        for f in [self.asks_path, *(d / name for d in tasks for name in self.PUBLISHED)]:
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

    def close_ask(self, n: int, answer: str = "", by: str = "") -> None:
        with self.lock():
            asks = self.asks()
            for a in asks:
                if a["id"] == n:
                    a["closed"], a["answer"] = now(), self.redact(answer.strip())
            write_json(self.asks_path, asks)
        self.event("ask_closed", id=n, by=by)

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
    skipped = read_split(s.get("coordinator_read", []), desk.root, s["providers"])[1]
    return {"settings": s, "keys": keys, "problems": validate(s, desk.root), "rev": rev, "read_skipped": skipped}
