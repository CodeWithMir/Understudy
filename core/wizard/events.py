"""Live events (8.0): WebSocket push to Mission Control's inbox and to the Studio.

* ``publish(type, data, audience)`` can be called from any thread (setup jobs, request handlers). Every connected
  socket whose audience matches gets the event; the last 200 events are kept for ``recent()`` (the polling fallback and
  sockets that reconnect).
* ``/api/admin/ws`` - Mission Control. ``/api/wizard/ws`` - a Studio user; only events for that user.
* Authentication is the first message ``{"token": "<admin or user token>"}`` (browsers can't set headers on a
  WebSocket, and a token in the URL would end up in access logs). No valid token within 5 s -> closed with 4401.
* One process only: with several Cloud Run instances an event reaches the sockets of the instance that produced it;
  the UIs also poll every few seconds, so nothing is missed.

Event types: tool_request.created, tool_request.updated, tool.activated, session.updated.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from collections import deque
from typing import Any, Deque, Dict, List, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter(tags=["Live events"])
_LOCK = threading.Lock()
_SUBS: List[Dict[str, Any]] = []
_RECENT: Deque[Dict[str, Any]] = deque(maxlen=200)
AUTH_TIMEOUT_S = 5.0


def publish(type_: str, data: Dict[str, Any], users: Optional[List[str]] = None, admin: bool = True) -> Dict[str, Any]:
    """users: Studio usernames that should get it (None = no Studio user); admin: Mission Control gets it."""
    ev = {"id": "ev-" + uuid.uuid4().hex[:10], "type": type_, "at": time.time(), "data": data,
          "_users": sorted(set(u for u in (users or []) if u)), "_admin": admin}
    with _LOCK:
        _RECENT.append(ev)
        subs = list(_SUBS)
    for sub in subs:
        if _visible(ev, sub["role"], sub["user"]):
            try:
                sub["loop"].call_soon_threadsafe(sub["queue"].put_nowait, _public(ev))
            except RuntimeError:                       # loop closed
                pass
    return _public(ev)


def _visible(ev: Dict[str, Any], role: str, user: Optional[str]) -> bool:
    return (role == "admin" and ev["_admin"]) or (role == "user" and user in ev["_users"])


def _public(ev: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in ev.items() if not k.startswith("_")}


def recent(role: str, user: Optional[str] = None, after: float = 0.0) -> List[Dict[str, Any]]:
    with _LOCK:
        evs = list(_RECENT)
    return [_public(e) for e in evs if e["at"] > after and _visible(e, role, user)]


def subscriber_count() -> int:
    with _LOCK:
        return len(_SUBS)


def reset() -> None:
    with _LOCK:
        _RECENT.clear()


async def _serve(ws: WebSocket, role: str) -> None:
    await ws.accept()
    try:
        first = await asyncio.wait_for(ws.receive_text(), timeout=AUTH_TIMEOUT_S)
        token = (json.loads(first) or {}).get("token") or ""
    except (asyncio.TimeoutError, ValueError, WebSocketDisconnect, AttributeError):
        await _close(ws, 4401, "authenticate with {\"token\": ...} first")
        return
    user = None
    if role == "admin":
        from core.wizard import admin_auth
        sess = admin_auth.session_for(token)
        if not sess:
            await _close(ws, 4401, "invalid admin token")
            return
        user = sess["user"]
    else:
        from core.wizard import users
        if users.login_required():
            info = users.verify(token)
            if not info:
                await _close(ws, 4401, "invalid user token")
                return
            user = info["username"]
        else:
            user = "local"
    sub = {"queue": asyncio.Queue(maxsize=500), "loop": asyncio.get_running_loop(), "role": role, "user": user}
    with _LOCK:
        _SUBS.append(sub)
    try:
        await ws.send_json({"type": "hello", "role": role, "user": user, "at": time.time()})
        while True:
            try:
                ev = await asyncio.wait_for(sub["queue"].get(), timeout=25)
                await ws.send_json(ev)
            except asyncio.TimeoutError:
                await ws.send_json({"type": "ping", "at": time.time()})
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        with _LOCK:
            if sub in _SUBS:
                _SUBS.remove(sub)


async def _close(ws: WebSocket, code: int, reason: str) -> None:
    try:
        await ws.close(code=code, reason=reason)
    except RuntimeError:
        pass


@router.websocket("/api/admin/ws")
async def admin_ws(ws: WebSocket):
    await _serve(ws, "admin")


@router.websocket("/api/wizard/ws")
async def user_ws(ws: WebSocket):
    await _serve(ws, "user")
