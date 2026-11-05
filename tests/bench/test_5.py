"""Execute the recipe with fake system tools and redirected guest paths; no VM/network."""
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[2]
RECIPE = REPO / "contrib/bench"
FAKE = '''import hashlib, json, os, pathlib, shutil, sys
name, a = pathlib.Path(sys.argv[0]).name, sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log: log.write(json.dumps([name, *a]) + "\\n")
if name == os.environ.get("FAKE_FAIL"): sys.exit(1)
if name == "id":
    if a == ["-u", "bench"] and not os.environ.get("BENCH_EXISTS"): sys.exit(1)
    print(os.environ["FAKE_UID"])
elif name == "dirname": print(pathlib.Path(a[-1]).parent)
elif name == "cat": sys.stdout.write(sys.stdin.read())
elif name == "python3":
    import types
    stub = types.SimpleNamespace(version_info=(3, int(os.environ["PY_MINOR"])), exit=sys.exit)
    exec(a[-1].replace("import sys;", "").strip(), {"sys": stub})
elif name == "install":
    mode, paths = int(a[a.index("-m") + 1], 8), a[a.index("-m") + 2:]
    if "-d" in a:
        for p in paths: pathlib.Path(p).mkdir(parents=True, exist_ok=True); os.chmod(p, mode)
    else:
        for p in paths[:-1]:
            dest = pathlib.Path(paths[-1]); dest = dest / pathlib.Path(p).name if dest.is_dir() else dest
            shutil.copyfile(p, dest); os.chmod(dest, mode)
elif name == "chmod": os.chmod(a[1], int(a[0], 8))
elif name == "dpkg-query":
    for p in a[2:]: print(p + "\\tfixture-version")
elif name == "sha256sum":
    for p in a: print(hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest() + "  " + p)
'''


class BenchRecipeTest(unittest.TestCase):
    def test_no_host_shares(self):
        text = (RECIPE / "lima.yaml").read_text()
        config = json.loads("\n".join(line for line in text.splitlines() if not line.startswith("#")))
        self.assertEqual(config["minimumLimaVersion"], "2.2.1")
        self.assertEqual([config[k] for k in ("vmType", "os", "arch", "cpus", "memory", "disk")],
                         ["vz", "Linux", "aarch64", 2, "2GiB", "20GiB"])
        self.assertEqual(config["images"], [{
            "location": "https://cloud.debian.org/images/cloud/trixie/20261001-2618/"
                        "debian-13-genericcloud-arm64-20261001-2618.qcow2",
            "arch": "aarch64",
            "digest": "sha512:d8470b8c6c38fead046c794b5800a5a7b96672d5bcf543cc230ceb0c4b8ace05ed"
                      "341a0c8928045422243fde26b2f1f2f58e99244c65709ffda2e3d4b674dd5a",
        }])
        self.assertTrue(config["plain"])
        self.assertEqual(config["mounts"], [])
        self.assertFalse(any(config["ssh"].values()))
        self.assertFalse(any(config["containerd"].values()))
        self.assertFalse(config["propagateProxyEnv"])
        self.assertEqual(config["portForwards"], [dict(guestIP="0.0.0.0", guestPortRange=[1, 65535],
                                                     proto="any", ignore=True)])

    def provision(self, fail="", exists=False, minor=13, uid=0, args=(),
                  key="ssh-ed25519 fixture-public-key\n"):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        bundle, binary = root / "bundle", root / "bin"
        shutil.copytree(RECIPE, bundle)
        binary.mkdir()
        (root / "etc/ssh").mkdir(parents=True)
        (root / "etc/ssh/sshd_config").write_text("# existing Lima admin configuration\n")
        for name in ("guest.py", "archive.py"):
            shutil.copyfile(REPO / "hansa/bench" / name, bundle / name)
        if key is not None:
            (bundle / "submission.pub").write_text(key)
        script = (bundle / "provision.sh").read_text()
        for prefix in ("/etc/", "/usr/local/", "/var/lib/"):
            script = script.replace(prefix, str(root) + prefix)
        (bundle / "provision.sh").write_text(script)
        fake = binary / "fake"
        fake.write_text("#!" + sys.executable + "\n" + FAKE)
        fake.chmod(0o755)
        for name in ("id dirname apt-get python3 useradd usermod passwd install cat chown chmod "
                     "dpkg-query sha256sum sysctl nft systemctl sshd").split():
            (binary / name).symlink_to(fake)
        (root / "log").touch()
        env = dict(PATH=str(binary), FAKE_LOG=str(root / "log"), FAKE_FAIL=fail,
                   BENCH_EXISTS="1" if exists else "", PY_MINOR=str(minor), FAKE_UID=str(uid))
        result = subprocess.run(["/bin/sh", str(bundle / "provision.sh"), *args], env=env,
                                capture_output=True, text=True, timeout=30)
        calls = [json.loads(line) for line in (root / "log").read_text().splitlines()]
        return root, calls, result

    def test_submission_key_required_before_install(self):
        for key in (None, ""):
            with self.subTest(key=key):
                root, calls, result = self.provision(key=key)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("submission.pub", result.stderr)
                self.assertIn("missing or empty", result.stderr)
                self.assertFalse(any(c[0] in ("apt-get", "install") for c in calls))
                self.assertFalse((root / "usr/local").exists())

    def test_forced_account_keeps_admin(self):
        root, calls, result = self.provision()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(["useradd", "-m", "-s", "/bin/sh", "bench"], calls)
        self.assertIn(["usermod", "-G", "", "-s", "/bin/sh", "bench"], calls)
        self.assertIn(["passwd", "-l", "bench"], calls)
        self.assertEqual((root / "etc/ssh/sshd_config").read_text(), "# existing Lima admin configuration\n")
        self.assertIn(["apt-get", "install", "-y", "--no-install-recommends", "bubblewrap", "nftables", "tmux",
                       "chromium", "fonts-dejavu-core", "git", "python3", "openssh-server"], calls)
        match = (root / "etc/ssh/sshd_config.d/50-bench.conf").read_text().splitlines()
        self.assertEqual((match[0], match[-1]), ("Match User bench", "Match all"))
        options = dict(line.strip().split(" ", 1) for line in match[1:-1])
        for name in ("PermitTTY AllowTcpForwarding AllowStreamLocalForwarding AllowAgentForwarding "
                     "X11Forwarding PermitTunnel PermitUserRC PasswordAuthentication KbdInteractiveAuthentication").split():
            self.assertEqual(options[name], "no")
        self.assertEqual(options["AuthenticationMethods"], "publickey")
        self.assertEqual(options["AuthorizedKeysFile"], str(root / "etc/ssh/bench/authorized_keys"))
        launcher = Path(options["ForceCommand"])
        # Exercise the installed gateway and shared module, including Python's isolated imports.
        launcher.write_text(launcher.read_text().replace("/usr/bin/python3", shlex.quote(sys.executable)))
        # A concurrent fork can inherit the writer until exec, causing ETXTBSY on Linux.
        # Reading the rewritten launcher through sh avoids executing a busy inode.
        refused = subprocess.run(["/bin/sh", str(launcher)], env=dict(SSH_ORIGINAL_COMMAND="echo forbidden"),
                                 capture_output=True, text=True, timeout=30)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("gateway infrastructure failure", refused.stderr)
        for path, mode in (("etc/ssh/bench/authorized_keys", "0644"), ("var/lib/hansa-bench", "0700")):
            self.assertEqual((root / path).stat().st_mode & 0o777, int(mode, 8))
            self.assertTrue(any(c[0] == "install" and mode in c and str(root / path) in c for c in calls))
        for call in (c for c in calls if c[0] == "install"):
            self.assertEqual(int(call[call.index("-m") + 1], 8) & 0o022, 0, call)
            owner = "bench" if str(root / "var/lib/hansa-bench") in call else "root"
            self.assertEqual(call[call.index("-o") + 1], owner)
            self.assertEqual(call[call.index("-g") + 1], owner)
        self.assertIn(["chown", "root:root", str(launcher)], calls)
        self.assertIn(["chmod", "0755", str(launcher)], calls)
        self.assertIn("ssh_deletekeys: false", (root / "etc/cloud/cloud.cfg.d/99-bench-keep-host-keys.cfg").read_text())
        self.assertIn("ssh_genkeytypes: []", (root / "etc/cloud/cloud.cfg.d/99-bench-keep-host-keys.cfg").read_text())
        self.assertIn("bubblewrap\tfixture-version", (root / "var/lib/hansa-bench-build/packages.tsv").read_text())
        self.assertIn("lima.yaml", (root / "var/lib/hansa-bench-build/recipe.sha256").read_text())
        self.assertLess(calls.index(["sshd", "-t"]), calls.index(["systemctl", "reload", "ssh.service"]))
        _, again, result = self.provision(exists=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(c[0] == "useradd" for c in again))
        _, stopped, result = self.provision(minor=12)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(c[0] == "useradd" for c in stopped))
        for options in (dict(uid=1000), dict(args=("unexpected",))):
            _, stopped, result = self.provision(**options)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(any(c[0] == "apt-get" for c in stopped))

    def test_bench_drop_first(self):
        root, calls, result = self.provision()
        self.assertEqual(result.returncode, 0, result.stderr)
        edge = (root / "etc/nftables.conf").read_text()
        self.assertIn("table inet bench_edge", edge)
        self.assertLess(edge.index("flush ruleset"), edge.index("table inet bench_edge"))
        self.assertIn("type filter hook output priority 0; policy accept;", edge)
        rules = [line.strip() for line in edge.splitlines() if line.strip().endswith(("drop", "accept"))]
        self.assertEqual(rules[:3], ['meta skuid "bench" counter drop', 'oifname "lo" accept',
                                    'ct state established,related accept'])
        self.assertEqual(rules[3:7], ['ip daddr 192.168.5.2 udp dport 53 accept',
                                    'ip daddr 192.168.5.2 tcp dport 53 accept', 'udp sport 68 udp dport 67 accept',
                                    'ip daddr 192.168.5.2 drop'])
        for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10",
                     "169.254.0.0/16", "224.0.0.0/4", "240.0.0.0/4"):
            self.assertIn(cidr, rules[7])
        self.assertEqual(rules[-1], "meta nfproto ipv6 drop")
        self.assertNotIn("198.18", edge)
        self.assertEqual((root / "etc/sysctl.d/90-hansa-bench.conf").read_text().splitlines(),
                         ["net.ipv6.conf.all.disable_ipv6 = 1", "net.ipv6.conf.default.disable_ipv6 = 1"])
        order = [["nft", "-c", "-f", str(root / "etc/nftables.conf")],
                 ["nft", "-f", str(root / "etc/nftables.conf")], ["systemctl", "enable", "--now", "nftables.service"],
                 ["sshd", "-t"], ["systemctl", "reload", "ssh.service"]]
        self.assertEqual(sorted(calls.index(c) for c in order), [calls.index(c) for c in order])
        for failure in ("python3", "sysctl", "nft", "systemctl", "sshd"):
            _, stopped, result = self.provision(fail=failure)
            self.assertNotEqual(result.returncode, 0, failure)
            self.assertNotIn(["systemctl", "reload", "ssh.service"], stopped)
