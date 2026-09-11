"""Dry-run acceptance session used by tests and the demo.

It stands in for the verifier: it reads the case pack the script assembled, prints a
verdict on stdout, and exits. Every switch below exists because a gate in
`bellhop/accept.py` needs a positive case and a negative control, and a stub that can
only behave well proves nothing about a pipeline built for the times it does not.

    --good              print a complete verdict: eight sections, one mutation per
                        criterion, a verdict line last
    --bad-format        print prose with no verdict line and no mutation (the default,
                        so an unswitched stub exercises the format gate)
    --retry-good        bad on the first run, good once the case pack carries the
                        reminder: this is the retry path
    --verdict V         pass | return | doubt for the good body
    --blocking          add a blocking doubt (a pass carrying one is a red)
    --touch-tree        write into the tree under test before answering
    --commit-tree       commit into the tree under test: HEAD's tree hash moves while
                        `git status --porcelain` stays empty
    --mutate            the honest mutation: break a tracked file in the copy, then
                        restore it with `git checkout --`, the way the terms ask
    --sleep S           sleep S seconds before answering
    --child-sleep S     leave a child sleeping S seconds in this process group
    --pid-file P        write the child's pid there, so a test can look for a survivor
    --json              also print a result object, the way `claude -p --output-format
                        json` does; without it the session's answer is bare stdout
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REMINDER_MARK = "Reminder:"


def read_pack() -> str:
    path = os.environ.get("BELLHOP_CASE_PACK") or os.environ.get("BELLHOP_PROMPT_FILE") or ""
    if not path or not Path(path).exists():
        return ""
    return Path(path).read_text(encoding="utf-8")


def criteria(pack: str) -> list:
    """The `- ` items under the case pack's Criteria heading, in order."""
    items, inside = [], False
    for line in pack.splitlines():
        if line.startswith("#"):
            inside = line.lstrip("# ").lower().startswith("criteria")
            continue
        if inside and line.strip().startswith("- "):
            items.append(line.strip()[2:].strip())
    return items or ["the packet's acceptance items (none found in the case pack)"]


def good_body(pack: str, verdict: str = "pass", blocking: bool = False) -> str:
    items = criteria(pack)
    mark = "x" if verdict == "pass" else " "
    lines = ["## Task", "", f"{os.environ.get('BELLHOP_ID', '?')} (stub acceptance)", "", "## Criteria", ""]
    lines += [f"- [{mark}] {it} — stub read the diff" for it in items]
    lines += ["", "## Mutation", ""]
    lines += [f"- broke: {it} / the check went red / restored" for it in items]
    lines += ["", "## Extra findings", "", "- none", "", "## Doubts", ""]
    lines += ["- [blocking] the stub was told to raise one" if blocking else "- [recorded] the stub is not a reader"]
    lines += ["", "## Hygiene", "", "- file set equal", "", "## Recommendation", "",
              "- merge" if verdict == "pass" else "- send it back", "", "## Verdict", ""]
    lines += [f"VERDICT: {verdict}" if verdict == "pass" else f"VERDICT: {verdict} — the stub was told to say so"]
    return "\n".join(lines) + "\n"


def bad_body() -> str:
    """Prose: no verdict line last, no mutation. What 35 of 71 stalls looked like."""
    return ("The change looks fine to me. I read the diff and the receipt and I have no\n"
            "objection: everything the packet asked for appears to be there.\n\n"
            "Overall: PASS (stub acceptance)\n")


def mutate() -> str:
    """Break a tracked file, then put it back. This is what the terms ask a verifier
    to do, and it must leave no trace: the recheck cannot tell an honest mutation from
    a careless edit except by whether the tree came back."""
    cwd = Path.cwd()
    tracked = subprocess.run(["git", "ls-files"], cwd=str(cwd), capture_output=True, text=True).stdout.split()
    if not tracked:
        return "no tracked file to break"
    victim = tracked[0]
    (cwd / victim).write_text("broken by the stub verifier\n", encoding="utf-8")
    subprocess.run(["git", "checkout", "--", victim], cwd=str(cwd), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return victim


def touch_tree(commit: bool) -> None:
    cwd = Path.cwd()
    (cwd / "verifier-scratch.txt").write_text("the verifier wrote this\n", encoding="utf-8")
    if not commit:
        return
    # The environment is scrubbed, so the identity comes on the command line.
    ident = ["-c", "user.name=stub", "-c", "user.email=stub@example.invalid"]
    quiet = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["git", "add", "verifier-scratch.txt"], cwd=str(cwd), check=True, **quiet)
    subprocess.run(["git", *ident, "commit", "-q", "-m", "stub verifier commit"], cwd=str(cwd), check=True, **quiet)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="stubaccept")
    ap.add_argument("--good", action="store_true")
    ap.add_argument("--bad-format", action="store_true")
    ap.add_argument("--retry-good", action="store_true")
    ap.add_argument("--verdict", default="pass", choices=["pass", "return", "doubt"])
    ap.add_argument("--blocking", action="store_true")
    ap.add_argument("--touch-tree", action="store_true")
    ap.add_argument("--commit-tree", action="store_true")
    ap.add_argument("--mutate", action="store_true")
    ap.add_argument("--sleep", type=float, default=0.0)
    ap.add_argument("--child-sleep", type=float, default=0.0)
    ap.add_argument("--pid-file")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    pack = read_pack()
    if args.child_sleep:
        child = subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({args.child_sleep})"])
        if args.pid_file:
            Path(args.pid_file).write_text(str(child.pid), encoding="utf-8")
    if args.mutate:
        mutate()
    if args.touch_tree or args.commit_tree:
        touch_tree(args.commit_tree)
    if args.sleep:
        time.sleep(args.sleep)

    if args.retry_good:
        body = good_body(pack, args.verdict, args.blocking) if REMINDER_MARK in pack else bad_body()
    elif args.bad_format:
        body = bad_body()
    elif args.good or args.blocking or args.verdict != "pass":
        body = good_body(pack, args.verdict, args.blocking)
    else:
        body = bad_body()

    if args.json:
        print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "num_turns": 4,
                          "duration_ms": 900, "total_cost_usd": 0.0, "session_id": "stub-accept",
                          "model": "stub", "result": body}))
    else:
        sys.stdout.write(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
