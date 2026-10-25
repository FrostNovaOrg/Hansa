# The Codex adapter

An optional part of hansa, for **macOS only**: it lets a worker run OpenAI's Codex command line (`codex exec`)
instead of Claude Code, without changing hansa. hansa runs every worker with `claude -p`, and finds `claude` through
`HANSA_CLAUDE`; `bin/claude-or-codex` takes its place for one desk and looks at each call:

- **A worker run on the Codex provider** (`-p`, `--output-format json`, `--session-id`, and
  `ANTHROPIC_BASE_URL=http://codex.invalid`) runs `codex exec` in srt, inside the sandbox hansa gave the worker,
  and answers on stdout in the shape hansa reads from `claude -p --output-format json`.
- **hansa's provider check on it** (the same, with no `--session-id`) runs `codex exec` once, in srt, in a new
  empty folder of its own (below, "The provider check").
- **`--resume` on it** gets an error answer: resume is not supported.
- **A call on the Cursor provider** (`ANTHROPIC_BASE_URL=http://cursor.invalid`) goes to the Cursor backend when a
  desk turns it on (below, "The Cursor backend"); while it is off, to the real claude, as every other call.
- **Every other call** is `exec`ed to the real claude, unchanged: same arguments, stdin, environment, exit code.

The wrapper is perl because it must hand on the environment exactly: a shell adds `PWD`, `SHLVL` and `_`, and
python adds `LC_CTYPE` (macOS's `/usr/bin/python3` more). It passes the shim a snapshot of the environment it got,
then execs python. The logic is in `shim/hansa_codex_shim.py`: Python 3.9, standard library only, no hansa import.

**Why macOS only.** Codex runs with its own sandbox off, since macOS cannot nest sandboxes, so srt is the only
boundary, and some of its write denies are globs (below). On Linux srt drops every glob from `denyWrite`
(bubblewrap has none), so Codex could make and write a nested `.claude`: the adapter refuses to run a worker there.

## Setting it up

1. **Install codex outside `~/.codex`.** hansa's sandbox denies reading `~/.codex`, and the standalone install keeps
   its program under `~/.codex/packages`, where a sandboxed run cannot start it. Install it into a folder of its
   own, for example `npm install -g --prefix <codex tools> @openai/codex@0.160.0`, outside every folder workers may not
   read. The adapter refuses a codex under such a folder.
   Use the Node and srt baseline and upgrade checks in [Tools and upgrades](../../docs/sandbox.md#tools-and-upgrades).
2. **Give the adapter its own Codex home and log in there once:**
   `CODEX_HOME=<Codex home> <codex tools>/bin/codex login`. The home is a folder of its own, outside `~/.codex`
   and the worktrees, and not inside a folder workers may not read.
3. **Put that home on the principal's list of folders workers may not read** (Settings, "workers may not read"),
   so that Claude workers on other lanes cannot read the login. The adapter leaves the home's own entries out of
   srt's `denyRead` for a Codex run alone, and the run's log names them. An entry *above* the home is kept, and the
   run is refused: the adapter never opens more than the home itself.
4. **Copy `codex.env.example` to `codex.env`**, beside it, and replace the placeholders. hansa gives a worker a
   clean environment, so settings in `hansa serve`'s environment would not reach the adapter: it reads its
   environment first, then `codex.env` (or `trial.env`, an older name, while there is no `codex.env`). git ignores
   both, and the default `logs/` here.
5. **Start the desk with the adapter:** `HANSA_CLAUDE=<checkout>/contrib/codex/bin/claude-or-codex hansa serve`.
6. **Add a `compatible` provider** (for example `codex`) with base URL `http://codex.invalid`, a key of at least 20
   random characters, and any model name (the adapter uses `HANSA_CODEX_MODEL`), and put it in a lane's providers.
   hansa refuses to merge a change that holds a provider's key, checking changed files, images and other binary
   files included: a short or common string can turn up in one by chance.

| setting | meaning | default |
|---|---|---|
| `HANSA_CODEX_BIN` | the codex program | (required) |
| `HANSA_CODEX_HOME` | its `CODEX_HOME`, holding the adapter's login | (required) |
| `HANSA_CODEX_MODEL` | `-m` | `gpt-6.1-sol` |
| `HANSA_CODEX_EFFORT` | `model_reasoning_effort`; hansa's `--effort` overrides it; either is passed as it is | `high` |
| `HANSA_CODEX_LOGS` | where each run's `<session-id>.jsonl`, `.err`, `.thread` and `.srt.json` are kept | `logs/` here |
| `HANSA_CODEX_SRT` | srt | `srt` on the worker's PATH |
| `HANSA_REAL_CLAUDE` | the real claude | the first `claude` on PATH that is not the adapter, else `~/.local/bin/claude` |
| `HANSA_CODEX_CA_FILE` | the certificates Codex checks TLS against, as `CODEX_CA_CERTIFICATE`; set but empty, none | `/etc/ssl/cert.pem` when it exists |
| `HANSA_CODEX_WEB_SEARCH` | Codex's own web search, `-c web_search=…`: `disabled`, `cached` or `live`; anything else is refused | `disabled` |
| `HANSA_CODEX_DISABLE` | Codex features turned off, space-separated, one `--disable` each; set but empty, none | `apps plugins browser_use computer_use image_generation multi_agent hooks` |

## The Codex run

```
srt --settings <logs>/<sid>.srt.json -- /bin/sh -c '<CODEX_CA_CERTIFICATE>; <source the lane env>; exec "$@"' ... \
  codex exec --json --ignore-user-config --ignore-rules --ephemeral --strict-config \
    -C <worktree> -m <model> -c model_reasoning_effort="<effort>" \
    -c developer_instructions="<hansa's --append-system-prompt, as a TOML string>" \
    -c shell_environment_policy.set.TMPPREFIX="<tmp>/zsh" \
    -c analytics.enabled=false -c approval_policy="never" -c approvals_reviewer="user" \
    -c check_for_update_on_startup=false -c web_search="disabled" --disable apps ... --disable hooks \
    -s danger-full-access -o <tmp>/last-message.txt -
```

The task goes in on stdin, as hansa gives it. Why each part:

- **`-c` values are TOML basic strings**, since `-c` reads its value as TOML and the system prompt is full of quotes
  and newlines.
- **zsh's `TMPPREFIX` is in the run's temporary folder.** zsh makes a here-document's file under `TMPPREFIX`
  (`/tmp/zsh` by default), not `TMPDIR`, and srt refuses that, so `cat > f <<'EOF'` would fail. Claude Code does the
  same for its own commands. `TMPDIR` stays as hansa gives it.
- **`CODEX_CA_CERTIFICATE`**: macOS's own certificate check, Codex's default, does not complete inside srt. Not
  `SSL_CERT_FILE`, which would change every command's certificates. A lane env that sets it wins.
- **The lane's env and sandbox prelude** (`CLAUDE_ENV_FILE`, in `.hansa`, which the sandbox may not read) are
  copied into the worker's temporary folder (mode 600), sourced and deleted by the sh inside srt.
- **Codex's stdout is the run's log file, not a pipe.** Codex panics when a write to a non-blocking stdout fails
  with EAGAIN, as a full pipe makes it on a long line; a file never does. The adapter follows the file.
- **Its own configuration:** `--ignore-user-config` loads no `config.toml` (`notify`, `mcp_servers`, providers,
  project and hook trust) and no `environments.toml`, which can send commands to another machine; `--ignore-rules`,
  no exec-policy rules; `--ephemeral`, no session files. With `--strict-config` an unknown `-c` key or `--disable`
  feature fails at once, and the answer says `Codex refused its configuration`. The login still comes from
  `CODEX_HOME`. `multi_agent` is off because a sub-agent's tokens are not counted in `turn.completed`.
- The `ANTHROPIC_*` keys hansa sets for Claude are removed from Codex's environment.

**The turn ceiling.** Each `command_execution` and `file_change` counts once; past `--max-turns`, the adapter
stops codex and answers `error_max_turns`. **The time limit.** srt runs in a session of its own. On SIGTERM, SIGINT
or SIGHUP (hansa's ceiling, or a cancel) the adapter sends SIGTERM to srt's group and to every process found below
srt by parent (so commands codex put in groups of their own go too), and SIGKILL three seconds later, within
hansa's five-second grace. If the adapter is killed outright, a watchdog in a session of its own does the same.
A run killed by a signal the adapter did not send (the out-of-memory killer, say) gets no answer and exits
128 + n, as a killed claude does, so hansa runs it again.

**Live progress.** Worker runs atomically replace `CLAUDE_CONFIG_DIR/progress/<session-id>.json` with
`{"replies": <turn count>, "last": <event epoch seconds>, "note": "<latest agent message's first line>"}`.
The note starts empty. Writes follow count or note changes, otherwise at most every ten seconds while events
arrive. Write failures do not fail the run. Provider checks write no progress file. hansa shows this in
`status`, `show` and the page.

## What it closes, and what it cannot

srt's settings are built from the `--settings` hansa gives the worker, and checked before each run:

- **Writes** are allowed in the worktree, its own git folder in a linked worktree (git keeps the index there, as
  on a Claude lane), the worker's temporary folder, the lane's write list and the Codex home.
- **git's data** stays hansa's: every entry hansa's worker settings deny in the worktree's `.git`, the common
  folder and the worktree's own folder must be denied, or the run is refused. So Codex cannot commit.
- **Project settings:** `.claude`, `.codex` and `.agents` at the top of the worktree are denied whole, and below
  it, as globs, the folder names themselves and `.claude`'s settings, hooks and skills. Whatever Codex wrote there
  hansa would commit, and a later Claude Code run on the branch would load it and run its hooks outside the
  sandbox. srt's move-blocking keeps such a name from being made anywhere, by `mkdir`, link or rename. A worktree
  path with a glob character is refused, since srt would read it as one.
- **The Codex home:** `AGENTS.md`, `AGENTS.override.md`, `config.toml`, `environments.toml`, `hooks.json` and
  `rules` there may not be written. Codex reads `AGENTS.md`, `AGENTS.override.md` and `skills/` on every run, so a
  run is refused while either file exists or `skills/` holds anything but Codex's own `.system`; only names are
  looked at.
- **Reads:** hansa's no-read list, less the Codex home's own entries (above).
- **Network:** the lane's hosts, plus `chatgpt.com`, `auth.openai.com` and `api.openai.com`.
- **The web on OpenAI's side**, which srt cannot stop: Codex's own web search, on by default in codex, and, with a
  ChatGPT login, the `codex_apps` tools, whose `web_run` opens any page. Closing one leaves the other open, so
  both are off by default (`HANSA_CODEX_WEB_SEARCH`, and `apps` in `HANSA_CODEX_DISABLE`), as hansa's workers have
  no web tools.

What it cannot close:

- **Codex and the commands it runs share one sandbox**, so a run's own commands can read the Codex login and
  write the rest of the home: Codex refreshes its token into `auth.json` during a run. Claude workers cannot read
  it (setup, step 3).
- A task stopped at a ceiling has only Codex's last message.
- **None of hansa's tool rules** (`--allowedTools`, `--disallowedTools`, Read denials) and no browser
  (`--mcp-config` is ignored): srt's file and network rules are the boundary. `permission_denials` is always empty.
- **Cost is 0**; the tokens are counted from `turn.completed`. A failed run's usage is unknown and shown as zero.
- The watchdog looks every half second; a process that leaves and loses its parent between two looks can escape.
  Codex's unlimited reconnect is kept: it waits for the network in place, and hansa's time ceiling still holds.

## How failures read

A failure is read from `turn.failed`, else the last `error` event that is not a retry (`Reconnecting...`), else
codex's stderr, and classed on the whole text, so that hansa's own readings take it as they take Claude Code's:

| Codex says | hansa reads | hansa does |
|---|---|---|
| a usage limit, out of credits, spend cap, quota, 429 | quota, with Codex's reset time | requeues; the provider rests until the reset |
| model at capacity, high demand | overloaded (529) | tries once more |
| `content_filter` | refused | the lane's next provider |
| a dropped stream, idle timeout | connection dropped | tries once more on the same provider |
| refused, reset or timed-out connection; `workspace routing discovery failed`; a 5xx | connection error, server error | tries once more |
| a token that cannot be refreshed, 401, not logged in | login | rests the provider until someone signs in |
| 403 | key | rests the provider |
| 404 (no such model), a refused configuration, anything else | neither | the task fails, saying why |

Codex's reset time (`try again at 3:05 PM`, or with a date) is read by the adapter and passed on as `resets_at`,
which hansa reads. A model warning is said in the result and `codex_warnings`; it never fails a run alone.

## The provider check

hansa checks a provider (`hansa ping`, the page, `hansa serve` while one cannot be reached) with
`claude -p --output-format json --max-turns 1 --tools ""`, in `<desk>/.hansa/check`, a clean environment and
`Reply with the single word OK.` on stdin, and judges the answer as it judges Claude's. Run from a shell,
`hansa ping` uses the `claude` program the recorded `hansa serve` start used, from the `env` in
`<desk>/.hansa/serve-start.json` (here the adapter), not whichever `claude` that shell would find, and says so
first when the two differ. hansa gives a check no sandbox, so the adapter makes one: a new empty folder
`codex-check-…` in `TMPDIR`, which is Codex's working folder and `TMPDIR` and is removed afterwards however
the check ends; writes there and in the Codex home only;
no reads of the usual logins in the home folder (`~/.ssh`, `~/.aws`, `~/.claude`, `~/.codex` and the like), of
the folder hansa ran the check in, or of the desk's `.hansa`; the network, OpenAI's hosts alone. The rest is a
worker run's, with `--skip-git-repo-check` and no developer instructions or lane env. A usage limit or a failed
login rests the provider as for Claude. Its logs are `<logs>/check-<id>.*`.

## Parallel runs and the shared login

Several Codex runs, on one lane or several, may run at once. They share the Codex home and its one login. Codex
refreshes the access token near its expiry, and reads `auth.json` again first, refreshing only if it is unchanged,
so a run that finds the token already refreshed uses the new one. Two runs collide only when they refresh at the
same instant: the loser fails with "refresh token was already used" while the winner has written a good login.
Then the adapter runs `codex login status` itself, outside the sandbox, on the same home, within 20 seconds:

- **logged in:** the answer is a dropped connection, so hansa tries the task once more and the provider does not
  rest;
- **not logged in, failed, or no answer in time** (it is stopped): a failed login, saying what the status command
  did.

The adapter never reads `auth.json`, and of the status command looks only at its exit code and first line, passing
neither on: the line may name the login. Codex's other files in the home are shared as Codex itself shares them.

## The dry run

Once the login exists, `./dry-run.sh` makes a scratch repository and worktree and starts one real Codex run through
the adapter, as hansa starts a worker: the same command line, `--settings` from hansa's own functions, a clean
environment. It prints the answer, then the worktree. The task asks Codex to make `hello.txt`, try `git commit`,
and write project settings at the top and below it (`sub/.claude` by `mkdir`, by renaming a folder into place and
as a link). Afterwards `hello.txt` should be there with only the `init` commit, and no `.claude/`, `.codex/` or
`sub/.claude` of any kind (`sub/x` and `sub/y` may be left): srt must refuse them all. The report should name task
`dry-run`, which shows the developer instructions arrived; a key or feature this codex does not know ends the run
at once with `Codex refused its configuration`. Workers never run it: it needs the real Codex and its login. hansa's
checkout is the one the script is in, unless `HANSA_SRC` names another.

## The Cursor backend

A second backend, **off unless a desk turns it on**: a provider whose base URL is `http://cursor.invalid` runs
Cursor's headless command line (`cursor-agent -p`) in place of Codex (`shim/hansa_cursor.py`). It handles a worker
run, hansa's provider check (Cursor once, in an empty folder of its own) and `--resume` (an error answer). While
`HANSA_CURSOR_ENABLE` is not `on`, in the environment or else in `codex.env`, a call on that provider goes to the
real claude unchanged, as a call on any other provider does, whatever else of Cursor's is set.

**Not in srt, but in Cursor's own sandbox.** On macOS Cursor cannot run inside srt: it needs the login keychain
when it starts, and srt refuses it. So Cursor itself runs outside srt, and its commands run in Cursor's sandbox,
set up for each run with a `HOME` of the run's own (a link to the account's keychain, and `.cursor/sandbox.json`)
and a `CURSOR_CONFIG_DIR` (no MCP tools, no web fetch). Commands may read and write the workspace, write the run's
temporary folder and the lane's write list, read `HANSA_CURSOR_READ`, write no `/tmp`, and reach the lane's hosts
alone, never this machine. Cursor never gets `--force` or `--sandbox disabled`.

**On a copy of the worktree.** Cursor's sandbox opens the workspace's git folder for writing, and follows it
wherever a command points the workspace's `.git`. So Cursor never gets hansa's worktree: it works in a copy, with
a one-commit repository of its own, and the adapter brings its change back as a patch between two snapshots of the
copy, taken in a repository Cursor is never given; hansa commits it as it commits a Claude worker's. A failed
run's change, or one stopped at `--max-turns`, comes back too. What that closes:

- **The copy's git boundary.** For the run's length the adapter takes every write permission off the worktree's
  git folders (its own and the repository's common one), recording the modes in `hansa-cursor-hold/` there, and
  the last Cursor run on the repository to end puts them back (a watchdog does, if the adapter is killed). After
  the run it compares the repository's refs, HEADs and objects: if they moved, the run fails, bringing nothing back.
- **What a later run would obey.** A patch touching `.cursor`, `.claude`, `.codex` or `.agents`, or a submodule,
  brings nothing back. A run is refused while the worktree holds a `.cursor`, a `.claude/settings.json` (Cursor runs
  its hooks outside its sandbox) or a nested repository, or a folder above the copy holds a `CLAUDE.md`,
  `CLAUDE.local.md`, `AGENTS.md` or `.cursorrules`, which Cursor would give the model.
- **A changed Cursor.** A run is refused when `cursor-agent -v` is not `HANSA_CURSOR_VERSION`: the sandbox was
  checked on that version. What Cursor leaves running (a `cursorsandbox` per command, a detached `worker-server`)
  is stopped after every run, on a signal and past `--max-turns`.

What it does not close: Cursor itself runs outside any sandbox, as the desk's account. The write permissions the
hold takes off are that account's, so a command Cursor's sandbox lets write a git folder can put them back; the
after-run check then fails the run, but a commit that landed in the repository stays there.

**Setting it up.** Install Cursor's command line and log in once (`cursor-agent login`). With the real Cursor, run
`./dry-run-cursor.sh`: one run as hansa starts a worker, on a scratch repository, whose task asks Cursor to try
each way out (git in its workspace, the copy's `.git` pointed at the main repository five ways, writes outside it,
the network); it prints whether the main repository changed (it must not) and what of the run is left running
(nothing must be). Then set these in `codex.env`, and add a `compatible` provider with base URL
`http://cursor.invalid`, such a key and any model name, to a lane on a repository of its own (below):

| setting | meaning | default |
|---|---|---|
| `HANSA_CURSOR_ENABLE` | `on` turns the backend on; unset or anything else leaves it off | off |
| `HANSA_CURSOR_BIN` | the `cursor-agent` program | (required) |
| `HANSA_CURSOR_VERSION` | what `cursor-agent -v` says; change it only after the dry run passes on the new version | (required) |
| `HANSA_CURSOR_MODEL` | `--model`; for `grok-4.7` hansa's `--effort` picks `grok-4.7-low` … `-xhigh` (`max` as `xhigh`, none as `high`); others as given | `grok-4.7` |
| `HANSA_CURSOR_READ` | extra folders Cursor's commands may read, separated by `:` | none |
| `HANSA_CURSOR_LOGS` | where each run's `.jsonl`, `.err`, `.patch`, `.sandbox.json` and `.cli-config.json` are kept | `logs/` here |

**Limits.** The first four were seen when it was tried on a desk:

- While a Cursor run runs, the repository's git directory is read-only, so no lane on that repository can commit,
  switch branches or merge until it ends. Give a Cursor lane a repository of its own.
- hansa's own tests, which make git repositories, cannot run in Cursor's sandbox (it cannot create `.git/hooks`),
  so a Cursor worker on hansa leans on the lane's check.
- hansa sees no progress until the run ends.
- The usage Cursor reports can be less than what the run used. Cost is shown as 0.
- A crash that takes the watchdog too leaves the git directory read-only until a later Cursor run repairs the
  hold, or a `chmod` by hand.
- Failures are classed as Codex's are; of Cursor's own words, only those for a missing login have been seen.

## Tests

The adapter's tests run in hansa's suite (`python3 tests/run_parallel.py`), and alone with
`python3 -m unittest contrib.codex.tests`. They import hansa from this checkout (`HANSA_SRC` wins), so hansa's own
readings judge every answer, and use fakes for codex, srt, claude, cursor-agent and cursorsandbox, replaying
hand-written JSONL: no real Codex or Cursor, no network. A test that starts a Codex worker run, runs the fake Cursor
or works on a worktree for it skips off macOS. `HANSA_CODEX_TEST_*` and `HANSA_CURSOR_TEST_*` switches are for the
tests only.
