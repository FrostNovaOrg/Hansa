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
from .run import Dispatcher, check_provider, git, provider_env, stop

CARD = Path(__file__).with_name("card.md")


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
        "allow": ["Bash(bh:*)", f"Edit(//{r}/lanes/**)", f"Write(//{r}/lanes/**)"],
        "deny": [f"Read(//{r}/secrets/**)", f"Read(//{r}/.bh/**)"]
                + [f"{t}({g})" for g in guarded for t in ("Edit", "Write")]}}
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
    tid = desk.new_task(_text(a), lane=a.lane or "", effort=a.effort or "", model=a.model or "",
                        title=a.title or "", max_turns=a.max_turns or 0)
    s = desk.settings()
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
    if m["state"] != "done":
        raise SystemExit(f"bh: only a finished task can be merged; {a.id} is {m['state']}")
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
    desk.event("task_merged", task=a.id, into=base)
    print(f"{a.id} merged into {base} at {repo}")


def cmd_retry(a):
    desk = Desk.find(a.desk)
    m = desk.meta(a.id)
    old = (desk.tasks_dir / a.id / "task.md").read_text(encoding="utf-8").strip()
    note = " ".join(a.note).strip()
    text = old + f"\n\n---\nFollow-up to {a.id} ({m['state']})." + (f" What to change: {note}" if note else "")
    opts = {"lane": m.get("lane", ""), "effort": a.effort or m.get("effort", ""), "model": m.get("model", ""),
            "parent": a.id, "title": f"{m['title']} (retry of {a.id})"[:80]}
    if not a.fresh and m.get("head"):
        opts.update(start_from=m["head"], base=m["base"])
        text += f"\nYou start from the previous attempt's last commit ({m['head'][:10]})."
    tid = desk.new_task(text, **opts)
    print(f"{tid} queued as a follow-up to {a.id}" + (" from the base branch" if a.fresh else ""))


def cmd_cancel(a):
    desk = Desk.find(a.desk)
    m = desk.meta(a.id)
    if m["state"] == "queued":
        desk.update(a.id, state="cancelled", error="cancelled before it started")
    elif m["state"] == "running":
        desk.update(a.id, cancel=True)
        stop(m.get("pid"))
    else:
        raise SystemExit(f"bh: {a.id} is {m['state']}; only queued or running tasks can be cancelled")
    desk.event("task_cancel", task=a.id)
    print(f"{a.id} cancelled")


def cmd_pause(a):
    Desk.find(a.desk).pause(" ".join(a.reason) or "paused", by=os.environ.get("BH_ROLE", "cli"))
    print("paused: no new tasks start until `bh resume`; running tasks finish")


def cmd_resume(a):
    Desk.find(a.desk).resume(by=os.environ.get("BH_ROLE", "cli"))
    print("resumed")


def cmd_ask(a):
    n = Desk.find(a.desk).add_ask(" ".join(a.text), by=os.environ.get("BH_ROLE", "cli"))
    print(f"ask #{n} is on the principal's page")


def cmd_explain(a):
    print(views.explain_text(Desk.find(a.desk), a.what or "", a.name or ""))


def cmd_cost(a):
    rep = cost.report(Desk.find(a.desk), a.days)
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
    argv = [claude_bin(), "--model", co.get("model") or p["model"]]
    if co.get("effort"):
        argv += ["--effort", co["effort"]]
    os.chdir(desk.root)
    os.execvpe(argv[0], argv + a.rest, env)


def cmd_help(a):
    print(CARD.read_text(encoding="utf-8"))


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
    p = add("status", cmd_status, "what is running, queued, waiting for review, and why")
    p.add_argument("--json", action="store_true")
    p = add("show", cmd_show, "one task: report, changes, refused commands, usage, next step")
    p.add_argument("id")
    p = add("diff", cmd_diff, "a task's changes against its base")
    p.add_argument("id")
    p.add_argument("--stat", action="store_true")
    p = add("merge", cmd_merge, "merge a finished task into the lane's base branch")
    p.add_argument("id")
    p = add("retry", cmd_retry, "queue a follow-up to a task")
    p.add_argument("id")
    p.add_argument("note", nargs="*")
    p.add_argument("--fresh", action="store_true", help="start from the base branch, not the previous attempt")
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    p = add("cancel", cmd_cancel, "stop a queued or running task")
    p.add_argument("id")
    p = add("pause", cmd_pause, "stop new tasks from starting")
    p.add_argument("reason", nargs="*")
    add("resume", cmd_resume, "let new tasks start again")
    p = add("ask", cmd_ask, "put a request on the principal's page")
    p.add_argument("text", nargs="+")
    p = add("explain", cmd_explain, "what a setting or state means: lane NAME | provider NAME | task ID | states | server")
    p.add_argument("what", nargs="?", choices=["lane", "provider", "task", "states", "server"])
    p.add_argument("name", nargs="?")
    p = add("cost", cmd_cost, "tokens and what they are worth")
    p.add_argument("--days", type=int, default=7)
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
    p = add("coordinator", cmd_coordinator, "start the coordinator session in this desk")
    p.add_argument("rest", nargs=argparse.REMAINDER, help="extra arguments passed to claude")
    add("help", cmd_help, "print the agent card")

    a = ap.parse_args(argv)
    if not getattr(a, "fn", None):
        ap.print_help()
        return 0
    if a.cmd == "add" and not a.text and not a.file:
        ap.error("add needs the task text or --file")
    a.fn(a)
    return 0
