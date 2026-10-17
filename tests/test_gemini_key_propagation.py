"""Integration test: the Gemini API key (and model) set from Mission Control is the one used by EVERY AI call.

Mission Control saves the key with ``POST /api/config/api-key`` and the model with ``POST /api/config/model``.
This test does exactly that over real HTTP against the live app, then drives a complete Understudy run -
media upload -> WATCH -> tool resolution -> BUILD -> GOVERN -> EXECUTE (real browser) -> failure -> self-healing
retry -> VERIFY -> ZIP - with the Google SDK replaced by a recording fake. It asserts that:

  * every AI call site was reached (all 8 Understudy prompts + the Model Armor semantic classifier + the File API
    video upload),
  * every ``genai.configure`` call used the key entered in Mission Control, and every model object was created with
    the model selected in Mission Control,
  * the steps are tagged ACTUAL_AI_API in the session trace,
  * a key rotated in Mission Control takes effect on the very next call (no caching),
  * the key never reaches the generated automation's process environment.
No request ever leaves the machine.
"""
import json
import os
import threading
import time
import types
import unittest
from unittest import mock

import requests

from tests.wizard_helpers import LiveServer, browser_available, fake_mp4, install_fake, wait_job
from core.wizard import admin_policy as ap
from core.wizard import session as wz
from core.wizard import templates, dom_discovery
from core.wizard.frameworks import CATALOG, FrameworkSpec
from core.wizard.prompts import PROMPTS

KEY_1 = "AIzaMissionControlKey-0000000000000000001"
KEY_2 = "AIzaRotatedMissionCtlKey-000000000000000002"
MODEL = "gemini-mission-control-test"


class RecordingGenAI(types.ModuleType):
    """Minimal stand-in for the Gen AI SDK (google-genai ``Client``) that records which key/model each call used."""

    def __init__(self, responder):
        super().__init__("fake_genai")
        self.lock = threading.Lock()
        self.current_key = None
        self.configured_keys = []
        self.calls = []            # {prompt_id, key, model}
        self.uploads = []
        self.responder = responder
        outer = self

        class GenerationConfig(dict):
            def __init__(self, **kw):
                super().__init__(**kw)

        class _Handle:
            def __init__(self, path):
                self.name = "files/" + os.path.basename(path)
                self.uri = "https://fake/" + self.name
                self.state = types.SimpleNamespace(name="ACTIVE")

        class GenerativeModel:
            def __init__(self, model_name, system_instruction=None, **kw):
                self.model_name = model_name
                self.system = system_instruction or ""

            def generate_content(self, parts, generation_config=None, request_options=None):
                text = parts if isinstance(parts, str) else "\n".join(p for p in parts if isinstance(p, str))
                prompt_id = next((pid for pid, spec in PROMPTS.items() if spec.system == self.system), None)
                if prompt_id is None:
                    prompt_id = "MODEL_ARMOR_CLASSIFIER" if "security classifier" in text else "OTHER"
                with outer.lock:
                    outer.calls.append({"prompt_id": prompt_id, "key": outer.current_key, "model": self.model_name})
                return types.SimpleNamespace(text=json.dumps(outer.responder(prompt_id, text)))

        self.GenerationConfig = GenerationConfig
        self.GenerativeModel = GenerativeModel
        self._Handle = _Handle

        class _Files:                                   # google-genai: client.files
            def __init__(self, key):
                self.key = key

            def upload(self, file=None, **kw):
                with outer.lock:
                    outer.uploads.append({"path": file, "key": self.key})
                return _Handle(file)

            def get(self, name=None, **kw):
                h = _Handle(name)
                h.name = name
                return h

        class _Models:                                  # google-genai: client.models
            def __init__(self, key):
                self.key = key

            def generate_content(self, model=None, contents=None, config=None, **kw):
                system = getattr(config, "system_instruction", "") or ""
                text = "\n".join(p for p in (contents or []) if isinstance(p, str))
                prompt_id = next((pid for pid, spec in PROMPTS.items() if spec.system == system), "OTHER")
                with outer.lock:
                    outer.calls.append({"prompt_id": prompt_id, "key": self.key, "model": model, "sdk": "google-genai"})
                return types.SimpleNamespace(text=json.dumps(outer.responder(prompt_id, text)))

            def list(self, **kw):
                return []

        class Client:                                   # google-genai: genai.Client(api_key=...)
            def __init__(self, api_key=None, vertexai=None, **kw):
                with outer.lock:
                    outer.configured_keys.append(api_key)
                self.files = _Files(api_key)
                self.models = _Models(api_key)

        self.Client = Client

    def configure(self, api_key=None, **kw):
        with self.lock:
            self.current_key = api_key
            self.configured_keys.append(api_key)

    def upload_file(self, path=None, **kw):
        with self.lock:
            self.uploads.append({"path": path, "key": self.current_key})
        return self._Handle(path)

    def get_file(self, name):
        return self._Handle(name)

    def list_models(self):
        return []


@unittest.skipUnless(browser_available(), "Playwright Chromium not available")
class TestMissionControlKeyIsUsedEverywhere(unittest.TestCase):
    def setUp(self):
        install_fake(None)
        ap.reset_to_defaults()
        # auto: Playwright (Python) is already installed here, so the generated suite runs with its own runtime -
        # that is what lets us check the key is NOT visible inside the automation process.
        os.environ["UNDERSTUDY_DEPENDENCY_INSTALL"] = "auto"
        self.srv = LiveServer.get()
        self.B = self.srv.base
        self.good_steps = None
        self.fake = RecordingGenAI(self.respond)
        # 9.4.1: google-genai is the only SDK - every AI call (and the key check) goes through genai.Client
        self.patches = [mock.patch("google.genai.Client", self.fake.Client)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GEMINI_MODEL", "UNDERSTUDY_DEPENDENCY_INSTALL"):
            os.environ.pop(k, None)

    # ------------------------------------------------------------------ fake model behaviour
    def respond(self, prompt_id, text):
        portal = self.B + "/portal1"
        if prompt_id == "MODEL_ARMOR_CLASSIFIER":
            return {"is_adversarial": False, "reason": "benign", "detected_trigger": ""}
        if prompt_id == "WATCH_URLS":           # 9.4: the address bar shows the page the user also typed
            return {"pages": [{"url": portal, "video_time": "00:01", "evidence": "address bar", "purpose": "AP entry"}]}
        if prompt_id in ("WATCH_ANALYZE", "WATCH_REFINE"):
            inv = dom_discovery.discover(portal)
            steps = [{"order": 1, "action": "navigate", "description": "Open the AP portal"}]
            params = []
            for el in inv.fields():
                key = el.name
                steps.append({"order": len(steps) + 1, "action": "fill", "selector": el.best_selector(), "label": el.label,
                              "param_key": key, "description": f"Type the {el.label}"})
                params.append({"key": key, "label": el.label, "type": {"date": "date", "number": "number"}.get(el.type, "text"),
                               "value": {"vendor_name": "Globex Corp", "invoice_date": "2026-06-15", "total_amount": 321.5,
                                         "invoice_notes": f"GEMINI-KEY-{int(time.time() * 1000) % 10**8}"}.get(key, "x"),
                               "source": "video"})
            steps.append({"order": len(steps) + 1, "action": "click", "selector": "#submit-btn", "label": "Submit Invoice",
                          "description": "Submit the invoice"})
            return {"process_name": "AP invoice entry (seen in video)", "summary": "Enters one supplier invoice.",
                    "app_type": "web_ui", "steps": steps, "parameters": params,
                    "verification": {"method": "dom_text", "url": portal,
                                     "expect_param_keys": ["vendor_name", "invoice_notes"]},
                    "observed_text": "Invoice entry form, Globex Corp", "framework_options": [
                        dict(CATALOG["playwright-python"].to_dict(), recommended=True)]}
        if prompt_id == "FRAMEWORK_RESOLVE":
            return CATALOG["playwright-python"].to_dict()
        if prompt_id == "BUILD_PARAMETERIZE":
            s = wz.SESSIONS[self.sid]
            return {"parameters": [dict(p, sop_dependent=p["key"] == "total_amount") for p in s["analysis"]["parameters"]]}
        if prompt_id == "BUILD_GENERATE_SUITE":
            s = wz.SESSIONS[self.sid]
            spec = FrameworkSpec.from_dict(s["framework"])
            files, entry = templates.render("playwright-python", s["analysis"]["steps"], s["parameters"], None, spec)
            code = files[entry].replace(
                "def main():\n",
                "def main():\n    print('KEYCHECK=' + str(bool(os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY'))), flush=True)\n", 1)
            return {"files": [{"path": entry, "content": code}], "entry_file": entry, "notes": "written by the fake model"}
        if prompt_id == "GOVERN_EXTRACT_RULES":
            return {"rules": [{"field": "total_amount", "rule_type": "max", "limit": 5000, "source_text": "Spend limit $5,000",
                               "plain_english": "Amount at most 5,000"}], "security_flags": []}
        if prompt_id == "GOVERN_COMPLIANCE_REVIEW":
            return {"warnings": [{"field": "invoice_notes", "message": "Looks fine", "severity": "info"}]}
        if prompt_id == "VERIFY_SELF_HEAL":
            return {"diagnosis": "Selectors drifted; restoring the ones from the live page.", "failure_category": "SELECTOR_DRIFT",
                    "steps": self.good_steps, "healing_notes": ["Use #vendor_name for the vendor field"]}
        if prompt_id == "ENV_UPGRADE_ADVICE":
            return {"summary": "Test advice", "steps": [{"title": "Upgrade"}]}
        if prompt_id == "CODE_HEALTH_FIX":
            return {"diagnosis": "Test: leave the file unchanged.", "strategy": "no_action", "edits": []}
        if prompt_id == "SESSION_TITLE":
            return {"title": "Supplier Invoice Entry - AP Portal"}
        if prompt_id == "INSPECTION_PLAN":
            return {"pages": [{"index": 1, "checks": [{"id": "targets", "why": "steps 2-5 fill the invoice form"}]}],
                    "stability": "if_suspicious", "stability_why": "ids look hand-written", "focus": ["one stable form"]}
        if prompt_id == "AUTOMATION_COMPLEXITY":
            return {"score": 2, "level": "Simple", "summary": "A short form on a stable page.", "challenges": [],
                    "tool_fit": [{"tool_id": "playwright-python", "fit": "good", "why": "auto-waits"}], "tips": []}
        if prompt_id == "AGENTIC_SCRIPT":
            return {"script": "bad", "notes": "x"}
        if prompt_id == "EXPORT_README":
            return {"readme": "# Supplier Invoice Entry\n\nRun `bash run.sh`.\n"}
        if prompt_id == "AUTOMATION_DIFFICULTY":
            return {"parameters": [{"key": "dom_stability", "name": "Form stability", "category": "System & Data Readiness",
                                    "value": 2, "weight": 20, "why_here": "stable ids"}],
                    "summary": "A short form on a stable page.",
                    "recommendation": {"approach": "traditional", "confidence": "high", "why": ["stable page"]},
                    "tool_fit": [{"tool_id": "playwright-python", "fit": "good", "why": "auto-waits"}]}
        if prompt_id == "AUTOMATION_ROUTING":
            return {"approach": "traditional", "confidence": "medium", "why": ["still a stable form"],
                    "tool_fit": [{"tool_id": "gemini-agent", "fit": "ok", "why": "not needed here"}]}
        if prompt_id == "TOOL_SETUP_PLAN":
            spec = CATALOG["cypress"].to_dict()
            return {"summary": "Install Cypress from npm.", "spec": spec, "download_steps": ["npm install cypress"],
                    "versions": {"suggested_allowed": [], "recommended_default": ""}, "instructions_applied": []}
        if prompt_id == "TOOL_SETUP_CLARIFY":
            return {"diagnosis": "The npm registry refused the download (E403).", "category": "network",
                    "question": "Admin, npm refused the Cypress download. Should I use your Nexus npm mirror?",
                    "options": [{"id": "use_mirror", "label": "Use our mirror", "effect": "sets the registry"},
                                {"id": "stop", "label": "Stop", "effect": "stops the setup"}]}
        if prompt_id == "TOOL_SETUP_RESOLVE":
            return {"next": "stop", "summary": "Stopping the setup as the admin asked.", "changes": {}}
        if prompt_id == "TOOL_SMOKE_TEST":
            cfg = ("module.exports = { e2e: { supportFile: false, video: false, specPattern: 'cypress/e2e/**/*.cy.js',\n"
                   "  setupNodeEvents(on) { on('task', { understudyEnv() { return { SMOKE_URL: process.env.SMOKE_URL || null }; } }); } } };\n")
            spec_js = ("describe('smoke', () => { it('clicks', () => { cy.task('understudyEnv').then((env) => {\n"
                       "  cy.visit(env.SMOKE_URL); cy.get('#name').type('Understudy'); cy.get('#go').click();\n"
                       "  cy.get('#status').should('have.text', 'CLICKED:Understudy'); }); }); });\n")
            return {"files": [{"path": "cypress.config.js", "content": cfg},
                              {"path": "cypress/e2e/automation.cy.js", "content": spec_js}], "notes": "fake smoke test"}
        if prompt_id == "TOOL_STACK_SUGGEST":
            return {"tool_family": "WebdriverIO", "summary": "WebdriverIO for browser tests.", "category": "browser",
                    "languages": [{"id": "javascript", "name": "JavaScript", "recommended": True, "package_managers": ["npm"]}],
                    "package_managers": [{"id": "npm", "name": "npm", "language": "javascript", "ecosystem": "npm"}],
                    "components": [{"id": "webdriverio", "name": "WebdriverIO", "role": "core", "required": True,
                                    "packages": [{"language": "javascript", "package_manager": "npm", "name": "webdriverio",
                                                  "recommended_version": "9.0.0"}]}],
                    "run_profiles": [{"language": "javascript", "package_manager": "npm", "run_command": ["node", "{entry}"],
                                      "entry_file": "run_automation.js"}]}
        if prompt_id == "RUNTIME_REPAIR":
            return {"diagnosis": "The packages look broken.", "action": "reinstall",
                    "explanation": "Reinstall the tool's packages."}
        return {}

    def api(self, method, path, **kw):
        if path.startswith(("/api/admin", "/api/config")):
            if not getattr(self, "_admin", None):
                from tests.wizard_helpers import admin_headers
                self._admin = admin_headers(self.B)
            kw.setdefault("headers", {}).update(self._admin)
        return requests.request(method, self.B + path, timeout=240, **kw)

    # ------------------------------------------------------------------ the test
    def test_key_and_model_from_mission_control_drive_every_ai_call(self):
        # 1. Admin sets the key + model in Mission Control (same endpoints the UI calls; not persisted to .env here).
        r = self.api("POST", "/api/config/api-key", json={"api_key": KEY_1, "selected_model": MODEL,
                                                          "validate_on_save": False, "persist": False})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.api("POST", "/api/config/model", json={"selected_model": MODEL, "persist": False}).status_code, 200)
        info = self.api("GET", "/api/wizard/info").json()
        self.assertEqual((info["ai_mode"], info["model"]), ("ACTUAL_AI_API", MODEL))

        # 2. A developer runs Understudy end to end.
        with open(fake_mp4(), "rb") as fh:
            r = self.api("POST", "/api/wizard/sessions?wait=true",
                         data={"target_url": self.B + "/portal1", "sop_text": "The spend limit per invoice is $5,000."},
                         files={"media": ("recording.mp4", fh, "video/mp4")})
        self.assertEqual(r.status_code, 200, r.text)
        s = r.json()["session"]
        self.sid = s["session_id"]
        self.assertEqual({k: s["analysis_meta"][k] for k in ("agent_source", "model")},
                         {"agent_source": "ACTUAL_AI_API", "model": MODEL})
        self.assertIsNone(s["analysis_meta"]["ai_error"])
        self.assertTrue(s["analysis"]["media_interpreted"])
        sid = self.sid
        # The user tells the AI what it missed: WATCH_REFINE re-watches the video with the correction (same key/model).
        r = self.api("POST", f"/api/wizard/sessions/{sid}/watch/clarify?wait=true",
                     json={"feedback": "You missed that the notes field must mention the PO number."})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["session"]["clarifications"][0]["status"], "applied")
        # 9.8: the user tunes a difficulty parameter -> AUTOMATION_ROUTING
        first = self.api("GET", f"/api/wizard/sessions/{sid}").json()["session"]["difficulty"]["parameters"][0]["key"]
        r = self.api("POST", f"/api/wizard/sessions/{sid}/difficulty/recalculate",
                     json={"parameters": [{"key": first, "value": 5}]})
        self.assertEqual(r.status_code, 200, r.text)
        self.api("POST", f"/api/wizard/sessions/{sid}/tool-request", json={"custom_name": "Playwright"})   # FRAMEWORK_RESOLVE
        s = self.api("POST", f"/api/wizard/sessions/{sid}/framework?wait=true", json={"framework_id": "playwright-python"}).json()["session"]
        s = wait_job(lambda: self.api("GET", f"/api/wizard/sessions/{sid}").json()["session"])
        self.api("PUT", f"/api/wizard/sessions/{sid}/govern/rules", json={"rules": wz.get(sid)["rules"], "confirmed": True})
        self.api("POST", f"/api/wizard/sessions/{sid}/govern/check")
        s = self.api("POST", f"/api/wizard/sessions/{sid}/build?wait=true").json()["session"]
        self.assertEqual((s["suite"]["generator"], s["suite"]["agent_source"], s["suite"]["model"]), ("ai", "ACTUAL_AI_API", MODEL))
        self.assertEqual(s["rules_meta"]["agent_source"], "ACTUAL_AI_API")
        s = wz.get(sid)
        self.assertEqual(s["pillars"]["GOVERN"]["status"], "PASS", s.get("compliance"))
        self.assertEqual(s["compliance"]["ai_review"]["agent_source"], "ACTUAL_AI_API")

        # Break the selectors so the run fails and the AI self-healer is exercised too.
        live = wz.get(sid)
        self.good_steps = [dict(st) for st in live["analysis"]["steps"]]
        for st in live["analysis"]["steps"]:
            if st["action"] == "fill":
                st.update(selector="#legacy-" + st["param_key"], fallback_selectors=[], label="legacy " + st["param_key"])
        self.api("PUT", f"/api/wizard/sessions/{sid}/govern/rules", json={"rules": wz.get(sid)["rules"], "confirmed": True})
        self.api("POST", f"/api/wizard/sessions/{sid}/govern/check")
        s = self.api("POST", f"/api/wizard/sessions/{sid}/build?wait=true").json()["session"]
        # 3. Admin rotates the key in Mission Control mid-session: the next AI calls must use the new key.
        calls_before_rotation = len(self.fake.calls)
        self.api("POST", "/api/config/api-key", json={"api_key": KEY_2, "validate_on_save": False, "persist": False})
        # The run fails on the broken selectors and heals AUTOMATICALLY (AI self-heal with the rotated key).
        s = self.api("POST", f"/api/wizard/sessions/{sid}/execute?wait=true").json()["session"]
        s = wait_job(lambda: self.api("GET", f"/api/wizard/sessions/{sid}").json()["session"])
        self.assertEqual(s["retry"]["count"], 1)
        self.assertEqual(s["retry"]["history"][0]["outcome"], "PASS")
        self.assertEqual(s["retry"]["last_heal"]["agent_source"], "ACTUAL_AI_API")
        self.assertTrue(all(s["pillars"][p]["status"] == "PASS" for p in wz.PILLARS), s["pillars"])
        self.assertIsNone(s["verification"])                     # 9.0: Step 5 only packages the ZIP
        self.assertTrue(s["export"]["ready"])
        logs = self.api("GET", f"/api/wizard/sessions/{sid}/logs").json()["lines"]
        self.assertEqual(s["execution"]["strategy"], "framework", logs[-5:])
        self.assertTrue(any("KEYCHECK=False" in l for l in logs), "the Gemini key leaked into the automation's environment")
        self.assertFalse(any("KEYCHECK=True" in l for l in logs))

        # Code health (Mission Control): with the key set, the AI is asked to fix start-up warnings - same key/model.
        # 9.5.2: the shipped tree starts without warnings, so one is added to the scan result (the AI answers
        # "no_action", so no file is touched).
        from unittest import mock as _mock
        from core.wizard import code_health
        _orig_scan = code_health.scan_tree

        def _scan_with_warning(root):
            res = _orig_scan(root)
            res["warnings"].append({"file": "server.py", "lineno": 1, "category": "DeprecationWarning",
                                    "message": "planted by the key-propagation test",
                                    "filename": os.path.join(root, "server.py")})
            return res

        with _mock.patch.object(code_health, "scan_tree", _scan_with_warning):
            if os.path.exists(code_health.state_path()):
                os.remove(code_health.state_path())
            self.api("POST", "/api/admin/code-health/run")
            deadline = time.time() + 180
            while time.time() < deadline:
                ch = self.api("GET", "/api/admin/code-health").json()
                if not ch["busy"] and ch["last_scan"]:
                    break
                time.sleep(1)
        self.assertTrue(ch["ai_available"])
        # Tool setup (Mission Control, 7.2): the AI prepares the setup of a tool the admin adds - same key/model.
        # (8.2: assistant=false prepares the plan directly; the stack assistant is covered by test_v82_ai_install.py)
        r = self.api("POST", "/api/admin/tool-setup", json={"name": "Cypress", "assistant": False})
        if r.status_code == 409:                                       # already approved by default: ask for another
            self.api("DELETE", "/api/admin/tools/cypress")
            r = self.api("POST", "/api/admin/tool-setup", json={"name": "Cypress", "assistant": False})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["request"]["plan"]["agent_source"], "ACTUAL_AI_API")
        # 8.0 synthesizer: a failed step asks the admin (TOOL_SETUP_CLARIFY), the answer is applied (TOOL_SETUP_RESOLVE)
        # and the smoke test is written by the AI (TOOL_SMOKE_TEST) - same key/model. (Installing for real needs the
        # network, so the failure is injected and the smoke test is generated without running it.)
        from core.wizard import tool_setup
        from core.wizard.sandbox import create_runner
        rid = r.json()["request"]["id"]
        tool_setup._halt(rid, {"status": "installing", "round": 0, "overrides": {}, "attempts": [], "allowed": [],
                               "default": None, "by": "admin", "logs": []},
                         tool_setup.SetupFailure("install", "npm exited with code 1", "npm install cypress", 1,
                                                 "npm error code E403\nnpm error 403 Forbidden - GET https://registry.npmjs.org/cypress"))
        q = ap.get_request(rid)["clarification"]
        self.assertEqual((q["agent_source"], q["category"]), ("ACTUAL_AI_API", "network"))
        r = self.api("POST", f"/api/admin/tool-requests/{rid}/answer?wait=true", json={"text": "No, stop for now.", "option": ""})
        self.assertEqual((r.status_code, r.json()["request"]["status"]), (200, "setup_failed"), r.text)
        req = ap.get_request(rid)
        spec = FrameworkSpec.from_dict(req["plan"]["spec"])
        with create_runner(spec, purpose="setup", mode="local") as runner:
            files, source, notes, _ai_notes = tool_setup._smoke_files(req, spec, runner.kind)
        self.assertEqual(source, "ai", notes)
        self.assertTrue(files)
        # 8.2: the AI install assistant (TOOL_STACK_SUGGEST) and the AI-advised runtime repair (RUNTIME_REPAIR)
        r = self.api("POST", "/api/admin/tool-setup", json={"name": "WebdriverIO"})
        self.assertEqual((r.status_code, r.json()["request"]["status"]), (200, "stack_ready"), r.text)
        from core.wizard import runtime_repair
        plan = runtime_repair.ai_plan_repair(spec, {"failure_reason": "boom"}, "boom", [], "UNKNOWN_RUNTIME", None, {})
        self.assertEqual((plan["repair_id"], plan["source"]), ("reinstall", "ai"))
        # 8.3: the AI runtime install advisor (Mission Control > Package managers)
        r = self.api("POST", "/api/admin/toolchains/advise", json={"programs": ["ruby", "gem"]})
        self.assertEqual(r.status_code, 200, r.text)
        # 9.9: the agentic-script author (an agentic tool's BUILD)
        from core.wizard import agentic_script
        agentic_script.generate([{"order": 1, "action": "navigate"}], [], ["http://x"], {"process_name": "p"})
        # 8.5: the setup repair agent
        from core.wizard import setup_agent
        setup_agent._call({k: "x" for k in ("tool", "server_facts", "stage", "command", "exit_code", "message", "output",
                                            "test_files", "history", "repeated", "lessons", "admin_notes", "checks",
                                            "observations", "last_step")}, None)

        # 4. Every AI call site was reached, with the Mission Control key and model.
        reached = {c["prompt_id"] for c in self.fake.calls}
        # INSPECTION_RELOAD runs between real page loads (probing is off here) - covered by test_v94_multi_url_inspection
        expected = (set(PROMPTS) - {"INSPECTION_RELOAD"}) | {"MODEL_ARMOR_CLASSIFIER"}
        self.assertTrue(expected <= reached, f"AI call sites never reached: {sorted(expected - reached)}")
        self.assertEqual(self.fake.uploads[0]["key"], KEY_1)                      # video went to the File API with KEY_1
        before, after = self.fake.calls[:calls_before_rotation], self.fake.calls[calls_before_rotation:]
        self.assertTrue(before and after)
        self.assertEqual({c["key"] for c in before}, {KEY_1}, "a call before rotation used a different key")
        self.assertEqual({c["key"] for c in after}, {KEY_2}, "a call after rotation still used the old key")
        self.assertEqual({c["model"] for c in self.fake.calls}, {MODEL}, "a call used a model other than the one selected")
        understudy_calls = [c for c in self.fake.calls if c["prompt_id"] in PROMPTS]
        self.assertTrue(understudy_calls and all(c.get("sdk") == "google-genai" for c in understudy_calls),
                        "Understudy must call Gemini through the current google-genai SDK")
        self.assertEqual(len(self.fake.uploads), 1, "the recording must be uploaded once and reused (WATCH + refine)")
        self.assertTrue(set(self.fake.configured_keys) <= {KEY_1, KEY_2})
        ai_stages = {t["stage"] for t in s["agent_trace"] if t["agent_source"] == "ACTUAL_AI_API"}
        self.assertTrue({"WATCH", "BUILD", "GOVERN", "VERIFY"} <= ai_stages, ai_stages)

        # 4b. Mission Control telemetry recorded every call: the exact prompt, the response, the masked key - never the key.
        tel = self.api("GET", "/api/admin/telemetry", params={"session_id": sid, "limit": 2000}).json()
        req_ids = {e["data"].get("prompt_id") for e in tel["events"] if e["kind"] == "AI_REQUEST"}
        # code health and tool setup are admin work, not part of a session
        session_prompts = set(PROMPTS) - {"CODE_HEALTH_FIX", "ENV_UPGRADE_ADVICE", "TOOL_SETUP_PLAN",
                                           "TOOL_SETUP_CLARIFY", "TOOL_SETUP_RESOLVE", "TOOL_SMOKE_TEST",
                                           "TOOL_STACK_SUGGEST", "RUNTIME_REPAIR",
                                           "RUNTIME_INSTALL_ADVICE", "SETUP_AGENT_STEP",
                                           "INSPECTION_RELOAD"}     # needs real page loads (probing is off here)
        self.assertTrue(session_prompts <= req_ids, sorted(session_prompts - req_ids))
        resp = [e for e in tel["events"] if e["kind"] == "AI_RESPONSE" and e["data"].get("prompt_id") == "WATCH_ANALYZE"]
        self.assertTrue(resp and resp[0]["data"]["raw_response"])
        req = next(e for e in tel["events"] if e["kind"] == "AI_REQUEST" and e["data"].get("prompt_id") == "WATCH_ANALYZE")
        self.assertIn("<untrusted_input", req["data"]["user_prompt"])
        self.assertEqual(req["data"]["model"], MODEL)
        self.assertEqual(req["data"]["media"]["file"].split(".")[-1], "mp4")
        raw_tel = json.dumps(tel)
        self.assertNotIn(KEY_1, raw_tel)
        self.assertNotIn(KEY_2, raw_tel)
        self.assertTrue(any(e["kind"] == "USER_ACTION" for e in tel["events"]))

        # 5. Clearing the key switches everything back to offline mode.
        os.environ.pop("GEMINI_API_KEY", None)
        os.environ.pop("GOOGLE_API_KEY", None)
        self.assertEqual(self.api("GET", "/api/wizard/info").json()["ai_mode"], "LOCAL_HEURISTIC")


if __name__ == "__main__":
    unittest.main()
