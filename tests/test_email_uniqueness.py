"""8.1.1: one email = one account across instances. Each account's email is reserved with an atomic create of a claim
document (an O_EXCL file locally, Firestore's create() on Cloud Run), so two backend instances racing for the same
address can't both win - even when neither sees the other's account in its own pre-check.

"Instances" are simulated by threads that share the store but NOT the process lock (users._LOCK is replaced by a
no-op) and whose pre-check (find_by_email / _ensure_free) is blinded, i.e. the worst-case race."""
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from core.wizard import mailer, otp, store, users
from core.wizard.store import FirestoreStore, LocalStore
from tests.test_durable_store import FakeFirestore

PW = "Secret123x"


class NoLock:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def race(n, fn):
    """Runs fn(i) in n threads released at the same moment; returns [(i, result | exception)]."""
    gate = threading.Barrier(n)
    out = []

    def run(i):
        gate.wait()
        try:
            out.append((i, fn(i)))
        except Exception as e:                                         # noqa: BLE001
            out.append((i, e))
    ts = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    return out


class Base(unittest.TestCase):
    store_factory = None

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="understudy-uniq-")
        self.st = self.make_store()
        self.p_store = mock.patch.object(store, "get_store", return_value=self.st)
        self.p_store.start()
        mailer.set_transport(lambda m, meta: None)

    def make_store(self):
        return LocalStore(self.tmp)

    def tearDown(self):
        mailer.set_transport(None)
        self.p_store.stop()

    def blind_race(self):
        """No shared lock, and pre-checks that see nothing: only the atomic claim can stop a duplicate."""
        return [mock.patch.object(users, "_LOCK", NoLock()),
                mock.patch.object(users, "_ensure_free", lambda u, e: None),
                mock.patch.object(users, "_scan_email", lambda e: None)]


class TestLocalStoreCreate(Base):
    def test_create_is_exclusive_across_store_objects(self):
        stores = [LocalStore(self.tmp) for _ in range(12)]          # 12 "processes" on one store folder
        res = race(12, lambda i: stores[i].create("claims", "same-id", {"by": i}))
        self.assertEqual(sum(1 for _, r in res if r is True), 1)
        winner = next(i for i, r in res if r is True)
        self.assertEqual(self.st.get("claims", "same-id"), {"by": winner})


class TestEmailRace(Base):
    def test_two_instances_creating_the_same_email_only_one_wins(self):
        patches = self.blind_race()
        [p.start() for p in patches]
        try:
            res = race(8, lambda i: users.create_user(f"user{i}", PW, "admin", email="Same@Example.com"))
        finally:
            [p.stop() for p in patches]
        ok = [r for _, r in res if isinstance(r, dict)]
        bad = [r for _, r in res if isinstance(r, users.UserError)]
        self.assertEqual((len(ok), len(bad)), (1, 7), res)
        self.assertTrue(all(e.status_code == 409 for e in bad))
        live = [d for d in store.iter_docs(users.COLLECTION) if d.get("email") == "same@example.com"]
        self.assertEqual([d["username"] for d in live], [ok[0]["user"]["username"]])
        self.assertEqual(users.find_by_email("SAME@example.com")["username"], ok[0]["user"]["username"])

    def test_two_signups_verified_at_the_same_moment(self):
        # two pending signups for one address (different usernames), both verified concurrently on two instances
        chs = []
        for i in range(2):
            ch = otp.create("signup", "race@example.com", f"racer{i}", {"salt": "00" * 16, "hash": "ab" * 32})
            doc = ch["_doc"]
            doc["email"] = "race@example.com"
            store.get_store().put(otp.CHALLENGES, doc["id"], doc)
            chs.append(ch)
            for d in store.get_store().list(otp.RATES):
                store.get_store().delete(otp.RATES, d)
        # create() above removed the first challenge (one per email + purpose): put it back to model two instances
        store.get_store().put(otp.CHALLENGES, chs[0]["_doc"]["id"], chs[0]["_doc"])
        patches = self.blind_race()
        [p.start() for p in patches]
        try:
            res = race(2, lambda i: users.finish_signup(chs[i]["_doc"]["id"], chs[i]["code"]))
        finally:
            [p.stop() for p in patches]
        ok = [r for _, r in res if isinstance(r, dict)]
        self.assertEqual(len(ok), 1, res)
        self.assertTrue(isinstance([r for _, r in res if not isinstance(r, dict)][0], users.UserError))

    def test_same_username_created_twice_at_once(self):
        patches = self.blind_race() + [mock.patch.object(users, "_get", lambda u: None)]
        [p.start() for p in patches]
        try:
            res = race(6, lambda i: users.create_user("dup", PW, "admin", email=f"dup{i}@example.com"))
        finally:
            [p.stop() for p in patches]
        self.assertEqual(sum(1 for _, r in res if isinstance(r, dict)), 1, res)
        # the losers released their email claims, so those addresses are free again
        claims = [store.get_store().get(users.EMAILS, users._claim_id(f"dup{i}@example.com")) for i in range(6)]
        self.assertEqual(sum(1 for c in claims if c), 1)


class TestClaimLifecycle(Base):
    def test_delete_releases_the_address_and_stale_claims_are_reclaimed(self):
        users.create_user("ann", PW, "admin", email="ann@example.com")
        self.assertIsNotNone(self.st.get(users.EMAILS, users._claim_id("ann@example.com")))
        users.delete_user("ann", "Leaving the company", "admin")
        self.assertIsNone(self.st.get(users.EMAILS, users._claim_id("ann@example.com")))
        users.create_user("ann2", PW, "admin", email="ann@example.com")
        # a claim left by an instance that died half-way (no account) blocks only for the grace period
        self.st.put(users.EMAILS, users._claim_id("ghost@example.com"),
                    {"email": "ghost@example.com", "username": "ghost", "at": time.time()})
        with self.assertRaises(users.UserError) as e:
            users.create_user("bob", PW, "admin", email="ghost@example.com")
        self.assertEqual(e.exception.status_code, 409)
        self.st.put(users.EMAILS, users._claim_id("ghost@example.com"),
                    {"email": "ghost@example.com", "username": "ghost", "at": time.time() - users.CLAIM_GRACE_S - 1})
        users.create_user("bob", PW, "admin", email="ghost@example.com")
        self.assertEqual(self.st.get(users.EMAILS, users._claim_id("ghost@example.com"))["username"], "bob")

    def test_backfill_and_lookup_for_accounts_created_before_claims(self):
        self.st.put(users.COLLECTION, "old", {"username": "old", "email": "old@example.com", "active": True,
                                              "salt": "00" * 16, "hash": users._hash(PW, bytes(16)).hex(), "gen": 1})
        self.assertEqual(users.find_by_email("old@example.com")["username"], "old")      # fallback scan
        with self.assertRaises(users.UserError):
            users.create_user("new", PW, "admin", email="old@example.com")            # scan still guards
        self.assertIsNone(self.st.get(users.EMAILS, users._claim_id("old@example.com")))  # loser released it
        self.assertEqual(users.backfill_email_claims(), 1)
        self.assertEqual(users.backfill_email_claims(), 0)                                # idempotent
        self.assertEqual(self.st.get(users.EMAILS, users._claim_id("old@example.com"))["username"], "old")


class TestFirestore(TestEmailRace):
    """The same races on the Firestore store (fake client whose document create() fails when it exists)."""

    def make_store(self):
        return FirestoreStore(FakeFirestore())

    def test_firestore_create_semantics(self):
        self.assertTrue(self.st.create("claims", "x", {"a": 1}))
        self.assertFalse(self.st.create("claims", "x", {"a": 2}))
        self.assertEqual(self.st.get("claims", "x"), {"a": 1})


class TestCloudRunDefault(unittest.TestCase):
    def test_deploy_script_turns_signup_off_by_default(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        sh = open(os.path.join(root, "deploy_cloudrun.sh"), encoding="utf-8").read()
        self.assertIn('UNDERSTUDY_SIGNUP=${UNDERSTUDY_SIGNUP:-off}', sh)
        self.assertIn("SIGNUP_ENV", sh.split("gcloud run deploy")[1])
