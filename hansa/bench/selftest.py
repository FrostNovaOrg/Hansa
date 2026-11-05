"""Disabled, exclusive acceptance. Missing observations never authorize enablement.
Keep all gates until the corresponding isolation/lifecycle requirement is removed.
"""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace

from . import archive, probes
from .vm import INSTANCE

NAMES = ('sandbox', 'sandbox-again', 'account', 'root-edge', 'bench-edge', 'inspect', 'dhcp',
         'descendant-normal', 'descendant-timeout', 'descendant-disconnect', 'descendant-gateway-death',
         'caps-output', 'caps-file', 'caps-total', 'caps-count', 'caps-links', 'header-cap', 'unknown-field', 'tar-cap', 'inspect-final')


def mandatory():
    required = dict(boots=('ports', 'keys'), lifecycle=('queue', 'restart', 'idle', 'memory'), stopped=('confirmed',))
    for name in NAMES:
        fixture = {'sandbox-again': 'sandbox', 'inspect-final': 'inspect'}.get(name, name)
        fields = (dict(renewed='') if name == 'dhcp' else dict(rejected='', survivors='', slots='')
                  if name in probes.malformed() else probes.checks(fixture, {}))
        if fixture == 'sandbox' or name.startswith('caps-') or name in ('descendant-normal', 'descendant-timeout'):
            fields['execution'] = ''
        required.update({str(boot) + '/' + name: tuple(fields) for boot in (1, 2)})
    return required


def complete(report):
    results = report.get('results', {})
    return not report.get('error') and isinstance(results, dict) and all(isinstance(results.get(n), dict) and results[n].get('status') == 'passed' and
               results[n].get('checks') == dict.fromkeys(fields, 'passed') for n, fields in mandatory().items())

# Admin supervises the real gateway as bench; inspect before any stop/purge can
# hide survivors. Only fixed shipped source and fixtures enter this root program.
SUPERVISE = '''import io, json, pwd, subprocess, sys, tempfile, time
from contextlib import redirect_stdout
request, mode, inspect = json.loads(sys.argv[1])
account = pwd.getpwnam('bench')
def observe():
    with redirect_stdout(io.StringIO()) as output: exec(inspect, {})
    return json.loads(output.getvalue())
with tempfile.TemporaryFile() as output:
    child = subprocess.Popen(['/usr/local/libexec/hansa-bench'], user=account.pw_uid,
        group=account.pw_gid, extra_groups=(), env={'SSH_ORIGINAL_COMMAND': 'bench-v1'},
        stdin=subprocess.PIPE, stdout=output, stderr=subprocess.DEVNULL)
    started = False
    try:
        child.stdin.write(bytes.fromhex(request)); child.stdin.flush()
        if mode in ('timeout', 'disconnect', 'gateway-death'):
            deadline = time.monotonic() + 20
            while not observe()['ready']:
                if child.poll() is not None or time.monotonic() >= deadline: raise TimeoutError('readiness')
                time.sleep(.05)
            started = True
            if mode == 'disconnect': child.stdin.close()
            if mode == 'gateway-death': child.kill()
        child.wait(timeout=210)
    finally:
        if child.poll() is None: child.kill()
        child.wait(timeout=10); child.stdin.close()
    deadline = time.monotonic() + 10
    evidence = observe()
    while evidence['survivors'] and time.monotonic() < deadline:
        time.sleep(.05); evidence = observe()
    evidence.update(started=started, gateway_exit=child.returncode)
    print(json.dumps(evidence), flush=True)
    output.seek(0); sys.stdout.buffer.write(output.read(80 * 1024**2))
'''
DHCP = '''import json, subprocess, time
from pathlib import Path
leases = {p: p.stat().st_mtime_ns for p in Path('/run/systemd/netif/leases').iterdir()}
if not leases: raise RuntimeError('no DHCP lease')
subprocess.run(['networkctl', 'renew', *[p.name for p in leases]], check=True, timeout=30)
deadline = time.monotonic() + 60
while not all(p.stat().st_mtime_ns != stamp and 'ADDRESS=' in p.read_text() for p, stamp in leases.items()):
    if time.monotonic() >= deadline: raise TimeoutError('DHCP renewal')
    time.sleep(.1)
print(json.dumps({'renewed': 'passed'}))
'''


def admin(machine, source, deadline, *args, account='root', **streams):
    return machine._call(['limactl', 'shell', INSTANCE, 'sudo', '-n', '-u', account,
                          '--', 'python3', '-I', '-c', source, *args], deadline, **streams)


def submission(machine, name, source, deadline):
    malformed = name in probes.malformed()
    header = json.dumps(dict(version=1, id='a' * 32, cwd='.', length=1024,
        timeout=30 if name == 'descendant-timeout' else 180,
        script='python3 -I -c ' + shlex.quote(source))).encode()
    wire = probes.malformed()[name] if malformed else len(header).to_bytes(4, 'big') + header + bytes(1024)
    with tempfile.TemporaryFile() as returned:
        evidence = {}
        if malformed or name.startswith('descendant-'):
            admin(machine, SUPERVISE, deadline, json.dumps([wire.hex(), name.removeprefix('descendant-'),
                  probes.fixtures()['inspect'][1]]), stdout=returned)
            returned.seek(0); evidence = json.loads(returned.readline(65536))
        else:
            reader, writer = os.pipe()
            with os.fdopen(reader, 'rb') as incoming, os.fdopen(writer, 'wb') as outgoing:
                errors = []
                def send():
                    try: outgoing.write(wire); outgoing.flush()
                    except OSError as error: errors.append(error)
                sender = threading.Thread(target=send); sender.start()
                try: machine.connect(incoming, returned, deadline)
                finally: incoming.close(); sender.join()
                if errors: raise errors[0]
            returned.seek(0)
        if malformed:
            return dict(rejected='passed' if evidence['gateway_exit'] != 0 and not returned.read(1) else 'failed',
                        **probes.checks('inspect', evidence))
        if name in ('descendant-disconnect', 'descendant-gateway-death'):
            return probes.checks(name, evidence)
        size = int.from_bytes(returned.read(4), 'big')
        if not 0 < size <= archive.HEADER_BYTES: raise ValueError('return header')
        response = json.loads(returned.read(size))
        if response.get('id') != 'a' * 32 or response.get('version') != 1: raise ValueError('return identity')
        if response['length'] != os.fstat(returned.fileno()).st_size - returned.tell(): raise ValueError('return length')
        with archive._load(returned, response['length'], archive.RUN_BYTES + archive.OUTPUT_BYTES + archive.HEADER_BYTES,
                           archive.ARTIFACT_ENTRIES + 3, archive.FILE_BYTES, archive.RUN_BYTES + archive.MAX_METADATA + archive.OUTPUT_BYTES) as (tar, members):
            data = {n: tar.extractfile(m).read() for n, m in members.items()}
        result = json.loads(data.pop('result.json'))
        stdout, stderr = data.pop('stdout'), data.pop('stderr')
        artifacts = {n[len('artifacts/'):]: v for n, v in data.items() if n.startswith('artifacts/')}
        evidence.update(result, output=len(stdout) + len(stderr))
        if name == 'sandbox': evidence.update(json.loads(stdout))
        if name == 'descendant-normal': evidence['started'] = artifacts.get('descendant.ready') == b'ready'
        checked = probes.checks(name, evidence, artifacts)
        checked['execution'] = 'passed' if result.get('id') == 'a' * 32 and result.get('status') == ('timeout' if name == 'descendant-timeout' else 'ok') and (name == 'descendant-timeout' or result.get('exit') == 0) else 'failed'
        return checked


def lifecycle(machine):
    from .broker import Broker
    from .channel import Channel, submit, _read
    with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
        desk = SimpleNamespace(state_dir=Path(folder))
        broker = Broker(desk, machine, clock=machine.clock); broker.ready = True
        workers, ids = {}, []
        for n in range(5):
            path = Path(folder) / str(n); path.mkdir()
            worker = Channel(path, str(n), path, broker.epoch); stack.callback(worker.close)
            workers[worker] = machine.clock() + 300
            ids.append(submit(worker.directory, worker.epoch, 'true'))
        broker.poll(workers, enabled=True, held=True)
        busy = _read(worker.directory, ids[-1] + '.reply')['result']['status'] == 'busy'
        broker.revoke()
        cancelled = all(_read(w.directory, rid + '.reply')['result']['status'] == 'cancelled' for w, rid in list(zip(workers, ids))[:4])
        stale = submit(worker.directory, worker.epoch, 'false')
        restarted = Broker(desk, machine, clock=machine.clock)
        restarted.poll(workers, enabled=True, held=True)
        discarded = not restarted.waiting and stale not in worker.claimed and machine._info(machine.clock() + 10)['status'] == 'Stopped'
        machine.reset(machine.clock() + 120); machine.start(machine.clock() + 120)
        def processes():
            return {int(p): (int(parent), int(rss), command) for p, parent, rss, command in
                    (line.split(None, 3) for line in machine._call(['ps', '-axo', 'pid=,ppid=,rss=,command='], machine.clock() + 10, stdout=subprocess.PIPE).stdout.decode().splitlines())}
        before = processes()
        owned = {p for p, (_, rss, command) in before.items() if rss > 0 and str(machine.private / 'lima' / INSTANCE) in command}
        while {p for p, (parent, _, _) in before.items() if parent in owned} - owned:
            owned.update(p for p, (parent, _, _) in before.items() if parent in owned)
        restarted.idle = machine.clock()
        deadline = machine.clock() + 90
        while machine.port is not None:
            if machine.clock() >= deadline: raise TimeoutError('idle stop')
            restarted.poll({}, enabled=True); time.sleep(.1)
        checked = {n: 'passed' if ok else 'failed' for n, ok in dict(queue=busy and cancelled, restart=discarded, idle=machine._info(machine.clock() + 10)['status'] == 'Stopped').items()}
        checked['memory'] = 'incomplete' if not owned else 'failed' if owned & processes().keys() else 'passed'
        return checked


def run(machine):
    """Caller holds manage.exclusive and has persisted incomplete acceptance."""
    fixtures = probes.fixtures()
    results = {n: dict(status='incomplete', checks=dict.fromkeys(fields, 'incomplete')) for n, fields in mandatory().items()}
    report = dict(status='incomplete', results=results, versions={})
    started, ports, keys = machine.clock(), [], []
    def record(name, action):
        before = machine.clock()
        try:
            checks = action()
            status = 'failed' if 'failed' in checks.values() else 'passed' if checks and set(checks.values()) == {'passed'} else 'incomplete'
            results[name] = dict(status=status, checks=checks)
        except Exception as error:
            results[name] = dict(status='incomplete', error=f'{type(error).__name__}: {error}')
        results[name]['seconds'] = machine.clock() - before
    try:
        with ExitStack() as stack:
            report['versions']['lima'] = machine._call(['limactl', '--version'], machine.clock() + 10, stdout=subprocess.PIPE).stdout.decode()
            targets = {}
            for name, address in (('host', '127.0.0.1'), ('lan', socket.gethostbyname(socket.gethostname()))):
                if name == 'lan' and (address.startswith('127.') or address == '0.0.0.0'): raise RuntimeError('LAN listener unavailable')
                listener = stack.enter_context(socket.socket()); listener.bind((address, 0)); listener.listen(16)
                with socket.create_connection(listener.getsockname(), timeout=3): pass
                targets[name] = ['192.168.5.2' if name == 'host' else address, listener.getsockname()[1]]
            machine.reset(machine.clock() + 120)
            for boot in (1, 2):
                ports.append(machine.start(machine.clock() + 120))
                deadline = machine.clock() + 900
                keys.append(machine._call(['limactl', 'shell', INSTANCE, 'sudo', '-n', 'cat', '/etc/ssh/ssh_host_ed25519_key.pub'], deadline, stdout=subprocess.PIPE).stdout.split()[:2])
                report['versions'][str(boot)] = machine._call(['limactl', 'shell', INSTANCE, 'sudo', '-n', 'cat', '/var/lib/hansa-bench-build/packages.tsv'], deadline, stdout=subprocess.PIPE).stdout.decode()
                for name in NAMES:
                    def probe():
                        if name == 'dhcp': return json.loads(admin(machine, DHCP, deadline, stdout=subprocess.PIPE).stdout)
                        fixture = {'sandbox-again': 'sandbox', 'inspect-final': 'inspect'}.get(name, name)
                        scope, source = ('submission', '') if fixture in probes.malformed() else fixtures[fixture]
                        if scope == 'submission': return submission(machine, fixture, source, deadline)
                        evidence = json.loads(admin(machine, source, deadline, json.dumps(targets), account=scope, stdout=subprocess.PIPE).stdout)
                        return probes.checks(fixture, evidence)
                    record(str(boot) + '/' + name, probe)
                    if name == 'descendant-gateway-death':
                        machine.reset(machine.clock() + 120); machine.start(machine.clock() + 120)
                machine.stop(machine.clock() + 30)
            record('boots', lambda: dict(ports='passed' if ports[0] != ports[1] else 'incomplete', keys='passed' if keys[0] == keys[1] and keys[0] else 'failed'))
            record('lifecycle', lambda: lifecycle(machine))
    except Exception as error:
        report['error'] = f'{type(error).__name__}: {error}'
    finally:
        def stopped():
            machine.stop(machine.clock() + 30)
            return {'confirmed': 'passed' if machine._info(machine.clock() + 10).get('status') == 'Stopped' else 'failed'}
        record('stopped', stopped)
    states = [r['status'] for r in results.values()]
    report.update(status='failed' if 'failed' in states else 'passed' if complete(report) else 'incomplete', seconds=machine.clock() - started)
    return report
