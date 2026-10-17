"""Unit tests: failures trigger AUTOMATIC self-healing (no user prompt) up to the admin limit, repairs never swap the
user's tool, and what fixed a run is saved as a learned skill that is applied before the next run."""
import os
import unittest
from unittest import mock

from tests.wizard_helpers import FakeLLM, fake_mp4, install_fake, govern
from tests.test_wizard_media_to_payload import TARGET, VIDEO_ANALYSIS, fixture_inventory
from core.wizard import admin_policy as ap
from core.wizard import dom_discovery, verifier
from core.wizard import session as wz

PASS_EXEC = {"status": "PASS", "exit_code": 0, "last_ok_step": 6, "strategy": "framework", "duration_ms": 10.0}
FAIL_SELECTOR = {"status": "FAIL", "exit_code": 1, "last_ok_step": 1, "failed_step": 2, "strategy": "framework",
                 "failure_reason": "Element not found (selectors=['#vendor_name'], label='Vendor Name')", "duration_ms": 5.0}
FAIL_ENV = {"status": "FAIL", "exit_code": 1, "last_ok_step": 0, "strategy": "framework", "duration_ms": 5.0,
            "failure_reason": "selenium.common.exceptions.SessionNotCreatedException: chromedriver unavailable",
            "failure_category_hint": "ENVIRONMENT"}
VERIFY_PASS = {"status": "PASS", "method": "dom_text", "checks": [{"check": "'vendor_name' visible after run", "passed": True}],
               "visual_diff_ratio": 0.01, "summary": "All expected values appear.", "missing_keys": [],
               "explanation": "ok", "explanation_source": "LOCAL_HEURISTIC", "evidence": {}}
VERIFY_FAIL = dict(VERIFY_PASS, status="FAIL", summary="Missing or unchanged: 'vendor_name' visible after run",
                   checks=[{"check": "'vendor_name' visible after run", "passed": False}])

# The live page after a UI redesign: every id changed (selector drift).
DRIFTED_HTML = """<html><body><form>
<label for="supplier_org">Vendor Name (Supplier Organization)</label><input id="supplier_org" data-testid="supplier-input">
<label for="posting_period">Invoice Date (Posting Period)</label><input type="date" id="posting_period">
<label for="gross_amount">Total Amount ($ USD Gross)</label><input type="number" id="gross_amount">
<label for="voucher_remarks">Invoice Notes (Voucher Remarks)</label><input id="voucher_remarks">
<button type="submit" id="post-voucher">Post Voucher</button></form></body></html>"""


def drifted_inventory(url, render_if_empty=True):
    return dom_discovery.parse_html(url, DRIFTED_HTML)


class RetryLoopBase(unittest.TestCase):
    def setUp(self):
        import tempfile
        ap.reset_to_defaults()
        os.environ["UNDERSTUDY_SKILLS_DIR"] = tempfile.mkdtemp(prefix="skills-")
        os.environ["UNDERSTUDY_DEPENDENCY_INSTALL"] = "dry-run"
        self.p_discover = mock.patch("core.wizard.watch.dom_discovery.discover", side_effect=fixture_inventory)
        self.p_discover.start()
        install_fake(FakeLLM({"WATCH_ANALYZE": VIDEO_ANALYSIS, "BUILD_PARAMETERIZE": RuntimeError("n/a"),
                              "BUILD_GENERATE_SUITE": RuntimeError("n/a"), "GOVERN_EXTRACT_RULES": RuntimeError("n/a"),
                              "GOVERN_COMPLIANCE_REVIEW": RuntimeError("n/a"), "VERIFY_SELF_HEAL": RuntimeError("n/a")}))
        s = wz.create_session(TARGET, "Approved vendors: Globex Corp, Acme.", fake_mp4(), "rec.mp4", background=False)
        self.sid = s["session_id"]
        wz.select_framework(self.sid, "playwright-python", background=False)
        s = govern(self.sid)                       # 9.9: Govern (Step 2) before Build (Step 3)
        self.assertEqual(s["pillars"]["GOVERN"]["status"], "PASS", s.get("compliance"))
        s = wz.build_suite(self.sid, background=False)

    def tearDown(self):
        self.p_discover.stop()
        install_fake(None)
        os.environ.pop("UNDERSTUDY_DEPENDENCY_INSTALL", None)

    def run_execute(self, results):
        with mock.patch("core.wizard.executor.execute", side_effect=[dict(r) for r in results]) as ex:
            s = wz.execute(self.sid, background=False)
        return s, ex


class TestAutomaticSelfHealing(RetryLoopBase):
    def test_happy_path_needs_no_retry_and_unlocks_zip(self):
        self.run_execute([PASS_EXEC])
        s = wz.export(self.sid, background=False)
        self.assertEqual(s["retry"]["count"], 0)
        self.assertTrue(s["export"]["ready"])
        self.assertEqual(s["status"], "completed")

    def test_selector_drift_heals_automatically_without_asking(self):
        with mock.patch("core.wizard.verifier.dom_discovery.discover", side_effect=drifted_inventory), \
                mock.patch("core.wizard.executor.execute", side_effect=[dict(FAIL_SELECTOR), dict(PASS_EXEC)]) as ex, \
                mock.patch("core.wizard.session.governance.run_compliance_check",
                           wraps=wz.governance.run_compliance_check) as gov_check:
            s = wz.execute(self.sid, background=False)
        self.assertEqual(s["job"]["status"], "done", s["job"])
        self.assertFalse(s["retry"]["awaiting_decision"])            # nobody was asked
        heal = s["retry"]["last_heal"]
        self.assertEqual(heal["category"], "SELECTOR_DRIFT")
        remapped = {c["from"]: c["to"] for c in heal["changes"]}
        self.assertEqual(remapped["#vendor_name"], "#supplier_org")
        self.assertEqual(remapped["#submit-btn"], "#post-voucher")
        self.assertTrue(any("#supplier_org" in n for n in s["healing_notes"]))
        self.assertIn("#supplier_org", s["suite"]["files"]["run_automation.py"])
        gov_check.assert_called_once()                                 # GOVERN re-run before re-execution
        self.assertEqual(ex.call_count, 2)
        self.assertEqual(s["retry"]["count"], 1)
        self.assertEqual(s["retry"]["history"][0]["outcome"], "PASS")
        self.assertTrue(all(s["pillars"][p]["status"] == "PASS" for p in wz.PILLARS))
        self.assertTrue(s["export"]["ready"])

    def test_healing_notes_are_sent_to_the_ai_code_generator(self):
        from core.wizard import llm
        fake = llm._TEST_BACKEND
        with mock.patch("core.wizard.verifier.dom_discovery.discover", side_effect=drifted_inventory), \
                mock.patch("core.wizard.executor.execute", side_effect=[dict(FAIL_SELECTOR), dict(PASS_EXEC)]):
            wz.execute(self.sid, background=False)
        last_gen = fake.called("BUILD_GENERATE_SUITE")[-1]
        self.assertIn("#supplier_org", last_gen["user"])
        self.assertIn("HEALING NOTES", last_gen["user"])

    def test_a_passing_run_goes_straight_to_export(self):
        """9.0: Step 5 only packages the ZIP - the target page is not opened again and nothing is verified."""
        s, _ = self.run_execute([PASS_EXEC])
        self.assertIsNone(s.get("verification"))
        self.assertEqual(s["pillars"]["EXPORT"]["status"], "PASS")
        self.assertEqual(s["current_step"], 5)
        self.assertTrue(s["export"]["ready"])
        s = wz.export(self.sid, background=False)                  # re-create the ZIP
        self.assertTrue(s["export"]["ready"])

    def test_retries_stop_at_the_admin_limit(self):
        ap.set_max_self_heal_retries(3)
        with mock.patch("core.wizard.verifier.dom_discovery.discover", side_effect=fixture_inventory), \
                mock.patch("core.wizard.executor.execute", side_effect=[dict(FAIL_SELECTOR) for _ in range(10)]) as ex:
            s = wz.execute(self.sid, background=False)
        self.assertEqual(ex.call_count, 4)                               # first run + 3 automatic retries
        self.assertEqual(s["retry"]["count"], 3)
        # 8.2: at the limit the user can still correct the AI (no dead end) - but nothing runs by itself
        self.assertEqual(s["status"], "needs_decision")
        self.assertTrue(s["retry"]["awaiting_decision"])
        self.assertIn("Tell the AI", s["retry"]["prompt"])
        self.assertFalse(s["export"]["ready"])
        with self.assertRaises(wz.WizardError) as ctx:
            wz.export_zip(self.sid)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_default_limit_is_five_and_zero_is_not_allowed(self):
        self.assertEqual(ap.max_self_heal_retries(), 5)
        with self.assertRaises(ap.PolicyError):
            ap.set_max_self_heal_retries(0)
        with self.assertRaises(ap.PolicyError):
            ap.set_max_self_heal_retries(51)

    def test_a_new_run_starts_a_fresh_retry_budget(self):
        ap.set_max_self_heal_retries(1)
        with mock.patch("core.wizard.verifier.dom_discovery.discover", side_effect=fixture_inventory), \
                mock.patch("core.wizard.executor.execute", side_effect=[dict(FAIL_SELECTOR)] * 2):
            s = wz.execute(self.sid, background=False)
        self.assertEqual((s["retry"]["count"], s["status"]), (1, "needs_decision"))
        # the user fixes something and clicks "Run again": a new budget
        with mock.patch("core.wizard.verifier.dom_discovery.discover", side_effect=drifted_inventory), \
                mock.patch("core.wizard.executor.execute", side_effect=[dict(FAIL_SELECTOR), dict(PASS_EXEC)]):
            s = wz.execute(self.sid, background=False)
        self.assertEqual(s["retry"]["count"], 1)
        self.assertTrue(s["export"]["ready"])

    def test_user_can_stop_the_loop(self):
        ap.set_max_self_heal_retries(10)
        calls = {"n": 0}

        def fail_and_stop(*a, **k):
            calls["n"] += 1
            if calls["n"] == 2:                                           # user clicks Stop during retry 1
                wz.decline_retry(self.sid)
            return dict(FAIL_SELECTOR)
        with mock.patch("core.wizard.verifier.dom_discovery.discover", side_effect=fixture_inventory), \
                mock.patch("core.wizard.executor.execute", side_effect=fail_and_stop):
            s = wz.execute(self.sid, background=False)
        self.assertEqual(calls["n"], 2)
        self.assertEqual(s["status"], "stopped")
        self.assertFalse(s["export"]["ready"])

    def test_nothing_to_stop_when_idle(self):
        with self.assertRaises(wz.WizardError) as ctx:
            wz.decline_retry(self.sid)
        self.assertEqual(ctx.exception.status_code, 409)
        with self.assertRaises(wz.WizardError):
            wz.retry(self.sid, background=False)

    def test_ai_heal_that_changes_parameters_is_rejected(self):
        from core.wizard import llm
        llm._TEST_BACKEND.responses["VERIFY_SELF_HEAL"] = {
            "diagnosis": "I changed the vendor field to a new parameter", "failure_category": "SELECTOR_DRIFT",
            "healing_notes": ["x"],
            "steps": [{"order": 1, "action": "navigate", "description": "Open"},
                      {"order": 2, "action": "fill", "selector": "#supplier_org", "param_key": "brand_new_key", "description": "x"}]}
        with mock.patch("core.wizard.verifier.dom_discovery.discover", side_effect=drifted_inventory), \
                mock.patch("core.wizard.executor.execute", side_effect=[dict(FAIL_SELECTOR), dict(PASS_EXEC)]):
            s = wz.execute(self.sid, background=False)
        self.assertEqual(s["retry"]["last_heal"]["agent_source"], "LOCAL_HEURISTIC")
        keys = {st.get("param_key") for st in s["analysis"]["steps"] if st.get("param_key")}
        self.assertEqual(keys, {"vendor_name", "invoice_date", "total_amount", "invoice_notes"})


class TestRuntimeRepairIsAutomatic(RetryLoopBase):
    def _switch_to_selenium(self):
        wz.select_framework(self.sid, "selenium-python", background=False)
        s = govern(self.sid)
        self.assertEqual(s["pillars"]["GOVERN"]["status"], "PASS")
        s = wz.build_suite(self.sid, background=False)

    WIN_FAIL = {"status": "FAIL", "exit_code": 1, "last_ok_step": 0, "strategy": "framework", "duration_ms": 600.0,
                "failure_category_hint": "ENVIRONMENT",
                "failure_reason": "ValueError: The browser path is not a valid file: (the automation could not start)"}
    WIN_LOG = ("selenium.common.exceptions.NoSuchDriverException: Message: Unable to obtain driver for chrome; For "
               "documentation on this error, please visit: https://www.selenium.dev/documentation/webdriver/"
               "troubleshooting/errors/driver_location")

    def win_fail(self, *a, **k):
        a[7]("  " + self.WIN_LOG)
        return dict(self.WIN_FAIL)

    def test_environment_failure_repairs_the_same_tool(self):
        ready = {"status": "READY", "message": "reinstalled", "logs": []}
        with mock.patch("core.wizard.dependency_resolver.install", return_value=dict(ready)) as inst, \
                mock.patch("core.wizard.executor.execute", side_effect=[dict(FAIL_ENV), dict(PASS_EXEC)]) as ex:
            s = wz.execute(self.sid, background=False)
        self.assertEqual(s["retry"]["last_heal"]["category"], "ENVIRONMENT")
        self.assertIsNone(ex.call_args.kwargs["strategy_override"])
        self.assertEqual(s["framework"]["id"], "playwright-python")
        self.assertEqual(s["runtime_repairs"][0]["repair_id"], "reinstall")
        self.assertTrue(inst.call_args.kwargs["force"])
        self.assertTrue(s["export"]["ready"])

    def test_windows_selenium_failure_heals_selenium_and_learns_it(self):
        """The reported bug: Selenium can't find Chrome. Understudy points Selenium at the installed browser by itself,
        re-runs SELENIUM (not Playwright), and saves the fix as a learned skill that is applied before the next run."""
        self._switch_to_selenium()
        chrome = os.path.join(os.path.dirname(os.path.abspath(__file__)), "__init__.py")   # any existing file
        calls = {"n": 0}

        def first_fails(*a, **k):
            calls["n"] += 1
            return self.win_fail(*a, **k) if calls["n"] == 1 else dict(PASS_EXEC)
        with mock.patch("core.wizard.runtime_repair.detect_browsers", return_value={"chrome": chrome}), \
                mock.patch("core.wizard.executor.execute", side_effect=first_fails) as ex:
            s = wz.execute(self.sid, background=False)
        self.assertEqual(s["retry"]["last_heal"]["category"], "ENVIRONMENT")
        self.assertIsNone(ex.call_args.kwargs["strategy_override"])
        self.assertEqual(ex.call_args.kwargs["runtime_env"], {"SELENIUM_BROWSER": "chrome", "CHROME_BINARY": chrome})
        self.assertEqual(s["framework"]["id"], "selenium-python")
        self.assertTrue(s["export"]["ready"])
        self.assertTrue(s["learned_skills"])
        # ---- the next session with Selenium gets the fix BEFORE its first run: no failure at all
        install_fake(FakeLLM({"WATCH_ANALYZE": VIDEO_ANALYSIS, "BUILD_PARAMETERIZE": RuntimeError("n/a"),
                              "BUILD_GENERATE_SUITE": RuntimeError("n/a"), "GOVERN_EXTRACT_RULES": RuntimeError("n/a"),
                              "GOVERN_COMPLIANCE_REVIEW": RuntimeError("n/a"), "VERIFY_SELF_HEAL": RuntimeError("n/a")}))
        s2 = wz.create_session(TARGET, "Approved vendors: Globex Corp, Acme.", fake_mp4(), "rec.mp4", background=False)
        s2 = wz.select_framework(s2["session_id"], "selenium-python", background=False)
        self.assertEqual(s2["runtime_env"]["CHROME_BINARY"], chrome)
        self.assertTrue(s2["applied_skills"])
        self.assertEqual(s2["applied_skills"][0]["category"], "ENVIRONMENT")

    def test_ladder_then_user_decides_reference(self):
        self._switch_to_selenium()
        ap.set_max_self_heal_retries(10)
        found = {"chrome": "/c/chrome", "edge": "/c/msedge"}
        failed = {"status": "FAILED", "message": "no network", "logs": []}
        with mock.patch("core.wizard.runtime_repair.detect_browsers", return_value=found), \
                mock.patch("core.wizard.dependency_resolver.install", return_value=dict(failed)), \
                mock.patch("core.wizard.executor.execute", side_effect=self.win_fail) as ex:
            s = wz.execute(self.sid, background=False)
        # run 1, then chrome, edge, reinstall - all automatic - then nothing left: ask the user (only now)
        self.assertEqual(ex.call_count, 4)
        self.assertEqual([r["repair_id"] for r in s["runtime_repairs"]], ["browser:chrome", "browser:edge", "reinstall", None])
        self.assertTrue(s["retry"]["awaiting_decision"])
        self.assertTrue(s["retry"]["suggest_reference"])
        self.assertEqual(s["status"], "needs_decision")
        self.assertIsNone(s.get("strategy_override"))
        # user installs Chrome and retries with Selenium: re-run as-is, then the ladder starts over automatically
        with mock.patch("core.wizard.runtime_repair.detect_browsers", return_value=found), \
                mock.patch("core.wizard.dependency_resolver.install", return_value=dict(failed)), \
                mock.patch("core.wizard.executor.execute", side_effect=[self.win_fail(None, None, None, None, None, None, None, lambda l: None),
                                                                        dict(PASS_EXEC)]):
            s = wz.retry(self.sid, background=False)
        self.assertEqual([r["repair_id"] for r in s["runtime_repairs"]][-2:], ["rerun", "browser:chrome"])
        self.assertTrue(s["export"]["ready"])

    def test_limit_reached_on_runtime_failure_asks_instead_of_giving_up(self):
        self._switch_to_selenium()
        ap.set_max_self_heal_retries(2)
        found = {"chrome": "/c/chrome", "edge": "/c/msedge", "chromium": "/c/chromium"}
        with mock.patch("core.wizard.runtime_repair.detect_browsers", return_value=found), \
                mock.patch("core.wizard.executor.execute", side_effect=self.win_fail) as ex:
            s = wz.execute(self.sid, background=False)
        self.assertEqual(ex.call_count, 3)                                 # run + 2 automatic repairs
        self.assertTrue(s["retry"]["awaiting_decision"])
        self.assertTrue(s["retry"]["suggest_reference"])
        self.assertEqual(s["status"], "needs_decision")
        with mock.patch("core.wizard.executor.execute", side_effect=[dict(PASS_EXEC, strategy="reference")]):
            s = wz.retry(self.sid, background=False, mode="reference")
        self.assertTrue(s["export"]["ready"])

    def test_user_chooses_built_in_driver_after_exhaustion(self):
        self._switch_to_selenium()
        ap.set_max_self_heal_retries(10)
        with mock.patch("core.wizard.runtime_repair.detect_browsers", return_value={}), \
                mock.patch("core.wizard.dependency_resolver.install", return_value={"status": "FAILED", "logs": []}), \
                mock.patch("core.wizard.executor.execute", side_effect=self.win_fail):
            s = wz.execute(self.sid, background=False)
        self.assertTrue(s["retry"]["awaiting_decision"])
        with mock.patch("core.wizard.executor.execute", side_effect=[dict(PASS_EXEC, strategy="reference")]) as ex:
            s = wz.retry(self.sid, background=False, mode="reference")
        self.assertEqual(ex.call_args.kwargs["strategy_override"], "reference")
        self.assertEqual(s["retry"]["last_heal"]["agent_source"], "USER")
        self.assertTrue(s["export"]["ready"])
        self.assertEqual(s["framework"]["id"], "selenium-python")

    def test_toolchain_missing_is_not_swapped_for_playwright(self):
        from core.wizard import executor
        from core.wizard.frameworks import CATALOG, FrameworkSpec
        spec = FrameworkSpec.from_dict(CATALOG["cypress"].to_dict())
        logs = []
        res = executor.execute(spec, {"run.cy.js": ""}, [], [], TARGET, os.path.join(wz.WORK_ROOT, "tc-test"),
                               "TOOLCHAIN_MISSING", logs.append, dep_message="Cypress needs npx")
        self.assertEqual(res["status"], "FAIL")
        self.assertEqual(res["failure_category_hint"], "ENVIRONMENT")
        self.assertNotEqual(res.get("strategy"), "reference")

    def test_ai_environment_diagnosis_repairs_runtime_and_keeps_plan(self):
        from core.wizard import llm
        llm._TEST_BACKEND.responses["VERIFY_SELF_HEAL"] = {
            "diagnosis": "The automation failed due to a browser driver environment issue (NoSuchDriverException).",
            "failure_category": "ENVIRONMENT", "healing_notes": ["Chrome/driver missing on the server"],
            "steps": [{"order": 1, "action": "navigate", "description": "Open"}]}
        odd_fail = {"status": "FAIL", "exit_code": 1, "last_ok_step": 2, "failed_step": 3, "strategy": "framework",
                    "failure_reason": "Message: something unusual happened in the runtime", "duration_ms": 5.0}
        steps_before = [dict(st) for st in wz.get(self.sid)["analysis"]["steps"]]
        with mock.patch("core.wizard.dependency_resolver.install", return_value={"status": "READY", "logs": []}), \
                mock.patch("core.wizard.executor.execute", side_effect=[dict(odd_fail), dict(PASS_EXEC)]) as ex:
            s = wz.execute(self.sid, background=False)
        heal = s["retry"]["last_heal"]
        self.assertEqual((heal["category"], heal["agent_source"]), ("ENVIRONMENT", "ACTUAL_AI_API"))
        self.assertIsNone(ex.call_args.kwargs["strategy_override"])
        self.assertEqual(s["analysis"]["steps"], steps_before)
        self.assertTrue(s["export"]["ready"])


class TestLearnedSkills(RetryLoopBase):
    def test_selector_fix_is_learned_written_as_skill_md_and_applied_next_time(self):
        from core.wizard import skills
        with mock.patch("core.wizard.verifier.dom_discovery.discover", side_effect=drifted_inventory), \
                mock.patch("core.wizard.executor.execute", side_effect=[dict(FAIL_SELECTOR), dict(PASS_EXEC)]):
            s = wz.execute(self.sid, background=False)
        sid = skills.skill_id("playwright-python", "localhost")
        self.assertIn(sid, s["learned_skills"])
        md = skills.read_skill(sid)["markdown"]
        self.assertTrue(md.startswith("---\nname: playwright-python--localhost"))
        self.assertIn("`#vendor_name` changed to `#supplier_org`", md)
        self.assertNotIn("Globex", md)                                 # payload values never go into skills
        # the ZIP ships the skill
        import io, zipfile
        z = zipfile.ZipFile(io.BytesIO(wz.export_zip(self.sid)["data"]))
        self.assertIn(f"skills/{sid}/SKILL.md", z.namelist())
        # next session on the same target: selectors are fixed before the first run, old one kept as fallback
        s2 = wz.create_session(TARGET, "Approved vendors: Globex Corp, Acme.", fake_mp4(), "rec.mp4", background=False)
        s2 = wz.select_framework(s2["session_id"], "playwright-python", background=False)
        vendor = next(st for st in s2["analysis"]["steps"] if st.get("param_key") == "vendor_name")
        self.assertEqual(vendor["selector"], "#supplier_org")
        self.assertIn("#vendor_name", vendor["fallback_selectors"])
        self.assertTrue(any(a["category"] == "SELECTOR_DRIFT" for a in s2["applied_skills"]))

    def test_unresolved_errors_are_recorded(self):
        from core.wizard import skills
        ap.set_max_self_heal_retries(1)
        with mock.patch("core.wizard.verifier.dom_discovery.discover", side_effect=fixture_inventory), \
                mock.patch("core.wizard.executor.execute", side_effect=[dict(FAIL_SELECTOR)] * 2):
            wz.execute(self.sid, background=False)
        data = skills._load(skills.skill_id("playwright-python", "localhost"))
        lesson = data["lessons"][0]
        self.assertEqual(lesson["status"], "unresolved")
        self.assertEqual(lesson["category"], "SELECTOR_DRIFT")
        self.assertGreaterEqual(lesson["occurrences"], 2)               # same signature -> same lesson

    def test_signature_normalises_paths_numbers_and_keys(self):
        from core.wizard import skills
        a = skills.signature("ENVIRONMENT", r"The browser path is not a valid file: C:\Program Files\x\chrome.exe (pid 1234)")
        b = skills.signature("ENVIRONMENT", "The browser path is not a valid file: /opt/google/chrome/chrome (pid 99)")
        self.assertEqual(a, b)
        os.environ["GEMINI_API_KEY"] = "AIzaSyTESTKEYTESTKEYTESTKEY1234"
        try:
            self.assertNotIn("AIzaSyTESTKEY", skills.signature("X", "bad key AIzaSyTESTKEYTESTKEYTESTKEY1234"))
        finally:
            os.environ.pop("GEMINI_API_KEY", None)


class TestNoPageChecks(RetryLoopBase):
    """9.0: no before / after snapshots - a failed run is healed and run again, the run's own exit code decides."""

    def test_failed_run_is_healed_and_rerun_without_snapshots(self):
        crashed = {"status": "FAIL", "exit_code": 1, "last_ok_step": 6, "failed_step": 7, "strategy": "framework",
                   "failure_reason": "TimeoutError: waiting for the confirmation banner", "duration_ms": 9.0}
        with mock.patch("core.wizard.executor.execute", side_effect=[dict(crashed), dict(PASS_EXEC)]) as ex:
            s = wz.execute(self.sid, background=False)
        self.assertEqual(ex.call_count, 2)
        self.assertIsNone(s.get("verification"))
        self.assertTrue(s["export"]["ready"])


class TestHealerUnits(unittest.TestCase):
    def test_classify_failure(self):
        self.assertEqual(verifier.classify_failure(FAIL_ENV, None, ""), "ENVIRONMENT")
        self.assertEqual(verifier.classify_failure(FAIL_SELECTOR, None, ""), "SELECTOR_DRIFT")
        self.assertEqual(verifier.classify_failure(PASS_EXEC, VERIFY_FAIL, ""), "TIMING")
        self.assertEqual(verifier.classify_failure({"status": "FAIL", "failed_step": 1, "failure_reason": "net::ERR_CONNECTION_REFUSED"},
                                                   None, ""), "NAVIGATION")
        self.assertEqual(verifier.classify_failure({"status": "FAIL", "failure_reason": "Cannot find module 'playwright'"},
                                                   None, "Error: Cannot find module 'playwright'"), "ENVIRONMENT")

    def test_classify_windows_selenium_logs(self):
        tail = ("selenium.common.exceptions.NoSuchDriverException: Message: Unable to obtain driver for chrome; "
                "please visit: https://www.selenium.dev/documentation/webdriver/troubleshooting/errors/driver_location")
        self.assertEqual(verifier.classify_failure({"status": "FAIL", "failed_step": 3, "failure_reason": "x"}, None, tail),
                         "ENVIRONMENT")

    def test_crash_before_first_step_is_flagged_as_environment(self):
        import sys
        import tempfile
        from core.wizard import dependency_resolver, executor
        from core.wizard.frameworks import CATALOG, FrameworkSpec
        d = tempfile.mkdtemp()
        spec = FrameworkSpec.from_dict(CATALOG["selenium-python"].to_dict())
        files = {"run_automation.py": "raise ValueError('The browser path is not a valid file')\n"}
        executor.write_run_dir(d, files, [])
        with mock.patch.object(dependency_resolver, "runtime_for",
                               return_value=([sys.executable, "run_automation.py"], dict(os.environ))):
            res = executor.run_framework(spec, d, "http://localhost:8000/portal1", [], lambda line: None)
        self.assertEqual(res["status"], "FAIL")
        self.assertEqual(res["failure_category_hint"], "ENVIRONMENT")
        self.assertIn("browser path is not a valid file", res["failure_reason"])

    def test_heal_real_portal1_plan_against_real_portal2_page(self):
        """Uses the mock ERP's real Portal 2 HTML (the 'drifted DOM v2.5' variant of Portal 1)."""
        from fastapi.testclient import TestClient
        import server
        html = TestClient(server.app).get("/portal2").text
        inv = dom_discovery.parse_html("http://localhost:8000/portal2", html)
        steps = [s for s in VIDEO_ANALYSIS["steps"]]
        steps = [dict(s, fallback_selectors=[]) for s in steps]
        healed, changes, notes = verifier.heuristic_heal(steps, inv, "SELECTOR_DRIFT")
        by_key = {s.get("param_key") or s["action"]: s.get("selector") for s in healed}
        self.assertEqual(by_key["vendor_name"], "#company_title")
        self.assertEqual(by_key["invoice_date"], "#posting_date")
        self.assertEqual(by_key["total_amount"], "#payable_amount")
        self.assertEqual(by_key["invoice_notes"], "#invoice_notes")     # unchanged id - kept
        self.assertEqual(by_key["click"], "#post-voucher-button")
        self.assertTrue(notes)

class TestUnfillableStep(unittest.TestCase):
    """Found by scripts/benchmark.py: a plan that 'fills' a hidden file-upload control can never pass by waiting."""

    def _inv(self):
        from core.wizard import dom_discovery
        return dom_discovery.parse_html("http://x/portal2", '<input type="file" id="up" class="hidden">'
                                        '<input id="vendor" name="vendor"><button id="save">Save</button>')

    def test_optional_empty_file_step_removed(self):
        from core.wizard.verifier import drop_unfillable_step
        steps = [{"order": 1, "action": "navigate"}, {"order": 2, "action": "fill", "selector": "#up", "param_key": "up"},
                 {"order": 3, "action": "fill", "selector": "#vendor", "param_key": "vendor"},
                 {"order": 4, "action": "click", "selector": "#save"}]
        params = [{"key": "up", "value": "", "required": False}, {"key": "vendor", "value": "Acme", "required": True}]
        out = drop_unfillable_step(steps, params, {"failed_step": 2},
                                   'STEP 2 FAILED: Locator.fill: Error: Input of type "file" cannot be filled', self._inv())
        self.assertIsNotNone(out)
        new_steps, change = out
        self.assertEqual([s["selector"] for s in new_steps if s.get("selector")], ["#vendor", "#save"])
        self.assertEqual([s["order"] for s in new_steps], [1, 2, 3])
        self.assertIn("file-upload", change["why"])

    def test_required_value_is_not_dropped(self):
        from core.wizard.verifier import drop_unfillable_step
        steps = [{"order": 1, "action": "fill", "selector": "#vendor", "param_key": "vendor"}]
        params = [{"key": "vendor", "value": "Acme", "required": True}]
        self.assertIsNone(drop_unfillable_step(steps, params, {"failed_step": 1},
                                               "Element is not editable", self._inv()))

    def test_heuristic_watch_skips_file_inputs(self):
        from core.wizard import watch
        a = watch._heuristic_analysis("http://x/portal2", self._inv(), "")
        self.assertNotIn("#up", [s.get("selector") for s in a["steps"]])
