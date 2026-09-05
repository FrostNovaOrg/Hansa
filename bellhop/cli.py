from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from . import accept as accept_mod
from . import archive as archive_mod
from . import card as card_mod
from . import daemon as daemon_mod
from . import hooks as hooks_mod
from . import decisions as decisions_mod
from . import desk_ops
from . import ledger as ledger_mod
from . import merge as merge_mod
from . import readings as readings_mod
from . import ring as ring_mod
from . import sampling
from . import seal as seal_mod
from . import sentinel as sentinel_mod
from . import watch as watch_mod
from .config import find_desk
from .packet import next_packet_id, render_packet
from .util import BellhopError, slugify, ts, write


def _desk(args):
    return find_desk(Path(args.desk) if getattr(args, "desk", None) else None)


STATE_FILES = ("state/next.md", "state/in-flight.md", "state/debts.md", "state/human-todo.md")


def _commit(desk, args, paths, message):
    """Coordinator commands archive what they touched, unless --no-commit was given."""
    if getattr(args, "no_commit", False):
        return
    try:
        sha = archive_mod.archive(desk, [desk.path(p) if isinstance(p, str) else p for p in paths], message)
        print(f"archived at {sha}")
    except BellhopError as exc:
        print(f"note: not archived ({exc})")


def cmd_init(args):
    desk = desk_ops.init_desk(Path(args.dir), force=args.force)
    print(f"desk ready at {desk.root}")
    print("next: edit bellhop.json (lanes), then `bellhop packet new --lane <lane> --title '...'`")


def cmd_packet_new(args):
    desk = _desk(args)
    desk.lane(args.lane)
    pid = args.id or next_packet_id(desk.inbox)
    base = args.base or "HEAD"
    tpl = desk.template("packet.md")
    text = render_packet(tpl, id=pid, lane=args.lane, title=args.title, base=base,
                         budget_minutes=args.budget, acceptance="- replace me with a checkable statement")
    path = desk.inbox / args.lane / f"{pid}-{slugify(args.title)}.md"
    if path.exists():
        raise BellhopError(f"packet exists: {path}")
    write(path, text)
    print(path)


def cmd_ring(args):
    desk = _desk(args)
    bell = ring_mod.ring(desk, Path(args.packet))
    print(f"rung: {bell.relative_to(desk.root)}")


def cmd_watch(args):
    desk = _desk(args)
    if args.loop:
        print(f"watching {desk.bells_pending} every {args.interval}s (SIGTERM to stop)")
        watch_mod.watch_loop(desk, interval=args.interval, archive=not args.no_archive)
        return
    result = watch_mod.watch_once(desk, archive=not args.no_archive)
    if result is None:
        print("no bell to answer")
    else:
        print(f"{'DEAD' if result['dead'] else 'done'}: {result['id']} on {result['lane']} in {result['duration_s']}s cost={result.get('cost_usd')}")
        if result["dead"]:
            sys.exit(2)


def cmd_archive(args):
    desk = _desk(args)
    sha = archive_mod.archive(desk, [Path(p) for p in args.paths], args.message)
    print(f"archived at {sha}")


def cmd_sentinel(args):
    desk = _desk(args)
    green, text = sentinel_mod.sentinel(desk, wake=not args.no_wake)
    print(text, end="")
    sys.exit(0 if green else 1)


def cmd_ledger(args):
    desk = _desk(args)
    if args.ledger_cmd == "add":
        body = args.body if args.body is not None else sys.stdin.read()
        path = ledger_mod.add(desk, args.title, body, refs=args.ref or [])
        print(path.relative_to(desk.root))
        _commit(desk, args, [path, *STATE_FILES], f"ledger: {args.title}")
    else:
        count, size = ledger_mod.stats(desk)
        print(f"{count} entries, {size} bytes")


def cmd_decide(args):
    desk = _desk(args)
    decisions_mod.decide(desk, args.words, who=args.who, scope=args.scope or "", expires=args.expires, standing=args.standing)
    print("recorded")
    _commit(desk, args, ["state/decisions.md"], f"decision by {args.who}")


def cmd_card(args):
    desk = _desk(args)
    if args.show:
        for k, v in card_mod.read_card(desk).items():
            print(f"{k}: {v}")
        return
    fields = {k: getattr(args, k) for k in ("ready", "next", "lane", "reason", "queue", "by")}
    card = card_mod.refresh_card(desk, **fields)
    print(f"card refreshed {card['refreshed']}: ready={card.get('ready')} next={card.get('next') or '-'}")


def cmd_sample(args):
    desk = _desk(args)
    hit, seed, digest = sampling.sampled(desk, args.id)
    print(f"{'sampled' if hit else 'not sampled'} id={args.id} seed={seed} digest={digest} rate=1/{desk.cfg['sample_rate']}")
    sys.exit(0 if hit else 3)


def cmd_accept(args):
    desk = _desk(args)
    out = accept_mod.accept(desk, args.id)
    print(out.relative_to(desk.root))
    _commit(desk, args, [out, desk.bells_logs / f"accept-{args.id}.prompt.md"], f"acceptance {args.id}")


def cmd_merge(args):
    desk = _desk(args)
    sha = merge_mod.merge(desk, args.id, push=args.push, verdict=args.verdict or "")
    print(f"merged at {sha}")
    _commit(desk, args, ["ledger", *STATE_FILES], f"merged {args.id} at {sha}")


def cmd_readings(args):
    desk = _desk(args)
    out = readings_mod.report(desk, week=args.week, transcripts=args.transcripts)
    print(out.relative_to(desk.root))
    _commit(desk, args, [out], f"readings {out.stem}")


def cmd_seal(args):
    desk = _desk(args)
    if args.verify:
        changed = seal_mod.verify(desk)
        if changed:
            print("changed since seal:\n  " + "\n  ".join(changed))
            sys.exit(1)
        print("seals hold")
        return
    p = seal_mod.seal(desk)
    print(f"sealed: {p.relative_to(desk.root)}")
    _commit(desk, args, [p, "rules", "frozen", ".claude", "templates"], "reseal")


def cmd_close(args):
    desk = _desk(args)
    print(desk_ops.close(desk))
    _commit(desk, args, ["ledger", "state/.sentinel/status.txt", *STATE_FILES], "day close")


def cmd_coldstart(args):
    desk = _desk(args)
    print(desk_ops.coldstart(desk), end="")


def cmd_daemon(args):
    desk = _desk(args)
    if args.daemon_cmd == "install":
        report = daemon_mod.install(desk, load=args.load, python=args.python)
        print(f"platform: {report['platform']}")
        print("service files written:")
        for f in report["files"]:
            print(f"  {f}")
        if report["loaded"]:
            print("loaded: yes")
        else:
            print("loaded: no" + (" (errors below)" if report["errors"] else " (run again with --load, or run these commands):"))
            for c in report["load_commands"]:
                print(f"  {c}")
        for e in report["errors"]:
            print(f"  error: {e}")
        _commit(desk, args, ["state/.daemon.json"], "resident service installed")
        if report["errors"]:
            sys.exit(1)
    elif args.daemon_cmd == "uninstall":
        report = daemon_mod.uninstall(desk)
        print("removed:\n  " + "\n  ".join(report["removed"]) if report["removed"] else "no service files to remove")
        for e in report["errors"]:
            print(f"  note: {e}")
    elif args.daemon_cmd == "status":
        print(daemon_mod.status_text(desk), end="")
        s = daemon_mod.status(desk)
        sys.exit(0 if (s["heartbeat_fresh"] or not s["installed"]) else 1)
    elif args.daemon_cmd == "files":
        for path, content in daemon_mod.service_files(desk, daemon_mod.platform_name(), args.python).items():
            print(f"===== {path}\n{content}")


def cmd_mcp(args):
    from . import mcp_server
    sys.exit(mcp_server.main(Path(args.desk) if args.desk else None))


def cmd_hook(args):
    sys.exit(hooks_mod.main(args.event))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bellhop", description="File-based operations for a team of Claude Code agents.")
    p.add_argument("--version", action="version", version=f"bellhop {__version__}")
    p.add_argument("--desk", help="desk directory (default: found from the current directory or $BELLHOP_DESK)")
    p.add_argument("--no-commit", action="store_true", help="do not archive the files this command touched")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="create a desk from the template"); s.add_argument("dir"); s.add_argument("--force", action="store_true"); s.set_defaults(fn=cmd_init)

    s = sub.add_parser("packet", help="packet tools"); ss = s.add_subparsers(dest="packet_cmd", required=True)
    n = ss.add_parser("new", help="write a packet skeleton in the inbox"); n.add_argument("--lane", required=True); n.add_argument("--title", required=True)
    n.add_argument("--base", help="base commit (default HEAD)"); n.add_argument("--id"); n.add_argument("--budget", type=int, default=45); n.set_defaults(fn=cmd_packet_new)

    s = sub.add_parser("ring", help="ring a bell for a packet"); s.add_argument("packet"); s.set_defaults(fn=cmd_ring)

    s = sub.add_parser("watch", help="answer bells (watchdog)"); s.add_argument("--loop", action="store_true"); s.add_argument("--once", action="store_true")
    s.add_argument("--interval", type=int, default=5); s.add_argument("--no-archive", action="store_true"); s.set_defaults(fn=cmd_watch)

    s = sub.add_parser("archive", help="commit desk files"); s.add_argument("-m", "--message", required=True); s.add_argument("paths", nargs="+"); s.set_defaults(fn=cmd_archive)

    s = sub.add_parser("sentinel", help="run the checks"); s.add_argument("--no-wake", action="store_true"); s.set_defaults(fn=cmd_sentinel)

    s = sub.add_parser("ledger", help="ledger entries"); ss = s.add_subparsers(dest="ledger_cmd", required=True)
    a = ss.add_parser("add"); a.add_argument("--title", required=True); a.add_argument("--body", help="entry body (default: stdin)"); a.add_argument("--ref", action="append")
    ss.add_parser("stats"); s.set_defaults(fn=cmd_ledger)

    s = sub.add_parser("decide", help="append a decision, verbatim"); s.add_argument("words"); s.add_argument("--who", default="principal")
    s.add_argument("--scope"); s.add_argument("--expires", default="until revoked"); s.add_argument("--standing", action="store_true"); s.set_defaults(fn=cmd_decide)

    s = sub.add_parser("card", help="refresh or show the dispatch card"); s.add_argument("--show", action="store_true")
    for k in ("ready", "next", "lane", "reason", "queue", "by"):
        s.add_argument(f"--{k}")
    s.set_defaults(fn=cmd_card)

    s = sub.add_parser("sample", help="is this id sampled for acceptance? (exit 0 yes, 3 no)"); s.add_argument("id"); s.set_defaults(fn=cmd_sample)
    s = sub.add_parser("accept", help="run acceptance in a fresh read-only session"); s.add_argument("id"); s.set_defaults(fn=cmd_accept)
    s = sub.add_parser("merge", help="merge a lane branch into its target (coordinator only)"); s.add_argument("id"); s.add_argument("--push", action="store_true"); s.add_argument("--verdict"); s.set_defaults(fn=cmd_merge)
    s = sub.add_parser("readings", help="weekly readings"); s.add_argument("--week"); s.add_argument("--transcripts", action="append", help="Claude Code project directory to summarise"); s.set_defaults(fn=cmd_readings)
    s = sub.add_parser("seal", help="record or verify seals"); s.add_argument("--verify", action="store_true"); s.set_defaults(fn=cmd_seal)
    sub.add_parser("close", help="daily close").set_defaults(fn=cmd_close)
    sub.add_parser("coldstart", help="print the reading list and check state files").set_defaults(fn=cmd_coldstart)

    s = sub.add_parser("daemon", help="resident watchdog and sentinel service"); ss = s.add_subparsers(dest="daemon_cmd", required=True)
    i = ss.add_parser("install", help="vendor Bellhop into the desk and write service files"); i.add_argument("--load", action="store_true", help="also load the services now")
    i.add_argument("--python", help="python interpreter for the service (default: this one)")
    ss.add_parser("uninstall", help="unload and remove the service files")
    ss.add_parser("status", help="installed? running? heartbeat? (exit 1 if installed but no fresh heartbeat)")
    f = ss.add_parser("files", help="print the service files without writing them"); f.add_argument("--python")
    s.set_defaults(fn=cmd_daemon)

    sub.add_parser("mcp", help="run the MCP server on stdio (used by the Claude Code plugin)").set_defaults(fn=cmd_mcp)
    s = sub.add_parser("hook", help="Claude Code hook entry point"); s.add_argument("event", choices=["session-start", "stop"]); s.set_defaults(fn=cmd_hook)
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.fn(args)
    except BellhopError as exc:
        print(f"bellhop: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
