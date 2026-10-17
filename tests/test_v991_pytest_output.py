"""9.9.1: a passing pytest suite must still show its GOVERN / STEP lines, and a missing GOVERN line is never ENVIRONMENT."""
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.wizard import build, executor, governance_runtime as gr, verifier


class PytestOutputTest(unittest.TestCase):
    def test_passing_test_prints_govern_line_with_s(self):
        try:
            import pytest  # noqa: F401
        except ImportError:
            self.skipTest("pytest not installed")
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "test_x.py"), "w").write(
                "import sys\ndef test_ok():\n    sys.__stdout__.write('GOVERN OK: 0 SOP rule(s)\\n')\n    print('STEP 1 OK')\n")
            open(os.path.join(d, "pytest.ini"), "w").write("[pytest]\n")
            env = dict(os.environ, PYTEST_ADDOPTS="-s", PYTHONUNBUFFERED="1")   # same output on every machine
            r = subprocess.run([sys.executable, "-m", "pytest", "test_x.py"], cwd=d, env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("GOVERN OK", r.stdout)
            self.assertIn("STEP 1 OK", r.stdout)
            env.pop("PYTEST_ADDOPTS")         # the bug: without -s a passing test shows nothing
            r = subprocess.run([sys.executable, "-m", "pytest", "test_x.py"], cwd=d, env=env, capture_output=True, text=True)
            self.assertNotIn("GOVERN OK", r.stdout)

    def test_pytest_ini_disables_capture_and_legacy_still_vetted(self):
        self.assertIn("addopts = -s", gr.PYTEST_INI_TEXT)
        self.assertTrue(gr.is_vetted("pytest.ini", gr.PYTEST_INI_TEXT))
        self.assertTrue(gr.is_vetted("pytest.ini", gr.PYTEST_INI_TEXT_V1))

    def test_missing_govern_is_not_environment(self):
        ex = {"status": "FAIL", "governance_missing": True, "failure_reason": "no GOVERN OK"}
        self.assertEqual(verifier.classify_failure(ex, None, "Self-healing: stale chromedriver copies hidden"), "GOVERNANCE")


BAD = """def test_x():
    try:
        # Step 1: Navigate
        print(f"STEP 1 OK: Navigate")
        driver.get(url)

        # Step 2: Fill vendor
        print(f"STEP 2 OK: Enter the vendor name")
        el.send_keys(v)
    except Exception as e:
        print(f"STEP FAILED: {e}")
        sys.exit(1)
"""
GOOD = """def test_x():
    step = 0
    try:
        # Step 1: Navigate
        step = 1
        driver.get(url)
        print(f"STEP 1 OK: Navigate")

        # Step 2: Fill vendor
        step = 2
        el.send_keys(v)
        print(f"STEP 2 OK: Enter the vendor name")
    except Exception as e:
        print(f"STEP {step} FAILED: {e}")
        sys.exit(1)
"""


class StepReportingTest(unittest.TestCase):
    def test_gate_rejects_ok_before_action_and_bare_failed(self):
        probs = build.step_report_problems({"tests/test_selenium.py": BAD})
        self.assertEqual(len(probs), 2, probs)
        self.assertIn("steps 1, 2", probs[0])
        self.assertIn("without a step number", probs[1])

    def test_gate_accepts_ok_after_action(self):
        self.assertEqual(build.step_report_problems({"tests/test_selenium.py": GOOD}), [])

    def test_templates_pass_the_gate(self):
        from core.wizard import templates
        self.assertEqual(build.step_report_problems({"a.py": "# Step 1: go\npage.goto(u)\nok(1, 'go')\n"}), [])

    def test_runtime_premature_ok_then_failure(self):
        st = {"last_ok_step": 0}
        for l in ["GOVERN OK: 0", "STEP 1 OK: nav", "STEP 2 OK: vendor", "STEP 2 FAILED: no element"]:
            executor._parse_step_line(l, st)
        self.assertEqual((st["failed_step"], st["last_ok_step"]), (2, 1))

    def test_runtime_bare_failed_is_a_failure(self):
        st = {"last_ok_step": 0}
        for l in ["STEP 1 OK: nav", "STEP FAILED: Message: no such element"]:
            executor._parse_step_line(l, st)
        self.assertTrue(st["step_failed_unnumbered"])
        self.assertIn("no such element", st["failure_reason"])


if __name__ == "__main__":
    unittest.main()
