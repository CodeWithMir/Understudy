"""Mission Control login.

* Default credentials (documented in README and printed by every start script):
      username  admin
      password  Understudy@2026
  They can be overridden for a deployment with UNDERSTUDY_ADMIN_USERNAME / UNDERSTUDY_ADMIN_PASSWORD.
* An admin can change the username/password from Mission Control. The change lives in memory only: when the backend
  is stopped and started again (run_demo.*, docker, ...), the credentials fall back to the defaults. Nothing is written
  to disk. The password is kept as a salted PBKDF2-SHA256 hash, never in plain text.
* Login returns a random session token (sent as ``X-Admin-Token``), valid for 12 hours or until logout / password
  change / restart. Five failed logins in a row lock logins for 60 seconds (per username and per client address).
* ``UNDERSTUDY_ADMIN_TOKEN`` (optional) is still accepted as a static API token for scripts and CI.
* Tokens are stateless and signed (HMAC-SHA256 over user, expiry and the credential generation), so any backend
  instance can check them. Changing the password bumps the generation (every other token stops working); logout
  adds the token to a short revocation list.
* On Google Cloud (``UNDERSTUDY_STORE=firestore``, several Cloud Run instances) the hashed credentials, the signing
  secret and the revocation list are shared through Firestore under the current Cloud Run revision (``K_REVISION``)
  - so all instances agree, and a new deployment/restart still falls back to the default password. Set
  ``UNDERSTUDY_SESSION_SECRET`` (e.g. from Secret Manager) to pin the signing secret.
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
from typing import Any, Dict, Optional

DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "Understudy@2026"
SESSION_TTL_S = 12 * 3600
MAX_FAILS, LOCK_S = 5, 60
SHARED_REFRESH_S = 3.0

_LOCK = threading.RLock()
_STATE: Dict[str, Any] = {}
_ISSUED: Dict[str, float] = {}          # token digest -> expiry (this instance; only used to count sessions)


class AuthError(Exception):
    def __init__(self, message: str, status_code: int = 401):
        super().__init__(message)
        self.status_code = status_code


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)


def default_username() -> str:
    return (os.environ.get("UNDERSTUDY_ADMIN_USERNAME") or DEFAULT_USERNAME).strip() or DEFAULT_USERNAME


def default_password() -> str:
    return os.environ.get("UNDERSTUDY_ADMIN_PASSWORD") or DEFAULT_PASSWORD


def _set(username: str, password: str, is_default: bool) -> None:
    salt = secrets.token_bytes(16)
    _STATE.update(username=username, salt=salt, hash=_hash(password, salt), is_default=is_default,
                  changed_at=None if is_default else time.time(), gen=int(_STATE.get("gen", 0)) + 1)


# ---------------------------------------------------------------------------
# Shared state (Firestore mode)
# ---------------------------------------------------------------------------
def _shared():
    try:
        from core.wizard import store
        st = store.get_store()
        return st if getattr(st, "kind", "local") != "local" else None
    except Exception:
        return None


def _doc_id() -> str:
    rev = os.environ.get("K_REVISION") or os.environ.get("UNDERSTUDY_DEPLOYMENT_ID") or "default"
    return "rev-" + re.sub(r"[^A-Za-z0-9_.-]", "-", rev)[:120]


def _to_doc() -> Dict[str, Any]:
    return {"username": _STATE["username"], "salt": _STATE["salt"].hex(), "hash": _STATE["hash"].hex(),
            "is_default": _STATE["is_default"], "changed_at": _STATE.get("changed_at"), "gen": _STATE["gen"],
            "secret": _STATE["secret"].hex(), "carry": _STATE.get("carry", {}), "revoked": {k: v for k, v in _STATE["revoked"].items()
                                                          if v > time.time()}}


def _push() -> None:
    st = _shared()
    if st is not None:
        st.put("admin_auth", _doc_id(), _to_doc())
        _STATE["pulled"] = time.time()


def _pull(force: bool = False) -> None:
    st = _shared()
    if st is None or (not force and time.time() - _STATE.get("pulled", 0) < SHARED_REFRESH_S):
        return
    try:
        d = st.get("admin_auth", _doc_id())
    except Exception:
        return                                   # store unreachable: keep the last known state
    if not d:
        _push()                                  # first instance of this revision publishes the defaults
        return
    _STATE.update(username=d["username"], salt=bytes.fromhex(d["salt"]), hash=bytes.fromhex(d["hash"]),
                  is_default=bool(d.get("is_default")), changed_at=d.get("changed_at"), gen=int(d.get("gen", 1)),
                  revoked=dict(d.get("revoked") or {}), carry=dict(d.get("carry") or {}), pulled=time.time())
    if not os.environ.get("UNDERSTUDY_SESSION_SECRET"):
        _STATE["secret"] = bytes.fromhex(d["secret"])


def reset_to_defaults() -> None:
    """Called at import (= every backend start) and by tests: default credentials, no sessions."""
    with _LOCK:
        _STATE.clear()
        # a password set with UNDERSTUDY_ADMIN_PASSWORD is the deployment's own, not the published default (9.5.2)
        _set(default_username(), default_password(), default_password() == DEFAULT_PASSWORD)
        env_secret = os.environ.get("UNDERSTUDY_SESSION_SECRET")
        _STATE["secret"] = hashlib.sha256(env_secret.encode()).digest() if env_secret else secrets.token_bytes(32)
        _STATE["revoked"] = {}
        _STATE["carry"] = {}
        _STATE["pulled"] = 0.0
        _ISSUED.clear()
        from core.wizard import lockout
        lockout.reset()


reset_to_defaults()


# ---------------------------------------------------------------------------
# Signed tokens
# ---------------------------------------------------------------------------
def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(body: str) -> str:
    return _b64(hmac.new(_STATE["secret"], body.encode(), hashlib.sha256).digest())


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:32]


def _issue(now: float) -> str:
    body = _b64(json.dumps({"u": _STATE["username"], "exp": int(now + SESSION_TTL_S), "g": _STATE["gen"],
                            "n": secrets.token_hex(8)}, separators=(",", ":")).encode())
    token = f"v1.{body}.{_sign(body)}"
    for k in [k for k, v in _ISSUED.items() if v < now]:
        _ISSUED.pop(k, None)
    _ISSUED[_digest(token)] = now + SESSION_TTL_S
    return token


def _verify(token: str) -> Optional[Dict[str, Any]]:
    try:
        v, body, sig = token.split(".")
        if v != "v1" or not hmac.compare_digest(sig, _sign(body)):
            return None
        claims = json.loads(_unb64(body))
    except Exception:
        return None
    carried = _digest(token) in _STATE.get("carry", {})
    if claims.get("exp", 0) < time.time() or (claims.get("g") != _STATE["gen"] and not carried):
        return None
    if _digest(token) in _STATE["revoked"]:
        return None
    return {"user": _STATE["username"] if carried else claims.get("u"), "created": claims["exp"] - SESSION_TTL_S,
            "expires": claims["exp"]}


def _locked(key: str) -> float:
    from core.wizard import lockout                   # 9.7: shared by every instance on Firestore (Cloud Run)
    return lockout.locked("admin", key)


def _fail(key: str) -> None:
    from core.wizard import lockout
    lockout.fail("admin", key, MAX_FAILS, LOCK_S)


def login(username: str, password: str, client: str = "") -> Dict[str, Any]:
    keys = [f"user:{(username or '').lower()}", f"ip:{client}"]
    with _LOCK:
        _pull()
        wait = max(_locked(k) for k in keys)
        if wait:
            raise AuthError(f"Too many failed attempts. Try again in {int(wait) + 1} seconds.", 429)
        ok_user = hmac.compare_digest((username or "").strip().lower(), _STATE["username"].lower())
        ok_pw = hmac.compare_digest(_hash(password or "", _STATE["salt"]), _STATE["hash"])
        if not (ok_user and ok_pw):
            for k in keys:
                _fail(k)
            raise AuthError("Wrong username or password.")
        from core.wizard import lockout
        for k in keys:
            lockout.clear("admin", k)
        now = time.time()
        token = _issue(now)
        return {"token": token, "username": _STATE["username"], "expires_at": now + SESSION_TTL_S,
                "default_password": _STATE["is_default"]}


def session_for(token: Optional[str]) -> Optional[Dict[str, Any]]:
    if not token:
        return None
    static = os.environ.get("UNDERSTUDY_ADMIN_TOKEN", "")
    if static and hmac.compare_digest(token, static):
        return {"user": "api-token", "static": True}
    with _LOCK:
        _pull()
        return _verify(token)


def logout(token: Optional[str]) -> None:
    with _LOCK:
        _pull(force=True)
        claims = _verify(token or "")
        if claims:
            _STATE["revoked"][_digest(token or "")] = float(claims["expires"])
            _ISSUED.pop(_digest(token or ""), None)
            _push()


def _check_strength(password: str) -> None:
    if len(password) < 8:
        raise AuthError("The new password must be at least 8 characters long.", 400)
    if not (re.search(r"[A-Za-z]", password) and re.search(r"\d", password)):
        raise AuthError("The new password must contain letters and numbers.", 400)
    if len(password) > 128:
        raise AuthError("The new password is too long (max 128 characters).", 400)


def change_credentials(token: str, current_password: str, new_password: str,
                       new_username: Optional[str] = None) -> Dict[str, Any]:
    """Changes the admin password (and optionally the username) until the backend restarts. Other sessions end."""
    with _LOCK:
        _pull(force=True)
        if not session_for(token):
            raise AuthError("Please log in again.")
        if not hmac.compare_digest(_hash(current_password or "", _STATE["salt"]), _STATE["hash"]):
            raise AuthError("The current password is wrong.", 400)
        _check_strength(new_password or "")
        user = (new_username or _STATE["username"]).strip()
        if not re.fullmatch(r"[A-Za-z0-9_.@-]{3,40}", user):
            raise AuthError("Username: 3-40 letters, digits or _ . @ -", 400)
        claims = _verify(token) or {}
        _set(user, new_password, False)          # new generation: every other session ends ...
        _ISSUED.clear()
        _STATE["revoked"] = {}
        _STATE["carry"] = {_digest(token): float(claims.get("expires") or time.time() + SESSION_TTL_S)}
        _ISSUED[_digest(token)] = _STATE["carry"][_digest(token)]     # ... except the admin who changed it
        _push()
        return status()


def derived_key(purpose: str) -> bytes:
    """A signing key for another token family (e.g. Studio user tokens), derived from the shared signing secret, so
    every backend instance agrees on it without storing another secret."""
    with _LOCK:
        _pull()
        return hmac.new(_STATE["secret"], ("understudy:" + purpose).encode(), hashlib.sha256).digest()


def status() -> Dict[str, Any]:
    _pull()
    now = time.time()
    return {"username": _STATE["username"], "default_password": _STATE["is_default"],
            "changed_at": _STATE.get("changed_at"), "resets_on_restart": True,
            "shared": _shared() is not None,
            "active_sessions": len([k for k, v in _ISSUED.items() if v > now and k not in _STATE["revoked"]])}
