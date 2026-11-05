"""Shipped acceptance checks with fake admin observations; no sockets or VM."""
import base64
import contextlib
import errno
import io
import json
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch
from hansa.bench import archive, guest, probes as p, selftest


class BenchProbesTest(unittest.TestCase):
    def test_edge_positive_control_is_narrow_and_cleans_up(self):
        targets = dict(host=['192.168.5.2', 12001], lan=['192.0.2.1', 12002])
        self.assertEqual(p.fixtures()['edge-control'][0], 'root')
        for failure in ('', 'unreachable', 'connect', 'insert', 'delete', 'retained'):
            rules, reached = [], []
            def run(*args):
                if args[1] == 'insert':
                    self.assertIn(list(args[11:12]) + [int(args[14])], list(targets.values()))
                    self.assertEqual(args[2:11], ('rule', 'inet', 'bench_edge', 'out', 'meta', 'skuid', '0', 'ip', 'daddr'))
                    self.assertEqual(args[12:14] + args[15:], ('tcp', 'dport', 'accept', 'comment', '"bench-selftest"'))
                    rules.append(dict(rule=dict(handle=42, comment='bench-selftest')))
                    if failure == 'insert': raise RuntimeError('insert')
                elif args[1] == 'delete':
                    self.assertEqual(args[2:], ('rule', 'inet', 'bench_edge', 'out', 'handle', '42'))
                    if failure == 'delete': raise RuntimeError('delete')
                    if failure != 'retained': rules.clear()
                else:
                    self.assertEqual(args[1:], ('-a', '-j', 'list', 'chain', 'inet', 'bench_edge', 'out'))
                    return json.dumps(dict(nftables=rules))
            def connected(target):
                self.assertTrue(rules); reached.append(list(target))
                if failure == 'connect': raise RuntimeError('connect')
                return failure != 'unreachable'
            with self.subTest(failure=failure), contextlib.redirect_stdout(io.StringIO()) as output:
                environment = dict(json=json, sys=NS(argv=['probe', json.dumps(targets)]), run=run, connected=connected)
                if failure in ('connect', 'insert', 'delete', 'retained'):
                    with self.assertRaises(RuntimeError): exec(p.EDGE_CONTROL, environment)
                else:
                    exec(p.EDGE_CONTROL, environment)
                    self.assertEqual(reached, list(targets.values()))
                    self.assertEqual(set(p.checks('edge-control', json.loads(output.getvalue())).values()),
                                     {'failed'} if failure else {'passed'})
                self.assertEqual(bool(rules), failure in ('delete', 'retained'))

    def test_disk_limit_requires_real_enospc(self):
        for error, shared, late in ((errno.ENOSPC, True, False), (errno.EACCES, True, False),
                                    (None, True, False), (errno.ENOSPC, False, False), (errno.ENOSPC, True, True)):
            writes = []
            def write(data):
                writes.append(len(data))
                if len(writes) == 257 and error and not late: raise OSError(error, 'fixture')
                return len(data)
            def sync(fd):
                if len(writes) == 257 and late: raise OSError(error, 'fixture')
            opened = Mock(side_effect=lambda *a, **kw: contextlib.nullcontext(NS(write=write, fileno=lambda: 1)))
            stat = lambda path: NS(st_dev=1 if path == '/etc' or not shared else 2)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                exec(p.DISK_LIMIT, dict(os=NS(stat=stat, fsync=sync), open=opened, json=json))
            self.assertEqual(writes, [16 * 1024**2] * 257)
            self.assertEqual([c.args[0] for c in opened.call_args_list],
                             ['/work/fill', '/out/fill', '/tmp/fill', '/dev/shm/fill'])
            checked = p.checks('disk_limit', json.loads(output.getvalue()))
            self.assertEqual(set(checked.values()) == {'passed'}, error == errno.ENOSPC and shared and not late)
        self.assertEqual(p.fixtures()['disk_limit'][0], 'submission')
        for boot in (1, 2):
            self.assertEqual(set(selftest.mandatory()[str(boot) + '/disk_limit']), {'shared', 'enospc', 'execution'})

    def test_detached_survivor_fails(self):
        process = NS(name='42', stat=lambda: NS(st_uid=1234), joinpath=lambda _: NS(read_text=lambda: 'systemd\n'))
        paths = lambda name, *args: NS(iterdir=lambda: [process] if name == '/proc' else [])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            exec(p.INSPECT, dict(Path=paths, pwd=NS(getpwnam=lambda _: NS(pw_uid=1234)), json=json))
        evidence = dict(json.loads(output.getvalue()), started=True)
        self.assertEqual(evidence['survivors'], [dict(pid=42, command='systemd')])
        for mode in ('normal', 'timeout', 'disconnect', 'gateway-death'):
            name = 'descendant-' + mode
            self.assertEqual(p.checks(name, evidence)['survivors'], 'failed')
            clean = dict(evidence, survivors=[])
            self.assertEqual(set(p.checks(name, clean).values()), {'passed'})
            self.assertEqual(p.checks('inspect', dict(clean, slots=['slot-dirty']))['slots'], 'failed')
            self.assertEqual(p.checks(name, dict(clean, started=None))['started'], 'incomplete')
        spawn = Mock()
        exec(p.DESCENDANT, dict(subprocess=NS(run=spawn, DEVNULL=-1), Path=lambda _: NS(exists=lambda: True), time=NS(monotonic=lambda: 0)))
        child = spawn.call_args[0][0][3]
        self.assertIn('os.setsid()', child)
        self.assertEqual(child.count('os.fork()'), 2)

    def test_missing_browser_evidence_fails(self):
        png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=')
        evidence = dict(p.EXPECTED['sandbox'])
        artifacts = {'browser.png': png, 'evidence.txt': b'bench artifact\n', 'bench-leftover': b'leftover'}
        self.assertEqual(set(p.checks('sandbox', evidence, artifacts).values()), {'passed'})
        for bad in (b'', b'changed'):
            self.assertEqual(p.checks('sandbox', evidence, dict(artifacts, **{'bench-leftover': bad}))['artifacts'], 'failed')
        self.assertEqual(p.checks('sandbox', evidence, {k: v for k, v in artifacts.items() if k != 'bench-leftover'})['artifacts'], 'failed')
        for bad in (b'', png[:24], b'not a screenshot'):
            self.assertEqual(p.checks('sandbox', evidence, dict(artifacts, **{'browser.png': bad}))['artifacts'], 'failed')
        self.assertEqual(p.checks('sandbox', evidence, {})['artifacts'], 'failed')
        self.assertEqual(p.checks('sandbox', evidence)['artifacts'], 'incomplete')
        for field in evidence:
            self.assertEqual(p.checks('sandbox', dict(evidence, **{field: None}), artifacts)[field], 'incomplete')
        self.assertEqual(p.checks('sandbox', dict(evidence, browser=False), artifacts)['browser'], 'failed')

    def test_wrong_firewall_order_fails(self):
        recipe = (Path(__file__).resolve().parents[2] / 'contrib/bench/edge.nft').read_text()
        live = recipe[recipe.index('table inet'):].replace('counter drop', 'counter packets 7 bytes 420 drop # handle 2')
        for uid in ('"bench"', '1234'):
            good = live.replace('"bench"', uid)
            self.assertTrue(p.firewall(good, 1234))
            self.assertTrue(p.firewall(good.replace('172.16.0.0/12, 192.168.0.0/16', '192.168.0.0/16, 172.16.0.0/12'), 1234))
            wall, loopback = good.splitlines()[3:5]
            self.assertFalse(p.firewall(good.replace(wall + '\n' + loopback, loopback + '\n' + wall), 1234))
            self.assertFalse(p.firewall(good.replace('counter packets 7 bytes 420 drop', 'accept'), 1234))
            self.assertFalse(p.firewall(good.replace('established,related accept', 'established,related drop'), 1234))
        self.assertEqual(p.checks('root-edge', {})['firewall'], 'incomplete')
        environment = dict(json=json, sys=NS(argv=['probe']), os=NS(getuid=lambda: 0),
                           pwd=NS(getpwnam=lambda _: NS(pw_uid=1234)), dns=lambda: True,
                           connected=lambda _: True, Path=lambda _: NS(read_text=lambda: '1'),
                           run=lambda *a: live if a[0] == 'nft' else 'enabled')
        with patch('urllib.request.build_opener') as opener, contextlib.redirect_stdout(io.StringIO()) as output:
            opener.return_value.open.return_value.__enter__.return_value.status = 200
            exec(p.EDGE, environment)
        checked = p.checks('root-edge', json.loads(output.getvalue()))
        self.assertEqual(checked['firewall'], 'passed')
        self.assertEqual(checked['host'], 'incomplete')  # No controlled listener supplied.

    def test_measured_nft_output(self):
        live = '''table inet bench_edge {
 chain out { # handle 1
  type filter hook output priority filter; policy accept;
  meta skuid 1000 counter packets 0 bytes 0 drop # handle 2
  oifname "lo" accept # handle 3
  ct state established,related accept # handle 4
  ip daddr 192.168.5.2 udp dport 53 accept # handle 5
  ip daddr 192.168.5.2 tcp dport 53 accept # handle 6
  udp sport 68 udp dport 67 accept # handle 7
  ip daddr 192.168.5.2 drop # handle 8
  ip daddr { 10.0.0.0/8, 100.64.0.0/10, 169.254.0.0/16, 172.16.0.0/12, 192.168.0.0/16, 224.0.0.0/3 } drop # handle 10
  meta nfproto ipv6 drop # handle 11
 }
}'''
        numeric = live.replace('priority filter', 'priority 0').replace('established,related', '0x2,0x4').replace('nfproto ipv6', 'nfproto 10')
        run = Mock(side_effect=lambda *args: numeric if '-n' in args else live)
        environment = dict(json=json, sys=NS(argv=['probe']), os=NS(getuid=lambda: 0), dns=lambda: True,
                           pwd=NS(getpwnam=lambda _: NS(pw_uid=1000)), connected=lambda _: True,
                           Path=lambda _: NS(read_text=lambda: '1'), run=run)
        with patch('urllib.request.build_opener'), contextlib.redirect_stdout(io.StringIO()) as output:
            exec(p.EDGE, environment)
        self.assertEqual(p.checks('root-edge', json.loads(output.getvalue()))['firewall'], 'passed')
        for rule in live.splitlines()[3:-2]:
            for changed in (live.replace(rule, ''), live.replace(rule, rule.replace('accept', 'drop') if 'accept' in rule else rule.replace('drop', 'accept')), live.replace(rule, rule + '\n' + rule)):
                self.assertFalse(p.firewall(changed, 1000))

    def test_fixture_contract(self):
        for name, (scope, source) in p.fixtures().items():
            compile(source, name, 'exec')
            self.assertIn(scope, ('submission', 'root', 'bench'))
        for frame in p.malformed().values():
            with self.assertRaises(ValueError):
                request = guest._header(io.BytesIO(frame))
                archive.unpack(io.BytesIO(), request['length'], None)
        for name in ('account', 'bench-edge', 'inspect'):
            evidence = dict(p.EXPECTED[name])
            self.assertEqual(set(p.checks(name, evidence).values()), {'passed'})
            for key in evidence:
                self.assertEqual(p.checks(name, dict(evidence, **{key: None}))[key], 'incomplete')
        self.assertEqual(set(p.checks('caps-output', dict(truncated=True, rejected=False, output=1024**2), {}).values()), {'passed'})
        self.assertEqual(p.checks('caps-count', dict(truncated=False, rejected=True), {})['artifacts'], 'failed')

    def test_guest_shapes_fail_closed(self):
        live = 'table inet' + (Path(__file__).resolve().parents[2] / 'contrib/bench/edge.nft').read_text().split('table inet', 1)[1]
        for name, evidence in ((None, {}), ('unknown', {}), ('caps-unknown', {}), ('descendant-unknown', {}),
                               ('inspect', []), ('inspect', None), ('inspect', 'passed'),
                               ('root-edge', dict(firewall=[], uid=1234)),
                               ('root-edge', dict(firewall=live, uid='1234'))):
            with self.subTest(name=name, evidence=evidence):
                self.assertIn('failed', p.checks(name, evidence).values())

    def test_png_end_dimensions_and_extra_artifacts(self):
        png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=')
        artifacts = {'browser.png': png, 'evidence.txt': b'bench artifact\n', 'bench-leftover': b'leftover'}
        self.assertGreaterEqual(len(png[:-12]), 45)
        for bad in (png[:-12], png[:16] + bytes(4) + png[20:], png[:20] + bytes(4) + png[24:]):
            with self.subTest(png=bad):
                self.assertEqual(p.checks('sandbox', p.EXPECTED['sandbox'],
                                         dict(artifacts, **{'browser.png': bad}))['artifacts'], 'failed')
        self.assertEqual(p.checks('sandbox', p.EXPECTED['sandbox'], dict(artifacts, extra=b'x'))['artifacts'], 'failed')

    def test_each_cap_rejects_wrong_evidence(self):
        block = bytes(16 * 1024**2)
        for cap in p.CAPS:
            name = 'caps-' + cap
            evidence = dict(truncated=cap == 'output', rejected=cap != 'output', output=1024**2)
            artifacts = ({str(n): block for n in range(4)} if cap == 'total' else
                         {str(n): b'x' for n in range(1000)} if cap == 'count' else {})
            self.assertEqual(set(p.checks(name, evidence, artifacts).values()), {'passed'})
            for field in ('truncated', 'rejected') + (('output',) if cap == 'output' else ()):
                with self.subTest(cap=cap, field=field):
                    wrong = 0 if field == 'output' else not evidence[field]
                    self.assertEqual(p.checks(name, dict(evidence, **{field: wrong}), artifacts)[field], 'failed')
            with self.subTest(cap=cap, field='artifacts'):
                self.assertEqual(p.checks(name, evidence, dict(artifacts, extra=b'x'))['artifacts'], 'failed')
