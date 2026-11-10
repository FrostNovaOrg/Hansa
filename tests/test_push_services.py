"""Push services and child-only HTTP acknowledgements, without a network."""
from contextlib import ExitStack, redirect_stdout
import base64
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from hansa import __version__, alert_chat, alert_http, alert_push, alert_services, views
from hansa.cli import init_desk, main
from hansa.desk import public_settings, read_json, write_json


def bark_example(key="1234567890123456", bits=128, mode="cbc", device="fixture-bark-key"):
    return (f"#!/bin/bash\nkey='{key}'\niv='copied-iv-unused'\n"
            f"ciphertext=$(printf '%s' '{{\"body\":\"test\"}}' | openssl enc -aes-{bits}-{mode} "
            "-e -K $(printf '%s' \"$key\" | xxd -p) -iv $(printf '%s' \"$iv\" | xxd -p) | base64)\n"
            f"curl 'https://api.day.app/{device}' -d \"ciphertext=$ciphertext\" -d \"iv=$iv\"\n")


def decrypt_bark(payload, key):
    """Test-only AES inverse, using the schedule covered by the FIPS vectors."""
    from hansa.aes import AES, SBOX

    def multiply(a, b):
        out = 0
        for _ in range(8):
            if b & 1:
                out ^= a
            a = ((a << 1) ^ (0x11b if a & 128 else 0)) & 255
            b >>= 1
        return out

    inverse = [SBOX.index(i) for i in range(256)]
    cipher = AES(key.encode("ascii"))
    data = base64.b64decode(payload["ciphertext"], validate=True)
    previous, plain = payload["iv"].encode("ascii"), bytearray()
    for offset in range(0, len(data), 16):
        block = data[offset:offset + 16]
        state = list(block)
        for round_key in reversed(cipher._round_keys[1:]):
            state = [a ^ b for a, b in zip(state, round_key)]
            if round_key != cipher._round_keys[-1]:
                for col in range(4):
                    a = state[col * 4:col * 4 + 4]
                    for row in range(4):
                        state[col * 4 + row] = (multiply(a[row], 14) ^ multiply(a[(row + 1) % 4], 11)
                                               ^ multiply(a[(row + 2) % 4], 13) ^ multiply(a[(row + 3) % 4], 9))
            state = [state[((col - row) % 4) * 4 + row] for col in range(4) for row in range(4)]
            state = [inverse[x] for x in state]
        state = [a ^ b for a, b in zip(state, cipher._round_keys[0])]
        plain.extend(a ^ b for a, b in zip(state, previous))
        previous = block
    padding = plain[-1]
    assert 1 <= padding <= 16 and plain[-padding:] == bytes([padding]) * padding
    return bytes(plain[:-padding])


class AESTest(unittest.TestCase):
    def test_fips_197_all_key_sizes(self):
        from hansa.aes import AES
        for size, expected in ((16, "69c4e0d86a7b0430d8cdb78070b4c55a"),
                               (24, "dda97ca4864cdfe06eaf70a0ec0d7191"),
                               (32, "8ea2b7ca516745bfeafc49904b496089")):
            with self.subTest(size=size):
                self.assertEqual(AES(bytes(range(size))).encrypt_block(
                    bytes.fromhex("00112233445566778899aabbccddeeff")).hex(), expected)

    def test_bark_cbc_vector_and_pkcs7_boundaries(self):
        from hansa.aes import cbc_encrypt
        key = b"1234567890123456"
        result = cbc_encrypt(key, key, b'{"body": "test", "sound": "birdsong"}')
        self.assertEqual(base64.b64encode(result).decode(),
                         "+aPt5cwN9GbTLLSFri60l3h1X00u/9j1FENfWiTxhNHVLGU+XoJ15JJG5W/d/yf0")
        for size in (16, 24, 32):
            for length in (0, 1, 15, 16, 17, 31, 32, 33):
                with self.subTest(size=size, length=length):
                    raw = bytes(range(length))
                    ciphertext = cbc_encrypt(b"k" * size, key, raw)
                    self.assertEqual(len(ciphertext), (length // 16 + 1) * 16)
                    self.assertEqual(decrypt_bark({"iv": key.decode(), "ciphertext":
                        base64.b64encode(ciphertext).decode()}, "k" * size), raw)

    def test_invalid_key_block_and_iv_lengths_are_refused(self):
        from hansa.aes import AES, cbc_encrypt
        for size in (0, 15, 17, 23, 25, 31, 33):
            with self.subTest(size=size), self.assertRaises(ValueError):
                AES(b"k" * size)
        for size in (0, 15, 17):
            with self.subTest(size=size):
                with self.assertRaises(ValueError):
                    AES(b"k" * 16).encrypt_block(b"x" * size)
                with self.assertRaises(ValueError):
                    cbc_encrypt(b"k" * 16, b"x" * size, b"body")


SAVED_SERVICE_CASES = (
    ("bark", "https://api.day.app/fixture-bark-key", "fixture-bark-key", "", "api.day.app/…ey"),
    ("ntfy", "https://ntfy.sh/fixture-private-topic", "fixture-private-topic", "tk_fixture-token", "ntfy.sh/…"),
    ("gotify", "https://gotify.example/message?token=fixture-gotify-token", "fixture-gotify-token", "",
     "gotify.example/message"),
    ("gotify", "https://gotify-extra.example", "fixture-gotify-extra-token", "fixture-gotify-extra-token",
     "gotify-extra.example/message"),
    ("pushdeer", "fixture-pushdeer-key", "fixture-pushdeer-key", "", "api2.pushdeer.com/message/push?pushkey=…"),
    ("serverchan", "SCTfixture-send-key", "SCTfixture-send-key", "", "sctapi.ftqq.com/…"),
    ("serverchan", "sctp123tfixture-third-key", "sctp123tfixture-third-key", "", "123.push.ft07.com/send/…"),
)


CHAT_SERVICE_CASES = (
    ("wecom", "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=fixture-wecom-key", "fixture-wecom-key", "",
     "qyapi.weixin.qq.com/cgi-bin/webhook/send?key=…"),
    ("dingtalk", "https://oapi.dingtalk.com/robot/send?access_token=fixture-dingtalk-token", "fixture-dingtalk-token",
     "SECfixture-signing-secret", "oapi.dingtalk.com/robot/send?access_token=…"),
    ("feishu", "https://open.feishu.cn/open-apis/bot/v2/hook/fixture-hook-id", "fixture-hook-id",
     "fixture-feishu-signing-secret", "open.feishu.cn/open-apis/bot/v2/hook/…"),
    ("telegram", "https://api.telegram.org/bot123456:fixture-telegram-token/sendMessage?chat_id=-100123456",
     "123456:fixture-telegram-token", "", "api.telegram.org/bot…/sendMessage?chat_id=-100123456"),
    ("discord", "https://discord.com/api/webhooks/123456/fixture-discord-token", "fixture-discord-token", "",
     "discord.com/api/webhooks/123456/…"),
    ("slack", "https://hooks.slack.com/services/T12345/B12345/fixture-slack-token",
     "/services/T12345/B12345/fixture-slack-token", "", "hooks.slack.com/services/…"),
    ("json", "https://home.example/automation/fixture-private-path?key=fixture-query-key",
     "https://home.example/automation/fixture-private-path?key=fixture-query-key", "fixture-bearer-token", "home.example/…"),
)


class _PushServicesCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.tmp = Path(folder.name)
        env = patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.tmp / "conf"),
                                     "XDG_STATE_HOME": str(self.tmp / "state")})
        env.start()
        self.addCleanup(env.stop)
        self.desk = init_desk(self.tmp / "shop" / "desk")

    def save(self, address, service="recognize", **fields):
        alert_push.change(self.desk, [{"address": address, "service": service, **fields}], by="page")
        return self.desk.settings()["alert_push"][-1]

    def deliver(self, entry, level="active", language="en", result=None):
        settings = self.desk.settings()
        settings["alert_language"] = language
        self.assertFalse(self.desk.save_settings(settings, by="page"))
        sender = patch.object(alert_http, "send", return_value=[result or {
            "ok": True, "why": "sent", "status": 200, "tries": 1}])
        with sender as sending:
            why = alert_push.send(self.desk, entry, [{"kind": "test", "level": level,
                                                     "text": "first line\nsecond 中文 line"}])
        sending.assert_called_once()
        self.assertEqual(sending.call_args.kwargs, {"deadline_s": 30})
        [request] = sending.call_args.args[0]
        return why, request


class BarkEncryptionTest(_PushServicesCase):
    def test_keys_private_encrypted_status_and_task_change_guard(self):
        from hansa import alert, history
        from hansa.run import keys_in_change
        for size in (16, 24, 32):
            key = "k" * size
            with self.subTest(size=size):
                entry = self.save("fixture-bark-key", "bark", secret=key)
                path = alert_push._file(self.desk, entry["id"], True)
                self.assertEqual(path.read_text(), key + "\n")
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertIn(key, self.desk.keys().values())
                self.assertNotIn(key, self.desk.redact(key))
                self.desk.event("fixture", text=key)
                why, _ = self.deliver(entry, result={"why": key})
                self.assertEqual(why, "network")
                for text in (json.dumps(public_settings(self.desk)), self.desk.settings_path.read_text(),
                             self.desk.events_path.read_text(), json.dumps(history.read(self.desk)),
                             json.dumps(views.status(self.desk)), views.status_text(views.status(self.desk)),
                             alert.explain_line(self.desk.settings(), self.desk),
                             alert_push._last_file(self.desk, entry["id"]).read_text()):
                    self.assertNotIn(key, text)
                self.assertTrue(alert_push.public(self.desk)[-1]["encrypted"])
                self.assertIn("encrypted", views.status_text(views.status(self.desk)))
                self.assertIn("encrypted", alert.explain_line(self.desk.settings(), self.desk))
        # Exercise the same changed-line reader used to refuse a task's merge.
        diff = "+" + key + "\n"
        with patch("hansa.run.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout=diff.encode())):
            self.assertTrue(keys_in_change(self.desk, self.tmp, "base", "head"))
        for command in ("status", "explain"):
            output = io.StringIO()
            with redirect_stdout(output):
                main(["--desk", str(self.desk.root), command])
            self.assertIn("encrypted", output.getvalue())
            self.assertNotIn(key, output.getvalue())

    def test_examples_keep_only_the_matching_cbc_key(self):
        for size in (16, 24, 32):
            key = "k" * size
            entry = self.save("fixture-bark-key", "bark", secret=bark_example(key, size * 8))
            self.assertEqual(alert_push._read(self.desk, entry["id"], True), key)
            self.assertNotIn("copied-iv", "".join(p.read_text() for p in self.desk.secrets_dir.iterdir()))
        # The completed pair must use a replacement address from this save.
        alert_push.change(self.desk, [{"id": entry["id"], "address": "replacement-device",
            "secret": bark_example(device="replacement-device")}], by="page")
        self.assertEqual(alert_push._read(self.desk, entry["id"], True), "1234567890123456")

    def test_example_with_literal_server_and_device_variables(self):
        example = bark_example().replace("curl 'https://api.day.app/fixture-bark-key'",
            'server=\'https://api.day.app\'\ndevice_key=\'fixture-bark-key\'\ncurl "$server/$device_key"')
        entry = self.save("fixture-bark-key", "bark", secret=example)
        self.assertEqual(alert_push._read(self.desk, entry["id"], True), "1234567890123456")

    def test_printable_ascii_key_preserves_spaces_in_storage_redaction_and_encryption(self):
        key = " abcdefghijklmn "
        self.assertEqual(len(key), 16)
        entry = self.save("fixture-bark-key", "bark", secret=key)
        self.assertEqual(alert_push._read(self.desk, entry["id"], True), key)
        self.assertIn(key, self.desk.keys().values())
        self.assertNotIn(key, self.desk.redact(key))
        self.assertEqual(json.loads(decrypt_bark(self.deliver(entry)[1]["json"], key))["title"], "shop/desk")

    def test_invalid_keys_and_examples_save_nothing(self):
        entry = self.save("fixture-bark-key", "bark")
        bad = ["k" * n for n in (1, 15, 17, 23, 25, 31, 33)] + [None, 123, "中" * 16, "k" * 15 + "\x00",
               bark_example(mode="ecb"), bark_example(mode="gcm"), bark_example(bits=192),
               bark_example().replace("openssl enc", "echo"), bark_example().replace("key=", "other="),
               bark_example().replace("https://api.day.app/fixture-bark-key", "https://api.day.app/")]
        for value in bad:
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "AES-128.*192.*256.*CBC"):
                alert_push.change(self.desk, [{"id": entry["id"], "secret": value}], by="page")
            self.assertFalse(alert_push._file(self.desk, entry["id"], True).exists())
        with self.assertRaisesRegex(ValueError, "this example is for another Bark device"):
            alert_push.change(self.desk, [{"id": entry["id"], "secret": bark_example(device="other-device")}], by="page")
        self.assertFalse(alert_push._file(self.desk, entry["id"], True).exists())

    def test_encrypted_requests_decrypt_all_fields_fresh_ivs_and_no_release_codes(self):
        for size in (16, 24, 32):
            key = "k" * size
            entry = self.save("fixture-bark-key", "bark", secret=key)
            ivs = []
            for level, mapped in (("passive", "passive"), ("active", "active"), ("urgent", "timeSensitive")):
                with patch.object(alert_push, "_text", return_value=("台面", "正文\n🙂")):
                    why, request = self.deliver(entry, level)
                self.assertEqual(why, "sent")
                self.assertEqual(request["url"], "https://api.day.app/push")
                self.assertEqual(set(request["json"]), {"device_key", "ciphertext", "iv"})
                self.assertEqual(request["json"]["device_key"], "fixture-bark-key")
                self.assertEqual(json.loads(decrypt_bark(request["json"], key)),
                    {"title": "台面", "body": "正文\n🙂", "group": "台面", "level": mapped})
                self.assertEqual(len(request["json"]["iv"]), 16)
                self.assertTrue(request["json"]["iv"].isascii())
                ivs.append(request["json"]["iv"])
            self.assertEqual(len(set(ivs)), len(ivs))
            with patch.object(alert_http, "send", return_value=[{"why": "sent"}]) as sending:
                alert_push.send(self.desk, entry, [{"kind": "release_waiting", "code": "654321",
                    "text": "approve with 654321", "translations": {"en": "release waits 654321"}}])
            self.assertNotIn("654321", decrypt_bark(sending.call_args.args[0][0]["json"], key).decode())

    def test_encrypted_final_bound_cuts_only_body_and_refuses_fixed_fields(self):
        from hansa import alert_bark
        service = alert_services.SERVICES["bark"]
        key = "1234567890123456"
        for body in ("中🙂" * 3000, 'x"\\\n' * 3000, "a" * 2500):
            [request] = service.requests("https://api.day.app/fixture-bark-key", key, "台面", body, "urgent")
            payload = request["json"]
            self.assertEqual(set(payload), {"device_key", "ciphertext", "iv"})
            self.assertLessEqual(len(json.dumps(payload, ensure_ascii=False).encode()), alert_bark.MAX_BYTES)
            inside = json.loads(decrypt_bark(payload, key))
            self.assertEqual((inside["title"], inside["group"], inside["level"]), ("台面", "台面", "timeSensitive"))
            self.assertTrue(inside["body"].endswith("…"))
            kept = inside["body"][:-1]
            self.assertTrue(body.startswith(kept))
            from hansa.aes import cbc_encrypt
            larger = json.dumps({**inside, "body": body[:len(kept) + 1] + "…"}, ensure_ascii=False).encode()
            bigger = {**payload, "ciphertext": base64.b64encode(cbc_encrypt(key.encode(), payload["iv"].encode(), larger)).decode()}
            self.assertGreater(len(json.dumps(bigger, ensure_ascii=False).encode()), alert_bark.MAX_BYTES)
        entry = self.save("k" * 3500, "bark", secret=key)
        with patch.object(alert_http, "send") as sending:
            self.assertEqual(alert_push.test(self.desk, entry["id"]), "too_big")
        sending.assert_not_called()
        with self.assertRaisesRegex(ValueError, "too_big"):
            service.requests("https://api.day.app/device", key, "中" * 1000, "", "active")

    def test_encrypted_test_push_explains_key_mismatch_in_both_languages(self):
        key = "1234567890123456"
        entry = self.save("fixture-bark-key", "bark", secret=key)
        for lang, word in (("en", "encrypted"), ("zh", "加密")):
            settings = self.desk.settings()
            settings["alert_language"] = lang
            self.assertFalse(self.desk.save_settings(settings, by="page"))
            with patch.object(alert_http, "send", return_value=[{"why": "sent"}]) as sending:
                self.assertEqual(alert_push.test(self.desk, entry["id"]), "sent")
            text = json.loads(decrypt_bark(sending.call_args.args[0][0]["json"], key))["body"]
            self.assertIn(word, text)
            self.assertIn("Decryption Failed", text)
            self.assertIn("keys differ" if lang == "en" else "密钥不同", text)

    def test_plain_bark_request_stays_byte_for_byte(self):
        [request] = alert_services.SERVICES["bark"].requests(
            "https://api.day.app/device", "", "desk", "正文", "active")
        self.assertEqual(json.dumps(request, ensure_ascii=False),
            '{"url": "https://api.day.app/push", "json": {"device_key": "device", "title": "desk", '
            '"body": "正文", "group": "desk", "level": "active"}}')
        entry = self.save("fixture-bark-key", "bark")
        self.assertIs(alert_push.public(self.desk)[0]["encrypted"], False)
        with patch.object(alert_http, "send", return_value=[{"why": "sent"}]) as sending:
            alert_push.test(self.desk, entry["id"])
        self.assertEqual(sending.call_args.args[0][0]["json"]["body"], "a test alert from hansa")


class PhoneServicesTest(_PushServicesCase):
    def test_secret_floor_applies_to_whole_values_and_parts_at_eight_characters(self):
        self.save("https://ntfy.sh/abcdefg", secret="tk_12345")
        self.save("https://ntfy.sh/abcdefgh", secret="tk_1234")
        alert_push._write_secret(self.desk, "orphan-seven", "1234567")
        alert_push._write_secret(self.desk, "orphan-eight", "12345678")
        keys = set(alert_push.keys(self.desk).values())
        for short in ("abcdefg", "tk_1234", "1234567"):
            self.assertNotIn(short, keys)
        for protected in ("abcdefgh", "tk_12345", "12345678"):
            self.assertIn(protected, keys)

    def test_ntfy_topic_trailing_slash_is_recognized_and_removed(self):
        address = "https://ntfy.sh/hansa-alerts-7f3k/"
        self.assertEqual(alert_services.parse(address), ("ntfy", address[:-1]))
        entry = self.save(address)
        self.assertEqual(self.deliver(entry)[1]["json"]["topic"], "hansa-alerts-7f3k")

    def test_short_secrets_do_not_redact_scrub_or_damage_other_entries(self):
        text = "abc parser and def utility"
        tid = self.desk.new_task(text, title=text)
        task = self.desk.tasks_dir / tid / "task.md"
        note = self.desk.tasks_dir / tid / "note.md"
        note.write_text(text, encoding="utf-8")
        before = task.read_bytes(), note.read_bytes()
        self.save("https://ntfy.sh/abc", secret="tk_fixture-access-token")
        self.save("https://ntfy.sh/fixture-abc-other-topic", secret="def")
        # The same floor covers unlisted files and separately stored secrets.
        alert_push._write_secret(self.desk, "orphan", "ghi", extra=True)
        keys = set(alert_push.keys(self.desk).values())
        self.assertTrue(all(len(key) >= 8 for key in keys))
        self.assertNotIn("abc", keys)
        self.assertNotIn("def", keys)
        self.assertNotIn("ghi", keys)
        self.assertEqual((task.read_bytes(), note.read_bytes()), before)
        self.assertEqual(self.desk.redact(text), text)
        self.assertEqual(self.desk.keys_in(text), [])
        other = "fixture-abc-other-topic"
        self.assertNotIn(other, self.desk.redact(other))
        self.assertEqual(self.desk.redact(other).count("[key of "), 1)
        later = self.desk.new_task(text, title=text)
        self.assertEqual((self.desk.tasks_dir / later / "task.md").read_text(), text + "\n")

    def test_ntfy_recognition_root_json_chinese_title_and_priorities(self):
        entry = self.save("https://ntfy.sh/hansa-alerts-7f3k")
        self.assertEqual(entry["service"], "ntfy")
        with patch.object(alert_push, "_text", return_value=("中文工作台", "提醒内容")):
            for level, priority in (("passive", 2), ("active", 3), ("urgent", 4)):
                with self.subTest(level=level):
                    why, request = self.deliver(entry, level)
                    self.assertEqual(why, "sent")
                    self.assertEqual(request["url"], "https://ntfy.sh/")
                    self.assertEqual(request["json"], {"topic": "hansa-alerts-7f3k", "title": "中文工作台",
                                                       "message": "提醒内容", "priority": priority})
                    self.assertNotIn("中文", json.dumps(request.get("headers", {}), ensure_ascii=False))
                    self.assertEqual(request["error_field"], "code")

    def test_short_ntfy_topic_needs_token_before_save_and_on_token_removal(self):
        address = "https://ntfy.example.com/abc"
        with self.assertRaisesRegex(ValueError, "topic.*short.*secret"):
            self.save(address, "ntfy")
        self.assertEqual(self.desk.settings()["alert_push"], [])
        self.assertEqual(list(self.desk.secrets_dir.glob("alert-push-*")), [])
        entry = self.save(address, "ntfy", secret="tk_fixture-access-token")
        why, request = self.deliver(entry)
        self.assertEqual(why, "sent")
        self.assertEqual(request["url"], "https://ntfy.example.com/")
        self.assertEqual(request["headers"], {"Authorization": "Bearer tk_fixture-access-token"})
        self.assertEqual(request["json"]["topic"], "abc")
        with self.assertRaisesRegex(ValueError, "topic.*short"):
            alert_push.change(self.desk, [{"id": entry["id"], "secret": ""}], by="page")
        alert_push.change(self.desk, [{"id": entry["id"], "address": address}], by="page")
        self.assertEqual(alert_push._read(self.desk, entry["id"], True), "tk_fixture-access-token")
        # An old or manually damaged entry is refused before transport too.
        alert_push._file(self.desk, entry["id"], True).unlink()
        with patch.object(alert_http, "send") as sending:
            self.assertEqual(alert_push.test(self.desk, entry["id"]), "refused_url")
        sending.assert_not_called()

    def test_gotify_is_chosen_and_moves_query_token_to_header(self):
        address = "https://push.example.com/message?token=T"
        with self.assertRaisesRegex(ValueError, "choose the push service"):
            alert_services.parse(address)
        entry = self.save(address, "gotify")
        for level, priority in (("passive", 2), ("active", 5), ("urgent", 8)):
            with self.subTest(level=level):
                _, request = self.deliver(entry, level)
                self.assertEqual(request["url"], "https://push.example.com/message")
                self.assertEqual(request["headers"], {"X-Gotify-Key": "T"})
                self.assertEqual(set(request["json"]), {"title", "message", "priority"})
                self.assertEqual(request["json"]["priority"], priority)
                self.assertEqual(request["error_field"], "errorCode")

    def test_gotify_server_and_extra_secret_are_validated_together(self):
        with self.assertRaisesRegex(ValueError, "application token"):
            self.save("https://gotify.example.com", "gotify")
        entry = self.save("https://gotify.example.com/", "gotify", secret="fixture-app-token")
        _, request = self.deliver(entry)
        self.assertEqual(request["url"], "https://gotify.example.com/message")
        self.assertEqual(request["headers"], {"X-Gotify-Key": "fixture-app-token"})
        with self.assertRaisesRegex(ValueError, "application token"):
            alert_push.change(self.desk, [{"id": entry["id"], "secret": ""}], by="page")

    def test_pushdeer_recognized_address_and_chosen_bare_key_form(self):
        for address, service in (("https://api2.pushdeer.com/message/push?pushkey=K", "recognize"),
                                 ("K", "pushdeer")):
            with self.subTest(address=address):
                entry = self.save(address, service)
                self.assertEqual(entry["service"], "pushdeer")
                with patch.object(alert_push, "_text", return_value=("台面", "正文\n下一行")):
                    _, request = self.deliver(entry)
                self.assertEqual(request["url"], "https://api2.pushdeer.com/message/push")
                self.assertEqual(request["form"], {"pushkey": "K", "type": "text", "text": "台面\n正文\n下一行"})
                self.assertEqual(request["success"], {"status": 200, "field": "code", "equals": 0})

    def test_pushdeer_self_hosted_https_and_http_8800_refusal(self):
        unsafe = "http://pushdeer.example.com:8800/message/push?pushkey=K"
        with self.assertRaisesRegex(ValueError, "HTTPS.*8800"):
            alert_services.SERVICES["pushdeer"].normalize(unsafe)
        with self.assertRaisesRegex(ValueError, "push service does not accept"):
            self.save(unsafe, "pushdeer")
        entry = self.save("https://pushdeer.example.com:8443/message/push?pushkey=K", "pushdeer")
        _, request = self.deliver(entry)
        self.assertEqual(request["url"], "https://pushdeer.example.com:8443/message/push")

    def test_serverchan_keys_and_addresses_recognize_exactly_once(self):
        for key, url in (("SCTfixture-send-key", "https://sctapi.ftqq.com/SCTfixture-send-key.send"),
                         ("sctp123tfixture-send-key", "https://123.push.ft07.com/send/sctp123tfixture-send-key.send")):
            for value in (key, url):
                with self.subTest(value=value):
                    matches = [s.id for s in alert_services.SERVICES.values() if s.recognizes(value)]
                    self.assertEqual(matches, ["serverchan"])
                    self.assertEqual(alert_services.parse(value), ("serverchan", url))
                    entry = self.save(value)
                    _, request = self.deliver(entry)
                    self.assertEqual(request["url"], url)
                    self.assertEqual(set(request["json"]), {"title", "desp"})

    def test_serverchan_title_and_literal_markdown_lines(self):
        entry = self.save("SCTfixture-send-key")
        body = "# heading\n**bold** [link](https://example.test)\n\n```\n<script> & _text_\n    indented\n- item\r# literal\r\n**also literal**"
        title = "题" * 20 + "\n" + "名" * 20
        with patch.object(alert_push, "_text", return_value=(title, body)):
            _, request = self.deliver(entry)
        self.assertEqual(request["json"]["title"], ("题" * 20 + " " + "名" * 20)[:32])
        self.assertEqual(request["json"]["desp"], "\n".join("    " + line for line in body.splitlines()))

    def test_bark_bare_key_and_one_copied_https_example(self):
        for value in ("KEY", "https://api.day.app/KEY/title/body?group=example&ttl=600",
                      "curl -X GET 'https://api.day.app/KEY/title/body?group=example&ttl=600'"):
            with self.subTest(value=value):
                self.assertEqual(alert_services.parse(value), ("bark", "https://api.day.app/KEY"))
                entry = self.save(value)
                _, request = self.deliver(entry)
                self.assertEqual(request["url"], "https://api.day.app/push")
                self.assertEqual(request["json"]["device_key"], "KEY")
        with self.assertRaises(ValueError):
            alert_services.parse("curl 'https://api.day.app/KEY' 'https://api.day.app/OTHER'")
        self.assertEqual(alert_services.parse("curl 'https://bark.example/KEY/a?ttl=600'", "bark"),
                         ("bark", "https://bark.example/KEY"))

    def test_no_levels_urgent_marker_uses_principal_language_only_when_urgent(self):
        for service, address, field in (("pushdeer", "fixture-deer-key", "text"),
                                        ("serverchan", "SCTfixture-send-key", "title")):
            entry = self.save(address, service)
            for language, marker in (("zh", "紧急："), ("en", "Urgent: ")):
                with self.subTest(service=service, language=language):
                    _, request = self.deliver(entry, "urgent", language)
                    payload = request.get("json", request.get("form"))
                    self.assertTrue(payload[field].startswith(marker), payload)
                    self.assertNotIn("priority", payload)
                    _, request = self.deliver(entry, "passive", language)
                    self.assertFalse(request.get("json", request.get("form"))[field].startswith(marker))

    def test_service_limits_cut_utf8_with_ellipsis(self):
        cases = (("bark", "fixture-bark-key", "body", 3500),
                 ("ntfy", "https://ntfy.sh/fixture-topic", "message", 4096),
                 ("gotify", "https://gotify.example/message?token=T", "message", 4096),
                 ("pushdeer", "fixture-deer-key", "text", 3500),
                 ("serverchan", "SCTfixture-send-key", "desp", 3500))
        for service, address, field, limit in cases:
            for body in ("中文🙂" * 3000, "a\n" * 4000):
                with self.subTest(service=service, multiline=body.startswith("a")):
                    definition = alert_services.SERVICES[service]
                    normalized = definition.normalize(address)
                    [request] = definition.requests(normalized, "", "title", body, "active")
                    payload = request.get("json", request.get("form"))
                    self.assertLessEqual(len(payload[field].encode("utf-8")), limit)
                    self.assertTrue(payload[field].endswith("…"))
                    if service == "bark":
                        self.assertLessEqual(len(json.dumps(payload, ensure_ascii=False).encode("utf-8")), limit)
        definition = alert_services.SERVICES["ntfy"]
        [request] = definition.requests(definition.normalize("https://ntfy.sh/fixture-topic"), "", "题" * 500, "body", "active")
        self.assertLessEqual(len(request["json"]["title"].encode("utf-8")), 1024)
        self.assertTrue(request["json"]["title"].endswith("…"))

    def test_secrets_are_saved_private_masked_and_redacted(self):
        for service, address, key, secret in (
                ("ntfy", "https://ntfy.sh/fixture-private-topic", "fixture-private-topic", "tk_fixture-token"),
                ("gotify", "https://gotify.example/message?token=fixture-gotify-token", "fixture-gotify-token", ""),
                ("pushdeer", "fixture-pushdeer-key", "fixture-pushdeer-key", ""),
                ("serverchan", "SCTfixture-send-key", "SCTfixture-send-key", "")):
            with self.subTest(service=service):
                entry = self.save(address, service, **({"secret": secret} if secret else {}))
                self.assertIn(key, alert_push.keys(self.desk).values())
                self.assertNotIn(key, self.desk.redact(key))
                self.assertNotIn(key, json.dumps(public_settings(self.desk)))
                self.assertNotIn(key, self.desk.settings_path.read_text())
                self.assertNotIn(key, self.desk.events_path.read_text())
                for extra in ((False, True) if secret else (False,)):
                    self.assertEqual(stat.S_IMODE(alert_push._file(self.desk, entry["id"], extra).stat().st_mode), 0o600)

    def test_saved_entries_use_only_the_recorded_service(self):
        for n, (service, address, key, secret, mask) in enumerate(SAVED_SERVICE_CASES + CHAT_SERVICE_CASES):
            with self.subTest(service=service, address=address):
                self.desk = init_desk(self.tmp / f"saved-{n}")
                entry = self.save(address, service, **({"secret": secret} if secret else {}))
                with ExitStack() as stack:
                    for definition in alert_services.SERVICES.values():
                        if service == "json":
                            # A JSON webhook file is checked against the other
                            # services on purpose: a switch cut short to it must
                            # still hide the previous service's parts. None of
                            # them recognizes this address.
                            stack.enter_context(patch.object(definition, "recognizes", return_value=False))
                        else:
                            # Saved entries must not be recognized again, even by their own service.
                            stack.enter_context(patch.object(
                                definition, "recognizes",
                                side_effect=AssertionError("recognition of saved entry")))
                        if definition.id != service:
                            for method in ("normalize", "secrets", "hidden", "validate", "requests"):
                                if hasattr(definition, method):
                                    stack.enter_context(patch.object(definition, method, side_effect=AssertionError(
                                        f"saved {service} entry reached {definition.id}.{method}")))
                    self.assertIn(key, alert_push.keys(self.desk).values())
                    self.assertNotIn(key, self.desk.redact(key))
                    self.assertEqual(public_settings(self.desk)["alert_push"][0]["address"], mask)
                    status = views.status(self.desk)
                    self.assertEqual(status["alert_push"][0]["address"], mask)
                    self.assertIn(mask, views.status_text(status))
                    # Check a saved pair and scrub it again, using the recorded service.
                    alert_push.change(self.desk, [{"id": entry["id"], "address": alert_push._read(self.desk, entry["id"])}], by="page")
                    self.assertEqual(self.deliver(entry)[0], "sent")

    def test_service_paths_stay_ordinary_text_in_tasks_attachments_and_names(self):
        text = ("Fix the message queue and send phone messages via ntfy.sh, "
                "api2.pushdeer.com, sctapi.ftqq.com and 123.push.ft07.com.")
        tid = self.desk.new_task(text, title="Fix the message queue")
        task = self.desk.tasks_dir / tid / "task.md"
        note = self.desk.tasks_dir / tid / "note.md"
        note.write_text(text, encoding="utf-8")
        before = task.read_bytes(), note.read_bytes()
        for service, address, _, secret, _ in SAVED_SERVICE_CASES:
            entry = self.save(address, service, **({"secret": secret} if secret else {}))
        self.assertEqual((task.read_bytes(), note.read_bytes()), before)
        self.assertEqual(self.desk.redact(text), text)
        self.assertEqual(self.desk.keys_in(text), [])
        alert_push.change(self.desk, [{"id": entry["id"], "name": "phone messages"}], by="page")
        self.assertEqual(public_settings(self.desk)["alert_push"][-1]["name"], "phone messages")
        source = self.tmp / "message.txt"
        source.write_text(text, encoding="utf-8")
        result = subprocess.run([sys.executable, "-m", "hansa", "--desk", str(self.desk.root),
                                 "add", text, "--attach", str(source)], capture_output=True, text=True,
                                cwd=Path(__file__).resolve().parents[1], timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        added = self.desk.tasks()[-1]
        self.assertEqual((self.desk.tasks_dir / added["id"] / "task.md").read_text(), text + "\n")
        self.assertEqual((self.desk.attachment_dir(added["id"]) / source.name).read_bytes(), source.read_bytes())
        self.assertEqual(self.desk.meta(tid)["title"], "Fix the message queue")

    def test_own_secrets_still_scrub_redact_and_block_attachments(self):
        private = {text for service, address, key, secret, _ in SAVED_SERVICE_CASES
                   for text in (alert_services.SERVICES[service].normalize(address), key, secret) if text}
        tid = self.desk.new_task("\n".join(sorted(private)))
        expected = set()
        for service, address, key, secret, _ in SAVED_SERVICE_CASES:
            entry = self.save(address, service, **({"secret": secret} if secret else {}))
            expected.update((alert_push._read(self.desk, entry["id"]), key))
            if secret:
                expected.add(secret)
        task = (self.desk.tasks_dir / tid / "task.md").read_text()
        for secret in private:
            with self.subTest(secret=secret):
                self.assertNotIn(secret, task)
                self.assertNotIn(secret, self.desk.redact(secret))
                source = self.tmp / "private.txt"
                source.write_text(secret, encoding="utf-8")
                with self.assertRaisesRegex(SystemExit, "holds the key"):
                    self.desk.new_task("Read the attachment", attach=[str(source)])
        self.assertEqual(set(alert_push.keys(self.desk).values()), expected)

    def test_only_unlisted_files_keep_whole_values_without_other_services(self):
        address = "https://gotify.example/message?token=fixture-orphan-token"
        for listed in (False, True):
            with self.subTest(listed=listed):
                self.desk = init_desk(self.tmp / f"unusable-{listed}")
                entry = self.save("https://ntfy.sh/fixture-topic", "ntfy") if listed else {"id": "orphan"}
                alert_push._write_secret(self.desk, entry["id"], address)
                # Full file values remain private. A listed entry whose recorded
                # service no longer accepts the file keeps the parts of the
                # service the address still is; an unlisted file has none to ask.
                expected = {address, "fixture-orphan-token"} if listed else {address}
                with patch.object(self.desk, "settings", side_effect=AssertionError("recursive settings load")):
                    self.assertEqual(set(alert_push.keys(self.desk).values()), expected)
                self.assertNotIn(address, self.desk.redact(address))
                self.assertEqual(self.desk.redact("Fix the message queue"), "Fix the message queue")
                if listed:
                    with patch.object(alert_http, "send") as sending:
                        self.assertEqual(alert_push.test(self.desk, entry["id"]), "refused_url")
                    sending.assert_not_called()

    def test_a_switch_cut_short_after_the_settings_write_hides_the_old_token(self):
        address = "https://push.example.com/message?token=fixture-old-gotify-token"
        entry = self.save(address, "gotify")
        with patch.object(alert_push, "_write_secret", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                alert_push.change(self.desk, [{"id": entry["id"], "service": "bark",
                                               "address": "https://api.day.app/fixture-bark-key"}], by="page")
        # The settings write landed and the address write did not: the entry
        # records Bark while its file still holds the Gotify address.
        self.assertEqual(self.desk.settings()["alert_push"][0]["service"], "bark")
        self.assertEqual(alert_push._read(self.desk, entry["id"]), address)
        values = set(alert_push.keys(self.desk).values())
        self.assertIn(address, values)
        self.assertIn("fixture-old-gotify-token", values)
        self.assertNotIn("fixture-old-gotify-token", self.desk.redact("fixture-old-gotify-token"))

    def test_files_not_in_their_saved_form_keep_parts_from_the_normalized_address(self):
        cases = (
            ("bark", "https://api.day.app/fixture-saved-bark-key",
             "https://API.day.app/fixture-bark-key", "fixture-bark-key"),
            ("gotify", "https://gotify.example/message?token=fixture-saved-gotify-token",
             "https://GOTIFY.example/message?token=fixture-gotify-token", "fixture-gotify-token"),
            ("ntfy", "https://ntfy.sh/fixture-saved-topic",
             "https://ntfy.sh/fixture-private-topic/", "fixture-private-topic"),
        )
        for n, (service, saved, edited, part) in enumerate(cases):
            with self.subTest(service=service):
                self.desk = init_desk(self.tmp / f"edited-address-{n}")
                entry = self.save(saved, service)
                alert_push._write_secret(self.desk, entry["id"], edited)
                values = set(alert_push.keys(self.desk).values())
                self.assertIn(edited, values)
                self.assertIn(part, values)
                self.assertNotEqual(self.desk.redact(part), part)

    def test_a_switch_cut_short_to_the_json_webhook_hides_the_old_token(self):
        address = "https://push.example.com/message?token=fixture-old-gotify-token"
        entry = self.save(address, "gotify")
        with patch.object(alert_push, "_write_secret", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                alert_push.change(self.desk, [{"id": entry["id"], "service": "json",
                                               "address": "https://hooks.example/fixture-json-webhook"}], by="page")
        # The settings write landed and the address write did not: the entry
        # records the JSON webhook while its file still holds the Gotify address.
        self.assertEqual(self.desk.settings()["alert_push"][0]["service"], "json")
        self.assertEqual(alert_push._read(self.desk, entry["id"]), address)
        values = set(alert_push.keys(self.desk).values())
        self.assertIn(address, values)
        self.assertIn("fixture-old-gotify-token", values)
        self.assertNotIn("fixture-old-gotify-token", self.desk.redact("fixture-old-gotify-token"))

    def test_one_unreadable_entry_leaves_the_readable_ones_hiding_and_sending(self):
        entry = self.save("https://api.day.app/fixture-bark-key", "bark")
        settings = self.desk.settings()
        settings["alert_push"].append({"id": "from-newer-hansa", "service": "no-such-service",
                                       "on": True, "name": ""})
        write_json(self.desk.settings_path, settings)
        values = set(alert_push.keys(self.desk).values())
        self.assertIn("fixture-bark-key", values)
        self.assertIn("https://api.day.app/fixture-bark-key", values)
        self.assertEqual([e["id"] for e in alert_push.enabled(self.desk, self.desk.settings())], [entry["id"]])
        self.assertEqual([e["id"] for e in public_settings(self.desk)["alert_push"]], [entry["id"]])
        with patch.object(alert_http, "send", return_value=[{"ok": True, "why": "sent", "status": 200, "tries": 1}]):
            self.assertEqual(alert_push.test(self.desk, entry["id"]), "sent")
        # The unreadable entry is still a shown problem, and saving still refuses.
        self.assertTrue(alert_push.problems(self.desk.settings()["alert_push"]))
        with self.assertRaisesRegex(ValueError, "known service"):
            alert_push.change(self.desk, [{"id": entry["id"], "name": "phone"}], by="page")

    def test_bark_refuses_another_services_address_and_names_it(self):
        for address, name in (("https://push.example.com/message?token=fixture-gotify-token", "Gotify"),
                              ("https://api2.pushdeer.com/message/push?pushkey=fixture-pushdeer-key", "PushDeer")):
            with self.subTest(address=address):
                with self.assertRaisesRegex(ValueError, f"looks like a {name} address; choose {name}"):
                    self.save(address, "bark")
                self.assertEqual(self.desk.settings()["alert_push"], [])
        # A self-hosted Bark server's address is still Bark's own.
        entry = self.save("https://bark.example.org/fixture-bark-key", "bark")
        self.assertIn("fixture-bark-key", alert_push.keys(self.desk).values())
        self.assertEqual(public_settings(self.desk)["alert_push"][0]["id"], entry["id"])

    def test_unreadable_metadata_keeps_whole_secret_files_without_guessing(self):
        entry = self.save("https://gotify.example/message?token=fixture-gotify-token", "gotify")
        address = alert_push._read(self.desk, entry["id"])
        self.desk.settings_path.write_text("{", encoding="utf-8")
        self.assertEqual(set(alert_push.keys(self.desk).values()), {address})
        self.assertNotIn(address, self.desk.redact(address))
        self.assertEqual(self.desk.redact("Fix the message queue"), "Fix the message queue")

    def test_refusal_code_persists_to_page_and_status_without_response(self):
        entry = self.save("fixture-pushdeer-key", "pushdeer")
        result = {"ok": False, "why": "refused_by_service", "status": 200, "tries": 1,
                  "code": 80501, "body": "response-body-secret", "message": "never expose this"}
        why, _ = self.deliver(entry, result=result)
        self.assertEqual(why, "refused_by_service")
        last = alert_push.last_result(self.desk, entry["id"])
        self.assertEqual(set(last), {"at", "ok", "why", "code"})
        self.assertEqual(last["code"], 80501)
        self.assertEqual(read_json(alert_push._last_file(self.desk, entry["id"]), {})["code"], 80501)
        self.assertEqual(public_settings(self.desk)["alert_push"][0]["last"], last)
        status = views.status(self.desk)
        self.assertEqual(status["alert_push"][0]["last"], last)
        self.assertIn("refused_by_service", views.status_text(status))
        self.assertIn("code 80501", views.status_text(status))
        self.assertNotIn("response-body-secret", json.dumps(status) + self.desk.events_path.read_text())
        self.deliver(entry)
        self.assertNotIn("code", alert_push.last_result(self.desk, entry["id"]))
        write_json(alert_push._last_file(self.desk, entry["id"]), {**last, "code": {"secret": "body"}})
        self.assertNotIn("code", alert_push.last_result(self.desk, entry["id"]))

    def test_credentials_cannot_be_rerouted_by_unsafe_addresses(self):
        for service, address in (("ntfy", "https://ntfy.sh/topic-123"),
                                 ("gotify", "https://gotify.example/message?token=T"),
                                 ("pushdeer", "https://api2.pushdeer.com/message/push?pushkey=K"),
                                 ("serverchan", "https://sctapi.ftqq.com/SCTfixture.send")):
            for unsafe in (address.replace("https://", "http://"), address.replace("https://", "https://u:p@"),
                           address + "#fragment", address + "\n", address + "\\evil"):
                with self.subTest(service=service, unsafe=repr(unsafe)), self.assertRaises(ValueError):
                    alert_services.parse(unsafe, service)
        for address in ("https://sctapi.ftqq.com/sctp123tfixture.send",
                        "https://456.push.ft07.com/send/sctp123tfixture.send"):
            with self.subTest(address=address), self.assertRaises(ValueError):
                alert_services.parse(address, "serverchan")


class ChatServicesTest(_PushServicesCase):
    def test_recognition_requests_and_registry_hints(self):
        public = {row["id"]: row for row in alert_services.public()}
        for service, address, _, secret, _ in CHAT_SERVICE_CASES:
            with self.subTest(service=service):
                if service == "json":
                    with self.assertRaisesRegex(ValueError, "choose the push service"):
                        alert_services.parse(address)
                else:
                    self.assertEqual(alert_services.parse(address), (service, address))
                entry = self.save(address, service, **({"secret": secret} if secret else {}))
                with patch.object(alert_push, "_text", return_value=("台面", "正文\n下一行")):
                    why, request = self.deliver(entry)
                self.assertEqual(why, "sent")
                self.assertEqual(public[service]["extra_secret"], bool(secret))
                payload = request["json"]
                text = "台面\n正文\n下一行"
                if service in ("wecom", "dingtalk"):
                    self.assertEqual(payload, {"msgtype": "text", "text": {"content": text}})
                    self.assertEqual(request["success"], {"field": "errcode", "equals": 0})
                elif service == "feishu":
                    self.assertEqual(payload["msg_type"], "text")
                    self.assertEqual(payload["content"], {"text": text})
                elif service == "telegram":
                    self.assertEqual(payload, {"chat_id": "-100123456", "text": text, "disable_notification": False})
                    self.assertEqual(request["url"], address.split("?", 1)[0])
                elif service == "discord":
                    self.assertEqual(payload, {"content": text})
                elif service == "slack":
                    self.assertEqual(payload, {"text": text})
                    self.assertEqual(request["success"], {"status": 200, "text": "ok"})
                else:
                    self.assertEqual(payload, {"desk": "台面", "title": "台面", "text": "正文\n下一行", "level": "active"})
                    self.assertEqual(request["headers"], {"Authorization": "Bearer " + secret})
                if service != "dingtalk":
                    self.assertEqual(request["url"], address.split("?", 1)[0] if service == "telegram" else address)

    def test_international_bot_hosts_and_endpoint_recognition(self):
        addresses = (("dingtalk", "https://oapi.dingtalk.io/robot/send?access_token=fixture-token"),
                     ("feishu", "https://open.larksuite.com/open-apis/bot/v2/hook/fixture-hook"))
        for service, address in addresses:
            with self.subTest(service=service):
                self.assertEqual(alert_services.parse(address), (service, address))
                self.assertEqual(self.deliver(self.save(address))[1]["url"], address)
        for address in ("https://qyapi.weixin.qq.com/other?key=fixture-key",
                        "https://oapi.dingtalk.com/other?access_token=fixture-token",
                        "https://open.feishu.cn/other/fixture-hook",
                        "https://open.larksuite.com/other/fixture-hook",
                        "https://discord.com/other/123/fixture-token",
                        "https://hooks.slack.com/other/T123/B123/fixture-token",
                        "https://home.example/anything"):
            with self.subTest(address=address), self.assertRaisesRegex(ValueError, "choose the push service"):
                alert_services.parse(address)
        # Telegram recognizes the host, then validates the sendMessage endpoint.
        with self.assertRaisesRegex(ValueError, "does not accept"):
            alert_services.parse("https://api.telegram.org/bot123:fixture-token/getUpdates")

    def test_bot_endpoints_require_complete_credentials_and_reject_query_extras(self):
        bad = (("wecom", "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"),
               ("wecom", "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key="),
               ("wecom", "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=K&key=OTHER"),
               ("dingtalk", "https://oapi.dingtalk.com/robot/send?access_token=T&sign=old"),
               ("feishu", CHAT_SERVICE_CASES[2][1] + "?secret=fixture"),
               ("telegram", CHAT_SERVICE_CASES[3][1].split("?", 1)[0]),
               ("telegram", CHAT_SERVICE_CASES[3][1] + "&parse_mode=HTML"),
               ("discord", "https://discord.com/api/webhooks/123456/.."),
               ("discord", "https://discord.com/api/webhooks/123456/."),
               ("slack", CHAT_SERVICE_CASES[5][1] + "?"))
        for service, address in bad:
            with self.subTest(service=service, address=address), self.assertRaises(ValueError):
                alert_services.parse(address, service)
        address = CHAT_SERVICE_CASES[3][1].replace("-100123456", "@fixture_channel")
        self.assertEqual(alert_services.parse(address), ("telegram", address))
        definition = alert_services.SERVICES["telegram"]
        [request] = definition.requests(address, "", "desk", "body", "active")
        self.assertEqual(request["json"]["chat_id"], "@fixture_channel")
        self.assertNotIn("@fixture_channel", definition.secrets(address))

    def test_dingtalk_fixed_signature_and_unsigned_request(self):
        address = CHAT_SERVICE_CASES[1][1]
        definition = alert_services.SERVICES["dingtalk"]
        with patch.object(alert_chat.time, "time", return_value=1700000000.123):
            [request] = definition.requests(address, "SECfixture-signing-secret", "desk", "body", "active")
        self.assertEqual(request["url"], address + "&timestamp=1700000000123&sign="
                         "wx22JBUieV9bR%2Bp79tpZSAjMTFZrEUL7XidAHDr5GW0%3D")
        [unsigned] = definition.requests(address, "", "desk", "body", "active")
        self.assertEqual(unsigned["url"], address)
        self.assertNotIn("timestamp", unsigned["json"])
        self.assertNotIn("sign", unsigned["json"])
        with patch.object(alert_chat.time, "time", return_value=1700000001.123):
            [later] = definition.requests(address, "SECfixture-signing-secret", "desk", "body", "active")
        self.assertIn("timestamp=1700000001123", later["url"])
        self.assertNotEqual(later["url"].split("&sign=")[1], request["url"].split("&sign=")[1])

    def test_feishu_fixed_signature_and_unsigned_request(self):
        address = CHAT_SERVICE_CASES[2][1]
        definition = alert_services.SERVICES["feishu"]
        with patch.object(alert_chat.time, "time", return_value=1700000000.123):
            [request] = definition.requests(address, "fixture-feishu-signing-secret", "desk", "body", "active")
        self.assertEqual(request["json"], {"msg_type": "text", "content": {"text": "desk\nbody"},
                                         "timestamp": "1700000000", "sign": "0awgJI8X4aWtM3EI2qIbWu1RV39l9bosNuMbjExSpks="})
        self.assertEqual(request["url"], address)
        [unsigned] = definition.requests(address, "", "desk", "body", "active")
        self.assertEqual(unsigned["json"], {"msg_type": "text", "content": {"text": "desk\nbody"}})

    def test_passive_telegram_and_discord_suppress_notifications(self):
        for service, address, token, _, _ in CHAT_SERVICE_CASES[3:5]:
            with self.subTest(service=service):
                entry = self.save(address)
                _, request = self.deliver(entry, "passive")
                payload = request["json"]
                if service == "telegram":
                    self.assertIs(payload["disable_notification"], True)
                else:
                    self.assertEqual(payload["flags"], 4096)
                    self.assertEqual(request["headers"]["User-Agent"],
                                     f"DiscordBot (https://github.com/FrostNovaOrg/Hansa, {__version__})")
                result = json.dumps(alert_push.last_result(self.desk, entry["id"]))
                status = views.status(self.desk)
                self.assertNotIn(token, result + json.dumps(status) + views.status_text(status))
                self.assertNotIn(token, self.desk.events_path.read_text())
                _, normal = self.deliver(entry, "active")
                if service == "telegram":
                    self.assertIs(normal["json"]["disable_notification"], False)
                else:
                    self.assertNotIn("flags", normal["json"])

    def test_chat_urgent_markers_and_code_free_batches(self):
        for service, address, _, secret, _ in CHAT_SERVICE_CASES:
            entry = self.save(address, service, **({"secret": secret} if secret else {}))
            for language, marker in (("en", "Urgent: "), ("zh", "紧急：")):
                with self.subTest(service=service, language=language):
                    _, request = self.deliver(entry, "urgent", language)
                    payload = request["json"]
                    text = (payload["text"]["content"] if service in ("wecom", "dingtalk") else
                            payload["content"]["text"] if service == "feishu" else
                            payload["content"] if service == "discord" else payload["text"])
                    if service != "json":
                        self.assertTrue(text.startswith(marker), payload)
                    else:
                        self.assertEqual(payload["level"], "urgent")
                        self.assertEqual(payload["desk"], payload["title"])
                        self.assertFalse(payload["title"].startswith(marker))
            with patch.object(alert_http, "send", return_value=[{"why": "sent"}]) as sending:
                self.assertEqual(alert_push.send(self.desk, entry, [{"kind": "release_waiting", "level": "active",
                    "text": "release needs code OTPfixture-123", "code": "OTPfixture-123"}]), "sent")
            sent = json.dumps(sending.call_args.args[0], ensure_ascii=False)
            self.assertNotIn("OTPfixture-123", sent)
            self.assertNotIn("needs code", sent)

    def test_byte_character_and_complete_json_limits(self):
        for service, address, _, secret, _ in CHAT_SERVICE_CASES[:-1]:
            definition = alert_services.SERVICES[service]
            for body in ("中文🙂" * 5000, "\n\"\\\t" * 7000):
                with self.subTest(service=service, escaped=body.startswith("\n")):
                    [request] = definition.requests(address, secret, "desk", body, "active")
                    payload = request["json"]
                    if service in ("wecom", "dingtalk"):
                        text = payload["text"]["content"]
                        self.assertLessEqual(len(text.encode("utf-8")), definition.max_bytes)
                        self.assertEqual(text, ("desk\n" + body)[:len(text) - 1] + "…")
                        # No split code point, and no unnecessary unused space.
                        next_text = ("desk\n" + body)[:len(text)] + "…"
                        self.assertGreater(len(next_text.encode("utf-8")), definition.max_bytes)
                    elif service == "feishu":
                        text = payload["content"]["text"]
                        self.assertLessEqual(len(json.dumps(payload, ensure_ascii=False).encode("utf-8")), 20 * 1024)
                    else:
                        text = payload["content" if service == "discord" else "text"]
                        self.assertEqual(len(text), {"telegram": 4096, "discord": 2000, "slack": 4000}[service])
                    self.assertTrue(text.endswith("…"))
            [short] = definition.requests(address, "", "desk", "body", "active")
            self.assertIn("desk\\nbody", json.dumps(short["json"]))

    def test_json_webhook_accepts_https_only_when_chosen_and_keeps_shape(self):
        address = "https://automation.example:8443/hooks/send?key=fixture-key"
        self.assertFalse(alert_services.SERVICES["json"].recognizes(address))
        self.assertEqual(alert_services.parse(address, "json"), ("json", address))
        definition = alert_services.SERVICES["json"]
        for level in ("passive", "active", "urgent"):
            for secret in ("", "fixture-token"):
                with self.subTest(level=level, authenticated=bool(secret)):
                    [request] = definition.requests(address, secret, "desk", "body", level)
                    self.assertEqual(request["url"], address)
                    self.assertEqual(request["json"], {"desk": "desk", "title": "desk", "text": "body", "level": level})
                    self.assertEqual(request["headers"], {"Authorization": "Bearer fixture-token"} if secret else {})

    def test_new_credentials_are_private_scrubbed_and_not_rerouted(self):
        for n, (service, address, key, secret, mask) in enumerate(CHAT_SERVICE_CASES):
            with self.subTest(service=service):
                self.desk = init_desk(self.tmp / f"private-chat-{n}")
                private = {address, key, secret} - {""}
                tid = self.desk.new_task("\n".join(sorted(private)))
                entry = self.save(address, service, **({"secret": secret} if secret else {}))
                task = (self.desk.tasks_dir / tid / "task.md").read_text()
                status = views.status(self.desk)
                self.assertEqual(status["alert_push"][0]["address"], mask)
                for value in private:
                    self.assertIn(value, alert_push.keys(self.desk).values())
                    self.assertNotIn(value, task)
                    self.assertNotIn(value, self.desk.redact(value))
                    self.assertNotIn(value, json.dumps(public_settings(self.desk)) + json.dumps(status)
                                     + self.desk.settings_path.read_text() + self.desk.events_path.read_text())
                if service == "telegram":
                    self.assertIn("-100123456", mask)
                for extra in ((False, True) if secret else (False,)):
                    self.assertEqual(stat.S_IMODE(alert_push._file(self.desk, entry["id"], extra).stat().st_mode), 0o600)
                for unsafe in (address.replace("https://", "http://"), address.replace("https://", "https://u:p@"),
                               address + "#fragment", address + "\n", address + "\\evil"):
                    with self.subTest(unsafe=repr(unsafe)), self.assertRaises(ValueError):
                        alert_services.parse(unsafe, service)
                if service != "json":
                    foreign = address.replace(urlsplit(address).netloc, "other.example")
                    with self.assertRaises(ValueError):
                        alert_services.parse(foreign, service)


class ChildAnswerTest(unittest.TestCase):
    """The real -I child prepares requests and parses responses using a fake opener."""

    def child(self, folder, source):
        helper = Path(folder) / "fake_push_child.py"
        helper.write_text("import importlib.util, io, json, os, sys\n"
                          f"spec = importlib.util.spec_from_file_location('transport', {alert_http.__file__!r})\n"
                          "transport = importlib.util.module_from_spec(spec)\n"
                          "spec.loader.exec_module(transport)\n" + textwrap.dedent(source), encoding="utf-8")
        return patch.object(alert_http, "_HELPER", helper)

    def run_child(self, requests):
        source = '''
            given = json.load(sys.stdin)
            sys.stdin = io.StringIO(json.dumps(given))
            plans = {request['url']: request['fixture'] for request in given['requests'] if isinstance(request, dict)}
            class Reply:
                headers = {}
                def __init__(self, plan):
                    self.code, self.plan = plan.get('status', 200), plan
                def read(self, size):
                    assert size == transport._RESPONSE_LIMIT
                    if self.plan.get('read_error'): raise OSError('response-body-secret')
                    return self.plan['body'].encode('utf-8')[:size]
                def close(self): pass
            class Opener:
                def __init__(self): self.calls = 0
                def open(self, request, timeout):
                    plan = plans[request.full_url]
                    self.calls += 1
                    assert self.calls <= plan.get('max_tries', 1), 'unexpected retry'
                    assert request.get_method() == 'POST'
                    for name, value in plan.get('headers', {}).items():
                        assert request.get_header(name) == value
                    if 'form' in plan:
                        from urllib.parse import parse_qs
                        assert request.get_header('Content-type') == 'application/x-www-form-urlencoded; charset=utf-8'
                        assert parse_qs(request.data.decode('ascii'), keep_blank_values=True) == {k: [v] for k, v in plan['form'].items()}
                    if 'json' in plan:
                        assert json.loads(request.data) == plan['json']
                    if plan.get('status', 200) >= 400:
                        raise transport.urllib.error.HTTPError(request.full_url, plan['status'], 'error-body-secret', {},
                                                               io.BytesIO(plan['body'].encode('utf-8')))
                    return Reply(plan)
            transport._BACKOFF = (0, 0)
            transport._make_opener = Opener
            transport._main()
        '''
        with tempfile.TemporaryDirectory() as folder, self.child(folder, source), \
                patch.object(alert_http.subprocess, "Popen", wraps=subprocess.Popen) as popen:
            results = alert_http.send(requests, deadline_s=20)
        self.assertEqual(popen.call_args.args[0][:2], [sys.executable, "-I"])
        self.assertEqual(popen.call_args.kwargs["stderr"], subprocess.DEVNULL)
        return results

    def request(self, body, **fields):
        return {"url": "https://push.example.test/send", "json": {"text": "request-secret"},
                "fixture": {"body": body}, **fields}

    def test_chat_service_acknowledgements_and_error_codes_in_child(self):
        cases = (("wecom", 200, '{"errcode":0}', "sent", None),
                 ("wecom", 200, '{"errcode":93000,"errmsg":"private reason"}', "refused_by_service", 93000),
                 ("wecom", 200, '{"errcode":45009}', "refused_by_service", 45009),
                 ("dingtalk", 200, '{"errcode":"0"}', "sent", None),
                 ("dingtalk", 200, '{"errcode":310000}', "refused_by_service", 310000),
                 ("dingtalk", 200, '{"errcode":400101}', "refused_by_service", 400101),
                 ("dingtalk", 200, '{"errcode":410100}', "refused_by_service", 410100),
                 ("feishu", 200, '{"code":0}', "sent", None),
                 *(("feishu", 200, json.dumps({"code": code}), "refused_by_service", code)
                   for code in (19021, 19022, 19024, 11232)),
                 ("telegram", 200, '{"ok":true}', "sent", None),
                 ("telegram", 200, '{"ok":false,"error_code":400}', "refused_by_service", 400),
                 ("telegram", 403, '{"ok":false,"error_code":403}', "http_4xx", 403),
                 ("discord", 204, '', "sent", None),
                 ("discord", 404, '{"code":10015,"message":"Unknown Webhook"}', "http_4xx", 10015),
                 ("slack", 200, 'ok', "sent", None),
                 ("slack", 201, 'ok', "refused_by_service", "ok"),
                 ("slack", 400, 'invalid_payload', "http_4xx", "invalid_payload"),
                 ("slack", 404, 'channel_not_found', "http_4xx", "channel_not_found"),
                 ("slack", 200, 'invalid_payload', "refused_by_service", "invalid_payload"),
                 ("json", 200, 'anything', "sent", None),
                 ("json", 204, '', "sent", None),
                 ("json", 299, 'anything', "sent", None))
        addresses = {service: address for service, address, _, _, _ in CHAT_SERVICE_CASES}
        for service, status, body, why, code in cases:
            with self.subTest(service=service, status=status, body=body):
                definition = alert_services.SERVICES[service]
                [request] = definition.requests(addresses[service], "", "desk", "body", "passive")
                request["fixture"] = {"status": status, "body": body, "json": request["json"]}
                if service == "discord":
                    request["fixture"]["headers"] = {"User-agent": request["headers"]["User-Agent"]}
                [result] = self.run_child([request])
                expected = {"ok": why == "sent", "why": why, "status": status, "tries": 1}
                if code is not None:
                    expected["code"] = code
                self.assertEqual(result, expected)

    def test_slack_text_error_codes_are_whole_bounded_words_only(self):
        bodies = ("invalid_payload\n", "private error message", "错", "a" * 41,
                  "invalid_payload" + " " * alert_http._RESPONSE_LIMIT, "<script>")
        requests = [dict(self.request(body, success={"text": "ok"}), url=f"https://push.example.test/{n}")
                    for n, body in enumerate(bodies)]
        for result in self.run_child(requests):
            self.assertEqual(result["why"], "refused_by_service")
            self.assertNotIn("code", result)

    def test_form_is_encoded_and_checked_in_child(self):
        form = {"pushkey": "key &+secret", "type": "text", "text": "中文题目\nbody & + = 🙂", "empty": ""}
        request = {"url": "https://push.example.test/send", "form": form,
                   "headers": {"content-TYPE": "text/plain", "X-Key": "header-secret"},
                   "success": {"field": "code", "equals": 0},
                   "fixture": {"body": '{"code":"0","secret":"response-body-secret"}',
                               "form": form, "headers": {"X-key": "header-secret"}}}
        [result] = self.run_child([request])
        self.assertEqual(result, {"ok": True, "why": "sent", "status": 200, "tries": 1})

    def test_forms_have_same_size_url_header_and_body_checks(self):
        base = {"url": "https://push.example.test/send", "form": {"text": "safe"}, "fixture": {"body": ""}}
        requests = [dict(base, form={"text": "x" * 65537}),
                    dict(base, form={"text": "中" * 8000}),
                    dict(base, url="http://push.example.test/send"),
                    dict(base, url="https://u:p@push.example.test/send"),
                    dict(base, headers={"X-Key": "key\nsecret"}),
                    dict(base, form={"text": ["not", "scalar"]}),
                    dict(base, form={"text": "a\ud800b"}),
                    dict(base, json={"text": "ambiguous"})]
        results = self.run_child(requests)
        self.assertEqual([r["why"] for r in results], ["too_big", "too_big"] + ["refused_url"] * 6)
        self.assertTrue(all(r["tries"] == 0 for r in results))

    def test_pushdeer_service_error_is_child_refusal_with_code_and_no_retry(self):
        definition = alert_services.SERVICES["pushdeer"]
        [request] = definition.requests(definition.normalize("fixture-key"), "", "台面", "正文", "active")
        request["fixture"] = {"body": '{"code":80501,"content":{"secret":"body-secret"},"message":"private reason"}',
                              "form": request["form"]}
        [result] = self.run_child([request])
        self.assertEqual(result, {"ok": False, "why": "refused_by_service", "code": 80501, "status": 200, "tries": 1})

    def test_numeric_true_and_exact_text_acknowledgements_in_child(self):
        cases = (({"field": "code", "equals": 0}, '{"code":0}', "sent", None),
                 ({"field": "code", "equals": 0}, '{"code":"0"}', "sent", None),
                 ({"field": "code", "equals": 0}, '{"code":"0.0"}', "sent", None),
                 ({"field": "code", "equals": 2.5}, '{"code":"2.50"}', "sent", None),
                 ({"field": "ok", "equals": True}, '{"ok":true}', "sent", None),
                 ({"field": "ok", "equals": True}, '{"ok":1}', "refused_by_service", 1),
                 ({"field": "ok", "equals": True}, '{"ok":"true"}', "refused_by_service", "true"),
                 ({"field": "code", "equals": 0}, '{"code":false}', "refused_by_service", None),
                 ({"field": "code", "equals": 0}, '{"code":"DENIED"}', "refused_by_service", "DENIED"),
                 ({"field": "code", "equals": 0}, 'not JSON secret', "refused_by_service", None),
                 ({"field": "code", "equals": 0}, '[]', "refused_by_service", None),
                 ({"text": "success"}, 'success', "sent", None),
                 ({"text": "success"}, 'success\n', "refused_by_service", None),
                 ({"text": "success"}, '{"code":0}', "refused_by_service", None))
        requests = [dict(self.request(body, success=check), url=f"https://push.example.test/{i}")
                    for i, (check, body, _, _) in enumerate(cases)]
        results = self.run_child(requests)
        for result, (check, body, why, code) in zip(results, cases):
            with self.subTest(check=check, body=body):
                self.assertEqual(result["why"], why)
                self.assertEqual(result["tries"], 1)
                self.assertEqual(result.get("code"), code)

    def test_non_2xx_keeps_status_category_and_safe_error_code(self):
        cases = (("ntfy", "https://ntfy.sh/fixture-topic", 403, {"code": 40301}),
                 ("ntfy", "https://ntfy.sh/fixture-topic", 429, {"code": 42908}),
                 ("gotify", "https://gotify.example/message?token=T", 401, {"errorCode": "Unauthorized"}),
                 ("pushdeer", "fixture-key", 400, {"code": 80501}),
                 ("serverchan", "SCTfixture-key", 400, {"code": 40001}))
        for service, address, status, answer in cases:
            with self.subTest(service=service, status=status):
                definition = alert_services.SERVICES[service]
                [request] = definition.requests(definition.normalize(address), "", "title", "body", "active")
                request["fixture"] = {"body": json.dumps({**answer, "message": "error-body-secret"}),
                                      "status": status, "max_tries": 3 if status == 429 else 1}
                [result] = self.run_child([request])
                self.assertEqual(result["why"], "http_429" if status == 429 else "http_4xx")
                self.assertEqual(result["code"], next(iter(answer.values())))
                self.assertEqual(result["tries"], 3 if status == 429 else 1)

    def test_separate_error_field_works_with_or_without_success_rule(self):
        requests = [dict(self.request('{"ok":false,"errcode":1301}',
                                     success={"field": "ok", "equals": True}, error_field="errcode"),
                         url="https://push.example.test/refusal"),
                    dict(self.request("", success={"field": "ok", "equals": True}, error_field="errcode"),
                         url="https://push.example.test/403",
                         fixture={"body": '{"ok":false,"errcode":1301}', "status": 403}),
                    dict(self.request("", error_field="errcode"), url="https://push.example.test/400",
                         fixture={"body": '{"errcode":"BadToken"}', "status": 400})]
        results = self.run_child(requests)
        self.assertEqual([(r["why"], r["code"]) for r in results],
                         [("refused_by_service", 1301), ("http_4xx", 1301), ("http_4xx", "BadToken")])

    def test_retry_deadline_keeps_last_http_service_code(self):
        source = '''
            class Reply:
                code, headers = 429, {}
                def read(self, size): return b'{"code":42908}'
                def close(self): pass
            class Opener:
                def open(self, request, timeout): return Reply()
            class Clock:
                now = 100.0
                def __call__(self): return self.now
                def sleep(self, seconds): self.now += seconds
            original = transport._send_one
            def send(request, deadline, *, progress):
                clock = Clock()
                return original(request, 100.5, opener=Opener(), clock=clock, sleep=clock.sleep, progress=progress)
            transport._send_one = send
            transport._main()
        '''
        with tempfile.TemporaryDirectory() as folder, self.child(folder, source):
            [result] = alert_http.send([self.request("", error_field="code")], deadline_s=20)
        self.assertEqual(result, {"ok": False, "why": "deadline", "status": 429, "tries": 1, "code": 42908})

    def test_only_http_200_satisfies_services_and_serverchan_checks_code(self):
        for service, address in (("ntfy", "https://ntfy.sh/fixture-topic"),
                                 ("gotify", "https://gotify.example/message?token=T"),
                                 ("pushdeer", "fixture-key"), ("serverchan", "SCTfixture-key")):
            definition = alert_services.SERVICES[service]
            for status, body in ((201, '{"code":0}'), (200, '{"code":40001}'), (200, 'not JSON')):
                if status == 200 and service in ("ntfy", "gotify"):
                    continue
                with self.subTest(service=service, status=status, body=body):
                    [request] = definition.requests(definition.normalize(address), "", "title", "body", "active")
                    request["fixture"] = {"body": body, "status": status}
                    [result] = self.run_child([request])
                    self.assertEqual(result["why"], "refused_by_service")
                    self.assertEqual(result["tries"], 1)

    def test_unsafe_service_code_shapes_never_leave_child(self):
        codes = [False, True, None, [], {"secret": "body"}, "a secret message", "错", "A" * 41,
                 "line\nbreak", "<script>", float("inf"), float("nan")]
        requests = [dict(self.request(json.dumps({"code": code, "message": "response-body-secret"}),
                                     success={"field": "code", "equals": 0}),
                         url=f"https://push.example.test/{i}") for i, code in enumerate(codes)]
        for result in self.run_child(requests):
            self.assertEqual(result["why"], "refused_by_service")
            self.assertNotIn("code", result)
            self.assertEqual(set(result), {"ok", "why", "status", "tries"})

    def test_truncated_or_unreadable_body_cannot_satisfy_acknowledgement(self):
        size = alert_http._RESPONSE_LIMIT
        json_prefix = '{"code":0}' + ' ' * (size - len('{"code":0}'))
        requests = [self.request("a" * size + "tail", success={"text": "a" * size}),
                    dict(self.request(json_prefix + "not JSON", success={"field": "code", "equals": 0}),
                         url="https://push.example.test/json"),
                    dict(self.request("", success={"text": ""}), url="https://push.example.test/read-error",
                         fixture={"body": "", "read_error": True})]
        for result in self.run_child(requests):
            self.assertEqual((result["why"], result["tries"]), ("refused_by_service", 1))
            self.assertNotIn("code", result)

    def test_parent_rechecks_child_codes_and_drops_arbitrary_fields(self):
        codes = [80501, "FORBIDDEN", "A" * 40, True, {"secret": "body"}, "a secret message", "A" * 41]
        source = '''
            given = json.load(sys.stdin)
            for i, request in enumerate(given['requests']):
                print(json.dumps({'index': i, 'tries': 1, 'why': 'refused_by_service', 'status': 200,
                                  'code': request['fixture']['code'], 'message': 'body-secret',
                                  'url': request['url'], 'ok': 'body-secret'}))
        '''
        requests = [dict(self.request(""), fixture={"code": code}) for code in codes]
        with tempfile.TemporaryDirectory() as folder, self.child(folder, source):
            results = alert_http.send(requests, deadline_s=20)
        for i, result in enumerate(results):
            self.assertEqual(result["why"], "refused_by_service")
            self.assertFalse(result["ok"])
            self.assertEqual(result.get("code"), codes[i] if i < 3 else None)
            self.assertLessEqual(set(result), {"ok", "why", "status", "tries", "code"})

    def test_malformed_acknowledgement_rules_are_refused_before_open(self):
        checks = [{"field": "code"}, {"equals": 0}, {"field": "code", "equals": False},
                  {"field": "code", "equals": "0"}, {"field": "code", "equals": []},
                  {"text": "ok", "field": "code", "equals": 0}, {"text": 1},
                  {"status": True}, {"status": 400}, {"secret": "body"}, []]
        requests = [dict(self.request("{}", success=check), url=f"https://push.example.test/{i}")
                    for i, check in enumerate(checks)]
        for result in self.run_child(requests):
            self.assertEqual((result["why"], result["tries"]), ("refused_url", 0))


if __name__ == "__main__":
    unittest.main()
