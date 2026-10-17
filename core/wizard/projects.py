"""User -> Projects -> Sessions hierarchy.

    user (core/wizard/users.py)
      project      {project_id, owner, name, description}          store collection ``projects``
        session    one complete run of the 5-step pipeline        store collection ``sessions`` (core/wizard/session.py)

A light-weight index of every session (``session_index``: owner, project, name, status, step, target, process,
framework, timestamps) is kept next to the full session documents so project pages, the Step-1 history search and
Mission Control never have to load full sessions (which can be close to a megabyte each).

Every read and write is scoped to the owner: a user never sees or touches another user's projects or sessions
(the API answers 404, so ids cannot be probed). Names and descriptions pass Model Armor before they are stored,
because they are later shown to other agents as history context.
"""
from __future__ import annotations

import re
import threading
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.wizard import store
from core.wizard.guardrails import screen_text

PROJECTS = "projects"
INDEX = "session_index"
MAX_NAME = 80
MAX_DESCRIPTION = 1000
_LOCK = threading.RLock()


class ProjectError(Exception):
    def __init__(self, message: str, status_code: int = 400, details: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.details = details


def _now() -> str:
    return datetime.utcnow().isoformat()


def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", str(name or "")).strip().lower()


def clean_name(value: str, what: str, max_len: int = MAX_NAME) -> str:
    v = re.sub(r"\s+", " ", str(value or "")).strip()
    if not v:
        raise ProjectError(f"{what} is required.", 400, {"field": what.lower().replace(' ', '_')})
    if len(v) > max_len:
        raise ProjectError(f"{what} must be at most {max_len} characters.", 400, {"field": what.lower().replace(' ', '_')})
    tv = screen_text(v, "PROJECT", use_semantic=False)
    if not tv.is_safe:
        raise ProjectError(tv.friendly_message, 400, {"field": what.lower().replace(' ', '_'), "security": True})
    return tv.sanitized.strip()


def clean_description(value: str) -> str:
    v = str(value or "").strip()
    if not v:
        raise ProjectError("Project Description is required.", 400, {"field": "project_description"})
    if len(v) > MAX_DESCRIPTION:
        raise ProjectError(f"Project Description must be at most {MAX_DESCRIPTION} characters.", 400,
                           {"field": "project_description"})
    tv = screen_text(v, "PROJECT")
    if not tv.is_safe:
        raise ProjectError(tv.friendly_message, 400, {"field": "project_description", "security": True})
    return tv.sanitized.strip()


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------
def _all_projects() -> List[Dict[str, Any]]:
    return [d for d in store.iter_docs(PROJECTS) if d.get("project_id")]


def get_project(owner: Optional[str], project_id: str) -> Dict[str, Any]:
    try:
        doc = store.get_store().get(PROJECTS, project_id) if str(project_id or "").startswith("prj-") else None
    except ValueError:
        doc = None
    if not doc or (owner is not None and doc.get("owner") != owner):
        raise ProjectError("Project not found.", 404)
    return doc


def create_project(owner: Optional[str], name: str, description: str) -> Dict[str, Any]:
    name = clean_name(name, "Project Name")
    description = clean_description(description)
    with _LOCK:
        if any(_norm(p.get("name")) == _norm(name) and p.get("owner") == owner for p in _all_projects()):
            raise ProjectError(f"You already have a project called '{name}'.", 409, {"field": "project_name"})
        doc = {"project_id": "prj-" + uuid.uuid4().hex[:10], "owner": owner, "name": name, "description": description,
               "created_at": _now(), "updated_at": _now()}
        store.get_store().put(PROJECTS, doc["project_id"], doc)
    return doc


def update_project(owner: Optional[str], project_id: str, name: Optional[str] = None,
                   description: Optional[str] = None) -> Dict[str, Any]:
    with _LOCK:
        doc = get_project(owner, project_id)
        if name is not None:
            name = clean_name(name, "Project Name")
            if any(_norm(p.get("name")) == _norm(name) and p.get("owner") == owner and p["project_id"] != project_id
                   for p in _all_projects()):
                raise ProjectError(f"You already have a project called '{name}'.", 409, {"field": "project_name"})
            doc["name"] = name
        if description is not None:
            doc["description"] = clean_description(description)
        doc["updated_at"] = _now()
        store.get_store().put(PROJECTS, project_id, doc)
    if name is not None:
        from core.wizard import session as wz
        wz.rename_project_in_sessions(project_id, doc["name"])
    return doc


def delete_project(owner: Optional[str], project_id: str) -> Dict[str, Any]:
    from core.wizard import session as wz
    doc = get_project(owner, project_id)
    rows = sessions_of(owner, project_id)
    busy = [r["name"] for r in rows if ((wz.SESSIONS.get(r["session_id"]) or {}).get("job") or {}).get("status") == "running"]
    if busy:                                   # check first, so a project is never left half-deleted
        raise ProjectError(f"These sessions are still working: {', '.join(busy)}. Wait for them to finish.", 409)
    removed = 0
    for row in rows:
        try:
            wz.delete_session(row["session_id"], owner)
        except wz.WizardError as e:
            if e.status_code != 404:
                raise
            unindex_session(row["session_id"])  # the index pointed at a session document that no longer exists
        removed += 1
    store.get_store().delete(PROJECTS, project_id)
    return {"deleted": project_id, "sessions_deleted": removed, "name": doc["name"]}


def reassign_owner(old: str, new: str) -> Dict[str, int]:
    """A deleted account's projects and sessions are kept for audit under ``new`` (see users.delete_user)."""
    from core.wizard import session as wz
    st = store.get_store()
    n_p = n_s = 0
    for p in _all_projects():
        if p.get("owner") == old:
            p["owner"] = new
            st.put(PROJECTS, p["project_id"], p)
            n_p += 1
    for r in [d for d in store.iter_docs(INDEX) if d.get("owner") == old]:
        s = wz.SESSIONS.get(r["session_id"]) or st.get("sessions", r["session_id"])
        if s:
            s["owner"] = new
            if r["session_id"] in wz.SESSIONS:
                wz._register_context(s)
            st.put("sessions", r["session_id"], s if r["session_id"] not in wz.SESSIONS else wz._durable_doc(s))
        r["owner"] = new
        st.put(INDEX, r["session_id"], r)
        n_s += 1
    return {"projects_archived": n_p, "sessions_archived": n_s}


def list_projects(owner: Optional[str]) -> List[Dict[str, Any]]:
    rows = index_rows(owner)
    out = []
    for p in _all_projects():
        if p.get("owner") != owner:
            continue
        mine = sorted([r for r in rows if r.get("project_id") == p["project_id"]],
                      key=lambda r: r.get("created_at") or "", reverse=True)
        out.append(dict(p, sessions=mine, session_count=len(mine),
                        completed=len([r for r in mine if r.get("status") == "completed"])))
    return sorted(out, key=lambda p: p.get("updated_at") or "", reverse=True)


# ---------------------------------------------------------------------------
# Session index
# ---------------------------------------------------------------------------
def summary_of(s: Dict[str, Any]) -> Dict[str, Any]:
    pillars = s.get("pillars") or {}
    passed = [p for p in ("WATCH", "BUILD", "GOVERN", "BUILD-EXECUTE", "EXPORT", "VERIFY") if (pillars.get(p) or {}).get("status") == "PASS"]
    return {"session_id": s["session_id"], "owner": s.get("owner"), "project_id": s.get("project_id"),
            "project_name": s.get("project_name"), "name": s.get("session_name") or s["session_id"],
            "name_auto": bool(s.get("name_auto")), "name_source": s.get("name_source"),
            "status": s.get("status"), "current_step": s.get("current_step"), "pillars_passed": len(passed),
            "target_url": s.get("target_url") or "", "process_name": (s.get("analysis") or {}).get("process_name"),
            "framework": (s.get("framework") or {}).get("name"),
            "steps": len((s.get("analysis") or {}).get("steps") or []),
            "has_analysis": bool(s.get("analysis")), "created_at": s.get("created_at"), "updated_at": s.get("updated_at"),
            "badge": pipeline_badge(s, passed)}


def pipeline_badge(s: Dict[str, Any], passed: List[str]) -> str:
    """8.0 workspace explorer micro-badge: DRAFT | ANALYSED | BUILT | GOVERNED | RUNNING | PENDING | FAILED | VERIFIED."""
    pillars = s.get("pillars") or {}
    if s.get("status") == "pending_admin_approval":
        return "PENDING"
    if len(passed) == 5:
        return "VERIFIED"
    if (s.get("job") or {}).get("status") == "running":
        return "RUNNING"
    if s.get("status") in ("needs_help", "stopped") or any((pillars.get(p) or {}).get("status") == "FAIL" for p in pillars):
        return "FAILED"
    if "GOVERN" in passed:
        return "GOVERNED"
    if "BUILD" in passed:
        return "BUILT"
    if s.get("analysis"):
        return "ANALYSED"
    return "DRAFT"


def index_session(s: Dict[str, Any]) -> None:
    try:
        store.get_store().put(INDEX, s["session_id"], summary_of(s))
    except Exception:
        pass


def unindex_session(session_id: str) -> None:
    try:
        store.get_store().delete(INDEX, session_id)
    except Exception:
        pass


def row_badge(r: Dict[str, Any]) -> str:
    """Badge for an index row written before 8.0 (no ``badge`` field): derived from the summary alone."""
    if r.get("status") == "pending_admin_approval":
        return "PENDING"
    n = int(r.get("pillars_passed") or 0)
    if n >= 5 or r.get("status") == "completed":
        return "VERIFIED"
    if r.get("status") in ("needs_help", "stopped", "failed"):
        return "FAILED"
    return {4: "GOVERNED", 3: "GOVERNED", 2: "BUILT"}.get(n) or ("ANALYSED" if r.get("has_analysis") else "DRAFT")


def index_rows(owner: Optional[str]) -> List[Dict[str, Any]]:
    rows = [d for d in store.iter_docs(INDEX) if d.get("session_id") and d.get("owner") == owner]
    for r in rows:
        if not r.get("badge"):
            r["badge"] = row_badge(r)
    return rows


def sessions_of(owner: Optional[str], project_id: str) -> List[Dict[str, Any]]:
    return [r for r in index_rows(owner) if r.get("project_id") == project_id]


def session_name_taken(owner: Optional[str], project_id: str, name: str, except_sid: str = "") -> bool:
    return any(_norm(r.get("name")) == _norm(name) and r["session_id"] != except_sid
               for r in sessions_of(owner, project_id))


def search(owner: Optional[str], query: str, exclude_session: str = "", limit: int = 25) -> Dict[str, Any]:
    """Step 1 history picker: the user's own projects and sessions whose name (or process / project name) matches."""
    q = _norm(query)
    words = [w for w in q.split(" ") if w]
    rows = [r for r in index_rows(owner) if r["session_id"] != exclude_session]
    projects = {p["project_id"]: p for p in _all_projects() if p.get("owner") == owner}

    def hit(*texts: Any) -> bool:
        t = " ".join(_norm(x) for x in texts if x)
        return all(w in t for w in words) if words else True
    sessions = [dict(r, usable=bool(r.get("has_analysis"))) for r in rows
                if hit(r.get("name"), r.get("project_name"), r.get("process_name"))]
    sessions.sort(key=lambda r: r.get("updated_at") or "", reverse=True)      # newest first ...
    sessions.sort(key=lambda r: not r["usable"])                              # ... sessions with an analysis first
    prj = []
    for p in projects.values():
        if hit(p.get("name"), p.get("description")):
            members = [r for r in rows if r.get("project_id") == p["project_id"]]
            prj.append({"project_id": p["project_id"], "name": p["name"], "description": p.get("description", "")[:200],
                        "session_count": len(members), "usable_sessions": len([r for r in members if r.get("has_analysis")])})
    prj.sort(key=lambda p: p["name"].lower())
    return {"query": query, "sessions": sessions[:limit], "projects": prj[:limit]}
