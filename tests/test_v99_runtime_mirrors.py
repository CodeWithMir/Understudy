"""v9.9 - mirrors for runtime downloads (an internal Artifactory / Nexus).

The official addresses stay fixed in the code; the admin can point a source at a mirror. Every download and version
list is fetched from the mirror, the checksum is still verified (a tampered mirror is refused), only https addresses
without credentials are accepted, every change is audited, and the mirror can be tested before saving.
"""
import hashlib
import io
import json
import os
import tarfile
import tempfile
import unittest
from unittest import mock

import tests.wizard_helpers  # noqa: F401  (isolated test environment)
from tests.wizard_helpers import admin_client
from core.wizard import activity_log, admin_policy as ap, runtime_catalog as rc, runtime_mirrors as rm, runtimes

MIRROR = "https://artifactory.example.com/artifactory/adoptium-api/"
FILES = "https://artifactory.example.com/artifactory/github-adoptium/"


class Base(unittest.TestCase):
    def setUp(self):
        ap.reset_to_defaults()
        activity_log.clear()
        rc._CACHE.clear()
        self.tmp = tempfile.mkdtemp()
        self.p_dir = mock.patch.object(runtimes, "RUNTIMES_DIR", self.tmp)
        self.p_dir.start()

    def tearDown(self):
        self.p_dir.stop()
        runtimes.set_transport(None)
        ap.reset_to_defaults()
        rc._CACHE.clear()


class TestRules(Base):
    def test_only_https_without_credentials(self):
        for bad in ("http://mirror.example.com/", "https://user:pw@mirror.example.com/", "https://tok@mirror.example.com/",
                    "ftp://x/", "https://mirror.example.com/?x=1", "javascript:alert(1)", "https://"):
            with self.assertRaises(rm.MirrorError, msg=bad):
                rm.set_mirror("nodejs", bad)
        self.assertEqual(rm.check_url("https://nexus.corp.local:8443/repository/nodejs"),
                         "https://nexus.corp.local:8443/repository/nodejs/")              # a trailing / is added
        with self.assertRaises(rm.MirrorError):
            rm.set_mirror("not-a-source", "https://x.example.com/")

    def test_mapping_both_ways(self):
        rm.set_mirror("adoptium-api", MIRROR)
        self.assertEqual(rm.mirrored("https://api.adoptium.net/v3/assets/latest/11/hotspot?x=1"),
                         MIRROR + "v3/assets/latest/11/hotspot?x=1")
        self.assertEqual(rm.mirrored("https://nodejs.org/dist/index.json"), "https://nodejs.org/dist/index.json")
        self.assertEqual(rm.official(MIRROR + "v3/x"), "https://api.adoptium.net/v3/x")

    def test_change_is_audited_and_removable(self):
        rm.set_mirror("nodejs", "https://nexus.example.com/repository/nodejs-dist/", "boss")
        ev = [e for e in activity_log.query(limit=100)["events"] if e["kind"] == "ADMIN"][-1]
        self.assertIn("boss", ev["title"])
        self.assertIn("nodejs-dist", ev["detail"])
        self.assertEqual(rm.mirrors(), {"nodejs": "https://nexus.example.com/repository/nodejs-dist/"})
        rm.set_mirror("nodejs", "")
        self.assertEqual(rm.mirrors(), {})


class TestDownloadsThroughTheMirror(Base):
    def jdk(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            data = b"#!/bin/sh\n"
            ti = tarfile.TarInfo("jdk-11.0.25+9/bin/java")
            ti.size, ti.mode = len(data), 0o755
            t.addfile(ti, io.BytesIO(data))
        return buf.getvalue()

    def test_jdk_comes_from_the_mirror_and_is_verified(self):
        rm.set_mirror("adoptium-api", MIRROR)
        rm.set_mirror("adoptium-files", FILES)
        blob = self.jdk()
        sha = hashlib.sha256(blob).hexdigest()
        seen = []

        def net(url):
            seen.append(url)
            if not url.startswith("https://artifactory.example.com/"):
                raise OSError("the official site is not reachable from this server: " + url)
            if "/v3/assets/" in url:      # a plain proxy repository hands back the official download link
                return json.dumps([{"binary": {"package": {"checksum": sha, "name": "jdk11.tar.gz",
                                   "link": "https://github.com/adoptium/temurin11-binaries/releases/download/x/jdk11.tar.gz"}},
                                    "release_name": "jdk-11.0.25+9"}]).encode()
            return blob
        runtimes.set_transport(net)
        info = runtimes.provision("jdk-11")
        self.assertTrue(os.path.isfile(os.path.join(info["home"], "bin", "java")))
        self.assertTrue(all(u.startswith("https://artifactory.example.com/") for u in seen), seen)
        self.assertTrue(any(u.startswith(FILES + "temurin11-binaries/") for u in seen))

    def test_a_tampered_mirror_is_refused(self):
        rm.set_mirror("adoptium-api", MIRROR)
        rm.set_mirror("adoptium-files", FILES)
        blob = self.jdk()
        sha = hashlib.sha256(blob).hexdigest()

        def net(url):
            if "/v3/assets/" in url:
                return json.dumps([{"binary": {"package": {"checksum": sha, "name": "jdk11.tar.gz",
                                   "link": "https://github.com/adoptium/temurin11-binaries/releases/download/x/jdk11.tar.gz"}},
                                    "release_name": "jdk-11.0.25+9"}]).encode()
            return blob + b"evil"
        runtimes.set_transport(net)
        with self.assertRaises(runtimes.RuntimeError_):
            runtimes.provision("jdk-11")
        self.assertIsNone(runtimes.installed("jdk-11"))

    def test_mirror_that_rewrites_links_to_itself_passes_the_location_check(self):
        rm.set_mirror("gradle", "https://nexus.example.com/repository/gradle/")
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("gradle-9.2.1/bin/gradle", "#!/bin/sh\n")
        blob = buf.getvalue()
        sha = hashlib.sha256(blob).hexdigest()
        base = "https://nexus.example.com/repository/gradle/"

        def net(url):
            if url.endswith("versions/all"):
                return json.dumps([{"version": "9.2.1", "downloadUrl": base + "distributions/gradle-9.2.1-bin.zip",
                                    "checksumUrl": base + "distributions/gradle-9.2.1-bin.zip.sha256"}]).encode()
            if url.endswith(".sha256"):
                return sha.encode()
            return blob
        runtimes.set_transport(net)
        info = runtimes.provision("gradle-9")
        self.assertIn("gradle-9.2.1", info["home"])

    def test_version_lists_come_from_the_mirror_too(self):
        rm.set_mirror("adoptium-api", MIRROR)
        seen = []
        runtimes.set_transport(lambda u: seen.append(u) or json.dumps(
            {"available_releases": [11, 21], "available_lts_releases": [11, 21], "most_recent_feature_release": 21}).encode())
        with mock.patch.dict(os.environ, {"UNDERSTUDY_RUNTIME_CATALOG": "on"}):
            rc._CACHE.clear()
            rc._java()
        self.assertEqual(seen[0], MIRROR + "v3/info/available_releases")


class TestApi(Base):
    def test_admin_endpoints(self):
        c = admin_client()
        rows = {r["id"]: r for r in c.get("/api/admin/runtime-mirrors").json()["sources"]}
        self.assertEqual(rows["nodejs"]["official"], "https://nodejs.org/dist/")
        self.assertEqual(c.put("/api/admin/runtime-mirrors/nodejs", json={"url": "http://x.example.com/"}).status_code, 400)
        r = c.put("/api/admin/runtime-mirrors/nodejs", json={"url": "https://nexus.example.com/nodejs/"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual({x["id"]: x for x in r.json()["sources"]}["nodejs"]["mirror"], "https://nexus.example.com/nodejs/")
        runtimes.set_transport(lambda u: b'[{"version":"v24.10.0"}]' if u == "https://nexus.example.com/nodejs/index.json"
                               else (_ for _ in ()).throw(OSError("unreachable")))
        t = c.post("/api/admin/runtime-mirrors/nodejs/test", json={}).json()
        self.assertTrue(t["ok"], t)
        t = c.post("/api/admin/runtime-mirrors/nodejs/test", json={"url": "https://other.example.com/"}).json()
        self.assertFalse(t["ok"])                                  # tested before saving - and it doesn't answer
        from fastapi.testclient import TestClient
        import server
        self.assertEqual(TestClient(server.app).get("/api/admin/runtime-mirrors").status_code, 401)   # admins only


if __name__ == "__main__":
    unittest.main()
