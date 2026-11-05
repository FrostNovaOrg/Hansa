"""Offline selftest dispatch and evidence gates; every external call is fake."""
import contextlib
import io
import json
import itertools
import os
import sys
import subprocess
import tarfile
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
        machine = Mock(clock=lambda: 0, private=self.desk.state_dir / 'bench', redact=self.desk.redact)
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
        def submission(machine, name, source, deadline, observed=None):
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
            self.assertEqual(request['script'], 'python3 -I -c ' + selftest.shlex.quote(probes.fixtures()[name][1]))
            self.assertEqual(source.read(request['length']), bytes(1024))
            with archive.collect(fd, dict(id=request['id'], status='ok', exit=0), stdout) as returned:
                data = returned.read()
            header = json.dumps(dict(version=1, id=request['id'], length=len(data))).encode()
            target.write(len(header).to_bytes(4, 'big') + header + data)
        with contextlib.ExitStack() as stack:
            import os
            fd = os.open(self.desk.root, os.O_RDONLY); stack.callback(os.close, fd)
            for name, stdout in (('caps-output', b'x' * (archive.OUTPUT_BYTES + 1)),
                                 ('disk_limit', b'{"shared": true, "enospc": true}')):
                checked = selftest.submission(Mock(connect=connect), name, probes.fixtures()[name][1], 300)
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
        self.assertEqual(json.loads(report['results']['stopped']['evidence'])['status'], 'Running')
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

    @contextlib.contextmanager
    def observations(self, ports=(2200, 2200), keys=(b'ssh-ed25519 fixture',) * 2, dhcp='passed'):
        def runner(machine):
            original = machine._call.side_effect
            key = iter(keys)
            machine.clock = Mock(side_effect=itertools.count(step=2.5))
            machine.start.side_effect = iter((ports[0], 2300, ports[1], 2301))
            def call(command, deadline, **streams):
                if command == ['limactl', '--version']: return Mock(stdout=b'limactl version fixture\n')
                if command[-1] == '/etc/ssh/ssh_host_ed25519_key.pub': return Mock(stdout=next(key))
                if selftest.DHCP in command: return Mock(stdout=json.dumps(dict(renewed=dhcp)).encode())
                return original(command, deadline, **streams)
            machine._call.side_effect = call
            return selftest.run(machine)
        with patch.object(self, 'runner', side_effect=runner):
            yield

    def response(self, result=None, artifacts=None, **fields):
        data = dict(id='a' * 32, status='ok', exit=0)
        data.update(result or {})
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w') as tar:
            for name, value in dict({'result.json': json.dumps(data).encode(), 'stdout': b'', 'stderr': b''},
                                    **{'artifacts/' + k: v for k, v in (artifacts or {}).items()}).items():
                member = tarfile.TarInfo(name); member.size = len(value)
                tar.addfile(member, io.BytesIO(value))
        body = stream.getvalue()
        header = json.dumps(dict(dict(version=1, id='a' * 32, length=len(body)), **fields)).encode()
        return len(header).to_bytes(4, 'big') + header + body

    def supervised(self, name, evidence, returned=b''):
        def call(command, deadline, **streams):
            self.assertEqual(command[:-1], ['limactl', 'shell', selftest.INSTANCE, 'sudo', '-n', '-u',
                                           'root', '--', 'python3', '-I', '-c', selftest.SUPERVISE])
            wire, mode, inspect = json.loads(command[-1])
            self.assertEqual(mode, name.removeprefix('descendant-'))
            self.assertEqual(inspect, probes.fixtures()['inspect'][1])
            if name in probes.malformed(): self.assertEqual(bytes.fromhex(wire), probes.malformed()[name])
            streams['stdout'].write(json.dumps(evidence).encode() + b'\n' + returned)
        machine = Mock(); machine._call.side_effect = call
        checked = selftest.submission(machine, name, '', 300)
        machine._call.assert_called_once()
        return checked

    def test_each_malformed_frame_rejects_bad_supervision(self):
        good = dict(gateway_exit=1, survivors=[], slots=[])
        for name in probes.malformed():
            self.assertEqual(set(self.supervised(name, good).values()), {'passed'})
            for change, tail, field in ((dict(gateway_exit=0), b'', 'rejected'), ({}, b'x', 'rejected'),
                                        (dict(survivors=[42]), b'', 'survivors'), (dict(slots=['dirty']), b'', 'slots')):
                with self.subTest(name=name, field=field, change=change):
                    self.assertEqual(self.supervised(name, dict(good, **change), tail)[field], 'failed')

    def test_descendant_supervision_cannot_hide_survivors(self):
        for mode in ('normal', 'timeout', 'disconnect', 'gateway-death'):
            returned = self.response(dict(status='timeout' if mode == 'timeout' else 'ok'), {'descendant.ready': b'ready'})
            evidence = dict(survivors=[42], slots=[], started=True, gateway_exit=0)
            with self.subTest(mode=mode):
                checked = self.supervised('descendant-' + mode, evidence, returned)
                self.assertEqual(checked['survivors'], 'failed')
                self.assertEqual(checked['started'], 'passed')
                evidence.update(survivors=[], started=False)
                returned = self.response(dict(status='timeout' if mode == 'timeout' else 'ok'))
                self.assertEqual(self.supervised('descendant-' + mode, evidence, returned)['started'], 'failed')

    def test_return_identity_length_and_execution_are_checked(self):
        evidence = dict(survivors=[], started=True)
        for returned, reason in ((self.response(id='b' * 32), 'identity'), (self.response(version=2), 'identity'),
                                 (self.response() + b'unclaimed bytes', 'length')):
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                self.supervised('descendant-normal', evidence, returned)
        for mode, result in (('normal', dict(id='b' * 32)), ('normal', dict(status='timeout')),
                             ('normal', dict(exit=1)), ('timeout', dict(status='ok'))):
            with self.subTest(mode=mode, result=result):
                self.assertEqual(self.supervised('descendant-' + mode, evidence,
                    self.response(result, {'descendant.ready': b'ready'}))['execution'], 'failed')

    def test_memory_return_requires_owned_processes_to_exit(self):
        now = [0]
        machine = Mock(clock=lambda: now[0], private=self.desk.state_dir / 'bench')
        machine.start.side_effect = lambda _: setattr(machine, 'port', 2200)
        machine.stop.side_effect = lambda _: setattr(machine, 'port', None)
        machine._info.side_effect = lambda _: dict(status='Running' if machine.port else 'Stopped')
        parent = '1 0 1024 ' + str(machine.private / 'lima' / selftest.INSTANCE) + '\n'
        child = '2 1 512 vm\n'
        for remaining in (parent, child):
            with self.subTest(remaining=remaining):
                machine.port = None
                machine._call.side_effect = lambda *a, **k: Mock(stdout=(parent + child if machine.port else remaining).encode())
                with patch.object(selftest.time, 'sleep', side_effect=lambda _: now.__setitem__(0, now[0] + 30)):
                    observed = {}; checked = selftest.lifecycle(machine, observed)
                self.assertEqual(checked, dict(queue='passed', restart='passed', idle='passed', memory='failed'))
                self.assertEqual((observed['owned'], observed['remaining']), ([1, 2], [int(remaining.split()[0])]))

    def test_boot_ports_and_keys_use_each_boot(self):
        with self.observations():
            self.assertEqual(self.acceptance()['status'], 'passed')  # Reusing a valid port is allowed.
        for ports, keys, field in (((2200, None), (b'key', b'key'), 'ports'),
                                   ((None, 2200), (b'key', b'key'), 'ports'),
                                   ((2200, True), (b'key', b'key'), 'ports'),
                                   ((2200, 0), (b'key', b'key'), 'ports'),
                                   ((2200, 65536), (b'key', b'key'), 'ports'),
                                   ((2200, 2200), (b'key', b'changed'), 'keys'),
                                   ((2200, 2201), (b'', b''), 'keys')):
            with self.subTest(ports=ports, keys=keys), self.observations(ports, keys):
                report = self.acceptance()
                self.assertEqual(report['results']['boots']['checks'][field], 'failed')
                self.assertEqual(json.loads(report['results']['boots']['evidence']), dict(ports=list(ports), keys=[k.decode().split() for k in keys]))
                self.assertEqual(report['status'], 'failed')

    def test_dhcp_dispatch_versions_and_probe_seconds(self):
        with self.observations(dhcp='failed'):
            report = self.acceptance()
        self.assertEqual(report['versions']['lima'], 'limactl version fixture\n')
        for boot in (1, 2):
            self.assertEqual(report['results'][str(boot) + '/dhcp']['checks'], dict(renewed='failed'))
            self.assertEqual(report['results'][str(boot) + '/dhcp']['status'], 'failed')
            self.assertEqual(json.loads(report['results'][str(boot) + '/dhcp']['evidence']), dict(probe=dict(renewed='failed')))
        for name, result in report['results'].items():
            with self.subTest(probe=name):
                self.assertEqual(result['seconds'], 7.5 if name == 'stopped' else 2.5)
        self.assertEqual(report['status'], 'failed')

    def test_dhcp_reconfigures_every_lease(self):
        folder = self.desk.root / 'leases'; folder.mkdir()
        leases = [folder / n for n in ('2', '3')]
        for fault in ('', 'stale', 'address'):
            for lease in leases: lease.write_text('ADDRESS=192.0.2.15\n'); os.utime(lease, ns=(1, 1))
            def networkctl(command, **kwargs):
                self.assertEqual(command[2:], [p.name for p in folder.iterdir()])
                if command[:2] == ['networkctl', 'reconfigure']:
                    for lease in leases:
                        if fault == 'stale' and lease == leases[-1]: continue
                        lease.write_text('SERVER=192.0.2.1' if fault == 'address' else 'ADDRESS=192.0.2.15')
                        os.utime(lease, ns=(1000000001, 1000000001))
            with self.subTest(fault=fault), patch('pathlib.Path', return_value=folder), \
                    patch('subprocess.run', side_effect=networkctl), patch('time.monotonic', side_effect=range(0, 150, 30)), \
                    patch('time.sleep'), contextlib.redirect_stdout(io.StringIO()) as output:
                if fault:
                    with self.assertRaises(TimeoutError): exec(selftest.DHCP, {})
                else:
                    exec(selftest.DHCP, {})
                    self.assertEqual(json.loads(output.getvalue()), dict(renewed='passed'))

    def test_failed_and_incomplete_results_keep_bounded_redacted_evidence(self):
        self.command('build')
        secret = 'fixture-private-"provider-key'
        self.desk.secrets_dir.mkdir(exist_ok=True); self.desk.key_file('fixture').write_text(secret)
        observation = dict(note=secret, survivors=[dict(pid=42, command=secret)], padding='')
        observation['padding'] = 'x' * (4083 - len(json.dumps(dict(probe=observation)))) + secret + 'z' * 4096
        submit = selftest.submission
        def runner(machine):
            call, fake = machine._call.side_effect, selftest.submission
            machine._call.side_effect = lambda command, *a, **kw: Mock(stdout=json.dumps(observation).encode()) if probes.fixtures()['account'][1] in command else call(command, *a, **kw)
            def submission(machine, name, source, deadline, observed=None):
                if name != 'caps-file': return fake(machine, name, source, deadline, observed)
                response = self.response(dict(rejected=True, truncated=False), {secret: b'artifact bytes stay out'})
                def connect(incoming, returned, deadline):
                    incoming.read(int.from_bytes(incoming.read(4), 'big') + 1024); returned.write(response)
                return submit(Mock(connect=connect), name, source, deadline, observed)
            with patch.object(selftest, 'submission', side_effect=submission): return BenchSelftestTest.runner(machine)
        with patch.object(self, 'runner', side_effect=runner), patch.object(selftest, 'run', side_effect=lambda _: self.acceptance()):
            report = json.loads(self.command('selftest', '--json'))['acceptance']
        for boot in (1, 2):
            account, caps = (report['results'][str(boot) + '/' + n] for n in ('account', 'caps-file'))
            self.assertEqual((account['status'], caps['status']), ('incomplete', 'failed'), caps)
            self.assertLessEqual(len(account['evidence'].encode()), 4096)
            self.assertEqual(account['evidence'].count('[key of fixture]'), 3)
            self.assertNotIn('fixture-private', account['evidence'])
            self.assertEqual(json.loads(caps['evidence'])['artifacts'], {'[key of fixture]': 23})
            self.assertNotIn('artifact bytes', caps['evidence'])
        self.assertFalse(selftest.complete(report))
        self.assertNotIn('evidence', report['results']['1/root-edge'])
        self.assertEqual(self.desk.settings()['bench']['acceptance'], report)
        with self.assertRaisesRegex(SystemExit, 'selftest'): self.command('on')

    def test_complete_rejects_error_despite_all_passes(self):
        report = self.acceptance()
        self.assertTrue(selftest.complete(report))
        report['error'] = 'stopped on an error'
        self.assertFalse(selftest.complete(report))

    def test_new_selftest_invalidates_saved_pass_before_running(self):
        self.command('build')
        with patch.object(selftest, 'run', side_effect=lambda _: self.acceptance()): self.command('selftest')
        self.assertEqual(self.desk.settings()['bench']['acceptance']['status'], 'passed')
        def interrupted(machine):
            self.assertEqual(self.desk.settings()['bench']['acceptance']['status'], 'incomplete')
            raise RuntimeError('interrupted fixture')
        with patch.object(selftest, 'run', side_effect=interrupted), self.assertRaisesRegex(SystemExit, 'interrupted'):
            self.command('selftest')
        self.assertEqual(self.desk.settings()['bench']['acceptance']['status'], 'incomplete')
        with self.assertRaisesRegex(SystemExit, 'selftest'): self.command('on')

    def test_controlled_listeners_are_bound_tested_and_forwarded(self):
        self.fail = unittest.TestCase.fail.__get__(self)  # Preserve assertion diagnostics from the shared fixture.
        def runner(machine):
            from unittest.mock import MagicMock
            host, lan, connection = MagicMock(), MagicMock(), MagicMock()
            for listener, address, port in ((host, '127.0.0.1', 12001), (lan, '192.0.2.1', 12002)):
                listener.__enter__.return_value = listener
                listener.getsockname.return_value = (address, port)
            with patch.object(selftest.socket, 'socket', side_effect=[host, lan]), \
                    patch.object(selftest.socket, 'create_connection', return_value=connection) as connect:
                report = selftest.run(machine)
            for listener, address, port in ((host, '127.0.0.1', 12001), (lan, '192.0.2.1', 12002)):
                listener.bind.assert_called_once_with((address, 0)); listener.listen.assert_called_once_with(16)
                listener.__exit__.assert_called_once()
                connect.assert_any_call((address, port), timeout=3)
            edges = [c.args[0] for c in machine._call.call_args_list if probes.fixtures()['root-edge'][1] in c.args[0]]
            self.assertEqual(len(edges), 4)
            for command in edges:
                self.assertEqual(json.loads(command[-1]), dict(host=['192.168.5.2', 12001], lan=['192.0.2.1', 12002]))
            controls = probes.fixtures()['edge-control'][1]
            sequence = [c.args[0] for c in machine._call.call_args_list if controls in c.args[0] or c.args[0] in edges]
            self.assertEqual([c[c.index('-c') + 1] for c in sequence], [controls, edges[0][-2], edges[0][-2]] * 2)
            for command in sequence[::3]:
                self.assertEqual(command[command.index('-u') + 1], 'root')
                self.assertEqual(command[-1], edges[0][-1])
            return report
        with patch.object(self, 'runner', side_effect=runner): self.assertEqual(self.acceptance()['status'], 'passed')
        with patch.object(selftest.socket, 'gethostname', side_effect=selftest.socket.gaierror('listener lookup failed')):
            self.assertEqual(self.acceptance()['status'], 'incomplete')

    def test_error_messages_are_saved_with_redaction(self):
        self.command('build')
        self.desk.secrets_dir.mkdir(exist_ok=True)
        secret = 'fixture-provider-secret'
        (self.desk.secrets_dir / 'fixture.key').write_text(secret)
        for target in ('gethostname', 'admin'):
            owner = selftest.socket if target == 'gethostname' else selftest
            with self.subTest(target=target), patch.object(owner, target, side_effect=selftest.socket.gaierror('lookup failed ' + secret)), \
                    patch.object(selftest, 'run', side_effect=lambda _: self.acceptance()):
                report = json.loads(self.command('selftest', '--json'))['acceptance']
            error = report['error'] if target == 'gethostname' else report['results']['1/edge-control']['error']
            self.assertIn('gaierror', error); self.assertIn('lookup failed', error)
            self.assertIn('[key of fixture]', error); self.assertNotIn(secret, json.dumps(report))
            self.assertEqual(self.desk.settings()['bench']['acceptance'], report)

    def test_unavailable_controlled_listener_blocks_acceptance(self):
        for stage in ('bind', 'listen', 'create_connection', 'lan'):
            def runner(machine):
                if stage == 'lan': selftest.socket.gethostbyname.return_value = '127.0.0.1'
                else:
                    failing = (selftest.socket.create_connection if stage == 'create_connection' else
                               getattr(selftest.socket.socket.return_value, stage))
                    failing.side_effect = OSError('controlled listener unavailable')
                return selftest.run(machine)
            with self.subTest(stage=stage), patch.object(self, 'runner', side_effect=runner):
                report = self.acceptance()
                self.assertEqual(report['status'], 'incomplete')
                self.assertIn('listener', report['error'])
                self.assertFalse(selftest.complete(report))

    def test_failed_edge_control_aborts_and_stops(self):
        for evidence in (dict(host=False, lan=True), dict(host=True, lan=False), {}, RuntimeError('cleanup failed')):
            def runner(machine):
                original = selftest.admin
                def admin(machine, source, *args, **kwargs):
                    if source == probes.fixtures()['edge-control'][1]:
                        if isinstance(evidence, Exception): raise evidence
                        return Mock(stdout=json.dumps(evidence).encode())
                    return original(machine, source, *args, **kwargs)
                with patch.object(selftest, 'admin', side_effect=admin): report = selftest.run(machine)
                machine.stop.assert_called_once()
                machine._info.assert_called_once()
                return report
            with self.subTest(evidence=evidence), patch.object(self, 'runner', side_effect=runner):
                report = self.acceptance()
                self.assertFalse(selftest.complete(report))
                self.assertIn('control', report['error'])
                self.assertEqual(report['results']['1/root-edge']['status'], 'incomplete')
