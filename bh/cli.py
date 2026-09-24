"""The `bh` command. Every subcommand is a thin wrapper over desk, run, views and cost."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import __version__, cost, views
from .desk import DEFAULT_ALLOW, Desk, default_lane, default_settings, write_json
from .run import VERIFY_TASK, Dispatcher, check_provider, git, provider_env, stop

CARD = Path(__file__).with_name("card.md")
HELPERS = Path(__file__).with_name("agents")  # the coordinator's helpers: scout, reviewer
RETIRED_HELPERS = ("verifier.md", "verifier-high.md")  # read-only checks, replaced by bh verify and the reviewer
# Claude Code's own general-purpose helpers run on the coordinator's model and effort with no
# limits of their own; the desk's helpers do the same jobs at a set effort instead.
BUILTIN_HELPERS = ("general-purpose", "Explore", "Plan", "claude", "statusline-setup", "claude-code-guide")


def init_desk(root: Path, repo: str = "", lane: str = "a", base: str = "") -> Desk:
    desk = Desk(root)
    if desk.settings_path.exists():
        raise SystemExit(f"bh: {desk.root} already has a desk")
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
    r = str(desk.root).lstrip("/")
    guarded = [f"//{r}/settings.json", f"//{r}/secrets/**", f"//{r}/tasks/**", f"//{r}/.bh/**", f"//{r}/events.jsonl"]
    perms = {"permissions": {
        "allow": ["Bash(bh:*)", f"Edit(//{r}/lanes/**)"],  # an Edit rule covers every file-editing tool
        "deny": [f"Read(//{r}/secrets/**)", f"Read(//{r}/.bh/**)"]
                + [f"Edit({g})" for g in guarded]}}
    (desk.root / ".claude").mkdir(exist_ok=True)
    write_json(desk.root / ".claude" / "settings.json", perms)
    desk.event("desk_created", repo=repo, lane=lane if repo else "")
    return desk


def _text(args) -> str:
    if getattr(args, "file", None):
        return Path(args.file).read_text(encoding="utf-8")
    return " ".join(args.text)


def cmd_init(a):
    desk = init_desk(Path(a.dir), a.repo or "", a.lane, a.base or "")
    print(f"desk ready at {desk.root}")
    print("next: start `bh serve` there, open the page with `bh web`, add keys and lanes, then `bh add`.")


def cmd_add(a):
    desk = Desk.find(a.desk)
    s = desk.settings()
    if a.browser and not s.get("browser"):
        raise SystemExit("bh: this desk has no browser set up (the principal sets one on the page); nothing was queued")
    tid = desk.new_task(_text(a), lane=a.lane or "", effort=a.effort or "", model=a.model or "",
                        title=a.title or "", max_turns=a.max_turns or 0, browser=a.browser or None)
    if a.lane and a.lane not in s["lanes"]:
        print(f"note: there is no lane {a.lane} yet; the task waits until the principal adds it")
    print(f"{tid} queued" + (f" for lane {a.lane}" if a.lane else "") + f". Follow it with: bh show {tid}")


def cmd_status(a):
    st = views.status(Desk.find(a.desk))
    print(json.dumps(st, ensure_ascii=False, indent=1) if a.json else views.status_text(st))


def cmd_show(a):
    print(views.show_text(Desk.find(a.desk), a.id))


def cmd_diff(a):
    print(views.diff_text(Desk.find(a.desk), a.id, a.stat))


def cmd_merge(a):
    desk = Desk.find(a.desk)
    m = desk.meta(a.id)
    if m.get("kind") == "verify":
        raise SystemExit(f"bh: {a.id} is a verification of {m.get('parent')}; it is never merged. "
                         f"Read it, then merge or send back {m.get('parent')}")
    if m["state"] != "done":
        raise SystemExit(f"bh: only a finished task can be merged; {a.id} is {m['state']}")
    v = views.verification(desk, m)
    if v and v["state"] in ("queued", "running"):
        raise SystemExit(f"bh: {v['task']} is still verifying {a.id}; nothing was merged. Wait for it: bh wait {v['task']}")
    if v and v["verdict"] == "does not hold" and not a.despite_check:
        raise SystemExit(f"bh: the verification {v['task']} found that {a.id} does not hold; nothing was merged. "
                         f"bh show {v['task']} says what it ran. Send it back with bh retry {a.id} \"...\", "
                         f"or merge anyway: bh merge {a.id} --despite-check")
    chk = m.get("check")
    if chk and not chk["ok"] and not a.despite_check:
        raise SystemExit(f"bh: {a.id} failed the lane's check ({chk['note'] or 'exit ' + str(chk['exit'])}); "
                         f"nothing was merged. bh show {a.id} has the output. "
                         f"To merge it anyway: bh merge {a.id} --despite-check")
    lane = desk.settings()["lanes"].get(m["lane"])
    if not lane:
        raise SystemExit(f"bh: lane {m['lane']} is no longer in settings")
    repo, base = lane["repo"], lane.get("base", "main")
    current = git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo)
    if current != base:
        raise SystemExit(f"bh: the checkout at {repo} is on {current}, not {base}; the principal needs to switch it back")
    if git("status", "--porcelain", "--untracked-files=no", cwd=repo):
        raise SystemExit(f"bh: the checkout at {repo} has uncommitted changes; nothing was merged")
    try:
        git("merge", "--no-ff", "-m", f"Merge {a.id}: {m['title']}", m["head"], cwd=repo)
    except RuntimeError as e:
        try:
            git("merge", "--abort", cwd=repo)
        except RuntimeError:
            pass
        raise SystemExit(f"bh: {a.id} conflicts with {base} ({e}); nothing was merged. "
                         f"Try: bh retry {a.id} --fresh \"<same change on the current base>\"")
    desk.update(a.id, state="merged", merged=time.time(), merge_commit=git("rev-parse", "HEAD", cwd=repo))
    despite = (chk and not chk["ok"]) or (v and v["verdict"] == "does not hold")
    desk.event("task_merged", task=a.id, into=base, **({"despite_check": True} if despite else {}))
    _close_verification(desk, m, f"closed: {a.id} was merged")
    print(f"{a.id} merged into {base} at {repo}")


def cmd_retry(a):
    desk = Desk.find(a.desk)
    m = desk.meta(a.id)
    if m.get("kind") == "verify":
        raise SystemExit(f"bh: {a.id} is a verification; send back the task it checked: bh retry {m.get('parent')} \"...\"")
    old = (desk.tasks_dir / a.id / "task.md").read_text(encoding="utf-8").strip()
    note = " ".join(a.note).strip()
    text = old + f"\n\n---\nFollow-up to {a.id} ({m['state']})." + (f" What to change: {note}" if note else "")
    report = desk.tasks_dir / a.id / "result.md"
    if report.exists() and report.read_text(encoding="utf-8").strip():
        text += "\nThe previous attempt's report:\n" + report.read_text(encoding="utf-8").strip()[-6000:]
    if a.browser and not desk.settings().get("browser"):
        raise SystemExit("bh: this desk has no browser set up (the principal sets one on the page); nothing was queued")
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
    m = desk.meta(a.id)
    if m["state"] == "queued":
        desk.update(a.id, state="cancelled", error="cancelled before it started")
    elif m["state"] == "running":
        desk.update(a.id, cancel=True)
        stop(m.get("pid"))
    elif m["state"] == "done":
        desk.update(a.id, state="cancelled", error="closed without merging")
    else:
        raise SystemExit(f"bh: {a.id} is {m['state']}; only queued, running or unmerged finished tasks can be cancelled")
    _close_verification(desk, m, f"closed: {a.id} was cancelled")
    desk.event("task_cancel", task=a.id)
    print(f"{a.id} cancelled")


def cmd_verify(a):
    desk = Desk.find(a.desk)
    m = desk.meta(a.id)
    if m.get("kind") == "verify":
        raise SystemExit(f"bh: {a.id} is itself a verification; verify the task it checks ({m.get('parent')})")
    if m["state"] != "done" or not m.get("commits"):
        raise SystemExit(f"bh: only a finished task with changes can be verified; {a.id} is {m['state']}"
                         + ("" if m.get("commits") else ", with nothing to merge"))
    if a.browser and not desk.settings().get("browser"):
        raise SystemExit("bh: this desk has no browser set up (the principal sets one on the page); nothing was queued")
    v = views.verification(desk, m)
    if v and v["state"] in ("queued", "running"):
        raise SystemExit(f"bh: {v['task']} is already verifying {a.id}")
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
          f"Follow it with: bh wait {vid}; the verdict shows in bh show {a.id}")


def _close_verification(desk: Desk, m: dict, why: str) -> None:
    """A verification belongs to the task it checks, and ends with it."""
    v = views.verification(desk, m)
    if not v:
        return
    if v["state"] in ("queued", "done"):
        desk.update(v["task"], state="cancelled", error=why)
    elif v["state"] == "running":
        desk.update(v["task"], cancel=True)
        stop(desk.meta(v["task"]).get("pid"))


def cmd_pause(a):
    Desk.find(a.desk).pause(" ".join(a.reason) or "paused", by=os.environ.get("BH_ROLE", "cli"))
    print("paused: no new tasks start until `bh resume`; running tasks finish")


def cmd_resume(a):
    Desk.find(a.desk).resume(by=os.environ.get("BH_ROLE", "cli"))
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
        print("note: bh serve is not running, so no task will start or finish until it is")
    end = time.time() + a.timeout * 60
    while True:
        ended = [desk.meta(t) for t in watch if not live(t)]
        answered = [x for x in desk.asks() if x["id"] in open_asks and x["closed"]]
        if ended or answered or time.time() >= end:
            break
        time.sleep(a.every)
    for m in ended:
        print(f"{m['id']} {m['state']}: {m['title']}  (bh show {m['id']})")
    for x in answered:
        print(f"request #{x['id']} answered: {x['answer'] or '(closed without a note)'}")
    still = [t for t in watch if live(t)]
    if not ended and not answered:
        print(f"nothing changed in {a.timeout:g} min")
    if still:
        print("still queued or running: " + ", ".join(still) + "  (bh wait again to keep waiting)")


def cmd_ask(a):
    n = Desk.find(a.desk).add_ask(" ".join(a.text), by=os.environ.get("BH_ROLE", "cli"))
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
    raise SystemExit("bh: --since takes HH:MM (today) or YYYY-MM-DD HH:MM")


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


def cmd_serve(a):
    from .web import start_web
    desk = Desk.find(a.desk)
    start_web(desk, a.host, a.port)
    d = Dispatcher(desk)
    desk.event("server_started", port=a.port)
    print(f"bh serve: desk {desk.root}; web page on {a.host}:{a.port} (run `bh web` for the address)", flush=True)
    try:
        while True:
            try:
                d.tick()
            except Exception as e:  # noqa: BLE001 - keep serving; the error is on record
                desk.event("server_error", error=str(e)[:300])
                print(f"bh serve: {e}", file=sys.stderr, flush=True)
            time.sleep(a.every)
    except KeyboardInterrupt:
        desk.event("server_stopped")


def cmd_web(a):
    if os.environ.get("BH_ROLE") or os.environ.get("BH_TASK"):
        raise SystemExit("bh: the web page is for the principal; agents use the other commands")
    desk = Desk.find(a.desk)
    print(f"Open http://127.0.0.1:{a.port}/?t={desk.web_token()}", flush=True)
    print("Keep this window open while you use the page; press Ctrl+C to close it.", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
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
    env["BH_ROLE"] = "coordinator"
    env["BH_DESK"] = str(desk.root)
    # the lanes' checkouts are readable for planning and review; workers do the editing
    repos = sorted({ln["repo"] for ln in s["lanes"].values()})
    no_edit = [f"Edit(//{str(d).lstrip('/')}/**)" for d in [*repos, desk.worktrees_dir]]
    no_builtin = [f"Agent({n})" for n in BUILTIN_HELPERS]
    argv = [claude_bin()] + (["--add-dir", *repos] if repos else []) + ["--disallowedTools", *no_edit, *no_builtin]
    argv += ["--model", co.get("model") or p["model"]]
    if co.get("effort"):
        argv += ["--effort", co["effort"]]
    _bring(desk, CARD, desk.root / "CLAUDE.md", "card_refreshed")
    for f in sorted(HELPERS.glob("*.md")):
        _bring(desk, f, desk.root / ".claude" / "agents" / f.name, "helper_refreshed")
    for name in RETIRED_HELPERS:
        old = desk.root / ".claude" / "agents" / name
        if old.is_file():
            desk.state_dir.mkdir(exist_ok=True)
            old.rename(desk.state_dir / f"{name}.{int(time.time())}")
            desk.event("helper_retired", file=name)
    os.chdir(desk.root)
    os.execvpe(argv[0], argv + a.rest, env)


def _bring(desk: Desk, src: Path, dst: Path, event: str) -> None:
    """Put bh's current copy of a file into the desk (bh was upgraded), keeping the old one in .bh/."""
    new = src.read_text(encoding="utf-8")
    if dst.is_file() and dst.read_text(encoding="utf-8") == new:
        return
    if dst.is_file():
        desk.state_dir.mkdir(exist_ok=True)
        (desk.state_dir / f"{dst.name}.{int(time.time())}").write_text(dst.read_text(encoding="utf-8"), encoding="utf-8")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(new, encoding="utf-8")
    desk.event(event, file=dst.name)


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


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bh", description="A file-based desk for a team of Claude Code agents. "
                                 "`bh help` prints the agent card.")
    ap.add_argument("--version", action="version", version=f"bh {__version__}")
    ap.add_argument("--desk", help="desk directory (default: the desk containing the current directory, or BH_DESK)")
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
