from __future__ import annotations

import json
import signal
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from . import archive as archive_mod
from . import closetask as closetask_mod
from . import headers as headers_mod
from . import health as health_mod
from . import providers as providers_mod
from .config import Desk
from .locking import desk_lock, serialized, worker_limit, worker_slot
from .lanes import lane_is_busy, lane_lock, prepare_worktree
from .packet import Packet, parse_packet
from .util import BellhopError, append, git, now, read, read_json, run, sha256_file, ts, write, write_json

_stop = False


def _handle_term(signum, frame):
    global _stop
    _stop = True


def build_worker_prompt(desk: Desk, packet: Packet, lane: dict, worktree: Path) -> str:
    tpl = desk.template("worker-prompt.md")
    receipt = desk.receipts / f"{packet.stem}.md"
    fields = {
        "desk": str(desk.root),
        "worktree": str(worktree),
        "lane": packet.lane,
        "id": packet.id,
        "title": packet.title,
        "base": packet.base,
        "packet": str(packet.path),
        "receipt": str(receipt),
        "receipt_template": str(desk.templates / "receipt.md"),
        "core_rules": str(desk.path("rules", "core.md")),
        "lane_rules": str(desk.path("rules", "lanes", f"{packet.lane}.md")),
        "budget_minutes": packet.budget_minutes,
        "acceptance": "\n".join(f"- {a}" for a in packet.acceptance),
    }
    for k, v in fields.items():
        tpl = tpl.replace("{{" + k + "}}", str(v))
    return tpl


def _claude_cmd(desk: Desk, lane: dict, packet: Packet, prompt: str) -> list:
    """The Claude worker command line. The building lives in `providers.py`.

    Kept as a name because it is the shape test's entry point: the lane's `allowed_tools`
    have to reach the command line as `--allowedTools`, and a lane whose allowlist never
    arrives produces a worker that can edit files and run nothing.
    """
    resolved = providers_mod.resolve(desk, lane, packet=packet)
    return providers_mod.claude_command(desk, lane, packet, prompt, resolved)


def launch_worker(desk: Desk, lane: dict, packet: Packet, worktree: Path, prompt: str, log_path: Path,
                  resolved: Optional[providers_mod.Resolved] = None) -> dict:
    """Start one worker session and read what it printed.

    `resolved` says who answers -- the lane's provider, or whatever the schedule window
    and the packet's `effort: heavy` made of it. `process_bell` resolves it before the
    lane lock is taken, so a refusal leaves nothing half-done; a caller that passes
    nothing gets the lane's own answer.
    """
    resolved = resolved or providers_mod.resolve(desk, lane, packet=packet)
    env = providers_mod.worker_env(desk, lane, resolved)
    prompt_file = log_path.with_suffix(".prompt.md")
    write(prompt_file, prompt)
    env.update(
        {
            "BELLHOP_DESK": str(desk.root),
            "BELLHOP_WORKTREE": str(worktree),
            "BELLHOP_PACKET": str(packet.path),
            "BELLHOP_RECEIPT": str(desk.receipts / f"{packet.stem}.md"),
            "BELLHOP_PROMPT_FILE": str(prompt_file),
            "BELLHOP_LANE": packet.lane,
            "BELLHOP_ID": packet.id,
            "BELLHOP_BASE": packet.base,
            "BELLHOP_PROVIDER": resolved.provider,
            "BELLHOP_MODEL": resolved.model,
        }
    )
    cmd = providers_mod.build_command(desk, lane, packet, prompt, resolved, worktree=worktree,
                                      prompt_file=prompt_file, log_path=log_path)
    started = now()
    # The wall clock for the record, a monotonic one for the arithmetic: a machine whose
    # clock steps while a worker runs would otherwise report a negative duration or an
    # extra hour, and the duration is one of the numbers the readings add up.
    elapsed = time.monotonic()
    timeout = max(60, packet.budget_minutes * 60 * 2)
    # -- opt-in worker observation (health.py); off by default ---------------
    watched = health_mod.observation(
        desk, packet.id,
        on_error=lambda exc: _log(desk, f"RED\t{packet.id}: worker health could not be recorded: {exc}"))
    try:
        from .accept import run_session
        if watched is None:
            session = run_session(cmd, worktree, env, timeout)
        else:
            from .worker_session import run_worker
            session = run_worker(
                cmd, worktree, env, timeout, watched,
                on_error=lambda exc: _log(desk, f"RED\t{packet.id}: worker health observation stopped: {exc}"))
        stdout, stderr, code = session["stdout"], session["stderr"], session["exit_code"]
    except Exception as exc:  # timeout or launch failure
        session = {}
        stdout, stderr, code = "", f"worker launch failed: {exc}", 124
    health = watched.finish(code) if watched is not None else None
    if watched is not None and not session.get("observed", True):
        health["observed"] = False
    # -- end worker observation ---------------------------------------------
    duration = time.monotonic() - elapsed
    write(log_path, f"# bellhop worker log {packet.stem}\n\n## stdout\n{stdout}\n\n## stderr\n{stderr}\n")
    parsed = providers_mod.parse_result(stdout, resolved.provider)
    raw = parsed.get("raw") or {}
    # `n/a` is what a person reads; `null` is what the readings parse. `na_none` is the
    # one place the two meet, and neither of them is `0`.
    return {
        # After the schedule override and the heavy mapping, not before: the header has
        # to say who actually answered. The model the CLI reported wins over the one we
        # asked for, because that is the one that ran; when it reports none, the resolved
        # model is the answer, never the lane's unresolved default.
        "provider": resolved.provider,
        "model": providers_mod.na_none(parsed["model"]) or resolved.model,
        "model_asked": resolved.model,
        "effort": resolved.effort or None,
        "heavy": resolved.heavy,
        "schedule_override": resolved.schedule_label or None,
        "turns": providers_mod.na_none(parsed["turns"]),
        # The wall clock, not the CLI's own number: it covers the launch and the exit too.
        "duration_s": round(duration, 1),
        "cost_usd": providers_mod.na_none(parsed["cost_usd"]),
        "stop_reason": providers_mod.na_none(parsed["stop_reason"]),
        # The provider's own two fields, kept unmerged beside the reading above. A run
        # that the transport cancelled prints `error_during_execution` with a native stop
        # reason of `cancelled`; `stop_reason` collapses those into one string, and
        # afterwards nothing in the meta can say whether the model stopped or the
        # channel did. `None` where the provider said nothing, never invented.
        "native_subtype": raw.get("subtype"),
        "native_stop_reason": raw.get("stop_reason"),
        "session_id": raw.get("session_id"),
        "exit_code": code,
        "started_at": ts(started),
        "is_error": bool(raw.get("is_error")) if raw else code != 0,
        "usage": raw.get("usage"),
        # What the observation saw, or `None` on a desk that did not ask for one. Bytes
        # are a proxy for a live session, never for progress; see health.py.
        "health": health,
    }


def _log(desk: Desk, line: str) -> None:
    """One line in the watchdog log. Every provider decision leaves one."""
    append(desk.bells_logs / "watchdog.log", f"{ts()}\t{line}\n")


# --------------------------------------------------------------------------
# bells that keep failing
# --------------------------------------------------------------------------
def dead_bells_dir(desk: Desk) -> Path:
    return desk.path("bells", "dead")


def _failure_file(desk: Desk, bell: Path) -> Path:
    return desk.path("bells", ".locks", "failures", bell.name + ".json")


def bell_failures(desk: Desk, bell: Path) -> int:
    return int((read_json(_failure_file(desk, bell), {}) or {}).get("count", 0))


def clear_bell_failures(desk: Desk, bell: Path) -> None:
    """A bell that was answered starts again from zero; the count is consecutive."""
    path = _failure_file(desk, bell)
    if path.exists():
        try:
            path.unlink()
        except OSError:
            pass


def bell_failure_limit(desk: Desk) -> int:
    try:
        limit = int(desk.cfg.get("bell_max_failures", 3))
    except (TypeError, ValueError):
        limit = 3
    return max(1, limit)


def record_bell_failure(desk: Desk, bell: Path, exc: BaseException) -> tuple:
    """Count one failed attempt on this bell; park it when it has had enough.

    A bell that fails for a reason that is not going to change - a packet whose file
    is gone, a lane whose repository was moved, a marker from an interrupted launch -
    is picked up again on the next tick, and the next, forever. Every pass writes the
    same red line, so the log grows and nothing in it says that this is the hundredth
    time. After `bell_max_failures` consecutive failures the bell moves to
    `bells/dead/` and the sentinel says so: a bell being retried and a bell nobody can
    answer must not look the same on the board.

    Returns (count, parked_path_or_None).
    """
    path = _failure_file(desk, bell)
    data = read_json(path, {}) or {}
    count = int(data.get("count", 0)) + 1
    limit = bell_failure_limit(desk)
    reason = f"{type(exc).__name__}: {exc}"
    write_json(path, {"bell": bell.name, "count": count, "last_error": reason[:500], "last_at": ts()})
    if count < limit or not bell.exists():
        return count, None
    parked = dead_bells_dir(desk) / bell.name
    parked.parent.mkdir(parents=True, exist_ok=True)
    bell.rename(parked)
    _log(desk, f"PARKED\t{bell.name}: {count} consecutive failures; moved to bells/dead/")
    _sentinel_line(desk, f"RED bell {bell.name} parked in bells/dead/ after {count} consecutive "
                         f"failures: {reason[:160]}")
    return count, parked


def _sentinel_line(desk: Desk, text: str) -> None:
    """One line where the operator already looks. The watchdog log is for the machine."""
    try:
        path = desk.path("state", "sentinel.log")
        path.parent.mkdir(parents=True, exist_ok=True)
        append(path, f"{ts()} {text}\n")
    except OSError:
        pass


def dead_bells(desk: Desk) -> list:
    d = dead_bells_dir(desk)
    return sorted(d.glob("*.bell")) if d.exists() else []


def process_bell(desk: Desk, bell: Path, archive: bool = True) -> dict:
    if not bell.exists():
        raise BellhopError(f"bell already claimed or completed: {bell.name}")
    data = json.loads(read(bell))
    packet_path = desk.root / data["packet"]
    if not packet_path.exists():
        raise BellhopError(f"bell {bell.name}: packet is gone: {packet_path}")
    if sha256_file(packet_path) != data.get("sha256"):
        raise BellhopError(f"bell {bell.name}: packet changed after the bell was rung; ring it again")
    packet = parse_packet(packet_path)
    lane = desk.lane(packet.lane)
    log_path = desk.bells_logs / f"{bell.stem}.log"
    # Resolved here, before the lock and before the worktree is prepared: a refusal --
    # a schedule window naming a lane-model file that is not there, a sandbox profile
    # that is gone, a provider nobody registered -- must leave the bell in `pending/`
    # with nothing half-done, so the watchdog can say why and try again once it is fixed.
    #
    # The clock is this minute's, not the packet's `created:` stamp. A bell can sit in
    # `pending/` for hours; the first version of this compared the dispatch time, and a
    # packet written at 09:00 and answered at 15:00 was started against the 09:00 window.
    resolved = providers_mod.resolve(desk, lane, packet=packet)
    providers_mod.sandbox_prefix(desk, lane)  # for the refusal, not the argv: built again at launch
    for note in resolved.notes:
        _log(desk, f"{note} ({bell.name})")
    if resolved.overridden:
        _log(desk, resolved.override_line(bell.name))
    with worker_slot(desk), lane_lock(desk, packet.lane):
        with desk_lock(desk):
            # A competing watchdog may have completed this bell since our first read.
            if not bell.exists():
                raise BellhopError(f"bell already claimed or completed: {bell.name}")
            if sha256_file(packet_path) != data.get("sha256"):
                raise BellhopError(f"packet changed before launch: {packet_path}")
            started_marker = desk.path("bells", ".locks", "started", bell.name + ".json")
            if started_marker.exists():
                raise BellhopError(f"unfinished launch recorded for {bell.name}; inspect the worker and receipt before recovery")
            worktree = prepare_worktree(desk, lane, packet.base)
            head_before = git(worktree, "rev-parse", "HEAD")
            from . import state as state_mod
            state_mod.backup(desk, desk.state / "in-flight.md")
            append(desk.state / "in-flight.md", f"- [worker] {packet.stem} on {packet.lane} since {ts()}\n")
            write_json(started_marker, {"bell": bell.name, "lane": packet.lane, "started_at": ts()})
        prompt = build_worker_prompt(desk, packet, lane, worktree)
        footer = launch_worker(desk, lane, packet, worktree, prompt, log_path, resolved=resolved)
        head_after = git(worktree, "rev-parse", "HEAD")
        receipt = desk.receipts / f"{packet.stem}.md"
        wrote_receipt = receipt.exists()
        if not wrote_receipt:
            write(receipt, f"# DEAD TASK {packet.stem}\n\nThe worker exited with code {footer['exit_code']} and wrote no receipt. See bells/logs/{log_path.name}.\n")
        # The machine header is computed here, still inside the lane lock: it runs the
        # lane's check command in the worktree, and a second task starting on the lane
        # would race it. It also counts what the worker left uncommitted, and that count
        # is the one this function judges on: `left_in_tree` is measured once, in
        # headers.py, and read from there. A worker that leaves uncommitted changes has
        # not finished — the work is unreachable from the lane branch, nothing can be
        # merged or accepted, and the next task cannot start because `prepare_worktree`
        # refuses a dirty worktree.
        try:
            values = headers_mod.compute_receipt_values(desk, receipt, packet.lane, packet=packet, result=footer)
            left = values.get("left_in_tree")
            header_note = "ok"
        except BellhopError as exc:
            values, left, header_note = None, None, f"failed: {exc}"
        # Three readings, not two: clean, dirty, and could-not-look. An unmeasurable tree
        # is treated as dead, because the next task cannot start in it either.
        dirty = None if left is None else int(left) > 0
        dead = footer["exit_code"] != 0 or footer.get("is_error") or not wrote_receipt or dirty is not False
        footer.update({"bell": bell.name, "id": packet.id, "lane": packet.lane, "base": packet.base,
                       "head_before": head_before, "head_after": head_after, "dead": bool(dead),
                       "dirty_worktree": dirty, "packet_sha256": data.get("sha256"),
                       "finished_at": ts(), "header": header_note})
        # The JSON footer stays as it was; every key in it is also a key in the header,
        # and the header adds the numbers a checker needs.
        append(receipt, "\n---\nbellhop: " + json.dumps({k: footer[k] for k in
               ("provider", "model", "turns", "duration_s", "cost_usd", "exit_code", "dead", "dirty_worktree", "head_after")}) + "\n")
        if values is not None:
            values["dead"] = bool(dead)  # judged after the tree was measured, so patched in here
            footer["check_exit"] = values.get("check_exit")
            headers_mod.write_header(receipt, "receipt", values)
        with desk_lock(desk):
            closetask_mod.settle_bell(desk, packet.id, title=packet.title, strict=False, refresh_card=False)
            meta = desk.receipts / f"{packet.id}.meta.json"
            write_json(meta, footer)
            _remove_inflight(desk, packet.stem)
            rung = desk.bells_rung / bell.name
            bell.rename(rung)
            started_marker.unlink()
            if archive:
                archive_mod.archive(
                    desk,
                    [receipt, meta, rung, bell, desk.state / "in-flight.md", desk.state / "closings.md", log_path, log_path.with_suffix(".prompt.md")],
                    f"receipt {packet.stem}" + (" (dead)" if dead else ""),
                )
    return footer


@serialized
def _remove_inflight(desk: Desk, stem: str) -> None:
    p = desk.state / "in-flight.md"
    if not p.exists():
        return
    from . import state as state_mod
    state_mod.backup(desk, p)
    lines = [l for l in read(p).splitlines() if f"[worker] {stem} " not in l]
    body = "\n".join(lines).rstrip("\n") + "\n"
    if not any(l.startswith("- ") for l in lines):
        body = body.rstrip("\n") + "\n- none\n"
    write(p, body)


def pending_bells(desk: Desk) -> list:
    return sorted(desk.bells_pending.glob("*.bell"))


def watch_once(desk: Desk, archive: bool = True) -> Optional[dict]:
    for bell in pending_bells(desk):
        data = json.loads(read(bell))
        if lane_is_busy(desk, data.get("lane", "")):
            continue
        try:
            result = process_bell(desk, bell, archive=archive)
        except Exception as exc:
            # The count is kept here too, so a bell answered by hand one attempt at a
            # time reaches the same limit as one the pool is retrying.
            count, parked = record_bell_failure(desk, bell, exc)
            if parked:
                raise BellhopError(f"{exc}\nbell {bell.name} failed {count} time(s) and is parked in "
                                   f"bells/dead/; it will not be answered again")
            raise
        clear_bell_failures(desk, bell)
        return result
    return None


class WatchPool:
    """Bounded lane concurrency; tick never waits for a model session.

    The slot locks also bound cooperating watch processes on the same desk. Lane
    claims last through settlement and archival. A crash leaves the lane claim for
    an operator to inspect, because an orphaned worker may still be running.
    """
    def __init__(self, desk: Desk, archive: bool = True):
        self.desk, self.archive = desk, archive
        self.limit = worker_limit(desk)
        self.executor = ThreadPoolExecutor(max_workers=self.limit, thread_name_prefix="bellhop-worker")
        self.active = {}  # Future -> (bell, lane)

    def tick(self, dispatch: bool = True):
        for future, (bell, lane) in list(self.active.items()):
            if not future.done():
                continue
            del self.active[future]
            try:
                result = future.result()
                clear_bell_failures(self.desk, bell)
                _log(self.desk, f"{'DEAD' if result['dead'] else 'OK'}\t{result['id']}\t"
                     f"{lane}\t{result['duration_s']}s\tcost={result.get('cost_usd')}")
            except Exception as exc:
                count, parked = record_bell_failure(self.desk, bell, exc)
                _log(self.desk, f"RED\t{bell.name}: {exc} (attempt {count}"
                                f"/{bell_failure_limit(self.desk)})")
                if parked:
                    _log(self.desk, f"RED\t{bell.name}: not answered again; see bells/dead/")
        if not dispatch:
            return
        lanes = {lane for _, lane in self.active.values()}
        for bell in pending_bells(self.desk):
            if len(self.active) >= self.limit:
                break
            try:
                data = json.loads(read(bell))
                lane = data["lane"]
                if lane in lanes or lane_is_busy(self.desk, lane):
                    continue
                future = self.executor.submit(process_bell, self.desk, bell, self.archive)
                self.active[future] = (bell, lane)
                lanes.add(lane)
            except (OSError, ValueError, KeyError) as exc:
                _log(self.desk, f"RED\t{bell.name}: {exc}")

    def close(self):
        # Graceful shutdown drains started workers; it never abandons their receipts.
        self.executor.shutdown(wait=True)
        self.tick(dispatch=False)


def watch_loop(desk: Desk, interval: int = 5, archive: bool = True) -> None:
    global _stop
    _stop = False
    signal.signal(signal.SIGTERM, _handle_term)
    signal.signal(signal.SIGINT, _handle_term)
    from . import daemon as daemon_mod
    pool = WatchPool(desk, archive)
    try:
        while not _stop or pool.active:
            daemon_mod.touch_heartbeat(desk, "resident")
            pool.tick(dispatch=not _stop)
            if not _stop or pool.active:
                time.sleep(max(0.05, interval))
    finally:
        pool.close()
