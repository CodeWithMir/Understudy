"""9.9.1: a Java suite keeps the packages its code imports, admin-added packages reach the run, and a Selenium
failure after step 1 is never called a broken runtime."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.wizard import dependency_lock, dependency_resolver, verifier
from core.wizard.frameworks import FrameworkSpec, Package

JAVA = """package automation;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.openqa.selenium.WebDriver;
import org.testng.annotations.Test;
public class AutomationTest {}
"""


def java_spec(extra=()):
    return FrameworkSpec(id="selenium-java-testng", name="Selenium (Java) + TestNG", language="Java", ecosystem="maven",
                         packages=[Package("org.seleniumhq.selenium:selenium-java", "4.51.0"),
                                   Package("org.testng:testng", "7.9.0"), *extra],
                         run_command=["mvn", "-q", "test"], entry_file="src/test/java/automation/AutomationTest.java")


class JavaImportsTest(unittest.TestCase):
    def test_jackson_import_detected_with_pinned_version(self):
        out = dependency_lock.detect(java_spec(), {"src/test/java/automation/AutomationTest.java": JAVA}, [])
        self.assertEqual(out["extras"], [{"name": "com.fasterxml.jackson.core:jackson-databind", "version": "2.17.1",
                                          "why": "imported by the generated code"}])
        self.assertEqual(out["untrusted"], [])

    def test_pom_never_latest_for_known_artifact(self):
        with mock.patch("core.wizard.jdk.release_for", return_value=11):
            pom = dependency_resolver.pom_xml(java_spec([Package("com.fasterxml.jackson.core:jackson-databind", "")]))
        self.assertIn("<artifactId>jackson-databind</artifactId><version>2.17.1</version>", pom)
        self.assertNotIn("LATEST", pom)


class StaleManifestTest(unittest.TestCase):
    def test_run_gets_pom_with_admin_added_package(self):
        from core.wizard import session as wz
        old_spec = java_spec()
        with mock.patch("core.wizard.jdk.release_for", return_value=11):
            files = dependency_lock.apply_to_files(old_spec, {"src/test/java/automation/AutomationTest.java": JAVA})
            self.assertNotIn("jackson", files["pom.xml"])
            s = {"suite": {"files": dict(files)}, "runtime_env": {}}
            logs = []
            new_spec = java_spec([Package("com.fasterxml.jackson.core:jackson-databind", "")])   # what the admin added
            wz._refresh_manifests(s, new_spec, logs.append)
        self.assertIn("jackson-databind</artifactId><version>2.17.1", s["suite"]["files"]["pom.xml"])
        self.assertIn("Updated pom.xml", logs[0])


class ClassifyTest(unittest.TestCase):
    TAIL = ("STEP 6 OK: Click Submit\nSTEP 7 FAILED: Expected condition failed: waiting for ...\n"
            "Driver info: org.openqa.selenium.chrome.ChromeDriver\n"
            "Capabilities {browserName: chrome, chrome: {chromedriverVersion: 154.0.8037.92}}\n")

    def test_failure_after_steps_passed_is_not_environment(self):
        ex = {"status": "FAIL", "last_ok_step": 6, "failed_step": 7, "failure_reason": "Expected condition failed"}
        self.assertEqual(verifier.classify_failure(ex, None, self.TAIL), "SELECTOR_DRIFT")

    def test_driver_problem_before_step_1_is_still_environment(self):
        ex = {"status": "FAIL", "last_ok_step": 0, "failure_reason": "SessionNotCreatedException"}
        self.assertEqual(verifier.classify_failure(ex, None, "SessionNotCreatedException: chromedriver too old"),
                         "ENVIRONMENT")


if __name__ == "__main__":
    unittest.main()
