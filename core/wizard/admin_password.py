"""The Mission Control password - generated once per installation, the same way everywhere (9.7.1).

Every way of starting Understudy behaves like the Cloud Run deployment:

* First start: a unique password is generated, saved, and shown **once**.
* Every later start: the saved password is used (nothing is generated again).
* Show it again any time:

  ==========================  ======================================================================================
  Where it is saved           How to show it
  ==========================  ======================================================================================
  ``.env`` (local, Docker)    ``python scripts/admin_password.py show``
                              (Docker: ``docker compose exec understudy-backend python scripts/admin_password.py show``)
  Secret Manager (Cloud Run)  ``gcloud secrets versions access latest --secret=understudy-admin-password``
  ==========================  ======================================================================================

* A password changed in Mission Control lasts until the next restart (on Cloud Run: the next revision); then the saved
  one applies again. To change the saved one: ``python scripts/admin_password.py reset`` (Cloud Run: add a secret
  version and redeploy).

The value is the ``UNDERSTUDY_ADMIN_PASSWORD`` setting (environment first, then ``.env``); ``admin_auth`` reads it.
The published fallback ``Understudy@2026`` is only used when nothing could be saved (e.g. a read-only container
started by hand without the setting) - the banner and Mission Control then warn.
"""
from __future__ import annotations

import os
import secrets
from typing import Any, Dict, Optional, Tuple

from core.wizard import env_file

ENV = "UNDERSTUDY_ADMIN_PASSWORD"
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"     # no 0/O, 1/l/I
GENERATED_THIS_START: Optional[str] = None                                  # shown once by the banner


def generate() -> str:
    """Same shape as the Cloud Run deploy scripts: Us-<18 random characters>-9 (passes the password rules)."""
    return "Us-" + "".join(secrets.choice(_ALPHABET) for _ in range(18)) + "-9"


def current(env_path: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
    """(password, where) - where is 'environment' or '.env'; (None, None) when none is set (empty counts as none)."""
    v = (os.environ.get(ENV) or "").strip()
    if v:
        return v, "environment"
    v = (env_file.parse(env_path).get(ENV) or "").strip()
    if v:
        return v, ".env"
    return None, None


def ensure(env_path: Optional[str] = None) -> Dict[str, Any]:
    """Makes sure a password is saved. Returns {password, source, created, path}."""
    global GENERATED_THIS_START
    pw, src = current(env_path)
    p = env_path or env_file.path()
    if pw:
        os.environ.setdefault(ENV, pw)
        return {"password": pw, "source": src, "created": False, "path": p}
    pw = generate()
    env_file.update({ENV: pw}, p)
    os.environ[ENV] = pw
    GENERATED_THIS_START = pw
    return {"password": pw, "source": ".env", "created": True, "path": p}


def reset(env_path: Optional[str] = None) -> Dict[str, Any]:
    """A new saved password (takes effect at the next start)."""
    p = env_path or env_file.path()
    pw = generate()
    env_file.update({ENV: pw}, p)
    return {"password": pw, "source": ".env", "created": True, "path": p}


def startup(env_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Called by server.py before the admin login is set up. Does nothing on Cloud Run (the deploy scripts mount the
    password from Secret Manager; instances can't share a generated one) or when turned off
    (``UNDERSTUDY_ADMIN_PASSWORD_GENERATE=off``, also the test-suite's ``UNDERSTUDY_SKIP_DOTENV=1``)."""
    if os.environ.get("K_SERVICE") or os.environ.get("UNDERSTUDY_SKIP_DOTENV") == "1" or \
            (os.environ.get("UNDERSTUDY_ADMIN_PASSWORD_GENERATE") or "on").lower() in ("off", "0", "false", "no"):
        return None
    try:
        return ensure(env_path)
    except OSError:                                   # read-only file system: fall back to the published default
        return None
