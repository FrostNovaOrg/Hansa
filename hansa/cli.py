"""The `hansa` command. Every subcommand is a thin wrapper over desk, run, views and cost."""
from __future__ import annotations

import argparse
import errno
import fcntl
import getpass
import json
import os
import re
import shlex
import signal
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import account, alert, build, cost, gaps, installed_text, notes, notify, plugin, release, session_vars, up, views, without_session
from .desk import (EFFORTS, Desk, SettingsError, allow_fix, default_lane, default_settings, deny_parts, no_read_parts, stop_text, home_secrets, identity_problem, json_text,
                   lane_targets, release_files_in, merge_message, message_fallback, MESSAGE_FIELDS, message_problem, now, own_title, title_forms, program, read_json, refused_rule, resolved, write_json,
                   argv_rules_problem, rules_problem, path_rule_problem, desk_paths_problem, lane_repos, other_desks, path_spellings, pattern_path_problem, run_homes,
                   PROJECT_INTRO, answer_name, compact_settings, work_lane_problem, write_text)
from .run import (HOLDING_VERDICTS, VERIFY_TASK, Dispatcher, base_moved, branch_holds, check_provider, close_verification, conflict_advice,
                  conflict_places, conflicted_files, finish_merge_check, rounds_of, git, identity_env, keys_in_change, lane_holder,
                  lane_identity, lane_occupied, occupied_lane, merge_lane, merge_check_lane,
                  MARKERS_ON_PURPOSE, beat, markers_in_change, markers_text, merge_check_ahead, merge_command, merge_minutes, merge_needs_check, merge_target,
                  not_looking, own_merge_check, proc_start, provider_env, pulse,
                  quiet_text, read_try, record_serve_code, record_serve_locale, record_serve_session, serve_session_text, server_state, stop, try_file, try_log, try_srt_at_start, unverified_note, waiting_reason, words_in_merge, words_text, write_try,
                  is_local, mac_loopback_listeners)

CARD = Path(__file__).with_name("card.md")
DOCS = Path(__file__).resolve().parent.parent / "docs"  # where hansa help TOPIC reads; an install may leave it out
# hansa help TOPIC: the topic's file under docs/, its heading (None: the whole file), and what it holds. No name
# equals a command, a lane subcommand or an explain target, a plural of one or a prefix of one (a test holds it)
TOPICS = {
    "workers-sandbox": ("sandbox.md", "What workers and verifiers may touch",
                        "what workers and verifiers may read, write and reach"),
    "git-folders": ("sandbox.md", "Git's folders", "what each run may write in git's folders, and what hansa checks "
                    "before it runs git in a worktree"),
    "sandbox-probe": ("sandbox.md", "The sandbox probe before a run", "the probe of the sandbox before a run, and "
                      "what holds runs back when it fails"),
    "write-env": ("sandbox.md", "A lane's write list and env", "the folders a lane's runs may write, and the "
                  "environment they get"),
    "network-list": ("sandbox.md", "A lane's network list", "the hosts a lane's runs may reach; localhost on Linux "
                     "and on macOS"),
    "web-tools": ("sandbox.md", "A lane's allow list: commands and web tools", "a lane's allow list: commands, "
                  "WebFetch and WebSearch, and the rules it refuses"),
    "refused-paths": ("sandbox.md", "Paths hansa refuses", "the paths hansa refuses on a lane's lists"),
    "leftovers": ("sandbox.md", "What hansa stops when a run ends", "what hansa stops when a run ends, and what "
                  "it leaves"),
    "release-flow": ("releases.md", "How a release goes out", "how a release goes out: targets, the principal's "
                     "answer and codes, fixed targets, stopping one"),
    "forbidden-words": ("releases.md", "Forbidden words before a release", "the word scan of the principal's "
                        "forbidden words, at a release, a merge and a title"),
    "release-files": ("releases.md", "Files a release runs outside the sandbox", "release_files: the files a "
                      "release runs outside the sandbox, and the verification a change to one needs"),
    "alerts": ("releases.md", "Alerts", "the principal's alert command: what it is told, and when"),
    "time-limits": ("tasks.md", "Time limits, and work left running in the background", "a task's limits of turns "
                    "and minutes, and work a worker leaves running in the background"),
    "provider-limits": ("tasks.md", "Dropped connections and usage limits", "retries after a dropped connection, "
                        "and a provider's rest after a usage limit"),
    "check-output": ("tasks.md", "A check's output", "where a check's and a merge check's output is kept, and "
                     "how to read it"),
    "effort": ("tasks.md", "Effort", "which effort a run gets, and where it comes from"),
    "writing-tasks": ("tasks.md", "Writing a task a verification can judge", "how to write a task, and its "
                      "verification's text, so that the verification can judge it"),
    "merge-check": ("tasks.md", "Merging, the merge check and conflicts", "merging, the merge check and conflicts, "
                    "and sending a task back"),
    "cost-figures": ("tasks.md", "What work costs", "what hansa cost counts, and what the figures mean"),
    "states": ("tasks.md", "Task states", "a task's states and what each means"),
    "inbox": ("desk.md", "Hearing from hansa", "how hansa posts to the coordinator's session, hansa wait, and the "
              "daily clear"),
    "bringing-up": ("desk.md", "Starting the server and the coordinator again", "hansa up, the saved starts of the "
                    "server and the coordinator's session, and this account's desk list"),
    "free-space": ("desk.md", "Free space", "the floor of free space under which hansa serve starts nothing new"),
    "desk-files": ("desk.md", "A desk's files", "where a desk's files are (keys, settings, notes/, PROJECT.md) and "
                   "where they grow"),
    "java": ("java-and-maven.md", None, "Java and Maven in a lane: the proxy, the temporary folder, "
             "mktemp, Maven's 407, Mockito, the lane's settings"),
    "linux-root": ("linux-server.md", "The coordinator as root, the desk as an ordinary account", "on a Linux "
                   "server: the coordinator as root, the desk as an ordinary account"),
    "linux-sandbox": ("linux-server.md", "What a lane's commands meet in Linux's sandbox", "what a lane's commands "
                      "meet in Linux's sandbox"),
    "who-changes-what": ("who-changes-what.md", "The principal, the coordinator, and what runs outside the worker",
                         "what the principal sets, what the coordinator sets, and what runs outside the worker"),
    "commits": ("who-changes-what.md", "Commit messages and identity", "the messages and the identity of the "
                "commits hansa makes"),
    "session-identity": ("who-changes-what.md", "A Claude Code session's identity, and the page's token", "what "
                         "a process started from a Claude Code session inherits from it, and the page's token"),
}
# hansa help: the commands, a few to a line (a command left out of these goes on the last line)
COMMAND_GROUPS = (("tasks", ("add", "show", "diff", "git", "merge", "retry", "verify", "cancel", "close")),
                  ("watching", ("status", "wait", "notify", "ask", "explain", "notes", "gaps", "cost")),
                  ("lanes and releases", ("lane", "release", "changelog", "hide", "concurrency", "disk-floor", "memory-floor", "pause", "resume")),
                  ("the desk", ("init", "up", "serve", "web", "coordinator", "ping", "help")))
HELPERS = Path(__file__).with_name("agents")  # the coordinator's helpers: scout, reviewer, operator
NO_SIGNATURE = Path(__file__).resolve().with_name("no-signature")  # hansa git's gpg: checks no signature
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
    moved = desk_paths_problem(desk, run_homes())  # a deny rule under it would close another folder
    if moved:
        raise SystemExit(f"hansa: {moved}; no desk was made")
    if repo:  # looked at before anything is made, so a refusal leaves no half-made desk
        if not resolved(repo):
            raise SystemExit(f"hansa: cannot follow the links of {repo} to see where it leads; no desk was made")
        repo = resolved(repo)
        theirs = account.foreign_repo(repo)
        if theirs:
            raise SystemExit(f"hansa: {theirs}; no desk was made")
        why = path_rule_problem(repo) or desk_paths_problem(desk, (), [(lane, repo)])
        if why:
            raise SystemExit(f"hansa: {why}; no desk was made")
        base = lane_base(repo, base, "no desk was made")
    for d in (desk.root, desk.tasks_dir, desk.lanes_dir, desk.state_dir, desk.notes_dir):
        d.mkdir(parents=True, exist_ok=True)
    desk.secrets_dir.mkdir(mode=0o700, exist_ok=True)
    s = default_settings()
    if repo:
        s["lanes"][lane] = default_lane(repo, base)
        (desk.lanes_dir / lane).mkdir(exist_ok=True)
        write_text(desk.lanes_dir / lane / "brief.md", "")
    write_json(desk.settings_path, s)
    write_text(desk.root / "CLAUDE.md", CARD.read_text(encoding="utf-8"))
    if not desk.project_path.exists() and not desk.project_path.is_symlink():  # made once; then only hansa notes archive writes it
        write_text(desk.project_path, PROJECT_INTRO)
    (desk.root / ".claude").mkdir(exist_ok=True)
    write_json(desk.root / ".claude" / "settings.json", {"permissions": permissions(desk)})
    desk.event("desk_created", repo=repo, lane=lane if repo else "")
    return desk


def lane_base(repo: str, base: str, nothing: str) -> str:
    """A new lane's base: base, or else the branch the checkout is on; refused in one line when it is not a git
    checkout, has no commit yet, or an explicit base does not resolve to a commit (a lane's tasks start from
    a commit). nothing: what was not done, said after."""
    try:
        if git("rev-parse", "--is-inside-work-tree", cwd=repo) and not subprocess.run(
                ["git", "rev-parse", "-q", "--verify", "HEAD^{commit}"], cwd=repo, capture_output=True).stdout.strip():
            raise SystemExit(f"hansa: {repo} has no commit yet; make a first commit there (git commit), then run "
                             f"this again; {nothing}")
        if base:
            try:
                git("rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}", cwd=repo)
            except RuntimeError:
                raise SystemExit(f"hansa: base {base!r} does not resolve to a commit in {repo}; choose an existing "
                                 f"branch (list them with git -C {shlex.quote(repo)} branch --list), then run this "
                                 f"again; {nothing}")
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
            "deny": desk_read_denials(desk) + [f"Edit({g})" for g in guarded] + other_desk_rules(desk, "Edit")
            + no_publish_rules()}


def desk_read_denials(desk: Desk) -> list:
    """Read denied on the desk's secrets/ and .hansa/: in its .claude/settings.json, and to a session hansa
    notify-launch starts (launch_rules); and on those of each other desk on the account's list (other_desk_rules)."""
    r = str(desk.root).lstrip("/")
    return [f"Read(//{r}/secrets/**)", f"Read(//{r}/.hansa/**)", *other_desk_rules(desk, "Read")]


def other_desk_rules(desk: Desk, tool: str) -> list:
    """Rules that deny `tool` the keys (secrets/) and state (.hansa/) of each other desk on the account's list
    (desk.other_desks), in each spelling of its path; none for a spelling whose rule Claude Code would split."""
    others = [x for d in other_desks(desk.root) for x in path_spellings(d) if not path_rule_problem(x)]
    return [f"{tool}(//{x.lstrip('/')}/{n}/**)" for x in dict.fromkeys(others) for n in ("secrets", ".hansa")]


def no_publish_rules() -> list:
    """The deny rules for pushing and publishing from the coordinator's shell: in the desk's .claude/settings.json,
    and on the command line of a session hansa notify-launch starts (launch_rules)."""
    return [f"Bash({c}:*)" for c in NO_PUBLISH] + [f"Bash({r})" for r in no_publish_with_options()]


# The desk's own files a session outside it may not change, besides what is in the desk now: those hansa writes
# there, made or not yet. Not notes/ (the coordinator's), nor worktrees/ (the lanes' checkouts, as their repositories:
# a commit made there directly is shown to the principal with the release that sends it)
DESK_FILES = ("settings.json", "secrets", "tasks", "lanes", ".hansa", "events.jsonl", "asks.json", "PAUSED",
              "CLAUDE.md", "CLAUDE.local.md", ".claude")


def launch_rules(desk: Desk, home: str) -> list:
    """The deny rules of a session hansa notify-launch starts: it runs in a folder of its own, so it reads none of
    the desk's settings. Those the desk's .claude/settings.json gives a session in the desk (no_publish_rules,
    desk_read_denials); Edit, which covers Write too, on each of the desk's files but notes/ and worktrees/, by its
    full path; Read on the built-in logins and keys under the home folder, as hansa coordinator gives its session.
    Not the principal's list of what workers may not read (it may hold what this session's own work reads), the
    no-edit rules on the lanes' repositories and worktrees, nor the helpers turned off: it has none of hansa's."""
    from .run import no_read_rules
    try:
        there = {e.name for e in os.scandir(desk.root)}
    except OSError:
        there = set()
    names = sorted({*DESK_FILES, *there} - {desk.notes_dir.name, desk.worktrees_dir.name})
    r = str(desk.root).lstrip("/")
    return [*no_publish_rules(), *desk_read_denials(desk),
            *(f"Edit(//{r}/{n}{end})" for n in names for end in ("", "/**")), *other_desk_rules(desk, "Edit"),
            *no_read_rules([os.path.join(home, f) for f in home_secrets()])]


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


def _file_problem(args, f, st=None) -> str:
    """Apply the worker content admission rules to text files, including before an account handoff."""
    from .attach import read_problem

    try:
        desk = Desk.find(getattr(args, "desk", None))
    except SystemExit:
        return ""
    try:
        return read_problem(desk, f, st)
    except RuntimeError as e:
        return f"cannot be read: {e}"


def _stdin_problem(args) -> str:
    """Check a regular file redirected to standard input; pipes carry text without a source path."""
    try:
        st = os.fstat(sys.stdin.fileno())
    except (OSError, ValueError):
        return ""
    if not stat.S_ISREG(st.st_mode):
        return ""
    path = ""
    try:
        if hasattr(fcntl, "F_GETPATH"):  # macOS
            path = fcntl.fcntl(sys.stdin.fileno(), fcntl.F_GETPATH, bytes(1024)).split(b"\0", 1)[0].decode()
        else:  # Linux
            path = os.readlink(f"/proc/self/fd/{sys.stdin.fileno()}")
            if path.endswith(" (deleted)"):
                try:
                    same = os.path.samestat(st, os.stat(path))
                except OSError:
                    same = False
                if not same:  # proc appends this suffix to the name of an unlinked file
                    path = path.removesuffix(" (deleted)")
    except (OSError, UnicodeDecodeError):
        pass  # private hard links can still be checked by device and inode
    return _file_problem(args, path, st)


def _text(args) -> str:
    if getattr(args, "file", None) == "-":  # on standard input: root hands a file's text to the desk's account so
        problem = _stdin_problem(args)
        if problem:
            raise SystemExit(f"hansa: standard input {problem}; nothing was changed")
        return sys.stdin.read()
    if getattr(args, "file", None):
        f = Path(args.file)
        if not f.is_file():
            raise SystemExit(f"hansa: {args.file} " + ("is a folder, not a file" if f.is_dir() else "does not exist")
                             + "; nothing was changed")
        try:
            f = f.resolve(strict=True)
            problem = _file_problem(args, f)
            if problem:
                raise SystemExit(f"hansa: {args.file} {problem}; nothing was changed")
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
    print("next: run `hansa up` there, open the page with `hansa web` and add providers and keys; "
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
    lane = merge_check_lane(desk.settings(), m)
    if lane != m["lane"]:
        return (f"One merge check runs at a time on {base}, so it waits for {ahead}." if ahead else
                views.merge_check_text(desk, m, sentence=True))
    return (f"One merge check runs at a time on {base}, so it waits for {ahead}, then runs when lane {lane} is "
            f"free." if ahead else f"It runs when lane {lane} is free.")


def _waits(desk: Desk, tid: str) -> str:
    """The line for a task just queued that no provider of its lane can start now, in hansa status's words;
    '' when one can."""
    m = desk.meta(tid)
    why = waiting_reason(desk, desk.settings(), m, set())
    return f"\n{tid} waits: {why}" if "no provider ready" in why else ""


def _title_allowed(desk: Desk, s: dict, title: str | None, text: str, lane: str) -> None:
    """Refuse, before anything is queued, a title that holds one of the principal's forbidden words: hansa puts it
    in the task's commit message and its merge's. With no --title it is the text's first line, cut at 80
    characters, as new_task makes it; the rest of the text is never committed and not looked at."""
    given = desk.redact(title or "")
    title = given or (desk.redact(text.strip()).splitlines() or [""])[0][:80]
    hits = release.title_hits(title, release.lane_words(s, lane), detailed=True)
    if any(h.get("kind") == "pattern_limit" for h in hits):
        raise SystemExit(desk.redact(
            f"hansa: the title could not be checked:\n{release.hits_text(hits)}\nNothing was queued"))
    found = [h["word"] for h in hits]
    if found:
        where = "--title" if given else "the title (the first line of the text, as no --title was given)"
        raise SystemExit(desk.redact(
            f"hansa: Words found that must not go out:\n"
            f"{where} holds {', '.join(json.dumps(w, ensure_ascii=False) for w in found)}, which the principal "
            f"keeps from going out, and the title goes into the task's commit message and its merge's: "
            f"{json.dumps(title, ensure_ascii=False)}. Nothing was queued: give another --title, or ask the principal "
            f"about the words (hansa ask). {see_more('forbidden-words')}"))


def cmd_add(a):
    desk = Desk.find(a.desk)
    s = desk.settings()
    _work_lane(s, a.lane)
    text = _text(a)
    _title_allowed(desk, s, a.title, text, a.lane)
    tid = desk.new_task(text, lane=a.lane or "", effort=a.effort or "", model=a.model or "",
                        title=a.title or "", max_turns=a.max_turns or 0, attach=a.attach,
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


def cmd_status(a):
    st = views.status(Desk.find(a.desk))
    print(json.dumps(st, ensure_ascii=False, indent=1) if a.json else views.status_text(st))


def cmd_show(a):
    desk = Desk.find(a.desk)
    print(views.report_text(desk, a.id) if a.report else views.check_output_text(desk, a.id) if a.check
          else views.check_log_text(desk, a.id) if a.check_log
          else views.check_log_text(desk, a.id, merge=True) if a.merge_check_log
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
    env = {k: v for k, v in without_session(os.environ).items() if not k.startswith("GIT_")}
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
        verification_note = (f"; verification {v['task']}: holds, with notes (report: hansa show {v['task']} --report)"
                             if v and v["verdict"] == "holds, with notes" else "")
        check_name = merge_lane(desk.settings(), m["lane"])
        check_lane = desk.settings()["lanes"][check_name]
        sess = notify.recorded(notify.session_here())  # the session that hears how a merge check ends
        tip = git("rev-parse", base, cwd=repo)
        if a.recheck:
            if not merge_command(check_lane):
                raise SystemExit(f"hansa: lane {check_name} has no check to run again; nothing was merged")
            by = os.environ.get("HANSA_ROLE", "cli")
            desk.update(a.id, state="merging", merge_check_result=None,
                        merge_check={"phase": "queued", "queued": now(), "moves": 0, "recheck": True, "by": by}, **sess)
            desk.event("check_rerun_queued", task=a.id, onto=tip, by=by, command=merge_command(check_lane))
            print(f"{a.id} is not merged yet: hansa runs the lane's {_merge_what(check_lane, lane)} again. It merges {a.id} onto "
                  f"{base} as it is now in lane {check_name}'s worktree, runs {_merge_step(check_lane, lane)} there, and merges "
                  f"exactly that if it passes. {_runs_when(desk, a.id, base)} "
                  f"{_wait_for(desk, a.id)} (hansa show {a.id} says where it is; hansa cancel {a.id} stops it)"
                  + verification_note + _unverified_later(desk, m))
            return
        # the base has commits the task's branch does not: the two together were never checked; or the lane's
        # merge check runs a command of its own, which ran on neither
        why_check = merge_needs_check(lane, m, repo, tip, check_lane)
        unchecked = bool(why_check)
        if unchecked and not a.despite_check:
            desk.update(a.id, state="merging", merge_check={"phase": "queued", "queued": now(), "moves": 0},
                        merge_check_result=None, **sess)
            behind = git("rev-list", "--count", f"{m['head']}..{tip}", cwd=repo)
            desk.event("merge_check_queued", task=a.id, onto=tip, command=merge_command(check_lane))
            why = (f"{base} has {behind} commit(s) that {a.id}'s branch does not, so the two together were never "
                   f"checked" if why_check == "moved" else
                   f"lane {check_name} has a merge check of its own, which hansa runs before every merge, not after "
                   f"each task")
            print(f"{a.id} is not merged yet: {why}. hansa merges {a.id} onto {base} as it is now in lane "
                  f"{check_name}'s worktree, runs {_merge_step(check_lane, lane)} there, and merges "
                  f"exactly that if it passes. "
                  f"{_runs_when(desk, a.id, base)} {_wait_for(desk, a.id)} "
                  f"(hansa show {a.id} says where it is; hansa cancel {a.id} stops it)"
                  + verification_note + _unverified_later(desk, m))
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
                        f"; if it has, it merges {a.id} onto {base} as it is then in lane {check_name}'s worktree, runs "
                        f"{_merge_step(check_lane, lane)} there, and merges exactly that if it passes"
                        if merge_command(check_lane) else ""))
            note = unverified_note(desk, m)
            checks = any(x["state"] == "merging" and x["id"] != a.id and merge_target(s, x.get("lane")) == at
                         and not (x.get("merge_check") or {}).get("plain") for x in desk.tasks())
            there = ("while a merge check runs or waits there" if checks else
                     "while other merges wait there (none of them needs a check)" if not releasing else
                     f"and not while a release runs on {repo}")
            print(f"{a.id} is not merged yet: merges go into {base} one at a time {there}, in the order they were "
                  f"asked, so it waits for {ahead}. When its turn comes, hansa merges it "
                  f"{then}. {_wait_for(desk, a.id)} (hansa show {a.id} says where it is; hansa cancel {a.id} stops it)"
                  + verification_note
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
                    merge_repo=release.repo_key(repo), merge_base=base,
                    unverified=note or None, merge_message_note=said or None)
        close_verification(desk, m, f"closed: {a.id} was merged")
    despite = (chk and not chk["ok"]) or (v and v["verdict"] == "does not hold") or unchecked
    desk.event("task_merged", task=a.id, into=base, **({"despite_check": True} if despite else {}),
               **({"merge_not_checked": True} if unchecked else {}))
    print(f"{a.id} merged into {base} at {repo}" + (f" (without checking it together with what {base} gained "
                                                     f"since it was made)" if why_check == "moved" else
                                                     f" (without running the lane's merge check, "
                                                     f"{merge_command(check_lane)})" if unchecked else "")
          + verification_note)
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


def _merge_what(lane: dict, task_lane: dict | None = None) -> str:
    """What a lane's merge check runs, named: its check, or its merge check when that is a command of its own."""
    return "merge check" if own_merge_check(lane, task_lane) else "check"


def _merge_step(lane: dict, task_lane: dict | None = None) -> str:
    """What a lane's merge check runs, with the command, for hansa merge to say."""
    return f"the lane's {'setup and ' if lane.get('setup') else ''}{_merge_what(lane, task_lane)} ({merge_command(lane)})"


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
                         f"markers in {', '.join(markers)}\"\n{MARKERS_ON_PURPOSE}\n{see_more('merge-check')}")
    words = words_in_merge(desk, repo, git("rev-parse", base, cwd=repo), m["head"],
                           merge_message(lane, m["lane"], a.id, m["title"]), lane, m["lane"])
    if words:
        desk.event("merge_forbidden", task=a.id, found=len(words))
        raise SystemExit(desk.redact(f"hansa: {words_text(a.id, words)}. Nothing was merged"))
    v = views.verification(desk, m)
    if v and v["state"] in ("queued", "running"):
        raise SystemExit(f"hansa: {v['task']} is still verifying {a.id}; nothing was merged. {_wait_for(desk, v['task'])}")
    touched = _release_files_touched(repo, m, lane)
    if touched and not (v and v["verdict"] in HOLDING_VERDICTS):
        beyond = len(touched) - 10
        raise SystemExit(
            f"hansa: the change in {a.id} touches what lane {m['lane']}'s releases build and run outside the sandbox "
            f"(its release_files): {', '.join(touched[:10])}" + (f" and {beyond} more" if beyond > 0 else "")
            + f"; nothing was merged, and no flag overrides this (--despite-check is for a failed check, not this). "
            + (f"Its verification {v['task']} " + ("found that it does not hold" if v["verdict"] == "does not hold"
                                                  else f"ended {v['verdict'] or 'without a verdict'}")
               + ". " if v else "Nothing has verified it. ")
            + f"Have it verified: hansa verify {a.id}, and merge once the verification holds. {see_more('release-files')}")
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
        raise SystemExit("hansa: --update continues from the earlier task and --fresh starts from the base; "
                         "choose one. Nothing was queued")
    lanes = desk.settings()["lanes"]
    own = lanes.get(m.get("lane"))
    lane = a.lane if a.lane is not None else m.get("lane", "")
    again = f"; nothing was queued. Give the work again as a new task: hansa add --lane {a.lane or 'L'}"
    if own is None and (m.get("lane") or a.lane is not None):
        raise SystemExit(f"hansa: {a.id}'s lane {m.get('lane') or '(none)'} is no longer on the desk" + again)
    if own and m.get("repo") and not _same_repo(own["repo"], m["repo"]):
        raise SystemExit(f"hansa: {a.id}'s lane {m['lane']} is now on {own['repo']}, not on {m['repo']}, "
                         "where the task ran" + again)
    _work_lane(desk.settings(), m.get("lane"))
    if a.lane is not None:
        if lane not in lanes:
            raise SystemExit(f"hansa: there is no lane {lane}; nothing was queued")
        _work_lane(desk.settings(), lane)
        if not _same_repo(lanes[lane]["repo"], own["repo"]):
            raise SystemExit(f"hansa: lane {lane} is on another repository ({lanes[lane]['repo']}), not on "
                             f"{own['repo']}, the repository of {a.id}'s lane {m['lane']}; nothing was queued")
        if lanes[lane].get("base", "main") != own.get("base", "main"):
            raise SystemExit(f"hansa: lane {lane} merges into {lanes[lane].get('base', 'main')}, not "
                             f"{own.get('base', 'main')}, the base branch of {a.id}'s lane {m['lane']}; "
                             "nothing was queued")
    if a.update and not m.get("head"):
        raise SystemExit(f"hansa: {a.id} has no branch to bring up to date; start over with hansa retry {a.id} --fresh "
                         f"\"...\". Nothing was queued")
    if not a.fresh and m.get("head") and m.get("kind") != "verify":  # it continues from the recorded commit
        held = branch_holds(own["repo"], m) if own else ""
        if held:
            raise SystemExit(f"hansa: {held}; nothing was queued. Start over from the base: hansa retry {a.id} "
                             f"--fresh \"...\"")
    if m["state"] == "replaced":
        raise SystemExit(f"hansa: {a.id} was already replaced by {m.get('replaced_by')}; send that one back instead")
    if a.file and a.note:
        raise SystemExit("hansa: give the note as words or with --file, not both. Nothing was queued")
    note = (_text(a) if a.file else " ".join(a.note)).strip()
    text = (_follow_up_start(desk, m) + f"\nFollow-up to {a.id} ({m['state']})."
            + (f" What to change: {note}" if note else ""))
    report = desk.tasks_dir / a.id / "result.md"
    if report.exists() and report.read_text(encoding="utf-8").strip():
        text += "\nThe earlier task's report:\n" + report.read_text(encoding="utf-8").strip()[-6000:]
    vid = m.get("verify")  # the latest verification of the attempt, whatever it found, given whole
    vreport = desk.tasks_dir / vid / "result.md" if vid else None
    if vreport and vreport.exists() and vreport.read_text(encoding="utf-8").strip():
        verdict = desk.meta(vid).get("verdict") or "none"
        text += (f"\n\nThe report of {vid}, the verification of the earlier task (a worker who ran the change "
                 f"to see whether it holds; its verdict: {verdict}):\n" + vreport.read_text(encoding="utf-8").strip())
    opts = {"lane": lane, "effort": a.effort or m.get("effort", ""), "model": m.get("model", ""),
            "parent": a.id, "title": f"{own_title(m['title'])} (retry of {a.id})",
            **notify.recorded(notify.session_here())}
    base = (own or {}).get("base") or "main"
    if a.update:  # the base's tip when it starts is merged into its branch then (run.prepare)
        opts.update(start_from=m["head"], update=True)
        text += (f"\nYou start from the earlier task's last commit ({m['head'][:10]}), with {base} as it is now "
                 f"merged into it.")
    elif not a.fresh and m.get("head"):
        opts.update(start_from=m["head"], base=m["base"])
        text += f"\nYou start from the earlier task's last commit ({m['head'][:10]})."
    tid = desk.new_task(text, attach=a.attach, attachments_from=a.id, **opts)
    write_text(desk.tasks_dir / tid / NOTE_FILE, desk.redact(note) + "\n")  # later rounds quote it
    if m["state"] in ("done", "failed"):  # the follow-up carries the work on; the original leaves the review list
        desk.update(a.id, state="replaced", replaced_by=tid, ended_as=m["state"])
        close_verification(desk, m, f"closed: {a.id} was replaced by {tid}")
    print(f"{tid} queued as a follow-up to {a.id}" + (f" on lane {lane}" if lane != m.get("lane", "") else "")
          + (" from the base branch" if a.fresh else
                                                      f", with the current {base} merged into its branch when it starts "
                                                      f"(a conflict is left for the worker to settle)" if a.update else "")
          + (f". {_wait_for(desk, tid)}" if notify.hears_of(desk, tid) else "") + _waits(desk, tid))


NOTE_FILE = "note.md"  # beside a follow-up's task.md: the note it was sent back with, on its own (empty: none)
NOTE_START = 150  # how much of an earlier round's note its line quotes


def _follow_up_start(desk: Desk, m: dict) -> str:
    """The start of a follow-up's text to the task m: the original task (the chain's first, which is no follow-up),
    then one line per earlier round, oldest first, m's own last. Earlier notes and reports are not carried whole:
    each round keeps them in its own text (hansa show ID --full)."""
    rounds = rounds_of(desk, m)
    first = (desk.tasks_dir / rounds[0]["id"] / "task.md").read_text(encoding="utf-8").strip()
    if rounds[0].get("parent"):  # the chain's first is gone: its text is the start of the earliest left
        first = first.split("\n\n---\n")[0]
    lines = []
    for r in rounds:
        state = m["state"] if r is m else r.get("ended_as") or r["state"]
        vid = r.get("verify")
        how = state + (f", verified by {vid} (verdict: {desk.meta(vid).get('verdict') or 'none'})" if vid
                       else ", not verified")
        f = desk.tasks_dir / r["id"] / NOTE_FILE
        if not r.get("parent"):
            said = "the original task, above"
        elif not f.exists():
            said = "its note was not recorded on its own (it was sent back before hansa recorded notes)"
        else:
            n = " ".join(f.read_text(encoding="utf-8").split())
            said = f"its note: \"{n[:NOTE_START]}{' ...' if len(n) > NOTE_START else ''}\"" if n else "no note"
        lines.append(f"- {r['id']}: {how}; {said}; its whole text: hansa show {r['id']} --full")
    return (first + "\n\n---\nThe earlier rounds of this task, oldest first:\n" + "\n".join(lines)
            + "\nTheir notes and reports are not carried beyond these lines: what still holds of an earlier note is "
              "said again in this round's note.")


def cmd_cancel(a):
    desk = Desk.find(a.desk)
    by = notify.session_here()  # the session that cancelled it hears nothing of it; others do
    with desk.lock():  # the state read here is the state changed here
        m = desk.meta(a.id)
        if m["state"] == "queued":
            desk.update(a.id, state="cancelled", error="cancelled before it started", finished=now(), cancel_by=by)
        elif m["state"] == "running":  # hansa serve stops it; stopping it here as well is only quicker
            m = desk.update(a.id, cancel=True, cancel_by=by)
        elif m["state"] == "done":
            raise SystemExit(f"hansa: {a.id} is finished; cancel stops queued, running or merging work. "
                             f"To close it without merging: hansa close {a.id}; nothing was changed")
        elif m["state"] == "merging":  # the merge check stops; the task goes back to waiting for review
            mc = m.get("merge_check") or {}
            if mc.get("phase") == "queued":
                desk.update(a.id, state="done", merge_check=None)
            else:
                desk.update(a.id, merge_check={**mc, "cancel": True})
                if mc.get("leader") and mc["leader"] != a.id:
                    leader = desk.meta(mc["leader"])
                    desk.update(mc["leader"], merge_check={**leader["merge_check"], "batch_cancel": True})
                owner = mc.get("leader", a.id)
                stopping_merge = desk.meta(owner).get("merge_check") or {}
        else:
            raise SystemExit(f"hansa: {a.id} is {m['state']}; only queued, running or merging tasks "
                             f"can be cancelled")
        if m["state"] != "merging":
            close_verification(desk, m, f"closed: {a.id} was cancelled", by)
    if m["state"] == "running":
        stop(m.get("pid"), m.get("pid_start", ""))
    if m["state"] == "merging":
        mc = m.get("merge_check") or {}
        if mc.get("phase") != "queued":
            stop(stopping_merge.get("pid"), stopping_merge.get("pid_start", ""))
            with desk.lock():
                current = desk.meta(owner).get("merge_check") or {}
                if current.get("launch") == stopping_merge.get("launch"):
                    finish_merge_check(desk, owner)  # serve may already have split the batch and started its next check
        desk.event("merge_check_cancel", task=a.id)
        print(f"{a.id}'s merge check stopped; nothing was merged, and {a.id} is back waiting for review")
        return
    desk.event("task_cancel", task=a.id)
    print(f"{a.id} cancelled")


def cmd_close(a):
    desk = Desk.find(a.desk)
    by = notify.session_here()
    with desk.lock():
        m = desk.meta(a.id)
        if m["state"] != "done":
            raise SystemExit(f"hansa: {a.id} is {m['state']}; only finished tasks or verifications can be closed; "
                             "nothing was changed")
        if m.get("kind") == "verify":
            desk.update(a.id, closed=m.get("closed") or f"closed: {a.id} was closed with hansa close")
        else:
            desk.update(a.id, state="cancelled", error="closed without merging", cancel_by=by)
        close_verification(desk, m, f"closed: {a.id} was cancelled", by)
    if m.get("kind") == "verify":
        if not m.get("closed"):
            desk.event("verification_closed", task=a.id, by="hansa close")
        print(f"{a.id} closed; it stays done" + (f", verdict: {m['verdict']}" if m.get("verdict") else ""))
    else:
        desk.event("task_cancel", task=a.id)
        print(f"{a.id} closed without merging")


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
    v = views.verification(desk, m)
    if v and v["state"] in ("queued", "running"):
        raise SystemExit(f"hansa: {v['task']} is already verifying {a.id}")
    lane = m.get("lane", "")
    s = desk.settings()
    selected = ""
    if not a.lane:
        candidates = [n for n, ln in s["lanes"].items() if lane in ln.get("verify_for", [])]
        if candidates:
            tasks = desk.tasks()
            busy = {occupied_lane(t) for t in tasks if lane_occupied(t)}
            queued = {n: sum(t["state"] == "queued" and t.get("lane") == n for t in tasks) for n in candidates}
            selected = min(candidates, key=lambda n: (n in busy or queued[n] > 0, queued[n]))
    target = a.lane or selected or lane
    _work_lane(s, target, verifying=True)
    own = s["lanes"].get(lane)
    moved = own is not None and m.get("repo") and not _same_repo(own["repo"], m["repo"])
    if target != lane or own is None or moved:
        _lane_reaches(desk, target, m)
        lane = target
    held = branch_holds(desk.settings()["lanes"][lane]["repo"], m)  # what is verified is the recorded commit
    if held:
        raise SystemExit(f"hansa: {held}; nothing was queued. hansa show {a.id} says what it holds")
    d = desk.tasks_dir / a.id
    task = (d / "task.md").read_text(encoding="utf-8").strip()
    report = (d / "result.md").read_text(encoding="utf-8").strip() if (d / "result.md").exists() else ""
    note = " ".join(a.note).strip()
    sess = notify.session_here()
    text = (VERIFY_TASK.format(id=a.id, base=m["base"][:12], head=m["head"][:12]) + _check_ran(desk, m)
            + (f"\nLook in particular at: {note}\n" if note else "")
            + ("\nThis task is a follow-up: what it asked is the original task together with this round's note, "
               "which restates what still holds of earlier rounds' notes. Where the two differ, the note stands; "
               "judge each of those asks against the whole change.\n" if m.get("parent") else "")
            + f"\n## Task {a.id} as it was given\n{task}\n\n## The report of the worker who did it\n{report or '(none)'}\n")
    vid = desk.new_task(text, kind="verify", parent=a.id, lane=lane, effort=a.effort or "",
                        attachments_from=a.id,
                        start_from=m["head"], base=m["head"], title=f"Verify {a.id}: {m['title']}"[:80],
                        **notify.recorded(sess))
    if v:
        close_verification(desk, m, f"closed: {vid} verifies {a.id} again")
    desk.update(a.id, verify=vid, **notify.recorded(sess))
    print(f"{vid} queued" + (f" on lane {lane}" if lane != m.get("lane", "") else "")
          + (f" (chosen as a verify lane for lane {m['lane']})" if selected else "")
          + f": a worker will run {a.id}'s change to see whether it holds. "
          + (_wait_for(desk, vid) if notify.hears_of(desk, vid) else f"Follow it with: hansa wait {vid}")
          + f"; the verdict shows in hansa show {a.id}" + _waits(desk, vid))


def _check_ran(desk: Desk, m: dict) -> str:
    """For a verification's text: the task's last check, as hansa ran it, so that its worker need not run the
    whole of it again."""
    chk = m.get("check") or {}
    if not chk.get("command"):
        return f"\nTask {m['id']} has no check: hansa ran none on it.\n"
    cmd = desk.redact(chk["command"])
    if chk.get("could_not_run"):
        return f"\nhansa could not run the lane's check on task {m['id']} ({chk.get('note', '')}): `{cmd}`\n"
    ended = (m.get("check_started") or 0) + (chk.get("seconds") or 0) if m.get("check_started") else None
    when = f" at {time.strftime('%Y-%m-%d %H:%M', time.localtime(ended))}" if ended else ""
    how = f"passed{when}" if chk.get("ok") else f"failed{when} ({chk.get('note') or 'exit ' + str(chk.get('exit'))})"
    return (f"\nThe task's last check, which hansa ran on it: `{cmd}`. It {how}. The whole check need not be run "
            f"again unless you need to.\n")


def _same_repo(x: str, y: str) -> bool:
    return (resolved(x) or x) == (resolved(y) or y)


def _work_lane(s: dict, name, *, verifying: bool = False) -> None:
    why = work_lane_problem(s, name, verifying=verifying)
    if why:
        detail = ": no tasks, verifications or retries" if s["lanes"][name].get("merge_lane") else ""
        raise SystemExit(f"hansa: {why}{detail}; nothing was queued")


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
        raise SystemExit("hansa: only the coordinator stops a release; a worker or verification "
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
                         "CODE. Never guess one; without it, the principal answers on the page. Nothing was changed. "
                         + see_more("release-flow"))
    yes = a.lane.strip().lower() == "yes"
    if yes and len(rest) > 1:  # a code split over words, or a reason a yes does not take: nothing is checked or counted
        raise SystemExit(f"hansa: hansa release yes {rid} CODE takes the code as one word (dashes allowed, no spaces), "
                         "and nothing after it; pass it again as one word. Nothing was checked or counted. "
                         + see_more("release-flow"))
    out = release.answer_by_code(desk, rid, yes, rest[0], " ".join(rest[1:]))  # by the coordinator, for the principal
    if not out["ok"]:
        raise SystemExit("hansa: " + desk.redact("; ".join(out["problems"])) + ". " + see_more("release-flow"))
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
    """Return when a watched task stops running, a to-do is closed, or time is up.
    Meant to run in the background, so the session is told when there is something to look at."""
    desk = Desk.find(a.desk)
    live = lambda t: desk.meta(t)["state"] in ("queued", "running", "merging")  # noqa: E731
    watch = sorted(set(a.ids)) or sorted(m["id"] for m in desk.tasks() if m["state"] in ("queued", "running", "merging"))
    open_asks = {x["id"] for x in desk.asks() if not x["closed"]}
    done_already = [t for t in watch if not live(t)]
    if not done_already and not any(map(live, watch)) and not open_asks:
        print("nothing is queued or running and no request to the principal is open: nothing to wait for")
        return
    if server_state(desk)["state"] == "gone":  # as hansa status's server_up says it
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
        if os.environ.get("HANSA_TASK"):  # a worker or verification: the requests are the coordinator's
            raise SystemExit("hansa: only the coordinator closes a request; a worker or verification "
                             "cannot. Say in your report that it may be done")
        Desk.find(a.desk).close_ask_as_coordinator(a.close, " ".join(a.text), by=os.environ.get("HANSA_ROLE", "cli"))
        print(f"ask #{a.close} is closed; the principal's page shows it as closed by the coordinator, with the outcome")
        return
    n = Desk.find(a.desk).add_ask(" ".join(a.text), by=os.environ.get("HANSA_ROLE", "cli"),
                                  session=notify.session_here(), why=a.why, holds=a.holds)
    print(f"ask #{n} is on the principal's to-do list; they answer in the conversation")


def cmd_changelog(a):
    from .changelog import lines
    desk = Desk.find(a.desk)
    try:
        for line in lines(desk, a.lane, a.target):
            print(desk.redact(line))
    except (RuntimeError, OSError) as e:
        raise SystemExit(f"hansa: cannot collect changelog: {e}")


def cmd_release(a):
    desk = Desk.find(a.desk)
    if a.stop is not None:
        _stop_release(desk, a)
        return
    if not a.lane:
        print(desk.redact(release.list_text(desk)))
        return
    if os.environ.get("HANSA_TASK"):  # a worker or verification: releases are the coordinator's
        raise SystemExit("hansa: only the coordinator asks for a release; a worker or verification "
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
                    session=notify.session_here(), target=target, direct=a.direct)
    who = release.what(r)
    head = (f"release #{r['id']}: lane {a.lane}'s {r['base']} at {r['commit'][:10]}"
            + (f" by target {r['target']}" if r.get("target") else "") + ", "
            + (f"{r['count']} commit(s) since release #{r['since_release']}" if r.get("since_release") else
               f"{r['count']} commit(s) on no remote-tracking branch (no release of it went out before)")
            + f", {r['files_count']} file(s) changed; {release.checked_text(r)}; command: {r['command']} "
            f"({release.runs_text(r)})")
    head += "".join("\n" + x for x in release.outside.lines(r))
    head += "".join("\n" + x for x in release.direct_lines(r))
    if r["state"] == "forbidden":
        mailbox = release.mailbox_refusal(r["hits"])
        if mailbox:
            raise SystemExit(desk.redact(
                f"hansa: {head}\nrefused: {mailbox}\nNothing waits for the principal and nothing runs"))
        refusal = ("refused" if any(h.get("kind") == "pattern_limit" for h in r["hits"]) else
                   "refused: what goes out holds words the principal keeps from going out (the web page, Settings)")
        raise SystemExit(desk.redact(
            f"hansa: {head}\n{refusal}, so nothing waits for the principal and nothing runs:\n"
            f"{release.hits_text(r['hits'])}\n"
            f"Take them out of the history of {r['base']} (hansa add a task for it, if it is in the code; a word in "
            f"a commit message or a file name stays in the history) or out of the release command, or ask the "
            f"principal with hansa ask. {see_more('forbidden-words')}"))
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
    if a.card is not None and a.action != "on":
        raise SystemExit("hansa: --card and --no-card require hansa notify on")
    desk = Desk.find(a.desk)
    if a.action == "on":  # and how hansa up starts this session again after a restart
        print("\n".join(x for x in (notify.turn_on(desk), up.notify_on_line(desk, a.card)) if x))
        return
    if a.action != "off":
        print(notify.list_text(desk))
        return
    said = notify.turn_off(desk)
    up.notify_off(desk, notify.session_here())  # its folder's files are open to runs that start from now on
    print("\n".join([said, *[f"hansa removed its copy of the card, {p}: sessions started in its "
                                              "folder no longer read it." for p in up.remove_copies(desk)]]))


def cmd_notify_launch(a):
    """hansa up's start of a session hansa notify on recorded: the follower for this process, which becomes the
    session as it execs claude (as hansa coordinator does), the start recorded again, then claude with the inbox
    the follower posts to, so the session hears from hansa without hansa notify on."""
    desk = Desk.find(a.desk)
    if not a.rest:
        raise SystemExit("hansa: notify-launch starts claude's command given after it; none was given")
    # the deny rules the desk's settings and hansa coordinator give the coordinator's own session (launch_rules),
    # in the command's own --disallowedTools list when it has one. Checked as hansa coordinator checks its own
    home = os.environ.get("HOME") or os.path.expanduser("~")
    rules = launch_rules(desk, home)
    unsplit = rules_problem(rules) or desk_paths_problem(desk, [home, *run_homes()])
    if unsplit:  # a deny rule Claude Code would split, or one under a path it reads as a pattern, closes nothing
        raise SystemExit(f"hansa: the coordinator's session was not started: {unsplit}")
    unrecorded = up.record_session(desk, {"pid": os.getpid(), "start": proc_start(os.getpid())}, a.rest, os.getcwd(),
                                   rules)
    if unrecorded:
        print(f"hansa: hansa up will not start this session again after a restart: {unrecorded}; nothing was "
              "recorded", file=sys.stderr)
    argv = [a.rest[0], *_notified(desk, a.rest), *up.with_rules(a.rest[1:], rules)]
    try:
        os.execvpe(argv[0], argv, os.environ)
    except OSError as e:
        raise SystemExit(f"hansa: {argv[0]} did not start: {e}")


def cmd_notify_follow(a):
    raise SystemExit(notify.follow(Desk.find(a.desk), a.key, {"pid": a.pid, "start": a.start}, a.socket, a.nonce,
                                   a.name))


def cmd_notify_feed(a):
    notify.cmd_feed(Desk.find(a.desk))


def cmd_explain(a):
    if a.what == "task":
        print(f"hansa explain task {a.name or 'ID'} is now hansa show {a.name or 'ID'}; "
              "this form goes in the next release")
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
    raise SystemExit("hansa: --since takes HH:MM (today's, or yesterday's when still ahead) or YYYY-MM-DD HH:MM")


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


def cmd_ping(a):
    desk = Desk.find(a.desk)
    names = [a.provider] if a.provider else sorted(desk.settings()["providers"])
    # the program the recorded hansa serve start runs its workers through, which need not be this shell's
    served, mine = up.recorded_program(desk), program("claude")
    if served and served != mine:
        print(f"pinging with {served}, the program the recorded hansa serve start uses; "
              f"this shell's is {mine or 'none'}")
    for n in names:
        r = check_provider(desk, n, claude=served or "")
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
    moved = desk_paths_problem(desk, run_homes(), lane_repos(desk.settings()))
    if moved:  # every run it would start would be refused: say it once, here
        raise SystemExit(f"hansa: hansa serve did not start, as no run may start: {moved}")
    held = hold_server_lock(desk)  # kept open while this server runs
    if not held:
        raise SystemExit(f"hansa: another hansa serve is already running on {desk.root}; one per desk, so no task is "
                         f"started twice. Stop that one first if you want to restart it")
    record_serve_code(desk)  # which code this server runs, for hansa status to tell from what is installed later
    record_serve_locale(desk)  # the locale its lanes' commands get, for hansa explain lane to say
    dropped = record_serve_session(desk)  # a Claude Code session's identity, which nothing it starts gets
    up.record_start(desk, a)  # how hansa up brings this server back, and this account's list of desks
    alert.sweep_runs(desk)  # the folders of alert runs a stopped or killed server left (none runs now: one server)
    try:  # a desk whose coordinator only runs hansa notify on gets hansa's card (and its copy) only here
        bring_card(desk)
    except OSError as e:
        print(f"hansa serve: the desk's card could not be brought up to date ({e.strerror or e})", file=sys.stderr,
              flush=True)
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
    write_text(desk.state_dir / "web-port", str(port))  # what hansa web opens
    write_text(desk.state_dir / "web-host", a.host)  # with it, where hansa status says the page is
    beat(desk)  # its rounds count from now, not from the last server's
    pulse(desk)
    alert.start_thread(desk)  # the principal's alert command, run from here only (hansa/alert.py)
    d = Dispatcher(desk)
    desk.event("server_started", port=port)
    print(f"hansa serve: desk {desk.root}; web page on {a.host}:{port} (run `hansa web` for the address)", flush=True)
    if dropped:
        print(f"hansa serve: {serve_session_text(dropped)}", file=sys.stderr, flush=True)
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
                try:
                    desk.event("server_error", error=str(e)[:300])
                except OSError:  # a full disk may refuse the error record too
                    pass
                print(f"hansa serve: {e}", file=sys.stderr, flush=True)
            time.sleep(a.every)
    except KeyboardInterrupt:
        desk.event("server_stopped")
    finally:
        alert.sweep_runs(desk)


def cmd_up(a):
    desk = Desk.find(a.desk, optional=True)
    if desk is None:
        ok = up.all_desks_up()
    else:
        if not (desk.settings_path.is_file() and desk.tasks_dir.is_dir()):
            raise SystemExit(f"hansa: {desk.root} is not a desk; nothing started")
        ok = up.desk_up(desk)
    if not ok:
        raise SystemExit(1)


def web_port(desk: Desk) -> int:
    """The port this desk's hansa serve last served the page on: another desk's server may hold the default."""
    return views.web_address(desk)[1]


def cmd_web(a):
    if os.environ.get("HANSA_ROLE") or os.environ.get("HANSA_TASK"):
        raise SystemExit("hansa: the web page is for the principal; agents use the other commands")
    desk = Desk.find(a.desk)
    if session_vars():  # an ordinary Claude Code session, or the shell of its ! prefix: the address would be in its context
        port, me = a.port or web_port(desk), Path(__file__).resolve().parent.parent / "bin" / "hansa"
        raise SystemExit(f"hansa: hansa web prints the page's address with its token, which approves releases, and it "
                         f"runs inside a Claude Code session here ({', '.join(session_vars())} set), whose context would "
                         f"keep it; nothing was printed. The principal opens the page from a terminal of their own, "
                         f"not one Claude Code started: from their computer, ssh -L {port}:127.0.0.1:{port} HOST "
                         f"{shlex.quote(str(me))} --desk {shlex.quote(str(desk.root))} web; on this machine, hansa web. "
                         + see_more("session-identity"))
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
    env = {k: v for k, v in without_session(os.environ).items() if not k.startswith("ANTHROPIC_")}
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
    # What the principal keeps workers from reading: Read is denied there to this session too.
    # Checked again as whoever starts the session, as those are
    kept, unkept = no_read_paths(desk, plan, theirs)
    for line in unkept:
        print(f"hansa: {line}; skipped (the principal changes the list on the page, Settings)", file=sys.stderr)
    # and the built-in logins and keys under the home folder of whoever runs the session. Not the Claude Code
    # folders, as for workers: its own memory lives in one (own_folder_rules closes the login in it)
    home = env.get("HOME") or os.path.expanduser("~")
    no_read = no_read_rules([*kept, *(os.path.join(home, f) for f in home_secrets())])
    dirs = repos
    no_edit = [f"Edit(//{str(d).lstrip('/')}/**)" for d in [*repos, desk.worktrees_dir]]
    no_builtin = [f"Agent({n})" for n in BUILTIN_HELPERS]
    claude = claude_bin()
    if "DISABLE_AUTOUPDATER" not in env and not can_update(claude):  # it would fail and say so at every start
        env["DISABLE_AUTOUPDATER"] = "1"
    argv = [claude] + (["--add-dir", *dirs] if dirs else []) + ["--disallowedTools", *no_edit, *no_builtin, *no_read]
    unsplit = argv_rules_problem(argv)
    if unsplit:  # a deny rule Claude Code would split leaves its folder open: the session never starts with one
        raise SystemExit(f"hansa: the coordinator's session was not started: {unsplit}")
    moved = desk_paths_problem(desk, [home, *run_homes(env)]) or next(
        (w for w in (pattern_path_problem(r, "a lane's repository", "git's data in it") for r in repos) if w), "")
    if moved:  # a deny rule under it would close another folder and leave its own open
        raise SystemExit(f"hansa: the coordinator's session was not started: {moved}")
    argv += ["--model", model]
    if effort:
        argv += ["--effort", effort]
    # when the session compacts while working: the account's plan may be the account's to write, so only a number in
    # range counts. The mod reads the other, idle_at, in the desk's settings file at each decision
    given = plan.get("compact") if isinstance(plan.get("compact"), dict) else {}
    working_at = compact_settings({"coordinator_compact": given})["working_at"]
    # preparing the mod never stops the start: a step that fails leaves it off
    mod = plugin.plan(desk, claude)
    argv += ["--autocompact", str(working_at)] if mod["autocompact"] else []
    if mod["on"]:  # for this start only: hansa names the mod in no settings file
        argv += ["--plugin-dir", mod["copy"]]
        env["HANSA_MOD_SETTINGS"] = str(desk.settings_path)
        if clear:  # the mod clears the session in this window; without the mod nothing does
            env["HANSA_MOD_CLEAR_DAILY"] = clear
    for line in [plugin.left_text(mod)] + plugin.start_lines(mod, clear):
        if line:
            print(f"hansa: {line}", file=sys.stderr)
    # after a /clear (the mod's daily one, or anyone's) the fresh context starts from the desk's state, and the desk's
    # owner records the clear (plugin.record_clear); root on the account's desk records none. The hook is hansa's own
    # code, never the desk's files
    hooks = {"SessionStart": [{"matcher": "clear", "hooks": [{"type": "command", "command": (
        f"{shlex.quote(str(Path(__file__).resolve().parent.parent / 'bin' / 'hansa'))} --desk "
        f"{shlex.quote(str(desk.root))} {'status' if theirs else 'coordinator-cleared'}")}]}]}
    own = own_folder_rules(env)  # the folder Claude Code runs from for this session: root's, when root runs it
    if theirs:  # the desk's .claude/settings.json is the account's, and its hooks would run as root: root's own
        rules = permissions(desk)
        rules["deny"] += own
        argv += ["--setting-sources", "user", "--settings", json.dumps({"permissions": rules, "hooks": hooks, **QUIET})]
    else:
        argv += ["--settings", json.dumps({"permissions": {"deny": own}, "hooks": hooks, **QUIET})]
    argv += _notified(desk, rest)
    # what hansa did at this start, kept in the desk by its owner (root's, by the account's hansa) for status and the
    # page, with the --autocompact options of the whole command line, the caller's too, as given, and the window
    rec = plugin.start_record(mod, argv + rest, plan.get("started"), clear)
    unrecorded = _record_roots_mod(theirs, desk, rec) if theirs else plugin.record(desk, rec)
    if unrecorded:
        print(f"hansa: {unrecorded}", file=sys.stderr)
    # how hansa up starts this session again; root on the account's desk writes nothing there, and hansa up runs
    # as the account
    unrecorded = "" if theirs else up.record_coordinator(desk, clear, rest)
    if unrecorded:
        print(f"hansa: hansa up will not start this session again after a restart: {unrecorded}; nothing was "
              "recorded", file=sys.stderr)
    os.chdir(desk.root)
    os.execvpe(argv[0], argv + rest, env)


def no_read_paths(desk: Desk, plan: dict, theirs: str = "") -> tuple:
    """What the plan says the principal keeps workers from reading, as the session may be denied it, and what was
    said of the entries left out: what the desk's owner found (coordinator_desk), then, as root on the account's
    desk, the same checks again."""
    given = plan.get("no_read") if isinstance(plan.get("no_read"), list) else []
    refused = plan.get("no_read_refused") if isinstance(plan.get("no_read_refused"), list) else []
    said = [x for x in refused if isinstance(x, str)]
    if not theirs:  # the desk is this user's: coordinator_desk made the plan in this process, with these checks
        return [x for x in given if isinstance(x, str)], said
    # root does not read the account's settings: the account's hansa looked at the lanes' repositories
    good, more, stops = deny_parts(given, desk.root, None)
    if stops:  # as coordinator_desk: a path the plan gives that cannot be written as a rule stops the session
        raise SystemExit(f"hansa: {stop_text(stops)}")
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
    out, clear, i = [], None, 0
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
    if clear is not None:  # given, even empty: a window, or refused
        clear = clear.strip()  # " 04-09" is 04-09
        try:
            plugin.parse_window(clear)
        except ValueError as e:
            raise SystemExit(f"hansa: {e}")
    return clear or "", out


def _notified(desk: Desk, rest: list) -> list:
    """Start the follower that posts to the session this process becomes (its pid stays when it execs Claude
    Code), at an inbox path it knows: claude's options for that path. Nothing when it cannot; the session can
    try again with hansa notify on."""
    given = next((rest[i + 1] if x == "--messaging-socket-path" and i + 1 < len(rest) else x.partition("=")[2]
                  for i, x in enumerate(rest) if x == "--messaging-socket-path"
                  or x.startswith("--messaging-socket-path=")), "")
    try:
        sock = given or notify.socket_path(os.getpid())
        # no name of hansa's own: the session is named by its pid until Claude Code gives it one (notify.who)
        notify.start(desk, {"pid": os.getpid(), "start": notify.proc_start(os.getpid())}, sock, wait=False)
    except (OSError, RuntimeError) as e:
        print(f"hansa: this session will not hear from hansa by itself ({e}); run hansa wait in the background, "
              f"or hansa notify on from the session", file=sys.stderr)
        return []
    return [] if given else ["--messaging-socket-path", sock]


def coordinator_desk(desk: Desk, by: int) -> dict:
    """The coordinator's part in the desk, done as the desk's owner: the card, the helpers and the
    permissions brought up to date, who runs the coordinator recorded. Returns
    what starting it needs from the desk: the lanes' checkouts, model, effort and the provider's environment."""
    s = desk.settings()
    co = s["coordinator"]
    p = s["providers"][co["provider"]]
    bring_card(desk, starting=True)
    for f in sorted(HELPERS.glob("*.md")):
        _bring(desk, f.read_text(encoding="utf-8"), desk.root / ".claude" / "agents" / f.name, "helper_refreshed")
    mine = desk.root / ".claude" / "settings.json"  # hansa keeps its rules current there; the rest is left as it is
    cs = read_json(mine, {})
    cs = {**cs, "permissions": permissions(desk)} if isinstance(cs, dict) else {"permissions": permissions(desk)}
    _bring(desk, json_text(cs), mine, "permissions_refreshed", keep_as="coordinator-settings.json")
    if not desk.notes_dir.exists() and not desk.notes_dir.is_symlink():  # a desk made before notes/
        desk.notes_dir.mkdir()
    desk.state_dir.mkdir(exist_ok=True)  # who runs the coordinator: hansa cost says when it cannot read its records
    plugin.forget(desk)  # the earlier start's record of the mod, before this start prepares it
    started = now()  # and when: this start's record of the mod is tied to it (plugin.status)
    write_json(desk.state_dir / "coordinator.json", {"uid": by, "user": account.name_of(by), "at": started})
    kept, unkept, stops = no_read_parts(s, desk.root)
    if stops:  # Read would be open there to the session: it does not start
        raise SystemExit(f"hansa: {stop_text(stops)}")
    for line in unkept:
        desk.event("no_read_skipped", why=line)
    return {"repos": sorted({ln["repo"] for ln in s["lanes"].values()}), "model": co.get("model") or p["model"],
            "effort": co.get("effort") or "", "env": provider_env(desk, co["provider"], p),
            "no_read": kept, "no_read_refused": unkept,
            "compact": {"working_at": compact_settings(s)["working_at"]}, "started": started}


def _record_roots_mod(theirs: str, desk: Desk, rec: dict) -> str:
    """Root's start of the coordinator, recorded in the desk by the account's hansa; '' when it was, else what the
    start says. Never raises."""
    try:
        r = account.run(theirs, ["coordinator-desk", "--by", str(account.uid()), "--mod", json.dumps(rec)], desk.root,
                        capture_output=True, text=True)
        said = json.loads(r.stdout).get("mod_unrecorded") if not r.returncode else (
            r.stderr.strip() or f"exit {r.returncode}")
    except (Exception, SystemExit) as e:  # noqa: BLE001 - SystemExit: no runuser
        said = f"{type(e).__name__}: {e}"
    return plugin.unrecorded_text("the desk", said) if said else ""


def cmd_coordinator_cleared(a):
    """The coordinator's SessionStart hook for /clear, as the desk's owner: record the clear, then print hansa status
    into the fresh context, and one line more only when the record could not be written."""
    desk = Desk.find(a.desk)
    unrecorded = plugin.record_clear(desk)
    print(views.status_text(views.status(desk)))
    if unrecorded:
        print(f"hansa: {unrecorded}")


def cmd_coordinator_desk(a):
    desk = Desk.find(a.desk)
    if not a.mod:
        print(json.dumps(coordinator_desk(desk, a.by)))
        return
    try:  # root's start, recorded by the account
        plugin.record_start(desk, json.loads(a.mod), root=True)
        print(json.dumps({}))
    except Exception as e:  # noqa: BLE001 - root starts all the same
        print(json.dumps({"mod_unrecorded": f"{type(e).__name__}: {e}"}))


def bring_card(desk: Desk, starting: bool = False) -> None:
    """The desk's card brought up to hansa's, and the copy hansa notify on wrote for a session outside the desk
    with it (up.refresh_copy; starting: hansa coordinator's start)."""
    _bring(desk, CARD.read_text(encoding="utf-8"), desk.root / "CLAUDE.md", "card_refreshed")
    up.refresh_copy(desk, starting=starting)


def _bring(desk: Desk, new: str, dst: Path, event: str, keep_as: str = "") -> None:
    """Put hansa's current version of a file into the desk (hansa was upgraded), keeping the old one in .hansa/."""
    if dst.is_file() and dst.read_text(encoding="utf-8") == new:
        return
    if dst.is_file():
        desk.state_dir.mkdir(exist_ok=True)
        write_text(desk.state_dir / f"{keep_as or dst.name}.{int(time.time())}", dst.read_text(encoding="utf-8"))
    dst.parent.mkdir(parents=True, exist_ok=True)
    write_text(dst, new)
    desk.event(event, file=dst.name)


LANE_LISTS = ("providers", "allow", "network", "write", "release_files", "verify_for")
LANE_NUMBERS = ("max_turns", "timeout_min", "merge_timeout_min", "merge_batch")
LANE_FIELDS = ("base", "providers", "allow", "network", "write", "env", "check", "merge_check", "setup",
               "release_files", "max_turns", "timeout_min", "merge_timeout_min", "merge_batch", "merge_lane", "verify_for",
               *MESSAGE_FIELDS, "identity")
# empty (none) takes them out: the check's command and limit again, hansa's own message forms,
# the identity of hansa's commits (then the repository's own, or hansa's)
LANE_UNSET = ("merge_check", "merge_timeout_min", "merge_batch", "merge_lane", "verify_for", *MESSAGE_FIELDS, "identity")
OUTSIDE_WORKER = ("setup", "check", "merge_check")  # run by hansa itself, not by a worker


def _shown(v) -> str:
    return json.dumps(v, ensure_ascii=False)


# a problem _save prints, by the lane field it starts with (after "lane L: "), and the topic that field's is in
SAVE_TOPICS = (("write", "write-env"), ("env", "write-env"), ("allow", "web-tools"), ("network", "network-list"),
               ("release_files", "release-files"), ("release", "release-flow"), ("targets", "release-flow"),
               ("has a release command", "release-flow"), ("the release command", "release-flow"))


def _save_topic(problem: str) -> str:
    """The topic for one problem _save prints; '' for a field no topic covers."""
    rest = re.sub(r"^lanes?[ .][^:]*: ", "", problem)
    return next((topic for start, topic in SAVE_TOPICS if rest.startswith(start)), "")


def _save(desk: Desk, new: dict) -> None:
    """Save the coordinator's change, checked as the page's are; on problems print them, with a pointer for each
    field they are about, and change nothing."""
    problems = desk.save_settings(new, by="coordinator")
    if problems:
        topics = dict.fromkeys(filter(None, map(_save_topic, problems)))
        raise SystemExit("hansa: nothing was changed:\n" + "\n".join(f"  {x}" for x in problems)
                         + "".join(f"\n{see_more(x)}" for x in topics))


def _lane_value(field: str, values: list):
    if values == ["none"]:  # the help's word for empty: what no value gives
        values = []
    elif "none" in [x for v in values for x in (v.split(",") if field == "providers" else [v])]:
        raise SystemExit(f"hansa: none means an empty {field}, so it cannot come with other values "
                         f"({' '.join(values)}): give none alone, or the values without it. Nothing was changed")
    if field == "allow":  # a rule given twice is kept once
        return list(dict.fromkeys(x for x in values if x))
    if field == "verify_for" and not values:
        return None
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
    if field == "merge_lane":
        if values not in (["true"], ["false"]):
            raise SystemExit("hansa: merge_lane takes true or false (none: unset, false). Nothing was changed")
        return values == ["true"]
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


def _identity_differs(name: str, lanes: dict) -> str:
    """The line for a lane whose commits carry another identity, as hansa explain lane shows it, than another lane's
    on its repository; '' when they match. Merge lanes are left out: a merge check commits as the task's lane."""
    ln = lanes[name]
    if ln.get("merge_lane"):
        return ""
    repo = str(Path(ln["repo"]).resolve())
    who, email, where = lane_identity(ln)
    mine, theirs = f"{who} <{email}>", {}
    for n, x in sorted(lanes.items()):
        if n != name and not x.get("merge_lane") and x.get("repo") and str(Path(x["repo"]).resolve()) == repo:
            w, e, _ = lane_identity(x)
            if f"{w} <{e}>" != mine:
                theirs.setdefault(f"{w} <{e}>", []).append(n)
    if not theirs:
        return ""
    local = (", from the repository's own config (git config --local user.name and user.email), not from the lane"
             if where.startswith("the repository's own config") else "")
    return (f"Lane {name} commits as {mine}{local}, unlike the other lanes on {repo}: "
            + "; ".join(f"{', '.join(ns)} {'commits' if len(ns) == 1 else 'commit'} as {v}" for v, ns in theirs.items())
            + f". Set it with: hansa lane set {name} identity \"Name <email>\"")


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
                 + "".join(f"  {f} {_shown(ln[f])}" for f in MESSAGE_FIELDS if ln.get(f))
                 + views.merge_lane_text(desk.settings(), name) + views.verify_lane_text(desk.settings(), name))
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
                ("merge_lane", a.merge_lane),
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
                                     f"changed. {see_more('web-tools')}")
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
                why = path_rule_problem(x) or pattern_path_problem(x, f"lane {a.name}'s repository", "git's data in it")
                if why:
                    raise SystemExit(f"hansa: {why}; nothing was changed")
            if not a.like:
                base = lane_base(repo, a.base, "nothing was changed")
                ln = default_lane(repo, base, given.pop("providers", None) or None)
                ln.update({k: v for k, v in given.items() if v or isinstance(v, int)})
            elif a.base:
                lane_base(repo, a.base, "nothing was changed")
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
                    write_text(aside, old)
                write_text(brief, text)
            elif not brief.exists():
                write_text(brief, "")
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
                print(f"The lane's allowed commands, which its workers and verifiers are given, are "
                      f"recorded: {_shown(ln['allow'])}. Tell the principal, in the conversation, what you listed "
                      f"and why.")
            if ln.get("setup") or ln.get("check") or ln.get("merge_check"):
                print("The lane's setup and check run outside the worker, so they are recorded. Tell the principal, "
                      "in the conversation, what they run and why.")
            if not ln.get("check") and not ln.get("merge_check"):
                check_name = merge_lane(new, a.name)
                if ln.get("merge_lane"):
                    print(f"Lane {a.name} has no check to run before merges; set one with: "
                          f"hansa lane set {a.name} merge_check \"<the command that runs the tests>\"")
                elif check_name != a.name:
                    print(f"Lane {a.name} has no check after each task; merge checks use lane {check_name}")
                else:
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
            identity = _identity_differs(a.name, new["lanes"])
            if identity:
                print(identity)
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
            write_text(brief, text)
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
    identity = _identity_differs(a.name, new["lanes"]) if a.field == "identity" else ""
    if identity:
        print(identity)
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
        print(f"The lane's allowed commands, which its workers and verifiers are given, changed: "
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


def _lane_worktrees(desk: Desk, name: str) -> list:
    """The lane's worktree, when it is still on disk."""
    wt = desk.worktrees_dir / name
    return [wt] if os.path.lexists(wt) else []


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
            + (f"  after merges: {t['after_merge_min']} min" if t.get("after_merge_min") else "")
            + f"  ({release.runs_text(t)})")


def _lane_target(desk: Desk, a, new: dict) -> None:
    """List, set or remove a lane's named release targets, including their order and after-merge setting."""
    ln = new["lanes"][a.name]
    if not a.target:
        shown = lane_targets(ln)
        for name, t in sorted(shown.items()):
            print(_target_text(name, t, ln))
            if t.get("after_merge_min"):
                print("  " + release.after_merge_text(new, a.name, name))
            for line in release.outside.lines(release.outside.inspect(t.get("command") or "", ln["repo"],
                                                                     fingerprint=False)):
                print("  " + line)
        if not shown:
            print(f"lane {a.name} has no release target: hansa lane target {a.name} NAME \"<command>\" names one")
        return
    if len(a.command) > 1:
        raise SystemExit("hansa: a target takes one command line; quote it if it has spaces. Nothing was changed. " + see_more("release-flow"))
    cmd = a.command[0] if a.command else None
    named = ln.get("targets") if isinstance(ln.get("targets"), dict) else {}
    old = named.get(a.target)
    if cmd == "none":  # the help's word for taking it out
        if a.minutes is not None or a.after is not None or a.fixed is not None or a.after_merge is not None:
            raise SystemExit("hansa: none takes the target out, so it cannot come with --minutes, --after, --fixed "
                             "or --after-merge. "
                             "Nothing was changed. " + see_more("release-flow"))
        if old is None:
            raise SystemExit(f"hansa: lane {a.name} has no release target {a.target}; its targets: "
                             f"{', '.join(sorted(named)) or 'none'}. Nothing was changed. " + see_more("release-flow"))
        followers = sorted(n for n, x in named.items() if isinstance(x, dict) and x.get("after") == a.target)
        if followers:
            raise SystemExit(f"hansa: target {', '.join(followers)} of lane {a.name} goes out only after "
                             f"{a.target}; change that first (hansa lane target {a.name} {followers[0]} --after none). "
                             "Nothing was changed. " + see_more("release-flow"))
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
                         f"hansa lane target {a.name} {a.target} \"<command>\". Nothing was changed. " + see_more("release-flow"))
    t = dict(old or {})
    if cmd is not None:
        if not cmd.strip():
            raise SystemExit("hansa: a target's command cannot be empty (none takes the target out). Nothing was changed. " + see_more("release-flow"))
        t["command"] = cmd
    if a.minutes is not None:
        if a.minutes == "none":
            t.pop("timeout_min", None)
        else:
            try:
                t["timeout_min"] = int(a.minutes)
            except ValueError:
                raise SystemExit("hansa: --minutes takes one whole number from 1 to 1440, or none (the lane's "
                                 "timeout_min). Nothing was changed. " + see_more("release-flow"))
    if a.after is not None:
        if a.after == "none":
            t.pop("after", None)
        else:
            t["after"] = a.after
    if a.after_merge is not None:
        if a.after_merge == "none":
            t.pop("after_merge_min", None)
        else:
            try:
                t["after_merge_min"] = int(a.after_merge)
            except ValueError:
                raise SystemExit("hansa: --after-merge takes one whole number from 1 to 1440, or none. "
                                 "Nothing was changed. " + see_more("release-flow"))
    if a.fixed is not None:
        if a.fixed == "none":
            t.pop("fixed", None)
        elif a.fixed == "yes":
            t["fixed"] = True
        else:
            raise SystemExit("hansa: --fixed takes no value (runs from a fixed checkout), or none (runs in the "
                             "repository again). Nothing was changed. " + see_more("release-flow"))
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
    if t.get("after_merge_min"):
        print(release.after_merge_text(new, a.name, a.target))
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
    """Say forbidden words and outside paths at once when a release target is set."""
    hits = release.command_hits(ln, release.lane_words(desk.settings(), name), only)
    if hits:
        print(release.command_hits_text(desk, name, hits))
    for target, t in lane_targets(ln).items():
        if only is not None and only != target:
            continue
        found = release.outside.inspect(t.get("command") or "", ln["repo"])
        if not found["outside"] and not found["outside_folders"]:
            continue
        print(desk.redact(f"Warning: lane {name}'s release target {target} names paths outside the repository:\n"
                         + "\n".join("  " + x for x in release.outside.lines(found))
                         + "\nNo worker can change them, and no verification covers them. A change to a "
                         "fingerprinted outside file voids a waiting release and unticks the target on the "
                         "principal's list of releases without asking. Folders have no fingerprint; files recorded "
                         "without a fingerprint do not void or untick anything. Better: move the files into "
                         "the repository and list them in release_files. " + see_more("release-files")))


def _lane_remove(desk: Desk, a, s: dict, new: dict) -> None:
    """Remove a lane and its worktrees, or the worktrees an earlier lane remove left of a lane that is gone.
    Work a worktree holds that no branch keeps is not lost: without --keep or --discard nothing is removed."""
    lanes, name = s["lanes"], a.name
    known = name in lanes
    wts = _lane_worktrees(desk, name)
    if not known and not wts:
        raise SystemExit(f"hansa: there is no lane {name}, and no worktree of one; lanes: "
                         f"{', '.join(sorted(lanes)) or 'none'}")
    if a.keep and a.discard:
        raise SystemExit("hansa: --keep puts what the worktree holds on a new branch and --discard drops it; choose "
                         "one. Nothing was changed")
    what = f"lane {name}" if known else f"lane {name}, which is gone,"
    tasks = [m for m in desk.tasks() if m.get("lane") == name or (lane_occupied(m) and occupied_lane(m) == name)]
    busy = [m["id"] for m in tasks if m["state"] in ("queued", "running", "merging")]
    if busy:
        raise SystemExit(f"hansa: {what} still has {', '.join(busy)} queued or running; nothing was "
                         f"changed. Wait for them (hansa wait) or cancel them first")
    # merging a finished task needs its lane's repo and base
    review = [m["id"] for m in tasks if m["state"] == "done" and m.get("kind") != "verify"]
    if review:
        raise SystemExit(f"hansa: {what} still has {', '.join(review)} finished and waiting for review; "
                         f"nothing was changed. Merging needs the lane: clear them first with hansa merge "
                         f"or hansa close")
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
    if a.keep:
        from .run import SandboxPathChanged, stage_work, wgit
        for wt, st in held.items():
            try:
                stage_work(desk, wt, wgit(desk, wt, "rev-parse", "HEAD"))
            except SandboxPathChanged as e:
                raise SystemExit(f"hansa: lane {name} was not removed: {e}") from None
    if known:
        del new["lanes"][name]
        verifiers = []
        for n, lane in new["lanes"].items():
            if name in lane.get("verify_for", []):
                lane["verify_for"] = [x for x in lane["verify_for"] if x != name]
                if not lane["verify_for"]:
                    del lane["verify_for"]
                verifiers.append(n)
        _save(desk, new)
        print(f"lane {name} removed; its standing instructions stay in {desk.lanes_dir / name}")
        for n in verifiers:
            print(f"lane {name} removed from lane {n}'s verify_for")
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


def cmd_hide(a):
    if not a.paths:
        raise SystemExit("hansa: hansa hide PATH [PATH ...]: name at least one full path to keep workers from reading "
                         "(such as /opt/myproject/keys); nothing was changed")
    desk = Desk.find(a.desk)
    added, there, refused, warnings = desk.hide(a.paths)
    for x in added:
        print(f"added to what workers may not read: {x}")
    for x, same in there:
        print(f"already there: {x}" + (f" (as {same})" if same != x else ""))
    for x, why in refused:
        print(f"refused: {x if x.isprintable() else json.dumps(x)}")
        print("".join(f"  {w}\n" for w in why), end="")
    if added:
        print("Runs that start from now on are kept from it; a run already going keeps the sandbox it started with. "
              "Read is denied there to the coordinator's session from its next start only when hansa coordinator "
              "starts it: not to a session hansa notify on recorded, even one hansa up started. Only the principal "
              "removes an entry, on the web page (Settings).")
    for x in warnings:  # as the page shows them on saving: the lanes' to fix, not the list's
        print(f"warning: {x}")
    if refused:
        raise SystemExit(1)


def cmd_disk_floor(a):
    from . import disk
    desk = Desk.find(a.desk)
    if a.gb is None:
        free = disk.desk_free(desk)[0]
        print(f"disk floor: {disk.floor_text(disk.floor(desk.settings()))}; "
              f"{disk.gb(free) if free is not None else '? GB'} free on the desk's volume")
        return
    if not 0 <= a.gb <= disk.MOST_FLOOR:
        raise SystemExit(f"hansa: the floor is a number of GB from 0 (off) to {disk.MOST_FLOOR}; nothing was changed")
    gb = int(a.gb) if a.gb == int(a.gb) else a.gb
    with desk.lock():
        new = desk.settings()
        old = disk.floor(new)
        new["disk_floor"] = gb
        _save(desk, new)
    print(f"disk floor: {disk.floor_text(old)} -> {disk.floor_text(gb)}"
          + (": hansa serve starts things however little space is free" if not gb else
             f": hansa serve starts nothing new while a volume it writes on has less free, and starts again once each "
             f"has {gb + disk.MARGIN:g} GB; a volume smaller than {disk.SMALL * gb:g} GB has a quarter of its size as "
             f"its floor instead, and a margin scaled with it (no more than a quarter of that floor)"))


def cmd_memory_floor(a):
    from . import disk, memory
    desk = Desk.find(a.desk)
    if a.gb is None:
        reading = memory.current()
        print(f"memory floor: {disk.floor_text(memory.floor(desk.settings()))}; "
              f"{disk.gb(reading['available']) if 'available' in reading else '? GB'} available")
        return
    if not 0 <= a.gb <= disk.MOST_FLOOR:
        raise SystemExit(f"hansa: the floor is a number of GB from 0 (off) to {disk.MOST_FLOOR}; nothing was changed")
    gb = int(a.gb) if a.gb == int(a.gb) else a.gb
    with desk.lock():
        new = desk.settings()
        old = memory.floor(new)
        new["memory_floor"] = gb
        _save(desk, new)
    print(f"memory floor: {disk.floor_text(old)} -> {disk.floor_text(gb)}"
          + (": hansa serve starts things however little memory is available" if not gb else
             f": hansa serve starts nothing new while less memory is available, and starts again at the floor "
             f"plus {memory.MARGIN} GB; a machine with less than {disk.SMALL * gb:g} GB uses a quarter of its memory "
             "as its floor instead"))


def cmd_gaps(a):
    desk = Desk.find(a.desk)
    if a.words and a.words[0] == "mark" and len(a.words) in (3, 4) and not a.all:
        _, nid, kind, *why = a.words
        gaps.mark(desk, nid, kind, why[0] if why else "", os.environ.get("HANSA_ROLE") or getpass.getuser(),
                  notify.session_here())
        print(gaps.show(desk, nid))
    elif len(a.words) == 2 and a.words[0] == "show" and not a.all:
        print(gaps.show(desk, a.words[1]))
    elif not a.words:
        print(gaps.list_text(desk, a.all))
    else:
        raise SystemExit('hansa: use hansa gaps [--all], hansa gaps show ID, or hansa gaps mark ID '
                         'fixed|later|wontfix "why"; nothing was changed')


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


def see_more(*topics) -> str:
    """The pointer a message or a command's help ends with: More: hansa help TOPIC, ..."""
    assert all(x in TOPICS for x in topics), topics
    return "More: " + ", ".join(f"hansa help {x}" for x in topics)


def topics_text() -> str:
    """hansa help topics: one line per topic, saying what it holds."""
    wide = max(map(len, TOPICS))
    return "\n".join(["Topics (hansa help TOPIC prints one, from docs/):",
                      *(f"  {k:<{wide}}  {v[2]}" for k, v in TOPICS.items())])


def index_text(commands: list, lane_commands: list) -> str:
    """`hansa help` with no word: the commands, a few to a line, then the topics."""
    grouped = [list(names) for _, names in COMMAND_GROUPS]
    grouped[-1] += [x for x in commands if not any(x in g for g in grouped)]
    shown = {"lane": f"lane ({', '.join(lane_commands)})"}
    return "\n".join([
        "hansa help COMMAND: that command's help, as hansa COMMAND --help prints it. hansa help TOPIC: one topic.",
        "hansa help card: the whole card (the desk's CLAUDE.md). hansa help topics: the topics alone.",
        "",
        "Commands:",
        *(f"  {label}: {', '.join(shown.get(x, x) for x in g if x in commands)}"
          for (label, _), g in zip(COMMAND_GROUPS, grouped)),
        "",
        topics_text()])


def topic_text(name: str) -> str:
    """hansa help TOPIC: its section of docs/, from its heading down to the next heading of the same or a higher
    level, subsections with it; the whole file for a topic that names no heading."""
    file, heading, _ = TOPICS[name]
    where = (f"the section \"{heading}\" of " if heading else "") + f"docs/{file}"
    try:
        lines = (DOCS / file).read_text(encoding="utf-8").splitlines()
    except OSError:
        raise SystemExit(f"hansa: this install has no {DOCS / file}, so it cannot print {name}: read {where} in "
                         "hansa's repository")
    if heading is None:
        return "\n".join(lines).strip()
    out, level, fence = [], 0, False
    for x in lines:
        if x.startswith("```"):
            fence = not fence
        m = None if fence else re.match(r"(#+) (.*)", x)
        if m and level and len(m.group(1)) <= level:
            break
        if m and not level and m.group(2).strip() == heading:
            level = len(m.group(1))
        if level:
            out.append(x)
    if not out:
        raise SystemExit(f"hansa: {DOCS / file} has no heading \"{heading}\", so it cannot print {name}: read "
                         f"{where} in hansa's repository")
    return "\n".join(out).strip()


def cmd_help(a):
    words = a.words
    if not words:
        print(index_text(a.visible, list(a.lane_commands)))
    elif len(words) == 2 and words[0] == "lane" and words[1] in a.lane_commands:
        a.lane_commands[words[1]].print_help()  # hansa help lane add: what hansa lane add --help prints
    elif len(words) > 1:
        raise SystemExit("hansa: one command or topic at a time, such as hansa help verify or hansa help effort "
                         "(lane takes its subcommand too: hansa help lane add)")
    elif words[0] == "card":
        print(CARD.read_text(encoding="utf-8"))
    elif words[0] == "topics":
        print(topics_text())
    elif words[0] in a.visible:  # hansa help verify: what hansa verify --help prints
        a.commands[words[0]].print_help()
    elif words[0] in TOPICS:
        print(topic_text(words[0]))
    else:
        raise SystemExit(f"hansa: there is no command or topic {words[0]}; hansa help topics lists the topics, and "
                         "hansa --help the commands")


def _split_claude_args(argv: list):
    """Everything after `coordinator` (or hansa up's launcher) belongs to claude, even when it looks like an option."""
    for i, x in enumerate(argv):
        if x in ("coordinator", up.LAUNCHER):
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
                                 "`hansa help` lists the commands and the topics, `hansa help card` prints the "
                                 "card.")
    ap.add_argument("--version", action=_Version, nargs=0, help="show which hansa this is and exit")
    ap.add_argument("--desk", help="desk directory (default: the desk containing the current directory, or HANSA_DESK)")
    sub = ap.add_subparsers(dest="cmd", metavar="command")
    visible = []  # the commands `hansa help` names, in this order; not the hidden ones below

    def add(name, fn, help_):
        p = sub.add_parser(name, help=help_, description=help_)
        p.set_defaults(fn=fn)
        visible.append(name)
        return p

    p = add("init", cmd_init, "create a desk")
    p.add_argument("dir")
    p.add_argument("--repo", help="git checkout the first lane works on")
    p.add_argument("--lane", default="a")
    p.add_argument("--base", help="base branch; must resolve to an existing commit (default: the checkout's current branch)")
    p.add_argument("--user", help="as root on a Linux server: the account the desk belongs to and hansa runs as")
    p = add("add", cmd_add, "queue a task")
    p.add_argument("text", nargs="*")
    p.add_argument("--attach", action="append", default=[], metavar="PATH",
                   help="copy a file into the task when queued (repeatable; at most 20 files, 20 MB in all). "
                   "Workers get copies in their run's temporary folder; hansa show ID lists names and sizes")
    p.add_argument("--file", help="a file with the text; - reads it from standard input. It is read when the task is "
                                  "queued: changing the file afterwards changes nothing (hansa show ID gives the text "
                                  "hansa read)")
    p.add_argument("--lane")
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--model", help="the model for this one task; it counts only when a Claude subscription "
                   "provider runs the task (the lane's provider order says which runs it): other providers use their "
                   "own model")
    p.add_argument("--title")
    p.add_argument("--max-turns", type=int, metavar="N", help="this one task's ceiling of turns, instead of its "
                   "lane's max_turns")
    p.epilog = see_more("effort")
    p = add("status", cmd_status, "what is running, queued, waiting for review, and why")
    p.add_argument("--json", action="store_true")
    p = add("show", cmd_show, "one task: its text, report, changes, refused commands, usage, next step")
    p.add_argument("id")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--full", action="store_true", help="the whole text and the whole report")
    g.add_argument("--report", action="store_true", help="the report alone, whole (for a verification, its verdict "
                                                         "and its report)")
    g.add_argument("--check", action="store_true", help="the task's last check and its last merge check, each apart: "
                   "how it ended, the file with its whole output and its size, and its last lines, keys removed")
    g.add_argument("--check-log", action="store_true", help="the whole output of the task's last check, keys removed")
    g.add_argument("--merge-check-log", action="store_true", help="the whole output of the task's last merge check, "
                                                                  "keys removed")
    p.epilog = see_more("check-output")
    p = add("diff", cmd_diff, "a task's changes against its base")
    p.add_argument("id")
    p.add_argument("paths", nargs="*", metavar="path", help="only these files or folders")
    p.add_argument("--stat", action="store_true")
    p.add_argument("--new", action="store_true", help="for a follow-up (hansa retry): only what it added on top of the "
                                                      "earlier task's last commit")
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
    p.add_argument("--despite-check", action="store_true", help="merge now with no merge check, despite a failed check "
                   "or a 'does not hold' verdict (recorded); never past release files or a key")
    p.add_argument("--recheck", action="store_true", help="run the merge check (the lane's merge_check when set) "
                   "on the task merged onto the base "
                   "as it is now, and merge exactly that if it passes (after a check that failed or could not run for "
                   "the machine's sake, once that is fixed; recorded)")
    p.epilog = see_more("merge-check")
    p = add("retry", cmd_retry, "queue a follow-up task (new id) that continues from this task's last commit with "
            "your note; this task becomes replaced")
    p.add_argument("id")
    p.add_argument("note", nargs="*")
    p.description = (p.description + ". Its worker is given the original task, one line per earlier round "
                     "(its id, how it ended, the start of its note), this note, the earlier task's "
                     "report and, when that earlier task was verified, the latest verification's report whole, with its "
                     "verdict, whatever the verdict was. Earlier notes are not carried, so repeat in this note what "
                     "still holds of them. The follow-up replaces the earlier task: the earlier "
                     "task is replaced at once (its verification, if one is open, closed), and the follow-up is the "
                     "one to review and merge. Its own lane must still be on the desk and, when recorded, on the "
                     "repository where it ran; otherwise give the work again with hansa add --lane L.")
    p.add_argument("--lane", metavar="L", help="queue the follow-up on this work lane on the same repository "
                   "and base branch as the task's own lane, for example when that lane is busy; the task's "
                   "start, text, effort and model are carried as usual")
    p.add_argument("--file", help="a file with the note, instead of words; - reads it from standard input")
    p.add_argument("--attach", action="append", default=[], metavar="PATH",
                   help="add a file to the attachments inherited from this task (repeatable; at most 20 files, "
                   "20 MB in all, including inherited files; copied when queued)")
    p.add_argument("--fresh", action="store_true", help="start from the base branch, not the earlier task")
    p.add_argument("--update", action="store_true", help="continue from the earlier task with the base as it is "
                   "now merged into its branch; a conflict is left in the files for the worker to settle")
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    p.epilog = see_more("effort")
    p = add("verify", cmd_verify, "have a worker run a finished task's change to see whether it holds (never merged)")
    p.description = ("have a worker run a finished task's change to see whether it holds (never merged). "
                     "By default it uses a verify lane listing the task's lane: free first, then fewest queued, "
                     "then lanes' order. Without one it runs on the task's own lane. "
                     "--lane L runs it on lane L instead: a verification by another model is a "
                     "stronger one, and an idle lane starts it sooner.")
    p.add_argument("id")
    p.add_argument("note", nargs="*", help="what to look at in particular")
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--lane", help="run the verification on this lane, with its provider, model, effort, sandbox and "
                   "lists, overriding verify lanes. A lane on the same repository as the task's; "
                   "use it to have another model check the task, or when the task's lane is busy")
    p.epilog = see_more("effort")
    p = add("cancel", cmd_cancel, "stop a queued or running task or verification; on a merging task, stop its merge "
            "check and return it to review; finished work needs close")
    p.add_argument("id")
    p = add("close", cmd_close, "close a finished task or verification without merging")
    p.add_argument("id")
    p = add("pause", cmd_pause, "stop new tasks from starting")
    p.add_argument("reason", nargs="*")
    add("resume", cmd_resume, "let new tasks start again")
    p = add("changelog", cmd_changelog, "list workers' changelog lines for a lane's next release to a target")
    p.add_argument("lane")
    p.add_argument("target", nargs="?")
    p.epilog = see_more("release-flow")
    p = add("release", cmd_release, "ask for a release of a lane's base branch (a named target, run by hansa "
            "after the principal's yes on the page), or list the releases")
    p.description = ("hansa release L [TARGET] [note]: ask for a release of lane L's base branch as it is now, by "
                     "one of its named release targets (hansa lane target L NAME \"...\"); TARGET is needed when "
                     "the lane has more than one. "
                     "hansa records the commit, the target and its command, the note, the commits that go out (on no "
                     "remote-tracking branch and not in that target's last release) and the files they change; a word "
                     "the principal keeps from going out, in the command line or in a line, a file name or a message "
                     "of any of those commits, refuses it. A target that must follow another (--after) is asked only "
                     "for a commit the other one already sent out. It waits for the principal's yes on the web page "
                     "(unless the principal lists that target as released without asking and the desk is not "
                     "paused, and direct commits do not require their yes; a tick stops counting when the lane's "
                     "repository or base branch, or the target's "
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
                     "recent releases, including direct base commits and hand merges. The principal's release_direct "
                     "setting defaults to record; hold requires --direct \"<why>\" and their yes for these commits, "
                     "even for a ticked target. --direct with no direct commits is refused. Only the coordinator asks. "
                     "hansa release --stop ID [reason] calls one off: one "
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
                "form is always an answer. Never guess a code, nor ask the principal to send it anywhere else. "
                + see_more("release-flow", "forbidden-words", "alerts"))
    p.add_argument("--stop", metavar="ID", help="call release ID off (the rest of the line is the reason): TERM to a "
                   f"running one's command, KILL {release.STOP_GRACE} s later")
    p.add_argument("--direct", metavar="WHY", help="record why direct base commits go out; required when the "
                   "principal's release_direct setting is hold, then waits for their yes even for a ticked target; "
                   "refused when there are no direct commits")
    p.add_argument("lane", nargs="?")
    p.add_argument("note", nargs="*")
    p = add("notify", cmd_notify, "list the sessions hansa posts to; on / off: start or stop posting to the Claude "
            "Code session this is run from")
    p.add_argument("action", nargs="?", choices=["on", "off"])
    card = p.add_mutually_exclusive_group()
    card.add_argument("--no-card", dest="card", action="store_false", default=None,
                      help="with on: remove hansa's card copy and keep it off for this desk until --card")
    card.add_argument("--card", action="store_true", default=None,
                      help="with on: turn the desk's card copy back on (default: keep the desk's choice)")
    p.epilog = see_more("inbox")
    p = add("wait", cmd_wait, "return when a task finishes or fails, or a to-do closes; run it in the "
            "background when hansa does not notify the session (hansa notify)")
    p.add_argument("ids", nargs="*", metavar="id", help="tasks to watch (default: every queued or running task)")
    p.add_argument("--timeout", type=float, default=60, help="minutes (default 60)")
    p.add_argument("--every", type=float, default=5.0, help=argparse.SUPPRESS)
    p.epilog = see_more("inbox")
    p = add("ask", cmd_ask, "record a to-do for the principal, or close it with its outcome (--close)")
    p.description = ('Record what only the principal can do: hansa ask "<what>" [--why "<why only the principal '
                     'can do it>"] [--holds]. Without --holds, work goes on. The page is read-only; the principal '
                     'answers in the conversation, and the coordinator closes the to-do: hansa ask --close N '
                     '"<outcome>". The outcome is required. Workers and verifications cannot close to-dos. '
                     'Every to-do stays on record.')
    p.add_argument("text", nargs="*", help="what the principal must do; with --close, the outcome")
    p.add_argument("--why", default="", help="why only the principal can do it")
    p.add_argument("--holds", action="store_true", help="work waits on this to-do (otherwise work goes on)")
    p.add_argument("--close", type=int, metavar="N", help="close open to-do #N of this desk with its outcome")
    p.epilog = see_more("inbox")
    p = add("explain", cmd_explain, "this desk's current settings and state: lane NAME | provider NAME | "
            "server | notify")
    p.add_argument("what", nargs="?", choices=["lane", "provider", "task", "server", "notify"],
                   metavar="lane | provider | server | notify")
    p.epilog = "What a task's states mean: hansa help states; hansa help topics lists the other topics."
    p.add_argument("name", nargs="?")
    p.add_argument("--all", action="store_true", help="with lane NAME: the whole built-in list of what its workers "
                   "may not read, name by name (without it, how many names it has)")
    p = add("lane", cmd_lane, "list the lanes, or add, change or remove one")
    lane = p.add_subparsers(dest="action", metavar="add | set | target | brief | remove | try")
    q = lane.add_parser("add", help="a new lane on a git checkout", description=(
        "A new lane on a git checkout, stopped at 200 turns by default. A lane runs one task at a time, in its own full worktree (the repository's "
        "disk space again): to run tasks side by side add lanes on the same repository and base, up to hansa "
        "concurrency in all, and to split work among providers give each its own lane with its provider order. "
        "The lane's setup and check run with /bin/sh -c: POSIX shell only, no bash syntax. " +
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
                   "providers, setup, check, merge_check, merge_lane, verify_for, network, folders it may write, env, allow, limits "
                   "with merge_timeout_min, commit_message, merge_message, identity, release "
                   "targets, release_files) "
                   "and standing instructions; "
                   "each other option given replaces that one field. It is a copy: later changes to LANE do not "
                   "reach the new lane. The worktree is the new lane's own. Standing instructions an earlier lane "
                   "of this name left, when they differ, are kept as brief-before-DATE.md beside the new ones. Not "
                   "with --repo")
    q.add_argument("--base", help="base branch; must resolve to an existing commit (default: the checkout's current branch)")
    q.add_argument("--providers", help="provider order, comma-separated (default: claude)")
    q.add_argument("--check", help="the command hansa runs after each task, and on a merge when the base has moved "
                   "unless the lane has a merge_check (/bin/sh -c)")
    q.add_argument("--setup", help="the command that installs dependencies before each task (/bin/sh -c)")
    q.add_argument("--merge-check", metavar="COMMAND", help="the command the merge check runs before each merge in "
                   "place of the check, on the task merged with the base (/bin/sh -c; default: the check)")
    q.add_argument("--merge-timeout", metavar="MIN", help="the merge check's time limit in minutes (default: the "
                   "lane's timeout_min)")
    q.add_argument("--merge-lane", choices=("true", "false"), help="serve this repository and base's merge checks "
                   "and take no work (default: false; at most one merge lane per repository and base)")
    q.add_argument("--network", nargs="+", metavar="HOST", help="the hosts its workers, setup and check may reach "
                   "(default: none)")
    q.add_argument("--identity", metavar='"NAME <EMAIL>"', help="the author and committer of every commit hansa "
                   "makes for the lane (default: the repository's own user.name and user.email, else hansa's)")
    q.add_argument("--allow", nargs="+", metavar="RULE", help="rules the lane's workers and verifiers are given without asking: commands such as 'Bash(pnpm test:*)' (workers run any command "
                   "in their sandbox and need none), and web tools: "
                   "'WebFetch(domain:docs.python.org)' or 'WebFetch(domain:*.python.org)' for each public site (a "
                   "bare WebFetch, an address, a local or private name or a wildcard over a shared suffix is "
                   "refused), WebSearch (it sends search queries out of the machine); each rule its own value")
    q.add_argument("--release-files", nargs="+", metavar="PATH", help="the files the lane's releases build and run "
                   "outside the sandbox, as paths or globs relative to the repository, separated by spaces (\"pom.xml "
                   "**/pom.xml build.sh tools/release.sh\"): hansa merge refuses a change touching one of them until "
                   "a verification of it holds (hansa verify ID), and no flag overrides that")
    q.epilog = see_more("write-env", "network-list", "web-tools", "commits", "release-files", "java", "linux-sandbox")
    q = lane.add_parser("set", help="change one field of a lane", description=(
        "allow is the rules the lane's workers and verifiers are given without asking: commands "
        "such as Bash(pnpm test:*) (workers run any command in their sandbox and need none), "
        "and Claude Code's web tools, which reach the network from its own process, outside the sandbox's network "
        "list: WebFetch only as WebFetch(domain:HOST) for each site, HOST a public site's name as the network list "
        "writes it (*. in front allowed, and it opens every name under it; a bare WebFetch, one for every site, an IP "
        "address, localhost, a name for a local or private network such as .internal or .local, or a wildcard over "
        "a shared suffix such as *.co.uk or *.github.io is refused, as are names with no dot and names such as nip.io or lvh.me that come back as this machine; any listed name can still be pointed at any address, so list only sites you know), and WebSearch, which sends search queries out "
        "of the machine; MCP tools are refused. Each rule is its own value: one Claude Code would read as several (it splits on commas and spaces outside parentheses, and every ')' closes them), such as Bash(x),WebFetch or Bash(a(b),WebFetch,c), is refused, and a rule given twice is kept once. A change of allow is recorded. providers, allow, network and write take the whole new list (none: empty); env takes NAME=VALUE pairs and replaces the "
        "lane's env; base, check, merge_check, setup, commit_message, merge_message and "
        "identity take "
        "one value (none: empty); max_turns, timeout_min, merge_timeout_min and merge_batch take one whole number. "
        "merge_batch is 1 to 10 (default 4, none restores it): the check lane takes that many queued merges "
        "together, in order, stopping before an alone task, plain merge, different check lane or conflict. "
        "A failed or stopped batch returns to its old places, each checked alone. "
        "merge_lane takes true or false (unset: false): one lane per repository and base can serve their merge "
        "checks in its own worktree and sandbox, taking no tasks, verifications or retries; "
        "a lane with work queued or running cannot become one. "
        "verify_for lists lanes on the same repository to verify (none: unset): it takes only verifications, "
        "never ordinary work, and cannot list itself, merge lanes or verify lanes, or itself be a merge lane. "
        "Ordinary work queued or running prevents setting it. "
        "check runs after each task; before a merge, hansa runs a merge check on the task merged with the base: by default the "
        "check again, when the base's tip is not in the task's recorded head. merge_check gives the merge check a "
        "command of its own (a heavier one: the whole build, packaging, the slow suites), run in the same sandbox, after the "
        "lane's setup, before every merge of a task that changed something, and never after a task; a lane with only "
        "a merge_check has no check after each task. merge_timeout_min is the merge check's own time limit in minutes "
        "(default: timeout_min). commit_message is the form of the message of the commit hansa makes of a task's "
        "work (unset: t0001: <title>), and of the merge of the base into a task's branch (retry --update); "
        "merge_message is the form of the message of a task's merge into the base (unset: Merge t0001: <title>). "
        "Each is one line with {task}, {title} and {lane}, and needs {task} or {title}. A lane whose release goes "
        "to a public repository usually leaves {task} out of both. none takes merge_check, merge_timeout_min, "
        "commit_message, merge_message or identity out again. identity is Name <email>, the author "
        "and committer of every commit hansa makes for the lane: a task's commit, its merge into the base (the merge "
        "check's merge too), and the merge of the base into a task's branch. Unset, "
        "hansa takes the repository's own user.name and user.email from its local config (git config --local), "
        "never the account's global one; with neither it writes hansa <hansa@localhost>. A lane whose release goes "
        "to a public repository should have one, on the lane or in the repository's config. A release target sends the base branch out (git push origin main, say), "
        "after the principal's yes on the page (hansa release L). A release command (with hansa "
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
    q.epilog = see_more("write-env", "network-list", "web-tools", "commits", "release-files", "java", "linux-sandbox")
    q.add_argument("values", nargs="*", metavar="value")
    q = lane.add_parser("target", help="list a lane's release targets, or set or remove a named one", description=(
        "A lane's release targets are the ways its base branch goes out: a push to the public repository, an install "
        "on a test server, an install on the production server. hansa lane target L lists them. hansa lane target L "
        "NAME \"<command>\" sets one (a new one, or a new command for one); --minutes N gives it its own time limit "
        "(1-1440; none: the lane's timeout_min); --after OTHER says it goes out only with a commit target OTHER of "
        "the same lane already sent out (none: no order). --after-merge MINUTES asks the target by itself after "
        "merges settle for that many minutes (1-1440; none: off), only with the principal's tick; refused with "
        "--after. Failed runs reach the coordinator as failed checks; only failure alerts the principal. "
        "Options alone change only those fields. hansa lane target "
        "L NAME none takes it out. An older unnamed release is now the target default, keeping its tick. "
        "hansa release L TARGET [note] asks for one; the principal answers each request, and ticks "
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
    q.add_argument("--after-merge", metavar="MINUTES", help="ask this target by itself after merges have settled "
                   "for 1-1440 minutes, only with the principal's tick; refused with --after (none: turn off)")
    q.add_argument("--fixed", nargs="?", const="yes", metavar="none", help="run from a checkout of the release's "
                   "commit made for it, holding nothing; its command names $HANSA_RELEASE_COMMIT, not the base branch "
                   "(none: run in the repository again)")
    q.epilog = see_more("release-flow")
    q = lane.add_parser("brief", help="print a lane's standing instructions, or replace them with TEXT or --file")
    q.add_argument("name")
    q.add_argument("text", nargs="*")
    q.add_argument("--file", help="a file with the new instructions; - reads them from standard input")
    q = lane.add_parser("remove", help="remove a lane with no task queued, running or waiting for review, and its "
                        "worktree", description=(
        "Remove a lane with no task queued, running or waiting for review, and its worktrees (git worktree remove, "
        "as the desk's account). When a worktree holds changes not committed or commits no branch keeps, nothing is "
        "removed unless --keep or --discard says what to do with them. For a lane that is gone but whose worktree "
        "is still there, it removes the worktree the same way. The lane's standing instructions stay."))
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
    q.epilog = see_more("linux-sandbox")
    p = add("gaps", cmd_gaps, "verification notes: list those not marked, oldest first; --all includes marks and why")
    p.description += ('. hansa gaps show ID prints a whole note, its task and title, when it was kept, and its mark, '
                      'why, who and when. hansa gaps mark ID fixed|later|wontfix "why" replaces its mark; why is '
                      'required. New "holds, with notes" verifications collect each top-level list item (including '
                      'sub-items) under the last Notes heading of any level. An absent or empty list keeps one '
                      'note pointing to the report. Other verdicts and old verifications add nothing. Notes and '
                      'marks stay in tasks/VID/meta.json across server restarts. List previews stop at 160 characters.')
    p.add_argument("words", nargs="*", metavar='show ID | mark ID fixed|later|wontfix "why"')
    p.add_argument("--all", action="store_true", help="include marked notes, with their marks and why")
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
    p.epilog = see_more("desk-files")
    p = add("hide", cmd_hide, "add folders or files to what workers may not read; only the principal removes one, on "
            "the web page")
    p.description = ("Add each full path to what workers may not read (the principal's list on the web page, Settings), "
                     "each judged as a page save adding it: any problem, even an old one, refuses it, with the page's "
                     "reason, and one already there, however it is spelled, is not added again. Each addition is "
                     "recorded as the coordinator's, with when, and shown so on the page and in hansa explain lane L. "
                     "Runs that start from now on are kept from it, and Read is denied there to the session hansa "
                     "coordinator starts from its next start (not to one hansa notify on recorded); a run already going keeps the sandbox it started with. There is no command to "
                     "remove an entry: removing one gives workers something back, so only the principal removes "
                     "entries, on the web page.")
    p.add_argument("paths", nargs="*", metavar="PATH")
    p.epilog = see_more("who-changes-what", "workers-sandbox")
    p = add("concurrency", cmd_concurrency, "show, or set, how many tasks run at once")
    p.add_argument("n", nargs="?", type=int)
    p = add("disk-floor", cmd_disk_floor, "show, or set, the free space (GB) under which hansa serve starts nothing "
            "new (default 10; 0: off)")
    p.description = ("Show, or set, the floor of free space (GB): while a volume hansa serve or its runs write on has "
                     "less free than its floor, hansa serve starts nothing new, and it starts again once each has its "
                     "floor and a margin more (1 GB). The floor holds for each volume by its own size: a volume "
                     "smaller than four times the floor (a tmpfs /tmp, say) has a quarter of its size as its floor (a "
                     "3.9 GB /tmp about 1 GB under the default 10), and a margin scaled with it, no more than a "
                     "quarter of that floor; a volume of four times the floor or more, or whose size is not known, "
                     "keeps the floor as set. One number for every volume: default 10; 0 turns it off.")
    p.add_argument("gb", nargs="?", type=float, metavar="GB")
    p.epilog = see_more("free-space")
    p = add("memory-floor", cmd_memory_floor, "show, or set, the available memory (GB) under which hansa serve "
            "starts nothing new (default 2; 0: off)")
    p.description = ("Show, or set, the floor of available memory (GB). What runs goes on; new work waits until "
                     "the floor plus 1 GB is available. A machine with less than four times the floor in memory "
                     "uses a quarter of its memory as its floor. Default 2; 0 turns it off. Only the coordinator "
                     "is notified when memory goes under or comes back; no principal alert runs.")
    p.add_argument("gb", nargs="?", type=float, metavar="GB")
    p.epilog = see_more("free-space")
    p = add("cost", cmd_cost, "tokens and what they are worth")
    p.description = ("Tokens and what they are worth: the workers' runs, and the coordinator's own session with its "
                     "helpers. The coordinator is counted only when it runs with the workers' Claude login (the same "
                     "config folder), because only then are its session records where hansa reads them; a session "
                     "started outside the desk counts once a hansa command run in it has told hansa its id. When it "
                     "is not counted, hansa cost says why. It has no lane or task, so in --by lane and --by task its "
                     "usage is a row of its own, (coordinator): in every view the rows add up to the total.")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--since", help="HH:MM (today's, or yesterday's when still ahead), or YYYY-MM-DD "
                   "HH:MM: count from then instead of --days")
    p.add_argument("--by", choices=["day", "seat", "lane", "task", "model", "provider"], help="the totals by "
                   "(default: day); not with --turns")
    p.add_argument("--json", action="store_true")
    p.add_argument("--rows", action="store_true", help="with --json: include every row")
    p.add_argument("--turns", action="store_true", help="the coordinator's own usage turn by turn: what started "
                   "each turn, its steps, the conversation's length at its first step, its worth and the tools it "
                   "called; then totals by what started them. No message text")
    p.epilog = see_more("cost-figures")
    p = add("ping", cmd_ping, "send one tiny request through a provider (or all)")
    p.description += (". It runs the claude program the recorded hansa serve start used, not necessarily this "
                      "shell's, and says so when they differ.")
    p.add_argument("provider", nargs="?")
    p = add("up", cmd_up, "start this desk's server and the coordinator's session again as they last ran; outside "
            "a desk, start this account's desks")
    p.epilog = see_more("bringing-up")
    p = add("serve", cmd_serve, "run the dispatcher and the web page in the foreground (what hansa up starts)")
    p.add_argument("--host", default=up.DEFAULTS["host"])
    p.add_argument("--port", type=int, default=up.DEFAULTS["port"], help="the web page's port (default 8765); another desk's server "
                   "on this machine needs another one")
    p.add_argument("--every", type=float, default=up.DEFAULTS["every"], help=argparse.SUPPRESS)
    p = add("web", cmd_web, "print the web page address for the principal")
    p.description += (", then wait on its input, which holds an ssh tunnel to the page open, until that input ends "
                      "or Ctrl+C. A script that wants only the address gives it no input (hansa web </dev/null): it "
                      "prints the address and returns")
    p.add_argument("--port", type=int, help="default: the port this desk's hansa serve serves the page on")
    p = add("coordinator", cmd_coordinator, "start the coordinator session in this desk; anything after the word "
            "coordinator is passed to claude as is, except --clear-daily HH-HH: hansa's mod clears the session once "
            "a day in that window of hours, local time, once it has been idle for 5 minutes")
    p.usage = "hansa coordinator [--clear-daily HH-HH] [claude's own options and words ...]"
    p.description = ("Start the coordinator session in this desk: claude, in the desk, with the card and the desk's "
                     "settings. hansa's own option is --clear-daily; every other word after coordinator goes to "
                     "claude as is (claude --help lists them). It also gives claude hansa's mod (--plugin-dir), which "
                     "compacts the session when it is idle, and --autocompact, at the principal's thresholds. "
                     "-h or --help after coordinator prints this help and starts nothing.")
    p.add_argument("--clear-daily", metavar="HH-HH", help="hansa's mod clears the session once a day in this window of "
                   "hours, local time (such as 04-09; 22-03 runs past midnight), once it has been idle for 5 minutes; "
                   "for this start only")
    p.epilog = see_more("linux-root", "inbox")
    p = add("help", cmd_help, "list the commands and the topics; with a command, that command's help; with a topic, "
            "that topic; card: the whole card")
    p.add_argument("words", nargs="*", metavar="COMMAND | TOPIC | card | topics", help="a command, such as verify: "
                   "its help, as hansa verify --help prints it (lane add: that subcommand's); a topic, such as "
                   "effort: its section of docs/; card: the whole card; topics: the topics, one line each")
    p = sub.add_parser("coordinator-cleared")  # hidden: the coordinator's hook after a /clear, as the desk's owner
    p.set_defaults(fn=cmd_coordinator_cleared)
    p = sub.add_parser("coordinator-desk")  # hidden: the desk's part of `hansa coordinator`, as the desk's owner
    p.set_defaults(fn=cmd_coordinator_desk)
    p.add_argument("--mod", default="")  # only this: record root's start of the mod (plugin.start_record), as JSON
    p.add_argument("--by", type=int, required=True)
    p = sub.add_parser("notify-follow")  # hidden: a follower (hansa/notify.py), started detached
    p.set_defaults(fn=cmd_notify_follow)
    for opt in ("--key", "--socket", "--nonce"):
        p.add_argument(opt, required=True)
    p.add_argument("--pid", type=int, required=True)
    p.add_argument("--start", default="")
    p.add_argument("--name", default="")
    p = sub.add_parser("notify-feed")  # hidden: a follower's request to the desk, as the desk's owner
    p.set_defaults(fn=cmd_notify_feed)
    p = sub.add_parser(up.LAUNCHER)  # hidden: hansa up's start of a session hansa notify on recorded; claude's after it
    p.set_defaults(fn=cmd_notify_launch)

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
    a.commands, a.visible, a.lane_commands = sub.choices, visible, lane.choices
    if a.cmd == "coordinator" and ("--help" in rest or "-h" in rest):  # hansa's help, not claude's
        sub.choices["coordinator"].print_help()
        return 0
    if not getattr(a, "fn", None):
        ap.print_help()
        return 0
    if a.cmd == "add" and not a.text and not a.file:
        ap.error("add needs the task text or --file")
    if a.cmd == "ask" and not a.text:
        ap.error("ask needs what the principal must do" if a.close is None else
                 "ask --close needs the outcome: hansa ask --close N \"<outcome>\"")
    # a follower runs as the coordinator's account, and reads the desk through the account's hansa itself
    if a.cmd not in ("coordinator", "init", "notify", "notify-follow", "up"):
        _as_desk_account(a, argv)
    if a.cmd != "init" and notify.session_here():  # run in the coordinator's session: hansa cost counts it by its id
        try:
            cost.note_session(Desk.find(a.desk))
        except SystemExit:  # no desk: the command says so itself
            pass
    try:
        a.fn(a)
    except SettingsError as e:
        raise SystemExit(f"hansa: {e}") from None
    return 0
