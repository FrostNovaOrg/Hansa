from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from . import accept as accept_mod
from . import archive as archive_mod
from . import board as board_mod
from . import card as card_mod
from . import closetask as closetask_mod
from . import daemon as daemon_mod
from . import headers as headers_mod
from . import hand as hand_mod
from . import hooks as hooks_mod
from . import intake as intake_mod
from . import decisions as decisions_mod
from . import dispatch as dispatch_mod
from . import desk_ops
from . import lanes as lanes_mod
from . import ledger as ledger_mod
from . import merge as merge_mod
from . import readings as readings_mod
from . import ring as ring_mod
from . import sampling
from . import seal as seal_mod
from . import sentinel as sentinel_mod
from . import state as state_mod
from . import stopdispatch as stop_mod
from . import watch as watch_mod
from .config import find_desk
from .packet import next_packet_id, parse_packet, render_packet
from .util import BellhopError, read, slugify, ts, write


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


def cmd_dispatch(args):
    """Write the packet, ring, start the in-flight row, refresh the card -- and echo four lines."""
    desk = _desk(args)
    report = dispatch_mod.dispatch(
        desk, args.lane, args.title, args.body,
        inflight=args.inflight or "", next_=args.next, continue_id=getattr(args, "continue"),
        return_id=args.return_, heavy=args.heavy, allow=args.allow or [],
        exception=args.exception, demo=args.demo, commit=not getattr(args, "no_commit", False),
    )
    if args.demo and report.packet_text:
        print(report.packet_text, end="" if report.packet_text.endswith("\n") else "\n")
    print(report.render())
    sys.exit(report.code)


def cmd_ring(args):
    desk = _desk(args)
    bell = ring_mod.ring(desk, Path(args.packet), commit=not getattr(args, "no_commit", False))
    print(f"rung: {bell.relative_to(desk.root)}")


def cmd_lane(args):
    desk = _desk(args)
    try:
        if args.lane_cmd == "reset":
            report = lanes_mod.lane_reset(desk, args.lane, dry_run=args.dry_run, force=args.force)
        else:
            report = lanes_mod.lane_align(desk, args.lane)
    except lanes_mod.LaneBlocked as exc:
        print(f"🛑 {exc}")
        sys.exit(2)
    for line in report["steps"]:
        print(f"  {line}")
    print(report["last"])


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
    green, text = sentinel_mod.sentinel(desk, wake=not args.no_wake, dry_run=args.dry_run)
    print(text, end="")
    sys.exit(0 if green else 1)


def cmd_stop_dispatch(args):
    """The switch that stops dispatch is a file, so it works without anyone reading it."""
    desk = _desk(args)
    if args.stop_cmd == "set":
        order = stop_mod.set_stop(desk, args.reason, by=args.by)
        print(f"dispatch stopped at {order['stopped_at']}: {order['reason']}")
        print(f"  {stop_mod.stop_path(desk)}")
        _commit(desk, args, [stop_mod.STOP_FILE], f"dispatch stopped: {order['reason']}")
    elif args.stop_cmd == "lift":
        order = stop_mod.lift(desk, note=args.note or "")
        print(f"dispatch allowed again (stopped at {order['stopped_at']}, {len(order['exceptions'])} exception(s))")
        print(f"  close-out: {order['closing']}")
        _commit(desk, args, [stop_mod.STOP_FILE, "state/closings.md"], "dispatch stop lifted")
    elif args.stop_cmd == "exception":
        order = stop_mod.add_exception(desk, args.reason)
        n = len(order["exceptions"])
        print(f"exception {n} recorded in {stop_mod.stop_path(desk).name}: {order['exceptions'][-1]}")
        if n >= stop_mod.PIERCED_AT:
            print(f"the stop order has now been pierced {n} times; the sentinel is red until it is lifted or obeyed")
        _commit(desk, args, [stop_mod.STOP_FILE], f"dispatch stop exception {n}")
    else:
        print(stop_mod.status_text(desk), end="")
        allowed, _reason = stop_mod.dispatch_allowed(desk)
        sys.exit(0 if allowed else 1)


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


def _desk_or_none(args):
    """The checkers read a file, not a desk; a desk only supplies the soft caps."""
    try:
        return _desk(args)
    except BellhopError:
        return None


def cmd_receipt_header(args):
    desk = _desk(args)
    receipt = Path(args.receipt)
    packet = None
    if args.packet:
        packet = parse_packet(Path(args.packet))
    else:
        task = args.task or receipt.stem.split("-", 1)[0]
        try:
            packet = headers_mod.find_task_packet(desk, task)
        except BellhopError:
            packet = None  # no packet means no base; the header says so instead of guessing
    try:
        if args.dry_run:
            values = headers_mod.compute_receipt_values(desk, receipt, args.lane, packet=packet,
                                                        task=args.task, result=args.result_json)
            print(headers_mod.render_header("receipt", values), end="")
            return
        values, action = headers_mod.write_receipt_header(desk, receipt, args.lane, packet=packet,
                                                          task=args.task, result=args.result_json)
    except BellhopError as exc:
        print(f"bellhop: {exc}", file=sys.stderr)
        sys.exit(headers_mod.EXIT_MALFORMED)
    print(headers_mod.render_header("receipt", values), end="")
    print(f"header {action} in {receipt}")


def cmd_acceptance_header(args):
    desk = _desk_or_none(args)
    path = Path(args.file)
    try:
        if args.dry_run:
            values = headers_mod.compute_acceptance_values(path, desk=desk, lane=args.lane, task=args.task)
            print(headers_mod.render_header("acceptance", values), end="")
            return
        values, action = headers_mod.write_acceptance_header(path, desk=desk, lane=args.lane, task=args.task)
    except BellhopError as exc:
        print(f"bellhop: {exc}", file=sys.stderr)
        sys.exit(headers_mod.EXIT_MALFORMED)
    print(headers_mod.render_header("acceptance", values), end="")
    print(f"header {action} in {path}")


def cmd_check_receipt(args):
    packet = parse_packet(Path(args.packet)) if args.packet else None
    report = headers_mod.check_receipt(Path(args.file), packet=packet)
    print(report.text(), end="")
    sys.exit(report.code)


def cmd_check_acceptance(args):
    desk = _desk_or_none(args)
    cap = int(desk.cfg.get("acceptance_bytes", headers_mod.DEFAULT_ACCEPTANCE_BYTES)) if desk else headers_mod.DEFAULT_ACCEPTANCE_BYTES
    report = headers_mod.check_acceptance(Path(args.file), max_bytes=cap)
    print(report.text(), end="")
    sys.exit(report.code)


def cmd_header_spec(args):
    for kind in (["receipt", "acceptance"] if args.kind == "both" else [args.kind]):
        print(f"### The `{headers_mod.FENCES[kind]}` block\n")
        print(headers_mod.spec_table(kind))


def cmd_accept(args):
    desk = _desk(args)
    out = accept_mod.accept(desk, args.id)
    print(out.relative_to(desk.root))
    check = accept_mod.check_path(desk, args.id)
    if check.exists():
        print(f"{check.relative_to(desk.root)}: {read(check).strip().splitlines()[-1]}")
    _commit(desk, args, [out, check, desk.bells_logs / f"accept-{args.id}.prompt.md"], f"acceptance {args.id}")


def cmd_merge(args):
    desk = _desk(args)
    sha = merge_mod.merge(desk, args.id, push=args.push, verdict=args.verdict or "")
    print(f"merged at {sha}")
    _commit(desk, args, ["ledger", *STATE_FILES], f"merged {args.id} at {sha}")


def cmd_close_task(args):
    """Six named steps in one command; the last line says where it stopped."""
    desk = _desk(args)
    try:
        closetask_mod.close_task(desk, args.id, next_card=args.next, note=args.note,
                                 dry_run=args.dry_run,
                                 commit=not getattr(args, "no_commit", False))
    except closetask_mod.CloseStopped as exc:
        print(exc.last_line)
        sys.exit(exc.code)


def cmd_settle_bell(args):
    desk = _desk(args)
    try:
        closetask_mod.settle_bell(desk, args.id, title=args.title or "", say=print)
    except state_mod.StateError as exc:
        print(exc.render(), file=sys.stderr)
        sys.exit(exc.code)


def cmd_readings(args):
    desk = _desk(args)
    out = readings_mod.report(desk, week=args.week, transcripts=args.transcripts)
    print(out.relative_to(desk.root))
    daily = desk.path(readings_mod.READINGS_FILE)
    if daily.exists():
        print(daily.relative_to(desk.root))
    _commit(desk, args, [out, daily], f"readings {out.stem}")


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
    # No paths: the close archives the whole roster. Desk configuration -- bellhop.json,
    # rules/, .claude/ -- is edited by hand between tasks and no other command claims it,
    # so this is where it enters git. A day that ends with an untracked rule change is a
    # day whose history and whose desk say different things.
    _commit(desk, args, [], "day close")


def cmd_coldstart(args):
    desk = _desk(args)
    print(desk_ops.coldstart(desk), end="")


def cmd_daemon(args):
    desk = _desk(args)
    if args.daemon_cmd == "install":
        report = daemon_mod.install(desk, load=args.load, python=args.python)
        print(f"platform: {report['platform']}")
        print(
            "autostart: "
            + ("yes (the services come up at boot/login)" if report["autostart"]
               else "no (after a reboot, start them by hand with the load commands below)")
        )
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


def cmd_board(args):
    """One page, read once. Everything a turn starts from, quoted from the files."""
    print(board_mod.board_text(_desk(args), cap=args.bytes), end="")


def cmd_intake(args):
    desk = _desk(args)
    if args.intake_cmd == "show":
        try:
            print(intake_mod.show_text(desk, args.number), end="")
        except KeyError as exc:
            raise BellhopError(str(exc).strip("'"))
        return
    print(intake_mod.list_text(desk, last=args.last), end="")


def cmd_hand(args):
    desk = _desk(args)
    if args.hand_cmd == "open":
        print(hand_mod.open_window(desk, minutes=args.minutes, reason=args.reason))
    elif args.hand_cmd == "close":
        print(hand_mod.close_window(desk))
    else:
        print(hand_mod.status_text(desk), end="")


def cmd_hook(args):
    sys.exit(hooks_mod.main(args.event))


def cmd_state(args):
    """Thin layer over bellhop.state: run the op, print what changed, carry its exit code."""
    desk = _desk(args)
    try:
        results = args.state_fn(desk, args)
    except state_mod.WrapupStopped as exc:
        for done in exc.done:
            print(done.render())
        print(exc.render(), file=sys.stderr)
        sys.exit(exc.code)
    except state_mod.StateError as exc:
        print(exc.render(), file=sys.stderr)
        sys.exit(exc.code)
    if isinstance(results, state_mod.Result):
        results = [results]
    for result in results:
        print(result.render())


def _card_flags(ns, prefix=""):
    return {k: getattr(ns, (prefix + k).replace("-", "_"), None) for k in state_mod.CARD_FLAGS}


def add_state_parser(sub):
    """`bellhop state <target> <op>` - one write entry for every state file."""
    st = sub.add_parser("state", help="the single write entry for the desk's state files")
    st.set_defaults(fn=cmd_state)
    targets = st.add_subparsers(dest="state_target", required=True)

    def ops(name, help_text):
        return targets.add_parser(name, help=help_text).add_subparsers(dest="state_op", required=True)

    o = ops("ledger", "append a ledger entry; the number is assigned here")
    p = o.add_parser("add", help="append one entry from a file; over the byte cap it is compressed, then split")
    p.add_argument("--file", required=True, help="file holding the entry ('-' reads stdin)")
    p.add_argument("--ref", action="append", help="a task id this entry refers to (repeatable)")
    p.set_defaults(state_fn=lambda d, ns: state_mod.ledger_add(d, ns.file, refs=ns.ref or []))

    o = ops("card", "the dispatch card")
    p = o.add_parser("set", help="set only the given keys; what the keys mean lives in rules/card.md")
    p.add_argument("--dispatchable", choices=["yes", "no"])
    for k in ("next", "lane", "reason", "pool"):
        p.add_argument(f"--{k}")
    p.set_defaults(state_fn=lambda d, ns: state_mod.card_set(d, **_card_flags(ns)))

    o = ops("inflight", "who is doing what right now")
    p = o.add_parser("start", help="add one line")
    p.add_argument("--text", required=True)
    p.set_defaults(state_fn=lambda d, ns: state_mod.inflight_start(d, ns.text))
    p = o.add_parser("finish", help="remove the line for a task and echo it")
    p.add_argument("--task", required=True)
    p.set_defaults(state_fn=lambda d, ns: state_mod.inflight_finish(d, ns.task))
    p = o.add_parser("tag", help="append a tag to the line for a task")
    p.add_argument("--task", required=True)
    p.add_argument("--tag", default="settled-awaiting-close")
    p.set_defaults(state_fn=lambda d, ns: state_mod.inflight_tag(d, ns.task, ns.tag))

    o = ops("debts", "what is owed")
    p = o.add_parser("add", help="add one debt line")
    p.add_argument("--text", required=True)
    p.set_defaults(state_fn=lambda d, ns: state_mod.debts_add(d, ns.text))
    p = o.add_parser("settle", help="move the matching line to state/debts-settled.md")
    p.add_argument("--match", required=True)
    p.add_argument("--keep", action="store_true", help="leave a struck-through copy in place")
    p.set_defaults(state_fn=lambda d, ns: state_mod.debts_settle(d, ns.match, keep=ns.keep))
    p = o.add_parser("set", help="replace the matching line in place, keeping its position")
    p.add_argument("--match", required=True)
    p.add_argument("--text", required=True)
    p.set_defaults(state_fn=lambda d, ns: state_mod.debts_set(d, ns.match, ns.text))

    o = ops("todo", "things only the principal can do")
    p = o.add_parser("add", help="add one line; it must carry a `why only you:` clause")
    p.add_argument("--text", required=True)
    p.set_defaults(state_fn=lambda d, ns: state_mod.todo_add(d, ns.text))
    p = o.add_parser("settle", help="move the matching open line to Done")
    p.add_argument("--match", required=True)
    p.set_defaults(state_fn=lambda d, ns: state_mod.todo_settle(d, ns.match))

    o = ops("own", "the coordinator's own work list")
    p = o.add_parser("add", help="add one line")
    p.add_argument("--text", required=True)
    p.set_defaults(state_fn=lambda d, ns: state_mod.own_add(d, ns.text))
    p = o.add_parser("settle", help="remove the matching line")
    p.add_argument("--match", required=True)
    p.set_defaults(state_fn=lambda d, ns: state_mod.own_settle(d, ns.match))
    o.add_parser("list", help="print the open lines").set_defaults(state_fn=lambda d, ns: state_mod.own_list(d))

    o = ops("selfcatch", "mistakes the coordinator caught in itself")
    p = o.add_parser("add", help="append one catch")
    p.add_argument("--root", required=True)
    p.add_argument("--kind", required=True, choices=list(state_mod.SELFCATCH_KINDS))
    p.add_argument("--cause", required=True, help="one sentence; a stub or the root id itself is refused")
    p.set_defaults(state_fn=lambda d, ns: state_mod.selfcatch_add(d, ns.root, ns.kind, ns.cause))
    p = o.add_parser("hook-hit", help="count a hook hit; the human count is never touched")
    p.add_argument("--root", required=True)
    p.set_defaults(state_fn=lambda d, ns: state_mod.selfcatch_hook_hit(d, ns.root))
    o.add_parser("table", help="print the per-root counts, recomputed from the rows").set_defaults(
        state_fn=lambda d, ns: state_mod.selfcatch_table(d))

    o = ops("hotrules", "the ten rule slots in .claude/CLAUDE.md")
    p = o.add_parser("promote", help="put one rule in a slot")
    p.add_argument("--root", required=True)
    p.add_argument("--text", required=True, help="one line; no date, no task id, no incident/failure")
    p.set_defaults(state_fn=lambda d, ns: state_mod.hotrules_promote(d, ns.root, ns.text))
    p = o.add_parser("retire", help="remove a rule and record it with its reason")
    p.add_argument("--root", required=True)
    p.add_argument("--reason", required=True)
    p.set_defaults(state_fn=lambda d, ns: state_mod.hotrules_retire(d, ns.root, ns.reason))
    o.add_parser("list", help="print the segment").set_defaults(state_fn=lambda d, ns: state_mod.hotrules_list(d))

    o = ops("closings", "the one-line close-out register")
    p = o.add_parser("add", help="append one close-out line")
    p.add_argument("--task", required=True)
    p.add_argument("--line", required=True)
    p.set_defaults(state_fn=lambda d, ns: state_mod.closings_add(d, ns.task, ns.line))
    p = o.add_parser("unread", help="print the lines after the pointer and advance it")
    p.add_argument("--peek", action="store_true", help="print them without advancing the pointer")
    p.set_defaults(state_fn=lambda d, ns: state_mod.closings_unread(d, advance=not ns.peek))

    p = targets.add_parser("wrapup", help="own list, then in flight, then debts, then the card, in that order")
    p.add_argument("--own")
    p.add_argument("--inflight-finish", dest="inflight_finish")
    p.add_argument("--debts-add", dest="debts_add")
    p.add_argument("--card-dispatchable", dest="card_dispatchable", choices=["yes", "no"])
    for k in ("next", "lane", "reason", "pool"):
        p.add_argument(f"--card-{k}", dest=f"card_{k}")
    p.set_defaults(state_op="wrapup", state_fn=lambda d, ns: state_mod.wrapup(
        d, own=ns.own, inflight_finish_id=ns.inflight_finish, debts_add_text=ns.debts_add,
        card=_card_flags(ns, "card_")))
    return st


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

    s = sub.add_parser("dispatch", help="write the packet, ring, start the in-flight row, refresh the card")
    s.add_argument("--lane", required=True)
    s.add_argument("--title", required=True, help="a short title; the id and the slug are computed")
    s.add_argument("--body", required=True, help="file holding the packet body ('-' reads stdin)")
    s.add_argument("--inflight", help="the one human sentence on the in-flight row")
    s.add_argument("--next", help="set the card's `next`; without it the card is not touched")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--continue", dest="continue", metavar="ID", help="pick up a packet that stopped without a receipt")
    g.add_argument("--return", dest="return_", metavar="ID", help="re-send a packet whose receipt was refused")
    s.add_argument("--heavy", action="store_true", help="mark the packet `effort: heavy`")
    s.add_argument("--allow", action="append", metavar="PATTERN", help="waive one forbidden-command pattern for this packet only (repeatable)")
    s.add_argument("--exception", metavar="REASON", help="dispatch under a stop-dispatch order; the reason is recorded and counted")
    s.add_argument("--demo", action="store_true", help="render the packet to stdout and write nothing")
    s.set_defaults(fn=cmd_dispatch)

    s = sub.add_parser("ring", help="ring a bell for a packet"); s.add_argument("packet"); s.set_defaults(fn=cmd_ring)

    s = sub.add_parser("lane", help="lane worktree maintenance"); ss = s.add_subparsers(dest="lane_cmd", required=True)
    r = ss.add_parser("reset", help="discard a lane's uncommitted work and put its branch back on the task base")
    r.add_argument("lane"); r.add_argument("--dry-run", action="store_true", help="print what it would do, change nothing")
    r.add_argument("--force", action="store_true", help="proceed even if the lane is busy or holds unmerged commits")
    a = ss.add_parser("align", help="fast-forward a clean, fully merged lane branch onto its base branch")
    a.add_argument("lane")
    s.set_defaults(fn=cmd_lane)

    s = sub.add_parser("watch", help="answer bells (watchdog)"); s.add_argument("--loop", action="store_true"); s.add_argument("--once", action="store_true")
    s.add_argument("--interval", type=int, default=5); s.add_argument("--no-archive", action="store_true"); s.set_defaults(fn=cmd_watch)

    s = sub.add_parser("archive", help="commit desk files"); s.add_argument("-m", "--message", required=True); s.add_argument("paths", nargs="+"); s.set_defaults(fn=cmd_archive)

    s = sub.add_parser("sentinel", help="run the checks; every run logs one line to state/sentinel.log")
    s.add_argument("--no-wake", action="store_true", help="run the checks but never run wake_cmd")
    s.add_argument("--dry-run", action="store_true", help="say what would happen (wake, clear, compact) and write nothing")
    s.set_defaults(fn=cmd_sentinel)

    s = sub.add_parser("stop-dispatch", help="the stop-dispatch switch: a file whose presence stops dispatch")
    ss = s.add_subparsers(dest="stop_cmd", required=True)
    a = ss.add_parser("set", help="stop dispatch until someone lifts it")
    a.add_argument("--reason", required=True, help="why; without it nobody else can decide whether to lift it")
    a.add_argument("--by", default="coordinator")
    a = ss.add_parser("lift", help="allow dispatch again and record the close-out")
    a.add_argument("--note", help="anything to add to the close-out line")
    a = ss.add_parser("exception", help="dispatch anyway, once, and write it into the order")
    a.add_argument("--reason", required=True)
    ss.add_parser("status", help="print the order (exit 1 while dispatch is stopped)")
    s.set_defaults(fn=cmd_stop_dispatch)

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
    s = sub.add_parser("receipt-header", help="compute a receipt's machine header from real commands and write it at the top")
    s.add_argument("--receipt", required=True); s.add_argument("--lane", required=True); s.add_argument("--task", help="packet id (default: the leading token of the receipt file name)")
    s.add_argument("--packet", help="packet file (default: found in the inbox by task id)"); s.add_argument("--result-json", help="worker result JSON (<id>.meta.json, or a CLI result object)")
    s.add_argument("--dry-run", action="store_true", help="print the header, write nothing"); s.set_defaults(fn=cmd_receipt_header)

    s = sub.add_parser("acceptance-header", help="read an acceptance verdict's body and write its machine header at the top")
    s.add_argument("--file", required=True); s.add_argument("--lane", help="lane, so paths outside its worktree can be counted"); s.add_argument("--task")
    s.add_argument("--dry-run", action="store_true", help="print the header, write nothing"); s.set_defaults(fn=cmd_acceptance_header)

    s = sub.add_parser("check-receipt", help="check a receipt's header (exit 1 on a red finding)")
    s.add_argument("file"); s.add_argument("--packet", help="packet file, to check the base"); s.set_defaults(fn=cmd_check_receipt)

    s = sub.add_parser("check-acceptance", help="check an acceptance verdict's header (exit 1 on a red finding)")
    s.add_argument("file"); s.set_defaults(fn=cmd_check_acceptance)

    s = sub.add_parser("header-spec", help="print the header spec straight from the code")
    s.add_argument("kind", nargs="?", default="both", choices=["receipt", "acceptance", "both"]); s.set_defaults(fn=cmd_header_spec)

    s = sub.add_parser("merge", help="merge a lane branch into its target (coordinator only)"); s.add_argument("id"); s.add_argument("--push", action="store_true"); s.add_argument("--verdict"); s.set_defaults(fn=cmd_merge)
    s = sub.add_parser("readings", help="weekly readings"); s.add_argument("--week"); s.add_argument("--transcripts", action="append", metavar="PATH", help="a Claude Code transcript, or a directory of them; repeatable"); s.set_defaults(fn=cmd_readings)

    s = sub.add_parser("close-task", help="close one task in one command: evidence, fileset, merge, ledger, state, archive")
    s.add_argument("id"); s.add_argument("--next", help="what goes on the card as the next thing")
    s.add_argument("--note", help="one sentence for the ledger entry")
    s.add_argument("--dry-run", action="store_true", help="run the two gates and print what the rest would do; write nothing")
    s.set_defaults(fn=cmd_close_task)

    s = sub.add_parser("settle-bell", help="the three things done the moment a worker exits: tag in flight, refresh the card's queue, record the close-out")
    s.add_argument("id"); s.add_argument("--title", help="the task's title, for the lines it writes")
    s.set_defaults(fn=cmd_settle_bell)
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

    add_state_parser(sub)

    sub.add_parser("mcp", help="run the MCP server on stdio (used by the Claude Code plugin)").set_defaults(fn=cmd_mcp)
    s = sub.add_parser("board", help="the one page: everything a turn starts from, quoted and byte-capped")
    s.add_argument("--bytes", type=int, help="byte cap for this run (default: config board_bytes)"); s.set_defaults(fn=cmd_board)

    s = sub.add_parser("intake", help="the principal's words, as the UserPromptSubmit hook logged them"); ss = s.add_subparsers(dest="intake_cmd", required=True)
    a = ss.add_parser("list", help="one line per entry"); a.add_argument("--last", type=int, help="only the last K entries")
    a = ss.add_parser("show", help="print one entry in full"); a.add_argument("number", type=int)
    s.set_defaults(fn=cmd_intake)

    s = sub.add_parser("hand", help="the hand-edit window: lane repositories are gated until one is open"); ss = s.add_subparsers(dest="hand_cmd", required=True)
    a = ss.add_parser("open", help="declare a hand-edit window"); a.add_argument("--minutes", type=int, help="default: config hand_window_minutes")
    a.add_argument("--reason", required=True, help="why this cannot go to a worker")
    ss.add_parser("close", help="close the window and record what happened inside it")
    ss.add_parser("status", help="is a window open, and for how much longer")
    s.set_defaults(fn=cmd_hand)

    s = sub.add_parser("hook", help="Claude Code hook entry point"); s.add_argument("event", choices=sorted(hooks_mod.EVENTS)); s.set_defaults(fn=cmd_hook)
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
