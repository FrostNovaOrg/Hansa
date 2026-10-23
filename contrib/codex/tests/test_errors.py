"""Codex's failures, worded so that hansa's own unusable_reason, network_reason and reset_time class them as they
would Claude Code's: the provider rests for a usage limit (until its reset) or a login, a dropped connection is
tried once more."""
import time
import unittest

from .codexkit import Scratch, hansa_run, shim


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

    def test_401_is_a_failed_login(self):
        run = self.failed_run("unauthorized.jsonl")
        self.assertEqual(hansa_run.unusable_reason(run, self.err), ("login", "not logged in (Please run /login)"))
        self.assertEqual(hansa_run.network_reason(run, self.err), "")

    def test_not_logged_in(self):
        run = self.failed_run("not_logged_in.jsonl")
        self.assertEqual(hansa_run.unusable_reason(run, self.err), ("login", "not logged in (Please run /login)"))

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


if __name__ == "__main__":
    unittest.main()
