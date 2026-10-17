"""9.9: starting without a typed page address.

* When the AI call that reads the addresses fails (key, quota, network), Watch says so - not "the address bar may not
  be visible".
* The addresses read from a recording are a learned skill: the same recording can start without an address even when
  AI is not available. Without AI and without that skill, Understudy asks for the address straight away.
"""
import os
import shutil
import tempfile
import unittest
from unittest import mock

from tests.wizard_helpers import FakeLLM, fake_mp4, install_fake
from tests.test_wizard_media_to_payload import TARGET, VIDEO_ANALYSIS, fixture_inventory
from core.wizard import activity_log, admin_policy as ap, recall, watch
from core.wizard import session as wz

QUOTA = RuntimeError("429 RESOURCE_EXHAUSTED: Quota exceeded for this key")


class Base(unittest.TestCase):
    def setUp(self):
        ap.reset_to_defaults()
        activity_log.clear()
        self.dir = tempfile.mkdtemp(prefix="understudy-recall-")
        self.env = mock.patch.dict(os.environ, {"UNDERSTUDY_SKILLS_DIR": self.dir, "UNDERSTUDY_RECALL": "on",
                                                "UNDERSTUDY_DEPENDENCY_INSTALL": "dry-run"})
        self.env.start()
        self.dom = mock.patch("core.wizard.watch.dom_discovery.discover", side_effect=fixture_inventory)
        self.dom.start()

    def tearDown(self):
        install_fake(None)
        self.dom.stop()
        self.env.stop()
        shutil.rmtree(self.dir, ignore_errors=True)


class TestRealReasonIsShown(Base):
    def test_a_failed_ai_call_is_reported_as_such(self):
        install_fake(FakeLLM({"WATCH_URLS": QUOTA}))
        s = wz.create_session("", "", fake_mp4(), "rec.mp4", background=False)
        w = s["pillars"]["WATCH"]
        self.assertEqual(w["status"], "FAIL")
        self.assertIn("quota", w["reason"].lower())
        self.assertNotIn("address bar may not be visible", w["reason"])
        self.assertTrue(w["evidence"]["needs_pages"])
        self.assertTrue(w["evidence"]["ai_error"])

    def test_no_address_in_the_video_keeps_the_old_message(self):
        install_fake(FakeLLM({"WATCH_URLS": {"pages": []}}))
        s = wz.create_session("", "", fake_mp4(), "rec.mp4", background=False)
        self.assertIn("address bar may not be visible", s["pillars"]["WATCH"]["reason"])


class TestLearnedPages(Base):
    def test_same_recording_starts_without_ai_and_without_an_address(self):
        media = fake_mp4(payload=b"rpa-challenge-recording")
        install_fake(FakeLLM({"WATCH_URLS": {"pages": [{"url": TARGET, "video_time": "00:02", "evidence": "address bar"}]},
                              "WATCH_ANALYZE": VIDEO_ANALYSIS, "BUILD_PARAMETERIZE": RuntimeError("n/a")}))
        first = wz.create_session("", "", media, "rec.mp4", background=False)
        self.assertEqual(first["target_url"], TARGET)
        self.assertTrue(any(m["prompt_id"] == "WATCH_URLS" for m in recall.list_memories()))

        install_fake(None)                                                 # AI gone (no key, restart, ...)
        self.assertTrue(watch.learned_pages(media))
        s = wz.create_session("", "", media, "rec.mp4", background=False)
        self.assertEqual(s["target_url"], TARGET)
        self.assertEqual(s["pages_meta"]["agent_source"], "LEARNED_SKILL")
        self.assertIsNotNone(s["analysis"])
        self.assertNotEqual((s.get("job") or {}).get("status"), "error")

    def test_another_recording_is_not_matched(self):
        install_fake(FakeLLM({"WATCH_URLS": {"pages": [{"url": TARGET}]}, "WATCH_ANALYZE": VIDEO_ANALYSIS,
                              "BUILD_PARAMETERIZE": RuntimeError("n/a")}))
        wz.create_session("", "", fake_mp4(payload=b"one"), "rec.mp4", background=False)
        install_fake(None)
        self.assertFalse(watch.learned_pages(fake_mp4(payload=b"two")))

    def test_without_ai_and_without_a_learned_skill_it_asks_at_once(self):
        install_fake(None)
        with self.assertRaises(wz.WizardError) as e:
            wz.create_session("", "", fake_mp4(payload=b"never seen"), "rec.mp4", background=False)
        self.assertEqual(e.exception.status_code, 400)
        self.assertEqual(e.exception.details["field"], "target_url")
        self.assertIn("hasn't been analysed with AI before", str(e.exception))

    def test_typed_address_without_ai_makes_no_page_call(self):
        install_fake(None)
        with mock.patch.object(watch, "pages_from_video") as pv:
            s = wz.create_session(TARGET, "", fake_mp4(), "rec.mp4", background=False)
        pv.assert_not_called()
        self.assertEqual(s["target_url"], TARGET)

    def test_a_check_does_not_count_as_a_use(self):
        media = fake_mp4(payload=b"hits")
        install_fake(FakeLLM({"WATCH_URLS": {"pages": [{"url": TARGET}]}, "WATCH_ANALYZE": VIDEO_ANALYSIS,
                              "BUILD_PARAMETERIZE": RuntimeError("n/a")}))
        wz.create_session("", "", media, "rec.mp4", background=False)
        install_fake(None)
        for _ in range(3):
            watch.learned_pages(media)
        hits = [m["hits"] for m in recall.list_memories() if m["prompt_id"] == "WATCH_URLS"]
        self.assertEqual(hits, [0])


if __name__ == "__main__":
    unittest.main()
