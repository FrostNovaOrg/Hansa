# The card, in flight, and debts: the rules for the lines

This file is the source of truth for the *format* of `state/next.md`,
`state/in-flight.md` and `state/debts.md`. The data files hold data only.

The reason is an incident, not tidiness: on a production desk the card's rules
lived below a divider in the card itself, a card-refresh regex matched its own
rule text, and the rules were silently rewritten. A
rule that lives in the file its writer edits is a rule the writer can eat.
Changing *this* file changes the rules; changing the data files changes the data.

## The card — `state/next.md`

Seven keys, one per line, `key: value`, read by `^key: ` and by nothing else.
`bellhop state card set` is the only writer; it changes only the keys it is
given, one anchored replacement each, and refuses if a key line does not match
exactly once.

| key | flag | value | who reads it |
|---|---|---|---|
| `ready` | `--dispatchable yes\|no` | `yes` when a packet is written and can be rung now | the sentinel: `ready: yes` with an idle lane is what wakes the coordinator |
| `next` | `--next` | one line: the name of the thing that would be dispatched next | the coordinator and the sentinel; required when `ready: yes` |
| `lane` | `--lane` | the lane name it would go to | dispatch |
| `reason` | `--reason` | what we are waiting on and who wakes us | required when `ready: no`; an empty card and a card nobody ever wrote look the same to a reader |
| `queue` | `--pool` | **starts with a digit**, e.g. `3 (2 product + 1 tooling)`; an empty pool is `0 (none)` | readers take the leading number, so a value that does not start with a digit reads as "no value" |
| `refreshed` | stamped | the time of the last write | the sentinel goes red when the card is older than the last receipt |
| `by` | stamped | who wrote it (`bellhop state`) | audit |

`refreshed` and `by` are stamped by the script on every write and are never
passed in. `--dispatchable yes` with no `next`, and `--dispatchable no` with no
`reason`, are both refused.

## In flight — `state/in-flight.md`

- One line per running item, `- <what> <task id>`; the line goes in at take-off
  and comes out at close-out. `bellhop state inflight` is the only writer.
- Put the task id in the line. `inflight finish --task <id>` matches the id as a
  whole token (`017` does not match `2017`), and it must match **exactly one**
  line: zero or several is refused and the candidates are printed. An id that
  appears in someone else's prose is how the wrong line gets deleted.
- Lines are capped (`inflight_line_chars`, default 160) and **truncated, not
  refused** — the row is a pointer to the work, not the record of it.
- No rows means the file says `- none` on its own line. An empty file and a
  missing file must not look alike.

## Debts — `state/debts.md`

- One line per debt, `- <who is owed> <what> <by when>`.
- Settle with `bellhop state debts settle --match <substr>`; the line moves to
  `state/debts-settled.md`. With `--keep` a struck copy stays behind. Never
  delete a debt line by hand.
- A baseline-style line that must be *updated* rather than re-added is changed
  with `bellhop state debts set --match <substr> --text <new line>`: it replaces
  in place and keeps its position in the file. Settling and re-adding sends the
  line to the bottom, and a line that moved and a line that was updated look the
  same in `grep`.
