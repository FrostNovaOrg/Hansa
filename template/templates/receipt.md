# Receipt {{id}}

Write this body in plain words. Do not write the block at the top of the file: the
watchdog runs `bellhop receipt-header` when you exit, and it computes base, head,
commit count, diff stat, files left in the tree, and the lane check's exit code by
running those commands itself. Its spec is in `bellhop/headers.py`; no other file
restates it. Numbers you type here are read by people, not by the checker.

## Acceptance

- <item> — checked: `<command>` → `<observed output>`

## Doubts

Start each one at the beginning of a line with `DOUBT:` — that prefix is what the
header counts, so a doubt written any other way is invisible to the machine.

DOUBT: <anything the packet did not settle, or that looked wrong>

## Extra

Anything you did beyond the packet, one per line, prefixed the same way:

EXTRA: <what you did that the packet did not ask for, and why>

## Not done

- <what was left out and why; "nothing left out" if none>
