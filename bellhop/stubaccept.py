"""Dry-run acceptance session used by tests and the demo."""
from __future__ import annotations

import os
import sys


def main() -> int:
    prompt = open(os.environ["BELLHOP_PROMPT_FILE"], encoding="utf-8").read()
    items = [l[2:] for l in prompt.splitlines() if l.startswith("- ") and "—" not in l][:10]
    print("## Verdict\n")
    for it in items:
        print(f"- {it}: PASS (stub)")
    print("\nOverall: PASS (stub acceptance)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
