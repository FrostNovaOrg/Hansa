# Setting up Hansa with a person

Help a person set up Hansa on this machine, following these steps in order. Paths are relative to this file; "the guide" is `docs/getting-started.md`.

Reply in the person's language. They may not write code: say what each step does in a sentence before running it. Ask before anything that installs software or asks for an administrator's password.

1. If `hansa` is not installed, run `sh install.sh` from this checkout, or the guide's install line. It changes no shell profile: if `hansa` is not on the `PATH`, use its full path, `~/.local/bin/hansa`, and show the person this line to add themselves:

   ```sh
   export PATH="$HOME/.local/bin:$PATH"
   ```

2. Run `hansa doctor --json`. Go through each row whose `state` is `fails`: say what it means, offer its `fix`, run it if the person agrees, then run the doctor again. Do the same for a missing `tmux`: step 5 needs it. A row that needs a login or an administrator's password is the person's to do: tell them the exact command and wait.

3. The login is theirs: they run `claude auth login` in their own terminal. Never ask for a password, key or token in the conversation, and never read one. An endpoint with a key goes on the page, by the person.

4. Ask which project this is for. If they have none, or want to see it work first, run `hansa example` and follow what it prints. Otherwise run `hansa init DESK --repo CHECKOUT`, with a new folder beside their project's checkout, which needs a first commit.

5. Tell them to open a plain terminal. Say why here and not in this session: a server and a coordinator started inside another Claude Code session inherit nothing useful, and `hansa web` refuses to run in one.

   They run `hansa`, tick the desk with Space, press `s`, any key, then Enter, and answer yes to Claude Code's folder-trust question.

   If they are not at the machine, give them this line with the real path and HOST; `ssh HOST` gives a terminal for `hansa up`:

   ```sh
   ssh -t -L 8765:127.0.0.1:8765 -L 8766:127.0.0.1:8766 HOST '"$HOME/.local/bin/hansa"'
   ```

6. Tell them the page's address comes from `hansa web`, in the desk's folder in their own terminal, and carries a token that can approve releases: they keep it to themselves. Do not ask for it.

7. Stop there. The desk's coordinator takes over from this session. Tell them what to say to it first: what `hansa example` printed, or the guide's request under "Finish the first task".
