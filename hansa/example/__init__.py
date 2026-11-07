"""`hansa example`: a small sample project, desk and first task, made without starting anything.

The sample's files are the ordinary files beside this one, copied into the folders the command makes:
sample_receipt.py and sample_test_receipt.py become the repository's receipt.py and test_receipt.py,
sample_receipt.txt its data, and first-task.md the desk's notes/first-task.md.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

from .. import alert, menu, up

SAMPLE = Path(__file__).resolve().parent
DEFAULT_DIR = "hansa-example"
CHECK = "python3 -m unittest discover"
NAME, EMAIL = "Hansa Example", "hansa-example@example.invalid"
COPY = (("sample_receipt.py", "receipt.py"), ("sample_test_receipt.py", "test_receipt.py"),
        ("sample_receipt.txt", "receipt.txt"))

STEPS = {
    "en": {
        "made": "hansa example made the sample at {root}.",
        "next": "Next:",
        "run": "Run `hansa` to open the desk menu.",
        "pick": "Pick the desk {name}; press s to start it, then Enter to open its coordinator.",
        "say": "Tell the coordinator: \"Do the task in notes/first-task.md, verify it, and merge it when it holds. "
               "Do not release it.\"",
        "remove": "To remove the sample again: stop its server (press x in the menu), run "
                  "`hansa desks --forget {desk}`, then delete {root}.",
    },
    "zh": {
        "made": "hansa example 已在 {root} 建好示例。",
        "next": "接下来：",
        "run": "运行 `hansa` 打开工作台菜单。",
        "pick": "选择工作台 {name}，按 s 启动，再按回车进入协调员。",
        "say": "对协调员说：“Do the task in notes/first-task.md, verify it, and merge it when it holds. "
               "Do not release it.”",
        "remove": "以后要删除示例：先在菜单里按 x 停止它的后台，再运行 `hansa desks --forget {desk}`，"
                  "最后删除 {root}。",
    },
}


def _refuse_taken(root: Path) -> None:
    """Refuse a folder that is not new or empty, before anything is made."""
    refuse = (f"hansa: {root} already holds something; hansa example makes the sample only in a new or empty "
              f"folder, and created nothing here. Choose another DIR, or remove that folder, then run it again.")
    if root.is_symlink():
        raise SystemExit(refuse)
    if root.exists() and not root.is_dir():
        raise SystemExit(refuse)
    if root.is_dir():
        try:
            taken = next(root.iterdir(), None) is not None
        except OSError as e:
            raise SystemExit(f"hansa: cannot read {root} ({e.strerror or e}); nothing was made")
        if taken:
            raise SystemExit(refuse)


def _step(what: str, root: Path, run):
    """Run one step; on a failure say which one, that what is there stays, and how to start over."""
    try:
        return run()
    except SystemExit as e:
        if e.code in (0, None):
            raise
        detail = str(e.code)
    except (OSError, RuntimeError, subprocess.SubprocessError, UnicodeError) as e:
        detail = str(e) or type(e).__name__
    raise SystemExit(f"hansa: could not {what}: {detail}\n"
                     f"What was made so far stays in {root}. Remove that folder "
                     f"(rm -rf {shlex.quote(str(root))}), then run hansa example again.") from None


def _git(repo: Path, *args: str, env=None) -> None:
    r = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, env=env)
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip() or f"git exited {r.returncode}")


def _make_repo(root: Path, repo: Path) -> Path:
    """The sample repository: the program, its tests and a receipt, in one commit with an identity of its own."""
    repo.mkdir()
    for source, target in COPY:
        shutil.copyfile(SAMPLE / source, repo / target)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    # The identity is on this command line alone: the account needs none, and nothing is written to its git settings.
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_"))}
    _git(repo, "-c", f"user.name={NAME}", "-c", f"user.email={EMAIL}", "-c", "commit.gpgsign=false",
         "commit", "-q", "-m", "Add a receipt total", env=env)
    return repo


def _make_desk(root: Path, repo: Path):
    from ..cli import init_desk
    desk = init_desk(root, str(repo), "a", command="hansa example")
    settings = desk.settings()
    settings["lanes"]["a"]["check"] = CHECK
    problems = desk.save_settings(settings, by="coordinator", command="hansa example")
    if problems:
        raise RuntimeError("; ".join(problems))
    shutil.copyfile(SAMPLE / "first-task.md", desk.notes_dir / "first-task.md")
    return desk


def _list_desk(desk) -> None:
    problem = up.list_desk(desk)
    if problem:
        raise RuntimeError(problem)


def next_steps(root: Path, desk, lang: str) -> str:
    words = STEPS[lang]
    name = alert.desk_name(desk)
    values = {"root": root, "name": name, "desk": shlex.quote(str(desk.root))}
    return "\n".join([words["made"].format(**values), "", words["next"],
                      f"  1. {words['run'].format(**values)}",
                      f"  2. {words['pick'].format(**values)}",
                      f"  3. {words['say'].format(**values)}",
                      "", words["remove"].format(**values)])


def make(given, lang=None) -> int:
    """Make the sample under given (default ~/hansa-example), printing what to do next in lang."""
    root = Path(os.path.abspath(os.path.expanduser(str(given)))) if given else Path.home() / DEFAULT_DIR
    lang = menu.language(lang)
    _refuse_taken(root)
    root = Path(os.path.realpath(root))  # one spelling in what it prints, as hansa's desk resolves its folder too
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise SystemExit(f"hansa: could not make {root}: {e.strerror or e}; nothing was made") from None
    repo = _step("make the sample's git repository", root, lambda: _make_repo(root, root / "repo"))
    desk = _step("make the desk", root, lambda: _make_desk(root / "desk", repo))
    _step("put the desk on the account's list of desks", root, lambda: _list_desk(desk))
    print(next_steps(root, desk, lang))
    return 0
