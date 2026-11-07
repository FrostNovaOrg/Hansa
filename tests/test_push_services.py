"""Phone push services and child-only HTTP acknowledgements, without a network."""
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

from hansa import alert_http, alert_push, alert_services, views
from hansa.cli import init_desk
from hansa.desk import public_settings, read_json, write_json


class PhoneServicesTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.tmp = Path(folder.name)
        env = patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.tmp / "conf"),
                                     "XDG_STATE_HOME": str(self.tmp / "state")})
        env.start()
        self.addCleanup(env.stop)
        self.desk = init_desk(self.tmp / "advisor" / "desk")

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
        with self.assertRaisesRegex(ValueError, "HTTPS.*8800"):
            self.save("http://pushdeer.example.com:8800/message/push?pushkey=K", "pushdeer")
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
