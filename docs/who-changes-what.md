# Who changes what

What the principal sets on the web page, and what the coordinator sets with `hansa`. What workers and
design sessions may touch is in [Workers and the sandbox](sandbox.md); the coordinator's own interface is
[hansa/card.md](../hansa/card.md).

## The principal, the coordinator, and what runs outside the worker

### The principal

The principal, on the page: providers, models, endpoints, keys, effort
defaults, the models of the coordinator and of design sessions, prices,
whether the coordinator audits cost, the browser tool a task given `--browser`
uses, the folders the coordinator may read outside the desk without asking
(`hansa explain` lists them), the folders and files workers may not read, the
release targets that go out without asking (each ticked on its own), the words
no release may send out, and the alert command ([Alerts](releases.md#alerts)).
The page also shows what goes on, and asks the principal about each release
([Releases](releases.md)).

What workers may not read, no worker, verifier, setup or check reads, and Read
is denied there to the coordinator and to design sessions too. No lane's
repository or write list may be such a folder, inside one or above one. What
the page refuses on that list, and what a saved entry that can no longer be
applied does, is in [Workers and the sandbox](sandbox.md#what-workers-verifiers-and-design-sessions-may-touch).

These are the principal's alone. When the coordinator needs one changed (a
folder it keeps needing to read, say), it asks with `hansa ask`; it never edits
the desk's `settings.json` itself.

### The coordinator

The coordinator, with `hansa`: tasks; the lanes (`hansa lane`: each one's
provider order, allowed commands (for workers, verifiers and design sessions), network and write lists, environment,
check, preview and setup commands, limits, and the merge check's own command and limit,
`merge_check` and `merge_timeout_min` ([Merging, the merge check and conflicts](tasks.md#merging-the-merge-check-and-conflicts))) and how many tasks run at once (`hansa concurrency`:
side by side means lanes on one repository; when they each add to a CHANGELOG, `CHANGELOG.md merge=union`
in the repository's `.gitattributes` keeps their merges from conflicting there);
each lane's standing instructions (`hansa lane brief`, kept in
`lanes/<lane>/brief.md`); pause and resume; each lane's release command
(`hansa lane set L release "git push origin main"`) or its named release
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
- Maven's: the `deploy` goal and the deploy plugin's `deploy:` goals
  (`deploy:deploy-file`), through `mvn` or a wrapper in any folder (`mvnw`,
  `./mvnw`, `../mvnw`);
- Gradle's: tasks whose name starts with `publish` (`publish`,
  `publishToMavenLocal`, a project's own `:sub:publish`), through `gradle` or a
  wrapper in any folder (`gradlew`, `./gradlew`, `../gradlew`).

Each is denied written plainly or with options, goals or subcommands before it
(`git -C DIR push`, `git subtree push`, `mvn clean deploy`,
`./gradlew :sub:publishToMavenLocal`).

The list stops the plain forms only. A Maven goal named in full
(`org.apache.maven.plugins:maven-deploy-plugin:3.1.1:deploy-file`),
`mvn release:perform` or `site:deploy`, a plugin's own publishing goal
(`nexus-staging:deploy`), or Gradle's abbreviated task names (`./gradlew pTML`)
are not caught: the principal's yes on a release is what stands between the
coordinator and an outside server.

Since the rules match words anywhere after the program, they also refuse, in
the coordinator's own shell, a Gradle command with any word starting with
`publish` (`--tests publishing.X`, `:publishing:build`, `-p publishing`) and
Maven's `-pl deploy`: run such a build through the lane's check. In the same
way a git command with `push` anywhere among its arguments is denied even when
it only reads (`git log --grep push`, `git grep push`): the coordinator reads a
repository through `hansa git`.

Claude Code also matches these rules after a plain `xargs` and after wrappers
such as `env`, `nice` or `timeout` (`xargs git push` is denied). Run through
another shell (`bash -c "git push"`), a script, or an `xargs` given options of
its own before the command (`xargs -n1 git push`), they are not caught; in
manual mode such a command asks the principal.

### What runs outside the worker, and what stays closed to the coordinator

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
branch on `retry --update`, a design session's commits, and the commit
`hansa lane remove --keep` makes) carries one
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

`hansa lane remove L --keep` puts what each of the lane's worktrees holds
(changes not committed, or commits no branch keeps) on a new branch
`hansa-kept/NAME-DATE` of its repository (NAME the worktree's folder: `L`, or
`L-design` for its design worktree), in one commit, `Left in worktree NAME when
lane L was removed`, with the identity the lane had before it was removed (else
the repository's own, else `hansa <hansa@localhost>`), and then removes the
worktree.

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
[`hansa/__init__.py`](../hansa/__init__.py)). So nothing hansa starts gets
them: not a release's command, the alert command, a design session, the
preview, git and its hooks, the follower that posts to a session, nor the
desk's account on a Linux server. Workers and the lanes' setups and checks
never had them, as hansa builds their environment itself. Only hansa's
notifying uses them, in the command run from the session: `CLAUDE_PID` says
which session asked for a task or request (the desk's account is given that
one alone), and `hansa notify on` hands `CLAUDE_CODE_MESSAGING_SOCKET` to its
follower on the follower's command line. How Claude Code runs
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
README's `ssh -L` line does. It refuses for `HANSA_ROLE` or `HANSA_TASK` as
before. `--file` (`hansa add`, `design`, `retry`, `lane brief`) refuses a file
in the desk's `.hansa/` or `secrets/`, wherever a link leads, and a hard link
to one of their files elsewhere; `--file -` refuses the same file redirected
to standard input (`< .hansa/web-token`), found by its path or, where that
cannot be told, its device and inode, while a pipe or a here-document is
read as before: they hold the
token, alerts waiting to go out with a release's one-time code, and the keys,
and a file's text would go into the desk where agents read it.

Nothing else hansa prints holds the token or a one-time code: `hansa serve`,
`hansa status` and `hansa explain` give the page's host and port only; the
page logs no requests and answers one without the token with a fixed
refusal; the event log, posts to a session and errors never carry either; a
release's code is kept as a keyed hash, and an alert command's output is not
kept when its alert held a code. Two places hold them in the clear, closed to
agents by the coordinator's permission rules (Read is denied there) and the
workers' sandbox: `.hansa/web-token`, and `.hansa/alerts.json` while an alert
with a code waits to go out. The coordinator runs outside any sandbox, so a
Bash command the principal approves could still read them; it should never be
asked to.
