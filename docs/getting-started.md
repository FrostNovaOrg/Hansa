# Getting started

Run hansa on a Mac or Linux machine that stays on. You, the **principal**, own
logins, keys and page settings; the coordinator plans work and sends tasks to
workers. If you work as root, first follow the account setup in
[On a Linux server](linux-server.md#the-coordinator-as-root-the-desk-as-an-ordinary-account).

## Install the tools

Install these outside the project's worktree, available to the account running
the desk and on the `PATH` used to start `hansa serve`:

| Tool | Version |
|---|---|
| Python | 3.9 or newer; standard library only, nothing to build |
| Git | Required; no minimum declared. Git 2.32+ preserves global configuration when hiding Linux sandbox placeholders ([details](linux-server.md#what-a-lanes-commands-meet-in-linuxs-sandbox)). |
| [Claude Code](https://code.claude.com/docs/en/overview) | 2.1.290+ for the coordinator plugin; CI uses 2.1.307 |
| Node | 22.12.0+ for srt; CI uses 24.22.0 |
| srt (`@anthropic-ai/sandbox-runtime`) | 0.0.79 |
| tmux | Recommended, required for `hansa up` to start the coordinator; no version pinned |

With Node and npm installed:

```sh
npm install -g @anthropic-ai/claude-code@2.1.307 @anthropic-ai/sandbox-runtime@0.0.79
python3 --version
git --version
claude --version
node --version
npm ls -g --depth=0
```

Check srt's **package** version with npm; its CLI version string may differ.
[Tools and upgrades](sandbox.md#tools-and-upgrades) records the baseline and upgrade checks.

Workers require Claude Code's operating-system sandbox. macOS supplies it.
On Linux install `bubblewrap` and `socat` (Debian/Ubuntu:
`sudo apt-get install bubblewrap socat tmux`); unprivileged user namespaces must
work. Ubuntu 24.04+ AppArmor restrictions can block them. Follow
[Claude Code's Linux sandbox setup](https://code.claude.com/docs/en/sandboxing#set-up-linux-and-wsl2)
for your distribution. Test the actual lane below; installed packages alone do
not prove the sandbox works.

## Get the current code

Use **alpha**; `main` holds the older Bellhop version.

```sh
git clone --branch alpha https://github.com/FrostNovaOrg/Hansa.git ~/hansa
export PATH="$HOME/hansa/bin:$PATH"
hansa --version
```

Add that export to your shell profile too. Either put hansa's `bin` folder on
`PATH`, as above, or put a symbolic link to `bin/hansa` in a folder already on
`PATH`, for example `ln -s ~/hansa/bin/hansa /opt/homebrew/bin/hansa`.
Absolute and relative links, including chains, work even when the installation
path contains spaces. The launcher follows only file links, keeping directory
links such as `current/bin/hansa` so switching `current` changes future starts.
It stops after 40 file links and reports a loop or an overly long chain.

## Log in (the principal's step)

For a subscription, run `claude` and complete login as the account that will
run the desk, then exit. A new desk's provider `claude` uses that account's login.
Alternatively, set an Anthropic-compatible endpoint and key on the page below;
this needs no subscription login. The principal handles credentials, not an agent.

## Create and start the desk

Use your project's checkout at `~/shop/repo`. It needs at least one commit
before `hansa init` or `hansa lane add` can use it. For a new, empty project
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
open. Configure the page below from another terminal,
then run `hansa up` to start the coordinator. For remote use, keep this initial
server in tmux too (`tmux new -s shop-server`, then `hansa serve` in the desk).

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
The token is the page's key, including permission to approve releases. Run this
and server startup from a plain shell, not a Claude Code session: `hansa web`
refuses to expose the token inside a session.

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

For an empty project, ask for a small Python function and a unittest instead.
The coordinator sets the check, for example
`hansa lane set a check "python3 -m unittest discover"`, and the download hosts
with `hansa lane set a network HOST ...`. The default network list is empty;
list each required registry or download host. See [network lists](sandbox.md#a-lanes-network-list).

`hansa lane try a` runs the setup and check in the lane's sandbox. Confirm it
passes, and check that `hansa explain server` says `hansa serve tried srt` and
`it ran` for the server's sandboxed probe.
Workers also require their own sandbox probe to pass.
Without srt, a check says **could not run**: a machine setup problem, not the
task's failure. A configured setup cannot run either and stops the worker from
starting. Fix the tools on the server's PATH and restart the server; the
coordinator can then recheck before merging (`hansa merge ID --recheck`).

Follow progress on Status. The coordinator reads `hansa show ID` and
`hansa diff ID`; hansa commits the worker's changes and checks them. The first
task is finished when reviewed and merged into the lane's base branch.

## Work over SSH

On your computer, `ssh my-server`, then follow the setup above on that machine.
Install tmux there. `hansa up` keeps the coordinator open after SSH disconnects.
Detach with Ctrl+B then D; reconnect and use its printed attach command again
(including its `-L` or `-S` socket option). `hansa status` describes the session;
run `hansa up` to bring back a stopped one, continuing its conversation.

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
