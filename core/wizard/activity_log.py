"""Detailed activity telemetry for Mission Control.

Everything the admin needs to audit a run is recorded here as structured events:

    USER_ACTION  - what the user did in Understudy (created a session, picked a tool, edited the payload, ...)
    AI_REQUEST   - the exact prompt sent to Gemini: model, system prompt, user prompt, attached media
    AI_RESPONSE  - the raw response text, the parsed JSON, and any items dropped by output validation
    AI_REPAIR    - the answer was not valid JSON / did not match the schema: the exact errors sent back to Gemini for
                   its one repair round (the next AI_RESPONSE or AI_ERROR says whether the fix was accepted)
    AI_ERROR     - why the call failed (invalid key, quota, timeout, model not found, invalid output, ...)
    RECALL       - no AI: a validated answer learned in an earlier AI session was re-used (core/wizard/recall.py)
    FALLBACK     - the deterministic LOCAL_HEURISTIC code that ran instead, and what it produced
    STEP         - pipeline progress (WATCH/BUILD/GOVERN/EXECUTE/VERIFY)
    EXECUTION    - automation log lines (STEP n OK / FAILED, runtime output)
    RUNTIME      - dependency installs, toolchain provisioning, runtime doctor probes
    SECURITY     - Model Armor decisions and blocks
    ADMIN        - admin changes in Mission Control

Events live in an in-memory ring buffer (newest 5,000) with a monotonically increasing ``seq`` so the UI can fetch
incrementally (``after=<seq>``), and are appended to ``generated/wizard/activity.jsonl`` for durability.
With ``UNDERSTUDY_STORE=firestore`` (Cloud Run, several instances) every event is also written - by a background
writer, so a slow network never delays the automation - to Cloud Firestore, ``seq`` becomes a microsecond timestamp
that is unique across instances, and Mission Control reads the merged stream from Firestore.
API keys are never recorded: every string is passed through ``redact`` before it is stored.
"""
from __future__ import annotations

import contextvars
import json
import os
import re
import queue
import threading
import time
from collections import deque
from datetime import datetime
from typing import Any, Dict, List, Optional

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MAX_EVENTS = 5000
MAX_FIELD_CHARS = int(os.environ.get("UNDERSTUDY_TELEMETRY_MAX_CHARS") or "60000")

_LOCK = threading.Lock()
_EVENTS: "deque[Dict[str, Any]]" = deque(maxlen=MAX_EVENTS)
_SEQ = [0]

# The session a piece of work belongs to (set by the wizard's job runner and API handlers).
current_session: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("understudy_session", default=None)
current_stage: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("understudy_stage", default=None)

# Run context of each session: who ran it, in which project, under which session name (core/wizard/session.py
# registers it). Every event of that session carries it, so Mission Control shows "[User] | [Project] | [Session]".
_CONTEXT: Dict[str, Dict[str, Any]] = {}
_CTX_UNSAFE = re.compile(r"[\[\]|\r\n]")


def _ctx_part(v: Any, default: str) -> str:
    return _CTX_UNSAFE.sub(" ", str(v or default)).strip()[:80] or default


def set_context(session_id: str, user: Optional[str] = None, project: Optional[str] = None,
                project_id: Optional[str] = None, session_name: Optional[str] = None) -> Dict[str, Any]:
    ctx = {"user": _ctx_part(user, "local"), "project": _ctx_part(project, "No project"), "project_id": project_id,
           "session_name": _ctx_part(session_name, session_id)}
    ctx["label"] = f"[{ctx['user']}] | [{ctx['project']}] | [{ctx['session_name']}]"
    _CONTEXT[session_id] = ctx
    return ctx


def context_for(session_id: Optional[str]) -> Optional[Dict[str, Any]]:
    return _CONTEXT.get(session_id or "")


# '[12:00:01] [alice] | [Invoices] | [Run 1] | STEP 1 OK' -> 'STEP 1 OK' (for AI prompts and fingerprints, which must
# not depend on who ran a session or what it was called)
_PREFIX_RX = re.compile(r"^(\[\d{2}:\d{2}:\d{2}\] )?\[[^\]|]*\] \| \[[^\]|]*\] \| \[[^\]|]*\] \| ", re.M)


def strip_context(text: str) -> str:
    return _PREFIX_RX.sub(lambda m: m.group(1) or "", text or "")


KINDS = ("USER_ACTION", "AI_REQUEST", "AI_RESPONSE", "AI_REPAIR", "AI_ERROR", "RECALL", "FALLBACK", "STEP", "EXECUTION", "RUNTIME",
         "SECURITY", "ADMIN")

_KEY_RX = re.compile(r"AIza[0-9A-Za-z_\-]{20,}")


def _secrets() -> List[str]:
    vals = []
    for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "UNDERSTUDY_ADMIN_TOKEN"):
        v = os.environ.get(k)
        if v and len(v) >= 6:
            vals.append(v)
    return vals


def redact(value: Any) -> Any:
    if isinstance(value, str):
        s = value
        for secret in _secrets():
            s = s.replace(secret, "***REDACTED***")
        s = _KEY_RX.sub("AIza***REDACTED***", s)
        if len(s) > MAX_FIELD_CHARS:
            s = s[:MAX_FIELD_CHARS] + f"\n... [truncated, {len(value) - MAX_FIELD_CHARS} more characters]"
        return s
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


def log_path() -> str:
    return os.environ.get("UNDERSTUDY_ACTIVITY_LOG") or os.path.join(_BASE, "generated", "wizard", "activity.jsonl")


def record(kind: str, title: str, detail: str = "", data: Optional[Dict[str, Any]] = None,
           session_id: Optional[str] = None, stage: Optional[str] = None, source: Optional[str] = None,
           level: str = "info") -> Dict[str, Any]:
    ev = {
        "ts": datetime.utcnow().isoformat() + "Z",
        "kind": kind if kind in KINDS else "STEP",
        "session_id": session_id or current_session.get(),
        "stage": stage or current_stage.get(),
        "title": redact(str(title))[:500],
        "detail": redact(str(detail or ""))[:4000],
        "source": source,                     # ACTUAL_AI_API | LOCAL_HEURISTIC | USER | ADMIN | SYSTEM
        "level": level,                       # info | warn | error
        "data": redact(data or {}),
    }
    ctx = _CONTEXT.get(ev["session_id"] or "")
    if ctx:
        ev["context"] = {k: ctx[k] for k in ("user", "project", "project_id", "session_name")}
        ev["context_label"] = ctx["label"]
    shared = _shared()
    with _LOCK:
        _SEQ[0] = max(_SEQ[0] + 1, int(time.time() * 1_000_000)) if shared is not None else _SEQ[0] + 1
        ev["seq"] = _SEQ[0]
        _EVENTS.append(ev)
    if shared is not None:
        _enqueue(shared, ev)
    try:
        path = log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(ev, default=str) + "\n")
    except Exception:
        pass
    return ev


def _shared():
    try:
        from core.wizard import store
        st = store.get_store()
        return st if getattr(st, "kind", "local") != "local" else None
    except Exception:
        return None


_QUEUE: "queue.Queue" = queue.Queue(maxsize=20000)
_WRITER: Dict[str, Any] = {"thread": None, "dropped": 0, "failed": 0}


def _writer_loop():
    while True:
        st, ev = _QUEUE.get()
        try:
            st.add_event(ev)
        except Exception:
            _WRITER["failed"] += 1                # the local JSONL copy still has it
        finally:
            _QUEUE.task_done()


def _enqueue(st, ev: Dict[str, Any]) -> None:
    if _WRITER["thread"] is None or not _WRITER["thread"].is_alive():
        _WRITER["thread"] = threading.Thread(target=_writer_loop, name="telemetry-writer", daemon=True)
        _WRITER["thread"].start()
    try:
        _QUEUE.put_nowait((st, ev))
    except queue.Full:
        _WRITER["dropped"] += 1


def flush(timeout: float = 5.0) -> None:
    """Waits until queued events reach the shared store (tests, shutdown)."""
    end = time.time() + timeout
    while _QUEUE.unfinished_tasks and time.time() < end:
        time.sleep(0.01)


def query(after: int = 0, session_id: Optional[str] = None, kinds: Optional[List[str]] = None,
          text: Optional[str] = None, limit: int = 500, user: Optional[str] = None,
          project: Optional[str] = None) -> Dict[str, Any]:
    shared = _shared()
    items = None
    if shared is not None:
        try:                                      # every instance's events, in seq order
            items = shared.events_after(int(after), max(limit * 4, 2000))
        except Exception:
            items = None                          # store unreachable: this instance's own buffer
    with _LOCK:
        if items is None:
            items = [e for e in _EVENTS if e["seq"] > after]
        last = max([_SEQ[0]] + [int(e.get("seq") or 0) for e in items])
    if session_id:
        items = [e for e in items if e.get("session_id") == session_id]
    if kinds:
        ks = set(kinds)
        items = [e for e in items if e["kind"] in ks]
    if user:
        items = [e for e in items if ((e.get("context") or {}).get("user") or "").lower() == user.lower()]
    if project:
        p = project.lower()
        items = [e for e in items if p in ((e.get("context") or {}).get("project") or "").lower()
                 or p == ((e.get("context") or {}).get("project_id") or "").lower()]
    if text:
        t = text.lower()
        items = [e for e in items if t in (e["title"] + " " + e["detail"] + " " + e.get("context_label", "")).lower()]
    return {"events": items[:limit], "last_seq": last, "more": len(items) > limit}


def stats() -> Dict[str, Any]:
    with _LOCK:
        items = list(_EVENTS)
    out: Dict[str, Any] = {k: 0 for k in KINDS}
    sessions, users, projects = set(), set(), set()
    for e in items:
        out[e["kind"]] = out.get(e["kind"], 0) + 1
        if e.get("session_id"):
            sessions.add(e["session_id"])
        ctx = e.get("context") or {}
        if ctx.get("user"):
            users.add(ctx["user"])
        if ctx.get("project") and ctx.get("project") != "No project":
            projects.add(ctx["project"])
    out["sessions"] = sorted(sessions)
    out["users"] = sorted(users)
    out["projects"] = sorted(projects)
    out["ai_calls"] = out["AI_REQUEST"]
    out["ai_failures"] = out["AI_ERROR"]
    out["ai_repairs"] = out["AI_REPAIR"]
    out["fallbacks"] = out["FALLBACK"]
    return out


def clear() -> None:
    with _LOCK:
        _EVENTS.clear()
