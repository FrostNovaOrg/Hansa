"""Small durable coordinator jobs, separate from formal lane acceptance.

The launcher owns provider credentials. Jobs own input snapshots and results.
Only explicit apply writes a reviewed code result back to an approved source.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid

from .locking import file_lock
from .util import BellhopError, read_json, ts, write_json

TERMINAL = {'completed', 'failed', 'blocked', 'interrupted', 'cancelled'}
RETRYABLE = {'rate_limit', 'overloaded', 'server_error', 'transport', 'authentication', 'model_not_found'}
FORBIDDEN = {'.git', '.claude', '.grok', '.codex', 'migrations_private', 'node_modules', '__pycache__'}


def config(desk):
    cfg = desk.cfg.get('support') or {}
    if not cfg.get('enabled'):
        raise BellhopError('support jobs are not configured for this desk')
    return cfg


def root(desk):
    cfg = config(desk)
    path = Path(cfg.get('root') or desk.path('.support')).absolute()
    if path.is_symlink() or path.resolve() != path:
        raise BellhopError('support root must be a real directory')
    path.mkdir(parents=True, exist_ok=True)
    return path


def job_path(desk, ident):
    if not isinstance(ident, str) or not re.fullmatch(r's-[0-9a-f]{12}', ident):
        raise BellhopError('invalid support job id')
    path = root(desk) / ident
    if path.is_symlink():
        raise BellhopError('support job must not be a symlink')
    return path


def relative(value):
    if not isinstance(value, str) or not value or '\\' in value:
        raise BellhopError('files must be relative POSIX paths')
    p = Path(value)
    if p.is_absolute() or any(x in ('..', '.') for x in value.split('/')):
        raise BellhopError('input path escapes source root')
    # Bellhop's inert test template is copied below workspace/src, never loaded as
    # the worker's own configuration. All other native agent config stays out.
    parts = p.parts
    if parts[:2] == ('template', '.claude'):
        parts = ('template',) + parts[2:]
    if any(x in FORBIDDEN or x.startswith('.env') or 'secret' in x.lower() or 'credential' in x.lower() for x in parts):
        raise BellhopError('private/configuration input excluded: ' + value)
    return p


def inside(path, roots):
    return any(path == r or r in path.parents for r in roots)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def get(desk, ident):
    path = job_path(desk, ident)
    result = read_json(path / 'job.json', None)
    if not isinstance(result, dict):
        raise BellhopError('support job not found: ' + ident)
    return result


def list_jobs(desk, limit=30):
    if not (desk.cfg.get('support') or {}).get('enabled'):
        return []
    found = []
    for p in root(desk).glob('s-*/job.json'):
        if p.parent.is_symlink() or p.is_symlink():
            continue
        item = read_json(p, {})
        if item:
            found.append(item)
    return sorted(found, key=lambda j: j.get('created_at', ''), reverse=True)[:limit]


def summary(desk):
    jobs = list_jobs(desk, 1000)
    return {'running': sum(j.get('status') not in TERMINAL for j in jobs),
            'unread': [j['id'] for j in jobs if j.get('status') in TERMINAL and not j.get('reviewed_at')],
            'recent': [{k: j.get(k) for k in ('id', 'title', 'status', 'provider', 'reviewed_at')} for j in jobs[:8]]}


def _save(path, job):
    job['updated_at'] = ts()
    write_json(path / 'job.json', job)


def _launcher(desk):
    launcher = Path(config(desk).get('launcher') or '')
    if not launcher.is_absolute() or not launcher.is_file() or launcher.is_symlink():
        raise BellhopError('configure an absolute, non-symlink support launcher')
    if launcher.stat().st_mode & 0o022:
        raise BellhopError('support launcher must not be group/world writable')
    return launcher


def submit(desk, spec, *, start=True):
    cfg = config(desk)
    if not isinstance(spec, dict) or set(spec) - {'title', 'prompt', 'source_root', 'files', 'writable_files', 'commands', 'mode', 'provider', 'input_set'}:
        raise BellhopError('invalid support request fields')
    title, prompt = spec.get('title', ''), spec.get('prompt', '')
    if not isinstance(title, str) or not title.strip() or len(title) > 200 or not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode()) > 24000:
        raise BellhopError('support request needs a short title and a bounded task prompt')
    mode = spec.get('mode', 'report')
    provider = spec.get('provider', 'glm')
    if mode not in ('report', 'code') or provider not in ('glm', 'astra'):
        raise BellhopError('mode must be report/code; provider must be glm/astra')
    bundle = {}
    if spec.get('input_set') is not None:
        name = spec['input_set']
        if not isinstance(name, str) or name not in cfg.get('input_sets', {}):
            raise BellhopError('unknown operator-configured input_set')
        bundle = cfg['input_sets'][name]
        if spec.get('source_root') and spec['source_root'] != bundle['source_root']:
            raise BellhopError('input_set and source_root disagree')
    if spec.get('source_root') is not None and not isinstance(spec['source_root'], str):
        raise BellhopError('source_root must be a path string')
    source = Path(spec.get('source_root') or bundle.get('source_root') or desk.root).absolute()
    roots = [Path(r).resolve() for r in cfg.get('source_roots', [])]
    if source.resolve() != source or source not in roots:
        raise BellhopError('source_root must exactly match a configured support source root')
    files, writable = spec.get('files', []), spec.get('writable_files', [])
    if bundle:
        if not isinstance(files, list): raise BellhopError('files must be a list')
        selected = []
        for pattern in bundle.get('include', []):
            relative(pattern)
            for candidate in sorted(source.glob(pattern)):
                if candidate.is_file():
                    try: selected.append(str(relative(str(candidate.relative_to(source)))))
                    except BellhopError: continue
        files = list(dict.fromkeys(selected + files))
    if not isinstance(files, list) or not isinstance(writable, list) or len(files) > 500 or len(writable) > 100:
        raise BellhopError('support input/write list is invalid or too large')
    files = list(dict.fromkeys(str(relative(p)) for p in files))
    writable = list(dict.fromkeys(str(relative(p)) for p in writable))
    if mode == 'report' and writable:
        raise BellhopError('report jobs cannot declare source writes')
    commands = spec.get('commands', [])
    allowed_commands = cfg.get('commands', [])
    if not isinstance(commands, list) or any(not isinstance(c, str) or c not in allowed_commands for c in commands):
        raise BellhopError('commands must be selected from the operator-configured exact command list')
    launcher = _launcher(desk)
    if start:
        check = subprocess.run([str(launcher), 'doctor', provider], text=True, capture_output=True, timeout=15)
        if check.returncode:
            raise BellhopError('provider is not ready; run bellhop support doctor (no job started)')
    home = root(desk)
    with file_lock(home / '.submit.flock', blocking=False):
        active = [j for j in list_jobs(desk, 10000) if j.get('status') not in TERMINAL]
        if len(active) >= int(cfg.get('max_parallel', 2)):
            raise BellhopError('support capacity is occupied; inspect existing jobs instead of duplicating them')
        ident = 's-' + uuid.uuid4().hex[:12]
        path = home / ident
        path.mkdir(mode=0o750)
        (path / 'control').mkdir(mode=0o750)
        work = path / 'workspace'
        (work / 'src').mkdir(parents=True)
        (work / 'out').mkdir()
        snapshot = []
        total = 0
        try:
            for name in dict.fromkeys(files + writable):
                src = source / name
                if src.resolve() != src or not inside(src.resolve(), [source]):
                    raise BellhopError('symlink input excluded: ' + name)
                existed = src.exists()
                if not existed and name not in writable:
                    raise BellhopError('input missing: ' + name)
                row = {'path': name, 'existed': existed, 'writable': name in writable}
                if existed:
                    if not src.is_file() or src.stat().st_nlink != 1:
                        raise BellhopError('input must be a regular non-hardlinked file: ' + name)
                    total += src.stat().st_size
                    if total > int(cfg.get('max_input_bytes', 20 * 1024 * 1024)):
                        raise BellhopError('support input snapshot exceeds its byte budget')
                    row.update(sha256=sha(src), mode=src.stat().st_mode & 0o777)
                    dst = work / 'src' / name
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(src, dst)
                    if sha(dst) != row['sha256'] or sha(src) != row['sha256']:
                        raise BellhopError('source changed during snapshot: ' + name)
                snapshot.append(row)
            job = {'id': ident, 'title': title, 'mode': mode, 'status': 'queued', 'provider': provider,
                   'created_at': ts(), 'source_root': str(source), 'inputs': snapshot, 'commands': commands,
                   'attempts': [], 'reviewed_at': None, 'applied': [], 'unit': 'bellhop-support-' + ident}
            write_json(path / 'control/request.json', dict(spec, files=files, writable_files=writable))
            (path / 'control/prompt.txt').write_text(prompt + '\n')
            _save(path, job)
        except BaseException:
            # Never leave a failed preparation consuming an active slot.
            _save(path, {'id': ident, 'title': title, 'created_at': ts(), 'status': 'failed',
                         'error': 'input_preparation_failed', 'reviewed_at': ts()})
            raise
        if start:
            cmd = ['systemd-run', '--user', '--quiet', '--unit', job['unit'], '--property=Type=exec',
                   '--property=KillMode=control-group', '--property=UMask=0077',
                   '--working-directory', str(desk.root), sys.executable,
                   str(Path(__file__).resolve().parents[1] / 'bellhop_main.py'), '--desk', str(desk.root),
                   'support', 'run', ident]
            result = subprocess.run(cmd, text=True, capture_output=True, timeout=20)
            if result.returncode:
                job.update(status='failed', error='service_start_failed')
                _save(path, job)
                raise BellhopError('support service did not start; input snapshot preserved at ' + str(path))
    return job


def run_job(desk, ident):
    path = job_path(desk, ident)
    with file_lock(path / '.run.flock', blocking=False):
        job = get(desk, ident)
        if job['status'] != 'queued':
            raise BellhopError('job already started; a new request is required for an explicit retry')
        job.update(status='running', runner_pid=os.getpid())
        _save(path, job)
        choices = [job['provider']] + (['astra'] if job['provider'] == 'glm' else [])
        for provider in choices:
            job['provider'] = provider
            job['status'] = 'running' if not job['attempts'] else 'fallback'
            _save(path, job)
            try:
                proc = subprocess.run([str(_launcher(desk)), 'run', str(path), provider],
                                      text=True, capture_output=True)
                (path / ('launcher-' + provider + '.stderr')).write_text(getattr(proc, 'stderr', '')[-16000:])
                result = json.loads(proc.stdout)
                if not isinstance(result, dict) or result.get('status') not in ('completed', 'failed', 'blocked'):
                    raise ValueError('invalid launcher result')
            except (OSError, ValueError):
                result = {'status': 'failed', 'error': 'launcher_protocol_error'}
            result['provider'] = provider
            job['attempts'].append({k: result.get(k) for k in ('provider', 'model', 'effort', 'status', 'error', 'session_id', 'usage', 'cost_usd')})
            _save(path, job)
            body = result.get('body') or ''
            (path / ('result-' + provider + '.md')).write_text(str(body))
            if result['status'] == 'completed':
                (path / 'result.md').write_text(str(body))
                job.update(status='completed', finished_at=ts(), model=result.get('model'), effort=result.get('effort'))
                break
            if provider == 'glm' and result.get('error') in RETRYABLE:
                # New provider sees the current files plus partial output, never a reset.
                (path / 'control/fallback.txt').write_text(
                    'The previous provider ended on API error ' + str(result.get('error')) + '.\n'
                    'Inspect current src/out and the prior partial result before continuing. '
                    'Do not replay completed side effects. Permission denials remain binding.\n' + str(body)[:24000])
                continue
            job.update(status=result['status'], error=result.get('error'), finished_at=ts())
            break
        else:
            job.update(status='failed', error=job['attempts'][-1].get('error'), finished_at=ts())
        if job['status'] in ('running', 'fallback'):
            job.update(status='failed', error=job['attempts'][-1].get('error'), finished_at=ts())
        _save(path, job)
        return job


def result_text(desk, ident):
    path, job = job_path(desk, ident), get(desk, ident)
    final = path / 'result.md'
    if final.exists(): return final.read_text()
    parts = []
    for attempt in job.get('attempts', []):
        body = path / ('result-' + attempt['provider'] + '.md')
        if body.exists(): parts.append(attempt['provider'] + ' (' + str(attempt.get('status')) + ')\n' + body.read_text())
    return '\n\n'.join(parts) or 'No report body yet; inspect status/attempts.'


def review(desk, ident, note):
    if not isinstance(note, str) or not note.strip():
        raise BellhopError('review requires a disposition note')
    path = job_path(desk, ident)
    with file_lock(path / '.review.flock', blocking=False):
        job = get(desk, ident)
        if job['status'] not in TERMINAL:
            raise BellhopError('job is still running')
        job.update(reviewed_at=ts(), review_note=note[:2000])
        _save(path, job)
    return job


def diff(desk, ident):
    path = job_path(desk, ident)
    job = get(desk, ident)
    text = []
    for row in job['inputs']:
        if not row['writable']:
            continue
        source, candidate = Path(job['source_root']) / row['path'], path / 'workspace/src' / row['path']
        if not candidate.is_file() or candidate.is_symlink():
            continue
        old = source.read_text().splitlines(True) if source.exists() else []
        new = candidate.read_text().splitlines(True)
        text.extend(difflib.unified_diff(old, new, fromfile='source/' + row['path'], tofile='candidate/' + row['path']))
    return ''.join(text)


def apply(desk, ident, note):
    cfg, path = config(desk), job_path(desk, ident)
    if not note or not note.strip():
        raise BellhopError('a review note is required before applying code')
    with file_lock(root(desk) / '.apply.flock', blocking=False), file_lock(path / '.review.flock', blocking=False):
        job = get(desk, ident)
        if job['status'] != 'completed' or job.get('applied_at') or job.get('applied'):
            raise BellhopError('only a completed, not-yet-applied support job can be applied')
        allowed = [Path(p).resolve() for p in cfg.get('apply_roots', [])]
        lane_roots = [Path(l['repo']).resolve() for l in desk.cfg.get('lanes', {}).values() if l.get('repo')]
        changes = []
        for row in job['inputs']:
            if not row['writable']:
                continue
            dst = Path(job['source_root']) / relative(row['path'])
            src = path / 'workspace/src' / row['path']
            if dst.resolve() != dst or src.resolve() != src or not src.is_file():
                raise BellhopError('candidate or destination is missing or uses a symlink')
            if not inside(dst, allowed) or inside(dst, lane_roots) or '.worktrees' in dst.parts:
                raise BellhopError('apply is limited to maintenance roots; formal product work requires lane acceptance')
            if dst.exists() != row['existed'] or (dst.exists() and sha(dst) != row['sha256']):
                raise BellhopError('source changed since the job snapshot: ' + row['path'])
            if not dst.exists() or sha(src) != row['sha256']:
                changes.append((row, src, dst))
        backup = path / 'control/apply-backup'
        backup.mkdir(exist_ok=False)
        applied = []
        try:
            for row, src, dst in changes:
                dst.parent.mkdir(parents=True, exist_ok=True)
                if dst.exists():
                    saved = backup / row['path']; saved.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(dst, saved)
                tmp = dst.with_name(dst.name + '.bellhop-apply-' + ident)
                shutil.copyfile(src, tmp); tmp.chmod(row.get('mode', 0o644)); tmp.replace(dst)
                applied.append((row, dst))
        except BaseException:
            for row, dst in reversed(applied):
                if row['existed']:
                    shutil.copy2(backup / row['path'], dst)
                else:
                    dst.unlink()
            raise
        job.update(applied=[str(dst) for _, dst in applied], applied_at=ts(), reviewed_at=ts(), review_note=note[:2000])
        _save(path, job)
        return job


def listen(desk, ident):
    previous = None
    while True:
        job = get(desk, ident)
        state = (job['status'], len(job.get('attempts', [])))
        if state != previous:
            print(json.dumps({k: job.get(k) for k in ('id', 'title', 'status', 'provider', 'error')}, ensure_ascii=False), flush=True)
            previous = state
        if job['status'] in TERMINAL:
            return
        time.sleep(2)


def doctor(desk):
    cfg = config(desk)
    result = {'configured': True, 'source_roots': cfg.get('source_roots', []),
              'commands': cfg.get('commands', []), 'input_sets': cfg.get('input_sets', {}),
              'apply_roots': cfg.get('apply_roots', []), 'providers': {}}
    for provider in ('glm', 'astra'):
        proc = subprocess.run([str(_launcher(desk)), 'doctor', provider], text=True, capture_output=True, timeout=15)
        result['providers'][provider] = {'ready': proc.returncode == 0, 'detail': proc.stdout.strip()[:500]}
    return result


def reconcile(desk):
    changed = []
    for job in list_jobs(desk, 10000):
        if job.get('status') in TERMINAL:
            continue
        unit = 'bellhop-support-' + job['id']
        proc = subprocess.run(['systemctl', '--user', 'show', unit, '-p', 'ActiveState', '--value'],
                              text=True, capture_output=True, timeout=10)
        if proc.returncode == 0 and proc.stdout.strip() in ('inactive', 'failed'):
            path = job_path(desk, job['id'])
            try:
                with file_lock(path / '.run.flock', blocking=False):
                    current = get(desk, job['id'])
                    if current.get('status') not in TERMINAL:
                        current.update(status='interrupted', error='service_ended_before_result', finished_at=ts())
                        _save(path, current); changed.append(job['id'])
            except BlockingIOError:
                continue  # A runner still owns this job; never interrupt it from a stale unit read.
    return changed


def cancel(desk, ident, note):
    if not note or not note.strip(): raise BellhopError('cancellation requires a reason')
    path, job = job_path(desk, ident), get(desk, ident)
    if job['status'] in TERMINAL: raise BellhopError('job has already ended')
    subprocess.run(['systemctl', '--user', 'stop', 'bellhop-support-' + ident], check=True, timeout=30)
    with file_lock(path / '.run.flock', blocking=False):
        job = get(desk, ident)
        if job['status'] not in TERMINAL:
            job.update(status='cancelled', cancel_note=note[:2000], finished_at=ts()); _save(path, job)
    return job
