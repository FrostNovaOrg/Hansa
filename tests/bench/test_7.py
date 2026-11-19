"""Management through the CLI and page, with fake Lima/SSH and no network."""
import contextlib
import hashlib
import io
import os
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from hansa import cli, web
from hansa.bench import image, vm
from hansa.desk import Desk, default_settings, validate
from tests.bench.test_6 import KEY


def desk_at(root) -> Desk:
    """A desk in this folder, holding what hansa init makes and what makes a folder a desk (Desk.is_desk):
    settings.json and tasks/."""
    desk = Desk(root)
    desk.tasks_dir.mkdir(parents=True, exist_ok=True)
    desk.settings_path.write_text("{}\n", encoding="utf-8")
    return desk


class BenchManagementTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.desk = desk_at(temporary.name)
        self.calls, self.fail_action, self.status = [], "", "Stopped"
        # Lima's download, faked: the recipe names IMAGE's bytes; a start without them in the private cache downloads.
        self.cache = Path(temporary.name) / "account-cache"  # the account's own cache folder, never the real one
        self.cache.mkdir()
        self.image, self.downloads, self.download_breaks = b"guest image bytes", 0, False

    URL = "https://images.example/debian-arm64.qcow2"

    def recipe_image(self, recipe=None):
        return self.URL, hashlib.sha512(self.image).hexdigest()

    def lima_start(self, private, options):
        entry = image.lima_entry(private / "Library" / "Caches", self.URL)
        data, digest = entry / "data", entry / "sha512.digest"
        if not (data.is_file() and digest.read_text() == "sha512:" + hashlib.sha512(self.image).hexdigest()):
            if self.download_breaks:
                options["stderr"].write(b"\n".join(b"progress line %d " % i + b"x" * 300 for i in range(20)) +
                                        b'\nlevel=fatal msg="failed to download \\"' + self.URL.encode() +
                                        b'\\": unexpected EOF"\n')
                entry.mkdir(parents=True, exist_ok=True)
                (entry / "data.tmp").write_bytes(self.image[:5])
                raise subprocess.CalledProcessError(1, ["limactl"])
            entry.mkdir(parents=True, exist_ok=True)
            data.write_bytes(self.image)
            digest.write_text("sha512:" + hashlib.sha512(self.image).hexdigest())
            self.downloads += 1

    def run_command(self, command, **options):
        self.calls.append(command)
        self.assertTrue(0 < options["timeout"] <= (3600 if "--name=" + vm.INSTANCE in command else 900))
        if self.fail_action and self.fail_action in command:
            raise subprocess.CalledProcessError(1, command)
        private = Path(options["env"]["HOME"])
        output = b""
        if command[0] == "ssh-keygen":
            (private / "submission").write_text("private fixture")
            (private / "submission").chmod(0o600)
            (private / "submission.pub").write_text(KEY)
        elif command[1] == "start":
            if "--name=" + vm.INSTANCE in command:
                self.lima_start(private, options)
            (private / "lima" / vm.INSTANCE).mkdir(exist_ok=True)
            self.status = "Running"
        elif command[1] == "stop":
            self.status = "Stopped"
        elif command[1] == "list":
            output = json.dumps(dict(name=vm.INSTANCE, status=self.status, sshLocalPort=2200)).encode()
        elif "tar" in command:
            from hansa.bench.manage import FILES
            with tarfile.open(fileobj=options["stdin"]) as bundle:
                self.assertEqual(set(bundle.getnames()), {p.name for p in FILES} | {"submission.pub"})
                for path in FILES:
                    self.assertEqual(bundle.extractfile(path.name).read(), path.read_bytes())
        elif "cat" in command:
            output = (KEY + " fixture-comment\n").encode()
        return subprocess.CompletedProcess(command, 0, output)

    @contextlib.contextmanager
    def fake_image(self):
        with patch.object(image, "account_cache", return_value=self.cache), \
                patch.object(image, "recipe_image", side_effect=self.recipe_image):
            yield

    def command(self, action, *args):
        initialize = vm.VM.__init__
        factory = lambda machine, private: initialize(machine, private, run=self.run_command, clock=lambda: 0)
        with patch.object(vm.VM, "__init__", factory), patch.object(vm.sys, "platform", "darwin"), \
                (self.fake_image() if hasattr(self, "cache") else contextlib.nullcontext()), \
                patch.object(cli, "_as_desk_account"), patch.object(cli.notify, "session_here", return_value=False), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            cli.main(["--desk", str(self.desk.root), "bench", action, *args])
        return out.getvalue()

    def test_page_preserves_bench(self):
        self.assertFalse(default_settings()["bench"]["enabled"])
        settings = self.desk.settings()
        bench = dict(enabled=False, generation=3, build="failed", recipe="fixture", failure="stopped",
                     acceptance=dict(generation=2, status="passed"))
        self.assertEqual(self.desk.save_settings({**settings, "bench": bench}), [])
        for value in (None, {"enabled": True}, {}):
            result = web._save_settings(self.desk, {"settings": {**settings, "bench": value}})
            self.assertTrue(result["ok"], result)
            self.assertEqual(self.desk.settings()["bench"], bench)
        settings.pop("bench")
        self.assertEqual(self.desk.save_settings(settings, by="page"), [])
        self.assertEqual(self.desk.settings()["bench"], bench)
        for value in (None, {**bench, "enabled": 1}, {**bench, "generation": -1}, {**bench, "acceptance": True}):
            self.assertTrue(any("bench" in p for p in validate({**settings, "bench": value})))

    def test_limactl_failure_messages_are_redacted(self):
        key = 'fixture-private-"provider\\key'
        self.desk.secrets_dir.mkdir()
        self.desk.key_file("fixture").write_text(key)
        run = self.run_command
        def fail(command, **options):
            if command[0] == "limactl" and ("sh" in command or "--version" in command or
                                           (action in ("build", "selftest") and command[1] == "stop")):
                return subprocess.run([sys.executable, "-c", "import sys; sys.stderr.write(" +
                                       repr("Lima refused " + key) + "); sys.exit(1)"], **options)
            return run(command, **options)
        for action in ("build", "rebuild", "selftest"):
            if action == "selftest":
                self.command("rebuild")
            with patch.object(self, "run_command", side_effect=fail):
                if action == "selftest":
                    shown = self.command(action, "--json")
                    self.assertIn("Lima refused", json.loads(shown)["acceptance"]["results"]["stopped"]["error"])
                else:
                    with self.assertRaises(SystemExit) as caught:
                        self.command(action)
                    shown = str(caught.exception)
            failure = self.desk.settings()["bench"]["failure"]
            self.assertIn("Lima refused [key of fixture]", failure)
            self.assertIn("Lima refused [key of fixture]", shown)
            if action == "build":
                self.assertIn("Lima refused [key of fixture]", failure.split("; stop: ")[1])
            self.assertNotIn(key, shown + json.dumps(self.desk.settings()) +
                             (self.desk.state_dir / "settings-history.jsonl").read_text())

    def test_page_bench_guard_independent_of_desk_guard(self):
        settings, save = self.desk.settings(), self.desk.save_settings
        changed = {**settings["bench"], "generation": 7}
        # Isolate the web guard: Desk also independently preserves bench for by="page".
        with patch.object(self.desk, "save_settings", side_effect=lambda new, **kw: save(new)):
            self.assertTrue(web._save_settings(self.desk, {"settings": {**settings, "bench": changed}})["ok"])
        self.assertEqual(self.desk.settings()["bench"], settings["bench"])

    def test_first_build_allows_twenty_minute_download(self):
        now, call, run = [0], vm.VM._call, self.run_command
        def timed(machine, *args, **kwargs):
            machine.clock = lambda: now[0]
            return call(machine, *args, **kwargs)
        def download(command, **options):
            if "--name=" + vm.INSTANCE in command:
                if options["timeout"] < 1200:
                    raise subprocess.TimeoutExpired(command, options["timeout"])
                now[0] += 1200
            return run(command, **options)
        with patch.object(vm.VM, "_call", timed), patch.object(self, "run_command", side_effect=download):
            self.command("build")
        self.assertEqual((now[0], self.desk.settings()["bench"]["build"]), (1200, "ready"))

    def check_redaction_precedes_stderr_cut(self, action, error):
        key = "fixture-secret-" + "k" * 15
        self.desk.secrets_dir.mkdir(exist_ok=True)
        self.desk.key_file("fixture").write_text(key)
        if action in ("selftest", "off"):
            self.command("build")
        fail_action = dict(build="sh", rebuild="sh", selftest="--version", off="stop")[action]
        run = self.run_command
        def fail(command, **options):
            if fail_action in command:
                options["stderr"].write(("prefix " + key + "x" * 4074 + "\nended").encode())
                raise error(command, options["timeout"]) if error is subprocess.TimeoutExpired else error(1, command)
            return run(command, **options)
        with patch.object(self, "run_command", side_effect=fail):
            if action == "selftest":
                shown = self.command(action, "--json")
                self.assertIn("[key of fixture]", json.loads(shown)["acceptance"]["error"])
            else:
                with self.assertRaises(SystemExit) as caught:
                    self.command(action)
                shown = str(caught.exception)
        failure = self.desk.settings()["bench"]["failure"]
        self.assertIn("[key of fixture]", failure)
        self.assertTrue(failure.endswith("ended"))
        self.assertNotIn(key[-15:], shown + json.dumps(self.desk.settings()) +
                         (self.desk.state_dir / "settings-history.jsonl").read_text())

    def test_redaction_precedes_stderr_cut_on_failure_and_timeout(self):
        for action, error in (("build", subprocess.CalledProcessError), ("rebuild", subprocess.TimeoutExpired)):
            self.check_redaction_precedes_stderr_cut(action, error)

    def test_selftest_redaction_precedes_stderr_cut(self):
        self.check_redaction_precedes_stderr_cut("selftest", subprocess.CalledProcessError)

    def test_off_redaction_precedes_stderr_cut(self):
        self.check_redaction_precedes_stderr_cut("off", subprocess.TimeoutExpired)

    def test_serve_broker_redacts_lima_errors_before_cut(self):
        from hansa.run import WorkerBench
        key = "fixture-secret-" + "k" * 15
        self.desk.secrets_dir.mkdir()
        self.desk.key_file("fixture").write_text(key)
        self.desk.state_dir.mkdir()
        with patch.object(vm.sys, "platform", "darwin"):
            bench = WorkerBench(self.desk)
        self.addCleanup(bench.close)
        for error in (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            for padding in ("", "x" * 4074):
                def fail(command, **options):
                    options["stderr"].write(("prefix " + key + padding + "\nended").encode())
                    raise error(command, options["timeout"]) if error is subprocess.TimeoutExpired else error(1, command)
                with self.subTest(error=error.__name__, cut=bool(padding)), \
                        patch.object(bench.broker.vm, "run", side_effect=fail):
                    with self.assertRaises(error) as caught:
                        bench.broker.vm.stop(bench.broker.vm.clock() + 30)
                    shown = str(caught.exception)
                    self.assertIn("[key of fixture]", shown)
                    self.assertTrue(shown.endswith("ended"))
                    self.assertNotIn(key, shown)
                    self.assertNotIn(key[-15:], shown)

    def test_build_failure_recorded(self):
        self.fail_action = "sh"
        with self.assertRaises(SystemExit):
            self.command("build")
        bench = self.desk.settings()["bench"]
        self.assertEqual((bench["enabled"], bench["generation"], bench["build"], bench["acceptance"]),
                         (False, 1, "failed", None))
        self.assertIn("CalledProcessError", bench["failure"])
        self.assertEqual(self.status, "Stopped")
        history = (self.desk.state_dir / "settings-history.jsonl").read_text()
        self.assertIn('"bench.build"', history)
        self.assertIn('"failed"', history)
        self.fail_action = ""
        self.command("rebuild")
        self.assertEqual(self.desk.settings()["bench"]["generation"], 2)
        self.assertEqual(sum(c[0] == "ssh-keygen" for c in self.calls), 2)
        from hansa.bench.manage import exclusive
        with exclusive(self.desk), self.assertRaisesRegex(SystemExit, "busy"):
            self.command("rebuild")

    def test_on_requires_current_pass(self):
        with self.assertRaisesRegex(SystemExit, "selftest"):
            self.command("on")
        self.command("build")
        self.assertEqual(self.status, "Stopped")
        settings = self.desk.settings()
        for acceptance in (None, dict(generation=2, status="passed"), dict(generation=1, status="failed"),
                           dict(generation=1, status="incomplete")):
            settings["bench"]["acceptance"] = acceptance
            self.assertEqual(self.desk.save_settings(settings), [])
            with self.assertRaisesRegex(SystemExit, "selftest"):
                self.command("on")
        from hansa.bench.selftest import mandatory
        settings["bench"]["acceptance"] = dict(generation=1, status="passed", results={
            n: dict(status="passed", checks=dict.fromkeys(fields, "passed")) for n, fields in mandatory().items()})
        self.assertEqual(self.desk.save_settings(settings), [])
        with patch("hansa.bench.manage.recipe_hash", return_value="changed"), self.assertRaisesRegex(SystemExit, "selftest"):
            self.command("on")
        read = Path.read_bytes
        for name in ("broker.py", "channel.py", "snapshot.py", "vm.py", "probes.py", "selftest.py"):
            with self.subTest(changed=name), patch.object(Path, "read_bytes",
                    new=lambda p: read(p) + (b"\n# upgrade\n" if p.name == name else b"")):
                with self.assertRaisesRegex(SystemExit, "selftest"): self.command("on")
        self.assertIn("bench: on", self.command("on"))
        for action in ("build", "rebuild"):
            with self.assertRaisesRegex(SystemExit, "off"):
                self.command(action)
        self.assertTrue(json.loads(self.command("status", "--json"))["enabled"])
        self.fail_action = "stop"
        with self.assertRaises(SystemExit):
            self.command("off")
        self.assertFalse(self.desk.settings()["bench"]["enabled"])
        self.assertIsNone(self.desk.settings()["bench"]["acceptance"])
        self.fail_action = ""
        self.assertIn("bench: off", self.command("off"))
        self.command("rebuild")
        with self.assertRaisesRegex(SystemExit, "selftest"):
            self.command("on")


    # The guest's system image is kept between rebuilds, in .hansa/bench-image/, checked against the recipe.

    def kept(self):
        folder = image.kept_dir(self.desk)
        return {p.name: p.read_bytes() for p in folder.iterdir()} if folder.is_dir() else {}

    def kept_name(self):
        return "sha512-" + hashlib.sha512(self.image).hexdigest()

    def test_image_downloaded_once_then_kept_across_rebuilds(self):
        self.command("build")
        self.assertEqual((self.downloads, self.kept()), (1, {self.kept_name(): self.image}))
        folder = image.kept_dir(self.desk)
        self.assertEqual((folder.parent, oct(folder.stat().st_mode & 0o777)), (self.desk.state_dir, "0o700"))
        self.assertEqual(oct((folder / self.kept_name()).stat().st_mode & 0o777), "0o600")
        (self.desk.state_dir / "bench" / "marker").write_text("old")
        out = self.command("rebuild")
        self.assertEqual(self.downloads, 1)  # no second download
        self.assertFalse((self.desk.state_dir / "bench" / "marker").exists())  # the rest is fresh
        self.assertEqual(sum(c[0] == "ssh-keygen" for c in self.calls), 2)
        self.assertEqual(self.kept(), {self.kept_name(): self.image})
        self.assertNotIn("already on this machine", out)
        self.assertEqual(self.desk.settings()["bench"]["build"], "ready")

    def account_entry(self):
        return image.lima_entry(self.cache, self.URL)

    def test_good_copy_in_the_accounts_lima_cache_is_copied_not_downloaded(self):
        entry = self.account_entry()
        self.assertEqual(entry.name, hashlib.sha256(self.URL.encode()).hexdigest())
        entry.mkdir(parents=True)
        (entry / "data").write_bytes(self.image)
        out = self.command("rebuild")
        self.assertEqual(self.downloads, 0)
        self.assertIn("used the system image already on this machine", out)
        self.assertEqual(self.kept(), {self.kept_name(): self.image})
        kept = image.kept_dir(self.desk) / self.kept_name()
        self.assertFalse(kept.is_symlink())
        self.assertNotEqual(kept.stat().st_ino, (entry / "data").stat().st_ino)  # a copy, not a link or a move
        self.assertEqual((entry / "data").read_bytes(), self.image)

    def test_other_things_in_the_accounts_lima_cache_are_ignored_and_left(self):
        other = Path(self.cache) / "elsewhere"
        other.write_bytes(self.image)
        for kind in ("changed", "link", "folder"):
            with self.subTest(kind=kind):
                entry = self.account_entry()
                entry.mkdir(parents=True, exist_ok=True)
                data = entry / "data"
                if kind == "changed":
                    data.write_bytes(b"G" + self.image[1:])
                elif kind == "link":
                    data.symlink_to(other)
                else:
                    data.mkdir()
                    (data / "inside").write_bytes(self.image)
                downloads = self.downloads
                out = self.command("rebuild")
                self.assertEqual(self.downloads, downloads + 1)
                self.assertNotIn("already on this machine", out)
                self.assertEqual((data.is_symlink(), data.is_dir(), data.is_file()),
                                 (kind == "link", kind == "folder", kind != "folder"))
                if kind == "changed":
                    self.assertEqual(data.read_bytes(), b"G" + self.image[1:])
                if kind == "folder":
                    self.assertEqual((data / "inside").read_bytes(), self.image)
                self.assertEqual(self.kept(), {self.kept_name(): self.image})
                image.kept_dir(self.desk).joinpath(self.kept_name()).unlink()
                if kind == "folder":
                    (data / "inside").unlink()
                    data.rmdir()
                else:
                    data.unlink()
        self.assertEqual(other.read_bytes(), self.image)

    def test_unusable_kept_image_is_removed_and_downloaded_again(self):
        target = self.desk.state_dir / "outside"
        for kind in ("changed byte", "truncated", "link", "folder", "group-readable"):
            with self.subTest(kind=kind):
                self.command("rebuild")
                kept = image.kept_dir(self.desk) / self.kept_name()
                kept.unlink()
                if kind == "changed byte":
                    kept.write_bytes(self.image[:-1] + b"X")
                elif kind == "truncated":
                    kept.write_bytes(self.image[:4])
                elif kind == "link":
                    target.write_bytes(self.image)
                    kept.symlink_to(target)
                elif kind == "folder":
                    kept.mkdir()
                else:
                    kept.write_bytes(self.image)
                if not kept.is_dir():
                    os.chmod(kept, 0o640 if kind == "group-readable" else 0o600, follow_symlinks=False)
                downloads = self.downloads
                self.command("rebuild")
                self.assertEqual(self.downloads, downloads + 1)
                self.assertEqual(self.kept(), {self.kept_name(): self.image})
                self.assertFalse(kept.is_symlink())
        self.assertEqual(target.read_bytes(), self.image)  # a link's target is not touched

    def test_broken_download_says_so_keeps_nothing_and_starts_again(self):
        self.download_breaks = True
        with self.assertRaises(SystemExit) as caught:
            self.command("rebuild")
        shown = str(caught.exception)
        self.assertTrue(shown.startswith("hansa bench: The bench's system image could not be downloaded; "
                                         "run hansa bench rebuild again"), shown)
        self.assertIn("is kept", shown.splitlines()[0])
        lima = shown.splitlines()[1:]
        self.assertTrue(lima[0].startswith("Lima: "), shown)
        self.assertLessEqual(len(lima), 3)
        self.assertTrue(all(len(line) <= 210 for line in lima), shown)
        self.assertIn("unexpected EOF", lima[-1])
        self.assertEqual(self.kept(), {})
        bench = self.desk.settings()["bench"]
        self.assertEqual(bench["build"], "failed")
        self.assertTrue(bench["failure"].startswith(image.DOWNLOAD_FAILED))
        status = self.command("status")
        self.assertTrue(status.startswith("bench: off; " + image.DOWNLOAD_FAILED + " {"), status)
        self.assertNotIn(image.DOWNLOAD_FAILED, self.command("status", "--json").split('"failure"')[0])
        self.download_breaks = False
        self.command("rebuild")
        self.assertEqual((self.downloads, self.kept()), (1, {self.kept_name(): self.image}))
        self.assertNotIn(image.DOWNLOAD_FAILED, self.command("status"))

    def test_other_failures_are_not_called_a_download_failure(self):
        self.fail_action = "sh"
        with self.assertRaises(SystemExit) as caught:
            self.command("rebuild")
        self.assertNotIn("could not be downloaded", str(caught.exception))
        self.assertNotIn("could not be downloaded", self.command("status"))
        self.assertEqual(self.kept(), {self.kept_name(): self.image})  # downloaded before provisioning failed

    def test_new_recipe_image_replaces_the_old_once_in_place(self):
        self.command("build")
        old = self.kept_name()
        self.image = b"next guest image"
        self.download_breaks = True
        with self.assertRaises(SystemExit):
            self.command("rebuild")
        self.assertEqual(set(self.kept()), {old})  # kept until the new one is in place
        self.download_breaks = False
        self.command("rebuild")
        self.assertEqual((self.downloads, self.kept()), (2, {self.kept_name(): self.image}))

    def test_check_uses_hansas_own_sha512_on_real_files(self):
        root = Path(tempfile.mkdtemp(dir=self.desk.root))
        digest = hashlib.sha512(self.image).hexdigest()
        good = root / "good"
        good.write_bytes(self.image)
        good.chmod(0o600)
        self.assertTrue(image._copy_checked(good, root / "copy", digest))
        self.assertEqual((root / "copy").read_bytes(), self.image)
        changed = root / "changed"
        changed.write_bytes(self.image[:3] + bytes([self.image[3] ^ 1]) + self.image[4:])
        link = root / "link"
        link.symlink_to(good)
        for source in (changed, link, root / "missing", root):
            with self.subTest(source=source.name):
                self.assertFalse(image._copy_checked(source, root / "out", digest))
                self.assertFalse(os.path.lexists(root / "out"))  # nothing unusable is left
        # A digest file beside the bytes is not believed: hansa hashes them itself.
        (root / "sha512.digest").write_text("sha512:" + digest)
        self.assertFalse(image._copy_checked(changed, root / "out", digest))
        self.assertTrue(image._kept_ok(good))
        self.assertFalse(image._kept_ok(link))
        with patch.object(image.os, "getuid", return_value=good.stat().st_uid + 1):
            self.assertFalse(image._kept_ok(good))  # not the account's
            self.assertFalse(image._copy_checked(good, root / "out", digest))
            self.assertFalse(os.path.lexists(root / "out"))
        self.assertTrue(image._copy_checked(good, root / "out", digest, own=False) is True)

    def test_recipe_names_a_sha512_digest(self):
        location, digest = image.recipe_image()
        self.assertTrue(location.startswith("https://") and len(digest) == 128)
