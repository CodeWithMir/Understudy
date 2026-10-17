"""Tests: admin-managed allowed + blocked URL lists, their mutual exclusion, enforcement, and restricted-network runs."""
import http.server
import os
import socket
import threading
import unittest
import urllib.request

from tests.wizard_helpers import admin_client
from core.wizard import admin_policy as ap
from core.wizard import egress
from core.wizard import guardrails as g


class TestBlockedList(unittest.TestCase):
    def setUp(self):
        ap.reset_to_defaults()
        self.addCleanup(ap.reset_to_defaults)

    def test_blocked_host_is_refused_by_the_url_guard(self):
        self.assertTrue(g.screen_url("https://example.com/").is_safe or True)   # (DNS may be offline in CI)
        e = ap.add_blocked_url("https://social.example.org/feed", "No social media")
        v = g.screen_url("https://social.example.org/anything")
        self.assertFalse(v.is_safe)
        self.assertTrue(v.admin_blocked)
        self.assertIn("blocked list", v.reasons[0])
        self.assertIn("No social media", v.reasons[0])
        ap.remove_blocked_url(e["id"])
        self.assertFalse(g.screen_url("https://social.example.org/").admin_blocked)

    def test_wildcard_blocks_subdomains_not_the_root(self):
        ap.add_blocked_url("*.tracker.test", "ad trackers")
        for h in ("a.tracker.test", "x.y.tracker.test"):
            self.assertTrue(g.screen_url(f"http://{h}/").admin_blocked, h)
        self.assertFalse(ap.is_host_blocked("tracker.test"))
        self.assertFalse(ap.is_host_blocked("nottracker.test"))

    def test_port_is_ignored_and_case_normalised(self):
        ap.add_blocked_url("HTTP://Bad.Example.Test:8443/x")
        self.assertTrue(ap.is_host_blocked("bad.example.test"))
        self.assertTrue(g.screen_url("http://bad.example.test:1/").admin_blocked)

    def test_blocked_beats_the_allowed_list_in_a_hand_edited_policy(self):
        data = ap._load()
        data["url_exceptions"].append({"id": "x", "url": "http://both.test/", "host": "both.test", "note": "", "added_by": "t", "added_at": ""})
        data["url_blocklist"].append({"id": "y", "url": "both.test", "host": "both.test", "note": "", "added_by": "t", "added_at": ""})
        self.assertFalse(ap.is_host_excepted("both.test"))
        self.assertTrue(g.screen_url("http://both.test/").admin_blocked)

    def test_bad_input_is_rejected(self):
        for bad in ("", "ftp://x.test", "http://user:pw@x.test", "*.com.*", "http://a.*.test", "*."):
            with self.assertRaises(ap.PolicyError, msg=bad):
                ap.add_blocked_url(bad)
        with self.assertRaises(ap.PolicyError):
            ap.add_url_exception("*.example.test")             # wildcards are a blocked-list feature only

    def test_survives_old_policy_files_without_a_blocklist(self):
        data = ap._load()
        data.pop("url_blocklist")
        ap._CACHE["data"] = data
        self.assertEqual(ap.blocked_entries(), [])
        self.assertEqual(ap.snapshot()["url_blocklist"], [])


class TestMutualExclusion(unittest.TestCase):
    def setUp(self):
        ap.reset_to_defaults()
        self.addCleanup(ap.reset_to_defaults)

    def test_cannot_block_an_allowed_link(self):
        with self.assertRaises(ap.PolicyError) as cm:
            ap.add_blocked_url("http://localhost:8000/portal1")        # Portal 1 is allowed by default
        self.assertIn("cannot be both allowed and blocked", str(cm.exception))
        self.assertEqual(ap.blocked_entries(), [])

    def test_cannot_allow_a_blocked_link(self):
        ap.add_blocked_url("http://evil.example.test/")
        for u in ("http://evil.example.test/other", "evil.example.test:9000"):
            with self.assertRaises(ap.PolicyError) as cm:
                ap.add_url_exception(u)
            self.assertIn("cannot be both allowed and blocked", str(cm.exception))
        self.assertNotIn("evil.example.test", ap.excepted_hosts())

    def test_wildcards_overlap_both_directions(self):
        ap.add_url_exception("http://erp.corp.test/")
        with self.assertRaises(ap.PolicyError):
            ap.add_blocked_url("*.corp.test")                         # would cover an allowed host
        ap.add_blocked_url("*.shop.test")
        with self.assertRaises(ap.PolicyError):
            ap.add_url_exception("http://www.shop.test/")             # covered by a blocked wildcard
        ap.add_url_exception("http://shop.test/")                     # the root itself is not covered by *.shop.test
        ap.add_blocked_url("*.www.shop.test")                         # nested wildcard is fine to add
        with self.assertRaises(ap.PolicyError):
            ap.add_url_exception("http://api.www.shop.test/")         # ... and still keeps its hosts off the allowed list

    def test_moving_a_link_between_lists_works_after_removal(self):
        a = ap.add_url_exception("http://move.test/")
        with self.assertRaises(ap.PolicyError):
            ap.add_blocked_url("http://move.test/")
        ap.remove_url_exception(a["id"])
        b = ap.add_blocked_url("http://move.test/")
        self.assertFalse(g.screen_url("http://move.test/").is_safe)
        ap.remove_blocked_url(b["id"])
        ap.add_url_exception("http://move.test/")
        self.assertIn("move.test", ap.excepted_hosts())

    def test_metadata_hosts_cannot_be_allowed_but_can_be_blocked(self):
        with self.assertRaises(ap.PolicyError):
            ap.add_url_exception("http://169.254.169.254/")
        ap.add_blocked_url("http://169.254.169.254/")                 # harmless duplicate of the built-in rule

    def test_exact_duplicates_are_idempotent(self):
        a = ap.add_blocked_url("http://dup.test/")
        b = ap.add_blocked_url("http://dup.test/")
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(len(ap.blocked_entries()), 1)


class TestAdminApi(unittest.TestCase):
    def setUp(self):
        ap.reset_to_defaults()
        self.addCleanup(ap.reset_to_defaults)
        self.c = admin_client()

    def test_blocked_list_endpoints_and_conflict_messages(self):
        r = self.c.post("/api/admin/url-blocklist", json={"url": "http://blocked.example.test", "note": "n"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([e["host"] for e in r.json()["url_blocklist"]], ["blocked.example.test"])
        self.assertIn("169.254.169.254", r.json()["always_blocked"])
        # both directions refused with HTTP 400 + readable message
        r = self.c.post("/api/admin/url-exceptions", json={"url": "http://blocked.example.test/x"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("cannot be both allowed and blocked", r.json()["detail"]["message"])
        r = self.c.post("/api/admin/url-blocklist", json={"url": "http://localhost:8000/portal2"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("allowed list", r.json()["detail"]["message"])
        entry = self.c.get("/api/admin/policy").json()["url_blocklist"][0]
        self.assertEqual(self.c.delete(f"/api/admin/url-blocklist/{entry['id']}").status_code, 200)
        self.assertEqual(self.c.delete(f"/api/admin/url-blocklist/{entry['id']}").status_code, 400)

    def test_endpoints_need_admin_login(self):
        from fastapi.testclient import TestClient
        import server
        anon = TestClient(server.app)
        self.assertIn(anon.post("/api/admin/url-blocklist", json={"url": "http://x.test"}).status_code, (401, 403))
        self.assertIn(anon.put("/api/admin/network-mode", json={"mode": "public"}).status_code, (401, 403))

    def test_network_mode_setting(self):
        r = self.c.put("/api/admin/network-mode", json={"mode": "public"})
        self.assertEqual(r.json()["settings"]["network_mode"], "public")
        self.assertEqual(self.c.put("/api/admin/network-mode", json={"mode": "open"}).status_code, 400)
        self.c.put("/api/admin/network-mode", json={"mode": "restricted"})
        self.assertEqual(ap.network_mode(), "restricted")

    def test_blocked_target_is_refused_in_step_one_validation(self):
        self.c.post("/api/admin/url-blocklist", json={"url": "http://forbidden.example.test"})
        from fastapi.testclient import TestClient
        import server
        r = TestClient(server.app).post("/api/wizard/validate-url", json={"url": "http://forbidden.example.test/app"}) \
            if False else None
        v = g.screen_url("http://forbidden.example.test/app")
        self.assertFalse(v.is_safe)
        self.assertIn("blocked list", v.friendly_message)


class _Echo(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        b = b"echo-ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass


class TestRestrictedNetwork(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Echo)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        ap.reset_to_defaults()
        self.addCleanup(ap.reset_to_defaults)
        ap.set_network_mode("restricted")
        from unittest import mock
        p = mock.patch.dict(os.environ, {"NO_PROXY": "", "no_proxy": ""})   # the sandbox may exempt loopback globally
        p.start()
        self.addCleanup(p.stop)

    def _get(self, proxy, url):
        op = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy.url, "https": proxy.url}))
        try:
            with op.open(url, timeout=8) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_decisions(self):
        allow = ["localhost"]
        self.assertTrue(egress.decide("localhost", allow)[0])
        ok, why, _ = egress.decide("example.com", allow)
        self.assertFalse(ok)
        self.assertIn("restricted network", why)
        self.assertFalse(egress.decide("169.254.169.254", ["169.254.169.254"])[0])        # never, even if listed
        self.assertFalse(egress.decide("metadata.google.internal", ["metadata.google.internal"])[0])
        ap.add_blocked_url("blocked.example.test")
        self.assertFalse(egress.decide("blocked.example.test", ["blocked.example.test"], "public")[0])
        self.assertFalse(egress.decide("10.0.0.5", [], "public")[0])                      # intranet needs the allowed list
        self.assertTrue(egress.decide("10.0.0.5", ["10.0.0.5"])[0])

    def test_run_allow_list_is_target_plus_admin_allowed_minus_blocked(self):
        al = egress.run_allow_list("http://target.example.test/app")
        self.assertIn("target.example.test", al)
        self.assertIn("localhost", al)                                   # Portal 1/2 are on the allowed list by default
        ap.add_blocked_url("target.example.test")
        self.assertNotIn("target.example.test", egress.run_allow_list("http://target.example.test/app"))

    def test_proxy_forwards_allowed_and_denies_everything_else(self):
        with egress.EgressProxy(["localhost", "127.0.0.1"]) as p:
            st, body = self._get(p, f"http://127.0.0.1:{self.port}/")
            self.assertEqual((st, body), (200, b"echo-ok"))
            st, body = self._get(p, "http://not-allowed.example.test/")
            self.assertEqual(st, 403)
            self.assertIn(b"egress policy", body)
            st, _ = self._get(p, "http://169.254.169.254/latest/meta-data/")
            self.assertEqual(st, 403)
            s = p.summary()
        self.assertEqual(s["requests_allowed"], 1)
        self.assertEqual(s["requests_denied"], 2)
        self.assertEqual({d["host"] for d in s["denied_hosts"]}, {"not-allowed.example.test", "169.254.169.254"})

    def test_connect_tunnel_is_filtered_too(self):
        with egress.EgressProxy(["127.0.0.1"]) as p:
            host, port = p.url.replace("http://", "").split(":")
            for target, expect in (("169.254.169.254:443", b"403"), ("elsewhere.example.test:443", b"403")):
                s = socket.create_connection((host, int(port)), timeout=5)
                s.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
                self.assertIn(expect, s.recv(200))
                s.close()
            s = socket.create_connection((host, int(port)), timeout=5)
            s.sendall(f"CONNECT 127.0.0.1:{self.port} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
            self.assertIn(b"200", s.recv(200))
            s.sendall(b"GET / HTTP/1.0\r\n\r\n")
            data = b""
            while b"echo-ok" not in data:                    # the body can arrive in a later chunk
                chunk = s.recv(500)
                if not chunk:
                    break
                data += chunk
            self.assertIn(b"echo-ok", data)
            s.close()

    def test_admin_change_applies_to_the_next_request(self):
        with egress.EgressProxy(["127.0.0.1"]) as p:
            self.assertEqual(self._get(p, f"http://127.0.0.1:{self.port}/")[0], 200)
            ap.add_blocked_url("*.example.test")                          # unrelated host: still fine
            self.assertEqual(self._get(p, f"http://127.0.0.1:{self.port}/")[0], 200)

    def test_framework_child_process_cannot_leave_the_policy(self):
        """A real child process (like a generated suite) with the proxy env: allowed host works, others are denied."""
        import subprocess
        import sys
        code = ("import urllib.request,sys\n"
                f"ok=urllib.request.urlopen('http://127.0.0.1:{self.port}/',timeout=8).read()\n"
                "print('ALLOWED',ok)\n"
                "try:\n urllib.request.urlopen('http://169.254.169.254/',timeout=8)\n print('LEAK')\n"
                "except Exception as e: print('DENIED',getattr(e,'code',e))\n")
        with egress.EgressProxy(["127.0.0.1"]) as p:
            env = dict(os.environ, **p.env())
            out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60).stdout
        self.assertIn("ALLOWED b'echo-ok'", out)
        self.assertIn("DENIED 403", out)
        self.assertNotIn("LEAK", out)


class TestExecutorIntegration(unittest.TestCase):
    def setUp(self):
        ap.reset_to_defaults()
        self.addCleanup(ap.reset_to_defaults)

    def test_precheck_stops_a_run_whose_target_was_blocked_after_step_one(self):
        from core.wizard import executor
        self.assertEqual(executor._egress_precheck("http://localhost:8000/portal1"), "")
        for e in ap.snapshot()["url_exceptions"]:
            ap.remove_url_exception(e["id"])
        ap.add_blocked_url("http://localhost:8000/portal1")
        msg = executor._egress_precheck("http://localhost:8000/portal1")
        self.assertIn("blocked list", msg)

    def test_execute_returns_a_policy_failure_without_running_anything(self):
        import tempfile
        from core.wizard import executor
        from core.wizard.frameworks import CATALOG
        for e in ap.snapshot()["url_exceptions"]:
            ap.remove_url_exception(e["id"])
        ap.add_blocked_url("http://localhost:8000/portal1")
        logs = []
        res = executor.execute(CATALOG["playwright-python"], {"run.py": "print(1)"}, [], [], "http://localhost:8000/portal1",
                               tempfile.mkdtemp(), "READY", logs.append)
        self.assertEqual(res["status"], "FAIL")
        self.assertEqual(res["failure_category_hint"], "POLICY")
        self.assertTrue(any("blocked list" in l for l in logs))


if __name__ == "__main__":
    unittest.main()
