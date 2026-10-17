"""9.7: Cloud Run readiness - state that must be shared by every instance, and the deployment check.

* Login lock-outs (Studio and Mission Control) live in the durable store when it is shared (Firestore on Cloud Run):
  another instance - simulated by forgetting this instance's memory - still sees the lock.
* Model Armor interceptions live in the durable store: a restart (module reload) keeps them; clear empties them.
* /api/health says where Understudy is hosted and which Google AI it uses; scripts/check_deployment.py reads that.
"""
import importlib
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from fastapi.testclient import TestClient

from core.wizard import admin_auth, lockout, security_alerts, store, users


class SharedLocalStore(store.LocalStore):
    """A local store that reports itself as shared, like Firestore."""
    kind = "firestore"


class SharedStoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="us-shared-")
        self.prev = store.get_store()
        store.set_store(SharedLocalStore(self.tmp))
        lockout.reset()

    def tearDown(self):
        store.set_store(self.prev)
        lockout.reset()


class LockoutTest(SharedStoreCase):
    def test_studio_lockout_is_seen_by_another_instance(self):
        with mock.patch.dict(os.environ, {"UNDERSTUDY_USER_LOGIN": "required"}):
            users.create_user("carol", "Secret123x", "admin", email="carol@example.com")
            for _ in range(users.MAX_FAILS):
                with self.assertRaises(users.UserError):
                    users.login("carol", "wrong-password-1", client="10.0.0.9")
            lockout._MEM.clear()                         # "another instance": nothing in this process's memory
            with self.assertRaises(users.UserError) as cm:
                users.login("carol", "Secret123x", client="10.0.0.10")
            self.assertEqual(cm.exception.status_code, 429)
            self.assertTrue(store.get_store().list(lockout.COLLECTION))       # the lock is in the shared store
            ids = " ".join(store.get_store().list(lockout.COLLECTION))
            self.assertNotIn("carol", ids)                                     # hashed keys only

    def test_admin_lockout_is_shared_too(self):
        admin_auth.reset_to_defaults()
        for _ in range(admin_auth.MAX_FAILS):
            with self.assertRaises(admin_auth.AuthError):
                admin_auth.login("admin", "nope", client="10.1.1.1")
        lockout._MEM.clear()
        with self.assertRaises(admin_auth.AuthError) as cm:
            admin_auth.login("admin", admin_auth.default_password(), client="10.1.1.2")
        self.assertEqual(cm.exception.status_code, 429)
        admin_auth.reset_to_defaults()

    def test_success_clears_the_counter(self):
        lockout.fail("studio", "user:dave", 5, 60)
        self.assertTrue(store.get_store().list(lockout.COLLECTION))
        lockout.clear("studio", "user:dave")
        self.assertFalse(store.get_store().list(lockout.COLLECTION))

    def test_local_store_keeps_counters_in_memory(self):
        store.set_store(store.LocalStore(tempfile.mkdtemp(prefix="us-local-")))
        lockout.fail("studio", "user:erin", 5, 60)
        self.assertEqual(store.get_store().list(lockout.COLLECTION), [])
        self.assertTrue(lockout._MEM)


class SecurityAlertsTest(SharedStoreCase):
    def test_alerts_survive_a_restart_and_clear(self):
        security_alerts.clear()
        a = security_alerts.record("wiz-1", "PROMPT INJECTION", "Understudy - SOP", "localhost", raw_text="x" * 900,
                                   model_name="Understudy pattern rules (no AI call)")
        self.assertEqual(len(a["raw_text"]), 400)
        importlib.reload(security_alerts)                # a new instance / revision
        rows = security_alerts.alerts()
        self.assertEqual([r["alert_id"] for r in rows], [a["alert_id"]])
        security_alerts.record("wiz-2", "PERSONA HIJACK", "Understudy - URL", "example.com")
        self.assertEqual(security_alerts.alerts()[0]["run_id"], "wiz-2")         # newest first
        security_alerts.clear()
        self.assertEqual(security_alerts.alerts(), [])

    def test_feed_is_pruned(self):
        security_alerts.clear()
        with mock.patch.object(security_alerts, "MAX_ALERTS", 5):
            for i in range(30):
                security_alerts.record(f"wiz-{i}", "X", "c", "p")
            security_alerts._prune(store.get_store())
            self.assertEqual(len(security_alerts.alerts()), 5)
            self.assertEqual(security_alerts.alerts()[0]["run_id"], "wiz-29")
        security_alerts.clear()


class HealthAndCheckTest(unittest.TestCase):
    def test_health_reports_hosting_and_google_ai(self):
        import server
        c = TestClient(server.app)
        h = c.get("/api/health").json()
        self.assertEqual(h["hosting"]["platform"], "self_hosted")
        self.assertEqual(h["ai"]["provider"], "google")
        with mock.patch.dict(os.environ, {"K_SERVICE": "understudy", "K_REVISION": "understudy-00009-xyz",
                                          "GEMINI_API_KEY": "AIzaTESTKEY0000000000"}):
            h = c.get("/api/health").json()
        self.assertEqual(h["hosting"], {"platform": "cloud_run", "service": "understudy",
                                        "revision": "understudy-00009-xyz"})
        self.assertEqual(h["ai"]["mode"], "studio")
        self.assertTrue(h["ai"]["model"])
        self.assertNotIn("AIza", json.dumps(h))                                  # never the key

    def _run_check(self, health):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts"))
        import check_deployment as cd
        importlib.reload(cd)

        def fake_get(url, timeout=30.0):
            if url.endswith("/healthz") or url.endswith("/api/health"):
                return 200, json.dumps(health)
            return 200, "<html></html>"
        out = io.StringIO()
        with mock.patch.object(cd, "get", fake_get), mock.patch.object(sys, "argv", ["x", "https://u.a.run.app/"]), \
                redirect_stdout(out):
            code = cd.main()
        return code, out.getvalue()

    def test_check_deployment_passes_on_cloud_run_with_gemini(self):
        code, text = self._run_check({"status": "ok", "version": "9.7.0", "store": "firestore",
                                      "hosting": {"platform": "cloud_run", "service": "understudy", "revision": "r1"},
                                      "ai": {"provider": "google", "mode": "vertex", "platform": "Vertex AI",
                                             "model": "gemini-2.5-flash"}})
        self.assertEqual(code, 0, text)
        self.assertIn("meets the submission requirements", text)

    def test_check_deployment_fails_without_cloud_run_or_gemini(self):
        code, text = self._run_check({"status": "ok", "version": "9.7.0", "store": "local",
                                      "hosting": {"platform": "self_hosted"},
                                      "ai": {"provider": "google", "mode": None}})
        self.assertEqual(code, 1)
        self.assertIn("FAIL  Deployed on Google Cloud Run", text)
        self.assertIn("FAIL  Google AI model (Gemini) configured", text)
