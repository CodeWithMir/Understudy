"""Studio user accounts and user login.

* Accounts come from the Mission Control admin (Users: username + email + password, or no password - then the user
  sets one with an emailed code) or from **self-signup** (username + email + password, activated by a 6-digit code
  sent to that email). One email address belongs to at most one account.
* Passwords are never shown to or set by the admin after an account exists: every reset goes through an OTP sent to
  the account's email (Profile > Reset password, or Forgot password on the login screen).
* The admin can **block** an account (reason required, mailed to the user with the admin contact), unblock it, or
  **delete** it (reason required and mailed too). Blocking ends every open session.
* Accounts live in the durable store (``users`` collection: local JSON files, or Firestore on Cloud Run), so unlike
  the admin's own credentials they survive restarts. Passwords are stored only as salted PBKDF2-SHA256 hashes.
* ``POST /api/auth/login`` returns a signed, stateless token (``X-User-Token``), valid for 12 hours. Resetting the
  password or disabling the account bumps the account's generation, which ends every session of that user at once.
  Logout revokes the one token. Five failed logins in a row lock that username / client address for 60 seconds.
* Every Studio endpoint (``/api/wizard/*``, ``/api/projects/*``, ``/api/history/*``) requires a user token
  (``UNDERSTUDY_USER_LOGIN=required``, the default). ``UNDERSTUDY_USER_LOGIN=off`` turns the requirement off for
  single-user local development and for the older automated test suites.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.wizard import store

SESSION_TTL_S = 12 * 3600
MAX_FAILS, LOCK_S = 5, 60
MAX_IP_FAILS = 30                 # per client address (many users can share one proxy address, e.g. on Cloud Run)
COLLECTION = "users"
EMAILS = "user_emails"           # one claim document per email address (atomic create = one email, one account)
CLAIM_GRACE_S = 120              # a claim whose account doesn't exist yet is "being created" for this long
_USER_RX = re.compile(r"^[A-Za-z0-9_.@-]{3,40}$")

_LOCK = threading.RLock()
_REVOKED: Dict[str, float] = {}


class UserError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def login_required() -> bool:
    return (os.environ.get("UNDERSTUDY_USER_LOGIN") or "required").strip().lower() not in ("off", "0", "false", "no")


def _now() -> str:
    return datetime.utcnow().isoformat()


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)


def _doc_id(username: str) -> str:
    return (username or "").strip().lower()


def check_username(username: str) -> str:
    u = (username or "").strip()
    if not _USER_RX.match(u):
        raise UserError("Username: 3-40 letters, digits or _ . @ -")
    if u.lower() in ("admin", "api-token", "system", "root"):
        raise UserError(f"'{u}' is reserved. Choose another username.")
    return u


def check_password(password: str) -> None:
    if len(password or "") < 8:
        raise UserError("The password must be at least 8 characters long.")
    if not (re.search(r"[A-Za-z]", password) and re.search(r"\d", password)):
        raise UserError("The password must contain letters and numbers.")
    if len(password) > 128:
        raise UserError("The password is too long (max 128 characters).")


def _get(username: str) -> Optional[Dict[str, Any]]:
    try:
        return store.get_store().get(COLLECTION, _doc_id(username)) if _doc_id(username) else None
    except ValueError:
        return None


def _put(doc: Dict[str, Any]) -> None:
    store.get_store().put(COLLECTION, _doc_id(doc["username"]), doc)


def public(doc: Dict[str, Any]) -> Dict[str, Any]:
    out = {k: doc.get(k) for k in ("username", "display_name", "email", "email_verified", "active", "created_by",
                                   "created_at", "updated_at", "last_login", "password_set_at", "password_set_by",
                                   "source")}
    out["blocked"] = doc.get("blocked") or None
    out["status"] = "blocked" if doc.get("blocked") else ("active" if doc.get("active", True) else "disabled")
    out["password_set"] = bool(doc.get("hash"))
    out["two_factor"] = bool(doc.get("two_factor"))             # 9.6: emailed code after the password
    return out


def check_email(email: str) -> str:
    from core.wizard.mailer import MailError, normalize_email
    try:
        return normalize_email(email)
    except MailError as e:
        raise UserError(str(e))


def _claim_id(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()[:40]


def _claim_email(email: str, username: str) -> None:
    """Atomically reserves ``email`` for ``username`` (store.create: an exclusive file create locally, Firestore's
    create() on Cloud Run - so two instances can't give one address to two accounts). Raises 409 when taken."""
    e = email.strip().lower()
    st = store.get_store()
    cid = _claim_id(e)
    taken = UserError("An account with this email address already exists. One email can only have one account - "
                      "use Forgot password if it's yours.", 409)
    for _attempt in range(2):
        if st.create(EMAILS, cid, {"email": e, "username": username, "at": time.time()}):
            break
        c = st.get(EMAILS, cid) or {}
        for _wait in range(20):                                   # the claim exists but may still be being written
            if c.get("at"):
                break
            time.sleep(0.05)
            c = st.get(EMAILS, cid) or {}
        holder = str(c.get("username") or "")
        if holder.lower() == username.lower():
            break                                                 # already ours (a retry)
        owner = _get(holder) if holder else None
        if owner and not owner.get("deleted") and (owner.get("email") or "").lower() == e:
            raise taken
        if not owner and time.time() - float(c.get("at") or 0) < CLAIM_GRACE_S:
            raise taken                                          # another instance is creating that account right now
        st.delete(EMAILS, cid)                                   # stale claim (account deleted / never finished)
    else:
        raise taken
    other = _scan_email(e)                                        # accounts from before claims existed
    if other and other["username"].lower() != username.lower():
        _release_email(e, username)
        raise taken


def _release_email(email: str, username: str) -> None:
    if not email:
        return
    st = store.get_store()
    cid = _claim_id(email)
    c = st.get(EMAILS, cid)
    if c and str(c.get("username") or "").lower() == username.lower():
        st.delete(EMAILS, cid)


def backfill_email_claims() -> int:
    """Claims the email of every existing account that has none yet (accounts created before 8.1.1). Idempotent;
    run at start-up. Returns how many claims were written."""
    st = store.get_store()
    n = 0
    for d in list(store.iter_docs(COLLECTION)):
        e = (d.get("email") or "").lower()
        if d.get("username") and not d.get("deleted") and e:
            if st.create(EMAILS, _claim_id(e), {"email": e, "username": d["username"], "at": time.time()}):
                n += 1
    return n


def _create_doc(doc: Dict[str, Any]) -> None:
    """Writes a new account atomically: a name nobody ever had is created with store.create (fails if another
    instance created it first); a deleted name's tombstone is replaced (it keeps the generation counter)."""
    old = _get(doc["username"])
    if old and not old.get("deleted"):
        raise UserError(f"The username '{doc['username']}' is already taken.", 409)
    if old:
        _put(doc)
    elif not store.get_store().create(COLLECTION, _doc_id(doc["username"]), doc):
        raise UserError(f"The username '{doc['username']}' is already taken.", 409)


def _scan_email(e: str) -> Optional[Dict[str, Any]]:
    for d in store.iter_docs(COLLECTION):
        if d.get("username") and not d.get("deleted") and (d.get("email") or "").lower() == e:
            return d
    return None


def find_by_email(email: str) -> Optional[Dict[str, Any]]:
    e = (email or "").strip().lower()
    if not e:
        return None
    c = store.get_store().get(EMAILS, _claim_id(e))               # O(1) through the claim
    if c and c.get("username"):
        d = _get(c["username"])
        if d and not d.get("deleted") and (d.get("email") or "").lower() == e:
            return d
    for d in store.iter_docs(COLLECTION):                        # fallback: an account without a claim yet
        if d.get("username") and not d.get("deleted") and (d.get("email") or "").lower() == e:
            return d
    return None


def _ensure_free(username: str, email: str) -> None:
    old = _get(username)
    if old and not old.get("deleted"):
        raise UserError(f"The username '{username}' is already taken.", 409)
    if find_by_email(email):
        raise UserError("An account with this email address already exists. One email can only have one account - "
                        "use Forgot password if it's yours.", 409)


def _new_doc(username: str, email: str, salt_hex: str, hash_hex: str, created_by: str, source: str,
             display_name: str = "", email_verified: bool = False) -> Dict[str, Any]:
    old = _get(username)
    return {"username": username, "display_name": (display_name or "").strip()[:80] or username, "email": email,
            "email_verified": email_verified, "active": True, "blocked": None, "salt": salt_hex, "hash": hash_hex,
            # a name that was deleted before continues its generation, so tokens of the old account never work again
            "gen": int((old or {}).get("gen", 0)) + 1, "created_by": created_by, "source": source,
            "created_at": _now(), "updated_at": _now(), "last_login": None,
            "password_set_at": _now() if hash_hex else None, "password_set_by": created_by if hash_hex else None}


# ---------------------------------------------------------------------------
# Admin operations
# ---------------------------------------------------------------------------
def list_users() -> List[Dict[str, Any]]:
    return [public(d) for d in store.iter_docs(COLLECTION) if d.get("username") and not d.get("deleted")]


def check_reason(reason: str, what: str = "block") -> str:
    r = re.sub(r"\s+", " ", str(reason or "")).strip()
    if len(r) < 5:
        raise UserError(f"Give the reason for the {what} (at least 5 characters) - it is emailed to the user.")
    if len(r) > 500:
        raise UserError("The reason is too long (max 500 characters).")
    from core.wizard.guardrails import screen_text
    tv = screen_text(r, "ADMIN_INSTRUCTION", use_semantic=False)
    if not tv.is_safe:
        raise UserError(tv.friendly_message)
    return tv.sanitized.strip()


def _mail(fn, *args) -> Dict[str, Any]:
    """Sends a notice; the account change stands even when the mail can't be delivered (the admin is told)."""
    from core.wizard import mailer
    try:
        fn(*args)
        return {"mail_sent": True, "mail_error": None}
    except mailer.MailError as e:
        return {"mail_sent": False, "mail_error": str(e)}


def create_user(username: str, password: Optional[str], created_by: str, display_name: str = "",
                email: str = "") -> Dict[str, Any]:
    """Admin creates an account: username + email (+ password). Without a password the user sets their own with an
    emailed code (Forgot password). The password is never returned or shown again."""
    from core.wizard import mailer
    u = check_username(username)
    e = check_email(email)
    if password:
        check_password(password)
    with _LOCK:
        _ensure_free(u, e)
        salt = secrets.token_bytes(16)
        doc = _new_doc(u, e, salt.hex(), _hash(password, salt).hex() if password else "", created_by, "admin",
                       display_name)
        _claim_email(e, u)
        try:
            _create_doc(doc)
        except UserError:
            _release_email(e, u)
            raise
    mail = _mail(mailer.welcome_mail, e, u, created_by, not password)
    return {"user": public(doc), "password": None, **mail}


def set_email(username: str, email: str, set_by: str) -> Dict[str, Any]:
    """Admin adds the email of an account created before email was required. An email that is already set can't be
    changed by the admin (that would let an admin take the account over through a password reset)."""
    e = check_email(email)
    with _LOCK:
        doc = _get(username)
        if not doc or doc.get("deleted"):
            raise UserError("User not found.", 404)
        if doc.get("email"):
            raise UserError("This account already has an email address; it can't be changed from Mission Control.", 409)
        other = find_by_email(e)
        if other and other["username"] != doc["username"]:
            raise UserError("Another account already uses this email address.", 409)
        _claim_email(e, doc["username"])
        doc.update(email=e, email_verified=False, updated_at=_now())
        _put(doc)
    return {"user": public(doc)}


def _set_password_hash(doc: Dict[str, Any], password: str, set_by: str) -> None:
    salt = secrets.token_bytes(16)
    doc.update(salt=salt.hex(), hash=_hash(password, salt).hex(), gen=int(doc.get("gen", 1)) + 1,
               updated_at=_now(), password_set_at=_now(), password_set_by=set_by)


def block_user(username: str, reason: str, by: str) -> Dict[str, Any]:
    from core.wizard import mailer
    r = check_reason(reason, "block")
    with _LOCK:
        doc = _get(username)
        if not doc or doc.get("deleted"):
            raise UserError("User not found.", 404)
        if doc.get("blocked"):
            raise UserError("This account is already blocked.", 409)
        doc.update(active=False, blocked={"reason": r, "by": by, "at": _now()}, gen=int(doc.get("gen", 1)) + 1,
                   updated_at=_now())                  # ends every open session at once
        _put(doc)
    mail = _mail(mailer.blocked_mail, doc["email"], doc["username"], r) if doc.get("email") else \
        {"mail_sent": False, "mail_error": "This account has no email address."}
    return {"user": public(doc), **mail}


def unblock_user(username: str, by: str) -> Dict[str, Any]:
    from core.wizard import mailer
    with _LOCK:
        doc = _get(username)
        if not doc or doc.get("deleted"):
            raise UserError("User not found.", 404)
        if not doc.get("blocked") and doc.get("active", True):
            raise UserError("This account isn't blocked.", 409)
        doc.update(active=True, blocked=None, gen=int(doc.get("gen", 1)) + 1, updated_at=_now(),
                   unblocked={"by": by, "at": _now()})
        _put(doc)
    mail = _mail(mailer.unblocked_mail, doc["email"], doc["username"]) if doc.get("email") else \
        {"mail_sent": False, "mail_error": "This account has no email address."}
    return {"user": public(doc), **mail}


def request_reset(username: str, by: str) -> Dict[str, Any]:
    """Admin asks the user to reset their password: a notice mail only - the admin never sees or sets a password."""
    from core.wizard import mailer
    doc = _get(username)
    if not doc or doc.get("deleted"):
        raise UserError("User not found.", 404)
    if not doc.get("email"):
        raise UserError("This account has no email address - add one first.", 409)
    try:
        mailer.reset_requested_mail(doc["email"], doc["username"], by)
    except mailer.MailError as e:
        raise UserError(f"Couldn't send the mail: {e}", 502)
    return {"user": public(doc), "mail_sent": True}


def delete_user(username: str, reason: str = "", by: str = "admin", notify: bool = True) -> Dict[str, Any]:
    """Deletes the account (reason required, mailed to the user). A tombstone keeps the generation counter (old tokens
    can never come back, even if the name is created again), and the user's projects and sessions are kept for audit
    under an archive owner name - so a new account with the same name does not inherit them. The email is released."""
    from core.wizard import mailer
    r = check_reason(reason, "deletion") if notify else (reason or "")
    with _LOCK:
        doc = _get(username)
        if not doc or doc.get("deleted"):
            raise UserError("User not found.", 404)
        archived = f"{doc['username']}~deleted-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
        _put({"username": doc["username"], "deleted": True, "gen": int(doc.get("gen", 1)) + 1, "deleted_at": _now(),
              "archived_as": archived, "active": False, "deleted_by": by, "deleted_reason": r})
        _release_email(doc.get("email") or "", doc["username"])      # the address can be used again
    from core.wizard import projects
    moved = projects.reassign_owner(doc["username"], archived)
    mail = {"mail_sent": False, "mail_error": None}
    if notify:
        mail = _mail(mailer.deleted_mail, doc["email"], doc["username"], r) if doc.get("email") else \
            {"mail_sent": False, "mail_error": "This account had no email address."}
    return {"deleted": doc["username"], "archived_as": archived, **moved, **mail}


# ---------------------------------------------------------------------------
# Self-service: signup, profile, OTP password reset
# ---------------------------------------------------------------------------
def signup_env_default() -> bool:
    return (os.environ.get("UNDERSTUDY_SIGNUP") or "on").strip().lower() not in ("off", "0", "false", "no")


def signup_enabled() -> bool:
    """Mission Control's switch (Users > Allow self-signup) wins; otherwise UNDERSTUDY_SIGNUP (default on)."""
    try:
        from core.wizard import admin_policy
        s = admin_policy.signup_setting()
    except Exception:
        s = None
    return bool(s["enabled"]) if s else signup_env_default()


def signup_status() -> Dict[str, Any]:
    from core.wizard import admin_policy
    s = admin_policy.signup_setting()
    d = admin_policy.signup_domains_setting()
    return {"enabled": signup_enabled(), "source": "admin" if s else "env", "env_default": signup_env_default(),
            "set_by": (s or {}).get("by"), "set_at": (s or {}).get("at"),
            # 8.2: only these email domains may self-sign-up ([] = any)
            "domains": admin_policy.signup_domains(), "domains_source": "admin" if d else "env",
            "domains_set_by": (d or {}).get("by"), "domains_set_at": (d or {}).get("at")}


def check_signup_domain(email: str) -> None:
    """8.2: self-signup is limited to the admin's email domains (Mission Control > Users)."""
    from core.wizard import admin_policy
    domains = admin_policy.signup_domains()
    if domains and not admin_policy.email_domain_allowed(email, domains):
        shown = ", ".join("@" + d if not d.startswith("*.") else f"@<any>.{d[2:]}" for d in domains)
        raise UserError(f"Sign-up is limited to email addresses ending in {shown}. Use your company email, or ask "
                        f"your admin for an account.", 403)


def _send_code(ch: Dict[str, Any]) -> Dict[str, Any]:
    from core.wizard import mailer, otp
    doc = ch["_doc"]
    try:
        mailer.otp_mail(doc["email"], ch["code"], doc["purpose"], doc["username"], otp.OTP_TTL_S // 60)
    except mailer.MailError as e:
        store.get_store().delete(otp.CHALLENGES, doc["id"])
        raise UserError(f"We couldn't send the code: {e}", 502)
    return ch["challenge"]


def start_signup(username: str, email: str, password: str, confirm: str, client: str = "") -> Dict[str, Any]:
    from core.wizard import otp
    if not signup_enabled():
        raise UserError("Self-signup is switched off. Ask your admin for an account.", 403)
    u = check_username(username)
    e = check_email(email)
    check_signup_domain(e)
    if password != confirm:
        raise UserError("The passwords don't match.")
    check_password(password)
    with _LOCK:
        _ensure_free(u, e)
    salt = secrets.token_bytes(16)                      # only the hash waits in the challenge, never the password
    try:
        ch = otp.create("signup", e, u, {"salt": salt.hex(), "hash": _hash(password, salt).hex()}, client)
    except otp.OtpError as ex:
        raise UserError(str(ex), ex.status_code)
    return _send_code(ch)


def finish_signup(challenge_id: str, code: str) -> Dict[str, Any]:
    """Verifies the code and creates the account (email verified); returns a login token."""
    from core.wizard import otp
    try:
        res = otp.verify(challenge_id, code, "signup")
        doc_ch = otp.consume(res["ticket"], ("signup",))
    except otp.OtpError as ex:
        raise UserError(str(ex), ex.status_code)
    if not signup_enabled():
        raise UserError("Self-signup was switched off. Ask your admin for an account.", 403)
    check_signup_domain(doc_ch["email"])                          # the admin may have narrowed the domains meanwhile
    with _LOCK:
        _ensure_free(doc_ch["username"], doc_ch["email"])       # someone else may have taken it meanwhile
        doc = _new_doc(doc_ch["username"], doc_ch["email"], doc_ch["data"]["salt"], doc_ch["data"]["hash"],
                       doc_ch["username"], "signup", email_verified=True)
        _claim_email(doc_ch["email"], doc_ch["username"])       # atomic across instances
        try:
            _create_doc(doc)
        except UserError:
            _release_email(doc_ch["email"], doc_ch["username"])
            raise
    return _issue(doc)


def start_reset(email: Optional[str] = None, username: Optional[str] = None, client: str = "") -> Dict[str, Any]:
    """Forgot password (by email) or Profile > Reset password (by the logged-in username): mails a 10-minute code."""
    from core.wizard import otp
    if username:
        doc = _get(username)
        purpose = "reset"
    else:
        doc = find_by_email(check_email(email or ""))
        purpose = "forgot"
        if not doc:
            raise UserError("No account uses this email address. Check it, or sign up.", 404)
    if not doc or doc.get("deleted"):
        raise UserError("User not found.", 404)
    if doc.get("blocked"):
        raise UserError("This account is blocked. Contact your admin to have it unblocked.", 403)
    if not doc.get("email"):
        raise UserError("Your account has no email address yet. Ask your admin to add it.", 409)
    try:
        ch = otp.create(purpose, doc["email"], doc["username"], {}, client)
    except otp.OtpError as ex:
        raise UserError(str(ex), ex.status_code)
    return _send_code(ch)


def resend_code(challenge_id: str, client: str = "") -> Dict[str, Any]:
    from core.wizard import otp
    try:
        ch = otp.resend(challenge_id, client)
    except otp.OtpError as ex:
        raise UserError(str(ex), ex.status_code)
    return _send_code(ch)


def verify_code(challenge_id: str, code: str, username: Optional[str] = None) -> Dict[str, Any]:
    """Step 2 of a reset: the code is right -> a one-time ticket for step 3 (new password)."""
    from core.wizard import otp
    try:
        doc = otp._get(challenge_id)
        if doc["purpose"] == "signup":
            raise UserError("Use the signup screen to finish creating your account.")
        if doc["purpose"] in ("login", "2fa_on"):                 # 9.6: these codes have their own endpoints
            raise UserError("This code is for signing in or for two-step sign-in, not for a password reset.")
        if username is not None and doc["username"].lower() != username.lower():
            raise UserError("This code belongs to another account.", 403)
        res = otp.verify(challenge_id, code)
    except otp.OtpError as ex:
        raise UserError(str(ex), ex.status_code)
    return {"ticket": res["ticket"], "expires_in": otp.TICKET_TTL_S, "username": res["challenge"]["username"]}


def reset_with_ticket(ticket: str, new_password: str, confirm: str, username: Optional[str] = None) -> Dict[str, Any]:
    from core.wizard import mailer, otp
    if new_password != confirm:
        raise UserError("The passwords don't match.")
    check_password(new_password)
    peek = otp.peek_ticket(ticket)
    if peek and username is not None and peek["username"].lower() != username.lower():
        raise UserError("This code belongs to another account.", 403)
    try:
        ch = otp.consume(ticket, ("reset", "forgot"))
    except otp.OtpError as ex:
        raise UserError(str(ex), ex.status_code)
    with _LOCK:
        doc = _get(ch["username"])
        if not doc or doc.get("deleted"):
            raise UserError("User not found.", 404)
        if doc.get("blocked"):
            raise UserError("This account is blocked. Contact your admin to have it unblocked.", 403)
        if (doc.get("email") or "").lower() != ch["email"].lower():
            raise UserError("The account's email changed meanwhile. Start again.", 409)
        if doc.get("hash") and hmac.compare_digest(_hash(new_password, bytes.fromhex(doc["salt"])).hex(), doc["hash"]):
            raise UserError("Choose a password you haven't just used.")
        _set_password_hash(doc, new_password, f"{doc['username']} (email code)")
        doc["email_verified"] = True                    # the code proved the address
        _put(doc)
    mail = _mail(mailer.password_changed_mail, doc["email"], doc["username"])
    return {"user": public(doc), **mail}


def profile(username: str) -> Dict[str, Any]:
    doc = _get(username)
    if not doc or doc.get("deleted"):
        raise UserError("User not found.", 404)
    from core.wizard.mailer import mask
    p = public(doc)
    return {"username": p["username"], "display_name": p["display_name"], "email": p.get("email"),
            "email_masked": mask(p["email"]) if p.get("email") else None, "email_verified": bool(p.get("email_verified")),
            "created_at": p["created_at"], "created_by": p.get("created_by"), "source": p.get("source"),
            "last_login": p["last_login"], "password_set_at": p["password_set_at"],
            "two_factor": bool(doc.get("two_factor")), "two_factor_since": doc.get("two_factor_since")}


# ---------------------------------------------------------------------------
# Login and tokens
# ---------------------------------------------------------------------------
def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _key() -> bytes:
    from core.wizard import admin_auth
    return admin_auth.derived_key("studio-user-tokens")


def _sign(body: str) -> str:
    return _b64(hmac.new(_key(), body.encode(), hashlib.sha256).digest())


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:32]


def _locked(key: str) -> float:
    from core.wizard import lockout                   # 9.7: shared by every instance on Firestore (Cloud Run)
    return lockout.locked("studio", key)


def _fail(key: str) -> None:
    from core.wizard import lockout
    lockout.fail("studio", key, MAX_IP_FAILS if key.startswith("ip:") else MAX_FAILS, LOCK_S)


def _unfail(key: str) -> None:
    from core.wizard import lockout
    lockout.clear("studio", key)


def login(username: str, password: str, client: str = "") -> Dict[str, Any]:
    keys = [f"user:{_doc_id(username)}", f"ip:{client}"]
    with _LOCK:
        wait = max(_locked(k) for k in keys)
        if wait:
            raise UserError(f"Too many failed attempts. Try again in {int(wait) + 1} seconds.", 429)
        doc = _get(username) if _USER_RX.match((username or "").strip()) else None
        if (not doc or doc.get("deleted")) and "@" in (username or ""):
            doc = find_by_email(username)                          # log in with the email address too
        if doc and doc.get("deleted"):
            doc = None
        salt = bytes.fromhex(doc["salt"]) if doc and doc.get("salt") else secrets.token_bytes(16)
        ok = bool(doc) and bool(doc.get("hash")) and hmac.compare_digest(_hash(password or "", salt).hex(),
                                                                         doc["hash"])   # constant work
        if not ok:
            for k in keys:
                _fail(k)
            raise UserError("Wrong username or password.", 401)
        if doc.get("blocked"):
            raise UserError(f"This account is blocked: {doc['blocked'].get('reason', '')} Contact your admin to have it "
                            "unblocked.", 403)
        if not doc.get("active", True):
            raise UserError("This account is disabled. Ask your Mission Control admin to enable it.", 403)
        if doc.get("deleted"):
            raise UserError("Wrong username or password.", 401)
        for k in keys:
            _unfail(k)
        if doc.get("two_factor"):                           # 9.6: the password is right - now the emailed code
            return _start_login_code(doc, client)
        doc["last_login"] = _now()
        _put(doc)
    return _issue(doc)


# ---------------------------------------------------------------------------
# Two-step sign-in (9.6): a 6-digit code mailed after the right password
# ---------------------------------------------------------------------------
def _start_login_code(doc: Dict[str, Any], client: str = "") -> Dict[str, Any]:
    from core.wizard import otp
    if not doc.get("email"):
        raise UserError("Two-step sign-in is on, but your account has no email address. Ask your admin to add one or "
                        "to turn two-step sign-in off.", 409)
    try:
        ch = otp.create("login", doc["email"], doc["username"], {"gen": int(doc.get("gen", 1))}, client)
    except otp.OtpError as ex:
        raise UserError(str(ex), ex.status_code)
    challenge = _send_code(ch)
    return {"two_factor_required": True, "username": doc["username"], "challenge": challenge,
            "message": f"Your password is right. We sent a 6-digit sign-in code to {challenge['email']}."}


def finish_login(challenge_id: str, code: str, client: str = "") -> Dict[str, Any]:
    """Step 2 of a two-step sign-in: the emailed code -> a session token."""
    from core.wizard import otp
    try:
        ch = otp._get(challenge_id)
    except otp.OtpError as ex:
        raise UserError(str(ex), ex.status_code)
    if ch.get("purpose") != "login":
        raise UserError("This code is not a sign-in code.", 400)
    key = f"user:{_doc_id(ch['username'])}"
    if _locked(key):
        raise UserError(f"Too many failed attempts. Try again in {int(_locked(key)) + 1} seconds.", 429)
    try:
        res = otp.verify(challenge_id, code, "login")
    except otp.OtpError as ex:
        if ex.status_code in (400, 429):
            _fail(key)                                       # wrong codes count towards the account lock-out too
        raise UserError(str(ex), ex.status_code)
    try:
        done = otp.consume(res["ticket"], ("login",))
    except otp.OtpError as ex:
        raise UserError(str(ex), ex.status_code)
    with _LOCK:
        doc = _get(done["username"])
        if not doc or doc.get("deleted"):
            raise UserError("Wrong username or password.", 401)
        if doc.get("blocked"):
            raise UserError(f"This account is blocked: {doc['blocked'].get('reason', '')} Contact your admin to have it "
                            "unblocked.", 403)
        if not doc.get("active", True):
            raise UserError("This account is disabled. Ask your Mission Control admin to enable it.", 403)
        if int(doc.get("gen", 1)) != int((done.get("data") or {}).get("gen", 1)) or \
                (doc.get("email") or "").lower() != done["email"].lower():
            raise UserError("Your account changed while you were signing in. Log in again.", 409)
        _unfail(key)
        doc["last_login"] = _now()
        _put(doc)
    return _issue(doc)


def start_two_factor(username: str, client: str = "") -> Dict[str, Any]:
    """Profile > Two-step sign-in > Turn on: mails a code first, so the user proves the mailbox works."""
    from core.wizard import otp
    doc = _get(username)
    if not doc or doc.get("deleted"):
        raise UserError("User not found.", 404)
    if doc.get("two_factor"):
        raise UserError("Two-step sign-in is already on.", 409)
    if not doc.get("email"):
        raise UserError("Your account has no email address yet. Ask your admin to add it.", 409)
    try:
        ch = otp.create("2fa_on", doc["email"], doc["username"], {}, client)
    except otp.OtpError as ex:
        raise UserError(str(ex), ex.status_code)
    return _send_code(ch)


def confirm_two_factor(username: str, challenge_id: str, code: str) -> Dict[str, Any]:
    from core.wizard import mailer, otp
    try:
        ch = otp._get(challenge_id)
        if ch.get("purpose") != "2fa_on" or ch["username"].lower() != username.lower():
            raise UserError("This code was issued for something else.", 400)
        res = otp.verify(challenge_id, code, "2fa_on")
        done = otp.consume(res["ticket"], ("2fa_on",))
    except otp.OtpError as ex:
        raise UserError(str(ex), ex.status_code)
    with _LOCK:
        doc = _get(username)
        if not doc or doc.get("deleted"):
            raise UserError("User not found.", 404)
        if (doc.get("email") or "").lower() != done["email"].lower():
            raise UserError("Your email address changed meanwhile. Start again.", 409)
        doc.update(two_factor=True, two_factor_since=_now(), email_verified=True, updated_at=_now())
        _put(doc)
    _mail(mailer.two_factor_mail, doc["email"], doc["username"], True)
    return profile(username)


def disable_two_factor(username: str, password: Optional[str] = None, by: str = "") -> Dict[str, Any]:
    """The user turns it off with their password; an admin (``by``) can turn it off for a locked-out user."""
    from core.wizard import mailer
    with _LOCK:
        doc = _get(username)
        if not doc or doc.get("deleted"):
            raise UserError("User not found.", 404)
        if not doc.get("two_factor"):
            raise UserError("Two-step sign-in is not on.", 409)
        if not by:
            ok = bool(doc.get("hash")) and hmac.compare_digest(
                _hash(password or "", bytes.fromhex(doc["salt"])).hex(), doc["hash"])
            if not ok:
                raise UserError("That password is not right.", 403)
        doc.update(two_factor=False, two_factor_since=None, updated_at=_now())
        _put(doc)
    if doc.get("email"):
        _mail(mailer.two_factor_mail, doc["email"], doc["username"], False, by)
    return public(doc)


def _issue(doc: Dict[str, Any]) -> Dict[str, Any]:
    now = time.time()
    body = _b64(json.dumps({"u": doc["username"], "exp": int(now + SESSION_TTL_S), "g": int(doc.get("gen", 1)),
                            "n": secrets.token_hex(8)}, separators=(",", ":")).encode())
    token = f"u1.{body}.{_sign(body)}"
    return {"token": token, "username": doc["username"], "display_name": doc.get("display_name") or doc["username"],
            "expires_at": now + SESSION_TTL_S}


def verify(token: Optional[str]) -> Optional[Dict[str, Any]]:
    """{username, display_name, expires} for a valid token of an existing, active account; otherwise None."""
    if not token or not token.startswith("u1."):
        return None
    try:
        _v, body, sig = token.split(".")
        if not hmac.compare_digest(sig, _sign(body)):
            return None
        claims = json.loads(_unb64(body))
    except Exception:
        return None
    if claims.get("exp", 0) < time.time() or _revoked(_digest(token)):
        return None
    doc = _get(str(claims.get("u") or ""))
    if not doc or doc.get("deleted") or not doc.get("active", True) or int(doc.get("gen", 1)) != int(claims.get("g", -1)):
        return None
    return {"username": doc["username"], "display_name": doc.get("display_name") or doc["username"],
            "expires": claims["exp"]}


def _revoked(digest: str) -> bool:
    """Revocations live in the durable store (another Cloud Run instance, or a restart with a pinned signing secret,
    must still refuse a token that was logged out) with an in-memory cache."""
    if digest in _REVOKED:
        return True
    try:
        d = store.get_store().get("user_revocations", digest)
    except Exception:
        return False
    if d and float(d.get("exp", 0)) > time.time():
        _REVOKED[digest] = float(d["exp"])
        return True
    return False


def logout(token: Optional[str]) -> None:
    claims = verify(token)
    if claims:
        digest = _digest(token or "")
        with _LOCK:
            now = time.time()
            for k in [k for k, v in _REVOKED.items() if v < now]:
                _REVOKED.pop(k, None)
            _REVOKED[digest] = float(claims["expires"])
        try:
            store.get_store().put("user_revocations", digest, {"exp": float(claims["expires"])})
        except Exception:
            pass


def reset_for_tests() -> None:
    from core.wizard import lockout
    with _LOCK:
        lockout.reset()
        _REVOKED.clear()
        st = store.get_store()
        for col in (COLLECTION, "user_revocations", EMAILS):
            for doc_id in st.list(col):
                st.delete(col, doc_id)
