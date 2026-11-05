"""Offline selftest dispatch and evidence gates; every external call is fake."""
import contextlib
import io
import json
import sys
import subprocess
import unittest
from unittest.mock import Mock, patch
from hansa.bench import archive, manage, probes, selftest
from tests.bench import test_7


class BenchSelftestTest(unittest.TestCase):
    runner = staticmethod(selftest.run)
    setUp = test_7.BenchManagementTest.setUp
    command = test_7.BenchManagementTest.command
    run_command = test_7.BenchManagementTest.run_command

    def acceptance(self, missing=False, running=False):
        machine = Mock(clock=lambda: 0, private=self.desk.state_dir / 'bench')
        machine.start.side_effect = iter(range(2200, 2220))
        machine._info.return_value = dict(status='Running' if running else 'Stopped')
        def call(command, **kwargs):
            source = command[command.index('-c') + 1] if '-c' in command else ''
            evidence = next((dict(probes.EXPECTED[n]) for n, (_, s) in probes.fixtures().items() if s == source and n in probes.EXPECTED), {})
            if source == probes.fixtures()['root-edge'][1]:
                evidence = dict(probes.EXPECTED[command[command.index('-u') + 1] + '-edge'])
                evidence.update(firewall='table inet' + (manage.RECIPE / 'edge.nft').read_text().split('table inet', 1)[1], uid=1234)
            if source == selftest.DHCP: evidence = dict(renewed='passed')
            return subprocess.CompletedProcess(command, 0, json.dumps(evidence).encode())
        machine._call.side_effect = lambda command, deadline, **kwargs: call(command, **kwargs)
        listener = Mock(); listener.__enter__ = Mock(return_value=listener); listener.__exit__ = Mock(return_value=False)
        listener.getsockname.return_value = ('192.0.2.1', 12345)
        shipped = probes.fixtures()
        if missing: shipped.pop('account')
        def submission(machine, name, source, deadline):
            self.assertEqual(source, '' if name in probes.malformed() else shipped[name][1])
            return dict.fromkeys(selftest.mandatory()['1/' + name], 'passed')
        with patch.object(selftest.socket, 'socket', return_value=listener), patch.object(selftest.socket, 'create_connection', return_value=listener), patch.object(selftest.socket, 'gethostbyname', return_value='192.0.2.1'), patch.object(probes, 'fixtures', return_value=shipped), patch.object(selftest, 'submission', side_effect=submission), patch.object(selftest, 'lifecycle', return_value=dict.fromkeys(selftest.mandatory()['lifecycle'], 'passed')):
            return self.runner(machine)

    def test_missing_probe_blocks_on(self):
        self.command('build')
        for missing in (False, True):
            with patch.object(selftest, 'run', side_effect=lambda _: self.acceptance(missing=missing)):
                report = json.loads(self.command('selftest', '--json'))['acceptance']
            self.assertEqual(report['status'], 'incomplete' if missing else 'passed')
            self.assertEqual(set(report['results']), set(selftest.mandatory()))
            if missing:
                with self.assertRaisesRegex(SystemExit, 'selftest'): self.command('on')
            else:
                self.command('on'); self.command('off')
                report['results']['1/sandbox']['checks'].pop('artifacts')
                settings = self.desk.settings(); settings['bench']['acceptance'] = report; self.desk.save_settings(settings)
                with self.assertRaisesRegex(SystemExit, 'selftest'): self.command('on')
        with manage.exclusive(self.desk), self.assertRaisesRegex(SystemExit, 'busy'): self.command('selftest')
        settings = self.desk.settings(); settings['bench']['enabled'] = True; self.desk.save_settings(settings)
        with self.assertRaisesRegex(SystemExit, 'disabled'): self.command('selftest')

    def test_selftest_uses_shipped_code(self):
        from hansa.bench.guest import _header
        def connect(source, target, deadline):
            request = _header(source)
            self.assertEqual(request['script'], 'python3 -I -c ' + selftest.shlex.quote(probes.fixtures()['caps-output'][1]))
            self.assertEqual(source.read(request['length']), bytes(1024))
            with archive.collect(fd, dict(id=request['id'], status='ok', exit=0), b'x' * (archive.OUTPUT_BYTES + 1)) as returned:
                data = returned.read()
            header = json.dumps(dict(version=1, id=request['id'], length=len(data))).encode()
            target.write(len(header).to_bytes(4, 'big') + header + data)
        with contextlib.ExitStack() as stack:
            import os
            fd = os.open(self.desk.root, os.O_RDONLY); stack.callback(os.close, fd)
            checked = selftest.submission(Mock(connect=connect), 'caps-output', probes.fixtures()['caps-output'][1], 300)
        self.assertEqual(set(checked.values()), {'passed'})
        compile(selftest.SUPERVISE, '<admin>', 'exec'); compile(selftest.DHCP, '<dhcp>', 'exec')
        for mode in ('normal', 'timeout', 'disconnect', 'gateway-death'):
            child = Mock(stdin=io.BytesIO(), returncode=0); child.poll.return_value = 0
            inspect = "import json; print(json.dumps(dict(survivors=[], slots=[], ready=True)))"
            output = io.TextIOWrapper(io.BytesIO(), write_through=True)
            with patch.object(sys, 'argv', ['probe', json.dumps(['00', mode, inspect])]), patch('pwd.getpwnam', return_value=Mock(pw_uid=123, pw_gid=123)), patch.object(subprocess, 'Popen', return_value=child), contextlib.redirect_stdout(output):
                exec(selftest.SUPERVISE, {})
            self.assertEqual(child.kill.call_count, int(mode == 'gateway-death'))
            self.assertEqual(json.loads(output.buffer.getvalue())['started'], mode != 'normal')
            output.close()

    def test_running_vm_cannot_pass(self):
        self.command('build')
        with patch.object(selftest, 'run', side_effect=lambda _: self.acceptance(running=True)):
            report = json.loads(self.command('selftest', '--json'))['acceptance']
        self.assertEqual(report['results']['stopped']['status'], 'failed')
        self.assertEqual(report['status'], 'failed')
        with self.assertRaisesRegex(SystemExit, 'selftest'): self.command('on')
        now = [0]
        machine = Mock(clock=lambda: now[0], port=2200, private=self.desk.root)
        machine.start.side_effect = lambda _: setattr(machine, 'port', 2200)
        machine.stop.side_effect = lambda _: setattr(machine, 'port', None)
        machine._info.side_effect = lambda _: dict(status='Running' if machine.port else 'Stopped')
        machine._call.side_effect = lambda *a, **k: Mock(stdout=('1 0 1024 ' + str(machine.private / 'lima' / 'hansa-bench') + '\n2 1 512 vm\n').encode() if machine.port else b'')
        with patch.object(selftest.time, 'sleep', side_effect=lambda _: now.__setitem__(0, now[0] + 30)):
            self.assertEqual(set(selftest.lifecycle(machine).values()), {'passed'})
