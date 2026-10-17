"""7.2: the admin allows SEVERAL versions of a tool; the user sees exactly those, picks one, and that version is the
one installed in the sandbox before execution - also after switching versions, and also when the sandbox lost it.

* always: the picker API lists the admin's versions and default; a version outside the list is refused (403); the
  user's pick (not the admin default) reaches the spec, the install plan, the generated manifest; switching versions
  re-plans; the executor's pre-run check installs the chosen version when the sandbox has a different one.
* live (PyPI reachable): Python Requests 2.31.0 and 2.32.3 really installed in turn into the isolated sandbox; the
  run against Portal 1 uses the chosen one with the tool's own runtime, VERIFY passes, the ZIP pins it.
"""
import io
import os
import shutil
import unittest
import zipfile
from unittest import mock

from fastapi.testclient import TestClient

from tests.wizard_helpers import LiveServer, fake_mp4, install_fake, govern
from scripts import tool_matrix as TM
from core.wizard import admin_policy as ap, dependency_resolver, dependency_lock
from core.wizard import session as wz

TOOL, V_OLD, V_NEW = "requests-api", "2.31.0", "2.32.3"
PYPI = TM._reachable("pypi.org")


class VersionChoiceBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        install_fake(None)
        cls.srv = LiveServer.get()

    def setUp(self):
        import server
        ap.reset_to_defaults()
        ap.set_tool_versions(TOOL, [V_OLD, V_NEW], default=V_NEW)          # admin: two versions, default the newer
        self.c = TestClient(server.app)
        os.environ["UNDERSTUDY_DEPENDENCY_INSTALL"] = "dry-run"

    def tearDown(self):
        os.environ.pop("UNDERSTUDY_DEPENDENCY_INSTALL", None)

    def session(self):
        s = wz.create_session(self.srv.base + "/portal1", TM.SOP, fake_mp4(), "rec.mp4", background=False)
        return s["session_id"]

    def choose(self, sid, version=None):
        r = self.c.post(f"/api/wizard/sessions/{sid}/framework?wait=true",
                        json={"framework_id": TOOL, **({"version": version} if version else {})})
        return r


class TestUserPicksAmongAdminVersions(VersionChoiceBase):
    def test_picker_shows_exactly_the_admin_versions(self):
        info = self.c.get(f"/api/wizard/frameworks/{TOOL}/versions").json()
        self.assertEqual((info["restricted"], info["versions"], info["default"]), (True, [V_OLD, V_NEW], V_NEW))
        sid = self.session()
        s = self.choose(sid).json()["session"]
        opt = next(o for o in s["framework_options"] if o["id"] == TOOL)
        self.assertEqual(opt["versions"]["allowed"], [V_OLD, V_NEW])          # what the UI's version picker lists
        self.assertEqual(s["framework"]["packages"][0]["version"], "==" + V_NEW)   # no pick -> admin default

    def test_users_pick_wins_and_reaches_plan_and_manifest(self):
        sid = self.session()
        s = self.choose(sid, V_OLD).json()["session"]
        self.assertEqual(s["framework"]["packages"][0]["version"], "==" + V_OLD)
        self.assertIn(f"requests=={V_OLD}", s["dependencies"]["plan"]["manifest_files"]["requirements.txt"])
        govern(sid)
        s = wz.build_suite(sid, background=False)
        self.assertIn(f"requests=={V_OLD}", s["suite"]["files"]["requirements.txt"].split())
        # switching versions re-plans the sandbox for the new pick
        s = self.choose(sid, V_NEW).json()["session"]
        self.assertIn(f"requests=={V_NEW}", s["dependencies"]["plan"]["manifest_files"]["requirements.txt"])
        self.assertNotEqual(dependency_resolver.plan_for(wz._spec(s)).env_dir,
                            dependency_resolver.plan_for(dependency_lock.effective_spec(
                                ap.allowed_spec(TOOL, V_OLD), [])).env_dir)       # each version: its own environment

    def test_version_outside_the_admin_list_is_refused(self):
        sid = self.session()
        r = self.choose(sid, "2.30.0")
        self.assertEqual(r.status_code, 403)
        self.assertIn("not allowed by your admin", r.json()["detail"]["message"])

    def test_pre_run_check_installs_the_chosen_version(self):
        sid = self.session()
        self.choose(sid, V_OLD)
        govern(sid)
        s = wz.build_suite(sid, background=False)
        spec = wz._spec(s)
        s["dependencies"] = {"status": "READY", "plan": {"cache_key": dependency_resolver.plan_for(spec).cache_key},
                             "framework_id": spec.id, "versions": []}
        wrong = [{"name": "requests", "requested": "==" + V_OLD, "installed": V_NEW, "source": "isolated environment"}]
        lines = []
        with mock.patch.dict(os.environ, {"UNDERSTUDY_DEPENDENCY_INSTALL": "auto"}), \
                mock.patch.object(dependency_resolver, "installed_versions", return_value=wrong), \
                mock.patch.object(dependency_resolver, "install",
                                  return_value={"status": "READY", "message": "ok", "plan": {}}) as inst:
            wz._ensure_dependencies(s, lines.append)
        installed_spec = inst.call_args.args[0]
        self.assertEqual(installed_spec.packages[0].version, "==" + V_OLD)
        self.assertTrue(any(f"{V_NEW} is installed but {V_OLD} is required" in ln for ln in lines), lines)

    def test_a_slow_install_of_the_previous_pick_never_overwrites_the_new_pick(self):
        """Regression (found recording the demo): the user switched 2.32.3 -> 2.31.0 while 2.32.3 was still
        installing; the old install finished last, FAILED, and overwrote the READY status of the new pick."""
        import threading
        sid = self.session()
        release = threading.Event()

        def fake_install(spec, log=None, approved=False):
            if spec.packages[0].version == "==" + V_NEW:      # the first (default) pick: slow and failing
                release.wait(10)
                return {"status": "FAILED", "message": "download failure", "plan": {}}
            return {"status": "READY", "message": f"using requests {V_OLD}", "plan": {}}

        with mock.patch.dict(os.environ, {"UNDERSTUDY_DEPENDENCY_INSTALL": "auto"}), \
                mock.patch.object(dependency_resolver, "install", side_effect=fake_install):
            wz.select_framework(sid, TOOL, None, background=True)   # admin default 2.32.3: install hangs in background
            for _ in range(100):
                if (wz.get(sid).get("dependencies") or {}).get("status") == "INSTALLING":
                    break
                threading.Event().wait(0.05)
            self.assertEqual(wz.get(sid)["dependencies"]["status"], "INSTALLING")
            s = wz.select_framework(sid, TOOL, None, background=False, version=V_OLD)
            self.assertEqual(s["dependencies"]["status"], "READY")
            release.set()
            for _ in range(100):                                  # let the stale job finish
                if not any(t.name == f"deps-{sid}" and t.is_alive() for t in threading.enumerate()):
                    break
                threading.Event().wait(0.05)
        s = wz.get(sid)
        self.assertEqual(s["dependencies"]["status"], "READY")
        self.assertIn(V_OLD, s["dependencies"]["message"])
        self.assertEqual(s["framework"]["packages"][0]["version"], "==" + V_OLD)


@unittest.skipUnless(PYPI, "PyPI not reachable from this machine")
class TestChosenVersionReallyInstalledAndRun(VersionChoiceBase):
    def setUp(self):
        super().setUp()
        os.environ["UNDERSTUDY_DEPENDENCY_INSTALL"] = "auto"

    def installed(self, sid):
        rows = dependency_resolver.installed_versions(wz._spec(wz.get(sid)))
        return next(r for r in rows if r["name"] == "requests")

    def test_each_pick_is_installed_and_the_run_uses_it(self):
        sid = self.session()
        self.assertEqual(self.choose(sid, V_OLD).status_code, 200)
        r = self.installed(sid)
        self.assertEqual((r["installed"], r["source"]), (V_OLD, "isolated environment"))
        self.choose(sid, V_NEW)
        r = self.installed(sid)
        self.assertEqual((r["installed"], r["source"]), (V_NEW, "isolated environment"))
        # back to the older one, and simulate a lost sandbox (another instance / cleaned cache): the run reinstalls it
        self.choose(sid, V_OLD)
        shutil.rmtree(dependency_resolver.plan_for(wz._spec(wz.get(sid))).env_dir, ignore_errors=True)
        dependency_resolver._READY_CACHE.clear()
        params = [dict(p) for p in wz.get(sid)["parameters"]]
        for p in params:
            if p.get("type") == "secret":
                p["value"] = TM.ERP_TOKEN
        wz.update_parameters(sid, params)
        self.assertEqual(govern(sid)["pillars"]["GOVERN"]["status"], "PASS")
        s = wz.build_suite(sid, background=False)
        self.assertIn(f"requests=={V_OLD}", s["suite"]["files"]["requirements.txt"].split())
        s = wz.execute(sid, background=False)
        logs = "\n".join(s["execution"]["logs"])
        self.assertEqual(s["pillars"]["BUILD-EXECUTE"]["status"], "PASS", logs[-2000:])
        self.assertEqual(s["execution"]["strategy"], "framework")
        self.assertIn("Installing the approved dependencies into the isolated sandbox before the run", logs)
        self.assertEqual(self.installed(sid)["installed"], V_OLD)
        self.assertEqual(s["pillars"]["EXPORT"]["status"], "PASS")
        z = zipfile.ZipFile(io.BytesIO(wz.export_zip(sid)["data"]))
        self.assertIn(f"requests=={V_OLD}", z.read("requirements.txt").decode().split())


if __name__ == "__main__":
    unittest.main()
