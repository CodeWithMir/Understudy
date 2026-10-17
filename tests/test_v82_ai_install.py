"""8.2: the AI install assistant (stack suggestion -> admin selection -> plan -> install -> AI smoke test -> admin review
-> ACTIVE), nothing pre-built, the Java "invalid target release" fix with AI-advised runtime repairs, Cypress 16
(Cypress.env() removed) and the signup email-domain restriction.

The AI is a FakeLLM, the package registry a fixed list. The live stack test installs Python Requests in the local
sandbox (already available to the backend, so nothing is downloaded)."""
import json
import os
import stat
import tempfile
import unittest
from importlib import metadata
from unittest import mock

from fastapi.testclient import TestClient

from tests.wizard_helpers import FakeLLM, admin_client, install_fake
from core.wizard import admin_policy as ap, dependency_resolver, events, governance, jdk, runtime_repair, templates
from core.wizard import tool_setup, tool_stack, tool_versions, users
from core.wizard.frameworks import CATALOG, FrameworkSpec

REQ_V = metadata.version("requests")
REGISTRY = {"requests": [REQ_V, "2.31.0"], "pytest": ["8.3.3", "8.0.0"],
            "org.seleniumhq.selenium:selenium-java": ["4.25.0", "4.24.0"], "org.testng:testng": ["7.10.2"],
            "io.cucumber:cucumber-java": ["7.20.1"], "io.cucumber:cucumber-testng": ["7.20.1"],
            "org.junit.jupiter:junit-jupiter": ["5.11.0"], "selenium": ["4.25.0"]}

REQUESTS_STACK = {
    "tool_family": "Requests", "summary": "HTTP API tests with Python Requests.", "category": "api",
    "languages": [{"id": "python", "name": "Python", "recommended": True, "package_managers": ["pip"]}],
    "package_managers": [{"id": "pip", "name": "pip", "language": "python", "ecosystem": "pip"}],
    "components": [
        {"id": "requests", "name": "Requests", "role": "core", "purpose": "HTTP client", "required": True,
         "packages": [{"language": "python", "package_manager": "pip", "name": "requests", "recommended_version": "2.31.0"}]},
        {"id": "pytest", "name": "pytest", "role": "test_runner", "purpose": "runs the tests",
         "packages": [{"language": "python", "package_manager": "pip", "name": "pytest", "recommended_version": "8.0.0"}]}],
    "run_profiles": [{"language": "python", "package_manager": "pip", "run_command": ["python", "{entry}"],
                      "entry_file": "run_api.py"}],
    "os_requirements": ["Python 3.10+"], "questions": []}

SELENIUM_STACK = {
    "tool_family": "Selenium", "summary": "Browser automation with Selenium WebDriver.", "category": "browser",
    "languages": [{"id": "java", "name": "Java", "recommended": True, "package_managers": ["maven", "gradle"]},
                  {"id": "python", "name": "Python", "package_managers": ["pip"]},
                  {"id": "javascript", "name": "JavaScript", "package_managers": ["npm", "npx"]}],
    "package_managers": [{"id": "maven", "language": "java", "ecosystem": "maven"},
                         {"id": "gradle", "language": "java", "ecosystem": "unsupported"},
                         {"id": "pip", "language": "python", "ecosystem": "pip"}],
    "components": [
        {"id": "selenium-webdriver", "name": "Selenium WebDriver", "role": "core", "required": True,
         "packages": [{"language": "java", "package_manager": "maven", "name": "org.seleniumhq.selenium:selenium-java",
                       "recommended_version": "4.25.0"},
                      {"language": "python", "package_manager": "pip", "name": "selenium", "recommended_version": "4.25.0"}]},
        {"id": "testng", "name": "TestNG", "role": "test_runner", "default_selected": True, "conflicts_with": ["junit5"],
         "packages": [{"language": "java", "package_manager": "maven", "name": "org.testng:testng", "recommended_version": "7.10.2"}]},
        {"id": "junit5", "name": "JUnit 5", "role": "test_runner", "conflicts_with": ["testng"],
         "packages": [{"language": "java", "package_manager": "maven", "name": "org.junit.jupiter:junit-jupiter",
                       "recommended_version": "5.11.0"}]},
        {"id": "cucumber", "name": "Cucumber", "role": "bdd",
         "packages": [{"language": "java", "package_manager": "maven", "name": "io.cucumber:cucumber-java",
                       "recommended_version": "7.20.1"},
                      {"language": "java", "package_manager": "maven", "name": "not a valid name!"}]}],
    "run_profiles": [
        {"language": "java", "package_manager": "maven", "requires": ["cucumber"], "run_command": ["mvn", "-q", "test"],
         "entry_file": "src/test/java/automation/RunCucumberTest.java"},
        {"language": "java", "package_manager": "maven", "run_command": ["mvn", "-q", "test"],
         "entry_file": "src/test/java/automation/AutomationTest.java"},
        {"language": "python", "package_manager": "pip", "run_command": ["curl", "http://evil"], "entry_file": "x.py"}],
    "questions": ["Which browsers must the tests run on?"]}

GOOD_SMOKE = {"files": [{"path": "run_api.py", "content": (
    "import os\nimport requests\nr = requests.get(os.environ['SMOKE_API_URL'].rstrip('/') + '/ping.json', timeout=15)\n"
    "assert r.json()['status'] == 'ok'\nprint('UNDERSTUDY_SMOKE_OK')\n")},
    {"path": "requirements.txt", "content": "evil-package==6.6.6\n"}], "notes": "pings the dummy API"}


def fake_fetch(eco, name):
    v = REGISTRY.get(name)
    if v is None:
        raise RuntimeError("not in the test registry")
    return {"versions": v, "latest": v[0]}


def fake_java(major: int) -> str:
    """A JDK home whose bin/java answers -XshowSettings like a real JDK of that release."""
    home = tempfile.mkdtemp(prefix=f"fake-jdk{major}-")
    os.makedirs(os.path.join(home, "bin"))
    for exe in ("java", "javac"):
        p = os.path.join(home, "bin", exe)
        with open(p, "w") as fh:
            fh.write(f"#!/bin/sh\necho 'Property settings:' >&2\necho '    java.home = {home}' >&2\n"
                     f"echo '    java.specification.version = {major}' >&2\necho 'openjdk version \"{major}.0.1\"' >&2\n")
        os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
    return home


class Base(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"UNDERSTUDY_DEPENDENCY_INSTALL": "auto"})
        self.env.start()
        os.environ.pop("UNDERSTUDY_DEFAULT_TOOLS", None)
        self.fetch = mock.patch.object(tool_versions, "_fetch", side_effect=fake_fetch)
        self.fetch.start()
        tool_versions._CACHE.clear()
        ap.reset_to_defaults()
        events.reset()
        self.admin = admin_client()

    def tearDown(self):
        install_fake(None)
        self.fetch.stop()
        self.env.stop()
        os.environ["UNDERSTUDY_DEFAULT_TOOLS"] = "catalog"
        tool_versions._CACHE.clear()
        ap.reset_to_defaults()


# ---------------------------------------------------------------------------------------------------------------
# 1. The AI install assistant
# ---------------------------------------------------------------------------------------------------------------
class TestInstallAssistant(Base):
    def test_nothing_is_pre_built(self):
        self.assertEqual(ap.default_whitelist(), [])
        self.assertEqual(set(ap.whitelist()), {"gemini-agent"})                  # 9.9: only the built-in agent
        self.assertFalse(any(e["status"] == "ACTIVE" for e in ap.registry() if e.get("tool_id") != "gemini-agent"))

    def test_without_ai_the_assistant_says_so(self):
        install_fake(None)
        r = self.admin.post("/api/admin/tool-setup", json={"name": "Selenium"})
        self.assertEqual(r.status_code, 503, r.text)
        self.assertIn("Gemini", r.json()["detail"]["message"])

    def test_full_flow_suggest_select_review_activate(self):
        fake = FakeLLM({"TOOL_STACK_SUGGEST": REQUESTS_STACK, "TOOL_SMOKE_TEST": GOOD_SMOKE,
                        "TOOL_SETUP_PLAN": RuntimeError("plan agent down")})
        install_fake(fake)
        r = self.admin.post("/api/admin/tool-setup", json={"name": "Python Requests", "instruction": "API tests only"})
        self.assertEqual(r.status_code, 200, r.text)
        req = r.json()["request"]
        self.assertEqual((req["status"], ap.REGISTRY_STATUS[req["status"]]), ("stack_ready", "CHOOSE_STACK"))
        self.assertIn("API tests only", fake.called("TOOL_STACK_SUGGEST")[-1]["user"])
        sug = req["stack"]["suggestion"]
        self.assertEqual([c["id"] for c in sug["components"]], ["requests", "pytest"])
        pip = next(p for p in sug["package_managers"] if p["id"] == "pip")
        self.assertTrue(pip["supported"])
        # real versions for the admin's language + package manager, the AI's recommendation pre-selected
        v = self.admin.get(f"/api/admin/tool-requests/{req['id']}/stack/versions",
                           params={"language": "python", "package_manager": "pip"}).json()
        self.assertEqual(v["components"]["requests"]["versions"], [REQ_V, "2.31.0"] if REQ_V != "2.31.0" else ["2.31.0"])
        self.assertEqual(v["components"]["requests"]["recommended"], "2.31.0")
        # the admin's choice -> spec -> plan
        r = self.admin.post(f"/api/admin/tool-requests/{req['id']}/stack/select", json={
            "language": "python", "package_manager": "pip", "package_managers_available": ["pip"],
            "components": ["requests"], "versions": {"requests": REQ_V}})
        self.assertEqual(r.status_code, 200, r.text)
        req = r.json()["request"]
        self.assertEqual(req["status"], "plan_ready")
        self.assertEqual(req["plan"]["spec"]["packages"], [{"name": "requests", "version": "==" + REQ_V}])
        self.assertEqual(req["plan"]["versions"]["default"], REQ_V)
        self.assertIn("Requests", req["plan"]["summary"])
        self.assertNotIn("offline", req["plan"]["code_generation"].lower().replace("offline template", ""))
        # approve -> install -> the AI writes the smoke test -> REVIEW (nothing runs yet)
        r = self.admin.post(f"/api/admin/tool-requests/{req['id']}/setup?wait=true", json={})
        self.assertEqual(r.status_code, 200, r.text)
        req = ap.get_request(req["id"])
        self.assertEqual(req["status"], "smoke_review", req["setup"].get("failure"))
        files = req["setup"]["smoke_review"]["files"]
        self.assertEqual(sorted(files), ["run_api.py"])                         # the AI's requirements.txt is dropped
        self.assertTrue(any("dependency manifest" in n for n in req["setup"]["smoke_review"]["notes"]))
        self.assertIn("Requests", fake.called("TOOL_SMOKE_TEST")[-1]["user"])  # the selected components reach the AI
        r = self.admin.post(f"/api/admin/tool-requests/{req['id']}/smoke/approve?wait=true")
        self.assertEqual(r.status_code, 200, r.text)
        req = ap.get_request(req["id"])
        self.assertEqual(req["status"], "approved", req.get("clarification"))
        e = ap.whitelist()[req["tool_id"]]
        self.assertEqual(e["setup"]["stack"]["components"], ["requests"])
        self.assertEqual(e["setup"]["smoke"]["status"], "PASSED")
        self.assertEqual(ap.tool_version_policy(req["tool_id"])["default"], REQ_V)

    def test_compose_selenium_java_testng_cucumber(self):
        sug, notes = tool_stack._clean_suggestion(SELENIUM_STACK, {"maven": "available", "pip": "available"})
        self.assertTrue(any("not a valid" in n for n in notes))                  # invalid package name dropped
        self.assertTrue(any("curl" in n for n in notes))                         # unsafe run command dropped
        gradle = next(p for p in sug["package_managers"] if p["id"] == "gradle")
        self.assertTrue(gradle["supported"])                                    # 8.2: Gradle has an installer
        spec, notes, sel = tool_stack.compose_spec(sug, {"language": "java", "package_manager": "maven",
                                                         "components": ["testng", "cucumber"],
                                                         "versions": {"selenium-webdriver": "4.24.0"}})
        self.assertEqual([p.name for p in spec.packages], ["org.seleniumhq.selenium:selenium-java", "org.testng:testng",
                                                           "io.cucumber:cucumber-java"])
        self.assertEqual(spec.packages[0].version, "4.24.0")                    # the admin's version
        self.assertEqual(spec.packages[1].version, "7.10.2")                    # the AI's recommendation
        self.assertEqual((spec.ecosystem, spec.run_command, spec.entry_file),
                         ("maven", ["mvn", "-q", "test"], "src/test/java/automation/RunCucumberTest.java"))
        self.assertEqual(spec.id, "selenium-java-testng-cucumber")
        self.assertIn("TestNG", spec.name)
        self.assertEqual(sel["components"][0], "selenium-webdriver")            # the core is always included
        with self.assertRaises(tool_stack.StackError):                         # alternatives can't be combined
            tool_stack.compose_spec(sug, {"language": "java", "package_manager": "maven", "components": ["testng", "junit5"]})
        with self.assertRaises(tool_stack.StackError) as e:
            tool_stack.compose_spec(sug, {"language": "java", "package_manager": "sbt", "components": []})
        self.assertEqual(e.exception.status_code, 422)
        with self.assertRaises(tool_stack.StackError):
            tool_stack.compose_spec(sug, {"language": "java", "package_manager": "maven", "components": [],
                                          "versions": {"selenium-webdriver": "4.0; rm -rf /"}})

    def test_user_request_resolves_to_the_composed_stack(self):
        """A user asked for 'Cypress'; the admin's composed stack gets another id - the request still resolves."""
        install_fake(FakeLLM({"TOOL_STACK_SUGGEST": REQUESTS_STACK}))
        spec = FrameworkSpec.from_dict(CATALOG["requests-api"].to_dict())
        req = ap.request_tool(spec, "", reason="SOP says so", requested_by="alice")
        req = tool_stack.suggest(req["id"])
        self.assertEqual(req["status"], "stack_ready")
        with mock.patch.object(tool_setup, "_setup_job"):                       # the plan only
            req = tool_stack.select(req["id"], {"language": "python", "package_manager": "pip", "components": ["requests"]})
        self.assertEqual((req["tool_id"], req["stack"]["original_tool_id"]), ("requests-python", "requests-api"))
        ap.whitelist_tool(FrameworkSpec.from_dict(req["plan"]["spec"]), "admin")
        ap.add_tool_alias("requests-python", "requests-api")
        self.assertTrue(ap.is_tool_allowed("requests-api"))
        self.assertEqual(ap.allowed_spec("requests-api").id, "requests-python")
        from core.wizard import tool_requirements
        self.assertEqual(tool_requirements.status({"tool_id": "requests-api"})["status"], "active")


# ---------------------------------------------------------------------------------------------------------------
# 1b. Package managers & build tools the admin controls (Gradle support)
# ---------------------------------------------------------------------------------------------------------------
def fake_tool_home(exe: str, version: str) -> str:
    home = tempfile.mkdtemp(prefix=f"fake-{exe}-")
    os.makedirs(os.path.join(home, "bin"))
    p = os.path.join(home, "bin", exe)
    with open(p, "w") as fh:
        fh.write(f"#!/bin/sh\necho '{exe.capitalize()} {version}'\n")
    os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
    return home


def _zip(files):
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for k, v in files.items():
            z.writestr(k, v)
    return buf.getvalue()


class TestPackageManagers(Base):
    def setUp(self):
        super().setUp()
        from core.wizard import runtimes
        self.runtimes = runtimes
        self.rt_dir = mock.patch.object(runtimes, "RUNTIMES_DIR", tempfile.mkdtemp(prefix="rt-"))
        self.rt_dir.start()

    def tearDown(self):
        self.runtimes.set_transport(None)
        self.rt_dir.stop()
        super().tearDown()

    def test_admin_switches_gradle_off_and_on(self):
        from core.wizard import package_managers as pms
        r = self.admin.get("/api/admin/package-managers")
        self.assertEqual(r.status_code, 200, r.text)
        ids = [x["id"] for x in r.json()["package_managers"]]
        self.assertIn("gradle", ids)
        self.assertTrue(pms.is_enabled("gradle"))
        r = self.admin.put("/api/admin/package-managers/gradle", json={"enabled": False})
        self.assertFalse(next(x for x in r.json()["package_managers"] if x["id"] == "gradle")["enabled"])
        sug, _ = tool_stack._clean_suggestion(SELENIUM_STACK, {"maven": "available", "gradle": "available"})
        row = next(p for p in sug["package_managers"] if p["id"] == "gradle")
        self.assertEqual((row["supported"], row["disabled_by_admin"]), (False, True))
        with self.assertRaises(tool_stack.StackError) as e:
            tool_stack.compose_spec(sug, {"language": "java", "package_manager": "gradle", "components": []})
        self.assertIn("switched off", str(e.exception))
        self.assertEqual(self.admin.put("/api/admin/package-managers/sbt", json={"enabled": True}).status_code, 404)
        self.admin.put("/api/admin/package-managers/gradle", json={"enabled": True})
        stack = json.loads(json.dumps(SELENIUM_STACK))
        for c in stack["components"]:                                          # the AI lists Gradle coordinates too
            c["packages"] += [dict(p, package_manager="gradle") for p in c["packages"] if p["package_manager"] == "maven"]
        sug, _ = tool_stack._clean_suggestion(stack, {"gradle": "available"})
        spec, notes, sel = tool_stack.compose_spec(sug, {"language": "java", "package_manager": "gradle",
                                                         "components": ["testng"]})
        self.assertEqual((spec.ecosystem, spec.run_command[0], spec.entry_file),
                         ("gradle", "gradle", "src/test/java/automation/AutomationTest.java"))
        files = dependency_resolver.manifest_files(spec)
        self.assertEqual(sorted(files), ["build.gradle", "settings.gradle"])
        self.assertIn("useTestNG()", files["build.gradle"])
        self.assertIn("testImplementation 'org.testng:testng:7.10.2'", files["build.gradle"])
        self.assertIn("options.release =", files["build.gradle"])
        plan = dependency_resolver.plan_for(spec)
        self.assertEqual(plan.commands[0][:3] + plan.commands[0][-1:], ["gradle", "-q", "--no-daemon", "understudyResolve"])
        sh = dependency_resolver.setup_scripts(spec)["run.sh"]
        self.assertIn("gradle -q --no-daemon understudyResolve", sh)
        self.assertIn("gradle --no-daemon --console=plain test", sh)

    def test_junit_platform_and_release_patch(self):
        from core.wizard.frameworks import Package
        spec = FrameworkSpec(id="g", name="G", language="Java", ecosystem="gradle",
                             packages=[Package("org.junit.jupiter:junit-jupiter", "5.11.0")],
                             run_command=["gradle", "test"], entry_file="src/test/java/A.java")
        b = dependency_resolver.build_gradle(spec, 17)
        self.assertIn("useJUnitPlatform()", b)
        self.assertIn("junit-platform-launcher:1.11.0", b)
        self.assertIn("options.release = 11", jdk.patch_gradle_release(b, 11))
        self.assertIn("JavaVersion.VERSION_1_8", jdk.patch_gradle_release(b, 8))
        self.assertNotIn("options.release", jdk.patch_gradle_release(b, 8))

    def test_register_an_existing_install(self):
        home = fake_tool_home("gradle", "8.10.2")
        bad = self.admin.put("/api/admin/toolchains", json={"kind": "gradle", "path": tempfile.mkdtemp()})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(self.admin.put("/api/admin/toolchains", json={"kind": "sbt", "path": home}).status_code, 400)
        r = self.admin.put("/api/admin/toolchains", json={"kind": "gradle", "path": os.path.join(home, "bin", "gradle")})
        self.assertEqual(r.status_code, 200, r.text)
        tc = next(t for t in r.json()["toolchains"] if t["kind"] == "gradle")
        self.assertEqual((tc["home"], tc["source"], tc["version"]), (home, "path", "8.10.2"))
        self.assertIn(os.path.join(home, "bin"), ap.toolchain_bins())
        self.assertEqual(ap.toolchain_homes()["GRADLE_HOME"], home)
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}):
            self.assertNotIn("gradle", dependency_resolver.check_toolchain("gradle"))       # found through the registration
            env = dependency_resolver._scrubbed_env()
            self.assertTrue(env["PATH"].startswith(os.path.join(home, "bin")))
            self.assertEqual(env["GRADLE_HOME"], home)
        r = self.admin.delete("/api/admin/toolchains/gradle")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(ap.toolchain_bins(), [])

    def test_download_gradle_and_maven_checksum_verified(self):
        import hashlib
        gzip_ = _zip({"gradle-8.10.2/bin/gradle": "#!/bin/sh\necho 'Gradle 8.10.2'\n", "gradle-8.10.2/lib/x.jar": "x"})
        mzip = _zip({"apache-maven-3.9.9/bin/mvn": "#!/bin/sh\necho 'Apache Maven 3.9.9'\n"})
        gurl = "https://services.gradle.org/distributions/gradle-8.10.2-bin.zip"
        central = self.runtimes.MAVEN_CENTRAL
        blobs = {
            "https://services.gradle.org/versions/all": json.dumps([
                {"version": "9.1.0", "downloadUrl": "https://services.gradle.org/distributions/gradle-9.1.0-bin.zip",
                 "checksumUrl": "https://x/9.sha256"},
                {"version": "8.11-rc-1", "rcFor": "8.11", "downloadUrl": "https://services.gradle.org/distributions/r.zip",
                 "checksumUrl": "https://x/rc"},
                {"version": "8.10.2", "downloadUrl": gurl, "checksumUrl": gurl + ".sha256"},
                {"version": "8.9", "downloadUrl": "https://services.gradle.org/distributions/gradle-8.9-bin.zip",
                 "checksumUrl": "https://x/89"}]).encode(),
            gurl: gzip_, gurl + ".sha256": hashlib.sha256(gzip_).hexdigest().encode(),
            central + "maven-metadata.xml": b"<metadata><versioning><versions><version>3.8.8</version>"
                                            b"<version>3.9.9</version><version>4.0.0-rc-2</version></versions></versioning></metadata>",
            central + "3.9.9/apache-maven-3.9.9-bin.zip": mzip,
            central + "3.9.9/apache-maven-3.9.9-bin.zip.sha512": hashlib.sha512(mzip).hexdigest().encode(),
        }
        self.runtimes.set_transport(lambda url: blobs[url])
        r = self.admin.post("/api/admin/toolchains/download?wait=true", json={"key": "gradle-8"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["jobs"]["gradle-8"]["status"], "done", r.json()["jobs"])
        tc = next(t for t in r.json()["toolchains"] if t["kind"] == "gradle")
        self.assertTrue(tc["home"].endswith("gradle-8.10.2"))
        self.assertTrue(os.access(os.path.join(tc["home"], "bin", "gradle"), os.X_OK))   # zip lost the exec bit
        r = self.admin.post("/api/admin/toolchains/download?wait=true", json={"key": "maven-3"})
        self.assertEqual(r.json()["jobs"]["maven-3"]["status"], "done", r.json()["jobs"])
        self.assertEqual(ap.toolchain_homes()["MAVEN_HOME"].rsplit(os.sep, 1)[-1], "apache-maven-3.9.9")
        # a tampered download is refused
        blobs[gurl] = gzip_ + b"tampered"
        import shutil
        shutil.rmtree(os.path.join(self.runtimes.RUNTIMES_DIR, "gradle-8"))
        r = self.admin.post("/api/admin/toolchains/download?wait=true", json={"key": "gradle-8"})
        self.assertEqual(r.json()["jobs"]["gradle-8"]["status"], "failed")
        self.assertIn("Checksum mismatch", r.json()["jobs"]["gradle-8"]["error"])
        self.assertEqual(self.admin.post("/api/admin/toolchains/download", json={"key": "sbt-1"}).status_code, 400)

    def test_missing_gradle_or_maven_is_repaired_by_a_download(self):
        for prog, key in (("gradle", "download:gradle-8"), ("mvn", "download:maven-3")):
            p = runtime_repair.plan_repair("x", {"failure_reason": f"Could not start '{prog}': [Errno 2] No such file "
                                                                   f"or directory: '{prog}'"}, "", [])
            self.assertEqual(p["repair_id"], key)


class TestSmokeLoopFixes(Base):
    """The 8.2 field report: Playwright (JavaScript) with run command 'npx playwright test {entry}' and entry 'tests/'
    - every AI test was rejected ('the entry file tests/ is missing') and the AI offered to skip the smoke test."""
    PW = {"id": "playwright-javascript", "name": "Playwright (JavaScript)", "language": "JavaScript", "ecosystem": "npm",
          "packages": [{"name": "@playwright/test", "version": "1.48.2"}], "run_command": ["npx", "playwright", "test", "{entry}"],
          "entry_file": "tests/"}
    SPEC_JS = ("const { test, expect } = require('@playwright/test');\n"
               "test('smoke', async ({ page }) => { await page.goto(process.env.SMOKE_URL); await page.fill('#name', 'Understudy');\n"
               "  await page.click('#go'); await expect(page.locator('#status')).toHaveText('CLICKED:Understudy');\n"
               "  console.log('UNDERSTUDY_SMOKE_OK'); });\n")

    def test_a_folder_entry_becomes_a_file(self):
        from core.wizard.frameworks import validate_spec
        spec = validate_spec(FrameworkSpec.from_dict(self.PW))
        self.assertEqual(spec.entry_file, "tests/automation.spec.js")
        self.assertIn(["npx", "playwright", "install", "chromium"], spec.post_install)     # its browser build too
        for d, want in ((dict(self.PW, entry_file="tests/**/*.spec.ts"), "tests/automation.spec.js"),
                        (dict(self.PW, ecosystem="pip", packages=[{"name": "pytest"}], run_command=["python", "-m", "pytest",
                         "{entry}"], entry_file="tests"), "tests/test_automation.py"),
                        (dict(self.PW, ecosystem="maven", packages=[{"name": "org.testng:testng"}],
                              run_command=["mvn", "-q", "test"], entry_file="src/test/java/"),
                         "src/test/java/AutomationTest.java")):
            self.assertEqual(validate_spec(FrameworkSpec.from_dict(d)).entry_file, want, d)

    def test_any_test_file_name_is_accepted_and_rejections_are_rewritten_once(self):
        from core.wizard.frameworks import validate_spec
        spec = validate_spec(FrameworkSpec.from_dict(self.PW))
        fake = FakeLLM({"TOOL_SMOKE_TEST": {"files": [{"path": "./tests/smoke.spec.js", "content": self.SPEC_JS}]}})
        install_fake(fake)
        files, source, notes, _ = tool_setup._smoke_files({}, spec, "local")
        self.assertEqual(sorted(files), ["tests/automation.spec.js"])       # the AI's file name is used as the entry
        answers = iter([{"files": [{"path": "a.js", "content": "x"}, {"path": "b.js", "content": "y"}]},
                        {"files": [{"path": "tests/automation.spec.js", "content": self.SPEC_JS}]}])
        fake.responses["TOOL_SMOKE_TEST"] = lambda *a: next(answers)
        files, source, notes, _ = tool_setup._smoke_files({}, spec, "local")
        self.assertIn("tests/automation.spec.js", files)
        self.assertIn("rejected_by_understudy_because", fake.called("TOOL_SMOKE_TEST")[-1]["user"])
        self.assertTrue(any("rewritten automatically" in n for n in notes))

    @unittest.skipUnless(__import__("shutil").which("node"), "Node.js not installed")
    def test_npm_tools_see_their_packages_from_the_script_folder(self):
        """'Playwright Test did not expect test() to be called here ... No tests found': with only NODE_PATH the test
        file and the CLI loaded two copies of @playwright/test. The script folder now gets a node_modules link."""
        from core.wizard.frameworks import validate_spec
        from core.wizard.sandbox import create_runner
        spec = validate_spec(FrameworkSpec.from_dict({"id": "linkcheck", "name": "Link check", "language": "JavaScript",
                                                      "ecosystem": "npm", "packages": [{"name": "left-pad", "version": "1.3.0"}],
                                                      "run_command": ["node", "{entry}"], "entry_file": "check.js"}))
        with create_runner(spec, purpose="setup", mode="local") as r:
            nm = os.path.join(r.env_dir, "node_modules", "fake-pkg")
            os.makedirs(nm, exist_ok=True)
            with open(os.path.join(nm, "index.js"), "w") as fh:
                fh.write("module.exports = 'from-env';\n")
            res = r.execute_script({"check.js": "const fs = require('fs');\n"
                                                "console.log('LOCAL=' + fs.existsSync('node_modules/fake-pkg/index.js'));\n"
                                                "console.log('REQ=' + require('fake-pkg'));\n"}, ["node", "{entry}"])
            self.assertIn("LOCAL=true", res.output)
            self.assertIn("REQ=from-env", res.output)
            self.assertTrue(os.path.isfile(os.path.join(nm, "index.js")))         # the environment is untouched
            leftovers = [os.path.join(d, "node_modules") for d in r._dirs if os.path.lexists(os.path.join(d, "node_modules"))]
            self.assertEqual(leftovers, [])                                       # the link is removed after the run

    def test_the_smoke_test_is_never_skipped(self):
        self.assertTrue(tool_setup._asks_to_skip_smoke("skip_smoke Skip the smoke test and proceed"))
        self.assertTrue(tool_setup._asks_to_skip_smoke("proceed without the test"))
        self.assertFalse(tool_setup._asks_to_skip_smoke("skip_browser_download Skip the browser download"))
        self.assertFalse(tool_setup._asks_to_skip_smoke("retry Rewrite the smoke test"))
        spec = FrameworkSpec.from_dict(CATALOG["requests-api"].to_dict())
        req = ap.request_tool(spec, "", requested_by="alice")
        install_fake(FakeLLM({"TOOL_SETUP_CLARIFY": {"diagnosis": "x", "category": "smoke_test", "question": "Admin, how?",
                                                     "options": [{"id": "retry_structure", "label": "Retry with a different structure"},
                                                                 {"id": "skip_smoke", "label": "Skip the smoke test and proceed"}]}}))
        tool_setup._halt(req["id"], {"status": "smoke_testing", "round": 0, "overrides": {}, "attempts": [], "allowed": [],
                                     "default": None, "by": "admin", "logs": []},
                         tool_setup.SetupFailure("smoke_test", "rejected"))
        q = ap.get_request(req["id"])["clarification"]
        self.assertNotIn("skip_smoke", [o["id"] for o in q["options"]])
        out = tool_setup.answer(req["id"], "admin", "", "skip_smoke", background=False)
        self.assertEqual(out["status"], "awaiting_admin")                      # still waiting - nothing was skipped
        self.assertIn("can't be skipped", out["clarification"]["question"])
        self.assertNotIn(spec.id, ap.whitelist())


# ---------------------------------------------------------------------------------------------------------------
# 2. Java: "invalid target release: 17" (the 8.1.1 field report)
# ---------------------------------------------------------------------------------------------------------------
SCREENSHOT_ERROR = ("[ERROR] Failed to execute goal org.apache.maven.plugins:maven-compiler-plugin:3.11.0:testCompile "
                    "(default-testCompile) on project understudy-selenium-java: Fatal error compiling: error: invalid "
                    "target release: 17 -> [Help 1]")


class TestJavaRelease(Base):
    def test_probe_and_release(self):
        j11, j21 = fake_java(11), fake_java(21)
        self.assertEqual(jdk.probe(jdk._exe(j11))["major"], 11)
        self.assertEqual(jdk.maven_jdk({"JAVA_HOME": j11, "PATH": os.path.join(j21, "bin")})["home"], j11)  # JAVA_HOME wins
        self.assertEqual(jdk.maven_jdk({"PATH": os.path.join(j21, "bin")})["major"], 21)
        self.assertEqual([jdk.release_for_major(m) for m in (None, 8, 11, 17, 21)], [17, 8, 11, 17, 17])
        self.assertEqual(jdk.required_release(SCREENSHOT_ERROR), 17)
        self.assertEqual(jdk.required_release("has been compiled by a more recent version of the Java Runtime "
                                              "(class file version 61.0)"), 17)

    def test_pom_follows_the_jdk_maven_uses(self):
        spec = FrameworkSpec.from_dict(CATALOG["selenium-java"].to_dict())
        self.assertIn("<maven.compiler.release>11</maven.compiler.release>", dependency_resolver.pom_xml(spec, 11))
        pom8 = dependency_resolver.pom_xml(spec, 8)
        self.assertIn("<maven.compiler.source>1.8</maven.compiler.source>", pom8)
        self.assertNotIn("maven.compiler.release", pom8)
        with mock.patch.dict(os.environ, {"JAVA_HOME": fake_java(11)}):
            self.assertIn("<maven.compiler.release>11</maven.compiler.release>", dependency_resolver.pom_xml(spec))
        with jdk.release_override(21):
            self.assertEqual(jdk.release_for(spec), 21)
        old = "<properties>\n<maven.compiler.source>17</maven.compiler.source>\n<maven.compiler.target>17</maven.compiler.target>\n</properties>"
        patched = jdk.patch_pom_release(old, 11)
        self.assertIn("<maven.compiler.release>11</maven.compiler.release>", patched)
        self.assertNotIn(">17<", patched)

    def test_repair_ladder(self):
        j11, j17 = fake_java(11), fake_java(17)
        cur = jdk.probe(jdk._exe(j11)) | {"source": "JAVA_HOME"}
        ex = {"failure_reason": SCREENSHOT_ERROR}
        self.assertEqual(runtime_repair.diagnose(ex, ""), "JDK_MISMATCH")
        jdks = [jdk.probe(jdk._exe(j17))]
        p = runtime_repair.plan_repair("selenium-java", ex, "", [], jdks=jdks, current_jdk=cur)
        self.assertEqual((p["repair_id"], p["env"]["JAVA_HOME"]), (f"jdk:{j17}", j17))
        self.assertEqual(p["path_prepend"], [os.path.join(j17, "bin")])
        self.assertIn("Java 11", p["explanation"])
        p = runtime_repair.plan_repair("selenium-java", ex, "", [f"jdk:{j17}"], jdks=jdks, current_jdk=cur)
        self.assertEqual((p["repair_id"], p["java_release"]), ("java_release:11", 11))
        p = runtime_repair.plan_repair("selenium-java", ex, "", [f"jdk:{j17}", "java_release:11"], jdks=jdks, current_jdk=cur)
        self.assertEqual((p["repair_id"], p["download_runtime"]), ("download:jdk-17", "jdk-17"))
        p = runtime_repair.plan_repair("selenium-java", ex, "", [f"jdk:{j17}", "java_release:11", "download:jdk-17"],
                                       jdks=jdks, current_jdk=cur)
        self.assertTrue(p["exhausted"])
        # the path the user typed in their correction comes first
        p = runtime_repair.plan_repair("selenium-java", ex, "", [], feedback=[f"my JDK 17 is in {j17}/bin/java"],
                                       jdks=[], current_jdk=cur)
        self.assertEqual(p["repair_id"], f"jdk:user:{j17}")

    def test_ai_advised_repair_is_validated(self):
        j17 = fake_java(17)
        facts = {"jdks_found": [{"home": j17, "major": 17}], "browsers_found": {}}
        plan, probs = runtime_repair.validate_ai_repair({"action": "set_env", "env": {"JAVA_HOME": j17},
                                                         "explanation": "use 17"}, facts, None, [])
        self.assertEqual((plan["repair_id"], plan["path_prepend"]), (f"ai:env:JAVA_HOME={j17}", [os.path.join(j17, "bin")]))
        for bad in ({"action": "set_env", "env": {"JAVA_HOME": "/nope/jdk"}},           # doesn't exist
                    {"action": "set_env", "env": {"LD_PRELOAD": "/tmp/x.so"}},          # not allowed
                    {"action": "set_env", "env": {"JAVA_HOME": tempfile.mkdtemp()}},    # exists, but unknown path
                    {"action": "download_runtime", "download_runtime": "jdk-8"},
                    {"action": "set_env", "env": {"MAVEN_OPTS": "-javaagent:/tmp/x.jar"}}):
            plan, probs = runtime_repair.validate_ai_repair(dict(bad, explanation="x"), facts, None, [])
            self.assertIsNone(plan, bad)
            self.assertTrue(probs)
        plan, _ = runtime_repair.validate_ai_repair({"action": "reinstall", "explanation": "x"}, facts, None, ["reinstall"])
        self.assertIsNone(plan)                                                     # never repeats a tried repair
        plan, _ = runtime_repair.validate_ai_repair({"action": "none", "explanation": "Install a JDK.",
                                                     "human_steps": "Ask IT."}, facts, None, [])
        self.assertTrue(plan["exhausted"])
        install_fake(FakeLLM({"RUNTIME_REPAIR": {"diagnosis": "JAVA_HOME points at JDK 11", "action": "set_env",
                                                 "env": {"JAVA_HOME": j17}, "explanation": "Use the JDK 17."}}))
        spec = FrameworkSpec.from_dict(CATALOG["selenium-java"].to_dict())
        plan = runtime_repair.ai_plan_repair(spec, {"failure_reason": SCREENSHOT_ERROR}, "", [], "JDK_MISMATCH", None, facts)
        self.assertEqual((plan["source"], plan["env"]["JAVA_HOME"]), ("ai", j17))


class TestSessionRuntimeRepair(Base):
    def _session(self):
        from core.wizard import session as wz
        spec = CATALOG["selenium-java"]
        pom = dependency_resolver.pom_xml(FrameworkSpec.from_dict(spec.to_dict()), 17)
        return wz, {"session_id": "wiz-v82-test", "framework": spec.to_dict(), "runtime_env": {},
                    "suite": {"files": {"pom.xml": pom, "src/test/java/automation/AutomationTest.java": "class A {}"}}}

    def test_reviewed_repairs_are_applied_exactly(self):
        wz, s = self._session()
        j17 = fake_java(17)
        e = wz._apply_runtime_repair(s, {"failure_reason": SCREENSHOT_ERROR}, "", plan={
            "cause": "JDK_MISMATCH", "repair_id": f"jdk:{j17}", "env": {"JAVA_HOME": j17},
            "path_prepend": [os.path.join(j17, "bin")], "explanation": "use 17", "exhausted": False})
        self.assertEqual(e["result"], "APPLIED")
        self.assertEqual((s["runtime_env"]["JAVA_HOME"], s["runtime_env"]["__path_prepend"]), (j17, os.path.join(j17, "bin")))
        e = wz._apply_runtime_repair(s, {"failure_reason": SCREENSHOT_ERROR}, "", plan={
            "cause": "JDK_MISMATCH", "repair_id": "java_release:11", "java_release": 11, "explanation": "compile for 11",
            "exhausted": False})
        self.assertEqual(s["runtime_env"]["__java_release"], "11")
        self.assertIn("<maven.compiler.release>11</maven.compiler.release>", s["suite"]["files"]["pom.xml"])  # exported too
        out = {"files": {"pom.xml": dependency_resolver.pom_xml(FrameworkSpec.from_dict(s["framework"]), 17)}}
        wz._apply_java_release(s, out["files"])                                  # a rebuilt suite keeps the release
        self.assertIn("<maven.compiler.release>11</maven.compiler.release>", out["files"]["pom.xml"])
        self.assertEqual([r["repair_id"] for r in s["runtime_repairs"]], [f"jdk:{j17}", "java_release:11"])

    def test_user_correction_and_ai_plan_the_next_repair(self):
        wz, s = self._session()
        j17, j11 = fake_java(17), fake_java(11)
        ex = {"failure_reason": SCREENSHOT_ERROR}
        with mock.patch.dict(os.environ, {"JAVA_HOME": j11}):
            p = wz._plan_runtime(s, ex, "", [f"JDK 17 is installed in {j17}"])
            self.assertEqual(p["repair_id"], f"jdk:user:{j17}")
            # rules exhausted -> the AI proposes one validated action
            s["runtime_repairs"] = [{"repair_id": r} for r in (f"jdk:user:{j17}", "java_release:11", "download:jdk-17")]
            install_fake(FakeLLM({"RUNTIME_REPAIR": {"diagnosis": "Maven uses JDK 11", "action": "set_env",
                                                     "env": {"JAVA_HOME": j17}, "explanation": "Point Maven at JDK 17."}}))
            with mock.patch.object(jdk, "find_jdks", return_value=[]):
                p = wz._plan_runtime(s, ex, "", [f"JDK 17 is installed in {j17}"])
            self.assertEqual((p["source"], p["env"]), ("ai", {"JAVA_HOME": j17}), p)

    def test_builtin_driver_needs_playwright_active(self):
        from core.wizard import session as wz
        self.assertFalse(wz.reference_available())                               # nothing is pre-built
        with mock.patch.dict(os.environ, {"UNDERSTUDY_DEFAULT_TOOLS": "playwright"}):
            ap.reset_to_defaults()
        self.assertTrue(wz.reference_available())


# ---------------------------------------------------------------------------------------------------------------
# 3. Cypress 16 removed Cypress.env()
# ---------------------------------------------------------------------------------------------------------------
class TestCypressEnv(unittest.TestCase):
    def test_template_uses_a_task_not_cypress_env(self):
        spec = FrameworkSpec.from_dict(CATALOG["cypress"].to_dict())
        steps = [{"order": 1, "action": "navigate", "value": "", "description": "Open"},
                 {"order": 2, "action": "fill", "selector": "#name", "label": "Name", "param_key": "name"},
                 {"order": 3, "action": "click", "selector": "#go", "label": "Go"}]
        params = [{"key": "name", "value": "Globex Corp", "type": "text"}, {"key": "token", "value": "", "type": "secret"}]
        files, entry = templates.render("cypress", steps, params, None, spec)
        blob = "\n".join(files.values())
        self.assertNotRegex(blob, r"Cypress\.env\s*\(|Cypress\.expose\s*\(|cy\.env\s*\(")
        self.assertIn("cy.task(\"understudyEnv\")", files[entry])
        self.assertIn("understudyEnv()", files["cypress.config.js"])
        self.assertEqual([f for f in governance.scan_suite(files, params, "http://x") if f["severity"] == "blocking"], [])
        bad = governance.scan_suite({"cypress/e2e/a.cy.js": "cy.visit(Cypress.env('TARGET_URL'))"}, [], "http://x")
        self.assertTrue(any(f["kind"] == "REMOVED_API" for f in bad))


# ---------------------------------------------------------------------------------------------------------------
# 4. Signup limited to company email domains
# ---------------------------------------------------------------------------------------------------------------
class TestSignupDomains(Base):
    def test_admin_limits_signup_to_a_domain(self):
        r = self.admin.put("/api/admin/signup/domains", json={"domains": ["@Acme.com", "*.corp.example", "acme.com"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["domains"], ["acme.com", "*.corp.example"])
        self.assertEqual(self.admin.put("/api/admin/signup/domains", json={"domains": ["not a domain"]}).status_code, 400)
        self.assertTrue(ap.email_domain_allowed("dev@acme.com"))
        self.assertTrue(ap.email_domain_allowed("dev@eu.corp.example"))
        self.assertFalse(ap.email_domain_allowed("dev@acme.com.evil.io"))
        self.assertFalse(ap.email_domain_allowed("dev@gmail.com"))
        import server
        c = TestClient(server.app)
        self.assertEqual(c.get("/api/auth/options").json()["signup_domains"], ["acme.com", "*.corp.example"])
        r = c.post("/api/auth/signup", json={"username": "outsider", "email": "outsider@gmail.com", "password": "Passw0rd!x",
                                             "confirm_password": "Passw0rd!x"})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertIn("@acme.com", r.json()["detail"]["message"])
        r = c.post("/api/auth/signup", json={"username": "insider82", "email": "insider82@acme.com", "password": "Passw0rd!x",
                                             "confirm_password": "Passw0rd!x"})
        self.assertEqual(r.status_code, 200, r.text)
        # the admin narrows the list while the code is on its way: finishing is refused too
        ap.set_signup_domains(["other.com"])
        with mock.patch("core.wizard.otp.verify", return_value={"ticket": "t"}), \
                mock.patch("core.wizard.otp.consume", return_value={"username": "insider82", "email": "insider82@acme.com",
                                                                    "data": {"salt": "00", "hash": "00"}}):
            with self.assertRaises(users.UserError) as e:
                users.finish_signup("x", "123456")
        self.assertEqual(e.exception.status_code, 403)
        # admin-created accounts are not limited; null = back to the environment default (any domain)
        self.assertEqual(self.admin.put("/api/admin/signup/domains", json={"domains": None}).json()["domains"], [])
        with mock.patch.dict(os.environ, {"UNDERSTUDY_SIGNUP_DOMAINS": "corp.example"}):
            self.assertEqual(ap.signup_domains(), ["corp.example"])


if __name__ == "__main__":
    unittest.main()
