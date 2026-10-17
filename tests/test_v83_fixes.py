"""8.3: AI-guided runtime installs (Ruby / Go / .NET / conda-forge), the setup doctor (companion packages, automatic
fixes for the smoke-test loops reported on 8.2, executable option payloads), escalation of a failed verification to
the admin with attachments, and user notifications."""
import hashlib
import io
import json
import os
import tarfile
import tempfile
import unittest
from unittest import mock

from tests.wizard_helpers import FakeLLM, admin_client, install_fake
from core.wizard import admin_policy as ap, events, notifications, package_managers as pms, runtime_advisor, runtimes
from core.wizard import setup_doctor, tool_setup, tool_versions
from core.wizard.frameworks import FrameworkSpec, Package
from core.wizard.sandbox.base import ExecResult


def _tgz(files):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, (content, mode) in files.items():
            data = content.encode()
            ti = tarfile.TarInfo(name)
            ti.size, ti.mode = len(data), mode
            t.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


# ---------------------------------------------------------------------------------------------------------------
# 1. Runtimes Understudy used to say it "can't download"
# ---------------------------------------------------------------------------------------------------------------
class TestRuntimeDownloads(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="runtimes-")
        self.p = mock.patch.object(runtimes, "RUNTIMES_DIR", self.dir)
        self.p.start()
        ap.reset_to_defaults()

    def tearDown(self):
        self.p.stop()
        runtimes.set_transport(None)
        ap.reset_to_defaults()

    def test_keys(self):
        for k in ("ruby-3", "go-1", "dotnet-8", "conda-php", "conda-ruby@3.3"):
            self.assertTrue(runtimes.is_supported(k), k)
        for k in ("conda-evil", "ruby-9", "conda-../x", "conda-ruby@3;rm"):
            self.assertFalse(runtimes.is_supported(k), k)
        self.assertEqual(runtimes.kind_of("conda-nodejs"), "node")
        self.assertEqual(runtimes.kind_of("ruby-3"), "ruby")

    def test_gem_and_go_rows_offer_a_download_instead_of_manual_install(self):
        rows = {r["id"]: r for r in pms.status()["package_managers"]}
        self.assertEqual([d["key"] for d in rows["gem"]["downloads"]], ["ruby-3"])
        self.assertEqual([d["key"] for d in rows["go"]["downloads"]], ["go-1"])
        self.assertEqual([d["key"] for d in rows["nuget"]["downloads"]], ["dotnet-8"])

    def test_go_download_is_checksum_verified_and_registered(self):
        script = "#!/bin/sh\necho 'go version go1.23.4 linux/amd64'\n"
        archive = _tgz({"go/bin/go": (script, 0o755), "go/VERSION": ("go1.23.4", 0o644)})
        sha = hashlib.sha256(archive).hexdigest()
        osn, arch = runtimes._os_arch()
        goos = {"linux": "linux", "mac": "darwin", "windows": "windows"}[osn]
        listing = [{"version": "go1.23.4", "stable": True, "files": [
            {"filename": "go1.23.4.x.tar.gz", "os": goos, "arch": "arm64" if arch == "aarch64" else "amd64",
             "kind": "archive", "sha256": sha, "version": "go1.23.4"}]}]

        def transport(url):
            if url == runtimes.GO_DL:
                return json.dumps(listing).encode()
            if url.endswith("go1.23.4.x.tar.gz"):
                return archive
            raise AssertionError(url)
        runtimes.set_transport(transport)
        if osn == "windows":
            self.skipTest("tar.gz layout is the POSIX one")
        st = pms.download("go-1", "admin", background=False)
        job = st["jobs"]["go-1"]
        self.assertEqual(job["status"], "done", job)
        self.assertTrue(job["verified"]["go"]["found"])
        self.assertIn("go", ap.toolchains())
        self.assertEqual(ap.toolchains()["go"]["env_name"], "GOROOT")

    def test_bad_checksum_is_refused_and_diagnosed(self):
        archive = _tgz({"go/bin/go": ("#!/bin/sh\n", 0o755)})
        osn, arch = runtimes._os_arch()
        goos = {"linux": "linux", "mac": "darwin", "windows": "windows"}[osn]
        listing = [{"stable": True, "files": [{"filename": "g.tar.gz", "os": goos, "arch": "arm64" if arch == "aarch64"
                                               else "amd64", "kind": "archive", "sha256": "0" * 64, "version": "go1"}]}]
        runtimes.set_transport(lambda url: json.dumps(listing).encode() if url == runtimes.GO_DL else archive)
        st = pms.download("go-1", "admin", background=False)
        job = st["jobs"]["go-1"]
        self.assertEqual(job["status"], "failed")
        self.assertIn("Checksum mismatch", job["error"])
        self.assertIn("diagnosis", job)
        self.assertNotIn("go", ap.toolchains())

    def test_rubyinstaller_needs_a_published_digest(self):
        rel = [{"tag_name": "RubyInstaller-3.3.6-2", "prerelease": False, "body": "", "assets": [
            {"name": "rubyinstaller-3.3.6-2-x64.7z", "browser_download_url":
             "https://github.com/oneclick/rubyinstaller2/releases/download/RubyInstaller-3.3.6-2/rubyinstaller-3.3.6-2-x64.7z"}]}]
        runtimes.set_transport(lambda url: json.dumps(rel).encode())
        with mock.patch.object(runtimes, "_os_arch", return_value=("windows", "x64")):
            with self.assertRaises(runtimes.RuntimeError_) as e:
                runtimes.provision("ruby-3")
        self.assertIn("SHA-256", str(e.exception))

    def test_register_existing_ruby_folder(self):
        home = tempfile.mkdtemp(prefix="ruby-")
        os.makedirs(os.path.join(home, "bin"))
        for exe in ("ruby", "gem"):
            p = os.path.join(home, "bin", exe)
            with open(p, "w") as fh:
                fh.write("#!/bin/sh\necho 'ruby 3.3.6'\n")
            os.chmod(p, 0o755)
        pms.register_path("ruby", home, "admin")
        self.assertEqual(ap.toolchains()["ruby"]["provides"], ["ruby", "gem", "bundle"])
        rows = {r["id"]: r for r in pms.status()["package_managers"]}
        self.assertTrue(rows["gem"]["ready"], rows["gem"])


class TestRuntimeAdvisor(unittest.TestCase):
    def tearDown(self):
        install_fake(None)

    def test_offline_mapping(self):
        install_fake(None)
        with mock.patch("core.wizard.llm.ai_available", return_value=False):
            a = runtime_advisor.advise(["ruby", "gem"], "RubyGems")
        self.assertEqual(a["recommended"], "ruby-3")
        self.assertIn("gem", a["expected_programs"])
        self.assertEqual(a["agent_source"], "LOCAL_HEURISTIC")

    def test_ai_choice_is_validated(self):
        fake = FakeLLM({"RUNTIME_INSTALL_ADVICE": {"recommended": "conda-ruby", "alternatives": ["ruby-3", "evil-1"],
                                                   "expected_programs": ["ruby", "gem", "bad name!"],
                                                   "summary": "Ruby from conda-forge.", "manual": "winget install Ruby"}})
        install_fake(fake)
        a = runtime_advisor.advise(["ruby", "gem"], "RubyGems for Watir")
        self.assertEqual((a["recommended"], a["alternatives"]), ("conda-ruby", ["ruby-3"]))
        self.assertEqual(a["expected_programs"], ["ruby", "gem"])
        self.assertIn("ruby-3", fake.called("RUNTIME_INSTALL_ADVICE")[0]["user"])
        install_fake(FakeLLM({"RUNTIME_INSTALL_ADVICE": {"recommended": "curl-pipe-bash", "summary": "x"}}))
        self.assertEqual(runtime_advisor.advise(["go"])["recommended"], "go-1")

    def test_api(self):
        install_fake(None)
        c = admin_client()
        with mock.patch("core.wizard.llm.ai_available", return_value=False):
            r = c.post("/api/admin/toolchains/advise", json={"programs": ["dotnet"], "context": "NuGet"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["advice"]["recommended"], "dotnet-8")


# ---------------------------------------------------------------------------------------------------------------
# 2. Setup doctor
# ---------------------------------------------------------------------------------------------------------------
def spec(eco, pkgs, run, entry, post=None):
    return FrameworkSpec(id="t-" + eco, name="T", language="x", ecosystem=eco, packages=[Package(*p) for p in pkgs],
                         run_command=run, entry_file=entry, post_install=post or [])


class TestSetupDoctor(unittest.TestCase):
    def test_wdio_runner_packages_follow_webdriverio(self):
        s = spec("npm", [("webdriverio", "8.36.0"), ("mocha", "10.4.0"), ("chai", "4.4.1")],
                 ["npx", "wdio", "run", "wdio.conf.js"], "test/specs/smoke.e2e.js")
        with mock.patch.object(tool_versions, "_fetch", return_value={"versions": ["8.36.1", "8.36.0", "9.0.0"]}):
            tool_versions._CACHE.clear()
            s, notes = setup_doctor.ensure_companions(s)
        got = {p.name: p.version for p in s.packages}
        for n in ("@wdio/cli", "@wdio/local-runner", "@wdio/mocha-framework", "@wdio/spec-reporter"):
            self.assertEqual(got[n], "8.36.0", n)
        self.assertEqual(len(notes), 4)
        s2, n2 = setup_doctor.ensure_companions(s)                 # idempotent
        self.assertEqual(n2, [])

    def test_standalone_wdio_needs_no_runner(self):
        s, notes = setup_doctor.ensure_companions(spec("npm", [("webdriverio", "8.36.0")], ["node", "{entry}"], "a.js"),
                                                  lookup_versions=False)
        self.assertEqual(notes, [])

    def test_pytest_and_robot_browser(self):
        s, n = setup_doctor.ensure_companions(spec("pip", [("selenium", "==4.25.0")], ["pytest", "{entry}"], "t.py"))
        self.assertIn("pytest", [p.name for p in s.packages])
        s, n = setup_doctor.ensure_companions(spec("pip", [("robotframework", ""), ("robotframework-browser", "")],
                                                   ["robot", "{entry}"], "s.robot"))
        self.assertIn(["python", "-m", "Browser.entry", "init"], s.post_install)
        self.assertEqual([d["value"] for d in setup_doctor.os_dependencies(s)], ["node", "npm"])

    def test_known_fixes(self):
        w = spec("npm", [("webdriverio", "8.36.0")], ["npx", "wdio", "run", "wdio.conf.js"], "t.js")
        f = setup_doctor.known_fix({"stage": "smoke_test", "output": "Error: Couldn't find plugin \"mocha\" framework"}, w)
        self.assertEqual(f["changes"]["add_packages"][0]["name"], "@wdio/mocha-framework")
        self.assertIsNone(setup_doctor.known_fix({"stage": "smoke_test", "output": "Error: Cannot find module 'fs'"}, w))
        p = spec("pip", [("selenium", "")], ["pytest", "{entry}"], "tests/test_automation.py")
        f = setup_doctor.known_fix({"stage": "smoke_test", "output": "ERROR: file or directory not found: tests/x.py"}, p)
        self.assertIn("tests/test_automation.py", f["changes"]["rewrite_smoke"])
        f = setup_doctor.known_fix({"stage": "smoke_test", "output": "ModuleNotFoundError: No module named 'yaml'"}, p)
        self.assertEqual(f["changes"]["add_packages"], [{"name": "PyYAML", "version": ""}])
        self.assertIsNone(setup_doctor.known_fix({"stage": "smoke_test", "output": "ModuleNotFoundError: No module named "
                                                  "'yaml'"}, p, tried=["add:PyYAML"]))
        self.assertIsNone(setup_doctor.known_fix({"stage": "smoke_test", "output": "Unable to obtain driver"}, p, "local"))

    def test_change_validation(self):
        p = spec("npm", [("webdriverio", "8.36.0")], ["npx", "wdio"], "t.js")
        ch, probs = tool_setup._validate_changes({"add_packages": [{"name": "@wdio/local-runner", "version": "8.36.0"},
                                                                   {"name": "rm -rf /"}],
                                                  "post_install": [["curl", "x"], ["npx", "playwright", "install"]]}, p, "")
        self.assertEqual(ch["add_packages"], [{"name": "@wdio/local-runner", "version": "8.36.0"}])
        self.assertEqual(ch["post_install"], [["npx", "playwright", "install"]])
        self.assertEqual(len(probs), 2)

    def test_entry_file_pick_among_several(self):
        p = spec("pip", [("selenium", "")], ["pytest", "{entry}"], "tests/test_automation.py")
        files = tool_setup._place_entry({"conftest.py": "x", "test_smoke.py": "y"}, p)
        self.assertEqual(files, {"conftest.py": "x", "tests/test_automation.py": "y"})


REQ_STACK_PYTEST = {
    "tool_family": "Requests", "summary": "API tests.", "category": "api",
    "languages": [{"id": "python", "name": "Python", "package_managers": ["pip"]}],
    "package_managers": [{"id": "pip", "language": "python", "ecosystem": "pip"}],
    "components": [{"id": "requests", "name": "Requests", "role": "core", "required": True,
                    "packages": [{"language": "python", "package_manager": "pip", "name": "requests"}]}],
    "run_profiles": [{"language": "python", "package_manager": "pip", "run_command": ["python", "{entry}"],
                      "entry_file": "run_api.py"}]}
SMOKE = {"files": [{"path": "run_api.py", "content": "import os\nimport requests\nprint(os.environ['SMOKE_API_URL'])\n"}]}


class TestAutomaticFixInTheSetup(unittest.TestCase):
    def setUp(self):
        from importlib import metadata
        self.v = metadata.version("requests")
        self.env = mock.patch.dict(os.environ, {"UNDERSTUDY_DEPENDENCY_INSTALL": "auto"})
        self.env.start()
        self.fetch = mock.patch.object(tool_versions, "_fetch", return_value={"versions": [self.v], "latest": self.v})
        self.fetch.start()
        tool_versions._CACHE.clear()
        ap.reset_to_defaults()
        events.reset()
        self.admin = admin_client()

    def tearDown(self):
        install_fake(None)
        self.fetch.stop()
        self.env.stop()
        tool_versions._CACHE.clear()
        ap.reset_to_defaults()

    def _to_review(self, fake):
        install_fake(fake)
        req = self.admin.post("/api/admin/tool-setup", json={"name": "Requests"}).json()["request"]
        req = self.admin.post(f"/api/admin/tool-requests/{req['id']}/stack/select", json={
            "language": "python", "package_manager": "pip", "components": ["requests"],
            "versions": {"requests": self.v}}).json()["request"]
        self.admin.post(f"/api/admin/tool-requests/{req['id']}/setup?wait=true", json={})
        self.assertEqual(ap.get_request(req["id"])["status"], "smoke_review")
        return req["id"]

    def test_missing_module_is_added_and_the_setup_continues_without_asking(self):
        ap.set_setup_fix_approval("automatic")                    # the admin chose automatic safe fixes
        rid = self._to_review(FakeLLM({"TOOL_STACK_SUGGEST": REQ_STACK_PYTEST, "TOOL_SMOKE_TEST": SMOKE,
                                       "TOOL_SETUP_PLAN": RuntimeError("down")}))
        fail = ExecResult("python run_api.py", 1, "Traceback\nModuleNotFoundError: No module named 'pytest'")
        ok = ExecResult("python run_api.py", 0, "UNDERSTUDY_SMOKE_OK")
        with mock.patch("core.wizard.sandbox.smoke.run", side_effect=[(fail, {}), (ok, {})]):
            self.admin.post(f"/api/admin/tool-requests/{rid}/smoke/approve?wait=true")
        r = ap.get_request(rid)
        self.assertEqual(r["status"], "approved", r.get("clarification"))
        self.assertEqual(r["setup"]["auto_fixes"][0]["changes"]["add_packages"][0]["name"], "pytest")
        self.assertIn("pytest", [p["name"] for p in ap.whitelist()[r["tool_id"]]["spec"]["packages"]])
        self.assertTrue(any(m.get("kind") == "auto_fix" for m in r.get("conversation") or []))

    def test_ai_option_with_changes_is_executable(self):
        q = {"diagnosis": "runner missing", "category": "smoke_test", "question": "Admin, add @wdio/local-runner?",
             "options": [{"id": "add_runner", "label": "Add it", "changes": {"add_packages": [{"name": "pytest"}]}},
                         {"id": "retry", "label": "Rewrite"}]}
        rid = self._to_review(FakeLLM({"TOOL_STACK_SUGGEST": REQ_STACK_PYTEST, "TOOL_SMOKE_TEST": SMOKE,
                                       "TOOL_SETUP_PLAN": RuntimeError("down"), "TOOL_SETUP_CLARIFY": q,
                                       "TOOL_SETUP_RESOLVE": RuntimeError("down")}))
        fail = ExecResult("python run_api.py", 1, "something odd happened")
        ok = ExecResult("python run_api.py", 0, "UNDERSTUDY_SMOKE_OK")
        with mock.patch("core.wizard.sandbox.smoke.run", side_effect=[(fail, {})]):
            self.admin.post(f"/api/admin/tool-requests/{rid}/smoke/approve?wait=true")
        r = ap.get_request(rid)
        self.assertEqual(r["status"], "awaiting_admin")
        opt = next(o for o in r["clarification"]["options"] if o["id"] == "add_runner")
        self.assertEqual(opt["changes"]["add_packages"][0]["name"], "pytest")
        with mock.patch("core.wizard.sandbox.smoke.run", side_effect=[(ok, {})]):
            tool_setup.answer(rid, "admin", option="add_runner", background=False)
        r = ap.get_request(rid)
        self.assertEqual(r["status"], "approved", r.get("clarification"))
        self.assertIn("pytest", [p["name"] for p in ap.whitelist()[r["tool_id"]]["spec"]["packages"]])

    def test_local_sandbox_facts_say_it_has_network(self):
        facts = tool_setup._server_facts(spec("pip", [("requests", "")], ["python", "{entry}"], "a.py"))
        self.assertIn("server's own network", facts["network"])
        self.assertIn("can download", facts["network"])


# ---------------------------------------------------------------------------------------------------------------
# 3. Escalation + notifications
# ---------------------------------------------------------------------------------------------------------------
from tests.test_wizard_self_healing import PASS_EXEC, RetryLoopBase  # noqa: E402
from core.wizard import escalations, session as wz  # noqa: E402



class EscalationBase(RetryLoopBase):
    """A session whose run passed (9.0: Step 5 only exports, so there is no 'verify failed' state any more); the
    runtime-help escalations below still go to the admin."""
    def _verify_fails(self):
        ap.set_max_self_heal_retries(1)
        s, _ = self.run_execute([PASS_EXEC])
        self.assertEqual(s["pillars"]["BUILD-EXECUTE"]["status"], "PASS")
        return s


class TestEscalation(EscalationBase):
    def test_no_verify_escalation_in_9(self):
        s = self._verify_fails()
        self.assertTrue(s["export"]["ready"])                       # the ZIP is created right after a passing run
        self.assertNotIn("escalation_eligible", wz.public_view(s))
        self.assertFalse(hasattr(escalations, "create"))            # 9.4.1: only runtime-help requests remain


if __name__ == "__main__":
    unittest.main()


class TestToolCardsAreUnique(unittest.TestCase):
    """8.3 fix: AI suggestions that are aliases of one approved stack showed as several identical cards that were all
    selected at once."""
    def setUp(self):
        ap.reset_to_defaults()

    def tearDown(self):
        ap.reset_to_defaults()

    def test_aliases_and_repeats_collapse_to_one_card(self):
        from core.wizard import session as wz
        approved = FrameworkSpec(id="cypress-javascript", name="Cypress (JavaScript)", language="JavaScript",
                                 ecosystem="npm", packages=[Package("cypress", "13.6.6")],
                                 run_command=["npx", "cypress", "run"], entry_file="cypress/e2e/a.cy.js")
        ap.whitelist_tool(approved, "admin")
        for alias in ("cypress", "cypress-js"):
            ap.add_tool_alias("cypress-javascript", alias)
        opt = lambda i, rec=False: dict(approved.to_dict(), id=i, recommended=rec)  # noqa: E731
        # alias first, then the real id, then a repeat - every order used to leak a duplicate
        out = wz.annotate_tools([opt("cypress"), opt("cypress-javascript"), opt("cypress-js", True), opt("cypress")])
        ids = [o["id"] for o in out]
        self.assertEqual(ids.count("cypress-javascript"), 1, ids)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(next(o for o in out if o["id"] == "cypress-javascript")["recommended"])


class TestRobotBrowserInitFailure(unittest.TestCase):
    """8.3 field report: 'python -m Browser.entry init' exited 1 ('Problem installing node dependencies'); the AI asked
    about npm proxies although npm tools installed fine on the same server."""
    def _spec(self):
        return spec("pip", [("robotframework", ""), ("robotframework-browser", "==19.1.0")], ["robot", "{entry}"],
                    "smoke.robot", post=[["python", "-m", "Browser.entry", "init"]])

    def test_failed_init_switches_to_batteries(self):
        s = self._spec()
        f = setup_doctor.known_fix({"stage": "install", "command": "python.exe -m Browser.entry init",
                                    "output": "RuntimeError: Problem installing node dependencies"}, s)
        self.assertEqual(f["key"], "rfbrowser-batteries")
        self.assertEqual(f["changes"]["add_packages"], [{"name": "robotframework-browser-batteries", "version": "19.1.0"}])
        ch, probs = tool_setup._validate_changes(f["changes"], s, "", trusted=True)
        self.assertEqual(probs, [])
        setup = {"overrides": {}}
        tool_setup._merge_changes(setup, ch)
        out = setup_doctor.apply_overrides(self._spec(), setup["overrides"])
        out, _n = setup_doctor.ensure_companions(out, lookup_versions=False)
        self.assertNotIn(["python", "-m", "Browser.entry", "init"], out.post_install)      # not re-added
        self.assertIn(["python", "-m", "Browser.entry", "install", "chromium"], out.post_install)
        self.assertIn("robotframework-browser-batteries", [p.name for p in out.packages])
        self.assertEqual(setup_doctor.os_dependencies(out), [])                            # no Node.js needed
        # the admin's own answers can't remove install steps
        ch2, _p = tool_setup._validate_changes({"post_install_remove": [["python", "-m", "Browser.entry", "init"]]}, s, "")
        self.assertNotIn("post_install_remove", ch2)

    def test_rfbrowser_log_is_added_to_the_failure(self):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "Lib", "site-packages", "Browser"))
        with open(os.path.join(d, "Lib", "site-packages", "Browser", "rfbrowser.log"), "w") as fh:
            fh.write("npm ERR! code EPERM\nnpm ERR! syscall rename\n")
        tail = setup_doctor.rfbrowser_log_tail(d)
        self.assertIn("EPERM", tail)
        self.assertIn("rfbrowser.log", tail)



class TestHumanInTheLoopSetupFix(TestAutomaticFixInTheSetup):
    def test_default_asks_the_admin_with_a_one_click_fix(self):
        self.assertEqual(ap.setup_fix_approval(), "ask")
        rid = self._to_review(FakeLLM({"TOOL_STACK_SUGGEST": REQ_STACK_PYTEST, "TOOL_SMOKE_TEST": SMOKE,
                                       "TOOL_SETUP_PLAN": RuntimeError("down"), "TOOL_SETUP_CLARIFY": RuntimeError("down"),
                                       "TOOL_SETUP_RESOLVE": RuntimeError("down")}))
        fail = ExecResult("python run_api.py", 1, "ModuleNotFoundError: No module named 'pytest'")
        ok = ExecResult("python run_api.py", 0, "UNDERSTUDY_SMOKE_OK")
        with mock.patch("core.wizard.sandbox.smoke.run", side_effect=[(fail, {})]):
            self.admin.post(f"/api/admin/tool-requests/{rid}/smoke/approve?wait=true")
        r = ap.get_request(rid)
        self.assertEqual(r["status"], "awaiting_admin")                       # nothing applied by itself
        first = r["clarification"]["options"][0]
        self.assertEqual(first["id"], "apply_ai_fix")
        self.assertIn("pytest", first["label"])
        self.assertEqual(first["changes"]["add_packages"][0]["name"], "pytest")
        with mock.patch("core.wizard.sandbox.smoke.run", side_effect=[(ok, {})]):
            tool_setup.answer(rid, "admin", option="apply_ai_fix", background=False)
        r = ap.get_request(rid)
        self.assertEqual(r["status"], "approved", r.get("clarification"))
        self.assertEqual(r["setup"]["auto_fixes"][0]["approved_by"], "admin")


class TestVersionCheckAndProjectFiles(unittest.TestCase):
    def test_requested_version_is_not_satisfied_by_another(self):
        from core.wizard import dependency_resolver as d
        for have, want, ok in [("1.63.0", "==1.30.0", False), ("1.63.0", "1.44.0", False), ("1.44.0", "1.44.0", True),
                               ("1.63.0", ">=1.42", True), ("2.1", "~=1.4", False), ("1.4.9", "~=1.4", True)]:
            self.assertEqual(d._version_ok(have, want), ok, (have, want))
            self.assertEqual(d._simple_version_ok(have, want if want[0] in "<>=!~" else "==" + want), ok, (have, want))
        self.assertFalse(d._simple_version_ok("1.0", "==not a version"))

    def test_extra_csproj_is_dropped(self):
        from core.wizard import dependency_lock
        s = spec("nuget", [("Selenium.WebDriver", "4.49.0"), ("NUnit", "4.1.0")], ["dotnet", "test"], "AutomationTest.cs")
        files = dependency_lock.apply_to_files(s, {"AutomationTest.cs": "x", "SeleniumTests.csproj": "<Project/>",
                                                   "Tests.sln": "", "sub/Other.csproj": ""})
        self.assertEqual(sorted(k for k in files if k.endswith((".csproj", ".sln"))), ["Automation.csproj"])


class TestUserSideRepairs(unittest.TestCase):
    def test_native_build_switches_to_an_allowed_newer_version(self):
        from core.wizard import runtime_repair
        log = "error: Microsoft Visual C++ 14.0 or greater is required.\nERROR: Failed building wheel for greenlet"
        plan = runtime_repair.plan_repair("playwright-python", {"failure_reason": "deps failed"}, log, [],
                                          versions={"package": "playwright", "current": "1.44.0",
                                                    "allowed": ["1.44.0", "1.63.0"], "backend": "1.63.0"})
        self.assertEqual((plan["cause"], plan["switch_version"]), ("NATIVE_BUILD", "1.63.0"))
        plan = runtime_repair.plan_repair("playwright-python", {"failure_reason": "deps failed"}, log, [],
                                          versions={"package": "playwright", "current": "1.44.0",
                                                    "allowed": ["1.44.0"], "backend": "1.63.0"})
        self.assertEqual(plan["ask_admin"]["action"], "allow_version")
        self.assertEqual(plan["ask_admin"]["version"], "1.63.0")

    def test_msb1011(self):
        from core.wizard import runtime_repair
        plan = runtime_repair.plan_repair("selenium-csharp", {"failure_reason": "x"},
                                          "MSBUILD : error MSB1011: Specify which project or solution file to use", [])
        self.assertTrue(plan["clean_project_files"])

    def test_ai_actions_are_validated(self):
        from core.wizard import runtime_repair
        facts = {"versions": {"allowed": ["1.63.0"]}}
        ok, _ = runtime_repair.validate_ai_repair({"action": "switch_version", "version": "1.63.0"}, facts, None, [])
        self.assertEqual(ok["switch_version"], "1.63.0")
        bad, probs = runtime_repair.validate_ai_repair({"action": "switch_version", "version": "9.9"}, facts, None, [])
        self.assertIsNone(bad)
        self.assertIn("ask_admin", probs[0])
        ok, _ = runtime_repair.validate_ai_repair({"action": "run_args", "run_args": ["Automation.csproj"]}, facts, None, [])
        self.assertEqual(ok["run_args"], ["Automation.csproj"])
        bad, _ = runtime_repair.validate_ai_repair({"action": "run_args", "run_args": ["-e", "evil()"]}, facts, None, [])
        self.assertIsNone(bad)
        ok, _ = runtime_repair.validate_ai_repair({"action": "ask_admin", "admin_action": "server_change",
                                                   "admin_request": "Install Microsoft C++ Build Tools 14+"}, facts, None, [])
        self.assertEqual(ok["ask_admin"]["action"], "server_change")

    def test_robot_rewrite_hint(self):
        s = spec("pip", [("robotframework", "")], ["python", "{entry}"], "run_automation.py")
        f = setup_doctor.known_fix({"stage": "smoke_test", "message": "The AI's smoke test was rejected by Understudy's "
                                    "safety check (twice): run_automation.py starts other programs ('subprocess')"}, s)
        self.assertIn("robot.run", f["changes"]["rewrite_smoke"])


class TestRuntimeHelpEscalation(EscalationBase):
    def test_allow_version_from_the_admin(self):
        from core.wizard import session as wz2
        s = self._verify_fails()
        tool = s["framework"]["id"]
        owner = s.get("owner") or "local"
        esc = escalations.create_help(s, {"action": "allow_version", "version": "9.9.9",
                                          "request": "Please allow version 9.9.9 (needs no compiler)"}, "why", "diag")
        self.assertEqual(esc["kind"], "runtime_help")
        self.assertEqual(wz2.get(self.sid)["runtime_help"]["status"], "open")
        c = admin_client()
        with mock.patch("core.wizard.tool_versions.validate_version", side_effect=lambda v: v), \
                mock.patch.object(ap, "set_tool_versions") as stv:
            stv.return_value = {}
            r = c.post(f"/api/admin/escalations/{esc['id']}/decide", json={"decision": "confirm"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(stv.call_args[0][0], tool)
        self.assertIn("9.9.9", stv.call_args[0][1])
        self.assertTrue(notifications.items(owner)[0]["title"].startswith("Your admin allowed"))


class TestGreenletFailureInARun(RetryLoopBase):
    def test_proposal_switches_version_after_user_approval(self):
        ap.set_self_heal_approval("ask")
        ap.set_tool_versions("playwright-python", ["1.44.0", "1.63.0"], "1.44.0")
        fail = {"status": "FAIL", "exit_code": 1, "last_ok_step": 0, "strategy": "framework", "duration_ms": 5.0,
                "failure_category_hint": "ENVIRONMENT",
                "failure_reason": "Playwright (Python)'s dependencies failed to install: Failed to build greenlet"}
        with mock.patch("core.wizard.executor.execute", side_effect=[dict(fail)]):
            wz.execute(self.sid, background=False)
        s = wz.get(self.sid)
        s["execution"]["logs"] = s["execution"].get("logs", []) + [
            "error: Microsoft Visual C++ 14.0 or greater is required.", "ERROR: Failed building wheel for greenlet"]
        s["retry"]["awaiting_approval"] = False
        with mock.patch.object(wz, "_tool_versions_facts", return_value={"package": "playwright", "current": "1.44.0",
                                                                         "allowed": ["1.44.0", "1.63.0"], "backend": "1.63.0"}):
            wz._propose_heal(s, "deps failed")
        p = s["retry"]["proposal"]
        self.assertEqual(p["kind"], "runtime", p.get("diagnosis"))
        self.assertEqual(p["repair_plan"]["switch_version"], "1.63.0")
        self.assertTrue(s["retry"]["awaiting_approval"])                     # waits for the user


class TestRunProxyAndAdminFixes(EscalationBase):
    def test_run_proxy_never_routes_localhost(self):
        from core.wizard import egress
        with egress.EgressProxy(["localhost"]) as px:
            env = px.env()
        for h in ("localhost", "127.0.0.1", "::1"):
            self.assertIn(h, env["NO_PROXY"].split(","))
        ap.set_proxy_bypass_hosts(["erp.intranet.local"])
        with egress.EgressProxy(["localhost"]) as px:
            self.assertIn("erp.intranet.local", px.env()["NO_PROXY"])
        with self.assertRaises(ap.PolicyError):
            ap.set_proxy_bypass_hosts(["169.254.169.254"])

    def test_admin_fixes_a_runtime_help_request_without_ai(self):
        s = self._verify_fails()
        tool = s["framework"]["id"]
        esc = escalations.create_help(s, {"action": "server_change", "request": "Configure the system proxy"}, "AI guess")
        c = admin_client()
        bad = c.post(f"/api/admin/escalations/{esc['id']}/decide",
                     json={"decision": "confirm", "actions": [{"type": "set_env", "name": "PATH", "value": "x"}]})
        self.assertEqual(bad.status_code, 422)                              # PATH can't be set this way
        with mock.patch.object(wz, "admin_rerun") as rr:
            r = c.post(f"/api/admin/escalations/{esc['id']}/decide", json={
                "decision": "confirm", "rerun": True,
                "actions": [{"type": "set_env", "name": "CHROME_BIN", "value": "/usr/bin/chromium"},
                            {"type": "proxy_bypass", "host": "erp.intranet.local"}]})
        self.assertEqual(r.status_code, 200, r.text)
        e = r.json()["escalation"]
        self.assertEqual(len(e["admin_actions_done"]), 2)
        self.assertTrue(e.get("rerun_started"))
        rr.assert_called_once()
        self.assertEqual(ap.tool_runtime(tool)["env_vars"]["CHROME_BIN"], "/usr/bin/chromium")
        self.assertIn("erp.intranet.local", ap.proxy_bypass_hosts())

    def test_tool_run_settings_api(self):
        s = self._verify_fails()
        tool = s["framework"]["id"]
        c = admin_client()
        r = c.put(f"/api/admin/tools/{tool}/runtime", json={"env_vars": {"NO_PROXY": "corp.local"}, "path_prepend": []})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["runtime"]["env_vars"], {"NO_PROXY": "corp.local"})
        r = c.put(f"/api/admin/tools/{tool}/runtime", json={"path_prepend": ["/definitely/not/here"]})
        self.assertEqual(r.status_code, 400)
