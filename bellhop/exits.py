"""One table of exit codes, for every command on the desk.

A coordinator branches on `$?`. That only works if the same number means the same thing
whichever verb produced it. On one production desk it did not: `bellhop state inflight`
with a bad argument printed usage and exited **128**, while other verbs used 2, 3 and 78
for different things, so three failures recorded as hard errors turned out to be three
typos. Guessing which kind of failure a number meant cost more than the failures did.

Six numbers, and nothing else may be returned by a command:

* **0 ok** - it did what it was asked.
* **1 red** - it ran to the end and the answer is no. A red sentinel, a receipt that
  fails its checks, dispatch stopped, no fresh heartbeat. Nothing went wrong with the
  command; the finding is the point.
* **2 usage** - the command line is wrong: an unknown verb or flag, a missing argument,
  a value the parser or the module refuses. Every argparse failure lands here, which is
  what `normalise_usage_exit` is for.
* **3 not found** - the command line was fine, but what it names does not exist or the
  question does not apply: a task id nobody dispatched, an id that is not sampled, a
  stop order to lift when none stands, a verdict that has not been written yet.
* **4 gate** - a gate refused: a stop-dispatch order, a lane that is not clean, a
  precondition on a state file, the hand-edit window. Re-running changes nothing; the
  gate has to be answered.
* **5 external** - something outside failed: a worker, a verifier, a release step, a
  service manager, the alert command. Re-running may well produce a different answer.
* **78 config** - the desk's own configuration is wrong or missing: no `bellhop.json`,
  a lane naming a file that is not there, a release with no steps. 78 is BSD's
  `EX_CONFIG`, kept because it is what init systems and service wrappers already read.

The split between 3, 4 and 5 is not about who is to blame. It is about what a caller
should do next: fix the name, answer the gate, or try again.

**The hook protocol is not this table.** `bellhop hook` and `bellhop provider-guard` are
called by Claude Code, which reads exit 2 as "block this tool call and hand the model
the reason". That 2 is a protocol, not a usage error, and those two commands keep it.
"""
from __future__ import annotations

OK = 0
RED = 1
USAGE = 2
NOT_FOUND = 3
GATE = 4
EXTERNAL = 5
CONFIG = 78

#: What Claude Code reads as "blocked, and here is why". Not from the table above.
HOOK_BLOCK = 2

TABLE = (
    (OK, "ok", "it did what it was asked"),
    (RED, "red", "it ran to the end and the answer is no; read the finding"),
    (USAGE, "usage", "the command line is wrong; fix the arguments"),
    (NOT_FOUND, "not found", "what it names does not exist, or the question does not apply"),
    (GATE, "gate", "a gate refused; answer the gate, do not retry"),
    (EXTERNAL, "external", "something outside failed; retrying may differ"),
    (CONFIG, "config", "the desk's configuration is wrong or missing (BSD EX_CONFIG)"),
)


def table_rows() -> str:
    """The table as Markdown. `docs/exit-codes.md` carries this, and a test pins it."""
    rows = ["| code | name | meaning |", "|---|---|---|"]
    rows += ["| %d | %s | %s |" % row for row in TABLE]
    return "\n".join(rows) + "\n"


def normalise_usage_exit(exc: SystemExit) -> int:
    """The exit code to use for a `SystemExit` raised while parsing arguments.

    argparse exits 2 on its own, but `--help` and `--version` exit 0 through the same
    exception, and a parser built with `exit_on_error=False`, a subparser wired by hand,
    or an interpreter that turns an uncaught exception into 128+n can all produce
    something else. One place decides, so "you typed it wrong" is always 2.
    """
    code = exc.code
    if code is None:
        return OK
    if isinstance(code, int):
        return OK if code == OK else USAGE
    return USAGE  # a message passed to sys.exit(): a usage error with words on it
