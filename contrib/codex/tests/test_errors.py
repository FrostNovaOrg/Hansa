"""Codex's failures, worded so that hansa's own unusable_reason, network_reason and reset_time class them as they
would Claude Code's: the provider rests for a usage limit (until its reset) or a login, a dropped connection is
tried once more."""
import json
import shlex
import subprocess
import time
import unittest
from unittest import mock

from .codexkit import FAKES, FIXTURES, Scratch, hansa_run, shim

from hansa.cli import init_desk
from hansa.desk import Desk
from hansa import views
from hansa.web import _top_up_provider


class ErrorMappings(Scratch):
    def failed_run(self, fixture):
        run, code = self.run_worker(fixture)
        self.assertEqual(code, 1)
        self.assertEqual((run["type"], run["subtype"], run["is_error"], run["session_id"]),
                         ("result", "error_during_execution", True, self.sid))
        return run

    def test_usage_limit_rests_until_the_reset(self):
        run = self.failed_run("usage_limit.jsonl")
        kind, why = hansa_run.unusable_reason(run, self.err)
        self.assertEqual(kind, "quota", run["result"])
        # the rest hansa's rest_provider gives: reset_time of the result and stderr, as finish passes them
        reset = hansa_run.reset_time(f"{run['result']}\n{self.err}")
        self.assertIsNotNone(reset, run["result"])
        self.assertAlmostEqual(reset, time.time() + 3 * 3600, delta=120)
        self.assertEqual(hansa_run.network_reason(run, self.err) if not kind else "", "")

    def test_usage_limit_without_a_time(self):
        run = self.failed_run("usage_limit_no_time.jsonl")
        self.assertEqual(hansa_run.unusable_reason(run, self.err)[0], "quota")
        self.assertIsNone(hansa_run.reset_time(f"{run['result']}\n{self.err}"))

    def test_workspace_credits_and_plan_upgrade_are_credit_errors(self):
        for fixture in ("out_of_credits.jsonl", "plan_upgrade.jsonl"):
            with self.subTest(fixture=fixture):
                run = self.failed_run(fixture)
                self.assertEqual((run["api_error_status"], hansa_run.unusable_reason(run, self.err)),
                                 (402, ("credit", "credit balance too low")))

    def test_401_is_a_failed_login(self):
        run = self.failed_run("unauthorized.jsonl")
        self.assertEqual(hansa_run.unusable_reason(run, self.err), ("login", f"not logged in; {run['login_hint']}"))
        self.assertIn(f"CODEX_HOME={shlex.quote(str(self.home))} {shlex.quote(str(FAKES / 'codex'))} login",
                      run["login_hint"])
        self.assertNotIn("/login", run["result"])
        self.assertEqual(hansa_run.network_reason(run, self.err), "")

    def test_not_logged_in(self):
        run = self.failed_run("not_logged_in.jsonl")
        self.assertEqual(hansa_run.unusable_reason(run, self.err), ("login", f"not logged in; {run['login_hint']}"))

    def test_dropped_stream_is_a_dropped_connection(self):
        run = self.failed_run("dropped_stream.jsonl")
        self.assertEqual(hansa_run.unusable_reason(run, self.err), ("", ""))
        self.assertEqual(hansa_run.network_reason(run, self.err),
                         "the connection to the provider failed (Connection dropped)")
        self.assertEqual(run["num_turns"], 1)

    def test_refused_connection(self):
        run = self.failed_run("connection_refused.jsonl")
        self.assertEqual(hansa_run.unusable_reason(run, self.err), ("", ""))
        self.assertEqual(hansa_run.network_reason(run, self.err),
                         "the connection to the provider failed (Connection error)")

    def test_workspace_routing_discovery_failed_is_a_connection_error(self):
        run = self.failed_run("workspace_routing_discovery_failed.jsonl")
        self.assertEqual(hansa_run.unusable_reason(run, self.err), ("", ""))
        self.assertEqual(hansa_run.network_reason(run, self.err),
                         "the connection to the provider failed (Connection error)")
        self.assertTrue(run["result"].startswith(
            "API Error: Connection error (Codex: workspace routing discovery failed)"))
        self.assertEqual(run["codex_warnings"], ["Falling back from WebSockets to HTTPS transport."])

    def test_server_error(self):
        run = self.failed_run("server_error.jsonl")
        self.assertEqual(hansa_run.unusable_reason(run, self.err), ("", ""))
        self.assertEqual(hansa_run.network_reason(run, self.err), "the provider had a server error (HTTP 503)")


class Wordings(unittest.TestCase):
    """Other forms Codex has used, through classify alone."""

    def kind(self, text):
        run = {"type": "result", "is_error": True, **shim.classify(text)}
        return hansa_run.unusable_reason(run, "")[0] or hansa_run.network_reason(run, ""), run

    def test_reset_forms(self):
        now = time.time()
        for text, want in (
                ("You've hit your usage limit. Try again in 2 hours 5 minutes.", now + 7500),
                ("You've hit your usage limit. Try again in 1 day 3 hours.", now + 97200),
                ('usage_limit_reached {"resets_in_seconds": 600}', now + 600),
                (f'{{"type":"usage_limit_reached","resets_at":{int(now) + 900}}}', now + 900)):
            kind, run = self.kind(text)
            self.assertEqual(kind, "quota", text)
            self.assertAlmostEqual(hansa_run.reset_time(run["result"]), want, delta=90, msg=text)

    def test_clock_reset_is_the_next_one(self):
        at = time.mktime((2026, 10, 10, 16, 0, 0, 0, 0, -1))
        self.assertEqual(shim.reset_at("or try again at 3:05 PM.", at), time.mktime((2026, 10, 11, 15, 5, 0, 0, 0, -1)))
        self.assertEqual(shim.reset_at("or try again at 5:30 PM.", at), time.mktime((2026, 10, 10, 17, 30, 0, 0, 0, -1)))

    def test_other_wordings(self):
        for text, want in (
                ("exceeded retry limit, last status: 429 Too Many Requests", "quota"),
                ("Rate limit reached for gpt in organization", "quota"),
                ("unexpected status 403 Forbidden: denied", "key"),
                ("Your access token could not be refreshed because your refresh token was already used. "
                 "Please log out and sign in again.", "login"),
                ("Token data is not available.", "login"),
                ("stream disconnected before completion: idle timeout waiting for SSE",
                 "the connection to the provider failed (Connection dropped)"),
                ("error sending request for url (https://chatgpt.com/backend-api/codex/responses): operation timed out",
                 "the connection to the provider failed (Connection error)"),
                ("workspace routing discovery failed", "the connection to the provider failed (Connection error)"),
                ("workspace routing discovery timed out", "the connection to the provider failed (Connection error)"),
                ("unexpected status 500 Internal Server Error", "the provider had a server error (HTTP 500)")):
            self.assertEqual(self.kind(text)[0], want, text)

    def test_other_routing_messages_are_not_connection_errors(self):
        for text, want in (
                ("workspace routing discovery unauthorized (401)", "key"),
                ("selected workspace missing from routing discovery", "")):
            with self.subTest(text=text):
                kind, run = self.kind(text)
                self.assertEqual(kind, want)
                self.assertEqual(hansa_run.network_reason(run, ""), "")

    def test_a_task_failure_is_neither(self):
        kind, run = self.kind("model produced invalid tool call")
        self.assertEqual(kind, "")
        self.assertTrue(run["result"].startswith("Codex failed: "))

    def test_an_adapter_hint_does_not_change_a_subscription_login(self):
        run = {"is_error": True, "result": "Not logged in", "login_hint": "sign in with the adapter"}
        self.assertEqual(hansa_run.unusable_reason(run, "", subscription=True),
                         ("login", "not logged in (Please run /login)"))

    def test_login_hint_quotes_the_configured_home_and_executable(self):
        home, codex = "/tmp/adapter's home", "/tmp/codex tools/bin/codex"
        run = shim.classify("Not logged in", home, codex)
        command = run["login_hint"].split("`")[1]
        self.assertEqual(shlex.split(command), [f"CODEX_HOME={home}", codex, "login"])


class ProviderFailures(Scratch):
    """Fixture failures through finish and the views, on any platform, without starting a Codex worker."""

    def setUp(self):
        super().setUp()
        self.desk = init_desk(self.root / "desk", str(self.root / "repo"), "a")
        s = self.desk.settings()
        s["providers"]["adapter"] = {"kind": "compatible", "model": shim.DEFAULT_MODEL, "base_url": shim.PLACEHOLDER}
        s["lanes"]["a"]["providers"] = ["adapter"]
        self.assertEqual(self.desk.save_settings(s), [])
        self.desk.write_key("adapter", "sk-placeholder-0001")
        self.home = str(self.root / "codex home's login")

    def failure(self, fixture):
        events = [json.loads(line) for line in (FIXTURES / fixture).read_text().splitlines()]
        text = next(e["error"]["message"] for e in events if e["type"] == "turn.failed")
        return {"type": "result", "subtype": "error_during_execution", "is_error": True,
                **shim.classify(text, self.home)}

    def finish_failure(self, run):
        tid = self.desk.new_task("Add hello.txt")
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.wt, text=True).strip()
        self.desk.update(tid, state="running", lane="a", provider="adapter", worktree=str(self.wt),
                         branch="task/t1", base=head)
        logs = self.desk.state_dir / "runs" / tid
        logs.mkdir(parents=True)
        (logs / "run.json").write_text(json.dumps(run))
        return hansa_run.finish(self.desk, tid, 1)

    def problem(self):
        return hansa_run.provider_problem(Desk(self.desk.root), self.desk.settings(), "adapter")

    def credit_events(self):
        return [e for e in map(json.loads, self.desk.events_path.read_text().splitlines())
                if e.get("event") == "provider_credit"]

    def test_unknown_model_codex_runs_rest_once_and_use_no_try(self):
        for run in (self.failure("model_not_found.jsonl"),
                    *({"is_error": True, **shim.classify(f"unexpected status {status}: model_not_found")}
                      for status in (400, 404))):
            with self.subTest(run=run):
                self.desk.write_key("adapter", "sk-placeholder-0001")
                for _ in range(2):
                    m = self.finish_failure(run)
                    self.assertEqual((m["state"], hansa_run.tries_used(m["attempts"])), ("queued", 0))
                self.assertIn(f"does not know model {shim.DEFAULT_MODEL}", self.problem())
                self.assertFalse(self.desk.provider_state()["adapter"].get("cool_until"))
        events = [e for e in map(json.loads, self.desk.events_path.read_text().splitlines())
                  if e.get("event") == "provider_model"]
        self.assertEqual(len(events), 3)  # one per rest, even when two tasks had already started

    def test_unknown_model_server_errors_use_the_outage_path(self):
        for text in ("API Error: 503 The model is currently not available, please try again later",
                     "500 model temporarily not available"):
            with self.subTest(text=text):
                m = self.finish_failure({"is_error": True, **shim.classify(text)})
                self.assertEqual((m["state"], m["attempts"][-1]["kind"]), ("queued", "network"))
                st = self.desk.provider_state()["adapter"]
                self.assertTrue(st.get("unreachable_since"))
                self.assertFalse(st.get("model_since"))
                self.assertIn("server error", self.problem())

    def test_lost_codex_login_shows_its_home_and_working_remedy(self):
        for fixture in ("refresh_token_expired.jsonl", "unauthorized.jsonl"):
            with self.subTest(fixture=fixture):
                m = self.finish_failure(self.failure(fixture))
                self.assertEqual((m["state"], m["attempts"][-1]["kind"], hansa_run.tries_used(m["attempts"])),
                                 ("queued", "login", 0))
                self.assertAlmostEqual(self.desk.provider_state()["adapter"]["cool_until"],
                                       time.time() + 24 * 3600, delta=30)
                problem = self.problem()
                self.assertIn(f"sign in again with `CODEX_HOME={shlex.quote(self.home)} codex login`", problem)
                self.assertIn("then `hansa ping adapter`", problem)
                for shown in (problem, m["error"], views.explain_text(self.desk, "provider", "adapter")):
                    self.assertNotIn("/login", shown)
                    self.assertNotIn("saving a new key", shown)

    def test_credit_and_plan_failures_wait_for_top_up_once_and_use_no_try(self):
        for fixture in ("out_of_credits.jsonl", "plan_upgrade.jsonl"):
            with self.subTest(fixture=fixture):
                self.desk.write_key("adapter", "sk-placeholder-0001")
                before = len(self.credit_events())
                run = self.failure(fixture)
                for _ in range(2):  # two tasks already running on the same provider
                    m = self.finish_failure(run)
                    self.assertEqual((m["state"], m["attempts"][0]["kind"], hansa_run.tries_used(m["attempts"])),
                                     ("queued", "credit", 0))
                    self.assertFalse(m["attempts"][0]["counted"])
                st = self.desk.provider_state()["adapter"]
                self.assertGreater(st["credit_since"], 0)
                self.assertEqual((st["cool_until"], st["rest_how"]), (0, "credit"))
                events = self.credit_events()
                self.assertEqual(len(events), before + 1)
                self.assertIn("topped up on the page", self.problem())
                with mock.patch.object(hansa_run.time, "time", return_value=time.time() + 40 * 86400):
                    self.assertIn("topped up on the page", self.problem())
                with mock.patch.object(hansa_run.subprocess, "run", return_value=subprocess.CompletedProcess(
                        [], 0, json.dumps({"type": "result", "result": "OK"}), "")):
                    self.assertTrue(hansa_run.check_provider(self.desk, "adapter", claude=str(FAKES / "claude"))["ok"])
                self.assertIn("topped up on the page", self.problem())
                self.desk.write_key("adapter", "sk-topped-up")
                self.assertEqual(self.problem(), "")
                self.finish_failure(run)
                self.assertEqual(_top_up_provider(self.desk, "adapter"), {"ok": True})
                self.assertEqual(self.problem(), "")

    def test_raw_out_of_credits_precedes_a_429_and_reset(self):
        run = {"is_error": True, "api_error_status": 429,
               "result": "Your workspace is out of credits. Add credits; resets in 1 minute."}
        self.assertEqual(hansa_run.unusable_reason(run, ""), ("credit", "credit balance too low"))


if __name__ == "__main__":
    unittest.main()
