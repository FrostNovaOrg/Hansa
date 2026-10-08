"""The `hansa` command. Every subcommand is a thin wrapper over desk, run, views and cost."""
from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import account, alert, build, cost, installed_text, notes, notify, release, views
from .desk import (EFFORTS, Desk, allow_fix, default_lane, default_settings, deny_parts, no_read_parts, stop_text, home_secrets, identity_problem, json_text,
                   lane_targets, release_files_in, merge_message, message_fallback, MESSAGE_FIELDS, message_problem, now, title_forms, read_json, read_split, refused_rule, resolved, stored, write_json,
                   argv_rules_problem, path_rule_problem,
                   PROJECT_INTRO, answer_name)
from .run import (DESIGN_ON_LINUX, VERIFY_TASK, Dispatcher, base_moved, branch_holds, check_provider, close_verification, conflict_advice,
                  conflict_places, conflicted_files, finish_merge_check, git, identity_env, keys_in_change, lane_holder,
                  lane_identity,
                  MARKERS_ON_PURPOSE, beat, markers_in_change, markers_text, merge_check_ahead, merge_command, merge_minutes, merge_needs_check, merge_target,
                  not_looking, own_merge_check, proc_start, provider_env, pulse,
                  quiet_text, read_try, record_serve_code, record_serve_locale, server_state, stop, try_file, try_log, try_srt_at_start, unverified_note, waiting_reason, write_try,
                  is_local, mac_loopback_listeners)

CARD = Path(__file__).with_name("card.md")
HELPERS = Path(__file__).with_name("agents")  # the coordinator's helpers: scout, reviewer, operator
NO_SIGNATURE = Path(__file__).resolve().with_name("no-signature")  # hansa git's gpg: checks no signature
RETIRED_HELPERS = ("verifier.md", "verifier-high.md")  # read-only checks, replaced by hansa verify and the reviewer
# Claude Code's own general-purpose helpers run on the coordinator's model and effort with no
# limits of their own; the desk's helpers do the same jobs at a set effort instead.
BUILTIN_HELPERS = ("general-purpose", "Explore", "Plan", "claude", "statusline-setup", "claude-code-guide")
ON_MAC = sys.platform == "darwin"


def answer_lane_refused(name: str) -> str:
    """Why a lane may not be named yes or no (lane add, init --lane)."""
    return (f"hansa: {name} cannot name a lane: yes and no (in any case) are kept for hansa release yes|no ID CODE, "
            "the principal's answer; nothing was changed")


def init_desk(root: Path, repo: str = "", lane: str = "a", base: str = "") -> Desk:
    if answer_name(lane):
        raise SystemExit(answer_lane_refused(lane))
    desk = Desk(root)
    if desk.settings_path.exists():
        raise SystemExit(f"hansa: {desk.root} already has a desk")
    for x in dict.fromkeys([str(desk.root), str(desk.given)]):  # every rule for the desk's folders starts with it
        if path_rule_problem(x):
            raise SystemExit(f"hansa: {path_rule_problem(x)}; no desk was made")
    if repo:  # looked at before anything is made, so a refusal leaves no half-made desk
        if not resolved(repo):
            raise SystemExit(f"hansa: cannot follow the links of {repo} to see where it leads; no desk was made")
        repo = resolved(repo)
        theirs = account.foreign_repo(repo)
        if theirs:
            raise SystemExit(f"hansa: {theirs}; no desk was made")
        if path_rule_problem(repo):
            raise SystemExit(f"hansa: {path_rule_problem(repo)}; no desk was made")
        base = lane_base(repo, base, "no desk was made")
    for d in (desk.root, desk.tasks_dir, desk.lanes_dir, desk.state_dir, desk.notes_dir):
        d.mkdir(parents=True, exist_ok=True)
    desk.secrets_dir.mkdir(mode=0o700, exist_ok=True)
    s = default_settings()
    if repo:
        s["lanes"][lane] = default_lane(repo, base)
        (desk.lanes_dir / lane).mkdir(exist_ok=True)
        (desk.lanes_dir / lane / "brief.md").write_text("", encoding="utf-8")
    write_json(desk.settings_path, s)
    (desk.root / "CLAUDE.md").write_text(CARD.read_text(encoding="utf-8"), encoding="utf-8")
    if not desk.project_path.exists() and not desk.project_path.is_symlink():  # made once; then only hansa notes archive writes it
        desk.project_path.write_text(PROJECT_INTRO, encoding="utf-8")
    (desk.root / ".claude").mkdir(exist_ok=True)
    write_json(desk.root / ".claude" / "settings.json", {"permissions": permissions(desk)})
    desk.event("desk_created", repo=repo, lane=lane if repo else "")
    return desk


def lane_base(repo: str, base: str, nothing: str) -> str:
    """A new lane's base: base, or else the branch the checkout is on; refused in one line when it is not a git
    checkout or has no commit yet (a lane's tasks start from a commit). nothing: what was not done, said after."""
    try:
        if git("rev-parse", "--is-inside-work-tree", cwd=repo) and not subprocess.run(
                ["git", "rev-parse", "-q", "--verify", "HEAD^{commit}"], cwd=repo, capture_output=True).stdout.strip():
            raise SystemExit(f"hansa: {repo} has no commit yet; make a first commit there (git commit), then run "
                             f"this again; {nothing}")
        return base or git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo)
    except (RuntimeError, OSError) as e:
        raise SystemExit(f"hansa: {repo} is not a git checkout ({e}); {nothing}")


def desk_spellings(desk: Desk) -> list:
    """The desk's path, and the other spellings that lead to it the coordinator may write: as it was given, and on
    macOS without /private in front (/tmp/desk for /private/tmp/desk), when they lead to the same folder."""
    out = [str(desk.root)]
    short = str(desk.root).removeprefix("/private") if str(desk.root).startswith("/private/") else ""
    for x in (str(desk.given), short):
        if x and x not in out and (resolved(x) or x) == str(desk.root):
            out.append(x)
    return out


def permissions(desk: Desk) -> dict:
    """The coordinator's Claude Code permission rules in the desk."""
    r = str(desk.root).lstrip("/")
    # lane briefs change through `hansa lane brief`: on a server the coordinator's own tools run as root
    guarded = [f"//{r}/settings.json", f"//{r}/secrets/**", f"//{r}/tasks/**", f"//{r}/.hansa/**", f"//{r}/events.jsonl",
               f"//{r}/lanes/**"]
    # an Edit rule covers every file-editing tool (Write too); //path is absolute. Claude Code allows a write only
    # when every path it resolves to matches, so a link at notes/ leads nowhere without asking; the denials still win.
    # A desk reached through a link (/tmp on macOS is /private/tmp) gets the rule for each spelling: a path written
    # the short way matches only with both
    return {"allow": ["Bash(hansa:*)", *(f"Edit(//{x.lstrip('/')}/notes/**)" for x in desk_spellings(desk))],
            "deny": [f"Read(//{r}/secrets/**)", f"Read(//{r}/.hansa/**)"] + [f"Edit({g})" for g in guarded]
            + [f"Bash({c}:*)" for c in NO_PUBLISH] + [f"Bash({r})" for r in no_publish_with_options()]}


# What the coordinator may not run from its own shell, whoever is at the session and whatever its mode: pushing
# and publishing. A release goes out through hansa release, which runs the lane's command itself after the
# principal's yes; the coordinator reads a repository through hansa git.
NO_PUBLISH = ("git push", "git send-email", "git svn dcommit", "gh release", "gh pr merge", "gh repo", "npm publish",
              "pnpm publish", "yarn publish", "yarn npm publish", "twine upload", "uv publish", "poetry publish",
              "cargo publish", "gem push", "docker push", "podman push")
# Maven's and Gradle's publishing, run by the tool or by a wrapper in any folder (../mvnw, sub/gradlew), with goals,
# tasks or options before it: deploy and the deploy plugin's goals (deploy:deploy-file); publish, publishTo... and a
# project's own (:sub:publish)
BUILD_PUBLISH = {("mvn", "mvnw", "*/mvnw"): ("deploy", "deploy *", "deploy:d*"),
                 ("gradle", "gradlew", "*/gradlew"): ("publish*", "*:publish*")}


def no_publish_with_options() -> list:
    """NO_PUBLISH with options between the program and the rest (git -C repo push, mvn -B deploy), and
    BUILD_PUBLISH: Claude Code reads a * anywhere in a Bash rule as any text, the whole command matched. Only a
    lone trailing " *" may also match nothing, so `git * push *` needs `git * push` beside it for a push without
    arguments. The text between may be a subcommand too, so `git * push` denies git subtree push, git submodule
    foreach git push and git lfs push, and a read with push among its arguments as well (git log --grep push):
    the coordinator reads a repository through hansa git. Claude Code matches them after a plain xargs and
    wrappers such as env, nice or timeout too; they stop nothing run through another shell (bash -c), a script,
    or an xargs with options of its own (xargs -n1 git push)."""
    return ([r for c in NO_PUBLISH for r in (c.replace(" ", " * ", 1), c.replace(" ", " * ", 1) + " *")]
            + [f"{p}{between}{g}" for ps, gs in BUILD_PUBLISH.items() for p in ps for g in gs
               for between in (" ", " * ")])


def _text(args) -> str:
    if getattr(args, "file", None) == "-":  # on standard input: root hands a file's text to the desk's account so
        return sys.stdin.read()
    if getattr(args, "file", None):
        f = Path(args.file)
        if not f.is_file():
            raise SystemExit(f"hansa: {args.file} " + ("is a folder, not a file" if f.is_dir() else "does not exist")
                             + "; nothing was changed")
        try:
            return f.read_text(encoding="utf-8")
        except OSError as e:
            raise SystemExit(f"hansa: cannot read {args.file} ({e.strerror or e}); nothing was changed")
        except UnicodeDecodeError:
            raise SystemExit(f"hansa: {args.file} is not UTF-8 text; nothing was changed")
    return " ".join(args.text)


def cmd_init(a):
    if answer_name(a.lane):  # before anything is made, for another account too
        raise SystemExit(answer_lane_refused(a.lane))
    if a.user:
        _init_for(a)
    desk = init_desk(Path(a.dir), a.repo or "", a.lane, a.base or "")
    print(f"desk ready at {desk.root}")
    print("next: start `hansa serve` there, open the page with `hansa web` and add providers and keys; "
          "the coordinator adds lanes with `hansa lane add` and queues work with `hansa add`.")


def _init_for(a) -> None:
    """hansa init DIR --user ACCOUNT: make DIR, give it to the account, and run the rest of init as it.
    Returns only when hansa already runs as that account."""
    if not account.LINUX:
        raise SystemExit("hansa: --user is for a Linux server, where root gives the desk to an account of its own")
    try:
        pw = account.pwd.getpwnam(a.user)
    except KeyError:
        raise SystemExit(f"hansa: there is no account {a.user}; make it first: useradd --system --create-home {a.user}")
    if account.uid() == pw.pw_uid:
        return
    if account.uid() != 0:
        raise SystemExit(f"hansa: only root can give a desk to the account {a.user}")
    # not resolved and nothing followed: the account may have put a link there, and root would give its target away
    root = Path(os.path.abspath(a.dir))
    refuse = f"hansa: {root} must be a new or an empty folder for the desk; nothing was changed"
    if root.is_symlink():
        raise SystemExit(refuse)
    root.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.mkdir(root)
    except FileExistsError:
        if root.is_symlink() or not root.is_dir() or any(root.iterdir()):
            raise SystemExit(refuse)
    os.chown(root, pw.pw_uid, pw.pw_gid, follow_symlinks=False)
    argv = ["init", str(root), "--lane", a.lane] + (["--repo", a.repo] if a.repo else []) \
        + (["--base", a.base] if a.base else [])
    account.switch(a.user, argv, root)


def _wait_for(desk: Desk, tid: str) -> str:
    """What to do while a task runs on by itself: nothing, in a session hansa tells when it ends; else hansa wait."""
    return notify.wait_step(desk, tid, True) if notify.hears_of(desk, tid) else f"Wait for it: hansa wait {tid}"


def _runs_when(desk: Desk, tid: str, base: str) -> str:
    """When a merge check just queued runs: after the one ahead of it on the same repository and base branch,
    if any, and when its lane is free."""
    m = desk.meta(tid)
    ahead = views.merge_check_waits_for(desk, m)
    return (f"One merge check runs at a time on {base}, so it waits for {ahead}, then runs when lane {m['lane']} is "
            f"free." if ahead else f"It runs when lane {m['lane']} is free.")


def _waits(desk: Desk, tid: str) -> str:
    """The line for a task just queued that no provider of its lane can start now, in hansa status's words;
    '' when one can."""
    m = desk.meta(tid)
    if m.get("kind") == "design":
        return ""
    why = waiting_reason(desk, desk.settings(), m, set())
    return f"\n{tid} waits: {why}" if "no provider ready" in why else ""


def cmd_add(a):
    desk = Desk.find(a.desk)
    s = desk.settings()
    if a.browser and not s.get("browser"):
        raise SystemExit("hansa: this desk has no browser set up (the principal sets one on the page); nothing was queued")
    tid = desk.new_task(_text(a), lane=a.lane or "", effort=a.effort or "", model=a.model or "",
                        title=a.title or "", max_turns=a.max_turns or 0, browser=a.browser or None,
                        **notify.recorded(notify.session_here()))
    if a.lane and a.lane not in s["lanes"]:
        print(f"note: there is no lane {a.lane} yet; the task waits until it is added: hansa lane add {a.lane} --repo PATH")
    if a.model:
        print("note: --model applies only when a Claude subscription provider runs the task; other providers use"
              f" their own model. Which provider runs it follows the lane's order: hansa explain lane {a.lane or 'L'}")
    first = (desk.tasks_dir / tid / "task.md").read_text(encoding="utf-8").splitlines()[0]
    forms = title_forms(s["lanes"].get(a.lane)) if not a.title and len(first) > 80 else []
    if forms:  # the title a form puts in commits, which a public repository shows
        print(f"note: lane {a.lane}'s {' and '.join(forms)} {'put' if len(forms) > 1 else 'puts'} the task's title in its commits; with no --title it"
              f" is the first line of the text, here cut at 80 characters: {json.dumps(desk.meta(tid)['title'], ensure_ascii=False)}."
              f" To give one, cancel it (hansa cancel {tid}) and add it again with"
              f" --title \"...\"")
    print(f"{tid} queued" + (f" for lane {a.lane}" if a.lane else "")
          + (f". {_wait_for(desk, tid)}; hansa show {tid} says where it is" if notify.hears_of(desk, tid)
             else f". Follow it with: hansa show {tid}") + _waits(desk, tid))


def cmd_design(a):
    if account.LINUX:
        raise SystemExit(f"hansa: {DESIGN_ON_LINUX}")
    desk = Desk.find(a.desk)
    s = desk.settings()
    lane = a.lane or (next(iter(s["lanes"])) if len(s["lanes"]) == 1 else "")
    if lane not in s["lanes"]:
        raise SystemExit("hansa: say which lane the design session is for: --lane L" if not lane
                         else f"hansa: there is no lane {lane}")
    with desk.lock():
        open_ = [m["id"] for m in desk.tasks() if m.get("kind") == "design" and m.get("lane") == lane
                 and m["state"] in ("queued", "running")]
        if open_:
            raise SystemExit(f"hansa: lane {lane} already has design session {open_[0]} open; one at a time per lane "
                             f"(hansa show {open_[0]})")
        tid = desk.new_task(_text(a), kind="design", lane=lane, title=a.title or "",
                            browser=bool(s.get("browser")) or None, **notify.recorded(notify.session_here()))
    why_not = not_looking(desk)  # as the waiting lines say it (run.waiting_reason)
    opens = (f"hansa opens the design session once `hansa serve` gets through its rounds; {why_not}. Then it"
             if why_not else "hansa opens the design session in seconds and")
    print(f"{tid} queued: {opens} asks the principal to join it. "
          f"It comes back for review when they are done: {notify.wait_step(desk, tid)}")


def cmd_status(a):
    st = views.status(Desk.find(a.desk))
    print(json.dumps(st, ensure_ascii=False, indent=1) if a.json else views.status_text(st))


def cmd_show(a):
    desk = Desk.find(a.desk)
    print(views.report_text(desk, a.id) if a.report else views.check_output_text(desk, a.id) if a.check
          else views.show_text(desk, a.id, full=a.full))


def cmd_diff(a):
    print(views.diff_text(Desk.find(a.desk), a.id, a.stat, a.paths, a.new))


# `hansa git LANE ...`: git commands that read, in a lane's repository or worktree, as the desk's account.
GIT_READS = ("log", "show", "diff", "status", "grep", "blame", "ls-files", "ls-tree", "cat-file", "rev-parse",
             "rev-list", "merge-base", "shortlog", "describe", "name-rev", "branch", "tag")
# options that write a file or run a program; git takes a long option by any unambiguous start of it
GIT_REFUSED_LONG = ("--output", "--open-files-in-pager", "--ext-diff", "--textconv", "--filters", "--exec-path")
GIT_LIST_FLAGS = {  # what branch and tag take when they only list; the rest create, delete, move or edit
    "branch": {"-a", "--all", "-r", "--remotes", "-l", "--list", "-v", "-vv", "--verbose", "-q", "--quiet",
               "--show-current", "--color", "--no-color", "--column", "--no-column", "-i", "--ignore-case",
               "--omit-empty", "--no-abbrev", "--abbrev"},
    "tag": {"-l", "--list", "-n", "--color", "--no-color", "--column", "--no-column", "-i", "--ignore-case",
            "--omit-empty"}}
GIT_NO_PROGRAMS = {"diff": ("--no-ext-diff", "--no-textconv"), "show": ("--no-ext-diff", "--no-textconv"),
                   "log": ("--no-ext-diff", "--no-textconv"), "blame": ("--no-textconv",),
                   "grep": ("--no-textconv",)}
# options that read outside the repository or run gpg, by the shortest start git takes for them, and where
GIT_REFUSED_PREFIX = (("--no-index", "--no-i", ("diff", "grep"), "it reads files outside the lane's repository"),
                      ("--contents", "--con", ("blame",), "it reads a file outside the lane's repository"),
                      ("--ignore-revs-file", "--ignore-revs", ("blame",), "it reads a file outside the lane's repository"),
                      ("--show-signature", "--show-s", ("log", "show"), "it would run gpg"))
GIT_LIST_VALUES = {"--contains", "--no-contains", "--merged", "--no-merged", "--points-at", "--sort", "--format"}


def git_refused(args: list) -> str:
    """Why hansa git will not run `git ARGS`; '' when it will."""
    if not args:
        return "name a git command, such as hansa git LANE log --oneline -5"
    cmd = args[0]
    if cmd.startswith("-"):
        return (f"{cmd} before the git command is refused: hansa git runs git in the lane's repository with its own "
                "settings (no -c, -C, --exec-path, --git-dir or the like)")
    changes = "code changes go through workers (hansa add), and merges through hansa merge"
    if cmd not in GIT_READS:
        return (f"git {cmd} is not one hansa git runs; it runs only commands that read: {', '.join(GIT_READS)} "
                f"(branch and tag only to list). {changes}")
    opts = args[1:args.index("--")] if "--" in args else args[1:]
    for x in opts:
        name = x.partition("=")[0]
        if name.startswith("--") and len(name) > 3 and any(r.startswith(name) for r in GIT_REFUSED_LONG):
            return f"{x} is refused: it would write a file or run a program"
        if not name.startswith("--") and name.startswith("-") and (
                name.startswith("-O") or (cmd == "grep" and "O" in name)
                or (name.startswith("-o") and cmd in ("diff", "log", "show"))):
            return f"{x} is refused: it would write a file or run a program"
        # blame -S FILE, alone or after other flags (-wS FILE); -L, -C and -M take the rest of the word as their value
        if cmd == "blame" and not name.startswith("--") and name.startswith("-") and "S" in re.split("[LCM]", x[1:])[0]:
            return f"{x} is refused: it reads a file outside the lane's repository"
        for full, shortest, cmds, why in GIT_REFUSED_PREFIX:
            if cmd in cmds and name.startswith(shortest) and full.startswith(name):
                return f"{x} is refused: {why}"
    if cmd == "diff":  # a path outside the repository makes git diff compare any two files, as --no-index does
        for x in args[1:]:
            if not x.startswith("-") and (x.startswith(("/", "~")) or ".." in x.split("/")):
                return f"{x} is refused: git diff would read files outside the lane's repository"
    if cmd in ("branch", "tag"):
        listing, words, i = False, [], 0
        while i < len(opts):
            x = opts[i]
            name = x.partition("=")[0]
            if name in GIT_LIST_VALUES:
                listing = True
                i += 1 if "=" in x or name in ("--contains", "--no-contains", "--merged", "--no-merged") else 2
                continue
            if x in ("-l", "--list") or (cmd == "tag" and re.fullmatch(r"-n\d*", x)):
                listing = True
            elif x.startswith("-") and name not in GIT_LIST_FLAGS[cmd] and not re.fullmatch(r"-n\d*", x):
                return f"git {cmd} {x} is refused: hansa git runs {cmd} only to list. {changes}"
            elif not x.startswith("-"):
                words.append(x)
            i += 1
        if words and not listing:
            return f"git {cmd} {' '.join(words)} would make a {cmd}; hansa git runs {cmd} only to list. {changes}"
    return ""


def cmd_git(a):
    from .run import check_git_file
    desk = Desk.find(a.desk)
    args, where = list(a.args), a.worktree
    while args and args[0] == "--worktree":
        args, where = args[1:], True
    ln = desk.settings()["lanes"].get(a.lane)
    if not ln:
        raise SystemExit(f"hansa: no lane {a.lane}; lanes: {', '.join(sorted(desk.settings()['lanes'])) or 'none'}")
    why = git_refused(args)
    if why:
        raise SystemExit(f"hansa git: {why}. Nothing was run")
    cwd = Path(ln["repo"])
    if where:
        cwd = desk.worktrees_dir / a.lane
        if not cwd.is_dir():
            raise SystemExit(f"hansa: lane {a.lane} has no worktree yet ({cwd}); its first task makes it")
        try:  # a worker may have pointed the worktree's .git elsewhere: no git through it then
            check_git_file(desk, cwd, ln["repo"])
        except RuntimeError as e:
            raise SystemExit(f"hansa: {e}")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_PAGER="cat", PAGER="cat", GIT_EDITOR="false", EDITOR="false", VISUAL="false",
               GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", GIT_ASKPASS="false", SSH_ASKPASS="false")
    # the repository's own config may still name programs to run: an fsmonitor, diff.external, textconv drivers,
    # gpg for signatures (also for a format such as %G?); a filter driver it names still runs, as the desk's account.
    # The signature programs become hansa's stand-in, which reads its input and fails: not false, which can end
    # before git has written the signed text to it, and git then dies of SIGPIPE
    args = [args[0], *GIT_NO_PROGRAMS.get(args[0], ()), *args[1:]]
    no_sig = str(NO_SIGNATURE)
    no_programs = ["-c", "core.fsmonitor=false", "-c", "log.showSignature=false", "-c", f"gpg.program={no_sig}",
                   "-c", f"gpg.ssh.program={no_sig}", "-c", f"gpg.x509.program={no_sig}"]
    r = subprocess.run(["git", "--no-pager", *no_programs, *args],
                       cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace")
    sys.stdout.write(desk.redact(r.stdout))
    sys.stderr.write(desk.redact(r.stderr))
    if r.returncode:
        raise SystemExit(r.returncode)


def cmd_merge(a):
    desk = Desk.find(a.desk)
    # Nothing changes the task, its verification, the keys or another merge until this one is done.
    # The repository's merge hooks run inside it: hansa serve waits for them, and a hansa command in a hook
    # that would change the desk stops at once (HANSA_MERGING) instead of waiting for this merge forever.
    if a.recheck and a.despite_check:
        raise SystemExit("hansa: --recheck runs the lane's check again before merging and --despite-check merges without "
                         "it; choose one. Nothing was merged")
    with desk.lock():
        m, v, chk, repo, base, lane = _mergeable(desk, a)
        sess = notify.recorded(notify.session_here())  # the session that hears how a merge check ends
        tip = git("rev-parse", base, cwd=repo)
        if a.recheck:
            if not merge_command(lane):
                raise SystemExit(f"hansa: lane {m['lane']} has no check to run again; nothing was merged")
            by = os.environ.get("HANSA_ROLE", "cli")
            desk.update(a.id, state="merging", merge_check_result=None,
                        merge_check={"phase": "queued", "queued": now(), "moves": 0, "recheck": True, "by": by}, **sess)
            desk.event("check_rerun_queued", task=a.id, onto=tip, by=by, command=merge_command(lane))
            print(f"{a.id} is not merged yet: hansa runs the lane's {_merge_what(lane)} again. It merges {a.id} onto "
                  f"{base} as it is now in lane {m['lane']}'s worktree, runs {_merge_step(lane)} there, and merges "
                  f"exactly that if it passes. {_runs_when(desk, a.id, base)} "
                  f"{_wait_for(desk, a.id)} (hansa show {a.id} says where it is; hansa cancel {a.id} stops it)"
                  + _unverified_later(desk, m))
            return
        # the base has commits the task's branch does not: the two together were never checked; or the lane's
        # merge check runs a command of its own, which ran on neither
        why_check = merge_needs_check(lane, m, repo, tip)
        unchecked = bool(why_check)
        if unchecked and not a.despite_check:
            desk.update(a.id, state="merging", merge_check={"phase": "queued", "queued": now(), "moves": 0},
                        merge_check_result=None, **sess)
            behind = git("rev-list", "--count", f"{m['head']}..{tip}", cwd=repo)
            desk.event("merge_check_queued", task=a.id, onto=tip, command=merge_command(lane))
            why = (f"{base} has {behind} commit(s) that {a.id}'s branch does not, so the two together were never "
                   f"checked" if why_check == "moved" else
                   f"lane {m['lane']} has a merge check of its own, which hansa runs before every merge, not after "
                   f"each task")
            print(f"{a.id} is not merged yet: {why}. hansa merges {a.id} onto {base} as it is now in lane "
                  f"{m['lane']}'s worktree, runs {_merge_step(lane)} there, and merges "
                  f"exactly that if it passes. "
                  f"{_runs_when(desk, a.id, base)} {_wait_for(desk, a.id)} "
                  f"(hansa show {a.id} says where it is; hansa cancel {a.id} stops it)" + _unverified_later(desk, m))
            return
        # a merge check runs or waits on this repository and base branch: going in now would move the base under
        # it, so this merge waits its turn in the same line
        s = desk.settings()
        at = merge_target(s, m["lane"])
        releasing = release.running_on(desk, repo)  # nor while a release runs there: it sends out what it saw
        if releasing or any(x["state"] == "merging" and x["id"] != a.id and merge_target(s, x.get("lane")) == at
                            for x in desk.tasks()):
            despite = bool((chk and not chk["ok"]) or (v and v["verdict"] == "does not hold") or unchecked)
            m = desk.update(a.id, state="merging", merge_check_result=None, **sess, merge_check={
                "phase": "queued", "queued": now(), "moves": 0, "plain": True,
                **({"despite": True, "despite_check": despite} if a.despite_check else {})})
            ahead = views.merge_check_waits_for(desk, m)
            desk.event("merge_queued", task=a.id, onto=tip, behind=(merge_check_ahead(s, m, desk.tasks()) or {}).get("id"))
            then = ("without a check, as --despite-check asks" if a.despite_check else
                    f"without a check if {base} has still not moved" + (
                        f"; if it has, it merges {a.id} onto {base} as it is then in lane {m['lane']}'s worktree, runs "
                        f"{_merge_step(lane)} there, and merges exactly that if it passes"
                        if merge_command(lane) else ""))
            note = unverified_note(desk, m)
            checks = any(x["state"] == "merging" and x["id"] != a.id and merge_target(s, x.get("lane")) == at
                         and not (x.get("merge_check") or {}).get("plain") for x in desk.tasks())
            there = ("while a merge check runs or waits there" if checks else
                     "while other merges wait there (none of them needs a check)" if not releasing else
                     f"and not while a release runs on {repo}")
            print(f"{a.id} is not merged yet: merges go into {base} one at a time {there}, in the order they were "
                  f"asked, so it waits for {ahead}. When its turn comes, hansa merges it "
                  f"{then}. {_wait_for(desk, a.id)} (hansa show {a.id} says where it is; hansa cancel {a.id} stops it)"
                  + (f"\nnote: {note}. It is merged all the same when its turn comes: this is a note, not a stop"
                     if note else ""))
            return
        try:
            git("merge", "--no-ff", "-m", merge_message(lane, m["lane"], a.id, m["title"]), m["head"], cwd=repo,
                env={"HANSA_MERGING": "1", **identity_env(lane)})
        except RuntimeError as e:
            files = conflicted_files(lambda *x: git(*x, cwd=repo))
            places = desk.redact(conflict_places(Path(repo), files)) if files else ""  # read before the abort
            try:
                git("merge", "--abort", cwd=repo)
            except RuntimeError:
                pass
            raise SystemExit(desk.redact(conflict_advice(a.id, base, places, str(e)[:300])))
        note = unverified_note(desk, m)  # before its verification is closed
        said = message_fallback(lane, m["lane"], "merge_message")
        desk.update(a.id, state="merged", merged=time.time(), merge_commit=git("rev-parse", "HEAD", cwd=repo),
                    unverified=note or None, merge_message_note=said or None)
        close_verification(desk, m, f"closed: {a.id} was merged")
    despite = (chk and not chk["ok"]) or (v and v["verdict"] == "does not hold") or unchecked
    desk.event("task_merged", task=a.id, into=base, **({"despite_check": True} if despite else {}),
               **({"merge_not_checked": True} if unchecked else {}))
    print(f"{a.id} merged into {base} at {repo}" + (f" (without checking it together with what {base} gained "
                                                     f"since it was made)" if why_check == "moved" else
                                                     f" (without running the lane's merge check, "
                                                     f"{merge_command(lane)})" if unchecked else ""))
    if note:
        print(f"note: {note}. It is merged: this is a note, not a stop")
    if said:
        print(f"note: {said}")


def _unverified_later(desk: Desk, m: dict) -> str:
    """For a merge that waits for its merge check: the line saying that nothing verified the change, or ''."""
    note = unverified_note(desk, m)
    lane = desk.settings()["lanes"].get(m.get("lane")) or {}
    return (f"\nnote: {note}. The merge check runs the lane's {_merge_what(lane)}, not a verification; if it passes, "
            f"{m['id']} is merged all the same: this is a note, not a stop") if note else ""


def _merge_what(lane: dict) -> str:
    """What a lane's merge check runs, named: its check, or its merge check when that is a command of its own."""
    return "merge check" if own_merge_check(lane) else "check"


def _merge_step(lane: dict) -> str:
    """What a lane's merge check runs, with the command, for hansa merge to say."""
    return f"the lane's {'setup and ' if lane.get('setup') else ''}{_merge_what(lane)} ({merge_command(lane)})"


def _mergeable(desk: Desk, a):
    """Everything that must hold before a merge; says why not and exits when something does not."""
    m = desk.meta(a.id)
    if m.get("kind") == "verify":
        raise SystemExit(f"hansa: {a.id} is a verification of {m.get('parent')}; it is never merged. "
                         f"Read it, then merge or send back {m.get('parent')}")
    if m["state"] == "merging":
        raise SystemExit(f"hansa: {a.id}'s merge check is already {(m.get('merge_check') or {}).get('phase', 'queued')}; "
                         f"it merges by itself if the check passes. {_wait_for(desk, a.id)} "
                         f"(hansa cancel {a.id} stops it)")
    if m["state"] != "done":
        raise SystemExit(f"hansa: only a finished task can be merged; {a.id} is {m['state']}")
    lane = desk.settings()["lanes"].get(m["lane"])
    if not lane:
        raise SystemExit(f"hansa: lane {m['lane']} is no longer in settings")
    repo, base = lane["repo"], lane.get("base", "main")
    held = branch_holds(repo, m)  # what is merged is the commit recorded as the task's work, still its branch's
    if held:
        raise SystemExit(f"hansa: {held}; nothing was merged. hansa show {a.id} says what it holds; hansa retry "
                         f"{a.id} \"...\" continues from the recorded commit")
    leaked = keys_in_change(desk, repo, m["base"], m["head"]) or m.get("leaked")  # a key saved since counts too
    if leaked:
        if leaked != m.get("leaked"):
            desk.update(a.id, leaked=leaked)
        raise SystemExit(f"hansa: the change in {a.id} holds the key of {', '.join(leaked)}; nothing was merged, "
                         f"and no flag overrides this. Redo it from the base without the key: hansa retry {a.id} --fresh "
                         f"\"...\", and tell the principal with hansa ask: they may want to change that key.")
    markers = markers_in_change(repo, m["base"], m["head"])
    if markers != (m.get("markers") or {}):  # found now, or gone since (marked in .gitattributes, say)
        desk.update(a.id, markers=markers)
    if markers:
        raise SystemExit(f"hansa: the change in {a.id} holds git's conflict markers ({markers_text(markers)}); nothing "
                         f"was merged, and no flag overrides this. Next: hansa retry {a.id} --update \"settle the "
                         f"markers in {', '.join(markers)}\"\n{MARKERS_ON_PURPOSE}")
    v = views.verification(desk, m)
    if v and v["state"] in ("queued", "running"):
        raise SystemExit(f"hansa: {v['task']} is still verifying {a.id}; nothing was merged. {_wait_for(desk, v['task'])}")
    touched = _release_files_touched(repo, m, lane)
    if touched and not (v and v["verdict"] == "holds"):
        more = len(touched) - 10
        raise SystemExit(
            f"hansa: the change in {a.id} touches what lane {m['lane']}'s releases build and run outside the sandbox "
            f"(its release_files): {', '.join(touched[:10])}" + (f" and {more} more" if more > 0 else "")
            + f"; nothing was merged, and no flag overrides this (--despite-check is for a failed check, not this). "
            + (f"Its verification {v['task']} " + ("found that it does not hold" if v["verdict"] == "does not hold"
                                                  else f"ended {v['verdict'] or 'without a verdict'}")
               + ". " if v else "Nothing has verified it. ")
            + f"Have it verified: hansa verify {a.id}, and merge once the verification holds")
    if v and v["verdict"] == "does not hold" and not a.despite_check:
        raise SystemExit(f"hansa: the verification {v['task']} found that {a.id} does not hold; nothing was merged. "
                         f"hansa show {v['task']} says what it ran. Send it back with hansa retry {a.id} \"...\", "
                         f"or merge anyway: hansa merge {a.id} --despite-check")
    chk = m.get("check")
    if chk and not chk["ok"] and not a.despite_check and not a.recheck:
        if chk.get("could_not_run"):
            raise SystemExit(f"hansa: {a.id}'s check {chk['note']}; nothing was merged. That is the machine's problem, "
                             f"not the task's: see hansa explain server. Once it is fixed, run the check again and merge "
                             f"if it passes: hansa merge {a.id} --recheck (or, to merge it unchecked: "
                             f"hansa merge {a.id} --despite-check)")
        machine = views.machine_hint(chk)
        raise SystemExit(f"hansa: {a.id} failed the lane's check ({chk['note'] or 'exit ' + str(chk['exit'])}); "
                         f"nothing was merged. hansa show {a.id} has the output. "
                         + (f"{machine}; " + ("if it should have printed something, once that is fixed"
                                                if views.silent(chk) else "once that is fixed")
                            + f": hansa merge {a.id} --recheck (to merge it anyway: "
                            f"hansa merge {a.id} --despite-check)" if machine else
                            f"To merge it anyway: hansa merge {a.id} --despite-check"))
    current = git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo)
    if current != base:
        raise SystemExit(f"hansa: the checkout at {repo} is on {current}, not {base}; the principal needs to switch it back")
    if git("status", "--porcelain", "--untracked-files=no", cwd=repo):
        raise SystemExit(f"hansa: the checkout at {repo} has uncommitted changes; nothing was merged")
    return m, v, chk, repo, base, lane


def _release_files_touched(repo, m: dict, lane: dict) -> list:
    """The files a task's change adds, changes or removes that the lane's release_files name."""
    patterns = lane.get("release_files") or []
    if not patterns:
        return []
    files = list(m.get("files") or [])
    try:
        files += git("diff", "--name-only", "--no-renames", m["base"], m["head"], cwd=repo).splitlines()
    except (RuntimeError, KeyError):
        pass
    return release_files_in(files, patterns)


def cmd_retry(a):
    desk = Desk.find(a.desk)
    with desk.lock():  # the task cannot start, end or be sent back twice meanwhile
        _retry(desk, a)


def _retry(desk: Desk, a) -> None:
    m = desk.meta(a.id)
    if m.get("kind") == "verify":
        raise SystemExit(f"hansa: {a.id} is a verification; send back the task it checked: hansa retry {m.get('parent')} \"...\"")
    if m["state"] in ("queued", "running", "merging"):
        raise SystemExit(f"hansa: {a.id} is {m['state']}; a follow-up is for a task that has ended. "
                         f"{_wait_for(desk, a.id)}, or cancel it first (hansa cancel {a.id})")
    if a.update and a.fresh:
        raise SystemExit("hansa: --update continues from the previous attempt and --fresh starts from the base; "
                         "choose one. Nothing was queued")
    if a.update and not m.get("head"):
        raise SystemExit(f"hansa: {a.id} has no branch to bring up to date; start over with hansa retry {a.id} --fresh "
                         f"\"...\". Nothing was queued")
    if not a.fresh and m.get("head") and m.get("kind") != "verify":  # it continues from the recorded commit
        own = desk.settings()["lanes"].get(m.get("lane"))
        held = branch_holds(own["repo"], m) if own else ""
        if held:
            raise SystemExit(f"hansa: {held}; nothing was queued. Start over from the base: hansa retry {a.id} "
                             f"--fresh \"...\"")
    if m["state"] == "replaced":
        raise SystemExit(f"hansa: {a.id} was already replaced by {m.get('replaced_by')}; send that one back instead")
    if a.file and a.note:
        raise SystemExit("hansa: give the note as words or with --file, not both. Nothing was queued")
    old = (desk.tasks_dir / a.id / "task.md").read_text(encoding="utf-8").strip()
    note = (_text(a) if a.file else " ".join(a.note)).strip()
    text = old + f"\n\n---\nFollow-up to {a.id} ({m['state']})." + (f" What to change: {note}" if note else "")
    report = desk.tasks_dir / a.id / "result.md"
    if report.exists() and report.read_text(encoding="utf-8").strip():
        text += "\nThe previous attempt's report:\n" + report.read_text(encoding="utf-8").strip()[-6000:]
    vid = m.get("verify")  # the latest verification of the attempt, whatever it found, given whole
    vreport = desk.tasks_dir / vid / "result.md" if vid else None
    if vreport and vreport.exists() and vreport.read_text(encoding="utf-8").strip():
        verdict = desk.meta(vid).get("verdict") or "none"
        text += (f"\n\nThe report of {vid}, the verification of the previous attempt (a worker who ran the change "
                 f"to see whether it holds; its verdict: {verdict}):\n" + vreport.read_text(encoding="utf-8").strip())
    if a.browser and not desk.settings().get("browser"):
        raise SystemExit("hansa: this desk has no browser set up (the principal sets one on the page); nothing was queued")
    opts = {"lane": m.get("lane", ""), "effort": a.effort or m.get("effort", ""), "model": m.get("model", ""),
            "parent": a.id, "title": f"{m['title']} (retry of {a.id})"[:80], "browser": a.browser or m.get("browser"),
            **notify.recorded(notify.session_here())}
    base = (desk.settings()["lanes"].get(m.get("lane")) or {}).get("base") or "main"
    if a.update:  # the base's tip when it starts is merged into its branch then (run.prepare)
        opts.update(start_from=m["head"], update=True)
        text += (f"\nYou start from the previous attempt's last commit ({m['head'][:10]}), with {base} as it is now "
                 f"merged into it.")
    elif not a.fresh and m.get("head"):
        opts.update(start_from=m["head"], base=m["base"])
        text += f"\nYou start from the previous attempt's last commit ({m['head'][:10]})."
    tid = desk.new_task(text, **opts)
    if m["state"] in ("done", "failed"):  # the follow-up carries the work on; the original leaves the review list
        desk.update(a.id, state="replaced", replaced_by=tid)
        close_verification(desk, m, f"closed: {a.id} was replaced by {tid}")
    print(f"{tid} queued as a follow-up to {a.id}" + (" from the base branch" if a.fresh else
                                                      f", with the current {base} merged into its branch when it starts "
                                                      f"(a conflict is left for the worker to settle)" if a.update else "")
          + (f". {_wait_for(desk, tid)}" if notify.hears_of(desk, tid) else "") + _waits(desk, tid))


def cmd_cancel(a):
    desk = Desk.find(a.desk)
    by = notify.session_here()  # the session that cancelled it hears nothing of it; others do
    with desk.lock():  # the state read here is the state changed here
        m = desk.meta(a.id)
        if m["state"] == "queued":
            desk.update(a.id, state="cancelled", error="cancelled before it started", finished=now(), cancel_by=by)
        elif m["state"] == "running":  # hansa serve stops it; stopping it here as well is only quicker
            m = desk.update(a.id, cancel=True, cancel_by=by)
        elif m["state"] == "done" and m.get("kind") == "verify":  # it has ended: it keeps its state and verdict
            desk.update(a.id, closed=m.get("closed") or f"closed: {a.id} was closed with hansa cancel")
        elif m["state"] == "done":
            desk.update(a.id, state="cancelled", error="closed without merging", cancel_by=by)
        elif m["state"] == "merging":  # the merge check stops; the task goes back to waiting for review
            mc = m.get("merge_check") or {}
            if mc.get("phase") == "queued":
                desk.update(a.id, state="done", merge_check=None)
            else:
                desk.update(a.id, merge_check={**mc, "cancel": True})
        else:
            raise SystemExit(f"hansa: {a.id} is {m['state']}; only queued, running, merging or unmerged finished tasks "
                             f"can be cancelled")
        if m["state"] != "merging":
            close_verification(desk, m, f"closed: {a.id} was cancelled", by)
    if m["state"] == "running":
        stop(m.get("pid"), m.get("pid_start", ""))
    if m["state"] == "merging":
        mc = m.get("merge_check") or {}
        if mc.get("phase") != "queued":
            stop(mc.get("pid"), mc.get("pid_start", ""))
            finish_merge_check(desk, a.id)  # hansa serve would too; this is only quicker
        desk.event("merge_check_cancel", task=a.id)
        print(f"{a.id}'s merge check stopped; nothing was merged, and {a.id} is back waiting for review")
        return
    if m["state"] == "done" and m.get("kind") == "verify":  # not cancelled: closed, or already closed before
        if not m.get("closed"):
            desk.event("verification_closed", task=a.id, by="hansa cancel")
        print(f"{a.id} closed; it stays done" + (f", verdict: {m['verdict']}" if m.get("verdict") else ""))
        return
    desk.event("task_cancel", task=a.id)
    print(f"{a.id} cancelled")


def cmd_verify(a):
    desk = Desk.find(a.desk)
    with desk.lock():  # one verification at a time, of a task that stays as it was checked
        _verify(desk, a)


def _verify(desk: Desk, a) -> None:
    m = desk.meta(a.id)
    if m.get("kind") == "verify":
        raise SystemExit(f"hansa: {a.id} is itself a verification; verify the task it checks ({m.get('parent')})")
    if m["state"] != "done" or not m.get("commits"):
        raise SystemExit(f"hansa: only a finished task with changes can be verified; {a.id} is {m['state']}"
                         + ("" if m.get("commits") else ", with nothing to merge"))
    if a.browser and not desk.settings().get("browser"):
        raise SystemExit("hansa: this desk has no browser set up (the principal sets one on the page); nothing was queued")
    v = views.verification(desk, m)
    if v and v["state"] in ("queued", "running"):
        raise SystemExit(f"hansa: {v['task']} is already verifying {a.id}")
    lane = m.get("lane", "")
    own = desk.settings()["lanes"].get(lane)
    moved = own is not None and m.get("repo") and not _same_repo(own["repo"], m["repo"])
    if (a.lane and a.lane != lane) or own is None or moved:
        _lane_reaches(desk, a.lane or lane, m)
        lane = a.lane or lane
    held = branch_holds(desk.settings()["lanes"][lane]["repo"], m)  # what is verified is the recorded commit
    if held:
        raise SystemExit(f"hansa: {held}; nothing was queued. hansa show {a.id} says what it holds")
    d = desk.tasks_dir / a.id
    task = (d / "task.md").read_text(encoding="utf-8").strip()
    report = (d / "result.md").read_text(encoding="utf-8").strip() if (d / "result.md").exists() else ""
    note = " ".join(a.note).strip()
    sess = notify.session_here()
    text = (VERIFY_TASK.format(id=a.id, base=m["base"][:12], head=m["head"][:12])
            + (f"\nLook in particular at: {note}\n" if note else "")
            + f"\n## Task {a.id} as it was given\n{task}\n\n## The report of the worker who did it\n{report or '(none)'}\n")
    vid = desk.new_task(text, kind="verify", parent=a.id, lane=lane, effort=a.effort or "",
                        start_from=m["head"], base=m["head"], title=f"Verify {a.id}: {m['title']}"[:80],
                        browser=a.browser or m.get("browser"), **notify.recorded(sess))
    if v:
        close_verification(desk, m, f"closed: {vid} verifies {a.id} again")
    desk.update(a.id, verify=vid, **notify.recorded(sess))
    print(f"{vid} queued" + (f" on lane {lane}" if lane != m.get("lane", "") else "")
          + f": a worker will run {a.id}'s change to see whether it holds. "
          + (_wait_for(desk, vid) if notify.hears_of(desk, vid) else f"Follow it with: hansa wait {vid}")
          + f"; the verdict shows in hansa show {a.id}" + _waits(desk, vid))


def _same_repo(x: str, y: str) -> bool:
    return (resolved(x) or x) == (resolved(y) or y)


def _lane_reaches(desk: Desk, name: str, m: dict) -> None:
    """Refuses, queueing nothing, a lane that cannot reach the task's change: one that is not on the desk, or not on
    the repository of the task's own lane, where the task's branch is."""
    lanes = desk.settings()["lanes"]
    nothing = "; nothing was queued"
    own = lanes.get(m.get("lane"))
    if own is not None and m.get("repo") and not _same_repo(own["repo"], m["repo"]):
        if name == m["lane"]:  # removed and made again on another repository: not where the task's branch is
            raise SystemExit(f"hansa: {m['id']}'s lane {name} is now on {own['repo']}, not on {m['repo']}, where "
                             f"{m['id']} ran and its branch is (the lane was removed and made again); verify it "
                             f"with --lane L, a lane on {m['repo']}{nothing}")
        own = {**own, "repo": m["repo"]}
    if own is None and name == m.get("lane"):
        raise SystemExit(f"hansa: {m['id']}'s lane {name or '(none)'} is no longer on the desk; verify it with --lane L, "
                         f"a lane on the repository where its branch {m.get('branch') or 'task/' + m['id']} is"
                         f"{nothing}")
    if name not in lanes:
        raise SystemExit(f"hansa: there is no lane {name}; lanes: {', '.join(sorted(lanes)) or 'none'}{nothing}")
    if own is None:  # the task's branch, still at the change, is what shows the lane is on its repository
        branch = m.get("branch") or f"task/{m['id']}"
        if branch_holds(lanes[name]["repo"], m):  # read as a plain file, never through a link
            raise SystemExit(f"hansa: {m['id']}'s lane {m.get('lane') or '(none)'} is no longer on the desk, and lane "
                             f"{name}'s repository {lanes[name]['repo']} has no branch {branch} at {m['id']}'s change "
                             f"({m['head'][:12]}), so lane {name} cannot be shown to reach it. Verify it on a lane on "
                             f"the repository where that branch is{nothing}")
        return
    if not _same_repo(lanes[name]["repo"], own["repo"]):
        raise SystemExit(f"hansa: lane {name} cannot reach {m['id']}'s change: it is on {lanes[name]['repo']}, and "
                         f"{m['id']}'s branch is in {own['repo']}, the repository of its lane {m['lane']}. Verify it "
                         f"on a lane on that repository{nothing}")


def _stop_release(desk: Desk, a) -> None:
    """hansa release --stop ID [reason]: call a release off."""
    if os.environ.get("HANSA_TASK"):
        raise SystemExit("hansa: only the coordinator stops a release; a worker, verification or design session "
                         "cannot. Say in your report why it should not go out")
    rid = a.stop.lstrip("#")
    if not rid.isdigit():
        raise SystemExit(f"hansa: --stop takes a release's number (hansa release lists them), not {a.stop!r}")
    reason = " ".join(([a.lane] if a.lane else []) + list(a.note))
    out = release.stop_release(desk, int(rid), reason, by=os.environ.get("HANSA_ROLE", "cli"))
    if not out["ok"]:
        raise SystemExit("hansa: " + "; ".join(out["problems"]) + ". Nothing was changed")
    r = release.get(desk, int(rid)) or {}
    if out["state"] == "stopped":
        print(desk.redact(f"release #{rid} of {release.what(r)} is stopped; nothing was run"))
        return
    print(desk.redact(
        f"release #{rid} of {release.what(r)} is being stopped: hansa serve sends TERM to its command's process group "
        f"within seconds, and KILL {release.STOP_GRACE} s later to what still runs; it is then recorded as stopped, "
        f"its output so far kept" + (" and its checkout removed" if r.get("fixed") else "")
        + ". The server may be left half-switched: hansa release shows its last lines"))


def _answer_by_code(desk: Desk, a) -> None:
    """hansa release yes|no ID CODE [reason]: the principal's answer, said in the conversation with the one-time code
    of their alert, passed on."""
    rid, rest = int(a.note[0].lstrip("#")), list(a.note[1:])
    if not rest:
        raise SystemExit(f"hansa: pass the code the principal said in the conversation: hansa release {a.lane} {rid} "
                         "CODE. Never guess one; without it, the principal answers on the page. Nothing was changed")
    yes = a.lane.strip().lower() == "yes"
    if yes and len(rest) > 1:  # a code split over words, or a reason a yes does not take: nothing is checked or counted
        raise SystemExit(f"hansa: hansa release yes {rid} CODE takes the code as one word (dashes allowed, no spaces), "
                         "and nothing after it; pass it again as one word. Nothing was checked or counted")
    out = release.answer_by_code(desk, rid, yes, rest[0], " ".join(rest[1:]))  # by the coordinator, for the principal
    if not out["ok"]:
        raise SystemExit("hansa: " + desk.redact("; ".join(out["problems"])) + ".")
    r = release.get(desk, rid) or {}
    if not yes:
        print(desk.redact(f"release #{rid} of {release.what(r)} is refused, as the principal said with the code of "
                          "their alert; nothing runs. Do not push or deploy it yourself"))
        return
    print(desk.redact(f"release #{rid} of {release.what(r)} is approved, as the principal said with the code of their "
                      f"alert (recorded as given through you); {release.waits_text(desk, desk.settings(), r)}. hansa "
                      "tells you when it ends"))


def cmd_pause(a):
    Desk.find(a.desk).pause(" ".join(a.reason) or "paused", by=os.environ.get("HANSA_ROLE", "cli"))
    print("paused: no new tasks start until `hansa resume`; running tasks finish")


def cmd_resume(a):
    Desk.find(a.desk).resume(by=os.environ.get("HANSA_ROLE", "cli"))
    print("resumed")


def cmd_wait(a):
    """Return when a watched task stops running, the principal answers a request, or time is up.
    Meant to run in the background, so the session is told when there is something to look at."""
    desk = Desk.find(a.desk)
    live = lambda t: desk.meta(t)["state"] in ("queued", "running", "merging")  # noqa: E731
    watch = sorted(set(a.ids)) or sorted(m["id"] for m in desk.tasks() if m["state"] in ("queued", "running", "merging"))
    open_asks = {x["id"] for x in desk.asks() if not x["closed"]}
    done_already = [t for t in watch if not live(t)]
    if not done_already and not any(map(live, watch)) and not open_asks:
        print("nothing is queued or running and no request to the principal is open: nothing to wait for")
        return
    if not views.status(desk)["server_up"]:
        print("note: hansa serve is not running, so no task will start or finish until it is")
    end = time.time() + a.timeout * 60
    while True:
        ended = [desk.meta(t) for t in watch if not live(t)]
        answered = [x for x in desk.asks() if x["id"] in open_asks and x["closed"]]
        if ended or answered or time.time() >= end:
            break
        time.sleep(a.every)
    for m in ended:
        mc = m.get("merge_check_result") or {}
        print(f"{m['id']} {m['state']}: {m['title']}  (hansa show {m['id']})"
              + (f"\n  its merge: {views.waited_text(mc)}" if m["state"] == "done" and mc.get("waited") else
                 f"\n  its merge check did not pass, so nothing was merged: "
                 + (mc.get("note") or f"the check failed (exit {mc.get('exit')})")
                 if m["state"] == "done" and mc and not mc.get("ok") else
                 "\n  merged after its merge check passed" if m["state"] == "merged" and mc.get("ok") else ""))
    for x in answered:
        print(f"request #{x['id']} closed by the coordinator: {x.get('reason', '')}" if x.get("closed_by") == "coordinator"
              else f"request #{x['id']} answered: {x['answer'] or '(closed without a note)'}")
    still = [desk.meta(t) for t in watch if live(t)]
    if not ended and not answered:
        print(f"no task finished in {a.timeout:g} min")
    for m in still:
        mins = int((time.time() - m["started"]) // 60) if m.get("started") else 0
        print(f"{m['id']} {m['state']}" + (f" {mins} min, so far: {views.progress_text(desk, m)}"
                                           if m["state"] == "running" else
                                           f": {views.merge_check_text(desk, m)}" if m["state"] == "merging" else ""))
    if still:
        print("hansa wait again to keep waiting. Some providers take minutes per reply; a task is stuck"
              " only when nothing has happened for a long while.")


def cmd_ask(a):
    if a.close is not None:
        if os.environ.get("HANSA_TASK"):  # a worker, verification or design session: the requests are the coordinator's
            raise SystemExit("hansa: only the coordinator closes a request; a worker, verification or design "
                             "session cannot. Say in your report that it may be done")
        Desk.find(a.desk).close_ask_as_coordinator(a.close, " ".join(a.text), by=os.environ.get("HANSA_ROLE", "cli"))
        print(f"ask #{a.close} is closed; the principal's page shows it as closed by the coordinator, with the reason")
        return
    n = Desk.find(a.desk).add_ask(" ".join(a.text), by=os.environ.get("HANSA_ROLE", "cli"),
                                  session=notify.session_here())
    print(f"ask #{n} is on the principal's page")


def cmd_release(a):
    desk = Desk.find(a.desk)
    if a.stop is not None:
        _stop_release(desk, a)
        return
    if not a.lane:
        print(desk.redact(release.list_text(desk)))
        return
    if os.environ.get("HANSA_TASK"):  # a worker, verification or design session: releases are the coordinator's
        raise SystemExit("hansa: only the coordinator asks for a release; a worker, verification or design session "
                         "cannot. Say in your report that it may be ready to go out")
    if answer_name(a.lane) and a.note and a.note[0].lstrip("#").isdigit():  # always an answer, never a request
        taken = [n for n in desk.settings()["lanes"] if answer_name(n)]
        if taken:
            raise SystemExit(f"hansa: hansa release {a.lane} ID CODE is the principal's answer, but this desk has a "
                             f"lane named {', '.join(taken)}: that lane must be removed first (hansa lane remove), "
                             "then pass the code again. Nothing was recorded")
        a.lane = a.lane.strip().lower()
        _answer_by_code(desk, a)
        return
    note, target = list(a.note), None
    if note and note[0] and note[0] in lane_targets(desk.settings()["lanes"].get(a.lane)):
        target = note.pop(0)  # hansa release L TARGET [note]; with one target, L [note] is enough
    r = release.ask(desk, a.lane, " ".join(note), by=os.environ.get("HANSA_ROLE", "cli"),
                    session=notify.session_here(), target=target)
    who = release.what(r)
    head = (f"release #{r['id']}: lane {a.lane}'s {r['base']} at {r['commit'][:10]}"
            + (f" by target {r['target']}" if r.get("target") else "") + ", "
            + (f"{r['count']} commit(s) since release #{r['since_release']}" if r.get("since_release") else
               f"{r['count']} commit(s) on no remote-tracking branch (no release of it went out before)")
            + f", {r['files_count']} file(s) changed; {release.checked_text(r)}; command: {r['command']} "
            f"({release.runs_text(r)})")
    if r["state"] == "forbidden":
        raise SystemExit(desk.redact(
            f"hansa: {head}\nrefused: what goes out holds words the principal keeps from going out (the web page, "
            f"Settings), so nothing waits for the principal and nothing runs:\n{release.hits_text(r['hits'])}\n"
            f"Take them out of the history of {r['base']} (hansa add a task for it, if it is in the code; a word in "
            f"a commit message or a file name stays in the history) or out of the release command, or ask the "
            f"principal with hansa ask."))
    heard = notify.hears(desk)
    after = ("hansa tells this session when it is answered and when it ends" if heard else
             "hansa release shows how it goes")
    if r["state"] == "approved":
        paused = desk.paused()
        listed = (f"{who} is on the principal's list of releases without asking" if r.get("target") else
                  f"lane {a.lane} is on the principal's list of lanes released without asking")
        gone = not_looking(desk)  # as the release's waiting text says it (release.waits_text)
        print(desk.redact(f"{head}\n{listed}, but the "
                          f"desk is paused ({paused}): while it is, only a release the principal said yes to runs, so "
                          f"this one waits until the desk is resumed. {after}" if paused else
                          f"{head}\n{listed}: hansa "
                          f"serve runs the command {'from a checkout of ' + r['commit'][:10] + ' made for it' if r.get('fixed') else 'in ' + r['repo']} "
                          + (f"once it gets through its rounds; {gone}. {after}" if gone else f"within seconds. {after}")))
        return
    named = f"\nThe command names changed files: {', '.join(r['named'])}." if r.get("named") else ""
    print(desk.redact(f"{head}{named}\nIt waits for the principal's yes on the web page; only then does hansa serve run "
                      f"it, and only while {r['base']} is still at {r['commit'][:10]} and the command is the same. "
                      f"Do not push or deploy yourself. {after}; tell the principal, in the conversation, that it waits "
                      f"for them."))


def cmd_notify(a):
    desk = Desk.find(a.desk)
    if a.clear_daily and a.action != "on":
        raise SystemExit("hansa: --clear-daily goes with hansa notify on (or hansa coordinator)")
    print(notify.turn_on(desk, a.clear_daily or "") if a.action == "on" else notify.turn_off(desk)
          if a.action == "off" else notify.list_text(desk))


def cmd_notify_follow(a):
    raise SystemExit(notify.follow(Desk.find(a.desk), a.key, {"pid": a.pid, "start": a.start}, a.socket, a.nonce,
                                   a.name, a.clear_daily))


def cmd_notify_feed(a):
    notify.cmd_feed(Desk.find(a.desk))


def cmd_explain(a):
    if a.all and a.what != "lane":
        raise SystemExit("hansa: --all is for hansa explain lane NAME: the whole built-in list of what its workers "
                         "may not read")
    print(views.explain_text(Desk.find(a.desk), a.what or "", a.name or "", a.all))


def _since(text: str) -> float:
    """'13:45' (today, or yesterday if that is still ahead) or '2026-09-24 13:45', local time."""
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%H:%M"):
        try:
            t = time.strptime(text.strip(), fmt)
        except ValueError:
            continue
        if fmt != "%H:%M":
            return time.mktime(t)
        now = time.localtime()
        ts = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, t.tm_hour, t.tm_min, 0, 0, 0, -1))
        return ts - 86400 if ts > time.time() else ts
    raise SystemExit("hansa: --since takes HH:MM (today) or YYYY-MM-DD HH:MM")


def cmd_cost(a):
    if a.turns and a.by:
        raise SystemExit("hansa: --turns lists the coordinator's turns session by session, with totals by what "
                         "started them; it has no --by. For totals by day, seat, lane, task, model or provider: "
                         "hansa cost --by ...")
    if a.turns:
        rep = cost.turns(Desk.find(a.desk), a.days, _since(a.since) if a.since else None)
        print(json.dumps(rep, ensure_ascii=False, indent=1) if a.json else views.cost_turns_text(rep))
        return
    rep = cost.report(Desk.find(a.desk), a.days, _since(a.since) if a.since else None)
    if a.json:
        rep = {k: v for k, v in rep.items() if k != "rows"} if not a.rows else rep
        print(json.dumps(rep, ensure_ascii=False, indent=1))
    else:
        print(views.cost_text(rep, a.by or "day"))


def cmd_check(a):
    desk = Desk.find(a.desk)
    names = [a.provider] if a.provider else sorted(desk.settings()["providers"])
    for n in names:
        r = check_provider(desk, n)
        print(f"{n}: {'ok' if r['ok'] else 'NOT ok'} - {r['detail']}")


LOCK_WAIT = 2.0  # seconds hansa serve waits for serve.lock before it says another server holds it


def hold_server_lock(desk: Desk):
    """One hansa serve per desk: it holds this lock while it runs. None when another server holds it."""
    desk.state_dir.mkdir(exist_ok=True)
    f = open(desk.state_dir / "serve.lock", "a")  # not emptied before it is held: it names the server holding it
    end = time.monotonic() + LOCK_WAIT  # by the clock: on macOS short sleeps overshoot, so counting them waits long
    while True:  # asking whether a server runs holds it shared for a moment (run.lock_held): not a server
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if time.monotonic() >= end:
                f.close()
                return None
            time.sleep(0.05)
    f.truncate(0)
    f.write(f"{os.getpid()} {proc_start(os.getpid())}")  # what hansa never stops when it stops what a task left
    # running (run.sweep), and whose being alive says hansa serve runs (run.server_state)
    f.flush()
    return f


def cmd_serve(a):
    from .web import start_web
    desk = Desk.find(a.desk)
    held = hold_server_lock(desk)  # kept open while this server runs
    if not held:
        raise SystemExit(f"hansa: another hansa serve is already running on {desk.root}; one per desk, so no task is "
                         f"started twice. Stop that one first if you want to restart it")
    record_serve_code(desk)  # which code this server runs, for hansa status to tell from what is installed later
    record_serve_locale(desk)  # the locale its lanes' commands get, for hansa explain lane to say
    alert.sweep_runs(desk)  # the folders of alert runs a stopped or killed server left (none runs now: one server)
    try:
        port = start_web(desk, a.host, a.port).server_address[1]
    except OSError as e:
        if e.errno != errno.EADDRINUSE:
            raise
        raise SystemExit(f"hansa: port {a.port} is in use, perhaps by another desk's hansa serve; start this desk's "
                         f"server on a free port with hansa serve --port N (hansa web then finds it)")

    def stopped(*_):  # TERM ends it as Ctrl+C does: the alert run's folder goes with it (the thread is a daemon)
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stopped)
    (desk.state_dir / "web-port").write_text(str(port), encoding="utf-8")  # what hansa web opens
    (desk.state_dir / "web-host").write_text(a.host, encoding="utf-8")  # with it, where hansa status says the page is
    beat(desk)  # its rounds count from now, not from the last server's
    pulse(desk)
    alert.start_thread(desk)  # the principal's alert command, run from here only (hansa/alert.py)
    d = Dispatcher(desk)
    desk.event("server_started", port=port)
    print(f"hansa serve: desk {desk.root}; web page on {a.host}:{port} (run `hansa web` for the address)", flush=True)
    srt = try_srt_at_start(desk)  # on this server's PATH, which runs the lanes' setups and checks
    desk.event("srt_tried", found=srt.get("found"), path=srt.get("path"), ran=srt.get("ran"), said=srt.get("said"))
    problem = views.srt_problem(desk, desk.settings())
    if problem:
        print(f"hansa serve: {problem}", file=sys.stderr, flush=True)
    try:
        while True:
            try:
                d.tick()
            except Exception as e:  # noqa: BLE001 - keep serving; the error is on record
                desk.event("server_error", error=str(e)[:300])
                print(f"hansa serve: {e}", file=sys.stderr, flush=True)
            time.sleep(a.every)
    except KeyboardInterrupt:
        desk.event("server_stopped")
    finally:
        alert.sweep_runs(desk)


def web_port(desk: Desk) -> int:
    """The port this desk's hansa serve last served the page on: another desk's server may hold the default."""
    return views.web_address(desk)[1]


def cmd_web(a):
    if os.environ.get("HANSA_ROLE") or os.environ.get("HANSA_TASK"):
        raise SystemExit("hansa: the web page is for the principal; agents use the other commands")
    desk = Desk.find(a.desk)
    print(f"Open http://127.0.0.1:{a.port or web_port(desk)}/?t={desk.web_token()}", flush=True)
    print("Keep this window open while you use the page; close it or press Ctrl+C when you are done.", flush=True)
    # This holds an ssh tunnel to the page open. Its input ends when the connection does (a closed
    # window, a laptop gone to sleep), and then it ends too instead of staying on the server.
    try:
        while os.read(0, 4096):
            pass
    except (KeyboardInterrupt, OSError):
        pass


def cmd_coordinator(a):
    """Start the coordinator's interactive session in the desk, on the coordinator provider."""
    clear, rest = _clear_daily(a.rest)
    desk = Desk.find(a.desk)
    theirs = account.desk_account(desk.root)
    if theirs:
        # root on the account's desk: the account may have put links where hansa expects files, so root
        # neither reads nor writes there. The account's hansa does the desk's part and says how to start
        r = account.run(theirs, ["coordinator-desk", "--by", str(account.uid())], desk.root,
                        capture_output=True, text=True)
        if r.returncode:
            sys.stderr.write(r.stderr)
            raise SystemExit(r.returncode)
        plan = json.loads(r.stdout)
    else:
        plan = coordinator_desk(desk, account.uid())
    from .run import PROVIDER_VARS, claude_bin, no_read_rules
    # the plan may be the account's to write, and it goes on root's command line and into root's environment
    model, effort = plan.get("model"), plan.get("effort") or ""
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9._:/\[\]][A-Za-z0-9._:/\[\]-]*", model):
        raise SystemExit(f"hansa: the coordinator's model {model!r} is not a model name "
                         "(letters, digits and ._:-/[], not starting with -)")
    if effort not in EFFORTS:
        raise SystemExit(f"hansa: the coordinator's effort {effort!r} is not one of {', '.join(EFFORTS[1:])}")
    env = {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_")}
    # only the variables provider_env sets for the provider, by name: others (HOME, PATH, and CLAUDE_*
    # ones that change how Claude Code runs commands) are not the desk owner's to set for root
    given = plan.get("env") if isinstance(plan.get("env"), dict) else {}
    env.update({k: given[k] for k in PROVIDER_VARS if isinstance(given.get(k), str)})
    if theirs:  # the provider's Claude Code folder is the account's: root's own, as root would start Claude Code
        env.pop("CLAUDE_CONFIG_DIR", None)
        if os.environ.get("CLAUDE_CONFIG_DIR"):
            env["CLAUDE_CONFIG_DIR"] = os.environ["CLAUDE_CONFIG_DIR"]
    env["PATH"] = str(Path(__file__).resolve().parent.parent / "bin") + os.pathsep + os.environ.get("PATH", "")
    env["HANSA_ROLE"] = "coordinator"
    env["CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION"] = "false"  # over the setting in QUIET, when the principal's says true
    env["HANSA_DESK"] = str(desk.root)
    # the lanes' checkouts are readable for planning and review; workers do the editing
    repos = [r for r in plan["repos"] if os.path.isabs(r)]
    # the folders the principal lets it read without asking, given as the repositories are. Checked again as
    # whoever starts the session: the plan may be the account's to write, and root's home and login are not its
    # what the principal keeps workers from reading: Read is denied there to this session too, and none of the
    # folders it is let read may open one. Checked again as whoever starts the session, as those are
    kept, unkept = no_read_paths(desk, plan, theirs)
    shut, said = read_folders(desk, plan, env, theirs, kept)
    for line in [*unkept, *said]:
        print(f"hansa: {line}; skipped (the principal changes the list on the page, Settings)", file=sys.stderr)
    # and the built-in logins and keys under the home folder of whoever runs the session. Not the Claude Code
    # folders, as for workers: its own memory lives in one (own_folder_rules closes the login in it)
    home = env.get("HOME") or os.path.expanduser("~")
    no_read = no_read_rules([*kept, *(os.path.join(home, f) for f in home_secrets())])
    dirs = [*repos, *(d for d in shut if d not in repos)]
    shut = [*shut, *(r for r in (stored(resolved(d) or d) for d in shut) if r not in shut)]  # where a link leads, too
    no_edit = [f"Edit(//{str(d).lstrip('/')}/**)" for d in [*repos, desk.worktrees_dir, *shut]]
    no_builtin = [f"Agent({n})" for n in BUILTIN_HELPERS]
    claude = claude_bin()
    if "DISABLE_AUTOUPDATER" not in env and not can_update(claude):  # it would fail and say so at every start
        env["DISABLE_AUTOUPDATER"] = "1"
    argv = [claude] + (["--add-dir", *dirs] if dirs else []) + ["--disallowedTools", *no_edit, *no_builtin, *no_read]
    unsplit = argv_rules_problem(argv)
    if unsplit:  # a deny rule Claude Code would split leaves its folder open: the session never starts with one
        raise SystemExit(f"hansa: the coordinator's session was not started: {unsplit}")
    argv += ["--model", model]
    if effort:
        argv += ["--effort", effort]
    # after a /clear (hansa's daily one, or anyone's) the fresh context starts from the desk's state; the hook is
    # hansa's own code, never the desk's files
    hooks = {"SessionStart": [{"matcher": "clear", "hooks": [{"type": "command", "command": (
        f"{shlex.quote(str(Path(__file__).resolve().parent.parent / 'bin' / 'hansa'))} --desk "
        f"{shlex.quote(str(desk.root))} status")}]}]}
    own = own_folder_rules(env)  # the folder Claude Code runs from for this session: root's, when root runs it
    if theirs:  # the desk's .claude/settings.json is the account's, and its hooks would run as root: root's own
        rules = permissions(desk)
        rules["deny"] += own
        argv += ["--setting-sources", "user", "--settings", json.dumps({"permissions": rules, "hooks": hooks, **QUIET})]
    else:
        argv += ["--settings", json.dumps({"permissions": {"deny": own}, "hooks": hooks, **QUIET})]
    argv += _notified(desk, rest, clear)
    os.chdir(desk.root)
    os.execvpe(argv[0], argv + rest, env)


def no_read_paths(desk: Desk, plan: dict, theirs: str = "") -> tuple:
    """What the plan says the principal keeps workers from reading, as the session may be denied it, and what was
    said of the entries left out: what the desk's owner found (coordinator_desk), then the same checks again."""
    given = plan.get("no_read") if isinstance(plan.get("no_read"), list) else []
    refused = plan.get("no_read_refused") if isinstance(plan.get("no_read_refused"), list) else []
    said = [x for x in refused if isinstance(x, str)]
    # root does not read the account's settings: the account's hansa looked at the lanes' repositories
    good, more, stops = deny_parts(given, desk.root, None if theirs else desk.settings()["lanes"])
    if stops:  # as coordinator_desk: a path the plan gives that cannot be written as a rule stops the session
        raise SystemExit(f"hansa: {stop_text(stops)}")
    return good, [*said, *(m for m in more if m not in said)]


def read_folders(desk: Desk, plan: dict, env: dict, theirs: str = "", denied=()) -> tuple:
    """The folders of the plan the session may be given to read, and what was said of those it is not: what
    the desk's owner found (coordinator_desk), then the same checks as the user starting the session, with the
    Claude Code folder it runs from."""
    given = plan.get("read") if isinstance(plan.get("read"), list) else []
    refused = plan.get("read_refused") if isinstance(plan.get("read_refused"), list) else []
    said = [x for x in refused if isinstance(x, str)]
    own = [os.path.abspath(os.path.expanduser(env.get("CLAUDE_CONFIG_DIR") or "~/.claude"))]
    # root does not read the account's settings: the account's hansa looked at the providers' folders
    good, more = read_split(given, desk.root, None if theirs else desk.settings()["providers"], own, denied)
    return good, [*said, *(m for m in more if m not in said)]


def can_update(claude: str) -> bool:
    """Whether this account can write where the Claude Code at `claude` is installed, as its auto-updater
    would: the folder of the command, and the folder its link leads to, or for an npm install the
    node_modules folder holding the package. One installed by another account cannot update itself here."""
    real = resolved(claude) or claude
    parts = Path(real).parts
    home = (str(Path(*parts[:len(parts) - parts[::-1].index("node_modules")])) if "node_modules" in parts
            else os.path.dirname(real))
    return all(os.access(d, os.W_OK) for d in (os.path.dirname(os.path.abspath(claude)), home))


# The coordinator's session: no claude.ai connectors of the account it logs in with (a way to publish off the
# machine, and tools on every turn), while MCP servers set up on the machine stay; and no guessed next message
# in its input box, which a capture without colors shows as typed
QUIET = {"disableClaudeAiConnectors": True, "promptSuggestionEnabled": False}


# Files Claude Code keeps directly in its folder, denied even before it has written them
OWN_FILES = (".credentials.json", ".claude.json", "settings.json", "settings.local.json", "CLAUDE.md")


def own_folder_rules(env: dict) -> list:
    """Deny rules for the Claude Code folder the coordinator's session runs from (CLAUDE_CONFIG_DIR, or ~/.claude
    and ~/.claude.json): the login and the machine owner's files. Not its projects/, where the session keeps its
    memory. hansa's rules rely on two forms only: an exact path, and DIR/** for everything under DIR at any depth,
    names starting with a dot too (.hansa/**); so the files directly in the folder are named one by one, those
    there now and those Claude Code writes there, and no rule reaches into another subfolder."""
    given = env.get("CLAUDE_CONFIG_DIR")
    home = env.get("HOME") or os.path.expanduser("~")
    folder = os.path.abspath(os.path.expanduser(given) if given else os.path.join(home, ".claude"))
    paths = []
    for top in dict.fromkeys([folder, resolved(folder) or folder]):  # a link's target too
        try:
            names = [e.name for e in os.scandir(top) if not e.is_dir()]
        except OSError:
            names = []
        paths += [os.path.join(top, n) for n in sorted({*OWN_FILES, *names})] + [os.path.join(top, "sessions", "**")]
    if not given:
        dot = os.path.join(os.path.abspath(home), ".claude.json")
        paths += [dot, resolved(dot) or dot]
    return [f"{tool}(//{p.lstrip('/')})" for p in dict.fromkeys(paths) for tool in ("Read", "Edit")]


def _clear_daily(rest: list) -> tuple:
    """hansa coordinator's own option among claude's: --clear-daily HH-HH. (window or '', the rest for claude)."""
    out, clear, i = [], "", 0
    while i < len(rest):
        x = rest[i]
        if x == "--clear-daily" or x.startswith("--clear-daily="):
            if "=" in x:
                clear = x.partition("=")[2]
            elif i + 1 < len(rest):
                clear, i = rest[i + 1], i + 1
            else:
                clear = "?"
        else:
            out.append(x)
        i += 1
    if clear:
        try:
            notify.parse_window(clear)
        except ValueError as e:
            raise SystemExit(f"hansa: {e}")
    return clear, out


def _notified(desk: Desk, rest: list, clear: str = "") -> list:
    """Start the follower that posts to the session this process becomes (its pid stays when it execs Claude
    Code), at an inbox path it knows: claude's options for that path. Nothing when it cannot; the session can
    try again with hansa notify on."""
    given = next((rest[i + 1] if x == "--messaging-socket-path" and i + 1 < len(rest) else x.partition("=")[2]
                  for i, x in enumerate(rest) if x == "--messaging-socket-path"
                  or x.startswith("--messaging-socket-path=")), "")
    try:
        sock = given or notify.socket_path(os.getpid())
        if clear:  # the follower finds the same; said here, before the session takes the screen
            print(f"hansa: {notify.clear_said(os.getpid(), clear)}", file=sys.stderr)
        # no name of hansa's own: the session is named by its pid until Claude Code gives it one (notify.who)
        notify.start(desk, {"pid": os.getpid(), "start": notify.proc_start(os.getpid())}, sock, wait=False,
                     clear=clear)
    except (OSError, RuntimeError) as e:
        print(f"hansa: this session will not hear from hansa by itself ({e}); run hansa wait in the background, "
              f"or hansa notify on from the session", file=sys.stderr)
        return []
    return [] if given else ["--messaging-socket-path", sock]


def coordinator_desk(desk: Desk, by: int) -> dict:
    """The coordinator's part in the desk, done as the desk's owner: the card, the helpers and the
    permissions brought up to date, old helpers retired, and who runs the coordinator recorded. Returns
    what starting it needs from the desk: the lanes' checkouts, model, effort and the provider's environment."""
    s = desk.settings()
    co = s["coordinator"]
    p = s["providers"][co["provider"]]
    _bring(desk, CARD.read_text(encoding="utf-8"), desk.root / "CLAUDE.md", "card_refreshed")
    for f in sorted(HELPERS.glob("*.md")):
        _bring(desk, f.read_text(encoding="utf-8"), desk.root / ".claude" / "agents" / f.name, "helper_refreshed")
    mine = desk.root / ".claude" / "settings.json"  # hansa keeps its rules current there; the rest is left as it is
    cs = read_json(mine, {})
    cs = {**cs, "permissions": permissions(desk)} if isinstance(cs, dict) else {"permissions": permissions(desk)}
    _bring(desk, json_text(cs), mine, "permissions_refreshed", keep_as="coordinator-settings.json")
    if not desk.notes_dir.exists() and not desk.notes_dir.is_symlink():  # a desk made before notes/
        desk.notes_dir.mkdir()
    for name in RETIRED_HELPERS:
        old = desk.root / ".claude" / "agents" / name
        if old.is_file():
            desk.state_dir.mkdir(exist_ok=True)
            old.rename(desk.state_dir / f"{name}.{int(time.time())}")
            desk.event("helper_retired", file=name)
    desk.state_dir.mkdir(exist_ok=True)  # who runs the coordinator: hansa cost says when it cannot read its records
    write_json(desk.state_dir / "coordinator.json", {"uid": by, "user": account.name_of(by), "at": now()})
    kept, unkept, stops = no_read_parts(s, desk.root)
    if stops:  # Read would be open there to the session: it does not start
        raise SystemExit(f"hansa: {stop_text(stops)}")
    for line in unkept:
        desk.event("no_read_skipped", why=line)
    read, refused = read_split(s.get("coordinator_read", []), desk.root, s["providers"], denied=kept)
    for line in refused:
        desk.event("coordinator_read_skipped", why=line)
    return {"repos": sorted({ln["repo"] for ln in s["lanes"].values()}), "model": co.get("model") or p["model"],
            "effort": co.get("effort") or "", "env": provider_env(desk, co["provider"], p),
            "read": read, "read_refused": refused, "no_read": kept, "no_read_refused": unkept}


def cmd_coordinator_desk(a):
    print(json.dumps(coordinator_desk(Desk.find(a.desk), a.by)))


def _bring(desk: Desk, new: str, dst: Path, event: str, keep_as: str = "") -> None:
    """Put hansa's current version of a file into the desk (hansa was upgraded), keeping the old one in .hansa/."""
    if dst.is_file() and dst.read_text(encoding="utf-8") == new:
        return
    if dst.is_file():
        desk.state_dir.mkdir(exist_ok=True)
        (desk.state_dir / f"{keep_as or dst.name}.{int(time.time())}").write_text(dst.read_text(encoding="utf-8"), encoding="utf-8")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(new, encoding="utf-8")
    desk.event(event, file=dst.name)


LANE_LISTS = ("providers", "allow", "network", "write", "release_files")
LANE_NUMBERS = ("max_turns", "timeout_min", "merge_timeout_min", "preview_wait_s")
LANE_FIELDS = ("base", "providers", "allow", "network", "write", "env", "check", "merge_check", "preview", "setup",
               "release", "release_files", "max_turns", "timeout_min", "merge_timeout_min", "preview_wait_s", "note",
               *MESSAGE_FIELDS, "identity")
# empty (none) takes them out: the check's command and limit again, hansa's own message forms, the preview's wait,
# the identity of hansa's commits (then the repository's own, or hansa's)
LANE_UNSET = ("merge_check", "merge_timeout_min", "preview_wait_s", *MESSAGE_FIELDS, "identity")
OUTSIDE_WORKER = ("setup", "check", "merge_check", "preview")  # run by hansa itself, not by a worker or design session


def _shown(v) -> str:
    return json.dumps(v, ensure_ascii=False)


def _save(desk: Desk, new: dict) -> None:
    """Save the coordinator's change, checked as the page's are; on problems print them and change nothing."""
    problems = desk.save_settings(new, by="coordinator")
    if problems:
        raise SystemExit("hansa: nothing was changed:\n" + "\n".join(f"  {x}" for x in problems))


def _lane_value(field: str, values: list):
    if values == ["none"]:  # the help's word for empty: what no value gives
        values = []
    elif "none" in [x for v in values for x in (v.split(",") if field == "providers" else [v])]:
        raise SystemExit(f"hansa: none means an empty {field}, so it cannot come with other values "
                         f"({' '.join(values)}): give none alone, or the values without it. Nothing was changed")
    if field == "allow":  # a rule given twice is kept once
        return list(dict.fromkeys(x for x in values if x))
    if field in LANE_LISTS:
        return [x for v in values for x in (v.split(",") if field == "providers" else v.split()
                                            if field == "release_files" else [v]) if x]
    if field == "env":
        env = {}
        for v in values:
            k, eq, val = v.partition("=")
            if not eq:
                raise SystemExit(f"hansa: env takes NAME=VALUE pairs; {v!r} has no '='. Nothing was changed")
            env[k] = val
        return env
    if len(values) > 1:
        raise SystemExit(f"hansa: {field} takes one value; quote it if it has spaces. Nothing was changed")
    if field in MESSAGE_FIELDS and values and message_problem(values[0]):
        raise SystemExit(f"hansa: {field} {_shown(values[0])} {message_problem(values[0])}. Nothing was changed")
    if field == "identity" and values and identity_problem(values[0].strip()):
        raise SystemExit(f"hansa: identity {_shown(values[0])} {identity_problem(values[0].strip())}. "
                         f"Nothing was changed")
    if field == "identity" and values:
        values = [values[0].strip()]
    if field in LANE_UNSET and not "".join(values).strip():
        return None
    if field in LANE_NUMBERS:
        try:
            return int(values[0])
        except (IndexError, ValueError):
            raise SystemExit(f"hansa: {field} takes one whole number. Nothing was changed")
    if field == "base" and not "".join(values).strip():
        raise SystemExit("hansa: base takes the name of a branch. Nothing was changed")
    return values[0] if values else ""


def _loopback_warning(name: str, hosts: list) -> str:
    """What localhost on a lane's network list opens on macOS, where it lets the sandbox reach the whole
    loopback: the servers listening there now, one line each. '' on Linux, where a worker's sandbox has
    a loopback of its own, and for a list without localhost."""
    if not ON_MAC or not any(is_local(h) for h in hosts):
        return ""
    head = (f"Warning: on macOS, localhost on lane {name}'s network list lets its workers, setup and check bind "
            "ports and connect to every server listening on this machine's loopback, not only their own: a local "
            "proxy among them reaches the internet whatever the network list says. Keep it only if this Mac's "
            "loopback holds nothing a worker must not reach; otherwise remove it (hansa lane set "
            f"{name} network ...).")
    got = mac_loopback_listeners()
    if got is None:
        return head + "\nWhat listens on the loopback now could not be read here."
    found, unread = got
    servers = {}
    for prog, pid, acct, addr, port in found:
        servers.setdefault((prog, pid, acct, port), []).append(f"{addr}:{port}")
    lines = [f"  {prog} (pid {pid}, {acct}): {', '.join(addrs)}" for (prog, pid, acct, _), addrs in servers.items()]
    text = head + "\n" + ("What listens on this machine's loopback now; a worker can reach every one:\n"
                          + "\n".join(lines) if lines else "Nothing that hansa can look at listens on the loopback now.")
    if unread:
        text += (f"\n{unread} processes of other accounts could not be looked at (only root can); what they listen "
                 "on (sshd, a database run as its own user) a worker can reach too.")
    return text


SIBLING_FIELDS = ("network", "write", "env", "max_turns", "timeout_min")  # what a lane's tasks can reach and how long


def _differs_from_siblings(name: str, ln: dict, lanes: dict) -> list:
    """How a new lane differs from the lanes already on its repository, one line per field; nothing is copied."""
    others = {n: x for n, x in lanes.items() if n != name and x.get("repo") and str(Path(x["repo"]).resolve()) == ln["repo"]}
    blank = {"write": [], **default_lane(ln["repo"], "main")}  # what a field left out of settings means
    lines = []
    for f in SIBLING_FIELDS:
        mine = ln.get(f, blank[f])
        theirs = {}
        for n, x in sorted(others.items()):
            v = x.get(f, blank[f])
            if v != mine and not (f in LANE_LISTS and set(v) == set(mine)):
                theirs.setdefault(_shown(v), []).append(n)
        if theirs:
            lines.append(f"{f}: {name} has {_shown(mine)}; " + "; ".join(
                f"{', '.join(ns)} {'has' if len(ns) == 1 else 'have'} {v}" for v, ns in theirs.items()))
    return lines


def _changed_from(name: str, ln: dict, other: str, theirs: dict) -> list:
    """What a lane made with --like OTHER was given in place of OTHER's settings, one line per field."""
    blank = {"write": [], "check": "", "setup": "", **default_lane(ln["repo"], "main")}
    return [f"{f}: {name} has {_shown(ln.get(f, blank.get(f, '')))}; {other} has {_shown(theirs.get(f, blank.get(f, '')))}"
            for f in dict.fromkeys([*theirs, *ln]) if ln.get(f, blank.get(f)) != theirs.get(f, blank.get(f))]


def cmd_lane(a):
    desk = Desk.find(a.desk)
    if not a.action:
        lanes = desk.settings()["lanes"]
        rows = [(name, ln["repo"], f"base {ln.get('base', 'main')}", "providers " + " > ".join(ln["providers"])
                 + "".join(f"  {f} {_shown(ln[f])}" for f in MESSAGE_FIELDS if ln.get(f)))
                for name, ln in sorted(lanes.items())]
        widths = [max((len(r[i]) for r in rows), default=0) for i in range(3)]
        for r in rows:
            print("  ".join(f"{x:<{w}}" for x, w in zip(r, widths)) + "  " + r[3])
        print("no lanes yet: hansa lane add L --repo PATH" if not lanes else "more on one: hansa explain lane L")
        return
    if a.action == "try":
        return _lane_try(desk, a)
    with desk.lock():  # the settings read here are the settings changed here
        s = desk.settings()
        new = json.loads(json.dumps(s))
        if a.action == "add":
            if answer_name(a.name):
                raise SystemExit(answer_lane_refused(a.name))
            if a.name in s["lanes"]:
                raise SystemExit(f"hansa: there is already a lane {a.name}; change it with hansa lane set {a.name} ...")
            # none as lane set takes it: alone it means empty, with other values it is refused
            given = {f: _lane_value(f, v if isinstance(v, list) else [v]) for f, v in (
                ("providers", a.providers), ("check", a.check), ("setup", a.setup), ("allow", a.allow),
                ("network", a.network), ("merge_check", a.merge_check), ("merge_timeout_min", a.merge_timeout),
                ("release_files", a.release_files), ("identity", a.identity))
                if v is not None}
            if a.base is not None:
                _lane_value("base", [a.base])
            if a.like:
                if a.repo is not None:  # an empty --repo too: it is not ignored
                    raise SystemExit(f"hansa: --like {a.like} takes the repository of lane {a.like}, which its "
                                     f"check and setup are for, so it cannot come with --repo; nothing was changed")
                if a.like not in s["lanes"]:
                    raise SystemExit(f"hansa: there is no lane {a.like} to copy; lanes: "
                                     f"{', '.join(sorted(s['lanes'])) or 'none'}. Nothing was changed")
                if given.get("providers") == []:
                    raise SystemExit("hansa: a lane needs a provider; nothing was changed")
                like = s["lanes"][a.like]
                bad = [r for r in like.get("allow", []) if refused_rule(r)]
                if bad:  # copied or not, they are refused in that lane
                    raise SystemExit(f"hansa: lane {a.like}'s allowed commands hold rules no longer accepted: "
                                     + "; ".join(f"{r} {refused_rule(r)}" for r in bad)
                                     + f". Remove them with: {allow_fix(a.like, like.get('allow', []))}. Nothing was "
                                     f"changed")
                ln = json.loads(json.dumps(like))  # a copy: later changes to the other lane do not reach this one
                ln.update(given, **({"base": a.base} if a.base else {}))
                for f in ("check", "setup", "merge_check", "merge_timeout_min", "identity"):
                    if ln.get(f) in ("", None):  # none: unset, as hansa lane set leaves it
                        ln.pop(f, None)
                repo, base = ln["repo"], ln.get("base", "main")
            elif not a.repo:
                raise SystemExit("hansa: say which git checkout the lane works on (--repo PATH), or which lane to "
                                 "copy (--like L); nothing was changed")
            else:
                repo = resolved(a.repo)
                if not repo:
                    raise SystemExit(f"hansa: cannot follow the links of {a.repo} to see where it leads; "
                                     "nothing was changed")
            theirs = account.foreign_repo(repo)
            if theirs:
                raise SystemExit(f"hansa: {theirs}; nothing was changed")
            for x in dict.fromkeys([repo, *([a.repo] if a.repo else [])]):  # the coordinator's rules name it
                if path_rule_problem(x):
                    raise SystemExit(f"hansa: {path_rule_problem(x)}; nothing was changed")
            if not a.like:
                base = lane_base(repo, a.base, "nothing was changed")
                ln = default_lane(repo, base, given.pop("providers", None) or None)
                ln.update({k: v for k, v in given.items() if v or isinstance(v, int)})
            new["lanes"][a.name] = ln
            _save(desk, new)
            desk.event("lane_added", lane=a.name, **({"like": a.like} if a.like else {}))
            (desk.lanes_dir / a.name).mkdir(parents=True, exist_ok=True)
            brief = desk.lanes_dir / a.name / "brief.md"
            aside = None
            if a.like:  # the standing instructions, copied as the settings are
                copied = desk.lanes_dir / a.like / "brief.md"
                text = copied.read_text(encoding="utf-8") if copied.is_file() else ""
                old = brief.read_text(encoding="utf-8") if brief.is_file() else ""
                if old.strip() and old != text:  # left by an earlier lane of this name: set aside, not lost
                    aside = brief.with_name(f"brief-before-{time.strftime('%Y%m%d-%H%M%S')}.md")
                    aside.write_text(old, encoding="utf-8")
                brief.write_text(text, encoding="utf-8")
            elif not brief.exists():
                brief.write_text("", encoding="utf-8")
            print(f"lane {a.name} added: {repo}, base {base}, providers {' > '.join(ln['providers'])}, "
                  f"stopped at {ln['max_turns']} turns or {ln['timeout_min']} minutes"
                  + "".join(f"; {f} {_shown(ln[f])}" for f in ("allow", "network", "setup", "check", "merge_check", "release_files",
                                                              "merge_timeout_min", "identity") if ln.get(f)))
            if aside:
                print(f"An earlier lane {a.name} left standing instructions in {brief}; lane {a.name} now has "
                      f"{a.like}'s, and the earlier ones are kept in {aside}")
            for f in OUTSIDE_WORKER:
                if ln.get(f):
                    desk.event("lane_command_changed", lane=a.name, field=f, old="", new=ln[f])
            if ln["network"]:
                desk.event("lane_network_changed", lane=a.name, old=[], new=ln["network"])
                print(f"The hosts the lane's workers may reach during a task are recorded: {_shown(ln['network'])}. "
                      f"Tell the principal, in the conversation, what you listed and why.")
                warning = _loopback_warning(a.name, ln["network"])
                if warning:
                    print(desk.redact(warning))
            if ln.get("allow"):
                desk.event("lane_allow_changed", lane=a.name, old=[], new=ln["allow"])
                print(f"The lane's allowed commands, which its workers, verifiers and design sessions are given, are "
                      f"recorded: {_shown(ln['allow'])}. Tell the principal, in the conversation, what you listed "
                      f"and why.")
            if ln.get("setup") or ln.get("check") or ln.get("merge_check"):
                print("The lane's setup and check run outside the worker, so they are recorded. Tell the principal, "
                      "in the conversation, what they run and why.")
            if not ln.get("check") and not ln.get("merge_check"):
                print(f"Lane {a.name} has no check, so its tasks will be merged untested: hansa merge checks nothing "
                      f"before it merges them, not even when {base} has moved since a task was made. Set one with: "
                      f"hansa lane set {a.name} check \"<the command that runs the tests>\"")
            changed = []
            if a.like:
                changed = _changed_from(a.name, ln, a.like, s["lanes"][a.like])
                print(desk.redact(f"Lane {a.name} is a copy of lane {a.like} as it is now; later changes to "
                                  f"{a.like} do not reach it. "
                                  + ("Changed from " + a.like + ":\n" + "\n".join(f"  {x}" for x in changed)
                                     if changed else "Nothing was changed from it.")))
            others = {n: x for n, x in s["lanes"].items() if n != a.like}
            differs = _differs_from_siblings(a.name, ln, others)
            if differs:
                print(desk.redact(f"Lane {a.name} differs from the other lanes on {repo}; nothing was copied from "
                                  f"them{' but ' + a.like if a.like else ''} (change it with hansa lane set {a.name} "
                                  f"FIELD ...):\n"
                                  + "\n".join(f"  {x}" for x in differs)))
            if differs or changed:
                print(f"try it: hansa lane try {a.name}")
            _say_forbidden(desk, a.name, ln)
            return
        if a.action == "remove":
            return _lane_remove(desk, a, s, new)
        if a.name not in s["lanes"]:
            raise SystemExit(f"hansa: there is no lane {a.name}; lanes: {', '.join(sorted(s['lanes'])) or 'none'}")
        if a.action == "target":
            return _lane_target(desk, a, new)
        if a.action == "brief":  # through hansa only: on a server the coordinator's own tools run as root
            brief = desk.lanes_dir / a.name / "brief.md"
            if not a.text and not a.file:
                old = brief.read_text(encoding="utf-8") if brief.is_file() else ""
                print(old if old.strip() else f"lane {a.name} has no standing instructions")
                return
            text = _text(a)
            brief.parent.mkdir(parents=True, exist_ok=True)
            brief.write_text(text, encoding="utf-8")
            desk.event("lane_brief_changed", lane=a.name, new=text)
            print(f"lane {a.name}: standing instructions replaced ({len(text)} characters); "
                  f"every worker in the lane gets them from its next task on")
            return
        value = _lane_value(a.field, a.values)
        old = s["lanes"][a.name].get(a.field, [] if a.field in LANE_LISTS else "")
        if value is None:  # unset: the check's command and limit, hansa's own message forms
            new["lanes"][a.name].pop(a.field, None)
            value = ""
        else:
            new["lanes"][a.name][a.field] = value
        _save(desk, new)
        desk.event("lane_set", lane=a.name, field=a.field)
    print(f"lane {a.name} {a.field}: {_shown(old)} -> {_shown(value)}")
    if a.field in OUTSIDE_WORKER and old != value:
        desk.event("lane_command_changed", lane=a.name, field=a.field, old=old, new=value)
        print(f"The lane's {a.field} runs outside the worker, so this change is recorded. Tell the principal, in "
              f"the conversation, what you changed and why.")
    if a.field == "release" and old != value:
        desk.event("lane_command_changed", lane=a.name, field="release", old=old, new=value)
        print(f"The lane's release command sends its base branch out: hansa runs it as the desk's account, outside "
              f"the sandbox, only after the principal says yes on the page, so this change is recorded and a release "
              f"asked with the old command no longer runs. Tell the principal, in the conversation, what you changed "
              f"and why. Ask for a release with: hansa release {a.name} \"<note>\"")
    if a.field == "release":
        _say_forbidden(desk, a.name, new["lanes"][a.name], "")
    if a.field == "network" and set(old) != set(value):  # the same hosts in another order change nothing
        desk.event("lane_network_changed", lane=a.name, old=old, new=value)
        print(f"The hosts the lane's workers may reach during a task changed: {_shown(old)} -> {_shown(value)}, so "
              f"this change is recorded. Tell the principal, in the conversation, what you changed and why.")
    if a.field == "network" and not any(is_local(h) for h in old):  # told once, when localhost comes on the list
        warning = _loopback_warning(a.name, value)
        if warning:
            print(desk.redact(warning))
    if a.field == "allow" and set(old) != set(value):
        desk.event("lane_allow_changed", lane=a.name, old=old, new=value)
        print(f"The lane's allowed commands, which its workers, verifiers and design sessions are given, changed: "
              f"{_shown(old)} -> {_shown(value)}, so this change is recorded. Tell the principal, in the "
              f"conversation, what you changed and why.")
    if a.field == "write" and set(old) != set(value):
        desk.event("lane_write_changed", lane=a.name, old=old, new=value)
        print(f"The folders the lane's workers may write besides the worktree changed: {_shown(old)} -> "
              f"{_shown(value)}, so this change is recorded. Tell the principal, in the conversation, what you "
              f"changed and why.")
    ln = new["lanes"][a.name]
    if a.field in ("setup", "check", "network", "write") and old != value and (ln.get("setup") or ln.get("check")):
        print(f"To see that the lane's setup and check work in its sandbox before a task needs them: "
              f"hansa lane try {a.name}")
    if a.field in ("setup", "merge_check", "network", "write") and old != value and own_merge_check(ln):
        print(f"To see that the lane's merge check works in its sandbox before a merge needs it: "
              f"hansa lane try {a.name} --merge-check")


def _lane_worktrees(desk: Desk, name: str, lanes: dict) -> list:
    """The worktrees lane NAME made on the desk that are still there: its own and its design sessions'.
    A folder another lane has by its name is that lane's: x-design is lane x's design worktree."""
    out = []
    for wt in (desk.worktrees_dir / name, desk.worktrees_dir / f"{name}-design"):
        theirs = wt.name != name and wt.name in lanes
        theirs = theirs or (wt.name == name and name not in lanes and name.endswith("-design")
                            and name[:-len("-design")] in lanes)
        if os.path.lexists(wt) and not theirs:
            out.append(wt)
    return out


def _worktree_repo(desk: Desk, wt: Path, repos: list):
    """The repository whose git lists this worktree: the one hansa remembered when it made it, or one of
    these; None when none does."""
    from .run import _known_git_files, git_admin_dirs
    line = _known_git_files(desk).get(str(wt)) or ""
    found = []
    if line.startswith("gitdir:"):  # <common>/worktrees/<id>; its commondir file leads back to <common>
        admin = Path(line[len("gitdir:"):].strip())
        try:
            found.append(str((admin / (admin / "commondir").read_text(encoding="utf-8").strip()).resolve()))
        except OSError:
            pass
    for repo in dict.fromkeys([*found, *repos]):
        try:
            if len(git_admin_dirs(Path(repo), wt)) == 1:
                return Path(repo)
        except (RuntimeError, OSError):
            continue
    return None


def _worktree_state(desk: Desk, wt: Path, repos: list) -> dict:
    """What a worktree holds that removing it would lose: changes not committed and commits no branch keeps.
    "why": hansa cannot tell, or runs no git there."""
    from .run import check_git_file, sandbox_leftovers, wgit
    repo = _worktree_repo(desk, wt, repos)
    if repo is None:
        return {"why": "no repository hansa knows of lists it as a worktree, so hansa cannot tell whose it is "
                       "and runs no git there"}
    try:
        check_git_file(desk, wt, repo)  # no git through a .git file hansa did not see git write
        left = set(sandbox_leftovers(desk, wt))
        try:
            changed = wgit(desk, wt, "diff", "--name-only", "-z", "HEAD").split("\0")
        except RuntimeError:  # no commit at HEAD: everything is uncommitted
            changed = wgit(desk, wt, "ls-files", "-z").split("\0")
        untracked = wgit(desk, wt, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
        try:
            commits = wgit(desk, wt, "log", "--format=%h %s", "HEAD", "--not", "--branches").splitlines()
        except RuntimeError:
            commits = []
    except RuntimeError as e:
        return {"why": str(e)}
    changes = [f for f in dict.fromkeys([*changed, *untracked]) if f and f not in left]
    return {"repo": repo, "changes": changes, "commits": commits}


def _held_text(wt: Path, st: dict) -> str:
    lines = [f"  {wt}:"]
    if st["changes"]:
        more = len(st["changes"]) - 10
        lines.append(f"    not committed: {', '.join(st['changes'][:10])}" + (f" (and {more} more)" if more > 0 else ""))
    if st["commits"]:
        more = len(st["commits"]) - 5
        lines += [f"    on no branch: {c}" for c in st["commits"][:5]] + ([f"    (and {more} more)"] if more > 0 else [])
    return "\n".join(lines)


def _remove_worktree(desk: Desk, lane: str, wt: Path, st: dict, keep: bool, ln: dict | None = None) -> str:
    """git worktree remove, after putting what it holds on a new branch when keep; the branch's name or ''. ln: the
    lane's settings as they were before it was removed, whose identity the kept commit carries."""
    from .run import _remember_git_file, commit_work, identity_env, wgit
    kept = ""
    if keep and (st["changes"] or st["commits"]):
        kept = f"hansa-kept/{wt.name}-{time.strftime('%Y%m%d-%H%M%S')}"
        wgit(desk, wt, "checkout", "-q", "-b", kept)
        commit_work(desk, wt, f"Left in worktree {wt.name} when lane {lane} was removed", kept,
                    ident=identity_env({**(ln or {}), "repo": st["repo"]}))
    # --force: what a worker's sandbox left there is nobody's work, and git would refuse for it
    git("worktree", "remove", "--force", str(wt), cwd=st["repo"])
    _remember_git_file(desk, wt, None)
    desk.event("lane_worktree_removed", lane=lane, worktree=str(wt), **({"kept": kept} if kept else {}))
    return kept


def _target_text(name: str, t: dict, ln: dict) -> str:
    return (f"{name or '(unnamed: the release command)'}: {t.get('command')}  "
            f"[{t.get('timeout_min') or ln.get('timeout_min', 240)} min"
            + ("" if t.get("timeout_min") else ", the lane's") + "]"
            + (f"  after {t['after']}" if t.get("after") else "")
            + f"  ({release.runs_text(t)})")


def _lane_target(desk: Desk, a, new: dict) -> None:
    """hansa lane target L [NAME [COMMAND|none] [--minutes N|none] [--after T|none] [--fixed [none]]]: list, set
    or remove a lane's named release targets."""
    ln = new["lanes"][a.name]
    if not a.target:
        shown = lane_targets(ln)
        for name, t in sorted(shown.items()):
            print(_target_text(name, t, ln))
        if not shown:
            print(f"lane {a.name} has no release target: hansa lane target {a.name} NAME \"<command>\" names one "
                  f"(or hansa lane set {a.name} release \"<command>\" sets its one unnamed target)")
        return
    if len(a.command) > 1:
        raise SystemExit("hansa: a target takes one command line; quote it if it has spaces. Nothing was changed")
    cmd = a.command[0] if a.command else None
    named = ln.get("targets") if isinstance(ln.get("targets"), dict) else {}
    old = named.get(a.target)
    if cmd == "none":  # the help's word for taking it out
        if a.minutes is not None or a.after is not None or a.fixed is not None:
            raise SystemExit("hansa: none takes the target out, so it cannot come with --minutes, --after or --fixed. "
                             "Nothing was changed")
        if old is None:
            raise SystemExit(f"hansa: lane {a.name} has no release target {a.target}; its targets: "
                             f"{', '.join(sorted(named)) or 'none'}. Nothing was changed")
        followers = sorted(n for n, x in named.items() if isinstance(x, dict) and x.get("after") == a.target)
        if followers:
            raise SystemExit(f"hansa: target {', '.join(followers)} of lane {a.name} goes out only after "
                             f"{a.target}; change that first (hansa lane target {a.name} {followers[0]} --after none). "
                             "Nothing was changed")
        del named[a.target]
        if not named:
            ln.pop("targets", None)
        _save(desk, new)
        desk.event("lane_set", lane=a.name, field="targets", target=a.target)
        desk.event("lane_command_changed", lane=a.name, field="release", target=a.target, old=old.get("command", ""),
                   new="")
        print(f"lane {a.name} release target {a.target} is taken out ({old.get('command')}); a release asked for it "
              "no longer runs. Tell the principal, in the conversation, what you changed and why.")
        return
    if old is None and not cmd:
        raise SystemExit(f"hansa: lane {a.name} has no release target {a.target}; a new one needs its command: "
                         f"hansa lane target {a.name} {a.target} \"<command>\". Nothing was changed")
    t = dict(old or {})
    if cmd is not None:
        if not cmd.strip():
            raise SystemExit("hansa: a target's command cannot be empty (none takes the target out). Nothing was changed")
        t["command"] = cmd
    if a.minutes is not None:
        if a.minutes == "none":
            t.pop("timeout_min", None)
        else:
            try:
                t["timeout_min"] = int(a.minutes)
            except ValueError:
                raise SystemExit("hansa: --minutes takes one whole number from 1 to 1440, or none (the lane's "
                                 "timeout_min). Nothing was changed")
    if a.after is not None:
        if a.after == "none":
            t.pop("after", None)
        else:
            t["after"] = a.after
    if a.fixed is not None:
        if a.fixed == "none":
            t.pop("fixed", None)
        elif a.fixed == "yes":
            t["fixed"] = True
        else:
            raise SystemExit("hansa: --fixed takes no value (runs from a fixed checkout), or none (runs in the "
                             "repository again). Nothing was changed")
    ln["targets"] = {**named, a.target: t}
    _save(desk, new)
    desk.event("lane_set", lane=a.name, field="targets", target=a.target)
    print(f"lane {a.name} release target {a.target}: {_shown(old) if old else 'none'} -> {_shown(t)}")
    if (old or {}).get("command") != t["command"]:
        desk.event("lane_command_changed", lane=a.name, field="release", target=a.target,
                   old=(old or {}).get("command", ""), new=t["command"])
        print(f"A release target's command sends the lane's base branch out: hansa runs it as the desk's account, "
              f"outside the sandbox, only after the principal says yes on the page (each target is ticked on its own "
              f"for going out without asking), so this change is recorded and a release asked with the old command no "
              f"longer runs. Tell the principal, in the conversation, what you changed and why. Ask for a release "
              f"with: hansa release {a.name} {a.target} \"<note>\"")
    _say_forbidden(desk, a.name, ln, a.target)
    if bool((old or {}).get("fixed")) != bool(t.get("fixed")):
        print(f"It now {release.runs_text(t)}"
              + (f": its command runs in a checkout of the release's commit made for it, so it names that commit as "
                 f"$HANSA_RELEASE_COMMIT (or HEAD), never the base branch, which may have moved by then; "
                 f"$HANSA_RELEASE_REPO is the lane's repository" if t.get("fixed") else "")
              + ". A tick for going out without asking stops counting until the principal takes it back in on "
                "the page (its button for that, or unticking and ticking it again, then Save; a plain Save keeps "
                "the old entry); "
                "tell the principal, in the conversation, what you changed and why.")


def _say_forbidden(desk: Desk, name: str, ln: dict, only: str | None = None) -> None:
    """Say at once when a release command just set holds one of the principal's forbidden words."""
    hits = release.command_hits(ln, desk.settings().get("release_forbidden") or [], only)
    if hits:
        print(release.command_hits_text(desk, name, hits))


def _lane_remove(desk: Desk, a, s: dict, new: dict) -> None:
    """Remove a lane and its worktrees, or the worktrees an earlier lane remove left of a lane that is gone.
    Work a worktree holds that no branch keeps is not lost: without --keep or --discard nothing is removed."""
    lanes, name = s["lanes"], a.name
    known = name in lanes
    wts = _lane_worktrees(desk, name, lanes)
    if not known and not wts:
        raise SystemExit(f"hansa: there is no lane {name}, and no worktree of one; lanes: "
                         f"{', '.join(sorted(lanes)) or 'none'}")
    if a.keep and a.discard:
        raise SystemExit("hansa: --keep puts what the worktree holds on a new branch and --discard drops it; choose "
                         "one. Nothing was changed")
    what = f"lane {name}" if known else f"lane {name}, which is gone,"
    tasks = [m for m in desk.tasks() if m.get("lane") == name]
    busy = [m["id"] for m in tasks if m["state"] in ("queued", "running", "merging")]
    if busy:
        raise SystemExit(f"hansa: {what} still has {', '.join(busy)} queued or running; nothing was "
                         f"changed. Wait for them (hansa wait) or cancel them first")
    # merging a finished task needs its lane's repo and base
    review = [m["id"] for m in tasks if m["state"] == "done" and m.get("kind") != "verify"]
    if review:
        raise SystemExit(f"hansa: {what} still has {', '.join(review)} finished and waiting for review; "
                         f"nothing was changed. Merging needs the lane: clear them first with hansa merge "
                         f"or hansa cancel")
    if read_try(desk, name).get("phase") in ("queued", "running"):
        raise SystemExit(f"hansa: a try of lane {name} is queued or running in its worktree; nothing was changed. "
                         f"Wait for it (hansa lane try {name} waits too), then remove the lane")
    repos = list(dict.fromkeys([*([lanes[name]["repo"]] if known else []), *(x["repo"] for x in lanes.values())]))
    states = {wt: _worktree_state(desk, wt, repos) for wt in wts}
    held = {wt: st for wt, st in states.items() if st.get("changes") or st.get("commits")}
    if held and not (a.keep or a.discard):
        raise SystemExit(
            f"hansa: {'lane ' + name + ' was not removed' if known else 'the worktree of lane ' + name + ' was not removed'}"
            f": its worktree holds work that no branch keeps, which removing it would lose:\n"
            + "\n".join(_held_text(wt, st) for wt, st in held.items())
            + f"\nTo keep it on a new branch (hansa-kept/{name}-DATE) of its repository and then remove "
              f"{'the lane and ' if known else ''}the worktree: hansa lane remove {name} --keep. To drop it: "
              f"hansa lane remove {name} --discard. Nothing was changed")
    ln = dict(lanes[name]) if known else None  # its identity, for the commit --keep makes after it is gone
    if known:
        del new["lanes"][name]
        _save(desk, new)
        print(f"lane {name} removed; its brief stays in {desk.lanes_dir / name}")
    for wt, st in states.items():
        if st.get("why"):
            print(f"Its worktree {wt} stays: {st['why']}. The principal can look at it and delete it; if git in its "
                  f"repository still lists it, git worktree prune there clears that")
            continue
        try:
            kept = _remove_worktree(desk, name, wt, st, a.keep, ln)
        except RuntimeError as e:
            print(f"Its worktree {wt} stays: {e}. hansa lane remove {name} tries again")
            continue
        print(f"Its worktree {wt} is removed, with git worktree remove in {st['repo']}, as the desk's account"
              + (f"; what it held that no branch kept is on branch {kept} there" if kept else
                 "; what it held that no branch kept is dropped" if held.get(wt) else ""))


def _lane_try(desk: Desk, a) -> None:
    """Have hansa serve run the lane's setup and check once, as for a task but with no worker, and wait for it.
    --at COMMIT: only at that commit, which must be the base's tip; a try queued or running at another commit
    (or of the other kind) is waited for, never taken as the answer. --log: print the last try's kept output."""
    if a.log:
        return _lane_try_log(desk, a)
    at, waited = "", None
    while True:
        with desk.lock():  # no task starts on the lane between this look and the try being queued
            ln = desk.settings()["lanes"].get(a.name)
            if not ln:
                raise SystemExit(f"hansa: there is no lane {a.name}; lanes: {', '.join(sorted(desk.settings()['lanes'])) or 'none'}")
            if a.merge_check and not merge_command(ln):
                raise SystemExit(f"hansa: lane {a.name} has no merge check and no check, so there is no merge check to "
                                 f"try. Set one with hansa lane set {a.name} merge_check|check \"<command>\"")
            if not a.merge_check and not ln.get("setup") and not ln.get("check"):
                raise SystemExit(f"hansa: lane {a.name} has no setup and no check, so there is nothing to try. Set them "
                                 f"with hansa lane set {a.name} setup|check \"<command>\""
                                 + (f"; its merge check: hansa lane try {a.name} --merge-check"
                                    if ln.get("merge_check") else ""))
            if a.at:
                at = _try_at(ln, a, waited)
            t = read_try(desk, a.name)
            if t.get("phase") in ("queued", "running"):  # asked for before, by a command that stopped waiting
                if not at or (bool(t.get("merge")) == bool(a.merge_check)
                              and (t.get("onto") or t.get("at")) == at):
                    print(f"a try of lane {a.name}" + (f" at {at[:10]}" if at else "")
                          + f" is already {t['phase']}; waiting for it", flush=True)
                    break
                waited = t
            else:
                holder = lane_holder(desk, a.name)
                if holder:
                    raise SystemExit(f"hansa: lane {a.name} is busy with {holder['id']} ({holder['state']}: "
                                     f"{holder['title']}); nothing was run. Try again when it is free: "
                                     + (f"{notify.wait_step(desk, holder['id'], True)}; then hansa lane try {a.name}"
                                        if notify.hears_of(desk, holder["id"]) else
                                        f"hansa wait {holder['id']}, then hansa lane try {a.name}"))
                st = server_state(desk)
                if st["state"] == "gone":
                    raise SystemExit("hansa: hansa serve is not running, and a try runs through it (with its environment, "
                                     "as tasks do); nothing was run. Start it, then try again")
                if st["state"] == "stuck":
                    raise SystemExit(f"hansa: hansa serve (pid {st['pid']}) is busy or stuck: no round finished for "
                                     f"{quiet_text(st['quiet'])}, and a try runs through it; nothing was run. See hansa "
                                     f"explain server, then try again")
                t = {"id": uuid.uuid4().hex[:12], "phase": "queued", "asked": now(),
                     **({"merge": True} if a.merge_check else {}), **({"at": at} if at else {})}
                write_try(desk, a.name, t)
                desk.event("lane_try_queued", lane=a.name, **({"merge_check": True} if a.merge_check else {}),
                           **({"at": at} if at else {}))
                steps = ["setup"] * bool(ln.get("setup")) + (
                    [f"merge check ({merge_command(ln)}, stopped after {merge_minutes(ln)} minutes)"] if a.merge_check
                    else ["check"] * bool(ln.get("check")))
                print(f"lane {a.name}: hansa serve runs its " + " and ".join(steps)
                      + f" in its worktree, at {ln.get('base') or 'main'}'s tip"
                      + (f" ({at[:10]})" if at else "") + ", in its sandbox; waiting for the result", flush=True)
                break
        where = (f"at {waited['onto'][:10]}" if waited.get("onto") else
                 "at the tip it starts on" if not waited.get("at") else f"at {waited['at'][:10]}")
        print(f"a try of lane {a.name} is {waited['phase']} {where}"
              + (" with the merge check" if waited.get("merge") else "") + f", not the one asked for at {at[:10]}"
              + (" with the merge check" if a.merge_check else "")
              + "; waiting for it to end, then trying at " + at[:10] + " if the base's tip is still there", flush=True)
        _await_try(desk, a, waited["id"], other=True)
    now_t = _await_try(desk, a, t["id"])
    print(desk.redact(views.try_text(a.name, now_t)))
    if not now_t.get("ok") or (at and now_t.get("onto") != at):
        raise SystemExit(1)


def _try_at(ln: dict, a, waited) -> str:
    """The commit --at names, in full, refused (nothing run) unless it is the base's tip now."""
    repo, base = Path(ln["repo"]), ln.get("base") or "main"
    try:
        at = git("rev-parse", "--verify", "--quiet", f"{a.at}^{{commit}}", cwd=repo)
    except RuntimeError:
        at = ""
    if not at:
        raise SystemExit(f"hansa: {a.at} is no commit of lane {a.name}'s repository; nothing was run")
    tip = git("rev-parse", base, cwd=repo)
    if tip != at:
        raise SystemExit(f"hansa: {base}'s tip is {tip[:10]}, not {at[:10]}"
                         + (", since the try hansa waited for ended" if waited else "")
                         + f"; nothing was run. A try with --at answers for that commit only: hansa lane try "
                         f"{a.name}{' --merge-check' if a.merge_check else ''} --at {tip[:10]} tries the tip")
    return at


def _await_try(desk: Desk, a, tid: str, other: bool = False) -> dict:
    """Wait for the try tid to end; its record. other: a try at another commit, waited for only so that the one
    asked for can run after it: it ends for this when it is gone or replaced too."""
    gone = None
    while True:
        now_t = read_try(desk, a.name)
        if now_t.get("id") != tid:
            if other:
                return now_t
            raise SystemExit(f"hansa: the try of lane {a.name} was replaced by another; see hansa lane try {a.name}")
        if now_t.get("phase") == "done":
            return now_t
        st = server_state(desk)  # stuck runs no rounds, so it neither starts nor finishes the try
        if st["state"] not in ("gone", "stuck"):
            gone = None
        elif gone is None:
            gone = time.time()
        elif time.time() - gone > 60:
            why = ("stopped" if st["state"] == "gone" else
                   f"(pid {st['pid']}) is busy or stuck, with no round finished for {quiet_text(st['quiet'])},")
            again = "Start hansa serve" if st["state"] == "gone" else "Once its rounds come again (hansa explain server)"
            if other:
                raise SystemExit(f"hansa: hansa serve {why} before the try hansa waited for ended; nothing was run "
                                 f"at {a.at}. {again}, then hansa lane try {a.name} --at {a.at}")
            with desk.lock():  # a server that comes back now starts it under this lock, or finds it gone
                last = read_try(desk, a.name)
                if last.get("id") == tid and last.get("phase") == "queued":  # never started: it would hold the lane
                    try_file(desk, a.name).unlink(missing_ok=True)
                    desk.event("lane_try_withdrawn", lane=a.name)
                    raise SystemExit(f"hansa: hansa serve {why} before the try started, so it was withdrawn and "
                                     f"lane {a.name} is free again; nothing was run. {again}, then "
                                     f"hansa lane try {a.name}")
            raise SystemExit(f"hansa: hansa serve {why} before the try ended. Once "
                             + ("it runs" if st["state"] == "gone" else "its rounds come") + " again, "
                             f"hansa lane try {a.name} waits for this try's result")
        time.sleep(a.every)


def _lane_try_log(desk: Desk, a) -> None:
    """hansa lane try L --log [--merge-check]: the whole output of the lane's last try of that kind."""
    if a.at:
        raise SystemExit("hansa: --log prints the last try's output and runs nothing; --at goes with a try")
    if a.name not in desk.settings()["lanes"] and not (desk.lanes_dir / a.name).is_dir():
        raise SystemExit(f"hansa: there is no lane {a.name}; lanes: {', '.join(sorted(desk.settings()['lanes'])) or 'none'}")
    f = try_log(desk, a.name, a.merge_check)
    if not f.is_file():
        raise SystemExit(f"hansa: lane {a.name} has no kept output of a{' merge-check' if a.merge_check else ''} try "
                         f"yet; hansa lane try {a.name}{' --merge-check' if a.merge_check else ''} runs one")
    sys.stdout.write(desk.redact(f.read_text(encoding="utf-8", errors="replace")))


def cmd_concurrency(a):
    desk = Desk.find(a.desk)
    if a.n is None:
        print(f"concurrency: {desk.settings()['concurrency']} task(s) at once")
        return
    with desk.lock():
        new = desk.settings()
        old = new["concurrency"]
        new["concurrency"] = a.n
        _save(desk, new)
    print(f"concurrency: {old} -> {a.n}")


def cmd_notes(a):
    desk = Desk.find(a.desk)
    if a.words[:1] == ["archive"] and len(a.words) > 1:
        if len(a.words) != 3:
            raise SystemExit('hansa: hansa notes archive FILE HEADING [--to OTHER]: one file and one heading (quote a '
                             'heading of several words: "Round 3"); nothing was changed')
        print(notes.archive(desk, a.words[1], a.words[2], a.to or ""))
        return
    if a.to:
        raise SystemExit("hansa: --to goes with hansa notes archive FILE HEADING")
    if len(a.words) > 1:
        raise SystemExit("hansa: hansa notes [PATH] lists one file or folder under notes/")
    print(notes.list_text(desk, a.words[0] if a.words else ""))


def cmd_help(a):
    print(CARD.read_text(encoding="utf-8"))


def _split_claude_args(argv: list):
    """Everything after `coordinator` belongs to claude, even when it looks like an option."""
    for i, x in enumerate(argv):
        if x == "coordinator":
            return argv[:i + 1], argv[i + 1:]
        if not x.startswith("-") and (i == 0 or argv[i - 1] != "--desk"):
            break
    return argv, []


class _Version(argparse.Action):
    """--version, worked out only when asked (it may ask git which commit this is)."""
    def __call__(self, parser, namespace, values, option_string=None):
        print(", ".join(filter(None, [f"hansa {build()}", installed_text()])))
        parser.exit()


def _as_desk_account(a, argv: list) -> None:
    """As root on a Linux desk that belongs to another account, run this command again as that account
    (hansa/account.py). Returns when hansa already runs as whoever it should."""
    try:
        desk = Desk.find(a.desk)
    except SystemExit:  # no desk: the command says so itself
        return
    theirs = account.desk_account(desk.root) if desk.root.is_dir() else ""
    if not theirs:
        return
    if getattr(a, "file", None):  # root reads it (the account may not be able to) and hands the text over
        text = _text(a)
        raise SystemExit(account.run(theirs, argv, desk.root, input=text, text=True).returncode)
    account.switch(theirs, argv, desk.root)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="hansa", description="A file-based desk for a team of Claude Code agents. "
                                 "`hansa help` prints the agent card.")
    ap.add_argument("--version", action=_Version, nargs=0, help="show which hansa this is and exit")
    ap.add_argument("--desk", help="desk directory (default: the desk containing the current directory, or HANSA_DESK)")
    sub = ap.add_subparsers(dest="cmd", metavar="command")

    def add(name, fn, help_):
        p = sub.add_parser(name, help=help_, description=help_)
        p.set_defaults(fn=fn)
        return p

    p = add("init", cmd_init, "create a desk")
    p.add_argument("dir")
    p.add_argument("--repo", help="git checkout the first lane works on")
    p.add_argument("--lane", default="a")
    p.add_argument("--base", help="base branch (default: the checkout's current branch)")
    p.add_argument("--user", help="as root on a Linux server: the account the desk belongs to and hansa runs as")
    p = add("add", cmd_add, "queue a task")
    p.add_argument("text", nargs="*")
    p.add_argument("--file", help="a file with the text; - reads it from standard input. It is read when the task is "
                                  "queued: changing the file afterwards changes nothing (hansa show ID gives the text "
                                  "hansa read)")
    p.add_argument("--lane")
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--model")
    p.add_argument("--title")
    p.add_argument("--max-turns", type=int)
    p.add_argument("--browser", action="store_true", help="give this one task a browser (read pages, take screenshots)")
    p = add("design", cmd_design, "open a session the principal joins to shape one feature on the real code")
    p.add_argument("text", nargs="*")
    p.add_argument("--file", help="a file with the text; - reads it from standard input")
    p.add_argument("--lane")
    p.add_argument("--title")
    p = add("status", cmd_status, "what is running, queued, waiting for review, and why")
    p.add_argument("--json", action="store_true")
    p = add("show", cmd_show, "one task: its text, report, changes, refused commands, usage, next step")
    p.add_argument("id")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--full", action="store_true", help="the whole text and the whole report")
    g.add_argument("--report", action="store_true", help="the report alone, whole (for a verification, its verdict "
                                                         "and its report)")
    g.add_argument("--check", action="store_true", help="the whole output of the task's last check and of its last "
                                                        "merge check, keys removed")
    p = add("diff", cmd_diff, "a task's changes against its base")
    p.add_argument("id")
    p.add_argument("paths", nargs="*", metavar="path", help="only these files or folders")
    p.add_argument("--stat", action="store_true")
    p.add_argument("--new", action="store_true", help="for a follow-up (hansa retry): only what it added on top of the "
                                                      "earlier attempt's last commit")
    p = add("git", cmd_git, "run a git command that reads (log, show, diff, grep ...) in a lane's repository, or "
            "with --worktree in its worktree, as the desk's account")
    p.description += (". It refuses git commands and options that write, run a program named on the command line, or "
                      "read outside the repository. It runs as the desk's account, so a program the repository's own "
                      "settings name (a filter driver, say) runs as that account, not as root")
    p.add_argument("--worktree", action="store_true", help="the lane's worktree instead of its repository")
    p.add_argument("lane")
    p.add_argument("args", nargs=argparse.REMAINDER, metavar="git-args")
    p = add("merge", cmd_merge, "merge a finished task into the lane's base branch")
    p.add_argument("id")
    p.add_argument("--despite-check", action="store_true", help="merge even though the lane's check failed (recorded)")
    p.add_argument("--recheck", action="store_true", help="run the lane's check again on the task merged onto the base "
                   "as it is now, and merge exactly that if it passes (after a check that failed or could not run for "
                   "the machine's sake, once that is fixed; recorded)")
    p = add("retry", cmd_retry, "queue a follow-up to a task")
    p.add_argument("id")
    p.add_argument("note", nargs="*")
    p.description = ("queue a follow-up to a task. Its worker is given the task, the note, the previous attempt's "
                     "report and, when that attempt was verified, the latest verification's report whole, with its "
                     "verdict, whatever the verdict was.")
    p.add_argument("--file", help="a file with the note, instead of words; - reads it from standard input")
    p.add_argument("--fresh", action="store_true", help="start from the base branch, not the previous attempt")
    p.add_argument("--update", action="store_true", help="continue from the previous attempt with the base as it is "
                   "now merged into its branch; a conflict is left in the files for the worker to settle")
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--browser", action="store_true", help="give the follow-up a browser")
    p = add("verify", cmd_verify, "have a worker run a finished task's change to see whether it holds (never merged)")
    p.description = ("have a worker run a finished task's change to see whether it holds (never merged). By default "
                     "it runs on the task's own lane, with that lane's provider and model, and waits there behind "
                     "the lane's other tasks. --lane L runs it on lane L instead: a check by another model is a "
                     "stronger one, and an idle lane starts it sooner.")
    p.add_argument("id")
    p.add_argument("note", nargs="*", help="what to look at in particular")
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--browser", action="store_true", help="give the verification a browser")
    p.add_argument("--lane", help="run the verification on this lane, with its provider, model, effort, sandbox and "
                   "lists; by default it runs on the task's own lane. A lane on the same repository as the task's; "
                   "use it to have another model check the task, or when the task's lane is busy")
    p = add("cancel", cmd_cancel, "stop a queued or running task, or close a finished one without merging")
    p.add_argument("id")
    p = add("pause", cmd_pause, "stop new tasks from starting")
    p.add_argument("reason", nargs="*")
    add("resume", cmd_resume, "let new tasks start again")
    p = add("release", cmd_release, "ask for a release of a lane's base branch (its release command, run by hansa "
            "after the principal's yes on the page), or list the releases")
    p.description = ("hansa release L [TARGET] [note]: ask for a release of lane L's base branch as it is now, by "
                     "one of its release targets: its release command (hansa lane set L release \"...\"), or a named "
                     "target (hansa lane target L NAME \"...\"); TARGET is needed when the lane has more than one. "
                     "hansa records the commit, the target and its command, the note, the commits that go out (on no "
                     "remote-tracking branch and not in that target's last release) and the files they change; a word "
                     "the principal keeps from going out, in the command line or in a line, a file name or a message "
                     "of any of those commits, refuses it. A target that must follow another (--after) is asked only "
                     "for a commit the other one already sent out. It waits for the principal's yes on the web page "
                     "(unless the principal lists that target as released without asking and the desk is not "
                     "paused; a tick stops counting when the lane's repository or base branch, or the target's "
                     "command, --after or --fixed, changes, not when only --minutes does); then hansa serve runs the "
                     "command, as the desk's account, outside the sandbox, with hansa's environment and the target's "
                     "minutes (else the lane's) as its limit, and only while the base branch and the command are what "
                     "the principal saw. One release runs at a time per repository. A release runs in the lane's "
                     "repository and holds it: merges there wait while it runs, a long install too. A target set "
                     "--fixed (hansa lane target L NAME --fixed) runs instead from a checkout of the release's commit "
                     "made for it, removed when it ends, and holds nothing: merges there go on; its command names the "
                     "commit as $HANSA_RELEASE_COMMIT, never the base branch. Each says which way it runs. The commits "
                     "going out are read and checked for forbidden words without holding the desk (other commands, the "
                     "page and hansa serve go on meanwhile), each commit's own change once (a merge's: only what it "
                     "adds beyond its parents); hansa serve checks again at the start only when the commit, the "
                     "starting point, the words or the command changed since. hansa release alone lists the open and "
                     "recent releases. Only the coordinator asks. hansa release --stop ID [reason] calls one off: one "
                     "waiting for the principal or approved and waiting its turn is closed as stopped; a running one "
                     f"gets TERM to its command's whole process group, then KILL {release.STOP_GRACE} seconds later to "
                     "what still runs, and is recorded as stopped, with who stopped it and why, its output so far kept "
                     "(a fixed target's checkout is removed as at any other end); one whose command had already ended "
                     "is recorded as it ended, with the stop noted as late. What a release's command started that "
                     "still runs, also outside its process group, is stopped at any end. The principal's alert command is "
                     "told (release_stopped). A stopped release went out for no --after. A stopped install may leave "
                     "the server half-switched: a release command for which that matters should handle TERM (finish "
                     "or undo the step it is in)")
    p.epilog = ("hansa release yes ID CODE, hansa release no ID CODE [reason]: the principal's answer to release ID, "
                "said in the conversation with the one-time code that only the principal's alert carries (6 "
                "characters, no 0/O or 1/I/l), given as one word (dashes allowed, no spaces: a yes with a word too many is "
                "refused, nothing counted). hansa checks it against release ID only; a right code answers it once, "
                "recorded as the principal's answer given through the coordinator (channel code); a wrong one is "
                f"refused and counted, and after {release.CODE_TRIES} the code is void and only the page answers. A "
                f"code lapses when the release is answered (on the page too), stopped or no longer what was asked, after "
                f"{release.CODE_LIFE // 3600} hours, and when a new waiting alert for it gives a new one. With no alert "
                "command there is no code: the principal answers on the page. yes and no are not lane names, so this "
                "form is always an answer. Never guess a code, nor ask the principal to send it anywhere else")
    p.add_argument("--stop", metavar="ID", help="call release ID off (the rest of the line is the reason): TERM to a "
                   f"running one's command, KILL {release.STOP_GRACE} s later")
    p.add_argument("lane", nargs="?")
    p.add_argument("note", nargs="*")
    p = add("notify", cmd_notify, "list the sessions hansa posts to; on / off: start or stop posting to the Claude "
            "Code session this is run from")
    p.add_argument("action", nargs="?", choices=["on", "off"])
    p.add_argument("--clear-daily", metavar="HH-HH", help="with on: clear the session once a day in this window of "
                   "hours, local time (such as 04-09), when it is idle and nobody is typing; it needs tmux")
    p = add("wait", cmd_wait, "return when a task finishes or fails, or the principal answers; run it in the "
            "background when hansa does not notify the session (hansa notify)")
    p.add_argument("ids", nargs="*", metavar="id", help="tasks to watch (default: every queued or running task)")
    p.add_argument("--timeout", type=float, default=60, help="minutes (default 60)")
    p.add_argument("--every", type=float, default=5.0, help=argparse.SUPPRESS)
    p = add("ask", cmd_ask, "put a request on the principal's page, or close one of yours that is done (--close)")
    p.description = ("put a request on the principal's page: hansa ask \"<request>\". The principal answers it there, "
                     "which closes it. When it is done some other way (the principal did it and said so in the "
                     "conversation, say), the coordinator closes it: hansa ask --close N \"why\"; the reason is "
                     "required, and the page shows it as closed by the coordinator, with the reason. Workers, "
                     "verifications and design sessions cannot close requests")
    p.add_argument("text", nargs="*", help="the request; with --close, why it is closed")
    p.add_argument("--close", type=int, metavar="N", help="close open request #N of this desk, saying why")
    p = add("explain", cmd_explain, "what a setting or state means: lane NAME | provider NAME | task ID | states | server "
            "| notify")
    p.add_argument("what", nargs="?", choices=["lane", "provider", "task", "states", "server", "notify"])
    p.add_argument("name", nargs="?")
    p.add_argument("--all", action="store_true", help="with lane NAME: the whole built-in list of what its workers "
                   "may not read, name by name (without it, how many names it has)")
    p = add("lane", cmd_lane, "list the lanes, or add, change or remove one")
    lane = p.add_subparsers(dest="action", metavar="add | set | target | brief | remove | try")
    q = lane.add_parser("add", help="a new lane on a git checkout", description=(
        "A new lane on a git checkout. A lane runs one task at a time, in its own full worktree (the repository's "
        "disk space again): to run tasks side by side add lanes on the same repository and base, up to hansa "
        "concurrency in all, and to split work among providers give each its own lane with its provider order. "
        "The lane's setup and check run with /bin/sh -c: POSIX shell only, no bash syntax; so write its preview "
        "(started by a design session's Bash tool, or by hansa for a task with a browser) the same way. " +
        "The network list is the hosts the lane's workers, setup and check may reach, such as registry.npmjs.org or "
        "*.pypi.org. localhost on it differs by system. On Linux each sandbox has a loopback of its own: workers' "
        "own local servers need nothing on the list, and localhost adds nothing for them; what it does add is that "
        "a program told to use the sandbox's proxy for localhost reaches this machine's loopback through it, so "
        "leave it off. On macOS local servers "
        "need localhost, and it opens every service on the machine's loopback, local proxies among them, which "
        "reach the internet: list it only on a Mac whose loopback holds nothing a worker must not reach."))
    q.add_argument("name")
    q.add_argument("--repo", help="the git checkout the lane works on")
    q.add_argument("--like", metavar="LANE", help="start as a copy of this lane's settings (repository, base, "
                   "providers, setup, check, merge_check, preview, network, folders it may write, env, allow, limits "
                   "with merge_timeout_min and preview_wait_s, commit_message, merge_message, identity, release "
                   "targets, release_files, note) "
                   "and standing instructions; "
                   "each other option given replaces that one field. It is a copy: later changes to LANE do not "
                   "reach the new lane. The worktree is the new lane's own. Not with --repo")
    q.add_argument("--base", help="base branch (default: the checkout's current branch)")
    q.add_argument("--providers", help="provider order, comma-separated (default: claude)")
    q.add_argument("--check", help="the command hansa runs after each task, and on a merge when the base has moved "
                   "unless the lane has a merge_check (/bin/sh -c)")
    q.add_argument("--setup", help="the command that installs dependencies before each task (/bin/sh -c)")
    q.add_argument("--merge-check", metavar="COMMAND", help="the command the merge check runs before each merge in "
                   "place of the check, on the task merged with the base (/bin/sh -c; default: the check)")
    q.add_argument("--merge-timeout", metavar="MIN", help="the merge check's time limit in minutes (default: the "
                   "lane's timeout_min)")
    q.add_argument("--network", nargs="+", metavar="HOST", help="the hosts its workers, setup and check may reach "
                   "(default: none)")
    q.add_argument("--identity", metavar='"NAME <EMAIL>"', help="the author and committer of every commit hansa "
                   "makes for the lane (default: the repository's own user.name and user.email, else hansa's)")
    q.add_argument("--allow", nargs="+", metavar="RULE", help="rules the lane's workers, verifiers and design "
                   "sessions are given without asking: commands such as 'Bash(pnpm test:*)' (workers run any command "
                   "in their sandbox and need none; design sessions do), and web tools: "
                   "'WebFetch(domain:docs.python.org)' or 'WebFetch(domain:*.python.org)' for each public site (a "
                   "bare WebFetch, an address, a local or private name or a wildcard over a shared suffix is "
                   "refused), WebSearch (it sends search queries out of the machine); each rule its own value")
    q.add_argument("--release-files", nargs="+", metavar="PATH", help="the files the lane's releases build and run "
                   "outside the sandbox, as paths or globs relative to the repository, separated by spaces (\"pom.xml "
                   "**/pom.xml build.sh tools/release.sh\"): hansa merge refuses a change touching one of them until "
                   "a verification of it holds (hansa verify ID), and no flag overrides that")
    q = lane.add_parser("set", help="change one field of a lane", description=(
        "allow is the rules the lane's workers, verifiers and design sessions are given without asking: commands "
        "such as Bash(pnpm test:*) (workers run any command in their sandbox and need none; design sessions do), "
        "and Claude Code's web tools, which reach the network from its own process, outside the sandbox's network "
        "list: WebFetch only as WebFetch(domain:HOST) for each site, HOST a public site's name as the network list "
        "writes it (*. in front allowed, and it opens every name under it; a bare WebFetch, one for every site, an IP "
        "address, localhost, a name for a local or private network such as .internal or .local, or a wildcard over "
        "a shared suffix such as *.co.uk or *.github.io is refused, as are names with no dot and names such as nip.io or lvh.me that come back as this machine; any listed name can still be pointed at any address, so list only sites you know), and WebSearch, which sends search queries out "
        "of the machine; MCP tools are refused. Each rule is its own value: one Claude Code would read as several (it splits on commas and spaces outside parentheses, and every ')' closes them), such as Bash(x),WebFetch or Bash(a(b),WebFetch,c), is refused, and a rule given twice is kept once. A change of allow is recorded. providers, allow, network and write take the whole new list (none: empty); env takes NAME=VALUE pairs and replaces the "
        "lane's env; base, check, merge_check, preview, setup, release, note, commit_message, merge_message and "
        "identity take "
        "one value (none: empty); max_turns, timeout_min, merge_timeout_min and preview_wait_s take one whole number. "
        "preview is the command that starts the app (npm run dev, say). A design session starts it itself. For a task "
        "or verification with a browser (hansa add --browser), hansa starts it on macOS before the worker: in the "
        "worktree after the lane's setup, in srt with the lane's sandbox plus local binding for that one process "
        "(the worker's own sandbox stays as the lane sets it); it waits up to preview_wait_s seconds (default 120; "
        "none: back to that) for the first http://localhost:PORT or http://127.0.0.1:PORT address the preview prints, "
        "or a port it names that answers, and tells the worker the address, or that it did not start, with its last "
        "lines; it stops the preview, with its whole process group and each process seen below it (looked for every "
        "half second), when the worker ends, however it ends; its output is kept in the task's folder, keys removed, "
        "its first and last 2 MB at most (hansa show says where). On macOS such a preview can reach every "
        "service on the machine's loopback while it runs, as a lane with localhost on its network list can: the price "
        "of looking at the app in a browser on a Mac. On Linux hansa does not start it: srt gives the command a network "
        "of its own, so the browser, outside the sandbox, could not reach the app; the worker is told so. check runs "
        "after each task; before a merge, hansa runs a merge check on the task merged with the base: by default the "
        "check again, when the base has moved since the task was made. merge_check gives the merge check a command "
        "of its own (a heavier one: the whole build, packaging, the slow suites), run in the same sandbox, after the "
        "lane's setup, before every merge of a task that changed something, and never after a task; a lane with only "
        "a merge_check has no check after each task. merge_timeout_min is the merge check's own time limit in minutes "
        "(default: timeout_min). commit_message is the form of the message of the commit hansa makes of a task's "
        "work (unset: t0001: <title>), and of the merge of the base into a task's branch (retry --update); "
        "merge_message is the form of the message of a task's merge into the base (unset: Merge t0001: <title>). "
        "Each is one line with {task}, {title} and {lane}, and needs {task} or {title}. A lane whose release goes "
        "to a public repository usually leaves {task} out of both. none takes merge_check, merge_timeout_min, "
        "preview_wait_s, commit_message, merge_message or identity out again. identity is Name <email>, the author "
        "and committer of every commit hansa makes for the lane: a task's commit, its merge into the base (the merge "
        "check's merge too), the merge of the base into a task's branch, and a design session's commits. Unset, "
        "hansa takes the repository's own user.name and user.email from its local config (git config --local), "
        "never the account's global one; with neither it writes hansa <hansa@localhost>. A lane whose release goes "
        "to a public repository should have one, on the lane or in the repository's config. release is the command line that sends the base branch out (git push "
        "origin main, say): hansa runs it after the principal's yes on the page (hansa release L). It is the lane's one "
        "unnamed release target; a lane that sends its base branch out in more than one way names its targets "
        "instead (hansa lane target L NAME ...), and then has no release. A release command (or a target's, with hansa "
        "lane target) that holds one of the principal's forbidden words is kept, and hansa says the word and where at "
        "once; a release with it is refused. release_files is the list of files the lane's releases build and run "
        "outside the sandbox (a Maven pom's plugins, a build script, the release script itself), as paths or globs "
        "relative to the repository, separated by spaces, with ** for any folders (hansa lane set L release_files "
        "\"pom.xml **/pom.xml build.sh tools/release.sh\"; none: empty): hansa merge refuses a change touching one of "
        "them until a verification of that change holds (hansa verify ID), and neither --despite-check nor anything "
        "else lets it through. " +
        "network is the list of hosts the lane's workers, setup and check may reach, such as registry.npmjs.org or "
        "*.pypi.org. localhost on it differs by system. On Linux each sandbox has a loopback of its own: workers' "
        "own local servers need nothing on the list, and localhost adds nothing for them; what it does add is that "
        "a program told to use the sandbox's proxy for localhost reaches this machine's loopback through it, so "
        "leave it off. On macOS local servers "
        "need localhost, and it opens every service on the machine's loopback, local proxies among them, which "
        "reach the internet: list it only on a Mac whose loopback holds nothing a worker must not reach."))
    q.add_argument("name")
    q.add_argument("field", choices=LANE_FIELDS)
    q.add_argument("values", nargs="*", metavar="value")
    q = lane.add_parser("target", help="list a lane's release targets, or set or remove a named one", description=(
        "A lane's release targets are the ways its base branch goes out: a push to the public repository, an install "
        "on a test server, an install on the production server. hansa lane target L lists them. hansa lane target L "
        "NAME \"<command>\" sets one (a new one, or a new command for one); --minutes N gives it its own time limit "
        "(1-1440; none: the lane's timeout_min); --after OTHER says it goes out only with a commit target OTHER of "
        "the same lane already sent out (none: no order). Options alone change only those fields. hansa lane target "
        "L NAME none takes it out. The lane's release command (hansa lane set L release) is its one unnamed target: "
        "a lane has either that or named targets, so take it out (hansa lane set L release none) when you name its "
        "first target. hansa release L TARGET [note] asks for one; the principal answers each request, and ticks "
        "each target on its own for going out without asking. A tick stops counting when the lane's repository or "
        "base branch, or the target's command or --after, changes (a target taken out and set again with the same "
        "command, --after and --fixed keeps it; --minutes alone does not untick). A release runs in the lane's "
        "repository and holds it while it runs: merges there wait for it, a long install too. --fixed runs the "
        "target instead from a detached checkout of the release's commit that hansa makes for it under the desk "
        "(removed when it ends, however it ends, and swept after a restart), holding nothing: merges and checks in "
        "the repository go on meanwhile; releases of one repository still run one at a time. Its environment has "
        "HANSA_RELEASE_COMMIT (the commit the principal saw), HANSA_RELEASE_TARGET and HANSA_RELEASE_REPO (the lane's "
        "repository). Such a command must name the commit, as $HANSA_RELEASE_COMMIT or HEAD (git push origin "
        "\"$HANSA_RELEASE_COMMIT:refs/heads/main\", say), never the base branch (git push origin main would send "
        "wherever main has moved by then). hansa refuses a fixed target whose command line itself names the base "
        "branch as what to send (main, refs/heads/main, main~0, main^{commit}, main@{0}, or the source of main:prod; "
        "HEAD:main names it only as where to send, and is allowed); a variable the command expands or a script it "
        "runs is not looked into, so a fixed target's command should send $HANSA_RELEASE_COMMIT. "
        "--fixed none runs it in the repository again"))
    q.add_argument("name")
    q.add_argument("target", nargs="?", metavar="NAME")
    q.add_argument("command", nargs="*", metavar="COMMAND", help="the command line (/bin/sh -c), quoted; none takes "
                   "the target out")
    q.add_argument("--minutes", metavar="N", help="its own time limit, 1-1440 (none: the lane's timeout_min)")
    q.add_argument("--after", metavar="TARGET", help="another target of the lane that must have sent out the same "
                   "commit first (none: no order)")
    q.add_argument("--fixed", nargs="?", const="yes", metavar="none", help="run from a checkout of the release's "
                   "commit made for it, holding nothing; its command names $HANSA_RELEASE_COMMIT, not the base branch "
                   "(none: run in the repository again)")
    q = lane.add_parser("brief", help="print a lane's standing instructions, or replace them with TEXT or --file")
    q.add_argument("name")
    q.add_argument("text", nargs="*")
    q.add_argument("--file", help="a file with the new instructions; - reads them from standard input")
    q = lane.add_parser("remove", help="remove a lane with no task queued, running or waiting for review, and its "
                        "worktree", description=(
        "Remove a lane with no task queued, running or waiting for review, and its worktrees (git worktree remove, "
        "as the desk's account). When a worktree holds changes not committed or commits no branch keeps, nothing is "
        "removed unless --keep or --discard says what to do with them. For a lane that is gone but whose worktree "
        "is still there, it removes the worktree the same way. The lane's brief stays."))
    q.add_argument("name")
    q.add_argument("--keep", action="store_true", help="put what the worktree holds that no branch keeps on a new "
                   "branch hansa-kept/NAME-DATE of the lane's repository, then remove it")
    q.add_argument("--discard", action="store_true", help="drop what the worktree holds that no branch keeps")
    q = lane.add_parser("try", help="run a lane's setup and check once in its worktree and sandbox, as for a task "
                        "but with no worker, and print how they ended")
    q.add_argument("name")
    q.add_argument("--merge-check", action="store_true", help="run the setup and the lane's merge check command (its "
                   "merge_check, else its check) instead, under the merge check's time limit")
    q.add_argument("--at", metavar="COMMIT", help="answer for this commit only (full or abbreviated, in the lane's "
                   "repository): refused, with nothing run, unless it is the base's tip; a try queued or running at "
                   "another commit is waited for, then this one runs if the tip is still COMMIT; exit 0 only when the "
                   "try at COMMIT passed")
    q.add_argument("--log", action="store_true", help="print the whole output of the lane's last try (with "
                   "--merge-check: of its last merge-check try), keys removed, its first and last 2 MB at most per step; "
                   "runs nothing")
    q.add_argument("--every", type=float, default=0.5, help=argparse.SUPPRESS)
    p = add("notes", cmd_notes, "the coordinator's notes: list the files under notes/ with their headings, or move "
            "a finished section out of one: hansa notes archive FILE HEADING [--to OTHER]")
    p.description = (
        "hansa notes [PATH]: the files under the desk's notes/ (or under notes/PATH), each with its size, its number "
        "of lines and its headings with their line numbers; under notes/archive/ only size and lines, unless PATH "
        "names it. hansa notes archive FILE HEADING [--to OTHER]: move the section of notes/FILE that starts at the "
        "heading matching HEADING (its text, whole, or the start of it when that matches only one heading; HEADING "
        "starting with #s, such as \"## Round 1\", matches only headings of that level) and runs up to the next heading of the same or a higher level, subsections with it, onto the end "
        "of notes/OTHER (default: notes/archive/FILE, made when missing), under a line saying when it was moved. "
        "Both files must be inside notes/. When HEADING matches no heading or several, nothing is changed and the "
        "headings are listed. The other file is written first and the notes file replaced whole after it, so a "
        "crash between the two leaves the section in both files, never in neither")
    p.add_argument("words", nargs="*", metavar="PATH | archive FILE HEADING")
    p.add_argument("--to", metavar="OTHER", help="with archive: the file under notes/ the section goes to "
                   "(default: archive/FILE)")
    p = add("concurrency", cmd_concurrency, "show, or set, how many tasks run at once")
    p.add_argument("n", nargs="?", type=int)
    p = add("cost", cmd_cost, "tokens and what they are worth")
    p.description = ("Tokens and what they are worth: the workers' runs, and the coordinator's own session with its "
                     "helpers. The coordinator is counted only when it runs with the workers' Claude login (the same "
                     "config folder), because only then are its session records where hansa reads them. It has no "
                     "lane or task, so in --by lane and --by task its usage is a row of its own, (coordinator): in "
                     "every view the rows add up to the total.")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--since", help="HH:MM today, or YYYY-MM-DD HH:MM: count from then instead of --days")
    p.add_argument("--by", choices=["day", "seat", "lane", "task", "model", "provider"], help="the totals by "
                   "(default: day); not with --turns")
    p.add_argument("--json", action="store_true")
    p.add_argument("--rows", action="store_true", help="with --json: include every row")
    p.add_argument("--turns", action="store_true", help="the coordinator's own usage turn by turn: what started "
                   "each turn, its steps, the conversation's length at its first step, its worth and the tools it "
                   "called; then totals by what started them. No message text")
    p = add("check", cmd_check, "send one tiny request through a provider (or all)")
    p.add_argument("provider", nargs="?")
    p = add("serve", cmd_serve, "run the dispatcher and the web page (keep it running)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765, help="the web page's port (default 8765); another desk's server "
                   "on this machine needs another one")
    p.add_argument("--every", type=float, default=3.0, help=argparse.SUPPRESS)
    p = add("web", cmd_web, "print the web page address for the principal")
    p.description += (", then wait on its input, which holds an ssh tunnel to the page open, until that input ends "
                      "or Ctrl+C. A script that wants only the address gives it no input (hansa web </dev/null): it "
                      "prints the address and returns")
    p.add_argument("--port", type=int, help="default: the port this desk's hansa serve serves the page on")
    add("coordinator", cmd_coordinator, "start the coordinator session in this desk; anything after the word "
        "coordinator is passed to claude as is, except --clear-daily HH-HH: clear the session once a day in that "
        "window of hours, local time, when it is idle and nobody is typing (it needs tmux)")
    add("help", cmd_help, "print the agent card")
    p = sub.add_parser("coordinator-desk")  # hidden: the desk's part of `hansa coordinator`, as the desk's owner
    p.set_defaults(fn=cmd_coordinator_desk)
    p.add_argument("--by", type=int, required=True)
    p = sub.add_parser("notify-follow")  # hidden: a follower (hansa/notify.py), started detached
    p.set_defaults(fn=cmd_notify_follow)
    for opt in ("--key", "--socket", "--nonce"):
        p.add_argument(opt, required=True)
    p.add_argument("--pid", type=int, required=True)
    p.add_argument("--start", default="")
    p.add_argument("--name", default="")
    p.add_argument("--clear-daily", default="")
    p = sub.add_parser("notify-feed")  # hidden: a follower's request to the desk, as the desk's owner
    p.set_defaults(fn=cmd_notify_feed)

    argv, rest = _split_claude_args(sys.argv[1:] if argv is None else list(argv))
    a, extra = ap.parse_known_args(argv)
    # words after an option belong to the note: hansa retry ID --fresh "note" as well as hansa retry ID "note" --fresh
    if extra and getattr(a, "cmd", "") in ("retry", "verify", "release") and not any(x.startswith("-") for x in extra):
        a.note += extra
    elif extra and getattr(a, "cmd", "") == "diff" and not any(x.startswith("-") for x in extra):
        a.paths += extra  # hansa diff ID --stat PATH as well as hansa diff ID PATH --stat
    elif extra:
        ap.error(f"unrecognized arguments: {' '.join(extra)}")
    a.rest = rest
    if not getattr(a, "fn", None):
        ap.print_help()
        return 0
    if a.cmd == "add" and not a.text and not a.file:
        ap.error("add needs the task text or --file")
    if a.cmd == "ask" and not a.text:
        ap.error("ask needs the request" if a.close is None else "ask --close needs the reason: hansa ask --close N \"why\"")
    # a follower runs as the coordinator's account, and reads the desk through the account's hansa itself
    if a.cmd not in ("coordinator", "init", "notify", "notify-follow"):
        _as_desk_account(a, argv)
    a.fn(a)
    return 0
