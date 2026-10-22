# Workers and the sandbox

What workers and verifiers may touch, how hansa keeps git's folders and probes the
sandbox before a run, what a lane's write list, env, network list and allow list may hold, which paths
hansa refuses, and what hansa stops when a run ends. The [README](../README.md#what-keeps-it-safe) has the
short version.

## What workers and verifiers may touch

Workers (task and verification workers) change files in their worktree and
report, inside Claude Code's sandbox, which the operating system enforces: any
command, but files changed only in the worktree (and its temporary folder, made
for each run and removed when it ends, and the lane's write list, `hansa lane set L write ...`), no
reading of the desk's keys and state, the coordinator's notes (`notes/`) and
card (the desk's `CLAUDE.md`), of `.claude`, `CLAUDE.md` and `CLAUDE.local.md`
in the folder `hansa notify on` recorded for the coordinator's session (built
in; none when that folder is the desk, a lane's repository or in one, nor for
an entry the principal's list would refuse; gone for runs that start after
`hansa notify off` there or another folder is recorded), of any provider's Claude Code folder or
`~/.claude`, of the logins, keys, tokens, browsers' profiles, messages and
shell histories hansa knows under the home folder (`HOME_SECRETS` and, for
macOS, `MAC_SECRETS` in `hansa/desk.py`: `~/.ssh`, `~/.aws`, Teleport, other
AI tools and Anthropic's own tool settings in `~/.config/anthropic`, cloud
tools, git and registry credentials, proxy clients' settings with their
server passwords or subscription links, password stores and keyrings, the
keychains, cookies, mail and messages under `~/Library`; Maven's master
password, `~/.m2/settings-security.xml`), or of
the folders and files the principal lists on the page (Settings, "workers may
not read": this machine's own folders of keys, and `~/.m2/settings.xml` or
`~/.gradle/gradle.properties` when they hold deploy credentials the project's
builds do not need: hansa leaves them readable, as they also hold the mirrors
and proxies a build may need; the coordinator may add to the list with `hansa
hide`, and only the principal removes an entry; Read is denied there to the
session `hansa coordinator` starts, not to one `hansa notify on` recorded; who else may not read them is
under [The principal](who-changes-what.md#the-principal). An entry that would stop
every worker is refused on saving, each with its reason: the home folder,
the desk, a lane's repository, a folder on the `PATH`, the system's programs
and libraries (`/usr/lib`, `/etc` and the like), hansa's own folder and its
output relay's file, Homebrew's `Cellar`, `opt` and `Caskroom`, `~/.config`,
`~/.local`, `~/.cache` (and `~/Library` on macOS), the temporary folder, the
desk's worktrees folder or a lane's worktree, or a folder above any of them,
and `/dev` or anything in it; a file or folder inside one of the others,
such as one file in `~/.config` or one formula's folder in `Cellar`, may be
listed. A name with one of `()[]{}*?\`, in the entry or where its link
leads, cannot be written as a rule and is refused too. An entry on
"workers may not read" that hides the Python hansa runs with, or hansa's
folder, makes runs fail at the start check, and the message says which.
A list saved before, or a link changed since, that holds such an entry,
or any other the page would refuse now, stops every run: hansa starts no
worker, verifier or coordinator, fails each task it would
have started with the entry and the reason, and says it in
`hansa status` and at the top of the page, until the principal changes the
entry on the page (Settings). Only an entry that has come to close a lane's
repository, a worktree or a folder on the `PATH` is left out of runs
instead, as a contradiction the coordinator and the principal settle; while
it lasts, `hansa status` and the page say each time which entry it is and that
the folder is open to that lane's runs and every other), no network but the
lane's network list (`hansa lane set L network ...`), and no git command that
changes history. They do not commit: hansa commits each task's changes on its
own branch, and merges only what the coordinator merges. The lane's setup,
before the worker, and its check, after it, run in the same sandbox (with
`srt`): the lane's network and write lists hold for them too, so a setup that
downloads needs its registry on the network list.

Of the desk itself a run reads only its own worktree (and a lane repository's git
folder inside the desk; on Linux, a lane command's own TMPDIR in `.hansa/tmp`):
not `settings.json`, `tasks/`, `lanes/`, `events.jsonl`, `asks.json` or other
lanes' worktrees. Of the runs' temporary folders (`hansa-*` in `/tmp` and the
system's temporary folder) it reads only its own. On Linux srt expands that
pattern when it wraps a command, so a folder made afterwards stays readable to it.
Claude Code's file tools get the same as Read rules, but a temporary folder made
after the run started is open to them.

A write-list entry that hansa would now refuse when a run starts (one saved before that refusal
was there, or one whose link has since been pointed elsewhere) is left out of that run, and `hansa show`
says so ([A lane's write list and env](#a-lanes-write-list-and-env)).

Workers see their checkout's own CLAUDE.md and settings, and hansa's instructions: not the card,
and not the settings, plugins, hooks or MCP servers of the account they run as. Nor may they, a
verifier read the card or `notes/`.

## Git's folders

What each run may write in git's folders, and what hansa checks before it runs git in a worktree itself.

hansa never commits a worker's change to the repository's top-level `.gitconfig`, `.gitmodules`,
`.bashrc`, `.bash_profile`, `.zshrc`, `.zprofile`, `.profile`, `.ripgreprc`, `.mcp.json`, or to `.vscode`,
`.idea` and `.claude` and anything under them (whole path segments, without regard to case). The sandbox
protects these paths on disk: an index change would bypass it and could make commands run when someone
later opens the project. Such a change fails a worker run's end, and stops
`hansa lane remove --keep`; nothing is committed and the work stays staged. Empty sandbox placeholders
absent from the branch's tip are left out first; content and mode matching the base merged by
`retry --update` may be committed. To change a protected path, the principal does it outside a task.
This check must stay while workers can write git's index and these paths can control commands.

### What each run may write there

Claude Code opens a worktree's whole shared git folder for writing, and on
Linux the sandbox (bubblewrap) keeps only paths that are there when a command
starts, takes no name patterns and cannot open a branch's folder beneath a
`refs/heads` it keeps, so a session that commits could make or move other
branches, the base among them. The coordinator runs outside the sandbox.
What each may write in git's folders: a worker (and a verifier) only its own
worktree's folder under `.git/worktrees/` (its `index`, say), not its `HEAD`,
logs, `commondir`, `gitdir` or `config.worktree`, nor what says a merge,
cherry-pick, revert or rebase is under way there (`MERGE_HEAD`, `MERGE_MODE`,
`CHERRY_PICK_HEAD`, `REVERT_HEAD`, `rebase-merge/`, `rebase-apply/`,
`sequencer/`): workers never merge, hansa does. The lane's setup and
check may write nothing of git's folders.
On Linux a worker's git denials hold for the names there when it starts (the
whole `refs/`, `logs/`, `objects/`, `hooks/` and `info/`, git's settings,
`packed-refs`, the worktree's `HEAD`, `commondir`, `gitdir` and
`config.worktree`, other worktrees' folders), not for the patterns that keep
what is made later in the shared folder and under `.git/worktrees/`, nor for
a `MERGE_HEAD` or the like that is not there when it starts (the sandbox would
put an empty file there, which git takes for a merge under way). What it
could make there reaches nothing outside the sandbox: git obeys a new
`commondir` or a worktree's folder only through the checks below, and hansa
commits nothing while a merge or the like it did not start is under way.

### Before hansa runs git in a worktree

Before hansa runs git in a worktree, it checks the worktree's `.git` file;
that the git folder it leads to still points back to the repository
(`commondir`) and to the worktree (`gitdir`) as git wrote them; that its
`config.worktree` is empty, as hansa makes it, and the shared folder's is as
hansa first saw it or empty; and that `extensions.worktreeConfig`, which makes
git obey those files, is not on where hansa first saw it off. When one was
changed, hansa runs no git there and keeps what it found; a `.git` file, a
`commondir`, `gitdir` or `config.worktree` it puts back as it was. Before it
commits, stashes or merges in a worktree, it also checks that the worktree's
`HEAD` names the branch it made for that run, that this branch's ref is a
plain commit id (or packed and not loose), never a link to another ref, and,
in a worker's or a verifier's worktree, that no merge, cherry-pick, revert or
rebase is under way but the merge of the base hansa itself made for `hansa
retry --update`; it puts back what it found or sets it aside, and the task
fails either way. hansa's commit at the end of a run is made from the index
with the branch's tip (and, after that merge, the base's tip it merged) for
parents, never from a `MERGE_HEAD` git would find there, and moves exactly
the run's branch from the old id to the new one (`git update-ref` with the
old id, without following a link). The old id is what the branch's ref file
(or `packed-refs`) holds, read as a plain file, and what the run left running
is stopped before hansa commits (and before the lane's check), so what hansa
finds can no longer change the ref; a leftover it cannot find (one that
cleared its environment, or on macOS a system program such as `/bin/sh`)
still could. So if the ref changed all the same (a link made, another id), or
the branch does not end on the new commit with the old id as its parent, the
task fails, the ref is put back to the old id and the work is left staged.
hansa records the commit it made (or the tip it read) as the task's work, by
its id, and never reads the branch again through a link: it reads the ref
once more as a plain file when it has recorded it, and if it then holds
anything else the task fails ("changed after hansa committed"), the ref is put
back to the recorded commit and the work is kept. From then on `hansa
verify`, `hansa merge` (and its merge check), `hansa retry` and `hansa show`
use exactly that commit, never what the branch's name leads to: while the
branch does not hold it, merge, verify and retry refuse with a line saying
what it holds instead, and `hansa show` says so. In the lane's own checkout, a `commondir` in its `.git`
stops every hansa git command there until it is removed by hand; hansa does
not remove it. hansa's own git in a worktree runs with `core.fsmonitor` off,
hooks from an empty folder and commit and tag signing off, whatever a
setting there says; in the lane's own checkout its hooks run as before.
That empty folder, `hansa-no-hooks-*` in the temporary folder, is made once
by each hansa process that needs it and removed as the process exits. One a
process could not remove (it became another program, as `hansa coordinator`
becomes Claude Code, or was killed), and an empty `hansa-<uid>`, where hansa
makes the inbox of each session it posts to (a session's follower removes its
inbox, and the folder once no other inbox is in it, when the session ends),
`hansa serve` removes as it starts and each hour,
from the temporary folder and `/tmp`: only empty ones, the account's own,
left alone for an hour.

## The sandbox probe before a run

Before it starts a worker or a verifier, hansa serve probes
that run's sandbox: `srt` with the same filesystem and network settings
Claude Code is given, on `/usr/bin/true`, then a write of what git needs
open there: the worktree's own `index` (opened to append, nothing written)
and a new file in its own git folder; and a write of what must be
kept, to see that the deny patterns match at all: a new file at the top of
the shared git folder (`.hansa-try-kept`), a name next to the worktree's
own folder under `worktrees/` (what such a write makes is
removed again). On Linux, where srt drops every deny pattern and keeps only
paths there when a command starts, it writes only the worktree's own
`index` and git folder, and as what must be kept the shared `config` and
other worktrees' `HEAD` (opened to append); new names in the shared git
folder are left to hansa's checks before it runs git. It probes once for each set of
settings while it runs (a lane's tasks do not each pay for it; a restart
probes again). When srt refuses them, the run does not start: its task is held,
not failed and without using a try, and `hansa status` shows the lane as
unable to run, with srt's first line and the part of the settings it
refuses (the exact deny-write paths, the deny-write patterns, the deny-read
list, the write list or the network, each tried alone, or "the deny-write
patterns keep what git needs open" or "the deny-write patterns do not
keep" with the path; a sandbox that does not keep what it should runs no
one); the principal's
alert is told once for each lane and reason. Only what was refused is
held: a worker's or a verifier's refusal holds its workers and verifiers, and `hansa status` says
which. Held tasks start by themselves once a probe passes: after a fix,
restart `hansa serve`, which as it starts probes the sandbox of each held
lane again, clearing the hold when the probe passes and keeping it with the
new reason when it fails (a change of the settings probes again too). Without srt, or with an srt that cannot run at all, the
run starts as before, in Claude Code's own sandbox, which is set to fail
rather than run a command outside it (`failIfUnavailable`). Claude Code
carries its own copy of the sandbox library, which may differ from the srt
installed, so the probe is a guard, not proof: hansa also reads each run's
session record while it runs, and when a Bash result begins with
`sandbox-exec: ` (macOS) or `bwrap: ` (Linux) before any Bash call of the
run succeeded, it stops the run and holds its task the same way ("the
sandbox could not start: ..."), not counting it as a try. The deny patterns
write their character classes as ranges, `-` first (`[-0-9B-Zb-z._]` for
all but `a`): macOS refuses a sandbox with two or more classes that list
some sixty characters one by one, and a negated class cannot stand in,
since srt escapes its `^`. macOS's sandbox matches paths without regard to
case, so a letter left out of a class is left out in both cases, and it
reads a `-` just before `]` as a range up to the end of the character set,
so the `-` goes first. And it silently drops a class of more than 15
elements (a character or a range): the profile still applies, but the
class matches nothing. So a class that would hold more is split into
several patterns of at most 15 each. hansa's own names are
small letters, digits, `.`, `_` and `-`.

## A lane's write list and env

A lane's write list (`hansa lane set L write PATH ...`, full paths such as
`/Users/me/hansa-cache/a/pip`) is the folders its workers, setup and check may
write besides the worktree and their temporary folder: a build or test tool's
cache folders. A lane writes only folders of its own. The rule, in two lines:

- in the home folder of the account (whatever `HOME` says), only a plain
  folder: no name between the home folder and the entry starts with a dot,
  and it is not inside `~/Library` or `~/Applications`. Such as
  `~/hansa-cache/<lane>/<tool>` or `~/work/cache`;
- outside the home folder, anywhere but where programs are installed and the
  folders on the `PATH`: a tools folder such as `/srv/x/tools/xdg-cache/pnpm`,
  another volume, `/tmp`.

So none of the user's caches can be listed: not `~/.cache/pip`,
`~/Library/Caches/pip`, `~/.m2/repository`, `~/.gradle/caches`,
`~/.npm/_cacache`, `~/.cargo/registry` or any other hidden folder. They are
shared with what runs outside the sandbox (apps' updaters, corepack's node,
what brew installs from, the user's own builds), and shells, git and the
tools hansa runs read hidden files when they start, so a worker that wrote
there would change what runs outside the sandbox later. Give the lane a folder
of its own, such as `~/hansa-cache/<lane>/<tool>`, list it, and point the tool
there with the lane's env. The well-known tool homes with plain names are
refused too, with what is inside them: `~/go` (Go's default GOPATH, whose
`pkg/mod` the user's own builds share), `~/sdk` (the Go versions Go
downloads), `~/miniconda3`, `~/anaconda3`, `~/miniforge3`, `~/mambaforge`,
`~/miniconda`, `~/anaconda`, `~/micromamba`. Those are the common ones, not a
complete list: hansa cannot see what the account's own shell, git or tools name
in plain folders of the home folder (a conda install, a git hooks folder, a
file its shell sources), so do not list such a folder.

Where programs are installed: the Homebrew prefix (`/opt/homebrew`,
`/home/linuxbrew/.linuxbrew`, wherever `brew` is), `/opt/local`, `/opt/X11`,
`/usr`, `/Applications`, `/Library`, `/nix`, `/snap`, `/var/lib`, and where
`claude`, `git`, `srt`, `node`, `tmux`, hansa and its python really live: each
one's install folder, what is inside it and the folders above it; for hansa,
the whole folder that holds its package and `bin/hansa`. The folders on the
`PATH` hansa runs with are refused with what is inside them and the folders
above them (`~/bin/git` with `~/bin` on the `PATH`): hansa would run a program
put there.

The other refusals hold on top: the home folder or above it, the desk, the
lane's repository, a provider's Claude Code folder and `~/.claude`, a login,
key or history workers may not read (such as `~/.ssh` or
`~/.cargo/credentials`), what the principal lists on the page as not for
workers to read, a hidden folder at the top (`/.x`: such a folder can be
another name for the whole disk, as `/.vol`, `/.nofollow` and `/.resolve` are
on macOS), `/System`, `/dev`, `/proc`, `/sys` and `/etc`, and on macOS
`/Volumes` (a folder on another volume, such as `/Volumes/Build/cache`, can be
listed). So is a path with `( ) [ ] { } * ?` or a backslash in a name, a
look-alike of one of them (a name that NFKC-normalizes to one), or an
invisible character (a space other than the plain one too), which the sandbox
may read as a pattern or a person as another name. Each refusal says what it
would open. `hansa explain lane L` marks each entry a run would leave out,
with why. The coordinator tells the principal what it added and why.

Point a tool at the lane's folder with the lane's env (`hansa lane set L env
NAME=/Users/me/hansa-cache/L/TOOL`, and list that folder): pip
`PIP_CACHE_DIR`, npm `npm_config_cache` (env names may be in either case),
pnpm's store `npm_config_store_dir`, yarn `YARN_CACHE_FOLDER`, cargo
`CARGO_HOME`, go `GOMODCACHE` and `GOCACHE`, gradle `GRADLE_USER_HOME`, maven
`MAVEN_OPTS=-Dmaven.repo.local=...`, ccache `CCACHE_DIR`, uv `UV_CACHE_DIR`,
poetry `POETRY_CACHE_DIR`, pipenv `PIPENV_CACHE_DIR`, bun
`BUN_INSTALL_CACHE_DIR`, deno `DENO_DIR`, NuGet `NUGET_PACKAGES`, coursier
`COURSIER_CACHE`.

A lane's env reaches the lane's commands, which run in the sandbox (setup,
check, `hansa lane try` and the worker's Bash commands), not Claude
Code itself and not hansa's own programs: srt, the scripts around a setup or
check and their git, and Claude Code (with its git, shell, helpers and MCP
servers) run with the environment of `hansa serve`, and
the worker's commands get the lane's env from a file hansa writes
(`CLAUDE_ENV_FILE`), readable by the desk's account alone (mode 0600). So a
lane's `PATH` does not find srt or node for hansa, and a setting meant for
Claude Code goes in the environment `hansa serve` is
started with, not in a lane's env.

As a second line, what any lane's env on the desk names is closed to workers,
as what hansa's own environment names is, so no worker writes where a lane's
commands look: a write entry is refused when it is, is inside, or is above a
folder on a lane's `PATH` or a full path in any other env value, this lane's
or another's. A cache variable (those above, and `XDG_CACHE_HOME`,
`XDG_DATA_HOME`, `XDG_STATE_HOME`) may point at the lane's own folder; put a
tool's bin folder on the `PATH` inside the check or setup command
(`PATH=$CARGO_HOME/bin:$PATH cargo test`), not in the lane's env. A lane's
`PATH` holds only full paths, none empty, and no env value names a place
inside the desk. The names that tell a loader, git, a shell or Claude Code
what to run are refused (any starting with `LD_`, `DYLD_`, `GIT_` or
`CLAUDE_`, and `NODE_OPTIONS`, `BASH_ENV`, `ENV`, `SHELL`, `ZDOTDIR`, `HOME`):
set such a thing inside the check or setup command, where it runs in the
sandbox. So are the names sh, bash or zsh keep for themselves (read-only or
special, such as `UID`, `EUID`, `PPID`, `SHELLOPTS`, `IFS`, `PWD` and zsh's
`path`, `keymaps` and `dis_aliases`; each one tried in sh, bash and zsh):
setting one would stop every command of the lane's workers. A saved env that
holds a name refused now is not passed on whole: a run leaves that variable
out, with a line in `hansa show`.

The locale (`LANG`, `LC_ALL`) of a lane's setup, check, merge check
and workers is that of whoever started hansa: `hansa serve`'s environment
(one started over ssh takes the client's), since nothing runs without it;
`hansa explain lane L` says which locale they get and where from (when serve
does not run, the locale of this shell, which a serve started here would
give). A lane whose results must not depend on who started hansa sets its own
(`hansa lane set L env ... LC_ALL=C.UTF-8`): tools such as `sort` and `comm`
order and compare by the locale, and some `sort -u` keep only one of
`acme-core` and `acmecore` under `en_US.UTF-8`.

## A lane's network list

A lane's network list (`hansa lane set L network HOST ...`, such as
`registry.npmjs.org`, `*.pypi.org` or `localhost`; `hansa lane add
--network`) is what its workers may reach during a task, and what its setup
and check may reach; by default it is empty, and nothing is reached. A setup
that installs dependencies needs its registry there, and a build that
downloads needs the hosts it downloads from. Add a host only when the lane's
work needs it, and the coordinator tells the principal what it added and why.
Claude Code's web tools are given on the allow list instead (below).

`localhost` on the list differs by system:

- **On macOS** workers' own local servers (a test that starts an HTTP server,
  say) need `localhost`, and it opens the machine's whole loopback: every
  service listening there, local proxies among them, which reach the internet
  whatever the list says. List it only on a Mac whose loopback holds nothing a
  worker must not reach. A `hansa lane set` (or `hansa lane add`) that puts
  `localhost` on the list prints that warning with what listens on the
  loopback now, one line per server (and how many processes of other accounts
  it could not look at, which a worker can reach too).
- **On Linux** each sandbox has a loopback of its own: workers' own local
  servers need nothing on the list, and `localhost` adds nothing for them. It
  only lets a program told to use the sandbox's proxy for localhost reach the
  machine's loopback through it, so leave it off.

`hansa explain lane L` shows the list and says which of these holds on this
machine.

## A lane's allow list: commands and web tools

A lane's allowed commands go to its workers and verifiers alike: workers need no command
rule. The list is also where a lane gives Claude Code's web tools, which reach the network
from Claude Code's own process, outside the sandbox's network list: `WebFetch` only site by site, as
`WebFetch(domain:docs.python.org)` or `WebFetch(domain:*.python.org)` (a bare `WebFetch`, or one for every
site, is refused), and `WebSearch`, which sends search queries out of the machine. `WebFetch` takes public
site names only: an IP address in any form, `localhost` and the names meant for local or private networks
(`.local`, `.internal`, `.lan`, `.home.arpa`, `.test` and the like) are refused, and so is a wildcard over a
suffix others share (`*.co.uk`, `*.github.io`), as a wildcard opens every name under it. So are names with no
dot (`metadata`, which a resolver may complete) and the well-known names that come back as an address written
in them or as this machine (`nip.io`, `sslip.io`, `lvh.me`, `localtest.me` and the like, with every name under
them). Any listed name can still be pointed at any address, this machine included: list only sites known to be safe;
the principal sees every change. Each rule is one value, checked as Claude Code splits the list (on commas and
spaces outside parentheses, where every `)` closes them): one it would split into several, such as
`Bash(x),WebFetch` or `Bash(a(b),WebFetch,c)`, is refused. MCP tools are refused on the list. A change of the list is recorded, and `hansa explain lane L` shows the
web tools and their sites beside the network list. A rule saved before it was refused is left out of runs; the
page warns of it, names the coordinator's command that removes it, and saves the rest.

Inside the sandbox a worker or verifier needs no rule to run a command, but Claude Code still refuses a shell
`-c` it cannot check, such as `bash -c "..."` with a line that runs `rm` taken from a build script. Workers and
verifiers are told to write such a line to a script file in their temporary folder and run it with `sh`
(`sh <their temporary folder>/probe.sh`, in the words they are given), which runs in a lane with no allowed commands as in any other.

## Paths hansa refuses

A repository for a lane needs a first commit: `hansa init` and `hansa lane add` refuse one without, in one
line. They also refuse a desk or repository whose path holds a `)` followed later by a space or a comma
(`Work (2024) x/desk`): Claude Code would split the rules hansa writes for it, and a rule meant to close a
folder would leave it open. No run starts while such a rule would come out. Nor may the desk, the home folder
or a lane's repository or worktree have one of `[ ] * ? \` in its path (or where its links lead): the
sandbox's deny list and Claude Code's Read rules read those as a pattern, so a rule for `a[1]/desk/secrets`
would close `a1/desk/secrets` and leave the real folder open. `hansa init`, `hansa lane add`, `hansa serve`,
`hansa coordinator` and every run (worker, verifier, setup, check, merge check,
`hansa lane try`) refuse such a path and name it; move it. `{ }` match as themselves there.

## What hansa stops when a run ends

What hansa stops when a run ends (a worker, a verifier, or
the lane's check after one): the run's process group; then each process
`hansa serve` recorded below the run while it ran, whatever its group,
environment or working folder (on each of its rounds, every few seconds, on
the task, so a restarted server still has the record), if it still runs and
is still the same process (its start time; a number since given to another
process is left alone). Nothing is recorded for a run whose own process number
no longer has the start time hansa saved for it, so a process that was given
that number later, and its group, are never taken for the run's; then each process that names one of the task's runs: the run's own marker,
`HANSA_RUN`, in its environment (made for each run and recorded on the task as
`run_marks`; `HANSA_TASK` alone finds nothing, since task ids repeat from desk to
desk and one account may run several desks), or one of the run's temporary folders
(the worker's; a setup's or check's, and its own folder on disk on Linux, kept on
the task as `run_folders`) in its arguments or as its TMPDIR. TERM first, KILL 5 seconds later to what still runs; `hansa show`
lists what was stopped, with its group or alone, and the event log has it. What can still escape: a process started and moved
out of every marker (`env -i`, another folder, a session of its own) between
two rounds, whose parent ended before the next round.
