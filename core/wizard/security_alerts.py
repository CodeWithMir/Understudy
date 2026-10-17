"""Model Armor interceptions shown in Mission Control > AI, security & account (``/api/security/alerts``).

Every input a guardrail stage blocks (``guardrails.record_block``) is recorded here: the trigger / pattern, the
channel (SOP, payload, URL, recording transcript ...), the target, the layer that blocked it and the first 400
characters of the text.

9.7: kept in the durable store (collection ``security_alerts`` - Cloud Firestore on Cloud Run), so every instance
shows the same feed and a restart or a new revision doesn't empty it. The newest ``MAX_ALERTS`` are kept. The full
audit trail is still the telemetry log. 9.5.0: replaced the alert part of the v4 in-memory ``core/datastore.py``.
"""
from __future__ import annotations

import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.wizard import store

COLLECTION = "security_alerts"
MAX_ALERTS = 500
_LOCK = threading.Lock()
_writes = 0
_last_ns = 0


def _prune(st) -> None:
    ids = sorted(st.list(COLLECTION))                    # ids start with a zero-padded timestamp -> oldest first
    for doc_id in ids[:-MAX_ALERTS]:
        st.delete(COLLECTION, doc_id)


def record(run_id: str, prompt_injection: Optional[str], channel: str, portal: str,
           portal_url: Optional[str] = None, raw_text: Optional[str] = None,
           model_name: Optional[str] = None) -> Dict[str, Any]:
    global _writes, _last_ns
    with _LOCK:                                          # strictly increasing within an instance, so ids sort in order
        ns = max(time.time_ns(), _last_ns + 1)
        _last_ns = ns
    now = ns / 1e9
    doc_id = f"{ns:020d}-{secrets.token_hex(3)}"
    alert = {
        "alert_id": f"SEC-{doc_id}",
        "run_id": run_id,
        "status": "BLOCKED",
        "prompt_injection": prompt_injection or "Uncategorized adversarial pattern",
        "channel": channel,
        "portal": portal,
        "portal_url": portal_url or "n/a",
        "raw_text": (raw_text or "")[:400],
        "model_name": model_name or "Model Armor",
        "timestamp": datetime.fromtimestamp(now, timezone.utc).replace(tzinfo=None).isoformat(),
    }
    with _LOCK:
        st = store.get_store()
        st.put(COLLECTION, doc_id, alert)
        _writes += 1
        if _writes % 25 == 0:
            _prune(st)
    return alert


def alerts() -> List[Dict[str, Any]]:
    """Newest first (at most MAX_ALERTS)."""
    rows = list(store.iter_docs(COLLECTION))
    rows.sort(key=lambda a: (a.get("timestamp") or "", a.get("alert_id") or ""), reverse=True)
    return rows[:MAX_ALERTS]


def clear() -> None:
    with _LOCK:
        st = store.get_store()
        for doc_id in st.list(COLLECTION):
            st.delete(COLLECTION, doc_id)
