"""Codex 0.162.1's own error texts (several with ’, U+2019), each classed on the whole text as hansa reads Claude
Code's: a provider limit (with its reset, read by hansa's reset_time), overloaded, a refusal, a dropped connection,
a failed login, a model not available; retries are not final, and --strict-config's refusal is a configuration
failure. And turn.completed's usage, cache writes and all."""
import time
import unittest
from datetime import datetime, timedelta

from .codexkit import Scratch, hansa_run, shim

DROPPED = "the connection to the provider failed (Connection dropped)"


def read(text):
    """(hansa's unusable kind, hansa's network reason, the answer's fields) for Codex's text."""
    run = {"type": "result", "is_error": True, **shim.classify(text)}
    return hansa_run.unusable_reason(run, "")[0], hansa_run.network_reason(run, ""), run


def clock(t: datetime) -> str:
    """t as Codex prints a reset: "3:05 PM" on the same day, else "Oct 12th, 2026 3:05 PM"."""
    day = t.day
    suffix = "th" if 11 <= day % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    hm = t.strftime("%I:%M %p").lstrip("0")
    return hm if t.date() == datetime.now().date() else t.strftime(f"%b {day}{suffix}, %Y ") + hm


class ProviderLimits(unittest.TestCase):
    def test_each_limit_rests_the_provider(self):
        for text in (
                "You’ve hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), visit "
                "https://chatgpt.com/codex/settings/usage to purchase more credits or try again later.",
                "You’ve hit your usage limit. Visit https://chatgpt.com/codex/settings/usage to purchase more "
                "credits or try again later.",
                "You’ve hit your usage limit. Try again later.",
                "You've hit your usage limit. Try again later.",
                "You hit your spend cap. Ask your workspace admin to raise it.",
                "Quota exceeded. Check your plan and billing details."):
            with self.subTest(text=text):
                kind, net, run = read(text)
                self.assertEqual((kind, net), ("quota", ""))
                self.assertEqual(run["api_error_status"], 429)
                self.assertIsNone(hansa_run.reset_time(run["result"]), "no time given: the provider's own rest")

    def test_credits_and_plan_are_credit_rests_even_with_quota_words(self):
        for text in ("Your workspace is out of credits. Add credits to continue using Codex.",
                     "To use Codex with your ChatGPT plan, upgrade to Plus: https://chatgpt.com/explore/plus."):
            with self.subTest(text=text):
                kind, net, run = read(text + " Quota exceeded; try again in 1 hour.")
                self.assertEqual((kind, net, run["api_error_status"]), ("credit", "", 402))

    def reset_read(self, text, want: datetime):
        kind, _, run = read(text)
        self.assertEqual(kind, "quota", text)
        self.assertIn("resets_at: ", run["result"])
        self.assertEqual(hansa_run.reset_time(run["result"]), want.timestamp(), text)

    def test_reset_time_forms_as_hansa_reads_them(self):
        now = datetime.now().replace(second=0, microsecond=0)
        for later in (now + timedelta(minutes=3), now + timedelta(hours=3), now + timedelta(days=2, hours=1)):
            t = clock(later)
            for text in (
                    f"You’ve hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), visit "
                    f"https://chatgpt.com/codex/settings/usage to purchase more credits or try again at {t}.",
                    f"You’ve hit your usage limit. Visit https://chatgpt.com/codex/settings/usage to purchase more "
                    f"credits or try again at {t}.",
                    f"You’ve hit your usage limit. Try again at {t}."):
                with self.subTest(text=text):
                    if later.date() != now.date():  # a clock alone would be read as today's or tomorrow's
                        self.assertIn(",", t)
                    self.reset_read(text, later)

    def test_reset_clock_and_date_read_as_local_time(self):
        at = time.mktime((2026, 10, 10, 16, 0, 0, 0, 0, -1))
        self.assertEqual(shim.reset_at("You’ve hit your usage limit. Try again at 5:30 PM.", at),
                         time.mktime((2026, 10, 10, 17, 30, 0, 0, 0, -1)))
        self.assertEqual(shim.reset_at("… or try again at Oct 12th, 2026 3:05 PM.", at),
                         time.mktime((2026, 10, 12, 15, 5, 0, 0, 0, -1)))
        self.assertEqual(shim.reset_at("… or try again at Nov 1st, 2026 12:00 AM.", at),
                         time.mktime((2026, 11, 1, 0, 0, 0, 0, 0, -1)))
        self.assertIsNone(shim.reset_at("You’ve hit your usage limit. Try again later.", at))

    def test_429_after_character_400(self):
        text = ("exceeded retry limit while " + "talking to the provider " * 30 + "; last status: 429 Too Many "
                "Requests")
        self.assertGreater(text.index("429"), 400)
        kind, _, run = read(text)
        self.assertEqual(kind, "quota")
        self.assertEqual(run["api_error_status"], 429)
        self.assertLessEqual(len(run["result"]), 450, "only what is shown is cut")
        self.assertNotIn("Too Many", run["result"])
        # and a limit's own words, far into a long text, with its reset
        later = datetime.now().replace(second=0, microsecond=0) + timedelta(hours=2)
        long = "x " * 400 + f"You’ve hit your usage limit. Try again at {clock(later)}."
        self.reset_read(long, later)


class OtherForms(unittest.TestCase):
    def test_unknown_model_400_and_404_are_model_errors(self):
        for status in (400, 404):
            for text in ("model_not_found", "The model `example` does not exist", "Unknown model: example",
                         "The model `example` is not supported", "model_not_available", "unknown_model",
                         "example is not a valid model ID", "Model Not Exist"):
                with self.subTest(status=status, text=text):
                    kind, net, run = read(f"unexpected status {status}: {text}")
                    self.assertEqual((kind, net, run["api_error_status"]), ("model", "", status))
        kind, net, _ = read("unexpected status 400 Bad Request: unsupported parameter temperature")
        self.assertEqual((kind, net), ("", ""))

    def test_overloaded_as_claude_overloaded(self):
        for text in ("Selected model is at capacity. Please try a different model.",
                     "We’re currently experiencing high demand, which may cause temporary errors."):
            with self.subTest(text=text):
                kind, net, run = read(text)
                self.assertEqual((kind, net), ("", "the provider was overloaded (HTTP 529)"))

    def test_content_filter_is_a_refusal_not_a_drop(self):
        kind, net, run = read("stream disconnected before completion: Incomplete response returned, reason: "
                              "content_filter")
        self.assertEqual(kind, "refused")
        self.assertEqual(run["stop_reason"], "content_filter")
        self.assertNotIn("Connection dropped", run["result"])

    def test_other_disconnects_are_drops(self):
        for reason in ("Incomplete response returned, reason: max_output_tokens", "idle timeout waiting for SSE",
                       "error sending request for url (https://chatgpt.com/backend-api/codex/responses)",
                       "stream closed before response.completed"):
            with self.subTest(reason=reason):
                self.assertEqual(read(f"stream disconnected before completion: {reason}")[:2], ("", DROPPED))

    def test_login_failed(self):
        for text in (
                "Your access token could not be refreshed because your refresh token has expired. Please log out "
                "and sign in again.",
                "Your access token could not be refreshed because your refresh token was already used. Please log "
                "out and sign in again.",
                "Your access token could not be refreshed because your refresh token was revoked. Please log out "
                "and sign in again.",
                "Your access token could not be refreshed. Please log out and sign in again.",
                "unexpected status 401 Unauthorized: Provided authentication token is expired."):
            with self.subTest(text=text):
                kind, net, run = read(text)
                self.assertEqual((kind, net), ("login", ""))
                self.assertIn("codex login", run["login_hint"])
                self.assertNotIn("/login", run["result"])

    def test_model_not_available(self):
        kind, net, run = read("unexpected status 404 Not Found: {\"detail\":\"Model not found gpt-x\"}")
        self.assertEqual((kind, net), ("model", ""))
        self.assertEqual(run["api_error_status"], 404)
        self.assertIn("model is not available", run["result"])

    def test_retry_messages_are_matched(self):
        for m in ("Reconnecting... 1/5 (stream disconnected before completion: x)",
                  "Reconnecting... waiting for network (error sending request)"):
            self.assertTrue(shim.RETRY.match(m), m)
        self.assertFalse(shim.RETRY.match("You’ve hit your usage limit. Try again later."))


class Runs(Scratch):
    def failed_run(self, fixture):
        run, code = self.run_worker(fixture)
        self.assertEqual((code, run["subtype"], run["is_error"]), (1, "error_during_execution", True))
        return run

    def test_retries_are_not_final(self):
        run = self.failed_run("retries_then_limit.jsonl")
        self.assertEqual(hansa_run.unusable_reason(run, self.err)[0], "quota")
        self.assertAlmostEqual(hansa_run.reset_time(run["result"]), time.time() + 3 * 3600, delta=120)

    def test_only_retries_then_an_end(self):
        run = self.failed_run("retries_only.jsonl")
        self.assertNotIn("Reconnecting", run["result"])
        self.assertEqual(hansa_run.network_reason(run, self.err), DROPPED)

    def test_refusal_run(self):
        run = self.failed_run("content_filter.jsonl")
        self.assertEqual(hansa_run.unusable_reason(run, self.err)[0], "refused")

    def test_strict_config_failure(self):
        for fixture, said in (("strict_config.jsonl", "unknown configuration field `approvals_reviewer`"),
                              ("unknown_feature.jsonl", "Unknown feature flag: multi_agent")):
            with self.subTest(fixture=fixture):
                run = self.failed_run(fixture)
                self.assertIn("Codex refused its configuration", run["result"])
                self.assertIn(said, run["result"])
                self.assertEqual(hansa_run.unusable_reason(run, self.err), ("", ""))
                self.assertEqual(hansa_run.network_reason(run, self.err), "")

    def test_model_warning_said_not_failed(self):
        run, code = self.run_worker("model_warning.jsonl")
        self.assertEqual((code, run["subtype"]), (0, "success"))
        self.assertTrue(run["result"].startswith("Done."))
        self.assertIn("Codex warned: Model metadata for `gpt-6.1-sol` not found", run["result"])
        self.assertEqual(len(run["codex_warnings"]), 1)

    def test_model_not_found_run(self):
        run = self.failed_run("model_not_found.jsonl")
        self.assertEqual(run["api_error_status"], 404)
        self.assertIn("Model metadata for `gpt-6.1-sol` not found", run["result"])
        self.assertEqual(hansa_run.unusable_reason(run, self.err)[0], "model")

    def test_usage_with_cache_writes(self):
        run, code = self.run_worker("all_usage.jsonl")
        self.assertEqual(code, 0)
        self.assertEqual(run["modelUsage"][shim.DEFAULT_MODEL], {
            "inputTokens": 50000 - 30000 - 12000, "cacheReadInputTokens": 30000, "cacheCreationInputTokens": 12000,
            "outputTokens": 900, "reasoningOutputTokens": 600})
        self.assertEqual(hansa_run.usage_of(run)[shim.DEFAULT_MODEL], {
            "in": 8000, "out": 900, "cache_read": 30000, "cache_write": 12000, "claude_code_usd": None, "basis": ""})


if __name__ == "__main__":
    unittest.main()
