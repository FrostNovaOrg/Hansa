"""The `hansa` command. Every subcommand is a thin wrapper over desk, run, views and cost."""
from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
import re
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import account, build, cost, installed_text, notes, notify, release, views
from .desk import (EFFORTS, Desk, default_lane, default_settings, deny_split, home_secrets, json_text,
                   kept_from_workers, merge_message, MESSAGE_FIELDS, message_problem, now, read_json, read_split, resolved, stored, write_json,
                   PROJECT_INTRO)
from .run import (VERIFY_TASK, Dispatcher, base_moved, check_provider, close_verification, conflict_advice,
                  conflict_places, conflicted_files, finish_merge_check, git, keys_in_change, lane_holder,
                  MARKERS_ON_PURPOSE, beat, markers_in_change, markers_text, merge_check_ahead, merge_command, merge_minutes, merge_needs_check, merge_target,
                  not_looking, own_merge_check, proc_start, provider_env, pulse,
                  quiet_text, read_try, record_serve_code, server_state, stop, try_file, try_srt_at_start, unverified_note, waiting_reason, write_try,
                  is_local, mac_loopback_listeners)

CARD = Path(__file__).with_name("card.md")
HELPERS = Path(__file__).with_name("agents")  # the coordinator's helpers: scout, reviewer, operator
NO_SIGNATURE = Path(__file__).resolve().with_name("no-signature")  # hansa git's gpg: checks no signature
RETIRED_HELPERS = ("verifier.md", "verifier-high.md")  # read-only checks, replaced by hansa verify and the reviewer
# Claude Code's own general-purpose helpers run on the coordinator's model and effort with no
# limits of their own; the desk's helpers do the same jobs at a set effort instead.
BUILTIN_HELPERS = ("general-purpose", "Explore", "Plan", "claude", "statusline-setup", "claude-code-guide")
ON_MAC = sys.platform == "darwin"


def init_desk(root: Path, repo: str = "", lane: str = "a", base: str = "") -> Desk:
    desk = Desk(root)
    if desk.settings_path.exists():
        raise SystemExit(f"hansa: {desk.root} already has a desk")
    for d in (desk.root, desk.tasks_dir, desk.lanes_dir, desk.state_dir, desk.notes_dir):
        d.mkdir(parents=True, exist_ok=True)
    desk.secrets_dir.mkdir(mode=0o700, exist_ok=True)
    s = default_settings()
    if repo:
        if not resolved(repo):
            raise SystemExit(f"hansa: cannot follow the links of {repo} to see where it leads; no desk was made")
        repo = resolved(repo)
        theirs = account.foreign_repo(repo)
        if theirs:
            raise SystemExit(f"hansa: {theirs}; no desk was made")
        base = base or git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo)
        s["lanes"][lane] = default_lane(repo, base)
        (desk.lanes_dir / lane).mkdir(exist_ok=True)
        (desk.lanes_dir / lane / "brief.md").write_text("", encoding="utf-8")
    write_json(desk.settings_path, s)
    (desk.root / "CLAUDE.md").write_text(CARD.read_text(encoding="utf-8"), encoding="utf-8")
    if not desk.project_path.exists() and not desk.project_path.is_symlink():  # made once; never written again
        desk.project_path.write_text(PROJECT_INTRO, encoding="utf-8")
    (desk.root / ".claude").mkdir(exist_ok=True)
    write_json(desk.root / ".claude" / "settings.json", {"permissions": permissions(desk)})
    desk.event("desk_created", repo=repo, lane=lane if repo else "")
    return desk


def permissions(desk: Desk) -> dict:
    """The coordinator's Claude Code permission rules in the desk."""
    r = str(desk.root).lstrip("/")
    # lane briefs change through `hansa lane brief`: on a server the coordinator's own tools run as root
    guarded = [f"//{r}/settings.json", f"//{r}/secrets/**", f"//{r}/tasks/**", f"//{r}/.hansa/**", f"//{r}/events.jsonl",
               f"//{r}/lanes/**"]
    # an Edit rule covers every file-editing tool (Write too); //path is absolute. Claude Code allows a write only
    # when every path it resolves to matches, so a link at notes/ leads nowhere without asking; the denials still win
    return {"allow": ["Bash(hansa:*)", f"Edit(//{r}/notes/**)"],
            "deny": [f"Read(//{r}/secrets/**)", f"Read(//{r}/.hansa/**)"] + [f"Edit({g})" for g in guarded]}


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
    print(f"{tid} queued" + (f" for lane {a.lane}" if a.lane else "")
          + (f". {_wait_for(desk, tid)}; hansa show {tid} says where it is" if notify.hears_of(desk, tid)
             else f". Follow it with: hansa show {tid}") + _waits(desk, tid))


def cmd_design(a):
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
    print(views.report_text(desk, a.id) if a.report else views.show_text(desk, a.id, full=a.full))


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
            git("merge", "--no-ff", "-m", merge_message(lane, m["lane"], a.id, m["title"]), m["head"], cwd=repo, env={"HANSA_MERGING": "1"})
        except RuntimeError as e:
            files = conflicted_files(lambda *x: git(*x, cwd=repo))
            places = desk.redact(conflict_places(Path(repo), files)) if files else ""  # read before the abort
            try:
                git("merge", "--abort", cwd=repo)
            except RuntimeError:
                pass
            raise SystemExit(desk.redact(conflict_advice(a.id, base, places, str(e)[:300])))
        note = unverified_note(desk, m)  # before its verification is closed
        desk.update(a.id, state="merged", merged=time.time(), merge_commit=git("rev-parse", "HEAD", cwd=repo),
                    unverified=note or None)
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
    if (a.lane and a.lane != lane) or lane not in desk.settings()["lanes"]:
        _lane_reaches(desk, a.lane or lane, m)
        lane = a.lane or lane
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


def _lane_reaches(desk: Desk, name: str, m: dict) -> None:
    """Refuses, queueing nothing, a lane that cannot reach the task's change: one that is not on the desk, or not on
    the repository of the task's own lane, where the task's branch is."""
    lanes = desk.settings()["lanes"]
    nothing = "; nothing was queued"
    own = lanes.get(m.get("lane"))
    if own is None and name == m.get("lane"):
        raise SystemExit(f"hansa: {m['id']}'s lane {name or '(none)'} is no longer on the desk; verify it with --lane L, "
                         f"a lane on the repository where its branch {m.get('branch') or 'task/' + m['id']} is"
                         f"{nothing}")
    if name not in lanes:
        raise SystemExit(f"hansa: there is no lane {name}; lanes: {', '.join(sorted(lanes)) or 'none'}{nothing}")
    if own is None:  # the task's branch, still at the change, is what shows the lane is on its repository
        branch = m.get("branch") or f"task/{m['id']}"
        try:
            tip = git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}", cwd=lanes[name]["repo"])
        except (RuntimeError, OSError):
            tip = ""
        if tip != m["head"]:
            raise SystemExit(f"hansa: {m['id']}'s lane {m.get('lane') or '(none)'} is no longer on the desk, and lane "
                             f"{name}'s repository {lanes[name]['repo']} has no branch {branch} at {m['id']}'s change "
                             f"({m['head'][:12]}), so lane {name} cannot be shown to reach it. Verify it on a lane on "
                             f"the repository where that branch is{nothing}")
        return
    same = lambda x, y: (resolved(x) or x) == (resolved(y) or y)
    if not same(lanes[name]["repo"], own["repo"]):
        raise SystemExit(f"hansa: lane {name} cannot reach {m['id']}'s change: it is on {lanes[name]['repo']}, and "
                         f"{m['id']}'s branch is in {own['repo']}, the repository of its lane {m['lane']}. Verify it "
                         f"on a lane on that repository{nothing}")


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
    if not a.lane:
        print(desk.redact(release.list_text(desk)))
        return
    if os.environ.get("HANSA_TASK"):  # a worker, verification or design session: releases are the coordinator's
        raise SystemExit("hansa: only the coordinator asks for a release; a worker, verification or design session "
                         "cannot. Say in your report that it may be ready to go out")
    r = release.ask(desk, a.lane, " ".join(a.note), by=os.environ.get("HANSA_ROLE", "cli"),
                    session=notify.session_here())
    head = (f"release #{r['id']}: lane {a.lane}'s {r['base']} at {r['commit'][:10]}, "
            + (f"{r['count']} commit(s) since release #{r['since_release']}" if r.get("since_release") else
               f"{r['count']} commit(s) on no remote-tracking branch (no release of it went out before)")
            + f", {r['files_count']} file(s) changed; {release.checked_text(r)}; command: {r['command']}")
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
        print(desk.redact(f"{head}\nlane {a.lane} is on the principal's list of lanes released without asking, but the "
                          f"desk is paused ({paused}): while it is, only a release the principal said yes to runs, so "
                          f"this one waits until the desk is resumed. {after}" if paused else
                          f"{head}\nlane {a.lane} is on the principal's list of lanes released without asking: hansa "
                          f"serve runs the command in {r['repo']} within seconds. {after}"))
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
    print(views.explain_text(Desk.find(a.desk), a.what or "", a.name or ""))


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
    if a.turns:
        rep = cost.turns(Desk.find(a.desk), a.days, _since(a.since) if a.since else None)
        print(json.dumps(rep, ensure_ascii=False, indent=1) if a.json else views.cost_turns_text(rep))
        return
    rep = cost.report(Desk.find(a.desk), a.days, _since(a.since) if a.since else None)
    if a.json:
        rep = {k: v for k, v in rep.items() if k != "rows"} if not a.rows else rep
        print(json.dumps(rep, ensure_ascii=False, indent=1))
    else:
        print(views.cost_text(rep, a.by))


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
    try:
        port = start_web(desk, a.host, a.port).server_address[1]
    except OSError as e:
        if e.errno != errno.EADDRINUSE:
            raise
        raise SystemExit(f"hansa: port {a.port} is in use, perhaps by another desk's hansa serve; start this desk's "
                         f"server on a free port with hansa serve --port N (hansa web then finds it)")
    (desk.state_dir / "web-port").write_text(str(port), encoding="utf-8")  # what hansa web opens
    beat(desk)  # its rounds count from now, not from the last server's
    pulse(desk)
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


def web_port(desk: Desk) -> int:
    """The port this desk's hansa serve last served the page on: another desk's server may hold the default."""
    try:
        return int((desk.state_dir / "web-port").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 8765


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
    good, more = deny_split(given, desk.root, None if theirs else desk.settings()["lanes"])
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
    kept, unkept = kept_from_workers(s, desk.root)
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


LANE_LISTS = ("providers", "allow", "network", "write")
LANE_NUMBERS = ("max_turns", "timeout_min", "merge_timeout_min")
LANE_FIELDS = ("base", "providers", "allow", "network", "write", "env", "check", "merge_check", "preview", "setup",
               "release", "max_turns", "timeout_min", "merge_timeout_min", "note", *MESSAGE_FIELDS)
# empty (none) takes them out: the check's command and limit again, hansa's own message forms
LANE_UNSET = ("merge_check", "merge_timeout_min", *MESSAGE_FIELDS)
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
    if field in LANE_LISTS:
        return [x for v in values for x in (v.split(",") if field == "providers" else [v]) if x]
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
            if a.name in s["lanes"]:
                raise SystemExit(f"hansa: there is already a lane {a.name}; change it with hansa lane set {a.name} ...")
            # none as lane set takes it: alone it means empty, with other values it is refused
            given = {f: _lane_value(f, v if isinstance(v, list) else [v]) for f, v in (
                ("providers", a.providers), ("check", a.check), ("setup", a.setup), ("allow", a.allow),
                ("network", a.network)) if v is not None}
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
                ln = json.loads(json.dumps(like))  # a copy: later changes to the other lane do not reach this one
                ln.update(given, **({"base": a.base} if a.base else {}))
                for f in ("check", "setup", "merge_check"):
                    if ln.get(f) == "":
                        del ln[f]
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
            if not a.like:
                try:
                    base = a.base or git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo)
                except (RuntimeError, OSError) as e:
                    raise SystemExit(f"hansa: {repo} is not a git checkout ({e}); nothing was changed")
                ln = default_lane(repo, base, given.pop("providers", None) or None)
                ln.update({k: v for k, v in given.items() if v})
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
                  + "".join(f"; {f} {_shown(ln[f])}" for f in ("allow", "network", "setup", "check", "merge_check",
                                                              "merge_timeout_min") if ln.get(f)))
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
            return
        if a.action == "remove":
            return _lane_remove(desk, a, s, new)
        if a.name not in s["lanes"]:
            raise SystemExit(f"hansa: there is no lane {a.name}; lanes: {', '.join(sorted(s['lanes'])) or 'none'}")
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
    if a.field == "network" and set(old) != set(value):  # the same hosts in another order change nothing
        desk.event("lane_network_changed", lane=a.name, old=old, new=value)
        print(f"The hosts the lane's workers may reach during a task changed: {_shown(old)} -> {_shown(value)}, so "
              f"this change is recorded. Tell the principal, in the conversation, what you changed and why.")
    if a.field == "network" and not any(is_local(h) for h in old):  # told once, when localhost comes on the list
        warning = _loopback_warning(a.name, value)
        if warning:
            print(desk.redact(warning))
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


def _remove_worktree(desk: Desk, lane: str, wt: Path, st: dict, keep: bool) -> str:
    """git worktree remove, after putting what it holds on a new branch when keep; the branch's name or ''."""
    from .run import _remember_git_file, commit_work, wgit
    kept = ""
    if keep and (st["changes"] or st["commits"]):
        kept = f"hansa-kept/{wt.name}-{time.strftime('%Y%m%d-%H%M%S')}"
        wgit(desk, wt, "checkout", "-q", "-b", kept)
        commit_work(desk, wt, f"Left in worktree {wt.name} when lane {lane} was removed")
    # --force: what a worker's sandbox left there is nobody's work, and git would refuse for it
    git("worktree", "remove", "--force", str(wt), cwd=st["repo"])
    _remember_git_file(desk, wt, None)
    desk.event("lane_worktree_removed", lane=lane, worktree=str(wt), **({"kept": kept} if kept else {}))
    return kept


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
            kept = _remove_worktree(desk, name, wt, st, a.keep)
        except RuntimeError as e:
            print(f"Its worktree {wt} stays: {e}. hansa lane remove {name} tries again")
            continue
        print(f"Its worktree {wt} is removed, with git worktree remove in {st['repo']}, as the desk's account"
              + (f"; what it held that no branch kept is on branch {kept} there" if kept else
                 "; what it held that no branch kept is dropped" if held.get(wt) else ""))


def _lane_try(desk: Desk, a) -> None:
    """Have hansa serve run the lane's setup and check once, as for a task but with no worker, and wait for it."""
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
        t = read_try(desk, a.name)
        if t.get("phase") in ("queued", "running"):  # asked for before, by a command that stopped waiting
            print(f"a try of lane {a.name} is already {t['phase']}; waiting for it", flush=True)
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
                 **({"merge": True} if a.merge_check else {})}
            write_try(desk, a.name, t)
            desk.event("lane_try_queued", lane=a.name, **({"merge_check": True} if a.merge_check else {}))
            steps = ["setup"] * bool(ln.get("setup")) + (
                [f"merge check ({merge_command(ln)}, stopped after {merge_minutes(ln)} minutes)"] if a.merge_check
                else ["check"] * bool(ln.get("check")))
            print(f"lane {a.name}: hansa serve runs its " + " and ".join(steps)
                  + f" in its worktree, at {ln.get('base') or 'main'}'s tip, in its sandbox; waiting for the result",
                  flush=True)
    gone = None
    while True:
        now_t = read_try(desk, a.name)
        if now_t.get("id") != t["id"]:
            raise SystemExit(f"hansa: the try of lane {a.name} was replaced by another; see hansa lane try {a.name}")
        if now_t.get("phase") == "done":
            break
        st = server_state(desk)  # stuck runs no rounds, so it neither starts nor finishes the try
        if st["state"] not in ("gone", "stuck"):
            gone = None
        elif gone is None:
            gone = time.time()
        elif time.time() - gone > 60:
            why = ("stopped" if st["state"] == "gone" else
                   f"(pid {st['pid']}) is busy or stuck, with no round finished for {quiet_text(st['quiet'])},")
            again = "Start hansa serve" if st["state"] == "gone" else "Once its rounds come again (hansa explain server)"
            with desk.lock():  # a server that comes back now starts it under this lock, or finds it gone
                last = read_try(desk, a.name)
                if last.get("id") == t["id"] and last.get("phase") == "queued":  # never started: it would hold the lane
                    try_file(desk, a.name).unlink(missing_ok=True)
                    desk.event("lane_try_withdrawn", lane=a.name)
                    raise SystemExit(f"hansa: hansa serve {why} before the try started, so it was withdrawn and "
                                     f"lane {a.name} is free again; nothing was run. {again}, then "
                                     f"hansa lane try {a.name}")
            raise SystemExit(f"hansa: hansa serve {why} before the try ended. Once "
                             + ("it runs" if st["state"] == "gone" else "its rounds come") + " again, "
                             f"hansa lane try {a.name} waits for this try's result")
        time.sleep(a.every)
    print(desk.redact(views.try_text(a.name, now_t)))
    if not now_t.get("ok"):
        raise SystemExit(1)


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
    p.description = ("hansa release L [note]: ask for a release of lane L's base branch as it is now. hansa records "
                     "the commit, the lane's release command (hansa lane set L release \"...\"), the note, the commits "
                     "that go out (on no remote-tracking branch and not in the lane's last release) and the files they "
                     "change; a word the principal keeps from going out, in the command line or in a line, a file name "
                     "or a message of any of those commits, refuses it. It waits for the principal's yes on the web "
                     "page (unless the principal lists the lane as released without asking and the desk is not "
                     "paused); then hansa serve runs the command in the lane's repository, as "
                     "the desk's account, outside the sandbox, with hansa's environment and the lane's minutes as its "
                     "limit, and only while the base branch and the command are what the principal saw. hansa release "
                     "alone lists the open and recent releases. Only the coordinator asks")
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
    p = add("lane", cmd_lane, "list the lanes, or add, change or remove one")
    lane = p.add_subparsers(dest="action", metavar="add | set | brief | remove | try")
    q = lane.add_parser("add", help="a new lane on a git checkout", description=(
        "A new lane on a git checkout. A lane runs one task at a time, in its own full worktree (the repository's "
        "disk space again): to run tasks side by side add lanes on the same repository and base, up to hansa "
        "concurrency in all, and to split work among providers give each its own lane with its provider order. "
        "The lane's setup and check run with /bin/sh -c: POSIX shell only, no bash syntax; so write its preview "
        "(started by a design session's Bash tool) the same way. " +
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
                   "providers, setup, check, network, folders it may write, env, limits) and standing instructions; "
                   "each other option given replaces that one field. It is a copy: later changes to LANE do not "
                   "reach the new lane. The worktree is the new lane's own. Not with --repo")
    q.add_argument("--base", help="base branch (default: the checkout's current branch)")
    q.add_argument("--providers", help="provider order, comma-separated (default: claude)")
    q.add_argument("--check", help="the command hansa runs after each task, and on a merge when the base has moved "
                   "unless the lane has a merge_check (/bin/sh -c)")
    q.add_argument("--setup", help="the command that installs dependencies before each task (/bin/sh -c)")
    q.add_argument("--network", nargs="+", metavar="HOST", help="the hosts its workers, setup and check may reach "
                   "(default: none)")
    q.add_argument("--allow", nargs="+", metavar="RULE", help="commands a design session may run without asking, such as "
                   "'Bash(pnpm test:*)'; workers run any command in their sandbox and need none")
    q = lane.add_parser("set", help="change one field of a lane", description=(
        "allow is the commands a design session may run without asking (workers run any command in their "
        "sandbox); providers, allow, network and write take the whole new list (none: empty); env takes NAME=VALUE pairs and replaces the "
        "lane's env; base, check, merge_check, preview, setup, release, note, commit_message and merge_message take "
        "one value (none: empty); max_turns, timeout_min and merge_timeout_min take one whole number. check runs "
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
        "commit_message or merge_message out again. release is the command line that sends the base branch out (git push "
        "origin main, say): hansa runs it after the principal's yes on the page (hansa release L). " +
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
    p.add_argument("--by", default="day", choices=["day", "seat", "lane", "task", "model", "provider"])
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
