"""v9.9 - recordings over 32 MiB on Cloud Run go straight to Cloud Storage.

Cloud Run refuses request bodies over 32 MiB before they reach Understudy (the UI showed a bare "Request failed (413)").
With UNDERSTUDY_GCS_BUCKET the browser gets a resumable upload session bound to its origin, PUTs the file to Cloud
Storage, and Step 1 names the upload; the backend downloads it, deletes the staging object and screens it as usual.
"""
import os
import time
import unittest
from unittest import mock

from fastapi import HTTPException
from fastapi.testclient import TestClient

import tests.wizard_helpers  # noqa: F401  (isolated test environment)
from core.wizard import api, store, uploads

MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 64


class FakeBlob:
    def __init__(self, bucket, name):
        self.bucket, self.name, self.size = bucket, name, None

    def create_resumable_upload_session(self, content_type=None, size=None, origin=None):
        self.bucket.sessions.append({"object": self.name, "content_type": content_type, "size": size, "origin": origin})
        return f"https://storage.googleapis.com/upload/fake?upload_id={len(self.bucket.sessions)}"

    def reload(self):
        if self.name not in self.bucket.objects:
            raise RuntimeError("404 not found")
        self.size = len(self.bucket.objects[self.name])

    def download_to_filename(self, path):
        with open(path, "wb") as fh:
            fh.write(self.bucket.objects[self.name])

    def delete(self):
        self.bucket.objects.pop(self.name, None)


class FakeBucket:
    def __init__(self):
        self.objects, self.sessions = {}, []

    def blob(self, name):
        return FakeBlob(self, name)

    def browser_puts(self, data=MP4):            # what the browser's PUT to the session URL does
        self.objects[self.sessions[-1]["object"]] = data


class Base(unittest.TestCase):
    def setUp(self):
        self.bucket = FakeBucket()
        self.patches = [mock.patch.object(uploads, "_bucket", return_value=self.bucket),
                        mock.patch.object(uploads, "direct_available", return_value=True)]
        [p.start() for p in self.patches]
        for d in store.get_store().list(uploads.COLLECTION):
            store.get_store().delete(uploads.COLLECTION, d)

    def tearDown(self):
        [p.stop() for p in self.patches]


class TestDirectUpload(Base):
    def test_round_trip(self):
        r = uploads.begin("Alice", "WhatsApp Video 2026-10-06.mp4", 80 * 1024 * 1024, "https://understudy-x.a.run.app")
        self.assertEqual(r["mode"], "gcs")
        self.assertTrue(r["upload_url"].startswith("https://storage.googleapis.com/"))
        sess = self.bucket.sessions[-1]
        self.assertEqual(sess["origin"], "https://understudy-x.a.run.app")       # only this page may upload
        self.assertEqual(sess["content_type"], "video/mp4")
        self.assertTrue(sess["object"].startswith(uploads.PREFIX))
        self.bucket.browser_puts()
        tmp, name = uploads.take(r["upload_id"], "alice")
        try:
            with open(tmp, "rb") as fh:
                self.assertEqual(fh.read(), MP4)
            self.assertTrue(name.endswith(".mp4"))
        finally:
            os.remove(tmp)
        self.assertEqual(self.bucket.objects, {})                                 # staging object deleted
        with self.assertRaises(uploads.UploadError):                             # one use only
            uploads.take(r["upload_id"], "alice")

    def test_another_user_cannot_take_it(self):
        r = uploads.begin("alice", "a.mp4", 1000, "https://x.run.app")
        self.bucket.browser_puts()
        with self.assertRaises(uploads.UploadError) as cm:
            uploads.take(r["upload_id"], "bob")
        self.assertEqual(cm.exception.status_code, 404)

    def test_not_finished_yet(self):
        r = uploads.begin("alice", "a.mp4", 1000, "https://x.run.app")
        with self.assertRaises(uploads.UploadError) as cm:
            uploads.take(r["upload_id"], "alice")
        self.assertEqual(cm.exception.status_code, 409)

    def test_expired(self):
        r = uploads.begin("alice", "a.mp4", 1000, "https://x.run.app")
        self.bucket.browser_puts()
        with mock.patch.object(uploads.time, "time", return_value=time.time() + uploads.TTL_S + 5):
            with self.assertRaises(uploads.UploadError) as cm:
                uploads.take(r["upload_id"], "alice")
        self.assertEqual(cm.exception.status_code, 410)

    def test_checks_before_opening_a_session(self):
        for name, size, origin, code in (("a.exe", 1000, "https://x.run.app", 415),
                                         ("a.mp4", uploads.MAX_MEDIA_BYTES + 1, "https://x.run.app", 413),
                                         ("a.mp4", 0, "https://x.run.app", 400),
                                         ("a.mp4", 1000, "javascript:alert(1)", 400)):
            with self.assertRaises(uploads.UploadError) as cm:
                uploads.begin("alice", name, size, origin)
            self.assertEqual(cm.exception.status_code, code, name)
        self.assertEqual(self.bucket.sessions, [])

    def test_bigger_than_announced_is_refused(self):
        r = uploads.begin("alice", "a.mp4", 1000, "https://x.run.app")
        self.bucket.browser_puts(MP4)
        with mock.patch.object(uploads, "MAX_MEDIA_BYTES", 10):
            with self.assertRaises(uploads.UploadError) as cm:
                uploads.take(r["upload_id"], "alice")
        self.assertEqual(cm.exception.status_code, 413)

    def test_bad_ids(self):
        for bad in ("", "../../etc", "x" * 32):
            with self.assertRaises(uploads.UploadError):
                uploads.take(bad, "alice")

    def test_step1_reads_the_upload(self):
        r = uploads.begin(None, "rec.mp4", 1000, "https://x.run.app")
        self.bucket.browser_puts()
        tmp, name = api._media(None, r["upload_id"], None)
        self.assertTrue(os.path.isfile(tmp))
        os.remove(tmp)
        with self.assertRaises(HTTPException) as cm:
            api._media(None, r["upload_id"], None)
        self.assertEqual(cm.exception.status_code, 404)


class TestPlan(unittest.TestCase):
    def test_without_a_bucket_upload_goes_through_the_app(self):
        with mock.patch.object(uploads, "direct_available", return_value=False), \
                mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("K_SERVICE", None)
            self.assertEqual(uploads.plan("a.mp4", 80 * 1024 * 1024), {"mode": "direct"})

    def test_cloud_run_without_bucket_explains_the_32_mb_limit(self):
        with mock.patch.object(uploads, "direct_available", return_value=False), \
                mock.patch.dict(os.environ, {"K_SERVICE": "understudy"}):
            p = uploads.plan("big.mp4", 80 * 1024 * 1024)
            self.assertIn("32 MB", p["message"])
            self.assertIn("UNDERSTUDY_GCS_BUCKET", p["message"])
            self.assertNotIn("message", uploads.plan("small.mp4", 5 * 1024 * 1024))

    def test_api_endpoint(self):
        import server
        c = TestClient(server.app)
        with mock.patch.object(uploads, "direct_available", return_value=False):
            r = c.post("/api/wizard/uploads", json={"filename": "a.mp4", "size": 1000})
        if r.status_code == 401:
            self.skipTest("user login is on in this environment")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["mode"], "direct")
        bucket = FakeBucket()
        with mock.patch.object(uploads, "direct_available", return_value=True), \
                mock.patch.object(uploads, "_bucket", return_value=bucket):
            r = c.post("/api/wizard/uploads", json={"filename": "a.mp4", "size": 1000},
                       headers={"Origin": "https://understudy-x.a.run.app"})
            self.assertEqual(r.json()["mode"], "gcs")
            self.assertEqual(bucket.sessions[-1]["origin"], "https://understudy-x.a.run.app")
            self.assertEqual(c.post("/api/wizard/uploads", json={"filename": "a.exe", "size": 10},
                                    headers={"Origin": "https://x.run.app"}).status_code, 415)


if __name__ == "__main__":
    unittest.main()
