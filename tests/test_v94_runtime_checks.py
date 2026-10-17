"""9.4: every runtime a tool needs is checked for what its build really uses (a JDK with javac, node WITH npm, ruby
WITH gem, a .NET host WITH an SDK). When the server has no working one, the setup stops BEFORE installing and asks
the admin to approve the download it needs; a click, 'yes', 'use 17' or a path continues the setup."""
import os
import stat
import tempfile
import unittest
from unittest import mock

from tests.wizard_helpers import LiveServer, install_fake  # noqa: F401  (sets the isolated test environment)

from core.wizard import admin_policy as ap, environments, tool_setup
from core.wizard.frameworks import CATALOG, FrameworkSpec


def prog(folder, name, out="1.0.0"):
    os.makedirs(folder, exist_ok=True)
    p = os.path.join(folder, name)
    with open(p, "w") as fh:
        fh.write(f"#!/bin/sh\necho '{out}'\n")
    os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
    return p


@unittest.skipIf(os.name == "nt", "uses POSIX shell stand-ins")
class TestWhatWorks(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="rt-checks-")
        environments._WORKS_CACHE.clear()

    def test_a_jre_is_not_a_jdk(self):
        jre = prog(os.path.join(self.root, "jre", "bin"), "java", 'openjdk version "17.0.9"')
        self.assertFalse(environments.runtime_works("java", jre))
        jdk_bin = os.path.join(self.root, "jdk", "bin")
        jdk = prog(jdk_bin, "java", 'openjdk version "17.0.9"')
        prog(jdk_bin, "javac", "javac 17.0.9")
        self.assertTrue(environments.runtime_works("java", jdk))

    def test_node_needs_npm_and_ruby_needs_gem(self):
        node = prog(os.path.join(self.root, "node"), "node", "v20.11.0")
        self.assertFalse(environments.runtime_works("node", node))
        prog(os.path.join(self.root, "node"), "npm", "10.2.4")
        environments._WORKS_CACHE.clear()
        self.assertTrue(environments.runtime_works("node", node))
        ruby = prog(os.path.join(self.root, "ruby"), "ruby", "ruby 3.3.0")
        self.assertFalse(environments.runtime_works("ruby", ruby))

    def test_windows_node_folder_uses_npm_cmd(self):
        """9.7.2: a Windows Node.js folder has both 'npm' (a Git Bash script Windows can't start) and 'npm.cmd'. The
        check used 'npm', so every Node.js - system or downloaded - was rejected as 'does not start'."""
        from core.wizard import package_managers as pms
        folder = os.path.join(self.root, "node-v22.23.3-win-x64")
        for name in ("node.exe", "npm", "npm.cmd", "gem", "gem.cmd", "ruby.exe"):
            prog(folder, name)
        tried = []

        def fake_version(exe):
            tried.append(os.path.basename(exe))
            return "10.9.4" if exe.endswith((".cmd", ".bat", ".exe")) else ""    # the bare script can't start
        with mock.patch.object(environments.os, "name", "nt"), \
                mock.patch.object(environments, "_version_of", return_value="22.23.3"), \
                mock.patch.object(pms, "_version", side_effect=fake_version):
            self.assertTrue(environments.runtime_works("node", os.path.join(folder, "node.exe")))
            self.assertTrue(environments.runtime_works("ruby", os.path.join(folder, "ruby.exe")))
        self.assertEqual(tried, ["npm.cmd", "gem.cmd"])

    def test_nothing_usable_means_a_download_is_proposed(self):
        jre_bin = os.path.join(self.root, "jre", "bin")
        prog(jre_bin, "java", 'openjdk version "11.0.20"')
        with mock.patch.object(environments, "inventory", return_value={"java": [], "maven": []}), \
                mock.patch.dict(os.environ, {"PATH": jre_bin, "JAVA_HOME": ""}):
            picks = environments.runtimes_for_setup("maven")
        java = next(p for p in picks if p["kind"] == "java")
        self.assertIsNone(java["rec"])
        self.assertIn("JRE without the compiler", java["why"])
        self.assertEqual(java["download_keys"][:2], ["jdk-25", "jdk-21"])      # 9.9: newest supported lines first
        maven = next(p for p in picks if p["kind"] == "maven")
        self.assertEqual(maven["download_keys"], ["maven-3"])

    def test_a_broken_java_home_is_not_trusted(self):
        jdk_bin = os.path.join(self.root, "jdk", "bin")
        prog(jdk_bin, "java", 'openjdk version "21.0.2"')
        prog(jdk_bin, "javac", "javac 21.0.2")
        good = environments._record("java", os.path.join(jdk_bin, "java"), "downloaded by Understudy", "21.0.2", "jdk-21")
        broken_home = os.path.join(self.root, "jre-home")
        prog(os.path.join(broken_home, "bin"), "java", 'openjdk version "11"')
        with mock.patch.object(environments, "inventory", return_value={"java": [good], "maven": []}), \
                mock.patch.dict(os.environ, {"PATH": jdk_bin}):
            picks = environments.runtimes_for_setup("maven", env={"JAVA_HOME": broken_home})
        java = next(p for p in picks if p["kind"] == "java")
        self.assertEqual(java["rec"]["id"], good["id"])
        self.assertIn("JAVA_HOME", java["why"])
        self.assertEqual(environments.runtime_settings(java["rec"])[1]["JAVA_HOME"], good["home"])


class TestAskToDownload(unittest.TestCase):
    FD = {"stage": "runtime_missing", "message": "needs Java", "runtime_kind": "java",
          "download_keys": ["jdk-21", "jdk-17"], "why": "the Java (JDK) on PATH (/usr/bin/java) can't build - it is a JRE",
          "missing_runtimes": [{"kind": "java", "why": "JRE", "download_keys": ["jdk-21", "jdk-17"]},
                               {"kind": "maven", "why": "no Apache Maven", "download_keys": ["maven-3"]}]}

    def setUp(self):
        install_fake(None)
        ap.reset_to_defaults()
        self.spec = FrameworkSpec.from_dict(CATALOG["selenium-java"].to_dict()) if "selenium-java" in CATALOG else \
            FrameworkSpec.from_dict(next(s for s in CATALOG.values() if s.ecosystem == "maven").to_dict())
        self.req = ap.request_tool(self.spec, "s1")
        ap.update_request(self.req["id"], plan={"spec": self.spec.to_dict(), "manifest": {}})

    def tearDown(self):
        ap.reset_to_defaults()

    def test_the_question_offers_the_download_in_one_click(self):
        q = tool_setup._runtime_question({"tool_name": self.spec.name}, self.FD)
        self.assertEqual(q["category"], "runtime_download")
        self.assertIn("May I download", q["question"])
        self.assertIn("Apache Maven", q["question"])                      # the other missing runtime comes along
        first = q["options"][0]
        self.assertIn("recommended", first["label"])
        self.assertEqual(first["changes"], {"download_runtimes": ["jdk-21", "maven-3"]})
        self.assertEqual(q["recommended"], first["id"])
        ids = [o["id"] for o in q["options"]]
        self.assertIn("provide_path", ids)
        self.assertIn("stop", ids)

    def test_plain_answers_continue_the_setup(self):
        q = tool_setup._runtime_question({"tool_name": self.spec.name}, self.FD)
        r = {"clarification": q}
        self.assertEqual(tool_setup._resolve_offline(r, "", "yes go ahead")["changes"],
                         {"download_runtimes": ["jdk-21", "maven-3"]})
        self.assertEqual(tool_setup._resolve_offline(r, "", "we standardise on 17 here")["changes"],
                         {"download_runtimes": ["jdk-17", "maven-3"]})
        self.assertEqual(tool_setup._resolve_offline(r, "", "stop")["next"], "stop")

    def test_halt_asks_without_guessing_and_the_answer_resumes(self):
        setup = {"status": "running", "round": 0, "logs": [], "overrides": {}}
        tool_setup._halt(self.req["id"], setup, tool_setup.SetupFailure(
            "runtime_missing", "needs Java", data={k: v for k, v in self.FD.items() if k not in ("stage", "message")}))
        r = ap.get_request(self.req["id"])
        self.assertEqual(r["status"], "awaiting_admin")
        self.assertEqual(r["clarification"]["category"], "runtime_download")
        with mock.patch.object(tool_setup, "_setup_job") as job:
            tool_setup.answer(self.req["id"], "admin", text="", option=r["clarification"]["options"][0]["id"],
                              background=False)
        r = ap.get_request(self.req["id"])
        self.assertEqual(r["setup"]["overrides"]["download_runtimes"], ["jdk-21", "maven-3"])
        self.assertTrue(job.called)


class TestUnfinishedNpmInstall(unittest.TestCase):
    """9.7.2: a failed npm install leaves a broken node_modules / package-lock.json; the retry starts clean."""

    def test_unfinished_tree_is_removed_and_a_finished_one_kept(self):
        from types import SimpleNamespace
        from core.wizard.sandbox import local
        env_dir = tempfile.mkdtemp(prefix="npm-env-")
        os.makedirs(os.path.join(env_dir, "node_modules", "webdriverio"))
        open(os.path.join(env_dir, "package-lock.json"), "w").write("{}")
        open(os.path.join(env_dir, "package.json"), "w").write("{}")
        logs = []
        me = SimpleNamespace(spec=SimpleNamespace(ecosystem="npm"), env_dir=env_dir, log=logs.append)
        local.LocalProcessSandboxRunner._clear_unfinished_npm_tree(me)
        self.assertEqual(sorted(os.listdir(env_dir)), ["package.json"])
        self.assertTrue(any("unfinished" in m for m in logs))
        os.makedirs(os.path.join(env_dir, "node_modules", "webdriverio"))      # a finished install is never touched
        open(os.path.join(env_dir, local.NPM_DONE), "w").write("ok")
        local.LocalProcessSandboxRunner._clear_unfinished_npm_tree(me)
        self.assertTrue(os.path.isdir(os.path.join(env_dir, "node_modules", "webdriverio")))
        me.spec.ecosystem = "pip"                                                # other ecosystems: untouched
        os.remove(os.path.join(env_dir, local.NPM_DONE))
        local.LocalProcessSandboxRunner._clear_unfinished_npm_tree(me)
        self.assertTrue(os.path.isdir(os.path.join(env_dir, "node_modules")))


if __name__ == "__main__":
    unittest.main()
