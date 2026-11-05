"""Shipped acceptance fixtures, inert until the disabled, exclusive selftest runs.
fixtures() returns (execution account, Python source); submission uses the gateway.
Edge argv[1] is JSON {host: [address, port], lan: [address, port]} from controlled
listeners. Run sandbox twice; await inspect.ready before interrupting descendants.
Inspect survivors through Lima admin after SSH closes, before purging; check slots
again after recovery (gateway death can leave a poisoned slot).
checks() consumes decoded stdout and returned artifact bytes, never guest claims
of overall success. Keep these gates while isolation is required; absent evidence
is incomplete. Boot, transport, DHCP and broker orchestration belong to the runner.
"""
import json
import re
import struct

COMMON = '''import json, os, pwd, resource, signal, socket, subprocess, sys, time
from pathlib import Path
def run(*args):
    return subprocess.check_output(args, timeout=30, stderr=subprocess.DEVNULL).decode()
def connected(address):
    try:
        with socket.create_connection(address, timeout=3): return True
    except OSError: return False
def dns():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(3)
        try:
            s.sendto(bytes.fromhex('123401000001000000000000') + b'\\x03deb\\x06debian\\x03org\\0\\0\\x01\\0\\x01', ('192.168.5.2', 53))
            data = s.recv(4096)
            return len(data) > 12 and data[:2] == b'\\x12\\x34' and data[3] & 15 == 0
        except OSError: return False
'''
SANDBOX = '''from http.server import BaseHTTPRequestHandler, HTTPServer
import threading, urllib.request
paths = [Path(p) / 'bench-leftover' for p in ('/work', '/out', '/tmp', '/dev/shm')]
e = dict(fresh=not any(p.exists() for p in paths))
for p in paths: p.write_text('leftover')
class Page(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b'<h1>bench acceptance</h1>')
    def log_message(self, *args): pass
server = HTTPServer(('127.0.0.1', 0), Page)
threading.Thread(target=server.serve_forever, daemon=True).start()
url = 'http://127.0.0.1:%s/' % server.server_port
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
with opener.open(url, timeout=10) as response: e['http'] = response.status
e['browser'] = subprocess.run(['chromium', '--headless', '--disable-gpu',
    '--user-data-dir=/tmp/chromium', '--screenshot=/out/browser.png', url], timeout=45,
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
e['pty'] = '/dev/pts/' in run('script', '-q', '-c', 'tty', '/dev/null')
run('tmux', '-S', '/tmp/bench-tmux', 'new-session', '-d', 'sh')
run('tmux', '-S', '/tmp/bench-tmux', 'send-keys', 'echo bench-terminal', 'Enter')
deadline = time.monotonic() + 15
while 'bench-terminal' not in run('tmux', '-S', '/tmp/bench-tmux', 'capture-pane', '-p').splitlines():
    if time.monotonic() >= deadline: raise TimeoutError('tmux readiness')
    time.sleep(.05)
e['tmux'] = True
e['interfaces'] = sorted(p.name for p in Path('/sys/class/net').iterdir()) if Path('/sys/class/net').exists() else sorted(line.split(':')[0].strip() for line in Path('/proc/net/dev').read_text().splitlines()[2:])
mounts = [line.split() for line in Path('/proc/self/mountinfo').read_text().splitlines()]
e['readonly'] = all(any(m[4] == p and 'ro' in m[5].split(',') for m in mounts) for p in ('/usr', '/etc', '/run.sh'))
e['credentials'] = not Path('/home').exists() and not any(os.access(p, os.R_OK) for p in ('/root/.ssh', '/etc/shadow', '/etc/ssh/ssh_host_ed25519_key'))
e['nproc'] = list(resource.getrlimit(resource.RLIMIT_NPROC))
e.update(dns=dns(), external=connected(('1.1.1.1', 80)))
Path('/out/evidence.txt').write_bytes(b'bench artifact\\n')
print(json.dumps(e))
'''
ACCOUNT = '''e = dict(line.split(' ', 1) for line in run('/usr/sbin/sshd', '-T', '-C', 'user=bench,host=localhost,addr=127.0.0.1').splitlines())
account = pwd.getpwnam('bench')
e['unprivileged'] = account.pw_uid != 0 and run('id', '-G', 'bench').split() == [str(account.pw_gid)]
paths = ('/etc/ssh/bench/authorized_keys', '/usr/local/libexec/hansa-bench', '/usr/local/lib/hansa-bench/bench/guest.py', '/usr/local/lib/hansa-bench/bench/archive.py')
e['owned'] = all(p.lstat().st_uid == 0 and p.is_file() and not p.is_symlink() and p.stat().st_mode & 0o022 == 0 for p in map(Path, paths))
e['sudo'] = subprocess.run(['runuser', '-u', 'bench', '--', 'sudo', '-n', 'true'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10).returncode == 0
print(json.dumps(e))
'''
EDGE = '''import urllib.request
targets = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
e = {name: connected(targets[name]) if name in targets else None for name in ('host', 'lan')}
e.update(dns=dns(), sshd=connected(('127.0.0.1', 22)))
try:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open('https://deb.debian.org/debian/', timeout=10) as response: e['internet'] = response.status == 200
except OSError: e['internet'] = False
if os.getuid() == 0:
    e['firewall'] = run('nft', '-a', '-n', 'list', 'chain', 'inet', 'bench_edge', 'out')
    e['uid'] = pwd.getpwnam('bench').pw_uid
    e['persistent'] = run('systemctl', 'is-enabled', 'nftables.service').strip() == 'enabled'
    e['ipv6'] = [Path('/proc/sys/net/ipv6/conf/' + n + '/disable_ipv6').read_text().strip() for n in ('all', 'default')]
print(json.dumps(e))
'''
DESCENDANT = '''child = "import os, signal; os.fork() and os._exit(0); os.setsid(); os.fork() and os._exit(0); open('/out/descendant.ready', 'w').write('ready'); signal.pause()"
subprocess.run(['python3', '-I', '-c', child, 'hansa-bench-descendant'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
deadline = time.monotonic() + 20
while not Path('/out/descendant.ready').exists():
    if time.monotonic() >= deadline: raise TimeoutError('descendant readiness')
    time.sleep(.05)
'''
INSPECT = '''uid = pwd.getpwnam('bench').pw_uid
survivors = []
for p in Path('/proc').iterdir():
    if not p.name.isdigit(): continue
    try:
        if p.stat().st_uid == uid: survivors.append(int(p.name))
    except FileNotFoundError: pass
slots = sorted(p.name for p in Path('/var/lib/hansa-bench').iterdir() if p.name != 'lock')
ready = any(Path('/var/lib/hansa-bench', s, 'out/descendant.ready').is_file() for s in slots)
print(json.dumps(dict(survivors=survivors, slots=slots, ready=ready)))
'''
CAPS = {
    'output': "os.write(1, b'x' * (1024 * 1024 + 1))",
    'file': "with open('/out/large', 'wb') as f: f.truncate(16 * 1024 * 1024 + 1)",
    'total': "for n in range(5):\n    with open('/out/%s' % n, 'wb') as f: f.truncate(16 * 1024 * 1024)",
    'count': "for n in range(1001): Path('/out/%s' % n).write_bytes(b'x')",
    'links': "os.symlink('/etc/passwd', '/out/link'); Path('/out/a').touch(); os.link('/out/a', '/out/b')",
}


def fixtures():
    """Runner wraps source in python3 -I; admin scopes use limactl shell, never SSH."""
    table = dict(sandbox=('submission', SANDBOX), account=('root', ACCOUNT),
                 inspect=('root', INSPECT), **{n + '-edge': (n, EDGE) for n in ('root', 'bench')})
    table.update({'descendant-' + mode: ('submission', DESCENDANT + ('' if mode == 'normal' else 'signal.pause()\n'))
                  for mode in ('normal', 'timeout', 'disconnect', 'gateway-death')})
    table.update({'caps-' + name: ('submission', source) for name, source in CAPS.items()})
    return {name: (scope, COMMON + source) for name, (scope, source) in table.items()}


def malformed():
    """Raw stdin cases: oversized envelope, unknown fields, excessive tar declaration."""
    header = dict(version=1, id='a' * 32, script='exit 0', cwd='.', timeout=30, length=1024)
    frames = {'header-cap': (65537).to_bytes(4, 'big')}
    for name, fields in (('unknown-field', dict(extra=True)), ('tar-cap', dict(length=302 * 1024 * 1024))):
        data = json.dumps(dict(header, **fields)).encode()
        frames[name] = len(data).to_bytes(4, 'big') + data
    return frames


def firewall(text, uid):
    """Inspect the live chain, including nft's counters/handles and numeric UID form."""
    if not isinstance(text, str) or type(uid) is not int: return False
    normalize = lambda s: re.sub(r'\{ ([^{}]+) \}', lambda m: '{ ' + ', '.join(sorted(m[1].split(', '))) + ' }', s)
    lines = [re.sub(r'\s+# handle \d+$', '', s.strip()) for s in text.splitlines()]
    rules = [s for s in lines if s and s != '}' and not s.startswith(('table ', 'chain ', 'type '))]
    first = r'meta skuid (?:"bench"|' + str(uid) + r') counter(?: packets \d+ bytes \d+)? drop'
    expected = ['oifname "lo" accept', 'ct state established,related accept',
                'ip daddr 192.168.5.2 udp dport 53 accept', 'ip daddr 192.168.5.2 tcp dport 53 accept',
                'udp sport 68 udp dport 67 accept', 'ip daddr 192.168.5.2 drop',
                'ip daddr { 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 100.64.0.0/10, 169.254.0.0/16, 224.0.0.0/4, 240.0.0.0/4 } drop', 'meta nfproto ipv6 drop']
    return ('type filter hook output priority filter; policy accept;' in lines or
            'type filter hook output priority 0; policy accept;' in lines) and len(rules) == 9 and bool(re.fullmatch(first, rules[0])) and list(map(normalize, rules[1:])) == list(map(normalize, expected))


EXPECTED = {
    'sandbox': dict(fresh=True, http=200, browser=0, pty=True, tmux=True, interfaces=['lo'],
                    readonly=True, credentials=True, nproc=[256, 256], dns=False, external=False),
    'account': dict.fromkeys(('permittty allowtcpforwarding allowstreamlocalforwarding allowagentforwarding '
                             'x11forwarding permittunnel permituserrc passwordauthentication kbdinteractiveauthentication').split(), 'no'),
    'root-edge': dict(host=False, lan=False, dns=True, internet=True, sshd=True, persistent=True, ipv6=['1', '1']),
    'bench-edge': dict(host=False, lan=False, dns=False, internet=False, sshd=False),
    'inspect': dict(survivors=[], slots=[]),
}
EXPECTED['account'].update(forcecommand='/usr/local/libexec/hansa-bench', authorizedkeysfile='/etc/ssh/bench/authorized_keys',
                           authenticationmethods='publickey', unprivileged=True, owned=True, sudo=False)


def checks(name, evidence, artifacts=None):
    """Each mandatory observation is passed/failed/incomplete; runner also checks exit/status.
    caps evidence is result metadata plus combined output byte count; artifacts maps names to bytes.
    Descendants need host-recorded started=True and post-teardown inspect evidence.
    """
    if not isinstance(name, str) or not isinstance(evidence, dict):
        return {'evidence': 'failed'}
    if name in ('descendant-normal', 'descendant-timeout', 'descendant-disconnect', 'descendant-gateway-death'):
        expected = dict(survivors=[], started=True)
    elif name.startswith('caps-') and name[5:] in CAPS:
        expected = dict(truncated=name == 'caps-output', rejected=name != 'caps-output')
    elif name in EXPECTED:
        expected = EXPECTED[name]
    else:
        return {'evidence': 'failed'}
    result = {k: 'incomplete' if evidence.get(k) is None else 'passed' if type(evidence[k]) is type(v) and evidence[k] == v else 'failed' for k, v in expected.items()}
    if name == 'root-edge':
        result['firewall'] = 'incomplete' if any(evidence.get(k) is None for k in ('firewall', 'uid')) else 'passed' if firewall(evidence['firewall'], evidence['uid']) else 'failed'
    if name == 'sandbox' or name.startswith('caps-'):
        result['artifacts'] = 'incomplete' if artifacts is None else 'passed'
        if artifacts is not None and name == 'sandbox':
            png = artifacts.get('browser.png', b'')
            valid = isinstance(png, bytes) and len(png) >= 45 and png[:16] == b'\x89PNG\r\n\x1a\n\0\0\0\rIHDR' and all(struct.unpack('>II', png[16:24])) and png[-12:] == b'\0\0\0\0IEND\xaeB`\x82'
            result['artifacts'] = 'passed' if valid and set(artifacts) == {'browser.png', 'evidence.txt'} and artifacts.get('evidence.txt') == b'bench artifact\n' else 'failed'
        elif artifacts is not None:
            sizes = sorted(map(len, artifacts.values()))
            valid = sizes == {'caps-total': [16 * 1024**2] * 4, 'caps-count': [1] * 1000}.get(name, [])
            result['artifacts'] = 'passed' if valid else 'failed'
        if name == 'caps-output': result['output'] = 'passed' if evidence.get('output') == 1024**2 else 'incomplete' if evidence.get('output') is None else 'failed'
    return result
