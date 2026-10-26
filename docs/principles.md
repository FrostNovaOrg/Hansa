# How a desk is run

In a desk such as `shop/desk`, the principal is the person in charge, the
coordinator plans the work, and workers change files. These rules explain the
division. The coordinator's card is available through `hansa help card`; this
page through `hansa help principles`.

## 1. The principal holds money, keys and releases

The principal sets provider and model definitions, keys, token prices and which
release targets may go out without asking, on the page. Agents do not change
those desk-wide choices. The coordinator can choose configured providers for a
lane and override a subscription provider's model for a task. This guards
against agents choosing new spending arrangements or permission to ship.

Authority: hansa restricts access; the card reserves these decisions for the principal.

## 2. The coordinator plans and judges, and leaves code to workers

Code changes go through workers, giving each change a task branch, the lane's
configured check and a merge. The coordinator reviews the result. A commit made
directly in a lane's repository is listed with the release that sends it. This
guards against edits bypassing the work's review and disappearing into a release.

Authority: the card requires this; hansa supplies edit denials and records direct commits.

## 3. A worker does one task, in a sandbox

Each task gets its own branch in its lane's worktree; the lane reuses that
worktree, one task at a time. Built-in workers' commands cannot commit or push,
read the desk's keys or state, or reach hosts outside the lane's network list.
Writes stay within the worktree, the run's temporary folder and the lane's write
list. Separately allowed web tools and optional adapters have
[limits](../README.md#limits), explained in [the sandbox guide](sandbox.md).
This contains a worker's mistake or hostile input it reads.

Authority: hansa enforces the sandbox and git restrictions for built-in workers.

## 4. hansa checks the result itself

After a successful worker run, hansa commits the changes and runs the lane's
check on that branch, if a check is configured and there is a change. The
recorded check result supplies evidence independent of the worker's report.
This guards against accepting a worker's claim of success without running
the check.

Authority: hansa enforces this when the lane has a check.

## 5. Someone else verifies code, preferably another model

A separate worker runs the change and tries rare cases. When tests changed, its
instructions require undoing the code to see which tests fail, then restoring
it and giving a verdict. Another lane can provide another provider or model;
that difference is chosen, not guaranteed. hansa blocks a merge after "does not
hold" unless explicitly overridden. This guards against the same blind spot
writing and checking the change.

Authority: the card requires verification for code; hansa enforces the negative-verdict gate.

## 6. One task has one goal, sized for one run

A task says what done means and asks only for what that goal needs. Work too
large for one worker run is split. A task that verification will judge gives
its cases as a table, saying whether each rare case is refused or accepted as
it is. This guards against vague work that sprawls and cannot be judged fairly.

Authority: the card asks the coordinator to write tasks this way.

## 7. Stop a rework loop

When verification rejects a task a second time for the same kind of reason,
the coordinator rethinks it: make it smaller, ask for less, or start again from
the base. Stop that loop instead of sending it back for another round. This
guards against rework costing more than the change is worth.

Authority: the card asks for this; hansa prompts after repeated failed verdicts.

## 8. Sort what verification finds

An ordinary-use defect is fixed now; a task that asked for the wrong thing
needs rewriting. Small gaps in rare combinations of states can receive
"holds, with notes" and stay on record in `hansa gaps`, marked later or won't
fix with a reason. Losing work, exposing keys, merging into the wrong branch
or falsely reporting a passed check always calls for rejection. This guards
against endless polishing and findings getting lost.

Authority: the card directs review; hansa gives verifiers these criteria and keeps notes and marks.

## 9. Nothing ships without the principal

Merges stop at the lane's base branch. A release needs the principal's yes on
the page, their alert's one-time code, or a target they ticked to go out without
asking. Approval applies only while its recorded inputs still match. A match
under the principal's forbidden-word rules refuses the release. This guards
against an agent publishing without the principal's authority.

Authority: hansa enforces release approval and the word scan; the card forbids publishing directly.

## 10. Work stays on record; the principal need not read code

Tasks, reports, verdicts, merges, releases and principal to-dos are kept. Token
usage is valued at the principal's prices, with costs by seat, lane, task and
model. Subscription values are not bills, and missing coordinator usage is
identified. The page shows what waits for the principal, what changed and
what it cost; the coordinator operates hansa for them. This guards against
losing track of decisions, changes and spending.

Authority: hansa keeps and displays the records; the card assigns operation to the coordinator.
