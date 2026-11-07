# Getting started

Run hansa on an always-on Mac or Linux machine. You, the **principal**, own logins,
keys and settings; the coordinator plans tasks for workers. As root, first follow
[On a Linux server](linux-server.md#the-coordinator-as-root-the-desk-as-an-ordinary-account).

## Install the tools

Install these outside the worktree, on the desk account's `PATH` when starting `hansa serve`:

| Tool | Version |
|---|---|
| Python | 3.9 or newer; standard library only, nothing to build |
| Git | Required; no minimum enforced. On Linux, Git 2.32+ recommended: preserves global configuration when hiding sandbox placeholders ([details](linux-server.md#what-a-lanes-commands-meet-in-linuxs-sandbox)). |
| [Claude Code](https://code.claude.com/docs/en/overview) | 2.1.306+ for the coordinator plugin; CI uses 2.1.307 |
| Node | 22.12.0+ for srt; CI uses 24.22.0 |
| srt (`@anthropic-ai/sandbox-runtime`) | 0.0.79 |
| tmux | Recommended, required for `hansa up` to start the coordinator; no version pinned |

With Node and npm installed:

```sh
npm install -g @anthropic-ai/claude-code@2.1.307 @anthropic-ai/sandbox-runtime@0.0.79
```

[Tools and upgrades](sandbox.md#tools-and-upgrades) records the baseline and upgrade checks.

Workers require Claude Code's operating-system sandbox. macOS supplies it.
On Linux, install `bubblewrap` and `socat` (Debian/Ubuntu:
`sudo apt-get install bubblewrap socat tmux`); unprivileged user namespaces must
work. Ubuntu 24.04+ AppArmor restrictions can block them. Follow
[Claude Code's Linux sandbox setup](https://code.claude.com/docs/en/sandboxing#set-up-linux-and-wsl2)
for your distribution.

## Install hansa

With Git and Python 3.9+, run:

```sh
curl -fsSL https://raw.githubusercontent.com/FrostNovaOrg/Hansa/main/install.sh | sh
```

It clones into `~/.local/share/hansa`, links `~/.local/bin/hansa`, needs no
administrator rights and leaves your shell profile unchanged.

To update, pause the desk, wait until running work finishes, rerun the installer,
then restart and resume ([upgrade steps](concepts.md#upgrading-and-going-back)).
To remove hansa (desks stay where they are):

```sh
rm -rf ~/.local/share/hansa ~/.local/bin/hansa
```

After installing, run `hansa doctor`; an agent helping with installation runs it first.

```sh
hansa doctor
```

It needs no desk and changes nothing: it checks tools and sandbox operation
without reading credentials, discards login output and removes its temporary
sandbox folder.
The login row fails until the principal logs in as described below; endpoint-only
setups can ignore that row. Rerun after fixes.

Rows give findings, requirements and fixes; notes do not fail. Exit codes: `0`
for no failures, `1` for a failed row. `--json` gives `{"ok": ..., "rows": [...]}`
with `name`, `state`, `found`, `need`, `fix`; states are `ok`, `note`, `fails`,
`skipped`. `--lang zh|en` selects language as in `hansa menu`.
Commands time out after 15 seconds, with a short cleanup wait for inherited output.

## Log in (the principal's step)

For a subscription, run `claude auth login` as the desk account; a new desk's
provider `claude` uses that login.
Alternatively, set an Anthropic-compatible endpoint and key on the page below;
this needs no subscription login. The principal alone handles credentials.

## Try the sample first

Run `hansa example` to make a sample task to try on your own login; the command
starts nothing and calls no model.

## Create and start the desk

`~/shop/repo` needs a commit before `hansa init` or `hansa lane add`.
For an empty project (replace the author details):

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

Use plain folder names; if one is rejected, see [paths hansa refuses](sandbox.md#paths-hansa-refuses).

Init creates lane `a` on the checkout's current branch and registers the stopped
desk for this account, so `hansa` shows it immediately. With a subscription login,
`hansa up` starts the server, then the coordinator in tmux.

For endpoint-only setups, keep `hansa serve` running. From another terminal,
configure the page as below, then run `hansa up`. Over SSH, use tmux
(`tmux new -s shop-server`, then `hansa serve`).

Attach using the coordinator's printed `attach:` command. Answer Claude Code's
folder-trust question yes so its allowed commands work.

**Without tmux, only the server starts.** Run `hansa coordinator` in another
terminal in `~/shop/desk`, or install tmux and rerun `hansa up`. Run only one
coordinator; `hansa up` does not restart running processes.

## Open the principal's page

In your terminal, go to the desk and run:

```sh
hansa web
```

Open its printed address, including `?t=...`, and keep the terminal open.
The token grants page access, including release approval.
Start the server and page from a plain shell: `hansa web` refuses to expose
the token inside a Claude Code session.

In **Settings → Providers**, add endpoints, default models and effort; use
**Save settings** and the separate **Save key** button. In **Coordinator**, select
its provider and model before starting it, or restart it after saving.
In **Prices**, set USD per million tokens. New provider settings apply to new
runs; price changes also recalculate past usage costs. `hansa ping` checks all
providers with a tiny request; the page also has a provider check button.

## Finish the first task

Ask the coordinator:

> Inspect lane a. Use the configured providers. Set a meaningful check and the network list
> needed for any dependency downloads. Try the lane first. Then make one small,
> testable improvement, review and check the result, verify if needed, and merge
> it when it holds. Do not release it.

For an empty project, ask for a Python function and unittest.
The coordinator sets the check, for example
`hansa lane set a check "python3 -m unittest discover"`, and the download hosts
with `hansa lane set a network HOST ...`. The default network list is empty;
list each required registry or download host. See [network lists](sandbox.md#a-lanes-network-list).

`hansa lane try a` runs the setup and check in the lane's sandbox. Confirm that
it passes and that `hansa explain server` reports `hansa serve tried srt` and
`it ran` for the server's sandboxed probe. Workers need their own sandbox probe
to pass too.
Without srt, setup and check commands say **could not run** because of a machine
setup problem; the task itself has not failed. If configured setup cannot run,
workers do not start. Fix the tools on the server's PATH, restart the server, and
recheck before merging (`hansa merge ID --recheck`).

Follow progress on Status. The coordinator reads `hansa show ID` and
`hansa diff ID`; hansa commits the worker's changes and checks them. The task is
finished when reviewed and merged into the lane's base branch.

## Work over SSH

After a restart, open the desk menu from your computer, forwarding the page
ports you need (here 8765 and 8766):

```sh
ssh -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes -t -L 8765:127.0.0.1:8765 -L 8766:127.0.0.1:8766 HOST '"$HOME/.local/bin/hansa"'
```

Select desks with Space, press `s` to start them, Enter to open a coordinator,
or `w` for its page address. Ctrl+B then D detaches from a coordinator; Ctrl+C
leaves the page. Nothing starts until you choose. `q` quits and leaves work running.

Install tmux on the server; `hansa up` keeps the coordinator open after SSH disconnects.
Reconnect using its printed attach command (including `-L` or `-S`). `hansa status`
describes the session; `hansa up` brings back a stopped one, continuing its conversation.

Find the page's port (initially 8765) through `hansa status`,
`hansa explain server`, or `.hansa/web-port` on the server.
If the actual port is **8766**, run this from your own computer:

```sh
ssh -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes -L 127.0.0.1:8766:127.0.0.1:8766 my-server '"$HOME/.local/bin/hansa" --desk "$HOME/shop/desk" web'
```

Replace each `8766` with the actual port. Open the printed URL locally and keep
the tunnel terminal open; closing it ends page access. Keep the server's default
`127.0.0.1` binding; open no inbound network ports beyond SSH.

`ServerAliveInterval=30` checks every 30 seconds; `ServerAliveCountMax=3`
disconnects after three unanswered checks. `ExitOnForwardFailure=yes` exits if
forwarding fails. Install `autossh` locally and run it separately to reopen
dropped tunnels automatically:

```sh
autossh -M 0 -N -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes -L 127.0.0.1:8766:127.0.0.1:8766 my-server
```

## When something goes wrong

| What you see | Why | Fix |
|---|---|---|
| `No module named hansa` | Unreadable checkout | Ensure the desk account can read the checkout. |
| Init refuses the repository | No commit | Make its first commit, then retry. |
| Server runs, no coordinator | tmux absent | Install tmux and rerun `hansa up`, or run `hansa coordinator`. |
| Coordinator cannot run allowed commands | Folder not trusted | Answer Claude Code's folder-trust question yes. |
| Login fails | Wrong account or provider | Principal logs in as the desk account, or saves endpoint and key on the page. |
| `could not run`, `srt MISSING` / `srt CANNOT RUN` | Missing runtime, Node or working sandbox | Check versions and server PATH, fix Linux prerequisites, restart server, recheck. |
| `bwrap:` / task held | Sandbox cannot start or refuses settings | Read `hansa status`; fix namespaces/AppArmor or the named settings, restart server. |
| Downloads refused | Empty or incomplete lane network list | Coordinator adds the required hosts and retries the lane. |
| Page unreachable / key refused | Wrong port, closed tunnel, missing token or agent terminal | Check actual port; reopen tunnel and the full `hansa web` URL from your own terminal. |
