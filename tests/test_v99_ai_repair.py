"""v9.9 - one automatic repair round for AI answers.

When Gemini's answer is not valid JSON or does not match the prompt's schema, Understudy sends it back ONCE with the
exact validation errors (the old answer is quoted as data). Only if the second answer is also rejected does the step
fall back (learned skill, then the local heuristic). Network / quota errors are not retried, and an item-list answer
with a few bad items keeps the good ones without a repair round.
"""
import os
import tempfile
import unittest
from unittest import mock

from tests.wizard_helpers import FakeLLM, install_fake
from core.wizard import activity_log, admin_policy as ap, llm, recall

PID = "GOVERN_COMPLIANCE_REVIEW"
VARS = {"rules": [], "payload": {"a": 1}, "sop_text": "Amount must be less than 200."}
GOOD = {"warnings": [{"field": "a", "message": "looks fine"}], "security_flags": []}


def events(kind):
    return [e for e in activity_log.query(limit=5000)["events"] if e["kind"] == kind]


class Sequence:
    """Answers in order; the last answer repeats."""

    def __init__(self, *answers):
        self.answers = list(answers)

    def __call__(self, system, user, media):
        a = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(a, Exception):
            raise a
        return a


class RepairBase(unittest.TestCase):
    def setUp(self):
        ap.reset_to_defaults()
        activity_log.clear()
        self.tmp_log = tempfile.mktemp(suffix=".jsonl")
        os.environ["UNDERSTUDY_ACTIVITY_LOG"] = self.tmp_log
        self.p_recall = mock.patch.object(recall, "lookup", return_value=None)   # no learned skills in these tests
        self.p_recall.start()

    def tearDown(self):
        self.p_recall.stop()
        install_fake(None)
        os.environ.pop("UNDERSTUDY_ACTIVITY_LOG", None)

    def run_with(self, *answers, **kw):
        fake = FakeLLM({PID: Sequence(*answers)})
        install_fake(fake)
        return fake, llm.generate_json(PID, dict(VARS), untrusted=["payload", "sop_text"], **kw)


class TestRepairRound(RepairBase):
    def test_broken_json_is_repaired_on_the_second_answer(self):
        fake, res = self.run_with('{"warnings": [ {"field": "a", ', GOOD)
        self.assertTrue(res.ok)
        self.assertTrue(res.repaired)
        self.assertEqual(res.data["warnings"][0]["message"], "looks fine")
        self.assertEqual(len(fake.called(PID)), 2)
        second = fake.called(PID)[1]["user"]
        self.assertIn("not valid JSON", second)                        # the exact error
        self.assertIn('{"warnings": [ {"field": "a", ', second)        # its own answer, quoted
        self.assertIn('"required"', second)                            # the schema to match
        self.assertIn("Amount must be less than 200.", second)         # the original task is still there
        rep = events("AI_REPAIR")
        self.assertEqual(len(rep), 1)
        self.assertEqual(rep[0]["level"], "warn")
        self.assertIn("not valid JSON", rep[0]["data"]["validation_errors"][0])
        self.assertIn("after the repair round", events("AI_RESPONSE")[-1]["title"])
        self.assertFalse(events("AI_ERROR"))

    def test_schema_mismatch_is_repaired_with_the_exact_path(self):
        fake, res = self.run_with({"notes": "no warnings key"}, GOOD)
        self.assertTrue(res.ok and res.repaired)
        self.assertIn("warnings", fake.called(PID)[1]["user"])
        self.assertTrue(any("warnings" in e for e in events("AI_REPAIR")[0]["data"]["validation_errors"]))

    def test_good_first_answer_makes_one_call(self):
        fake, res = self.run_with(GOOD)
        self.assertTrue(res.ok)
        self.assertFalse(res.repaired)
        self.assertEqual(len(fake.called(PID)), 1)
        self.assertFalse(events("AI_REPAIR"))
        self.assertNotIn("repair", events("AI_RESPONSE")[-1]["title"])

    def test_falls_back_after_the_second_bad_answer(self):
        fake, res = self.run_with("not json at all", "still not json")
        self.assertFalse(res.ok)
        self.assertEqual(len(fake.called(PID)), 2)                     # exactly one retry, never more
        self.assertEqual(len(events("AI_REPAIR")), 1)
        err = events("AI_ERROR")[-1]
        self.assertIn("after the repair round", err["title"])
        self.assertTrue(res.validation_errors)

    def test_second_bad_answer_uses_a_learned_skill_when_one_fits(self):
        hit = {"exact": True, "score": 1.0, "threshold": 0.8, "label": "earlier review", "learned_at": "2026-10-01",
               "learned_with": "gemini", "memory_id": "m1", "detail": "", "data": GOOD, "step": "GOVERN"}
        with mock.patch.object(recall, "lookup", return_value=hit):
            fake, res = self.run_with("bad", "bad again")
        self.assertTrue(res.ok)
        self.assertEqual(len(fake.called(PID)), 2)
        self.assertIn("also after one repair round", events("RECALL")[-1]["data"]["ai_unavailable_reason"])

    def test_network_errors_are_not_retried(self):
        fake, res = self.run_with(RuntimeError("429 RESOURCE_EXHAUSTED: Quota exceeded"), GOOD)
        self.assertFalse(res.ok)
        self.assertEqual(len(fake.called(PID)), 1)
        self.assertFalse(events("AI_REPAIR"))
        self.assertIn("quota", res.error.lower())

    def test_error_on_the_repair_call_falls_back(self):
        fake, res = self.run_with("bad", RuntimeError("503 UNAVAILABLE"))
        self.assertFalse(res.ok)
        self.assertEqual(len(fake.called(PID)), 2)
        self.assertEqual(events("AI_ERROR")[-1]["data"]["attempt"], 2)

    def test_quoted_answer_is_truncated(self):
        fake, res = self.run_with("x" * 50000, GOOD)
        self.assertTrue(res.ok)
        self.assertLess(len(fake.called(PID)[1]["user"]), 50000)

    def test_api_key_never_reaches_the_repair_prompt_or_log(self):
        key = "AIzaSyFAKE_REPAIR_KEY_1234567890abcdefXYZ"
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": key}):
            fake, res = self.run_with("bad", GOOD)
            self.assertTrue(res.ok)
        self.assertNotIn(key, fake.called(PID)[1]["user"])
        with open(self.tmp_log, encoding="utf-8") as fh:
            self.assertNotIn(key, fh.read())


class TestItemListTolerance(RepairBase):
    PID = "GOVERN_EXTRACT_RULES"
    RVARS = {"parameter_keys": [{"key": "amount", "label": "Amount", "type": "number"}],
             "sop_text": "Amount must be less than 200."}

    def call(self, *answers):
        fake = FakeLLM({self.PID: Sequence(*answers)})
        install_fake(fake)
        return fake, llm.generate_json(self.PID, dict(self.RVARS), untrusted=["sop_text"], item_schema_wrapper="rules")

    def test_a_few_bad_items_are_dropped_without_a_repair_round(self):
        fake, res = self.call({"rules": [42, "nonsense"]})
        self.assertTrue(res.ok)
        self.assertEqual(res.data["rules"], [])
        self.assertEqual(len(fake.called(self.PID)), 1)
        self.assertFalse(events("AI_REPAIR"))

    def test_missing_list_is_repaired_and_shown_the_whole_shape(self):
        fake, res = self.call({"rule": "oops"}, {"rules": []})
        self.assertTrue(res.ok and res.repaired)
        second = fake.called(self.PID)[1]["user"]
        self.assertIn("$.rules: expected array", second)
        self.assertIn('"required":["rules"]', second)                 # the wrapper, not just one item's schema


class TestRepairPrompt(unittest.TestCase):
    def test_shape(self):
        p = llm.repair_prompt("TASK", '{"a":', ["$.warnings: required"], {"type": "object"})
        self.assertTrue(p.startswith("TASK"))
        self.assertIn("$.warnings: required", p)
        self.assertIn('{"a":', p)
        self.assertIn("Return ONLY the corrected JSON object", p)


if __name__ == "__main__":
    unittest.main()
