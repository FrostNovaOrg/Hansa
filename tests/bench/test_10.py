"""Shipped acceptance checks with fake admin observations; no sockets or VM."""
import base64
import contextlib
import io
import json
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch
from hansa.bench import archive, guest, probes as p


class BenchProbesTest(unittest.TestCase):
    def test_detached_survivor_fails(self):
        process = NS(name='42', stat=lambda: NS(st_uid=1234))
        paths = lambda name, *args: NS(iterdir=lambda: [process] if name == '/proc' else [])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            exec(p.INSPECT, dict(Path=paths, pwd=NS(getpwnam=lambda _: NS(pw_uid=1234)), json=json))
        evidence = dict(json.loads(output.getvalue()), started=True)
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
        artifacts = {'browser.png': png, 'evidence.txt': b'bench artifact\n'}
        self.assertEqual(set(p.checks('sandbox', evidence, artifacts).values()), {'passed'})
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
        artifacts = {'browser.png': png, 'evidence.txt': b'bench artifact\n'}
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
