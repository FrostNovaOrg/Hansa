# Who changes what

What the principal sets on the web page, and what the coordinator sets with `hansa`. What workers and
design sessions may touch is in [Workers and the sandbox](sandbox.md); the coordinator's own interface is
[hansa/card.md](../hansa/card.md).

## The principal

The principal, on the page: providers, models, endpoints, keys, effort
defaults, the models of the coordinator and of design sessions, prices,
whether the coordinator audits cost, the release targets that go out without
asking (each ticked on its own), the words no release may send out, and the
alert command ([Alerts](releases.md#alerts)). The page also shows what goes on, and asks the
principal about each release ([Releases](releases.md)).

## The coordinator

The coordinator, with `hansa`: tasks; the lanes (`hansa lane`: each one's
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

### The messages of hansa's commits

Each lane may also set the form of the messages hansa writes into its
repository: `commit_message` for the commit of a task's work (and the merge
of the base into a task's branch on `retry --update`), `merge_message` for a
task's merge into the base, each one line with `{task}`, `{title}` and
`{lane}` that holds `{task}` or `{title}`
(`hansa lane set L merge_message "Merge: {title}"`). Unset, they are
`t0001: <title>` and `Merge t0001: <title>`. A lane whose release goes to a
public repository usually leaves `{task}` out; the release's word check reads
every outgoing commit message all the same. With no `--title`, a task's
title is the first line of its text cut at 80 characters, perhaps within a
word; when a lane's form holds `{title}`, `hansa add` says so for a title it
cut, with the title that will be used: give one with `--title`.

### The identity of hansa's commits

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
