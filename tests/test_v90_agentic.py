"""9.0: Export-only Step 5, the agentic build / heal loop, the live-page probe (rpachallenge-style Portal 3), the
"how complex is this automation" summary, and per-tool user access for admin-approved tools."""
import os
import unittest
from unittest import mock

from tests.wizard_helpers import LiveServer, browser_available, govern  # noqa: F401  (sets the isolated test environment)

from core.wizard import admin_policy as ap, build as build_mod, build_agent, complexity, page_probe
from core.wizard.frameworks import CATALOG, FrameworkSpec

HAS_BROWSER = browser_available()


def spec():
    return FrameworkSpec.from_dict(CATALOG["playwright-python"].to_dict())


def ai_out(locators, code="page.locator(\"xpath=//label[normalize-space()='First Name']/following-sibling::input\")"):
    return {"files": {"run_automation.py": code}, "entry_file": "run_automation.py", "generator": "ai",
            "agent_source": "ACTUAL_AI_API", "model": "fake", "notes": [], "findings": [], "locators": locators}


PROFILE = {"renderer": "chromium", "loads": 2, "signals": {"dynamic_ids": True, "shuffled_fields": True}, "fields": []}
LABEL_LOC = {"id": "first", "strategy": "xpath", "value": "//label[normalize-space()='First Name']/following-sibling::input",
             "action": "fill", "expect_label": "First Name"}
ID_LOC = {"id": "first", "strategy": "css", "value": "#x1Ab", "action": "fill", "expect_label": "First Name"}


class TestBuildAgentLoop(unittest.TestCase):
    def test_a_locator_that_breaks_on_a_fresh_load_goes_back_to_the_ai(self):
        calls = []

        def gen(*a, **kw):
            calls.append(kw.get("feedback"))
            return ai_out([ID_LOC], code="page.locator('#x1Ab')") if len(calls) == 1 else ai_out([LABEL_LOC])

        def verify(url, locs, loads=1, **kw):
            ok = locs[0]["strategy"] == "xpath"
            return {"checked": 1, "loads": 2, "ok": ok, "results": [
                {"id": "first", "strategy": locs[0]["strategy"], "value": locs[0]["value"], "ok": ok,
                 "problems": [] if ok else ["load 2: matched 0 elements"]}]}
        with mock.patch.object(build_mod, "generate_suite", side_effect=gen), \
                mock.patch.object(page_probe, "verify_locators", side_effect=verify):
            out = build_agent.generate(spec(), [], [], None, "http://localhost:8000/portal3", [], profile=PROFILE)
        self.assertEqual(len(calls), 2)
        self.assertTrue(any("did not hold on a fresh page load" in f for f in calls[1]), calls[1])
        self.assertTrue(any("previous code" in f for f in calls[1]))
        self.assertEqual(out["agent"]["status"], "verified")
        self.assertEqual(out["agent"]["verified_locators"], 1)
        self.assertEqual(len(out["agent"]["rounds"]), 2)

    def test_gate_rejection_is_feedback_not_a_template(self):
        n = {"i": 0}

        def gen(*a, **kw):
            n["i"] += 1
            if n["i"] == 1:
                self.assertFalse(kw["allow_template"])
                raise build_mod.AttemptRejected(["hard-coded value 'Understudy'"], {"run_automation.py": "x"})
            return ai_out([LABEL_LOC])
        with mock.patch.object(build_mod, "generate_suite", side_effect=gen), \
                mock.patch.object(page_probe, "verify_locators",
                                  return_value={"checked": 1, "loads": 2, "ok": True, "results": [
                                      {"id": "first", "strategy": "xpath", "value": "v", "ok": True, "problems": []}]}):
            out = build_agent.generate(spec(), [], [], None, "http://x/", [], profile=PROFILE)
        self.assertEqual(out["generator"], "ai")
        self.assertEqual(out["agent"]["rounds"][0]["result"], "rejected by Understudy's gates")

    def test_locators_not_in_the_code_are_flagged(self):
        with mock.patch.object(build_mod, "generate_suite", return_value=ai_out([LABEL_LOC], code="print(1)")):
            out = build_agent.generate(spec(), [], [], None, "http://x/", [], profile=None, max_rounds=1)
        self.assertEqual(out["agent"]["status"], "unverified")
        self.assertIn("does not appear in your code", out["agent"]["unresolved"][0])

    def test_template_suites_skip_the_loop(self):
        t = dict(ai_out([]), generator="template")
        with mock.patch.object(build_mod, "generate_suite", return_value=t):
            out = build_agent.generate(spec(), [], [], None, "http://x/", [], profile=PROFILE)
        self.assertEqual(out["agent"]["status"], "not run")

    def test_failure_feedback_carries_the_evidence(self):
        fb = build_agent.failure_feedback({"failed_step": 3, "failure_reason": "Timeout waiting for #abc"},
                                          {"run_automation.py": "code"}, "STEP 3 FAILED", "id changed", ["use labels"])
        blob = "\n".join(fb)
        for x in ("step 3", "Timeout waiting for #abc", "id changed", "STEP 3 FAILED", "use labels", "code"):
            self.assertIn(x, blob)


class TestComplexity(unittest.TestCase):
    def test_baseline_scores_a_shifting_form_as_complex(self):
        b = complexity.baseline([{}] * 9, [{}] * 7, "Fill the form for each row of the Excel file", PROFILE)
        names = [f["name"] for f in b["factors"]]
        self.assertIn("Element ids change on every load", names)
        self.assertIn("Fields change position", names)
        self.assertIn("Repeated data rows", names)
        self.assertGreaterEqual(b["score"], 6)
        self.assertIn(b["level"], ("Complex", "Very complex"))
        self.assertTrue(all(f["how_handled"] for f in b["factors"]))

    def test_simple_page(self):
        b = complexity.baseline([{}] * 3, [{}] * 2, "", {"renderer": "chromium", "signals": {}})
        self.assertEqual((b["score"], b["level"]), (1, "Simple"))

    def test_ai_can_move_the_score_by_two_at_most(self):
        res = mock.Mock(ok=True, agent_source="ACTUAL_AI_API", model="fake",
                        data={"score": 1, "summary": "easy", "tips": ["t"], "tool_fit": [
                            {"tool_id": "playwright-python", "fit": "good", "why": "auto-waits"}],
                              "challenges": [{"name": "Fields change position", "how_handled": "labels"}]})
        s = {"analysis": {"steps": [{}] * 3}, "parameters": [], "sop_text": "",
             "framework_options": [{"id": "playwright-python", "name": "Playwright"}]}
        with mock.patch.object(complexity, "generate_json", return_value=res):
            out = complexity.assess(s, PROFILE)
        base = complexity.baseline([{}] * 3, [], "", PROFILE)["score"]
        self.assertEqual(out["score"], base - 2)
        self.assertEqual(out["tool_fit"][0]["fit"], "good")
        self.assertEqual(next(f for f in out["factors"] if f["name"] == "Fields change position")["how_handled"], "labels")

    def test_without_ai_the_rules_answer(self):
        res = mock.Mock(ok=False, data=None)
        s = {"analysis": {"steps": []}, "parameters": [], "sop_text": "",
             "framework_options": [{"id": "requests-python", "name": "Requests", "category": "api"}]}
        with mock.patch.object(complexity, "generate_json", return_value=res):
            out = complexity.assess(s, None)
        self.assertEqual(out["agent_source"], "LOCAL_HEURISTIC")
        self.assertEqual(out["tool_fit"][0]["fit"], "poor")
        self.assertFalse(out["probed"])


class TestToolAccess(unittest.TestCase):
    def setUp(self):
        ap.whitelist_catalog_tool("selenium-python")

    def test_selected_users_only(self):
        ap.set_tool_access("selenium-python", "selected", ["Alice", "bob"])
        self.assertTrue(ap.user_may_use("selenium-python", "alice"))
        self.assertFalse(ap.user_may_use("selenium-python", "carol"))
        self.assertTrue(ap.user_may_use("selenium-python", None))          # no accounts -> nothing to restrict
        from core.wizard.session import annotate_tools
        carol = {o["id"]: o for o in annotate_tools([dict(CATALOG["selenium-python"].to_dict(), recommended=True)], "carol")}
        self.assertTrue(carol["selenium-python"].get("restricted"))
        ap.set_tool_access("selenium-python", "all")
        self.assertTrue(ap.user_may_use("selenium-python", "carol"))

    def test_no_user(self):
        """9.9: 'No user' - the tool stays approved and installed, but nobody can pick or run it (not even without
        accounts); opening it again restores access."""
        ap.set_tool_access("selenium-python", "none", ["alice"])
        self.assertEqual(ap.tool_access("selenium-python")["mode"], "none")
        self.assertEqual(ap.tool_access("selenium-python")["users"], [])             # a user list is ignored
        self.assertTrue(ap.is_tool_allowed("selenium-python"))
        for who in ("alice", "carol", None, "local"):
            self.assertFalse(ap.user_may_use("selenium-python", who), who)
        from core.wizard.session import annotate_tools
        opts = {o["id"]: o for o in annotate_tools([dict(CATALOG["selenium-python"].to_dict(), recommended=True)], "carol")}
        self.assertTrue(opts["selenium-python"]["restricted"])
        self.assertEqual(opts["selenium-python"]["access_mode"], "none")
        self.assertIn("turned Selenium", ap.access_denied_message("Selenium", "selenium-python"))
        self.assertNotIn("selenium-python", [o["id"] for o in annotate_tools([], "carol")])   # not appended either
        ap.set_tool_access("selenium-python", "all")
        self.assertTrue(ap.user_may_use("selenium-python", "carol"))

    def test_builtin_access_cannot_be_changed(self):
        with self.assertRaises(ap.PolicyError):
            ap.set_tool_access("gemini-agent", "none")
        self.assertTrue(ap.user_may_use("gemini-agent", "carol"))

    def test_bad_mode_refused(self):
        with self.assertRaises(ap.PolicyError):
            ap.set_tool_access("selenium-python", "nobody")

    def test_selected_needs_a_user(self):
        with self.assertRaises(ap.PolicyError):
            ap.set_tool_access("selenium-python", "selected", [])

    def test_delete_removes_it_for_everyone(self):
        res = ap.delete_tool("selenium-python")
        self.assertEqual(res["tool_id"], "selenium-python")
        self.assertFalse(ap.is_tool_allowed("selenium-python"))
        self.assertFalse(ap.user_may_use("selenium-python", None))
        with self.assertRaises(ap.PolicyError):
            ap.set_tool_access("selenium-python", "all")


class TestExportOnly(unittest.TestCase):
    def test_pillars(self):
        from core.wizard import session as wz
        self.assertEqual(wz.PILLARS[-1], "EXPORT")
        self.assertNotIn("VERIFY", wz.PILLARS)
        self.assertFalse(hasattr(wz, "verify"))                     # 9.4.1: the pre-9.0 alias is gone


@unittest.skipUnless(HAS_BROWSER, "Playwright Chromium not available")
class TestPortal3Probe(unittest.TestCase):
    """The rpachallenge.com-style form: new random ids / names and a new field order on every load."""

    @classmethod
    def setUpClass(cls):
        cls.url = LiveServer.get().base + "/portal3"

    def test_probe_sees_the_page_change_and_label_locators_hold(self):
        prof = page_probe.probe(self.url)
        self.assertEqual(prof["renderer"], "chromium", prof.get("error"))
        sig = prof["signals"]
        self.assertTrue(sig["dynamic_ids"] or sig["dynamic_names"])
        self.assertGreaterEqual(sig["field_count"], 7)
        first = next(f for f in prof["fields"] if f["label"] == "First Name")
        self.assertEqual(first["recommended"]["strategy"], "xpath")
        self.assertIn("First Name", first["recommended"]["value"])
        res = page_probe.verify_locators(self.url, [
            dict(LABEL_LOC, value=first["recommended"]["value"]),
            {"id": "bad", "strategy": "css", "value": "input >> nth=0", "action": "fill", "expect_label": "First Name"}],
            profile=prof)                      # 9.4: one fresh load + what the probe saw change (fields move)
        by = {r["id"]: r for r in res["results"]}
        self.assertTrue(by["first"]["ok"], by["first"])
        self.assertFalse(by["bad"]["ok"])

    def test_offline_wizard_run_on_the_shifting_form(self):
        """No AI: the form reader keys fields by their random ids; the probe rebuilds the plan from the visible labels,
        and the generated Playwright suite fills the right values although every load moves and renames the fields."""
        import requests
        from tests.wizard_helpers import fake_mp4, install_fake
        install_fake(None)
        B = LiveServer.get().base
        api = lambda m, p, **k: requests.request(m, B + "/api/wizard" + p, timeout=300, **k)  # noqa: E731
        requests.post(B + "/api/erp/portal3/reset")
        sop = ("Fill the form with First Name Ada, Last Name Lovelace, Company Name Analytical Engines, Role in Company "
               "Engineer, Address 1 Main St, Email ada@example.com, Phone Number 5551234 and submit.")
        with mock.patch.dict(os.environ, {"UNDERSTUDY_PAGE_PROBE": "on"}), open(fake_mp4(), "rb") as fh:
            s = api("POST", "/sessions?wait=true", data={"target_url": self.url, "sop_text": sop},
                    files={"media": ("rec.mp4", fh, "video/mp4")}).json()["session"]
        sid = s["session_id"]
        self.assertIn(s["complexity"]["level"], ("Complex", "Very complex"))
        self.assertIn("Fields change position", [f["name"] for f in s["complexity"]["factors"]])
        self.assertEqual({p["key"]: p["value"] for p in s["parameters"]}["first_name"], "Ada")
        self.assertTrue(all(st.get("grounded_by") for st in s["analysis"]["steps"][1:]))
        s = api("POST", f"/sessions/{sid}/framework?wait=true", json={"framework_id": "playwright-python"}).json()["session"]
        if s["dependencies"]["status"] not in ("READY",):
            self.skipTest("Playwright dependencies not installable here: " + s["dependencies"]["status"])
        govern(sid)
        s = api("POST", f"/sessions/{sid}/build?wait=true").json()["session"]
        s = api("POST", f"/sessions/{sid}/execute?wait=true").json()["session"]
        self.assertEqual(s["pillars"]["BUILD-EXECUTE"]["status"], "PASS", s["execution"].get("failure_reason"))
        self.assertEqual(s["pillars"]["EXPORT"]["status"], "PASS")
        sub = requests.get(B + "/api/erp/portal3/submissions").json()["submissions"][-1]
        self.assertEqual((sub["First Name"], sub["Email"], sub["Role in Company"]), ("Ada", "ada@example.com", "Engineer"))


class TestPlanFromProfile(unittest.TestCase):
    def test_labels_become_keys_and_sop_values_are_used(self):
        prof = {"renderer": "chromium", "fields": [
            {"label": "First Name", "kind": "field", "tag": "input", "type": "text",
             "recommended": {"strategy": "xpath", "value": "//label[.='First Name']/following::input[1]", "why": "label"}},
            {"label": "Email", "kind": "field", "tag": "input", "type": "email",
             "recommended": {"strategy": "xpath", "value": "//label[.='Email']/following::input[1]", "why": "label"}},
            {"label": "Submit", "kind": "action", "tag": "input", "type": "submit", "recommended": None}]}
        plan = page_probe.plan_from_profile(prof, "First Name: Ada\nUse Email ada@example.com, then submit")
        self.assertEqual([p["key"] for p in plan["parameters"]], ["first_name", "email"])
        self.assertEqual([p["value"] for p in plan["parameters"]], ["Ada", "ada@example.com"])
        self.assertEqual(plan["parameters"][1]["type"], "email")
        self.assertEqual(plan["steps"][1]["selector"], "//label[.='First Name']/following::input[1]")
        self.assertIsNone(page_probe.plan_from_profile({"renderer": "none"}))


if __name__ == "__main__":
    unittest.main()
