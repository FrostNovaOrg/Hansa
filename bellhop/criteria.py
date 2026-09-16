"""Criteria that can go red: every acceptance item names the way it breaks.

A criterion that cannot fail has passed before the worker starts. It reads like a
check -- "the endpoint returns the right shape", "the module is well structured" --
and it is a sentence about intent with no red state behind it. On one production
desk, **most of the self-caught mistakes and several returned tasks** were criteria
written that way: born green. The worker did something reasonable, the receipt said
the criterion was met, and nobody could point at the reading that would have said
otherwise, because there was none. A criterion that was never checkable and one that
passed look exactly the same in a receipt.

The convention is one clause, on the criterion's own line:

    - <criterion> — breaks when: <what makes it red>

The clause is not decoration. Writing it forces the author to name a state of the
world in which the criterion fails, and an author who cannot name one has found the
problem before dispatch rather than at acceptance. It is also what the verifier's
mutation step needs: break what the criterion claims, watch the check go red, put it
back. A criterion with no stated way to break leaves the verifier inventing one.

`bellhop dispatch` refuses a packet whose acceptance section has a line without the
clause, names the first one, and does not write anything. `--allow-unbreakable`
dispatches anyway and writes a ledger entry saying so, because an exception that is
not recorded is indistinguishable from a rule that was never there.

The marker is the words `breaks when:`; the em dash is the convention and not the
gate, so a coordinator who typed a hyphen is not refused over punctuation.
"""
from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

MARKER = "breaks when:"
SHAPE = "- <criterion> — breaks when: <what makes it red>"

# An acceptance item arrives here without its `- `: `bellhop dispatch` has already
# lifted the bullets out of the body's Acceptance section.
NO_CLAUSE = "no `breaks when:` clause"
EMPTY_CLAUSE = "`breaks when:` with nothing after it"
EMPTY_CRITERION = "`breaks when:` with no criterion in front of it"


def split(item: str) -> Tuple[str, Optional[str]]:
    """(criterion, the breaks-when clause or None). Case-insensitive on the marker."""
    text = " ".join(str(item or "").split())
    lowered = text.lower()
    at = lowered.find(MARKER)
    if at < 0:
        return text, None
    head = text[:at].rstrip(" \t-—–:")
    return head, text[at + len(MARKER):].strip()


def problem(item: str) -> Optional[str]:
    """Why this item cannot go red, or None when it names its own failure."""
    criterion, clause = split(item)
    if clause is None:
        return NO_CLAUSE
    if not clause:
        return EMPTY_CLAUSE
    if not criterion:
        return EMPTY_CRITERION
    return None


def first_problem(items: Sequence[str]) -> Optional[Tuple[int, str, str]]:
    """(1-based position, the item, the reason) for the first item that cannot go red."""
    for n, item in enumerate(items or (), 1):
        why = problem(item)
        if why:
            return n, str(item), why
    return None


def refusal(position: int, item: str, why: str, source: str = "") -> str:
    """The message `bellhop dispatch` prints. It names the line, not the count."""
    lines = [
        "acceptance item %d has %s: %r" % (position, why, item),
        "Every criterion says how it goes red, on its own line:",
        "    " + SHAPE,
        "A criterion with no way to break has passed before the worker starts; on one "
        "desk that was most of the self-caught mistakes and several returned tasks.",
    ]
    if source:
        lines.append(
            "These items came from packet %s and a return must keep them, so this one cannot be "
            "reworded here: scope a new task with criteria that can go red, or pass "
            "--allow-unbreakable and the exception is written to the ledger." % source
        )
    else:
        lines.append("Rewrite it, or pass --allow-unbreakable to dispatch anyway and record why.")
    return "\n".join(lines)


def exception_body(task_id: str, lane: str, title: str, items: Sequence[str]) -> str:
    found = [(n, item, why) for n, item, why in
             ((n, str(i), problem(str(i))) for n, i in enumerate(items or (), 1)) if why]
    lines = ["Dispatched %s on lane %s (%s) with --allow-unbreakable." % (task_id, lane, title),
             "",
             "These acceptance items do not name a way to go red, so acceptance cannot",
             "distinguish them from work that was never done:",
             ""]
    lines += ["- item %d (%s): %s" % (n, why, item) for n, item, why in found]
    lines += ["",
              "The convention waived here is `%s`." % SHAPE,
              "This entry is the record that the waiver happened; the packet itself does not carry it."]
    return "\n".join(lines)


def check(desk, items: Sequence[str], *, allow: bool = False, record: bool = True,
          task_id: str = "", lane: str = "", title: str = "", source: str = "") -> Optional[str]:
    """The gate `bellhop dispatch` calls before it writes the packet.

    Returns None when every item names its failure, the refusal text when one does
    not, and None-with-a-ledger-entry when `--allow-unbreakable` waived it.
    """
    found = first_problem(items)
    if found is None:
        return None
    position, item, why = found
    if not allow:
        return refusal(position, item, why, source)
    if record:
        from . import ledger as ledger_mod  # imported here: the refusal path writes nothing

        ledger_mod.add(desk, "unbreakable acceptance criteria allowed for %s" % (task_id or "a packet"),
                       exception_body(task_id, lane, title, items),
                       refs=[task_id] if task_id else [])
    return None


def unbreakable(items: Sequence[str]) -> List[str]:
    """Every item that cannot go red. For readers other than dispatch."""
    return [str(i) for i in (items or ()) if problem(str(i))]
