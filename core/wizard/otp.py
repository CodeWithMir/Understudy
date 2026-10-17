"""One-time codes (OTP) for signup email verification, password resets and two-step sign-in (9.6).

* A 6-digit code is mailed; only an HMAC of it is stored (``otp_challenges`` in the durable store - local files or
  Firestore, so any Cloud Run instance can verify it). Valid for ``OTP_TTL_S`` (10 minutes), single use, at most
  ``MAX_ATTEMPTS`` wrong guesses before the challenge is void.
* Sending is rate limited per email address (one code per ``RESEND_COOLDOWN_S`` for the same purpose,
  ``MAX_SENDS_PER_HOUR`` per hour in total) and per client address.
* A verified code yields a short-lived *ticket* (``TICKET_TTL_S``); the password change needs the ticket, so the
  code itself is never sent twice and the steps can't be skipped.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from typing import Any, Dict, List, Optional

from core.wizard import store

OTP_TTL_S = 10 * 60
TICKET_TTL_S = 10 * 60
MAX_ATTEMPTS = 5
RESEND_COOLDOWN_S = 60
MAX_SENDS_PER_HOUR = 8
MAX_IP_SENDS_PER_HOUR = 30
CHALLENGES = "otp_challenges"
RATES = "otp_rates"
PURPOSES = ("signup", "reset", "forgot", "login", "2fa_on")   # 9.6: two-step sign-in + turning it on
_LOCK = threading.RLock()
_IP: Dict[str, List[float]] = {}


class OtpError(Exception):
    def __init__(self, message: str, status_code: int = 400, retry_after: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


def _key() -> bytes:
    from core.wizard import admin_auth
    return admin_auth.derived_key("otp-codes")


def _mac(challenge_id: str, code: str) -> str:
    return hmac.new(_key(), f"{challenge_id}:{code}".encode(), hashlib.sha256).hexdigest()


def _email_key(email: str) -> str:
    return hashlib.sha256(email.lower().encode()).hexdigest()[:32]


def new_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def _sends(doc: Dict[str, Any], now: float) -> List[List[Any]]:
    out = []
    for x in doc.get("sends", []):
        t, p = (x, "") if isinstance(x, (int, float)) else (x[0], x[1])
        if now - t < 3600:
            out.append([t, p])
    return out


def check_rate(email: str, client: str = "", purpose: str = "") -> None:
    """Raises 429 when this address (or client) asked for too many codes."""
    now = time.time()
    with _LOCK:
        if client:
            hits = [t for t in _IP.get(client, []) if now - t < 3600]
            if len(hits) >= MAX_IP_SENDS_PER_HOUR:
                raise OtpError("Too many codes were requested from your network. Try again later.", 429, 600)
        st = store.get_store()
        doc = st.get(RATES, _email_key(email)) or {"sends": []}
        sends = _sends(doc, now)
        same = [t for t, p in sends if p == purpose or not p or not purpose]
        if same and now - same[-1] < RESEND_COOLDOWN_S:
            wait = int(RESEND_COOLDOWN_S - (now - same[-1])) + 1
            raise OtpError(f"A code was just sent. You can ask for a new one in {wait} seconds.", 429, wait)
        if len(sends) >= MAX_SENDS_PER_HOUR:
            raise OtpError("Too many codes were requested for this email address. Try again in an hour.", 429, 3600)


def _note_send(email: str, client: str = "", purpose: str = "") -> None:
    now = time.time()
    with _LOCK:
        if client:
            _IP.setdefault(client, []).append(now)
        st = store.get_store()
        doc = st.get(RATES, _email_key(email)) or {"sends": []}
        doc["sends"] = _sends(doc, now) + [[now, purpose]]
        st.put(RATES, _email_key(email), doc)


def create(purpose: str, email: str, username: str, data: Optional[Dict[str, Any]] = None,
           client: str = "") -> Dict[str, Any]:
    """New challenge -> (challenge, code). The caller mails the code and must never return it over HTTP."""
    if purpose not in PURPOSES:
        raise ValueError(purpose)
    check_rate(email, client, purpose)
    try:
        cleanup()                                           # 9.6: expired challenges are removed as new ones arrive
    except Exception:                                       # pragma: no cover - never block sending a code
        pass
    cid = "otp-" + secrets.token_hex(12)
    code = new_code()
    now = time.time()
    doc = {"id": cid, "purpose": purpose, "email": email, "username": username, "mac": _mac(cid, code),
           "created_at": now, "expires_at": now + OTP_TTL_S, "attempts": 0, "verified": False, "ticket": None,
           "data": data or {}}
    with _LOCK:
        st = store.get_store()
        for old in list(store.iter_docs(CHALLENGES)):          # one open challenge per email + purpose
            if old.get("email") == email and old.get("purpose") == purpose and old.get("id") != cid:
                st.delete(CHALLENGES, old["id"])
        st.put(CHALLENGES, cid, doc)
    _note_send(email, client, purpose)
    return {"challenge": public(doc), "code": code, "_doc": doc}


def resend(challenge_id: str, client: str = "") -> Dict[str, Any]:
    """A fresh code for the same challenge (new expiry, attempts reset)."""
    with _LOCK:
        doc = _get(challenge_id)
        if doc["verified"]:
            raise OtpError("This code was already used.", 409)
        check_rate(doc["email"], client, doc["purpose"])
        code = new_code()
        now = time.time()
        doc.update(mac=_mac(challenge_id, code), expires_at=now + OTP_TTL_S, attempts=0, created_at=now)
        store.get_store().put(CHALLENGES, challenge_id, doc)
    _note_send(doc["email"], client, doc["purpose"])
    return {"challenge": public(doc), "code": code, "_doc": doc}


def _get(challenge_id: str) -> Dict[str, Any]:
    if not str(challenge_id or "").startswith("otp-"):
        raise OtpError("This code request is unknown or has expired. Ask for a new code.", 404)
    try:
        doc = store.get_store().get(CHALLENGES, challenge_id)
    except ValueError:
        doc = None
    if not doc:
        raise OtpError("This code request is unknown or has expired. Ask for a new code.", 404)
    return doc


def public(doc: Dict[str, Any]) -> Dict[str, Any]:
    from core.wizard.mailer import mask
    return {"challenge_id": doc["id"], "purpose": doc["purpose"], "email": mask(doc["email"]),
            "expires_at": doc["expires_at"], "expires_in": max(0, int(doc["expires_at"] - time.time())),
            "attempts_left": max(0, MAX_ATTEMPTS - int(doc.get("attempts", 0))),
            "resend_in": max(0, int(RESEND_COOLDOWN_S - (time.time() - doc["created_at"])))}


def verify(challenge_id: str, code: str, purpose: Optional[str] = None) -> Dict[str, Any]:
    """Checks the code; returns the challenge with a one-time ``ticket``."""
    code = "".join(ch for ch in str(code or "") if ch.isdigit())
    with _LOCK:
        doc = _get(challenge_id)
        st = store.get_store()
        if purpose and doc["purpose"] != purpose:
            raise OtpError("This code was issued for something else.", 400)
        if doc["verified"]:
            raise OtpError("This code was already used. Ask for a new code.", 409)
        if time.time() > doc["expires_at"]:
            st.delete(CHALLENGES, challenge_id)
            raise OtpError("The code has expired (codes are valid for 10 minutes). Ask for a new code.", 410)
        if int(doc.get("attempts", 0)) >= MAX_ATTEMPTS:
            st.delete(CHALLENGES, challenge_id)
            raise OtpError("Too many wrong codes. Ask for a new code.", 429)
        if len(code) != 6 or not hmac.compare_digest(doc["mac"], _mac(challenge_id, code)):
            doc["attempts"] = int(doc.get("attempts", 0)) + 1
            left = MAX_ATTEMPTS - doc["attempts"]
            if left <= 0:
                st.delete(CHALLENGES, challenge_id)
                raise OtpError("Too many wrong codes. Ask for a new code.", 429)
            st.put(CHALLENGES, challenge_id, doc)
            raise OtpError(f"That code is not right. {left} attempt{'s' if left != 1 else ''} left.", 400)
        ticket = "tkt-" + secrets.token_urlsafe(24)
        doc.update(verified=True, ticket=hashlib.sha256(ticket.encode()).hexdigest(),
                   ticket_expires_at=time.time() + TICKET_TTL_S, mac="")
        st.put(CHALLENGES, challenge_id, doc)
    return {"challenge": doc, "ticket": ticket}


def consume(ticket: str, purposes: tuple = PURPOSES) -> Dict[str, Any]:
    """Redeems a ticket once (the challenge is deleted). Returns the challenge document."""
    digest = hashlib.sha256(str(ticket or "").encode()).hexdigest()
    with _LOCK:
        for doc in list(store.iter_docs(CHALLENGES)):
            if doc.get("verified") and doc.get("ticket") and hmac.compare_digest(doc["ticket"], digest):
                store.get_store().delete(CHALLENGES, doc["id"])
                if time.time() > float(doc.get("ticket_expires_at", 0)):
                    raise OtpError("This step has expired. Start again and ask for a new code.", 410)
                if doc["purpose"] not in purposes:
                    raise OtpError("This code was issued for something else.", 400)
                return doc
    raise OtpError("This step has expired or was already used. Start again and ask for a new code.", 410)


def peek_ticket(ticket: str) -> Optional[Dict[str, Any]]:
    digest = hashlib.sha256(str(ticket or "").encode()).hexdigest()
    for doc in store.iter_docs(CHALLENGES):
        if doc.get("verified") and doc.get("ticket") and hmac.compare_digest(doc["ticket"], digest):
            return doc
    return None


def cleanup() -> int:
    """Removes expired challenges (called opportunistically)."""
    now = time.time()
    n = 0
    with _LOCK:
        st = store.get_store()
        for doc in list(store.iter_docs(CHALLENGES)):
            if now > max(float(doc.get("expires_at", 0)), float(doc.get("ticket_expires_at") or 0)) + 60:
                st.delete(CHALLENGES, doc["id"])
                n += 1
    return n


def reset_for_tests() -> None:
    with _LOCK:
        _IP.clear()
        st = store.get_store()
        for col in (CHALLENGES, RATES):
            for doc_id in st.list(col):
                st.delete(col, doc_id)
