# Setting up Hansa with a person

You are an AI agent (Claude Code) on the machine where Hansa should live. A person asked you to install and set up
Hansa with them. They may never have used a terminal. Follow the rules and steps below in order.

Paths are relative to this file. "The guide" is [docs/getting-started.md](docs/getting-started.md). For a Chinese
reader, use [上手指南](docs/zh-CN/getting-started.md) when that file is installed; otherwise use the English guide.
Take facts from the guide and the pages it links, not from memory, and point the person to them rather than repeating long passages.
Before you name a `hansa` command or option, check it with `hansa <command> --help`.

## Rules

1. **Reply in the person's language** and in plain words. Explain what a term means the first time you use it.
2. **One question at a time.** Each question comes with the answer you recommend and why, in one sentence, so the
   person can answer just "yes".
3. **Show the plan before changing anything.** Once you know the answers to steps 2 to 4, show the whole plan: every
   command you will run, in order, each with what it does in plain words. Act only after the person agrees. If the plan
   has to change later, show only what changes, and wait for a yes again. Commands that only read (`hansa doctor`,
   `--help`, looking at files) need no plan.
4. **Keys are theirs.** Never ask for, show, read or type a key, password or token. Logins are done by the person: in
   their own terminal (`claude auth login` for Claude Code), or on Hansa's page (a provider's key). If they paste one
   into the conversation anyway, tell them not to, and do not repeat it.
5. **Do not edit shell start files** such as `~/.zshrc` or `~/.bashrc`. When `~/.local/bin` is not on the `PATH`, call
   Hansa by its full path, `~/.local/bin/hansa`, and give the person this line to add to their shell start file
   themselves:

   ```sh
   export PATH="$HOME/.local/bin:$PATH"
   ```

6. **Ask before installing anything** the machine lacks. Name what it is, what it is for and where it comes from (for
   example "Node.js, from nodejs.org or Homebrew"). Anything that asks for an administrator's password (`sudo`) is
   the person's to run, in their own terminal: give them the exact command and wait.
7. **When a step fails, stop.** Say what failed in plain words, what it means, and what the person can do. Do not try
   something else without asking. Never get past a refusal by switching off the sandbox, a permission check or any
   other safeguard; a refusal is something to report, not to work around.

## Steps

### 1. Install Hansa and check the machine

If `hansa --version` (or `~/.local/bin/hansa --version`) does not answer, install it with the command in the
[README's quick start](README.md#quick-start):

```sh
curl -fsSL https://raw.githubusercontent.com/FrostNovaOrg/Hansa/main/install.sh | sh
```

It puts Hansa in `~/.local/share/hansa` and `~/.local/bin/hansa`, needs no administrator, and changes no shell start
file. Keep what it prints: the `export PATH` line (rule 5) and the `ssh -t -L` line for step 6.

Then run `hansa doctor`. It changes nothing. Go through each row it reports as failing, one at a time: say what is
missing, offer its fix (rule 6), and run `hansa doctor` again after each fix. `hansa doctor --json` gives the same rows
in a form easy to read (`name`, `state`, `found`, `need`, `fix`). The guide's
[Install the tools](docs/getting-started.md#install-the-tools) lists what Hansa needs; tmux is recommended there, and
step 5 needs it.

The login row fails until the person logs in. That is theirs (rule 4): ask them to run `claude auth login` in their own
terminal, or, if they will use another provider through its key, to leave that row and enter the key on the page later
([Log in](docs/getting-started.md#log-in-the-principals-step)).

### 2. Choose the project

Ask which project Hansa should work on: a git repository on this machine. If the current folder is in one, recommend
it. For a new or empty folder, `hansa init` makes the repository and an empty first commit itself; a folder that
already holds files needs them committed first ([Create and start the desk](docs/getting-started.md#create-and-start-the-desk)).
The commit's author name and email are not secrets: ask for them if the repository has none set.

### 3. Choose where the desk goes

The desk is the folder holding the team's settings and tasks. Recommend a folder next to the repository, named after
it with `-desk` (for `~/shop`, `~/shop-desk`). Use plain folder names; see
[paths hansa refuses](docs/sandbox.md#paths-hansa-refuses). `hansa init` refuses a folder that already holds
files, the project's own among them; a desk inside a git repository it keeps out of that repository's commits with a
line in its `info/exclude`, and says so. It is created in the plan's step:

```sh
hansa init ~/shop-desk --repo ~/shop
```

This makes the first lane, `a`, on the repository's current branch. A lane is one working copy of the code with its
own rules.

### 4. Set up the first lane's check

The check is the command Hansa runs after every task to test the work. Look at the project's files to find its test
command: for example `package.json` (`scripts.test`), `pyproject.toml` or `setup.cfg` (pytest or unittest), `pom.xml`
(Maven), `Makefile`, or the README. Recommend the command you found, and why.

If the tests need dependencies installed first (`npm ci`, say), recommend that as the lane's setup command. List the
hosts the setup and check download from (such as `registry.npmjs.org` or `pypi.org` and `files.pythonhosted.org`): the
lane's network list is empty by default, so nothing is reached
([network lists](docs/sandbox.md#a-lanes-network-list)). For Java, see [Java and Maven](docs/java-and-maven.md).

npm also needs a cache folder it may write: give the lane one of its own on its write list and point
`npm_config_cache` at it in the lane's env ([write list](docs/sandbox.md#a-lanes-write-list-and-env)). `<desk>` is the
desk path's last two folders joined by `-` (for `/home/me/shop-desk`, `me-shop-desk`). For an npm project, for
example, these go in the plan:

```sh
hansa --desk ~/shop-desk lane set a check "npm test"
hansa --desk ~/shop-desk lane set a setup "npm ci"
hansa --desk ~/shop-desk lane set a network registry.npmjs.org
hansa --desk ~/shop-desk lane set a write ~/hansa-cache/<desk>/a/npm
hansa --desk ~/shop-desk lane set a env "npm_config_cache=$HOME/hansa-cache/<desk>/a/npm"
```

`hansa --desk ~/shop-desk lane try a` then runs the setup and check once in the lane's sandbox, with no worker. It
runs through the desk's server, so run it right after step 5 starts the desk, and show the person its result. If it
fails, `hansa --desk ~/shop-desk lane try a --log` prints the whole output; explain the cause, and change the plan
(rule 3) before trying again.
"Could not run" means a problem with the machine's tools, not with the project; see
[Finish the first task](docs/getting-started.md#finish-the-first-task) and the guide's table of problems.

### 5. Start the desk

Ask the person to open a plain terminal of their own (not this session) and go to the desk's folder.

With only an API endpoint (no subscription login), start the server first and set the provider on the page before
the coordinator: run `hansa serve` in that terminal and keep it running; set the provider, model and key on the page
from another terminal ([Open the principal's page](docs/getting-started.md#open-the-principals-page)); then run
`hansa --desk ~/shop-desk up`.

With a subscription login, run:

```sh
cd ~/shop-desk
hansa up
```

It starts the desk's server and then the coordinator, the agent they will talk to from now on, in a tmux session.
Say why in their terminal: the server must keep running after this session ends, and the coordinator must be a
session of its own. Instead of `hansa up` they can run `hansa` alone: a menu where Space picks the desk, `s` starts it
and Enter opens its coordinator
([the menu](docs/desk.md#starting-the-server-and-the-coordinator-again)).

To reach the coordinator, they use the `attach:` command `hansa up` prints. They answer yes to Claude Code's
folder-trust question. Ctrl+B then D leaves the coordinator running in the background. Without tmux only the server
starts; see [Create and start the desk](docs/getting-started.md#create-and-start-the-desk).

Once the server runs, do step 4's `hansa --desk ~/shop-desk lane try a` and show its result.
`hansa --desk ~/shop-desk status` shows what the desk is doing.

### 6. Using it from elsewhere

Tell the person that Hansa belongs on a machine that stays on, and that they reach it from their own computer over SSH
and tmux, which keeps the coordinator running when they disconnect. From their computer they run the `ssh -t -L` line
the installer printed for this machine (if Hansa was already installed, the one in the guide's
[Work over SSH](docs/getting-started.md#work-over-ssh)), with `YOUR-HOST` (`HOST` in the guide) replaced by this machine's name or address. It opens the
`hansa` menu here and carries the page through the tunnel.

The page is where they approve releases, enter providers' keys and follow the work. Its address holds a key: they get
it by running `hansa web` in the desk's folder, in their own terminal, or with `w` in the menu
([Open the principal's page](docs/getting-started.md#open-the-principals-page)). You must not run `hansa web`, open the
address or copy it; it refuses inside a Claude Code session anyway.

### 7. The first task

Suggest a first small task for them to give the coordinator, in their own words: something small and easy to check
in their project, such as fixing a typo, adding a test, or making one error message clearer. Remind them that the
coordinator plans it, a worker makes the change, Hansa runs the check, and nothing is released without their yes.
The guide's [Finish the first task](docs/getting-started.md#finish-the-first-task) has a fuller first request.

Leave the person the guide's installed path. For a Chinese reader, label it **上手指南** and use
`docs/zh-CN/getting-started.md` if that file is installed; otherwise use `docs/getting-started.md`.
For an English reader, use `docs/getting-started.md`.

Then stop: from here the coordinator takes over.
