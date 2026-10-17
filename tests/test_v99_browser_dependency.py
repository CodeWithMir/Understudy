"""v9.9 - a tool setup that needs a browser finds it on Windows / macOS too.

The setup plan names the browser the Linux way ("google-chrome"). On Windows Chrome is
C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe and never on PATH, so the plain PATH lookup said "not installed"
on a machine that has Chrome, and the setup agent kept asking the admin to install it.
"""
import os
import tempfile
import unittest
from unittest import mock

import tests.wizard_helpers  # noqa: F401  (isolated test environment)
from core.wizard import runtime_repair as rr
from core.wizard.frameworks import CATALOG, Package
from core.wizard.sandbox import LocalProcessSandboxRunner

NO_BROWSERS = {"chrome": None, "edge": None, "chromium": None, "firefox": None, "playwright_chromium": None}


def fake_exe(name="chrome.exe"):
    d = tempfile.mkdtemp()
    p = os.path.join(d, name)
    with open(p, "wb") as fh:
        fh.write(b"MZ")
    return p


def spec_with(*packages):
    import dataclasses
    return dataclasses.replace(CATALOG["selenium-python"], packages=list(packages))


class TestBrowserLookup(unittest.TestCase):
    def test_names(self):
        for n in ("google-chrome", "google-chrome-stable", "chrome.exe", "Chrome"):
            self.assertEqual(rr.browser_kind(n), "chrome", n)
        self.assertEqual(rr.browser_kind("microsoft-edge"), "edge")
        self.assertIsNone(rr.browser_kind("java"))

    def test_windows_install_folder_counts(self):
        exe = fake_exe()
        with mock.patch.object(rr, "detect_browsers", return_value=dict(NO_BROWSERS, chrome=exe)):
            self.assertEqual(rr.find_browser("google-chrome"), exe)

    def test_chrome_bin_from_the_admin_wins(self):
        exe = fake_exe()
        with mock.patch.object(rr, "detect_browsers", return_value=NO_BROWSERS):
            self.assertEqual(rr.find_browser("google-chrome", {"CHROME_BIN": exe}), exe)
            self.assertIsNone(rr.find_browser("google-chrome", {"CHROME_BIN": exe + ".missing"}))

    def test_selenium_manager_versions(self):
        self.assertTrue(rr.selenium_manager_fetches_browsers([Package("selenium", "4.51.0")]))
        self.assertTrue(rr.selenium_manager_fetches_browsers([Package("selenium", "")]))
        self.assertFalse(rr.selenium_manager_fetches_browsers([Package("selenium", "4.10.0")]))
        self.assertFalse(rr.selenium_manager_fetches_browsers([Package("pytest", "8.0.2")]))


class TestSetupDependencyCheck(unittest.TestCase):
    def check(self, spec, value="google-chrome", browsers=NO_BROWSERS):
        with mock.patch.object(rr, "detect_browsers", return_value=browsers), \
                mock.patch("core.wizard.sandbox.local.shutil.which", return_value=None):
            return LocalProcessSandboxRunner(spec).check_dependency("binary", value)

    def test_chrome_in_program_files_passes(self):
        exe = fake_exe()
        ok, detail = self.check(spec_with(Package("selenium", "4.10.0")), browsers=dict(NO_BROWSERS, chrome=exe))
        self.assertTrue(ok)
        self.assertEqual(detail, exe)

    def test_no_chrome_but_selenium_manager_downloads_it(self):
        ok, detail = self.check(spec_with(Package("selenium", "4.51.0"), Package("pytest", "8.0.2")))
        self.assertTrue(ok)
        self.assertIn("Selenium Manager", detail)

    def test_no_chrome_and_old_selenium_says_where_it_looked(self):
        ok, detail = self.check(spec_with(Package("selenium", "4.10.0")))
        self.assertFalse(ok)
        self.assertIn("standard install folders", detail)

    def test_other_binaries_unchanged(self):
        ok, detail = self.check(spec_with(Package("selenium", "4.51.0")), value="java")
        self.assertFalse(ok)
        self.assertIn("not installed or not on PATH", detail)


class TestAgentFacts(unittest.TestCase):
    def test_setup_agent_is_told_chrome_is_installed(self):
        from core.wizard import tool_setup
        exe = fake_exe()
        with mock.patch.object(rr, "detect_browsers", return_value=dict(NO_BROWSERS, chrome=exe)):
            facts = tool_setup._server_facts(CATALOG["selenium-python"])
        self.assertTrue(facts["on_path"]["google-chrome"])
        self.assertEqual(facts["browsers_installed"]["chrome"], exe)


if __name__ == "__main__":
    unittest.main()
