"""Escalations (8.3): runtime-help requests from a user's run to the admin.

When a run in step 4 fails because of the server (a missing package, a version the admin has not allowed, a runtime
that is not installed, a proxy / network setting ...), self-healing can't fix it inside the user's code. Instead it
sends a **runtime-help request** (``create_help``) with the AI's diagnosis and the change it proposes:

1. Mission Control > Escalations shows it live (WebSocket inbox). Understudy adds its own evidence automatically: the
   run's log tail, the target, the tool and the parameters (secret values never).
2. The admin **confirms** - applying the AI's change or their own (*Fix it yourself*: a package, an allowed version, a
   run variable, a PATH folder, a proxy-bypass host, the network mode) for **everyone** or only for **this user**
   (9.3), optionally re-running the user's step 4 - **rejects** with a reason, or **asks** a question.
3. Every decision reaches the user as a Studio notification (bell) and by email.

Before 9.0 users could also escalate a run whose page verification failed; 9.0 exports every run that passes step 4,
so that path no longer exists.
"""
from __future__ import annotations

import os
import re
import threading
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from core.wizard import activity_log, notifications, store

COLLECTION = "escalations"
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FILES_DIR = os.path.join(BASE_DIR, "generated", "escalations")
OPEN = ("open", "info_requested")
_LOCK = threading.Lock()


class EscalationError(ValueError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _now() -> str:
    return datetime.utcnow().isoformat()


def _get(esc_id: str) -> Dict[str, Any]:
    if not re.match(r"^ESC-[A-F0-9]{8}$", esc_id or ""):
        raise EscalationError("Unknown escalation.", 404)
    doc = store.get_store().get(COLLECTION, esc_id)
    if not doc:
        raise EscalationError("Unknown escalation.", 404)
    return doc


def _put(doc: Dict[str, Any]) -> None:
    doc["updated_at"] = _now()
    store.get_store().put(COLLECTION, doc["id"], doc)


def _screen(text: str, channel: str) -> str:
    from core.wizard.guardrails import screen_text
    tv = screen_text(text[:4000], channel)
    if not tv.is_safe:
        activity_log.record("SECURITY", "Model Armor blocked an escalation message", tv.friendly_message,
                            {"pattern": tv.pattern}, source="Model Armor", level="warn")
        raise EscalationError(tv.friendly_message)
    return tv.sanitized


def attachment_path(doc: Dict[str, Any], file_id: str) -> Tuple[str, Dict[str, Any]]:
    att = next((a for a in doc.get("attachments") or [] if a["id"] == file_id), None)
    if not att:
        raise EscalationError("Unknown attachment.", 404)
    p = os.path.join(FILES_DIR, doc["id"], att["id"] + att["ext"])
    if not os.path.isfile(p) or not os.path.abspath(p).startswith(os.path.abspath(FILES_DIR)):
        raise EscalationError("The attachment is no longer on this server.", 404)
    return p, att


def _evidence(s: Dict[str, Any]) -> Dict[str, Any]:
    v = s.get("verification") or {}
    ex = s.get("execution") or {}
    spec = (s.get("framework") or {})
    return {
        "target_url": s.get("target_url"), "tool": spec.get("name") or spec.get("id"),
        "tool_version": str(((spec.get("packages") or [{}])[0] or {}).get("version") or "").lstrip("=") or None,
        "verify": {"summary": v.get("summary"), "method": v.get("method"), "checks": (v.get("checks") or [])[:30],
                   "visual_diff_ratio": v.get("visual_diff_ratio"), "explanation": v.get("explanation"),
                   "has_before": bool((v.get("evidence") or {}).get("before_screenshot")),
                   "has_after": bool((v.get("evidence") or {}).get("after_screenshot"))},
        "execute": {"status": ex.get("status"), "strategy": ex.get("strategy"), "duration_ms": ex.get("duration_ms"),
                    "log_tail": [str(x)[:400] for x in (ex.get("logs") or [])[-60:]]},
        "parameters": [{"key": p.get("key"), "label": p.get("label"),
                        "value": "(secret)" if p.get("type") == "secret" else str(p.get("value"))[:200]}
                       for p in (s.get("parameters") or [])[:40]],
        "retries": int((s.get("retry") or {}).get("count") or 0),
        "pillars": {k: (v2 or {}).get("status") for k, v2 in (s.get("pillars") or {}).items()},
    }


def _summary(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {k: doc.get(k) for k in ("id", "status", "created_at", "updated_at", "decided_by", "decided_at",
                                    "admin_note", "question", "run_id")}


def _sync_session(doc: Dict[str, Any]) -> None:
    from core.wizard import session as wz
    try:
        if doc.get("kind") == "runtime_help":
            wz.set_runtime_help(doc["session_id"], dict(_summary(doc), kind="runtime_help",
                                                         admin_action=doc.get("admin_action")))
    except Exception:
        pass


# any ecosystem: pip / gem / nuget names, scoped npm (@wdio/cli), Maven / Gradle coordinates (group:artifact), Go modules
_PKG_CHARS = r"@?[A-Za-z0-9][A-Za-z0-9._~/:-]{0,150}"
_PKG_ASK = re.compile(r"(?i)\b(?:add|install|include)\b[^'\"`]{0,60}['\"`](" + _PKG_CHARS + r")['\"`]")
_PKG_NAME = re.compile(r"^" + _PKG_CHARS + r"$")


def _package_in(text: str) -> str:
    """The package a runtime-help request asks the admin to add, e.g. "Please add 'robotframework-jsonlibrary' ..."."""
    t = (text or "").strip()
    if _PKG_NAME.match(t):
        return t
    m = _PKG_ASK.search(t)
    return m.group(1) if m else ""


def create_help(s: Dict[str, Any], admin_action: Dict[str, Any], explanation: str, diagnosis: str = "") -> Dict[str, Any]:
    """8.3: self-healing found a fix only the admin can make (allow a version, install build tools, register an
    interpreter ...). The user approved sending it; it lands in Mission Control > Escalations as 'runtime help'."""
    from core.wizard import events, session as wz
    act: Dict[str, Any] = {k: str(v)[:800] for k, v in (admin_action or {}).items()
                           if k in ("action", "version", "request", "kind")}
    if act.get("action") not in ("allow_version", "server_change", "use_version", "add_package"):
        act["action"] = "server_change"
    pkg = _package_in(str((admin_action or {}).get("package") or "") or act.get("request", "") + " " + explanation)
    if pkg and act["action"] == "server_change":         # 8.5.3: "please add 'robotframework-jsonlibrary'" -> one click
        act.update(action="add_package", package=pkg)
    if act["action"] == "use_version":               # 8.4: a runtime switch / download from the pre-run check
        fix = (admin_action or {}).get("fix") or {}
        if fix.get("type") == "switch" and fix.get("install_id"):
            act["fix"] = {"type": "switch", "install_id": str(fix["install_id"])[:40],
                          "version": str(fix.get("version") or "")[:40], "path": str(fix.get("path") or "")[:400],
                          "label": str(fix.get("label") or "")[:300]}
        elif fix.get("type") == "download" and fix.get("key"):
            act["fix"] = {"type": "download", "key": str(fix["key"])[:40], "label": str(fix.get("label") or "")[:300]}
        else:
            act["action"] = "server_change"
    spec = s.get("framework") or {}
    act["tool_id"] = spec.get("id")
    act["tool_name"] = spec.get("name")
    esc_id = "ESC-" + uuid.uuid4().hex[:8].upper()
    msg = (act.get("request") or explanation or "The automation can't run on this machine without an admin change.")[:1500]
    doc = {"id": esc_id, "kind": "runtime_help", "session_id": s["session_id"], "owner": s.get("owner") or "local",
           "project_id": s.get("project_id"), "project_name": s.get("project_name"), "session_name": s.get("session_name"),
           "status": "open", "created_at": _now(), "message": msg, "attachments": [], "admin_action": act,
           "evidence": dict(_evidence(s), ai_diagnosis=diagnosis, explanation=explanation[:1500]),
           "run_id": wz.run_identity(s),
           "conversation": [{"role": "user", "by": s.get("owner") or "local", "at": _now(), "attachments": [],
                             "text": "Self-healing needs you: " + msg}]}
    _put(doc)
    _sync_session(doc)
    activity_log.record("USER_ACTION", f"{doc['owner']} sent a runtime fix to the admin ({esc_id})", msg[:300],
                        {"escalation": esc_id, "session": s["session_id"], "admin_action": act}, source="USER")
    events.publish("escalation.created", {"escalation": esc_id, "session_id": s["session_id"], "owner": doc["owner"],
                                          "session_name": doc.get("session_name"), "status": "open",
                                          "kind": "runtime_help"}, admin=True)
    notifications.notify(doc["owner"], "help_sent", f"Sent to your admin ({esc_id})",
                         "Self-healing needs your admin: " + msg[:300] + " You'll be notified when they answer.",
                         {"session_id": s["session_id"], "escalation_id": esc_id})
    return public(doc)


ADMIN_ACTIONS = ("allow_version", "set_env", "proxy_bypass", "path_prepend", "network_mode", "use_version", "add_package")


SCOPES = ("everyone", "user")
# 9.3: these change something only one server has - they always apply to everyone
SHARED_ONLY = ("proxy_bypass", "network_mode", "use_version")


def _for_user(doc: Dict[str, Any], admin: str, tool_id: str, kind: str, key: str, value: str = "") -> bool:
    from core.wizard import admin_policy as ap
    try:
        return ap.add_user_override(tool_id, doc.get("owner"), kind, key, value, by=admin)
    except ap.PolicyError as e:
        raise EscalationError(str(e), 422)


def run_admin_actions(doc: Dict[str, Any], admin: str, actions: List[Dict[str, Any]],
                      scope: str = "everyone") -> List[str]:
    """8.3: what the ADMIN decided to change themselves (no AI involved) - each validated by the same policy functions
    Mission Control uses elsewhere. Returns one sentence per action for the user's notification. 9.3: ``scope`` 'user'
    applies packages, allowed versions, run variables and PATH folders to the requesting user only; server-wide
    settings (proxy bypass, network mode, runtime switch / download) always apply to everyone."""
    from core.wizard import admin_policy as ap
    only_me = scope == "user"
    tool_id = (doc.get("admin_action") or {}).get("tool_id") or (doc.get("evidence") or {}).get("tool_id")
    if not tool_id:
        from core.wizard import session as wz
        try:
            tool_id = (wz.get(doc["session_id"]).get("framework") or {}).get("id")
        except Exception:
            tool_id = None
    done: List[str] = []
    for a in (actions or [])[:8]:
        t = str(a.get("type") or "")
        if t not in ADMIN_ACTIONS:
            raise EscalationError(f"Unknown admin action '{t}'.")
        if t == "allow_version":
            v = str(a.get("version") or "").strip().lstrip("=v")
            if not tool_id or not v:
                raise EscalationError("Allowing a version needs the tool and the version.")
            if only_me:
                _for_user(doc, admin, tool_id, "version", v)
                done.append(f"allowed version {v} for you")
                continue
            pol = ap.tool_version_policy(tool_id)
            allowed = list(pol.get("allowed") or [])
            if v not in allowed:
                allowed.append(v)
            ap.set_tool_versions(tool_id, allowed, pol.get("default") or v, set_by=admin)
            done.append(f"allowed version {v}")
        elif t == "set_env":
            name, value = str(a.get("name") or "").strip(), str(a.get("value") or "")
            if not tool_id or not name:
                raise EscalationError("A run setting needs a variable name.")
            if only_me and value:
                _for_user(doc, admin, tool_id, "env", name, value)
                done.append(f"set {name} for your runs of the tool")
                continue
            env = dict(ap.tool_runtime(tool_id).get("env_vars") or {})
            if value == "":
                env.pop(name, None)
            else:
                env[name] = value
            ap.set_tool_runtime(tool_id, env_vars=env, by=admin)
            done.append(f"set {name} for every run of the tool" if value else f"removed {name}")
        elif t == "path_prepend":
            p = str(a.get("path") or "").strip()
            if not tool_id or not p:
                raise EscalationError("Adding a PATH folder needs the folder.")
            if only_me:
                _for_user(doc, admin, tool_id, "path", p)
                done.append(f"added {p} to PATH for your runs of the tool")
                continue
            cur = list(ap.tool_runtime(tool_id).get("path_prepend") or [])
            ap.set_tool_runtime(tool_id, path_prepend=[p] + [x for x in cur if x != p], by=admin)
            done.append(f"added {p} to PATH for the tool")
        elif t == "proxy_bypass":
            h = str(a.get("host") or "").strip()
            cur = [x for x in ap.proxy_bypass_hosts() if x not in ap.LOOPBACK_BYPASS]
            ap.set_proxy_bypass_hosts(cur + [h], admin)
            done.append(f"let runs reach {h} without the run proxy")
        elif t == "network_mode":
            m = str(a.get("mode") or "")
            ap.set_network_mode(m)
            done.append(f"set the run network to {m}")
        elif t == "add_package":                      # 8.5.3: a package the suite needs (e.g. robotframework-jsonlibrary)
            name = str(a.get("name") or "").strip()
            if not tool_id or not name:
                raise EscalationError("Adding a package needs the tool and the package name.")
            done.append(_add_package(doc, admin, tool_id, name, str(a.get("version") or "").strip(), scope))
        elif t == "use_version":                      # 8.4: an installed runtime version, or a download of one
            kind = str(a.get("kind") or "")
            if not tool_id or not kind:
                raise EscalationError("Switching a runtime needs the tool and the runtime.")
            fix = ({"type": "switch", "install_id": str(a["install_id"])} if a.get("install_id") else
                   {"type": "download", "key": str(a["key"])} if a.get("key") else None)
            if not fix:
                raise EscalationError("Pick an installed version or a download.")
            done.append(_apply_version_fix(doc, admin, tool_id, kind, fix))
    return done


def _apply_version_fix(doc: Dict[str, Any], admin: str, tool_id: str, kind: str, fix: Dict[str, Any]) -> str:
    from core.wizard import environments

    def after(job: Dict[str, Any]):
        if fix.get("type") != "download":
            return
        ok = job.get("status") == "done" and job.get("switched")
        notifications.notify(doc["owner"], "runtime_ready" if ok else "runtime_failed",
                             "The runtime your run needs is ready - run it again" if ok else
                             "The runtime download failed",
                             (f"Your admin's download finished and the tool now uses version "
                              f"{(job.get('switched') or {}).get('version')}. Open the session and click Run again."
                              if ok else f"{job.get('error') or 'The download did not finish.'} Your admin has been told."),
                             {"session_id": doc["session_id"], "escalation_id": doc["id"]},
                             level="success" if ok else "error", email=True)
    try:
        return environments.apply_fix(tool_id, kind, fix, admin, after=after)
    except environments.EnvironmentError_ as e:
        raise EscalationError(str(e), e.status_code)


def _add_package(doc: Dict[str, Any], admin: str, tool_id: str, name: str, version: str = "",
                 scope: str = "everyone") -> str:
    """Adds the package to the approved tool (9.3: or only to the requesting user's runs of it) AND to the user's
    session, so their next run installs it."""
    from core.wizard import admin_policy as ap, session as wz
    if not version and ":" in name:                  # 9.9.1: Maven / Gradle coordinates - never an unpinned LATEST
        from core.wizard.dependency_lock import default_version
        version = default_version(name)
    if scope == "user":
        added = _for_user(doc, admin, tool_id, "package", name, version)
    else:
        try:
            added = ap.add_tool_packages(tool_id, [{"name": name, "version": version}], admin)
        except ap.PolicyError as e:
            raise EscalationError(str(e), 422)
    try:
        s = wz.get(doc["session_id"])
        for key in ("framework", "framework_base"):
            sp = s.get(key)
            if sp and not any(str(p.get("name", "")).lower() == name.lower() for p in sp.get("packages") or []):
                v = version and (("==" + version) if version[0] not in "<>=!~" else version)
                sp.setdefault("packages", []).append({"name": name, "version": v or ""})
        wz._persist(s)
    except Exception:
        pass                                         # the tool has it; a new session picks it up anyway
    who = " for you" if scope == "user" else ""
    return (f"added {name}{' ' + version if version else ''} to {tool_id}{who}" if added
            else f"{name} was already part of {tool_id}{who}")


def _run_admin_action(doc: Dict[str, Any], admin: str, scope: str = "everyone") -> str:
    """What 'Confirm' does for a runtime-help request: allow the version (one click) or just record that the admin made
    the server change. Returns a sentence for the user."""
    from core.wizard import admin_policy as ap
    act = doc.get("admin_action") or {}
    if act.get("action") == "allow_version" and act.get("tool_id") and act.get("version") and scope == "user":
        _for_user(doc, admin, act["tool_id"], "version", act["version"])
        return f"allowed {act.get('tool_name') or act['tool_id']} {act['version']} for you"
    if act.get("action") == "allow_version" and act.get("tool_id") and act.get("version"):
        pol = ap.tool_version_policy(act["tool_id"])
        allowed = list(pol.get("allowed") or [])
        if act["version"] not in allowed:
            allowed.append(act["version"])
        ap.set_tool_versions(act["tool_id"], allowed, pol.get("default") or act["version"], set_by=admin)
        return f"allowed {act.get('tool_name') or act['tool_id']} {act['version']}"
    if act.get("action") == "add_package" and act.get("tool_id") and act.get("package"):
        return _add_package(doc, admin, act["tool_id"], act["package"], "", scope)
    if act.get("action") == "use_version" and act.get("tool_id") and act.get("kind") and act.get("fix"):
        return _apply_version_fix(doc, admin, act["tool_id"], act["kind"], act["fix"])
    return "made the change on the server"


def decide(esc_id: str, admin: str, decision: str, note: str = "", actions: Optional[List[Dict[str, Any]]] = None,
           rerun: bool = False, scope: str = "everyone") -> Dict[str, Any]:
    """confirm | reject | ask (needs the note as the question). 8.3 runtime help: ``actions`` are changes the admin
    makes themselves on confirm (allow a version, set a run variable, a PATH folder, a proxy-bypass host, the network
    mode) instead of - or as well as - the AI's suggestion; ``rerun`` starts the user's run again right away."""
    from core.wizard import events, session as wz
    doc = _get(esc_id)
    if doc["status"] not in OPEN:
        raise EscalationError(f"This escalation is already {doc['status']}.", 409)
    decision = str(decision or "").lower()
    note = str(note or "").strip()
    if note:
        note = _screen(note, "ADMIN_INSTRUCTION")
    if decision not in ("confirm", "reject", "ask"):
        raise EscalationError("Decide with confirm, reject or ask.")
    scope = str(scope or "everyone").lower()
    if scope not in SCOPES:
        raise EscalationError("Apply the change to 'everyone' or to this 'user' only.")
    if decision in ("reject", "ask") and len(note) < 5:
        raise EscalationError("Tell the user why (reject) or what you need (ask) - at least 5 characters.")
    link = {"session_id": doc["session_id"], "escalation_id": esc_id}
    label = f"'{doc.get('session_name') or doc['session_id']}'"
    if decision == "confirm" and doc.get("kind") == "runtime_help":
        try:
            own = run_admin_actions(doc, admin, actions or [], scope)
            did = ", ".join(own) if own else _run_admin_action(doc, admin, scope)
        except EscalationError:
            raise
        except Exception as e:
            raise EscalationError(f"Could not apply it: {e}", 422)
        doc["admin_actions_done"] = own
        doc["scope"] = scope
        doc.update(status="confirmed", decided_by=admin, decided_at=_now(), admin_note=note)
        title, body, level = (f"Your admin {did} - run {label} again",
                              f"{admin} answered {esc_id}: {did}." + (f" Note: {note}" if note else "")
                              + " Open the session and click Run again"
                              + (f" (pick version {doc['admin_action']['version']})."
                                 if (doc.get("admin_action") or {}).get("action") == "allow_version" else "."), "success")
    elif decision == "confirm":                    # a pre-9.0 "verification" escalation: nothing left to confirm
        raise EscalationError("Only runtime-help requests can be confirmed. Reject this older verification "
                              "escalation - since 9.0 a run that passes step 4 is exported right away.", 409)
    elif decision == "reject":
        doc.update(status="rejected", decided_by=admin, decided_at=_now(), admin_note=note)
        title, body, level = (f"Your admin did not confirm {label}", f"{admin} rejected escalation {esc_id}: {note}",
                              "error")
    else:
        doc.update(status="info_requested", question=note, asked_by=admin, asked_at=_now())
        title, body, level = (f"Your admin has a question about {label}", f"{admin} asks: {note}", "warning")
    doc["conversation"] = list(doc.get("conversation") or []) + [
        {"role": "admin", "by": admin, "text": ({"confirm": "Confirmed", "reject": "Rejected", "ask": "Question"}[decision]
                                                + (f": {note}" if note else "")), "at": _now(), "decision": decision}]
    _put(doc)
    _sync_session(doc)
    activity_log.record("ADMIN", f"Admin '{admin}' {decision}ed escalation {esc_id}".replace("asked", "asked about")
                        .replace("confirmed", "confirmed"), note[:300], {"escalation": esc_id}, source="ADMIN")
    events.publish("escalation.updated", {"escalation": esc_id, "session_id": doc["session_id"], "status": doc["status"],
                                          "owner": doc["owner"]}, users=[doc["owner"]], admin=True)
    notifications.notify(doc["owner"], f"escalation_{decision}", title, body, link, level=level, email=True)
    if rerun and decision == "confirm" and doc.get("kind") == "runtime_help":
        try:                                       # 8.3: the admin re-runs Step 4 for the user
            wz.admin_rerun(doc["session_id"], admin)
            doc["rerun_started"] = True
        except Exception as e:
            doc["rerun_error"] = str(e)[:300]
        _put(doc)
    return public(doc, admin=True)


def public(doc: Dict[str, Any], admin: bool = False) -> Dict[str, Any]:
    out = {k: v for k, v in doc.items() if k != "evidence" or admin}
    if not admin:
        out["evidence"] = {"verify": {"summary": ((doc.get("evidence") or {}).get("verify") or {}).get("summary")}}
    return out


def for_admin(status: Optional[str] = None) -> List[Dict[str, Any]]:  # noqa: D401 - both kinds
    rows = [d for d in store.iter_docs(COLLECTION) if not status or d.get("status") == status
            or (status == "open_any" and d.get("status") in OPEN)]
    return sorted(rows, key=lambda d: d.get("created_at") or "", reverse=True)


def get_for_admin(esc_id: str) -> Dict[str, Any]:
    return public(_get(esc_id), admin=True)


def open_count() -> int:
    return sum(1 for d in store.iter_docs(COLLECTION) if d.get("status") == "open")
