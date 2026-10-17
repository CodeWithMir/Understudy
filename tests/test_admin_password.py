"""9.7.1: the Mission Control password is generated once per installation - the same rule locally and on Cloud Run.

* First start: generated, saved in .env, remembered for the banner; later starts reuse it.
* An empty UNDERSTUDY_ADMIN_PASSWORD= line counts as unset and is replaced (not duplicated).
* The environment wins over .env (Docker / Cloud Run).
* startup() does nothing on Cloud Run (K_SERVICE: the deploy scripts use Secret Manager) or when turned off.
* scripts/admin_password.py ensure / show / reset.
"""
import io
import os
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from core.wizard import admin_password, env_file

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHAPE = re.compile(r"^Us-[A-HJ-NP-Za-km-z2-9]{18}-9$")


def _clean_env():
    env = {k: v for k, v in os.environ.items() if k not in ("UNDERSTUDY_ADMIN_PASSWORD", "K_SERVICE",
                                                            "UNDERSTUDY_SKIP_DOTENV",
                                                            "UNDERSTUDY_ADMIN_PASSWORD_GENERATE")}
    return mock.patch.dict(os.environ, env, clear=True)


class AdminPasswordTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="us-adminpw-")
        self.env = os.path.join(self.dir, ".env")
        with open(self.env, "w") as f:
            f.write("GEMINI_API_KEY=\nUNDERSTUDY_ADMIN_USERNAME=\nUNDERSTUDY_ADMIN_PASSWORD=\nUNDERSTUDY_SIGNUP=on\n")
        admin_password.GENERATED_THIS_START = None

    def tearDown(self):
        admin_password.GENERATED_THIS_START = None

    def _lines(self):
        with open(self.env) as f:
            return [ln for ln in f.read().splitlines() if ln.startswith("UNDERSTUDY_ADMIN_PASSWORD=")]

    def test_generate_shape_meets_password_rules(self):
        pws = {admin_password.generate() for _ in range(20)}
        self.assertEqual(len(pws), 20)
        for pw in pws:
            self.assertRegex(pw, SHAPE)

    def test_first_start_generates_and_later_starts_reuse(self):
        with _clean_env():
            r1 = admin_password.ensure(self.env)
            self.assertTrue(r1["created"])
            self.assertRegex(r1["password"], SHAPE)
            self.assertEqual(os.environ["UNDERSTUDY_ADMIN_PASSWORD"], r1["password"])
            self.assertEqual(admin_password.GENERATED_THIS_START, r1["password"])
            self.assertEqual(self._lines(), [f"UNDERSTUDY_ADMIN_PASSWORD={r1['password']}"])     # replaced, not added
        admin_password.GENERATED_THIS_START = None
        with _clean_env():                                                                      # "restart"
            r2 = admin_password.ensure(self.env)
            self.assertFalse(r2["created"])
            self.assertEqual(r2["password"], r1["password"])
            self.assertEqual(r2["source"], ".env")
            self.assertIsNone(admin_password.GENERATED_THIS_START)
        self.assertEqual(env_file.parse(self.env)["GEMINI_API_KEY"], "")                       # other lines kept
        self.assertEqual(env_file.parse(self.env)["UNDERSTUDY_SIGNUP"], "on")

    def test_environment_wins(self):
        with _clean_env(), mock.patch.dict(os.environ, {"UNDERSTUDY_ADMIN_PASSWORD": "Mine-12345x"}):
            r = admin_password.ensure(self.env)
            self.assertEqual((r["password"], r["source"], r["created"]), ("Mine-12345x", "environment", False))
        self.assertEqual(self._lines(), ["UNDERSTUDY_ADMIN_PASSWORD="])                        # .env untouched

    def test_startup_is_off_on_cloud_run_and_when_disabled(self):
        for extra in ({"K_SERVICE": "understudy"}, {"UNDERSTUDY_ADMIN_PASSWORD_GENERATE": "off"},
                      {"UNDERSTUDY_SKIP_DOTENV": "1"}):
            with _clean_env(), mock.patch.dict(os.environ, extra):
                self.assertIsNone(admin_password.startup(self.env))
                self.assertNotIn("UNDERSTUDY_ADMIN_PASSWORD", os.environ)
        self.assertEqual(self._lines(), ["UNDERSTUDY_ADMIN_PASSWORD="])
        with _clean_env():
            self.assertTrue(admin_password.startup(self.env)["created"])

    def test_startup_survives_a_read_only_file_system(self):
        with _clean_env(), mock.patch.object(env_file, "update", side_effect=OSError("read-only")):
            self.assertIsNone(admin_password.startup(self.env))

    def test_reset_makes_a_new_one(self):
        with _clean_env():
            first = admin_password.ensure(self.env)["password"]
            new = admin_password.reset(self.env)["password"]
            self.assertNotEqual(first, new)
            self.assertEqual(self._lines(), [f"UNDERSTUDY_ADMIN_PASSWORD={new}"])


class CliTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="us-adminpw-cli-")
        self.env = os.path.join(self.dir, ".env")
        open(self.env, "w").write("UNDERSTUDY_ADMIN_PASSWORD=\n")
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import admin_password as cli                                # scripts/admin_password.py
        self.cli = cli

    def tearDown(self):
        sys.path.remove(os.path.join(ROOT, "scripts"))
        sys.modules.pop("admin_password", None)
        admin_password.GENERATED_THIS_START = None

    def run_cli(self, *args):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["admin_password.py", *args]), redirect_stdout(out):
            code = self.cli.main()
        return code, out.getvalue()

    def test_ensure_show_reset(self):
        with _clean_env(), mock.patch.dict(os.environ, {"UNDERSTUDY_ENV_FILE": self.env}):
            code, text = self.run_cli("show")
            self.assertEqual(code, 1)
            code, text = self.run_cli("ensure")
            self.assertEqual(code, 0)
            self.assertIn("shown ONCE", text)
            pw = env_file.parse(self.env)["UNDERSTUDY_ADMIN_PASSWORD"]
            self.assertIn(pw, text)
            os.environ.pop("UNDERSTUDY_ADMIN_PASSWORD", None)                 # next start
            code, text = self.run_cli("ensure")
            self.assertNotIn(pw, text)                                        # not shown again
            self.assertIn("admin_password.py show", text)
            code, text = self.run_cli("show")
            self.assertEqual(code, 0)
            self.assertIn(pw, text)
            os.environ.pop("UNDERSTUDY_ADMIN_PASSWORD", None)
            code, text = self.run_cli("reset")
            self.assertEqual(code, 0)
            self.assertNotEqual(env_file.parse(self.env)["UNDERSTUDY_ADMIN_PASSWORD"], pw)
            with mock.patch.dict(os.environ, {"UNDERSTUDY_ADMIN_PASSWORD": "Set-in-shell-1"}):
                code, text = self.run_cli("reset")
                self.assertEqual(code, 1)


class StartScriptsTest(unittest.TestCase):
    """Every start script uses the same rule and none prints the published default any more."""

    def read(self, name):
        with open(os.path.join(ROOT, name), encoding="utf-8") as f:
            return f.read()

    def test_scripts(self):
        for name in ("run_demo.sh", "run_demo.bat", "run_demo.ps1", "entrypoint.sh"):
            self.assertIn("admin_password.py", self.read(name), name)
        self.assertIn("ensure_admin_password.ps1", self.read("deploy_docker.bat"))
        self.assertIn("/dev/urandom", self.read("deploy_docker.sh"))
        for name in ("run_demo.sh", "run_demo.bat", "run_demo.ps1", "deploy_docker.sh", "deploy_docker.bat",
                     "entrypoint.sh", "entrypoint-cloudrun.sh", "docker-compose.yml"):
            self.assertNotIn("Understudy@2026", self.read(name), name)
        for name in ("run_demo.bat", "run_demo.ps1", "deploy_docker.bat", "scripts/ensure_admin_password.ps1"):
            with open(os.path.join(ROOT, name), "rb") as f:
                data = f.read()
            self.assertEqual(data.count(b"\n"), data.count(b"\r\n"), f"{name} must keep CRLF line endings")


if __name__ == "__main__":
    unittest.main()
