"""User notifications (8.3): the bell in the Studio.

Things that happen while the user is somewhere else land here - durably (store collection ``notifications``, one
document per user, newest 100 kept) and live (WebSocket event ``notification`` on /api/wizard/ws):

* an escalation they raised was confirmed (the ZIP is ready), rejected, or the admin asked a question;
* a tool their session was waiting for became ACTIVE (the session resumed) or was rejected.

Important ones are also emailed when the account has an email address and mail is configured.
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.wizard import store

COLLECTION = "notifications"
MAX_ITEMS = 100
_LOCK = threading.Lock()
LEVELS = ("info", "success", "warning", "error")


def _now() -> str:
    return datetime.utcnow().isoformat()


def _doc_id(user: str) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9_.@-]", "_", user or "local")[:180] or "local"


def _load(user: str) -> Dict[str, Any]:
    return store.get_store().get(COLLECTION, _doc_id(user)) or {"user": user, "items": []}


def notify(user: Optional[str], kind: str, title: str, body: str = "", link: Optional[Dict[str, Any]] = None,
           level: str = "info", email: bool = False) -> Optional[Dict[str, Any]]:
    """Adds a notification for a Studio user (and pushes it live). email=True also mails it."""
    if not user:
        return None
    item = {"id": "N-" + uuid.uuid4().hex[:10], "kind": str(kind)[:40], "title": str(title)[:200],
            "body": str(body)[:1500], "link": dict(link or {}), "level": level if level in LEVELS else "info",
            "at": _now(), "read": False}
    with _LOCK:
        doc = _load(user)
        doc["items"] = ([item] + list(doc.get("items") or []))[:MAX_ITEMS]
        store.get_store().put(COLLECTION, _doc_id(user), doc)
    try:
        from core.wizard import events
        events.publish("notification", {"notification": item, "unread": unread_count(user)}, users=[user], admin=False)
    except Exception:
        pass
    if email:
        _mail(user, item)
    return item


def _mail(user: str, item: Dict[str, Any]) -> None:
    try:
        from core.wizard import mailer, users
        prof = users.profile(user)
        to = prof.get("email")
        if not to:
            return
        text = f"Hello {user},\n\n{item['title']}\n\n{item['body']}\n\nOpen the Understudy Studio to see the details.\n"
        mailer.send(to, f"Understudy: {item['title']}", text,
                    mailer._html(mailer._esc(item["title"]), f"<p>Hello {mailer._esc(user)},</p><p>"
                                 f"{mailer._esc(item['body'])}</p><p>Open the Understudy Studio to see the details.</p>"),
                    kind=f"notice-{item['kind']}")
    except Exception:                                            # mail is best effort, never blocks the flow
        pass


def items(user: str, limit: int = 50) -> List[Dict[str, Any]]:
    return list(_load(user).get("items") or [])[:max(1, min(limit, MAX_ITEMS))]


def unread_count(user: str) -> int:
    return sum(1 for i in _load(user).get("items") or [] if not i.get("read"))


def mark_read(user: str, ids: Optional[List[str]] = None) -> int:
    """Marks the given notifications (or all, ids=None) as read. Returns how many changed."""
    n = 0
    with _LOCK:
        doc = _load(user)
        for i in doc.get("items") or []:
            if not i.get("read") and (ids is None or i["id"] in ids):
                i["read"], i["read_at"] = True, _now()
                n += 1
        if n:
            store.get_store().put(COLLECTION, _doc_id(user), doc)
    return n


def clear(user: str) -> None:
    with _LOCK:
        store.get_store().put(COLLECTION, _doc_id(user), {"user": user, "items": []})
