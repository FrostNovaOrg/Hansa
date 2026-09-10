from __future__ import annotations

import json
from pathlib import Path

from . import headers as headers_mod
from .config import FRAMEWORK_ROOT, Desk
from .packet import parse_packet
from .util import BellhopError, git, now, read, run, scrubbed_env, ts, write

READ_ONLY_TOOLS = "Read,Grep,Glob,Bash(git diff:*),Bash(git log:*),Bash(git show:*)"


def terms_path(desk: Desk) -> Path:
    """The verifier's terms: the desk's copy if it has one, else the framework's."""
    p = desk.path("rules", "acceptance-terms.md")
    return p if p.exists() else FRAMEWORK_ROOT / "template" / "rules" / "acceptance-terms.md"


def check_path(desk: Desk, task_id: str) -> Path:
    """Where the checker's reading is recorded, next to the verdict it read."""
    return desk.receipts / f"{task_id}.acceptance.check.txt"


def find_packet(desk: Desk, task_id: str) -> Path:
    hits = [p for p in desk.inbox.rglob(f"{task_id}-*.md")]
    if not hits:
        raise BellhopError(f"no packet with id {task_id} under {desk.inbox}")
    return hits[0]


def accept(desk: Desk, task_id: str) -> Path:
    packet = parse_packet(find_packet(desk, task_id))
    lane = desk.lane(packet.lane)
    receipt = desk.receipts / f"{packet.stem}.md"
    if not receipt.exists():
        raise BellhopError(f"no receipt yet for {packet.stem}")
    wt = Path(lane["worktree"])
    diff = git(wt, "diff", f"{packet.base}..HEAD", "--stat") + "\n\n" + git(wt, "diff", f"{packet.base}..HEAD")
    if len(diff) > 200_000:
        diff = diff[:200_000] + "\n[diff truncated]\n"
    tpl = desk.template("acceptance-prompt.md")
    prompt = (
        tpl.replace("{{packet}}", read(packet.path))
        .replace("{{receipt}}", read(receipt))
        .replace("{{diff}}", diff)
        .replace("{{acceptance}}", "\n".join(f"- {a}" for a in packet.acceptance))
        .replace("{{id}}", packet.id)
    )
    out = desk.receipts / f"{packet.id}.acceptance.md"
    prompt = prompt.replace("{{terms}}", read(terms_path(desk))).replace("{{out}}", str(out))
    env = scrubbed_env(lane.get("env_allow", []))
    env.update({"BELLHOP_DESK": str(desk.root), "BELLHOP_ID": packet.id, "BELLHOP_ACCEPTANCE_OUT": str(out)})
    prompt_file = desk.bells_logs / f"accept-{packet.id}.prompt.md"
    write(prompt_file, prompt)
    env["BELLHOP_PROMPT_FILE"] = str(prompt_file)
    started = now()
    if lane.get("accept_cmd"):
        proc = run(["/bin/sh", "-c", lane["accept_cmd"]], cwd=wt, env=env)
        verdict = proc.stdout or ""
    else:
        cmd = [desk.cfg["claude_bin"], "-p", prompt, "--model", lane["model"], "--allowedTools", READ_ONLY_TOOLS,
               "--output-format", "json", "--max-turns", "30"]
        proc = run(cmd, cwd=wt, env=env, timeout=1800)
        verdict = ""
        for line in (proc.stdout or "").splitlines():
            if line.strip().startswith("{"):
                try:
                    verdict = json.loads(line).get("result", "") or verdict
                except json.JSONDecodeError:
                    pass
    if proc.returncode != 0:
        raise BellhopError(f"acceptance session failed ({proc.returncode}): {(proc.stderr or '')[:400]}")
    header = f"# Acceptance {packet.stem}\n\n- time: {ts(started)}\n- duration_s: {(now() - started).total_seconds():.0f}\n- base: {packet.base}\n- head: {git(wt, 'rev-parse', '--short', 'HEAD')}\n\n"
    write(out, header + verdict.strip() + "\n")
    # The verdict is now a file. Give it its machine header, then read only that header
    # back and record what the checker found beside the verdict. A red here is a finding
    # about the verdict, not a failure of this command: the coordinator still decides.
    try:
        headers_mod.write_acceptance_header(out, desk=desk, lane=packet.lane, task=packet.id)
        report = headers_mod.check_acceptance(out, max_bytes=int(desk.cfg.get("acceptance_bytes", headers_mod.DEFAULT_ACCEPTANCE_BYTES)))
        write(check_path(desk, packet.id), report.text())
    except BellhopError as exc:
        write(check_path(desk, packet.id), f"red: header could not be written: {exc}\nred 1 warn 0\n")
    return out
