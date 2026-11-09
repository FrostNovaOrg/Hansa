# Who changes what

What the principal sets on the web page, and what the coordinator sets with `hansa`. What workers may touch is in [Workers and the sandbox](sandbox.md); the coordinator's own interface is
[hansa/card.md](../hansa/card.md), and `hansa help` for the rest.

## The principal, the coordinator, and what runs outside the worker

### The principal

The principal, on the page: providers, models, endpoints, keys, effort
defaults, the coordinator's model and effort (a new desk starts it on `opus` at
effort `max`), when the
coordinator's session compacts ([Compaction](desk.md#compaction)), prices,
the folders and files workers may not read (the coordinator may add to them, below), the
release targets that go out without asking (each ticked on its own), the words
no release may send out, the mailbox rule and its address/domain allowlist
([Releases](releases.md#forbidden-words-before-a-release)), and the alert command
([Alerts](releases.md#alerts)).
The page also shows what goes on, and asks the principal about each release
([Releases](releases.md)).

What workers may not read, no worker, verifier, setup or check reads, and Read
is denied there to the session `hansa coordinator` starts too. No lane's
repository may be such a folder or inside one (an entry inside a repository is
allowed), and no write list entry such a folder, inside one or above one. What
the page refuses on that list, and what a saved entry that can no longer be
applied does, is in [Workers and the sandbox](sandbox.md#what-workers-and-verifiers-may-touch).

These are the principal's alone, but for one thing: the coordinator may add to
what workers may not read with `hansa hide PATH [PATH ...]`, since an entry
only takes something away from workers. Each path is judged as a page save
adding it: any problem, even one already saved, refuses it, with the page's
reason; and one already on the list, however spelled, is not added again.
hansa records each addition as the coordinator's, with when, writes an event
(`no_read_added`) for it, and shows it beside the entry on the page and in
`hansa explain lane L`; an entry the principal saves on the page is the
principal's. Runs that start afterwards are kept from it, a run already going
keeps the sandbox it started with, and Read is denied there to the session
`hansa coordinator` starts from its next start. Only the principal removes an entry, on the page: removing
one gives workers something back (keys, logins), so there is no command for it,
and no command of the coordinator's may take one out. A page loaded before an
addition does not drop it on saving: as for any change made since it loaded,
the save is refused before any other check, and asks for Reload.

When the coordinator needs anything else here changed (a folder it keeps
needing to read, say), it asks with `hansa ask`; it never edits the desk's
`settings.json` itself.

### The coordinator

The coordinator, with `hansa`: tasks; the lanes (`hansa lane`: each one's
provider order, allowed commands (for workers and verifiers), network and write lists, environment,
check and setup commands, limits, and the merge check's own command and limit,
`merge_check` and `merge_timeout_min` ([Merging, the merge check and conflicts](tasks.md#merging-the-merge-check-and-conflicts))) and how many tasks run at once (`hansa concurrency`:
side by side means lanes on one repository; workers' changelog lines are collected,
and `hansa changelog` lists them for one task to write up);
each lane's standing instructions (`hansa lane brief`, kept in
`lanes/<lane>/brief.md`); pause and resume; each lane's release command
(`hansa lane target L default "git push origin main"`) or its named release
targets (`hansa lane target L NAME "<command>"`) and asking for a release
(`hansa release L [TARGET] "note"`), never pushing or deploying by itself:
its settings deny pushing and publishing from its own shell, whatever its
mode. `hansa release` runs the release command itself after the principal's yes.

The deny list:

- git's: `git push`, `git send-email`, `git svn dcommit`;
- GitHub's: `gh release`, `gh pr merge`, `gh repo`;
- a package or image registry's: `npm publish`, `pnpm publish`, `yarn publish`,
  `yarn npm publish`, `twine upload`, `uv publish`, `poetry publish`,
  `cargo publish`, `gem push`, `docker push`, `podman push`;
- Maven's: the `deploy` goal and the deploy plugin's goals starting with
  `deploy:d` (`deploy:deploy-file`; not `deploy:help`), through `mvn` or a wrapper in any folder (`mvnw`,
  `./mvnw`, `../mvnw`);
- Gradle's: tasks whose name starts with `publish` (`publish`,
  `publishToMavenLocal`, a project's own `:sub:publish`), through `gradle` or a
  wrapper in any folder (`gradlew`, `./gradlew`, `../gradlew`).

Each is denied written plainly or with options, goals or subcommands before it
(`git -C DIR push`, `git subtree push`, `mvn clean deploy`,
`./gradlew :sub:publishToMavenLocal`).

The list catches these commands and goals only. A Maven goal named in full
(`org.apache.maven.plugins:maven-deploy-plugin:3.1.1:deploy-file`),
`mvn release:perform` or `site:deploy`, a plugin's own publishing goal
(`nexus-staging:deploy`), Gradle's abbreviated task names (`./gradlew pTML`),
and tools that publish by themselves (`docker buildx --push`, `gh api`,
`npx semantic-release`) are not caught: the principal's yes on a release is what stands between the
coordinator and an outside server.

Since the rules match words anywhere after the program, they also refuse, in
the coordinator's own shell, a Gradle command with any word starting with
`publish` (`--tests publishing.X`, `:publishing:build`, `-p publishing`) and
Maven's `-pl deploy`: run such a build through the lane's check. In the same
way a git command with `push` anywhere among its arguments is denied even when
it only reads (`git log --grep push`, `git grep push`): the coordinator reads a
repository through `hansa git`.

Claude Code also matches these rules after a plain `xargs` and after wrappers
such as `env`, `nice` or `timeout` (`xargs git push` is denied). Run by its
full path (`/usr/bin/git push`), through `sudo`, `command`, `python -m` or a
git alias, through another shell (`bash -c "git push"`), a script, or an
`xargs` given options of its own before the command (`xargs -n1 git push`),
they are not caught. Claude Code asks the principal before each command that is
not already allowed.

### What runs outside the worker, and what stays closed to the coordinator

Setup, check and merge check run outside the worker, in the lane's sandbox (srt), and each change to
them is recorded and told to the principal by the coordinator. The coordinator is not a
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
a merge. Its interface is `hansa/card.md`, copied into the desk as
`CLAUDE.md`, and `hansa help` for the rest.

## Commit messages and identity

### The messages of hansa's commits

Each lane may also set the form of the messages hansa writes into its
repository: `commit_message` for the commit of a task's work (and the merge
of the base into a task's branch on `retry --update`), `merge_message` for a
task's merge into the base, each one line with `{task}`, `{title}` and
`{lane}` that holds `{task}` or `{title}`
(`hansa lane set L merge_message "Merge: {title}"`). Unset, they are
`t0001: <title>` and `Merge t0001: <title>`. A lane whose release goes to a
public repository usually leaves `{task}` out; the release's word scan reads
every outgoing commit message all the same. With no `--title`, a task's
title is the first line of its text cut at 80 characters, perhaps within a
word; when a lane's form holds `{title}`, `hansa add` says so for a title it
cut, with the title that will be used: give one with `--title`.

### The identity of hansa's commits

Every commit hansa makes for a lane (a task's commit, its merge into the
base, the merge check's merge included, the merge of the base into a task's
branch on `retry --update`, and the commit
`hansa lane remove --keep` makes) carries one
identity as both author and committer: the lane's `identity` (`hansa lane set
L identity "Name <email>"`, or `hansa lane add --identity`; copied by
`--like`; none: unset), else the repository's own `user.name` and
`user.email`, read from its local config alone (`git config --local`), else
`hansa <hansa@localhost>`. An account's global or personal identity is never
taken up by itself. A lane whose release goes to a public repository should
have one set, on the lane or in the repository's config; `hansa explain lane
L` shows the identity in use and where it comes from. When `hansa lane add`
(with or without `--like`) or `hansa lane set L identity` leaves a lane whose
identity in use differs from another lane's on the same repository, it says
so in one line: both identities, whether the lane's comes from the
repository's local config rather than the lane, and `hansa lane set L
identity "Name <email>"` to set it. Merge lanes are not compared: a merge
check's commits carry the task lane's identity. Workers keep `hansa
worker L` as theirs: they commit nothing (hansa does, from the index, with
the lane's identity), so it only marks what a worker's git would record
without a commit, and a commit a worker made against its rules stands out
by it.

`hansa lane remove L --keep` puts what each of the lane's worktrees holds
(changes not committed, or commits no branch keeps) on a new branch
`hansa-kept/NAME-DATE` of its repository (NAME the worktree's folder, `L`), and then removes the worktree.
The branch keeps the worktree's commits; changes not committed go in one more commit, `Left in worktree NAME
when lane L was removed`, with the identity the lane had before it was removed (else
the repository's own, else `hansa <hansa@localhost>`). With a clean tree no commit is made.

## A Claude Code session's identity, and the page's token

A process started from a Claude Code session (its Bash tool, a tmux it
opened, the `!` prefix) inherits the session's identity and inbox:
`CLAUDE_CODE_MESSAGING_TOKEN` and `CLAUDE_CODE_MESSAGING_SOCKET`, with which a
process can post into other sessions as that session, and
`CLAUDE_CODE_SESSION_ID`, `CLAUDE_PID`, `CLAUDECODE`,
`CLAUDE_CODE_ENTRYPOINT`, `CLAUDE_CODE_EXECPATH`, `CLAUDE_EFFORT`, `AI_AGENT`
and others. Every `hansa` command takes them out of its own environment as it
starts: by name, and by rule every `CLAUDE_CODE_MESSAGING_*`,
`CLAUDE_CODE_SESSION_*` and `CLAUDE_CODE_CHILD_*` (the list is in
[`hansa/__init__.py`](../hansa/__init__.py)). So nothing hansa starts inherits
them: not a release's command, the alert command, git and its hooks, the follower that posts to a session, nor the
desk's account on a Linux server. Workers and the lanes' setups and checks
never had them, as hansa builds their environment itself. The command run
from the session keeps them for itself and passes on only two, both for
notifying. `CLAUDE_PID` goes to the desk's account, in its environment, so that
the command it runs there can record which session asked for a task or request.
`CLAUDE_CODE_MESSAGING_SOCKET` goes to the follower of `hansa notify on`, on
the follower's command line (`--socket`, with `CLAUDE_PID`'s value as `--pid`),
as the inbox it posts to; its environment holds neither. How Claude Code runs
(`CLAUDE_CONFIG_DIR`, `CLAUDE_CODE_TMPDIR`, `CLAUDE_CODE_MAX_RETRIES` and the
like) is not a session's identity and stays.

`hansa serve` started inside a Claude Code session runs without them, and says
so: once as it starts, in `hansa status` and in `hansa explain server`, naming
what it dropped. It is better started from a plain shell, a terminal or tmux
of the principal's own; the coordinator tells the principal so.

The page's address carries its token, which approves releases. `hansa web`
prints it only outside a Claude Code session: in one (any of the variables
above set, the `!` prefix's shell included) it refuses, prints nothing of the
address, and says how the principal opens the page from a terminal of their own, as the
`ssh -L` line in [Getting started](getting-started.md#work-over-ssh) does. It refuses for `HANSA_ROLE` or `HANSA_TASK` as
before. `--file` (`hansa add`, `retry`, `lane brief`) admits only files workers may read,
using the same rules as new attachments, including the principal's list and the built-in private
folders. It follows symbolic links and checks private hard links. `--file -` applies those checks
to a regular file redirected to standard input, using its path and its device and inode; if its
path cannot be determined, it refuses. These checks keep private file content out of text agents
receive and apply before an account handoff too; they remain necessary while commands accept file
content for workers. Inline text, pipes and here-documents are read as before.

Nothing else hansa prints holds the token or a one-time code: `hansa serve`,
`hansa status` and `hansa explain server` give the page's host and port only; the
page logs no requests and answers an API request without the token with a fixed
refusal; the event log, posts to a session and errors never carry either; a
release's code is kept as a keyed hash, and an alert command's output is not
kept when its alert held a code. The token is held in the clear in
`.hansa/web-token`, closed to agents by the coordinator's permission rules
(Read is denied there) and the workers' sandbox; a release's code is made
immediately before its alert command runs, kept in memory and passed through
a pipe on standard input without a temporary input file.
The coordinator runs outside any sandbox, so a
Bash command the principal approves could still read them; it should never be
asked to.
