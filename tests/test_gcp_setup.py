""".env loading, the Google Cloud set-up checker (CLI + Mission Control) and the Vertex AI model fallback (6.0.1).

No network and no Google account: google.auth, the Firestore client, Cloud Storage and Gemini are replaced by fakes.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests import wizard_helpers as H  # noqa: E402
from tests.test_durable_store import FakeFirestore  # noqa: E402
from core.wizard import env_file, gcp_check, gemini, store  # noqa: E402

ROOT = H.ROOT
GCP_VARS = ("GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_CLOUD_PROJECT", "UNDERSTUDY_VERTEX_PROJECT", "UNDERSTUDY_STORE",
            "UNDERSTUDY_GCS_BUCKET", "GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS",
            "UNDERSTUDY_VERTEX_MODEL")


class TestEnvFile(unittest.TestCase):
    def setUp(self):
        self.p = os.path.join(tempfile.mkdtemp(), ".env")
        with open(self.p, "w") as fh:
            fh.write("# comment\nGOOGLE_CLOUD_PROJECT=from-file\nexport UNDERSTUDY_STORE='firestore'\n"
                     "UNDERSTUDY_GCS_BUCKET=bkt  # inline comment\nGEMINI_API_KEY=\nGOOGLE_CLOUD_LOCATION=\"asia-south1\"\n")

    def test_parse(self):
        d = env_file.parse(self.p)
        self.assertEqual(d["GOOGLE_CLOUD_PROJECT"], "from-file")
        self.assertEqual(d["UNDERSTUDY_STORE"], "firestore")
        self.assertEqual(d["UNDERSTUDY_GCS_BUCKET"], "bkt")
        self.assertEqual(d["GOOGLE_CLOUD_LOCATION"], "asia-south1")

    def test_load_never_overrides_real_env_and_skips_empty(self):
        with mock.patch.dict(os.environ, {"GOOGLE_CLOUD_PROJECT": "from-shell", "UNDERSTUDY_SKIP_DOTENV": ""}, clear=False):
            for k in ("UNDERSTUDY_STORE", "UNDERSTUDY_GCS_BUCKET", "GEMINI_API_KEY", "GOOGLE_CLOUD_LOCATION"):
                os.environ.pop(k, None)
            applied = env_file.load(self.p)
            self.assertEqual(os.environ["GOOGLE_CLOUD_PROJECT"], "from-shell")      # Docker / shell win
            self.assertEqual(os.environ["UNDERSTUDY_STORE"], "firestore")
            self.assertNotIn("GEMINI_API_KEY", applied)                            # empty values ignored
            for k in applied:
                os.environ.pop(k, None)

    def test_skip_flag(self):
        with mock.patch.dict(os.environ, {"UNDERSTUDY_SKIP_DOTENV": "1"}):
            self.assertEqual(env_file.load(self.p), [])

    def test_update_keeps_comments_and_order(self):
        env_file.update({"GOOGLE_CLOUD_PROJECT": "new", "GOOGLE_GENAI_USE_VERTEXAI": "true", "UNDERSTUDY_GCS_BUCKET": None},
                        self.p)
        text = open(self.p).read()
        self.assertTrue(text.startswith("# comment\nGOOGLE_CLOUD_PROJECT=new\n"))
        self.assertNotIn("UNDERSTUDY_GCS_BUCKET", text)
        self.assertTrue(text.rstrip().endswith("GOOGLE_GENAI_USE_VERTEXAI=true"))


class CheckBase(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {k: "" for k in GCP_VARS})
        self.env.start()
        for k in GCP_VARS:
            os.environ.pop(k, None)
        store.set_store(None)

    def tearDown(self):
        self.env.stop()
        store.set_store(None)

    def ids(self, rep):
        return {c["id"]: c for c in rep["checks"]}


class TestGcpCheck(CheckBase):
    def test_nothing_configured_is_a_warning_not_a_failure(self):
        rep = gcp_check.check(network=False)
        self.assertEqual(rep["status"], "WARN")
        self.assertIn("aistudio.google.com/apikey", self.ids(rep)["ai_config"]["fix"])

    def test_vertex_without_project(self):
        os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "true"
        rep = gcp_check.check(network=False)
        self.assertEqual(self.ids(rep)["project"]["status"], "FAIL")
        self.assertIn("GOOGLE_CLOUD_PROJECT", self.ids(rep)["project"]["fix"])

    def test_no_credentials_explains_login_and_skips_live_calls(self):
        os.environ.update(GOOGLE_GENAI_USE_VERTEXAI="true", GOOGLE_CLOUD_PROJECT="p1", UNDERSTUDY_STORE="firestore")
        import google.auth.exceptions as gae
        with mock.patch("google.auth.default", side_effect=gae.DefaultCredentialsError("not found")):
            rep = gcp_check.check(network=True)
        c = self.ids(rep)
        self.assertEqual(c["adc"]["status"], "FAIL")
        self.assertIn("gcloud auth application-default login", c["adc"]["fix"])
        self.assertEqual((c["ai_call"]["status"], c["firestore"]["status"]), ("SKIP", "SKIP"))

    def test_full_check_passes_with_working_services(self):
        os.environ.update(GOOGLE_GENAI_USE_VERTEXAI="true", GOOGLE_CLOUD_PROJECT="p1", UNDERSTUDY_STORE="firestore",
                          UNDERSTUDY_GCS_BUCKET="rec")
        db = FakeFirestore()
        blobs = {}

        class Blob:
            def __init__(self, n): self.n = n
            def upload_from_string(self, s, content_type=None): blobs[self.n] = s.encode()
            def download_as_bytes(self): return blobs[self.n]
            def delete(self): blobs.pop(self.n)

        storage_client = SimpleNamespace(bucket=lambda b: SimpleNamespace(blob=Blob))
        creds = SimpleNamespace(service_account_email="understudy-run@p1.iam.gserviceaccount.com")
        with mock.patch("google.auth.default", return_value=(creds, "p1")), \
                mock.patch.object(store, "FirestoreStore", lambda _orig=store.FirestoreStore: _orig(client=db)), \
                mock.patch("google.cloud.storage.Client", lambda project=None: storage_client), \
                mock.patch.object(gemini, "generate", return_value='{"ok": true}'):
            rep = gcp_check.check(network=True)
        c = self.ids(rep)
        self.assertEqual(rep["status"], "PASS", rep)
        self.assertIn("understudy-run@p1", c["adc"]["detail"])
        self.assertEqual(c["firestore"]["status"], "PASS")
        self.assertEqual(c["gcs"]["status"], "PASS")
        self.assertFalse(db.collection("understudy_healthcheck").docs)        # test record cleaned up
        self.assertFalse(blobs)

    def test_error_explanations(self):
        os.environ["GOOGLE_CLOUD_PROJECT"] = "p1"
        cases = [
            (Exception("403 SERVICE_DISABLED: Vertex AI API has not been used in project p1"), "vertex",
             "gcloud services enable aiplatform.googleapis.com --project p1"),
            (Exception("404 The database (default) does not exist for project p1"), "firestore",
             "gcloud firestore databases create"),
            (Exception("403 PERMISSION_DENIED: Missing or insufficient permissions."), "firestore", "roles/datastore.user"),
            (Exception("404 The specified bucket does not exist."), "storage", "gcloud storage buckets create"),
            (Exception("400 API key not valid. API_KEY_INVALID"), "vertex", "aistudio.google.com/apikey"),
        ]
        for e, svc, expected in cases:
            self.assertIn(expected, gcp_check._explain(e, svc), e)

    def test_cli_quick_and_json(self):
        env = dict(os.environ, UNDERSTUDY_SKIP_DOTENV="1")
        out = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "gcp_check.py"), "--quick", "--json"],
                             capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn('"checks"', out.stdout)


class TestFirestoreUnavailable(CheckBase):
    """Found while testing 6.0.1: Firestore switched on without credentials made Mission Control return 500s."""

    def test_falls_back_to_local_files_and_says_why(self):
        os.environ.update(UNDERSTUDY_STORE="firestore", GOOGLE_CLOUD_PROJECT="p1")
        import google.auth.exceptions as gae
        with mock.patch.object(store, "FirestoreStore", side_effect=gae.DefaultCredentialsError("not found")):
            store.set_store(None)
            store._STORE["obj"] = None
            self.assertEqual(store.get_store().kind, "local")
            d = store.describe()
            self.assertEqual(d["requested"], "firestore")
            self.assertIn("DefaultCredentialsError", d["degraded"])
            from core.wizard import admin_policy
            admin_policy._CACHE.update(path=None, data=None, t=0.0)
            self.assertTrue(admin_policy.whitelist())                       # Mission Control keeps working
            rep = gcp_check.check(network=False)
            sc = self.ids(rep)["store_config"]
            self.assertEqual(sc["status"], "FAIL")
            self.assertIn("gcloud auth application-default login", sc["fix"])
            r = H.admin_client().get("/api/config/api-key")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertIn("DefaultCredentialsError", r.json()["platform"]["store_degraded"])
        admin_policy._CACHE.update(path=None, data=None, t=0.0)

    def test_retries_firestore_after_the_interval(self):
        os.environ.update(UNDERSTUDY_STORE="firestore", GOOGLE_CLOUD_PROJECT="p1")
        db = FakeFirestore()
        attempts = []

        def flaky(_orig=store.FirestoreStore):
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("no credentials yet")
            return _orig(client=db)
        with mock.patch.object(store, "FirestoreStore", side_effect=flaky):
            store._STORE["obj"] = None
            self.assertEqual(store.get_store().kind, "local")
            store._STORE["retry_at"] = 0                                    # a minute later ...
            self.assertEqual(store.get_store().kind, "firestore")           # ... the fixed credentials are used
            self.assertIsNone(store.degraded())


class TestAdminEndpoint(CheckBase):
    def test_requires_login_and_returns_checks(self):
        from fastapi.testclient import TestClient
        import server
        self.assertEqual(TestClient(server.app).post("/api/admin/gcp/check?network=false").status_code, 401)
        r = H.admin_client().post("/api/admin/gcp/check?network=false")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn(r.json()["status"], ("PASS", "WARN", "FAIL"))
        self.assertTrue(r.json()["checks"])


class TestVertexModelFallback(CheckBase):
    def test_alias_not_offered_on_vertex_falls_back(self):
        os.environ.update(GOOGLE_GENAI_USE_VERTEXAI="true", GOOGLE_CLOUD_PROJECT="p1")
        calls = []

        class Models:
            def generate_content(self, model, contents, config):
                calls.append(model)
                if model == "gemini-flash-latest":
                    raise RuntimeError("404 NOT_FOUND: Publisher Model gemini-flash-latest was not found")
                return SimpleNamespace(text='{"ok": true}')

        with mock.patch("google.genai.Client", lambda **kw: SimpleNamespace(models=Models(), files=None)):
            out = gemini.generate("gemini-flash-latest", "s", "u", None, 0.0, 10)
        self.assertEqual(out, '{"ok": true}')
        self.assertEqual(calls, ["gemini-flash-latest", "gemini-2.5-flash"])
        self.assertEqual(gemini.LAST_FALLBACK["used"], "gemini-2.5-flash")

    def test_explicit_vertex_model_and_no_fallback_on_studio(self):
        os.environ.update(GOOGLE_GENAI_USE_VERTEXAI="true", GOOGLE_CLOUD_PROJECT="p1", UNDERSTUDY_VERTEX_MODEL="gemini-2.5-pro")
        calls = []
        models = SimpleNamespace(generate_content=lambda model, contents, config: calls.append(model) or SimpleNamespace(text="{}"))
        with mock.patch("google.genai.Client", lambda **kw: SimpleNamespace(models=models, files=None)):
            gemini.generate("gemini-flash-latest", "s", "u", None, 0.0, 10)
        self.assertEqual(calls, ["gemini-2.5-pro"])


class TestStartScripts(unittest.TestCase):
    """The start scripts install the Google Cloud clients and show the Google Cloud status."""

    def read(self, name):
        with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
            return fh.read()

    def test_setup_installs_gcp_clients(self):
        for f in ("setup.sh", "setup.bat", "Dockerfile", "Dockerfile.backend"):
            self.assertIn("requirements-gcp.txt", self.read(f), f)

    def test_launchers_run_the_quick_check(self):
        for f in ("run_demo.sh", "run_demo.bat", "run_demo.ps1"):
            self.assertIn("gcp_check.py", self.read(f), f)
            self.assertIn("gcp_setup", self.read(f), f)

    def test_docker_mounts_credentials_only_when_needed(self):
        self.assertIn("docker-compose.gcp.yml", self.read("deploy_docker.sh"))
        self.assertIn("docker-compose.gcp.yml", self.read("deploy_docker.bat"))
        self.assertIn(":ro", self.read("docker-compose.gcp.yml"))
        self.assertIn("UNDERSTUDY_STORE", self.read("docker-compose.yml"))


if __name__ == "__main__":
    unittest.main()
