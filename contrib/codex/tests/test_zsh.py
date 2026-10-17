"""TMPPREFIX: where zsh makes a here-document's temporary file (default /tmp/zsh, not under TMPDIR), which srt
must let the run write, or `cat > f <<'EOF'` fails in Codex's commands as it did in its first real runs."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from .codexkit import Scratch, shim, skip_unless_macos

ZSH = shutil.which("zsh") or ""
HEREDOC = "cat <<'EOF'\nhere-document ok\nEOF\n"
REFUSED = "can't create temp file for here document"


def zsh_heredoc(prefix=None, run=()):
    """zsh -c with a here-document, TMPPREFIX set to `prefix` (None: zsh's default), maybe under `run`."""
    env = {k: v for k, v in os.environ.items() if k != "TMPPREFIX"}
    if prefix is not None:
        env["TMPPREFIX"] = prefix
    return subprocess.run([*run, ZSH, "-f", "-c", HEREDOC], env=env, capture_output=True, text=True, timeout=60)


class ZshPrefix(Scratch):
    def test_codex_gets_a_prefix_srt_lets_it_write(self):
        run, code = self.run_worker("success.jsonl", TMPPREFIX="/tmp/zsh")
        self.assertEqual((code, run["subtype"]), (0, "success"), run["result"])
        called = self.codex_called()
        prefix = called["env"]["TMPPREFIX"]
        self.assertEqual(called["env"]["TMPDIR"], str(self.tmp), "TMPDIR is left as hansa gives it")
        folder = os.path.dirname(prefix)
        self.assertEqual(os.path.dirname(folder), str(self.tmp), "in the shim's own folder in TMPDIR")
        self.assertTrue(os.path.basename(folder).startswith("codex-"))
        fs = self.srt_called()["settings"]["filesystem"]
        self.assertTrue(any(shim.under(x, w) for x in shim.both([prefix]) for w in fs["allowWrite"]), fs)
        self.assertFalse(shim.denied(prefix, fs["denyWrite"]))
        # and to Codex's commands, whatever its shell_environment_policy inherits
        self.assertIn(f"shell_environment_policy.set.TMPPREFIX={json.dumps(prefix)}", called["argv"])
        self.assertFalse(os.path.exists(folder), "removed with the run's own files")


@unittest.skipUnless(ZSH, "no zsh here")
class ZshHeredoc(unittest.TestCase):
    """Why: zsh writes the here-document under TMPPREFIX, and fails as in the first real runs where it may not."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="codex-zsh-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_works_with_a_prefix_it_may_write(self):
        mine = self.root / "codex-run"
        mine.mkdir()
        p = zsh_heredoc(str(mine / "zsh"))
        self.assertEqual((p.returncode, p.stdout), (0, "here-document ok\n"), p.stderr)

    @unittest.skipIf(os.geteuid() == 0, "root writes in any folder")
    def test_fails_as_in_the_trial_where_it_may_not(self):
        shut = self.root / "shut"
        shut.mkdir()
        shut.chmod(0o500)
        self.addCleanup(shut.chmod, 0o700)
        p = zsh_heredoc(str(shut / "zsh"))
        self.assertNotEqual(p.returncode, 0)
        self.assertIn(REFUSED, p.stderr)


@unittest.skipUnless(ZSH, "no zsh here")
class ZshUnderSrt(Scratch):
    """Under real srt with the shim's settings, when srt can start here (not inside another sandbox: there it
    cannot listen on its socket, `listen EPERM`)."""

    def test_heredoc_in_srt(self):
        skip_unless_macos(self)  # the shim's settings are for srt on macOS
        srt = os.environ.get("HANSA_CODEX_SRT") or shutil.which("srt")
        if not srt:
            self.skipTest("no srt here")
        settings = self.root / "srt.json"
        settings.write_text(json.dumps(shim.srt_settings(self.box, str(self.wt), str(self.tmp), str(self.home))))
        mine = Path(tempfile.mkdtemp(prefix="codex-", dir=self.tmp))
        run = (srt, "--settings", str(settings), "--")
        p = zsh_heredoc(str(mine / "zsh"), run)
        if p.returncode != 0 and REFUSED not in p.stderr:
            self.skipTest(f"srt cannot start here: {p.stderr.strip()[:200]}")
        self.assertEqual((p.returncode, p.stdout), (0, "here-document ok\n"), p.stderr)
        p = zsh_heredoc(None, run)  # zsh's default, /tmp/zsh: what the first real runs had
        self.assertNotEqual(p.returncode, 0)
        self.assertIn(REFUSED, p.stderr)
