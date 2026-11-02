# Getting started

Run hansa on a Mac or Linux machine that stays on. You, the **principal**, own
logins, keys and settings; the coordinator plans tasks for workers. As root,
first follow
[On a Linux server](linux-server.md#the-coordinator-as-root-the-desk-as-an-ordinary-account).

## Install the tools

Install these outside the project's worktree, available to the account running
the desk and on the `PATH` used to start `hansa serve`:

| Tool | Version |
|---|---|
| Python | 3.9 or newer; standard library only, nothing to build |
| Git | Required; no minimum enforced. On Linux, Git 2.32+ recommended: preserves global configuration when hiding sandbox placeholders ([details](linux-server.md#what-a-lanes-commands-meet-in-linuxs-sandbox)). |
| [Claude Code](https://code.claude.com/docs/en/overview) | 2.1.290+ for the coordinator plugin; CI uses 2.1.307 |
| Node | 22.12.0+ for srt; CI uses 24.22.0 |
| srt (`@anthropic-ai/sandbox-runtime`) | 0.0.79 |
| tmux | Recommended, required for `hansa up` to start the coordinator; no version pinned |

With Node and npm installed:

```sh
npm install -g @anthropic-ai/claude-code@2.1.307 @anthropic-ai/sandbox-runtime@0.0.79
```

[Tools and upgrades](sandbox.md#tools-and-upgrades) records the baseline and upgrade checks.

Workers require Claude Code's operating-system sandbox. macOS supplies it.
On Linux install `bubblewrap` and `socat` (Debian/Ubuntu:
`sudo apt-get install bubblewrap socat tmux`); unprivileged user namespaces must
work. Ubuntu 24.04+ AppArmor restrictions can block them. Follow
[Claude Code's Linux sandbox setup](https://code.claude.com/docs/en/sandboxing#set-up-linux-and-wsl2)
for your distribution.

## Get the current code

Use **alpha**; `main` holds Bellhop.

```sh
git clone --branch alpha https://github.com/FrostNovaOrg/Hansa.git ~/hansa
export PATH="$HOME/hansa/bin:$PATH"
hansa --version
hansa doctor
```

Run `hansa doctor` after installing; an agent helping install runs it first.
It needs no desk, checks tools and sandbox operation, and changes nothing.
The login row fails until the principal logs in below; endpoint-only setups
can ignore that row. Rerun after fixes.
It reads no credentials, discards login output and removes its sandbox temporary
folder afterwards.

Rows give findings, requirements and fixes; notes do not fail. Exit 0 means no
failures; exit 1 means a failed row. `--json` gives `{"ok": ..., "rows": [...]}`
with `name`, `state`, `found`, `need`, `fix`; states are `ok`, `note`, `fails`,
`skipped`. `--lang zh|en` selects language as in `hansa menu`.
Commands time out after 15 seconds, with a short cleanup wait for inherited output.

Add `export PATH="$HOME/hansa/bin:$PATH"` to your shell profile too.
Either put hansa's `bin` folder on
`PATH`, as above, or put a symbolic link to `bin/hansa` in a folder already on
`PATH`, for example `ln -s ~/hansa/bin/hansa /opt/homebrew/bin/hansa`.

## Log in (the principal's step)

For a subscription, run `claude auth login` yourself as the account that will
run the desk. A new desk's provider `claude` uses that account's login.
Alternatively, set an Anthropic-compatible endpoint and key on the page below;
this needs no subscription login. The principal handles credentials, not an agent.

## Create and start the desk

Use your project's checkout at `~/shop/repo`. It needs a commit before
`hansa init` or `hansa lane add`. For a new, empty project
(replace the author placeholders with your details):

```sh
mkdir -p ~/shop
git init ~/shop/repo
git -C ~/shop/repo config user.name "Your Name"
git -C ~/shop/repo config user.email "you@example.com"
git -C ~/shop/repo commit --allow-empty -m "Initial commit"
```

Then:

```sh
hansa init ~/shop/desk --repo ~/shop/repo
cd ~/shop/desk
```

Use plain folder names; see [paths hansa refuses](sandbox.md#paths-hansa-refuses)
if a path is rejected.

Init creates lane `a` on the checkout's current branch. With a subscription
login, run `hansa up`: it starts the server, then the coordinator in tmux.

For an endpoint without a login, first run `hansa serve`, keeping that terminal
open. Configure the page below from another terminal, then run `hansa up`.
Over SSH, keep this initial server in tmux too (`tmux new -s shop-server`, then
`hansa serve` in the desk).

Attach using the coordinator's printed `attach:` command. Answer Claude Code's
folder-trust question yes so its allowed commands work.

**Without tmux, only the server starts.** Run `hansa coordinator` in another
terminal in `~/shop/desk`, or install tmux and run `hansa up` again. Keep just
one coordinator. `hansa up` leaves running processes alone; it does not restart them.

## Open the principal's page

From your own terminal in the desk, run:

```sh
hansa web
```

Open its printed address, including `?t=...`, and keep the terminal open.
The token is the page's key, including permission to approve releases.
Start the server and page from a plain shell: `hansa web` refuses to expose
the token inside a Claude Code session.

In **Settings → Providers**, add endpoints, default models and effort; use
**Save settings** and the separate **Save key** button. In **Coordinator**, select
its provider and model before starting it (or exit and start it again after saving).
In **Prices**, set USD per million tokens. New provider settings apply to new
runs; prices recalculate past usage too. `hansa ping` checks all providers with
a tiny request, or use the page's provider check button.

## Finish the first task

Ask the coordinator:

> Inspect lane a. Use the configured providers. Set a meaningful check and the network list
> needed for any dependency downloads. Try the lane first. Then make one small,
> testable improvement, review the result and check, verify if needed, and merge
> it when it holds. Do not release it.

For an empty project, ask for a Python function and unittest.
The coordinator sets the check, for example
`hansa lane set a check "python3 -m unittest discover"`, and the download hosts
with `hansa lane set a network HOST ...`. The default network list is empty;
list each required registry or download host. See [network lists](sandbox.md#a-lanes-network-list).

`hansa lane try a` runs the setup and check in the lane's sandbox. Confirm it
passes, and check that `hansa explain server` says `hansa serve tried srt` and
`it ran` for the server's sandboxed probe.
Workers also require their own sandbox probe to pass.
Without srt, setup and check commands say **could not run**: a machine setup
problem, not the task's failure; configured setup stops workers from starting.
Fix the tools on the server's PATH and restart the server; recheck before merging
(`hansa merge ID --recheck`).

Follow progress on Status. The coordinator reads `hansa show ID` and
`hansa diff ID`; hansa commits the worker's changes and checks them. The first
task is finished when reviewed and merged into the lane's base branch.

## Work over SSH

After a restart, open the desk menu from your computer, forwarding the page
ports you need (here 8765 and 8766):

```sh
ssh -t -L 8765:127.0.0.1:8765 -L 8766:127.0.0.1:8766 HOST /path/to/hansa/bin/hansa
```

Tick desks with Space, press `s` to start them, Enter to enter a coordinator,
or `w` for its page address. Ctrl+B then D returns from a coordinator; Ctrl+C
returns from the page. Nothing starts until you choose. `q` leaves work running.

On your computer, `ssh my-server`, then follow the setup above on that machine.
Install tmux there. `hansa up` keeps the coordinator open after SSH disconnects.
Detach with Ctrl+B then D; reconnect using its printed attach command
(including `-L` or `-S`). `hansa status` describes the session; `hansa up` brings
back a stopped one, continuing its conversation.

For the page, first read `hansa status` or `hansa explain server` on the server.
The port starts at 8765 but can change; it is saved in `.hansa/web-port`.
If the actual port is **8766**, run this from your own computer:

```sh
ssh -L 127.0.0.1:8766:127.0.0.1:8766 my-server '"$HOME/hansa/bin/hansa" --desk "$HOME/shop/desk" web'
```

Replace both 8766s with the actual port. Open the printed URL locally. Keep the
tunnel terminal open while using the page; closing it ends access. Keep the
server's default `127.0.0.1` binding; open no inbound network ports beyond SSH.
This works for a Mac mini or Linux server without exposing the page publicly.

## When something goes wrong

| What you see | Why | Fix |
|---|---|---|
| Older Bellhop files | Cloned `main` | Use the `alpha` clone above. |
| `No module named hansa` | Unreadable checkout | Ensure the desk account can read the checkout; either its `bin` on PATH or a launcher symlink works. |
| Init refuses the repository | No commit | Make its first commit, then retry. |
| Server runs, no coordinator | tmux absent | Install tmux and rerun `hansa up`, or run `hansa coordinator`. |
| Coordinator cannot run allowed commands | Folder not trusted | Answer Claude Code's folder-trust question yes. |
| Login fails | Wrong account or provider | Principal logs in as the desk account, or saves endpoint and key on the page. |
| `could not run`, `srt MISSING` / `srt CANNOT RUN` | Missing runtime, Node or working sandbox | Check versions and server PATH, fix Linux prerequisites, restart server, recheck. |
| `bwrap:` / task held | Sandbox cannot start or refuses settings | Read `hansa status`; fix namespaces/AppArmor or the named settings, restart server. |
| Downloads refused | Empty or incomplete lane network list | Coordinator adds the required hosts and retries the lane. |
| Page unreachable / key refused | Wrong port, closed tunnel, missing token or agent terminal | Check actual port; reopen tunnel and the full `hansa web` URL from your own terminal. |
