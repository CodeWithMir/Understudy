"""9.4: a tool setup uses a runtime Understudy already downloaded when the one on PATH can't build - e.g. the .NET host
in Program Files with no SDK ('No .NET SDKs were found', dotnet restore exit 2147516561) while Mission Control >
Environments has a .NET SDK 8 downloaded by Understudy. A path the admin types is understood too."""
import os
import stat
import tempfile
import unittest
from unittest import mock

from tests.wizard_helpers import LiveServer  # noqa: F401  (sets the isolated test environment)

from core.wizard import environments, setup_agent, tool_setup
from core.wizard.frameworks import FrameworkSpec

FAILURE = {"stage": "install", "command": "dotnet restore Automation.csproj", "exit_code": 2147516561,
           "message": "install failed: 'dotnet restore ...' exited with code 2147516561",
           "output": "The command could not be loaded, possibly because:\n  * You intended to execute a .NET SDK command:\n"
                     "      No .NET SDKs were found.\nDownload a .NET SDK:\nhttps://aka.ms/dotnet/download\n"
                     "Learn about SDK resolution:\nhttps://aka.ms/dotnet/sdk-not-found"}


def _script(folder, sdks):
    os.makedirs(folder, exist_ok=True)
    p = os.path.join(folder, "dotnet")
    with open(p, "w") as fh:
        fh.write("#!/bin/sh\nif [ \"$1\" = \"--list-sdks\" ]; then\n" + "".join(f"  echo '{s}'\n" for s in sdks)
                 + "  exit 0\nfi\necho 8.0.425\n")
    os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
    return p


@unittest.skipIf(os.name == "nt", "uses POSIX shell stand-ins for dotnet")
class TestDotnetFallback(unittest.TestCase):
    def setUp(self):
        root = tempfile.mkdtemp(prefix="dotnet-fallback-")
        self.host_dir = os.path.join(root, "program-files-dotnet")
        self.sdk_dir = os.path.join(root, "runtimes", "dotnet-8")
        self.host = _script(self.host_dir, [])                         # the host only - no SDK
        self.sdk = _script(self.sdk_dir, ["8.0.425 [" + self.sdk_dir + "/sdk]"])
        environments._WORKS_CACHE.clear()
        self.rec = environments._record("dotnet", self.sdk, "downloaded by Understudy", "8.0.425", "dotnet-8")
        self.p = [mock.patch.object(environments, "inventory", return_value={"dotnet": [self.rec]}),
                  mock.patch.dict(os.environ, {"PATH": self.host_dir + os.pathsep + os.environ.get("PATH", "")})]
        for p in self.p:
            p.start()
        self.spec = FrameworkSpec.from_dict({"id": "selenium-csharp", "name": "Selenium (C#)", "language": "csharp",
                                             "ecosystem": "nuget", "packages": [], "run_command": ["dotnet", "test"],
                                             "entry_file": "Tests.cs"})

    def tearDown(self):
        for p in self.p:
            p.stop()

    def test_an_sdk_less_host_does_not_count_as_working(self):
        self.assertFalse(environments.runtime_works("dotnet", self.host))
        self.assertTrue(environments.runtime_works("dotnet", self.sdk))

    def test_the_setup_picks_the_downloaded_sdk(self):
        picks = environments.runtimes_for_setup("nuget")
        self.assertEqual([p["rec"]["id"] for p in picks], [self.rec["id"]])
        self.assertIn("no SDK", picks[0]["why"])
        bins, env = environments.runtime_settings(picks[0]["rec"])
        self.assertEqual(bins, [self.sdk_dir])
        self.assertEqual((env["DOTNET_ROOT"], env["DOTNET_MULTILEVEL_LOOKUP"]), (self.sdk_dir, "0"))
        # nothing to do once the setup's PATH already has a working SDK first
        self.assertEqual(environments.runtimes_for_setup("nuget", [self.sdk_dir]), [])

    def test_the_failure_offers_the_downloaded_sdk_in_one_click(self):
        q = tool_setup.diagnose(FAILURE, self.spec)
        self.assertIn("already has a working one", q["question"])
        first = q["options"][0]
        self.assertEqual(first["label"], "Use .NET SDK 8.0.425 (downloaded by Understudy)")
        changes, problems = tool_setup._validate_changes(first["changes"], self.spec, "", trusted=True)
        self.assertEqual(problems, [])
        self.assertEqual(changes["path_prepend"], [self.sdk_dir])
        self.assertEqual(changes["env_vars"]["DOTNET_ROOT"], self.sdk_dir)
        # ... and the agent's question carries it too
        opts = setup_agent._runtime_options(FAILURE, self.spec)
        self.assertEqual(opts[0]["changes"], {"use_runtime": self.rec["id"]})

    def test_a_typed_path_to_the_program_is_understood(self):
        r = {"clarification": {"category": "missing_runtime", "options": []}}
        res = tool_setup._resolve_offline(r, "", f"use this one {self.sdk}")
        self.assertEqual(res["changes"], {"use_runtime": self.rec["id"]})
        # a dotnet folder Understudy does not know: PATH + DOTNET_ROOT, not "is not a folder"
        other = os.path.join(os.path.dirname(self.sdk_dir), "my-dotnet")
        _script(other, ["8.0.100 [x]"])
        res = tool_setup._resolve_offline(r, "", f"{other}/dotnet")
        self.assertEqual(res["changes"]["env_vars"]["DOTNET_ROOT"], other)
        changes, problems = tool_setup._validate_changes(res["changes"], self.spec, f"{other}/dotnet")
        self.assertEqual(problems, [])
        self.assertEqual(changes["path_prepend"], [other])

    def test_a_program_path_from_the_ai_is_turned_into_its_folder(self):
        changes, problems = tool_setup._validate_changes({"path_prepend": [self.sdk]}, self.spec, self.sdk)
        self.assertEqual(problems, [])
        self.assertEqual(changes["path_prepend"], [self.sdk_dir])
        self.assertEqual(changes["env_vars"]["DOTNET_ROOT"], self.sdk_dir)

    def test_the_agent_is_told_what_a_typed_path_is(self):
        txt = setup_agent._annotate_notes([f"admin answered: the sdk is at {self.sdk}"])
        self.assertIn(f"use_runtime = '{self.rec['id']}'", txt)
        self.assertIn("use_runtime", setup_agent.AGENT_KEYS)
        facts = tool_setup._server_facts(self.spec)
        self.assertEqual(facts["installed_runtimes"][0]["id"], self.rec["id"])
        self.assertTrue(facts["installed_runtimes"][0]["works"])


if __name__ == "__main__":
    unittest.main()
