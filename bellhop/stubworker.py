"""Dry-run worker used by tests and the demo.

It behaves like a well-mannered worker: reads the prompt, makes one small commit in the
worktree, writes a receipt that answers every acceptance item, and prints a JSON result
line shaped like the Claude Code CLI's `--output-format json` result.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    worktree = Path(os.environ["BELLHOP_WORKTREE"])
    receipt = Path(os.environ["BELLHOP_RECEIPT"])
    packet = Path(os.environ["BELLHOP_PACKET"])
    task_id = os.environ["BELLHOP_ID"]
    mode = os.environ.get("BELLHOP_STUB_MODE", "ok")
    if mode == "crash":
        print("stub worker: simulated crash", file=sys.stderr)
        return 1
    if mode == "quota":
        # What a provider that will not answer at all looks like from here: a result
        # object carrying the status, the vendor's word for it, and how long it wants
        # to be left alone. `BELLHOP_STUB_RETRY_AFTER` is seconds, or an HTTP date.
        print(json.dumps({
            "type": "result", "subtype": "error_during_execution", "is_error": True,
            "error": {"type": "rate_limit_error", "status": 429,
                      "message": "429 Too Many Requests: usage limit reached"},
            "headers": {"retry-after": os.environ.get("BELLHOP_STUB_RETRY_AFTER", "120")},
            "num_turns": 0, "duration_ms": 30, "total_cost_usd": 0.0, "model": "stub"}))
        return 1
    note = worktree / f"bellhop-stub-{task_id}.txt"
    note.write_text(f"work for {task_id}\n", encoding="utf-8")
    if mode != "nocommit":  # "nocommit" leaves the change uncommitted, like a worker denied `git commit`
        subprocess.run(["git", "add", note.name], cwd=worktree, check=True)
        subprocess.run(["git", "-c", "user.name=stub", "-c", "user.email=stub@example.invalid", "commit", "-q", "-m", f"stub: {task_id}"], cwd=worktree, check=True)
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=worktree, capture_output=True, text=True).stdout.strip()
    acceptance = []
    for line in packet.read_text(encoding="utf-8").splitlines():
        if line.startswith("  - ") or line.startswith("- "):
            acceptance.append(line.strip()[2:])
    body = [f"# Receipt {task_id}", "", f"- base: {os.environ.get('BELLHOP_BASE', 'see packet')}", f"- head: {head}", f"- changed: {note.name}", "", "## Acceptance", ""]
    for a in acceptance:
        body.append(f"- {a} — checked: `echo ok` → `ok`")
    body += ["", "## Doubts", "", "- none", "", "## Not done", "", "- nothing left out"]
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text("\n".join(body) + "\n", encoding="utf-8")
    result = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 3,
              "duration_ms": 1200, "total_cost_usd": 0.0, "session_id": "stub", "model": "stub"}
    if mode == "thinking":
        # The shape that started this: thinking blocks with nothing readable in them, and
        # tokens billed against them anyway.
        result["content"] = [{"type": "thinking", "thinking": "", "signature": "encrypted"}]
        result["usage"] = {"output_tokens": 10, "thinking_tokens": 4321}
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
