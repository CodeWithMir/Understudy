"""One sample test case per configured automation tool (scripts/tool_matrix.py), against the same sample website
(the bundled demo ERP, Portal 1).

Always (fast, no installs): for Playwright Python, Playwright JS, Puppeteer, Cypress, Selenium, Robot Framework,
Python Requests and REST Assured the admin pins one exact version; the user picks the tool without choosing a version;
the admin's version must be the one in the generated manifest, the sandbox install plan and the ZIP.

LIVE (UNDERSTUDY_LIVE_TOOL_TESTS=1, needs network to the package registries): the version is really installed in the
isolated sandbox and checked, the generated suite is run with the tool's own runtime against Portal 1, VERIFY must
prove the record landed in the ERP, and the ZIP must pin the version. A step this machine cannot do (no Chrome +
chromedriver, Cypress binary or Maven Central unreachable) is a skip with the reason - never a pass.
"""
import os
import unittest

from tests.wizard_helpers import LiveServer, install_fake
from scripts import tool_matrix as TM

LIVE = os.environ.get("UNDERSTUDY_LIVE_TOOL_TESTS") == "1"


class ToolMatrixBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        install_fake(None)
        cls.srv = LiveServer.get()
        cls.caps = TM.capabilities() if LIVE else {k: False for k in (
            "node", "java_maven", "pypi", "npm_registry", "maven_central", "cypress_download", "chrome_for_testing")} | {
            "chrome": None, "chromedriver": None, "playwright_chromium": TM.playwright_chromium()}

    def run_tool(self, tool: str):
        case = next(c for c in TM.CASES if c["tool"] == tool)
        res = TM.run_case(case, self.srv.base, LIVE, self.caps, log=lambda *_: None)
        failed = [c for c in res["checks"] if c["status"] == "FAIL"]
        self.assertFalse(failed, "\n".join(f"{c['name']}: {c['detail']}" for c in failed))
        names = {c["name"]: c["status"] for c in res["checks"]}
        for must in ("admin default version applied to the user's tool", "generated manifest pins the admin's version",
                     "sandbox install plan uses the same version", "ZIP manifest pins the same version"):
            self.assertEqual(names.get(must), "PASS", (must, res["checks"]))
        skipped = [c for c in res["checks"] if c["status"] == "SKIPPED"]
        if LIVE and skipped:
            self.skipTest(f"{tool}: version checks passed; {skipped[0]['name']} skipped - {skipped[0]['detail']}")
        return res


class TestEveryToolTakesTheAdminVersion(ToolMatrixBase):
    def test_playwright_python(self):
        self.run_tool("playwright-python")

    def test_playwright_js(self):
        self.run_tool("playwright-js")

    def test_puppeteer(self):
        self.run_tool("puppeteer")

    def test_cypress(self):
        self.run_tool("cypress")

    def test_selenium_python(self):
        self.run_tool("selenium-python")

    def test_robot_framework(self):
        self.run_tool("robot-framework")

    def test_requests_api(self):
        self.run_tool("requests-api")

    def test_rest_assured(self):
        self.run_tool("rest-assured")

    def test_gemini_agent(self):
        self.run_tool("gemini-agent")

    def test_every_whitelisted_tool_has_a_case(self):
        from core.wizard import admin_policy as ap
        ap.reset_to_defaults()
        self.assertEqual(set(ap.whitelist()), {c["tool"] for c in TM.CASES})


if __name__ == "__main__":
    unittest.main()
