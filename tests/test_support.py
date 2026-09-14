import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bellhop import support
from bellhop.config import Desk
from bellhop.util import BellhopError


class SupportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.source = self.base / 'source'; self.source.mkdir()
        (self.source / 'code.py').write_text('before\n')
        self.launcher = self.base / 'launcher'; self.launcher.write_text('#!/bin/sh\n'); self.launcher.chmod(0o700)
        self.desk = Desk(self.base / 'desk', {'support': {'enabled': True, 'root': str(self.base / 'jobs'),
                        'launcher': str(self.launcher), 'source_roots': [str(self.source)],
                        'apply_roots': [str(self.source)], 'commands': ['python3 -m unittest'], 'max_parallel': 2}, 'lanes': {}})
        self.spec = {'title': 'code fixture', 'prompt': 'change the assigned file', 'source_root': str(self.source),
                     'files': ['code.py'], 'writable_files': ['code.py'], 'mode': 'code'}

    def tearDown(self): self.tmp.cleanup()

    def make(self): return support.submit(self.desk, self.spec, start=False)

    def response(self, status='completed', error=None):
        return type('Result', (), {'returncode': 0, 'stdout': json.dumps({'status': status, 'error': error, 'body': 'complete report body', 'model': 'fixture', 'effort': 'medium'})})()

    def test_snapshot_and_new_declared_file(self):
        self.spec['writable_files'].append('new.py')
        j = self.make(); p = support.job_path(self.desk, j['id'])
        self.assertEqual((p / 'workspace/src/code.py').read_text(), 'before\n')
        self.assertFalse(j['inputs'][1]['existed'])

    def test_inputs_reject_private_traversal_symlink_and_arbitrary_command(self):
        for filename in ['../code.py', '.env', 'migrations_private/a.sql', '.claude/settings.json', '/etc/passwd']:
            with self.subTest(filename=filename), self.assertRaises(BellhopError):
                support.submit(self.desk, dict(self.spec, files=[filename]), start=False)
        (self.source / 'link.py').symlink_to(self.source / 'code.py')
        with self.assertRaises(BellhopError): support.submit(self.desk, dict(self.spec, files=['link.py']), start=False)
        with self.assertRaises(BellhopError): support.submit(self.desk, dict(self.spec, commands=['python3 -c dangerous']), start=False)

    def test_fallback_only_for_provider_failures_keeps_current_files(self):
        j = self.make(); path = support.job_path(self.desk, j['id'])
        (path / 'workspace/src/code.py').write_text('partial\n')
        with patch.object(support.subprocess, 'run', side_effect=[self.response('failed', 'rate_limit'), self.response()]) as call:
            result = support.run_job(self.desk, j['id'])
        self.assertEqual(result['status'], 'completed'); self.assertEqual(call.call_count, 2)
        self.assertEqual((path / 'workspace/src/code.py').read_text(), 'partial\n')
        self.assertIn('Do not replay', (path / 'control/fallback.txt').read_text())
        self.assertEqual([a['provider'] for a in result['attempts']], ['glm', 'astra'])

    def test_permission_denial_never_falls_back(self):
        j = self.make()
        with patch.object(support.subprocess, 'run', return_value=self.response('blocked', 'permission_denied')) as call:
            result = support.run_job(self.desk, j['id'])
        self.assertEqual(result['status'], 'blocked'); self.assertEqual(call.call_count, 1)
        self.assertIn('complete report body', support.result_text(self.desk, j['id']))

    def test_both_provider_failures_stop_and_duplicate_run_refused(self):
        j = self.make()
        with patch.object(support.subprocess, 'run', return_value=self.response('failed', 'rate_limit')) as call:
            result = support.run_job(self.desk, j['id'])
        self.assertEqual(result['status'], 'failed'); self.assertEqual(call.call_count, 2)
        with self.assertRaises(BellhopError): support.run_job(self.desk, j['id'])

    def test_completed_results_stay_unread_until_review(self):
        j = self.make()
        with patch.object(support.subprocess, 'run', return_value=self.response()): support.run_job(self.desk, j['id'])
        self.assertEqual(support.summary(self.desk)['unread'], [j['id']])
        support.review(self.desk, j['id'], 'reviewed body, follow-up recorded')
        self.assertEqual(support.summary(self.desk)['unread'], [])

    def test_apply_checks_original_revision_and_keeps_backup(self):
        j = self.make(); path = support.job_path(self.desk, j['id'])
        (path / 'workspace/src/code.py').write_text('after\n')
        with patch.object(support.subprocess, 'run', return_value=self.response()): support.run_job(self.desk, j['id'])
        self.assertIn('+after', support.diff(self.desk, j['id']))
        support.apply(self.desk, j['id'], 'reviewed changes')
        self.assertEqual((self.source / 'code.py').read_text(), 'after\n')
        self.assertEqual((path / 'control/apply-backup/code.py').read_text(), 'before\n')
        with self.assertRaises(BellhopError): support.apply(self.desk, j['id'], 'do not repeat')

    def test_source_drift_and_product_apply_are_refused(self):
        j = self.make(); path = support.job_path(self.desk, j['id'])
        (path / 'workspace/src/code.py').write_text('after\n')
        with patch.object(support.subprocess, 'run', return_value=self.response()): support.run_job(self.desk, j['id'])
        (self.source / 'code.py').write_text('concurrent edit\n')
        with self.assertRaises(BellhopError): support.apply(self.desk, j['id'], 'drift')
        (self.source / 'code.py').write_text('before\n')
        self.desk.cfg['lanes'] = {'product': {'repo': str(self.source)}}
        with self.assertRaises(BellhopError): support.apply(self.desk, j['id'], 'product not maintenance')

    def test_capacity_and_report_write_scope(self):
        self.make(); self.make()
        with self.assertRaises(BellhopError): self.make()
        with self.assertRaises(BellhopError): support.submit(self.desk, dict(self.spec, mode='report'), start=False)

    def test_no_change_apply_is_recorded_once(self):
        j = self.make()
        with patch.object(support.subprocess, 'run', return_value=self.response()): support.run_job(self.desk, j['id'])
        self.assertEqual(support.apply(self.desk, j['id'], 'no changes needed')['applied'], [])
        with self.assertRaises(BellhopError): support.apply(self.desk, j['id'], 'repeat')

    def test_reconcile_ignores_active_and_marks_orphan(self):
        j = self.make()
        reply = type('Result', (), {'returncode': 0, 'stdout': 'active\n'})()
        with patch.object(support.subprocess, 'run', return_value=reply): self.assertEqual(support.reconcile(self.desk), [])
        reply.stdout = 'inactive\n'
        with patch.object(support.subprocess, 'run', return_value=reply): self.assertEqual(support.reconcile(self.desk), [j['id']])
        self.assertEqual(support.get(self.desk, j['id'])['status'], 'interrupted')

    def test_cancel_records_reason_without_retry(self):
        j = self.make()
        with patch.object(support.subprocess, 'run', return_value=self.response()) as call:
            self.assertEqual(support.cancel(self.desk, j['id'], 'owner stopped fixture')['status'], 'cancelled')
        self.assertEqual(call.call_args.args[0][-1], 'bellhop-support-' + j['id'])

    def test_invalid_root_type_is_friendly_error(self):
        with self.assertRaises(BellhopError): support.submit(self.desk, dict(self.spec, source_root=42), start=False)

    def test_input_set_expands_operator_bundle_but_not_private_files(self):
        (self.source / '.env').write_text('fixture only')
        self.desk.cfg['support']['input_sets'] = {'code': {'source_root': str(self.source), 'include': ['*']}}
        j = support.submit(self.desk, {'title': 'bundle', 'prompt': 'inspect', 'input_set': 'code'}, start=False)
        self.assertEqual([r['path'] for r in j['inputs']], ['code.py'])
        with self.assertRaises(BellhopError):
            support.submit(self.desk, dict(self.spec, input_set='unknown'), start=False)

    def test_inert_template_config_does_not_allow_live_agent_config(self):
        self.assertEqual(str(support.relative('template/.claude/settings.json')), 'template/.claude/settings.json')
        with self.assertRaises(BellhopError): support.relative('.claude/settings.json')
        with self.assertRaises(BellhopError): support.relative('other/.claude/settings.json')


if __name__ == '__main__': unittest.main()
