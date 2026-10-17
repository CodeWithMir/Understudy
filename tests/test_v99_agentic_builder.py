"""9.9: agentic tools on Understudy's own agent, set up by the admin like a traditional tool stack - the browser library
(Playwright / Selenium), the browser, versions and extra packages. Install in the sandbox -> Understudy's agent smoke
test (self-test mode, no Gemini key) -> ACTIVE. Plus the runtime's browser drivers, and no final screenshot check."""
import functools
import http.server
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from tests.wizard_helpers import FakeLLM, LiveServer, admin_client, browser_available, fake_mp4, install_fake, govern
from tests.test_wizard_media_to_payload import VIDEO_ANALYSIS
from core.wizard import admin_policy as ap, agent_stacks, agentic_script, difficulty, exporter
from core.wizard import session as wz
from core.wizard.frameworks import CATALOG, FrameworkSpec
from core.wizard.sandbox import smoke


def run_selftest(spec, extra_env=None):
    """Runs Understudy's agent smoke test for ``spec`` against the smoke page (no sandbox) -> (exit code, output)."""
    d = tempfile.mkdtemp()
    smoke.write_assets(d)
    for k, v in agent_stacks.smoke_files(spec).items():
        with open(os.path.join(d, k), "w", encoding="utf-8") as fh:
            fh.write(v)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
                                          functools.partial(smoke._Quiet, directory=os.path.join(d, "smoke")))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    env = dict(os.environ, SMOKE_URL=f"http://127.0.0.1:{srv.server_address[1]}/smoke.html",
               NO_PROXY="127.0.0.1,localhost", **agent_stacks.run_env(spec), **(extra_env or {}))
    try:
        r = subprocess.run([sys.executable, "run_agent.py"], cwd=d, env=env, capture_output=True, text=True, timeout=180)
    finally:
        srv.shutdown()
    return r.returncode, r.stdout + r.stderr


def selenium_chrome_env():
    """Selenium + Chrome on this machine, or None (then the Selenium tests skip). UNDERSTUDY_TEST_CHROMEDRIVER_DIR /
    CHROME_BINARY point at a matching driver / browser when the machine's defaults don't match."""
    try:
        import selenium  # noqa: F401
    except ImportError:
        return None
    env = {}
    if os.environ.get("UNDERSTUDY_TEST_CHROMEDRIVER_DIR"):
        env["PATH"] = os.environ["UNDERSTUDY_TEST_CHROMEDRIVER_DIR"] + os.pathsep + os.environ.get("PATH", "")
    probe = ("from selenium import webdriver\no = webdriver.ChromeOptions()\n"
             "[o.add_argument(a) for a in ('--headless=new', '--no-sandbox', '--disable-dev-shm-usage')]\n"
             "import os\nb = os.environ.get('CHROME_BINARY')\nif b: o.binary_location = b\n"
             "webdriver.Chrome(options=o).quit()\n")
    try:
        ok = subprocess.run([sys.executable, "-c", probe], env=dict(os.environ, **env), capture_output=True,
                            timeout=60).returncode == 0
    except Exception:
        ok = False
    return env if ok else None


class TestCompose(unittest.TestCase):
    def test_stack_becomes_an_agentic_tool_on_understudys_agent(self):
        s = agent_stacks.compose("selenium", "edge", driver_version="4.25.0", extra_packages=["requests==2.32.3"],
                                 model="gemini-2.5-flash")
        self.assertEqual((s.id, s.category, s.template, s.entry_file), ("understudy-agent-selenium-edge", "agentic",
                                                                        "gemini-agent", "run_agent.py"))
        self.assertEqual([(p.name, p.version) for p in s.packages],
                         [("selenium", "==4.25.0"), ("google-genai", ">=1.0.0"), ("requests", "==2.32.3")])
        self.assertEqual(s.agent, {"driver": "selenium", "browser": "edge", "model": "gemini-2.5-flash"})
        self.assertEqual(s.post_install, [])                                   # Selenium Manager finds the driver
        pw = agent_stacks.compose("playwright", "firefox", name="Agent Firefox")
        self.assertEqual(pw.id, "understudy-agent-firefox")
        self.assertEqual(pw.post_install, [["python", "-m", "playwright", "install", "firefox"]])
        self.assertEqual(agent_stacks.compose("playwright", "msedge").post_install, [])   # the machine's own Edge
        self.assertTrue(agent_stacks.is_understudy_agent(s) and agent_stacks.is_understudy_agent(CATALOG["gemini-agent"]))
        self.assertEqual(agent_stacks.run_env(s), {"UNDERSTUDY_AGENT_DRIVER": "selenium", "UNDERSTUDY_AGENT_BROWSER": "edge"})
        self.assertEqual(agent_stacks.run_env(CATALOG["gemini-agent"]),
                         {"UNDERSTUDY_AGENT_DRIVER": "playwright", "UNDERSTUDY_AGENT_BROWSER": "chromium"})
        self.assertEqual(FrameworkSpec.from_dict(s.to_dict()).agent, s.agent)

    def test_bad_input_is_refused(self):
        for args, kw, want in ((("puppeteer", "chrome"), {}, "browser library"),
                               (("selenium", "webkit"), {}, "works with"),
                               (("playwright", "chromium"), {"extra_packages": ["rm -rf /"]}, "not a pip package"),
                               (("playwright", "chromium"), {"driver_version": "latest; curl x"}, "not a version"),
                               (("playwright", "chromium"), {"model": "../../etc"}, "not a Gemini model"),
                               (("playwright", "chromium"), {"extra_packages": [f"p{i}" for i in range(9)]}, "At most")):
            with self.assertRaisesRegex(agent_stacks.StackError, want):
                agent_stacks.compose(*args, **kw)

    def test_options_offer_both_libraries(self):
        o = agent_stacks.options()
        self.assertEqual({d["id"] for d in o["drivers"]}, {"playwright", "selenium"})
        self.assertEqual(o["language"], "Python")


class TestRuntimeDrivers(unittest.TestCase):
    """The agent smoke test proves a stack end to end (snapshot, typing, clicking, page text) without Gemini."""

    def test_self_test_on_playwright_chromium(self):
        if not browser_available():
            self.skipTest("no Playwright browser")
        code, out = run_selftest(agent_stacks.compose("playwright", "chromium"))
        self.assertEqual(code, 0, out[-2000:])
        self.assertIn("playwright / chromium, SELF-TEST", out)
        self.assertIn("AGENT step 2: fill 'Name' <- name", out)
        self.assertIn("STEP 4 OK: Check the result", out)
        self.assertNotIn("VERIFY", out)

    def test_self_test_on_selenium_chrome(self):
        env = selenium_chrome_env()
        if env is None:
            self.skipTest("Selenium + Chrome are not usable on this machine")
        code, out = run_selftest(agent_stacks.compose("selenium", "chrome"), env)
        self.assertEqual(code, 0, out[-2000:])
        self.assertIn("selenium / chrome, SELF-TEST", out)
        self.assertIn("AUTOMATION COMPLETE", out)

    def test_selenium_guards_and_secret_masking(self):
        env = selenium_chrome_env()
        if env is None:
            self.skipTest("Selenium + Chrome are not usable on this machine")
        with mock.patch.dict(os.environ, env):
            from core.wizard import agent_runtime as eng
            d = eng.SeleniumDriver.start("chrome")
            try:
                d.goto("data:text/html,<label for=q>Search</label><input id=q><label for=t>Token</label>"
                       "<input id=t><button>Go</button>")
                secrets = {"token": "S3cret-Token-For-ERP"}
                els = eng.snapshot(d, secrets)
                tok = next(e["id"] for e in els if e["name"] == "Token")
                eng.perform(d, {"action": "fill", "element": tok, "value_key": "token"},
                            {"order": 2, "action": "fill", "param_key": "token"}, {}, secrets, {e["id"] for e in els})
                self.assertEqual(next(e for e in eng.snapshot(d, secrets) if e["id"] == tok)["value"], "(filled)")
                with self.assertRaisesRegex(RuntimeError, "not allowed for a 'click' step"):
                    eng.perform(d, {"action": "fill", "element": tok, "value_key": "token"},
                                {"order": 3, "action": "click"}, {}, secrets, {e["id"] for e in els})
                with self.assertRaisesRegex(RuntimeError, "not one of the elements"):
                    eng.perform(d, {"action": "click", "element": "0.99"}, {"order": 3, "action": "click"}, {}, secrets,
                                {e["id"] for e in els})
            finally:
                d.close()


class TestNoFinalScreenshotCheck(unittest.TestCase):
    def test_scripts_end_with_the_last_step(self):
        code = agentic_script.render(VIDEO_ANALYSIS["steps"], "Invoice", {"method": "visual", "success_text": "x"})
        self.assertNotIn("verify", code)
        self.assertEqual(agentic_script.check_script(code, VIDEO_ANALYSIS["steps"]), [])
        bad = code.replace("\n\n\nif __name__", '\n        agent.verify("saved")\n\n\nif __name__')
        self.assertTrue(agentic_script.check_script(bad, VIDEO_ANALYSIS["steps"]))
        from core.wizard import agent_runtime
        self.assertFalse(hasattr(agent_runtime.Agent, "verify"))


class TestAdminBuilder(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        install_fake(None)
        cls.srv = LiveServer.get()

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"UNDERSTUDY_DEFAULT_TOOLS": "none",
                                                "UNDERSTUDY_DEPENDENCY_INSTALL": "dry-run"})
        self.env.start()
        ap.reset_to_defaults()
        self.admin = admin_client()

    def tearDown(self):
        install_fake(None)
        self.env.stop()
        ap.reset_to_defaults()

    def test_options_and_validation_through_the_api(self):
        r = self.admin.get("/api/admin/agentic-tools/options")
        self.assertEqual(r.status_code, 200)
        self.assertIn("selenium", {d["id"] for d in r.json()["drivers"]})
        r = self.admin.post("/api/admin/agentic-tools", json={"driver": "selenium", "browser": "webkit"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("works with", r.json()["detail"]["message"])

    def test_admin_sets_up_a_selenium_edge_agent_and_users_get_it(self):
        r = self.admin.post("/api/admin/agentic-tools?wait=true",
                            json={"driver": "selenium", "browser": "edge", "name": "Agent on Edge",
                                  "driver_version": "4.25.0"})
        self.assertEqual(r.status_code, 200, r.text)
        req = r.json()["request"]
        self.assertEqual(req["status"], "approved", req.get("admin_note"))       # dry-run: installed nothing, skipped
        tid = "understudy-agent-on-edge"
        e = ap.whitelist()[tid]
        self.assertEqual(e["spec"]["agent"], {"driver": "selenium", "browser": "edge"})
        self.assertEqual(e["setup"]["runtime"]["env_vars"]["UNDERSTUDY_AGENT_DRIVER"], "selenium")
        self.assertEqual(ap.tool_version_policy(tid)["allowed"], ["4.25.0"])
        r = self.admin.post("/api/admin/agentic-tools", json={"driver": "selenium", "browser": "edge",
                                                              "name": "Agent on Edge"})
        self.assertEqual(r.status_code, 409)                                      # the name is taken
        # users: the admin's agentic tool - the built-in one gives way to it (9.9), nothing else agentic
        s = wz.create_session(self.srv.base + "/portal1", "", fake_mp4(), "rec.mp4", background=False)
        agentic = [o["id"] for o in s["framework_options"] if o.get("category") == "agentic"]
        self.assertEqual(sorted(agentic), [tid])
        lists = s["difficulty"]["recommendation"]["agentic"]["tools"]
        self.assertEqual(sorted(t["tool_id"] for t in lists), [tid])
        why = next(t for t in lists if t["tool_id"] == tid)["why"]
        self.assertIn("selenium / edge", why)
        # its build is the agentic script + the same runtime; the ZIP's .env.example names the stack
        s = wz.select_framework(s["session_id"], tid, background=False)
        govern(s["session_id"])
        s = wz.build_suite(s["session_id"], background=False)
        self.assertEqual(sorted(s["suite"]["files"]), ["governance.json", "requirements.txt", "run_agent.py", "understudy_agent.py", "understudy_governance.py"])
        self.assertIn("selenium", s["suite"]["files"]["requirements.txt"])
        env = exporter.env_example_lines(s, wz._spec(s))
        self.assertIn("UNDERSTUDY_AGENT_DRIVER=selenium", env)
        self.assertIn("UNDERSTUDY_AGENT_BROWSER=edge", env)

    def test_the_agent_smoke_test_runs_for_real(self):
        if not browser_available():
            self.skipTest("no Playwright browser")
        with mock.patch.dict(os.environ, {"UNDERSTUDY_DEPENDENCY_INSTALL": "auto"}):
            r = agent_stacks.create("admin", "playwright", "chromium", name="PW Chromium", background=False)
        smoke_rec = r["setup"]["smoke"]
        self.assertEqual((r["status"], smoke_rec["status"], smoke_rec["source"]),
                         ("approved", "PASSED", "understudy-agent"), smoke_rec.get("output"))
        self.assertIn("STEP 4 OK", smoke_rec["output"])
        self.assertTrue(ap.whitelist()["understudy-agent-pw-chromium"]["setup"]["verified"])

    def test_tools_from_outside_understudys_agent_stay_out(self):
        lists = difficulty.tool_lists([{"id": "browser-use", "name": "Browser Use", "category": "agentic"},
                                       dict(agent_stacks.compose("selenium", "chrome").to_dict())], {}, {})
        self.assertEqual(sorted(t["tool_id"] for t in lists["agentic"]),
                         ["understudy-agent-selenium-chrome"])                 # 9.9: the built-in one gives way
        alone = difficulty.tool_lists([{"id": "browser-use", "name": "Browser Use", "category": "agentic"}], {}, {})
        self.assertEqual([t["tool_id"] for t in alone["agentic"]], ["gemini-agent"])   # the fallback


if __name__ == "__main__":
    unittest.main()


class TestBuiltinGivesWayToTheAdminsAgent(unittest.TestCase):
    """9.9: once the admin set up an agentic tool on Understudy's agent, the built-in Understudy Gemini Agent is no
    longer listed for users (it stays the fallback when there is none)."""

    def setUp(self):
        from core.wizard import admin_policy as ap
        self.ap = ap
        ap.reset_to_defaults()
        self.opts = [{"id": "playwright-python", "name": "Playwright", "category": "browser", "template": "playwright-python"}]

    def tearDown(self):
        self.ap.reset_to_defaults()

    def own_tool(self):
        spec = agent_stacks.compose("playwright", "chromium", name="Understudy Agent (Playwright + Chromium)")
        self.ap.whitelist_tool(spec)
        return spec.id

    def agentic_ids(self, user="carol", current=None):
        from core.wizard import difficulty
        from core.wizard.session import annotate_tools
        opts = annotate_tools(list(self.opts), user, current)
        lists = difficulty.tool_lists([dict(o) for o in opts], {}, {})
        return ([o["id"] for o in opts if (o.get("category") or "") == "agentic"],
                [t["tool_id"] for t in lists["agentic"]])

    def test_builtin_only_without_an_admin_agent(self):
        self.assertFalse(self.ap.builtin_agent_hidden("carol"))
        opts, lists = self.agentic_ids()
        self.assertIn("gemini-agent", opts)
        self.assertEqual(lists, ["gemini-agent"])

    def test_the_admins_agent_replaces_the_builtin(self):
        tid = self.own_tool()
        self.assertTrue(self.ap.builtin_agent_hidden("carol"))
        opts, lists = self.agentic_ids()
        self.assertEqual(opts, [tid])
        self.assertEqual(lists, [tid])

    def test_a_session_already_on_the_builtin_keeps_it(self):
        self.own_tool()
        opts, _ = self.agentic_ids(current="gemini-agent")
        self.assertIn("gemini-agent", opts)

    def test_builtin_comes_back_when_the_admins_agent_is_closed_to_the_user(self):
        tid = self.own_tool()
        self.ap.set_tool_access(tid, "none")
        self.assertFalse(self.ap.builtin_agent_hidden("carol"))
        self.assertIn("gemini-agent", self.agentic_ids()[0])
        self.ap.set_tool_access(tid, "selected", ["alice"])
        self.assertTrue(self.ap.builtin_agent_hidden("alice"))
        self.assertFalse(self.ap.builtin_agent_hidden("carol"))

    def test_older_assessments_are_filtered_when_shown(self):
        from core.wizard import session as wz
        self.own_tool()
        s = {"session_id": "x", "owner": "carol", "framework_options": [], "difficulty": {"recommendation": {
            "agentic": {"tools": [{"tool_id": "gemini-agent"}, {"tool_id": "agent-x"}]}}}}
        v = wz.public_view(s)
        self.assertEqual([t["tool_id"] for t in v["difficulty"]["recommendation"]["agentic"]["tools"]], ["agent-x"])
