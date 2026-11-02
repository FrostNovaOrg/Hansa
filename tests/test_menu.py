"""The principal's menu: temporary desks, no page ports or real tmux servers."""
import contextlib
import copy
import fcntl
import io
import json
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import textwrap
import time
import unittest
from unittest.mock import Mock, patch

import hansa
from hansa import cli, menu, up, views
from hansa.desk import Desk, desks_file, write_json


def skip_in_sandbox(test, why):
    # Import after test_hansa has loaded this class into the lane's test entry point.
    from tests.test_hansa import skip_in_sandbox as skip
    skip(test, why)


class MenuTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name).resolve()
        env = patch.dict(os.environ, {"HOME": str(self.tmp), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                                     "XDG_STATE_HOME": str(self.tmp / "state"), "TERM": "xterm", "LANG": "C"}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        session = patch.dict(hansa.SESSION, {}, clear=True)
        session.start()
        self.addCleanup(session.stop)
        self.calls = self.tmp / "calls"
        self.tmux_calls = self.tmp / "tmux-calls"
        self.tmux = self.executable("fake tmux", f"""
            import json, sys
            with open({str(self.tmux_calls)!r}, 'a') as out:
                out.write(json.dumps(sys.argv[1:]) + '\\n')
        """)
        os.environ["HANSA_TMUX"] = str(self.tmux)
        self.hansa = self.executable("old hansa", f"""
            import json, os, signal, sys
            if sys.argv[1:] == ['up', '--help']:
                sys.exit(0)
            with open({str(self.calls)!r}, 'a') as out:
                out.write(json.dumps(sys.argv[1:]) + '\\n')
            if os.environ.get('MENU_TEST_WAIT'):
                print('command is waiting', flush=True)
                try:
                    while True: signal.pause()
                except KeyboardInterrupt:
                    print('command interrupted', flush=True)
            else:
                print('own installation output', flush=True)
        """)
        self.first, self.second = self.desk("one/desk"), self.desk("二/工作台")
        self.list_desks(self.first, self.second)

    def executable(self, name, source):
        path = self.tmp / name
        path.write_text("#!" + sys.executable + "\n" + textwrap.dedent(source), encoding="utf-8")
        path.chmod(0o755)
        return path

    def desk(self, name):
        desk = Desk(self.tmp / name)
        desk.tasks_dir.mkdir(parents=True)
        desk.state_dir.mkdir()
        write_json(desk.settings_path, {})
        return desk

    def list_desks(self, *desks):
        desks_file().parent.mkdir(parents=True, exist_ok=True)
        write_json(desks_file(), [{"desk": str(d.root), "hansa": str(self.hansa)} for d in desks])

    def task(self, **meta):
        folder = self.first.tasks_dir / str(len(list(self.first.tasks_dir.iterdir())))
        folder.mkdir()
        write_json(folder / "meta.json", meta)

    def snapshot(self, desk):
        return {str(p.relative_to(desk.root)): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in desk.root.rglob("*") if p.is_file()}

    def state(self):
        return menu.rows()

    def test_rows_are_counts_only_read_only_and_never_run_tmux(self):
        self.task(state="running", kind="task", text="private task")
        self.task(state="running", kind="verify")
        self.task(state="done", kind="task")
        self.task(state="done", kind="verify")
        write_json(self.first.asks_path, [{"text": "private todo"}, {"closed": "then"}])
        write_json(self.first.state_dir / "releases.json", [{"state": "waiting", "code": "private code"}])
        self.first.paused_path.write_text("private reason")
        before = [self.snapshot(d) for d in (self.first, self.second)]
        with patch.object(menu.notify, "names", side_effect=[["session"], []]), \
                patch.object(up, "still_runs", return_value=12), \
                patch.object(Desk, "web_token", side_effect=AssertionError("no page key")), \
                patch.object(menu.subprocess, "run", side_effect=AssertionError("no process on draw")):
            state = self.state()
            text = menu.screen(state, 0, set(), "en", 200, 24)
        first = state["desks"][0]
        self.assertEqual([first[k] for k in ("running", "review", "releases", "todos")], [2, 1, 1, 1])
        self.assertTrue(first["paused"])
        self.assertTrue(all(r["coordinator"] for r in state["desks"]))
        self.assertNotIn("private", text)
        self.assertNotIn("[x]", text)
        self.assertEqual(before, [self.snapshot(d) for d in (self.first, self.second)])
        self.assertFalse(self.tmux_calls.exists())

    def test_empty_broken_and_missing_desks_keep_the_shared_problem(self):
        self.list_desks()
        self.assertIn("hansa init", menu.screen(self.state(), 0, set(), "en", 200, 24))
        desks_file().write_text("not json")
        self.assertEqual(self.state()["problem"], views.desks()["problem"])
        gone = Desk(self.tmp / "missing")
        self.list_desks(gone, self.first)
        state = self.state()
        self.assertEqual(state["desks"][0]["problem"], "folder is gone")
        terminal = Mock()
        terminal.outside = contextlib.nullcontext
        with contextlib.redirect_stdout(io.StringIO()) as out:
            menu.perform("s", state["desks"][0], state["desks"], terminal, "en")
        self.assertIn("nothing to do", out.getvalue())
        self.assertEqual(json.loads(self.calls.read_text()), ["--desk", str(self.first.root), "up"])
        self.assertFalse(gone.root.exists())

    def test_start_reports_the_same_missing_old_and_other_account_output_as_up(self):
        row = self.state()["desks"][0]
        for saved in (self.tmp / "missing installation", self.executable("too old", "raise SystemExit(2)")):
            row["saved"]["hansa"] = str(saved)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertFalse(up.listed_desk_up(self.first.root, row["saved"], stream=True))
            self.assertIn("missing or too old", out.getvalue())
            self.assertIn("Last server command:", out.getvalue())
        foreign = self.executable("foreign desk hansa", "import sys\nif '--help' in sys.argv: sys.exit(0)\n"
                                  "print('This desk belongs to desk-user; run as desk-user'); raise SystemExit(1)")
        with patch.object(up.subprocess, "run", wraps=subprocess.run) as run:
            self.assertFalse(up.listed_desk_up(self.first.root, {"hansa": str(foreign)}, stream=True))
        self.assertFalse(run.call_args.kwargs["capture_output"])
        self.assertFalse(self.calls.exists())

    def test_pause_resume_and_web_use_the_full_saved_command_and_ssh_port(self):
        row = self.state()["desks"][0]
        argv, _, _ = menu.command(row, "p", "en")
        self.assertEqual(argv, [str(self.hansa), "--desk", str(self.first.root), "pause", "hansa menu"])
        row["paused"] = True
        self.assertEqual(menu.command(row, "p", "en")[0][-1], "resume")
        self.assertIn("Press s", menu.command(row, "w", "en")[2])
        self.assertEqual(menu.command(row, "w", "en")[0], [])
        row["server"] = "up"
        (self.first.state_dir / "web-port").write_text("9876")
        with patch.dict(os.environ, {"SSH_CONNECTION": "host client server port"}):
            argv, _, note = menu.command(row, "w", "en")
        self.assertEqual(argv, [str(self.hansa), "--desk", str(self.first.root), "web"])
        self.assertIn("ssh -L 9876:127.0.0.1:9876 HOST", note)
        self.assertEqual(menu.command(row, "w", "en")[2], "")

    def test_enter_uses_notify_records_and_switches_only_within_the_same_server(self):
        row = self.state()["desks"][0]
        self.assertIn("Press s", menu.command(row, "\r", "en")[2])
        row["coordinator"] = True
        self.assertEqual(menu.command(row, "\r", "en")[0], [])
        rec = {"kind": "notify", "session": {"pid": os.getpid()},
               "tmux": {"server": ["-L", "remembered"], "session": "principal"},
               "env": {"TMUX_TMPDIR": str(self.tmp)}}
        write_json(self.first.state_dir / up.COORDINATOR_START, rec)
        row = self.state()["desks"][0]
        self.assertTrue(row["coordinator"])
        argv, env, note = menu.command(row, "\r", "en")
        self.assertEqual(argv, ["env", "TMUX_TMPDIR=" + str(self.tmp), str(self.tmux), "-L", "remembered", "attach", "-t", "principal"])
        self.assertIn("Ctrl+B", note)
        subprocess.run(argv, env=env, check=True)
        socket = self.tmp / f"tmux-{os.getuid()}" / "remembered"
        with patch.dict(os.environ, {"TMUX": str(socket) + ",12,0"}):
            self.assertEqual(menu.command(row, "\r", "en")[0], [str(self.tmux), "-L", "remembered", "switch-client", "-t", "principal"])
        with patch.dict(os.environ, {"TMUX": str(self.tmp / "other") + ",12,0"}):
            argv, _, note = menu.command(row, "\r", "en")
            self.assertFalse(argv)
            self.assertIn("Leave tmux", note)
            self.assertIn("attach -t principal", note)
        self.assertEqual(len(self.tmux_calls.read_text().splitlines()), 1)
        rec["tmux"]["server"] = ["-S", str(socket)]
        write_json(self.first.state_dir / up.COORDINATOR_START, rec)
        with patch.dict(os.environ, {"TMUX": str(socket) + ",12,0"}):
            self.assertIn("switch-client", menu.command(row, "\r", "en")[0])
        with patch.object(menu, "program", return_value=""):
            self.assertIn("tmux is missing", menu.command(row, "\r", "en")[2])

    def test_stop_refuses_each_running_kind_and_counts_starting_work(self):
        for kind in ("task", "verify"):
            self.task(state="running", kind=kind)
        self.task(state="merging", merge_check={"phase": "starting"})
        self.task(state="merging", merge_check={"phase": "queued"})
        write_json(self.first.state_dir / "releases.json", [{"state": "starting"}, {"state": "running"}, {"state": "waiting"}])
        (self.first.state_dir / "tries").mkdir()
        write_json(self.first.state_dir / "tries/a.json", {"phase": "running"})
        write_json(self.first.state_dir / "tries/b.json", {"phase": "queued"})
        self.assertEqual(menu.busy_counts(self.first), {"task": 1, "verify": 1, "merge": 1, "release": 2, "try": 1})
        with patch.object(menu.os, "kill") as kill:
            note = menu.stop_server(self.first, "en")
        kill.assert_not_called()
        for part in ("1 tasks", "1 verifications", "1 merge checks", "2 releases", "1 lane tries", "p pauses"):
            self.assertIn(part, note)

    def test_stop_waits_for_term_and_reports_stuck_pid_without_kill(self):
        stopped = {"state": "gone", "pid": None}
        stuck = {"state": "stuck", "pid": 12345}
        with patch.object(menu, "server_state", side_effect=[stuck, stopped]), patch.object(menu.os, "kill") as kill:
            self.assertEqual(menu.stop_server(self.first, "en"), "Server stopped.")
        kill.assert_called_once_with(12345, signal.SIGTERM)
        with patch.object(menu, "server_state", return_value=stuck), patch.object(menu.os, "kill") as kill, \
                patch.object(menu.time, "monotonic", side_effect=[0, 31]):
            self.assertIn("12345", menu.stop_server(self.first, "en"))
        kill.assert_called_once_with(12345, signal.SIGTERM)
        with patch.object(menu, "server_state", return_value=stopped), patch.object(menu.os, "kill") as kill:
            menu.stop_server(self.first, "en")
        kill.assert_not_called()
        with patch.object(menu, "server_state", return_value=stuck), \
                patch.object(menu.os, "kill", side_effect=ProcessLookupError):
            self.assertIn("stopped", menu.stop_server(self.first, "en"))
        with patch.object(menu.account, "desk_account", return_value="another-user"), patch.object(menu.os, "kill") as kill:
            self.assertIn("another-user", menu.stop_server(self.first, "en"))
        kill.assert_not_called()

    def test_stop_terminates_a_real_locked_server_without_a_port(self):
        server = self.executable("server", f"""
            import fcntl, os, signal
            lock = open({str(self.first.state_dir / 'serve.lock')!r}, 'w')
            fcntl.flock(lock, fcntl.LOCK_EX)
            lock.write(str(os.getpid()))
            lock.flush()
            print('ready', flush=True)
            while True: signal.pause()
        """)
        proc = subprocess.Popen([str(server)], stdout=subprocess.PIPE)
        try:
            self.assertTrue(select.select([proc.stdout], [], [], 15)[0], "server did not hold its lock")
            self.assertEqual(proc.stdout.readline(), b"ready\n")
            self.assertNotEqual(menu.server_state(self.first)["state"], "gone")
            self.assertIn("stopped", menu.stop_server(self.first, "en"))
            self.assertEqual(proc.wait(timeout=15), -signal.SIGTERM)
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=15)
            proc.stdout.close()

    def test_x_requires_y_and_rechecks_work_after_confirmation(self):
        terminal = Mock()
        terminal.outside = contextlib.nullcontext
        terminal.key.side_effect = ["n", ".", "y", "."]
        row = self.state()["desks"][0]
        with patch.object(menu, "stop_server", return_value="stopped") as stop, contextlib.redirect_stdout(io.StringIO()) as out:
            menu.perform("x", row, [row], terminal, "en")
            stop.assert_not_called()
            menu.perform("x", row, [row], terminal, "en")
        stop.assert_called_once()
        self.assertIn("session stays open", out.getvalue())

    def test_language_precedence_chinese_cells_and_no_wrapping(self):
        for env, expected in (({"LANG": "zh_CN.UTF-8"}, "zh"),
                              ({"LC_MESSAGES": "en_US", "LANG": "zh_CN"}, "en"),
                              ({"LC_ALL": "zh_TW", "LC_MESSAGES": "en_US"}, "zh")):
            with patch.dict(os.environ, env):
                self.assertEqual(menu.language(), expected)
                self.assertEqual(menu.language("en"), "en")
        self.assertEqual(menu.cells("a中e\u0301"), 4)
        self.assertEqual(menu.cut("中ab", 3), "中a")
        state = self.state()
        for lang in ("en", "zh"):
            full = menu.screen(state, 0, set(), lang, 200, 24).splitlines()
            self.assertEqual(menu.cells(full[2].split("one/desk")[0]), menu.cells(full[3].split("二/工作台")[0]))
            for width in (12, 40, 80):
                text = menu.screen(state, 0, set(), lang, width, 24).replace("\x1b[H\x1b[2J", "")
                self.assertTrue(all(menu.cells(line) <= width for line in text.splitlines()))
        self.assertIn("做完待审", menu.screen(state, 0, set(), "zh", 200, 24))
        self.assertNotIn("\x1b", menu.cut("folder\x1b[2J\n", 100))

    def test_keys_fit_eighty_columns_and_a_short_screen_drops_rows_not_keys(self):
        for lang, labels in (("en", ("↑↓ choose", "Space tick", "a all", "s start", "Enter enter", "w page",
                                     "x stop", "p pause/resume", "r refresh", "q quit")),
                             ("zh", ("↑↓ 选择", "空格 勾选", "a 全选", "s 启动", "回车 进入", "w 页面",
                                     "x 停止", "p 暂停/恢复派活", "r 刷新", "q 退出"))):
            text = menu.screen(self.state(), 1, set(), lang, 80, 7)
            first, second = text.splitlines()[-2:]
            self.assertTrue(all(x in first for x in labels[:6]) and all(x in second for x in labels[6:]))
            self.assertTrue(menu.cells(first) <= 80 and menu.cells(second) <= 80)
            self.assertIn("二/工作台", text)  # a short screen drops rows, not keys
            self.assertNotIn("one/desk", text)

    def test_pause_and_web_print_what_start_prints_for_an_unusable_installation(self):
        row = self.state()["desks"][0]
        row["server"] = "up"
        not_ours = self.tmp / "another account's hansa"
        not_ours.write_text("#!/bin/sh\n")
        not_ours.chmod(0o600)
        terminal = Mock(outside=contextlib.nullcontext)
        for saved in (self.tmp / "missing installation", self.executable("too old", "raise SystemExit(2)"), not_ours):
            row["saved"]["hansa"] = str(saved)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertFalse(up.listed_desk_up(Path(row["desk"]), row["saved"], stream=True))
            for key in ("p", "w"):
                with patch.object(menu.subprocess, "call") as call, contextlib.redirect_stdout(io.StringIO()):
                    argv, _, note = menu.command(row, key, "en")
                    self.assertEqual((argv, note), ([], out.getvalue().rstrip("\n")))
                    menu.perform(key, row, [row], terminal, "en")
                call.assert_not_called()

    def test_enter_and_web_wait_for_a_key_only_after_a_failed_command(self):
        row, terminal = self.state()["desks"][0], Mock(outside=contextlib.nullcontext)
        for key in ("\r", "w"):
            with contextlib.redirect_stdout(io.StringIO()), \
                    patch.object(menu, "command", return_value=(["then"], {}, "")):
                for outcome, waits in ((1, True), (0, False), (KeyboardInterrupt, False)):
                    terminal.key.reset_mock()
                    with patch.object(menu.subprocess, "call", side_effect=[outcome]) as call:
                        menu.perform(key, row, [row], terminal, "en")
                    call.assert_called_once()
                    self.assertEqual(terminal.key.called, waits)

    def test_zero_sized_terminal_draws_as_eighty_by_twenty_four(self):
        text = menu.screen(self.state(), 0, set(), "en", 0, 0).replace("\x1b[H\x1b[2J", "")
        self.assertIn("hansa · choose a desk", text)
        self.assertIn("q quit", text)
        self.assertTrue(all(menu.cells(line) <= 80 for line in text.splitlines()))

    def test_no_desks_does_not_ask_to_tick_or_start(self):
        self.list_desks()
        text = menu.screen(self.state(), 0, set(), "en", 200, 24)
        self.assertIn("No desks listed", text)
        self.assertNotIn("Tick the desks", text)

    def test_cli_routes_only_principals_with_both_terminals_to_menu(self):
        with patch.object(sys.stdin, "isatty", return_value=True), patch.object(sys.stdout, "isatty", return_value=True), \
                patch.object(menu, "run") as run, patch.object(Desk, "find", return_value=None), \
                patch.object(up, "all_desks_up", return_value=True) as all_up:
            cli.main([])
            cli.main(["up"])
            self.assertEqual(run.call_count, 2)
            cli.main(["up", "--all"])
            all_up.assert_called_once()
            for name in ("HANSA_ROLE", "HANSA_TASK", "CLAUDE_PID"):
                out = io.StringIO()
                out.isatty = lambda: True
                with patch.dict(os.environ, {name: "agent"}), contextlib.redirect_stdout(out):
                    cli.main([])
                    self.assertIn("usage:", out.getvalue())
                    cli.main(["up"])
                    with self.assertRaisesRegex(SystemExit, "principal"):
                        cli.main(["menu"])
            self.assertEqual(run.call_count, 2)
        for stdin, stdout in ((True, False), (False, True), (False, False)):
            out = io.StringIO()
            out.isatty = lambda: stdout
            with patch.object(sys.stdin, "isatty", return_value=stdin), contextlib.redirect_stdout(out), \
                    patch.object(Desk, "find", return_value=None), patch.object(up, "all_desks_up", return_value=True) as all_up:
                cli.main([])
                self.assertIn("usage:", out.getvalue())
                cli.main(["up"])
                all_up.assert_called_once()
                with self.assertRaisesRegex(SystemExit, "hansa desks or hansa up"):
                    cli.main(["menu"])

    def test_menu_ignores_desk_context_but_up_all_refuses_it(self):
        with patch.object(sys.stdin, "isatty", return_value=True), patch.object(sys.stdout, "isatty", return_value=True), \
                patch.object(menu, "run") as run, patch.object(Desk, "find", return_value=self.first):
            for argv in (["menu", "--lang", "zh"], ["--desk", str(self.first.root), "menu", "--lang", "zh"]):
                with patch.dict(os.environ, {"HANSA_DESK": str(self.first.root)}):
                    cli.main(argv)
            self.assertEqual(run.call_count, 2)
            self.assertEqual(run.call_args.args, ("zh",))
            with self.assertRaisesRegex(SystemExit, "outside a desk"):
                cli.main(["up", "--all"])
        for explicit in (False, True):
            with patch.dict(os.environ, {"HANSA_DESK": str(self.first.root)}):
                with self.assertRaisesRegex(SystemExit, "outside a desk"):
                    cli.main((["--desk", str(self.first.root)] if explicit else []) + ["up", "--all"])

    def test_loop_ticks_start_targets_clear_all_and_refresh_without_a_terminal(self):
        terminal = Mock()
        terminal.opened = lambda: contextlib.nullcontext(terminal)
        terminal.outside = contextlib.nullcontext
        terminal.key.side_effect = ["\x1b[B", " ", "\x1b[A", "s", ".", "a", "a", "s", ".", None, "r", "q"]
        with patch.object(menu, "Terminal", return_value=terminal), \
                patch.object(menu.shutil, "get_terminal_size", return_value=os.terminal_size((200, 24))), \
                contextlib.redirect_stdout(io.StringIO()):
            menu.run("en")
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual(calls, [["--desk", str(d.root), "up"] for d in (self.second, self.first)])
        screens = [call.args[0] for call in terminal.write.call_args_list]
        self.assertNotIn("[x]", screens[0])
        self.assertIn(">[x]", screens[2])
        self.assertNotIn("[x]", screens[4])
        self.assertEqual(screens[5].count("[x]"), 2)
        self.assertNotIn("[x]", screens[6])
        self.assertEqual(len(screens), 10)
        self.assertEqual(sum(call.args == (5,) for call in terminal.key.call_args_list), 10)
        self.assertFalse(self.tmux_calls.exists())

    def test_loop_empty_broken_lists_and_eof_exit_without_actions(self):
        for broken in (False, True):
            self.list_desks()
            if broken:
                desks_file().write_text("broken list")
            terminal = Mock()
            terminal.opened = lambda: contextlib.nullcontext(terminal)
            terminal.key.side_effect = ["s", "a", "r", "q"]
            with patch.object(menu, "Terminal", return_value=terminal), patch.object(menu, "perform") as perform:
                menu.run("en")
            perform.assert_not_called()
            self.assertEqual(terminal.write.call_count, 4)
        terminal.key.side_effect = EOFError
        with patch.object(menu, "Terminal", return_value=terminal):
            menu.run()
        self.assertFalse(self.calls.exists())

    def test_stream_actions_return_after_interrupt_and_web_passes_output_directly(self):
        row = self.state()["desks"][0]
        row["server"] = "up"
        terminal = Mock()
        terminal.outside = contextlib.nullcontext
        for key in ("p", "w", "s"):
            with self.subTest(key=key), patch.object(menu.subprocess, "call", side_effect=KeyboardInterrupt), \
                    patch.object(up, "listed_desk_up", side_effect=KeyboardInterrupt), \
                    contextlib.redirect_stdout(io.StringIO()):
                menu.perform(key, row, [row], terminal, "en")
            terminal.key.assert_not_called()
        with patch.object(menu.subprocess, "call", return_value=0) as call:
            menu.perform("w", row, [row], terminal, "en")
        self.assertEqual(call.call_args.args[0], [str(self.hansa), "--desk", str(self.first.root), "web"])
        self.assertEqual(set(call.call_args.kwargs), {"env"})
        terminal.key.assert_not_called()
        terminal.key.side_effect = KeyboardInterrupt
        with patch.object(menu.subprocess, "call", return_value=0), contextlib.redirect_stdout(io.StringIO()):
            menu.perform("p", row, [row], terminal, "en")
        terminal.key.assert_called_once()

    def test_loop_uses_actual_terminal_width_instead_of_stale_columns(self):
        terminal = Mock()
        terminal.opened = lambda: contextlib.nullcontext(terminal)
        terminal.key.return_value = "q"
        with patch.object(menu, "Terminal", return_value=terminal), patch.dict(os.environ, {"COLUMNS": "999"}), \
                patch.object(menu.os, "get_terminal_size", return_value=os.terminal_size((40, 24))):
            menu.run("zh")
        text = terminal.write.call_args.args[0].replace("\x1b[H\x1b[2J", "")
        self.assertTrue(all(menu.cells(line) <= 40 for line in text.splitlines()))

    def test_utf8_output_and_terminal_restoration_on_an_error_without_a_pty(self):
        buffer = io.BytesIO()
        output = io.TextIOWrapper(buffer, encoding="ascii")
        try:
            with patch.object(sys, "stdout", output):
                menu.Terminal().write("工作台")
            self.assertEqual(buffer.getvalue().decode("utf-8"), "工作台")
            attrs = [0, 0, 0, termios.ECHO | termios.ICANON | termios.ISIG, 0, 0, [0] * 32]
            terminal = menu.Terminal()
            with patch.object(menu.termios, "tcgetattr", side_effect=lambda fd: copy.deepcopy(attrs)), \
                    patch.object(menu.termios, "tcsetattr") as set_attrs, patch.object(terminal, "write") as write:
                with self.assertRaisesRegex(RuntimeError, "draw"), terminal.opened():
                    raise RuntimeError("draw")
            self.assertEqual(set_attrs.call_count, 2)
            self.assertEqual(set_attrs.call_args.args, (0, termios.TCSANOW, attrs))
            self.assertEqual(write.call_args.args, ("\x1b[?25h\x1b[?1049l",))
            terminal = Mock()
            terminal.opened = lambda: contextlib.nullcontext(terminal)
            terminal.key.return_value = "q"
            with patch.object(sys, "stdout", output), patch.object(menu, "Terminal", return_value=terminal):
                menu.run("zh")
            self.assertEqual(output.encoding, "utf-8")
        finally:
            output.close()

    def pty_process(self, source=None):
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 200, 0, 0))
        before = termios.tcgetattr(slave)

        def controlling_terminal():
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        command = ([sys.executable, "-c", source] if source else
                   [str(Path(cli.__file__).resolve().parent.parent / "bin/hansa")])
        try:
            proc = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave, cwd=self.tmp,
                                    env={**os.environ, "PYTHONPATH": str(Path(cli.__file__).resolve().parent.parent)},
                                    preexec_fn=controlling_terminal)
        except BaseException:
            os.close(master)
            os.close(slave)
            raise
        return proc, master, slave, before

    def pty_problem(self):
        try:
            master, slave = os.openpty()
        except PermissionError as e:
            return f"sandbox refuses a pseudo-terminal: {e}"
        os.close(master)
        os.close(slave)
        return ""

    def receive(self, master, needle):
        output, end = b"", time.monotonic() + 20
        while needle not in output:
            self.assertLess(time.monotonic(), end, repr(output))
            if select.select([master], [], [], max(0, end - time.monotonic()))[0]:
                output += os.read(master, 65536)
        return output

    def close_pty(self, proc, master, slave):
        try:
            if proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=15)
        finally:
            os.close(master)
            os.close(slave)

    def test_pty_arrows_tick_start_only_one_unticked_desk_unchanged_and_restore(self):
        why = self.pty_problem()
        if why:
            skip_in_sandbox(self, why)
        before_files = self.snapshot(self.first)
        proc, master, slave, before = self.pty_process()
        try:
            opening = self.receive(master, b"q quit")
            self.assertNotIn(b"[x]", opening)
            self.assertFalse(self.calls.exists())
            self.assertFalse(self.tmux_calls.exists())
            os.write(master, b"\x1b[B")
            self.receive(master, b"q quit")
            os.write(master, b" ")
            self.assertIn(b">[x]", self.receive(master, b"q quit"))
            os.write(master, b"\x1b[A")
            self.receive(master, b"q quit")
            os.write(master, b"s")
            self.receive(master, b"Press any key")
            self.assertEqual(json.loads(self.calls.read_text()), ["--desk", str(self.second.root), "up"])
            self.assertEqual(before_files, self.snapshot(self.first))
            self.assertFalse(self.tmux_calls.exists())
            os.write(master, b".")
            self.assertNotIn(b"[x]", self.receive(master, b"q quit"))
            os.write(master, b"q")
            self.assertEqual(proc.wait(timeout=15), 0)
            self.assertEqual(termios.tcgetattr(slave), before)
            self.receive(master, b"\x1b[?25h\x1b[?1049l")
        finally:
            self.close_pty(proc, master, slave)

    def test_pty_ctrl_c_during_command_returns_then_ctrl_c_exits_restored(self):
        why = self.pty_problem()
        if why:
            skip_in_sandbox(self, why)
        with patch.dict(os.environ, {"MENU_TEST_WAIT": "1"}):
            proc, master, slave, before = self.pty_process()
        try:
            self.receive(master, b"q quit")
            os.write(master, b"p")
            self.receive(master, b"command is waiting")
            os.write(master, b"\x03")
            returned = self.receive(master, b"q quit")
            self.assertIn(b"command interrupted", returned)
            self.assertIsNone(proc.poll())
            os.write(master, b"\x03")
            self.assertEqual(proc.wait(timeout=15), 0)
            self.assertEqual(termios.tcgetattr(slave), before)
        finally:
            self.close_pty(proc, master, slave)

    def test_pty_drawing_error_restores_and_connection_drop_exits(self):
        why = self.pty_problem()
        if why:
            skip_in_sandbox(self, why)
        proc, master, slave, before = self.pty_process(
            "from hansa import menu\n"
            "def broken(): raise RuntimeError('drawing failed')\n"
            "menu.rows = broken\nmenu.run()")
        try:
            self.assertNotEqual(proc.wait(timeout=15), 0)
            self.assertEqual(termios.tcgetattr(slave), before)
            self.receive(master, b"\x1b[?25h\x1b[?1049l")
        finally:
            self.close_pty(proc, master, slave)
        proc, master, slave, _ = self.pty_process()
        try:
            self.receive(master, b"q quit")
            os.close(master)
            master = None
            proc.wait(timeout=15)
        finally:
            if master is None:
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=15)
                os.close(slave)
            else:
                self.close_pty(proc, master, slave)
