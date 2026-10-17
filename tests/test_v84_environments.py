"""8.4: Mission Control > Environments - every runtime version on the server, a per-tool version picker (several
versions side by side), standalone Python downloads, and per-tool version requirements checked before every run."""
import hashlib
import io
import json
import os
import tarfile
import tempfile
import unittest
from unittest import mock

from tests.wizard_helpers import admin_client
from core.wizard import admin_policy as ap, dependency_resolver, environments as envs, package_managers as pms, runtimes
from core.wizard.frameworks import FrameworkSpec, Package


def _script(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(f"#!/bin/sh\necho '{text}'\n")
    os.chmod(path, 0o755)
    return path


def _tgz(files):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, (content, mode) in files.items():
            data = content.encode()
            ti = tarfile.TarInfo(name)
            ti.size, ti.mode = len(data), mode
            t.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def _tool(tool_id, eco, packages, run, entry, language="javascript"):
    spec = FrameworkSpec(id=tool_id, name=tool_id.title(), language=language, ecosystem=eco,
                         packages=[Package(n, v) for n, v in packages], run_command=run, entry_file=entry)
    ap.whitelist_tool(spec, "admin")
    return spec


class TestVersionSpecs(unittest.TestCase):
    def test_normalize(self):
        cases = {"17+": ">=17", "Java 17+": ">=17", "3.9-3.12": ">=3.9,<=3.12", "3.9 – 3.12": ">=3.9,<=3.12",
                 ">=3.9,<3.13": ">=3.9,<3.13", "20": "==20", "20.x": "==20", "any": "any", "": "any",
                 "18 or newer": ">=18"}
        for text, want in cases.items():
            self.assertEqual(envs.normalize_spec(text), want, text)
        for bad in ("latest", ">=abc", "17;rm -rf /", ">=1,>=2,>=3,>=4,>=5"):
            with self.assertRaises(ValueError, msg=bad):
                envs.normalize_spec(bad)

    def test_satisfies(self):
        self.assertTrue(envs.satisfies("17.0.2", ">=17", "java"))
        self.assertTrue(envs.satisfies("17.0.2", "==17", "java"))
        self.assertFalse(envs.satisfies("1.8.0_292", ">=11", "java"))           # Java 8
        self.assertTrue(envs.satisfies("1.8.0_292", "==8", "java"))
        self.assertTrue(envs.satisfies("3.12.7", "<=3.12"))
        self.assertFalse(envs.satisfies("3.12.7", "<3.12"))
        self.assertFalse(envs.satisfies("3.14.0", ">=3.9,<=3.12"))
        self.assertTrue(envs.satisfies("3.10.1", "~=3.9"))
        self.assertFalse(envs.satisfies("4.0", "~=3.9"))
        self.assertFalse(envs.satisfies("", ">=1"))
        self.assertTrue(envs.satisfies("", "any"))
        self.assertEqual(envs.describe(">=3.9,<=3.12"), "3.9 or newer and 3.12 or older")

    def test_requirement_sentences(self):
        self.assertEqual(envs.parse_requirement_text("Java 17+ JDK"), ("java", ">=17"))
        self.assertEqual(envs.parse_requirement_text("Node.js 20+"), ("node", ">=20"))
        self.assertEqual(envs.parse_requirement_text("Python 3.9 to 3.12"), ("python", ">=3.9,<=3.12"))
        self.assertIsNone(envs.parse_requirement_text("Google Chrome"))

    def test_requirements_from_the_ai_stack(self):
        sug = {"runtime_requirements": [{"runtime": "java", "spec": "17+", "language": "java", "reason": "Selenium 4.2x"},
                                        {"runtime": "node", "spec": ">=20", "language": "javascript"},
                                        {"runtime": "evil", "spec": ">=1"}],
               "os_requirements": ["Maven 3.6 or newer", "Google Chrome", "Python 3.10+"]}
        got = envs.requirements_from_stack(sug, "java")
        self.assertEqual(got["java"], {"spec": ">=17", "source": "ai", "reason": "Selenium 4.2x"})
        self.assertEqual(got["maven"]["spec"], ">=3.6")
        self.assertNotIn("node", got)                     # another language
        self.assertNotIn("python", got)                   # not a Java runtime


class EnvBase(unittest.TestCase):
    def setUp(self):
        ap.reset_to_defaults()
        self.tmp = tempfile.mkdtemp(prefix="envs-")
        self.node18 = _script(os.path.join(self.tmp, "n18", "bin", "node"), "v18.19.1")
        self.node22 = _script(os.path.join(self.tmp, "n22", "bin", "node"), "v22.3.0")
        self.py311 = _script(os.path.join(self.tmp, "py311", "bin", "python3.11"), "Python 3.11.9")
        globs = {"node": [os.path.join(self.tmp, "n*", "bin", "node")],
                 "python": [os.path.join(self.tmp, "py*", "bin", "python3*")]}
        self.patches = [mock.patch.object(envs, "_globs", side_effect=lambda k: globs.get(k, [])),
                        mock.patch.object(runtimes, "RUNTIMES_DIR", os.path.join(self.tmp, "runtimes"))]
        for p in self.patches:
            p.start()
        envs._CACHE.clear()
        pms._VERSION_CACHE.clear()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        envs._CACHE.clear()
        runtimes.set_transport(None)
        ap.reset_to_defaults()

    def install(self, kind, path):
        return next(r for r in envs.inventory(refresh=True)[kind] if r["path"] == path)


class TestInventoryAndPicker(EnvBase):
    def test_several_versions_are_listed_side_by_side(self):
        inv = envs.inventory(refresh=True)
        self.assertEqual(set(inv), set(envs.KINDS))
        nodes = {r["path"]: r for r in inv["node"]}
        self.assertEqual(nodes[self.node18]["version"], "18.19.1")
        self.assertEqual(nodes[self.node22]["version"], "22.3.0")
        py = {r["id"]: r for r in inv["python"]}
        self.assertTrue(py["python-backend"]["default"])           # Understudy's own Python is the default
        self.assertIn(self.py311, [r["path"] for r in inv["python"]])

    def test_pick_a_node_version_for_one_tool_and_switch_back(self):
        _tool("wdio-js", "npm", [("webdriverio", "9.0.0")], ["node", "{entry}"], "run_automation.js")
        n18 = self.install("node", self.node18)
        view = envs.select("wdio-js", "node", n18["id"], "boss", verify=False)
        row = next(r for r in view["runtimes"] if r["kind"] == "node")
        self.assertEqual((row["current"]["version"], row["current"]["source"]), ("18.19.1", "picked in Mission Control"))
        self.assertTrue(row["ok"])                                  # rule: Node.js 18+
        self.assertEqual(ap.tool_runtime("wdio-js")["path_prepend"][0], os.path.dirname(self.node18))
        n22 = self.install("node", self.node22)
        envs.select("wdio-js", "node", n22["id"], "boss", verify=False)
        self.assertEqual(ap.tool_runtime("wdio-js")["path_prepend"], [os.path.dirname(self.node22)])   # no pile-up
        envs.select("wdio-js", "node", None, "boss", verify=False)
        self.assertEqual(ap.tool_runtime("wdio-js")["path_prepend"], [])
        self.assertEqual(ap.tool_runtime("wdio-js")["versions"], {})

    def test_requirement_check_offers_a_switch(self):
        _tool("wdio-js", "npm", [("webdriverio", "9.0.0")], ["node", "{entry}"], "run_automation.js")
        envs.select("wdio-js", "node", self.install("node", self.node18)["id"], "boss", verify=False)
        envs.set_requirements("wdio-js", {"node": "20+"}, "boss")
        chk = envs.check("wdio-js")
        self.assertFalse(chk["ok"])
        prob = chk["problems"][0]
        self.assertEqual((prob["kind"], prob["required"], prob["current"]), ("node", ">=20", "18.19.1"))
        self.assertEqual(prob["fix"]["type"], "switch")
        self.assertTrue(envs.satisfies(prob["fix"]["version"], ">=20"))
        envs.apply_fix("wdio-js", "node", prob["fix"], "boss")
        self.assertTrue(envs.check("wdio-js")["ok"])
        with self.assertRaises(envs.EnvironmentError_):
            envs.set_requirements("wdio-js", {"node": "newest please"}, "boss")

    def test_no_installed_version_fits_offers_a_download(self):
        _tool("wdio-js", "npm", [("webdriverio", "9.0.0")], ["node", "{entry}"], "run_automation.js")
        envs.set_requirements("wdio-js", {"node": "==20"}, "boss")
        fix = envs.check("wdio-js")["problems"][0]["fix"]
        self.assertEqual((fix["type"], fix["key"]), ("download", "node-20"))

    def test_python_pick_rebuilds_the_tools_venv_with_that_interpreter(self):
        spec = _tool("requests-api", "pip", [("requests", "")], ["python", "{entry}"], "run_automation.py", "python")
        before = dependency_resolver.plan_for(spec)
        envs.select("requests-api", "python", self.install("python", self.py311)["id"], "boss", verify=False)
        after = dependency_resolver.plan_for(spec)
        self.assertNotEqual(before.cache_key, after.cache_key)     # a new isolated environment
        self.assertEqual(after.commands[0][:3], [self.py311, "-m", "venv"])
        self.assertNotIn("--system-site-packages", after.commands[0])
        view = envs.tool_view("requests-api")
        self.assertEqual(view["runtimes"][0]["current"]["version"], "3.11.9")
        with self.assertRaises(envs.EnvironmentError_):
            envs.select("wdio-none", "python", "python-backend", "boss", verify=False)

    def test_java_pick_replaces_the_setup_jdk(self):
        _tool("selenium-java", "maven", [("org.seleniumhq.selenium:selenium-java", "4.20.0")], ["mvn", "-q", "test"],
              "src/test/java/automation/AutomationTest.java", "java")
        jhome = os.path.join(self.tmp, "jdk17")
        os.makedirs(os.path.join(jhome, "bin"))
        with ap._LOCK:
            rt = ap._load()["tool_whitelist"]["selenium-java"].setdefault("setup", {}).setdefault("runtime", {})
            rt.update(java_home="/old/jdk", java_release=11, env_vars={"JAVA_HOME": "/old/jdk"})
        fake = {"install_id": "java-x", "version": "17.0.9", "path": os.path.join(jhome, "bin", "java"), "home": jhome,
                "bin": os.path.join(jhome, "bin"), "env": {"JAVA_HOME": jhome}, "source": "system"}
        rt = ap.set_tool_runtime_choice("selenium-java", "java", fake, "boss")
        self.assertEqual(rt["env_vars"]["JAVA_HOME"], jhome)
        self.assertIsNone(rt["java_release"])                      # recompiled for the new JDK
        self.assertEqual(rt["path_prepend"][0], os.path.join(jhome, "bin"))
        rt = ap.set_tool_runtime_choice("selenium-java", "java", None, "boss")
        self.assertNotIn("JAVA_HOME", rt["env_vars"])


class TestPythonDownload(EnvBase):
    def _transport(self, archive, digest=True, url_prefix="https://github.com/astral-sh/python-build-standalone/releases/download/"):
        sha = hashlib.sha256(archive).hexdigest()
        name = f"cpython-3.12.7+20241016-{runtimes._pbs_triple()}-install_only.tar.gz"
        asset = {"name": name, "browser_download_url": url_prefix + "20241016/" + name}
        if digest:
            asset["digest"] = "sha256:" + sha
        other = {"name": f"cpython-3.11.10+20241016-{runtimes._pbs_triple()}-install_only.tar.gz",
                 "browser_download_url": url_prefix + "x", "digest": "sha256:" + "0" * 64}
        rels = [{"tag_name": "20241016", "assets": [other, asset], "body": ""}]

        def transport(url):
            if url == runtimes.PYTHON_BUILDS_API:
                return json.dumps(rels).encode()
            if url == asset["browser_download_url"]:
                return archive
            raise AssertionError(url)
        return transport

    def test_side_by_side_python_is_checksum_verified_and_never_the_server_default(self):
        archive = _tgz({"python/bin/python3": ("#!/bin/sh\necho 'Python 3.12.7'\n", 0o755)})
        runtimes.set_transport(self._transport(archive))
        pms.download("python-3.12", "boss", background=False)
        job = pms._JOBS["python-3.12"]
        self.assertEqual(job["status"], "done", job)
        self.assertTrue(job["side_by_side"])
        self.assertNotIn("python", ap.toolchains())                # Understudy keeps running on its own Python
        rec = next(r for r in envs.inventory(refresh=True)["python"] if r["download_key"] == "python-3.12")
        self.assertEqual(rec["version"], "3.12.7")
        self.assertIn("downloaded", rec["sources"])

    def test_download_then_switch_the_tool(self):
        _tool("requests-api", "pip", [("requests", "")], ["python", "{entry}"], "run_automation.py", "python")
        archive = _tgz({"python/bin/python3": ("#!/bin/sh\necho 'Python 3.12.7'\n", 0o755)})
        runtimes.set_transport(self._transport(archive))
        with mock.patch.object(envs, "_start_verify"):
            envs.download("python-3.12", "boss", tool_id="requests-api", background=False)
        cur = envs.current("requests-api", "python")
        self.assertEqual((cur["version"], cur["source"]), ("3.12.7", "picked in Mission Control"))

    def test_bad_checksum_or_location_is_refused(self):
        archive = _tgz({"python/bin/python3": ("#!/bin/sh\necho 'Python 3.12.7'\n", 0o755)})
        good = self._transport(archive)

        def tampered(url):
            data = good(url)
            return data + b"x" if url.endswith(".tar.gz") else data
        runtimes.set_transport(tampered)
        with self.assertRaises(runtimes.RuntimeError_):
            runtimes.provision("python-3.12")
        runtimes.set_transport(self._transport(archive, url_prefix="https://evil.example/"))
        with self.assertRaises(runtimes.RuntimeError_):
            runtimes.provision("python-3.12")


class TestActivationRecordsRequirements(EnvBase):
    def test_ai_requirements_and_tested_with(self):
        from core.wizard import tool_setup
        spec = _tool("wdio-js", "npm", [("webdriverio", "9.0.0")], ["node", "{entry}"], "run_automation.js")
        r = {"stack": {"selection": {"language": "javascript"},
                       "suggestion": {"runtime_requirements": [{"runtime": "node", "spec": ">=18.20", "reason": "wdio 9"}]}}}
        tool_setup._record_environment(spec, r)
        self.assertEqual(ap.tool_requirements("wdio-js")["node"]["spec"], ">=18.20")
        self.assertEqual(envs.requirements("wdio-js")["node"]["source"], "ai")
        self.assertTrue(ap.tool_tested_with("wdio-js").get("node"))


class TestAdminApi(EnvBase):
    def test_overview_and_edits(self):
        _tool("wdio-js", "npm", [("webdriverio", "9.0.0")], ["node", "{entry}"], "run_automation.js")
        c = admin_client()
        r = c.get("/api/admin/environments")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual([k["kind"] for k in body["kinds"]], list(envs.KINDS))
        self.assertIn("python-3.12", [d["key"] for d in body["downloadable"]])
        tool = next(t for t in body["tools"] if t["tool_id"] == "wdio-js")
        self.assertEqual(tool["runtimes"][0]["requirement"]["spec"], ">=18")
        n18 = self.install("node", self.node18)
        r = c.post("/api/admin/environments/tools/wdio-js/select", json={"kind": "node", "install_id": n18["id"], "verify": False})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["tool"]["runtimes"][0]["current"]["version"], "18.19.1")
        r = c.put("/api/admin/environments/tools/wdio-js/requirements", json={"requirements": {"node": "bogus"}})
        self.assertEqual(r.status_code, 400)
        r = c.put("/api/admin/environments/tools/wdio-js/requirements", json={"requirements": {"node": "any"}})
        self.assertTrue(r.json()["tool"]["ok"])
        r = c.post("/api/admin/environments/tools/wdio-js/select", json={"kind": "node", "install_id": "node-nope"})
        self.assertEqual(r.status_code, 404)


# ---------------------------------------------------------------------------------------------------------------
# The check before every run
# ---------------------------------------------------------------------------------------------------------------
from tests.test_wizard_self_healing import PASS_EXEC, RetryLoopBase  # noqa: E402
from core.wizard import escalations, notifications, session as wz  # noqa: E402

PROBLEM = {"kind": "python", "label": "Python", "required": "==3.11", "required_text": "3.11.x", "reason": "", "source": "admin",
           "current": "3.13.1", "current_path": "/usr/bin/python3",
           "message": "Playwright needs Python 3.11.x, but its runs use Python 3.13.1 (/usr/bin/python3).",
           "fix": {"type": "switch", "install_id": "python-abc", "version": "3.11.9", "path": "/opt/py311/bin/python3",
                   "label": "Use Python 3.11.9 (/opt/py311/bin/python3)"}}


class TestRunIsHeld(RetryLoopBase):
    def test_unmet_requirement_holds_the_run_until_fixed_or_overridden(self):
        held = {"ok": False, "tool_id": "playwright-python", "problems": [dict(PROBLEM)]}
        with mock.patch.object(envs, "check", return_value=held), \
                mock.patch("core.wizard.executor.execute") as ex:
            s = wz.execute(self.sid, background=False)
        ex.assert_not_called()
        self.assertFalse(s["runtime_check"]["ok"])
        self.assertEqual(wz.public_view(s)["runtime_check"]["problems"][0]["kind"], "python")
        # the user sends the one-click fix to the admin
        with mock.patch.object(envs, "check", return_value=held):
            s = wz.request_runtime_fix(self.sid, "python")
            with self.assertRaises(wz.WizardError):
                wz.request_runtime_fix(self.sid, "python")            # only once
        esc_id = s["runtime_check"]["sent"]["python"]
        doc = escalations.get_for_admin(esc_id)
        self.assertEqual(doc["admin_action"]["action"], "use_version")
        self.assertEqual(doc["admin_action"]["fix"]["install_id"], "python-abc")
        c = admin_client()
        with mock.patch.object(envs, "apply_fix", return_value="switched the tool to Python 3.11.9") as af:
            r = c.post(f"/api/admin/escalations/{esc_id}/decide", json={"decision": "confirm"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(af.call_args[0][:3], ("playwright-python", "python", doc["admin_action"]["fix"]))
        owner = wz.get(self.sid).get("owner") or "local"
        self.assertIn("switched the tool to Python 3.11.9", notifications.items(owner)[0]["title"])

    def test_run_anyway_and_passing_check(self):
        held = {"ok": False, "tool_id": "playwright-python", "problems": [dict(PROBLEM)]}
        with mock.patch.object(envs, "check", return_value=held), \
                mock.patch("core.wizard.executor.execute", side_effect=[dict(PASS_EXEC)]) as ex:
            s = wz.execute(self.sid, background=False, ignore_requirements=True)
        ex.assert_called_once()
        self.assertTrue(s["runtime_check"]["ignored"])
        s, ex = self.run_execute([PASS_EXEC])                     # the real check passes on this machine
        ex.assert_called_once()
        self.assertIsNone(s["runtime_check"])

    def test_admin_action_use_version_from_the_escalation_panel(self):
        s = wz.get(self.sid)
        esc = escalations.create_help(s, {"action": "server_change", "request": "needs another Python"}, "why")
        c = admin_client()
        with mock.patch.object(envs, "apply_fix", return_value="started downloading Python 3.12") as af:
            r = c.post(f"/api/admin/escalations/{esc['id']}/decide", json={
                "decision": "confirm", "actions": [{"type": "use_version", "kind": "python", "key": "python-3.12"}]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(af.call_args[0][2], {"type": "download", "key": "python-3.12"})


if __name__ == "__main__":
    unittest.main()
