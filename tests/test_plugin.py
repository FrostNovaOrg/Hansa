"""Plugin surface: the MCP server, the resident-service installer and the Claude Code hooks."""
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bellhop import daemon, desk_ops, ring  # noqa: E402
from bellhop.config import Desk  # noqa: E402
from bellhop.util import read_json, write  # noqa: E402

MAIN = str(ROOT / "bellhop_main.py")
TOOL_NAMES = {"desk_status", "ring", "watch_once", "sample", "refresh_card", "ledger_add", "decide", "readings"}


class PluginTest(unittest.TestCase):
    def setUp(self):
        os.environ["GIT_AUTHOR_NAME"] = os.environ["GIT_COMMITTER_NAME"] = "test"
        os.environ["GIT_AUTHOR_EMAIL"] = os.environ["GIT_COMMITTER_EMAIL"] = "test@example.invalid"
        self.tmp = Path(tempfile.mkdtemp(prefix="bellhop-plugin-test.")).resolve()
        self.desk_dir = self.tmp / "desk"
        desk_ops.init_desk(self.desk_dir)
        self.services = self.tmp / "services"
        self.services.mkdir()
        self.desk = Desk(self.desk_dir, read_json(self.desk_dir / "bellhop.json", {}))
        self._env_backup = {k: os.environ.get(k) for k in ("BELLHOP_DESK", "BELLHOP_SERVICE_DIR")}
        os.environ.pop("BELLHOP_DESK", None)
        os.environ["BELLHOP_SERVICE_DIR"] = str(self.services)

    def tearDown(self):
        for k, v in self._env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers -----------------------------------------------------------
    def child_env(self, **extra):
        env = dict(os.environ)
        env.pop("BELLHOP_DESK", None)
        env.update(extra)
        return env

    def set_cfg(self, **fields):
        cfg = read_json(self.desk_dir / "bellhop.json", {})
        cfg.update(fields)
        write(self.desk_dir / "bellhop.json", json.dumps(cfg, indent=2))
        self.desk = Desk(self.desk_dir, cfg)

    def packet(self, pid="001", title="Add health check note"):
        text = (
            f"---\nid: {pid}\nlane: api\ntitle: {title}\nbase: HEAD\nbudget_minutes: 10\n"
            "acceptance:\n  - ./check.sh prints 'check.sh: all green'\n---\nAdd a note file. Keep checks green.\n"
        )
        p = self.desk.inbox / "api" / f"{pid}-note.md"
        write(p, text)
        return p

    def tree(self, root: Path):
        return sorted(str(p.relative_to(root)) for p in root.rglob("*") if ".git/" not in str(p.relative_to(root)) + "/")

    # -- 1. MCP server -----------------------------------------------------
    def test_mcp_server_answers_jsonrpc_over_stdio(self):
        # the background watchdog would answer bells; this test only speaks protocol
        self.set_cfg(session_watchdog=False)
        errlog = self.tmp / "mcp.err"
        with errlog.open("wb") as err:
            proc = subprocess.Popen(
                [sys.executable, MAIN, "--desk", str(self.desk_dir), "mcp"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err,
                text=True, bufsize=1, cwd=str(self.tmp), env=self.child_env(),
            )
        killer = threading.Timer(15.0, proc.kill)
        killer.start()
        try:
            init = self.rpc(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"}}})
            self.assertEqual(init["id"], 1)
            self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")
            self.assertEqual(init["result"]["serverInfo"]["name"], "bellhop")

            self.send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})

            listed = self.rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            self.assertEqual(listed["id"], 2)
            self.assertEqual({t["name"] for t in listed["result"]["tools"]}, TOOL_NAMES)
            self.assertEqual(len(listed["result"]["tools"]), 8)

            status = self.rpc(proc, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                     "params": {"name": "desk_status", "arguments": {}}})
            self.assertEqual(status["id"], 3)
            self.assertFalse(status["result"]["isError"])
            text = status["result"]["content"][0]["text"]
            self.assertIn("card:", text)
            self.assertIn("pending bells: 0", text)

            sample = self.rpc(proc, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                     "params": {"name": "sample", "arguments": {"id": "001"}}})
            self.assertEqual(sample["id"], 4)
            self.assertIn("sampled id=001", sample["result"]["content"][0]["text"])

            bad = self.rpc(proc, {"jsonrpc": "2.0", "id": 5, "method": "no/such/method"})
            self.assertEqual(bad["id"], 5)
            self.assertEqual(bad["error"]["code"], -32601)
            self.assertNotIn("result", bad)

            proc.stdin.close()
            self.assertEqual(proc.wait(timeout=10), 0, errlog.read_text(encoding="utf-8"))
        finally:
            killer.cancel()
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            proc.stdin.close()
            proc.stdout.close()

    def send(self, proc, msg):
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()

    def rpc(self, proc, msg):
        self.send(proc, msg)
        line = proc.stdout.readline()
        self.assertTrue(line.strip(), f"no reply to {msg.get('method')}")
        reply = json.loads(line)
        self.assertEqual(reply["jsonrpc"], "2.0")
        return reply

    # -- 2. resident-service installer -------------------------------------
    def service_manager_fixture(self):
        """Nothing here may reach the host's real service manager.

        The fixture installs files and never loads them, so every probe and unload
        below is about a unit this machine has never heard of. Asking `launchctl` or
        `systemctl` about it works by accident — the return codes for "not loaded"
        differ by host, and a developer running the suite should not have their own
        services spoken to at all. The double replaces the manager and the boundary
        patch makes any escape to a real command fail loudly rather than quietly work.
        """
        lab = daemon.label(self.desk)
        if sys.platform == "darwin":
            target = f"gui/{os.getuid()}/{lab}"
            probe = ["launchctl", "print", target]
            unload = [["launchctl", "bootout", target], ["launchctl", "bootout", target + ".sentinel"],
                      ["launchctl", "disable", target], ["launchctl", "disable", target + ".sentinel"]]
        elif sys.platform.startswith("linux"):
            probe = ["systemctl", "--user", "is-active", f"{lab}.service"]
            unload = [["systemctl", "--user", "stop", f"{lab}.service", f"{lab}-sentinel.timer"]]
        else:  # pragma: no cover - the installer refuses other platforms
            self.skipTest("the service installer supports macOS and Linux")
        allowed = [probe] + unload

        def double(argv):
            self.assertIn(argv, allowed, "unexpected service-manager command")
            return subprocess.CompletedProcess(
                argv, 3 if argv == probe else 1, "inactive\n", "test double: unit not loaded")

        manager = patch.object(daemon, "_run_quiet", side_effect=double).start()
        escape = patch.object(daemon, "run", side_effect=AssertionError(
            "a real service-manager command escaped the fixture")).start()
        self.addCleanup(patch.stopall)
        return manager, escape, probe, unload

    def test_daemon_install_status_and_uninstall(self):
        manager, escape, probe, unload = self.service_manager_fixture()
        report = daemon.install(self.desk, load=False)
        self.assertFalse(report["loaded"])
        self.assertEqual(report["errors"], [])

        vendored = self.desk_dir / ".bellhop"
        main = vendored / "bellhop_main.py"
        self.assertTrue(main.exists())
        self.assertTrue((vendored / "bellhop" / "cli.py").exists())
        self.assertTrue((vendored / "template" / "templates" / "worker-prompt.md").exists())
        self.assertEqual((vendored / "VERSION").read_text(encoding="utf-8").strip(), daemon.vendored_version(self.desk))

        written = sorted(self.services.iterdir())
        self.assertEqual([str(p) for p in written], sorted(report["files"]))
        if sys.platform == "darwin":
            self.assertEqual(len(written), 2)
            self.assertTrue(all(p.suffix == ".plist" for p in written), written)
            for p in written:
                plist = plistlib.loads(p.read_bytes())
                self.assertIn(str(main), plist["ProgramArguments"])
                self.assertEqual(plist["WorkingDirectory"], str(self.desk_dir))
                self.assertTrue(plist["Label"].startswith("com.frostnova.bellhop."))
        elif sys.platform.startswith("linux"):
            names = [p.name for p in written]
            self.assertTrue(any(n.endswith(".service") for n in names), names)
            self.assertTrue(any(n.endswith(".timer") for n in names), names)
            for p in written:
                if p.suffix == ".service":
                    self.assertIn("ExecStart=", p.read_text(encoding="utf-8"))
                    self.assertIn(str(main), p.read_text(encoding="utf-8"))
        else:  # pragma: no cover - the installer refuses other platforms
            self.fail(f"unexpected platform {sys.platform}")

        marker = read_json(self.desk_dir / "state" / ".daemon.json", None)
        self.assertIsNotNone(marker)
        self.assertFalse(marker["loaded"])
        self.assertEqual(marker["service_files"], report["files"])

        status = daemon.status(self.desk)
        self.assertTrue(status["installed"])
        self.assertIsNone(status["heartbeat_age_s"])
        self.assertFalse(status["heartbeat_fresh"])
        self.assertFalse(status["vendored_stale"])
        self.assertIn("resident service: installed", daemon.status_text(self.desk))

        self.assertIsNone(daemon.heartbeat_age(self.desk))
        daemon.touch_heartbeat(self.desk, "test")
        age = daemon.heartbeat_age(self.desk)
        self.assertIsNotNone(age)
        self.assertLess(age, 5)
        self.assertTrue(daemon.status(self.desk)["heartbeat_fresh"])

        # nothing was ever loaded, so the unload commands are expected to fail
        removed = daemon.uninstall(self.desk)
        self.assertEqual(sorted(removed["removed"]), sorted(report["files"]))
        self.assertTrue(removed["errors"], "unloading a service that was never loaded should report an error")
        self.assertEqual(list(self.services.iterdir()), [])
        self.assertFalse((self.desk_dir / "state" / ".daemon.json").exists())
        self.assertFalse(daemon.status(self.desk)["installed"])
        # the only commands that were run are the probe (once per status read) and the
        # platform's unload list, and none of them reached a real service manager
        argv = [c.args[0] for c in manager.call_args_list]
        self.assertEqual([a for a in argv if a == probe], [probe] * 3)
        self.assertEqual([a for a in argv if a != probe], unload)
        escape.assert_not_called()

    # -- 3. hooks ----------------------------------------------------------
    def hook(self, event, stdin="", cwd=None):
        return subprocess.run([sys.executable, MAIN, "hook", event], input=stdin, capture_output=True,
                              text=True, cwd=str(cwd or self.tmp), env=self.child_env())

    def test_hook_session_start_briefs_the_session(self):
        out = self.hook("session-start", stdin=json.dumps({"cwd": str(self.desk_dir)}))
        self.assertEqual(out.returncode, 0, out.stderr)
        # The reading list first, then the board it stands in for.
        self.assertIn("Reading list, in order:", out.stdout)
        self.assertIn("== card", out.stdout)
        self.assertIn("== watchdog", out.stdout)
        self.assertIn(str(self.desk_dir), out.stdout)
        self.assertIn("== sentinel", out.stdout)

    def test_hook_is_silent_outside_a_desk(self):
        elsewhere = self.tmp / "not-a-desk"
        elsewhere.mkdir()
        out = self.hook("session-start", stdin="", cwd=elsewhere)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout, "")
        out = self.hook("stop", stdin="", cwd=elsewhere)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout, "")

    def test_hook_stop_warns_about_unanswered_bells(self):
        ring.ring(self.desk, self.packet())
        self.assertEqual(len(list(self.desk.bells_pending.glob("*.bell"))), 1)
        self.assertIsNone(daemon.heartbeat_age(self.desk))
        out = self.hook("stop", stdin=json.dumps({"cwd": str(self.desk_dir)}))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("pending bell", out.stdout)
        self.assertIn("no watchdog heartbeat", out.stdout)
        # a fresh heartbeat means someone is answering; stay quiet
        daemon.touch_heartbeat(self.desk, "test")
        out = self.hook("stop", stdin=json.dumps({"cwd": str(self.desk_dir)}))
        self.assertEqual(out.stdout, "")

    # -- 4. `bellhop daemon files` -----------------------------------------
    def test_daemon_files_prints_without_writing(self):
        before = self.tree(self.desk_dir)
        out = subprocess.run([sys.executable, MAIN, "--desk", str(self.desk_dir), "daemon", "files"],
                             capture_output=True, text=True, cwd=str(self.tmp), env=self.child_env())
        self.assertEqual(out.returncode, 0, out.stderr)
        headers = [l for l in out.stdout.splitlines() if l.startswith("===== ")]
        self.assertEqual(len(headers), 2 if sys.platform == "darwin" else 3, out.stdout)
        self.assertIn(str(self.desk_dir / ".bellhop" / "bellhop_main.py"), out.stdout)
        self.assertTrue(all(str(self.services) in h for h in headers), headers)
        self.assertEqual(list(self.services.iterdir()), [])
        self.assertEqual(self.tree(self.desk_dir), before)
        self.assertFalse((self.desk_dir / ".bellhop").exists())


if __name__ == "__main__":
    unittest.main()
