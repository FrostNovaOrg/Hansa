from __future__ import annotations

import json
import signal
import time
from pathlib import Path
from typing import Optional

from . import archive as archive_mod
from . import closetask as closetask_mod
from . import headers as headers_mod
from .config import Desk
from .lanes import lane_is_busy, lane_lock, prepare_worktree
from .packet import Packet, parse_packet
from .util import BellhopError, append, git, now, read, run, scrubbed_env, sha256_file, ts, write, write_json

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
    max_turns = packet.max_turns or lane["max_turns"]
    cmd = [
        desk.cfg["claude_bin"],
        "-p",
        prompt,
        "--model",
        lane["model"],
        "--permission-mode",
        lane["permission_mode"],
        "--add-dir",
        str(desk.root),
        "--output-format",
        "json",
        "--max-turns",
        str(max_turns),
    ]
    if lane.get("allowed_tools"):
        cmd += ["--allowedTools", ",".join(lane["allowed_tools"])]
    return cmd


def _parse_result(stdout: str) -> Optional[dict]:
    """Find the JSON result object in the worker's stdout (last JSON line wins)."""
    result = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            result = obj
    if result is None:
        text = stdout.strip()
        if text.startswith("{"):
            try:
                result = json.loads(text)
            except json.JSONDecodeError:
                result = None
    return result


def launch_worker(desk: Desk, lane: dict, packet: Packet, worktree: Path, prompt: str, log_path: Path) -> dict:
    env = scrubbed_env(lane.get("env_allow", []))
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
        }
    )
    if lane.get("worker_cmd"):
        cmd = ["/bin/sh", "-c", lane["worker_cmd"]]
        provider = lane.get("provider") or "custom"
    else:
        cmd = _claude_cmd(desk, lane, packet, prompt)
        provider = "claude"
    started = now()
    timeout = max(60, packet.budget_minutes * 60 * 2)
    try:
        proc = run(cmd, cwd=worktree, env=env, timeout=timeout)
        stdout, stderr, code = proc.stdout or "", proc.stderr or "", proc.returncode
    except Exception as exc:  # timeout or launch failure
        stdout, stderr, code = "", f"worker launch failed: {exc}", 124
    duration = (now() - started).total_seconds()
    write(log_path, f"# bellhop worker log {packet.stem}\n\n## stdout\n{stdout}\n\n## stderr\n{stderr}\n")
    result = _parse_result(stdout) if code == 0 or stdout else None
    return {
        "provider": provider,
        "model": (result or {}).get("model") or lane.get("model"),
        "turns": (result or {}).get("num_turns"),
        "duration_s": round(duration, 1),
        "cost_usd": (result or {}).get("total_cost_usd"),
        "session_id": (result or {}).get("session_id"),
        "exit_code": code,
        "started_at": ts(started),
        "is_error": bool((result or {}).get("is_error")) if result else code != 0,
        "usage": (result or {}).get("usage"),
    }


def process_bell(desk: Desk, bell: Path, archive: bool = True) -> dict:
    data = json.loads(read(bell))
    packet_path = desk.root / data["packet"]
    if not packet_path.exists():
        raise BellhopError(f"bell {bell.name}: packet is gone: {packet_path}")
    if sha256_file(packet_path) != data.get("sha256"):
        raise BellhopError(f"bell {bell.name}: packet changed after the bell was rung; ring it again")
    packet = parse_packet(packet_path)
    lane = desk.lane(packet.lane)
    log_path = desk.bells_logs / f"{bell.stem}.log"
    with lane_lock(desk, packet.lane):
        worktree = prepare_worktree(desk, lane, packet.base)
        head_before = git(worktree, "rev-parse", "HEAD")
        append(desk.state / "in-flight.md", f"- [worker] {packet.stem} on {packet.lane} since {ts()}\n")
        prompt = build_worker_prompt(desk, packet, lane, worktree)
        footer = launch_worker(desk, lane, packet, worktree, prompt, log_path)
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
        closetask_mod.settle_bell(desk, packet.id, title=packet.title, strict=False, refresh_card=False)
    meta = desk.receipts / f"{packet.id}.meta.json"
    write_json(meta, footer)
    _remove_inflight(desk, packet.stem)
    rung = desk.bells_rung / bell.name
    bell.rename(rung)
    if archive:
        archive_mod.archive(
            desk,
            [receipt, meta, rung, bell, desk.state / "in-flight.md", desk.state / "closings.md", log_path, log_path.with_suffix(".prompt.md")],
            f"receipt {packet.stem}" + (" (dead)" if dead else ""),
        )
    return footer


def _remove_inflight(desk: Desk, stem: str) -> None:
    p = desk.state / "in-flight.md"
    if not p.exists():
        return
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
        return process_bell(desk, bell, archive=archive)
    return None


def watch_loop(desk: Desk, interval: int = 5, archive: bool = True) -> None:
    signal.signal(signal.SIGTERM, _handle_term)
    signal.signal(signal.SIGINT, _handle_term)
    from . import daemon as daemon_mod
    while not _stop:
        try:
            daemon_mod.touch_heartbeat(desk, 'resident')
            result = watch_once(desk, archive=archive)
        except BellhopError as exc:
            append(desk.bells_logs / "watchdog.log", f"{ts()}\tRED\t{exc}\n")
            result = None
        if result:
            append(desk.bells_logs / "watchdog.log",
                   f"{ts()}\t{'DEAD' if result['dead'] else 'OK'}\t{result['id']}\t{result['lane']}\t{result['duration_s']}s\tcost={result.get('cost_usd')}\n")
        else:
            time.sleep(interval)
