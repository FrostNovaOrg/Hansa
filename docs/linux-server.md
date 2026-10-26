# On a Linux server

[Getting started](getting-started.md) covers installation, login, the first task
and [SSH access](getting-started.md#work-over-ssh), where the desk belongs to the
user who runs hansa. Here: a Linux server where the principal works as root,
what a lane's commands meet in Linux's sandbox, and what the kernel stops first
when memory runs out.

## The coordinator as root, the desk as an ordinary account

There the coordinator runs as root, so it can install software and set up the
machine, but workers do not: Claude Code's sandbox cannot start as root. The
desk, the lanes' repositories and the provider logins belong to an ordinary
account instead: hansa's server and workers run as that account, but root's follower and its files stay root's.
```sh
useradd --system --create-home hansa                   # the account
bin/hansa init /path/to/desk --user hansa              # a desk that is the account's
chown -R hansa /path/to/checkout                       # and each lane's repository
```
Keep hansa's own checkout where the account can reach and read it, not under root's home: root's `hansa`
runs its commands again as the account, from that checkout, and the account cannot load hansa from a
folder it cannot read or enter (`No module named hansa`).
Install Claude Code where the account can run it (not only under root's home),
and log each subscription provider in as the account: `runuser -u hansa -- claude`
(with `env CLAUDE_CONFIG_DIR=...` before `claude` when the provider has a
config_dir). Follow the guide for [page setup](getting-started.md#open-the-principals-page).
In this root/account arrangement, run `hansa serve` in the desk: started from root, it and every
other `hansa` command but `coordinator`, `init`, `notify`, `notify-follow` and `up`
switch to the account by themselves (that needs `runuser`, from util-linux). Run `hansa coordinator` as root: it stays root, uses root's own
Claude Code login and folder (the first time, log in inside the session), and
writes nothing in the desk as root: hansa does the coordinator's part there as
the account. Its own usage then is not in `hansa cost`,
because Claude Code keeps root's records where the account cannot read them.

Most `hansa` commands run as the account by themselves, but the coordinator stays
root: it changes the desk only through `hansa`, never with its own tools. Git
refuses to work for root in a repository another account owns, so the
coordinator reads the lanes' repositories with `hansa git LANE ...` (which runs
git as the account) and with the Read tool, one simple command at a time, and
never tells git to trust them (`safe.directory`): that would have root run what
the account's repository settings name. In `manual` mode any command other than
`hansa` waits for the principal's approval.

## What a lane's commands meet in Linux's sandbox

What a lane's commands meet in the sandbox there (bubblewrap), which
`hansa explain lane` repeats:
- **The mount table can call a writable folder read-only.** The sandbox mounts
  `/` read-only and binds the worktree, the lane's write list and a worker's
  temporary folder writable inside it. A bind of a folder keeps its file
  system's device, and Java's `FileStore.isReadOnly()` finds a folder's mount by
  walking up while the device stays the same, so it reaches the read-only `/`
  (or `/tmp`, when that is a file system of its own) and says true; `findmnt`
  and `df` name that mount too. Trust the error of the write itself. A project
  that uses `FileStore.isReadOnly()` to tell "read-only file system" from "no
  permission" reports the wrong cause there. The TMPDIR of the setup, check
  and merge check is not such a bind, and Java reads it as writable; see the
  next point. The worktree and the write list cannot be given a mount of
  their own: they must be the folders outside, and srt binds only those, so
  there trust the write. A worker's own temporary folder is Claude Code's, and
  stays as it is.
- **A lane command's TMPDIR is a folder of its own on the desk's disk.** Each
  setup, check and merge check gets two folders, made for it alone (mode 700)
  and removed when it ends: the run's folder in the system's temporary folder
  (`/tmp/hansa-check-*`), which srt is given as `TMPDIR` and
  `CLAUDE_CODE_TMPDIR` and keeps its sockets in, and the command's own,
  `.hansa/tmp/hansa-check-*` (`hansa-setup-*` for a setup) in the desk, which
  the run's folder links to as `hansa-tmp`. The prelude hansa runs before the
  command follows that link and makes the folder TMPDIR (and Java's
  `java.io.tmpdir`); inside, `CLAUDE_CODE_TMPDIR` stays the run's folder.
  Every run's sandbox hides `.hansa` under an empty tmpfs, and srt binds a
  folder the command may write back over it from the disk: so no other run (a
  worker, another lane's check, this task's other command) can read it, and
  it is a mount of its own, writable in the mount table, whose parent is
  another device, which is how Java's `FileStore.isReadOnly()` reads it as
  writable. `findmnt` and `df` there name the desk's file system, with its
  size and free space. What is written there takes the desk's disk, not
  memory: it used to be a tmpfs srt mounted in the run's folder, as big as
  half the machine's memory, which a build's temporary files (a Java suite's
  fonts, some 2.7 GB in one run) filled with "No space left on device",
  taking memory the build itself needed. hansa removes the folder when the
  command ends, or, for a command that was stopped, when its task or try
  ends. Where the prelude finds no folder it may write there,
  TMPDIR stays the run's folder; where the desk's path holds a space, a
  TMPDIR many build tools break on, hansa makes no such folder and TMPDIR is
  the run's folder too (read-only to Java, as a bind). macOS is as
  before: TMPDIR is the run's folder.
- **git there does not list the sandbox's placeholders.** Where a path it
  protects is missing, the sandbox mounts an empty file at the top of the
  worktree (`.bashrc`, `.bash_profile`, `.zshrc`, `.zprofile`, `.profile`,
  `.gitconfig`, `.gitmodules`, `.ripgreprc`, `.mcp.json`, `.vscode`, `.idea`)
  for as long as it runs. So that a build script that refuses a tree with
  untracked files still builds, the prelude gives git in the sandbox (setup,
  check, merge check and a worker's Bash) an excludes file in the
  command's TMPDIR: the excludes git would read (`core.excludesFile` from the
  system's and the account's config, else `~/.config/git/ignore`), then those
  names anchored at the top. With git 2.32 or later it does so through
  `GIT_CONFIG_GLOBAL`, a config file there that includes the account's own
  (`~/.gitconfig` and `~/.config/git/config`, or the file `GIT_CONFIG_GLOBAL`
  named) and then sets `core.excludesFile`, so every setting there still
  applies and a command that sets its own `GIT_CONFIG_COUNT` keeps it. A read
  with `git config --global <key>` follows no include, so it prints nothing for
  the account's `user.name`: use `git config <key>`, or
  `git config --global --includes <key>`. An older
  git gets it as `GIT_CONFIG_COUNT`, which such a command replaces. A tracked
  file of such a name is still listed; git outside the sandbox (what hansa
  commits, the main checkout) reads none of it. Not the repository's
  `info/exclude`, which the main checkout and every worktree share. A
  repository's own `core.excludesFile` wins over it, as git means it to, and so
  does one a command sets (`git -c core.excludesFile=...`, or a lane's env
  through `GIT_CONFIG_*`): git there lists the placeholders again. Add the
  names, anchored (`/.bashrc`, `/.vscode` and the rest), to the file it names,
  or drop the setting. What `git config --global` writes in the sandbox goes to
  that file in TMPDIR, not the account's.
  Both files are named by a checksum of what they hold
  (`hansa-git-excludes-<sum>`, `hansa-gitconfig-<sum>`), each written under a
  name of its own (`mktemp`) and then moved in place, so commands side by side
  in one TMPDIR, even with the same process id, as srt's process namespaces
  give them, never read a half-written file or one written for another
  account's settings (`HOME`, `XDG_CONFIG_HOME`, `GIT_CONFIG_GLOBAL`); a test
  starts a dozen of them at once with the same `$$`. One pair stays in TMPDIR
  per distinct set of settings, until the temporary folder goes at the end of
  the run (a lane command's own, at its own end). A command that empties
  TMPDIR while it runs (a cleanup) takes them with it: it and the commands
  sharing that TMPDIR list the placeholders until their next start.
  The empty files the sandbox puts under `.claude/` are still listed there, and
  hansa never commits them.

To see what Java says in a lane after an upgrade, run
`tests/linux_sandbox_probe.sh` (in hansa's checkout) as the lane's check once:
`hansa lane set L check "sh /path/to/hansa/tests/linux_sandbox_probe.sh"`,
`hansa lane try L`, which prints the last lines of its log, then set the check back. It prints
TMPDIR, what `findmnt`, `df` and `FileStore.isReadOnly()` say for TMPDIR and
the worktree, whether a write there works, and the untracked files git lists at
the top of the worktree (the first 3, and how many more), in at most 14 lines.

## When memory runs out

On Linux, hansa sets `oom_score_adj` to 500 outside the sandbox before
starting workers, verifications, setups, checks, merge checks and lane tries;
their children inherit it, so the kernel prefers killing them when memory runs
out, while `hansa serve`, the coordinator and releases keep their scores. A
short shell sets the score and then replaces itself with the command (`exec`),
so the process hansa records is the command's own. A refused write does not
stop the run, and a program that cannot be found starts without that shell,
so hansa says it could not start as before. This does not choose among the
workers; systemd-oomd, where it runs, picks by other rules.

hansa reads `oom_kill` in `/proc/vmstat` outside the sandbox at each run's start and end (Linux 4.13+).
Workers, verifications, checks, merge checks, lane tries and releases keep a positive
difference on their records; `hansa show` and end notices say how many processes the kernel killed while
the run ran. This is the whole machine's count, so another run can account for it. macOS, a missing or
unreadable file, or a file without `oom_kill` leaves no count and does not interrupt the run.
Setups belong to those runs and are covered by their counts.

A failed check or merge check runs once more only with a readable Linux count that rose while it ran,
and only if hansa did not stop it itself. A merge check whose lane setup fails is not run again and shows
the setup's output as before. Exit 137 or no exit code alone does not qualify: its command may have killed
itself, or its exit file could not be written.
The rerun has the lane's full time ceiling; a merge check stays first in line.
`hansa show ID --check` keeps the first run's exit and last lines, even
if the base moves after the rerun, and states the count's evidence without claiming this run was killed
or promising a rerun before it starts.
If the rerun fails with another rising count, both counts appear with a memory hint: fewer runs at once
may help (`hansa concurrency`). Without a second rise it ends as an ordinary failed check. Workers and
verifications still retry one system kill; lane tries and releases only record the count. This rule
covers a killed test child whose check shell exits 1, while leaving unrelated failures alone when the
count stays unchanged. The count covers the whole machine and cannot identify which run was killed.
