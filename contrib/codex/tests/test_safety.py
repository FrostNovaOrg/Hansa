"""The provider's safety check stopped a Codex run: the shim answers with a safety stop (stop_reason content_filter,
is_error), in its own words, and hansa ends the task blocked."""
import json
import time
from pathlib import Path

from .codexkit import Scratch, alive, hansa_run, shim
from hansa.cli import init_desk

RUNTEXT = "RUNTEXT-7f3a"  # in every message of the fixtures' runs: never in the shim's answer
PHRASES = {"safety_cyber.jsonl": "flagged for possible cybersecurity risk",
           "safety_bio.jsonl": "flagged for possible biological risk",
           "safety_usage_policy.jsonl": "flagged as potentially violating our usage policy",
           "safety_misalignment.jsonl": "blocked due to a misalignment policy violation"}
CODES = {"safety_code_task_complete.jsonl": "cyber_policy", "safety_code_bio_policy.jsonl": "bio_policy",
         "safety_code_invalid_prompt.jsonl": "invalid_prompt",
         "safety_code_misalignment_policy_violation.jsonl": "misalignment_policy_violation"}
STOPS = [*PHRASES, *CODES, "safety_rerouted.jsonl"]


class Words(Scratch):
    def test_each_phrase_is_a_safety_stop_in_the_shims_words(self):
        for phrase in PHRASES.values():
            for said in (phrase, f"Error: this was {phrase.upper()} ({RUNTEXT})."):
                with self.subTest(said=said):
                    got = shim.classify(said)
                    self.assertEqual(got["stop_reason"], "content_filter")
                    self.assertEqual(got["result"].lower(), f"codex: the provider's safety check stopped the run "
                                                            f"({phrase})")
                    self.assertTrue(hansa_run.safety_stopped({**got, "is_error": True}))

    def test_error_info_wherever_it_is(self):
        self.assertEqual(shim.error_info({"type": "task_complete", "error": {"codex_error_info": "cyber_policy"}}),
                         "cyber_policy")
        self.assertEqual(shim.error_info({"msg": {"codex_error_info": {"http_connection_failed": {"s": 1}}}}),
                         "http_connection_failed")
        self.assertIsNone(shim.error_info({"type": "error", "message": "cyber_policy"}))


class Runs(Scratch):
    def stopped(self, fixture, **extra):
        run, code = self.run_worker(fixture, **extra)
        self.assertEqual((code, run["subtype"], run["is_error"], run["stop_reason"]),
                         (1, "error_during_execution", True, "content_filter"), run)
        self.assertTrue(run["result"].startswith("Codex: the provider's safety check stopped the run ("), run)
        self.assertNotIn(RUNTEXT, json.dumps(run))
        self.assertTrue(hansa_run.safety_stopped(run))
        return run

    def test_a_final_error_with_a_phrase(self):
        for fixture, phrase in PHRASES.items():  # error and turn.failed, turn.failed alone, error alone
            with self.subTest(fixture):
                self.assertEqual(self.stopped(fixture)["result"],
                                 f"Codex: the provider's safety check stopped the run ({phrase})")

    def test_a_code_in_codex_error_info(self):
        for fixture, code in CODES.items():  # a task_complete event's error, an error event, turn.failed's error
            with self.subTest(fixture):
                self.assertEqual(self.stopped(fixture)["result"],
                                 f"Codex: the provider's safety check stopped the run ({code})")

    def test_a_retried_attempt_then_a_normal_end_is_not_a_stop(self):
        for fixture in ("safety_retried.jsonl", "safety_code_retried.jsonl"):
            with self.subTest(fixture):
                run, code = self.run_worker(fixture)
                self.assertEqual((code, run["subtype"], run["is_error"]), (0, "success", False), run)
                self.assertNotIn("stop_reason", run)
                self.assertFalse(hansa_run.safety_stopped(run))

    def test_another_code_is_not_a_stop(self):
        run, code = self.run_worker("error_code_other.jsonl")
        self.assertEqual(code, 1)
        self.assertNotIn("stop_reason", run)
        self.assertFalse(hansa_run.safety_stopped(run))

    def test_the_content_filter_disconnect_as_before(self):
        run = self.run_worker("content_filter.jsonl")[0]
        self.assertEqual(run["result"], "Codex: the provider refused the request (content_filter): stream "
                                        "disconnected before completion: Incomplete response returned, reason: "
                                        "content_filter")
        self.assertTrue(hansa_run.safety_stopped(run))

    def test_a_reroute_is_a_stop_and_nothing_after_it_is_used(self):
        # all of it written at once: the lines after the item, and Codex's last message (-o), are not read
        run = self.stopped("safety_rerouted.jsonl")
        self.assertEqual(run["result"], "Codex: the provider's safety check stopped the run (the request was moved "
                                        "to another model; hansa-codex-shim stopped Codex)")
        self.assertNotIn("AFTER-REROUTE", json.dumps(run))
        self.assertNotIn("rerouted", run["result"])
        self.assertEqual((run["num_turns"], run["usage"]), (0, {}))  # not the command or the usage after it

    def test_a_reroute_stops_codex_at_once(self):
        done = self.root / "codex-done"
        began = time.monotonic()
        run = self.stopped("safety_rerouted.jsonl", FAKE_CODEX_DELAY="2", FAKE_CODEX_DONE=str(done))
        # Codex would write the four lines after the item over eight seconds more, then the done file
        self.assertLess(time.monotonic() - began, 7 + 10)
        self.assertNotIn("AFTER-REROUTE", json.dumps(run))
        self.assertFalse(alive(self.codex_called()["pid"]), "Codex goes on no longer")
        time.sleep(1)
        self.assertFalse(done.exists(), "Codex did not reach its end")


class Blocked(Scratch):
    def test_each_stop_ends_blocked_with_nothing_of_the_run_outside_its_folder(self):
        desk = init_desk(self.root / "desk", repo=str(self.root / "repo"))
        settings = desk.settings()
        settings["providers"]["codex"] = {"kind": "compatible", "model": shim.DEFAULT_MODEL, "effort": "",
                                           "base_url": shim.PLACEHOLDER}
        settings["lanes"]["a"]["providers"] = ["codex"]
        self.assertEqual(desk.save_settings(settings), [])
        base = hansa_run.git("rev-parse", "HEAD", cwd=self.wt)
        for fixture in STOPS:
            with self.subTest(fixture):
                p = self.start(self.worker_argv(), self.env(fixture))
                out, err = p.communicate(timeout=60)
                tid = desk.new_task("Check the scanner", lane="a")
                desk.change(tid, lambda m: {"state": "running", "provider": "codex", "session_id": self.sid,
                                           "worktree": str(self.wt), "branch": "task/t1", "base": base,
                                           "run_start": {"commit": base}, "started": time.time()})
                raw = desk.state_dir / "runs" / tid
                raw.mkdir(parents=True, exist_ok=True)
                (raw / "run.json").write_bytes(out)
                (raw / "run.err").write_bytes(err + f"\n{RUNTEXT}\n".encode())  # as if Codex said more there
                meta = hansa_run.finish(desk, tid, p.returncode)
                self.assertEqual((meta["state"], meta["blocked"]["reason"]), ("blocked", "safety"), meta)
                self.assertEqual((desk.tasks_dir / tid / "result.md").read_text(), hansa_run.BLOCKED_REPORT)
                folder = desk.state_dir / "blocked" / tid
                self.assertIn("safety check stopped the run", (folder / "run.json").read_text())
                self.assertIn(RUNTEXT, (folder / "run.err").read_text())
                for f in [*desk.tasks_dir.rglob("*"), desk.events_path, desk.asks_path]:
                    if Path(f).is_file():
                        self.assertNotIn(RUNTEXT.encode(), Path(f).read_bytes(), f)
                        self.assertNotIn(b"AFTER-REROUTE", Path(f).read_bytes(), f)
