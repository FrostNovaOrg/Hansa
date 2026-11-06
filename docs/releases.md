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
release L [TARGET] [note]` (TARGET needed when there is more than one)
records the request (a new request for the same target replaces one still
waiting): the target, the commit, the command, the note, the
commits that go out (on no remote-tracking branch of the repository and not
in that target's last release; the whole history when it has no
remote-tracking branch) and the files they change, a file the command names
(such as `./deploy.sh`) marked. A word from the principal's list of words no
release may send out, in the command line or in a line, a file name, a
message or the author's or committer's name or email of any outgoing commit
(all are checked, and the page shows at most 200 of them and says how many
were checked; the word scan and its limits are under [Forbidden words before a
release](#forbidden-words-before-a-release)), refuses it; each hit names its commit and where it is (`author`, `committer`, a
file). A command that holds one of those words
is kept when it is set (`hansa lane target`), and hansa says the word and where at once, before
any release is asked. A command that holds one of the desk's keys is
refused when it is set, since a command is stored and shown as written: have
it read the key from where the tool keeps it (its own login or config). A target that must follow another is asked only for
a commit the other one already sent out, and looked at again before it
starts. Otherwise the page shows it, with its target, with Release and
Refuse; a target the principal ticks as released without asking (each one
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
with `hansa release yes ID CODE` (or `no ID CODE`). The code travels only
in the principal's alert: the queue holds the release ID, request time and text
without a code. Immediately before the alert command runs, hansa checks that the
same request still waits, makes one code per release in that run, and rebuilds
its text in memory. Re-asking after a void gets a new release ID; an old alert
cannot acquire a code for the new request. Alerts whose requests no longer wait
still go out, without a code. Waiting through the hourly cap or a server restart
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
alert for the release brings a new one. With no alert command there is no
code, even with Bark on: the page alone answers. The principal's alert command must not keep the alert's
text anywhere the coordinator can read it (a file, a log, a chat history):
the code in it approves a release. No lane may be named `yes` or `no`, as
those words begin the answer. hansa serve runs the
command only then, and only while the base branch and the command are still
what the principal saw (else the request is void and the coordinator asks
again): in the lane's repository at that commit, as the desk's account,
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
`hansa release` lists them; `hansa explain lane L`
shows each target's command and its last release.

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

hansa posts a line for that release to the coordinator's session: watch passed
(exit 0), failed (its exit code and last lines, keys removed), or timed out.
The page's release list and `hansa release` show the state and result; output
is kept in `.hansa/releases/<id>.watch.log`, with the same last-line and daily
trimming limits as release output. The page shows the watch command beside
the target's command before approval. Changing, adding or removing it voids
a request still waiting for approval and stops the target's tick counting,
as changing its release command does. Changing only `--watch-minutes` keeps
the tick.

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
release, all history counts. A release outside base history is noted; git's range still applies.
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
for an answer that never comes. So start the try there with `nohup`, its output
then its exit code going to a fresh file named after the commit (a rerun never
reads an earlier result), and poll for that code every 30 seconds or so with
short ssh calls (connect timeout, keepalive) until it appears or time runs out:

```sh
c=$HANSA_RELEASE_COMMIT f=/path/to/tries/$c.log
o="-o ConnectTimeout=20 -o ServerAliveInterval=10"
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

The request lists direct commits among its outgoing commits, with their count (at most 200 SHAs and
subjects): walk the base's first-parent line; a task's recorded `merge_commit` that hansa landed in
this repository and base, from any lane, and what it brought in are not direct. Every other commit
on that line, and what a direct merge brought in (even a task's branch merged by hand), is direct; a
later hansa merge does not hide an earlier direct commit. With no remote-tracking branch and no
readable last release, history older on that line than the oldest hansa merge in the outgoing range
is history from before hansa and is not direct. With no hansa merge in that range either, every
outgoing commit is listed as direct: hansa cannot tell what came before it. With a remote-tracking
branch, no such cutoff applies, even when the last release's commit is gone. Commits on
remote-tracking branches or in the target's last release are not looked at, nor are working files
or uncommitted changes; the ask and waiting line use recorded task landings to identify direct commits. Only the
principal saves `release_direct` on the page: `record` (default) lists these commits and keeps ticked
targets going out without asking; `hold` (also used for an unknown setting value) refuses a release
with them unless asked with `--direct "<why>"`, then records the reason and waits for the principal's
yes even for a ticked target. The refusal names the first ten commits and their count and records
nothing; `--direct` with no direct commits is refused. The page and `hansa release` show the recorded
commits and reason; waiting alerts give only their count, never subjects. Older requests show as before.

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
counting. Outside folders named as where the command runs are shown
without a fingerprint; files over 16 MB or unreadable are named as not
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

Alerts reach the principal away from the page through Bark on an iPhone, an
alert command, or both. To set up Bark, open **Settings → Alerts → Bark** on the
page. Copy the device key from the Bark app (the part of its example address
right after the server's address), paste it into **Device key**, switch **Bark on**,
keep the default `https://api.day.app` or enter your own HTTPS server, then
**Save settings** (the key is saved with them) and press **Send a test push**.
Left blank, the key field keeps the saved key; **Remove key** removes it and
switches Bark off at the next save. The page shows only whether a key is saved.
Only the principal can save the settings and key, which stays under `secrets/`.
The desk's name and alert texts leave this machine for the Bark server and
Apple's push service; both can read them. One-time release codes never go to
Bark. Answer its waiting releases on the page; only an alert command receives
codes, and Bark alone makes no code.

Each gathered batch goes to every channel that is set up. Bark needs its switch
on and a saved key; a command needs a nonblank command line. With both, the sends
run side by side with independent deadlines and results. A slow or failed
channel does not delay the other's send, change its result, or send it a failure
notice. With neither, nothing is queued.

The principal may also set one
alert command line on the page (Settings, Alerts), such as a script that
sends a phone message; the page records that it saved it, and when. Only the
page sets it: no `hansa` command sets, changes or clears it, and `hansa lane
set` has nothing like it. `hansa serve` runs it when a release starts waiting
for the principal's yes (also when a ticked target's tick no longer holds and
its release goes back to waiting), when a release ends (done, failed, void or
forbidden), when the coordinator stops a release, when a to-do for the principal is recorded (`hansa ask`), when a
lane's runs cannot start because their sandbox cannot (once for each lane and reason), when free space goes under the
floor and `hansa serve` starts nothing new (once each time: see [free space](desk.md#free-space)), when
its own rounds have stopped for 15 minutes, and when what hansa posts to the coordinator's session cannot reach it
(below); not for ordinary task events,
which go to the coordinator. It runs as the desk's account, outside the
sandbox, in a temporary folder of its own under the desk's `secrets/alert-runs` (closed to the
coordinator, workers and verifiers; removed when the command ends, and what a stopped or killed
`hansa serve` left there is swept when it starts), with hansa's own environment (not a lane's),
the kind of alert in `HANSA_ALERT_KIND` (`release_waiting`, `release_ended`,
`release_stopped`, `ask`, `server_stuck`, `sandbox`, `disk`, `coordinator`, `credit`, `test`; comma-separated when several go
out together)
and the level in `HANSA_ALERT_LEVEL` (the highest in a batch; see
[alert settings](desk.md#settings-on-the-web-page)), and the text on standard input: UTF-8, in the saved
`alert_language` (`en` by default, or `zh`; set only by the principal beside the command), keys removed, cut at 1,000
characters; a release's text names its lane and target, preceded by its description
as kept when the release was asked, if any. Alerts name the desk by
the last two folders of its path, as the page title does: `[hansa] desk shop/desk: ...`
for `/work/shop/desk`, or `[hansa] desk desk: ...` for `/desk`. Spaces and
non-ASCII letters stay as they are. Desks with the same last two folders have
the same alert name. A new to-do reads `to-do #N for you: <text>`; the principal
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
| `passive` | A release that finished successfully |
| `active` | A waiting release, a to-do, a failed, void, forbidden or stopped release, and the test |
| `urgent` | `server_stuck`, `coordinator`, `sandbox`, `disk`, `credit` |

Bark maps these to passive, active and time-sensitive notifications, respectively.

For a release refused by the word scan, the alert gives only counts of hits
and lines that could not be checked against patterns; the entries, lines and
reasons stay in the page's release list and `hansa release`, so refused
content does not go out in the alert. A phone shows the first hundred
characters or so, so the alert for a release that waits with a
code gives the code, and what to do with it, right after the desk's name:

    [hansa] desk NAME: release #1 (lane a, target staging) · code K7P3QX: to release it, give this code to the coordinator in the conversation (this release only, once, 24 hours; send it nowhere else), or answer on the page · main at 08e2cfc99f, 8 commit(s), command: ...; note: ...

When several alerts go out in one run, the first line also gives each
waiting release's number and code (`[hansa] desk NAME: 3 alerts · #1 code
K7P3QX · #2 code M4R8TW`), and the lines holding a code come first, each
starting as a single one does. A release that waits with no code (it had
already stopped waiting when its code was to be made) reads `release #1 of lane a, target staging
(main at 08e2cfc99f) waits for your yes on the page: 8 commit(s), command:
...`. The cut at 1,000 characters never takes a code. It has 30 seconds; its own exit ends the run, even if it leaves a
child running, and only at the limit is its process group killed. Alerts
within 20 seconds of each other go out in one run to every channel that is set up,
one batch at a time, and at most 20 runs an hour: beyond that, alerts are held and go out together in one
run when the hour frees a run, with a line saying how many were held back
(the page's test button is not held). A run that fails or times out is recorded
(event `alert_failed` with `channel: "command"`) and `hansa status` shows the last failure with its exit
code and last lines (none for an alert that held a release's one-time code:
then only how it ended is kept); the command is never retried: the next alert tries again.
Bark has a separate 30-second deadline including its transport's at most three
tries; the batch never retries it. `hansa status` also shows Bark's last result,
`sent` or the reason it could not send. A failed push records `alert_failed` with
`channel: "bark"` and its reason. Neither channel carries the other's failure.
The page has a button that sends a test alert through every channel that is set
up; no coordinator command sends one.
`hansa explain` says which channels are set up: the alert command (and what it is) and Bark (on, with a key or
not).

The `coordinator` alert says that what hansa posts to the coordinator's session (see [hearing from
hansa](desk.md#hearing-from-hansa)) cannot reach it. Its text names the desk, the session (its pid, and its name
when known), the case, and what waits: how many things and the oldest, or the task. It has four cases, each after
the minutes the principal sets on the page (Settings, Alerts: whole minutes from 0 to 1440, 0 turns that case off; no
`hansa` command sets them):

- **No session listens** (10 minutes by default): something hansa would post to the coordinator has waited that long
  in the event log, and no session hears from hansa (its session ended, or notifications were turned off).
  `hansa serve` sends it once, and again only after a follower has run since, live when it looks or started and
  ended between two of its looks, and the case begins anew.
  A session that starts hearing still first gets what was missed, as before.
  The same minutes also cover a running session whose inbox refuses posts: its follower alerts once, saying that
  the inbox refuses posts, and keeps the lines to try again. A successful post ends that stretch; a later stretch
  counts from its own first refusal. A failed startup summary also waits for a successful post before it counts
  as caught up. The refusal's wait and whether it was alerted survive a follower's restart on new code. Only
  refusals while lines wait count; a failed post an older hansa recorded starts none. While its follower tries
  again, the session still counts as heard, and `hansa status` keeps the line.
- **The session waits on its window** (10 minutes): `claude agents --json` lists the session as `waiting` (a
  permission dialog or a question is open) that long after something was posted to it. Once per stretch of waiting.
  Only `waiting` counts: `busy`, `idle`, `shell`, a status hansa does not know, or no answer sends nothing, and a
  lookup that fails is written in the follower's log.
- **Posted work sits untouched** (120 minutes): a task hansa posted to the session has waited that long for review
  (done, not merged or closed), and the session is `idle` at a lookup after that. Once per task across the desk,
  however many followers watch it (a task that records no session goes to each); its next round may alert again.
  hansa does not post it again.
- **Its turns fail** (30 minutes, `turn_min`): API errors keep ending the session's turns (case `turn`).
  Errors only the principal can fix alert at once. After an alert, a normal turn
  sends one quiet recovery notice (case `turn_ok`, passive). This also works with Bark alone.
  Before that, hansa tries to get the session going again: when an API error ends a turn hansa started, the
  follower posts again what it posted to the session that a normal turn has not handled yet, headed `Your last
  turn ended with an API error (ERROR) at HH:MM`, once the session is seen idle after the failure. At most three
  reposts in a stretch of failures, one per failed turn, none once the principal was alerted, and none for a
  turn the principal started (that one alerts after the minutes). By the error:
  - `rate_limit`, `overloaded`, `server_error`, `unknown` (waiting may cure them): reposts 2, 10 and 30 minutes
    after the stretch began; the alert after the minutes. For `rate_limit` it adds that a usage limit may only
    reset hours later.
  - `billing_error`, `oauth_org_not_allowed`, `account_on_hold`, `verification_required`, `model_not_found`,
    `cloud_credential_error`, `invalid_request` (only the principal can fix them): the alert at once, and no
    repost until a turn ends normally. For `invalid_request` it adds that `/compact` or `/clear` in that session
    may cure it.
  - `authentication_failed` (perhaps a short login refresh): one repost after 2 minutes; the alert if the turn
    after it fails too, or after 2 minutes when nothing waits. It says to log in again in that session.
  - `max_output_tokens`: one repost at once, ending `go on`; the alert if the next failure is that too.

  The alert names the session, the error, since when and how many things posted to it wait unhandled. 0 turns the
  `turn` and `turn_ok` alerts off; the reposts still go. While a stretch holds, `hansa status` shows one line for
  the session, such as `the coordinator's session pid 4242 (desk-b4): its turns end with an API error (overloaded)
  since 14:02 (12 min) · hansa posted again what waits 2 time(s) · the principal is not alerted yet`; none once a
  turn ends normally.

The session's follower looks the last two up, at most once a minute counting its lookup of the session's name (one
run of `claude agents --json` serves both), and only while one could hold: something was
posted in the minutes of the second case or waits to be posted, or a task it posted has waited the minutes of the
third. It looks nothing up in an idle round with nothing waiting, nor while no alert channel is set up, and what it has
alerted stays alerted when it restarts on new code. `hansa status` shows a line while a case holds, such as
`the coordinator's session pid 4242 (desk-b4) has waited on its window for 14 min`.

### Bark channel

`hansa.alert_bark` supplies a built-in channel for the principal's Bark iPhone
app. `hansa serve` delivers each gathered batch to it while Bark is on with a
key, beside the alert command, which alone still gets release codes. No
coordinator command sets or tests Bark.

#### On the page

Settings, Alerts has a Bark block below the alert command: a switch, the server
(blank means `https://api.day.app`), the device key in a password field, Send a
test push, and the last push's result and time. Its help, in English and Chinese,
says to install Bark from the App Store, copy the key from the example address
Bark shows (the part right after the server's address), paste it, switch Bark
on, save and test; that the public server can read what is pushed through it;
and that release codes never go to Bark, since releases are answered on the page.

- The page shows only whether a key is saved (`alert_bark_key_set` in
  `GET /api/settings`, with `alert_bark_last`), never the key.
- The key typed there goes with the ordinary settings save, `POST /api/settings`,
  beside the settings as `alert_bark_key`; Remove key sends
  `alert_bark_key_remove: true` and switches Bark off. Left blank, the saved key
  is kept. The save is judged whole first (a stale page, any settings problem,
  a key that does not look like one) and only then writes the settings and gives
  the key to `save_key`/`remove_key`, so a refused save changes neither. No reply
  holds the key.
- Switching Bark on while no key is saved, and none comes with the save, is
  refused with a plain message; nothing changes. The page refuses it before
  posting, in its own language. A switch already on is not refused by an
  unrelated save, even if the key file went missing.
- Test (`POST /api/alert/bark-test`) calls `alert_bark.test` once with what is
  saved and replies `{ok, why, last}`; the page puts `why` in plain words.

Desk settings hold `alert_bark: {"on": false, "server": "https://api.day.app"}`.
Only the principal (`Desk.save_settings(..., by="page")`) changes these. An older
page that omits the setting preserves it; an omitted server uses the default.
A private Bark server may replace the
default; it must be HTTPS, without userinfo, query, fragment or a path except
an optional `/`. Invalid saves write nothing. This prevents a pasted device URL
or credential from becoming a stored destination; retain this rule while the
principal can set a server address. Sending adds `/push`.

The channel functions are:

- `save_key(desk, value, by="page")`: create or replace the device key; an empty
  value removes it. `remove_key(desk, by="page")` removes it explicitly. Other
  callers are refused before writing. Both record only that the key changed in
  settings history. They expose no key reader.
- `has_key(desk)`: key presence without returning its value. The key is in
  `secrets/alert-bark.device-key`, atomically replaced with mode 0600 under the
  0700 `secrets/` folder. Its distinct filename cannot collide with a provider's
  key. It is included in `Desk.redact()` and the shared secret checks.
- `send(desk, items, held=0)`: send one gathered queue batch, before the command
  adds codes. It uses the saved alert language and queued code-free wording;
  releases ask for an answer on the page. Even a batch already given codes uses
  its queued translations, falling back to a code-free page instruction when
  those are absent. This safeguard must remain while command batches can hold
  release credentials.
- `test(desk)`: send a test in the saved alert language using the saved key and
  server, even while the channel is off; leave the alert queue alone.
- `last_result(desk)`: the last attempt's `{at, ok, why}`, or `None` before any
  attempt, stored in `.hansa/alert-bark-last.json`.

One batch makes one JSON POST: the title and group are the desk name from the
alert header, and the body is the alert text with that header removed. The highest
level maps passive to `passive`, active to `active`, urgent to `timeSensitive`;
`critical` is never sent. The existing alert formatter's character cap applies,
then a byte limit counts the entire JSON encoding, including the key and JSON
escapes. If necessary the body is cut at a character boundary and ends in `…`,
so the full UTF-8 JSON stays at most 3,500 bytes. Fixed fields are never cut; if
they cannot fit, the result is `too_big` without a request. Retain this bound
while Bark and Apple's push service add their own envelope.

Send and test use `alert_http.send(..., deadline_s=30)` and return only a category.
Each attempt records `alert_channel` with `channel: "bark"`, `ok` and `why`, and
updates the last result. Failure also records `alert_failed` with that channel
and reason. Local outcomes are `off`, `no_key`, `empty`,
`refused_url` and `too_big`; otherwise the category is the transport's result.
`hansa status` and its JSON/page API expose `alert_bark: {on, key_set, last}`;
the command shows key presence and the last category, without the key or server.
Keys never enter settings, history values, status, events or page replies.

A Bark key addresses one device. This channel does not implement Bark encryption:
the configured Bark server and Apple's push service can read the push text.

### HTTPS transport

`hansa.alert_http.send(requests: list[dict], deadline_s: float = 30) -> list[dict]`
is the transport for built-in push channels, currently Bark. Bark delivery uses it automatically; ntfy is not implemented.

Each request supplies `url`, a `json` object and a `headers` mapping. The transport
POSTs UTF-8 JSON with `ensure_ascii=False` and
`Content-Type: application/json; charset=utf-8`, overriding any supplied content
type. Results keep request order and contain only `ok`, `status` (an HTTP integer
or `None`), `tries` (0–3) and `why`. The fixed categories are `sent`, `refused_url`,
`redirect`, `timeout`, `network`, `tls`, `http_429`, `http_4xx`, `http_5xx`,
`too_big` and `deadline`. No result or log includes a URL, header value, request
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
JSON bodies larger than 64 KiB are also refused before sending (`too_big`, zero
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
read and discarded, including error responses; a failure discarding an already
acknowledged response does not resend it. Each try builds a fresh urllib request
so proxy-handler mutations cannot change later tries from HTTPS to HTTP.

`AlertHTTPTest` in `tests/test_hansa.py` checks these rules with fake openers and
responses, plus real isolated children with sleeping fakes for deadline and
concurrency checks. Keep the tests while channels depend on this transport;
they prevent retry, proxy, deadline and secret-disclosure regressions without
using a network service.
