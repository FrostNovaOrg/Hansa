# Releases and alerts

How a lane's base branch goes out after the principal's yes, and how hansa reaches the principal away
from the page. The coordinator's side of it is in [hansa/card.md](../hansa/card.md) (Releasing).

## How a release goes out

`hansa lane add X --like a` leaves release targets out; name one deliberately with
`hansa lane target X NAME "<command>"`. This keeps stale copies off the principal's release list.
A merge lane releases nothing: remove every target with `hansa lane target L NAME none`
before setting `merge_lane true`. It refuses target settings and release requests; old targets
can still be removed one at a time. `hansa status` and `hansa explain lane L` name those
left by older versions or hand edits, with the commands to remove them.

Releases are the last step. hansa merges into a lane's base branch and stops
there; a lane's release command (a push, a deploy script) is how the base branch goes out. A lane
that sends it out in more than one way (a push to the public repository, an
install on a test server, an install on the production server) names its
release targets, with `hansa lane target L NAME "<command>"
[--minutes N] [--after OTHER] [--fixed] [--about "<words>"]`: each has its own command, its own time limit
(else the lane's `timeout_min`; an invalid target limit in a hand-edited settings file also uses the lane's)
and optionally another target of the lane
that must have sent out the same commit first. An older unnamed release
moves to `default` if absent, taking only its tick for the same command.
Other unnamed ticks match nothing, even if `default` has the same command.
`--about "install on the test server; nothing goes outside"` describes a target
in plain words: one line of up to 120 characters; a longer description or a line
break is refused without changing anything. `--about none` removes it.
`--about` alone changes only the description, and is recorded like other target changes.
A target left without one gets a one-line reminder from `hansa lane target`, and on the
page (Settings, the list of releases without asking) a line saying it has no description yet
and the command that adds it. Each entry there also says its target's last release that ran
to an end (when, and done, failed or stopped) or that it was never released.
A new or changed description holding a key the desk knows is refused to prevent
storing and showing the key as written; this check is needed while descriptions
are stored and shown as written.
Settings shows each tick with the target's name, then its lane, its description
if present, and always its command on a separate line. The command is cut at the
row's width; hovering shows it whole.
`hansa lane target L` lists them, each with its description and the way it runs. `hansa
release L [TARGET] [note]` (TARGET needed when there is more than one, and before a one-word
note that could be a target name, as in `hansa release a public staging`)
records the request (a new request for the same target replaces one still
waiting): the target, the commit, the command, the note, the
commits that go out (with one remote or none: on no remote-tracking branch of the repository
and not in that target's last release; the whole history when it has no
remote-tracking branch) and the files they change, a file the command names
(such as `./deploy.sh`) marked. On a one-target lane, an unknown word that `hansa lane target`
would accept as a new target name is refused, to keep a mistyped target from releasing the only one;
a quoted note with spaces can still omit TARGET. The reply names any waiting request it replaces
and, if it had a one-time code, says that code no longer works. At the same commit as the target's
last finished release, if the request is not refused, the reply first says there is nothing new,
names that release and says it was asked to run again;
asking again stays allowed. These remarks appear in the command's reply, not in the alert.
For a target's first release with two or more remotes, including a `--fixed` target,
every base commit missing from at least one remote's tracking branches is counted, shown,
scanned and checked for direct commits, even if another remote already has it;
the heading gives each remote's missing count (for example, `0 not on remote mirror, 3 not on remote public`),
and a remote with no tracking branches lacks the whole history, so an earlier private push cannot hide a first public release.
A word from the principal's list of words no
release may send out, in the release or watch command line or in a line, a file name, a
message or the author's or committer's name or email of any outgoing commit
(all are checked, and the page shows at most 200 of them and says how many
were checked; the word scan and its limits are under [Forbidden words before a
release](#forbidden-words-before-a-release)), refuses it; each hit names its commit and where it is (`author`, `committer`, a
file). A release or watch command that holds one of those words
is kept when it is set (`hansa lane target`), and hansa says the word and where at once, before
any release is asked. A command that holds one of the desk's keys is
refused when it is set, since a command is stored and shown as written: have
it read the key from where the tool keeps it (its own login or config). A target that must follow another is asked only for
a commit the other one already sent out, and looked at again before it
starts. Otherwise the page shows it, with its target, with Release and
Refuse. Release takes one tap; the optional refusal reason and Refuse sit
together on a separate row. A target the principal ticks as released without asking (each one
on its own) needs no answer while the lane's repository and base branch, the
target's command and watch command, the target it must follow (its `--after`, or none) and
whether it is `--fixed` are what they were when the list was saved, and while the desk is not paused: a
change to any of them, `--after` dropped or changed included, unticks it
until the principal takes it back in (the page's button that takes changed
entries back in as they are now, or unticking and ticking it again, then
Save; a plain Save keeps the old entry); `--minutes` or `--about` alone does not, and a
target taken out and set again with the same command and `--after` keeps its
tick. An entry for a target no lane has now (the lane is gone, has no release, or its
release command became named targets) stays on the list, ticked, among the
others, said as such with the command it had when saved, until the principal
unticks it and saves. The principal answers in one of two ways: on the page, or by saying
in the conversation the one-time code that comes with the alert for that
release (6 characters, no look-alikes), which the coordinator passes on
with `hansa release yes ID CODE` (or `no ID CODE [reason]`). Pass the code as one word,
with optional dashes and any letter case. A word that, after removing dashes, is not
six characters from `ABCDEFGHJKMNPQRSTUVWXYZ23456789` with at least one letter is
refused without counting a wrong try, for either answer. This check is needed while
split or malformed input could use up the principal's three tries. A wrong code of that
form still counts. The code travels only
to the principal's alert command and enabled Bark entries that encrypt: the queue holds the release ID,
request time and text without a code. Immediately before the channels start, hansa checks that the
same request still waits, makes one code per release in that batch for all eligible channels, and rebuilds
its text in memory. Re-asking after a void gets a new release ID; an old alert
cannot acquire a code for the new request. Waiting alerts whose requests no longer wait,
or whose request time has changed, are left out; alerts reporting their end still go out. Waiting through the hourly cap or a server restart
creates no code. Older queues that contain codes are rewritten on their next
read under the desk lock: the old code is revoked and both it and its text are
replaced by a code-free entry, ready for a fresh code when sent. This migration
is needed while desks may still have queues written by older hansa versions.
hansa keeps only a keyed hash of a code (the key is in
the desk's `secrets/`, which no agent may read), keeps none of what the
alert command prints for such an alert (only how it ended), passes its text
through a pipe on stdin without a temporary input file, runs the command
in a temporary folder of its own under the desk's `secrets/alert-runs`
(closed to every agent, removed when it ends), and never writes
the code in events, the release's
record, `hansa status`, `hansa release` or the page, which says only whether
a code went out and when. It answers that release only, once, and is
recorded as the principal's answer given through the coordinator (channel
`code`); three wrong codes void it, and it lapses when the release is
answered, stopped or no longer what was asked, after 24 hours, or when a new
alert for the release brings a new one. With no alert command or enabled Bark entry that encrypts there is no
code: the page alone answers. The principal's alert command must not keep the alert's
text anywhere the coordinator can read it (a file, a log, a chat history):
the code in it approves a release. No lane may be named `yes` or `no`, as
those words begin the answer. hansa serve runs the
command only then, and only while the base branch and the command are still
what the principal saw (else the request is void and the coordinator asks
again; when the lane or its target was removed, the message says to set it
again first, with the command, before asking again): in the lane's
repository at that commit, as the desk's account,
outside the sandbox (it needs the account's logins and network), with
hansa's own environment (not the lane's env), the target's time limit as its
limit, its output kept without keys in `.hansa/releases/` (the daily trim keeps
the last 256 KB once it ended over 30 days ago). One release at a
time per repository. A release that runs in the repository holds it: none
starts while a merge or merge check runs there, nor a merge while it runs. A
target set `--fixed` runs instead in a detached checkout of the release's
commit made for it under the desk (removed when it ends, however it ends,
and swept after a restart), with `HANSA_RELEASE_COMMIT`,
`HANSA_RELEASE_TARGET` and `HANSA_RELEASE_REPO` in its environment, and
does not hold the repository's merge lock, so merges and merge checks go on
during a long install;
its command should send the commit (`$HANSA_RELEASE_COMMIT`, or `HEAD`), not
the base branch, which may have moved past what the principal saw. hansa
refuses a fixed target whose command line itself names the base branch as
what to send (`main`, `refs/heads/main`, `main~0`, `main^{commit}`,
`main@{0}`, or the source of `main:prod`; `HEAD:main` is allowed), but a
variable the command expands or a script it runs is not looked into. The commits going out
are read and scanned for forbidden words without holding the desk, each
commit's own change once (a merge's: only what it adds beyond its parents,
git's `--cc`), and recorded only if they are still what goes out; hansa
serve checks again at the start only when the commit, the starting point,
the words or the command changed. The coordinator hears when the principal answers
and when the release ends. When a release passes, the posted line includes
`next: hansa release L U` for each target U of that lane whose `--after` names
the passing target, unless U already sent that commit or has a release waiting,
approved, starting or running. A target
with `--after` runs only when asked; the hint does not ask for it or start it.
`hansa release` lists them. Void (including replaced) and stopped requests take one line each,
with their number, target, outcome and reason. Outside-path warnings appear in the list and in
`hansa release --show ID` only while a request waits for the principal's yes; the ask and
`hansa explain lane L` still show them.
The ask puts the waiting instruction directly after the head, before commit details.
`hansa explain lane L` gives each target short lines for its command and whether it goes out
without asking, then its last successful release (number, commit and time) and the latest request
if different. `hansa lane target L` shows whether every target goes out without asking, including
targets without `--after-merge`. When a saved tick has lapsed, both views and a new release ask
name the reason.

Put a step that cannot be taken back (pushing a tag, publishing a package) last in a target's
command, after the build it depends on has passed, so that a target that fails halfway has published
nothing.

### Watching CI after a push

Set `hansa lane target L NAME --watch "<command>"` to check the pushed commit
after the release command exits 0; `--watch none` removes it. The watch runs
outside the repository's queue: merges and later releases go ahead, and each
release keeps its own watch. hansa runs it outside the sandbox, with its own
environment and `HANSA_RELEASE_COMMIT`, `HANSA_RELEASE_TARGET` and
`HANSA_RELEASE_REPO`, from that release's checkout of the commit (a fixed target
keeps its working folder until the watch ends; other targets get a checkout for
the watch). `--watch-minutes N` sets a separate limit, default 90 minutes
(1–1440). At the limit hansa stops the watch's process group. A failed, void or
stopped release starts no watch. An unfinished watch starts again from the
beginning after hansa serve restarts, so it should be safe to repeat.
The watch command gets the same forbidden-word warning when saved and refusal
when a release is requested as the release command, with `watch command` as the place found,
including when a pattern could not check it within the limit. A refusal's remedy names the release command,
the watch command or both, according to where the words were found;
this prevents a watch from sending words the principal forbids.

hansa posts a line for that release to the coordinator's session: watch passed
(exit 0), failed (its exit code and last lines, keys removed), or timed out.
The page's release list and `hansa release` show the state and result; `hansa release --show ID`
shows the request and all retained release and watch output (up to 15 lines each), keys removed,
including output so far while running. It shows waiting, void and stopped requests too. Failed release lists
and posted lines keep their last five lines and name this command for more. The whole log stays
the principal's and is not printed by `--show`. Watch output
is kept in `.hansa/releases/<id>.watch.log`, with the same last-line and daily
trimming limits as release output. The page shows the watch command beside
the target's command before approval. Changing, adding or removing it voids
a request still waiting for approval and stops the target's tick counting,
as changing its release command does. Changing only `--watch-minutes` keeps
the tick.

While a release's command runs, `hansa status` adds to its line under `releases (hansa release):` how long it has
run and its limit (`running 4 min (limit 30)`); while its watch runs, it keeps a line for the release, which is done
by then (`sent; its check after the push has run 34 min (limit 120)`); a watch that ended is not listed. A lane held
by `hansa lane try` says the same of the try. `hansa status --json` and the page's status data gather these in
`at_work`: a row per running release command, running watch and running lane try, with `kind` (`release`, `watch`
or `try`), `id` (the release's; none for a try), `lane`, `target`, `started`, `minutes` and `limit_min`; each
`releases[]` row carries its `started`. The coordinator's status line names them instead of `idle`
([the status line](desk.md#the-status-line)).

For GitHub Actions, this example watches the push run of `tests.yml` for the
exact pushed commit. It retries empty results and network errors for five
minutes while the run appears, then uses `gh` to wait for its result and read
its conclusion, trying again after a network error at either step: it exits
0 when the run succeeded and otherwise non-zero, printing the conclusion.
Set the workflow to the repository's CI workflow; `gh` uses the desk account's
login. Single quotes keep `$HANSA_RELEASE_COMMIT` for the watch to expand:

```sh
hansa lane target L NAME --watch '
end=$(( $(date +%s) + 300 ))
while :; do
  run=$(gh run list --workflow tests.yml --event push --commit "$HANSA_RELEASE_COMMIT" --limit 1 --json databaseId --jq ".[0].databaseId // empty") && [ -n "$run" ] && break
  [ "$(date +%s)" -lt "$end" ] || { echo "No CI run appeared within five minutes"; exit 1; }
  sleep 10
done
while :; do
  gh run watch "$run" && break
  sleep 10
done
while :; do
  conclusion=$(gh run view "$run" --json conclusion --jq ".conclusion") && break
  sleep 10
done
[ "$conclusion" = success ] || { echo "CI run $run: $conclusion"; exit 1; }
'
```

### Collecting changelog lines

Workers end reports with `Changelog: SENTENCE` for users, or `Changelog: none`.
Follow-ups describe the whole branch change; verifications supply no line.
`hansa changelog L [TARGET]` lists changes since that target's last done release,
across lanes sharing the repository and base, for one task to write up once per release.
TARGET is required with several targets. The header names the range; without a done
release and without remote-tracking history, commits before the first hansa landing are left out,
using the release ask's cutoff. Without a hansa landing, all history still counts. With remote-tracking
history or a previous done release, the existing range applies. A release outside base history is noted;
git's range still applies.
The last marker counts, ignoring case and accepting bold or bullet formatting.
`none` (with optional backticks or period) and `-` ids appear last. Missing lines,
direct commits and unidentifiable task commits are flagged. Verification reports are never read.

### A release gated on a full build on another desk

A release that should go on only if a full build passes on another desk's
machine (one whose sandbox allows what this one's refuses): a `--fixed`
target that pushes `$HANSA_RELEASE_COMMIT` to the other desk's base branch and
runs `hansa lane try L --merge-check --at "$HANSA_RELEASE_COMMIT"` there, and
the public target set `--after` it. `--at COMMIT` answers for that commit
only: it is refused with nothing run unless COMMIT is the base's tip there, a
try at another commit (one already running, say) is waited for and never taken
as the answer, and it exits 0 only when the try at COMMIT passed. So a try asked
again after the base moved never passes on an older commit's verdict.

A full build can run for minutes with no output; an ssh silent that long can
break with neither end noticing, and the release then waits out its time limit
for an answer that never comes. Give each attempt its own remote folder with
`mktemp -d` under an existing `/path/to/tries`. Start the try there with `nohup`,
its output then its exit code going to that folder's `result.log`. A rerun at
the same commit gets a different folder and cannot read an earlier result.
Poll for that code every 30 seconds or so with
short ssh calls (connect timeout, keepalive) until it appears or time runs out:

```sh
c=$HANSA_RELEASE_COMMIT
o="-o ConnectTimeout=20 -o ServerAliveInterval=10"
d=$(ssh $o HOST "mktemp -d /path/to/tries/$c.XXXXXXXX") || exit 1
f=$d/result.log
ssh $o HOST "nohup sh -c '/path/to/hansa/bin/hansa --desk /path/to/desk lane try L --merge-check --at $c; echo try-exit=\$?' >$f 2>&1 </dev/null &" || exit 1
end=$(( $(date +%s) + 3000 ))  # under the target's own time limit
while [ "$(date +%s)" -lt "$end" ]; do
  sleep 30
  rc=$(ssh $o HOST "sed -n 's/^try-exit=//p' $f") || continue
  [ -n "$rc" ] && exit "$rc"
done
exit 1
```

### Stopping a release

Beside Stop, the page shows a short warning that stopping may leave the server
partly updated. Tap or click that warning to read the full explanation, including
TERM, KILL after 30 seconds, and stopping a running watch. It stays open across
status refreshes while you read, in either page language. On a phone, Stop and
its warning sit under the release's number, at the start of its row, so they are
in view without scrolling the releases table sideways.

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

When the release is done and its watch still runs, the same `--stop ID [reason]`
or Stop on the page stops the watch, leaving the release done. The watch records
stopped, by whom and why, and keeps its output so far; a server restart does not
start it again. A release whose watch has ended, or that has no watch, is still
refused as already ended. If the watch's command has already written its exit
code when the stop is handled, hansa keeps its passed or failed result and says
the stop came after its command had ended, as for a release.

The request lists direct commits among its outgoing commits, with their count (at most 200 SHAs and
subjects): walk the base's first-parent line; a task's recorded `merge_commit` that hansa landed in
this repository and base, from any lane, and what it brought in are not direct. Every other commit
on that line, and what a direct merge brought in (even a task's branch merged by hand), is direct; a
later hansa merge does not hide an earlier direct commit. With no remote-tracking branch and no
readable last release, history older on that line than the oldest hansa merge in the outgoing range
is history from before hansa and is not direct. With no hansa merge in that range either, every
outgoing commit is listed as direct: hansa cannot tell what came before it. With a remote-tracking
branch, no such cutoff applies, even when the last release's commit is gone. Commits outside
the outgoing range described above are not looked at, nor are working files
or uncommitted changes; the ask and waiting line use recorded task landings to identify direct commits. Only the
principal saves `release_direct` on the page: `record` (default) lists these commits and keeps ticked
targets going out without asking; `hold` (also used for an unknown setting value) refuses a release
with them unless asked with `--direct "<why>"`, then records the reason and waits for the principal's
yes even for a ticked target. The refusal names the first ten commits and their count and records
nothing; `--direct` with no direct commits is refused. The page and `hansa release` show the recorded
commits and reason. The ask and `hansa release` show the count and at most the first ten direct
commits, with a copyable `git -C REPO log --oneline COMMIT [^SINCE] --` command for the full
commit range (including task commits). This also reaches commits beyond the 200 stored on a request;
the full range is read from git, so the repository must still have those commits. Waiting alerts
give only their count, never subjects; posted completion lines already omit commit subjects.

For checks that need another machine, system or the project's CI, the coordinator can set
`hansa lane target L NAME --after-merge MINUTES` (1–1440; `none` turns it off), refused on a target
with `--after`: an invalid `after_merge_min` in a hand-edited settings file turns it off. The setting is
kept without the principal's tick, but only a target whose tick still
counts runs by itself. After a task lands in that repository and base branch, from any lane and by
any hansa merge, hansa waits MINUTES after the last such merge and until no task of that repository
is merging, then asks the ordinary release, marked as asked by hansa after merges and naming them.
It checks forbidden words and outside files, waits for resume and free space over the floor, and
voids the request if its tick no longer counts; with `release_direct: hold`, outgoing direct commits
prevent the ask and tell the coordinator to ask by hand with `--direct "<why>"`. One run per target
covers a batch; merges during it make one more run after it ends, and no new merges mean no new
request or git/file reads for scheduling. A run asked by hand, once passed or failed, covers the
merges before it was asked; a target that never ran covers all merges before its first run, naming
the 10 newest and the count of all. `hansa lane target L` and `hansa explain lane L` show the
setting and tick. A failed run reaches the coordinator as a failed check, naming the target and
merges since its last passing run: make a task to fix what failed, and the target runs again after
its merge. Only failure alerts the principal.

While a batch waits, `hansa status` and the page show its lane, target, current base commit and
scheduled start (MINUTES after the last merge). Another merge moves that time and commit. A missing
or invalidated tick says it waits for the principal's tick; outgoing direct commits under
`release_direct: hold` say it is held for the principal's yes. Once the start time arrives, an
unasked batch says it starts when the server next looks. A paused desk says it waits for resume.
Once the release is asked, its ordinary release row takes over.
No new merges since the last run means no waiting line.

## Forbidden words before a release

A release, and the steps before it, look for the words the principal keeps
from going out (listed on the page): the word scan. By the time a release finds a forbidden word, the word is on the base
branch: taking it out takes another task, and the history still holds it.
So hansa looks for the same words, matched the same way (any case, within
one line, in text read as UTF-8), earlier:

- `hansa add` refuses a title that holds one, saying the
  word and that it is in `--title` or in the text's first line (the title
  when no `--title` is given), and queue nothing: the title goes into the
  task's commit message and its merge's. The rest of the task's text is
  not committed and is not looked at.
- `hansa merge` refuses a task whose commits that the
  base branch does not hold yet hold one: in a message, an added line (a
  merge commit's: what it adds beyond its parents), the name of a file
  added or changed, or an author's or committer's name or email. The merge
  commit's own message and identity (the lane's `merge_message` and
  `identity`) are looked at too. It says the words and the places, as a
  release does, and what to do: a retry that takes them out (`hansa retry
  ID --fresh "..."`; a retry that continues keeps the commits, and a word in
  the title needs the task added again with another `--title`), or ask the
  principal. A merge that waited its turn looks again before it merges, and
  a merge check looks again at the merge commit it would land; either sends
  the task back for review with the places in its result.

An entry starting with `re:` is a Python regular expression, matched with
`re.IGNORECASE` within each line wherever a word is checked; a hit names the
whole entry as written, including `re:`. For example, `re:第\s*\d+\s*笔`
finds any number in that shape, and `re:\blane [a-k]\b` finds lane codes in
any case. The page refuses to save a pattern that does not compile, exceeds
200 characters including `re:`, or matches the empty string, saying why and
saving nothing. It also refuses backreferences and repeats of two or more
whose body contains a group, alternation or another repeat, because they can
backtrack badly and stall the desk.
To end stalls from allowed patterns, each scan has fixed limits of 2 seconds on one line and 60 seconds spent matching patterns in all, and with patterns set a line over 10,000 characters is refused without searching it with patterns (literal words still check the whole line): shorten or split the line, or ask the principal to except the entry for that lane (the limits can go when matching itself has a bounded runtime).
A scan reads added lines only until it has 20 hits, and names at most those 20.
Other entries stay literal, including regex characters such
as the dot in `a.b`. These patterns catch shapes a finite word list cannot;
the principal can remove an entry when that shape is safe to send out.

The principal's `release_forbidden_except` on the same page lists exceptions
under the forbidden entries, one row per entry with its lanes:
`{"entry": "internal-only", "lanes": ["e", "f"]}`. The entry must be spelled
exactly as on `release_forbidden`. On those lanes it is skipped in task
titles, merges and merge checks (including checks run by a merge
lane for that task), release targets' commands and releases. Every other
entry and the mailbox rule still apply. The page offers the current entries to
choose from. An exception matches its entry exactly as the word scan reads it,
`re:` included, and the page trims the spaces around each line typed in the box
of entries. Every settings save, commands included, refuses dangling
exceptions, preventing silent mistakes; reasons give position and lanes, never
entry text. Legacy dangling rows are marked with removal
controls. Removing an entry removes its exceptions in that save, after showing
their count. Missing lanes stay marked as matching nothing.
Changing a lane's exceptions makes its earlier scans unusable.
No command changes the exceptions; `hansa explain lane L` gives only their
count. Exceptions let a repository use a word it needs while other lanes
still refuse it; remove one when that repository no longer needs the word.

With an empty list and the mailbox rule off nothing is looked for and nothing changes. The release's
own word scan stays as it was, the last net: a word set after a merge, or a
commit that came to the base branch another way, is still found there.

On the same page the principal can turn on `release_mailboxes` (off by
default): merges and releases then check mailbox-shaped strings
(`[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}`) in every added line, in every
file the word scan reads as UTF-8, even with no forbidden words. The rule does
not check removed or unchanged lines, commit messages, names or file names;
a merge's own lines are read as the word scan reads them. Reserved example
domains, ignoring case, are `example.com`, `example.org`, `example.net` and
their subdomains, and domains ending in `.invalid`, `.test` or `.example`.
The principal's `release_mailbox_allow` on that page allows addresses the
project needs: one entry per line, at most 500 entries. A full address allows
that address, ignoring case; `@domain` allows that domain and its subdomains,
ignoring case (`@python.org` allows `a@python.org` and `a@mail.python.org`,
not `a@notpython.org` or `a@python.org.evil.com`). Saving refuses empty entries,
spaces, pattern characters (`*?[]`), `@` alone and entries in neither form.
Empty domain labels and more than one `@` are refused.
The domain must contain a dot and its last label must be at least two letters,
as the matcher requires; `@com`, `@realmail` and `jane@realmail` are refused.
Each entry may have at most 254 characters. Duplicates are refused ignoring
case, naming both entry numbers. The page drops blank lines before saving;
an empty entry sent by other means is refused.
Strings that look like addresses and are not, such as `git@github.com` in a clone URL or `logo@2x.png`, go on the allowlist like an address.
No command changes the rule or allowlist: on a refusal, which names each
mailbox, commit, file and line (at most 20 hits), the coordinator replaces
the address with an example one in a fresh retry, or, if the project needs
it, asks the principal to add the address or its domain to the allowlist on
the page (`hansa ask`). `hansa explain` says whether the rule is on and lists
the first 20 allowlist entries, then says how many more are on the page.
An allowlist change makes earlier scans unusable, whether an
entry was added or removed.

The word scan is a net, not a proof. A word is matched within one line only,
so a phrase split over a line break is not found; and only in text hansa can
read as UTF-8, so a UTF-16 file is not looked into. A binary file (a NUL byte
in its new content's first 8,000 bytes) is looked at by its name only,
whatever `.gitattributes` says.

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
`hansa explain lane L` also derives recent check times and slowdowns (a merge
lane shows the merge checks it ran for all lanes it serves), check commands
repeated by the actual merge command, and counts of merged tasks touching each
`release_files` entry in the last 14 days, including renamed-away files, from
existing records and git when asked, never in an idle round.

Before recording, approving or starting a release, hansa checks recognized
inputs that are existing regular repository files, including binary and
ignored files. Each file and repository link followed to reach it must occur
in the release commit; staging alone does not count. For initialized submodules,
their recorded gitlink commits apply. Otherwise the request is refused or voided;
the message explains how to ask again.
These checks keep recognized inputs tied to the approved commit when a release
runs with the desk account's permissions. They may be removed when the execution
environment ensures the same binding for every recognized input.

The scan checks command names, operands of recognized shells and interpreters
(`sh`, `bash`, `dash`, `zsh`, `ksh`, `ash`, `python` and numbered variants,
`perl`, `ruby`, `node`, `php`, `lua`),
`source`/`.` and `cat`/`awk`/`gawk`/`mawk`, `sed` script-file options, and
`timeout`/`xargs`/`find` operands. It follows common command prefixes (`env`,
`sudo`, `command`, `exec`, `nohup`, `nice`). Other commands' ordinary operands, including
output and metadata arguments, are left alone. Input redirections (`<`, `<>`)
are checked with every command; literal output destinations are left alone.
Unquoted filename patterns (`*`, `?`, `[abc]`) expand; quoted or escaped pattern
characters stay literal. Recognized `cd` paths set subsequent working directories,
with logical `-L` (the default) or physical `-P` handling of `..`; parentheses
and pipelines restore the enclosing directory. Uncertain control flow can mean
checking several directories. This is a scan of words, including nested command
text, not shell evaluation or a complete list of dependencies. It does not resolve
variables other than HOME, programs on PATH, computed filenames, or what a
script reads or runs in turn. Use committed command inputs and review the rest.

Prefer `--fixed` with relative inputs: these are checked in the actual fixed
checkout just before launch, independently of the original working copy.
Recognized inputs that reach back into the original repository must match the commit.
Creating a fixed checkout does not initialize submodules. An ordinary target
also requires the base branch and no tracked changes at start. These checks
do not certify inputs the scan does not recognize.

A command can also name a file outside the lane's repository: hansa looks
in its words, inside quotes too, for absolute paths, `~/`, `$HOME/` or
`${HOME}/` paths, and relative paths that leave the repository, expanding
HOME and following links. This includes a relative path such as
`./deploy.sh` or `deploy.sh` in `bash deploy.sh` through a repository link
to an outside file or folder; an outside link back into the repository is
not named. The part before the first `=` is a name, not a path, unless the
word starts like a path (`/`, `~/`, `$HOME/`, `${HOME}/`, `./` or `../`), so
`deploy.sh=1` names nothing while `~/ops/deploy=v2.sh` is taken whole.
Words split on `=`, not `:`: `--config=~/x` and `env V=~/x`
name `~/x`, while remote paths such as `host:/etc/hosts` and
`PATH=/usr/bin:/bin` name nothing. Words are split with `shlex`, falling
back to splitting on spaces when it cannot split the line. Comments,
here-strings and heredoc bodies are read as words too. Substitutions
(`$( )`, `>( )`, `<( )`, backticks) are read as words like the rest of the
line. An output redirection's destination (`>`, `>>`, `>|`, `&>`, `&>>`,
`>&`, including a descriptor number such as `2>`) is not named or
fingerprinted; an input redirection's file (`<`) is still looked into.
A file `tee` writes is named like any other, so write logs with `>>`,
which is left out. Existing regular text files (no NUL in the first 8
KB) are named when a target is set and recorded with their sha256 on a
release request and on the principal's tick; no worker can change them
and no verification covers them, so move them into the repository and
list them in `release_files`. A recorded fingerprint must still match: a
file that changes, disappears or can no longer be fingerprinted (over 16
MB, unreadable or now binary) has changed. A file not recorded must
still be absent: a new one counts even if it cannot be fingerprinted.
Such a change voids a waiting or approved request before the answer
counts or the release starts, and a change since the tick was saved
makes that tick stop counting; the page names the file, and taking
changed entries back in records their new fingerprints. An older request
or tick that did not record an outside file is void or stops counting
too, saying it did not record the file; one with no outside file keeps
counting. An explicit local `git push` destination is described as where the command sends to,
without advice to move it into the repository; a file it runs or folder it runs in keeps its existing
warning and advice. Outside folders are shown without a fingerprint; files over 16 MB or unreadable are named as not
fingerprinted, and a file recorded without a fingerprint is not compared.
Devices, binaries, bare names found on PATH, variables other than HOME,
and what a script runs in turn are not looked into. Fingerprints are taken
only when a target is set, a release is asked or checked again, or a tick
is saved or evaluated, never in an idle round with no release waiting or
approved. The page fingerprints each tick once per load, sharing the result
between languages. It also notes an alert command's outside text files
that are outside the desk's `secrets/`: the coordinator can change them and
one-time release codes pass through them, so keep them under `secrets/`;
saving is still allowed.

## Alerts

Alerts reach phones and chats through push addresses, alongside the principal's
alert command. Supported services are Bark, ntfy, Gotify, PushDeer, Server酱
(WeChat), WeCom, DingTalk, Feishu/Lark, Telegram, Discord, Slack and a chosen
JSON webhook. See
[push setup, privacy, levels, limits and error codes](push-services.md)
(`hansa help push-services`) for what to paste from each app. To add a push address:

1. Open **Settings → Alerts → Push addresses → Add an address** on the page.
2. Paste the whole address from your push app or service. Leave **Recognize it**
   selected, or choose a service by name. Each service's hint on the page says
   what to paste; an extra secret field appears only when it takes one.
3. Optionally name the entry, then press **Save settings**. Only the hidden
   address is shown after saving.
4. Press **Send a test push** on the entry. It sends even while off, and its
   last result shows sent with the time, or what went wrong.

For Bark, install and open the Bark app on your iPhone. Copy the whole address
shown on the Bark app's home screen and paste it into **Add an address**.
A copied command such as
`curl -X GET 'https://api.day.app/KEY/title/body?group=example&ttl=600'`
also works when the pasted text contains exactly one HTTPS address. Hansa
retains only the server and device key, dropping example text after the key
and Bark's query parameters.
If a self-hosted address cannot be recognized, choose **Bark** and save again.
For encryption, choose **AES-128**, **AES-192** or **AES-256**, mode **CBC**, in
Bark's encryption settings. Choose **Bark** on hansa's page and paste the same
16, 24 or 32 character ASCII key into **Encryption key (optional)**, or paste
the app's copied example there. Only the key is saved; the example must match
this entry's device and CBC key length. Hansa discards its IV and generates a
fresh random 16-character IV for every push. Save, then send a test push. The
encrypted test explains that **Decryption Failed** means the two keys differ.
The page, `hansa status` and `hansa explain` mark the saved entry **encrypted**.
See [the Bark guide](push-services.md#bark) for setup and privacy.

To replace an address or extra secret, paste the new value into the saved
entry; leave either field empty to keep its saved value. Switch off, rename
or remove entries, then **Save settings**. Only the principal changes addresses.

Each address (and an encryption key, signing secret or access token when needed) is kept
in a separate 0600 secret file under the desk's 0700 `secrets/` folder. Metadata
alone enters settings; history records changes without credentials. The page
and status show secret parts hidden.

The desk's name and alert texts leave this machine for each address's service
and, for phone apps, on to Apple's or Google's push service. Those services can
read them unless Bark encryption is enabled; then Bark's server and Apple see
ciphertext and routing information, without the desk name or alert text.
One-time release codes go to the alert command and to Bark entries that encrypt, inside the ciphertext
only. Other push entries get code-free wording asking for an answer on the page. With no alert command
or enabled Bark entry that encrypts, no code is made. Answering on the page is always available;
the code is a second way to answer, by giving it to the coordinator in the conversation.
If making any code fails, hansa voids the codes already made for that batch, records an
`alert_error`, and sends the whole batch to every channel without codes, asking for an answer on the page.
Removing an entry's encryption key makes later batches to it code-free. Its test push never makes or
carries a release code, even while a release waits. A key file edited by hand to a key the page would
refuse makes the entry send nothing (`bad_key`) and carry no code.

Each gathered batch goes to every enabled entry with an address. A command
needs a nonblank command line. Sends run side by side with independent deadlines
and results. A slow or failed entry does not delay another's send, change its
result, or send it a failure notice. With neither, nothing is queued.

The principal may also set one
alert command line on the page (Settings, Alerts), such as a script that
sends a phone message; the page records that it saved it, and when. Only the
page sets it: no `hansa` command sets, changes or clears it, and `hansa lane
set` has nothing like it. `hansa serve` runs it when a release starts waiting
for the principal's yes (also when a ticked target's tick no longer holds and
its release goes back to waiting), when a release ends (done, failed, void or
forbidden), when the coordinator stops a release, when a to-do for the principal is recorded (`hansa ask`), when a
provider runs out of credit (once per rest), when a
lane's runs cannot start because their sandbox cannot (once for each lane and reason until a passing sandbox try
clears the hold), when free space goes under the
floor and `hansa serve` starts nothing new (once each time: see [free space](desk.md#free-space)), when
its own rounds have stopped for 15 minutes, and when what hansa posts to the coordinator's session cannot reach it
(below); not for ordinary task events,
which go to the coordinator. After a passing sandbox try clears a lane's hold, a later hold alerts again,
including for the same reason.

After a `disk`, `sandbox` or `server_stuck` alert, recovery adds one passive line to the next gathered batch:
free space is back over the floor and its margin, that lane's sandbox passes a try again, or `hansa serve`
finishes a round again. The line says when the trouble began and ended, with dates and local times.
It keeps the same alert kind; sandbox recovery is per lane. The beginning is remembered across processes
and server restarts. A healthy look without an earlier queued alert sends no recovery; recovery while
channels are off clears the notice without saving it for later. The coordinator's `turn_ok` recovery stays as below.

The principal reads alerts on a phone and need not use a terminal, so a problem alert ends with a next step they
can take, after `next:` (in Chinese `下一步：`). For `hansa serve` finishing no round, and for a lane held because its
sandbox cannot start, it says what the trouble means and to tell the coordinator, which can find the cause with
`hansa explain server`; the sandbox alert adds that the held runs start by themselves once a try of the sandbox
passes, after a fix, as `hansa serve` starts again or the settings change. A command the principal runs names
where: `hansa up` in a terminal on the desk's machine. In Chinese alerts hansa's own words are Chinese, while a task's
title and a program's own message stay as they are, quoted: `其工作智能体的沙箱无法启动：「srt: …」`. A release stopped
with `hansa release --stop` names who stopped it: the coordinator (from a Claude Code session, or with
`HANSA_ROLE=coordinator`) or a command in a terminal; a stop recorded by an older hansa says a hansa command.

The command runs as the desk's account, outside the
sandbox, in a temporary folder of its own under the desk's `secrets/alert-runs` (closed to the
coordinator, workers and verifiers; removed when the command ends, and what a stopped or killed
`hansa serve` left there is swept when it starts), with hansa's own environment (not a lane's),
the kind of alert in `HANSA_ALERT_KIND` (`release_waiting`, `release_ended`,
`release_stopped`, `ask`, `server_stuck`, `sandbox`, `disk`, `coordinator`, `credit`, `test`; comma-separated when several go
out together)
and the level in `HANSA_ALERT_LEVEL` (the highest in a batch; see
[alert settings](desk.md#settings-on-the-web-page)), and the text on standard input: UTF-8, in the saved
`alert_language` (`en` by default, or `zh`; set only by the principal beside the command), keys removed,
within 1,000 characters. A long batch shortens the explanation before each alert's final next step,
keeping the step whole and marking omitted text with `…`, only if at least 20 characters of the
alert's start also fit. Otherwise the alert keeps the existing cut from the end. Code-bearing alerts also keep that
cut, so codes stay in their original position. Urgent alerts come first, and the header counts the whole batch.
Short alerts use only the room they need; longer alerts share what remains. When that share would
fall below 60 characters of alert text (excluding `- ` and the newline), the batch shows as many
alerts as fit at that minimum, or their full length if shorter, in the same order. Its last line counts
the rest and names `hansa status`, in the saved alert language. The hourly cap and its held-back
count are unchanged. A release's text names its lane and target;
its description, as kept when asked, follows the request, code and instructions, or the result. Alerts use the
[desk name saved by the principal](desk.md#settings-on-the-web-page), as the
page title does: `[hansa] desk NAME: ...`. With no name set, they use the last
two folders of its path: `shop/desk` for `/work/shop/desk`, or `desk` for `/desk`.
Spaces and non-ASCII letters stay as they are. Desks with the same saved name,
or the same folders when unnamed, have the same alert name.
A new to-do reads `to-do #N for you: <text>`; the principal
answers in the conversation. When the gathered alerts go out, to-dos already
closed are left out. If nothing remains, neither channel sends. Closing a to-do
after its alert went out sends nothing more.

The page prefills an unsaved `alert_language` from its own language; alerts stay
English until the principal saves that choice. Settings history records changes.
Other values are refused on save; an invalid file value reads as `en`.
A saved choice also applies to alerts still gathering. Each queued alert has a
level, and `HANSA_ALERT_LEVEL` takes the highest in a batch:

| Level | Alerts |
| --- | --- |
| `passive` | A release that finished successfully; recovery after `disk`, `sandbox`, `server_stuck` or coordinator turn errors |
| `active` | A waiting release, a to-do, a failed, void, forbidden or stopped release, and the test |
| `urgent` | Problem notices: `server_stuck`, `coordinator`, `sandbox`, `disk`, `credit` |

Bark maps these to passive, active and time-sensitive notifications, respectively.
ntfy uses priorities 2, 3 and 4; Gotify uses 2, 5 and 8. Telegram and Discord
silence passive batches and otherwise use normal notifications. For them and
services without levels (PushDeer, Server酱, WeCom, DingTalk, Feishu/Lark and
Slack), urgent batches start with `Urgent: ` or `紧急：` in the principal's
alert language. These markers do not change the phone's settings. The JSON
webhook sends the level as a field.

For a release refused by the word scan, the alert gives only counts of hits
and lines that could not be checked against patterns; the entries, lines and
reasons stay in the page's release list and `hansa release`, so refused
content does not go out in the alert. A phone shows the first hundred
characters or so, so the alert for a release that waits with a
code gives the code, and what to do with it, right after the desk's name:

    [hansa] desk NAME: release #1 (lane a, target staging) · code K7P3QX: to release it, give this code to the coordinator in the conversation (this release only, once, 24 hours; send it nowhere else), or answer on the page · main at 08e2cfc99f, 8 commit(s), command: ...; note: ...

When several alerts go out in one run, the first line also gives each
waiting release's number and code (`[hansa] desk NAME: 3 alerts · #1 code
K7P3QX · #2 code M4R8TW`). Urgent alerts come first, then other lines holding a code,
each starting as a single one does. A release that waits with no code reads `release #1 of lane a, target staging
(main at 08e2cfc99f) waits for your yes on the page: 8 commit(s), command:
...`. The header keeps codes early within the 1,000-character limit. A code went out when at least one channel whose final
text held it sent successfully. If no channel's final text held it, including after Bark's byte limit,
the code is void and the page answers. `hansa status`'s line under the waiting releases follows them, not
the set-up: when none has a live code (none was made, making it failed, or it was voided), it says the
principal answers on the page. It has 30 seconds; its own exit ends the run, even if it leaves a
child running, and only at the limit is its process group killed. Alerts
within 20 seconds of each other go out in one run to every channel that is set up,
one batch at a time, and at most 20 runs an hour: beyond that, alerts are held and go out together in one
run when the hour frees a run, with a line saying how many were held back
(the page's test button is not held and is excluded from that count).
`hansa status`, and the page's status line as a warning, show how many alerts are currently held and when the hourly limit
frees a run, leaving out answered requests. A run that fails or times out is recorded
(event `alert_failed` with `channel: "command"`) and `hansa status` shows the last failure with its exit
code and last lines (none for an alert that held a release's one-time code:
then only how it ended is kept). The status and page warning clears at the next successful command run.
A waiting release whose code-bearing alert failed says that the alert carrying its code reported a failure,
that the code may not have reached the principal, and to answer on the page. A later successful run
clears the general warning but does not change that release's delivery record.
The failed alert is never retried: the next alert tries again.
Each push service has a separate 30-second deadline including its transport's at most three
tries; the batch never retries it. `hansa status` shows each push entry's last result,
`sent` or the reason it could not send (`bad_key`: a Bark entry's encryption key is not valid; set it
again on the page). A failed push records `alert_failed` with
`channel: "push"`, the entry's `id`, `service` and reason, never its address or a code-bearing push's text,
even when it failed or was cut. Neither channel carries the other's failure.
The page has a button that sends a test alert through every channel that is set
up; its result lists every channel, and any failed channel makes the test fail.
No coordinator command sends one.
The Alerts summary in `hansa explain` says whether the alert command is set and when it was saved, and lists
each named push address (service, hidden address and on/off). The command's value is withheld from
this summary and from the settings history (`hansa explain history` and the page's History view);
the history shows `changed (values withheld)` instead of old and new values. The principal can view
and edit the command on the page, Settings, Alerts.

The `coordinator` alert says that what hansa posts to the coordinator's session (see [hearing from
hansa](desk.md#hearing-from-hansa)) cannot reach it. Its text names the desk, the session (its pid, and its name
when known), the case, and what waits: how many things and the oldest, or the task. It has four cases, each after
the minutes the principal sets on the page (Settings, Alerts: whole minutes from 0 to 1440, 0 turns that case off; no
`hansa` command sets them):

- **No session listens** (10 minutes by default): something hansa would post to the coordinator has waited that long
  in the event log, and no session hears from hansa (its session ended, or notifications were turned off).
  `hansa serve` sends it once, and again only after a follower has run since, live when it looks or started and
  ended between two of its looks, and the case begins anew.
  Only what still waits for the coordinator counts, said as it reads now: a task while it waits for review or has
  failed (not once it is merged, closed or cancelled, nor while a verification of it runs), and the server's stop
  only while `hansa serve` is not running, so a server that starts again after a stop does not alert of its own stop.
  `hansa status` counts the same way. Its next step: when the session ended, or none has heard yet, to run `hansa
  up` in a terminal on the desk's machine, which starts the session again; when the session runs but does not hear, to
  ask the coordinator in that session to run `hansa notify on`. In Chinese it says the oldest thing in Chinese: a task
  by its ID, its title (quoted) and its state.
  A session that starts hearing still first gets what was missed, as before.
  The same minutes also cover a running session whose inbox refuses posts: its follower alerts once, saying that
  the inbox refuses posts, and keeps the lines to try again; its next step is, if it lasts, to quit that session and
  run `hansa up` in a terminal on the desk's machine. A successful post ends that stretch; a later stretch
  counts from its own first refusal. A failed startup summary also waits for a successful post before it counts
  as caught up. The refusal's wait and whether it was alerted survive a follower's restart on new code. Only
  refusals while lines wait count; a failed post an older hansa recorded starts none. While its follower tries
  again, the session still counts as heard, and `hansa status` keeps the line.
- **The session waits on its window** (10 minutes): `claude agents --json` lists the session as `waiting` (a
  permission dialog or a question is open) that long, with something posted to it that it has not finished. The wait
  counts from the first lookup that saw it `waiting`, or from the post when the dialog was already open. A session that
  takes a post (`busy`) and then stops on a dialog counts too, however long it was busy first; once a lookup sees it
  otherwise than `busy` or `waiting` (`idle`, say), what was posted before counts as finished. Once per stretch of
  waiting. Only `waiting` alerts: `busy`, `idle`, `shell`, a status hansa does not know, or no answer sends nothing,
  and a lookup that fails is written in the follower's log.
- **Posted work sits untouched** (120 minutes): a task hansa posted to the session has waited that long for review
  (done, not merged or closed), and the session is `idle` at a lookup after that. Once per task across the desk,
  however many followers watch it (a task that records no session goes to each); its next round may alert again.
  hansa does not post it again; the alert's next step is to ask the coordinator in that session to review it. A task
  does not wait for review while a verification of it is queued or running,
  whoever asked for it; its verdict's post starts the wait again. A follower started afresh (the session quit and
  opened again, a reboot, `hansa up`) takes the tasks that already wait for review and would be posted to its
  session, as waiting since they ended (or their verification did): the alert says `since it ended`.
- **Its turns fail** (30 minutes, `turn_min`): API errors keep ending the session's turns (case `turn`).
  Errors only the principal can fix alert at once. After an alert, a normal turn
  sends one quiet recovery notice (case `turn_ok`, passive). This also works with push addresses alone.
  Before that, hansa tries to get the session going again: when an API error ends a turn hansa started, the
  follower posts again what it posted to the session that a normal turn has not handled yet, headed `Your last
  turn ended with an API error (ERROR) at HH:MM`, once the session is seen idle after the failure. At most three
  reposts in a stretch of failures, one per failed turn, none once the principal was alerted, and none for a
  turn the principal started (that one alerts after the minutes). By the error:
  - `rate_limit`, `overloaded`, `server_error`, `unknown` (waiting may cure them): reposts 2, 10 and 30 minutes
    after the stretch began; the alert after the minutes, but not while the turn a repost started may still end
    well: it waits until that turn ends with an error too, or until no turn has started 3 minutes after the repost
    (the session is not seen busy or waiting). For `rate_limit` it adds that a usage limit may only reset hours later.
  - `billing_error`, `oauth_org_not_allowed`, `account_on_hold`, `verification_required`, `model_not_found`,
    `cloud_credential_error`, `invalid_request` (only the principal can fix them): the alert at once, and no
    repost until a turn ends normally. For `invalid_request` it adds that `/compact` or `/clear` in that session
    may cure it.
  - `authentication_failed` (perhaps a short login refresh): one repost after 2 minutes; the alert if the turn
    after it fails too, or after 2 minutes when nothing waits. It says to log in again in that session.
  - `max_output_tokens`: one repost at once, ending `go on`; the alert if the next failure is that too.

  The alert ends with advice for the error, for each of them: for those only the principal can fix, and
  `authentication_failed`, what to fix and where (`billing_error`: add credit or fix billing on the account's page
  with its provider; `model_not_found`: choose another model with `/model` in that session; and so on); for the
  others, that hansa tries again with its next post to the session.

  The alert names the session, the error, since when and, when any do, how many things posted to it wait unhandled. When only the
  principal's own request failed in the stretch (no turn hansa started did), it says that instead, with no minutes:
  `the principal's own request ended with an API error (overloaded) at 16:00 and was not done · type it again in
  that session`. For an error only the principal can fix, and `authentication_failed`, typing again does nothing
  until the cause is fixed, so the advice for it comes first: `… · its login failed: log in again in that session
  (/login), then type it again in that session`. When things hansa posted meanwhile wait unhandled, it ends with how
  many: `· 2 thing(s) hansa posted meanwhile wait unhandled`. 0 turns the `turn` and `turn_ok` alerts off; the reposts still go. While a stretch holds, `hansa
  status` shows one line for the session, such as `the coordinator's session pid 4242 (desk-b4): its turns end with
  an API error (overloaded) since 14:02 (12 min) · hansa posted again what waits 2 time(s) · the principal is not
  alerted yet`, or the principal's own request in the same words as its alert; none once a turn ends normally.

The session's follower looks the last two up, at most once a minute counting its lookup of the session's name (one
run of `claude agents --json` serves both), and only while one could hold: something was
posted in the minutes of the second case or waits to be posted, or the session was last seen `busy` or `waiting` with
something posted it has not finished, or a task it posted has waited the minutes of the third, or was alerted of and
still waits for review (so that its status line shows only while the session is `idle`). It looks nothing up in an
idle round with nothing waiting, nor while no alert channel is set up, and what it has alerted stays alerted when it
restarts on new code. `hansa status` shows a line while a case holds, such as
`the coordinator's session pid 4242 (desk-b4) has waited on its window for 14 min`.
While an alert channel is set up, it also shows the minutes of the four cases with the other alert settings:
`Coordinator alerts after: no listening session 10 min · waiting for input 10 min · task waiting for review 120 min ·
turns failing 30 min (set on the page, Settings, Alerts)`, `off` for 0. A minutes box emptied on the page takes the
default; the save says so, and which (`Coordinator's turns failing (minutes): left empty, so the default, 30 minutes, applies.`).

### Push addresses and services

On the principal's page:

1. Open **Settings → Alerts → Push addresses → Add an address**.
2. Paste the whole address from the push app or service. Leave the service at
   **Recognize it**, or choose the service's name. Each service's hint on the
   page says what to paste. An extra secret field appears only for a service
   that takes one.
3. Give it a name if you like, then press **Save settings**. The entry now shows
   only the hidden address, with its key concealed.
4. Press **Send a test push** on that entry. Its last result shows sent and the
   time, or what went wrong. A test sends even when the entry is switched off.

For Bark on iPhone, install it from the App Store and follow the
[Bark guide](push-services.md#bark) to copy its address. The
[service guide](push-services.md) covers the other services and self-hosted servers.

To replace a saved address or extra secret, paste the new value into that
entry. Leave either field empty to keep its saved value. Switch an entry off,
rename it or press **Remove**, then **Save settings**. Until saved, address and
secret changes and removals show the ordinary unsaved-changes note. Provider
keys still use their own **Save key** buttons.

`alert_push` is a list of at most ten `{id, service, on, name?}` entries.
IDs are stable, unique lowercase letters/digits, hyphens or underscores (up to
64 characters). Addresses are stored atomically in
`secrets/alert-push-ID.address`; an optional extra secret goes in
`secrets/alert-push-ID.secret`, both mode 0600 under 0700 `secrets/`.
`Desk.keys` includes whole addresses and secret parts extracted only by each
entry's recorded service, when at least eight characters long, for redaction
and refusal of a task's change that contains them. Saving scrubs those values
from files covered by `Desk.scrub`: task files, requests and kept lane-try output.
This protection covers the keys hansa knows. The append-only event log is not
rewritten, so text recorded before a key was saved stays in earlier entries.
Only the recorded service checks,
masks and sends a saved entry. Recognition asks all services only before an
address is saved. A service switch cut short between the settings write and the
address write leaves the old service's address behind: when the recorded
service no longer accepts a saved address as its own saved form, `Desk.keys`
takes its parts from the service whose saved form it still is, if any. An
orphan secret file with no entry, or one whose metadata cannot be read, still
has its whole value protected, without guessing a service to extract parts.
An entry this hansa cannot read, written by hand or by a newer hansa with an
unknown service, is left out alone: the other entries still hide, send and
show, and the list's problem is still reported. Keep this separation while
services share address paths: an ordinary
word such as `message` must not become a secret through another service's parser.
Settings history records secret changes with values withheld.

On first access an old `alert_bark` setting and
`secrets/alert-bark.device-key` become the first Bark entry, preserving its
server, key and on/off choice. Its previous last result moves too. The old
setting, key file and result file are removed; settings history says that hansa
moved them. Without a key file, no entry is created: even an invalid old setting
is removed, with a settings-history entry and no migration problem. This is the
sole automatic address change; otherwise only the principal's save changes addresses.
If the key cannot be preserved exactly (including an old key containing `/`
or punctuation), the saved configuration is invalid, or ten entries leave no
room, hansa keeps the old setting and key file without sending to that key.
`hansa status` and the page's settings problems ask the principal to paste the
Bark address again on the page. Unrelated saves keep the old data; saving a
Bark address removes the old setting, key and result files, recording the setting's
removal in settings history as the principal's change. Keep these checks
while older desks can still have Bark settings: an upgrade must neither stop
desk commands nor silently change a saved key.

The page interface, for use on the page, Settings, Alerts:

- `GET /api/settings` returns metadata in `settings.alert_push`, and the public
  entries in top-level `alert_push`: `id`, `service`, `name`, `on`, `address`
  (hidden), `address_set`, `secret_set`, `last` (`{at, ok, why, code?}` or null).
  `alert_push_services` lists `{id, name: {en, zh}, hint: {en, zh}, extra_secret}`.
  Names are redacted in both lists if a saved key was pasted into one.
- `POST /api/settings` accepts `settings`, `rev` and optional top-level
  `alert_push_changes`, a list of edits. An add has `address`, optional
  `service` (`"recognize"` by default), `name` and `on` (true by default).
  An edit names an existing `id` and supplies `address` to replace it, `on`
  to switch it, `name` to rename it, or `remove: true` to delete it and its
  secrets and last result. `secret` replaces the optional extra secret;
  omission preserves it, and an empty value removes it. Changing service
  requires an address and clears the previous extra secret. The whole save
  is validated before writing, including the revision and the ten-entry cap.
  Settings are saved before private files, so a failed settings write leaves
  no unlisted address. An automatic upgrade instead removes its new address
  and result files if its settings write fails, retaining the old Bark data.
  A retired `settings.alert_bark` posted by an older page is ignored; other
  edits still save, and nothing in that retired field is applied.
  No reply includes submitted credentials.
- `POST /api/alert/push-test` with `{id}` sends exactly one saved entry a test
  batch now, even while off, without touching the queue. It returns
  `{ok, why, last}`. No coordinator command saves or tests an address.

The page builds its service choices and paste hints from `alert_push_services`.
Blank replacement address and extra secret fields omit those edits, preserving
the saved values. An add draft containing only a service choice and optional
name is ignored on save and does not mark the settings unsaved. Unrecognized
addresses prompt the principal to choose a service; address refusals and
upgrade problems appear in the page's language. The former Bark page fields and
`/api/alert/bark-test` endpoint have been removed; use the push interface above.

`hansa.alert_services.SERVICES` holds service definitions. A definition supplies
its id, English/Chinese names and paste hints, whether it takes an extra secret,
address recognition and normalization, secret-part extraction and masking,
byte limit, level mapping, deadline, and a builder of HTTPS requests for a
code-free batch. Adding a service means adding its definition to the registry
and its tests. Requests pass through `alert_http.send`, including response
checks in its isolated child. A definition may also supply
`validate(address, secret)` to check the completed pair before saving or sending:
ntfy requires a token for topics shorter than eight characters, and Gotify
requires an application token. Bark's optional `normalize_secret(address, value)`
extracts the encryption key from a copied example before secret validation;
`validate` checks the saved ASCII key length. Service definitions may supply
English/Chinese extra-secret labels and hints. Keep these checks while topics need protected
access and tokens can be pasted separately from addresses. Redaction and file
scrubbing deliberately ignore secret parts shorter than eight characters to
avoid changing ordinary task text; ntfy therefore requires a token for a short topic.

Bark recognizes `api.day.app` and ordinary bare device keys automatically;
`SCT…` and `sctp<uid>t…` keys belong only to Server酱. ntfy recognizes
`ntfy.sh`, PushDeer recognizes `api2.pushdeer.com`, and Server酱 recognizes its
two official address forms. Gotify is always chosen. Bare PushDeer keys require
choosing PushDeer, and self-hosted servers require choosing their service.
Choosing Bark for an address another service recognizes is refused, naming the
service to choose instead, only when Bark does not also accept the address as
its own: a Gotify `/message?token=…` or PushDeer `/message/push?pushkey=…`
address, a Server酱 `SCT…` or `sctp<uid>t…` key or `.send` address, an ntfy
address on `ntfy.sh` that is not exactly `https://ntfy.sh/TOPIC`, or a Telegram
`sendMessage`, WeCom, DingTalk, Feishu/Lark, Discord or Slack official endpoint
address. A plain `https://HOST/KEY` address with a lowercase host, one key path
and no query or fragment is Bark's own form and is accepted, as are a saved Bark
address, any address on `api.day.app`, a bare device key, and a self-hosted Bark
server's address. That includes `https://ntfy.sh/TOPIC`,
`https://api2.pushdeer.com/KEY` and `https://sctapi.ftqq.com/KEY`, which another
service also recognizes but which Bark accepts as its own form.
See [the service guide](push-services.md) for request contents and limits.
WeCom, DingTalk (including `oapi.dingtalk.io`), Feishu/Lark, Discord and Slack
recognize their official hosts and endpoint paths; Telegram recognizes its
official host and validates the sendMessage address. The JSON webhook accepts
any HTTPS address only when chosen explicitly.

For Bark, a different
host requires the explicit `bark` service. Its HTTPS address must have a key as
the first path component; later example text, query parameters and fragments
are discarded. Pasted text or a copied command with exactly one HTTPS address
supplies that address; surrounding spaces and line breaks, and tabs, CR and LF
in the paste, are tolerated. Multiple HTTPS addresses, HTTP, userinfo,
backslashes, spaces in the address, and dot path components (including encoded
ones) are refused before saving or sending. Other control characters anywhere
in the paste are refused. Keep these checks while principals can paste
addresses: URL parsers can silently discard controls or redirect credentials
to a different host.

Bark sends one JSON POST to the origin's `/push` with `device_key`. The title
and group are the desk name and the body is alert text without the command's
header or codes. Passive maps to `passive`, active to `active`, urgent to
`timeSensitive`; `critical` is never sent. The command formatter's character
cap applies, then the entire UTF-8 JSON is limited to 3,500 bytes including
escapes and the key. Only the body may be shortened, at a character boundary
with `…`. Fixed fields that cannot fit give `too_big` without a request.
Keep this bound while Bark and Apple's push service add their own envelope.
With a saved encryption key, Bark sends only `device_key`, `ciphertext` and
`iv` outside the ciphertext; all notification fields, including `level`, go
inside. Standard-library Python implements AES-128/192/256-CBC with PKCS7
padding and standard padded base64, without running a program with the key.
The 3,500-byte bound applies to the final encrypted JSON request, including
ciphertext and IV; only the inner body may be shortened. This hides the desk
name and text from Bark's server and Apple. Without a key, the plain request
is unchanged. A waiting release's one-time code goes inside the ciphertext only; a Bark entry without a key, like
every other push entry, gets code-free wording asking for an answer on the page.

Each entry has `.hansa/alert-push-ID-last.json` with `{at, ok, why}` and an
optional safe service `code` on failure.
Each attempt records `alert_channel` with `channel: "push"`, `id`, `service`,
`ok`, `why`; failure also records `alert_failed` with the id, service and
reason. No address, request or exception text is logged. `hansa status` shows
one line per entry with service, hidden address, on/off and last result.

### HTTPS transport

`hansa.alert_http.send(requests: list[dict], deadline_s: float = 30) -> list[dict]`
is the transport for built-in push channels.

Each request supplies `url`, exactly one of a `json` object or a `form` mapping
of text fields, and an optional `headers` mapping. The transport POSTs UTF-8
JSON with `ensure_ascii=False` and `Content-Type: application/json; charset=utf-8`,
or UTF-8 form fields with `application/x-www-form-urlencoded; charset=utf-8`,
overriding any supplied content type. The encoded body has the same 64 KiB limit
and URL/header checks for either format.

An optional `success` mapping describes the acknowledgement: `{field: "code",
equals: 0}` checks a top-level JSON field, `{field: "ok", equals: true}` checks
the JSON boolean, and `{text: "success"}` checks the exact whole response body.
A numeric expectation also accepts a numeric string such as `"0"`; booleans
do not count as numbers. An optional `status: 200` within that mapping restricts
success to HTTP 200. Without `success`, the existing HTTP 2xx rule applies.
An optional `error_field` names the JSON field holding the service's error code;
it defaults to the success field. For a whole-body text check, a failed reply
can instead supply its entire bounded ASCII word as the code (Slack).
This also extracts a code on non-2xx replies
without changing their HTTP category. A 2xx reply that fails its check, including
invalid JSON when JSON was expected, is `refused_by_service` and is never retried.
Only the child inspects response bodies, up to the existing 16 KiB read bound.
An unreadable response or one filling that entire bound cannot satisfy a body
check, since hansa cannot know it received the whole answer; it yields no code.

Results keep request order and contain only `ok`, `status` (an HTTP integer
or `None`), `tries` (0–3), `why`, and an optional `code` on failure. A code is a
finite number or an ASCII word of at most 40 letters, digits, underscores or
hyphens. Both child and parent enforce this bound; no other body content crosses.
The fixed categories are `sent`, `refused_url`,
`redirect`, `timeout`, `network`, `tls`, `http_429`, `http_4xx`, `http_5xx`,
`too_big`, `deadline` and `refused_by_service`. No result or log includes a URL, header value, request
or response body, or exception text.

One standalone child runs this module with `sys.executable -I`, avoiding caller
import hooks and an import of the rest of hansa. Requests arrive on stdin, never
on the command line. Each request has its own thread; completed results and
attempt counts stream back immediately. The parent bounds stdin transfer and
waiting by the shared deadline, then sends SIGKILL to the child's new process
group and reaps it. Completed results survive; unfinished requests return
`deadline`. Child stderr is discarded to keep exception details private.

Only HTTPS URLs without userinfo, fragments or control characters are accepted;
header names and values containing CR or LF are refused before a connection.
Encoded JSON or form bodies larger than 64 KiB are also refused before sending (`too_big`, zero
tries). Requests that cannot be serialized as UTF-8 JSON, including byte values,
circular objects and lone surrogates, return `refused_url` with zero tries;
other requests in the batch still run. These checks prevent request splitting
and accidental disclosure to a different destination; keep them when adding
channels. TLS always uses Python's
verified default context. urllib keeps its default proxy handlers, including
environment and macOS system settings and `no_proxy`; there is no independent
DNS lookup or address-range restriction, so proxy setups using 198.18.x.x work.

A try has a socket timeout of at most five seconds, shortened by the remaining
deadline. There are at most three tries with backoff of one then three seconds.
Connection failures and timeouts retry, as do 429 and 500/502/503/504. A 429 uses
`Retry-After` in integer seconds when the wait fits before the deadline, otherwise
normal backoff. Redirects are never followed or retried. TLS failures and other
4xx/5xx stop immediately; 413 is `too_big`. At most 16 KiB of each response is
read, checked when requested, then discarded, including error responses; a read
failure after the status arrives does not resend it. Each try builds a fresh urllib request
so proxy-handler mutations cannot change later tries from HTTPS to HTTP.

`AlertHTTPTest` in `tests/test_hansa.py` checks these rules with fake openers and
responses, plus real isolated children with sleeping fakes for deadline and
concurrency checks. Keep the tests while channels depend on this transport;
they prevent retry, proxy, deadline and secret-disclosure regressions without
using a network service.
`tests/test_push_services.py` adds phone and chat recognition, fixed DingTalk
and Feishu signing examples, limits, secret handling, silent delivery and urgent
markers, plus form and acknowledgement checks inside real isolated children
with fake openers. It runs in the default parallel check too.
