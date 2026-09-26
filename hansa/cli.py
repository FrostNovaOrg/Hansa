"""The `hansa` command. Every subcommand is a thin wrapper over desk, run, views and cost."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import time
from pathlib import Path

from . import build, cost, views
from .desk import Desk, default_lane, default_settings, json_text, now, read_json, write_json
from .run import VERIFY_TASK, Dispatcher, check_provider, git, keys_in_change, provider_env, stop

CARD = Path(__file__).with_name("card.md")
HELPERS = Path(__file__).with_name("agents")  # the coordinator's helpers: scout, reviewer
RETIRED_HELPERS = ("verifier.md", "verifier-high.md")  # read-only checks, replaced by hansa verify and the reviewer
# Claude Code's own general-purpose helpers run on the coordinator's model and effort with no
# limits of their own; the desk's helpers do the same jobs at a set effort instead.
BUILTIN_HELPERS = ("general-purpose", "Explore", "Plan", "claude", "statusline-setup", "claude-code-guide")


def init_desk(root: Path, repo: str = "", lane: str = "a", base: str = "") -> Desk:
    desk = Desk(root)
    if desk.settings_path.exists():
        raise SystemExit(f"hansa: {desk.root} already has a desk")
    for d in (desk.root, desk.tasks_dir, desk.lanes_dir, desk.state_dir):
        d.mkdir(parents=True, exist_ok=True)
    desk.secrets_dir.mkdir(mode=0o700, exist_ok=True)
    s = default_settings()
    if repo:
        repo = str(Path(repo).resolve())
        base = base or git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo)
        s["lanes"][lane] = default_lane(repo, base)
        (desk.lanes_dir / lane).mkdir(exist_ok=True)
        (desk.lanes_dir / lane / "brief.md").write_text("", encoding="utf-8")
    write_json(desk.settings_path, s)
    (desk.root / "CLAUDE.md").write_text(CARD.read_text(encoding="utf-8"), encoding="utf-8")
    (desk.root / ".claude").mkdir(exist_ok=True)
    write_json(desk.root / ".claude" / "settings.json", {"permissions": permissions(desk)})
    desk.event("desk_created", repo=repo, lane=lane if repo else "")
    return desk


def permissions(desk: Desk) -> dict:
    """The coordinator's Claude Code permission rules in the desk."""
    r = str(desk.root).lstrip("/")
    guarded = [f"//{r}/settings.json", f"//{r}/secrets/**", f"//{r}/tasks/**", f"//{r}/.hansa/**", f"//{r}/events.jsonl"]
    return {"allow": ["Bash(hansa:*)", f"Edit(//{r}/lanes/**)"],  # an Edit rule covers every file-editing tool
            "deny": [f"Read(//{r}/secrets/**)", f"Read(//{r}/.hansa/**)"] + [f"Edit({g})" for g in guarded]}


def _text(args) -> str:
    if getattr(args, "file", None):
        return Path(args.file).read_text(encoding="utf-8")
    return " ".join(args.text)


def cmd_init(a):
    desk = init_desk(Path(a.dir), a.repo or "", a.lane, a.base or "")
    print(f"desk ready at {desk.root}")
    print("next: start `hansa serve` there, open the page with `hansa web` and add providers and keys; "
          "the coordinator adds lanes with `hansa lane add` and queues work with `hansa add`.")


def cmd_add(a):
    desk = Desk.find(a.desk)
    s = desk.settings()
    if a.browser and not s.get("browser"):
        raise SystemExit("hansa: this desk has no browser set up (the principal sets one on the page); nothing was queued")
    tid = desk.new_task(_text(a), lane=a.lane or "", effort=a.effort or "", model=a.model or "",
                        title=a.title or "", max_turns=a.max_turns or 0, browser=a.browser or None)
    if a.lane and a.lane not in s["lanes"]:
        print(f"note: there is no lane {a.lane} yet; the task waits until it is added: hansa lane add {a.lane} --repo PATH")
    if a.model:
        print("note: --model applies only when a Claude subscription provider runs the task; other providers use"
              f" their own model. Which provider runs it follows the lane's order: hansa explain lane {a.lane or 'L'}")
    print(f"{tid} queued" + (f" for lane {a.lane}" if a.lane else "") + f". Follow it with: hansa show {tid}")


def cmd_design(a):
    desk = Desk.find(a.desk)
    s = desk.settings()
    lane = a.lane or (next(iter(s["lanes"])) if len(s["lanes"]) == 1 else "")
    if lane not in s["lanes"]:
        raise SystemExit("hansa: say which lane the design session is for: --lane L" if not lane
                         else f"hansa: there is no lane {lane}")
    with desk.lock():
        open_ = [m["id"] for m in desk.tasks() if m.get("kind") == "design" and m.get("lane") == lane
                 and m["state"] in ("queued", "running")]
        if open_:
            raise SystemExit(f"hansa: lane {lane} already has design session {open_[0]} open; one at a time per lane "
                             f"(hansa show {open_[0]})")
        tid = desk.new_task(_text(a), kind="design", lane=lane, title=a.title or "",
                            browser=bool(s.get("browser")) or None)
    print(f"{tid} queued: hansa opens the design session in seconds and asks the principal to join it. "
          f"It comes back for review when they are done: hansa wait {tid}")


def cmd_status(a):
    st = views.status(Desk.find(a.desk))
    print(json.dumps(st, ensure_ascii=False, indent=1) if a.json else views.status_text(st))


def cmd_show(a):
    print(views.show_text(Desk.find(a.desk), a.id))


def cmd_diff(a):
    print(views.diff_text(Desk.find(a.desk), a.id, a.stat))


def cmd_merge(a):
    desk = Desk.find(a.desk)
    # Nothing changes the task, its verification, the keys or another merge until this one is done.
    # The repository's merge hooks run inside it: hansa serve waits for them, and a hansa command in a hook
    # that would change the desk stops at once (HANSA_MERGING) instead of waiting for this merge forever.
    with desk.lock():
        m, v, chk, repo, base = _mergeable(desk, a)
        try:
            git("merge", "--no-ff", "-m", f"Merge {a.id}: {m['title']}", m["head"], cwd=repo, env={"HANSA_MERGING": "1"})
        except RuntimeError as e:
            try:
                git("merge", "--abort", cwd=repo)
            except RuntimeError:
                pass
            raise SystemExit(f"hansa: {a.id} conflicts with {base} ({e}); nothing was merged. "
                             f"Try: hansa retry {a.id} --fresh \"<same change on the current base>\"")
        desk.update(a.id, state="merged", merged=time.time(), merge_commit=git("rev-parse", "HEAD", cwd=repo))
        _close_verification(desk, m, f"closed: {a.id} was merged")
    despite = (chk and not chk["ok"]) or (v and v["verdict"] == "does not hold")
    desk.event("task_merged", task=a.id, into=base, **({"despite_check": True} if despite else {}))
    print(f"{a.id} merged into {base} at {repo}")


def _mergeable(desk: Desk, a):
    """Everything that must hold before a merge; says why not and exits when something does not."""
    m = desk.meta(a.id)
    if m.get("kind") == "verify":
        raise SystemExit(f"hansa: {a.id} is a verification of {m.get('parent')}; it is never merged. "
                         f"Read it, then merge or send back {m.get('parent')}")
    if m["state"] != "done":
        raise SystemExit(f"hansa: only a finished task can be merged; {a.id} is {m['state']}")
    lane = desk.settings()["lanes"].get(m["lane"])
    if not lane:
        raise SystemExit(f"hansa: lane {m['lane']} is no longer in settings")
    repo, base = lane["repo"], lane.get("base", "main")
    leaked = keys_in_change(desk, repo, m["base"], m["head"]) or m.get("leaked")  # a key saved since counts too
    if leaked:
        if leaked != m.get("leaked"):
            desk.update(a.id, leaked=leaked)
        raise SystemExit(f"hansa: the change in {a.id} holds the key of {', '.join(leaked)}; nothing was merged, "
                         f"and no flag overrides this. Redo it from the base without the key: hansa retry {a.id} --fresh "
                         f"\"...\", and tell the principal with hansa ask: they may want to change that key.")
    v = views.verification(desk, m)
    if v and v["state"] in ("queued", "running"):
        raise SystemExit(f"hansa: {v['task']} is still verifying {a.id}; nothing was merged. Wait for it: hansa wait {v['task']}")
    if v and v["verdict"] == "does not hold" and not a.despite_check:
        raise SystemExit(f"hansa: the verification {v['task']} found that {a.id} does not hold; nothing was merged. "
                         f"hansa show {v['task']} says what it ran. Send it back with hansa retry {a.id} \"...\", "
                         f"or merge anyway: hansa merge {a.id} --despite-check")
    chk = m.get("check")
    if chk and not chk["ok"] and not a.despite_check:
        raise SystemExit(f"hansa: {a.id} failed the lane's check ({chk['note'] or 'exit ' + str(chk['exit'])}); "
                         f"nothing was merged. hansa show {a.id} has the output. "
                         f"To merge it anyway: hansa merge {a.id} --despite-check")
    current = git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo)
    if current != base:
        raise SystemExit(f"hansa: the checkout at {repo} is on {current}, not {base}; the principal needs to switch it back")
    if git("status", "--porcelain", "--untracked-files=no", cwd=repo):
        raise SystemExit(f"hansa: the checkout at {repo} has uncommitted changes; nothing was merged")
    return m, v, chk, repo, base


def cmd_retry(a):
    desk = Desk.find(a.desk)
    with desk.lock():  # the task cannot start, end or be sent back twice meanwhile
        _retry(desk, a)


def _retry(desk: Desk, a) -> None:
    m = desk.meta(a.id)
    if m.get("kind") == "verify":
        raise SystemExit(f"hansa: {a.id} is a verification; send back the task it checked: hansa retry {m.get('parent')} \"...\"")
    if m["state"] in ("queued", "running"):
        raise SystemExit(f"hansa: {a.id} is {m['state']}; a follow-up is for a task that has ended. Wait for it "
                         f"(hansa wait {a.id}), or cancel it first (hansa cancel {a.id})")
    if m["state"] == "replaced":
        raise SystemExit(f"hansa: {a.id} was already replaced by {m.get('replaced_by')}; send that one back instead")
    old = (desk.tasks_dir / a.id / "task.md").read_text(encoding="utf-8").strip()
    note = " ".join(a.note).strip()
    text = old + f"\n\n---\nFollow-up to {a.id} ({m['state']})." + (f" What to change: {note}" if note else "")
    report = desk.tasks_dir / a.id / "result.md"
    if report.exists() and report.read_text(encoding="utf-8").strip():
        text += "\nThe previous attempt's report:\n" + report.read_text(encoding="utf-8").strip()[-6000:]
    if a.browser and not desk.settings().get("browser"):
        raise SystemExit("hansa: this desk has no browser set up (the principal sets one on the page); nothing was queued")
    opts = {"lane": m.get("lane", ""), "effort": a.effort or m.get("effort", ""), "model": m.get("model", ""),
            "parent": a.id, "title": f"{m['title']} (retry of {a.id})"[:80], "browser": a.browser or m.get("browser")}
    if not a.fresh and m.get("head"):
        opts.update(start_from=m["head"], base=m["base"])
        text += f"\nYou start from the previous attempt's last commit ({m['head'][:10]})."
    tid = desk.new_task(text, **opts)
    if m["state"] in ("done", "failed"):  # the follow-up carries the work on; the original leaves the review list
        desk.update(a.id, state="replaced", replaced_by=tid)
        _close_verification(desk, m, f"closed: {a.id} was replaced by {tid}")
    print(f"{tid} queued as a follow-up to {a.id}" + (" from the base branch" if a.fresh else ""))


def cmd_cancel(a):
    desk = Desk.find(a.desk)
    with desk.lock():  # the state read here is the state changed here
        m = desk.meta(a.id)
        if m["state"] == "queued":
            desk.update(a.id, state="cancelled", error="cancelled before it started", finished=now())
        elif m["state"] == "running":  # hansa serve stops it; stopping it here as well is only quicker
            m = desk.update(a.id, cancel=True)
        elif m["state"] == "done":
            desk.update(a.id, state="cancelled", error="closed without merging")
        else:
            raise SystemExit(f"hansa: {a.id} is {m['state']}; only queued, running or unmerged finished tasks can be "
                             f"cancelled")
        _close_verification(desk, m, f"closed: {a.id} was cancelled")
    if m["state"] == "running":
        stop(m.get("pid"), m.get("pid_start", ""))
    desk.event("task_cancel", task=a.id)
    print(f"{a.id} cancelled")


def cmd_verify(a):
    desk = Desk.find(a.desk)
    with desk.lock():  # one verification at a time, of a task that stays as it was checked
        _verify(desk, a)


def _verify(desk: Desk, a) -> None:
    m = desk.meta(a.id)
    if m.get("kind") == "verify":
        raise SystemExit(f"hansa: {a.id} is itself a verification; verify the task it checks ({m.get('parent')})")
    if m["state"] != "done" or not m.get("commits"):
        raise SystemExit(f"hansa: only a finished task with changes can be verified; {a.id} is {m['state']}"
                         + ("" if m.get("commits") else ", with nothing to merge"))
    if a.browser and not desk.settings().get("browser"):
        raise SystemExit("hansa: this desk has no browser set up (the principal sets one on the page); nothing was queued")
    v = views.verification(desk, m)
    if v and v["state"] in ("queued", "running"):
        raise SystemExit(f"hansa: {v['task']} is already verifying {a.id}")
    d = desk.tasks_dir / a.id
    task = (d / "task.md").read_text(encoding="utf-8").strip()
    report = (d / "result.md").read_text(encoding="utf-8").strip() if (d / "result.md").exists() else ""
    note = " ".join(a.note).strip()
    text = (VERIFY_TASK.format(id=a.id, base=m["base"][:12], head=m["head"][:12])
            + (f"\nLook in particular at: {note}\n" if note else "")
            + f"\n## Task {a.id} as it was given\n{task}\n\n## The report of the worker who did it\n{report or '(none)'}\n")
    vid = desk.new_task(text, kind="verify", parent=a.id, lane=m.get("lane", ""), effort=a.effort or "",
                        start_from=m["head"], base=m["head"], title=f"Verify {a.id}: {m['title']}"[:80],
                        browser=a.browser or m.get("browser"))
    if v:
        _close_verification(desk, m, f"closed: {vid} verifies {a.id} again")
    desk.update(a.id, verify=vid)
    print(f"{vid} queued: a worker will run {a.id}'s change to see whether it holds. "
          f"Follow it with: hansa wait {vid}; the verdict shows in hansa show {a.id}")


def _close_verification(desk: Desk, m: dict, why: str) -> None:
    """A verification belongs to the task it checks, and ends with it."""
    v = views.verification(desk, m)
    if not v:
        return
    if v["state"] in ("queued", "done"):
        desk.update(v["task"], state="cancelled", error=why)
    elif v["state"] == "running":  # hansa serve stops it within seconds
        desk.update(v["task"], cancel=True)


def cmd_pause(a):
    Desk.find(a.desk).pause(" ".join(a.reason) or "paused", by=os.environ.get("HANSA_ROLE", "cli"))
    print("paused: no new tasks start until `hansa resume`; running tasks finish")


def cmd_resume(a):
    Desk.find(a.desk).resume(by=os.environ.get("HANSA_ROLE", "cli"))
    print("resumed")


def cmd_wait(a):
    """Return when a watched task stops running, the principal answers a request, or time is up.
    Meant to run in the background, so the session is told when there is something to look at."""
    desk = Desk.find(a.desk)
    live = lambda t: desk.meta(t)["state"] in ("queued", "running")  # noqa: E731
    watch = sorted(set(a.ids)) or sorted(m["id"] for m in desk.tasks() if m["state"] in ("queued", "running"))
    open_asks = {x["id"] for x in desk.asks() if not x["closed"]}
    done_already = [t for t in watch if not live(t)]
    if not done_already and not any(map(live, watch)) and not open_asks:
        print("nothing is queued or running and no request to the principal is open: nothing to wait for")
        return
    if not views.status(desk)["server_up"]:
        print("note: hansa serve is not running, so no task will start or finish until it is")
    end = time.time() + a.timeout * 60
    while True:
        ended = [desk.meta(t) for t in watch if not live(t)]
        answered = [x for x in desk.asks() if x["id"] in open_asks and x["closed"]]
        if ended or answered or time.time() >= end:
            break
        time.sleep(a.every)
    for m in ended:
        print(f"{m['id']} {m['state']}: {m['title']}  (hansa show {m['id']})")
    for x in answered:
        print(f"request #{x['id']} answered: {x['answer'] or '(closed without a note)'}")
    still = [desk.meta(t) for t in watch if live(t)]
    if not ended and not answered:
        print(f"no task finished in {a.timeout:g} min")
    for m in still:
        mins = int((time.time() - m["started"]) // 60) if m.get("started") else 0
        print(f"{m['id']} {m['state']}" + (f" {mins} min, so far: {views.progress_text(desk, m)}"
                                           if m["state"] == "running" else ""))
    if still:
        print("hansa wait again to keep waiting. Some providers take minutes per reply; a task is stuck"
              " only when nothing has happened for a long while.")


def cmd_ask(a):
    n = Desk.find(a.desk).add_ask(" ".join(a.text), by=os.environ.get("HANSA_ROLE", "cli"))
    print(f"ask #{n} is on the principal's page")


def cmd_explain(a):
    print(views.explain_text(Desk.find(a.desk), a.what or "", a.name or ""))


def _since(text: str) -> float:
    """'13:45' (today, or yesterday if that is still ahead) or '2026-09-24 13:45', local time."""
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%H:%M"):
        try:
            t = time.strptime(text.strip(), fmt)
        except ValueError:
            continue
        if fmt != "%H:%M":
            return time.mktime(t)
        now = time.localtime()
        ts = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, t.tm_hour, t.tm_min, 0, 0, 0, -1))
        return ts - 86400 if ts > time.time() else ts
    raise SystemExit("hansa: --since takes HH:MM (today) or YYYY-MM-DD HH:MM")


def cmd_cost(a):
    rep = cost.report(Desk.find(a.desk), a.days, _since(a.since) if a.since else None)
    if a.json:
        rep = {k: v for k, v in rep.items() if k != "rows"} if not a.rows else rep
        print(json.dumps(rep, ensure_ascii=False, indent=1))
    else:
        print(views.cost_text(rep, a.by))


def cmd_check(a):
    desk = Desk.find(a.desk)
    names = [a.provider] if a.provider else sorted(desk.settings()["providers"])
    for n in names:
        r = check_provider(desk, n)
        print(f"{n}: {'ok' if r['ok'] else 'NOT ok'} - {r['detail']}")


def hold_server_lock(desk: Desk):
    """One hansa serve per desk: it holds this lock while it runs. None when another server holds it."""
    desk.state_dir.mkdir(exist_ok=True)
    f = open(desk.state_dir / "serve.lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def cmd_serve(a):
    from .web import start_web
    desk = Desk.find(a.desk)
    held = hold_server_lock(desk)  # kept open while this server runs
    if not held:
        raise SystemExit(f"hansa: another hansa serve is already running on {desk.root}; one per desk, so no task is "
                         f"started twice. Stop that one first if you want to restart it")
    start_web(desk, a.host, a.port)
    d = Dispatcher(desk)
    desk.event("server_started", port=a.port)
    print(f"hansa serve: desk {desk.root}; web page on {a.host}:{a.port} (run `hansa web` for the address)", flush=True)
    try:
        while True:
            try:
                d.tick()
            except Exception as e:  # noqa: BLE001 - keep serving; the error is on record
                desk.event("server_error", error=str(e)[:300])
                print(f"hansa serve: {e}", file=sys.stderr, flush=True)
            time.sleep(a.every)
    except KeyboardInterrupt:
        desk.event("server_stopped")


def cmd_web(a):
    if os.environ.get("HANSA_ROLE") or os.environ.get("HANSA_TASK"):
        raise SystemExit("hansa: the web page is for the principal; agents use the other commands")
    desk = Desk.find(a.desk)
    print(f"Open http://127.0.0.1:{a.port}/?t={desk.web_token()}", flush=True)
    print("Keep this window open while you use the page; close it or press Ctrl+C when you are done.", flush=True)
    # This holds an ssh tunnel to the page open. Its input ends when the connection does (a closed
    # window, a laptop gone to sleep), and then it ends too instead of staying on the server.
    try:
        while os.read(0, 4096):
            pass
    except (KeyboardInterrupt, OSError):
        pass


def cmd_coordinator(a):
    """Start the coordinator's interactive session in the desk, on the coordinator provider."""
    desk = Desk.find(a.desk)
    s = desk.settings()
    co = s["coordinator"]
    p = s["providers"][co["provider"]]
    from .run import claude_bin
    env = {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_")}
    env.update({k: v for k, v in provider_env(desk, co["provider"], p).items() if k not in ("PATH", "TERM")})
    env["PATH"] = str(Path(__file__).resolve().parent.parent / "bin") + os.pathsep + os.environ.get("PATH", "")
    env["HANSA_ROLE"] = "coordinator"
    env["HANSA_DESK"] = str(desk.root)
    # the lanes' checkouts are readable for planning and review; workers do the editing
    repos = sorted({ln["repo"] for ln in s["lanes"].values()})
    no_edit = [f"Edit(//{str(d).lstrip('/')}/**)" for d in [*repos, desk.worktrees_dir]]
    no_builtin = [f"Agent({n})" for n in BUILTIN_HELPERS]
    argv = [claude_bin()] + (["--add-dir", *repos] if repos else []) + ["--disallowedTools", *no_edit, *no_builtin]
    argv += ["--model", co.get("model") or p["model"]]
    if co.get("effort"):
        argv += ["--effort", co["effort"]]
    _bring(desk, CARD.read_text(encoding="utf-8"), desk.root / "CLAUDE.md", "card_refreshed")
    for f in sorted(HELPERS.glob("*.md")):
        _bring(desk, f.read_text(encoding="utf-8"), desk.root / ".claude" / "agents" / f.name, "helper_refreshed")
    mine = desk.root / ".claude" / "settings.json"  # hansa keeps its rules current there; the rest is left as it is
    s = read_json(mine, {})
    s = {**s, "permissions": permissions(desk)} if isinstance(s, dict) else {"permissions": permissions(desk)}
    _bring(desk, json_text(s), mine, "permissions_refreshed", keep_as="coordinator-settings.json")
    for name in RETIRED_HELPERS:
        old = desk.root / ".claude" / "agents" / name
        if old.is_file():
            desk.state_dir.mkdir(exist_ok=True)
            old.rename(desk.state_dir / f"{name}.{int(time.time())}")
            desk.event("helper_retired", file=name)
    os.chdir(desk.root)
    os.execvpe(argv[0], argv + a.rest, env)


def _bring(desk: Desk, new: str, dst: Path, event: str, keep_as: str = "") -> None:
    """Put hansa's current version of a file into the desk (hansa was upgraded), keeping the old one in .hansa/."""
    if dst.is_file() and dst.read_text(encoding="utf-8") == new:
        return
    if dst.is_file():
        desk.state_dir.mkdir(exist_ok=True)
        (desk.state_dir / f"{keep_as or dst.name}.{int(time.time())}").write_text(dst.read_text(encoding="utf-8"), encoding="utf-8")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(new, encoding="utf-8")
    desk.event(event, file=dst.name)


LANE_LISTS = ("providers", "allow", "network")
LANE_NUMBERS = ("max_turns", "timeout_min")
LANE_FIELDS = ("base", "providers", "allow", "network", "env", "check", "preview", "setup", "max_turns", "timeout_min", "note")
OUTSIDE_WORKER = ("setup", "check", "preview")  # run by hansa itself, not under the worker's allowed commands


def _shown(v) -> str:
    return json.dumps(v, ensure_ascii=False)


def _save(desk: Desk, new: dict) -> None:
    """Save the coordinator's change, checked as the page's are; on problems print them and change nothing."""
    problems = desk.save_settings(new, by="coordinator")
    if problems:
        raise SystemExit("hansa: nothing was changed:\n" + "\n".join(f"  {x}" for x in problems))


def _lane_value(field: str, values: list):
    if field in LANE_LISTS:
        return [x for v in values for x in (v.split(",") if field == "providers" else [v]) if x]
    if field == "env":
        env = {}
        for v in values:
            k, eq, val = v.partition("=")
            if not eq:
                raise SystemExit(f"hansa: env takes NAME=VALUE pairs; {v!r} has no '='. Nothing was changed")
            env[k] = val
        return env
    if len(values) > 1:
        raise SystemExit(f"hansa: {field} takes one value; quote it if it has spaces. Nothing was changed")
    if field in LANE_NUMBERS:
        try:
            return int(values[0])
        except (IndexError, ValueError):
            raise SystemExit(f"hansa: {field} takes one whole number. Nothing was changed")
    if field == "base" and not "".join(values).strip():
        raise SystemExit("hansa: base takes the name of a branch. Nothing was changed")
    return values[0] if values else ""


def cmd_lane(a):
    desk = Desk.find(a.desk)
    if not a.action:
        lanes = desk.settings()["lanes"]
        for name, ln in sorted(lanes.items()):
            print(f"{name}  {ln['repo']}  base {ln.get('base', 'main')}  providers {' > '.join(ln['providers'])}")
        print("no lanes yet: hansa lane add L --repo PATH" if not lanes else "more on one: hansa explain lane L")
        return
    with desk.lock():  # the settings read here are the settings changed here
        s = desk.settings()
        new = json.loads(json.dumps(s))
        if a.action == "add":
            if a.name in s["lanes"]:
                raise SystemExit(f"hansa: there is already a lane {a.name}; change it with hansa lane set {a.name} ...")
            repo = str(Path(a.repo).resolve())
            try:
                base = a.base or git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo)
            except (RuntimeError, OSError) as e:
                raise SystemExit(f"hansa: {repo} is not a git checkout ({e}); nothing was changed")
            providers = [x for x in (a.providers or "").split(",") if x] or None
            new["lanes"][a.name] = default_lane(repo, base, providers)
            _save(desk, new)
            (desk.lanes_dir / a.name).mkdir(parents=True, exist_ok=True)
            brief = desk.lanes_dir / a.name / "brief.md"
            if not brief.exists():
                brief.write_text("", encoding="utf-8")
            ln = new["lanes"][a.name]
            print(f"lane {a.name} added: {repo}, base {base}, providers {' > '.join(ln['providers'])}, "
                  f"stopped at {ln['max_turns']} turns or {ln['timeout_min']} minutes")
            return
        if a.name not in s["lanes"]:
            raise SystemExit(f"hansa: there is no lane {a.name}; lanes: {', '.join(sorted(s['lanes'])) or 'none'}")
        if a.action == "remove":
            tasks = [m for m in desk.tasks() if m.get("lane") == a.name]
            busy = [m["id"] for m in tasks if m["state"] in ("queued", "running")]
            if busy:
                raise SystemExit(f"hansa: lane {a.name} still has {', '.join(busy)} queued or running; nothing was "
                                 f"changed. Wait for them (hansa wait) or cancel them first")
            # merging a finished task needs its lane's repo and base
            review = [m["id"] for m in tasks if m["state"] == "done" and m.get("kind") != "verify"]
            if review:
                raise SystemExit(f"hansa: lane {a.name} still has {', '.join(review)} finished and waiting for review; "
                                 f"nothing was changed. Merging needs the lane: clear them first with hansa merge "
                                 f"or hansa cancel")
            del new["lanes"][a.name]
            _save(desk, new)
            print(f"lane {a.name} removed; its brief stays in {desk.lanes_dir / a.name}")
            return
        value = _lane_value(a.field, a.values)
        old = s["lanes"][a.name].get(a.field, "")
        new["lanes"][a.name][a.field] = value
        _save(desk, new)
    print(f"lane {a.name} {a.field}: {_shown(old)} -> {_shown(value)}")
    if a.field in OUTSIDE_WORKER and old != value:
        desk.event("lane_command_changed", lane=a.name, field=a.field, old=old, new=value)
        print(f"The lane's {a.field} runs outside the worker, so this change is recorded. Tell the principal, in "
              f"the conversation, what you changed and why.")
    if a.field == "network" and old != value:
        desk.event("lane_network_changed", lane=a.name, old=old, new=value)
        print("The lane's workers may now reach these hosts during a task. Tell the principal, in the conversation, "
              "what you changed and why.")


def cmd_concurrency(a):
    desk = Desk.find(a.desk)
    if a.n is None:
        print(f"concurrency: {desk.settings()['concurrency']} task(s) at once")
        return
    with desk.lock():
        new = desk.settings()
        old = new["concurrency"]
        new["concurrency"] = a.n
        _save(desk, new)
    print(f"concurrency: {old} -> {a.n}")


def cmd_help(a):
    print(CARD.read_text(encoding="utf-8"))


def _split_claude_args(argv: list):
    """Everything after `coordinator` belongs to claude, even when it looks like an option."""
    for i, x in enumerate(argv):
        if x == "coordinator":
            return argv[:i + 1], argv[i + 1:]
        if not x.startswith("-") and (i == 0 or argv[i - 1] != "--desk"):
            break
    return argv, []


class _Version(argparse.Action):
    """--version, worked out only when asked (it may ask git which commit this is)."""
    def __call__(self, parser, namespace, values, option_string=None):
        print(f"hansa {build()}")
        parser.exit()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="hansa", description="A file-based desk for a team of Claude Code agents. "
                                 "`hansa help` prints the agent card.")
    ap.add_argument("--version", action=_Version, nargs=0, help="show which hansa this is and exit")
    ap.add_argument("--desk", help="desk directory (default: the desk containing the current directory, or HANSA_DESK)")
    sub = ap.add_subparsers(dest="cmd", metavar="command")

    def add(name, fn, help_):
        p = sub.add_parser(name, help=help_, description=help_)
        p.set_defaults(fn=fn)
        return p

    p = add("init", cmd_init, "create a desk")
    p.add_argument("dir")
    p.add_argument("--repo", help="git checkout the first lane works on")
    p.add_argument("--lane", default="a")
    p.add_argument("--base", help="base branch (default: the checkout's current branch)")
    p = add("add", cmd_add, "queue a task")
    p.add_argument("text", nargs="*")
    p.add_argument("--file")
    p.add_argument("--lane")
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--model")
    p.add_argument("--title")
    p.add_argument("--max-turns", type=int)
    p.add_argument("--browser", action="store_true", help="give this one task a browser (read pages, take screenshots)")
    p = add("design", cmd_design, "open a session the principal joins to shape one feature on the real code")
    p.add_argument("text", nargs="*")
    p.add_argument("--file")
    p.add_argument("--lane")
    p.add_argument("--title")
    p = add("status", cmd_status, "what is running, queued, waiting for review, and why")
    p.add_argument("--json", action="store_true")
    p = add("show", cmd_show, "one task: report, changes, refused commands, usage, next step")
    p.add_argument("id")
    p = add("diff", cmd_diff, "a task's changes against its base")
    p.add_argument("id")
    p.add_argument("--stat", action="store_true")
    p = add("merge", cmd_merge, "merge a finished task into the lane's base branch")
    p.add_argument("id")
    p.add_argument("--despite-check", action="store_true", help="merge even though the lane's check failed (recorded)")
    p = add("retry", cmd_retry, "queue a follow-up to a task")
    p.add_argument("id")
    p.add_argument("note", nargs="*")
    p.add_argument("--fresh", action="store_true", help="start from the base branch, not the previous attempt")
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--browser", action="store_true", help="give the follow-up a browser")
    p = add("verify", cmd_verify, "have a worker run a finished task's change to see whether it holds (never merged)")
    p.add_argument("id")
    p.add_argument("note", nargs="*", help="what to look at in particular")
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--browser", action="store_true", help="give the verification a browser")
    p = add("cancel", cmd_cancel, "stop a queued or running task, or close a finished one without merging")
    p.add_argument("id")
    p = add("pause", cmd_pause, "stop new tasks from starting")
    p.add_argument("reason", nargs="*")
    add("resume", cmd_resume, "let new tasks start again")
    p = add("wait", cmd_wait, "return when a task finishes or fails, or the principal answers; run it in the background")
    p.add_argument("ids", nargs="*", metavar="id", help="tasks to watch (default: every queued or running task)")
    p.add_argument("--timeout", type=float, default=60, help="minutes (default 60)")
    p.add_argument("--every", type=float, default=5.0, help=argparse.SUPPRESS)
    p = add("ask", cmd_ask, "put a request on the principal's page")
    p.add_argument("text", nargs="+")
    p = add("explain", cmd_explain, "what a setting or state means: lane NAME | provider NAME | task ID | states | server")
    p.add_argument("what", nargs="?", choices=["lane", "provider", "task", "states", "server"])
    p.add_argument("name", nargs="?")
    p = add("lane", cmd_lane, "list the lanes, or add, change or remove one")
    lane = p.add_subparsers(dest="action", metavar="add | set | remove")
    q = lane.add_parser("add", help="a new lane on a git checkout")
    q.add_argument("name")
    q.add_argument("--repo", required=True, help="the git checkout the lane works on")
    q.add_argument("--base", help="base branch (default: the checkout's current branch)")
    q.add_argument("--providers", help="provider order, comma-separated (default: claude)")
    q = lane.add_parser("set", help="change one field of a lane", description=(
        "providers, allow and network take the whole new list (none: empty); env takes NAME=VALUE pairs and replaces the "
        "lane's env; base, check, preview, setup and note take one value (none: empty); max_turns and "
        "timeout_min take one whole number"))
    q.add_argument("name")
    q.add_argument("field", choices=LANE_FIELDS)
    q.add_argument("values", nargs="*", metavar="value")
    q = lane.add_parser("remove", help="remove a lane with no task queued, running or waiting for review")
    q.add_argument("name")
    p = add("concurrency", cmd_concurrency, "show, or set, how many tasks run at once")
    p.add_argument("n", nargs="?", type=int)
    p = add("cost", cmd_cost, "tokens and what they are worth")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--since", help="HH:MM today, or YYYY-MM-DD HH:MM: count from then instead of --days")
    p.add_argument("--by", default="day", choices=["day", "seat", "lane", "task", "model", "provider"])
    p.add_argument("--json", action="store_true")
    p.add_argument("--rows", action="store_true", help="with --json: include every row")
    p = add("check", cmd_check, "send one tiny request through a provider (or all)")
    p.add_argument("provider", nargs="?")
    p = add("serve", cmd_serve, "run the dispatcher and the web page (keep it running)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--every", type=float, default=3.0, help=argparse.SUPPRESS)
    p = add("web", cmd_web, "print the web page address for the principal")
    p.add_argument("--port", type=int, default=8765)
    add("coordinator", cmd_coordinator, "start the coordinator session in this desk; "
        "anything after the word coordinator is passed to claude as is")
    add("help", cmd_help, "print the agent card")

    argv, rest = _split_claude_args(sys.argv[1:] if argv is None else list(argv))
    a = ap.parse_args(argv)
    a.rest = rest
    if not getattr(a, "fn", None):
        ap.print_help()
        return 0
    if a.cmd == "add" and not a.text and not a.file:
        ap.error("add needs the task text or --file")
    a.fn(a)
    return 0
