"""What the tests share: hansa imported from its checkout, a scratch worktree with hansa's own sandbox for it,
and a way to call bin/claude-or-codex as hansa calls claude, with the fakes in place of codex, srt and claude."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
WRAPPER = REPO / "bin" / "claude-or-codex"
FAKES = HERE / "fakes"
FIXTURES = HERE / "fixtures"

# hansa's checkout: HANSA_SRC, else the one the adapter is in (contrib/codex)
HANSA_SRC = Path(os.environ.get("HANSA_SRC") or REPO.parent.parent)

sys.path.insert(0, str(HANSA_SRC))
sys.path.insert(0, str(REPO / "shim"))
import hansa.run as hansa_run  # noqa: E402  hansa's own readings of a run, from its checkout
import hansa_codex_shim as shim  # noqa: E402

for f in FAKES.iterdir():
    f.chmod(0o755)

# The adapter refuses a worker run anywhere but macOS (shim.sandbox_problem): a test that starts one skips elsewhere.
ONLY_MACOS = "the adapter runs Codex workers only on macOS: srt elsewhere drops the nested write denies"


def skip_unless_macos(test):
    if sys.platform != "darwin":
        test.skipTest(ONLY_MACOS)


def git(*args, cwd):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=str(cwd), check=True,
                   capture_output=True)


class Scratch(unittest.TestCase):
    """A repository with a linked worktree (as hansa's task worktrees are), a worker temporary folder,
    a Codex home, a log folder and the --settings hansa would give the worker there."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="codex-adapter-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        repo = self.root / "repo"
        repo.mkdir()
        git("init", "-q", "-b", "main", cwd=repo)
        (repo / "README.md").write_text("# scratch\n")
        git("add", "README.md", cwd=repo)
        git("commit", "-q", "-m", "init", cwd=repo)
        git("worktree", "add", "-q", str(self.root / "wt"), "-b", "task/t1", cwd=repo)
        self.wt = self.root / "wt"
        self.tmp = self.root / "worker-tmp"
        self.home = self.root / "codex-home"
        self.logs = self.root / "logs"
        self.secret = self.root / "secrets"
        self.cache = self.root / "build-cache"
        for d in (self.tmp, self.secret, self.cache):
            d.mkdir()
        self.lane_env = self.root / "lane-env.sh"
        self.lane_env.write_text("export FROM_LANE_ENV='lane value'\n")
        # hansa's git deny list as on macOS, where the adapter runs (on Linux, hansa lists only the GIT_OPS there)
        with mock.patch.object(hansa_run.account, "LINUX", False):
            deny_write = hansa_run.git_write_deny(self.wt)
        self.box = {"sandbox": {
            "enabled": True, "failIfUnavailable": True, "autoAllowBashIfSandboxed": True,
            "allowUnsandboxedCommands": False,
            "filesystem": {"denyRead": hansa_run.full_paths([self.secret, Path.home() / ".codex"]),
                           "denyWrite": deny_write,
                           "allowWrite": hansa_run.full_paths([self.cache])},
            "network": {"allowedDomains": ["pypi.org"], "strictAllowlist": True},
            "credentials": {"envVars": [{"name": v, "mode": "deny"} for v in hansa_run.KEY_VARS]}}}
        self.sid = str(uuid.uuid4())
        self.codex_record = self.root / "codex-record.json"
        self.srt_record = self.root / "srt-record.json"

    def env(self, fixture="success.jsonl", **extra):
        env = {k: os.environ[k] for k in ("HOME", "PATH", "LANG", "USER", "LOGNAME", "SHELL") if k in os.environ}
        env.update({
            "TERM": "dumb", "ANTHROPIC_BASE_URL": shim.PLACEHOLDER, "ANTHROPIC_AUTH_TOKEN": "placeholder-key",
            "CLAUDE_CONFIG_DIR": str(self.root / "claude-home"), "CLAUDE_ENV_FILE": str(self.lane_env),
            "TMPDIR": str(self.tmp), "CLAUDE_CODE_TMPDIR": str(self.tmp), "HANSA_TASK": "t1",
            "HANSA_CODEX_BIN": str(FAKES / "codex"), "HANSA_CODEX_HOME": str(self.home),
            "HANSA_CODEX_LOGS": str(self.logs), "HANSA_CODEX_SRT": str(FAKES / "srt"),
            "HANSA_REAL_CLAUDE": str(FAKES / "claude"),
            "FAKE_CODEX_FIXTURE": str(FIXTURES / fixture), "FAKE_CODEX_RECORD": str(self.codex_record),
            "FAKE_SRT_RECORD": str(self.srt_record)})
        env.update(extra)
        return env

    def worker_argv(self, max_turns=300, effort="", box=None):
        """A worker's command line as hansa's start builds it (run.py), after the program."""
        argv = ["-p", "--output-format", "json", "--session-id", self.sid, "--permission-mode", "acceptEdits",
                "--max-turns", str(max_turns), "--model", "codex", "--append-system-prompt",
                "You are a worker on task t1.\nSay \"done\" when done; C:\\path stays."]
        if effort:
            argv += ["--effort", effort]
        argv += hansa_run.isolated(self.wt, self.box if box is None else box)
        argv += ["--disallowedTools", "Bash(git commit:*)", "--allowedTools", "Bash(ls:*)"]
        return argv

    def start(self, argv, env, task="Add hello.txt.\n", cwd=None):
        if shim.call_kind(argv, env) == "codex":
            skip_unless_macos(self)
        task_file = self.root / "task.md"
        task_file.write_text(task)
        with open(task_file, "rb") as stdin:
            return subprocess.Popen([str(WRAPPER), *argv], cwd=str(cwd or self.wt), env=env, stdin=stdin,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)

    def run_worker(self, fixture="success.jsonl", timeout=60, argv=None, **extra):
        p = self.start(argv or self.worker_argv(), self.env(fixture, **extra))
        out, err = p.communicate(timeout=timeout)
        self.err = err.decode(errors="replace")
        self.assertTrue(out.strip(), f"no answer; stderr: {self.err}")
        return json.loads(out), p.returncode

    def codex_called(self):
        return json.loads(self.codex_record.read_text())

    def srt_called(self):
        return json.loads(self.srt_record.read_text())


def alive(pid: int) -> bool:
    table = shim.process_table()
    if table:
        return pid in table
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True
