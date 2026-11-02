# How a desk is run

In a desk such as `shop/desk`, the principal is in charge, the coordinator plans
the work, and workers change files. `hansa help card` prints the coordinator's
card; `hansa help principles` prints this page.

## 1. The principal holds money, keys and releases

The principal sets provider and model definitions, keys, token prices and which
release targets may go out without asking, on the page. Agents do not change
those desk-wide choices. The coordinator can choose configured providers for a
lane and override a subscription provider's model for a task. This guards
against agents arranging new spending or gaining permission to ship.

Authority: hansa restricts access; the card reserves these decisions for the principal.

## 2. The coordinator plans and judges, and leaves code to workers

Workers change code on task branches. hansa runs the lane's configured check and
the coordinator reviews each change before merging. Direct commits in a lane's
repository are listed with the release that sends them. This guards against
unreviewed edits reaching a release unnoticed.

Authority: the card requires this; hansa supplies edit denials and records direct commits.

## 3. A worker does one task, in a sandbox

Each task gets its own branch in its lane's worktree; the lane reuses that
worktree, one task at a time. Built-in workers' commands cannot commit or push,
read the desk's keys or state, or reach hosts outside the lane's network list.
Writes stay within the worktree, the run's temporary folder and the lane's write
list. Separately allowed web tools and optional adapters have
[limits](concepts.md#limits), explained in [the sandbox guide](sandbox.md).
This limits the damage from a worker's mistake or hostile input it reads.

Authority: hansa enforces the sandbox and git restrictions for built-in workers.

## 4. hansa checks the result itself

After a successful worker run with changes, hansa commits them and runs the lane's
check on that branch, if one is configured. It records its own result, not the
worker's report, so a worker cannot pass the check just by claiming success.

Authority: hansa enforces this when the lane has a check.

## 5. Someone else verifies code, preferably another model

A separate worker runs the change and tries rare cases. When tests changed, its
instructions require undoing the code to see which tests fail, restoring it and
giving a verdict. Verification can use a different provider or model configured
on another lane; a different model is chosen, not guaranteed.
hansa blocks a merge after "does not hold" unless the coordinator explicitly
overrides it. This guards against shared blind spots between writing and verification.

Authority: the card requires verification for code; hansa enforces the negative-verdict gate.

## 6. One task has one goal, sized for one run

A task says what counts as done and asks only for the work needed to finish.
Work too large for one worker run is split. If verification will judge a task,
include a table of cases and state whether each rare case should be refused or
accepted unchanged. This guards against vague tasks that grow in scope and
cannot be judged fairly.

Authority: the card asks the coordinator to write tasks this way.

## 7. Stop a rework loop

After verification rejects a task a second time for the same kind of reason,
the coordinator stops the rework loop: make it smaller, ask for less, or start
again from the base. This guards against rework costing more than the change
is worth.

Authority: the card asks for this; hansa prompts after repeated failed verdicts.

## 8. Sort what verification finds

A defect in ordinary use is fixed now; a task that asked for the wrong thing
needs rewriting. Small gaps in rare combinations of states can receive
"holds, with notes"; they stay on record in `hansa gaps`, where they can be
marked as later or won't fix with a reason. Losing work, exposing keys, merging
into the wrong branch or falsely reporting a passed check always calls for
rejection. This guards against endless polishing and findings getting lost.

Authority: the card directs review; hansa gives verifiers these criteria and keeps notes and marks.

## 9. Nothing ships without the principal

Merges stop at the lane's base branch. A release needs the principal's yes on
the page, their alert's one-time code, or a target they ticked to go out without
asking. Approval remains valid only while the release still matches its recorded
inputs. The forbidden-word scan uses the principal's rules and refuses a release
when it finds a match. This guards against an agent publishing without the
principal's authority.

Authority: hansa enforces release approval and the word scan; the card forbids publishing directly.

## 10. Work stays on record; the principal need not read code

Tasks, reports, verdicts, merges, releases and principal to-dos are kept. Token
usage is valued at the principal's prices, with costs by seat, lane, task and
model. Subscription values are not bills; hansa flags missing coordinator
usage. The page shows what waits for the principal, what changed and
what it cost; the coordinator operates hansa for them. This guards against
losing track of decisions, changes and spending.

Authority: hansa keeps and displays the records; the card assigns operation to the coordinator.
