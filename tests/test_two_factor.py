"""9.6: two-step sign-in - the user turns it on in Profile (a code proves the mailbox), after the right password the
login answers with a mailed 6-digit code instead of a token, wrong codes are limited, the user turns it off with the
password, and the admin can turn it off for a locked-out user."""
from fastapi.testclient import TestClient

from core.wizard import users
from tests.test_email_accounts import PW, Base


class TwoFactorTest(Base):
    def enable(self, name="alice"):
        c = self.login(name)
        r = c.post("/api/auth/profile/two-factor/start")
        self.assertEqual(r.status_code, 200, r.text)
        ch = r.json()["challenge"]
        code = self.mail.code(f"{name}@example.com", "otp-2fa_on")
        self.assertNotIn(code, r.text)                                    # the code is only in the mail
        r = c.post("/api/auth/profile/two-factor/verify", json={"challenge_id": ch["challenge_id"], "code": code})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["profile"]["two_factor"])
        return c

    def test_off_by_default_and_plain_login_still_works(self):
        self.create("alice")
        c = self.login("alice")
        self.assertFalse(c.get("/api/auth/profile").json()["profile"]["two_factor"])

    def test_turn_on_then_login_needs_the_mailed_code(self):
        self.create("alice")
        self.enable()
        self.assertEqual(self.mail.last("alice@example.com", "2fa-on")["kind"], "2fa-on")   # notice mail
        r = self.anon.post("/api/auth/login", json={"username": "alice", "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["two_factor_required"])
        self.assertNotIn("token", body)                                   # no session before the code
        code = self.mail.code("alice@example.com", "otp-login")
        self.assertNotIn(code, r.text)                                    # the code never goes over HTTP
        r = self.anon.post("/api/auth/login/verify", json={"challenge_id": body["challenge"]["challenge_id"], "code": code})
        self.assertEqual(r.status_code, 200, r.text)
        tok = r.json()["token"]
        c = TestClient(self.app)
        c.headers.update({"X-User-Token": tok})
        self.assertEqual(c.get("/api/auth/me").json()["username"], "alice")
        # single use
        r = self.anon.post("/api/auth/login/verify", json={"challenge_id": body["challenge"]["challenge_id"], "code": code})
        self.assertNotEqual(r.status_code, 200)

    def test_wrong_password_sends_no_code(self):
        self.create("alice")
        self.enable()
        n = len(self.mail.msgs)
        self.login("alice", password="Wrong12345", status=401)
        self.assertEqual(len(self.mail.msgs), n)

    def test_wrong_codes_are_limited(self):
        self.create("alice")
        self.enable()
        ch = self.anon.post("/api/auth/login", json={"username": "alice", "password": PW}).json()["challenge"]
        last = None
        for _ in range(5):
            last = self.anon.post("/api/auth/login/verify", json={"challenge_id": ch["challenge_id"], "code": "000000"})
            self.assertNotEqual(last.status_code, 200)
        self.assertIn(last.status_code, (429, 404))
        good = self.mail.code("alice@example.com", "otp-login")
        r = self.anon.post("/api/auth/login/verify", json={"challenge_id": ch["challenge_id"], "code": good})
        self.assertNotEqual(r.status_code, 200)                           # the challenge is void

    def test_login_code_cannot_reset_a_password(self):
        self.create("alice")
        self.enable()
        ch = self.anon.post("/api/auth/login", json={"username": "alice", "password": PW}).json()["challenge"]
        code = self.mail.code("alice@example.com", "otp-login")
        r = self.anon.post("/api/auth/code/verify", json={"challenge_id": ch["challenge_id"], "code": code})
        self.assertEqual(r.status_code, 400, r.text)

    def test_turn_off_needs_the_password(self):
        self.create("alice")
        c = self.enable()
        r = c.post("/api/auth/profile/two-factor/disable", json={"password": "Wrong12345"})
        self.assertEqual(r.status_code, 403, r.text)
        r = c.post("/api/auth/profile/two-factor/disable", json={"password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["profile"]["two_factor"])
        self.assertIn("token", self.anon.post("/api/auth/login", json={"username": "alice", "password": PW}).json())

    def test_admin_turns_it_off_for_a_locked_out_user(self):
        self.create("alice")
        self.enable()
        lst = self.admin.get("/api/admin/users").json()
        row = next(u for u in (lst.get("users") or lst) if u["username"] == "alice")
        self.assertTrue(row["two_factor"])
        r = self.admin.post("/api/admin/users/alice/two-factor/disable")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(users.profile("alice")["two_factor"])
        self.assertIn("token", self.anon.post("/api/auth/login", json={"username": "alice", "password": PW}).json())
        self.assertEqual(self.mail.last("alice@example.com", "2fa-off")["kind"], "2fa-off")

    def test_blocked_meanwhile_is_refused_at_the_code_step(self):
        self.create("alice")
        self.enable()
        ch = self.anon.post("/api/auth/login", json={"username": "alice", "password": PW}).json()["challenge"]
        code = self.mail.code("alice@example.com", "otp-login")
        self.admin.post("/api/admin/users/alice/block", json={"reason": "Left the company last week"})
        r = self.anon.post("/api/auth/login/verify", json={"challenge_id": ch["challenge_id"], "code": code})
        self.assertEqual(r.status_code, 403, r.text)
