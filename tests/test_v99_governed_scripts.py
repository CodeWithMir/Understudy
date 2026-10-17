"""9.9: Govern comes before Build, and the confirmed SOP rules are part of every generated script - traditional or
agentic: governance.json + a vetted checker (Python / JavaScript / Java) called before step 1. The checkers agree with
Understudy's rule engine; a payload that breaks a rule stops the script before it touches the application; the
executor requires the check and does not self-heal a data problem."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests.wizard_helpers import FakeLLM, fake_mp4, govern, install_fake
from tests.test_wizard_media_to_payload import VIDEO_ANALYSIS, WizardTestBase
from core.wizard import build as build_mod, executor, governance, governance_runtime as gr
from core.wizard import session as wz
from core.wizard.frameworks import CATALOG, FrameworkSpec

PARAMS = [{"key": "vendor", "type": "text"}, {"key": "amount", "type": "number"}, {"key": "date", "type": "date"},
          {"key": "code", "type": "text"}, {"key": "note", "type": "text"}, {"key": "pw", "type": "secret"}]


def rule(field, t, **kw):
    r = {"rule_id": "r-" + field + t, "field": field, "bound": True, "rule_type": t, "values": kw.get("values", []),
         "limit": kw.get("limit"), "exclusive": kw.get("exclusive", False), "start_date": kw.get("start_date"),
         "end_date": kw.get("end_date"), "pattern": kw.get("pattern"), "source_text": "", "source": "user",
         "enabled": True}
    r["plain_english"] = governance.plain_english(r)
    return r


RULES = [rule("vendor", "allowed_values", values=["Acme Industrial Supplies", "Globex Corp"]),
         rule("amount", "max", limit=5000), rule("amount", "min", limit=10, exclusive=True),
         rule("date", "date_range", start_date="2026-01-01", end_date="2026-12-31"),
         rule("code", "regex", pattern=r"^INV-\d{4}$"), rule("note", "denied_values", values=["test"]),
         rule("pw", "required")]
OK = {"vendor": "acme  industrial supplies", "amount": "4,999.50", "date": "2026-09-23", "code": "INV-0042",
      "note": "ok", "pw": "x"}
CASES = [OK, dict(OK, vendor="Initech"), dict(OK, amount="5.1k"), dict(OK, amount="10"), dict(OK, date="23/09/2027"),
         dict(OK, date="Sept 3rd, 2026"), dict(OK, code="INV-42"), dict(OK, note="a TEST note"), dict(OK, pw=""),
         dict(OK, amount="abc", date="not a date")]


class TestCheckersAgreeWithTheRuleEngine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = tempfile.mkdtemp()
        with open(os.path.join(cls.d, gr.RULES_FILE), "w") as fh:
            json.dump(gr.document(RULES, PARAMS), fh)
        for name, src in ((gr.PY_FILE, gr.PY_CHECKER), (gr.JS_FILE, gr.JS_CHECKER),
                          (gr.JAVA_CLASS + ".java", gr.JAVA_CHECKER)):
            with open(os.path.join(cls.d, name), "w") as fh:
                fh.write(src)
        cls.java = bool(shutil.which("javac") and shutil.which("java"))
        if cls.java:
            with open(os.path.join(cls.d, "Main.java"), "w") as fh:
                fh.write("public class Main { public static void main(String[] a) throws Exception {\n"
                         "  String s = new String(java.nio.file.Files.readAllBytes(java.nio.file.Paths.get(a[0])));\n"
                         "  try { UnderstudyGovernance.check(UnderstudyGovernance.asMap(new UnderstudyGovernance.Json(s)"
                         ".value())); } catch (IllegalStateException e) { System.exit(1); } } }\n")
            cls.java = subprocess.run(["javac", "UnderstudyGovernance.java", "Main.java"], cwd=cls.d,
                                      capture_output=True).returncode == 0

    def _cases(self):
        for i, c in enumerate(CASES):
            path = os.path.join(self.d, f"p{i}.json")
            with open(path, "w") as fh:
                json.dump(c, fh)
            want = governance.evaluate_compliance(RULES, [dict(p, value=c.get(p["key"])) for p in PARAMS])["passed"]
            yield i, path, want

    def test_python(self):
        for i, path, want in self._cases():
            r = subprocess.run([sys.executable, gr.PY_FILE, path], cwd=self.d, capture_output=True, text=True)
            self.assertEqual(r.returncode == 0, want, (i, r.stdout))
            self.assertIn("GOVERN OK" if want else "GOVERN FAILED", r.stdout)

    def test_javascript(self):
        if not shutil.which("node"):
            self.skipTest("no Node.js")
        for i, path, want in self._cases():
            r = subprocess.run(["node", gr.JS_FILE, path], cwd=self.d, capture_output=True, text=True)
            self.assertEqual(r.returncode == 0, want, (i, r.stdout))

    def test_java(self):
        if not self.java:
            self.skipTest("no JDK")
        for i, path, want in self._cases():
            r = subprocess.run(["java", "-cp", ".", "Main", path], cwd=self.d, capture_output=True, text=True)
            self.assertEqual(r.returncode == 0, want, (i, r.stdout))

    def test_secret_values_come_from_the_environment(self):
        sys.path.insert(0, self.d)
        try:
            import understudy_governance as ug
        finally:
            sys.path.remove(self.d)
        path = os.path.join(self.d, gr.RULES_FILE)
        with mock.patch.dict(os.environ, {"UNDERSTUDY_SECRET_PW": "from-env"}):
            self.assertTrue(ug.check(dict(OK, pw=""), path=path, exit_on_fail=False))
        with self.assertRaisesRegex(RuntimeError, "GOVERN FAILED: .*pw is required"):
            ug.check(dict(OK, pw=""), path=path, exit_on_fail=False)


class TestEveryScriptCarriesTheRules(unittest.TestCase):
    CTX = {"rules": [rule("total_amount", "max", limit=500)], "sop": "The spend limit per invoice is $500."}
    CALLS = {"playwright-python": ("run_automation.py", "govern(payload)", gr.PY_FILE),
             "selenium-python": ("run_automation.py", "govern(payload)", gr.PY_FILE),
             "playwright-js": ("run_automation.js", 'require("./understudy_governance").check(payload)', gr.JS_FILE),
             "puppeteer": ("run_automation.js", 'require("./understudy_governance").check(payload)', gr.JS_FILE),
             "cypress": ("cypress.config.js", 'require("./understudy_governance").checkFile(', gr.JS_FILE),
             "robot-framework": ("automation.robot", "Check Governance    ${payload}", gr.PY_FILE),
             "rest-assured": ("src/test/java/automation/AutomationTest.java", "UnderstudyGovernance.check(payload)",
                              "src/test/java/automation/UnderstudyGovernance.java")}

    def test_templates_check_the_rules_before_step_1(self):
        params = [dict(p) for p in VIDEO_ANALYSIS["parameters"]]
        api = {"url": "http://localhost:8000/api/x", "method": "POST", "body_keys": ["vendor_name"]}
        with mock.patch.object(build_mod, "generate_json",
                               return_value=type("R", (), {"ok": False, "error": "offline", "agent_source": "x"})()):
            for tid, (where, call, checker) in self.CALLS.items():
                spec = FrameworkSpec.from_dict(CATALOG[tid].to_dict())
                out = build_mod.generate_suite(spec, VIDEO_ANALYSIS["steps"], params, api, "http://localhost:8000/portal1",
                                               [], task_context=self.CTX)
                files = out["files"]
                self.assertIn(call, files[where], tid)
                self.assertIn(checker, files, tid)
                self.assertIn('"limit": 500', files[gr.RULES_FILE], tid)
                self.assertTrue(gr.uses_checker(files, spec), tid)
                self.assertFalse([f for f in out["findings"] if f["severity"] == "blocking"], (tid, out["findings"]))
        java = files["src/test/java/automation/UnderstudyGovernance.java"]
        self.assertTrue(java.startswith("package automation;"))

    def test_a_rule_breaking_payload_stops_the_script_before_step_1(self):
        spec = FrameworkSpec.from_dict(CATALOG["playwright-python"].to_dict())
        params = [dict(p) for p in VIDEO_ANALYSIS["parameters"]]
        with mock.patch.object(build_mod, "generate_json",
                               return_value=type("R", (), {"ok": False, "error": "offline", "agent_source": "x"})()):
            out = build_mod.generate_suite(spec, VIDEO_ANALYSIS["steps"], params, None, "http://localhost:9/x", [],
                                           task_context=self.CTX)
        d = tempfile.mkdtemp()
        executor.write_run_dir(d, out["files"], [dict(p, value="9000") if p["key"] == "total_amount" else p
                                                 for p in params])
        r = subprocess.run([sys.executable, "run_automation.py"], cwd=d, capture_output=True, text=True, timeout=60,
                           env=dict(os.environ, TARGET_URL="http://localhost:9/x"))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("GOVERN FAILED: total_amount = 9000 is over the SOP limit", r.stdout)
        self.assertNotIn("STEP 1", r.stdout)                                    # nothing was touched

    def test_the_agent_stops_before_gemini_or_the_browser(self):
        from core.wizard import agentic_script
        d = tempfile.mkdtemp()
        files = {"run_agent.py": agentic_script.render(VIDEO_ANALYSIS["steps"], "Invoice", {}),
                 "understudy_agent.py": agentic_script.runtime_source()}
        files.update(gr.files_for(FrameworkSpec.from_dict(CATALOG["gemini-agent"].to_dict()), self.CTX["rules"],
                                  VIDEO_ANALYSIS["parameters"], "", files, "run_agent.py"))
        executor.write_run_dir(d, files, [dict(p, value="9000") if p["key"] == "total_amount" else dict(p)
                                          for p in VIDEO_ANALYSIS["parameters"]])
        env = {k: v for k, v in os.environ.items() if "GEMINI" not in k and "GOOGLE_API" not in k}
        r = subprocess.run([sys.executable, "run_agent.py"], cwd=d, capture_output=True, text=True, timeout=60,
                           env=dict(env, TARGET_URL="http://localhost:9/x"))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("GOVERN FAILED: total_amount = 9000", r.stdout)
        self.assertNotIn("could not start Gemini", r.stdout)                 # the rules came first (no key needed)


class TestOfflineRuleBinding(unittest.TestCase):
    """Found with the RPA Challenge recording: 'companies' must bind to Company Name (not Role in Company), and a field
    named in plain words binds to the field with that label."""

    def test_binding(self):
        params = [{"key": k, "label": lbl, "type": "text"} for k, lbl in (
            ("phone_number", "Phone Number"), ("role_in_company", "Role in Company"), ("company_name", "Company Name"),
            ("last_name", "Last Name"))]
        rules = governance.parse_sop_rules("Approved companies: assistant, Acme Corp. The Last Name is required.", params)
        self.assertEqual([(r["field"], r["bound"], r["rule_type"]) for r in rules],
                         [("company_name", True, "allowed_values"), ("last_name", True, "required")])


class TestExecutorAndSession(WizardTestBase):
    def test_parse_lines(self):
        st = {}
        executor._parse_step_line("GOVERN OK: 2 SOP rule(s) checked against the payload", st)
        executor._parse_step_line("STEP 1 OK: open", st)
        self.assertTrue(st["governed"])
        late = {}
        executor._parse_step_line("STEP 1 OK: open", late)
        executor._parse_step_line("GOVERN OK: 0", late)
        self.assertFalse(late.get("governed"))                                # the check must come before step 1
        bad = {}
        executor._parse_step_line("GOVERN FAILED: amount over the limit", bad)
        self.assertEqual(bad["governance_failed"], "amount over the limit")

    def test_a_violation_in_the_run_is_not_self_healed(self):
        install_fake(FakeLLM({"WATCH_ANALYZE": VIDEO_ANALYSIS, "BUILD_PARAMETERIZE": RuntimeError("n/a"),
                              "BUILD_GENERATE_SUITE": RuntimeError("n/a"), "GOVERN_EXTRACT_RULES": RuntimeError("n/a"),
                              "GOVERN_COMPLIANCE_REVIEW": RuntimeError("n/a")}))
        s = self.new_session()
        sid = s["session_id"]
        wz.select_framework(sid, "playwright-python", background=False)
        govern(sid)
        s = wz.build_suite(sid, background=False)
        self.assertIn("governance.json", s["suite"]["files"])
        res = {"status": "FAIL", "exit_code": 1, "failed_step": 0, "last_ok_step": 0, "strategy": "framework",
               "failure_category_hint": "POLICY", "governance_violation": True, "duration_ms": 1.0,
               "failure_reason": "The payload breaks the SOP rules: total_amount = 9000 is over the SOP limit"}
        with mock.patch("core.wizard.executor.execute", return_value=res):
            s = wz.execute(sid, background=False)
        self.assertEqual(s["pillars"]["BUILD-EXECUTE"]["status"], "FAIL")
        self.assertEqual(s["status"], "needs_help")
        self.assertFalse(s["retry"].get("awaiting_decision") or s["retry"].get("awaiting_approval"))
        self.assertTrue(any("SOP rules" in n for n in s["notes"]))


if __name__ == "__main__":
    unittest.main()


class TestCheckerFromATestSubfolder(unittest.TestCase):
    """9.9 (found on Windows): an AI-written pytest suite put its test in tests/ - pytest only puts THAT folder on the
    import path, so `from understudy_governance import check` failed (ModuleNotFoundError), and pytest picked up the
    pytest.ini of the folder Understudy runs in."""

    TEST = ("import json, os\n"
            "from understudy_governance import check as govern\n\n"
            "def test_invoice():\n"
            "    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))\n"
            "    payload = json.load(open(os.path.join(here, 'payload.json')))\n"
            "    govern(payload)\n"
            "    print('STEP 1 OK')\n")

    def spec(self):
        import dataclasses
        from core.wizard.frameworks import CATALOG, Package
        base = CATALOG["selenium-python"]
        return dataclasses.replace(base, id="selenium-python-pytest", run_command=["python", "-m", "pytest", "-q", "-s"],
                                   packages=list(base.packages) + [Package("pytest", "8.0.2")])

    def run_suite(self, files, amount):
        import subprocess
        import sys
        outer = tempfile.mkdtemp()
        with open(os.path.join(outer, "pytest.ini"), "w") as fh:            # like Understudy's own pytest.ini above
            fh.write("[pytest]\ntestpaths = somewhere_else\naddopts = --strict-markers\n")
        run = os.path.join(outer, "generated", "attempt-1")
        for p, src in files.items():
            os.makedirs(os.path.dirname(os.path.join(run, p)), exist_ok=True)
            with open(os.path.join(run, p), "w") as fh:
                fh.write(src)
        with open(os.path.join(run, "payload.json"), "w") as fh:
            json.dump({"amount": amount}, fh)
        env = {k: v for k, v in os.environ.items() if not k.startswith("UNDERSTUDY_GOVERNANCE")}
        # -P: like the `pytest` command, the working folder is NOT put on the import path (python -m would hide the bug)
        return subprocess.run([sys.executable, "-P", "-m", "pytest", "-q", "-s", "-p", "no:cacheprovider"], cwd=run,
                              capture_output=True, text=True, env=env, timeout=120)

    def files(self):
        from core.wizard import governance_runtime as gr
        rules = [rule("amount", "max", limit=200, exclusive=True)]
        files = {"tests/test_invoice.py": self.TEST, "requirements.txt": "pytest\n"}
        files.update(gr.files_for(self.spec(), rules, [{"key": "amount", "type": "number"}], "Amount < 200",
                                  files, "tests/test_invoice.py"))
        return files

    def test_suite_gets_a_checker_next_to_the_test_and_its_own_pytest_ini(self):
        from core.wizard import governance_runtime as gr
        files = self.files()
        self.assertEqual(files["tests/understudy_governance.py"], gr.PY_CHECKER)
        self.assertEqual(files["understudy_governance.py"], gr.PY_CHECKER)
        self.assertEqual(files["pytest.ini"], gr.PYTEST_INI_TEXT)
        self.assertTrue(gr.is_vetted("pytest.ini", files["pytest.ini"]))
        self.assertTrue(gr.is_vetted("tests/understudy_governance.py", gr.PY_CHECKER))

    def test_the_test_in_a_subfolder_runs_and_the_rules_hold(self):
        files = self.files()
        ok = self.run_suite(files, 150)
        self.assertEqual(ok.returncode, 0, ok.stdout[-2000:] + ok.stderr[-1000:])
        self.assertIn("GOVERN OK", ok.stdout)
        self.assertIn("STEP 1 OK", ok.stdout)
        bad = self.run_suite(files, 250)
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("GOVERN FAILED", bad.stdout)
        self.assertNotIn("STEP 1 OK", bad.stdout)

    def test_without_the_fix_it_fails_like_on_windows(self):
        files = {k: v for k, v in self.files().items() if k not in ("tests/understudy_governance.py", "pytest.ini")}
        r = self.run_suite(files, 150)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("understudy_governance", r.stdout + r.stderr)

    def test_an_own_pytest_config_is_kept(self):
        from core.wizard import governance_runtime as gr
        files = {"tests/test_invoice.py": self.TEST, "pyproject.toml": "[tool.pytest.ini_options]\naddopts = '-q'\n"}
        self.assertNotIn("pytest.ini", gr.companions(self.spec(), files))

    def test_before_a_run_missing_copies_are_added(self):
        from core.wizard import governance_runtime as gr
        files = self.files()
        files["e2e/test_moved.py"] = self.TEST                     # self-healing moved / added a test
        extra = gr.companions(self.spec(), files)
        self.assertEqual(list(extra), ["e2e/understudy_governance.py"])
        self.assertEqual(gr.companions(self.spec(), dict(files, **extra)), {})


@unittest.skipUnless(shutil.which("javac") and shutil.which("java"), "needs a JDK")
class TestJavaCheckerInAnyPackage(unittest.TestCase):
    """9.9: Selenium Java - the checker is generated in the entry test's package; a test class in ANOTHER package that
    calls UnderstudyGovernance.check(...) gets its own vetted copy, and governance.json is found from a sub-folder."""

    @staticmethod
    def make_class(pkg, name):
        return (f"package {pkg};\n\nimport java.util.Map;\n\npublic class {name} {{\n"
                "  public static void main(String[] a) {\n"
                "    UnderstudyGovernance.check(Map.of(\"amount\", Integer.parseInt(a[0])));\n"
                f"    System.out.println(\"STEP 1 OK {name}\");\n  }}\n}}\n")

    def suite(self):
        files = {"pom.xml": "<project/>",
                 "src/test/java/com/acme/InvoiceTest.java": self.make_class("com.acme", "InvoiceTest"),
                 "src/test/java/com/acme/flows/ApprovalTest.java": self.make_class("com.acme.flows", "ApprovalTest")}
        spec = CATALOG["selenium-java"]
        files.update(gr.files_for(spec, [rule("amount", "max", limit=200, exclusive=True)],
                                  [{"key": "amount", "type": "number"}], "", files,
                                  "src/test/java/com/acme/InvoiceTest.java"))
        return files

    def compile_and_run(self, files, cls, amount, drop=()):
        root = tempfile.mkdtemp()
        srcs = []
        for p, src in files.items():
            if p in drop:
                continue
            full = os.path.join(root, p)
            os.makedirs(os.path.dirname(full) or root, exist_ok=True)
            with open(full, "w") as fh:
                fh.write(src)
            if p.endswith(".java"):
                srcs.append(full)
        out = os.path.join(root, "target", "classes")
        c = subprocess.run(["javac", "-d", out] + srcs, capture_output=True, text=True)
        if c.returncode:
            return c
        # Surefire runs from the project folder; run from target/ to prove governance.json is found from below too
        return subprocess.run(["java", "-cp", out, cls, str(amount)], cwd=os.path.join(root, "target"),
                              capture_output=True, text=True)

    def test_a_copy_in_each_package_that_uses_it(self):
        files = self.suite()
        self.assertIn("src/test/java/com/acme/UnderstudyGovernance.java", files)
        copy = files["src/test/java/com/acme/flows/UnderstudyGovernance.java"]
        self.assertTrue(copy.startswith("package com.acme.flows;"))
        self.assertTrue(gr.is_vetted("src/test/java/com/acme/flows/UnderstudyGovernance.java", copy))
        self.assertEqual(gr.companions(CATALOG["selenium-java"], files), {})          # nothing missing any more

    def test_both_tests_compile_run_and_enforce_the_rules(self):
        files = self.suite()
        for cls in ("com.acme.InvoiceTest", "com.acme.flows.ApprovalTest"):
            ok = self.compile_and_run(files, cls, 150)
            self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
            self.assertIn("GOVERN OK", ok.stdout)
            self.assertIn("STEP 1 OK", ok.stdout)
            bad = self.compile_and_run(files, cls, 250)
            self.assertNotEqual(bad.returncode, 0)
            self.assertIn("GOVERN FAILED", bad.stdout + bad.stderr)
            self.assertNotIn("STEP 1 OK", bad.stdout)

    def test_without_the_copy_the_other_package_does_not_compile(self):
        r = self.compile_and_run(self.suite(), "com.acme.flows.ApprovalTest", 150,
                                 drop=("src/test/java/com/acme/flows/UnderstudyGovernance.java",))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("UnderstudyGovernance", r.stderr)

    def test_an_import_is_respected(self):
        files = {"src/test/java/com/acme/flows/ApprovalTest.java":
                 self.make_class("com.acme.flows", "ApprovalTest").replace(
                     "import java.util.Map;", "import java.util.Map;\nimport com.acme.UnderstudyGovernance;"),
                 "src/test/java/com/acme/UnderstudyGovernance.java": "package com.acme;\n\n" + gr.JAVA_CHECKER}
        self.assertEqual(gr.companions(CATALOG["selenium-java"], files), {})


@unittest.skipUnless(shutil.which("node"), "needs Node.js")
class TestJsCheckerFromAnyFolder(unittest.TestCase):
    """9.9: JavaScript / TypeScript load the checker with a path relative to the test file; Understudy puts the vetted
    checker wherever that path points, and in an ES-module project the .js entry is an ESM wrapper around the .cjs."""

    CJS_TEST = ("const g = require('./understudy_governance');\n"
                "g.checkFile('payload.json');\nconsole.log('STEP 1 OK');\n")
    CJS_UP = ("const { check } = require('../understudy_governance');\nconst fs = require('fs');\n"
              "check(JSON.parse(fs.readFileSync('payload.json', 'utf-8')));\nconsole.log('STEP 1 OK');\n")
    ESM_TEST = ("import { check } from '{spec}';\nimport fs from 'fs';\n"
                "check(JSON.parse(fs.readFileSync('payload.json', 'utf-8')));\nconsole.log('STEP 1 OK');\n")

    def suite(self, files):
        files = dict(files)
        files.update(gr.files_for(CATALOG["playwright-js"] if "playwright-js" in CATALOG else
                                  next(f for f in CATALOG.values() if f.ecosystem == "npm"),
                                  [rule("amount", "max", limit=200, exclusive=True)],
                                  [{"key": "amount", "type": "number"}], "", files, "tests/a.spec.js"))
        return files

    def run_node(self, files, script, amount):
        root = tempfile.mkdtemp()
        for p, src in files.items():
            os.makedirs(os.path.dirname(os.path.join(root, p)) or root, exist_ok=True)
            with open(os.path.join(root, p), "w") as fh:
                fh.write(src)
        with open(os.path.join(root, "payload.json"), "w") as fh:
            json.dump({"amount": amount}, fh)
        return subprocess.run(["node", script], cwd=root, capture_output=True, text=True, timeout=60)

    def check_both(self, files, script):
        ok = self.run_node(files, script, 150)
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        self.assertIn("GOVERN OK", ok.stdout)
        self.assertIn("STEP 1 OK", ok.stdout)
        bad = self.run_node(files, script, 250)
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("GOVERN FAILED", bad.stdout + bad.stderr)
        self.assertNotIn("STEP 1 OK", bad.stdout)

    def test_commonjs_test_in_a_subfolder_with_a_same_folder_path(self):
        files = self.suite({"tests/a.spec.js": self.CJS_TEST})
        self.assertEqual(files["tests/understudy_governance.js"], gr.JS_CHECKER)
        self.check_both(files, "tests/a.spec.js")
        broken = {k: v for k, v in files.items() if k != "tests/understudy_governance.js"}
        self.assertNotEqual(self.run_node(broken, "tests/a.spec.js", 150).returncode, 0)   # the bug without the fix

    def test_commonjs_test_with_a_correct_path_up(self):
        files = self.suite({"tests/b.spec.js": self.CJS_UP})
        self.assertNotIn("tests/understudy_governance.js", files)                # nothing extra needed
        self.check_both(files, "tests/b.spec.js")

    def test_es_module_project(self):
        pkg = json.dumps({"name": "suite", "type": "module"})
        files = self.suite({"package.json": pkg,
                            "tests/c.spec.js": self.ESM_TEST.replace("{spec}", "../understudy_governance.js"),
                            "tests/d.spec.js": self.ESM_TEST.replace("{spec}", "./understudy_governance.js")})
        self.assertEqual(files["understudy_governance.js"], gr.JS_ESM_WRAPPER)
        self.assertEqual(files["understudy_governance.cjs"], gr.JS_CHECKER)
        self.assertEqual(files["tests/understudy_governance.js"], gr.JS_ESM_WRAPPER)
        for f in ("understudy_governance.js", "understudy_governance.cjs", "tests/understudy_governance.cjs"):
            self.assertTrue(gr.is_vetted(f, files[f]), f)
        self.check_both(files, "tests/c.spec.js")
        self.check_both(files, "tests/d.spec.js")
        self.assertEqual(gr.companions(CATALOG["playwright-js"] if "playwright-js" in CATALOG else
                                       next(f for f in CATALOG.values() if f.ecosystem == "npm"), files), {})
