"""Failed-login counters (Studio users and the Mission Control admin), shared by every instance (9.7).

N failures in a row for a key (``user:<name>`` or ``ip:<address>``) lock that key for ``lock_s`` seconds.

* One instance (local store, a laptop or a VM): counters live in memory, as before.
* Several instances (``UNDERSTUDY_STORE=firestore``, e.g. Cloud Run scaling out): counters live in the durable store
  (collection ``login_lockouts``, document id = a hash of the key, so no username or address is readable there).
  That way an attacker can't get around the lock-out by being sent to another Cloud Run instance, and a restart
  doesn't reset it. Updates are read-modify-write without a transaction: two simultaneous failures on two instances
  may count as one, so the lock-out can be at most a few attempts late, never skipped.
"""
from __future__ import annotations

import hashlib
import threading
import time
from typing import Any, Dict, Optional

from core.wizard import store

COLLECTION = "login_lockouts"
_MEM: Dict[str, Dict[str, float]] = {}
_LOCK = threading.Lock()


def _doc_id(scope: str, key: str) -> str:
    return hashlib.sha256(f"{scope}|{key}".encode()).hexdigest()[:40]


def _shared():
    try:
        st = store.get_store()
    except Exception:                                    # pragma: no cover - never break a login over the store
        return None
    return st if getattr(st, "kind", "local") != "local" else None


def _read(scope: str, key: str) -> Optional[Dict[str, Any]]:
    st = _shared()
    if st is None:
        return _MEM.get(_doc_id(scope, key))
    try:
        return st.get(COLLECTION, _doc_id(scope, key))
    except Exception:                                    # pragma: no cover
        return _MEM.get(_doc_id(scope, key))


def _write(scope: str, key: str, doc: Optional[Dict[str, Any]]) -> None:
    did = _doc_id(scope, key)
    st = _shared()
    if st is None:
        if doc is None:
            _MEM.pop(did, None)
        else:
            _MEM[did] = doc
        return
    try:
        if doc is None:
            st.delete(COLLECTION, did)
        else:
            st.put(COLLECTION, did, doc)
    except Exception:                                    # pragma: no cover - fall back to this instance
        if doc is None:
            _MEM.pop(did, None)
        else:
            _MEM[did] = doc


def locked(scope: str, key: str) -> float:
    """Seconds this key is still locked (0 when it isn't)."""
    with _LOCK:
        f = _read(scope, key)
    until = float((f or {}).get("until", 0))
    return until - time.time() if until > time.time() else 0.0


def fail(scope: str, key: str, max_failures: int, lock_s: float) -> None:
    with _LOCK:
        f = dict(_read(scope, key) or {"n": 0, "until": 0})
        if float(f.get("until", 0)) > time.time():
            return                                       # already locked
        f["n"] = int(f.get("n", 0)) + 1
        if f["n"] >= max_failures:
            f.update(n=0, until=time.time() + lock_s)
        f["updated"] = time.time()
        _write(scope, key, f)


def clear(scope: str, key: str) -> None:
    with _LOCK:
        if _read(scope, key) is not None:
            _write(scope, key, None)


def reset(scope: Optional[str] = None) -> None:
    """Tests / an explicit admin reset: forgets this instance's counters (shared ones expire on their own)."""
    with _LOCK:
        _MEM.clear()
