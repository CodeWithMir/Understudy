"""9.9.1: the exported launchers write a report of every run (results/<run id>/understudy-report.html / .json, run.log)."""
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.wizard import dependency_resolver, export_readme, run_report
from core.wizard.frameworks import FrameworkSpec, Package

PASS = """import os
print("GOVERN OK: 1 SOP rule(s) checked against the payload")
print("STEP 1 OK: Open the portal")
print("token", os.environ.get("UNDERSTUDY_SECRET_ERP_TOKEN"))
print('STEP 2 OK: Type </script><b>x</b> "q" back\\\\slash')
"""
FAIL = """import sys
print("GOVERN OK: 0 SOP rule(s)")
print("STEP 1 OK: Open")
print("STEP 2 FAILED: Element not found: #vendor_name")
sys.exit(3)
"""


def spec(eco="none", run=("python3", "{entry}"), entry="run_automation.py", packages=()):
    return FrameworkSpec(id="t", name="Sample (Python)", language="Python", ecosystem=eco,
                         packages=[Package(n, v) for n, v in packages], run_command=list(run), entry_file=entry)


class LauncherFilesTest(unittest.TestCase):
    def test_every_launcher_and_helper_is_shipped(self):
        files = dependency_resolver.setup_scripts(spec("pip", packages=[("selenium", "4.21.0")]))
        self.assertEqual(sorted(files), sorted(["run.sh", "run.ps1", "run.bat", run_report.AWK_FILE,
                                                run_report.TEMPLATE_FILE]))
        sh = files["run.sh"]
        self.assertIn("pip install -r requirements.txt", sh)
        self.assertIn("python3 run_automation.py 2>&1 |", sh)
        self.assertIn(run_report.AWK_FILE, sh)
        self.assertIn("results/latest.html", sh)
        self.assertIn("run.ps1", files["run.bat"])
        self.assertIn("Invoke-Setup 'pip install -r requirements.txt'", files["run.ps1"])
        self.assertIn("@@JSON@@", files[run_report.TEMPLATE_FILE])

    def test_readme_facts_and_check_mention_the_reports(self):
        st = dependency_resolver.launcher_steps(spec("maven", ("mvn", "-q", "test"), "src/test/java/A.java"))
        self.assertEqual((st["sh"], st["run"]), (["mvn -q dependency:resolve"], "mvn -q test"))
        f = {"env_vars": [], "pages": [], "tool": {"run_command": "mvn -q test"}}
        out = export_readme.check("# Title\n\nRun `mvn -q test`.\n", f, [])
        self.assertIn("results/latest.html", out["markdown"])
        self.assertIn("reports", out["added"])


@unittest.skipUnless(shutil.which("bash") and shutil.which("awk"), "bash and awk are needed")
class BashReportTest(unittest.TestCase):
    def package(self, code):
        d = tempfile.mkdtemp(prefix="us-report-")
        self.addCleanup(shutil.rmtree, d, True)
        files = dependency_resolver.setup_scripts(spec())
        files.update({"run_automation.py": code, "governance.json": "{}",
                      "payload.json": json.dumps({"vendor_name": "Acme"}),
                      ".env": "TARGET_URL=http://localhost:8000/portal1\nUNDERSTUDY_SECRET_ERP_TOKEN=S3cretTok\n"})
        for p, c in files.items():
            os.makedirs(os.path.dirname(os.path.join(d, p)) or d, exist_ok=True)
            with open(os.path.join(d, p), "w", newline="") as fh:
                fh.write(c)
        return d

    def run_it(self, d):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("UNDERSTUDY_", "PYTEST_"))}
        res = subprocess.run(["bash", "run.sh"], cwd=d, env=env, capture_output=True, text=True, timeout=120)
        out = glob.glob(os.path.join(d, "results", "*", "understudy-report.json"))
        self.assertEqual(len(out), 1, res.stdout + res.stderr)
        with open(out[0], encoding="utf-8") as fh:
            raw = fh.read()
        return res, json.loads(raw), raw, os.path.dirname(out[0])

    def test_passing_run(self):
        res, r, raw, out = self.run_it(self.package(PASS))
        self.assertEqual((res.returncode, r["status"], r["exit_code"]), (0, "PASS", 0))
        self.assertEqual(r["governance"]["status"], "ok")
        self.assertEqual([(s["n"], s["status"]) for s in r["steps"]], [(1, "OK"), (2, "OK")])
        self.assertIsNone(r["failure"])
        for name in ("understudy-report.html", "run.log"):
            with open(os.path.join(out, name), encoding="utf-8") as fh:
                self.assertNotIn("S3cretTok", fh.read())
        self.assertNotIn("S3cretTok", raw)
        self.assertNotIn("<", raw)                     # the JSON can never close the page's <script> element
        with open(os.path.join(out, "understudy-report.html"), encoding="utf-8") as fh:
            html = fh.read()
        self.assertNotIn("@@JSON@@", html)
        self.assertIn('"status": "PASS"', html)
        self.assertTrue(os.path.exists(os.path.join(os.path.dirname(out), "latest.html")))

    def test_failing_run_keeps_the_exit_code_and_names_the_step(self):
        res, r, _raw, _out = self.run_it(self.package(FAIL))
        self.assertEqual((res.returncode, r["status"], r["exit_code"]), (3, "FAIL", 3))
        self.assertEqual(r["failure"], {"step": 2, "reason": "Element not found: #vendor_name"})
        self.assertIn("FAIL - report:", res.stdout)


@unittest.skipUnless(shutil.which("pwsh"), "PowerShell is needed")
class PowerShellLauncherTest(unittest.TestCase):
    def test_run_ps1_parses(self):
        d = tempfile.mkdtemp(prefix="us-ps-")
        self.addCleanup(shutil.rmtree, d, True)
        p = os.path.join(d, "run.ps1")
        with open(p, "w", newline="") as fh:
            fh.write(dependency_resolver.setup_scripts(spec("npm", ("node", "{entry}"), "run.js"))["run.ps1"])
        cmd = ("$e=$null;$t=$null;[void][System.Management.Automation.Language.Parser]::ParseFile("
               f"'{p}',[ref]$t,[ref]$e);$e.Count")
        res = subprocess.run(["pwsh", "-NoProfile", "-Command", cmd], capture_output=True, text=True, timeout=60)
        self.assertEqual(res.stdout.strip(), "0", res.stdout + res.stderr)


if __name__ == "__main__":
    unittest.main()
