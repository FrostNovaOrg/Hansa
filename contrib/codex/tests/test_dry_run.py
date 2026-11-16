"""Exercise the dry run's commit check without starting Codex or using a login."""
import subprocess
import sys

from .codexkit import HANSA_SRC, REPO, Scratch


class DryRunCommitCheck(Scratch):
    def commit_check(self):
        script = (REPO / "dry-run.sh").read_text()
        source = script.split("<<'COMMIT_CHECK'\n", 1)[1].split("\nCOMMIT_CHECK", 1)[0]
        result = subprocess.run([sys.executable, "-", str(HANSA_SRC)], input=source,
                                cwd=self.wt, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_linux_commit_check_prints_refused_settings_and_keeps_init(self):
        paths = ("sub/.claude/settings.json", "deep/.codex/config.toml", "deep/.agents/skill.md")
        for name in paths:
            path = self.wt / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}\n")
        (self.wt / "link").mkdir()
        (self.wt / "link/.claude").symlink_to("../sub/.claude")
        (self.wt / "hello.txt").write_text("hello from codex\n")
        output = self.commit_check()
        self.assertIn("refusing to commit", output)
        for name in (*paths, "link/.claude"):
            self.assertIn(name, output)
        log = subprocess.check_output(["git", "log", "--format=%s"], cwd=self.wt, text=True)
        self.assertEqual(log.splitlines(), ["init"])
        staged = subprocess.check_output(["git", "diff", "--cached", "--name-only"], cwd=self.wt, text=True)
        self.assertIn("hello.txt", staged)
        self.assertTrue((self.wt / paths[0]).exists())

    def test_linux_commit_check_handles_no_protected_changes(self):
        (self.wt / "hello.txt").write_text("hello from codex\n")
        (self.wt / "sub/.claude").mkdir(parents=True)
        output = self.commit_check()
        self.assertIn("No protected changes", output)
        self.assertIn("empty folders", output)
        self.assertNotIn("refusing to commit", output)
