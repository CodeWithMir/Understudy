"""9.9.1: Robot Framework suites - local imports resolve, keyword libraries behave, STEP lines are never lost."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.wizard import executor, robot_syntax
from core.wizard.frameworks import FrameworkSpec

# what the user's session generated (tests/test.robot importing the root library one folder too far up)
ROBOT = """*** Settings ***
Library    SeleniumLibrary
Library    ../../run_automation.py

*** Test Cases ***
Run ERP Automation
    Execute Automation Script
"""
LIB_BAD = """import sys
def execute_automation_script():
    print(f'STEP 1 OK: Open')
    sys.exit(1)
"""
LIB_GOOD = """from robot.api import logger
def execute_automation_script():
    logger.console('STEP 1 OK: Open')
    raise AssertionError('Step 2 failed')
"""


class ImportRepairTest(unittest.TestCase):
    def test_wrong_relative_library_path_is_repaired(self):
        files = {"tests/test.robot": ROBOT, "run_automation.py": LIB_GOOD}
        self.assertEqual(len(robot_syntax.lint_imports(files)), 1)
        fixed, notes = robot_syntax.repair_files(files)
        self.assertIn("Library    ../run_automation.py\n", fixed["tests/test.robot"])
        self.assertIn("Library    SeleniumLibrary", fixed["tests/test.robot"])
        self.assertEqual(robot_syntax.lint_imports(fixed), [])
        self.assertTrue(any("relative to the .robot file" for n in notes))

    def test_correct_curdir_and_library_names_are_left_alone(self):
        robot = ("*** Settings ***\nLibrary    SeleniumLibrary\nResource    ${CURDIR}/common.resource\n"
                 "Library    ../lib.py    arg1\n\n*** Test Cases ***\nT\n    No Operation\n")
        files = {"tests/t.robot": robot, "tests/common.resource": "*** Keywords ***\n", "lib.py": ""}
        fixed, notes = robot_syntax.repair_files(files)
        self.assertEqual((fixed, notes), (files, []))
        self.assertEqual(robot_syntax.lint_imports(files), [])

    def test_ambiguous_or_missing_file_is_reported_not_guessed(self):
        robot = "*** Settings ***\nLibrary    helper.py\n\n*** Test Cases ***\nT\n    No Operation\n"
        files = {"suite/t.robot": robot, "a/helper.py": "", "b/helper.py": ""}
        fixed, _notes = robot_syntax.repair_files(files)
        self.assertEqual(fixed["suite/t.robot"], robot)
        self.assertEqual(len(robot_syntax.lint_imports(fixed)), 1)
        files = {"t.robot": "*** Settings ***\nLibrary    gone.py\n"}
        self.assertIn("does not exist in the suite", robot_syntax.lint_imports(files)[0])

    def test_imports_outside_the_settings_table_are_ignored(self):
        robot = "*** Test Cases ***\nT\n    Log    Library    ../../x.py\n"
        self.assertEqual(robot_syntax.lint_imports({"t.robot": robot}), [])


class LibraryRulesTest(unittest.TestCase):
    def test_sys_exit_and_print_markers_are_rejected(self):
        files = {"tests/test.robot": ROBOT.replace("../../", "../"), "run_automation.py": LIB_BAD}
        probs = robot_syntax.library_problems(files)
        self.assertEqual(len(probs), 2, probs)
        self.assertIn("AssertionError", probs[0])
        self.assertIn("logger.console", probs[1])

    def test_logger_console_and_assertion_error_pass(self):
        files = {"tests/test.robot": ROBOT.replace("../../", "../"), "run_automation.py": LIB_GOOD}
        self.assertEqual(robot_syntax.library_problems(files), [])

    def test_print_to_real_stdout_is_fine(self):
        lib = "import sys\ndef k():\n    print('STEP 1 OK: x', file=sys.__stdout__)\n"
        files = {"t.robot": "*** Settings ***\nLibrary    lib.py\n", "lib.py": lib}
        self.assertEqual(robot_syntax.library_problems(files), [])


class OutputXmlFallbackTest(unittest.TestCase):
    def test_step_lines_printed_into_robots_log_are_read_back(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "output.xml"), "w", encoding="utf-8") as fh:
            fh.write('<robot><test><kw><msg time="t" level="INFO">GOVERN OK: 0 SOP rule(s)\nSTEP 1 OK: Open &amp; go'
                     '</msg><msg time="t" level="INFO">STEP 2 FAILED: no &lt;input&gt;</msg></kw></test></robot>')
        state, logs = {"last_ok_step": 0}, []
        spec = FrameworkSpec(id="r", name="Robot", language="Python", ecosystem="pip", packages=[],
                             run_command=["robot", "{entry}"], entry_file="tests/test.robot")
        self.assertTrue(executor._is_robot(spec))
        executor._robot_markers(d, state, logs.append)
        self.assertTrue(state["governed"])
        self.assertEqual((state["last_ok_step"], state["failed_step"], state["failure_reason"]), (1, 2, "no <input>"))
        self.assertTrue(any("logger.console" in line for line in logs))


if __name__ == "__main__":
    unittest.main()
