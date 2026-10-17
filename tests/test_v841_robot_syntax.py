"""8.4.1: the Robot Framework smoke-test loop - "Keyword 'Browser.Fill Text' expected 2 to 3 arguments, got 0".

The AI kept writing a keyword alone on a line with its arguments below it; every retry / rewrite repeated it. Understudy
now repairs that layout itself, rejects what it can't repair before the test runs (with the rule in the rewrite
request), and turns the Robot error into a precise automatic fix."""
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from core.wizard import robot_syntax as rs, setup_doctor
from core.wizard.frameworks import FrameworkSpec, Package
from core.wizard.sandbox import smoke
from core.wizard.sandbox.base import ExecResult

BROKEN = """*** Settings ***
Library    Browser

*** Test Cases ***
Smoke Test UI
    New Browser    chromium    headless=True
    New Page    %{SMOKE_URL}
    Fill Text
    id=name    Understudy
    Click    id=go
    ${status}=    Get Text
    ...    id=status
    Should Be Equal    ${status}    CLICKED:Understudy
    Close Browser
"""

GOOD = """*** Settings ***
Library    Browser

*** Variables ***
${NAME}    Understudy

*** Test Cases ***
Smoke
    New Browser    chromium    headless=True
    New Page    %{SMOKE_URL}
    Fill Text    id=name    ${NAME}
    FOR    ${i}    IN RANGE    2
        Log    ${i}
    END
    My Check    id=status

*** Keywords ***
My Check
    [Arguments]    ${sel}
    Get Text    ${sel}    ==    CLICKED:Understudy
"""


def robot_spec():
    return FrameworkSpec(id="robot-framework", name="Robot Framework", language="python", ecosystem="pip",
                         packages=[Package("robotframework", "7.1"), Package("robotframework-browser", "19.0.0")],
                         run_command=["robot", "{entry}"], entry_file="smoke.robot")


class TestRepair(unittest.TestCase):
    def test_arguments_on_the_next_line_are_joined(self):
        fixed, notes = rs.repair(BROKEN)
        self.assertIn("    Fill Text\n    ...    id=name    Understudy\n", fixed)
        self.assertIn("    Click    id=go", fixed)                        # the next real call is untouched
        self.assertIn("    ...    id=status\n", fixed)                     # an existing continuation is kept
        self.assertEqual(len(notes), 1, notes)
        self.assertEqual(rs.lint(fixed), [])
        self.assertTrue(rs.lint(BROKEN))

    def test_single_spaces_and_nbsp(self):
        fixed, notes = rs.repair("*** Test Cases ***\nT\n    Fill Text id=name Understudy\n    Click  id=go\n")
        self.assertIn("    Fill Text    id=name    Understudy", fixed)
        self.assertIn("    Click  id=go", fixed)
        self.assertEqual(len(notes), 2)

    def test_a_correct_file_is_left_alone(self):
        self.assertEqual(rs.repair(GOOD), (GOOD, []))
        self.assertEqual(rs.lint(GOOD), [])

    def test_lint_explains_the_rule(self):
        p = rs.lint("*** Test Cases ***\nT\n    Fill Text\n    Some User Keyword\n")
        self.assertEqual(len(p), 1)
        self.assertIn("SAME line", p[0])

    @unittest.skipUnless(subprocess.run([sys.executable, "-c", "import robot"], capture_output=True).returncode == 0,
                         "robotframework not installed")
    def test_robot_itself_accepts_the_repaired_file(self):
        d = tempfile.mkdtemp(prefix="rf-")
        with open(os.path.join(d, "Browser.py"), "w") as fh:            # same signatures as the real library
            fh.write("class Browser:\n    ROBOT_LIBRARY_SCOPE = 'GLOBAL'\n"
                     "    def new_browser(self, browser='chromium', headless=True): pass\n"
                     "    def new_page(self, url=None): pass\n"
                     "    def fill_text(self, selector, txt, force=False): pass\n"
                     "    def click(self, selector, button='left'): pass\n"
                     "    def get_text(self, selector, assertion_operator=None, assertion_expected=None):\n"
                     "        return 'CLICKED:Understudy'\n"
                     "    def close_browser(self, browser='CURRENT'): pass\n")
        env = dict(os.environ, SMOKE_URL="http://127.0.0.1/smoke.html", PYTHONPATH=d)
        for text, ok in ((BROKEN, False), (rs.repair(BROKEN)[0], True)):
            with open(os.path.join(d, "smoke.robot"), "w") as fh:
                fh.write(text)
            out = subprocess.run([sys.executable, "-m", "robot", "--output", "NONE", "--report", "NONE", "--log", "NONE",
                                  "smoke.robot"], cwd=d, env=env, capture_output=True, text=True)
            self.assertEqual(out.returncode == 0, ok, out.stdout[-800:])
            if not ok:
                self.assertIn("expected 2 to 3 arguments, got 0", out.stdout)


class TestSetupLoop(unittest.TestCase):
    def test_check_rejects_what_it_cannot_repair(self):
        files = {"smoke.robot": "*** Test Cases ***\nT\n    New Page    %{SMOKE_URL}\n    Fill Text\n    Click    id=go\n"}
        problems = smoke.validate_files(files, robot_spec())
        self.assertTrue(any("'Fill Text' is called with 0 argument(s)" in p for p in problems), problems)
        self.assertIn("SAME line", setup_doctor.rewrite_hints("; ".join(problems), robot_spec()))

    def test_the_robot_error_gets_a_precise_fix_not_a_plain_retry(self):
        out = ("Smoke Test UI    | FAIL |\nKeyword 'Browser.Fill Text' expected 2 to 3 arguments, got 0.\n"
               "1 test, 0 passed, 1 failed")
        fix = setup_doctor.known_fix({"stage": "smoke_test", "message": "exit 1", "output": out}, robot_spec())
        self.assertEqual(fix["key"], "robot-syntax")
        self.assertTrue(fix["needs_review"])
        self.assertIn("expected 2 to 3 arguments, got 0", fix["changes"]["rewrite_smoke"])
        self.assertIn("FOUR spaces", fix["changes"]["rewrite_smoke"])
        self.assertIsNone(setup_doctor.known_fix({"stage": "smoke_test", "output": out}, robot_spec(), tried=["robot-syntax"]))

    def test_the_run_uses_the_repaired_test(self):
        seen = {}

        class Runner:
            kind, scratch = "local", tempfile.mkdtemp()

            def execute_script(self, files, argv, env=None, timeout_s=None, cwd=None, **kw):
                seen.update(files)
                return ExecResult(argv, 0, "1 test, 1 passed", 10, False, False, None, "local")
        res, info = smoke.run(Runner(), robot_spec(), {"smoke.robot": BROKEN})
        self.assertTrue(res.ok)
        self.assertIn("...    id=name    Understudy", seen["smoke.robot"])
        self.assertTrue(info["robot_repairs"])

    def test_ai_written_test_is_repaired_before_review(self):
        from core.wizard import tool_setup
        res = mock.Mock(ok=True, data={"files": [{"path": "smoke.robot", "content": BROKEN}]})
        with mock.patch.object(tool_setup, "_ai_on", return_value=True), \
                mock.patch("core.wizard.llm.generate_json", return_value=res):
            files, source, notes, _ = tool_setup._smoke_files({"setup": {}}, robot_spec(), "local")
        self.assertEqual(source, "ai", notes)
        self.assertIn("...    id=name    Understudy", files["smoke.robot"])
        self.assertTrue(any("Robot Framework layout" in n for n in notes))


if __name__ == "__main__":
    unittest.main()


class TestHashSelectors(unittest.TestCase):
    """8.5.2: "Fill Text    #name    Understudy" - '#' starts a COMMENT in Robot Framework, so the keyword got 0 arguments
    although the line looked right; the AI (told to 'type into #name') kept writing it on every rewrite."""
    SRC = ("*** Settings ***\nLibrary    Browser\n\n*** Test Cases ***\nSmoke Test Browser\n"
           "    New Page    %{SMOKE_URL}\n    Fill Text    #name    Understudy\n    Click    #go\n"
           "    Get Text    #status    ==    CLICKED:Understudy    # the page says so\n    # a real comment\n"
           "    Log    done    # trailing comment\n")

    def test_selectors_are_escaped_and_comments_kept(self):
        fixed, notes = rs.repair(self.SRC)
        self.assertIn("    Fill Text    \\#name    Understudy\n", fixed)
        self.assertIn("    Click    \\#go\n", fixed)
        self.assertIn("    Get Text    \\#status    ==    CLICKED:Understudy    # the page says so\n", fixed)
        self.assertIn("    # a real comment\n", fixed)
        self.assertIn("    Log    done    # trailing comment\n", fixed)
        self.assertEqual(len(notes), 3, notes)
        self.assertEqual(rs.lint(fixed), [])

    def test_lint_explains_the_comment_rule(self):
        p = rs.lint(self.SRC)
        self.assertTrue(p and "COMMENT" in p[0] and "id=name" in p[0], p)
        self.assertIn("COMMENT", rs.RULES)

    def test_check_rejects_and_hints(self):
        files = {"smoke.robot": self.SRC}
        fixed, _ = rs.repair_files(files)
        self.assertEqual(smoke.validate_files(fixed, robot_spec()), [])          # repaired before it is checked
        self.assertTrue(any("COMMENT" in x for x in smoke.validate_files(files, robot_spec())))

    @unittest.skipUnless(subprocess.run([sys.executable, "-c", "import robot"], capture_output=True).returncode == 0,
                         "robotframework not installed")
    def test_robot_itself(self):
        d = tempfile.mkdtemp(prefix="rf-")
        with open(os.path.join(d, "Browser.py"), "w") as fh:
            fh.write("class Browser:\n    ROBOT_LIBRARY_SCOPE = 'GLOBAL'\n"
                     "    def new_page(self, url=None): pass\n"
                     "    def fill_text(self, selector, txt, force=False):\n        assert selector == '#name', selector\n"
                     "    def click(self, selector, button='left'):\n        assert selector == '#go', selector\n"
                     "    def get_text(self, selector, assertion_operator=None, assertion_expected=None):\n"
                     "        assert selector == '#status', selector\n        return 'CLICKED:Understudy'\n")
        env = dict(os.environ, SMOKE_URL="http://127.0.0.1/smoke.html", PYTHONPATH=d)
        for text, ok in ((self.SRC, False), (rs.repair(self.SRC)[0], True)):
            with open(os.path.join(d, "smoke.robot"), "w") as fh:
                fh.write(text)
            out = subprocess.run([sys.executable, "-m", "robot", "--output", "NONE", "--report", "NONE", "--log", "NONE",
                                  "smoke.robot"], cwd=d, env=env, capture_output=True, text=True)
            self.assertEqual(out.returncode == 0, ok, out.stdout[-800:])
