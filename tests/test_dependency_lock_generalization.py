"""v7.0:
* Dependency & version fix: the admin-approved / user-picked tool version is what the sandbox installs, what the run
  directory declares and what the ZIP ships; extra packages the generated code imports are added (trusted only);
  the sandbox is verified (and repaired) before every run; a version the admin no longer allows stops the run.
* Generalisation: page discovery for any site (links, ARIA widgets, radios, contenteditable, constraints, forms,
  same-origin API calls, SPA rendering), grounding of the AI's steps on the real page, payload schema from the page,
  and a full offline run against a site that is not one of the sample portals.
"""
import io
import json
import os
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from urllib.parse import parse_qs

from tests.wizard_helpers import FakeLLM, browser_available, fake_mp4, free_port, install_fake, govern
from tests.test_wizard_media_to_payload import TARGET, VIDEO_ANALYSIS, fixture_inventory
from core.wizard import (admin_policy as ap, dependency_lock, dependency_resolver, dom_discovery, exporter,
                         payload_schema, templates, watch)
from core.wizard import session as wz
from core.wizard.frameworks import CATALOG, FrameworkSpec


class Base(unittest.TestCase):
    def setUp(self):
        ap.reset_to_defaults()
        os.environ["UNDERSTUDY_DEPENDENCY_INSTALL"] = "dry-run"
        self.p = mock.patch("core.wizard.watch.dom_discovery.discover", side_effect=fixture_inventory)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        install_fake(None)
        os.environ.pop("UNDERSTUDY_DEPENDENCY_INSTALL", None)


def ai_suite_with_own_manifest(extra_import="import dotenv  # loads .env when present"):
    """What Gemini typically returned before 7.0: working code plus its OWN unpinned requirements.txt."""
    steps = watch.normalize_analysis(dict(VIDEO_ANALYSIS), TARGET)[0]["steps"]
    params = [{"key": p["key"], "type": p["type"], "label": p["label"]} for p in VIDEO_ANALYSIS["parameters"]]
    files, entry = templates.render("playwright-python", steps, params, None, CATALOG["playwright-python"])
    code = files[entry].replace("import json\n", "import json\n" + extra_import + "\n", 1)
    return {"files": [{"path": entry, "content": code},
                      {"path": "requirements.txt", "content": "playwright\npython-dotenv\n"}],
            "entry_file": entry, "notes": "AI suite"}


class TestDependencyLock(Base):
    def _session(self, suite, version="1.44.0"):
        ap.set_tool_versions("playwright-python", ["1.40.0", "1.44.0"], default="1.40.0")
        install_fake(FakeLLM({"WATCH_ANALYZE": VIDEO_ANALYSIS, "BUILD_PARAMETERIZE": RuntimeError("n/a"),
                              "BUILD_GENERATE_SUITE": suite}))
        s = wz.create_session(TARGET, "", fake_mp4(), "rec.mp4", background=False)
        s = wz.select_framework(s["session_id"], "playwright-python", background=False, version=version)
        govern(s["session_id"])
        return wz.build_suite(s["session_id"], background=False)

    def test_pinned_version_and_extras_reach_suite_sandbox_and_zip(self):
        s = self._session(ai_suite_with_own_manifest())
        self.assertEqual(s["pillars"]["BUILD"]["status"], "PASS", s["pillars"]["BUILD"])
        self.assertEqual(s["suite"]["generator"], "ai")
        req = s["suite"]["files"]["requirements.txt"].split()
        self.assertEqual(req, ["playwright==1.44.0", "python-dotenv"])            # the AI's unpinned manifest is gone
        self.assertEqual([e["name"] for e in s["dependency_lock"]["extras"]], ["python-dotenv"])
        self.assertEqual(s["dependency_lock"]["chosen_version"], "1.44.0")
        # the sandbox plan installs exactly that
        plan = s["dependencies"]["plan"]
        self.assertEqual(plan["manifest_files"]["requirements.txt"].split(), req)
        self.assertEqual(s["framework"]["packages"][0], {"name": "playwright", "version": "==1.44.0"})
        # ZIP: pinned to what was really installed in the sandbox
        s["dependencies"]["versions"] = [{"name": "playwright", "requested": "==1.44.0", "installed": "1.44.0",
                                          "source": "isolated environment"},
                                         {"name": "python-dotenv", "requested": "any", "installed": "1.0.1",
                                          "source": "isolated environment"}]
        z = zipfile.ZipFile(io.BytesIO(exporter.build_zip(s, wz._spec(s))))
        self.assertEqual(z.read("requirements.txt").decode().split(), ["playwright==1.44.0", "python-dotenv==1.0.1"])
        lock = json.loads(z.read("config/dependency_lock.json"))
        self.assertEqual(lock["primary"]["chosen_version"], "1.44.0")
        self.assertEqual(lock["primary"]["admin_allowed"], ["1.40.0", "1.44.0"])
        self.assertEqual({p["name"]: p["installed"] for p in lock["packages"]}, {"playwright": "1.44.0", "python-dotenv": "1.0.1"})
        readme = z.read("README.md").decode()
        self.assertIn("## Dependency versions", readme)
        self.assertIn("`python-dotenv` | `any` | `1.0.1`", readme)
        self.assertIn("pip install -r requirements.txt", z.read("run.sh").decode())

    def test_admin_default_version_applies_when_the_user_picks_none(self):
        ap.set_tool_versions("playwright-python", ["1.40.0", "1.44.0"], default="1.40.0")
        install_fake(FakeLLM({"WATCH_ANALYZE": VIDEO_ANALYSIS, "BUILD_PARAMETERIZE": RuntimeError("n/a"),
                              "BUILD_GENERATE_SUITE": RuntimeError("offline")}))
        s = wz.create_session(TARGET, "", fake_mp4(), "rec.mp4", background=False)
        s = wz.select_framework(s["session_id"], "playwright-python", background=False)
        govern(s["session_id"])
        s = wz.build_suite(s["session_id"], background=False)
        self.assertEqual(s["suite"]["generator"], "template")
        self.assertEqual(s["suite"]["files"]["requirements.txt"].strip(), "playwright==1.40.0")

    def test_untrusted_package_in_ai_code_falls_back_to_the_template(self):
        s = self._session(ai_suite_with_own_manifest("import leftpad_evil"))
        self.assertEqual(s["suite"]["generator"], "template")
        self.assertTrue(any("outside the trusted list" in n for n in s["suite"]["notes"]), s["suite"]["notes"])
        self.assertEqual(s["suite"]["files"]["requirements.txt"].strip(), "playwright==1.44.0")

    def test_npm_manifest_keeps_module_type_and_pins(self):
        spec = FrameworkSpec.from_dict(CATALOG["playwright-js"].to_dict())
        spec.packages[0].version = "1.47.2"
        files = {"run_automation.js": "import { chromium } from 'playwright';\nimport fs from 'node:fs';\n"
                                      "import { test } from '@playwright/test';\nimport x from './helpers.js';\n",
                 "package.json": json.dumps({"type": "module", "dependencies": {"playwright": "^1.30.0", "left-pad": "1.0.0"}}),
                 "package-lock.json": "{}"}
        det = dependency_lock.detect(spec, files)
        self.assertEqual([e["name"] for e in det["extras"]], ["@playwright/test"])
        self.assertEqual(det["untrusted"], ["left-pad"])
        self.assertTrue(any("approved version" in n for n in det["notes"]))
        out = dependency_lock.apply_to_files(dependency_lock.effective_spec(spec, det["extras"]), files)
        pj = json.loads(out["package.json"])
        self.assertEqual(pj["type"], "module")
        self.assertEqual(pj["dependencies"], {"playwright": "1.47.2", "@playwright/test": "latest"})
        self.assertNotIn("package-lock.json", out)

    def test_gem_versions_are_pinned_in_the_install_plan(self):
        spec = FrameworkSpec.from_dict({"id": "capy", "name": "Capybara", "language": "Ruby", "ecosystem": "gem",
                                        "packages": [{"name": "capybara", "version": "3.40.0"}],
                                        "run_command": ["ruby", "{entry}"], "entry_file": "a.rb"})
        cmd = dependency_resolver.plan_for(spec).commands[0]
        self.assertEqual(cmd[-3:], ["capybara", "-v", "3.40.0"])

    def test_sandbox_is_verified_and_repaired_before_the_run(self):
        s = self._session(ai_suite_with_own_manifest())
        spec = wz._spec(s)
        s["dependencies"] = {"status": "READY", "plan": {"cache_key": dependency_resolver.plan_for(spec).cache_key},
                             "framework_id": spec.id, "versions": []}
        lines = []
        wrong = [{"name": "playwright", "requested": "==1.44.0", "installed": "1.48.0", "source": "shared backend environment"},
                 {"name": "python-dotenv", "requested": "any", "installed": None, "source": "not installed"}]
        ok = {"status": "READY", "message": "installed", "versions": [], "plan": {}}
        with mock.patch.dict(os.environ, {"UNDERSTUDY_DEPENDENCY_INSTALL": "auto"}), \
                mock.patch.object(dependency_resolver, "installed_versions", return_value=wrong), \
                mock.patch.object(dependency_resolver, "install", return_value=ok) as inst:
            self.assertIsNone(wz._ensure_dependencies(s, lines.append))
        inst.assert_called_once()
        self.assertTrue(inst.call_args.kwargs["force"])
        self.assertTrue(any("1.48.0 is installed but 1.44.0 is required" in ln for ln in lines), lines)
        self.assertEqual(s["dependencies"]["status"], "READY")
        # and when everything matches nothing is installed
        right = [{"name": "playwright", "requested": "==1.44.0", "installed": "1.44.0", "source": "isolated environment"},
                 {"name": "python-dotenv", "requested": "any", "installed": "1.0.1", "source": "isolated environment"}]
        s["dependencies"] = {"status": "READY", "plan": {"cache_key": dependency_resolver.plan_for(spec).cache_key},
                             "framework_id": spec.id, "versions": right}
        with mock.patch.dict(os.environ, {"UNDERSTUDY_DEPENDENCY_INSTALL": "auto"}), \
                mock.patch.object(dependency_resolver, "installed_versions", return_value=right), \
                mock.patch.object(dependency_resolver, "install") as inst2:
            self.assertIsNone(wz._ensure_dependencies(s, lines.append))
        inst2.assert_not_called()

    def test_version_no_longer_allowed_stops_the_run_without_retry(self):
        s = self._session(ai_suite_with_own_manifest())
        ap.set_tool_versions("playwright-python", ["1.40.0"], default="1.40.0")       # admin narrowed it afterwards
        res = wz._do_execute(s, 1)
        self.assertEqual(res["failure_category_hint"], "POLICY")
        self.assertIn("1.44.0 is no longer allowed", res["failure_reason"])
        wz._after_execute(s, res)
        self.assertEqual(s["status"], "needs_help")
        self.assertFalse(s["retry"]["awaiting_decision"])


# ---------------------------------------------------------------------------------------------------------------
GENERIC_HTML = """<!doctype html><html><head><title>Volunteer sign-up</title></head><body>
<nav>""" + "".join(f'<a href="/page{i}">Menu item {i}</a>' for i in range(300)) + """</nav>
<form id="signup" action="/signup" method="post">
  <label for="fullname">Full name</label><input id="fullname" name="fullname" required maxlength="40">
  <label>Team <select name="team"><option value="">Choose...</option><option>Kitchen</option><option>Garden</option></select></label>
  <label><input type="radio" name="shift" value="am"> Morning</label><label><input type="radio" name="shift" value="pm"> Evening</label>
  <label>Hours <input type="number" name="hours" min="1" max="8"></label>
  <label>Postcode <input name="postcode" pattern="[0-9]{5}"></label>
  <div role="textbox" contenteditable="true" aria-label="Notes"></div>
  <div role="button" tabindex="0" aria-label="Preview">Preview</div>
  <button type="submit">Sign me up</button>
</form>
<script>axios.post('/api/volunteers', {fullname: n, team: t}); fetch('https://tracker.example.net/t', {method: 'POST'});</script>
</body></html>"""


class TestGenericDiscovery(unittest.TestCase):
    def setUp(self):
        self.inv = dom_discovery.parse_html("https://volunteer.example.org/join", GENERIC_HTML)

    def test_inventory_covers_any_site(self):
        inv = self.inv
        kinds = {}
        for e in inv.elements:
            kinds[e.kind] = kinds.get(e.kind, 0) + 1
        self.assertEqual(kinds["link"], 300)
        radios = [e for e in inv.elements if e.type == "radio"]
        self.assertEqual([r.best_selector() for r in radios], ['input[name="shift"][value="am"]', 'input[name="shift"][value="pm"]'])
        team = inv.find('select[name="team"]')
        self.assertEqual(team.constraints()["options"], ["Kitchen", "Garden"])
        self.assertEqual(inv.find("#fullname").constraints(), {"required": True, "maxlength": "40"})
        self.assertEqual(inv.find('input[name="hours"]').constraints(), {"min": "1", "max": "8", "format": "number"})
        self.assertTrue(inv.find('[aria-label="Notes"]').editable)
        self.assertEqual(inv.find('[aria-label="Preview"]').kind, "button")
        self.assertEqual(inv.forms, [{"index": 0, "id": "signup", "name": "", "action": "/signup", "method": "post"}])
        self.assertEqual([(c["method"], c["path"]) for c in inv.api_calls], [("POST", "/api/volunteers")])  # other hosts ignored

    def test_prompt_inventory_is_ranked_and_bounded(self):
        out = dom_discovery.inventory_for_prompt(self.inv)
        self.assertLessEqual(len(out), 15000)
        data = json.loads(out)
        kinds = [e["kind"] for e in data["elements"]]
        self.assertEqual(kinds[0], "input")                                        # form controls first
        self.assertLessEqual(kinds.count("link"), 30)                              # navigation menus capped
        self.assertIn("omitted", data["note"])

    def test_script_built_pages_are_rendered(self):
        shell = '<html><body><div id="root"></div><script src="/app.js"></script></body></html>'
        rendered = dom_discovery.parse_html("https://spa.example.com/", GENERIC_HTML.replace("<nav>", "<nav hidden>"))
        rendered.renderer = "chromium"
        with mock.patch.object(dom_discovery, "fetch_html", return_value=("https://spa.example.com/", shell)), \
                mock.patch.object(dom_discovery, "render_with_browser", return_value=rendered) as rb:
            inv = dom_discovery.discover("https://spa.example.com/")
        rb.assert_called_once()
        self.assertEqual(inv.renderer, "chromium")
        with mock.patch.object(dom_discovery, "fetch_html", return_value=("https://x.example.com/", GENERIC_HTML)), \
                mock.patch.object(dom_discovery, "render_with_browser") as rb2:
            dom_discovery.discover("https://x.example.com/")
        rb2.assert_not_called()                                                    # a plain HTML form needs no browser


class TestGrounding(unittest.TestCase):
    def setUp(self):
        self.inv = dom_discovery.parse_html("https://volunteer.example.org/join", GENERIC_HTML)

    def analyse(self, steps, params):
        raw = {"steps": steps, "parameters": params, "verification": {"method": "dom_text"}}
        a, _ = watch.normalize_analysis(raw, "https://volunteer.example.org/join")
        rep = watch.ground_analysis(a, self.inv, "https://volunteer.example.org/join")
        return a, rep

    def test_steps_are_checked_and_repaired_against_the_page(self):
        a, rep = self.analyse(
            [{"order": 1, "action": "navigate"},
             {"order": 2, "action": "fill", "selector": "#full-name-input", "label": "Full name", "param_key": "full_name"},
             {"order": 3, "action": "fill", "selector": "select[name=\"team\"]", "label": "Team", "param_key": "team"},
             {"order": 4, "action": "check", "selector": "input[name=\"shift\"][value=\"pm\"]", "label": "Evening",
              "param_key": "evening"},
             {"order": 5, "action": "click", "selector": "button.submit-xyz", "label": "Sign me up"},
             {"order": 6, "action": "navigate", "value": "/thanks"},
             {"order": 7, "action": "click", "selector": "#download-receipt", "label": "Download receipt"}],
            [{"key": "full_name", "label": "Full name", "type": "text", "value": "Ada"},
             {"key": "team", "label": "Team", "type": "text", "value": "garden"},
             {"key": "evening", "label": "Evening", "type": "text", "value": "true"}])
        st = {s["order"]: s for s in a["steps"]}
        self.assertEqual(st[2]["selector"], "#fullname")                          # repaired by label
        self.assertIn("#full-name-input", st[2]["fallback_selectors"])
        self.assertEqual(st[3]["action"], "select")                                # fill on a <select> -> select
        self.assertEqual(st[5]["selector"], 'button:has-text("Sign me up")')
        self.assertTrue(all(st[n]["grounded"] for n in (2, 3, 4, 5)))
        self.assertFalse(st[7]["grounded"])
        self.assertIn("later page", rep["not_found"][0]["why"])
        p = {x["key"]: x for x in a["parameters"]}
        self.assertEqual((p["team"]["type"], p["team"]["value"], p["team"]["constraints"]["options"]),
                         ("select", "Garden", ["Kitchen", "Garden"]))               # type + value snapped to the page
        self.assertEqual(p["evening"]["type"], "boolean")
        self.assertEqual(p["full_name"]["constraints"], {"required": True, "maxlength": "40"})
        self.assertEqual(rep["on_page"], 4)

    def test_payload_schema_checks(self):
        def chk(t, v, c):
            return payload_schema.check_value({"key": "k", "type": t, "value": v, "constraints": c})
        self.assertEqual(chk("select", "Pool", {"options": ["Kitchen", "Garden"]})[0][:10], "must be on")
        self.assertEqual(chk("number", "9", {"min": "1", "max": "8"}), ["must be at most 8"])
        self.assertEqual(chk("text", "x" * 41, {"maxlength": "40"}), ["must be at most 40 characters"])
        self.assertEqual(chk("text", "12ab5", {"pattern": "[0-9]{5}"}), ["does not match the format the page expects"])
        self.assertEqual(chk("text", "", {"required": True}), ["the page marks this field as required"])
        self.assertEqual(chk("text", "aaaa!", {"pattern": "(a+)+$"}), [])         # ReDoS-shaped page patterns are skipped
        self.assertEqual(chk("text", "12345", {"pattern": "[0-9]{5}", "maxlength": "5"}), [])
        schema = payload_schema.schema_json([{"key": "team", "type": "select", "label": "Team", "value": "Garden",
                                              "constraints": {"options": ["Kitchen", "Garden"], "required": True}}])
        self.assertEqual(schema["properties"]["team"]["enum"], ["Kitchen", "Garden"])


class TestPayloadTableUsesThePage(Base):
    def test_values_the_page_would_reject_are_caught_in_step_2(self):
        install_fake(FakeLLM({"WATCH_ANALYZE": VIDEO_ANALYSIS, "BUILD_PARAMETERIZE": RuntimeError("n/a")}))
        s = wz.create_session(TARGET, "", fake_mp4(), "rec.mp4", background=False)
        s = wz.select_framework(s["session_id"], "playwright-python", background=False)
        vendor = next(p for p in s["parameters"] if p["key"] == "vendor_name")
        self.assertEqual(vendor["constraints"], {"required": True})               # read from the page (required)
        params = [dict(p) for p in s["parameters"]]
        for p in params:
            if p["key"] == "vendor_name":
                p["value"] = ""
                p["constraints"] = {}                                              # a client cannot drop the page's rules
        with self.assertRaises(wz.WizardError) as ctx:
            wz.update_parameters(s["session_id"], params)
        self.assertEqual(ctx.exception.details["problems"][0]["pattern"], "PAGE_CONSTRAINT")


# ---------------------------------------------------------------------------------------------------------------
class _Site(BaseHTTPRequestHandler):
    """A small public-style site that is NOT one of the sample portals: a volunteer sign-up form + list."""
    entries = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        rows = "".join(f"<li>{e['fullname']} - {e['team']} - {e.get('hours', '')}h</li>" for e in self.entries)
        body = (f"""<!doctype html><html><head><title>Volunteer sign-up</title></head><body>
<h1>Join us</h1><form method="post" action="/signup">
<label for="vname">Your name</label><input id="vname" name="fullname" required maxlength="40">
<label for="vteam">Team</label><select id="vteam" name="team"><option value="">Choose...</option><option>Kitchen</option><option>Garden</option></select>
<label for="vhours">Hours per week</label><input id="vhours" type="number" name="hours" min="1" max="8" value="2">
<button type="submit">Sign me up</button></form><h2>Volunteers</h2><ul id="list">{rows}</ul></body></html>""").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        q = {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode()).items()}
        self.entries.append(q)
        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()


@unittest.skipUnless(browser_available(), "Playwright Chromium not available")
class TestAnySiteEndToEnd(unittest.TestCase):
    """Offline (no AI) end to end against a site Understudy has never seen: discovery -> plan -> payload schema ->
    build -> govern -> real browser run -> verified -> ZIP."""

    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", cls.port), _Site)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def setUp(self):
        ap.reset_to_defaults()
        install_fake(None)
        os.environ["UNDERSTUDY_DEPENDENCY_INSTALL"] = "dry-run"

    def tearDown(self):
        os.environ.pop("UNDERSTUDY_DEPENDENCY_INSTALL", None)

    def test_offline_pipeline_on_an_unknown_site(self):
        url = f"http://localhost:{self.port}/"
        s = wz.create_session(url, "Team: Garden\nYour name: Grace Hopper", fake_mp4(), "rec.mp4", background=False)
        self.assertEqual(s["job"]["status"], "done", s["job"])
        a = s["analysis"]
        self.assertEqual([st["action"] for st in a["steps"]], ["navigate", "fill", "select", "fill", "click"])
        self.assertTrue(all(st.get("grounded") for st in a["steps"][1:]))
        p = {x["key"]: x for x in s["parameters"]}
        self.assertEqual((p["team"]["type"], p["team"]["value"]), ("select", "Garden"))
        self.assertEqual(p["fullname"]["value"], "Grace Hopper")
        self.assertEqual(p["hours"]["constraints"], {"min": "1", "max": "8", "format": "number"})
        sid = s["session_id"]
        s = wz.select_framework(sid, "playwright-python", background=False)
        params = [dict(x) for x in s["parameters"]]
        for x in params:
            if x["key"] == "hours":
                x["value"] = "12"
        with self.assertRaises(wz.WizardError):                                    # the page allows at most 8
            wz.update_parameters(sid, params)
        for x in params:
            if x["key"] == "hours":
                x["value"] = "5"
        wz.update_parameters(sid, params)
        s = govern(sid)
        self.assertEqual(s["pillars"]["GOVERN"]["status"], "PASS", s.get("compliance"))
        s = wz.build_suite(sid, background=False)
        self.assertEqual(s["pillars"]["BUILD"]["status"], "PASS")
        s = wz.execute(sid, background=False)
        self.assertEqual(s["pillars"]["BUILD-EXECUTE"]["status"], "PASS", s["execution"].get("logs", [])[-8:])
        self.assertEqual(_Site.entries[-1], {"fullname": "Grace Hopper", "team": "Garden", "hours": "5"})
        self.assertEqual(s["pillars"]["EXPORT"]["status"], "PASS")        # 9.0: the ZIP is created right after the run
        z = zipfile.ZipFile(io.BytesIO(wz.export_zip(sid)["data"]))
        schema = json.loads(z.read("payload.schema.json"))
        self.assertEqual(schema["properties"]["team"]["enum"], ["Kitchen", "Garden"])
        self.assertEqual(schema["properties"]["hours"]["maximum"], 8.0)


if __name__ == "__main__":
    unittest.main()
