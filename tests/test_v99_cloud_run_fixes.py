"""v9.9 - three Cloud Run fixes.

1. Agentic runs use the organisation's Gemini through a per-run relay (core/wizard/agent_relay.py): token-bound,
   size- and budget-limited, gone when the run ends; the agent runtime's RelayBrain talks to it without the proxy.
2. The upload limit the UI shows is the real one (Cloud Run without Cloud Storage: what fits in one request).
3. Runtimes earlier tool setups downloaded come back on an instance that doesn't have them
   (core/wizard/runtime_restore.py), and paths into an older patch release map onto the one this machine has.
"""
import base64
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest import mock

import tests.wizard_helpers  # noqa: F401  (isolated test environment)
from tests.wizard_helpers import FakeLLM, install_fake
from core.wizard import admin_policy as ap, agent_relay, runtime_restore, runtimes, uploads


def post(url, body, token, raw=None):
    data = raw if raw is not None else json.dumps(body).encode()
    req = urllib.request.Request(url + "/generate", data=data, method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


class TestRelay(unittest.TestCase):
    def setUp(self):
        self.seen = []

        def gen(system, prompt, image, model):
            self.seen.append((system, prompt, image, model))
            return json.dumps({"action": "click", "element": "e1"})
        self.relay = agent_relay.Relay(generate=gen, model_name="gemini-test").start()

    def tearDown(self):
        self.relay.stop()

    def test_answers_with_the_token_only(self):
        st, body = post(self.relay.url, {"system": "s", "prompt": "p"}, self.relay.token)
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(body["text"])["action"], "click")
        self.assertEqual(self.seen[0][3], "gemini-test")
        self.assertEqual(post(self.relay.url, {"system": "s", "prompt": "p"}, "wrong")[0], 401)
        self.assertEqual(self.relay.summary()["calls"], 1)

    def test_screenshot_is_passed_on(self):
        png = b"\x89PNG fake"
        st, _ = post(self.relay.url, {"system": "s", "prompt": "p", "image": base64.b64encode(png).decode()},
                     self.relay.token)
        self.assertEqual(st, 200)
        self.assertEqual(self.seen[0][2], png)

    def test_limits(self):
        self.assertEqual(post(self.relay.url, {"system": "s", "prompt": ""}, self.relay.token)[0], 413)
        self.assertEqual(post(self.relay.url, None, self.relay.token, raw=b"not json")[0], 400)
        big = "x" * (agent_relay.MAX_PROMPT_BYTES + 1)
        self.assertEqual(post(self.relay.url, {"system": "s", "prompt": big}, self.relay.token)[0], 413)
        with mock.patch.object(agent_relay, "MAX_CALLS", 1):
            self.assertEqual(post(self.relay.url, {"system": "s", "prompt": "p"}, self.relay.token)[0], 200)
            st, body = post(self.relay.url, {"system": "s", "prompt": "p"}, self.relay.token)
        self.assertEqual(st, 429)
        self.assertIn("budget", body["error"])

    def test_errors_keep_their_meaning(self):
        r = agent_relay.Relay(generate=lambda *a: (_ for _ in ()).throw(RuntimeError("429 RESOURCE_EXHAUSTED"))).start()
        try:
            st, body = post(r.url, {"system": "s", "prompt": "p"}, r.token)
        finally:
            r.stop()
        self.assertEqual(st, 429)
        self.assertTrue(body["error"])

    def test_token_is_useless_after_the_run(self):
        url, token, env = self.relay.url, self.relay.token, self.relay.env()
        self.assertEqual(env[agent_relay.TOKEN_ENV], token)
        self.assertTrue(env[agent_relay.TOKEN_ENV] and env[agent_relay.URL_ENV].startswith("http://127.0.0.1:"))
        self.relay.stop()
        with self.assertRaises(Exception):
            post(url, {"system": "s", "prompt": "p"}, token)

    def test_relay_token_passes_the_sandbox_but_is_not_a_payload_secret(self):
        from core.wizard.sandbox.local import restricted_env
        env = restricted_env(self.relay.env())
        self.assertEqual(env.get(agent_relay.TOKEN_ENV), self.relay.token)
        import core.wizard.agent_runtime as rt
        self.assertIn(agent_relay.TOKEN_ENV, rt.NOT_PAYLOAD)


class TestRelayBrain(unittest.TestCase):
    def test_agent_runtime_asks_through_the_relay(self):
        def gen(system, prompt, image, model):
            return '{"answer": true, "evidence": "ok"}'
        relay = agent_relay.Relay(generate=gen).start()
        try:
            import importlib
            with mock.patch.dict(os.environ, {**relay.env(), "HTTPS_PROXY": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9"}):
                import core.wizard.agent_runtime as rt
                rt = importlib.reload(rt)
                brain = rt.RelayBrain()
                self.assertEqual(brain.ask("sys", "{}")["answer"], True)       # loopback, not through the proxy
        finally:
            relay.stop()
            import importlib
            import core.wizard.agent_runtime as rt
            importlib.reload(rt)


class TestOrganisationAi(unittest.TestCase):
    def tearDown(self):
        install_fake(None)

    def test_info_follows_the_admins_setup(self):
        install_fake(None)
        self.assertFalse(agent_relay.info()["available"])
        install_fake(FakeLLM({}))
        i = agent_relay.info()
        self.assertTrue(i["available"])
        self.assertTrue(i["model"])

    def test_default_generator_uses_the_stand_in(self):
        install_fake(FakeLLM({"AGENT_STEP": {"action": "done"}}))
        self.assertEqual(json.loads(agent_relay._generate("s", "{}", None, "m"))["action"], "done")


class TestUploadLimit(unittest.TestCase):
    def test_cloud_run_without_storage_shows_what_fits(self):
        with mock.patch.dict(os.environ, {"K_SERVICE": "understudy"}), \
                mock.patch.object(uploads, "direct_available", return_value=False):
            info = uploads.limit_info()
        self.assertEqual(info["max_media_mb"], 31)
        self.assertIn("32 MB", info["max_media_why"])

    def test_cloud_run_with_storage_keeps_the_full_limit(self):
        with mock.patch.dict(os.environ, {"K_SERVICE": "understudy"}), \
                mock.patch.object(uploads, "direct_available", return_value=True):
            info = uploads.limit_info()
        self.assertEqual(info["max_media_mb"], uploads.MAX_MEDIA_BYTES // (1024 * 1024))
        self.assertIn("Cloud Storage", info["max_media_why"])

    def test_local_keeps_the_full_limit(self):
        env = {k: v for k, v in os.environ.items() if k != "K_SERVICE"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(uploads.max_bytes(), uploads.MAX_MEDIA_BYTES)

    def test_info_endpoint_reports_it(self):
        from fastapi.testclient import TestClient
        import server
        with mock.patch.dict(os.environ, {"K_SERVICE": "understudy"}), \
                mock.patch.object(uploads, "direct_available", return_value=False):
            j = TestClient(server.app).get("/api/wizard/info").json()
        self.assertEqual(j["max_media_mb"], 31)


class TestRuntimeRestore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.p_dir = mock.patch.object(runtimes, "RUNTIMES_DIR", self.tmp)
        self.p_dir.start()
        ap.reset_to_defaults()

    def tearDown(self):
        self.p_dir.stop()
        ap.reset_to_defaults()

    def fake_install(self, key, release):
        home = os.path.join(self.tmp, key, release)
        os.makedirs(os.path.join(home, "bin"))
        info = {"key": key, "home": home, "bin": os.path.join(home, "bin"), "env_name": "JAVA_HOME",
                "version": release, "source": "test"}
        with open(os.path.join(self.tmp, key, ".understudy-runtime.json"), "w") as fh:
            json.dump(info, fh)
        return info

    def test_paths_into_an_older_release_map_onto_this_machines(self):
        info = self.fake_install("jdk-21", "jdk-21.0.13+1")
        old = os.path.join(self.tmp, "jdk-21", "jdk-21.0.12+1", "bin")
        self.assertEqual(runtimes.resolve_path(old), info["bin"])
        self.assertEqual(runtimes.resolve_path(os.path.join(self.tmp, "jdk-21", "jdk-21.0.12+1")), info["home"])
        self.assertEqual(runtimes.resolve_path("/usr/bin"), "/usr/bin")
        self.assertEqual(runtimes.key_of_path(old), "jdk-21")
        self.assertIsNone(runtimes.key_of_path("/usr/lib/jvm"))

    def test_missing_runtimes_are_found_in_the_shared_settings_and_restored(self):
        old_home = os.path.join(self.tmp, "jdk-21", "jdk-21.0.12+1")
        ap.set_toolchain("jdk", {"home": old_home, "bin": os.path.join(old_home, "bin"), "env_name": "JAVA_HOME",
                                 "version": "21.0.12", "source": "download", "kind": "jdk"})
        ap.whitelist_catalog_tool("selenium-python")
        e = ap._load()["tool_whitelist"]["selenium-python"]
        e.setdefault("setup", {})["runtime"] = {"env_vars": {"MAVEN_HOME": os.path.join(self.tmp, "maven-3", "apache-maven-3.9.9")},
                                                "path_prepend": [os.path.join(self.tmp, "maven-3", "apache-maven-3.9.9", "bin")]}
        ap._save()
        self.assertEqual(runtime_restore.missing(), ["jdk-21", "maven-3"])
        provisioned = []

        def provision(key, log=None):
            provisioned.append(key)
            return self.fake_install(key, {"jdk-21": "jdk-21.0.13+1", "maven-3": "apache-maven-3.9.11"}[key])
        with mock.patch.object(runtimes, "provision", side_effect=provision):
            st = runtime_restore.restore()
        self.assertEqual(st["restored"], ["jdk-21", "maven-3"])
        self.assertEqual(provisioned, ["jdk-21", "maven-3"])
        self.assertEqual(runtime_restore.missing(), [])
        tc = ap.toolchains()["jdk"]                                     # re-registered at the new release
        self.assertTrue(tc["home"].endswith("jdk-21.0.13+1"))
        rt = ap.tool_runtime("selenium-python")                         # the tool's settings map onto it
        self.assertTrue(rt["env_vars"]["MAVEN_HOME"].endswith("apache-maven-3.9.11"))
        self.assertTrue(rt["path_prepend"][0].endswith(os.path.join("apache-maven-3.9.11", "bin")))

    def test_a_failed_download_is_reported_not_raised(self):
        old_home = os.path.join(self.tmp, "jdk-21", "jdk-21.0.12+1")
        ap.set_toolchain("jdk", {"home": old_home, "bin": os.path.join(old_home, "bin"), "kind": "jdk", "source": "download"})
        with mock.patch.object(runtimes, "provision", side_effect=RuntimeError("network down")):
            st = runtime_restore.restore()
        self.assertEqual(st["status"], "failed")
        self.assertIn("network down", st["failed"]["jdk-21"])

    def test_nothing_to_do_without_downloads(self):
        with mock.patch.object(runtimes, "provision") as p:
            runtime_restore.ensure()
        p.assert_not_called()

    def test_toolchain_check_counts_the_tools_own_run_settings(self):
        from core.wizard import dependency_resolver
        bindir = os.path.join(self.tmp, "fakejava", "bin")
        os.makedirs(bindir)
        exe = os.path.join(bindir, "java")
        with open(exe, "w") as fh:
            fh.write("#!/bin/sh\necho java\n")
        os.chmod(exe, 0o755)
        ap.whitelist_catalog_tool("selenium-java")
        e = ap._load()["tool_whitelist"]["selenium-java"]
        e.setdefault("setup", {})["runtime"] = {"path_prepend": [bindir]}
        ap._save()
        with mock.patch.dict(os.environ, {"PATH": "/nonexistent"}):
            self.assertIn("java", dependency_resolver.check_toolchain("maven"))
            self.assertNotIn("java", dependency_resolver.check_toolchain("maven", "selenium-java"))


if __name__ == "__main__":
    unittest.main()
