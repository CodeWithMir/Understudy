"""9.4: the AI reads the page addresses from the recording; the user checks, edits and can ask it to watch again;
Build waits until the pages are checked."""
import os
import unittest
from unittest import mock

from tests.wizard_helpers import FakeLLM, fake_mp4, install_fake, govern
from tests.test_wizard_media_to_payload import TARGET, VIDEO_ANALYSIS, fixture_inventory
from core.wizard import activity_log, admin_policy as ap, watch
from core.wizard import session as wz

SECOND = "http://localhost:8000/portal2"


def no_dns(url, *a, **k):
    from core.wizard.guardrails import screen_url
    return screen_url(url, *a, **dict(k, resolve=False))


class TestCompleteUrl(unittest.TestCase):
    def test_completion(self):
        self.assertEqual(watch.complete_url("erp.example.com/invoices/new", []), "https://erp.example.com/invoices/new")
        self.assertEqual(watch.complete_url("localhost:8000/portal1", []), "http://localhost:8000/portal1")
        self.assertEqual(watch.complete_url("/invoices/new", ["https://erp.example.com/home"]),
                         "https://erp.example.com/invoices/new")
        self.assertEqual(watch.complete_url("/invoices/new", []), "")              # a bare path needs a known site
        self.assertEqual(watch.complete_url("Invoices page", []), "")
        self.assertEqual(watch.complete_url("<https://a.example.com/x>.", []), "https://a.example.com/x")

    def test_merge_keeps_the_typed_start_page_first_and_the_recording_order(self):
        user = [{"url": "https://a.example/start", "source": "user"}, {"url": "https://a.example/typed", "source": "user"}]
        found = [{"url": "https://a.example/login"}, {"url": "https://a.example/start", "video_time": "00:05"},
                 {"url": "https://a.example/done"}]
        m = wz._merge_video_pages(user, found)
        self.assertEqual([p["url"] for p in m], ["https://a.example/start", "https://a.example/login",
                                                 "https://a.example/typed", "https://a.example/done"])
        self.assertEqual((m[0]["source"], m[0]["seen_in_video"], m[0]["video_time"]), ("user", True, "00:05"))
        self.assertEqual(m[1]["source"], "video")
        self.assertFalse(m[2]["seen_in_video"])

    def test_addresses_from_the_video_are_screened(self):
        res = mock.Mock(ok=True, agent_source="ACTUAL_AI_API", model="fake", data={"pages": [
            {"url": "localhost:8000/portal1", "video_time": "00:02", "evidence": "address bar", "purpose": "start"},
            {"url": "http://169.254.169.254/latest/meta-data/", "video_time": "00:09"},
            {"url": "/portal2", "video_time": "00:20", "evidence": "after clicking Next"},
            {"url": "localhost:8000/portal1", "video_time": "00:30"},
            {"url": "the invoices screen"}]})
        with mock.patch.object(watch, "generate_json", return_value=res), \
                mock.patch.object(watch, "screen_url", side_effect=no_dns):
            out = watch.pages_from_video("/tmp/rec.mp4", [])
        self.assertEqual([f["url"] for f in out["found"]], [TARGET, SECOND])
        self.assertEqual(out["found"][0]["video_time"], "00:02")
        reasons = [r["reason"] for r in out["rejected"]]
        self.assertEqual(len(reasons), 2)
        self.assertTrue(any("readable" in r for r in reasons))


class PagesBase(unittest.TestCase):
    def setUp(self):
        ap.reset_to_defaults()
        activity_log.clear()
        os.environ["UNDERSTUDY_DEPENDENCY_INSTALL"] = "dry-run"
        self.p = [mock.patch("core.wizard.watch.dom_discovery.discover", side_effect=fixture_inventory)]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()
        install_fake(None)
        os.environ.pop("UNDERSTUDY_DEPENDENCY_INSTALL", None)

    def fake(self, pages):
        f = FakeLLM({"WATCH_URLS": pages, "WATCH_ANALYZE": VIDEO_ANALYSIS, "BUILD_PARAMETERIZE": RuntimeError("n/a")})
        install_fake(f)
        return f


class TestPagesFromVideo(PagesBase):
    def test_no_url_typed_the_ai_finds_them_and_build_waits_for_the_check(self):
        fake = self.fake({"pages": [{"url": TARGET, "video_time": "00:01", "evidence": "address bar"},
                                    {"url": SECOND, "video_time": "00:40", "evidence": "after Save"}]})
        s = wz.create_session("", "", fake_mp4(), "rec.mp4", background=False)
        sid = s["session_id"]
        self.assertEqual(s["target_urls"], [TARGET, SECOND])
        self.assertEqual([p["source"] for p in s["pages"]], ["video", "video"])
        self.assertFalse(s["pages_confirmed"])
        self.assertIn("1. " + TARGET, fake.called("WATCH_ANALYZE")[0]["user"])     # analysed with the pages it found
        self.assertIsNotNone(fake.called("WATCH_URLS")[0]["media"])                  # read from the recording
        wz.select_framework(sid, "playwright-python", background=False)
        with self.assertRaises(wz.WizardError) as e:
            wz.build_suite(sid, background=False)
        self.assertEqual(e.exception.status_code, 409)
        self.assertTrue(e.exception.details["needs_page_check"])
        s = wz.confirm_pages(sid)
        self.assertTrue(s["pages_confirmed"])
        govern(sid)

    def test_the_user_edits_and_asks_the_ai_to_watch_again(self):
        fake = self.fake({"pages": [{"url": TARGET, "video_time": "00:01"}]})
        s = wz.create_session("", "", fake_mp4(), "rec.mp4", background=False)
        sid = s["session_id"]
        wz.select_framework(sid, "playwright-python", background=False)
        # edit only: no new AI watch, the pages + inspection are refreshed, the tool choice survives
        s = wz.set_pages(sid, [TARGET, SECOND], background=False)
        self.assertEqual(s["target_urls"], [TARGET, SECOND])
        self.assertTrue(s["pages_confirmed"])
        self.assertEqual(len(fake.called("WATCH_URLS")), 1)
        self.assertEqual(s["pillars"]["WATCH"]["status"], "PASS")
        # watch again, with a note: the AI gets the user's pages and the note
        s = wz.set_pages(sid, [TARGET, SECOND], rewatch=True, note="After Save the app opens the approvals page.",
                         background=False)
        calls = fake.called("WATCH_URLS")
        self.assertEqual(len(calls), 2)
        self.assertIn(SECOND, calls[-1]["user"])
        self.assertIn("approvals page", calls[-1]["user"])
        self.assertIn("approvals page", fake.called("WATCH_ANALYZE")[-1]["user"])
        self.assertEqual([p["source"] for p in s["pages"]], ["user", "user"])        # the user's checked list stays
        self.assertTrue(s["pages_confirmed"])
        self.assertEqual(s["framework"]["id"], "playwright-python")
        self.assertEqual(s["pillars"]["WATCH"]["status"], "PASS")

    def test_watch_again_respects_pages_the_user_removed(self):
        self.fake({"pages": [{"url": TARGET, "video_time": "00:01"}, {"url": SECOND, "video_time": "00:30"}]})
        sid = wz.create_session("", "", fake_mp4(), "rec.mp4", background=False)["session_id"]
        s = wz.set_pages(sid, [TARGET], rewatch=True, background=False)          # the user removed page 2
        self.assertEqual(s["target_urls"], [TARGET])
        self.assertTrue(s["pages_confirmed"])
        self.assertEqual([(r["url"], r["kind"]) for r in s["pages_rejected"]], [(SECOND, "removed")])
        s = wz.set_pages(sid, [TARGET, SECOND], rewatch=True, background=False)  # ... and added it back
        self.assertEqual(s["target_urls"], [TARGET, SECOND])
        self.assertEqual(s["pages_rejected"], [])

    def test_unsafe_edit_is_refused_with_its_index(self):
        self.fake({"pages": [{"url": TARGET}]})
        sid = wz.create_session("", "", fake_mp4(), "rec.mp4", background=False)["session_id"]
        with self.assertRaises(wz.WizardError) as e:
            wz.set_pages(sid, [TARGET, "http://169.254.169.254/x"], background=False)
        self.assertEqual((e.exception.status_code, e.exception.details["index"]), (400, 1))
        self.assertEqual(wz.get(sid)["target_urls"], [TARGET])

    def test_nothing_readable_asks_for_the_start_page_then_watches_again(self):
        self.fake({"pages": [], "notes": "the address bar is never visible"})
        s = wz.create_session("", "", fake_mp4(), "rec.mp4", background=False)
        sid = s["session_id"]
        self.assertIsNone(s["analysis"])
        self.assertTrue(s["job"]["details"]["needs_pages"])
        self.assertEqual(s["pillars"]["WATCH"]["status"], "FAIL")
        s = wz.set_pages(sid, [TARGET], background=False)          # no analysis yet: saving watches again
        self.assertIsNotNone(s["analysis"])
        self.assertEqual(s["target_url"], TARGET)

    def test_typed_pages_still_work_and_need_no_check(self):
        self.fake({"pages": [{"url": TARGET, "video_time": "00:01"}]})
        s = wz.create_session(TARGET, "", fake_mp4(), "rec.mp4", background=False)
        self.assertTrue(s["pages_confirmed"])
        self.assertEqual((s["pages"][0]["source"], s["pages"][0]["seen_in_video"]), ("user", True))

    def test_without_ai_a_start_page_is_required(self):
        install_fake(None)
        with mock.patch.object(wz, "ai_available", return_value=False):
            with self.assertRaises(wz.WizardError) as e:
                wz.create_session("", "", fake_mp4(), "rec.mp4", background=False)
        self.assertEqual(e.exception.details["field"], "target_url")


if __name__ == "__main__":
    unittest.main()
