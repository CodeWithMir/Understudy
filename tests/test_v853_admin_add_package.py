"""8.5.3: a runtime-help request "Please add 'robotframework-jsonlibrary' ..." - the admin adds the package with one
click (no AI needed): the approved tool and the user's session get it, the next run installs it. Robot ``Library``
imports are detected when the suite is built."""
import unittest

from tests.test_v83_fixes import EscalationBase
from tests.wizard_helpers import admin_client
from core.wizard import admin_policy as ap, dependency_lock, escalations, notifications, session as wz
from core.wizard.frameworks import FrameworkSpec, Package

ASK = ("Please add 'robotframework-jsonlibrary' to the list of required packages for the Robot Framework environment "
       "to resolve the ModuleNotFoundError.")


class TestAddPackage(EscalationBase):
    def test_one_click_adds_the_package_to_tool_and_session(self):
        s = self._verify_fails()
        tool = s["framework"]["id"]
        esc = escalations.create_help(s, {"action": "server_change", "request": ASK}, "why",
                                      "Importing library 'JSONLibrary' failed: ModuleNotFoundError")
        self.assertEqual(esc["admin_action"]["action"], "add_package")
        self.assertEqual(esc["admin_action"]["package"], "robotframework-jsonlibrary")
        c = admin_client()
        r = c.post(f"/api/admin/escalations/{esc['id']}/decide", json={"decision": "confirm"})
        self.assertEqual(r.status_code, 200, r.text)
        names = [p["name"] for p in ap.whitelist()[tool]["spec"]["packages"]]
        self.assertIn("robotframework-jsonlibrary", names)
        s2 = wz.get(self.sid)
        for key in ("framework", "framework_base"):
            if s2.get(key):
                self.assertIn("robotframework-jsonlibrary", [p["name"] for p in s2[key]["packages"]], key)
        owner = s2.get("owner") or "local"
        self.assertIn("added robotframework-jsonlibrary", notifications.items(owner)[0]["title"])

    def test_admin_action_from_the_fix_panel_and_validation(self):
        s = self._verify_fails()
        esc = escalations.create_help(s, {"action": "server_change", "request": "Configure something"}, "x")
        self.assertEqual(esc["admin_action"]["action"], "server_change")
        c = admin_client()
        bad = c.post(f"/api/admin/escalations/{esc['id']}/decide",
                     json={"decision": "confirm", "actions": [{"type": "add_package", "name": "bad name; rm -rf"}]})
        self.assertEqual(bad.status_code, 422, bad.text)
        ok = c.post(f"/api/admin/escalations/{esc['id']}/decide",
                    json={"decision": "confirm", "actions": [{"type": "add_package", "name": "robotframework-requests"}]})
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertIn("added robotframework-requests", ok.json()["escalation"]["admin_actions_done"][0])


class TestAnyEcosystem(unittest.TestCase):
    def test_package_names_of_every_ecosystem_are_recognised_and_validated(self):
        from core.wizard import setup_doctor
        cases = {"pip": "pytest-html", "npm": "@wdio/allure-reporter", "maven": "com.fasterxml.jackson.core:jackson-databind",
                 "gradle": "org.testng:testng", "gem": "rspec-expectations", "go": "github.com/stretchr/testify",
                 "nuget": "NUnit3TestAdapter"}
        for eco, name in cases.items():
            self.assertEqual(escalations._package_in(f"Please add '{name}' to the {eco} packages"), name, eco)
            spec = FrameworkSpec(id="x", name="X", language="", ecosystem=eco, packages=[Package("core", "")],
                                 run_command=["python", "{entry}"], entry_file="a")
            probs = []
            self.assertTrue(setup_doctor.clean_add_packages([{"name": name}], spec, probs), (eco, probs))
        self.assertEqual(escalations._package_in("Configure the proxy on the server"), "")


class TestRobotLibraryDetection(unittest.TestCase):
    def test_library_lines_are_dependencies(self):
        spec = FrameworkSpec(id="robot-framework", name="Robot Framework", language="python", ecosystem="pip",
                             packages=[Package("robotframework", "7.1")], run_command=["robot", "{entry}"],
                             entry_file="smoke.robot")
        files = {"smoke.robot": "*** Settings ***\nLibrary    JSONLibrary\nLibrary    Collections\n"
                                "Library    RequestsLibrary\nLibrary    ./helpers.py\n"}
        found = dependency_lock.scan_imports("pip", files)
        self.assertEqual(found, ["robotframework-jsonlibrary", "robotframework-requests"])
        lock = dependency_lock.detect(spec, files)
        extras = [e["name"] for e in lock["extras"]]
        self.assertIn("robotframework-jsonlibrary", extras)              # installed with the suite - no run-time error
        self.assertEqual(lock["untrusted"], [])
        eff = dependency_lock.effective_spec(spec, lock["extras"])
        self.assertIn("robotframework-jsonlibrary", [p.name for p in eff.packages])


if __name__ == "__main__":
    unittest.main()
