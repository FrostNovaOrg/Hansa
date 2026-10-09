# Releases and alerts

How a lane's base branch goes out after the principal's yes, and how hansa reaches the principal away
from the page. The coordinator's side of it is in [hansa/card.md](../hansa/card.md) (Releasing).

## How a release goes out

Releases are the last step. hansa merges into a lane's base branch and stops
there; a lane's release command (a push, a wait for CI such as `gh run watch
--exit-status`, a deploy script) is how the base branch goes out. A lane
that sends it out in more than one way (a push to the public repository, an
install on a test server, an install on the production server) names its
release targets instead, with `hansa lane target L NAME "<command>"
[--minutes N] [--after OTHER] [--fixed]`: each has its own command, its own time limit
(else the lane's `timeout_min`) and optionally another target of the lane
that must have sent out the same commit first; a lane has either the
`release` command (its one unnamed target) or named targets. `hansa
release L [TARGET] [note]` (TARGET needed when there is more than one)
records the request: the target, the commit, the command, the note, the
commits that go out (on no remote-tracking branch of the repository and not
in that target's last release; the whole history when it has no
remote-tracking branch) and the files they change, a file the command names
(such as `./deploy.sh`) marked. A word from the principal's list of words no
release may send out, in the command line or in a line, a file name, a
message or the author's or committer's name or email of any outgoing commit
(all are checked; matched within one line, in text read as UTF-8), refuses
it; each hit names its commit and where it is (`author`, `committer`, a
file). A command that holds one of those words
is kept when it is set (`hansa lane set L release`, `hansa lane target`,
`hansa lane add --like`), and hansa says the word and where at once, before
any release is asked. A command that holds a known key is
refused when it is set. A target that must follow another is asked only for
a commit the other one already sent out, and looked at again before it
starts. Otherwise the page shows it, with its target, with Release and
Refuse; a target the principal ticks as released without asking (each one
on its own) needs no answer while the lane's repository and base branch, the
target's command, the target it must follow (its `--after`, or none) and
whether it is `--fixed` are what they were when the list was saved, and while the desk is not paused: a
change to any of them, `--after` dropped or changed included, unticks it
until the principal takes it back in (the page's button that takes changed
entries back in as they are now, or unticking and ticking it again, then
Save; a plain Save keeps the old entry); `--minutes` alone does not, and a
target taken out and set again with the same command and `--after` keeps its
tick. An entry for a target no lane has now (the lane is gone, has no release, or its
release command became named targets) stays on the list, ticked, among the
others, said as such with the command it had when saved, until the principal
unticks it and saves. The principal answers in one of two ways: on the page, or by saying
in the conversation the one-time code that comes with the alert for that
release (6 characters, no look-alikes), which the coordinator passes on
with `hansa release yes ID CODE` (or `no ID CODE`). The code travels only
in the principal's alert: hansa keeps only a keyed hash of it (the key is in
the desk's `secrets/`, which no agent may read), keeps none of what the
alert command prints for such an alert (only how it ended), runs the command
in a temporary folder of its own under the desk's `secrets/alert-runs`
(closed to every agent, removed when it ends), and never writes
the code in events, the release's
record, `hansa status`, `hansa release` or the page, which says only whether
a code went out and when. It answers that release only, once, and is
recorded as the principal's answer given through the coordinator (channel
`code`); three wrong codes void it, and it lapses when the release is
answered, stopped or no longer what was asked, after 24 hours, or when a new
alert for the release brings a new one. With no alert command there is no
code: the page alone answers. Your alert command must not keep the alert's
text anywhere the coordinator can read it (a file, a log, a chat history):
the code in it approves a release. No lane may be named `yes` or `no`, as
those words begin the answer. hansa serve runs the
command only then, and only while the base branch and the command are still
what the principal saw (else the request is void and the coordinator asks
again): in the lane's repository at that commit, as the desk's account,
outside the sandbox (it needs the account's logins and network), with
hansa's own environment (not the lane's env), the target's time limit as its
limit, its output kept without keys in `.hansa/releases/`. One release at a
time per repository. A release that runs in the repository holds it: none
starts while a merge or merge check runs there, nor a merge while it runs. A
target set `--fixed` runs instead in a detached checkout of the release's
commit made for it under the desk (removed when it ends, however it ends,
and swept after a restart), with `HANSA_RELEASE_COMMIT`,
`HANSA_RELEASE_TARGET` and `HANSA_RELEASE_REPO` in its environment, and
holds nothing, so a long install leaves the repository's merges going on;
its command should send the commit (`$HANSA_RELEASE_COMMIT`, or `HEAD`), not
the base branch, which may have moved past what the principal saw. hansa
refuses a fixed target whose command line itself names the base branch as
what to send (`main`, `refs/heads/main`, `main~0`, `main^{commit}`,
`main@{0}`, or the source of `main:prod`; `HEAD:main` is allowed), but a
variable the command expands or a script it runs is not looked into. The commits going out
are read and checked for forbidden words without holding the desk, each
commit's own change once (a merge's: only what it adds beyond its parents,
git's `--cc`), and recorded only if they are still what goes out; hansa
serve checks again at the start only when the commit, the starting point,
the words or the command changed. The coordinator hears when the principal answers
and when the release ends. `hansa release` lists them; `hansa explain lane L`
shows each target's command and its last release.

## Files a release runs outside the sandbox

A release builds and starts what is on the base branch outside the sandbox,
so a change to its build files (a Maven pom's plugins, a build script, the
release script itself) is worker-written code a release runs unsandboxed. A
lane lists such files as `release_files`, paths or globs relative to the
repository (`**/` for any folders): `hansa lane set L release_files
"pom.xml **/pom.xml build.sh tools/release.sh"`, `hansa lane add
--release-files ...`, copied by `--like`. `hansa merge` refuses a change
touching one of them until a verification of that change holds (`hansa
verify ID`), naming the files; `--despite-check` does not let it through,
nor does anything else. `hansa explain lane L` shows the list, and advises
one for a lane with release targets and none.

## Stopping a release

A release can be stopped: the coordinator with `hansa release --stop ID
[reason]`, the principal with Stop on the page. One that has not started yet
(waiting for the principal's answer, or approved and waiting its turn) is
closed as stopped and never runs. A running one gets TERM to its command's
whole process group, then KILL 30 seconds later to what still runs; it is
recorded as stopped, with who stopped it and why, and its output so far is
kept (a fixed target's checkout is removed as at any other end). A stopped
release went out for no `--after`: a target that must follow it is not let
through. A stopped install may leave the server half-switched, so a release
command for which that matters should handle TERM: finish or undo the step it
is in. A stop that comes after the command has already ended is not a stop:
the release is recorded done or failed, as its exit says, with a note that the
stop came after it ended. What a release's command starts is recorded while
it runs, also what leaves its process group, and what of it still runs is
stopped at any end (a stop, the time limit, a failure, done). The principal's
alert command is told when the coordinator stops one (`release_stopped`); the
coordinator hears when the principal does.

## Alerts

Alerts reach the principal away from the page. The principal may set one
alert command line on the page (Settings, Alerts), such as a script that
sends a phone message; the page records that it saved it, and when. Only the
page sets it: no `hansa` command sets, changes or clears it, and `hansa lane
set` has nothing like it. `hansa serve` runs it when a release starts waiting
for the principal's yes (also when a ticked target's tick no longer holds and
its release goes back to waiting), when a release ends (done, failed, void or
forbidden), when the coordinator stops a release, when a request to the principal is filed (`hansa ask`), and when
its own rounds have stopped for 15 minutes; not for ordinary task events,
which go to the coordinator. It runs as the desk's account, outside the
sandbox, in a temporary folder of its own under the desk's `secrets/alert-runs` (closed to the
coordinator, workers, verifiers and design sessions; removed when the command ends, and what a stopped or killed
`hansa serve` left there is swept when it starts), with hansa's own environment (not a lane's),
the kind of alert in `HANSA_ALERT_KIND` (`release_waiting`, `release_ended`,
`release_stopped`, `ask`, `server_stuck`, `test`; comma-separated when several go out together)
and the text on standard input: UTF-8, in English, keys removed, cut at 1,000
characters; a release's text names its lane and target. It has 30 seconds; its own exit ends the run, even if it leaves a
child running, and only at the limit is its process group killed. Alerts
within 20 seconds of each other go out in one run, one run at a time, and at
most 20 runs an hour: beyond that, alerts are held and go out together in one
run when the hour frees a run, with a line saying how many were held back
(the page's test button is not held). A run that fails or times out is recorded
(event `alert_failed`) and `hansa status` shows the last failure with its exit
code and last lines (none for an alert that held a release's one-time code:
then only how it ended is kept); there is no retry: the next alert tries again. The page
has a button that sends a test alert; no coordinator command sends one.
`hansa explain` says whether a command is set and what it is.
