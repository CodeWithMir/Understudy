"""Mission Control's model list comes from the key's real Gemini ``models.list`` (not the built-in suggestions).

Regression (9.5.1): the google-genai client was created inline and garbage-collected before the pager fetched its
first page ("Cannot send a request, as the client has been closed"), so only the 5 built-in models were shown.
A local HTTP server stands in for the Gemini API.
"""
import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

_MODELS = {"models": [
    {"name": "models/gemini-2.5-flash", "displayName": "Gemini 2.5 Flash",
     "supportedGenerationMethods": ["generateContent", "countTokens"]},
    {"name": "models/gemini-9.9-test-flash", "displayName": "Gemini 9.9 Test Flash",
     "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/text-embedding-004", "displayName": "Embedding",
     "supportedGenerationMethods": ["embedContent"]},
]}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = json.dumps(_MODELS).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class ModelDiscoveryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}/"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_lists_the_keys_models(self):
        from google import genai
        from google.genai import types

        from core.wizard import api_keys
        real = genai.Client

        def client(**kw):
            kw["http_options"] = types.HttpOptions(base_url=self.base)
            return real(**kw)

        env = {"NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1", "UNDERSTUDY_SKIP_DOTENV": "1"}
        with mock.patch.dict(os.environ, env), mock.patch("google.genai.Client", client):
            api_keys._DISCOVERED_MODELS_CACHE.clear()
            try:
                ids = [m["id"] for m in api_keys.fetch_available_models("AIzaTESTKEY0000000000")]
            finally:
                api_keys._DISCOVERED_MODELS_CACHE.clear()
        self.assertIn("gemini-9.9-test-flash", ids)       # only the live list has this one
        self.assertIn("gemini-2.5-flash", ids)
        self.assertNotIn("text-embedding-004", ids)       # cannot generateContent


class TestVertexModelDiscovery(unittest.TestCase):
    """9.9: on Vertex AI (service account, no key) the model list comes from Vertex AI's Google publisher models."""

    PUBLISHED = ["publishers/google/models/gemini-3.6-flash", "publishers/google/models/gemini-2.5-pro",
                 "publishers/google/models/gemini-2.5-flash", "publishers/google/models/gemini-2.5-flash-lite",
                 "publishers/google/models/gemini-2.5-flash-image", "publishers/google/models/gemini-embedding-001",
                 "publishers/google/models/gemini-2.5-flash-preview-tts", "publishers/google/models/imagen-4.0-generate-001",
                 "publishers/google/models/veo-3.0-generate-001", "publishers/google/models/gemini-1.5-pro",
                 "publishers/google/models/gemini-live-2.5-flash", "publishers/google/models/text-bison"]

    def setUp(self):
        from core.wizard import api_keys
        self.api_keys = api_keys
        api_keys._DISCOVERED_MODELS_CACHE.clear()
        api_keys._CACHE_SOURCE["source"] = ""
        self.calls = []

        class M:
            def __init__(self, name):
                self.name, self.display_name, self.description, self.supported_actions = name, None, None, None

        outer = self

        class Models:
            def list(self, config=None):
                outer.calls.append(config)
                return [M(n) for n in outer.PUBLISHED]

        class Client:
            models = Models()
        self.client = Client()

    def tearDown(self):
        self.api_keys._DISCOVERED_MODELS_CACHE.clear()
        self.api_keys._CACHE_SOURCE["source"] = ""

    def discover(self):
        from core.wizard import gemini
        with mock.patch.object(gemini, "mode", return_value="vertex"), \
                mock.patch.object(gemini, "_client", return_value=self.client):
            return [m["id"] for m in self.api_keys.fetch_available_models()]

    def test_lists_vertex_gemini_models_without_a_key(self):
        ids = self.discover()
        self.assertEqual(self.calls[0]["query_base"], True)               # Google's publisher models
        self.assertEqual(set(ids), {"gemini-3.6-flash", "gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.5-flash-lite"})
        self.assertEqual(ids[0], "gemini-3.6-flash")                       # recommended first

    def test_status_uses_the_vertex_list_and_keeps_the_selected_model(self):
        from core.wizard import gemini
        with mock.patch.object(gemini, "mode", return_value="vertex"), \
                mock.patch.object(gemini, "_client", return_value=self.client), \
                mock.patch.dict(os.environ, {"GEMINI_MODEL": "gemini-custom-tuned", "UNDERSTUDY_SKIP_DOTENV": "1"}):
            st = self.api_keys.get_key_status()
        self.assertIn("gemini-2.5-pro", st["model_ids"])
        self.assertEqual(st["model_ids"][0], "gemini-custom-tuned")        # the chosen model is always selectable
        self.assertGreaterEqual(st["total_models_count"], 5)

    def test_a_vertex_list_never_serves_a_key_and_the_other_way_round(self):
        self.discover()
        self.assertEqual(self.api_keys._cached("studio"), [])
        self.assertTrue(self.api_keys._cached("vertex"))

    def test_endpoint_says_the_list_came_from_vertex(self):
        from fastapi.testclient import TestClient
        from core.wizard import gemini
        import server
        with mock.patch.object(gemini, "mode", return_value="vertex"), \
                mock.patch.object(gemini, "_client", return_value=self.client):
            j = TestClient(server.app).get("/api/config/models").json()
        self.assertEqual(j["source"], "vertex")
        self.assertEqual(j["total_count"], 4)

    def test_listing_error_falls_back_to_suggestions(self):
        from core.wizard import gemini

        class Broken:
            class models:
                @staticmethod
                def list(config=None):
                    raise RuntimeError("403 PERMISSION_DENIED")
        with mock.patch.object(gemini, "mode", return_value="vertex"), \
                mock.patch.object(gemini, "_client", return_value=Broken()):
            ids = [m["id"] for m in self.api_keys.fetch_available_models()]
        self.assertIn("gemini-2.5-flash", ids)
        self.assertIn("PERMISSION_DENIED", self.api_keys._CACHE_SOURCE["error"])   # shown to the admin


if __name__ == "__main__":
    unittest.main()
