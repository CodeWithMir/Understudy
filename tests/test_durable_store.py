"""Durable / shared state (core/wizard/store.py): local JSON store, and Cloud Firestore through a fake client.

Covers what the hackathon review called out as "in-memory only": sessions survive a backend restart (and can be
picked up by another Cloud Run instance), the admin policy, learned skills, telemetry and admin logins are shared
between instances in Firestore mode.
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests import wizard_helpers as H  # noqa: E402  (sets the test environment)
from core.wizard import activity_log, admin_auth, admin_policy, skills, store  # noqa: E402
from core.wizard import session as wz  # noqa: E402


# ---------------------------------------------------------------------------
# A small in-memory stand-in for google.cloud.firestore.Client
# ---------------------------------------------------------------------------
class _Snap:
    def __init__(self, doc_id, data):
        self.id, self._data = doc_id, data
        self.exists = data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class _Doc:
    def __init__(self, col, doc_id):
        self.col, self.id = col, doc_id

    def get(self):
        return _Snap(self.id, self.col.docs.get(self.id))

    def set(self, data):
        self.col.db.writes += 1
        self.col.docs[self.id] = dict(data)

    def create(self, data):                                     # DocumentReference.create: fails if it exists
        if self.id in self.col.docs:
            raise AlreadyExists(f"Document already exists: {self.id}")
        self.set(data)

    def delete(self):
        self.col.docs.pop(self.id, None)


class AlreadyExists(Exception):
    """Stands in for google.api_core.exceptions.AlreadyExists (HTTP 409)."""
    code = 409


class _Query:
    def __init__(self, col, filters=(), order=None, lim=None):
        self.col, self.filters, self.order, self.lim = col, list(filters), order, lim

    def where(self, field=None, op=None, value=None, filter=None):
        if filter is not None:                                   # FieldFilter(field_path, op_string, value)
            field, op, value = filter.field_path, filter.op_string, filter.value
        return _Query(self.col, self.filters + [(field, op, value)], self.order, self.lim)

    def order_by(self, field):
        return _Query(self.col, self.filters, field, self.lim)

    def limit(self, n):
        return _Query(self.col, self.filters, self.order, n)

    def stream(self):
        rows = [(k, v) for k, v in self.col.docs.items()]
        for field, op, value in self.filters:
            assert op == ">"
            rows = [(k, v) for k, v in rows if v.get(field, 0) > value]
        if self.order:
            rows.sort(key=lambda kv: kv[1].get(self.order))
        if self.lim:
            rows = rows[: self.lim]
        return [_Snap(k, v) for k, v in rows]


class _Col(_Query):
    def __init__(self, db, name):
        super().__init__(self)
        self.db, self.name, self.docs = db, name, {}

    def document(self, doc_id):
        return _Doc(self, doc_id)

    def list_documents(self):
        return [_Doc(self, k) for k in list(self.docs)]


class FakeFirestore:
    def __init__(self):
        self.cols, self.writes = {}, 0

    def collection(self, name):
        return self.cols.setdefault(name, _Col(self, name))


class FirestoreBase(unittest.TestCase):
    def setUp(self):
        self.db = FakeFirestore()
        self.env = mock.patch.dict(os.environ, {"UNDERSTUDY_STORE": "firestore", "GOOGLE_CLOUD_PROJECT": "demo-proj"})
        self.env.start()
        store.set_store(store.FirestoreStore(client=self.db))
        admin_policy._CACHE.update(path=None, data=None, t=0.0)
        admin_auth.reset_to_defaults()

    def tearDown(self):
        activity_log.flush()
        self.env.stop()
        store.set_store(None)
        admin_policy._CACHE.update(path=None, data=None, t=0.0)
        admin_auth.reset_to_defaults()


class TestLocalStore(unittest.TestCase):
    def test_roundtrip_and_ids(self):
        st = store.LocalStore(tempfile.mkdtemp())
        st.put("sessions", "wiz-abc", {"x": 1})
        self.assertEqual(st.get("sessions", "wiz-abc"), {"x": 1})
        self.assertEqual(st.list("sessions"), ["wiz-abc"])
        st.delete("sessions", "wiz-abc")
        self.assertIsNone(st.get("sessions", "wiz-abc"))
        for bad in ("../etc", "a/b", "", ".."):
            with self.assertRaises(ValueError):
                st.put("sessions", bad, {})

    def test_default_is_local(self):
        with mock.patch.dict(os.environ, {"UNDERSTUDY_STORE": "local"}):
            store.set_store(None)
            self.assertEqual(store.describe()["kind"], "local")


class TestSessionRestore(unittest.TestCase):
    """A backend restart (or another instance) picks the session up from the store."""

    def setUp(self):
        H.install_fake(None)
        store.set_store(store.LocalStore(tempfile.mkdtemp()))

    def tearDown(self):
        store.set_store(None)

    def _make(self):
        s = wz.create_session("http://localhost:8000/portal1", "Enter the invoice and save it.", H.fake_mp4(), "rec.mp4",
                              background=False)
        return s

    def test_restart_restores_session_without_secrets(self):
        s = self._make()
        sid = s["session_id"]
        s["parameters"] = [{"name": "password", "label": "Password", "type": "secret", "value": "hunter2"},
                           {"name": "vendor", "label": "Vendor", "type": "text", "value": "ACME"}]
        s["job"] = {"name": "execute", "status": "running"}
        wz._persist(s)
        raw = open(os.path.join(store.get_store().root, "sessions", sid + ".json")).read()
        self.assertNotIn("hunter2", raw)                           # secrets never reach the store
        wz.SESSIONS.clear()                                        # "restart"
        r = wz.get(sid)
        self.assertEqual(r["analysis"], s["analysis"])
        self.assertEqual(r["pillars"], s["pillars"])
        self.assertEqual(r["job"]["status"], "error")              # the interrupted job is reported, not stuck
        self.assertTrue(r["job"]["interrupted"])
        self.assertTrue(any("re-enter" in n and "Password" in n for n in r["notes"]))
        self.assertEqual(r["parameters"][1]["value"], "ACME")
        self.assertIn(sid, wz.PIPELINE_RUN_REGISTRY)               # download gate re-synced

    def test_unknown_session_still_404(self):
        with self.assertRaises(wz.WizardError) as cm:
            wz.get("wiz-doesnotexist")
        self.assertEqual(cm.exception.status_code, 404)
        with self.assertRaises(wz.WizardError):
            wz.get("../../etc/passwd")


class TestFirestoreSharing(FirestoreBase):
    def test_session_in_firestore(self):
        H.install_fake(None)
        s = wz.create_session("http://localhost:8000/portal1", "Enter the invoice.", H.fake_mp4(), "rec.mp4",
                              background=False)
        sid = s["session_id"]
        self.assertIn(sid, self.db.collection("understudy_sessions").docs)
        wz.SESSIONS.clear()
        self.assertEqual(wz.get(sid)["session_id"], sid)

    def test_admin_policy_shared_between_instances(self):
        admin_policy.add_url_exception("http://erp.intranet.local/app", "ERP")
        self.assertIn("understudy_admin_policy", self.db.cols)
        admin_policy._CACHE.update(path=None, data=None, t=0.0)     # "another instance" with an empty cache
        self.assertTrue(admin_policy.is_host_excepted("erp.intranet.local"))
        # a change made elsewhere shows up after the refresh interval
        doc = store.get_store().get("admin_policy", "main")
        doc["url_exceptions"] = [e for e in doc["url_exceptions"] if e["host"] != "erp.intranet.local"]
        store.get_store().put("admin_policy", "main", doc)
        admin_policy._CACHE["t"] = 0.0
        self.assertFalse(admin_policy.is_host_excepted("erp.intranet.local"))

    def test_skills_shared(self):
        skills.record_error("selenium_python", "Selenium", "http://localhost:8000/portal1", "SELECTOR",
                            "NoSuchElementException: #vendor", "wiz-test")
        ids = self.db.collection("understudy_skills").docs
        self.assertTrue(ids)
        with mock.patch.dict(os.environ, {"UNDERSTUDY_SKILLS_DIR": tempfile.mkdtemp()}):   # other machine, no files
            listed = skills.list_skills()
            self.assertTrue(listed)
            self.assertIn("NoSuchElement", skills.read_skill(listed[0]["id"])["markdown"])

    def test_telemetry_merged_from_firestore(self):
        activity_log.record("STEP", "event from instance A")
        activity_log.flush()
        self.assertTrue(self.db.collection("understudy_telemetry").docs)
        with activity_log._LOCK:
            activity_log._EVENTS.clear()                           # instance B has nothing in memory
        page = activity_log.query(after=0)
        self.assertTrue(any(e["title"] == "event from instance A" for e in page["events"]))
        self.assertGreater(page["last_seq"], 10 ** 12)             # cross-instance seq (microseconds)

    def test_admin_login_valid_on_every_instance(self):
        token = admin_auth.login("admin", "Understudy@2026")["token"]
        self.assertTrue(admin_auth.session_for(token))
        # instance B: fresh process state for the same Cloud Run revision
        admin_auth.reset_to_defaults()
        self.assertTrue(admin_auth.session_for(token))
        admin_auth.change_credentials(token, "Understudy@2026", "N3wSecret99", "opsadmin")
        admin_auth.reset_to_defaults()                             # instance C sees the new password ...
        self.assertTrue(admin_auth.login("opsadmin", "N3wSecret99")["token"])
        admin_auth.logout(token)
        admin_auth.reset_to_defaults()
        self.assertIsNone(admin_auth.session_for(token))           # ... and the logout
        # a NEW deployment (revision) starts again from the documented default
        with mock.patch.dict(os.environ, {"K_REVISION": "understudy-00002-new"}):
            admin_auth.reset_to_defaults()
            self.assertTrue(admin_auth.login("admin", "Understudy@2026")["token"])

    def test_forged_or_tampered_token_rejected(self):
        token = admin_auth.login("admin", "Understudy@2026")["token"]
        v, body, sig = token.split(".")
        self.assertIsNone(admin_auth.session_for(f"{v}.{body}x.{sig}"))
        self.assertIsNone(admin_auth.session_for("v1.e30." + sig))
        self.assertIsNone(admin_auth.session_for("random-garbage"))


if __name__ == "__main__":
    unittest.main()
