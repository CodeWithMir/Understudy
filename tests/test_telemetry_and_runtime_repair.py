"""v5.2 tests:

* Mission Control telemetry records user actions, the exact prompt sent to the AI, the raw response, AI errors
  (with a friendly reason), and every local-heuristic fallback with what it produced. Keys are never recorded.
* AI output validation is tolerant (drops invalid optional fields / items instead of rejecting the whole answer).
* A failed Watch shows the REAL reason (e.g. invalid key) instead of "No Gemini API key is configured".
* Runtime repair: Windows Selenium diagnosis, repair ladder, secret-free but complete child environment,
  Windows .cmd launcher resolution.
* Tool versions: registry lists, admin allowed versions + default, pinning applied to the user's tool.
"""
import os
import tempfile
import unittest
from unittest import mock

from tests.wizard_helpers import FakeLLM, fake_mp4, install_fake
from tests.test_wizard_media_to_payload import TARGET, VIDEO_ANALYSIS, fixture_inventory
from core.wizard import activity_log, admin_policy as ap, dependency_resolver, llm, runtime_repair, tool_versions
from core.wizard import session as wz
from core.wizard.frameworks import CATALOG

FAKE_KEY = "AIzaSyFAKE_TEST_KEY_1234567890abcdefXYZ"
INVALID_KEY_ERR = RuntimeError("400 API key not valid. Please pass a valid API key. [reason: \"API_KEY_INVALID\" "
                               "domain: \"googleapis.com\"]")


def events(kind=None, session_id=None):
    return [e for e in activity_log.query(limit=5000)["events"]
            if (kind is None or e["kind"] == kind) and (session_id is None or e["session_id"] == session_id)]


class TelemetryBase(unittest.TestCase):
    def setUp(self):
        ap.reset_to_defaults()
        activity_log.clear()
        self.tmp_log = tempfile.mktemp(suffix=".jsonl")
        os.environ["UNDERSTUDY_ACTIVITY_LOG"] = self.tmp_log
        os.environ["UNDERSTUDY_DEPENDENCY_INSTALL"] = "dry-run"
        self.p_discover = mock.patch("core.wizard.watch.dom_discovery.discover", side_effect=fixture_inventory)
        self.p_discover.start()

    def tearDown(self):
        self.p_discover.stop()
        install_fake(None)
        os.environ.pop("UNDERSTUDY_ACTIVITY_LOG", None)
        os.environ.pop("UNDERSTUDY_DEPENDENCY_INSTALL", None)
        os.environ.pop("GEMINI_API_KEY", None)


class TestAiTelemetry(TelemetryBase):
    def test_request_and_response_are_recorded_with_full_prompt(self):
        install_fake(FakeLLM({"GOVERN_COMPLIANCE_REVIEW": {"warnings": [{"field": "a", "message": "w1"}], "security_flags": []}}))
        res = llm.generate_json("GOVERN_COMPLIANCE_REVIEW", {"rules": [], "payload": {"a": 1}, "sop_text": "Pay Acme only."},
                                untrusted=["payload", "sop_text"])
        self.assertTrue(res.ok)
        req = events("AI_REQUEST")[-1]
        self.assertEqual(req["data"]["prompt_id"], "GOVERN_COMPLIANCE_REVIEW")
        self.assertIn("Pay Acme only.", req["data"]["user_prompt"])
        self.assertTrue(req["data"]["system_prompt"])
        resp = events("AI_RESPONSE")[-1]
        self.assertIn("w1", resp["data"]["raw_response"])
        self.assertEqual(resp["data"]["parsed"]["warnings"][0]["message"], "w1")
        self.assertIsInstance(resp["data"]["duration_ms"], float)

    @mock.patch.dict(os.environ, {"GEMINI_MODEL": "gemini-flash-latest"})
    def test_api_key_is_never_recorded(self):
        os.environ["GEMINI_API_KEY"] = FAKE_KEY
        install_fake(FakeLLM({"GOVERN_COMPLIANCE_REVIEW": RuntimeError(f"bad request for key={FAKE_KEY}")}))
        llm.generate_json("GOVERN_COMPLIANCE_REVIEW", {"rules": [], "payload": {}, "sop_text": f"token {FAKE_KEY}"},
                          untrusted=["sop_text"])
        with open(self.tmp_log, encoding="utf-8") as fh:
            on_disk = fh.read()
        self.assertNotIn(FAKE_KEY, on_disk)
        self.assertNotIn(FAKE_KEY, str(activity_log.query(limit=5000)))
        self.assertIn("REDACTED", on_disk)
        self.assertIn("...", events("AI_REQUEST")[-1]["data"]["api_key"])     # masked, e.g. AIza...XYZ

    def test_error_is_mapped_to_a_friendly_reason(self):
        install_fake(FakeLLM({"GOVERN_COMPLIANCE_REVIEW": INVALID_KEY_ERR}))
        res = llm.generate_json("GOVERN_COMPLIANCE_REVIEW", {"rules": [], "payload": {}, "sop_text": ""})
        self.assertFalse(res.ok)
        self.assertIn("API_KEY_INVALID", res.error)
        self.assertIn("Mission Control", res.error)
        err = events("AI_ERROR")[-1]
        self.assertIn("rejected by Google", err["data"]["friendly"])
        self.assertEqual(err["level"], "error")

    def test_friendly_error_table(self):
        f = llm.friendly_ai_error
        self.assertIn("quota", f("429 RESOURCE_EXHAUSTED: Quota exceeded").lower())
        self.assertIn("not available for this key", f("404 models/gemini-9 is not found for API version v1beta, model"))
        self.assertIn("PERMISSION_DENIED", f("403 PERMISSION_DENIED"))
        self.assertIn("timeout", f("DeadlineExceeded: 504 Deadline Exceeded").lower())

    def test_unavailable_reason_explains_missing_key(self):
        install_fake(None)
        self.assertIn("No Gemini API key", llm.unavailable_reason())
        res = llm.generate_json("GOVERN_COMPLIANCE_REVIEW", {"rules": [], "payload": {}, "sop_text": ""})
        self.assertFalse(res.ok)
        self.assertIn("Mission Control", res.error)


class TestWatchFallbackShowsRealReason(TelemetryBase):
    def test_invalid_key_is_reported_not_no_key(self):
        install_fake(FakeLLM({"WATCH_ANALYZE": INVALID_KEY_ERR, "BUILD_PARAMETERIZE": RuntimeError("n/a")}))
        s = wz.create_session(TARGET, "Approved vendors: Acme.", fake_mp4(), "rec.mp4", background=False)
        meta = s["analysis_meta"]
        self.assertEqual(meta["agent_source"], "LOCAL_HEURISTIC")
        self.assertTrue(meta["ai_configured"])
        self.assertIn("API_KEY_INVALID", meta["ai_error"])
        self.assertNotIn("No Gemini API key", s["analysis"]["summary"])
        self.assertIn("rejected by Google", s["analysis"]["summary"])
        fb = [e for e in events("FALLBACK", s["session_id"]) if e["data"]["prompt_id"] == "WATCH_ANALYZE"]
        self.assertEqual(len(fb), 1)
        self.assertTrue(fb[0]["data"]["heuristic_output"]["steps"])          # what the heuristic produced
        self.assertIn("API_KEY_INVALID", fb[0]["data"]["reason"])

    def test_user_actions_are_recorded(self):
        install_fake(FakeLLM({"WATCH_ANALYZE": VIDEO_ANALYSIS, "BUILD_PARAMETERIZE": RuntimeError("n/a")}))
        s = wz.create_session(TARGET, "Approved vendors: Globex Corp, Acme.", fake_mp4(), "rec.mp4", background=False)
        sid = s["session_id"]
        wz.select_framework(sid, "playwright-python", background=False)
        s = wz.get(sid)
        params = [dict(p) for p in s["parameters"]]
        wz.update_parameters(sid, params)
        titles = [e["title"] for e in events("USER_ACTION", sid)]
        self.assertTrue(any("started a new Understudy session" in t for t in titles), titles)
        self.assertTrue(any("picked the tool Playwright (Python)" in t for t in titles), titles)
        self.assertTrue(any("edited the payload table" in t for t in titles), titles)
        # AI calls made in the session's jobs are tagged with its session id
        self.assertTrue(any(e["data"].get("prompt_id") == "WATCH_ANALYZE" for e in events("AI_REQUEST", sid)))
        # secret values never appear in USER_ACTION payload logs
        for e in events("USER_ACTION", sid):
            for p in e["data"].get("parameters") or []:
                if p["type"] == "secret":
                    self.assertEqual(p["value"], "***")


class TestTolerantValidation(unittest.TestCase):
    SCHEMA = {"type": "object", "required": ["steps"],
              "properties": {"summary": {"type": "string"}, "confidence": {"type": "number"},
                             "app_type": {"enum": ["web_ui", "api"]},
                             "steps": {"type": "array", "items": {"type": "object", "required": ["action"],
                                                                  "properties": {"action": {"enum": ["fill", "click"]},
                                                                                 "order": {"type": "integer"}}}}}}

    def test_invalid_optional_fields_and_items_are_dropped(self):
        dropped = []
        data = {"summary": 42, "confidence": "0.9", "app_type": "desktop",
                "steps": [{"action": "fill", "order": "1"}, {"action": "hover"}, {"action": "CLICK"}, None]}
        out = llm.sanitize_against_schema(data, self.SCHEMA, dropped)
        self.assertEqual(out["summary"], "42")                      # number -> string coercion
        self.assertEqual(out["confidence"], 0.9)                    # numeric string -> number
        self.assertNotIn("app_type", out)                           # invalid optional enum dropped
        self.assertEqual(out["steps"], [{"action": "fill", "order": 1}, {"action": "click"}])
        self.assertTrue(any("app_type" in d for d in dropped))
        self.assertTrue(any("steps[1]" in d for d in dropped))

    def test_missing_required_field_still_rejects(self):
        self.assertIs(llm.sanitize_against_schema({"summary": "x"}, self.SCHEMA, []), llm._INVALID)

    def test_generate_json_accepts_partially_invalid_watch_output(self):
        bad = dict(VIDEO_ANALYSIS)
        bad["framework_options"] = [{"id": 123}]             # junk the old strict validator rejected wholesale
        bad["app_type"] = "Web UI"
        install_fake(FakeLLM({"WATCH_ANALYZE": bad}))
        try:
            res = llm.generate_json("WATCH_ANALYZE", {"target_url": TARGET, "dom_inventory": "", "sop_text": "",
                                                      "catalog_hint": ""}, untrusted=["dom_inventory", "sop_text"])
        finally:
            install_fake(None)
        self.assertTrue(res.ok, res.error)
        self.assertEqual(len(res.data["steps"]), len(VIDEO_ANALYSIS["steps"]))


class TestAdminTelemetryEndpoint(TelemetryBase):
    def test_endpoint_filters_and_stats(self):
        from fastapi.testclient import TestClient
        import server
        from tests.wizard_helpers import admin_client
        client = admin_client()
        activity_log.record("USER_ACTION", "hello", session_id="wiz-a")
        activity_log.record("AI_REQUEST", "req", session_id="wiz-a", data={"prompt_id": "X"})
        activity_log.record("FALLBACK", "fb", session_id="wiz-b")
        r = client.get("/api/admin/telemetry", params={"kinds": "AI_REQUEST,FALLBACK"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual({e["kind"] for e in body["events"]}, {"AI_REQUEST", "FALLBACK"})
        self.assertEqual(body["stats"]["ai_calls"], 1)
        self.assertEqual(body["stats"]["fallbacks"], 1)
        last = body["last_seq"]
        activity_log.record("USER_ACTION", "later", session_id="wiz-a")
        r = client.get("/api/admin/telemetry", params={"after": last, "session_id": "wiz-a"})
        self.assertEqual([e["title"] for e in r.json()["events"]], ["later"])      # incremental polling
        self.assertEqual(TestClient(server.app).get("/api/admin/telemetry").status_code, 401)   # login required


# ---------------------------------------------------------------------------------------------------------------
class TestRuntimeRepair(unittest.TestCase):
    WIN_TAIL = ("ValueError: The browser path is not a valid file: C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe\n"
                "selenium.common.exceptions.NoSuchDriverException: Message: Unable to obtain driver for chrome")

    def test_windows_selenium_error_is_browser_not_found(self):
        self.assertEqual(runtime_repair.diagnose({"failure_reason": "x"}, self.WIN_TAIL), "BROWSER_NOT_FOUND")

    def test_toolchain_missing_cannot_be_repaired(self):
        plan = runtime_repair.plan_repair("cypress", {"failure_reason": "[WinError 2] The system cannot find the file"}, "", [])
        self.assertEqual(plan["cause"], "TOOLCHAIN_MISSING")
        self.assertTrue(plan["exhausted"])

    def test_ladder_prefers_real_browsers_then_reinstall(self):
        found = {"chrome": "/c/chrome", "edge": "/c/edge", "chromium": None, "playwright_chromium": "/pw/chrome"}
        tried, ids = [], []
        for _ in range(5):
            p = runtime_repair.plan_repair("selenium-python", {}, self.WIN_TAIL, tried, found)
            ids.append(p["repair_id"])
            if p["repair_id"]:
                tried.append(p["repair_id"])
        self.assertEqual(ids, ["browser:chrome", "browser:edge", "browser:playwright_chromium", "reinstall", None])
        first = runtime_repair.plan_repair("selenium-python", {}, self.WIN_TAIL, [], found)
        self.assertEqual(first["env"], {"SELENIUM_BROWSER": "chrome", "CHROME_BINARY": "/c/chrome"})

    def test_driver_mismatch_hides_stale_driver(self):
        p = runtime_repair.plan_repair("selenium-python", {}, "This version of ChromeDriver only supports Chrome version 114",
                                       [], {"chrome": "/c/chrome"})
        self.assertEqual(p["repair_id"], "browser:chrome:nodriverpath")
        self.assertTrue(p["strip_driver_path"])
        d = tempfile.mkdtemp()
        open(os.path.join(d, "chromedriver"), "w").close()
        self.assertNotIn(d, runtime_repair.strip_driver_dirs(os.pathsep.join([d, "/usr/bin"])).split(os.pathsep))

    def test_playwright_browser_download(self):
        p = runtime_repair.plan_repair("playwright-js", {}, "browserType.launch: Executable doesn't exist at /x", [])
        self.assertEqual(p["repair_id"], "install_browsers")
        self.assertEqual(runtime_repair.browser_install_command("playwright-js"), ["npx", "playwright", "install", "chromium"])

    def test_child_env_keeps_system_vars_and_drops_secrets(self):
        fake = {"PATH": "/usr/bin", "PROGRAMFILES": r"C:\Program Files", "PATHEXT": ".COM;.EXE;.CMD", "SystemRoot": r"C:\Windows",
                "LOCALAPPDATA": r"C:\Users\u\AppData\Local", "GEMINI_API_KEY": FAKE_KEY, "GOOGLE_API_KEY": FAKE_KEY,
                "UNDERSTUDY_ADMIN_TOKEN": "t", "AWS_SECRET_ACCESS_KEY": "x", "MY_TOKEN": "x", "NPM_CONFIG_REGISTRY": "https://r"}
        with mock.patch.dict(os.environ, fake, clear=True):
            env = dependency_resolver._scrubbed_env({"EXTRA": "1"})
        for k in ("PATH", "PROGRAMFILES", "PATHEXT", "SystemRoot", "LOCALAPPDATA", "NPM_CONFIG_REGISTRY", "EXTRA"):
            self.assertIn(k, env)
        for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "UNDERSTUDY_ADMIN_TOKEN", "AWS_SECRET_ACCESS_KEY", "MY_TOKEN"):
            self.assertNotIn(k, env)

    def test_windows_cmd_launchers_are_resolved(self):
        d = tempfile.mkdtemp()
        name = "npx.cmd" if os.name == "nt" else "npx"
        path = os.path.join(d, name)
        with open(path, "w") as fh:
            fh.write("@echo off\n" if os.name == "nt" else "#!/bin/sh\n")
        os.chmod(path, 0o755)
        self.assertEqual(os.path.normcase(dependency_resolver.resolve_executable("npx", {"PATH": d})), os.path.normcase(path))
        self.assertEqual(dependency_resolver.resolve_executable("definitely-not-here-xyz", {"PATH": d}), "definitely-not-here-xyz")

    def test_selenium_template_supports_browser_selection(self):
        from core.wizard import templates
        from core.wizard.frameworks import FrameworkSpec
        spec = FrameworkSpec.from_dict(CATALOG["selenium-python"].to_dict())
        steps = [{"order": 1, "action": "navigate", "description": "Open"}]
        files, entry = templates.render("selenium-python", steps, [], None, spec)
        code = files[entry]
        compile(code, entry, "exec")
        for token in ("SELENIUM_BROWSER", "CHROME_BINARY", "EDGE_BINARY", "webdriver.Edge", "def make_driver"):
            self.assertIn(token, code)


# ---------------------------------------------------------------------------------------------------------------
class TestToolVersions(TelemetryBase):
    REG = {"versions": ["4.20.0", "4.21.0", "4.22.0rc1", "4.25.0", "3.141.0"], "latest": "4.25.0"}

    def setUp(self):
        super().setUp()
        tool_versions._CACHE.clear()
        self.p_fetch = mock.patch.object(tool_versions, "_fetch", return_value=dict(self.REG))
        self.fetch = self.p_fetch.start()
        self.p_inst = mock.patch.object(tool_versions, "installed_version", return_value=None)
        self.p_inst.start()

    def tearDown(self):
        self.p_fetch.stop()
        self.p_inst.stop()
        tool_versions._CACHE.clear()
        super().tearDown()

    def test_registry_versions_are_sorted_stable_and_cached(self):
        spec = CATALOG["selenium-python"]
        out = tool_versions.available_versions(spec)
        self.assertEqual(out["source"], "registry")
        self.assertEqual(out["versions"], ["4.25.0", "4.21.0", "4.20.0", "3.141.0"])   # newest first, no pre-releases
        self.assertEqual(out["latest"], "4.25.0")
        tool_versions.available_versions(spec)
        self.assertEqual(self.fetch.call_count, 1)                                  # cached

    def test_registry_offline_falls_back(self):
        self.fetch.side_effect = OSError("Tunnel connection failed: 403")
        out = tool_versions.available_versions(CATALOG["cypress"], refresh=True)
        self.assertEqual(out["source"], "offline")
        self.assertIn("403", out["error"])

    def test_admin_versions_pin_the_users_tool(self):
        with self.assertRaises(ap.PolicyError):
            ap.set_tool_versions("selenium-python", ["4.25.0"], default="4.21.0")    # default must be allowed
        with self.assertRaises(ap.PolicyError):
            ap.set_tool_versions("selenium-python", ["4.25.0; rm -rf /"])           # injection-proof
        pol = ap.set_tool_versions("selenium-python", ["4.25.0", "4.21.0"], default="4.21.0")
        self.assertEqual(pol["default"], "4.21.0")
        self.assertEqual(ap.allowed_spec("selenium-python").packages[0].version, "==4.21.0")
        self.assertEqual(ap.allowed_spec("selenium-python", "4.25.0").packages[0].version, "==4.25.0")
        with self.assertRaises(ap.PolicyError):
            ap.allowed_spec("selenium-python", "3.141.0")
        # re-approving the tool keeps the admin's version choice
        ap.whitelist_catalog_tool("selenium-python")
        self.assertEqual(ap.tool_version_policy("selenium-python")["allowed"], ["4.25.0", "4.21.0"])
        # the pinned version flows into the manifest that gets installed and exported
        self.assertIn("selenium==4.21.0", dependency_resolver.requirements_txt(ap.allowed_spec("selenium-python")))

    def test_user_picks_an_allowed_version(self):
        ap.set_tool_versions("selenium-python", ["4.25.0", "4.21.0"], default="4.25.0")
        install_fake(FakeLLM({"WATCH_ANALYZE": VIDEO_ANALYSIS, "BUILD_PARAMETERIZE": RuntimeError("n/a")}))
        s = wz.create_session(TARGET, "Approved vendors: Globex Corp, Acme.", fake_mp4(), "rec.mp4", background=False)
        sid = s["session_id"]
        s = wz.select_framework(sid, "selenium-python", background=False)
        self.assertEqual(s["framework"]["packages"][0]["version"], "==4.25.0")          # admin default
        s = wz.select_framework(sid, "selenium-python", background=False, version="4.21.0")
        self.assertEqual(s["framework"]["packages"][0]["version"], "==4.21.0")
        with self.assertRaises(wz.WizardError) as ctx:
            wz.select_framework(sid, "selenium-python", background=False, version="3.141.0")
        self.assertEqual(ctx.exception.status_code, 403)
        opt = next(o for o in wz.annotate_tools(s["framework_options"]) if o["id"] == "selenium-python")
        self.assertEqual(opt["versions"]["allowed"], ["4.25.0", "4.21.0"])

    def test_admin_api_versions(self):
        from tests.wizard_helpers import admin_client
        client = admin_client()
        r = client.get("/api/admin/tools/selenium-python/versions")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["package"], "selenium")
        self.assertEqual(r.json()["versions"][0], "4.25.0")
        r = client.put("/api/admin/tools/selenium-python/versions", json={"allowed": ["4.25.0"], "default": None})
        self.assertEqual(r.status_code, 200)
        wl = {t["id"]: t for t in r.json()["tools"]["whitelist"]}
        self.assertEqual(wl["selenium-python"]["versions"]["default"], "4.25.0")
        self.assertIn("toolchain_missing", wl["selenium-python"])
        self.assertEqual(client.put("/api/admin/tools/selenium-python/versions", json={"allowed": ["x y"]}).status_code, 400)
        self.assertTrue(any("allowed versions" in e["title"] for e in events("ADMIN")))


if __name__ == "__main__":
    unittest.main()
