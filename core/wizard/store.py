"""Durable state for Understudy: local JSON files by default, Cloud Firestore on Google Cloud.

    UNDERSTUDY_STORE=local       (default) generated/store/<collection>/<id>.json on the machine running Understudy
    UNDERSTUDY_STORE=firestore   Cloud Firestore (google-cloud-firestore) - project from GOOGLE_CLOUD_PROJECT,
                                 database from UNDERSTUDY_FIRESTORE_DATABASE (default "(default)"), collections
                                 prefixed with UNDERSTUDY_FIRESTORE_PREFIX (default "understudy_")

What lives here (so several backend instances - e.g. Cloud Run - share it, and a restart loses nothing):
    sessions        every Understudy session snapshot (secret values are never stored)
    admin_policy    Mission Control policy (URL exceptions, tool whitelist + versions, requests, settings)
    skills          learned-skill lessons (the SKILL.md files are also written locally, on the user's machine)
    telemetry       Mission Control activity events (Firestore mode; locally the JSONL file + memory ring buffer)
"""
from __future__ import annotations

import json
import os
import re
import threading
from typing import Any, Dict, Iterable, List, Optional

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MAX_DOC_BYTES = 900_000          # Firestore's document limit is 1 MiB
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.@-]{1,200}$")


def _check_id(doc_id: str) -> str:
    if not _SAFE_ID.match(doc_id or "") or doc_id in (".", ".."):
        raise ValueError(f"invalid document id: {doc_id!r}")
    return doc_id


class LocalStore:
    kind = "local"

    def __init__(self, root: Optional[str] = None):
        self.root = root or os.environ.get("UNDERSTUDY_STORE_DIR") or os.path.join(_BASE, "generated", "store")
        self._lock = threading.RLock()

    def _path(self, collection: str, doc_id: str) -> str:
        return os.path.join(self.root, _check_id(collection), _check_id(doc_id) + ".json")

    def get(self, collection: str, doc_id: str) -> Optional[Dict[str, Any]]:
        try:
            with open(self._path(collection, doc_id), encoding="utf-8") as fh:
                return json.load(fh)
        except (FileNotFoundError, ValueError):
            return None

    def put(self, collection: str, doc_id: str, doc: Dict[str, Any]) -> None:
        p = self._path(collection, doc_id)
        with self._lock:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = p + f".{threading.get_ident()}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, indent=2, default=str)
            os.replace(tmp, p)

    def create(self, collection: str, doc_id: str, doc: Dict[str, Any]) -> bool:
        """Writes the document only if it doesn't exist yet - atomically, also between processes (O_CREAT|O_EXCL).
        True = created, False = it already existed. Used for uniqueness claims (one email = one account)."""
        p = self._path(collection, doc_id)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        try:
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, default=str)
        return True

    def delete(self, collection: str, doc_id: str) -> None:
        try:
            os.remove(self._path(collection, doc_id))
        except FileNotFoundError:
            pass

    def list(self, collection: str) -> List[str]:
        d = os.path.join(self.root, _check_id(collection))
        try:
            return sorted(n[:-5] for n in os.listdir(d) if n.endswith(".json"))
        except FileNotFoundError:
            return []

    # telemetry stays in the local JSONL file + ring buffer (activity_log.py)
    def add_event(self, event: Dict[str, Any]) -> None:
        return None

    def events_after(self, after: int, limit: int) -> Optional[List[Dict[str, Any]]]:
        return None


class FirestoreStore:
    kind = "firestore"

    def __init__(self, client: Any = None):
        if client is None:
            from google.cloud import firestore           # google-cloud-firestore
            client = firestore.Client(project=os.environ.get("GOOGLE_CLOUD_PROJECT") or None,
                                      database=os.environ.get("UNDERSTUDY_FIRESTORE_DATABASE") or "(default)")
        self.db = client
        self.prefix = os.environ.get("UNDERSTUDY_FIRESTORE_PREFIX", "understudy_")

    def _col(self, collection: str):
        return self.db.collection(self.prefix + _check_id(collection))

    def get(self, collection: str, doc_id: str) -> Optional[Dict[str, Any]]:
        snap = self._col(collection).document(_check_id(doc_id)).get()
        if not getattr(snap, "exists", False):
            return None
        data = snap.to_dict() or {}
        # large/nested documents are stored as one JSON string (Firestore limits nesting depth and field names)
        return json.loads(data["json"]) if "json" in data else data

    def put(self, collection: str, doc_id: str, doc: Dict[str, Any]) -> None:
        self._col(collection).document(_check_id(doc_id)).set({"json": json.dumps(doc, default=str)})

    def create(self, collection: str, doc_id: str, doc: Dict[str, Any]) -> bool:
        """Firestore ``DocumentReference.create`` fails when the document exists - atomic across every Cloud Run
        instance. True = created, False = it already existed."""
        try:
            self._col(collection).document(_check_id(doc_id)).create({"json": json.dumps(doc, default=str)})
            return True
        except Exception as e:                                   # google.api_core.exceptions.AlreadyExists (409)
            if type(e).__name__ in ("AlreadyExists", "Conflict") or getattr(e, "code", None) == 409:
                return False
            raise

    def delete(self, collection: str, doc_id: str) -> None:
        self._col(collection).document(_check_id(doc_id)).delete()

    def list(self, collection: str) -> List[str]:
        return sorted(d.id for d in self._col(collection).list_documents())

    def add_event(self, event: Dict[str, Any]) -> None:
        self._col("telemetry").document(f"{event['seq']:020d}").set({"seq": event["seq"],
                                                                     "json": json.dumps(event, default=str)})

    def events_after(self, after: int, limit: int) -> Optional[List[Dict[str, Any]]]:
        col = self._col("telemetry")
        try:
            from google.cloud.firestore_v1.base_query import FieldFilter
            q = col.where(filter=FieldFilter("seq", ">", int(after)))
        except ImportError:                                   # fakes / older clients
            q = col.where("seq", ">", int(after))
        q = q.order_by("seq").limit(int(limit))
        return [json.loads(d.to_dict()["json"]) for d in q.stream()]


_STORE: Dict[str, Any] = {"obj": None, "key": None, "degraded": None, "retry_at": 0.0}
_LOCK = threading.Lock()
RETRY_S = 60.0


def get_store():
    """The configured store. If Firestore was requested but cannot be used (no credentials, library missing, ...),
    Understudy keeps working on local files, says why (describe()["degraded"], banner, Mission Control, the
    Google Cloud check) and tries Firestore again every minute - so fixing the credentials needs no restart."""
    import time as _t
    kind = (os.environ.get("UNDERSTUDY_STORE") or "local").lower()
    key = (kind, os.environ.get("UNDERSTUDY_STORE_DIR"), os.environ.get("GOOGLE_CLOUD_PROJECT"))
    with _LOCK:
        stale = _STORE["obj"] is None or _STORE["key"] != key
        if not stale and _STORE["degraded"] and _t.time() >= _STORE["retry_at"]:
            stale = True                                   # degraded: try Firestore again
        if stale:
            _STORE["key"] = key
            if kind == "firestore":
                try:
                    _STORE["obj"], _STORE["degraded"] = FirestoreStore(), None
                except Exception as e:
                    _STORE["obj"] = LocalStore()
                    _STORE["degraded"] = f"{type(e).__name__}: {str(e)[:200]}"
                    _STORE["retry_at"] = _t.time() + RETRY_S
            else:
                _STORE["obj"], _STORE["degraded"] = LocalStore(), None
        return _STORE["obj"]


def set_store(obj: Any) -> None:
    """Test seam / explicit wiring."""
    with _LOCK:
        _STORE["obj"] = obj
        _STORE["degraded"] = None
        _STORE["key"] = ((os.environ.get("UNDERSTUDY_STORE") or "local").lower(), os.environ.get("UNDERSTUDY_STORE_DIR"),
                         os.environ.get("GOOGLE_CLOUD_PROJECT"))


def degraded() -> Optional[str]:
    """Why Firestore was requested but local files are used instead (None when all is well)."""
    get_store()
    return _STORE["degraded"]


def describe() -> Dict[str, Any]:
    s = get_store()
    return {"kind": s.kind, "location": getattr(s, "root", None) or f"Firestore ({getattr(s, 'prefix', '')}*)",
            "requested": (os.environ.get("UNDERSTUDY_STORE") or "local").lower(), "degraded": _STORE["degraded"]}


def iter_docs(collection: str) -> Iterable[Dict[str, Any]]:
    st = get_store()
    for doc_id in st.list(collection):
        d = st.get(collection, doc_id)
        if d is not None:
            yield d
