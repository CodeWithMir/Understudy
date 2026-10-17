"""core/wizard/gemini.py: google-genai on AI Studio or Vertex AI, and the upload-once recording cache.

No network: google.genai.Client and google.cloud.storage.Client are replaced with recording fakes.
"""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests import wizard_helpers as H  # noqa: E402,F401  (test environment)
from core.wizard import gemini  # noqa: E402

CLEAN = {k: "" for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI", "UNDERSTUDY_VERTEX_PROJECT",
                         "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION", "UNDERSTUDY_VERTEX_LOCATION",
                         "UNDERSTUDY_GEMINI_SDK", "UNDERSTUDY_GCS_BUCKET")}


class FakeGenAI:
    def __init__(self):
        self.clients, self.uploads, self.gets, self.calls = [], [], [], []
        self.expired = set()
        me = self

        class _Files:
            def upload(self, file):
                me.uploads.append(file)
                return SimpleNamespace(name=f"files/{len(me.uploads)}", state=SimpleNamespace(name="ACTIVE"))

            def get(self, name):
                me.gets.append(name)
                if name in me.expired:
                    raise RuntimeError("404 file not found")
                return SimpleNamespace(name=name, state=SimpleNamespace(name="ACTIVE"))

        class _Models:
            def generate_content(self, model, contents, config):
                me.calls.append({"model": model, "contents": contents, "config": config})
                return SimpleNamespace(text='{"ok": true}')

        class Client:
            def __init__(self, **kw):
                me.clients.append(kw)
                self.files, self.models = _Files(), _Models()

        self.Client = Client


class Base(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, CLEAN)
        self.env.start()
        gemini._MEDIA_CACHE.clear()
        gemini.MEDIA_STATS.update(uploads=0, reused=0)
        self.fake = FakeGenAI()
        self.p = mock.patch("google.genai.Client", self.fake.Client)
        self.p.start()
        self.video = H.fake_mp4(payload=os.urandom(32))

    def tearDown(self):
        self.p.stop()
        self.env.stop()


class TestModeSelection(Base):
    def test_no_credentials(self):
        self.assertIsNone(gemini.mode())
        self.assertIn("No Gemini API key", gemini.unavailable_reason())

    def test_studio_with_key(self):
        os.environ["GEMINI_API_KEY"] = "AIzaTESTKEY0000000000000000000000000"
        self.assertEqual(gemini.mode(), "studio")
        self.assertEqual(gemini.describe()["sdk"], "google-genai")

    def test_vertex_needs_project(self):
        os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "true"
        self.assertIsNone(gemini.mode())
        self.assertIn("GOOGLE_CLOUD_PROJECT", gemini.unavailable_reason())
        os.environ["GOOGLE_CLOUD_PROJECT"] = "my-proj"
        self.assertEqual(gemini.mode(), "vertex")                   # no API key needed (ADC)
        d = gemini.describe()
        self.assertEqual((d["project"], d["location"], d["platform"]), ("my-proj", "us-central1", "Vertex AI (Google Cloud)"))

    def test_vertex_wins_over_key(self):
        os.environ.update(GEMINI_API_KEY="AIzaTESTKEY0000000000000000000000000", UNDERSTUDY_VERTEX_PROJECT="p2",
                          UNDERSTUDY_VERTEX_LOCATION="europe-west4")
        self.assertEqual(gemini.mode(), "vertex")
        gemini.generate("gemini-2.5-flash", "sys", "user", None, 0.1, 30)
        self.assertEqual(self.fake.clients[-1], {"vertexai": True, "project": "p2", "location": "europe-west4"})

    def test_no_deprecated_sdk_mode(self):
        """9.4.1: google-genai is the only SDK; the old UNDERSTUDY_GEMINI_SDK=legacy switch has no effect."""
        os.environ.update(GEMINI_API_KEY="AIzaTESTKEY0000000000000000000000000", UNDERSTUDY_GEMINI_SDK="legacy")
        self.assertEqual(gemini.mode(), "studio")


class TestStudio(Base):
    def setUp(self):
        super().setUp()
        os.environ["GEMINI_API_KEY"] = "AIzaTESTKEY0000000000000000000000000"

    def test_json_config_and_system_prompt(self):
        out = gemini.generate("gemini-2.5-flash", "SYSTEM", "USER", None, 0.2, 45)
        self.assertEqual(out, '{"ok": true}')
        c = self.fake.calls[-1]
        self.assertEqual(c["model"], "gemini-2.5-flash")
        self.assertEqual(c["contents"], ["USER"])
        self.assertEqual(c["config"].system_instruction, "SYSTEM")
        self.assertEqual(c["config"].response_mime_type, "application/json")
        self.assertEqual(c["config"].http_options.timeout, 45000)
        self.assertEqual(self.fake.clients[-1], {"api_key": "AIzaTESTKEY0000000000000000000000000"})

    def test_recording_uploaded_once_and_reused(self):
        for _ in range(3):                                          # WATCH, a correction, a self-heal
            gemini.generate("m", "s", "u", self.video, 0.1, 30)
        self.assertEqual(len(self.fake.uploads), 1)
        self.assertEqual(gemini.MEDIA_STATS, {"uploads": 1, "reused": 2})

    def test_expired_file_is_uploaded_again(self):
        gemini.generate("m", "s", "u", self.video, 0.1, 30)
        self.fake.expired.add("files/1")
        gemini.generate("m", "s", "u", self.video, 0.1, 30)
        self.assertEqual(len(self.fake.uploads), 2)

    def test_different_key_does_not_share_uploads(self):
        gemini.generate("m", "s", "u", self.video, 0.1, 30)
        os.environ["GEMINI_API_KEY"] = "AIzaOTHERKEY000000000000000000000000"
        gemini.generate("m", "s", "u", self.video, 0.1, 30)
        self.assertEqual(len(self.fake.uploads), 2)


class TestVertexMedia(Base):
    def setUp(self):
        super().setUp()
        os.environ.update(GOOGLE_GENAI_USE_VERTEXAI="true", GOOGLE_CLOUD_PROJECT="proj")

    def test_small_recording_inline_once(self):
        gemini.generate("m", "s", "u", self.video, 0.1, 30)
        gemini.generate("m", "s", "u", self.video, 0.1, 30)
        part = self.fake.calls[-1]["contents"][0]
        self.assertEqual(part.inline_data.mime_type, "video/mp4")
        self.assertEqual(gemini.MEDIA_STATS, {"uploads": 1, "reused": 1})

    def test_large_recording_needs_bucket(self):
        with mock.patch("os.path.getsize", return_value=gemini.INLINE_LIMIT + 1):
            with self.assertRaises(RuntimeError) as cm:
                gemini.generate("m", "s", "u", self.video, 0.1, 30)
        self.assertIn("UNDERSTUDY_GCS_BUCKET", str(cm.exception))

    def test_cloud_storage_path(self):
        os.environ["UNDERSTUDY_GCS_BUCKET"] = "rec-bucket"
        uploaded = []

        class Blob:
            def __init__(self, name):
                self.name = name

            def exists(self):
                return False

            def upload_from_filename(self, path, content_type=None):
                uploaded.append((self.name, content_type))

        class Bucket:
            def blob(self, name):
                return Blob(name)

        class StorageClient:
            def bucket(self, name):
                assert name == "rec-bucket"
                return Bucket()

        with mock.patch("google.cloud.storage.Client", StorageClient):
            gemini.generate("m", "s", "u", self.video, 0.1, 30)
            gemini.generate("m", "s", "u", self.video, 0.1, 30)
        self.assertEqual(len(uploaded), 1)
        part = self.fake.calls[-1]["contents"][0]
        self.assertTrue(part.file_data.file_uri.startswith("gs://rec-bucket/understudy/recordings/"))


if __name__ == "__main__":
    unittest.main()
