"""9.3: the admin decides whether an escalation's fix applies to EVERYONE (the tool) or only to the USER who asked.
Per-user changes (packages, allowed versions, run variables, PATH folders) reach only that user's runs; server-wide
settings (proxy bypass, network mode, runtime switch / download) always apply to everyone."""
import os
import tempfile

from tests.test_v83_fixes import EscalationBase
from tests.wizard_helpers import admin_client
from core.wizard import admin_policy as ap, escalations, notifications, session as wz

ASK = "Please add 'robotframework-jsonlibrary' to the list of required packages."


class TestScope(EscalationBase):
    def esc(self, act=None):
        s = self._verify_fails()
        s["owner"] = "alice"
        return s, escalations.create_help(s, act or {"action": "server_change", "request": ASK}, "why", "diag")

    def decide(self, esc, **body):
        r = admin_client().post(f"/api/admin/escalations/{esc['id']}/decide", json=dict({"decision": "confirm"}, **body))
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["escalation"]

    def test_package_for_this_user_only(self):
        s, esc = self.esc()
        tool = s["framework"]["id"]
        doc = self.decide(esc, scope="user")
        self.assertEqual(doc["scope"], "user")
        self.assertNotIn("robotframework-jsonlibrary", [p["name"] for p in ap.whitelist()[tool]["spec"]["packages"]])
        self.assertIn("robotframework-jsonlibrary", [p.name for p in ap.allowed_spec(tool, user="alice").packages])
        self.assertNotIn("robotframework-jsonlibrary", [p.name for p in ap.allowed_spec(tool, user="bob").packages])
        self.assertIn("robotframework-jsonlibrary", [p["name"] for p in wz.get(self.sid)["framework"]["packages"]])
        self.assertIn("for you", notifications.items("alice")[0]["title"])
        rows = admin_client().get("/api/admin/user-overrides").json()["overrides"]
        self.assertTrue(any(r["user"] == "alice" and r["key"] == "robotframework-jsonlibrary" for r in rows))
        r = admin_client().post("/api/admin/user-overrides/remove", json={"user": "alice", "tool_id": tool,
                                                                          "kind": "package", "key": "robotframework-jsonlibrary"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn("robotframework-jsonlibrary", [p.name for p in ap.allowed_spec(tool, user="alice").packages])

    def test_package_for_everyone_is_unchanged(self):
        s, esc = self.esc()
        self.decide(esc)                                                        # default scope: everyone
        self.assertIn("robotframework-jsonlibrary", [p["name"] for p in ap.whitelist()[s["framework"]["id"]]["spec"]["packages"]])

    def test_version_env_and_path_for_this_user(self):
        s, esc = self.esc({"action": "server_change", "request": "Configure Chrome"})
        tool = s["framework"]["id"]
        ap.set_tool_versions(tool, ["1.55.0"], "1.55.0")
        folder = tempfile.mkdtemp()
        doc = self.decide(esc, scope="user", actions=[{"type": "allow_version", "version": "1.44.0"},
                                                      {"type": "set_env", "name": "CHROME_BIN", "value": "/usr/bin/chromium"},
                                                      {"type": "path_prepend", "path": folder}])
        self.assertEqual(len(doc["admin_actions_done"]), 3)
        self.assertIn("1.44.0", ap.tool_version_policy(tool, "alice")["allowed"])
        self.assertNotIn("1.44.0", ap.tool_version_policy(tool, "bob")["allowed"])
        self.assertNotIn("1.44.0", ap.tool_version_policy(tool)["allowed"])
        self.assertEqual(ap.tool_runtime(tool, "alice")["env_vars"]["CHROME_BIN"], "/usr/bin/chromium")
        self.assertNotIn("CHROME_BIN", ap.tool_runtime(tool)["env_vars"])
        env = wz._user_runtime_env(wz.get(self.sid))                           # what alice's next run gets
        self.assertEqual(env["CHROME_BIN"], "/usr/bin/chromium")
        self.assertIn(folder, env["__path_prepend"].split(os.pathsep))

    def test_server_settings_always_apply_to_everyone(self):
        s, esc = self.esc({"action": "server_change", "request": "Bypass the proxy"})
        self.decide(esc, scope="user", actions=[{"type": "proxy_bypass", "host": "erp.intranet.local"}])
        self.assertIn("erp.intranet.local", ap.proxy_bypass_hosts())

    def test_bad_scope(self):
        s, esc = self.esc()
        r = admin_client().post(f"/api/admin/escalations/{esc['id']}/decide", json={"decision": "confirm", "scope": "team"})
        self.assertEqual(r.status_code, 400, r.text)
