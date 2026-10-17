"""7.1: the admin can always see whether the AI changed Understudy's own code, exactly which lines, why, where the
backup is - and revert it (Mission Control or `scripts/code_health.py`), also after the file was edited again.

Runs on a throw-away copy of the code base (UNDERSTUDY_CODE_HEALTH_ROOT) - the real files are never touched."""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from tests.wizard_helpers import FakeLLM, admin_client, install_fake
from tests.test_clarify_versions_codehealth import FIXTURE_REL, FIXTURE_SRC, SUPPRESS_FIX, install_fixture
from core.wizard import admin_policy as ap, code_health


class AuditBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="ch-audit-")
        with mock.patch.dict(os.environ, {"UNDERSTUDY_CODE_HEALTH_ROOT": code_health._BASE}):
            code_health._copy_tree(cls.root)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def setUp(self):
        os.environ["UNDERSTUDY_CODE_HEALTH_ROOT"] = self.root
        ap.reset_to_defaults()
        shutil.rmtree(os.path.join(self.root, "generated"), ignore_errors=True)
        install_fixture(self.root)
        self.original = FIXTURE_SRC
        self.path = os.path.join(self.root, *FIXTURE_REL.split("/"))

    def tearDown(self):
        os.environ.pop("UNDERSTUDY_CODE_HEALTH_ROOT", None)
        install_fake(None)

    def propose_and_apply(self, by="alice"):
        st = code_health.scan()
        fid = next(f for f in st["findings"] if f.get("file") == FIXTURE_REL)["id"]
        install_fake(FakeLLM({"CODE_HEALTH_FIX": SUPPRESS_FIX}))
        f = code_health.propose(fid)
        self.assertEqual(f["status"], "proposed", f["proposal"].get("verification"))
        return fid, f, code_health.apply(fid, by=by)


class TestAiChangesAreVisibleAndRevertible(AuditBase):
    def test_proposal_shows_the_exact_lines_before_anything_changes(self):
        st = code_health.scan()
        self.assertEqual(st["changes"]["total"], 0)                     # "the AI has not changed any code"
        fid = next(f for f in st["findings"] if f.get("file") == FIXTURE_REL)["id"]
        install_fake(FakeLLM({"CODE_HEALTH_FIX": SUPPRESS_FIX}))
        p = code_health.propose(fid)["proposal"]
        self.assertIn(f"--- a/{FIXTURE_REL} (before)", p["diff"])
        self.assertIn("+    with _warnings.catch_warnings():", p["diff"])
        ch = p["changes"]
        self.assertTrue(ch and all({"old_lines", "new_lines", "before", "after", "kind"} <= set(c) for c in ch))
        orig_lines = self.original.splitlines()
        for c in ch:                                                    # the reported line numbers are real
            if c["before"]:
                self.assertEqual(orig_lines[c["old_lines"][0] - 1], c["before"][0])
        self.assertEqual(open(self.path, encoding="utf-8").read(), self.original)   # nothing applied yet
        self.assertEqual(code_health.state()["changes"]["total"], 0)

    def test_applied_change_records_who_why_what_and_backup(self):
        fid, f, st = self.propose_and_apply(by="alice")
        self.assertEqual(st["changes"]["total"], 1)
        self.assertEqual(st["changes"]["in_place"], 1)
        self.assertEqual(st["changes"]["files_in_place"], [FIXTURE_REL])
        self.assertTrue(st["changes"]["restart_needed"])
        h = st["history"][0]
        self.assertEqual((h["status"], h["applied_by"]), ("in_place", "admin 'alice'"))
        self.assertEqual(h["warning"]["category"], "FutureWarning")
        self.assertEqual(h["diagnosis"], SUPPRESS_FIX["diagnosis"])
        self.assertEqual(h["explanation"], SUPPRESS_FIX["explanation"])
        self.assertTrue(h["summary"] and h["changes"] and h["diff"])
        self.assertTrue(h["verification"]["warning_gone"])
        # the backup and its self-explaining files are on disk, at a relative path the admin can find
        self.assertTrue(h["backup"].startswith("generated/code_health/backups/" + h["id"] + "/"))
        bdir = os.path.join(self.root, h["backup_dir"])
        self.assertEqual(open(os.path.join(self.root, h["backup"]), encoding="utf-8").read(), self.original)
        for name in ("CHANGE.md", "change.json", "fix.diff"):
            self.assertTrue(os.path.exists(os.path.join(bdir, name)), name)
        md = open(os.path.join(bdir, "CHANGE.md"), encoding="utf-8").read()
        self.assertIn("## Why", md)
        self.assertIn(f"python scripts/code_health.py revert {h['id']}", md)
        self.assertEqual(json.load(open(os.path.join(bdir, "change.json")))["file"], FIXTURE_REL)
        self.assertEqual(code_health.backup_content(h["id"]), self.original)
        # the finding links to its change
        self.assertEqual(code_health.finding(fid)["history_id"], h["id"])

    def test_auto_apply_is_labelled_as_the_ai_acting_alone(self):
        _, _, st = self.propose_and_apply(by="auto-apply")
        self.assertEqual(st["history"][0]["applied_by"], "AI (auto-apply - no admin click)")
        self.assertEqual(st["changes"]["by_ai_alone"], 1)

    def test_revert_and_status_tracking(self):
        _, _, st = self.propose_and_apply()
        hid = st["history"][0]["id"]
        st = code_health.rollback(hid, by="bob")
        self.assertEqual(open(self.path, encoding="utf-8").read(), self.original)
        h = st["history"][0]
        self.assertEqual((h["status"], h["rolled_back_by"]), ("rolled_back", "bob"))
        self.assertEqual(st["changes"]["in_place"], 0)
        with self.assertRaises(ValueError):                               # not twice
            code_health.rollback(hid)

    def test_file_edited_after_the_fix_needs_force_and_nothing_is_lost(self):
        _, _, st = self.propose_and_apply()
        hid = st["history"][0]["id"]
        edited = open(self.path, encoding="utf-8").read() + "\n# a developer's later edit\n"
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(edited)
        self.assertEqual(code_health.history_detail(hid)["status"], "modified_since")
        with self.assertRaises(code_health.RollbackConflict):
            code_health.rollback(hid)
        st = code_health.rollback(hid, force=True)
        self.assertEqual(open(self.path, encoding="utf-8").read(), self.original)
        h = st["history"][0]
        self.assertTrue(h["rolled_back_forced"])
        self.assertEqual(open(os.path.join(self.root, h["saved_before_revert"]), encoding="utf-8").read(), edited)

    def test_cli_lists_shows_and_reverts_without_a_server(self):
        import io
        from contextlib import redirect_stdout
        from scripts import code_health as cli
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cli.main(["list"]), 0)
        self.assertIn("The AI has not changed any code.", buf.getvalue())
        _, _, st = self.propose_and_apply()
        hid = st["history"][0]["id"]
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["list"])
            cli.main(["show", hid])
        out = buf.getvalue()
        self.assertIn("IN PLACE", out)
        self.assertIn(FIXTURE_REL, out)
        self.assertIn("+    with _warnings.catch_warnings():", out)
        self.assertIn("backup: generated/code_health/backups/", out)
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cli.main(["revert", hid]), 0)
        self.assertIn("Reverted", buf.getvalue())
        self.assertEqual(open(self.path, encoding="utf-8").read(), self.original)

    def test_admin_api_history_backup_and_forced_revert(self):
        c = admin_client()
        _, _, st = self.propose_and_apply()
        hid = st["history"][0]["id"]
        r = c.get("/api/admin/code-health/history")
        self.assertEqual(r.json()["summary"]["in_place"], 1)
        d = c.get(f"/api/admin/code-health/history/{hid}").json()
        self.assertEqual(d["status"], "in_place")
        b = c.get(f"/api/admin/code-health/history/{hid}/backup")
        self.assertEqual(b.text, self.original)
        self.assertIn("attachment", b.headers["content-disposition"])
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write("\n# edited\n")
        r = c.post(f"/api/admin/code-health/history/{hid}/rollback")
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.json()["detail"]["needs_force"])
        r = c.post(f"/api/admin/code-health/history/{hid}/rollback?force=true")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(open(self.path, encoding="utf-8").read(), self.original)
        from fastapi.testclient import TestClient
        import server
        self.assertEqual(TestClient(server.app).get(f"/api/admin/code-health/history/{hid}/backup").status_code, 401)


if __name__ == "__main__":
    unittest.main()
