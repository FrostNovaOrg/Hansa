# On a Linux server

The [README](../README.md#quick-start) covers the usual setup, where the desk belongs to the user who runs
hansa. Here: a Linux server where you work as root, and what a lane's commands meet in Linux's sandbox.

## The coordinator as root, the desk as an ordinary account

There the coordinator runs as root, so it can install software and set up the
machine, but workers do not: Claude Code's sandbox cannot start as root. The
desk, the lanes' repositories and the provider logins belong to an ordinary
account instead, and everything hansa starts runs as that account.
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
config_dir). Then run `hansa serve` in the desk; started from root, it and every
other `hansa` command switch to the account by themselves (that needs `runuser`,
from util-linux). Run `hansa coordinator` as root: it stays root, uses root's own
Claude Code login and folder (the first time, log in inside the session), and
writes nothing in the desk as root: hansa does the coordinator's part there as
the account. Its own usage then is not in `hansa cost`,
because Claude Code keeps root's records where the account cannot read them. A
design session runs under the account's tmux; root joins it with the command the
page gives.

## What a lane's commands meet in Linux's sandbox

Two things a lane's commands meet in the sandbox there (bubblewrap), which
`hansa explain lane` repeats:
- **The mount table can call a writable folder read-only.** The sandbox mounts
  `/` read-only and binds the worktree, the lane's write list and a worker's
  temporary folder writable inside it. A bind of a folder keeps its file
  system's device, and Java's `FileStore.isReadOnly()` finds a folder's mount by
  walking up while the device stays the same, so it reaches the read-only `/`
  (or `/tmp`, when that is a file system of its own) and says true; `findmnt`
  and `df` name that mount too. Trust the error of the write itself. A project
  that uses `FileStore.isReadOnly()` to tell "read-only file system" from "no
  permission" reports the wrong cause there. The TMPDIR of the setup, check,
  merge check and preview is a tmpfs of its own, which Java reads as writable:
  hansa makes `hansa-tmpfs` in the command's temporary folder and lists it as
  not readable, srt hides it under an empty tmpfs, writable inside, and the
  prelude hansa runs before the command makes it TMPDIR (and Java's
  `java.io.tmpdir`). What is written there is in memory and goes when the
  command ends. Where srt mounts nothing there, TMPDIR stays the run's folder.
  The worktree and the write list cannot be given such a mount: they must be
  the folders outside, and srt binds only those, so there trust the write. A
  worker's own temporary folder is Claude Code's, and stays as it is.
- **git there does not list the sandbox's placeholders.** Where a path it
  protects is missing, the sandbox mounts an empty file at the top of the
  worktree (`.bashrc`, `.bash_profile`, `.zshrc`, `.zprofile`, `.profile`,
  `.gitconfig`, `.gitmodules`, `.ripgreprc`, `.mcp.json`, `.vscode`, `.idea`)
  for as long as it runs. So that a build script that refuses a tree with
  untracked files still builds, the prelude gives git in the sandbox (setup,
  check, merge check, preview and a worker's Bash) an excludes file in the
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
  the run (a lane command's tmpfs, at its own end). A command that empties
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
