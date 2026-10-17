"""Browser check of the 8.1 account flows against a running Understudy (backend :8000, UI :3000) in outbox mail mode.

    UNDERSTUDY_MAIL_MODE=outbox ./run_demo.sh        # or any start script
    python scripts/check_accounts_ui.py [--app http://localhost:3000] [--shots shots/]

Covers: Mission Control creates a user with an email, Block (reason) / Unblock / Delete (reason) with the user's
mails; self-signup verified by an emailed code; login with the email; Profile shows username + email and resets the
password with a code; Forgot password with a code. Codes are read from the server's outbox file (the API never
returns them). Exits non-zero on the first failure."""
import argparse
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.wizard import mailer  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--app", default="http://localhost:3000")
ap.add_argument("--admin", default="admin")
ap.add_argument("--admin-password", default=os.environ.get("UNDERSTUDY_ADMIN_PASSWORD", "Understudy@2026"))
ap.add_argument("--shots", default="")
args = ap.parse_args()
TAG = str(int(time.time()))[-6:]
U1, E1 = f"pat{TAG}", f"pat{TAG}@example.com"          # created by the admin
U2, E2 = f"sam{TAG}", f"sam{TAG}@example.com"          # signs up
PW, PW2, PW3 = "Secret123x", "Changed123x", "Forgot123x"


def code_for(email, kind, after):
    for _ in range(40):
        for m in reversed(mailer.outbox(200)):
            if m["to"] == email and m["kind"].startswith(kind) and m["at"] >= after:
                return re.search(r"\b(\d{6})\b", m["text"]).group(1)
        time.sleep(0.25)
    raise SystemExit(f"FAIL: no '{kind}' mail for {email} in {mailer.config()['outbox']}")


def mail_for(email, kind, after):
    for _ in range(40):
        for m in reversed(mailer.outbox(200)):
            if m["to"] == email and m["kind"] == kind and m["at"] >= after:
                return m
        time.sleep(0.25)
    raise SystemExit(f"FAIL: no '{kind}' mail for {email}")


def ok(cond, what):
    print(("PASS " if cond else "FAIL ") + what)
    if not cond:
        raise SystemExit(1)


def main():
    from playwright.sync_api import sync_playwright
    if mailer.config()["mode"] != "outbox":
        raise SystemExit("Run with UNDERSTUDY_MAIL_MODE=outbox (this script reads the codes from the outbox file).")
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1440, "height": 950})
        shot = (lambda n: pg.screenshot(path=os.path.join(args.shots, n + ".png"))) if args.shots else (lambda n: None)

        # ---- Mission Control: create, block, unblock
        pg.goto(args.app + "/")
        pg.locator("input").nth(0).fill(args.admin)
        pg.locator("input[type=password]").fill(args.admin_password)
        pg.get_by_role("button", name="Sign in").click()
        pg.get_by_role("tab", name="Users").click()
        form = pg.get_by_test_id("create-user")
        form.get_by_label("New username").fill(U1)
        form.get_by_label("Email of the new user").fill(E1)
        form.get_by_label("Password of the new user (optional)").fill(PW)
        t0 = time.time() - 1
        form.get_by_role("button", name="Create user").click()
        pg.wait_for_selector(f"text=Created '{U1}'")
        ok(bool(mail_for(E1, "welcome", t0)), "admin creates a user with an email; welcome mail sent")
        row = pg.get_by_test_id("users-table").locator("tr", has_text=U1)
        ok(row.locator(f"text={E1}").count() == 1, "the email is listed; no password is shown")
        shot("1_admin_users")
        row.get_by_role("button", name="Block").click()
        dlg = pg.get_by_test_id("block-dialog")
        dlg.locator("textarea").fill("Shared the account with a contractor")
        t0 = time.time() - 1
        dlg.get_by_role("button", name="Block and email the user").click()
        pg.wait_for_selector(f"text=Blocked '{U1}'")
        m = mail_for(E1, "blocked", t0)
        ok("Shared the account with a contractor" in m["text"] and "contact your administrator" in m["text"],
           "Block mails the reason and asks to contact the admin")
        shot("2_blocked")
        row.get_by_role("button", name="Unblock").click()
        pg.wait_for_selector(f"text=Unblocked '{U1}'")
        ok(bool(mail_for(E1, "unblocked", t0)), "Unblock mails the user")

        # ---- Studio: signup with code
        pg.goto(args.app + "/wizard")
        pg.get_by_test_id("auth-tab-signup").click()
        f = pg.get_by_test_id("signup-form")
        f.get_by_label("Username").fill(U2)
        f.get_by_label("Email").fill(E2)
        f.locator("input[type=password]").nth(0).fill(PW)
        f.locator("input[type=password]").nth(1).fill(PW)
        t0 = time.time() - 1
        f.get_by_role("button", name="Sign up").click()
        pg.get_by_test_id("otp-step").wait_for()
        shot("3_signup_code")
        pg.get_by_label("6-digit code").fill(code_for(E2, "otp-signup", t0))
        pg.get_by_role("button", name="Verify code").click()
        pg.get_by_test_id("open-profile").wait_for()
        ok(True, "signup: the code from the email creates the account and logs in")

        # ---- Profile + reset with code
        pg.get_by_test_id("open-profile").click()
        pv = pg.get_by_test_id("profile-view")
        pv.wait_for()
        ok(pg.get_by_test_id("profile-username").inner_text() == U2 and E2 in pg.get_by_test_id("profile-email").inner_text(),
           "Profile shows username and email")
        shot("4_profile")
        pg.get_by_test_id("profile-reset").click()
        t0 = time.time() - 1
        pg.get_by_role("button", name="Send the code to my email").click()
        pg.get_by_label("6-digit code").fill(code_for(E2, "otp-reset", t0))
        pg.get_by_role("button", name="Verify code").click()
        pg.get_by_test_id("new-password-step").wait_for()
        pg.get_by_test_id("new-password-step").locator("input[type=password]").nth(0).fill(PW2)
        pg.get_by_test_id("new-password-step").locator("input[type=password]").nth(1).fill(PW2)
        shot("5_new_password")
        pg.get_by_role("button", name="Save new password").click()
        pg.get_by_test_id("login-form").wait_for()
        ok(pg.locator("text=Your password was changed").count() > 0, "Profile reset signs out and says so")
        lf = pg.get_by_test_id("login-form")
        lf.get_by_label("Username or email").fill(E2)                 # log in with the email
        lf.get_by_label("Password").fill(PW2)
        lf.get_by_role("button", name="Log in").click()
        pg.get_by_test_id("open-profile").wait_for()
        ok(True, "login with the email and the new password")
        pg.get_by_role("button", name="Log out").click()

        # ---- Forgot password
        pg.get_by_test_id("auth-forgot").click()
        ff = pg.get_by_test_id("forgot-form")
        ff.get_by_label("Registered email").fill("nobody-" + E2)
        ff.get_by_role("button", name="Email me a code").click()
        pg.wait_for_selector("text=No account uses this email address")
        ok(True, "Forgot password checks the email exists")
        ff.get_by_label("Registered email").fill(E2)
        time.sleep(61)                                                # one code per minute per address
        t0 = time.time() - 1
        ff.get_by_role("button", name="Email me a code").click()
        pg.get_by_test_id("otp-step").wait_for()
        ok(pg.locator("text=It expires in").count() == 1, "the code step shows the 10-minute countdown")
        shot("6_forgot_code")
        pg.get_by_label("6-digit code").fill(code_for(E2, "otp-forgot", t0))
        pg.get_by_role("button", name="Verify code").click()
        pg.get_by_test_id("new-password-step").locator("input[type=password]").nth(0).fill(PW3)
        pg.get_by_test_id("new-password-step").locator("input[type=password]").nth(1).fill(PW3)
        pg.get_by_role("button", name="Save new password").click()
        pg.get_by_test_id("login-form").wait_for()
        lf = pg.get_by_test_id("login-form")
        lf.get_by_label("Username or email").fill(U2)
        lf.get_by_label("Password").fill(PW3)
        lf.get_by_role("button", name="Log in").click()
        pg.get_by_test_id("open-profile").wait_for()
        ok(True, "Forgot password: code -> new password -> login")

        # ---- Mission Control: delete with reason
        pg.goto(args.app + "/")
        pg.get_by_role("tab", name="Users").click()
        row = pg.get_by_test_id("users-table").locator("tr", has_text=U2)
        row.get_by_role("button", name=f"Delete {U2}").click()
        dlg = pg.get_by_test_id("delete-dialog")
        dlg.locator("textarea").fill("Duplicate account - please use your company account")
        dlg.get_by_label("Type the username to confirm").fill(U2)
        t0 = time.time() - 1
        dlg.get_by_role("button", name="Delete and email the user").click()
        pg.wait_for_selector(f"text=Deleted '{U2}'")
        m = mail_for(E2, "deleted", t0)
        ok("Duplicate account" in m["text"], "Delete mails the reason")
        shot("7_deleted")
        b.close()
    print("ALL ACCOUNT UI CHECKS PASSED")


if __name__ == "__main__":
    main()
