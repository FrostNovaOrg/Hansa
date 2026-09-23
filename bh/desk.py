"""The desk: settings, keys, tasks and the event log, as plain files.

Layout (everything under one directory):

    settings.json        the principal's settings (edited on the web page)
    secrets/<name>.key   provider keys; written by the page, never shown
    lanes/<lane>/brief.md  standing instructions sent to every worker in a lane
    tasks/<id>/          task.md, meta.json, result.md, run.json, run.err
    worktrees/<lane>/    one git worktree per lane
    events.jsonl         append-only record of everything that happened
    asks.json            requests from agents to the principal
    PAUSED               present = no new work starts (holds the reason)
    .bh/                 runtime state: provider cooling, heartbeat, web token
"""
from __future__ import annotations

import contextlib
import copy
import fcntl
import json
import os
import re
import secrets as _secrets
import time
from pathlib import Path

NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
RULE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\(.+\))?$")
PROVIDER_KINDS = ("subscription", "compatible")
EFFORTS = ("", "low", "medium", "high", "xhigh", "max")
PRICE_FIELDS = ("in", "out", "cache_read", "cache_write", "cache_write_1h")
MANAGED_ENV = ("CLAUDE_CONFIG_DIR", "BH_TASK")

# Bash commands every lane may run. File tools are added per run, scoped to
# the lane's worktree, so they are not listed here.
DEFAULT_ALLOW = [
    "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)",
    "Bash(git add:*)", "Bash(git commit:*)", "Bash(ls:*)",
]


def default_settings() -> dict:
    return {
        "version": 1,
        "concurrency": 1,
        "cool_minutes": 30,
        "coordinator": {"provider": "claude", "model": "opus", "effort": "high", "audits_cost": False},
        "providers": {
            "claude": {"kind": "subscription", "model": "sonnet", "effort": "", "config_dir": ""},
        },
        "lanes": {},
        # US dollars per million tokens. Cache prices default to the usual
        # multiples of the input price when left out.
        "prices": {
            "claude-haiku-4-5": {"in": 1, "out": 5},
            "claude-opus-5": {"in": 5, "out": 25},
        },
    }


def default_lane(repo: str, base: str = "main", providers: list | None = None) -> dict:
    return {"repo": repo, "base": base, "providers": providers or ["claude"],
            "allow": list(DEFAULT_ALLOW), "env": {}, "max_turns": 80, "timeout_min": 60}


def now() -> float:
    return round(time.time(), 3)


def write_json(path: Path, data) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _is_int(v, lo: int, hi: int) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi


def validate(s: dict) -> list:
    """Every problem with a settings object, in words; empty means valid."""
    p = []
    if not isinstance(s, dict):
        return ["settings must be an object"]
    known = {"version", "concurrency", "cool_minutes", "coordinator", "providers", "lanes", "prices"}
    p += [f"unknown setting: {k}" for k in s if k not in known]
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
        extra = set(ln) - {"repo", "base", "providers", "allow", "env", "max_turns", "timeout_min", "note"}
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
        env = ln.get("env", {})
        if not isinstance(env, dict):
            p.append(f"{at}: env must be an object")
        else:
            for k, v in env.items():
                if not ENV_NAME.match(k) or not isinstance(v, str):
                    p.append(f"{at}: env {k} must be NAME=text")
                elif k in MANAGED_ENV or k.startswith("ANTHROPIC_"):
                    p.append(f"{at}: env {k} is set from the provider, not the lane")
        if not _is_int(ln.get("max_turns", 80), 1, 500):
            p.append(f"{at}: max_turns must be from 1 to 500")
        if not _is_int(ln.get("timeout_min", 60), 1, 600):
            p.append(f"{at}: timeout_min must be from 1 to 600")

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


class Desk:
    def __init__(self, root) -> None:
        self.root = Path(root).resolve()
        self.settings_path = self.root / "settings.json"
        self.secrets_dir = self.root / "secrets"
        self.tasks_dir = self.root / "tasks"
        self.lanes_dir = self.root / "lanes"
        self.worktrees_dir = self.root / "worktrees"
        self.state_dir = self.root / ".bh"
        self.events_path = self.root / "events.jsonl"
        self.asks_path = self.root / "asks.json"
        self.paused_path = self.root / "PAUSED"

    @classmethod
    def find(cls, explicit: str | None = None) -> "Desk":
        if explicit or os.environ.get("BH_DESK"):
            return cls(explicit or os.environ["BH_DESK"])
        here = Path.cwd().resolve()
        for d in (here, *here.parents):
            if (d / "settings.json").is_file() and (d / "tasks").is_dir():
                return cls(d)
        raise SystemExit("bh: no desk here. Run inside a desk, pass --desk DIR, or set BH_DESK.")

    # -- settings ---------------------------------------------------------
    def settings(self) -> dict:
        s = default_settings()
        loaded = read_json(self.settings_path, {})
        if isinstance(loaded, dict):
            s.update(loaded)
        return s

    def save_settings(self, new: dict, by: str = "") -> list:
        problems = validate(new)
        if problems:
            return problems
        old = self.settings()
        changed = sorted(k for k in set(old) | set(new) if old.get(k) != new.get(k))
        write_json(self.settings_path, new)
        self.event("settings_saved", by=by, changed=changed)
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
            f.unlink(missing_ok=True)
            self.event("key_removed", provider=provider, by=by)
            return
        tmp = f.with_name(f.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(value.strip() + "\n")
        os.replace(tmp, f)
        self.set_provider_state(provider, cool_until=0, reason="")  # a new key ends any rest
        self.event("key_set", provider=provider, by=by)

    # -- log, lock, pause -------------------------------------------------
    def event(self, kind: str, **fields) -> None:
        line = json.dumps({"ts": now(), "event": kind, **fields}, ensure_ascii=False)
        with open(self.events_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    @contextlib.contextmanager
    def lock(self):
        self.state_dir.mkdir(exist_ok=True)
        with open(self.state_dir / "lock", "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

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
        with self.lock():
            self.tasks_dir.mkdir(exist_ok=True)
            ids = [int(d.name[1:]) for d in self.tasks_dir.iterdir() if d.name[1:].isdigit()]
            tid = f"t{max(ids, default=0) + 1:04d}"
            d = self.tasks_dir / tid
            d.mkdir()
            (d / "task.md").write_text(text + "\n", encoding="utf-8")
            title = opts.pop("title", "") or text.splitlines()[0][:80]
            meta = {"id": tid, "title": title, "state": "queued", "created": now(),
                    "lane": "", "effort": "", "model": "", "max_turns": 0, "attempts": []}
            meta.update({k: v for k, v in opts.items() if v not in (None, "")})
            write_json(d / "meta.json", meta)
        self.event("task_added", task=tid, lane=meta["lane"])
        return tid

    def meta(self, tid: str) -> dict:
        m = read_json(self.tasks_dir / tid / "meta.json")
        if not isinstance(m, dict):
            raise SystemExit(f"bh: no task {tid}")
        return m

    def update(self, tid: str, **fields) -> dict:
        with self.lock():
            m = self.meta(tid)
            m.update(fields)
            write_json(self.tasks_dir / tid / "meta.json", m)
        return m

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

    def add_ask(self, text: str, by: str = "") -> int:
        with self.lock():
            asks = self.asks()
            n = max((a["id"] for a in asks), default=0) + 1
            asks.append({"id": n, "text": text.strip(), "by": by, "created": now(), "closed": None, "answer": ""})
            write_json(self.asks_path, asks)
        self.event("ask", id=n, by=by)
        return n

    def close_ask(self, n: int, answer: str = "", by: str = "") -> None:
        with self.lock():
            asks = self.asks()
            for a in asks:
                if a["id"] == n:
                    a["closed"], a["answer"] = now(), answer.strip()
            write_json(self.asks_path, asks)
        self.event("ask_closed", id=n, by=by)

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
    s = copy.deepcopy(desk.settings())
    keys = {name: desk.has_key(name) for name, p in s["providers"].items() if p.get("kind") == "compatible"}
    return {"settings": s, "keys": keys, "problems": validate(s)}
