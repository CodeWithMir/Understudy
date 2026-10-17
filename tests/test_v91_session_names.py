"""9.1: sessions are named by the AI after Step 1 (like a chat title) - the user never has to type a name, and can
rename a session any time (a name the user gave is never overwritten)."""
import unittest
from unittest import mock

import tests.wizard_helpers  # noqa: F401  (isolated test environment)
from core.wizard import projects, session as wz


def analysed(s, process="Enter supplier invoice"):
    s["analysis"] = {"process_name": process, "summary": "Fills the AP invoice form and submits it.",
                     "steps": [{"order": 1, "action": "navigate"}, {"order": 2, "action": "fill", "label": "Vendor"}]}
    s["target_url"] = "http://localhost:8000/portal1"
    return s


def ai(title):
    return mock.Mock(ok=True, data={"title": title}, agent_source="ACTUAL_AI_API", model="fake")


class TestAutoName(unittest.TestCase):
    def setUp(self):
        import uuid
        self.prj = projects.create_project("namer", "Invoices " + uuid.uuid4().hex[:6], "AP work")["project_id"]

    def new(self, name=None):
        return wz.new_session("namer", self.prj, name)

    def test_no_name_needed_and_placeholders_are_unique(self):
        a, b = self.new(), self.new()
        self.assertEqual(a["session_name"], "New session")
        self.assertEqual(b["session_name"], "New session (2)")
        self.assertTrue(a["name_auto"])

    def test_ai_names_the_session_after_watch(self):
        s = analysed(self.new())
        with mock.patch.object(wz, "generate_json", return_value=ai('"Supplier Invoice Entry - AP Portal."')) as g:
            wz._auto_name(s)
        self.assertEqual(s["session_name"], "Supplier Invoice Entry - AP Portal")
        self.assertEqual(s["name_source"], "ACTUAL_AI_API")
        self.assertEqual(g.call_args[0][0], "SESSION_TITLE")
        row = next(r for r in projects.sessions_of("namer", self.prj) if r["session_id"] == s["session_id"])
        self.assertEqual(row["name"], "Supplier Invoice Entry - AP Portal")

    def test_same_title_twice_gets_a_suffix(self):
        s1, s2 = analysed(self.new()), analysed(self.new())
        with mock.patch.object(wz, "generate_json", return_value=ai("RPA Challenge Form Fill")):
            wz._auto_name(s1)
            wz._auto_name(s2)
        self.assertEqual((s1["session_name"], s2["session_name"]), ("RPA Challenge Form Fill", "RPA Challenge Form Fill (2)"))

    def test_without_ai_the_process_name_is_used(self):
        s = analysed(self.new(), process="enter supplier invoice")
        with mock.patch.object(wz, "generate_json", return_value=mock.Mock(ok=False, data=None)):
            wz._auto_name(s)
        self.assertEqual((s["session_name"], s["name_source"]), ("Enter supplier invoice", "LOCAL_HEURISTIC"))

    def test_a_name_the_user_gave_is_kept(self):
        s = analysed(self.new("My own name"))
        self.assertFalse(s["name_auto"])
        with mock.patch.object(wz, "generate_json") as g:
            wz._auto_name(s)
        g.assert_not_called()
        self.assertEqual(s["session_name"], "My own name")

    def test_rename_any_time_and_ai_never_renames_again(self):
        s = analysed(self.new())
        wz.rename_session(s["session_id"], "  Q3   invoices  ")
        self.assertEqual((s["session_name"], s["name_auto"], s["name_source"]), ("Q3 invoices", False, "USER"))
        with mock.patch.object(wz, "generate_json") as g:
            wz._auto_name(s)
        g.assert_not_called()
        other = self.new()
        with self.assertRaises(wz.WizardError) as e:
            wz.rename_session(other["session_id"], "q3 INVOICES")
        self.assertEqual(e.exception.status_code, 409)
        with self.assertRaises(wz.WizardError):
            wz.rename_session(other["session_id"], "   ")

    def test_rename_api(self):
        from fastapi.testclient import TestClient
        import server
        s = self.new()
        c = TestClient(server.app)
        r = c.put(f"/api/wizard/sessions/{s['session_id']}/name", json={"name": "Renamed From The UI"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["session"]["session_name"], "Renamed From The UI")
        self.assertFalse(r.json()["session"]["name_auto"])


if __name__ == "__main__":
    unittest.main()
