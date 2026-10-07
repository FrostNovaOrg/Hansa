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
(tunnel that port instead). On the page: add providers (a Claude Code login, or any
endpoint that speaks the Anthropic API, with its key), models, effort
defaults, prices. Then start the coordinator with `hansa coordinator`; it sets
up the lanes and how many tasks run at once with `hansa lane` and
`hansa concurrency`. A worker runs any command inside its sandbox, writing
only its worktree, its own temporary folder and the lane's write list; a
lane's allowed commands matter only to design sessions.

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
  whether the coordinator audits cost, the lanes whose releases go out without
  asking and the words no release may send out. The page also shows what goes
  on, and asks the principal about each release (below).
- The coordinator, with `hansa`: tasks; the lanes (`hansa lane`: each one's
  provider order, allowed commands (for design sessions), network and write lists, environment,
  check, preview and setup commands, limits, and the merge check's own command and limit,
  `merge_check` and `merge_timeout_min`, for a heavier command run only before a merge: unset, the
  merge check runs the check under `timeout_min`) and how many tasks run at once (`hansa concurrency`);
  each lane's standing instructions (`hansa lane brief`, kept in
  `lanes/<lane>/brief.md`); pause and resume; each lane's release command
  (`hansa lane set L release "git push origin main"`) and asking for a release
  (`hansa release L "note"`), never pushing or deploying by itself.
  Each lane may also set the form of the messages hansa writes into its
  repository: `commit_message` for the commit of a task's work (and the merge
  of the base into a task's branch on `retry --update`), `merge_message` for a
  task's merge into the base, each one line with `{task}`, `{title}` and
  `{lane}` that holds `{task}` or `{title}`
  (`hansa lane set L merge_message "Merge: {title}"`). Unset, they are
  `t0001: <title>` and `Merge t0001: <title>`. A lane whose release goes to a
  public repository usually leaves `{task}` out; the release's word check reads
  every outgoing commit message all the same.
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
  reading of the desk's keys and state, of any provider's Claude Code folder or
  `~/.claude`, of the logins, keys, tokens, browsers' profiles, messages and
  shell histories hansa knows under the home folder (`HOME_SECRETS` and, for
  macOS, `MAC_SECRETS` in `hansa/desk.py`: `~/.ssh`, `~/.aws`, Teleport, other
  AI tools and Anthropic's own tool settings in `~/.config/anthropic`, cloud
  tools, git and registry credentials, proxy clients' settings with their
  server passwords or subscription links, password stores and keyrings, the
  keychains, cookies, mail and messages under `~/Library`), or of
  the folders and files the principal lists on the page (Settings, "workers may
  not read": this machine's own folders of keys; Read is denied there to every
  Claude Code run hansa starts, the coordinator too), no network but the
  lane's network list (`hansa lane set L network ...`), and no git command that
  changes history. They do not commit: hansa commits each task's changes on its
  own branch, and merges only what the coordinator merges. The lane's setup,
  before the worker, and its check, after it, run in the same sandbox (with
  `srt`): the lane's network and write lists hold for them too, so a setup that
  downloads needs its registry on the network list. Design sessions, which the
  principal works in directly, and the coordinator run outside it.
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
  `CCACHE_DIR`. A lane's env reaches the lane's commands, in the sandbox
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
  `HOME`), no `PATH` entry that is empty or not a full path, and no place
  inside the desk. `hansa/card.md` says the same.
- Releases are the last step. hansa merges into a lane's base branch and stops
  there; a lane's release command (a push, a wait for CI such as `gh run watch
  --exit-status`, a deploy script) is how the base branch goes out. `hansa
  release L` records the request: the commit, the command, the note, the
  commits that go out (on no remote-tracking branch of the repository and not
  in the lane's last release; the whole history when it has no
  remote-tracking branch) and the files they change, a file the command names
  (such as `./deploy.sh`) marked. A word from the principal's list of words no
  release may send out, in the command line or in a line, a file name or a
  message of any outgoing commit (all are checked; matched within one line,
  in text read as UTF-8), refuses it. A command that holds a known key is
  refused when it is set. Otherwise the page shows it with Release and Refuse;
  a lane the principal lists as released without asking needs no answer while
  its repository, base branch and command are what they were when the list
  was saved, and while the desk is not paused. hansa serve runs the
  command only then, and only while the base branch and the command are still
  what the principal saw (else the request is void and the coordinator asks
  again): in the lane's repository at that commit, as the desk's account,
  outside the sandbox (it needs the account's logins and network), with
  hansa's own environment (not the lane's env), the lane's `timeout_min` as its
  limit, its output kept without keys in `.hansa/releases/`. One release at a
  time per repository, and none while a merge or merge check runs there, nor a
  merge while a release runs. The coordinator hears when the principal answers
  and when the release ends. `hansa release` lists them; `hansa explain lane L`
  shows the command and the last release.
- Workers see their checkout's own CLAUDE.md and settings, and hansa's instructions: not the desk's card,
  and not the settings, plugins, hooks or MCP servers of the account they run as.
- Nobody reads hansa's code to use it. If the card or a command's output is not
  enough, fix the card or the output.

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
costs nothing until something is written there); hansa never writes it again. A
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
- A worker's commands, and the lane's setup and check commands, run in the
  operating system's sandbox; the lane's preview command, design sessions and
  the coordinator do not, and run as the desk's user. Run a desk as a user, or on a machine, whose files you
  are content for that code to reach.
- hansa replaces the principal's keys wherever it shows or records text, and will not
  merge a change that holds one. It cannot see into compressed files, and the
  event log keeps what it recorded before a key was saved.
- What a worker leaves running is stopped when the worker ends, unless `hansa serve`
  was restarted in between.
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
