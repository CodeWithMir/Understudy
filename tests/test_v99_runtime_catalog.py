"""v9.9 - any runtime version the official sources offer, not a fixed list.

Download keys name a release line (jdk-11, node-18, python-3.9, gradle-7, dotnet-6, maven-4); runtime_catalog lists
the lines each source publishes (Adoptium, the Node.js release schedule, CPython's cadence, Microsoft's release index,
Gradle, Maven Central) with their support status. An end-of-life line is offered but needs the admin's confirmation.
The download itself is unchanged: official archive, checksum verified before extraction.
"""
import hashlib
import io
import json
import os
import tarfile
import tempfile
import unittest
import zipfile
from datetime import date
from unittest import mock

import tests.wizard_helpers  # noqa: F401  (isolated test environment)
from core.wizard import package_managers as pms, runtime_catalog as rc, runtimes

TODAY = date.today()
SCHEDULE = {"v18": {"start": "2022-04-19", "lts": "2022-10-25", "end": "2025-04-30"},
            "v20": {"start": "2023-04-18", "lts": "2023-10-24", "end": "2026-04-30"},
            "v22": {"start": "2024-04-24", "lts": "2024-10-29", "end": "2027-04-30"},
            "v24": {"start": "2025-05-06", "lts": "2025-10-28", "end": "2028-04-30"},
            "v25": {"start": "2025-10-15", "end": "2026-06-01"},
            "v99": {"start": "2099-01-01", "end": "2101-01-01"}, "v0.10": {"start": "2013-03-11", "end": "2016-10-31"}}
ADOPTIUM = {"available_releases": [8, 11, 16, 17, 21, 24, 25], "available_lts_releases": [8, 11, 17, 21, 25],
            "most_recent_feature_release": 25, "most_recent_lts": 25}
DOTNET = {"releases-index": [{"channel-version": "10.0", "support-phase": "active", "release-type": "lts"},
                             {"channel-version": "9.0", "support-phase": "maintenance", "release-type": "sts"},
                             {"channel-version": "6.0", "support-phase": "eol", "release-type": "lts", "eol-date": "2024-11-12"},
                             {"channel-version": "11.0", "support-phase": "preview", "release-type": "sts"}]}
MAVEN_META = b"<metadata><versioning><versions><version>3.9.11</version><version>4.0.0</version></versions></versioning></metadata>"


def net(url):
    if "adoptium" in url and "available_releases" in url:
        return json.dumps(ADOPTIUM).encode()
    if "schedule.json" in url:
        return json.dumps(SCHEDULE).encode()
    if "releases-index.json" in url:
        return json.dumps(DOTNET).encode()
    if "versions/current" in url:
        return json.dumps({"version": "9.2.1"}).encode()
    if "maven-metadata.xml" in url:
        return MAVEN_META
    raise OSError("offline: " + url)


class CatalogBase(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"UNDERSTUDY_RUNTIME_CATALOG": "on"})
        self.env.start()
        runtimes.set_transport(net)
        rc._CACHE.clear()

    def tearDown(self):
        runtimes.set_transport(None)
        rc._CACHE.clear()
        self.env.stop()

    def by_key(self):
        return {e["key"]: e for e in rc.entries()}


class TestKeys(unittest.TestCase):
    def test_any_release_line_is_a_valid_key(self):
        for k in ("jdk-8", "jdk-11", "jdk-25", "node-18", "python-3.9", "gradle-7", "dotnet-6", "maven-4", "ruby-3", "go-1"):
            self.assertTrue(runtimes.is_supported(k), k)
        for k in ("jdk-7", "jdk-11.0.2", "node-1", "python-2.7", "maven-2", "gradle-x", "rm -rf", "jdk-11;ls"):
            self.assertFalse(runtimes.is_supported(k), k)
        self.assertEqual(runtimes.kind_of("jdk-11"), "jdk")
        self.assertEqual(rc.label("node-18"), "Node.js 18")
        self.assertEqual(rc.nominal("python-3.9"), "3.9.999")
        self.assertEqual(pms.label("jdk-11"), "Temurin JDK 11")


class TestCatalog(CatalogBase):
    def test_java_lines_from_adoptium(self):
        e = self.by_key()
        self.assertTrue(e["jdk-11"]["lts"] and not e["jdk-11"]["eol"])            # JDK 11 now downloadable
        self.assertTrue(e["jdk-8"]["lts"] and not e["jdk-8"]["eol"])
        self.assertTrue(e["jdk-16"]["eol"])                                        # old non-LTS line
        self.assertFalse(e["jdk-25"]["eol"])
        self.assertEqual(e["jdk-11"]["source"], "live")

    def test_node_lines_and_end_of_life_from_the_schedule(self):
        e = self.by_key()
        self.assertTrue(e["node-18"]["eol"])
        self.assertEqual(e["node-18"]["eol_date"], "2025-04-30")
        self.assertFalse(e["node-24"]["eol"])
        self.assertTrue(e["node-24"]["lts"])
        self.assertNotIn("node-99", e)                                             # not released yet

    def test_python_lines_by_cadence(self):
        e = self.by_key()
        self.assertIn("python-3.9", e)
        self.assertEqual(e["python-3.9"]["eol_date"], "2025-10-31")
        self.assertEqual(e["python-3.9"]["eol"], TODAY.isoformat() > "2025-10-31")
        self.assertFalse(e["python-3.13"]["eol"])

    def test_dotnet_gradle_maven(self):
        e = self.by_key()
        self.assertTrue(e["dotnet-6"]["eol"])
        self.assertFalse(e["dotnet-10"]["eol"])
        self.assertNotIn("dotnet-11", e)                                           # preview
        self.assertFalse(e["gradle-9"]["eol"])
        self.assertTrue(e["gradle-8"]["eol"])
        self.assertIn("maven-4", e)

    def test_unreachable_sources_fall_back_to_the_built_in_list(self):
        runtimes.set_transport(lambda u: (_ for _ in ()).throw(OSError("no network")))
        rc._CACHE.clear()
        e = self.by_key()
        self.assertIn("jdk-11", e)
        self.assertEqual(e["jdk-21"]["source"], "built-in")
        self.assertIn("python-3.12", e)

    def test_offline_mode_skips_the_network(self):
        calls = []
        runtimes.set_transport(lambda u: calls.append(u) or net(u))
        with mock.patch.dict(os.environ, {"UNDERSTUDY_RUNTIME_CATALOG": "offline"}):
            rc.entries(refresh=True)
        self.assertEqual(calls, [])


class TestEndOfLifeNeedsConfirmation(CatalogBase):
    def test_eol_download_is_refused_until_confirmed(self):
        with self.assertRaises(pms.PackageManagerError) as cm:
            pms.download("node-18", "admin", background=False)
        self.assertEqual(cm.exception.status_code, 409)
        self.assertTrue(cm.exception.details["eol"])
        self.assertIn("no security fixes", str(cm.exception))
        with mock.patch.object(runtimes, "provision", side_effect=RuntimeError("stop here")) as p:
            pms.download("node-18", "admin", background=False, confirm_eol=True, make_default=False)
        p.assert_called_once()

    def test_supported_line_needs_no_confirmation(self):
        with mock.patch.object(runtimes, "provision", side_effect=RuntimeError("stop here")) as p:
            pms.download("jdk-11", "admin", background=False, make_default=False)
        p.assert_called_once()

    def test_environments_endpoint_passes_the_confirmation(self):
        from tests.wizard_helpers import admin_client
        c = admin_client()
        r = c.post("/api/admin/environments/download", json={"key": "node-18"})
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.json()["detail"]["details"]["eol"])
        with mock.patch.object(runtimes, "provision", side_effect=RuntimeError("stop here")):
            r = c.post("/api/admin/environments/download", json={"key": "node-18", "confirm_eol": True})
        self.assertEqual(r.status_code, 200, r.text)

    def test_environments_list_marks_old_lines(self):
        from core.wizard import environments
        rows = {d["key"]: d for d in environments.downloadable()}
        self.assertTrue(rows["node-18"]["eol"])
        self.assertEqual(rows["jdk-11"]["kind"], "java")
        keys = [k for k, _ in environments.downloads()["node"]]
        self.assertLess(keys.index("node-24"), keys.index("node-18"))            # supported lines first


class TestDownloadsOfOtherLines(unittest.TestCase):
    """The same verified downloader for lines that weren't in the fixed list."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.p_dir = mock.patch.object(runtimes, "RUNTIMES_DIR", self.tmp)
        self.p_dir.start()

    def tearDown(self):
        self.p_dir.stop()
        runtimes.set_transport(None)

    @staticmethod
    def tgz(files):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            for name, mode in files:
                data = b"#!/bin/sh\necho ok\n"
                ti = tarfile.TarInfo(name)
                ti.size, ti.mode = len(data), mode
                t.addfile(ti, io.BytesIO(data))
        return buf.getvalue()

    def test_jdk_11(self):
        blob = self.tgz([("jdk-11.0.25+9/bin/java", 0o755)])
        sha = hashlib.sha256(blob).hexdigest()
        seen = []

        def net_(url):
            seen.append(url)
            if "api.adoptium.net" in url:
                return json.dumps([{"binary": {"package": {"link": "https://github.example/jdk11.tar.gz", "checksum": sha,
                                                           "name": "jdk11.tar.gz"}}, "release_name": "jdk-11.0.25+9"}]).encode()
            return blob
        runtimes.set_transport(net_)
        info = runtimes.provision("jdk-11")
        self.assertTrue(os.path.isfile(os.path.join(info["home"], "bin", "java")))
        self.assertIn("/latest/11/", seen[0])
        self.assertEqual(info["env_name"], "JAVA_HOME")

    def test_node_18_checksum_still_enforced(self):
        plat = {"linux": "linux", "mac": "darwin", "windows": "win"}[runtimes._os_arch()[0]]
        if plat == "win":
            self.skipTest("tar.gz layout")
        arch = "x64" if runtimes._os_arch()[1] == "x64" else "arm64"
        name = f"node-v18.20.8-{plat}-{arch}.tar.gz"
        blob = self.tgz([(f"node-v18.20.8-{plat}-{arch}/bin/node", 0o755)])
        sums = f"{hashlib.sha256(blob).hexdigest()}  {name}\n"
        runtimes.set_transport(lambda u: sums.encode() if u.endswith("SHASUMS256.txt") else blob)
        info = runtimes.provision("node-18")
        self.assertTrue(os.path.isfile(os.path.join(info["bin"], "node")))
        runtimes.set_transport(lambda u: sums.encode() if u.endswith("SHASUMS256.txt") else blob + b"x")
        import shutil
        shutil.rmtree(os.path.join(self.tmp, "node-18"))
        with self.assertRaises(runtimes.RuntimeError_):
            runtimes.provision("node-18")

    def test_maven_4_line(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("apache-maven-4.0.0/bin/mvn", "#!/bin/sh\n")
        blob = buf.getvalue()
        sha = hashlib.sha512(blob).hexdigest()
        seen = []

        def net_(url):
            seen.append(url)
            if url.endswith("maven-metadata.xml"):
                return MAVEN_META
            if url.endswith(".sha512"):
                return sha.encode()
            return blob
        runtimes.set_transport(net_)
        info = runtimes.provision("maven-4")
        self.assertIn("4.0.0", info["home"])
        self.assertTrue(any("/4.0.0/apache-maven-4.0.0-bin.zip" in u for u in seen))

    def test_cloud_run_memory_guard(self):
        with mock.patch.dict(os.environ, {"K_SERVICE": "understudy"}), \
                mock.patch.object(runtimes, "memory_headroom_mb", return_value=300):
            with self.assertRaises(runtimes.RuntimeError_) as cm:
                runtimes.provision("jdk-21")
        self.assertIn("--memory=8Gi", str(cm.exception))

    def test_setup_chat_understands_any_line(self):
        from core.wizard import runtime_catalog
        self.assertEqual(runtime_catalog.parse("jdk-11"), ("jdk", 11))


if __name__ == "__main__":
    unittest.main()


class TestSelfHealingDownloads(CatalogBase):
    def test_unattended_repair_only_downloads_supported_lines(self):
        from core.wizard import runtime_repair
        facts = {"jdks_found": [], "browsers_found": {}}
        ok, _ = runtime_repair.validate_ai_repair({"action": "download_runtime", "download_runtime": "jdk-11",
                                                   "explanation": "x"}, facts, None, [])
        self.assertEqual(ok["download_runtime"], "jdk-11")
        bad, why = runtime_repair.validate_ai_repair({"action": "download_runtime", "download_runtime": "node-18",
                                                      "explanation": "x"}, facts, None, [])
        self.assertIsNone(bad)                                     # end of life: only the admin, not self-healing
        self.assertIn("Environments", why[0])
