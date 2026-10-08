# Hansa Agents

**A small team of Claude Code agents, organized like a company.**

A coordinator session queues tasks; workers (one `claude -p` run per task, each
in its own git worktree) do them; hansa commits their changes, runs the lane's
check and keeps the record; and a person (the principal) owns the settings,
keys and budget through a local web page. Python standard library only; no
build step.

This branch holds an alpha rewrite that is much smaller than 0.4. Up to 0.4 the
project was called Bellhop; that version is on the `main` branch.

## Set up
Needs Python 3, git and Claude Code. Workers run in Claude Code's sandbox and do
not start without it: on macOS it is built in; on Linux it needs `bubblewrap`
and `socat`, and on Ubuntu 24.04 and later the system's AppArmor rules can stop
it (see Claude Code's sandboxing documentation). A lane with a setup or check
command also needs `srt`, Anthropic's sandbox runtime, which runs them in the
lane's sandbox: `npm install -g @anthropic-ai/sandbox-runtime`. Without it,
tasks in such a lane fail, and `hansa status` says so.
```sh
bin/hansa init /path/to/desk --repo /path/to/checkout   # first lane is "a"
cd /path/to/desk && hansa serve                          # keep it running (tmux, systemd, ...)
```
From your own computer, open the page through an SSH tunnel:
```sh
ssh -L 8765:127.0.0.1:8765 HOST /path/to/bin/hansa --desk /path/to/desk web
```
It prints an address with a token; open it in the browser and keep the
terminal open. More than one desk on a machine: each desk's `hansa serve` needs
its own port (`hansa serve --port 8766`), and that desk's `hansa web` finds it
(tunnel that port instead); `hansa status` and `hansa explain` say the address and
port the page is served on, and whether `hansa serve` serves it now. A repository
for a lane needs a first commit: `hansa init` and `hansa lane add` refuse one
without, in one line. They also refuse a desk or repository whose path holds a
`)` followed later by a space or a comma (`Work (2024) x/desk`): Claude Code
would split the rules hansa writes for it, and a rule meant to close a folder
would leave it open. No run starts while such a rule would come out. On the page: add providers (a Claude Code login, or any
endpoint that speaks the Anthropic API, with its key), models, effort
defaults, prices. Then start the coordinator with `hansa coordinator`; it sets
up the lanes and how many tasks run at once with `hansa lane` and
`hansa concurrency`. A worker runs any command inside its sandbox, writing
only its worktree, its own temporary folder and the lane's write list. A
lane's allowed commands go to its workers, verifiers and design sessions alike:
workers need no command rule, design sessions do. The list is also where a lane
gives Claude Code's web tools, which reach the network from Claude Code's own
process, outside the sandbox's network list: `WebFetch` only site by site, as
`WebFetch(domain:docs.python.org)` or `WebFetch(domain:*.python.org)` (a bare
`WebFetch`, or one for every site, is refused), and `WebSearch`, which sends
search queries out of the machine. `WebFetch` takes public site names only: an
IP address in any form, `localhost` and the names meant for local or private
networks (`.local`, `.internal`, `.lan`, `.home.arpa`, `.test` and the like)
are refused, and so is a wildcard over a suffix others share (`*.co.uk`,
`*.github.io`), as a wildcard opens every name under it. So are names with no
dot (`metadata`, which a resolver may complete) and the well-known names that
come back as an address written in them or as this machine (`nip.io`,
`sslip.io`, `lvh.me`, `localtest.me` and the like, with every name under them).
Any listed name can still be pointed at any address, this machine included:
list only sites you know; the principal sees every change. Each rule is one
value, checked as Claude Code splits the list (on commas and spaces outside
parentheses, where every `)` closes them): one it would split into several,
such as `Bash(x),WebFetch` or `Bash(a(b),WebFetch,c)`, is refused.
MCP tools are refused on the list (a task gets a browser with
`hansa add --browser`). A change of the list is recorded, and
`hansa explain lane L` shows the web tools and their sites beside the network
list. A rule saved before it was refused is left out of runs; the page warns of
it, names the coordinator's command that removes it, and saves the rest.

hansa tells the coordinator's session when a task ends or you answer a request,
through the session's inbox (`hansa notify` shows which sessions it posts to).
If your Claude Code settings hold messages from other sessions
(`crossSessionInbound`), the coordinator does not hear it; only you can change that.

The first time Claude Code opens in a new desk, it asks whether you trust the
folder: choose yes. Claude Code ignores the allowed commands of a folder it does
not trust, and the coordinator could not run `hansa`. The same question comes up
once for each lane's design worktree, in the first design session.

## On a Linux server
There the coordinator runs as root, so it can install software and set up the
machine, but workers do not: Claude Code's sandbox cannot start as root. The
desk, the lanes' repositories and the provider logins belong to an ordinary
account instead, and everything hansa starts runs as that account.
```sh
useradd --system --create-home hansa                   # the account
bin/hansa init /path/to/desk --user hansa              # a desk that is the account's
chown -R hansa /path/to/checkout                       # and each lane's repository
```
Install Claude Code where the account can run it (not only under root's home),
and log each subscription provider in as the account: `runuser -u hansa -- claude`
(with `env CLAUDE_CONFIG_DIR=...` before `claude` when the provider has a
config_dir). Then run `hansa serve` in the desk; started from root, it and every
other `hansa` command switch to the account by themselves (that needs `runuser`,
from util-linux). Run `hansa coordinator` as root: it stays root, uses root's own
Claude Code login and folder (the first time, log in inside the session), and
writes nothing in the desk as root: hansa does the coordinator's part there as
the account. Its own usage then is not in `hansa cost`,
because Claude Code keeps root's records where the account cannot read them. A
design session runs under the account's tmux; root joins it with the command the
page gives.

## Who changes what
- The principal, on the page: providers, models, endpoints, keys, effort
  defaults, the models of the coordinator and of design sessions, prices,
  whether the coordinator audits cost, the release targets that go out without
  asking (each ticked on its own), the words no release may send out, and the
  alert command (below). The page also shows what goes on, and asks the
  principal about each release (below).
- The coordinator, with `hansa`: tasks; the lanes (`hansa lane`: each one's
  provider order, allowed commands (for workers, verifiers and design sessions), network and write lists, environment,
  check, preview and setup commands, limits, and the merge check's own command and limit,
  `merge_check` and `merge_timeout_min`, for a heavier command run only before a merge: unset, the
  merge check runs the check under `timeout_min`) and how many tasks run at once (`hansa concurrency`:
  side by side means lanes on one repository; when they each add to a CHANGELOG, `CHANGELOG.md merge=union`
  in the repository's `.gitattributes` keeps their merges from conflicting there);
  each lane's standing instructions (`hansa lane brief`, kept in
  `lanes/<lane>/brief.md`); pause and resume; each lane's release command
  (`hansa lane set L release "git push origin main"`) or its named release
  targets (`hansa lane target L NAME "<command>"`) and asking for a release
  (`hansa release L [TARGET] "note"`), never pushing or deploying by itself:
  its settings deny `git push`, `gh release`, `gh pr merge`, `gh repo` and the
  usual publish commands (`npm`, `pnpm`, `yarn`, `uv` and `poetry publish`,
  `twine upload`, `cargo publish`, `gem push`, `docker` and `podman push`,
  `mvn deploy`) from its own shell, whatever its mode, written plainly or with
  options before the subcommand (`git -C DIR push`), and after a plain `xargs`
  or a wrapper such as `env`, `nice` or `timeout`, which Claude Code looks
  past (`xargs git push` is caught); run through another shell
  (`bash -c "git push"`), a script, or an `xargs` with options of its own
  before the command (`xargs -n1 git push`), they are not caught, and in
  manual mode such a command asks the principal. `hansa release`
  runs the release command itself after the principal's yes.
  Each lane may also set the form of the messages hansa writes into its
  repository: `commit_message` for the commit of a task's work (and the merge
  of the base into a task's branch on `retry --update`), `merge_message` for a
  task's merge into the base, each one line with `{task}`, `{title}` and
  `{lane}` that holds `{task}` or `{title}`
  (`hansa lane set L merge_message "Merge: {title}"`). Unset, they are
  `t0001: <title>` and `Merge t0001: <title>`. A lane whose release goes to a
  public repository usually leaves `{task}` out; the release's word check reads
  every outgoing commit message all the same.
  Every commit hansa makes for a lane (a task's commit, its merge into the
  base, the merge check's merge included, the merge of the base into a task's
  branch on `retry --update`, and a design session's commits) carries one
  identity as both author and committer: the lane's `identity` (`hansa lane set
  L identity "Name <email>"`, or `hansa lane add --identity`; copied by
  `--like`; none: unset), else the repository's own `user.name` and
  `user.email`, read from its local config alone (`git config --local`), else
  `hansa <hansa@localhost>`. An account's global or personal identity is never
  taken up by itself. A lane whose release goes to a public repository should
  have one set, on the lane or in the repository's config; `hansa explain lane
  L` shows the identity in use and where it comes from. Workers keep `hansa
  worker L` as theirs: they commit nothing (hansa does, from the index, with
  the lane's identity), so it only marks what a worker's git would record
  without a commit, and a commit a worker made against its rules stands out
  by it.
  Setup, check, merge check and preview run outside the worker, so each change to them is
  recorded and the coordinator tells the principal about it. It is not a
  worker: it runs on this machine outside any sandbox, and besides `hansa` it
  may run other commands the principal's work needs, such as installing
  software a lane needs or setting up a tool. Claude Code asks the principal
  before each one unless they have allowed it, and the coordinator says in
  plain words what the command does and why. It has three helpers: `scout`
  and `reviewer` only read; `operator` does one hands-on job on the machine
  (installing software, setting up a tool) with the coordinator's rights, each
  command with the principal's permission. Three things stay closed by design:
  reading `secrets/` (the keys), reading or editing the files directly in the
  Claude Code folder it runs from and its `sessions/` (its other folders stay
  open, its memory under `projects/` among them; a file that appears directly
  in the folder after the session starts is closed from the next start), and
  editing code in the lanes' repositories;
  code changes go through workers, so each gets a branch, the lane's check and
  a merge. Its whole interface is `hansa/card.md`, copied into the desk as
  `CLAUDE.md`.
- Workers (task and verification workers) change files in their worktree and
  report, inside Claude Code's sandbox, which the operating system enforces: any
  command, but files changed only in the worktree (and its temporary folder and
  the lane's write list, `hansa lane set L write ...`), no
  reading of the desk's keys and state, the coordinator's notes (`notes/`) and
  card (the desk's `CLAUDE.md`; design sessions may not read these either), of any provider's Claude Code folder or
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
  and proxies a build may need; Read is denied there to every
  Claude Code run hansa starts, the coordinator too. An entry that would stop
  every worker is refused on saving, each with its reason: the home folder,
  the desk, a lane's repository, a folder on the `PATH`, the system's programs
  and libraries (`/usr/lib`, `/etc` and the like), Homebrew's `Cellar`, `opt`
  and `Caskroom`, `~/.config`, `~/.local`, `~/.cache` (and `~/Library` on
  macOS), the temporary folder, the desk's worktrees folder or a lane's
  worktree, or a folder above any of them, and `/dev` or anything in it; a
  file or folder inside one of the others, such as one file in `~/.config` or
  one formula's folder in `Cellar`, may be listed. A name with one of
  `()[]{}*?\`, in the entry or where its link leads, cannot be written as a
  rule and is refused too. A list saved before, or a link changed since, that
  holds such an entry, or any other the page would refuse now, stops every
  run: hansa starts no worker, verifier, design session or coordinator, fails
  each task it would have started with the entry and the reason, and says it in
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
  downloads needs its registry on the network list. A design session's commands
  run in the sandbox of its lane's workers too (the same network list, write
  list, what may not be read and keys kept from the environment), with two
  differences: it may write in git what committing and merging into its own
  branch (`task/<id>`) takes, and on macOS it may bind local ports, so a preview it starts
  serves the principal's browser (that also lets its commands reach every
  service on the Mac's loopback). It runs only its allowed commands, as
  before, and a command the sandbox refuses does not run outside it: the
  sandbox's `allowUnsandboxedCommands` is off, so Claude Code ignores
  `dangerouslyDisableSandbox`. There are no design sessions on Linux: `hansa
  design` refuses there with one line, and one queued before does not start.
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
  `sequencer/`): workers never merge, hansa does. A design session (macOS) may write in git exactly what
  committing and merging into its own branch (`task/<id>`) takes: its own
  worktree's folder under `.git/worktrees/` but its `HEAD`, `commondir`, `gitdir`
  and `config.worktree` (git commits and merges on a branch without writing
  `HEAD`), git's objects, and its branch's ref and reflog, each with git's
  `.lock` beside it; its own merges' files (`MERGE_HEAD` and the like) among
  them. Its branch's ref it may write, but only as a commit id: hansa refuses
  one that leads to another ref (below). It cannot move the base, another branch or a tag, or write
  `packed-refs`, another worktree's folder, git's settings, hooks or `info/`, or
  the worktree's `.git` file; this holds for branches and worktrees made while
  it runs too (named with letters, digits, `.`, `_` and `-`, as hansa names
  them). Its commands run git with automatic gc, maintenance and rerere off
  (through the environment, after any git settings it already holds), since
  those write what it may not; git still tries to lock `packed-refs` on each
  commit and merge, prints that it could not, and goes on. The lane's setup and
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
  Before it starts a worker, a verifier or a design session, hansa serve tries
  that run's sandbox: `srt` with the same filesystem and network settings
  Claude Code is given, on `/usr/bin/true`, then a write of what git needs
  open there: the worktree's own `index` (opened to append, nothing written)
  and a new file in its own git folder, and for a design session its branch's
  ref's `.lock` and a new file in `objects/`; and a write of what must be
  kept, to see that the deny patterns match at all: a new file at the top of
  the shared git folder (`.hansa-try-kept`), a name next to the worktree's
  own folder under `worktrees/`, and for a design session a name next to its
  branch's ref and a new file in `refs/heads/` (what such a write makes is
  removed again). On Linux, where srt drops every deny pattern and keeps only
  paths there when a command starts, it writes only the worktree's own
  `index` and git folder, and as what must be kept the shared `config` and
  other worktrees' `HEAD` (opened to append); new names in the shared git
  folder are left to hansa's checks before it runs git. It does so once for each set of
  settings while it runs (a lane's tasks do not each pay for it; a restart
  tries again). When srt refuses them, the run does not start: its task is held,
  not failed and without using a try, and `hansa status` shows the lane as
  unable to run, with srt's first line and the part of the settings it
  refuses (the exact deny-write paths, the deny-write patterns, the deny-read
  list, the write list or the network, each tried alone, or "the deny-write
  patterns keep what git needs open" or "the deny-write patterns do not
  keep" with the path; a sandbox that does not keep what it should runs no
  one); the principal's
  alert is told once for each lane and reason. Only what was refused is
  held: a design session's refusal holds the lane's design sessions, a
  worker's or a verifier's its workers and verifiers, and `hansa status` says
  which. Held tasks start by themselves once a try passes: after a fix,
  restart `hansa serve`, which as it starts tries the sandbox of each held
  lane again, clearing the hold when the try passes and keeping it with the
  new reason when it fails (a change of the settings tries again too). Without srt, or with an srt that cannot run at all, the
  run starts as before, in Claude Code's own sandbox, which is set to fail
  rather than run a command outside it (`failIfUnavailable`). Claude Code
  carries its own copy of the sandbox library, which may differ from the srt
  installed, so the try is a guard, not proof: hansa also reads each run's
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
  several patterns of at most 15 each (`[-0-9A-KMNPQS-VX-Z._]*` and
  `[a-kmnpqs-vx-z]*` for all but `objects`, `refs`, `logs` and `worktrees` at
  the top of a design session's shared git folder). hansa's own names are
  small letters, digits, `.`, `_` and `-`.
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
  what it holds instead, and `hansa show` says so. At the end of a design session that
  commit is always a plain one on its branch: a merge the session left under
  way is committed as the changes it made, without the merged commit as a
  parent (the session's own merge commits stay as they are). In the lane's own checkout, a `commondir` in its `.git`
  stops every hansa git command there until it is removed by hand; hansa does
  not remove it. hansa's own git in a worktree runs with `core.fsmonitor` off,
  hooks from an empty folder and commit and tag signing off, whatever a
  setting there says; in the lane's own checkout its hooks run as before.
- One Bash call of a task or verification worker may run as long as the lane's
  check may, plus 5 minutes, but never past the worker's own limit: hansa sets
  Claude Code's `BASH_MAX_TIMEOUT_MS` to that (without it a call may run at most
  10 minutes). The check's limit is the lane's `merge_timeout_min`, else its
  `timeout_min`; the worker's is `timeout_min`. A call that asks for no timeout
  still stops after 10 minutes (`BASH_DEFAULT_TIMEOUT_MS`; less when the most is
  less), so a command that hangs does not hold it for the whole limit. Workers
  are told these limits in minutes, to give the Bash tool's timeout (in
  milliseconds, up to the most) for a long build or suite, and to run it in the
  foreground, in one Bash call, never in the background while they end their
  turn. A worker that does end its turn while
  background work it started still runs is resumed once, in the same session,
  with a short message: that work was stopped with it, so it runs it again in the
  foreground, waits for it, then reports. A second time it is not resumed; `hansa
  show` then says what was still running. The time limit counts from the task's
  start, across both runs. When the task is queued again for a new try (after a
  dropped connection, say), that try starts with nothing of the earlier try's
  resume: it may be resumed once in its own right, and the earlier try's turns
  and tokens stay in its attempt record. For a browser task whose lane has a
  preview, the first run's preview is stopped when that run ends, before hansa
  decides on the resume, and the resumed run gets one started again from the same
  worktree: never two at once. The first one's output is kept beside the second's
  (`preview-before-resume.log`, which `hansa show` names).
- A run that ended on a dropped connection is tried once more by itself. hansa
  reads only Claude Code's own error lines for this: one that begins `API Error`
  (a 5xx, a connection error or reset, a request that timed out, an overloaded
  provider, unable to connect, but not a certificate error), one that begins
  with a connection reset, refused or closed, a socket hang up, a fetch that failed or "Connection lost while your
  computer was asleep", one that ends `(ENETUNREACH)`, and the API's
  `overloaded_error`. A test that
  timed out, or a log line saying "overloaded", is the task's own failure.
  A write list holds a build or test tool's cache folders. The rule, in two
  lines: in the home folder of the account (whatever `HOME` says), only a
  plain folder (no name between the home folder and the entry starts with a
  dot, and not inside `~/Library` or `~/Applications`), such as
  `~/hansa-cache/<lane>/<tool>`; outside the home folder, anywhere but where
  programs are installed (the Homebrew prefix, `/opt/local`, `/opt/X11`,
  `/usr`, `/Applications`, `/Library`, `/nix`, `/snap`, `/var/lib`, the
  system's own folders, and where `claude`, `git`, `srt`, `node`, `tmux`,
  hansa and its python really live) and the folders on the `PATH` hansa runs
  with, what is inside them and the folders above them. A lane writes only
  folders of its own: the user's caches (`~/.cache`, `~/Library/Caches`,
  `~/.m2/repository`, `~/.npm/_cacache` and the rest) cannot be listed, since
  they are shared with what runs outside the sandbox, and neither can the
  common tool homes with plain names (`~/go`, `~/miniconda3`, `~/anaconda3`,
  `~/miniforge3`, `~/mambaforge`, `~/miniconda`, `~/anaconda`,
  `~/micromamba`, `~/sdk`) or what is inside them. That list is not
  complete: hansa cannot see what your own shell, git or tools name in plain
  folders of the home folder (a conda install, a git hooks folder, a file
  your shell sources), so do not list such a folder. `hansa explain lane L`
  marks each entry a run would leave out, with why. A lane's env names may be
  in either case (`npm_config_cache`). The desk, the lane's
  repository, Claude Code's folders, credential files and other names for the
  whole disk are refused as before, and so is a name with `( ) [ ] { } * ?`,
  a backslash, a look-alike of one, or an invisible character. Point a tool at
  the lane's folder with the lane's env: pip `PIP_CACHE_DIR`, npm
  `npm_config_cache`, pnpm's store `npm_config_store_dir`, yarn
  `YARN_CACHE_FOLDER`, cargo `CARGO_HOME`, go `GOMODCACHE` and `GOCACHE`,
  gradle `GRADLE_USER_HOME`, maven `MAVEN_OPTS=-Dmaven.repo.local=...`, ccache
  `CCACHE_DIR`, uv `UV_CACHE_DIR`, poetry `POETRY_CACHE_DIR`, pipenv
  `PIPENV_CACHE_DIR`, bun `BUN_INSTALL_CACHE_DIR`, deno `DENO_DIR`, NuGet
  `NUGET_PACKAGES`, coursier `COURSIER_CACHE`. A lane's env reaches the lane's commands, in the sandbox
  (setup, check, preview, `hansa lane try` and the worker's Bash commands),
  not Claude Code and not hansa's own programs: srt, the scripts around a
  setup or check and Claude Code run with the server's environment, and the
  worker's commands get the lane's env from a file hansa writes
  (`CLAUDE_ENV_FILE`). As a second line, what any lane's env on the desk
  names is closed to workers: a write entry cannot be, be inside or be above
  a folder on a lane's `PATH` or a full path in another env value, this
  lane's or another's. A cache variable (those
  above and `XDG_CACHE_HOME`, `XDG_DATA_HOME`, `XDG_STATE_HOME`) may point at
  the lane's own folder; put a tool's bin folder on the `PATH` inside the
  check command, not in the lane's env. A lane's env takes no name that tells
  a loader, git, a shell or Claude Code what to run (`LD_*`, `DYLD_*`,
  `GIT_*`, `CLAUDE_*`, `NODE_OPTIONS`, `BASH_ENV`, `ENV`, `SHELL`, `ZDOTDIR`,
  `HOME`), no name sh, bash or zsh keeps for itself (read-only or special, such
  as `UID`, `EUID`, `PPID`, `SHELLOPTS`, `IFS`, `PWD` and zsh's `path` and
  `keymaps`, each one tried in sh, bash and zsh: setting one
  would stop every command of the lane's workers), no `PATH` entry that is empty
  or not a full path, and no place inside the desk. The file hansa writes it to
  for a run is readable by the desk's account alone (mode 0600). `hansa/card.md` says the same.
- A task or verification with a browser (`hansa add --browser`, `hansa verify
  --browser`) on a lane with a `preview` (the command that starts the app, such
  as `npm run dev`) gets the app started for it. On macOS hansa starts the
  preview itself before the worker: in the lane's worktree, after the lane's
  setup, in `srt` with the lane's sandbox (network and write lists, env, the
  prelude), with local binding allowed for that one process; the worker's own
  sandbox stays as the lane sets it. hansa waits up to the lane's
  `preview_wait_s` (default 120 seconds) for the first `http://localhost:PORT`
  or `http://127.0.0.1:PORT` address the preview prints, or a port it names
  that answers, and puts the address in the worker's instructions; a preview
  not ready by then is stopped, and the worker is told it did not start, with
  its last lines, and goes on without it. hansa stops the preview with its
  whole process group when the worker ends, however it ends (done, failed,
  cancelled, stopped at the ceiling, or ended while `hansa serve` was
  restarted). It stops the processes the preview started that left that group
  too: while the preview runs, hansa looks every half second for what runs
  below it and records each one (its number and start time), and at the end
  stops each recorded one still running, whatever its group or environment,
  and any orphan (a process whose parent is the system's first process, as a
  daemon that forked twice is) started since whose working folder is in the
  worktree or the preview's temporary folder. A process with a living parent
  that the preview did not start is not touched, so a shell, an editor or a
  file watcher you start in the worktree while a browser task runs there is
  left alone. But a process you start there with `nohup` (or an editor that
  detaches itself from the shell that started it) becomes an orphan once that
  shell ends, and is stopped at the end of the task. What can still escape on macOS: a process that forks twice,
  leaves the group, clears its environment and changes folder all between two
  looks (half a second). It keeps running after the task, with the
  preview's sandbox, local binding included: it can reach the machine's
  loopback, write in the worktree and the lane's write list, and reach the
  hosts on the lane's network list, until it ends or the machine restarts. A
  worker's own processes are recorded too, on each round of `hansa serve`
  (see what hansa stops when a run ends, below). Its output is kept in the task's folder as
  `preview.log`, keys removed, at most its first and last 2 MB on disk with a
  line saying how much was left out between them (a byte that is not UTF-8 is
  kept as one `?`; where a cut falls inside a line, 4 KB more on each side of
  it are left out, so no key is kept in part, and the line says so; if
  removing keys made it longer, it is cut again after; hansa reads the whole output as
  it comes, so a preview that prints without end never waits on it and never
  fills the disk); `hansa show` says where and what became of it. **On macOS such a
  preview can reach every service on this machine's loopback while it runs**,
  as a lane with `localhost` on its network list can: that is the price of
  looking at the app in a browser on a Mac. **On Linux hansa does not start
  it**: `srt` runs a command there in a network namespace of its own
  (bubblewrap's `--unshare-net`; local binding is a macOS setting), so a
  server bound inside listens on the sandbox's own loopback, and the browser,
  which runs outside the sandbox, cannot reach it. The worker is told so. A
  design session starts its own preview, as before.
- Releases are the last step. hansa merges into a lane's base branch and stops
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
  tick. The principal answers in one of two ways: on the page, or by saying
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
- A release builds and starts what is on the base branch outside the sandbox,
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
- A release can be stopped: the coordinator with `hansa release --stop ID
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
- Alerts reach the principal away from the page. The principal may set one
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
- Workers see their checkout's own CLAUDE.md and settings, and hansa's instructions: not the desk's card,
  and not the settings, plugins, hooks or MCP servers of the account they run as. Nor may they, a
  verifier or a design session read the desk's card or `notes/`.
- Nobody reads hansa's code to use it. If the card or a command's output is not
  enough, fix the card or the output.

## Java and Maven in a lane
**What hansa does by itself.** Before a lane's setup, check and merge check, and in
front of each Bash command of a worker or verifier, hansa runs a short sh prelude
inside the sandbox, after the lane's env is set:
- The sandbox's proxy wants a user and password, which it puts only in
  `HTTPS_PROXY` and `HTTP_PROXY`; Maven reads neither. When one of them holds a
  proxy, the prelude writes a Maven global settings file in the run's temporary
  folder naming that proxy for https and http (`localhost` and `127.0.0.1` left
  out with `nonProxyHosts`), and adds `-gs <that file>` to `MAVEN_ARGS`, which
  Maven 3.9 reads. A global settings file, so a project's own
  `~/.m2/settings.xml` (its mirrors, say) still applies where the sandbox lets
  Maven read it.
- Java ignores `TMPDIR`, and on macOS the sandbox does not let it write its own
  temporary folder. The prelude adds `-Djava.io.tmpdir=$TMPDIR` to
  `JAVA_TOOL_OPTIONS`, which every JVM reads, the one Surefire starts for the
  tests too.

What was already in `MAVEN_ARGS` and `JAVA_TOOL_OPTIONS` (the sandbox's, or the
lane's env) is kept. The prelude prints nothing, never the proxy's password, and
skips what it cannot do instead of failing the run.

**What this changes for a build script.**
- Every JVM that finds `JAVA_TOOL_OPTIONS` set prints `Picked up
  JAVA_TOOL_OPTIONS: ...` on stderr before anything else. `java -version` and
  `java -XshowSettings` print on stderr, so their output starts with that line;
  `javac -version` and `mvn -v` print on stdout, so theirs starts with it only
  when stderr is read with it (`2>&1`). A script that reads the version from
  the first line (`java -version 2>&1 | sed -n '1s/.*version "\([0-9]*\).*/\1/p'`)
  reads nothing and fails as if java were missing: look for the line holding
  `version "` instead (`sed -n '/version "/{s/.*version "\([0-9]*\).*/\1/p;q;}'`).
- A build script that sets `MAVEN_ARGS` itself drops the `-gs <file>` hansa put
  there, and Maven then gets `407 Proxy Authentication Required` for anything
  its local repository lacks (on a machine whose local repository holds
  everything, nothing shows). In bash, `MAVEN_ARGS=(-B)` makes it an array,
  which is not exported, and `mvn "${MAVEN_ARGS[@]}"` passes only `-B`. Keep
  hansa's: `MAVEN_ARGS=(-B ${MAVEN_ARGS:-})`. A failed step whose output holds
  Maven's 407 (a line with `407` and `proxy authentication` along with Maven's
  own words, such as `Could not transfer artifact`) gets a line saying so.
- On macOS, `mktemp -d`, `mktemp -t PREFIX` and a bare `mktemp` ignore `TMPDIR`:
  they write to the account's own temporary folder under `/var/folders`, which
  a lane may not write (other programs of the account, `hansa serve` among
  them, keep files there), and fail with `mktemp: mkdtemp failed on
  /var/folders/...: Operation not permitted`. Give mktemp a template in
  `TMPDIR`: `mktemp -d "${TMPDIR:-/tmp}/name.XXXXXX"` or `mktemp -d -p
  "$TMPDIR"`. Linux's mktemp reads `TMPDIR`. A failed step whose output holds
  that refusal (`Operation not permitted`, on macOS or under `/var/folders`)
  gets a line saying so; Linux's mktemp refused gets a plainer one, that its
  folder is outside what the lane may write. This holds for any lane, not only Java's.

**What a project should do.** Mockito's inline mock maker attaches itself to the
running JVM, and cannot in the sandbox: the JVM's attach files and socket live in
the system's temporary folder whatever `java.io.tmpdir` says, and the sandbox
does not open that folder to workers. Load Mockito as a Java agent instead, as
Mockito documents; in the pom:
```xml
<properties>
  <argLine></argLine> <!-- empty, so @{argLine} is there without a coverage agent -->
</properties>
...
<plugin>
  <groupId>org.apache.maven.plugins</groupId>
  <artifactId>maven-dependency-plugin</artifactId>
  <executions>
    <execution>
      <goals><goal>properties</goal></goals> <!-- sets ${org.mockito:mockito-core:jar} -->
    </execution>
  </executions>
</plugin>
<plugin>
  <groupId>org.apache.maven.plugins</groupId>
  <artifactId>maven-surefire-plugin</artifactId>
  <configuration>
    <argLine>@{argLine} -javaagent:${org.mockito:mockito-core:jar}</argLine>
  </configuration>
</plugin>
```

**The lane.**
- A Maven repository of the lane's own, on its write list, named in its
  commands: `hansa lane set L write ~/hansa-cache/L/m2`, and
  `-Dmaven.repo.local=...` with that full path in the setup and check (not
  `~/.m2/repository`, which the write list refuses).
- `repo.maven.apache.org` on the network list, and any other repository the
  build downloads from.
- Tests that bind a local port cannot on macOS unless `localhost` is on the
  network list, which opens the machine's whole loopback. Have such tests skip
  when the bind is refused (JUnit's `Assumptions`, say), as hansa's own do.

Gradle is not covered yet: the prelude sets nothing for it but
`JAVA_TOOL_OPTIONS`.

## When a provider hits a usage limit
When a provider answers with a usage limit (HTTP 402 or 429, or the wording of one), it rests: no task
starts on it until the rest ends, and a task stopped by the limit moves to the next ready provider of its
lane, or waits for one.

- **Until the reset time, when the error gives one**, and a minute, for at most 31 days. hansa reads a
  clock time (`You've hit your session limit · resets 3pm (Europe/Berlin)`, as Claude Code prints a
  subscription's limit), a date and time (`resets Oct 9, 3pm (Europe/Berlin)`, `resets 2026-08-08 00:00
  UTC`), a Unix time (`usage limit reached|1760000000`, `resets_at`), a `retry-after` value given in the
  message, and "in N minutes" or "in N hours" (`Please try again in 20 minutes`). A time without a zone is
  the machine's local time; a month and day without a year is in the year that puts it nearest to now.
  A form hansa is unsure of, a time already past, or a line it cannot read gets the fixed length below.
- **Else for the provider's own rest**, `rest_minutes` on the page (1 to 44640, a month), or the desk's
  `cool_minutes` (30 by default) when it is empty. Fit it to the provider: a subscription's window is a
  few hours, an API key's limit may be per minute, a plan's allowance may be monthly.
- **No lost tries.** A task whose run ended on its provider's usage limit goes back to the queue without
  using one of its 3 tries, 5 times; after that, each such end counts as a try, so a provider that hits its
  limit again right after each rest cannot hold a task forever. Any other failure counts as before.
  `hansa show ID` says until when the task waits; `hansa status` and `hansa explain provider P` say until
  when each provider rests and why (the reset time it read, or its fixed length), and its rest length.
- **Every try costs, and counts.** When a task goes back to the queue for another try (a usage limit, a
  dropped connection, a provider that refused it), that run's tokens, turns and worth are kept on its
  attempt record; a run an earlier `hansa serve` started and lost track of is read from its session record
  as the task goes back. A run hansa resumed counts with the run before it, the resumed part read from
  the session record after the resume only. `hansa cost` (also `--by task`, which shows each task's runs
  and turns), `hansa show`, and the page's cost and task view count every try once, by when each run ended.

## Files
Everything is under the desk directory; see the top of `hansa/desk.py`. Keys live
in `secrets/` (mode 600) and are never shown or sent anywhere but to the
provider's endpoint, as that worker's environment. hansa replaces a key with
`[key of <provider>]` wherever it would otherwise show, and refuses to merge a
change that holds one. The coordinator's permission rules are hansa's: they are
in the desk's `.claude/settings.json` and come up to date whenever the
coordinator starts. Rules of your own go in `.claude/settings.local.json`.
The coordinator creates and edits files under the desk's `notes/` without
asking (its plan and findings, kept past a cleared session); everything else it
edits asks as before, and the closed files stay closed. `hansa notes archive FILE HEADING`
moves a finished section from a notes file to `notes/archive/FILE` without the
coordinator typing it out again.

The project's own rules (how it builds, what may never go out, who decides what)
go in the desk's `notes/PROJECT.md`. The card imports it with a line of its own,
`@notes/PROJECT.md` (Claude Code's import syntax for memory files), so Claude Code
loads it with the card at every start, after `/clear` and after compaction, and the
import survives every card refresh. `hansa init` makes the file once, holding only
an HTML comment that says what it is for (Claude Code drops such comments, so it
costs nothing until something is written there); after that hansa rewrites it
only when the coordinator asks, with `hansa notes archive PROJECT.md HEADING`. A
missing file loads nothing and stops nothing. The coordinator and you keep it; the
coordinator edits it without asking, as anything under `notes/`. Workers do not
see it: rules for a lane's workers go in its brief. `hansa status` shows its size,
and calls it long past 8 KB: it is part of the conversation's first message, so
every turn rereads it, and 8 KB (some 2,000 tokens) is about the 200 lines Claude
Code advises a memory file to stay under.

The coordinator's session does not load the claude.ai connectors of the Claude
account it logs in with (Claude Code's `disableClaudeAiConnectors`): a connector
is a way to publish outside the machine, and it adds its tools to every turn.
MCP servers you set up on the machine stay. hansa also turns off Claude Code's
guessed next message in the coordinator's input box (prompt suggestions); in a
tmux capture without colors that grey guess looked typed. If you run an older
Claude Code where a grey line still shows there, do not press Enter on what
looks like an empty box: it may send the guess as if you wrote it.

## Current limits
- Alpha: settings, commands and files may still change between versions.
- A worker's and a design session's commands (the lane's preview among them),
  and the lane's setup and check commands, run in the operating system's
  sandbox, and so does the preview hansa starts for a task with a browser (on
  macOS only, with local binding: it can reach every service on the machine's
  loopback while it runs, as a design session's commands can); the coordinator
  does not, and runs as the desk's user. There are no design sessions on Linux
  (above). Run a desk as a user, or on a machine, whose files you
  are content for that code to reach.
- hansa replaces the principal's keys wherever it shows or records text, and will not
  merge a change that holds one. It cannot see into compressed files, and the
  event log keeps what it recorded before a key was saved.
- What hansa stops when a run ends (a worker, a verifier, a design session, or
  the lane's check after one): the run's process group; then each process
  `hansa serve` recorded below the run while it ran, whatever its group,
  environment or working folder (on each of its rounds, every few seconds, on
  the task, so a restarted server still has the record), if it still runs and
  is still the same process (its start time; a number since given to another
  process is left alone). Nothing is recorded for a run whose own process number
  no longer has the start time hansa saved for it, so a process that was given
  that number later, and its group, are never taken for the run's; then each process that names the task: `HANSA_TASK`
  in its environment, or the run's temporary folder in its arguments or as its
  TMPDIR. TERM first, KILL 5 seconds later to what still runs; `hansa show`
  lists what was stopped, with its group or alone, and the event log has it. What can still escape: a process started and moved
  out of every marker (`env -i`, another folder, a session of its own) between
  two rounds, whose parent ended before the next round. A design session's
  tmux session is ended with it; the tmux server is left running.
- A lane repository's merge hooks run while `hansa merge` holds the desk, so
  `hansa serve` waits for them.
- One `hansa serve` per desk, on one machine.

## Tests
`python3 -m unittest tests.test_hansa` (uses a stand-in for `claude`; no model is called).
They run on GitHub's machines (Ubuntu and macOS, Python 3.9 and the newest) for every push to `alpha` or
`main` and every pull request: `.github/workflows/tests.yml`.

## License

Hansa is dual-licensed.

- **Open source:** [GNU AGPL v3 or later](LICENSE). Use it, change it, share it; if you run a modified version as a network service or distribute it, the AGPL asks you to share your source under the same terms.
- **Commercial:** [Hansa Commercial License](LICENSE-COMMERCIAL.md) from FrostNova, for embedding Hansa in proprietary products or services without AGPL obligations.

`SPDX-License-Identifier: AGPL-3.0-or-later OR LicenseRef-Hansa-Commercial`

Copyright (c) 2026 FrostNova.
