"""Wizard session state machine, recorded in the pipeline contract (core/pipeline_contract.py).

For every step it:
  * records a ``PillarResult`` under the pillar names WATCH, BUILD, GOVERN, BUILD-EXECUTE, EXPORT with strict
    order / gating (a step can't start until the previous pillar passed),
  * writes Mission Control telemetry (``activity_log``) and a tagged console line (``emit_pipeline_telemetry``,
    ACTUAL_AI_API / LOCAL_HEURISTIC provenance),
  * mirrors its state into a ``PipelineReport`` in ``PIPELINE_RUN_REGISTRY`` from the moment the session is
    created, and keeps the ZIP in ``GENERATED_ZIP_CACHE`` (served by ``GET /api/wizard/sessions/{id}/export``,
    403 until all five pillars pass),
  * enforces the admin policy (core/wizard/admin_policy.py): only admin-whitelisted tools can be selected,
    anything else becomes an access request that an admin approves in Mission Control.

Long-running work (AI video analysis, inspection, code generation, execution, self-healing) runs as a
background job per session; the UI polls ``GET /api/wizard/sessions/{id}`` and ``/logs`` (and listens on
``/api/wizard/ws``).
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import re
import threading
import time
import traceback
import uuid
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from core.pipeline_contract import (GENERATED_ZIP_CACHE, PIPELINE_RUN_REGISTRY, PillarResult, PipelineReport,
                                    emit_pipeline_telemetry)
from core.wizard import build as build_mod
from core.wizard import (activity_log, admin_policy, dependency_lock, dependency_resolver, executor, exporter,
                          governance, payload_schema, runtime_repair, skills, store, verifier, watch)
from core.wizard.frameworks import (CATALOG, FrameworkSpec, FrameworkValidationError, resolve_custom_framework,
                                    validate_spec)
from core.wizard.guardrails import (MAX_TARGET_URLS, GuardrailViolation, parse_url_list, record_block, safe_filename,
                                    screen_media, screen_parameters, screen_text, screen_url)
from core.wizard.llm import ai_available, display_model, generate_json, unavailable_reason

BASE_DIR = dependency_resolver.BASE_DIR
WORK_ROOT = os.path.join(BASE_DIR, "generated", "wizard")
UPLOAD_ROOT = os.path.join(BASE_DIR, "uploads", "wizard")
SAMPLE_MEDIA = os.path.join(BASE_DIR, "assets", "demo_ap_recording.mp4")
# 9.0: step 5 is EXPORT - a run that passes step 4 is packaged right away (no before/after page verification)
# 9.9: Govern comes before Build - the confirmed SOP rules are part of every generated script (governance.json + the
# check before step 1), so the code is written after the rules are settled.
PILLARS = ["WATCH", "GOVERN", "BUILD", "BUILD-EXECUTE", "EXPORT"]
STEP_OF_PILLAR = {"WATCH": 1, "GOVERN": 2, "BUILD": 3, "BUILD-EXECUTE": 4, "EXPORT": 5}
def max_retries() -> int:
    """Self-healing retry limit set by the admin in Mission Control (0 = unlimited)."""
    return admin_policy.max_self_heal_retries()

SESSIONS: Dict[str, Dict[str, Any]] = {}
_LOCKS: Dict[str, threading.RLock] = {}


class WizardError(Exception):
    def __init__(self, message: str, status_code: int = 400, details: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.details = details


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _now() -> str:
    return datetime.utcnow().isoformat()


def _lock(sid: str) -> threading.RLock:
    return _LOCKS.setdefault(sid, threading.RLock())


def get(sid: str) -> Dict[str, Any]:
    s = SESSIONS.get(sid) or _restore(sid)
    if not s:
        raise WizardError("Session not found", 404)
    activity_log.current_session.set(sid)      # AI calls made while handling this request are tagged with the session
    return s


def _activity(s: Dict[str, Any], kind: str, title: str, detail: str = "", data: Optional[Dict[str, Any]] = None,
              source: Optional[str] = None, level: str = "info", stage: Optional[str] = None):
    """Detailed Mission Control telemetry (core/wizard/activity_log.py)."""
    try:
        activity_log.record(kind, title, detail, data, session_id=s["session_id"], stage=stage,
                            source=source or ("USER" if kind == "USER_ACTION" else "SYSTEM"), level=level)
    except Exception:
        pass


def _telemetry(s: Dict[str, Any], stage: str, source: str, action: str, detail: str = "", agent: Optional[str] = None):
    _activity(s, "STEP", action, detail, {"agent": agent}, source=source, stage=stage)
    s.setdefault("agent_trace", []).append({"stage": stage, "agent_source": source, "agent": agent, "action": action[:300],
                                            "at": _now()})
    s["agent_trace"] = s["agent_trace"][-200:]
    try:
        # the console line knows two sources; a learned-skill answer made no AI call, so it is reported as local
        emit_pipeline_telemetry(run_id=s["session_id"], stage=stage,  # type: ignore[arg-type]
                                agent_source=source if source in ("ACTUAL_AI_API", "LOCAL_HEURISTIC") else "LOCAL_HEURISTIC",
                                action_description=f"[Wizard] {context_label(s)} | {action}", detail=detail[:500],
                                agent_name=agent)
    except Exception:
        pass


def _set_pillar(s: Dict[str, Any], pillar: str, status: str, reason: str, evidence: Optional[Dict[str, Any]] = None):
    s["pillars"][pillar] = PillarResult(pillar=pillar, status=status, reason=reason,  # type: ignore[arg-type]
                                        evidence=evidence or {}).to_dict()
    _sync_report(s)


def _reset_from(s: Dict[str, Any], pillar: str):
    idx = PILLARS.index(pillar)
    for p in PILLARS[idx:]:
        s["pillars"].pop(p, None)
    if idx <= PILLARS.index("GOVERN"):
        s["compliance"] = None
    if idx <= PILLARS.index("BUILD-EXECUTE"):
        s["execution"] = None
        s["retry"]["awaiting_decision"] = False
        s["retry"]["awaiting_approval"] = False
        s["retry"]["proposal"] = None
    s["verification"] = None
    s["export"] = {"ready": False}
    GENERATED_ZIP_CACHE.pop(s["session_id"], None)
    _sync_report(s)


def _sync_report(s: Dict[str, Any]):
    """Mirrors wizard state into the pipeline report registry (core/pipeline_contract.py)."""
    all_pass = all(s["pillars"].get(p, {}).get("status") == "PASS" for p in PILLARS)
    failed = next((p for p in PILLARS if s["pillars"].get(p, {}).get("status") == "FAIL"), None)
    pending = next((p for p in PILLARS if p not in s["pillars"]), None)
    fw = (s.get("framework") or {}).get("id", "UNSELECTED")
    rep = PipelineReport(
        run_id=s["session_id"], engine_target=f"WIZARD:{fw}", target_portal_url=s.get("target_url", ""),
        overall_status="PASS" if all_pass else "FAIL", passed=all_pass,
        failed_pillar=None if all_pass else (failed or pending),
        failure_reason=None if all_pass else (s["pillars"].get(failed, {}).get("reason") if failed else
                                              f"Wizard step '{pending}' has not passed yet"),
        pillars=copy.deepcopy(s["pillars"]), zip_available=bool(s.get("export", {}).get("ready")),
        zip_filename=s.get("export", {}).get("filename"), zip_download_url=s.get("export", {}).get("download_url"),
        retry_count=s["retry"]["count"], max_retries_allowed=s["retry"]["max"], governing_policy_version="understudy",
        retry_history=copy.deepcopy(s["retry"]["history"]))
    PIPELINE_RUN_REGISTRY[s["session_id"]] = rep


def _durable_doc(s: Dict[str, Any]) -> Dict[str, Any]:
    """What goes to the durable store (core/wizard/store.py): the whole session EXCEPT secret values and the raw
    failure page, so a restarted backend - or another Cloud Run instance - can pick the session up again."""
    d = json.loads(json.dumps({k: v for k, v in s.items()}, default=str))
    for p in d.get("parameters") or []:
        if p.get("type") == "secret":
            p["has_value"] = bool(p.get("value"))
            p["value"] = ""
    if isinstance(d.get("execution"), dict):
        d["execution"].pop("failure_html", None)
        d["execution"]["logs"] = (d["execution"].get("logs") or [])[-400:]
    if isinstance(d.get("dependencies"), dict):
        d["dependencies"]["logs"] = (d["dependencies"].get("logs") or [])[-200:]
    # Firestore documents are limited to 1 MiB: drop the bulkiest, re-creatable parts if a session grows past that
    for trim in (("execution", "logs"), ("dependencies", "logs"), ("agent_trace",), ("verification", "evidence")):
        if len(json.dumps(d, default=str)) < store.MAX_DOC_BYTES:
            break
        parent = d
        for k in trim[:-1]:
            parent = parent.get(k) if isinstance(parent, dict) else None
        if isinstance(parent, dict) and trim[-1] in parent:
            parent[trim[-1]] = [] if isinstance(parent[trim[-1]], list) else {}
    return d


def _persist(s: Dict[str, Any]):
    s["updated_at"] = _now()
    try:
        d = os.path.join(WORK_ROOT, s["session_id"])
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "session.json"), "w", encoding="utf-8") as fh:
            json.dump(public_view(s, include_logs=True), fh, indent=2, default=str)
    except Exception:
        pass
    try:
        store.get_store().put("sessions", s["session_id"], _durable_doc(s))
    except Exception as e:                       # durability is best-effort; the session keeps working in memory
        _activity(s, "STEP", "Could not save the session to the durable store", f"{type(e).__name__}: {e}"[:300],
                  level="warn")
    from core.wizard import projects
    projects.index_session(s)


def _share_recording(s: Dict[str, Any]) -> None:
    """With UNDERSTUDY_GCS_BUCKET set (Cloud Run), the recording is also kept in Cloud Storage so any instance can
    continue the session - and Vertex AI reads it from there instead of receiving it again."""
    bucket = os.environ.get("UNDERSTUDY_GCS_BUCKET")
    if not bucket or not s.get("media"):
        return
    try:
        from core.wizard import gemini
        path = s["media"]["path"]
        s["media"]["gcs_uri"] = gemini.upload_to_gcs(path, bucket, gemini.recording_object(path))
    except Exception as e:
        _activity(s, "RUNTIME", "Could not copy the recording to Cloud Storage", f"{type(e).__name__}: {e}"[:300],
                  level="warn")


def _restore(sid: str) -> Optional[Dict[str, Any]]:
    """Loads a session saved by another backend instance or before a restart."""
    if not isinstance(sid, str) or not sid.startswith("wiz-"):
        return None
    try:
        doc = store.get_store().get("sessions", sid)
    except Exception:
        return None
    if not doc or doc.get("session_id") != sid:
        return None
    with _lock(sid):
        if sid in SESSIONS:
            return SESSIONS[sid]
        s = doc
        s.setdefault("notes", [])
        job = s.get("job") or {}
        if job.get("status") == "running":
            s["job"] = {**job, "status": "error", "finished_at": _now(), "interrupted": True,
                        "error": f"Understudy restarted while '{job.get('name')}' was running. "
                                 f"Nothing was lost - press the step's button again to continue."}
        r = s.setdefault("retry", {"count": 0, "max": max_retries(), "history": []})
        r.setdefault("history", [])
        missing = [p.get("label") or p.get("name") for p in s.get("parameters") or []
                   if p.get("type") == "secret" and p.get("has_value") and not p.get("value")]
        if missing:
            s["notes"].append("Secret values are never saved, so after the restart please re-enter: "
                              + ", ".join(str(m) for m in missing) + ".")
        media = s.get("media") or {}
        if media.get("path") and not os.path.exists(media["path"]) and media.get("gcs_uri"):
            try:                                 # restored on another instance: fetch the recording back
                from core.wizard import gemini
                gemini.download_from_gcs(media["gcs_uri"], media["path"])
            except Exception:
                s["notes"].append("The recording could not be fetched from Cloud Storage on this instance.")
        s["restored_at"] = _now()
        for k, v in _blank(sid).items():         # sessions saved by older versions
            s.setdefault(k, v)
        SESSIONS[sid] = s
        _register_context(s)
        _sync_report(s)
        if (s.get("export") or {}).get("ready"):
            try:
                _make_export(s)                  # the ZIP bytes live in memory only: rebuild them
            except Exception:
                s["export"] = {"ready": False}
                _sync_report(s)
    _activity(s, "STEP", "Session restored from the durable store", store.describe()["kind"])
    return s


def _security_event(s: Dict[str, Any], channel: str, pattern: Optional[str], raw: str, message: str):
    s.setdefault("security_events", []).append({"channel": channel, "pattern": pattern, "message": message[:300],
                                                "at": _now()})
    record_block(s["session_id"], channel, pattern, raw, s.get("target_url", ""), detail=message)
    _activity(s, "SECURITY", f"Model Armor blocked {channel} input ({pattern})", message,
              {"channel": channel, "pattern": pattern, "input_excerpt": str(raw)[:400]}, source="MODEL_ARMOR", level="warn")
    _telemetry(s, "GOVERN", "LOCAL_HEURISTIC", f"Model Armor blocked {channel} input ({pattern})", message,
               "Model Armor (wizard guardrails)")


def public_view(s: Dict[str, Any], include_logs: bool = False) -> Dict[str, Any]:
    v = copy.deepcopy({k: val for k, val in s.items() if not k.startswith("_")})
    for p in v.get("parameters") or []:
        if p.get("type") == "secret":
            p["has_value"] = bool(p.get("value"))
            p["value"] = ""
    prop = (v.get("retry") or {}).get("proposal")
    if prop:
        prop.pop("suite", None)          # the UI shows the diff; the full reviewed suite stays server-side
    ex = v.get("execution")
    if ex:
        ex.pop("failure_html", None)
        ex["log_count"] = len(ex.get("logs", []))
        if not include_logs:
            ex["logs"] = ex.get("logs", [])[-40:]
    if v.get("media"):
        v["media"].pop("path", None)
    deps = v.get("dependencies") or {}
    if deps.get("logs") and not include_logs:
        deps["logs"] = deps["logs"][-30:]
    v["framework_options"] = annotate_tools(v.get("framework_options") or [], v.get("owner"),
                                            (v.get("framework") or {}).get("id"))
    ag = (((v.get("difficulty") or {}).get("recommendation") or {}).get("agentic") or {})
    if ag.get("tools") and admin_policy.builtin_agent_hidden(v.get("owner")):   # 9.9: also in older assessments
        cur = (v.get("framework") or {}).get("id")
        ag["tools"] = [t for t in ag["tools"] if not admin_policy.is_builtin(t.get("tool_id") or "") or t.get("tool_id") == cur]
    if v.get("tool_requirement"):
        from core.wizard import tool_requirements
        v["tool_requirement"] = dict(v["tool_requirement"], **tool_requirements.status(v["tool_requirement"]))
    if v.get("pending_request"):
        try:
            r = admin_policy.get_request(v["pending_request"]["id"])
            v["pending_request"] = dict(v["pending_request"], status=r["status"],
                                        registry_status=admin_policy.REGISTRY_STATUS.get(r["status"], r["status"].upper()),
                                        admin_note=r.get("admin_note", ""))
        except admin_policy.PolicyError:
            pass
    prof = v.get("page_profile")
    if prof:                                     # 9.0: the UI needs the summary and the chosen locators, not the raw DOM
        from core.wizard import page_probe
        v["page_profile"] = {"renderer": prof.get("renderer"), "loads": prof.get("loads"), "title": prof.get("title"),
                             "error": prof.get("error"), "signals": prof.get("signals") or {},
                             "summary": page_probe.strategy_summary(prof),
                             "pages": prof.get("pages") or [], "checks": prof.get("checks") or [],
                             "plan": prof.get("plan"), "reload_decision": prof.get("reload_decision"),
                             "fields": [{"label": f.get("label"), "kind": f.get("kind"), "page": f.get("page"),
                                         "recommended": f.get("recommended"), "unstable": f.get("unstable"),
                                         "moved_between_loads": f.get("moved_between_loads")}
                                        for f in (prof.get("fields") or [])[:25]]}
    v["ai_mode"] = "ACTUAL_AI_API" if ai_available() else "LOCAL_HEURISTIC"
    v["ai_model"] = display_model() if ai_available() else None
    v["agentic"] = is_agentic(s)
    if v["agentic"]:                                        # 9.9: the organisation's Gemini - no key from the user
        from core.wizard import agent_relay
        v["agent_ai"] = dict(agent_relay.info(), **({"model": _tool_agent_model(s)} if _tool_agent_model(s) else {}))
    return v


def annotate_tools(options: List[Dict[str, Any]], user: Optional[str] = None, current: Optional[str] = None) -> List[Dict[str, Any]]:
    """Marks each suggested tool as allowed (admin whitelist) or not, with its access-request status, and
    appends admin-whitelisted tools that the AI didn't suggest so users can still pick them. 9.0: a tool the admin
    opened to selected users only - or (9.9) to no user - is shown locked (``restricted``, ``access_mode``) to everyone
    it excludes, and not appended at all."""
    out = []
    seen = set()
    wl = admin_policy.whitelist()
    if admin_policy.builtin_agent_hidden(user):              # 9.9: not listed next to the admin's agentic tools
        seen.update(t for t in admin_policy.BUILTIN_TOOLS if t != current)
    for o in options:
        if o.get("id") in seen and admin_policy.is_builtin(o.get("id") or ""):
            continue
        o = dict(o)
        rid = admin_policy.resolve_tool_id(o["id"])
        if rid != o["id"] and rid in wl:            # 8.2: the admin set this tool up as a composed stack
            o = dict(wl[rid]["spec"], recommended=o.get("recommended", False), source="whitelist")
        if o["id"] in seen:
            # 8.3 fix: several AI suggestions ("cypress", "cypress-js", ...) can be aliases of ONE approved stack, and
            # the AI may repeat an id. Each tool is shown once - otherwise every copy shared the id, so picking one
            # highlighted them all. A duplicate only passes on its "recommended" flag.
            if o.get("recommended"):
                next(x for x in out if x["id"] == o["id"])["recommended"] = True
            continue
        approved = admin_policy.is_tool_allowed(o["id"])
        o["builtin"] = admin_policy.is_builtin(o["id"])
        o["allowed"] = approved and admin_policy.user_may_use(o["id"], user)
        o["restricted"] = approved and not o["allowed"]
        if o["restricted"]:
            o.update(blocked=None, access_request=None, versions=None,
                     access_mode=admin_policy.tool_access(o["id"])["mode"])
            out.append(o)
            seen.add(o["id"])
            continue
        blk = None if o["allowed"] else admin_policy.tool_block_for(FrameworkSpec.from_dict(o))
        o["blocked"] = {"name": blk["name"], "note": blk.get("note", "")} if blk and not blk.get("versions") else None
        o["access_request"] = None if o["allowed"] or o["blocked"] else admin_policy.request_status_for(o["id"])
        o["versions"] = admin_policy.tool_version_policy(o["id"], user) if o["allowed"] else None
        if o["allowed"]:
            o["version_requests"] = _version_requests(o["id"])
        out.append(o)
        seen.add(o["id"])
    for tid, entry in admin_policy.whitelist().items():
        if tid not in seen and admin_policy.user_may_use(tid, user):
            o = dict(entry["spec"], allowed=True, access_request=None, recommended=False, source="whitelist",
                     builtin=bool(entry.get("builtin")),
                     versions=admin_policy.tool_version_policy(tid, user), blocked=None, version_requests=_version_requests(tid))
            out.append(o)
    return out


def _version_requests(tool_id: str) -> List[Dict[str, Any]]:
    return [{"id": r["id"], "version": r.get("version"), "status": r["status"], "admin_note": r.get("admin_note", "")}
            for r in admin_policy.requests() if r.get("kind") == "version" and r["tool_id"] == tool_id][:10]


def _run_job(s: Dict[str, Any], name: str, fn: Callable[[], None], background: bool):
    with _lock(s["session_id"]):
        if s.get("job", {}).get("status") == "running":
            raise WizardError(f"Please wait - '{s['job']['name']}' is still running.", 409)
        s["job"] = {"name": name, "status": "running", "started_at": _now(), "error": None}

    def _wrapped():
        activity_log.current_session.set(s["session_id"])     # AI calls made by this job are tagged with the session
        try:
            fn()
            s["job"].update({"status": "done", "finished_at": _now()})
        except WizardError as e:
            s["job"].update({"status": "error", "error": str(e), "details": e.details, "finished_at": _now()})
            _activity(s, "STEP", f"'{name}' stopped: {e}", "", {"details": e.details}, level="warn")
        except GuardrailViolation as e:
            _security_event(s, e.verdict.channel, e.verdict.pattern, e.verdict.sanitized, str(e))
            s["job"].update({"status": "error", "error": str(e), "security": True, "finished_at": _now()})
        except Exception as e:  # pragma: no cover - defensive
            traceback.print_exc()
            s["job"].update({"status": "error", "error": f"Unexpected error: {type(e).__name__}: {e}", "finished_at": _now()})
            _activity(s, "STEP", f"'{name}' crashed: {type(e).__name__}: {e}", traceback.format_exc()[-3000:], level="error")
        finally:
            _persist(s)

    if background:
        threading.Thread(target=_wrapped, name=f"wizard-{name}-{s['session_id']}", daemon=True).start()
    else:
        _wrapped()


def _require(s: Dict[str, Any], *pillars: str):
    for p in pillars:
        if s["pillars"].get(p, {}).get("status") != "PASS":
            raise WizardError(f"Step '{p}' must pass first (current status: {s['pillars'].get(p, {}).get('status', 'not started')}).", 409)


def _spec(s: Dict[str, Any]) -> FrameworkSpec:
    if not s.get("framework"):
        raise WizardError("Choose an automation framework first.", 409)
    return FrameworkSpec.from_dict(s["framework"])


# ---------------------------------------------------------------------------
# STEP 1 - WATCH
# ---------------------------------------------------------------------------
def _blank(sid: str) -> Dict[str, Any]:
    return {
        "session_id": sid, "created_at": _now(), "updated_at": _now(), "current_step": 1, "status": "draft",
        "owner": None, "project_id": None, "project_name": None, "session_name": None,
        "target_url": "", "target_urls": [], "pages": [], "pages_rejected": [], "pages_confirmed": True,
        "pages_meta": None, "pages_removed": [], "sop_text": "", "media": None, "analysis": None, "inventory": None,
        "framework": None, "framework_options": [], "dependencies": None, "parameters": [], "parameter_problems": [],
        "suite": None, "rules": [], "rules_confirmed": False, "rules_meta": {}, "compliance": None,
        "execution": None, "verification": None, "healing_notes": [], "strategy_override": None,
        "retry": {"count": 0, "max": max_retries(), "history": [], "awaiting_decision": False, "prompt": None,
                  "last_heal": None, "awaiting_approval": False, "proposal": None},
        "pillars": {}, "security_events": [], "job": {"status": "idle"}, "export": {"ready": False},
        "page_profile": None, "complexity": None, "inspection_plan": None, "difficulty": None,
        "agent_trace": [], "notes": [], "history_context": None,
    }


def context_label(s: Dict[str, Any]) -> str:
    """'[User] | [Project Name] | [Session Name]' - shown on every log line and telemetry event of the run."""
    ctx = activity_log.context_for(s.get("session_id")) or _register_context(s)
    return ctx["label"]


def _register_context(s: Dict[str, Any]) -> Dict[str, Any]:
    return activity_log.set_context(s["session_id"], user=s.get("owner"), project=s.get("project_name"),
                                    project_id=s.get("project_id"), session_name=s.get("session_name"))


def new_session(owner: Optional[str] = None, project_id: Optional[str] = None,
                session_name: Optional[str] = None) -> Dict[str, Any]:
    """Creates a named session inside one of the owner's projects. It stays a draft until Step 1 is submitted."""
    from core.wizard import projects
    sid = "wiz-" + uuid.uuid4().hex[:10]
    s = _blank(sid)
    if project_id:
        try:
            prj = projects.get_project(owner, project_id)
            # 9.1: no name needed - like a new chat, the session is "New session" until WATCH has understood the
            # recording, then the AI names it (the user can rename it any time)
            auto = not str(session_name or "").strip()
            name = _unique_name(owner, project_id, "New session") if auto else \
                projects.clean_name(session_name or "", "Session Name")
        except projects.ProjectError as e:
            raise WizardError(str(e), e.status_code, e.details)
        if not auto and projects.session_name_taken(owner, project_id, name):
            raise WizardError(f"This project already has a session called '{name}'.", 409, {"field": "session_name"})
        s.update(owner=owner, project_id=project_id, project_name=prj["name"], session_name=name, name_auto=auto)
    elif session_name:
        try:
            s["session_name"] = projects.clean_name(session_name, "Session Name")
        except projects.ProjectError as e:
            raise WizardError(str(e), e.status_code, e.details)
        s["owner"] = owner
    else:
        s["owner"] = owner
        s["name_auto"] = True
    SESSIONS[sid] = s
    _register_context(s)
    _sync_report(s)
    activity_log.current_session.set(sid)
    _activity(s, "USER_ACTION", f"User started a new Understudy session {context_label(s)}", "",
              {"project_id": project_id, "session_name": s.get("session_name")})
    _persist(s)
    return s


def create_session(target_url: Any, sop_text: str, media_tmp_path: Optional[str], media_filename: Optional[str],
                   use_sample_media: bool = False, background: bool = True, owner: Optional[str] = None,
                   project_id: Optional[str] = None, session_name: Optional[str] = None,
                   context_refs: Optional[List[Dict[str, Any]]] = None,
                   target_urls: Optional[List[str]] = None) -> Dict[str, Any]:
    """New session + Step 1 in one call (kept for scripts, CI and the single-user mode)."""
    s = new_session(owner, project_id, session_name)
    try:
        return start_session(s["session_id"], target_url, sop_text, media_tmp_path, media_filename,
                             use_sample_media=use_sample_media, background=background, context_refs=context_refs,
                             target_urls=target_urls)
    except WizardError:
        if session_name and not s.get("media"):
            # the one-call form never showed the user a draft: don't leave a named one behind (a retry with the same
            # name must work); the security event and telemetry are already recorded
            delete_session(s["session_id"])
        raise


def target_urls_of(s: Dict[str, Any]) -> List[str]:
    """9.4: every page the automation visits, in order (the first one is where it starts = ``target_url``)."""
    urls = [u for u in (s.get("target_urls") or []) if u]
    if s.get("target_url") and s["target_url"] not in urls:
        urls.insert(0, s["target_url"])
    return urls


def _apply_pages(s: Dict[str, Any], pages: List[Dict[str, Any]]) -> None:
    """9.4: the session's page list (url, source user|video, where it was seen in the recording) drives
    ``target_url`` (page 1) and ``target_urls``."""
    s["pages"] = pages[:MAX_TARGET_URLS]
    s["target_urls"] = [p["url"] for p in s["pages"]]
    s["target_url"] = s["target_urls"][0] if s["target_urls"] else ""


def _merge_video_pages(user: List[Dict[str, Any]], found: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The recording's order wins; the user's start page stays first; a typed page the AI did not see keeps its place."""
    by_url = {p["url"]: dict({"seen_in_video": False}, **p) for p in user}
    merged: List[Dict[str, Any]] = []
    for f in found:
        if f["url"] in by_url:
            e = by_url.pop(f["url"])
            e.update(seen_in_video=True, video_time=f.get("video_time", ""), evidence=f.get("evidence", ""),
                     purpose=f.get("purpose", ""))
            merged.append(e)
        else:
            merged.append({"url": f["url"], "source": "video", "seen_in_video": True,
                           "video_time": f.get("video_time", ""), "evidence": f.get("evidence", ""),
                           "purpose": f.get("purpose", "")})
    for i, p in enumerate(user):                      # typed pages the AI did not see, at their own position
        if p["url"] in by_url:
            merged.insert(min(i, len(merged)), by_url.pop(p["url"]))
    if user:
        start = next((m for m in merged if m["url"] == user[0]["url"]), None)
        if start is not None and merged[0] is not start:
            merged.remove(start)
            merged.insert(0, start)
    return merged


def _pages_from_video(s: Dict[str, Any], note: str = "") -> None:
    """9.4: the WATCH_URLS agent reads the page addresses from the recording; they join the user's typed ones and
    wait for the user's check before Build."""
    if not (s.get("media") or {}).get("path"):
        return
    user = [p for p in s.get("pages") or [] if p.get("source") == "user"] or \
        [{"url": u, "source": "user", "seen_in_video": False} for u in target_urls_of(s)]
    if not ai_available() and user:
        return                                    # without AI the typed addresses are all there is (no extra call)
    _telemetry(s, "WATCH", "ACTUAL_AI_API", "Reading the page addresses from the recording",
               ", ".join(p["url"] for p in user)[:300] or "no address typed", display_model())
    res = watch.pages_from_video(s["media"]["path"], [p["url"] for p in user], s.get("sop_text") or "", note)
    for r in res["rejected"]:
        if r.get("kind") == "blocked":
            _security_event(s, "URL", "UNSAFE_URL", r["url"], r["reason"])
    removed = set(s.get("pages_removed") or [])
    for f in [f for f in res["found"] if f["url"] in removed]:     # the user took it out: the AI does not re-add it
        res["found"].remove(f)
        res["rejected"].append({"url": f["url"], "video_time": f.get("video_time", ""), "kind": "removed",
                                "reason": "you removed this page - add it again if the process needs it"})
    s["pages_rejected"] = res["rejected"]
    s["pages_meta"] = {"agent_source": res["agent_source"], "model": res["model"], "notes": res["notes"],
                       "error": res["error"], "at": _now()}
    if res["error"]:
        _activity(s, "STEP", "Could not read the page addresses from the recording", res["error"][:300], level="warn")
        return
    merged = _merge_video_pages(user, res["found"])
    _apply_pages(s, merged)
    new = [p for p in merged if p.get("source") == "video"]
    s["pages_confirmed"] = not new
    _telemetry(s, "WATCH", res["agent_source"],
               f"Found {len(res['found'])} page address(es) in the recording" + (f", {len(new)} new" if new else ""),
               " -> ".join(p["url"] for p in merged)[:400] + (f" | not usable: {len(res['rejected'])}" if res["rejected"]
                                                              else ""), res["model"] or "Page-address agent")


def _combine_urls(target_url: Any, target_urls: Any) -> List[str]:
    out: List[str] = []
    for u in parse_url_list(target_url) + parse_url_list(target_urls):
        if u not in out:
            out.append(u)
    return out


def start_session(sid: str, target_url: Any, sop_text: str, media_tmp_path: Optional[str],
                  media_filename: Optional[str], use_sample_media: bool = False, background: bool = True,
                  context_refs: Optional[List[Dict[str, Any]]] = None,
                  target_urls: Optional[List[str]] = None) -> Dict[str, Any]:
    """STEP 1 for a draft session: screen the inputs, store the recording, resolve the chosen history context, and
    start WATCH. 9.4: ``target_url`` may be a list (or ``target_urls`` given) - the automation can visit several pages;
    the first URL is where it starts."""
    s = get(sid)
    if s.get("media") or s.get("analysis"):
        raise WizardError("Step 1 has already been submitted for this session. Create a new session for another run.",
                          409, {"session_id": sid})
    if (s.get("job") or {}).get("status") == "running":
        raise WizardError("Please wait - Step 1 is still running.", 409, {"session_id": sid})
    urls = _combine_urls(target_url, target_urls)
    target_url = urls[0] if urls else ""
    s["status"] = "active"
    _activity(s, "USER_ACTION", "User started Step 1 (Watch)",
              "Target URL" + (f"s: {', '.join(urls)}" if len(urls) > 1 else f": {target_url}"),
              {"target_url": target_url, "target_urls": urls, "sop_chars": len(sop_text or ""),
                                            "media_filename": media_filename, "use_sample_media": use_sample_media,
                                            "sop_text": (sop_text or "")[:4000],
                                            "history_refs": context_refs or []})
    # --- guardrails on every input (synchronous: fast, and the user needs immediate feedback) -------
    if len(urls) > MAX_TARGET_URLS:
        raise WizardError(f"Add at most {MAX_TARGET_URLS} page addresses ({len(urls)} given).", 400,
                          {"field": "target_urls", "session_id": sid})
    clean_urls: List[str] = []
    for i, u in enumerate(urls):
        uv = screen_url(u)
        if not uv.is_safe:
            _security_event(s, "URL", "UNSAFE_URL", str(u), uv.friendly_message)
            msg = uv.friendly_message if len(urls) < 2 else f"Page address {i + 1} ({str(u)[:80]}): {uv.friendly_message}"
            _set_pillar(s, "WATCH", "FAIL", msg, {"url_verdict": uv.to_dict(), "url_index": i})
            _persist(s)
            raise WizardError(msg, 400, {"field": "target_url" if i == 0 else "target_urls", "index": i,
                                         "session_id": sid})
        if uv.normalized_url not in clean_urls:
            clean_urls.append(uv.normalized_url)
    # 9.4: these are the pages the USER typed (optional with AI) - the AI adds the ones it sees in the recording
    _apply_pages(s, [{"url": u, "source": "user", "seen_in_video": False} for u in clean_urls])
    tv = screen_text(sop_text or "", "SOP")
    if not tv.is_safe:
        _security_event(s, "SOP", tv.pattern, tv.sanitized, tv.friendly_message)
        _set_pillar(s, "WATCH", "FAIL", tv.friendly_message, {"sop_verdict": tv.to_dict()})
        _persist(s)
        raise WizardError(tv.friendly_message, 400, {"field": "sop_text", "session_id": sid})
    s["sop_text"] = tv.sanitized
    if tv.warnings:
        s["notes"].extend(tv.warnings)
    _set_history(s, context_refs)
    media_dir = os.path.join(UPLOAD_ROOT, sid)
    os.makedirs(media_dir, exist_ok=True)
    if use_sample_media or not media_tmp_path:
        if not use_sample_media:
            raise WizardError("Please upload a video (or audio) recording of the process, or tick 'use the sample video'.",
                              400, {"field": "media", "session_id": sid})
        src, fname = SAMPLE_MEDIA, "demo_ap_recording.mp4"
    else:
        src, fname = media_tmp_path, media_filename or "recording.mp4"
    dest = os.path.join(media_dir, safe_filename(fname))
    shutil.copyfile(src, dest)
    mv = screen_media(dest, fname)
    if not mv.is_safe:
        os.remove(dest)
        _security_event(s, "MEDIA", "INVALID_MEDIA", fname, mv.friendly_message)
        _set_pillar(s, "WATCH", "FAIL", mv.friendly_message, {"media_verdict": mv.to_dict()})
        _persist(s)
        raise WizardError(mv.friendly_message, 400, {"field": "media", "session_id": sid})
    s["media"] = {**mv.to_dict(), "path": dest, "sample": bool(use_sample_media)}
    if not clean_urls and not ai_available() and not watch.learned_pages(dest, s["sop_text"]):
        # no AI and no skill learned for this recording: nothing can read the addresses, so ask now, not after a job
        shutil.rmtree(media_dir, ignore_errors=True)
        s.pop("media", None)
        why = unavailable_reason() or "AI is not available"
        msg = (f"Understudy can't read the page addresses from the recording without AI ({why.rstrip('.')}), and this "
               "recording hasn't been analysed with AI before. Add the address where the process starts, or set up "
               "Gemini in Mission Control.")
        raise WizardError(msg, 400, {"field": "target_url", "session_id": sid})
    _share_recording(s)
    _telemetry(s, "WATCH", "LOCAL_HEURISTIC", "Inputs cleared Model Armor ("
               + (f"{len(clean_urls)} URLs" if len(clean_urls) > 1 else "URL" if clean_urls else "no URL typed - the AI "
                  "reads them from the recording") + ", SOP text, media)",
               f"{mv.kind} {mv.size_bytes} bytes sha256={mv.sha256[:12]}", "Model Armor (wizard guardrails)")
    _run_job(s, "watch", lambda: _watch_job(s), background)
    return s


def _set_history(s: Dict[str, Any], refs: Optional[List[Dict[str, Any]]]) -> None:
    """Resolves the previous sessions / projects the user picked as context for this run (owner-scoped)."""
    from core.wizard import history
    if not refs:
        s["history_context"] = None
        return
    try:
        ctx = history.resolve(s.get("owner"), refs, exclude_session=s["session_id"])
    except history.HistoryError as e:
        raise WizardError(str(e), e.status_code, {"field": "context_refs", "session_id": s["session_id"]})
    for blk in ctx.get("blocked") or []:
        _security_event(s, "HISTORY", blk.get("pattern"), blk.get("excerpt", ""), blk.get("message", ""))
    s["history_context"] = ctx
    _activity(s, "USER_ACTION", f"User added the context of {len(ctx['sessions'])} previous session(s)",
              ", ".join(f"{r['project_name']} / {r['name']}" for r in ctx["sessions"])[:600],
              {"refs": ctx["refs"], "sessions": [r["session_id"] for r in ctx["sessions"]], "chars": ctx["chars"]})
    _telemetry(s, "WATCH", "LOCAL_HEURISTIC",
               f"History context: {len(ctx['sessions'])} previous session(s) ({ctx['chars']} characters) will be "
               f"given to the WATCH agent", "; ".join(r["name"] for r in ctx["sessions"])[:300], "History context")


def set_history_context(sid: str, refs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Change the chosen history context after Step 1 (applied by the next 'Tell the AI' re-analysis)."""
    s = get(sid)
    _set_history(s, refs)
    _persist(s)
    return s


def rename_project_in_sessions(project_id: str, project_name: str) -> None:
    from core.wizard import projects
    for row in [r for r in store.iter_docs(projects.INDEX) if r.get("project_id") == project_id]:
        s = SESSIONS.get(row["session_id"]) or _restore(row["session_id"])
        if s:
            s["project_name"] = project_name
            _register_context(s)
            _persist(s)


def delete_session(sid: str, owner: Optional[str] = None) -> Dict[str, Any]:
    from core.wizard import projects
    s = get(sid)
    if owner is not None and s.get("owner") != owner:
        raise WizardError("Session not found", 404)
    if (s.get("job") or {}).get("status") == "running":
        raise WizardError("This session is still working. Wait for it to finish, then delete it.", 409)
    _activity(s, "USER_ACTION", f"User deleted the session {context_label(s)}", "")
    SESSIONS.pop(sid, None)
    GENERATED_ZIP_CACHE.pop(sid, None)
    PIPELINE_RUN_REGISTRY.pop(sid, None)
    try:
        store.get_store().delete("sessions", sid)
    except Exception:
        pass
    projects.unindex_session(sid)
    for d in (os.path.join(WORK_ROOT, sid), os.path.join(UPLOAD_ROOT, sid)):
        shutil.rmtree(d, ignore_errors=True)
    return {"deleted": sid}


def _watch_job(s: Dict[str, Any], note: str = ""):
    _pages_from_video(s, note)
    if not target_urls_of(s):
        err = (s.get("pages_meta") or {}).get("error")
        if err:
            # the AI call itself failed (key, quota, network, invalid answer) - say that, not "address not visible"
            msg = (f"Understudy couldn't read the page addresses from your recording: {str(err)[:300].rstrip('.')}. "
                   "Fix that (Mission Control > AI) and click 'Watch again', or add the address where the process starts "
                   "under 'Pages'.")
        else:
            msg = ("The AI could not read any page address in your recording (the address bar may not be visible). Add "
                   "the address where the process starts under 'Pages', then click 'Watch again'.")
        _set_pillar(s, "WATCH", "FAIL", msg, {"needs_pages": True, "ai_error": err})
        raise WizardError(msg, 422, {"field": "pages", "needs_pages": True})
    src = "ACTUAL_AI_API" if ai_available() else "LOCAL_HEURISTIC"
    _telemetry(s, "WATCH", src, "Watching the recording together with the SOP and the target page"
               if src == "ACTUAL_AI_API" else f"AI not available ({unavailable_reason()}) - checking the skills learned in "
                                              "earlier AI sessions, then reading the target page's form",
               s["target_url"], display_model() if src == "ACTUAL_AI_API" else "DOM discovery")
    try:
        out = watch.analyze_inputs(s["target_url"], s["sop_text"], s["media"]["path"], s["media"]["kind"],
                                   history=s.get("history_context"), extra_urls=target_urls_of(s)[1:], page_note=note)
    except GuardrailViolation as e:
        _set_pillar(s, "WATCH", "FAIL", e.verdict.friendly_message.replace("media transcript", "video's on-screen/audio text"),
                    {"guardrail": e.verdict.to_dict()})
        raise
    s["inventory"] = {"url": out["inventory"].get("url"), "title": out["inventory"].get("title"),
                      "fields": len([e for e in out["inventory"].get("elements", []) if e.get("kind") not in ("button", "link")]),
                      "buttons": len([e for e in out["inventory"].get("elements", []) if e.get("kind") == "button"]),
                      "links": len([e for e in out["inventory"].get("elements", []) if e.get("kind") == "link"]),
                      "forms": len(out["inventory"].get("forms") or []),
                      "error": out["inventory"].get("error"), "renderer": out["inventory"].get("renderer")}
    s["notes"].extend(out["notes"])
    for prob in out["security_problems"]:
        if prob.get("pattern") not in ("INVALID_KEY", "DUPLICATE_KEY", "INVALID_TYPE"):
            _security_event(s, "MEDIA_TRANSCRIPT", prob.get("pattern"), prob.get("message", ""), prob.get("message", ""))
    if not out["analysis"]:
        _set_pillar(s, "WATCH", "FAIL", " ".join(out["notes"])[-400:], {"inventory": s["inventory"]})
        raise WizardError(" ".join(out["notes"])[-400:], 422)
    s["analysis"] = out["analysis"]
    s["framework_options"] = out["analysis"].pop("framework_options")
    s["parameters"] = out["analysis"]["parameters"]
    s["analysis_meta"] = {"agent_source": out["agent_source"], "model": out["model"], "ai_error": out.get("ai_error"),
                          "ai_configured": ai_available(), "recall": out.get("recall")}
    _detect_tool_requirement(s)
    _note_history_use(s, out)
    _telemetry(s, "WATCH", out["agent_source"],
               f"Reconstructed {len(s['analysis']['steps'])} steps and {len(s['parameters'])} parameters; "
               f"suggested {len(s['framework_options'])} frameworks", s["analysis"].get("process_name", ""), out["model"])
    _auto_name(s)
    _probe_and_assess(s)


def _unique_name(owner: Optional[str], project_id: Optional[str], base: str, except_sid: str = "") -> str:
    from core.wizard import projects
    base = base[:72].strip() or "New session"
    if not project_id:
        return base
    name, n = base, 2
    while projects.session_name_taken(owner, project_id, name, except_sid):
        name, n = f"{base} ({n})", n + 1
    return name


def _auto_name(s: Dict[str, Any]) -> None:
    """9.1: name the session from what WATCH understood (like a chat title). Only while the name is still the automatic
    one - a name the user typed or renamed is never touched."""
    from core.wizard import projects
    if not s.get("name_auto"):
        return
    a = s.get("analysis") or {}
    host = (urlparse(s.get("target_url") or "").hostname or "").replace("www.", "")
    existing = []
    if s.get("project_id"):
        existing = [r.get("name") for r in projects.sessions_of(s.get("owner"), s["project_id"])
                    if r["session_id"] != s["session_id"]][:30]
    title, src, model = "", "LOCAL_HEURISTIC", "Session namer"
    res = generate_json("SESSION_TITLE", {
        "process_name": a.get("process_name") or "", "summary": (a.get("summary") or "")[:500],
        "steps": "\n".join(f"{st.get('order')}. {st.get('action')} {st.get('label') or st.get('description') or ''}"
                           for st in (a.get("steps") or [])[:15]),
        "site": host, "page_title": (s.get("inventory") or {}).get("title") or "",
        "sop": (s.get("sop_text") or "")[:800], "existing": ", ".join(x for x in existing if x) or "(none)"},
        untrusted=["process_name", "summary", "steps", "page_title", "sop"])
    if res.ok and isinstance(res.data, dict):
        title = re.sub(r"\s+", " ", str(res.data.get("title") or "")).strip().strip("\"'.").strip()
        src, model = res.agent_source, res.model
    if not title or len(title) > 80:
        pn = re.sub(r"\s+", " ", str(a.get("process_name") or "")).strip()
        title = (pn[:1].upper() + pn[1:]) if pn else (f"{host} automation" if host else "")
        src, model = "LOCAL_HEURISTIC", "Session namer"
    if not title:
        return
    try:
        title = projects.clean_name(title, "Session Name")
    except projects.ProjectError:
        return
    title = _unique_name(s.get("owner"), s.get("project_id"), title, s["session_id"])
    old = s.get("session_name")
    s["session_name"] = title
    s["name_source"] = src
    _register_context(s)
    _sync_report(s)
    _telemetry(s, "WATCH", src, f"Named the session '{title}'", f"was '{old}'" if old else "", model)
    _persist(s)


def rename_session(sid: str, name: str) -> Dict[str, Any]:
    """9.1: the user renames a session (any time). The AI never renames it again."""
    from core.wizard import projects
    s = get(sid)
    try:
        new = projects.clean_name(name, "Session Name")
    except projects.ProjectError as e:
        raise WizardError(str(e), e.status_code, e.details)
    if s.get("project_id") and projects.session_name_taken(s.get("owner"), s["project_id"], new, sid):
        raise WizardError(f"This project already has a session called '{new}'.", 409, {"field": "session_name"})
    old = s.get("session_name")
    s.update(session_name=new, name_auto=False, name_source="USER")
    _register_context(s)
    _sync_report(s)
    _activity(s, "USER_ACTION", f"User renamed the session to '{new}'", f"was '{old}'" if old else "",
              {"session_name": new})
    _persist(s)
    return s


def _probe_and_assess(s: Dict[str, Any]) -> None:
    """9.4: automation-aware inspection. Look at what the automation does (its steps, pages, secrets, SOP), decide
    which checks it needs (INSPECTION_PLAN agent, rules offline), open each page ONCE to run them - reloading a page
    only when the plan or the first load says its ids / order must be compared - re-anchor the step plan when needed,
    and score how complex this automation is. The score goes to the build / heal agent too."""
    from core.wizard import build_agent, complexity, inspection, page_probe
    prof = None
    urls = target_urls_of(s)
    try:
        the_plan = inspection.plan(s, urls)                    # what THIS automation needs checked, and why
        s["inspection_plan"] = the_plan
        _telemetry(s, "WATCH", the_plan.get("agent_source", "LOCAL_HEURISTIC"),
                   "Inspection plan: " + "; ".join(
                       f"page {p['index']}: " + ", ".join(inspection.CHECK_TITLES.get(c, c) for c in p["checks"])
                       for p in the_plan["pages"])[:300],
                   f"Reload to compare ids / order: {the_plan.get('stability')} - {the_plan.get('stability_why')}"[:300],
                   the_plan.get("model") or "Inspection rules")
        if build_agent.probing_enabled():
            prof = inspection.inspect(urls, the_plan, (s.get("analysis") or {}).get("steps") or [],
                                      sop=s.get("sop_text") or "")
    except Exception as e:                                       # never block WATCH over the inspection
        _activity(s, "STEP", "Could not inspect the target page(s)", f"{type(e).__name__}: {e}", level="warn")
    s["page_profile"] = prof
    if prof and prof.get("renderer") == "chromium":
        n = page_probe.ground_steps(s["analysis"]["steps"], prof)
        sig = prof.get("signals") or {}
        dynamic = sig.get("dynamic_ids") or sig.get("dynamic_names") or sig.get("shuffled_fields")
        if dynamic and not n and (s.get("analysis_meta") or {}).get("agent_source") != "ACTUAL_AI_API":
            n = _plan_from_probe(s, prof)
            if n:
                inspection.recount_targets(prof, s["analysis"]["steps"], urls)
        _telemetry(s, "WATCH", "LOCAL_HEURISTIC", inspection.summary_line(prof),
                   page_probe.strategy_summary(prof) + (f" {n} step selector(s) re-anchored on visible labels." if n else ""),
                   "Automation-aware inspection")
    try:
        s["complexity"] = complexity.assess(s, prof)
        c = s["complexity"]
        _telemetry(s, "WATCH", c.get("agent_source", "LOCAL_HEURISTIC"),
                   f"Automation complexity: {c['level']} ({c['score']}/10)", c.get("summary", "")[:300],
                   c.get("model") or "Complexity assessment")
    except Exception as e:
        s["complexity"] = None
        _activity(s, "STEP", "Could not assess the automation's complexity", f"{type(e).__name__}: {e}", level="warn")
    _assess_difficulty(s, prof)


def _assess_difficulty(s: Dict[str, Any], prof: Optional[Dict[str, Any]]) -> None:
    """9.8: the Automation Difficulty Score (ten parameters) and the traditional / agentic recommendation."""
    from core.wizard import difficulty
    from core.wizard.frameworks import CATALOG
    from core.wizard.admin_policy import BUILTIN_TOOLS
    from core.wizard.agent_stacks import is_understudy_agent
    # 9.9: the agentic tools are those on Understudy's agent (built in + admin-built), next to the AI's suggestions
    opts = [o for o in s.get("framework_options") or []
            if (o.get("category") or "").lower() != "agentic" or is_understudy_agent(o)]
    have = {o.get("id") for o in opts}
    if admin_policy.builtin_agent_hidden(s.get("owner")):   # 9.9: the admin's own agentic tools replace the built-in one
        opts = [o for o in opts if not admin_policy.is_builtin(o.get("id") or "")]
    else:
        opts += [dict(CATALOG[b].to_dict(), recommended=False) for b in BUILTIN_TOOLS if b in CATALOG and b not in have]
    for tid, entry in admin_policy.whitelist().items():          # the agentic tools the admin set up on the agent
        if tid not in have and tid not in BUILTIN_TOOLS and is_understudy_agent(entry.get("spec") or {}) \
                and admin_policy.user_may_use(tid, s.get("owner")):
            opts.append(dict(entry["spec"], recommended=False))
    s["framework_options"] = opts
    try:
        s["difficulty"] = difficulty.assess(s, prof)
        difficulty.apply_to_complexity(s)
        d = s["difficulty"]
        rec = d["recommendation"]
        _telemetry(s, "WATCH", d.get("agent_source", "LOCAL_HEURISTIC"),
                   f"Automation difficulty: {d['score']}/10 ({d['level']}) - recommends {rec['approach']} automation",
                   (rec.get("summary") or "")[:300], d.get("model") or "Difficulty rules")
    except Exception as e:
        s["difficulty"] = None
        _activity(s, "STEP", "Could not score the automation's difficulty", f"{type(e).__name__}: {e}", level="warn")


def recalculate_difficulty(sid: str, changes: List[Dict[str, Any]], reset: bool = False) -> Dict[str, Any]:
    """9.8: the user tuned the difficulty parameters (values / weights) - recompute the score and the recommendation."""
    from core.wizard import difficulty
    s = get(sid)
    _require_not_pending(s)
    if _watch_running(s):
        raise WizardError("Step 1 is still running - wait for it to finish.", 409)
    with _lock(sid):
        try:
            if reset:
                difficulty.reset(s, by=s.get("owner") or "local")
                what = ["reset to the assessed values and default weights"]
            else:
                _d, what = difficulty.recalculate(s, changes, by=s.get("owner") or "local")
        except ValueError as e:
            raise WizardError(str(e), 400 if s.get("difficulty") else 409)
        difficulty.apply_to_complexity(s)
        d = s["difficulty"]
        _activity(s, "USER_ACTION", f"User recalculated the difficulty: {d['score']}/10 ({d['level']}), recommends "
                  f"{d['recommendation']['approach']}", "; ".join(what)[:600] or "no change",
                  {"changes": what, "score": d["score"], "approach": d["recommendation"]["approach"]})
        _telemetry(s, "WATCH", d["recommendation"].get("source", "LOCAL_HEURISTIC"),
                   f"Difficulty recalculated by the user: {d['score']}/10 - recommends {d['recommendation']['approach']}",
                   "; ".join(what)[:300], d["recommendation"].get("model") or "Routing rules")
        _persist(s)
    return s


def _watch_running(s: Dict[str, Any]) -> bool:
    job = s.get("job") or {}
    return job.get("status") == "running" and job.get("name") in ("watch", "clarify", "pages")


def _plan_from_probe(s: Dict[str, Any], prof: Dict[str, Any]) -> int:
    """9.0, offline WATCH on a page whose ids / order change (rpachallenge-style): the form reader keyed the fields by
    their random ids, which are gone on the next load. Rebuild the field steps and the payload from the probe's visible
    labels; the submit click is anchored on the button's text."""
    from core.wizard import page_probe
    urls = target_urls_of(s)
    # 9.4: only the START page is rebuilt; the steps on the other pages the user listed are kept as they are
    first_only = dict(prof, fields=[f for f in prof.get("fields") or [] if f.get("page", 1) == 1])
    plan = page_probe.plan_from_profile(first_only, s.get("sop_text") or "")
    if not plan:
        return 0
    old = s["analysis"]["steps"]
    tail: List[Dict[str, Any]] = []
    if len(urls) > 1:
        cut = next((i for i, st in enumerate(old) if i > 0 and st.get("action") == "navigate" and st.get("value")), None)
        if cut is not None:
            old, tail = old[:cut], old[cut:]
    steps = plan["steps"]
    for st in old:
        if st.get("action") in ("fill", "select", "check", "navigate"):
            continue
        st = dict(st)
        if st.get("action") == "click":
            act = next((f for f in prof.get("fields") or [] if f["kind"] == "action" and f.get("label")
                        and re.search(r"(?i)submit|save|send|post|create|add|ok", f["label"])), None)
            if act:
                lab = act["label"].replace("'", "")
                st.update(selector=f"//input[(@type='submit' or @type='button') and normalize-space(@value)='{lab}']"
                                   f" | //button[normalize-space()='{lab}']", label=act["label"][:80],
                          fallback_selectors=[st.get("selector")] if st.get("selector") else [],
                          grounded_by="page probe: the button's own text")
        steps.append(st)
    steps.extend(tail)
    for i, st in enumerate(steps, 1):
        st["order"] = i
    tail_keys = {st.get("param_key") for st in tail if st.get("param_key")}
    kept = [p for p in s["analysis"].get("parameters") or [] if p["key"] in tail_keys]
    new_keys = {p["key"] for p in plan["parameters"]}
    params = plan["parameters"] + [p for p in kept if p["key"] not in new_keys]
    s["analysis"]["steps"] = steps
    s["analysis"]["parameters"] = params
    s["parameters"] = copy.deepcopy(params)
    ver = s["analysis"].get("verification")
    if isinstance(ver, dict):
        ver["expect_param_keys"] = [p["key"] for p in plan["parameters"] if p.get("value")][:4]
    s["notes"].append("The page changes its field ids / order on every load, so the steps and payload keys were rebuilt "
                      "from the fields' visible labels (page probe).")
    return len(plan["parameters"])


def _note_history_use(s: Dict[str, Any], out: Dict[str, Any]) -> None:
    ctx = s.get("history_context")
    if not ctx:
        return
    applied = [str(x)[:300] for x in (out["analysis"].get("history_applied") or [])][:20]
    ctx["applied"] = applied
    ctx["used_by"] = out["agent_source"]
    _activity(s, "STEP", f"WATCH used the context of {len(ctx['sessions'])} previous session(s)",
              "; ".join(applied)[:1500] or "No specific reuse reported.", {"applied": applied}, stage="WATCH",
              source=out["agent_source"])


MAX_CLARIFICATION_CHARS = 4000


def clarify_watch(sid: str, feedback: str, background: bool = True) -> Dict[str, Any]:
    """The user explains what the AI misunderstood or missed. The correction is screened by Model Armor, added to the
    correction history, and WATCH runs again with ALL earlier corrections as context (like self-healing keeps its
    healing notes). Can be repeated until the user confirms the understanding."""
    s = get(sid)
    if not s.get("analysis"):
        raise WizardError("The recording has not been analysed yet.", 409)
    text = str(feedback or "").strip()
    if not text:
        raise WizardError("Please describe what the AI missed or got wrong.", 400, {"field": "feedback"})
    if len(text) > MAX_CLARIFICATION_CHARS:
        raise WizardError(f"Please keep the explanation under {MAX_CLARIFICATION_CHARS} characters.", 400,
                          {"field": "feedback"})
    tv = screen_text(text, "CLARIFICATION")
    if not tv.is_safe:
        _security_event(s, "CLARIFICATION", tv.pattern, text, tv.friendly_message)
        raise WizardError(tv.friendly_message, 400, {"field": "feedback", "security": True})
    history = s.setdefault("clarifications", [])
    history.append({"round": len(history) + 1, "text": tv.sanitized, "at": _now(), "status": "pending"})
    s["understanding_confirmed"] = False
    _activity(s, "USER_ACTION", f"User corrected the AI's understanding (round {len(history)})", tv.sanitized,
              {"round": len(history)})
    _run_job(s, "clarify", lambda: _clarify_job(s), background)
    return s


def _clarify_job(s: Dict[str, Any]):
    history = s["clarifications"]
    corrections = [c["text"] for c in history]
    _telemetry(s, "WATCH", "ACTUAL_AI_API" if ai_available() else "LOCAL_HEURISTIC",
               f"Re-analysing with the user's correction (round {len(history)})", corrections[-1][:300],
               display_model() if ai_available() else "Correction parser")
    previous = dict(s["analysis"], framework_options=s.get("framework_options") or [])
    try:
        out = watch.refine_analysis(s["target_url"], s["sop_text"], s["media"]["path"], previous, s["parameters"],
                                    corrections, history=s.get("history_context"), extra_urls=target_urls_of(s)[1:])
    except GuardrailViolation as e:
        history[-1]["status"] = "blocked"
        _set_pillar(s, "WATCH", "FAIL", e.verdict.friendly_message, {"guardrail": e.verdict.to_dict()})
        raise
    for prob in out["security_problems"]:
        if prob.get("pattern") not in ("INVALID_KEY", "DUPLICATE_KEY", "INVALID_TYPE"):
            _security_event(s, "MEDIA_TRANSCRIPT", prob.get("pattern"), prob.get("message", ""), prob.get("message", ""))
    old_secrets = {p["key"]: p.get("value") for p in s["parameters"] if p.get("type") == "secret" and p.get("value")}
    analysis = out["analysis"]
    options = analysis.pop("framework_options")
    for p in analysis["parameters"]:
        if p.get("type") == "secret" and p["key"] in old_secrets:
            p["value"] = old_secrets[p["key"]]          # a secret the user already entered survives the re-analysis
    s["analysis"] = analysis
    s["parameters"] = analysis["parameters"]
    s["framework_options"] = options
    s["analysis_meta"] = {"agent_source": out["agent_source"], "model": out["model"], "ai_error": out.get("ai_error"),
                          "ai_configured": ai_available(), "recall": out.get("recall")}
    _note_history_use(s, out)
    history[-1].update(status="applied" if not out["notes"] else "not_applied",
                       agent_source=out["agent_source"], notes=out["notes"], summary=analysis.get("summary", "")[:600])
    s["notes"].extend(out["notes"])
    # Everything downstream was built on the old understanding.
    _reset_from(s, "WATCH")
    s["current_step"] = 1
    _probe_and_assess(s)             # 9.4: the steps changed - so may what the inspection must check, and the score
    if s.get("framework"):
        spec = _spec(s)
        _apply_learned_skills(s, spec)
        _set_pillar(s, "WATCH", "PASS",
                    f"Re-analysed after {len(history)} correction(s) ({out['agent_source']}), "
                    f"{len(s['analysis']['steps'])} steps, {len(s['parameters'])} parameters; framework '{spec.name}'.",
                    {"analysis": {k: s["analysis"][k] for k in ("process_name", "steps", "verification")},
                     "framework": spec.id, "clarifications": len(history)})
        _params_job(s)
    _telemetry(s, "WATCH", out["agent_source"],
               f"Updated understanding after correction {len(history)}: {len(s['analysis']['steps'])} steps, "
               f"{len(s['parameters'])} parameters", s["analysis"].get("summary", "")[:300], out["model"])


def confirm_watch(sid: str) -> Dict[str, Any]:
    s = get(sid)
    if not s.get("analysis"):
        raise WizardError("The recording has not been analysed yet.", 409)
    s["understanding_confirmed"] = True
    s["pages_confirmed"] = True
    _activity(s, "USER_ACTION", "User confirmed the AI understood the process",
              f"after {len(s.get('clarifications') or [])} correction(s)")
    _persist(s)
    return s


# ---------------------------------------------------------------------------
# 9.4: the pages the AI read from the recording - the user checks, edits, and can ask the AI to watch again
# ---------------------------------------------------------------------------
def confirm_pages(sid: str) -> Dict[str, Any]:
    s = get(sid)
    if not target_urls_of(s):
        raise WizardError("Add at least the address where the process starts.", 400, {"field": "pages"})
    s["pages_confirmed"] = True
    _activity(s, "USER_ACTION", "User confirmed the pages the automation visits", " -> ".join(target_urls_of(s))[:600])
    _persist(s)
    return s


def set_pages(sid: str, urls: List[str], rewatch: bool = False, note: str = "", background: bool = True) -> Dict[str, Any]:
    """The user's edited page list (order = the order the process visits them). Every address is screened. ``rewatch``
    asks the AI to watch the recording again with these pages (and ``note``); otherwise the steps keep their
    understanding and only the pages, the inspection and the complexity are refreshed."""
    s = get(sid)
    _require_not_pending(s)
    if not s.get("media"):
        raise WizardError("Start Step 1 first.", 409)
    if (s.get("job") or {}).get("status") == "running":
        raise WizardError("Please wait - Step 1 is still running.", 409)
    urls = parse_url_list(urls)
    if not urls:
        raise WizardError("Add at least the address where the process starts.", 400, {"field": "pages"})
    if len(urls) > MAX_TARGET_URLS:
        raise WizardError(f"Add at most {MAX_TARGET_URLS} page addresses ({len(urls)} given).", 400, {"field": "pages"})
    old = {p["url"]: p for p in s.get("pages") or []}
    clean: List[Dict[str, Any]] = []
    for i, u in enumerate(urls):
        uv = screen_url(u)
        if not uv.is_safe:
            _security_event(s, "URL", "UNSAFE_URL", str(u), uv.friendly_message)
            raise WizardError(f"Page address {i + 1} ({str(u)[:80]}): {uv.friendly_message}", 400,
                              {"field": "pages", "index": i})
        if any(c["url"] == uv.normalized_url for c in clean):
            continue
        prev = old.get(uv.normalized_url)
        # a page the user kept is now theirs (they checked it); seen-in-video details stay for reference
        clean.append(dict(prev, source="user") if prev else {"url": uv.normalized_url, "source": "user",
                                                              "seen_in_video": False})
    nv = screen_text(note or "", "SOP") if note else None
    if nv is not None and not nv.is_safe:
        _security_event(s, "SOP", nv.pattern, nv.sanitized, nv.friendly_message)
        raise WizardError(nv.friendly_message, 400, {"field": "note"})
    old_urls = target_urls_of(s)
    kept = {c["url"] for c in clean}
    s["pages_removed"] = sorted((set(s.get("pages_removed") or []) | (set(old_urls) - kept)) - kept)[:40]
    _apply_pages(s, clean)
    s["pages_confirmed"] = True
    _activity(s, "USER_ACTION", "User " + ("asked the AI to watch the recording again with these pages" if rewatch
                                           else "changed the pages the automation visits"),
              " -> ".join(s["target_urls"])[:600] + (f" | note: {nv.sanitized[:300]}" if nv else ""),
              {"pages": s["target_urls"], "before": old_urls, "rewatch": rewatch})
    if rewatch or not s.get("analysis"):
        _run_job(s, "watch", lambda: _rewatch_job(s, nv.sanitized if nv else ""), background)
    else:
        _run_job(s, "pages", lambda: _pages_job(s, old_urls), background)
    return s


def _rewatch_job(s: Dict[str, Any], note: str) -> None:
    old_secrets = {p["key"]: p.get("value") for p in s.get("parameters") or [] if p.get("type") == "secret" and p.get("value")}
    _watch_job(s, note)
    for p in s.get("parameters") or []:
        if p.get("type") == "secret" and p["key"] in old_secrets:
            p["value"] = old_secrets[p["key"]]
    s["pages_confirmed"] = not any(p.get("source") == "video" for p in s.get("pages") or [])
    _after_understanding_changed(s, "Watched the recording again with the user's pages")


def _pages_job(s: Dict[str, Any], old_urls: List[str]) -> None:
    """Pages edited without re-watching: navigate steps follow the edit (page n's old address -> its new one), then the
    inspection and the complexity are redone for the new pages."""
    new_urls = target_urls_of(s)
    remap = {o.rstrip("/"): new_urls[i] for i, o in enumerate(old_urls) if i < len(new_urls) and o not in new_urls}
    for st in (s.get("analysis") or {}).get("steps") or []:
        v = str(st.get("value") or "").rstrip("/")
        if st.get("action") == "navigate" and v in remap:
            st["value"] = remap[v]
    _probe_and_assess(s)
    _after_understanding_changed(s, "Updated the pages the automation visits")


def _after_understanding_changed(s: Dict[str, Any], what: str) -> None:
    """Everything after WATCH was built on the old understanding (like a WATCH correction)."""
    s["understanding_confirmed"] = False
    _reset_from(s, "WATCH")
    s["current_step"] = 1
    if s.get("framework") and s.get("analysis"):
        spec = _spec(s)
        _apply_learned_skills(s, spec)
        _set_pillar(s, "WATCH", "PASS", f"{what}: {len(target_urls_of(s))} page(s), {len(s['analysis']['steps'])} "
                                        f"steps, {len(s['parameters'])} parameters; framework '{spec.name}'.",
                    {"pages": target_urls_of(s), "framework": spec.id})
        _params_job(s)


def _resolve_choice(s: Dict[str, Any], framework_id: Optional[str], custom_name: Optional[str]):
    """Returns (validated spec, meta) for a suggested/catalog id or any typed tool name."""
    meta: Dict[str, Any] = {}
    if custom_name:
        spec, meta = resolve_custom_framework(custom_name, s["analysis"].get("app_type", "web_ui"))
        if meta.get("blocked"):
            _security_event(s, "TOOL_NAME", meta.get("pattern"), custom_name, meta["message"])
        if not spec:
            raise WizardError(meta.get("message", "Could not set up that tool."), 422)
        return spec, meta
    approved = admin_policy.allowed_spec(framework_id or "", user=s.get("owner"))
    if approved:
        return approved, {"message": "Admin-approved tool"}
    opt = next((o for o in s["framework_options"] if o["id"] == framework_id), None)
    if not opt and framework_id in CATALOG:
        opt = CATALOG[framework_id].to_dict()
    if not opt:
        raise WizardError(f"Unknown framework '{framework_id}'.", 404)
    try:
        return validate_spec(FrameworkSpec.from_dict(opt)), meta
    except FrameworkValidationError as e:
        raise WizardError(f"Framework setup failed safety validation: {e}", 422)


_TYPED_VERSION_RX = re.compile(r"^(?P<name>.*?[A-Za-z].*?)\s*(?:@|==|\s)\s*v?(?P<version>\d+(?:\.\d+){0,2})\s*$")


def split_typed_version(custom_name: Optional[str], version: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """'Cypress 13.6', 'cypress@13.6.6', 'selenium==4.25' typed in the tool box -> (name, version).
    An explicit ``version`` wins; names that end in digits but have no separator (e.g. 'k6') are left alone."""
    if not custom_name or version:
        return custom_name, version
    m = _TYPED_VERSION_RX.match(custom_name.strip())
    if not m:
        return custom_name, version
    return m.group("name").strip(), m.group("version")


def select_framework(sid: str, framework_id: Optional[str] = None, custom_name: Optional[str] = None,
                     background: bool = True, version: Optional[str] = None,
                     override_sop_tool: bool = False) -> Dict[str, Any]:
    """Selects a tool. Only tools on the admin whitelist can be used; anything else needs an access request.
    8.0: when the SOP requires a tool, another tool needs ``override_sop_tool`` (the user confirms it)."""
    custom_name, version = split_typed_version(custom_name, version)
    s = get(sid)
    if not s.get("analysis"):
        raise WizardError("The recording has not been analysed yet.", 409)
    spec, meta = _resolve_choice(s, framework_id, custom_name)
    req = s.get("tool_requirement")
    if req and spec.id != req.get("tool_id") and not override_sop_tool:
        from core.wizard import tool_requirements
        raise WizardError(f"Your SOP requires {req['name']}" + (f" {req['version']}" if req.get("version") else "")
                          + f" (\"{req.get('quote', '')[:120]}\"). Use it, or confirm that you want {spec.name} instead.",
                          409, {"sop_tool_required": dict(req, **tool_requirements.status(req)), "chosen": spec.id})
    if req and spec.id == req.get("tool_id") and req.get("version") and not version:
        allowed = admin_policy.tool_version_policy(spec.id, s.get("owner"))["allowed"]
        version = next((a for a in allowed if a == req["version"] or a.startswith(req["version"] + ".")), None)
    if req and spec.id != req.get("tool_id") and override_sop_tool and not req.get("overridden"):
        req["overridden"] = {"by": s.get("owner") or "local", "tool": spec.id, "at": _now()}
        _activity(s, "USER_ACTION", f"User chose {spec.name} although the SOP requires {req['name']}",
                  req.get("quote", ""), {"requirement": req, "chosen": spec.id}, level="warn")
    _activity(s, "USER_ACTION", f"User picked the tool {spec.name}" + (f" version {version}" if version else ""), "",
              {"framework_id": spec.id, "custom_name": custom_name, "version": version})
    blk = admin_policy.tool_block_for(spec, custom_name or "", version)
    if blk and (not blk.get("versions") or version):
        what = spec.name + (f" {version}" if blk.get("versions") else "")
        raise WizardError(str(admin_policy.ToolBlocked(blk, what)), 403, {"blocked": True, "tool_id": spec.id})
    if not admin_policy.is_tool_allowed(spec.id):
        req = admin_policy.request_status_for(spec.id)
        raise WizardError(f"{spec.name} isn't on your admin's list of approved tools. Request access and you'll be able "
                          f"to use it once an admin approves it in Mission Control.", 403,
                          {"needs_approval": True, "tool_id": spec.id, "tool_name": spec.name, "request": req})
    if not admin_policy.user_may_use(spec.id, s.get("owner")):
        raise WizardError(admin_policy.access_denied_message(spec.name, spec.id) + " Ask your admin for access in "
                          "Mission Control > Access & tools, or pick another tool.", 403,
                          {"restricted": True, "tool_id": spec.id, "tool_name": spec.name})
    _check_unrestricted_version(spec.id, version)
    try:
        # always use the exact spec the admin approved, pinned to the chosen (or admin-default) version
        spec = admin_policy.allowed_spec(spec.id, version, user=s.get("owner")) or spec
    except admin_policy.PolicyError as e:
        raise WizardError(str(e), 403, {"versions": admin_policy.tool_version_policy(spec.id, s.get("owner")), "needs_version_approval": True,
                                        "tool_id": spec.id, "version": version})
    if s.get("pending_request"):
        _activity(s, "USER_ACTION", f"User continued with {spec.name} instead of waiting for the admin",
                  s["pending_request"].get("tool_name", ""), {"pending_request": s["pending_request"]})
        s["pending_request"] = None
        s["status"] = "active"
    s["framework"] = spec.to_dict()
    s["framework_base"] = spec.to_dict()  # approved + pinned spec, before the generated code adds extra packages
    from core.wizard import difficulty
    difficulty.refresh_fit(s)             # 9.9: Task Complexity-Solution Fit of the chosen tool
    s["dependency_lock"] = None
    s["runtime_env"] = {}                 # runtime repairs belong to the previously selected tool
    s["runtime_repairs"] = []
    s["strategy_override"] = None
    _apply_learned_skills(s, spec)
    if not any(o["id"] == spec.id for o in s["framework_options"]):
        s["framework_options"].append(spec.to_dict())
    _reset_from(s, "WATCH")
    _telemetry(s, "WATCH", meta.get("agent_source", "LOCAL_HEURISTIC"), f"Framework selected: {spec.name}",
               meta.get("message", ""), meta.get("model") or "Admin tool whitelist")
    # Admin approval covers the tool's dependencies (including packages outside the trusted list).
    start_dependency_install(s, approved=True, background=background)
    _set_pillar(s, "WATCH", "PASS",
                f"Analysed the recording ({s['analysis_meta']['agent_source']}), {len(s['analysis']['steps'])} steps, "
                f"{len(s['parameters'])} parameters; framework '{spec.name}' selected.",
                {"analysis": {k: s["analysis"][k] for k in ("process_name", "steps", "verification")},
                 "framework": spec.id, "media_sha256": s["media"]["sha256"]})
    s["current_step"] = 2
    _run_job(s, "prepare_parameters", lambda: _params_job(s), background)
    return s


def versions_for_user(framework_id: str, refresh: bool = False, user: Optional[str] = None) -> Dict[str, Any]:
    from core.wizard import tool_versions
    spec = admin_policy.allowed_spec(framework_id, user=user) if admin_policy.is_tool_allowed(framework_id) else None
    if spec is None:
        raise WizardError("This tool isn't approved by your admin.", 403)
    pol = admin_policy.tool_version_policy(framework_id, user)
    base = FrameworkSpec.from_dict(admin_policy.whitelist()[framework_id]["spec"])
    pkg = base.packages[0].name if base.packages else None
    if pol["allowed"]:
        return {"tool_id": framework_id, "package": pkg, "restricted": True, "versions": pol["allowed"],
                "default": pol["default"], "source": "admin", "latest": None, "error": None}
    info = tool_versions.available_versions(base, refresh=refresh, limit=100)
    return {"tool_id": framework_id, "package": pkg, "restricted": False, "versions": info["versions"],
            "default": None, "source": info["source"], "latest": info["latest"], "installed": info.get("installed"),
            "constraint": info.get("catalog_constraint"), "error": info.get("error")}


def _check_unrestricted_version(framework_id: str, version: Optional[str]):
    """No admin restriction: any version that really exists in the registry (when it can be reached)."""
    if not version or admin_policy.tool_version_policy(framework_id)["allowed"]:
        return
    from core.wizard import tool_versions
    try:
        tool_versions.validate_version(version)
    except ValueError as e:
        raise WizardError(str(e), 422)
    entry = admin_policy.whitelist().get(framework_id)
    if not entry:
        return
    info = tool_versions.available_versions(FrameworkSpec.from_dict(entry["spec"]), include_prereleases=True, limit=None)
    if info["source"] == "registry" and version not in info["versions"]:
        raise WizardError(f"Version {version} of {entry['spec'].get('name', framework_id)} doesn't exist in its package "
                          f"registry.", 422, {"versions": info["versions"][:20]})


def _apply_learned_skills(s: Dict[str, Any], spec: FrameworkSpec):
    """Apply what Understudy learned from earlier errors with this tool / on this target BEFORE the first run."""
    try:
        res = skills.apply_learned(spec.id, s["target_url"], s["analysis"]["steps"], s.get("runtime_env") or {},
                                   s.get("healing_notes") or [])
    except Exception as e:  # a broken skill file must never block a run
        _activity(s, "RUNTIME", "Could not read learned skills", str(e), level="warn")
        return
    s["applied_skills"] = res["applied"]
    if not res["applied"]:
        return
    s["runtime_env"] = res["runtime_env"]
    s["analysis"]["steps"] = res["steps"]
    s["healing_notes"] = res["healing_notes"]
    for a in res["applied"]:
        _activity(s, "RUNTIME", f"Applied learned skill {a['skill_id']}: {', '.join(a['applied'])[:200]}",
                  a["signature"], {"skill": a}, source="LEARNED_SKILL")
    _telemetry(s, "WATCH", "LOCAL_HEURISTIC", f"Applied {len(res['applied'])} learned skill lesson(s) from earlier runs",
               "; ".join(a["signature"] for a in res["applied"])[:400], "Learned skills")


def _placeholder_spec(name: str) -> FrameworkSpec:
    """A tool the user typed that neither the catalog nor the AI could set up yet (e.g. no AI key): the request still
    reaches the admin, and the admin's AI setup plan (core/wizard/tool_setup.py) works out how to install it."""
    slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:40] or "custom-tool"
    return validate_spec(FrameworkSpec(id=slug if len(slug) > 1 else "custom-tool", name=name[:80], language="",
                                       ecosystem="none", packages=[], run_command=["python", "{entry}"],
                                       entry_file="run_automation.py", category="other",
                                       beginner_summary="Not set up yet - the admin's AI setup will work out how to "
                                                        "install and run it.", source="user"))


def request_tool_access(sid: str, framework_id: Optional[str] = None, custom_name: Optional[str] = None,
                        reason: str = "", version: Optional[str] = None) -> Dict[str, Any]:
    """Asks the admin for a tool that isn't approved (any suggested, catalog or typed tool) or - with ``version`` - for
    a version of an approved tool that the admin hasn't allowed. Blocked tools / versions are refused (403).
    A typed name may carry the version ('Cypress 13.6')."""
    custom_name, version = split_typed_version(custom_name, version)
    s = get(sid)
    if not s.get("analysis"):
        raise WizardError("The recording has not been analysed yet.", 409)
    if reason:
        tv = screen_text(reason, "PAYLOAD")
        if not tv.is_safe:
            _security_event(s, "TOOL_REQUEST", tv.pattern, reason, tv.friendly_message)
            raise WizardError(tv.friendly_message, 400)
        reason = tv.sanitized
    try:
        spec, meta = _resolve_choice(s, framework_id, custom_name)
    except WizardError as e:
        if not custom_name or e.status_code != 422 or "safety" in str(e).lower() or "Model Armor" in str(e):
            raise
        tv = screen_text(custom_name, "TOOL_NAME")
        if not tv.is_safe:
            raise
        spec, meta = _placeholder_spec(tv.sanitized.strip()), {"agent_source": "user"}
    who = s.get("owner")
    try:
        if version and admin_policy.is_tool_allowed(spec.id):
            req = admin_policy.request_version(spec.id, version, s["session_id"], reason=reason, requested_by=who)
            what = f"{spec.name} {version}"
        else:
            from core.wizard import tool_versions
            want = version or ((s.get("tool_requirement") or {}).get("version")
                               if (s.get("tool_requirement") or {}).get("tool_id") == spec.id else None)
            if want:
                try:
                    want = tool_versions.validate_version(str(want).lstrip("=v"))
                except ValueError:
                    want = None
            req = admin_policy.request_tool(spec, s["session_id"], reason=reason, source=meta.get("agent_source", "user"),
                                            requested_by=who, typed_name=custom_name or "", version=want)
            what = spec.name
    except admin_policy.ToolBlocked as e:
        _activity(s, "USER_ACTION", f"User asked for {spec.name}{' ' + version if version else ''} - blocked by the admin",
                  str(e), level="warn")
        raise WizardError(str(e), 403, {"blocked": True, "tool_id": spec.id})
    except admin_policy.PolicyError as e:
        raise WizardError(str(e), 400)
    if not version and not any(o["id"] == spec.id for o in s["framework_options"]):
        s["framework_options"].append(dict(spec.to_dict(), source="requested"))
    _activity(s, "USER_ACTION", f"User requested access to {what}", reason, {"request": req})
    s.setdefault("tool_requests", [])
    if req.get("id") and req["id"] not in s["tool_requests"]:
        s["tool_requests"].append(req["id"])
    if req.get("id"):
        _pause_for_request(s, req, what, reason)
    _telemetry(s, "WATCH", "LOCAL_HEURISTIC",
               f"Access to {what} requested from the admin ({req.get('id') or 'already approved'})", reason,
               "Admin tool whitelist")
    _persist(s)
    return s


def _spec_version(spec) -> str:
    try:
        v = str(spec.packages[0].version or "")
    except (AttributeError, IndexError):
        return ""
    return v[2:] if v.startswith("==") else ""


def start_dependency_install(s: Dict[str, Any], approved: bool = False, background: bool = True):
    spec = _spec(s)
    install_id = uuid.uuid4().hex[:12]
    logs: List[str] = []
    s["dependencies"] = {"status": "INSTALLING", "message": f"Resolving dependencies for {spec.name}...", "logs": logs,
                         "framework_id": spec.id, "approved": approved, "install_id": install_id}

    def _job():
        activity_log.current_session.set(s["session_id"])

        def _log(m: str):
            stamp = datetime.utcnow().strftime("%H:%M:%S")
            logs.append(f"[{stamp}] {context_label(s)} | {m[:500]}")
        _activity(s, "RUNTIME", f"Installing dependencies for {spec.name}", "", {"framework_id": spec.id})
        res = dependency_resolver.install(spec, log=_log, approved=approved)
        superseded = (s.get("dependencies") or {}).get("install_id") != install_id
        version = _spec_version(spec)
        _activity(s, "RUNTIME", f"Dependencies for {spec.name}{' ' + version if version else ''}: {res['status']}"
                  + (" (superseded - the user chose another version meanwhile; result not used)" if superseded else ""),
                  res.get("message", ""),
                  {"logs": logs[-80:], "plan": res.get("plan"), "toolchain_missing": res.get("toolchain_missing"),
                   "superseded": superseded},
                  level="info" if superseded or res["status"] in ("READY", "DRY_RUN", "SKIPPED") else "warn")
        if superseded:
            return          # a newer install (another tool or version) owns s["dependencies"] now: never overwrite it
        res["logs"] = logs
        s["dependencies"] = {**res, "framework_id": spec.id, "approved": approved, "install_id": install_id}
        _telemetry(s, "WATCH", "LOCAL_HEURISTIC", f"Dependencies for {spec.name}: {res['status']}", res.get("message", ""),
                   "Dependency Resolver")
        _persist(s)

    if background:
        threading.Thread(target=_job, daemon=True, name=f"deps-{s['session_id']}").start()
    else:
        _job()


# ---------------------------------------------------------------------------
# STEP 2 - BUILD
# ---------------------------------------------------------------------------
def _params_job(s: Dict[str, Any]):
    spec = _spec(s)
    out = build_mod.prepare_parameters(s["analysis"], spec, s["sop_text"])
    s["parameters"] = out["parameters"]
    s["parameter_problems"] = out["problems"]
    s["parameters_meta"] = {"agent_source": out["agent_source"], "model": out["model"], "recall": out.get("recall")}
    s["notes"].extend(out["notes"])
    _telemetry(s, "GOVERN", out["agent_source"], f"Prepared {len(out['parameters'])} editable payload parameters",
               "", out["model"])
    _extract_rules_job(s)                 # 9.9: Govern (Step 2) has the payload table and the SOP rules together


def update_parameters(sid: str, parameters: List[Dict[str, Any]]) -> Dict[str, Any]:
    s = get(sid)
    _require(s, "WATCH")
    old = {p["key"]: p for p in s["parameters"]}
    incoming = []
    for p in parameters:
        q = dict(p)
        if q.get("type") == "secret" and not q.get("value") and old.get(q.get("key"), {}).get("value"):
            q["value"] = old[q["key"]]["value"]      # keep the stored secret when the UI sends the masked blank
        q["type"] = q.get("type") if q.get("type") in ("text", "number", "date", "email", "select", "boolean", "secret") else "text"
        q.setdefault("source", "user" if q.get("key") not in old else old[q["key"]].get("source", "user"))
        q.pop("constraints", None)                    # constraints come from the page, never from the client
        if (old.get(q.get("key")) or {}).get("constraints"):
            q["constraints"] = old[q["key"]]["constraints"]
        incoming.append(q)
    _activity(s, "USER_ACTION", "User edited the payload table", "",
              {"parameters": [{"key": q.get("key"), "type": q.get("type"),
                               "value": "***" if q.get("type") == "secret" else q.get("value")} for q in incoming]})
    clean, problems = screen_parameters(incoming)
    if problems:
        for pr in problems:
            if pr.get("pattern") not in ("INVALID_KEY", "DUPLICATE_KEY", "INVALID_TYPE"):
                raw = next((str(x.get("value")) for x in incoming if x.get("key") == pr["key"]), "")
                _security_event(s, "PAYLOAD", pr.get("pattern"), raw, pr["message"])
        raise WizardError("Some parameters need fixing.", 400, {"problems": problems})
    page_problems = payload_schema.problems(clean)
    if page_problems:
        raise WizardError("Some values would be rejected by the page.", 400, {"problems": page_problems})
    used = {st.get("param_key") for st in s["analysis"]["steps"] if st.get("param_key")}
    missing = sorted(used - {p["key"] for p in clean})
    if missing:
        raise WizardError(f"These parameters are used by the recorded steps and can't be removed: {', '.join(missing)}", 400,
                          {"problems": [{"key": k, "message": "Used by a step"} for k in missing]})
    keys_changed = set(old) != {p["key"] for p in clean} or any(
        old.get(p["key"], {}).get("type") != p.get("type") for p in clean)
    s["parameters"] = clean
    if keys_changed:                      # new / renamed keys: the rules are bound again, the code is rebuilt
        _reset_from(s, "GOVERN")
        _extract_rules_job(s)
        s["current_step"] = 2
    elif s["pillars"].get("GOVERN"):      # new values: Govern checks them again; the code (no values in it) stays
        _reset_from(s, "BUILD-EXECUTE")
        s["pillars"].pop("GOVERN", None)
        s["compliance"] = None
        s["current_step"] = 2
    _persist(s)
    return s


def build_suite(sid: str, background: bool = True) -> Dict[str, Any]:
    s = get(sid)
    _require_not_pending(s)
    _require(s, "WATCH")
    if not s.get("pages_confirmed", True):
        raise WizardError("Check the pages the AI found in your recording first (Step 1 > Pages): edit them if needed, "
                          "then click 'These pages are right'.", 409, {"field": "pages", "needs_page_check": True})
    _require(s, "GOVERN")                 # 9.9: the code is written after the SOP rules are confirmed (Step 2)
    _reset_from(s, "BUILD")
    _activity(s, "USER_ACTION", "User generated the automation suite", "")
    _run_job(s, "build", lambda: _build_job(s), background)
    return s


def _build_job(s: Dict[str, Any], retry_context: bool = False):
    spec = _spec(s)
    if not retry_context:
        _telemetry(s, "BUILD", "ACTUAL_AI_API" if ai_available() else "LOCAL_HEURISTIC",
                   f"Generating the {spec.name} automation suite (values come from payload.json)", "",
                   display_model() if ai_available() else f"Template {spec.template}")
    from core.wizard import build_agent
    feedback = None
    profile = s.get("page_profile")
    if retry_context:                                    # 9.0 heal agent: the failed run is the feedback
        ex = s.get("execution") or {}
        feedback = build_agent.failure_feedback(
            ex, (s.get("suite") or {}).get("files") or {},
            activity_log.strip_context("\n".join(ex.get("logs", [])[-60:])),
            ((s["retry"].get("last_heal") or {}).get("diagnosis") or ""))
        profile = _fresh_profile(s) or profile
    try:
        out = build_agent.generate(spec, s["analysis"]["steps"], s["parameters"], s["analysis"].get("api"),
                                   target_urls_of(s), s["healing_notes"], profile=profile, feedback=feedback,
                                   log=lambda m: _activity(s, "BUILD", m[:300], "", stage="BUILD"),
                                   complexity=s.get("complexity"), task_context=_task_context(s))
    except ValueError as e:
        _set_pillar(s, "BUILD", "FAIL", str(e))
        raise WizardError(str(e), 422)
    _install_suite(s, out, retry_context)


def _task_context(s: Dict[str, Any]) -> Dict[str, Any]:
    """9.8: what an agentic tool's task file describes besides the steps."""
    a = s.get("analysis") or {}
    return {"process_name": a.get("process_name") or "", "summary": a.get("summary") or "",
            "verification": a.get("verification") or {}, "sop": s.get("sop_text") or "",
            "difficulty": s.get("difficulty"), "rules": [r for r in s.get("rules") or [] if r.get("enabled", True)]}


def _fresh_profile(s: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """A new look at the live page(s) for the heal agent (they may have changed since WATCH). 9.4: the same inspection
    plan, each page opened once - what the first inspection learned by reloading (changing ids / order) is kept."""
    from core.wizard import build_agent, inspection
    if not build_agent.probing_enabled():
        return None
    urls = target_urls_of(s)
    try:
        the_plan = s.get("inspection_plan") or inspection.rule_plan(
            (s.get("analysis") or {}).get("steps") or [], s.get("parameters") or [], s.get("sop_text") or "", urls)
        prof = inspection.inspect(urls, the_plan, (s.get("analysis") or {}).get("steps") or [],
                                  prior=s.get("page_profile"), sop=s.get("sop_text") or "")
    except Exception:
        return None
    if prof and prof.get("renderer") == "chromium":
        s["page_profile"] = prof
        return prof
    return None


def _install_suite(s: Dict[str, Any], out: Dict[str, Any], retry_context: bool = False):
    """Makes a generated suite the session's suite (BUILD pillar + telemetry). Also used for a self-healing proposal
    the user approved, so the code that runs is exactly the code the user reviewed."""
    _lock_dependencies(s, out)
    _apply_java_release(s, out.get("files") or {})
    from core.wizard import robot_syntax           # 8.4.1: a keyword with its arguments on the next line, etc.
    out["files"], fixed = robot_syntax.repair_files(out.get("files") or {})
    if fixed:
        _activity(s, "BUILD", "Understudy fixed the Robot Framework layout of the generated suite", "; ".join(fixed)[:600])
    _spec(s)                                         # a framework must be chosen
    s["framework"]["entry_file"] = out["entry_file"]
    s["suite"] = out
    blocking = [f for f in out["findings"] if f["severity"] == "blocking"]
    _set_pillar(s, "BUILD", "PASS" if not blocking else "FAIL",
                f"Generated {len(out['files'])} file(s) with the {out['generator']} generator; "
                f"{'all parameters injected from payload.json' if not blocking else str(len(blocking)) + ' safety finding(s)'}.",
                {"generator": out["generator"], "agent_source": out["agent_source"], "files": sorted(out["files"]),
                 "entry_file": out["entry_file"], "findings": out["findings"]})
    _telemetry(s, "BUILD", out["agent_source"], f"Suite generated ({out['generator']}): {', '.join(sorted(out['files']))}",
               "; ".join(out["notes"])[:400], out["model"])
    if blocking:
        raise WizardError("The generated code did not pass the safety scan.", 422, {"findings": blocking})
    if not retry_context:
        s["current_step"] = 4


def _base_spec(s: Dict[str, Any]) -> FrameworkSpec:
    return FrameworkSpec.from_dict(s.get("framework_base") or s["framework"])


def _effective_for(s: Dict[str, Any], out: Dict[str, Any]):
    """(approved base spec, effective spec, extras) for a generated suite - extras are re-detected against the base
    spec, so packages added by an earlier build are kept exactly when the new code still imports them."""
    base = _base_spec(s)
    extras = dependency_lock.detect(base, out["files"], out.get("extra_packages"))["extras"]
    return base, dependency_lock.effective_spec(base, extras), extras


def _lock_dependencies(s: Dict[str, Any], out: Dict[str, Any]) -> None:
    """The generated suite's dependencies = the approved, version-pinned tool + the trusted extra packages its code
    needs. The manifest in the suite is (re)written from that, so sandbox, run directory and ZIP all agree; when the
    package set changed, the sandbox environment is updated right away (core/wizard/dependency_lock.py)."""
    base, eff, extras = _effective_for(s, out)
    out["files"] = dependency_lock.apply_to_files(eff, out["files"])
    before = dependency_resolver.plan_for(_spec(s)).cache_key
    s["framework"]["packages"] = [{"name": p.name, "version": p.version} for p in eff.packages]
    s["dependency_lock"] = {"extras": extras, "primary": eff.packages[0].name if eff.packages else None,
                            "chosen_version": dependency_lock.chosen_version(base),
                            "manifests": sorted(dependency_resolver.manifest_files(eff)),
                            "notes": [n for n in out.get("notes") or [] if "approved version" in n]}
    if extras:
        _activity(s, "RUNTIME", f"The generated code needs {len(extras)} extra package(s): "
                  + ", ".join(e["name"] for e in extras), "; ".join(e.get("why", "") for e in extras)[:500],
                  {"extras": extras})
    deps = s.get("dependencies") or {}
    if dependency_resolver.plan_for(eff).cache_key != before or deps.get("framework_id") != eff.id:
        if deps.get("status") in ("INSTALLING", "PENDING"):
            _wait_for_install(s, 300)
        start_dependency_install(s, approved=True, background=False)


def _wait_for_install(s: Dict[str, Any], seconds: float) -> None:
    deadline = time.time() + seconds
    while (s.get("dependencies") or {}).get("status") in ("INSTALLING", "PENDING") and time.time() < deadline:
        time.sleep(0.5)


def _ensure_dependencies(s: Dict[str, Any], log: Callable[[str], None]) -> Optional[str]:
    """Right before a run: the exact approved versions must be installed in the sandbox. Re-checks the admin's version
    policy, verifies the isolated environment really contains the pinned versions (another Cloud Run instance, a
    restart or a deleted cache may have lost it) and installs whatever is missing. Returns a POLICY message when the
    chosen version is no longer allowed, else None."""
    base = _base_spec(s)
    version = dependency_lock.chosen_version(base)
    pol = admin_policy.tool_version_policy(base.id, s.get("owner"))
    if version and pol["allowed"] and version not in pol["allowed"]:
        return (f"{base.name} {version} is no longer allowed by your admin (allowed: {', '.join(pol['allowed'])}). "
                f"Pick an allowed version in Step 1 and rebuild.")
    if not admin_policy.is_tool_allowed(base.id):
        return f"{base.name} was removed from your admin's list of approved tools."
    if not admin_policy.user_may_use(base.id, s.get("owner")):
        return admin_policy.access_denied_message(base.name, base.id) + " Ask your admin for access."
    spec = _spec(s)
    deps = s.get("dependencies") or {}
    if deps.get("status") in ("INSTALLING", "PENDING"):
        log("Waiting up to 300s for the dependency installation to finish...")
        _wait_for_install(s, 300)
        deps = s.get("dependencies") or {}
    from core.wizard import runtime_restore              # 9.9: Cloud Run instances start without earlier downloads
    runtime_restore.ensure(log)
    if deps.get("status") == "TOOLCHAIN_MISSING" and not dependency_resolver.check_toolchain(spec.ecosystem, base.id):
        log("The tool's toolchain is on this server now - installing its packages.")
        deps = dict(deps, status="FAILED")
    if dependency_resolver.install_mode() != "auto" or deps.get("status") in ("TOOLCHAIN_MISSING", "AWAITING_APPROVAL"):
        return None
    current = dependency_resolver.plan_for(spec).cache_key
    planned = ((deps.get("plan") or {}).get("cache_key"))
    ok, problems = False, ["the dependencies were never installed for this package set"]
    if deps.get("status") == "READY" and planned == current:
        try:
            ok, problems = dependency_lock.verify_installed(spec, dependency_resolver.installed_versions(spec))
        except Exception as e:                   # never block a run because the check itself failed
            ok, problems = True, [f"(could not verify: {e})"]
    if ok:
        log("Sandbox dependencies verified: " + ", ".join(
            f"{r['name']} {r.get('installed') or r.get('requested')}" for r in deps.get("versions") or []))
        return None
    log("Installing the approved dependencies into the isolated sandbox before the run ("
        + "; ".join(problems)[:300] + ")...")
    s["dependencies"] = {"status": "INSTALLING", "logs": [], "framework_id": spec.id, "approved": True}
    res = dependency_resolver.install(spec, log=lambda m: log("  " + m), approved=True, force=deps.get("status") == "READY")
    s["dependencies"] = {**res, "framework_id": spec.id, "approved": True}
    log(f"Dependencies: {res['status']} - {res.get('message', '')}")
    return None


# ---------------------------------------------------------------------------
# STEP 3 - GOVERN
# ---------------------------------------------------------------------------
def _extract_rules_job(s: Dict[str, Any]):
    out = governance.extract_rules(s["sop_text"], s["parameters"], s["session_id"])
    s["rules"] = out["rules"]
    s["rules_confirmed"] = False
    s["rules_meta"] = {k: out.get(k) for k in ("agent_source", "model", "security_flags", "notes", "recall",
                                               "sop_language", "language_warning")}
    for flag in out.get("security_flags") or []:
        _security_event(s, "SOP", "AI-FLAGGED INSTRUCTION", flag, f"The AI noticed an instruction inside the SOP: {flag}")
    _telemetry(s, "GOVERN", out["agent_source"], f"Extracted {len(out['rules'])} governance rules from the SOP",
               "; ".join(out.get("notes") or [])[:300], out["model"])


def _require_payload(s: Dict[str, Any]) -> None:
    _require(s, "WATCH")
    if not s.get("framework"):
        raise WizardError("Choose an automation tool in Step 1 first - Govern needs its payload table.", 409)


def extract_rules(sid: str, background: bool = False) -> Dict[str, Any]:
    s = get(sid)
    _require_payload(s)
    _reset_from(s, "GOVERN")
    _activity(s, "USER_ACTION", "User asked Understudy to extract the SOP rules", "")
    _run_job(s, "extract_rules", lambda: _extract_rules_job(s), background)
    return s


def update_rules(sid: str, rules: List[Dict[str, Any]], confirmed: bool) -> Dict[str, Any]:
    s = get(sid)
    _require_payload(s)
    clean, problems = [], []
    for i, r in enumerate(rules):
        for fld in ("source_text", "pattern"):
            if r.get(fld):
                tv = screen_text(r[fld], "SOP", use_semantic=False)
                if not tv.is_safe:
                    _security_event(s, "SOP", tv.pattern, str(r[fld]), tv.friendly_message)
                    problems.append({"index": i, "message": tv.friendly_message})
        for v in r.get("values") or []:
            tv = screen_text(v, "PAYLOAD", use_semantic=False)
            if not tv.is_safe:
                _security_event(s, "SOP", tv.pattern, str(v), tv.friendly_message)
                problems.append({"index": i, "message": tv.friendly_message})
        try:
            clean.append(governance.normalize_rule(r, s["parameters"], source=r.get("source") or "user"))
        except ValueError as e:
            problems.append({"index": i, "message": str(e)})
    if problems:
        raise WizardError("Some rules need fixing.", 400, {"problems": problems})
    s["rules"] = clean
    s["rules_confirmed"] = bool(confirmed)
    _activity(s, "USER_ACTION", f"User {'confirmed' if confirmed else 'edited'} {len(clean)} SOP rule(s)", "",
              {"rules": clean})
    _reset_from(s, "GOVERN")
    _telemetry(s, "GOVERN", "LOCAL_HEURISTIC", f"User {'confirmed' if confirmed else 'edited'} {len(clean)} rules", "",
               "User review")
    _persist(s)
    return s


def update_sop(sid: str, sop_text: str, background: bool = False) -> Dict[str, Any]:
    s = get(sid)
    _require(s, "WATCH")
    _activity(s, "USER_ACTION", "User edited the SOP", "", {"sop_text": (sop_text or "")[:4000]})
    tv = screen_text(sop_text or "", "SOP")
    if not tv.is_safe:
        _security_event(s, "SOP", tv.pattern, tv.sanitized, tv.friendly_message)
        raise WizardError(tv.friendly_message, 400, {"field": "sop_text"})
    s["sop_text"] = tv.sanitized
    _detect_tool_requirement(s)
    sop_fields = {r["field"] for r in governance.parse_sop_rules(s["sop_text"], s["parameters"])}
    for p in s["parameters"]:
        p["sop_dependent"] = p["key"] in sop_fields
    if s.get("framework") and s["pillars"].get("WATCH", {}).get("status") == "PASS":
        _reset_from(s, "GOVERN")          # 9.9: new rules -> Govern again, then the code is rebuilt with them
        _run_job(s, "extract_rules", lambda: _extract_rules_job(s), background)
    _persist(s)
    return s


def compliance_check(sid: str) -> Dict[str, Any]:
    s = get(sid)
    _require_payload(s)
    _activity(s, "USER_ACTION", "User ran the compliance check", "")
    if not s["rules_confirmed"]:
        raise WizardError("Please review the rules and confirm that the AI understood your SOP correctly.", 409)
    # 9.9: the payload against the rules; the code is generated next (Step 3) and scanned there
    res = governance.run_compliance_check(s["rules"], s["parameters"], s["sop_text"], {}, target_urls_of(s),
                                          s["analysis"]["steps"])
    s["compliance"] = res
    if res["passed"]:
        _set_pillar(s, "GOVERN", "PASS",
                    f"Payload complies with all {res['rules_checked']} checked SOP rule(s); the rules go into the "
                    f"generated script.", {"rules": s["rules"], "compliance": res})
        s["current_step"] = 3
    else:
        _set_pillar(s, "GOVERN", "FAIL", f"{len(res['violations'])} violation(s): " +
                    "; ".join(v["message"] for v in res["violations"][:3]), {"compliance": res})
    _telemetry(s, "GOVERN", res.get("ai_review", {}).get("agent_source", "LOCAL_HEURISTIC"),
               f"Compliance check {'passed' if res['passed'] else 'flagged ' + str(len(res['violations'])) + ' violation(s)'}",
               "", "Deterministic rule engine + AI advisory review")
    _persist(s)
    return s


# ---------------------------------------------------------------------------
# STEP 4 - EXECUTE
# ---------------------------------------------------------------------------
def _exec_log(s: Dict[str, Any]) -> Callable[[str], None]:
    def _log(line: str):
        stamp = datetime.utcnow().strftime("%H:%M:%S")
        s["execution"]["logs"].append(f"[{stamp}] {context_label(s)} | {line}")
        _activity(s, "EXECUTION", line[:300], line if len(line) > 300 else "", stage="EXECUTE",
                  level="error" if "FAILED" in line or "Traceback" in line else "info")
        if line.startswith("STEP") or "FAILED" in line or line.startswith("Running"):
            _telemetry(s, "EXECUTE", "LOCAL_HEURISTIC", line[:200], "", "Execution Driver")
    return _log


def _do_execute(s: Dict[str, Any], attempt: int) -> Dict[str, Any]:
    spec = _spec(s)
    run_dir = os.path.join(WORK_ROOT, s["session_id"], f"attempt-{attempt}")
    s["execution"] = {"status": "RUNNING", "attempt": attempt, "logs": [], "started_at": _now(), "run_dir": run_dir}
    log = _exec_log(s)
    urls = target_urls_of(s)
    log(f"Attempt {attempt}: running the automation against {s['target_url']}"
        + (f" (+{len(urls) - 1} more page(s): {', '.join(urls[1:])})" if len(urls) > 1 else "") + " ...")
    policy_block = _ensure_dependencies(s, log)
    if policy_block:
        log(f"STEP 0 FAILED: {policy_block}")
        res = {"status": "FAIL", "exit_code": 1, "last_ok_step": 0, "failure_reason": policy_block,
               "failure_category_hint": "POLICY", "strategy": "framework", "duration_ms": 0.0, "finished_at": _now()}
        logs = s["execution"]["logs"]
        s["execution"] = {**res, "logs": logs, "attempt": attempt, "run_dir": run_dir,
                          "started_at": s["execution"]["started_at"]}
        return res
    deps = (s.get("dependencies") or {}).get("status")
    agent_env = None
    if is_agentic(s):                         # 9.9: the organisation's Gemini, through the run's relay (executor)
        from core.wizard import agent_relay, agent_stacks
        agent_env = None
        if agent_relay.available():
            agent_env = {k: os.environ[k] for k in ("UNDERSTUDY_AGENT_MAX_CALLS", "UNDERSTUDY_AGENT_MAX_TRIES")
                         if os.environ.get(k)}                          # the admin's limits for the agent
            agent_env.update(agent_stacks.run_env(s.get("framework") or spec.to_dict()))
            agent_env[agent_relay.MODEL_ENV] = _tool_agent_model(s) or agent_relay.model()
        if agent_env is None:
            why = (f"{spec.name} is an agentic tool: it calls Gemini while it runs, and your admin hasn't set up "
                   f"Gemini in Mission Control ({agent_relay.unavailable_reason()}).")
            log(f"STEP 0 FAILED: {why}")
            res = {"status": "FAIL", "exit_code": 1, "last_ok_step": 0, "failure_reason": why, "needs_ai": True,
                   "failure_category_hint": "POLICY", "strategy": "framework", "duration_ms": 0.0, "finished_at": _now()}
            logs = s["execution"]["logs"]
            s["execution"] = {**res, "logs": logs, "attempt": attempt, "run_dir": run_dir,
                              "started_at": s["execution"]["started_at"]}
            return res
    _refresh_manifests(s, spec, log)
    if "governance.json" in s["suite"]["files"]:            # 9.9: the checker importable from every test folder
        from core.wizard import governance_runtime
        extra = governance_runtime.companions(spec, s["suite"]["files"])
        if extra:
            s["suite"]["files"].update(extra)
            log("Added " + ", ".join(sorted(extra)) + " so every test can load the SOP-rules check.")
    res = executor.execute(spec, s["suite"]["files"], s["analysis"]["steps"], s["parameters"], s["target_url"], run_dir,
                           deps, log, strategy_override=s.get("strategy_override"),
                           runtime_env=_user_runtime_env(s) or None, extra_urls=target_urls_of(s)[1:],
                           dep_message=(s.get("dependencies") or {}).get("message", ""), agent_env=agent_env)
    if agent_env is not None:
        res["agentic"] = True
        res["agent_model"] = (res.get("agent_ai") or {}).get("model") or agent_env.get("UNDERSTUDY_AGENT_MODEL")
        res["agent_stack"] = {k.split("_")[-1].lower(): v for k, v in agent_env.items()
                              if k in ("UNDERSTUDY_AGENT_DRIVER", "UNDERSTUDY_AGENT_BROWSER")}
    logs = s["execution"]["logs"]
    s["execution"] = {**res, "logs": logs, "attempt": attempt, "run_dir": run_dir, "started_at": s["execution"]["started_at"]}
    return res


def _refresh_manifests(s: Dict[str, Any], spec: FrameworkSpec, log: Callable[[str], None]) -> None:
    """9.9.1: the suite's pom.xml / package.json / requirements.txt was written at build time. When the package set
    changed since (the admin added a package from an escalation, a self-healing repair), the run would copy the OLD
    manifest into the run directory and compile without the new package - while the sandbox already has it. Rewrite
    the manifests from the tool's current packages before every run, so sandbox, run directory and ZIP agree."""
    files = s["suite"].get("files") or {}
    if not any(n in dependency_lock.ALL_MANIFESTS for n in files):
        return
    fresh = dependency_lock.apply_to_files(spec, files)
    _apply_java_release(s, fresh)
    changed = sorted(n for n in fresh if n in dependency_lock.ALL_MANIFESTS and fresh[n] != files.get(n))
    if changed:
        s["suite"]["files"] = fresh
        log("Updated " + ", ".join(changed) + " to the tool's current packages ("
            + ", ".join(f"{p.name} {p.version or 'default'}" for p in spec.packages) + ").")


def _user_runtime_env(s: Dict[str, Any]) -> Dict[str, Any]:
    """9.3: the run variables / PATH folders the admin approved for THIS user only, under the session's own
    self-healing settings (those win). Tool-wide settings come from the tool's registry entry as before."""
    env = dict(s.get("runtime_env") or {})
    try:
        mine = admin_policy.user_overrides(_spec(s).id, s.get("owner"))
    except Exception:
        return env
    out = dict(mine["env_vars"], **env)
    if mine["path_prepend"]:
        own = [p for p in str(env.get("__path_prepend") or "").split(os.pathsep) if p]
        out["__path_prepend"] = os.pathsep.join(own + [p for p in mine["path_prepend"] if p not in own])
    return out


def _runtime_check(s: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """8.4: the tool's runtime requirements (Mission Control > Environments) against the versions its runs would use.
    None when there is nothing to check (or the check itself failed - a broken probe never blocks a run)."""
    try:
        from core.wizard import environments
        tid = _base_spec(s).id
        if not admin_policy.is_tool_allowed(tid):
            return None
        return environments.check(tid)
    except Exception as e:
        _activity(s, "RUNTIME", "Could not check the tool's runtime versions", f"{type(e).__name__}: {e}", level="warn")
        return None


def is_agentic(s: Dict[str, Any]) -> bool:
    """9.8: the session's tool is an agentic one (an AI agent performs the steps at run time)."""
    from core.wizard import agentic_templates
    fw = s.get("framework") or {}
    # only Understudy's vetted agentic runners get the user's key - not any tool that calls itself "agentic"
    return (fw.get("category") or "").lower() == "agentic" and fw.get("template") in agentic_templates.TEMPLATES


def _tool_agent_model(s: Dict[str, Any]) -> Optional[str]:
    """9.9: the default model an admin-built agentic tool asks for (else the organisation's model)."""
    return ((s.get("framework") or {}).get("agent") or {}).get("model") or None


def execute(sid: str, background: bool = True, ignore_requirements: bool = False) -> Dict[str, Any]:
    s = get(sid)
    _require_not_pending(s)
    _require(s, "WATCH", "BUILD", "GOVERN")
    if is_agentic(s):
        from core.wizard import agent_relay
        if not agent_relay.available():
            raise WizardError(f"{(s.get('framework') or {}).get('name', 'This tool')} is an agentic tool - it calls "
                              "Gemini while it runs, and your admin hasn't set up Gemini in Mission Control yet.", 409,
                              {"needs_ai": True})
    prev = s.get("runtime_check") or {}
    chk = _runtime_check(s)
    if chk and not chk["ok"] and not ignore_requirements:
        chk["sent"] = {k: v for k, v in (prev.get("sent") or {}).items()
                       if any(p["kind"] == k for p in chk["problems"])}
        s["runtime_check"] = chk
        s["current_step"] = 4
        _activity(s, "RUNTIME", "Run held: the tool's runtime versions don't meet its requirements",
                  " ".join(p["message"] for p in chk["problems"])[:600], {"problems": chk["problems"]}, level="warn")
        _persist(s)
        return s
    s["runtime_check"] = dict(chk, ignored=True) if chk and not chk["ok"] else None
    if chk and not chk["ok"]:
        _activity(s, "USER_ACTION", "User ran the automation despite the runtime-version warning",
                  " ".join(p["message"] for p in chk["problems"])[:600], level="warn")
    _reset_from(s, "BUILD-EXECUTE")
    s["current_step"] = 4
    s["retry"].update(count=0, awaiting_decision=False, prompt=None, suggest_reference=False, stop_requested=False,
                      duplicate_warning="",
                      awaiting_approval=False, proposal=None)
    s["status"] = "active"
    _activity(s, "USER_ACTION", "User started the automation run", "", {"framework": (s.get("framework") or {}).get("id")})

    def _job():
        res = _do_execute(s, s["retry"]["count"] + 1)
        _after_execute(s, res)
    _run_job(s, "execute", _job, background)
    return s


def _after_execute(s: Dict[str, Any], res: Dict[str, Any]):
    ev = {k: v for k, v in res.items() if k not in ("failure_html",)}
    ev["log_tail"] = s["execution"]["logs"][-25:]
    if res["status"] == "PASS":
        _set_pillar(s, "BUILD-EXECUTE", "PASS", f"Automation ran to completion against {s['target_url']} "
                    f"({res['strategy']} strategy, {res['duration_ms']} ms).", ev)
        s["current_step"] = 5
        _finish_export(s)
    else:
        where = (f"at step {res['failed_step']}" if res.get("failed_step") else
                 f"after step {res['last_ok_step']}" if res.get("last_ok_step") else
                 "at a step it did not number" if res.get("step_failed_unnumbered") else "before its first step")
        reason = f"Failed {where}: {res.get('failure_reason', 'unknown error')}"
        _set_pillar(s, "BUILD-EXECUTE", "FAIL", reason, ev)
        if res.get("governance_violation"):
            # 9.9: the script's own SOP check stopped it - the data is wrong, not the code: no automatic retry.
            s["status"] = "needs_help"
            s["notes"].append("Run stopped by the SOP rules - " + str(res.get("failure_reason", ""))[:200])
            _telemetry(s, "GOVERN", "LOCAL_HEURISTIC", "Run stopped by the script's SOP check - no automatic retry",
                       str(res.get("failure_reason", ""))[:300], "SOP rules in the script")
            return
        if res.get("failure_category_hint") == "POLICY":
            # An admin policy (blocked list / SSRF rule) stopped the run: retrying cannot change that.
            s["status"] = "needs_help"
            s["notes"].append("Run stopped by the admin's network policy - " + str(res.get("failure_reason", ""))[:200])
            _telemetry(s, "VERIFY", "LOCAL_HEURISTIC", "Run stopped by the admin's URL policy - no automatic retry",
                       str(res.get("failure_reason", ""))[:300], "Model Armor")
            return
        _offer_retry(s, f"The automation stopped {where} ({str(res.get('failure_reason', ''))[:160]}).")


# ---------------------------------------------------------------------------
# STEP 5 - EXPORT (9.0), RETRY (self-healing)
# ---------------------------------------------------------------------------
def _finish_export(s: Dict[str, Any]) -> None:
    """9.0: the run passed step 4 - package it. No before/after page verification and no admin escalation: the
    generated suite's own assertions decide whether the run passed."""
    s["retry"]["awaiting_decision"] = False
    _set_pillar(s, "EXPORT", "PASS", "The automation ran to completion - the ZIP with the code, the payload and the run "
                "log is ready.", {"strategy": (s.get("execution") or {}).get("strategy")})
    _learn_from_success(s)
    _make_export(s)


def export(sid: str, background: bool = True) -> Dict[str, Any]:
    """Step 5: creates (or re-creates) the ZIP once step 4 passed."""
    s = get(sid)
    _require(s, "WATCH", "BUILD", "GOVERN", "BUILD-EXECUTE")
    s["current_step"] = 5

    def _job():
        _finish_export(s)
    _run_job(s, "export", _job, background)
    return s


def _remember_error(s: Dict[str, Any], why: str):
    """Every failure becomes a lesson in a learned skill (resolved later when a retry succeeds)."""
    try:
        spec = _spec(s)
        execution = s.get("execution") or {}
        verification = s.get("verification")
        tail = activity_log.strip_context("\n".join(execution.get("logs", [])[-150:]))
        category = verifier.classify_failure(execution, verification if execution.get("status") == "PASS" else None, tail)
        text = (execution.get("failure_reason") if execution.get("status") != "PASS"
                else (verification or {}).get("summary")) or why
        ref = skills.record_error(spec.id, spec.name, s["target_url"], category, str(text), s["session_id"])
        refs = s.setdefault("skill_refs", [])
        if ref not in refs:
            refs.append(ref)
        _activity(s, "RUNTIME", f"Recorded the error in learned skill {ref['skill_id']}", str(text)[:300],
                  {"skill": ref, "category": category}, source="LEARNED_SKILL")
    except Exception as e:  # pragma: no cover - learning must never break the run
        _activity(s, "RUNTIME", "Could not record the error as a skill", str(e), level="warn")


def _learn_from_success(s: Dict[str, Any]):
    refs = s.get("skill_refs") or []
    if not refs:
        return
    try:
        spec = _spec(s)
        env = {} if s.get("strategy_override") == "reference" else (s.get("runtime_env") or {})
        changed = skills.record_success(spec.id, spec.name, s["target_url"], refs, env, s["retry"]["history"],
                                        s.get("healing_notes") or [])
        s["learned_skills"] = changed
        s["skill_refs"] = []
        for sid in changed:
            _activity(s, "RUNTIME", f"Learned skill updated: {sid}", "Next runs apply this fix before the first attempt.",
                      {"skill_id": sid}, source="LEARNED_SKILL")
        if changed:
            _telemetry(s, "VERIFY", "LOCAL_HEURISTIC", f"Saved what fixed this run as learned skill(s): {', '.join(changed)}",
                       "", "Learned skills")
    except Exception as e:  # pragma: no cover
        _activity(s, "RUNTIME", "Could not save the learned skill", str(e), level="warn")


def _offer_retry(s: Dict[str, Any], why: str):
    """A run failed. Self-healing is AUTOMATIC (no user prompt): Understudy diagnoses, fixes and re-runs by itself
    until the automation passes or the admin's retry limit is reached. The user can stop it at any time."""
    _remember_error(s, why)
    r = s["retry"]
    r["max"] = max_retries()          # read live, so an admin change applies to running sessions too
    r["awaiting_decision"] = False
    r["prompt"] = None
    if r.pop("stop_requested", False):
        s["status"] = "stopped"
        _activity(s, "USER_ACTION", "Self-healing stopped by the user", why)
        _telemetry(s, "VERIFY", "LOCAL_HEURISTIC", "Self-healing stopped by the user - no ZIP", why, "User")
        return
    if r["count"] >= r["max"]:
        last = r.get("last_heal") or {}
        if last.get("category") == "ENVIRONMENT" and s.get("strategy_override") != "reference":
            # The tool's runtime still can't start: let the user decide (fix the machine and retry with the tool, or
            # verify with the built-in driver) instead of just giving up.
            r["suggest_reference"] = True
            _ask_user(s, f"Understudy made {r['count']} automatic repair attempts (your admin's limit) but "
                         f"{_spec(s).name} still can't start on this machine. {why}")
            return
        s["notes"].append(f"Self-healing stopped after {r['count']} automatic retries (admin limit {r['max']}).")
        _telemetry(s, "VERIFY", "LOCAL_HEURISTIC",
                   f"Retry limit reached ({r['count']}/{r['max']}) - waiting for the user's correction", why,
                   "Verification Auditor")
        # 8.2: the user can still steer the AI (a correction starts a fresh budget) instead of a dead end
        _ask_user(s, f"Understudy made {r['count']} self-healing attempts (your admin's limit) and the automation still "
                     f"fails: {why} Tell the AI what is wrong to try again with your correction, or stop here.")
        return
    s["status"] = "healing"
    if s.get("job", {}).get("status") == "running":
        s["job"]["name"] = "retry"      # the UI shows the self-healing state and live log
    if admin_policy.self_heal_approval() == "ask":
        _telemetry(s, "VERIFY", "LOCAL_HEURISTIC", f"Self-healing retry {r['count'] + 1} of {r['max']}: preparing a fix "
                   f"for the user to review", why, "Self-healer")
        _propose_heal(s, why)
        return
    _telemetry(s, "VERIFY", "LOCAL_HEURISTIC", f"Automatic self-healing retry {r['count'] + 1} of {r['max']}", why,
               "Self-healer")
    _retry_job(s, "heal")


def _ask_user(s: Dict[str, Any], why: str):
    """The only situation in which Understudy asks: every runtime repair for the chosen tool has been tried. Switching
    to the built-in Playwright driver stays the user's decision."""
    r = s["retry"]
    r["max"] = max_retries()
    r["awaiting_decision"] = True
    r["prompt"] = why
    # 8.2: nothing is pre-built - the built-in Playwright driver is offered only when the admin activated Playwright
    r["reference_available"] = reference_available()
    s["status"] = "needs_decision"
    _telemetry(s, "VERIFY", "LOCAL_HEURISTIC", "Runtime repairs exhausted - waiting for the user's decision", why,
               "Self-healer")


def decline_retry(sid: str) -> Dict[str, Any]:
    """Stop: ends a pending decision, or stops the automatic self-healing loop after the current attempt."""
    s = get(sid)
    if s["retry"].get("awaiting_approval"):
        p = s["retry"].get("proposal") or {}
        s["retry"].update(awaiting_approval=False, proposal=None)
        s["status"] = "stopped"
        _activity(s, "USER_ACTION", f"User rejected the self-healing fix for retry {p.get('attempt', '?')} and stopped", "")
        _telemetry(s, "VERIFY", "LOCAL_HEURISTIC", "User stopped instead of approving the proposed fix - no ZIP", "", "User")
        _persist(s)
        return s
    if not s["retry"]["awaiting_decision"]:
        if s.get("job", {}).get("status") == "running" and s["job"].get("name") in ("retry", "execute", "verify",
                                                                                     "heal_revise"):
            s["retry"]["stop_requested"] = True
            _activity(s, "USER_ACTION", "User asked to stop self-healing after the current attempt", "")
            _persist(s)
            return s
        raise WizardError("Nothing to stop.", 409)
    s["retry"]["awaiting_decision"] = False
    s["retry"]["prompt"] = None
    s["status"] = "stopped"
    _activity(s, "USER_ACTION", "User stopped the run", "")
    _telemetry(s, "VERIFY", "LOCAL_HEURISTIC", "User stopped the run - no ZIP will be produced", "", "User")
    _persist(s)
    return s


def reference_available() -> bool:
    """The built-in Playwright driver is a fallback only when the admin has Playwright (Python) ACTIVE (8.2)."""
    try:
        return admin_policy.is_tool_allowed("playwright-python")
    except Exception:
        return False


def retry(sid: str, background: bool = True, mode: str = "heal", feedback: Optional[str] = None) -> Dict[str, Any]:
    """mode='heal' (default): diagnose + self-heal and re-run the SAME tool (repairing its runtime if needed).
    mode='reference': the user explicitly chose to verify the process with the built-in Playwright driver.
    feedback (8.2): the user tells the AI what is different on this machine ("my JDK 17 is in C:\\Java\\jdk-17") -
    the AI plans the next repair with it (shown for approval in 'ask' mode)."""
    s = get(sid)
    if not s["retry"]["awaiting_decision"]:
        raise WizardError("Retry is only available after a failed execution or verification.", 409)
    if mode not in ("heal", "reference"):
        raise WizardError("mode must be 'heal' or 'reference'.", 422)
    if mode == "reference" and not reference_available():
        raise WizardError("The built-in Playwright driver isn't available: your admin hasn't activated Playwright. "
                          "Retry with your tool (tell the AI what is different on this machine), or ask your admin.", 409)
    text = str(feedback or "").strip()
    if text:
        if len(text) > MAX_CLARIFICATION_CHARS:
            raise WizardError(f"Please keep the correction under {MAX_CLARIFICATION_CHARS} characters.", 400,
                              {"field": "feedback"})
        tv = screen_text(text, "HEAL_FEEDBACK")
        if not tv.is_safe:
            _security_event(s, "HEAL_FEEDBACK", tv.pattern, text, tv.friendly_message)
            raise WizardError(tv.friendly_message, 400, {"field": "feedback", "security": True})
        text = tv.sanitized
        s.setdefault("runtime_feedback", []).append(text)
        s["runtime_feedback"] = s["runtime_feedback"][-5:]
    s["retry"]["awaiting_decision"] = False
    s["retry"]["prompt"] = None
    s["retry"]["count"] = 0            # an explicit user decision starts a fresh automatic-retry budget
    s["retry"]["stop_requested"] = False
    if s["retry"].get("suggest_reference") and mode == "heal":
        # Every repair was tried and the user retries with their tool anyway (typically after installing the missing
        # browser/toolchain): re-run it as-is first, then walk the repair ladder again from the start if needed.
        s["_rerun_as_is"] = True
    s["retry"]["suggest_reference"] = False
    s["status"] = "active"
    _activity(s, "USER_ACTION", ("User retried with the chosen tool" + (" and told the AI: " + text if text else ""))
              if mode == "heal" else "User chose to verify with the built-in Playwright driver", "", {"mode": mode})
    if mode == "heal" and (text or s["retry"].get("duplicate_warning")):
        s.pop("_rerun_as_is", None)        # the user's correction is applied instead of a plain re-run
        if admin_policy.self_heal_approval() == "ask":
            why = str((s.get("execution") or {}).get("failure_reason") or "The tool could not start.")
            s["status"] = "healing"
            fb = list(s.get("runtime_feedback") or []) if text else []
            _run_job(s, "heal_revise", lambda: _propose_heal(s, why, fb), background)
            return s
    _run_job(s, "retry", lambda: _retry_job(s, mode), background)
    return s


def _tried_repairs(history: List[Dict[str, Any]]) -> List[str]:
    start = max((i for i, r in enumerate(history) if r.get("repair_id") == "rerun"), default=-1) + 1
    return [r.get("repair_id") for r in history[start:] if r.get("repair_id")]


def _apply_java_release(s: Dict[str, Any], files: Dict[str, str]) -> None:
    """8.2: a self-healing repair that compiles for the JDK on this machine ("java_release") is kept in pom.xml - the
    run uses it and the exported ZIP gets the same file."""
    rel = (s.get("runtime_env") or {}).get("__java_release")
    if rel:
        from core.wizard import jdk
        if "pom.xml" in files:
            files["pom.xml"] = jdk.patch_pom_release(files["pom.xml"], int(rel))
        if "build.gradle" in files:
            files["build.gradle"] = jdk.patch_gradle_release(files["build.gradle"], int(rel))


def _runtime_context(s: Dict[str, Any]) -> Tuple[Dict[str, str], List[str]]:
    """The environment the next run gets (tool setup runtime + self-healing settings) - what Maven will really see."""
    spec = _spec(s)
    rt = admin_policy.tool_runtime(spec.id, s.get("owner"))
    env = dict(os.environ)
    env.update(dependency_resolver.toolchain_env())
    env["PATH"] = dependency_resolver.toolchain_path(env.get("PATH", ""))
    env.update(rt.get("env_vars") or {})
    renv = s.get("runtime_env") or {}
    env.update({k: str(v) for k, v in renv.items() if not k.startswith("__")})
    pp = [p for p in str(renv.get("__path_prepend") or "").split(os.pathsep) if p] + list(rt.get("path_prepend") or [])
    return env, pp


def _plan_runtime(s: Dict[str, Any], execution: Dict[str, Any], log_tail: str,
                  feedback: Optional[List[str]] = None) -> Dict[str, Any]:
    """The next runtime repair: Understudy's rules first (with the user's corrections), then - when the rules don't
    recognise the failure, have run out, or the user corrected them - one AI-advised repair (RUNTIME_REPAIR), validated
    before anyone approves it."""
    from core.wizard import jdk
    spec = _spec(s)
    tried = _tried_repairs(s.get("runtime_repairs") or [])
    env, pp = _runtime_context(s)
    current = None
    if spec.ecosystem in ("maven", "gradle") or runtime_repair.JDK_MISMATCH.search(log_tail + str(execution.get("failure_reason"))):
        current = jdk.maven_jdk(env, pp)
    versions = _tool_versions_facts(s)
    plan = runtime_repair.plan_repair(spec.id, execution, log_tail, tried, feedback=feedback, current_jdk=current,
                                      versions=versions)
    user_path_used = str(plan.get("repair_id") or "").startswith(("jdk:user:", "browser:user:"))
    # 8.3: the AI also gets a say once Understudy's own first repair for this run didn't help (tried), and for
    # generic "reinstall" answers - so failures nobody wrote a rule for still get a real, reviewed fix.
    generic = plan.get("repair_id") == "reinstall" and plan["cause"] in ("MISSING_PACKAGE", "UNKNOWN_RUNTIME")
    if plan["exhausted"] or plan["cause"] == "UNKNOWN_RUNTIME" or (feedback and not user_path_used) or tried or generic:
        try:
            facts = runtime_repair.machine_facts(env, pp)
        except Exception:
            facts = {}
        facts["versions"] = versions
        ai = runtime_repair.ai_plan_repair(spec, execution, log_tail, tried, plan["cause"], feedback, facts)
        if ai is not None and (not ai["exhausted"] or plan["exhausted"]):
            ai["cause"] = plan["cause"]
            if ai["exhausted"] and plan.get("explanation"):
                ai["explanation"] = plan["explanation"] + " AI: " + ai["explanation"]
            plan = ai
    return plan


def _tool_versions_facts(s: Dict[str, Any]) -> Dict[str, Any]:
    """8.3: what self-healing may know about the tool's versions: the one in use, the ones the admin allows, the newest
    in the registry and the one installed in Understudy's own environment (known to work on this Python)."""
    try:
        base = _base_spec(s)
    except Exception:
        return {}
    if not base.packages:
        return {}
    name = base.packages[0].name
    out: Dict[str, Any] = {"package": name, "current": dependency_lock.chosen_version(base) or base.packages[0].version,
                           "allowed": list(admin_policy.tool_version_policy(base.id, s.get("owner")).get("allowed") or [])}
    try:
        from core.wizard import setup_doctor
        out["newest"] = setup_doctor.newest_version(base.ecosystem, name, "")
    except Exception:
        out["newest"] = None
    if base.ecosystem == "pip":
        try:
            from importlib import metadata
            out["backend"] = metadata.version(name.split("[")[0])
        except Exception:
            out["backend"] = None
    return out


def request_runtime_fix(sid: str, kind: str) -> Dict[str, Any]:
    """8.4: the user sends the one-click fix of a held run ("use Java 17", "download Python 3.12") to the admin, who
    approves it in Mission Control > Escalations."""
    from core.wizard import escalations
    s = get(sid)
    chk = _runtime_check(s) or {"ok": True, "problems": []}
    prob = next((p for p in chk.get("problems") or [] if p["kind"] == kind), None)
    if not prob:
        s["runtime_check"] = None
        _persist(s)
        raise WizardError("This runtime already meets the tool's requirements - run it again.", 409)
    sent = dict((s.get("runtime_check") or {}).get("sent") or {})
    if sent.get(kind):
        raise WizardError(f"Already sent to your admin ({sent[kind]}).", 409)
    fix = prob.get("fix")
    req = prob["message"] + (f" Suggested fix: {fix['label']}." if fix else
                             " Understudy can't download a version that fits - please install one on the server.")
    esc = escalations.create_help(s, {"action": "use_version", "kind": kind, "fix": fix, "request": req,
                                      "version": (fix or {}).get("version") or ""}, prob["message"])
    sent[kind] = esc["id"]
    s["runtime_check"] = dict(chk, sent=sent)
    _persist(s)
    return s


def _send_runtime_help(s: Dict[str, Any], plan: Dict[str, Any]) -> Optional[str]:
    """8.3: the repair needs the admin - the user approved sending it. Creates a 'runtime help' escalation."""
    try:
        from core.wizard import escalations
        esc = escalations.create_help(s, plan.get("ask_admin") or {}, plan.get("explanation") or "",
                                      str(plan.get("ai_diagnosis") or "")[:600])
        return esc["id"]
    except Exception as e:                                     # pragma: no cover - surfaced in the log
        _activity(s, "RUNTIME", "Could not send the request to the admin", f"{type(e).__name__}: {e}", level="warn")
        return None


def _apply_runtime_repair(s: Dict[str, Any], execution: Dict[str, Any], log_tail: str,
                          plan: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """ENVIRONMENT failure: repair the chosen tool's runtime instead of switching tools. ``plan`` = the repair the user
    reviewed and approved (8.2: applied exactly); without it the next repair is planned here (automatic mode)."""
    spec = _spec(s)
    history = s.setdefault("runtime_repairs", [])
    if s.pop("_rerun_as_is", False) and plan is None:
        entry = {"at": _now(), "cause": runtime_repair.diagnose(execution, log_tail), "repair_id": "rerun",
                 "explanation": "Re-running the same tool as-is (you may have fixed the machine since the last attempt).",
                 "env": {}, "exhausted": False, "result": "APPLIED", "log": []}
        history.append(entry)
        _activity(s, "RUNTIME", "Runtime repair: re-run as-is after the user retried", entry["explanation"], {"repair": entry})
        return entry
    plan = dict(plan) if plan else _plan_runtime(s, execution, log_tail, list(s.get("runtime_feedback") or []) or None)
    for k, v in runtime_repair._empty(plan.get("cause") or "").items():
        plan.setdefault(k, v)
    entry = {"at": _now(), "cause": plan["cause"], "repair_id": plan["repair_id"], "explanation": plan["explanation"],
             "env": {k: v for k, v in (plan.get("env") or {}).items()}, "exhausted": plan["exhausted"], "result": None,
             "source": plan.get("source", "rules"), "path_prepend": list(plan.get("path_prepend") or []),
             "download_runtime": plan.get("download_runtime"), "java_release": plan.get("java_release"),
             "switch_version": plan.get("switch_version"), "run_args": list(plan.get("run_args") or []),
             "clean_project_files": bool(plan.get("clean_project_files")), "ask_admin": plan.get("ask_admin")}
    lines: List[str] = []
    if plan["exhausted"]:
        entry["result"] = "EXHAUSTED"
    elif plan.get("ask_admin"):
        esc_id = _send_runtime_help(s, plan)
        entry.update(result=f"SENT_TO_ADMIN {esc_id}" if esc_id else "FAILED: could not reach the admin",
                     exhausted=True, waiting_for_admin=bool(esc_id),
                     explanation=(plan["explanation"] + f" Sent to your admin ({esc_id}) - you'll get a notification "
                                  "when they answer; then run it again.") if esc_id else plan["explanation"])
    else:
        env = dict(s.get("runtime_env") or {})
        if plan.get("env") or plan.get("strip_driver_path") or plan.get("path_prepend"):
            if plan["cause"] == "JDK_MISMATCH" or "JAVA_HOME" in (plan.get("env") or {}):
                for k in ("JAVA_HOME", "__path_prepend"):          # the previous JDK attempt is replaced
                    env.pop(k, None)
            elif plan.get("env") or plan.get("strip_driver_path"):
                for k in ("SELENIUM_BROWSER", "CHROME_BINARY", "EDGE_BINARY", "CHROME_BIN", "PUPPETEER_EXECUTABLE_PATH",
                          "__strip_driver_path"):
                    env.pop(k, None)
            env.update(plan.get("env") or {})
            if plan.get("strip_driver_path"):
                env["__strip_driver_path"] = "1"
            if plan.get("path_prepend"):
                old = [p for p in str(env.get("__path_prepend") or "").split(os.pathsep) if p]
                env["__path_prepend"] = os.pathsep.join(list(dict.fromkeys(list(plan["path_prepend"]) + old)))
            entry["result"] = "APPLIED"
        if plan.get("download_runtime"):
            from core.wizard import runtimes
            try:
                info = runtimes.provision(plan["download_runtime"], lines.append)
                if info.get("env_name"):
                    env[info["env_name"]] = info["home"]
                old = [p for p in str(env.get("__path_prepend") or "").split(os.pathsep) if p]
                env["__path_prepend"] = os.pathsep.join(list(dict.fromkeys([info["bin"]] + old)))
                entry["result"] = "APPLIED"
                entry["env"].update({info["env_name"]: info["home"]} if info.get("env_name") else {})
            except Exception as e:
                entry["result"] = f"FAILED: {e}"
        if plan.get("java_release"):
            env["__java_release"] = str(int(plan["java_release"]))
            entry["result"] = "APPLIED"
        s["runtime_env"] = env
        if plan.get("switch_version"):                          # 8.3: another version the admin allows
            try:
                pinned = admin_policy.allowed_spec(spec.id, plan["switch_version"], user=s.get("owner"))
                if pinned:
                    s["framework"] = pinned.to_dict()
                    s["framework_base"] = pinned.to_dict()
                    s["dependency_lock"] = None
                    if s.get("suite"):
                        _lock_dependencies(s, s["suite"])
                    res = dependency_resolver.install(_spec(s), log=lines.append, approved=True)
                    s["dependencies"] = {**res, "framework_id": pinned.id, "approved": True, "logs": lines[-60:]}
                    entry["result"] = "APPLIED" if res["status"] in ("READY", "DRY_RUN", "SKIPPED") else res["status"]
                    spec = _spec(s)
            except admin_policy.PolicyError as e:
                entry["result"] = f"FAILED: {e}"
        if plan.get("run_args"):                                # 8.3: e.g. dotnet test Automation.csproj
            for key in ("framework", "framework_base"):
                if s.get(key):
                    cmd = list(s[key].get("run_command") or [])
                    s[key]["run_command"] = cmd + [a for a in plan["run_args"] if a not in cmd]
            try:
                validate_spec(_spec(s))
                entry["result"] = "APPLIED"
            except FrameworkValidationError as e:
                entry["result"] = f"FAILED: {e}"
        if plan.get("clean_project_files") and s.get("suite"):  # 8.3: MSB1011 - one project file only
            before = sorted((s["suite"].get("files") or {}).keys())
            _lock_dependencies(s, s["suite"])
            gone = [f for f in before if f not in (s["suite"].get("files") or {})]
            lines.append("Removed extra project files: " + (", ".join(gone) or "none found"))
            entry["result"] = "APPLIED"
        if plan.get("java_release") and s.get("suite"):
            _apply_java_release(s, s["suite"].setdefault("files", {}))
        if plan.get("reinstall"):
            res = dependency_resolver.install(spec, log=lines.append, approved=True, force=True)
            res["logs"] = lines[-60:]
            s["dependencies"] = {**res, "framework_id": spec.id, "approved": True}
            entry["result"] = res["status"]
        if plan.get("install_browsers"):
            cmd = runtime_repair.browser_install_command(spec.id)
            if cmd:
                res = dependency_resolver.run_tool_command(spec, cmd, log=lines.append)
                entry["result"] = "APPLIED" if res["ok"] else "FAILED: " + res["message"]
    entry["log"] = lines[-30:]
    history.append(entry)
    _activity(s, "RUNTIME", f"Runtime repair: {plan['repair_id'] or 'none left'} ({plan['cause']}"
              + (", AI" if plan.get("source") == "ai" else "") + ")", plan["explanation"],
              {"repair": entry}, level="warn" if plan["exhausted"] else "info",
              source="ACTUAL_AI_API" if plan.get("source") == "ai" else None)
    return entry


def _retry_job(s: Dict[str, Any], mode: str = "heal"):
    r = s["retry"]
    r["count"] += 1
    attempt = r["count"]
    execution = s.get("execution") or {}
    verification = s.get("verification") if s["pillars"].get("BUILD-EXECUTE", {}).get("status") == "PASS" else None
    if mode == "reference":
        # Explicit user choice: verify the process with Understudy's built-in Playwright driver. The user's tool and
        # generated code are unchanged and still exported.
        s["strategy_override"] = "reference"
        record = {"attempt": attempt, "category": "USER_CHOICE", "agent_source": "USER", "strategy_override": "reference",
                  "diagnosis": "You chose to verify the recorded process with Understudy's built-in Playwright driver. "
                               f"Your {_spec(s).name} code is unchanged and is still exported in the ZIP.",
                  "changes": [], "repair": None, "at": _now(), "outcome": "RUNNING"}
        r["history"].append(record)
        r["last_heal"] = record
        _run_after_heal(s, r, record, attempt)
        return
    _telemetry(s, "VERIFY", "ACTUAL_AI_API" if ai_available() else "LOCAL_HEURISTIC",
               f"Self-healing retry {attempt}" + (f"/{r['max']}" if r["max"] else "") + ": analysing logs and the live page", "",
               display_model() if ai_available() else "Deterministic self-healer")
    heal = verifier.self_heal(s["analysis"]["steps"], s["parameters"], s["target_url"], execution, verification,
                              execution.get("logs", []), s["healing_notes"], extra_urls=target_urls_of(s)[1:])
    record = {"attempt": attempt, "category": heal["category"], "diagnosis": heal["diagnosis"], "changes": heal["changes"],
              "agent_source": heal["agent_source"], "strategy_override": heal.get("strategy_override"),
              "repair": None, "at": _now(), "outcome": "RUNNING"}
    r["history"].append(record)
    r["last_heal"] = record
    if heal["category"] == "ENVIRONMENT" and not s.get("strategy_override"):
        repair = _apply_runtime_repair(s, execution, activity_log.strip_context("\n".join(execution.get("logs", [])[-150:])))
        record["repair"] = repair
        if repair["exhausted"]:
            record["outcome"] = "REPAIR_EXHAUSTED"
            record["diagnosis"] = heal["diagnosis"].split(" Understudy will repair")[0] + " " + repair["explanation"]
            r["count"] -= 1           # nothing was re-run, so this does not consume a retry
            r["suggest_reference"] = True
            s["current_step"] = 5
            _ask_user(s, repair["explanation"])
            return
        record["diagnosis"] = heal["diagnosis"] + " Repair: " + repair["explanation"]
    # Self-correct the agent context: steps + healing notes feed the next BUILD prompt.
    s["analysis"]["steps"] = heal["steps"]
    s["healing_notes"] = (s["healing_notes"] + heal.get("new_notes", []))[-10:]
    if heal.get("strategy_override"):
        s["strategy_override"] = heal["strategy_override"]
    _telemetry(s, "VERIFY", heal["agent_source"], f"Diagnosis ({heal['category']}): {heal['diagnosis'][:200]}",
               json.dumps(heal["changes"])[:400], heal.get("model"))
    _run_after_heal(s, r, record, attempt)


def _run_after_heal(s: Dict[str, Any], r: Dict[str, Any], record: Dict[str, Any], attempt: int,
                    approved_suite: Optional[Dict[str, Any]] = None):
    for p in ("GOVERN", "BUILD", "BUILD-EXECUTE", "EXPORT", "VERIFY"):
        s["pillars"].pop(p, None)
    s["verification"] = None
    # GOVERN again (mandatory - the payload against the rules) -> BUILD again (with healing notes, the rules in the
    # script) -> EXECUTE -> VERIFY
    res = governance.run_compliance_check(s["rules"], s["parameters"], s["sop_text"], {}, target_urls_of(s),
                                          s["analysis"]["steps"])
    s["compliance"] = res
    if not res["passed"]:
        _set_pillar(s, "GOVERN", "FAIL", "Re-validation after self-healing failed: " +
                    "; ".join(v["message"] for v in res["violations"][:3]), {"compliance": res})
        record["outcome"] = "FAIL_AT_GOVERN"
        s["current_step"] = 2
        s["status"] = "needs_help"
        raise WizardError("The healed automation did not pass governance re-validation.", 422, {"compliance": res})
    _set_pillar(s, "GOVERN", "PASS", f"Re-validated after self-healing retry {attempt}.", {"compliance": res})
    try:
        if approved_suite is not None:
            _install_suite(s, copy.deepcopy(approved_suite), retry_context=True)   # exactly the reviewed code
        else:
            _build_job(s, retry_context=True)
    except WizardError:
        record["outcome"] = "FAIL_AT_BUILD"
        s["current_step"] = 3
        s["status"] = "needs_help"
        raise
    exec_res = _do_execute(s, attempt + 1)
    if exec_res["status"] != "PASS":
        record["outcome"] = "FAIL_AT_EXECUTE"
        _after_execute(s, exec_res)
        return
    _set_pillar(s, "BUILD-EXECUTE", "PASS", f"Retry {attempt}: automation ran to completion "
                f"({exec_res['strategy']} strategy).", {k: v for k, v in exec_res.items() if k != "failure_html"})
    record["outcome"] = "PASS"
    s["current_step"] = 5
    _finish_export(s)


# ---------------------------------------------------------------------------
# Self-healing with the user in the loop (6.3, admin setting "ask" - the default)
# ---------------------------------------------------------------------------
MAX_HEAL_REVISIONS = int(os.environ.get("UNDERSTUDY_MAX_HEAL_REVISIONS") or 5)


def _diff_files(old: Dict[str, str], new: Dict[str, str], max_lines: int = 400) -> List[Dict[str, Any]]:
    import difflib
    out = []
    for name in sorted(set(old) | set(new)):
        a, b = old.get(name), new.get(name)
        if a == b:
            continue
        lines = list(difflib.unified_diff((a or "").splitlines(), (b or "").splitlines(), f"a/{name}", f"b/{name}",
                                          n=2, lineterm=""))
        added = sum(1 for l in lines if l.startswith("+") and not l.startswith("+++"))
        removed = sum(1 for l in lines if l.startswith("-") and not l.startswith("---"))
        out.append({"file": name, "status": "added" if a is None else "removed" if b is None else "changed",
                    "added": added, "removed": removed, "diff": "\n".join(lines[:max_lines]),
                    "truncated": len(lines) > max_lines})
    return out


def _propose_heal(s: Dict[str, Any], why: str, feedback: Optional[List[str]] = None, revision: int = 0):
    """Diagnose the failure and PREPARE the fix - new step plan, healing notes and the regenerated script - without
    running anything. The user sees what went wrong, what changed (step changes + script diff) and any question the AI
    has, then approves (approve_heal), corrects the AI (revise_heal) or stops."""
    r = s["retry"]
    feedback = list(feedback or [])
    attempt = r["count"] + 1
    spec = _spec(s)
    execution = s.get("execution") or {}
    verification = s.get("verification") if s["pillars"].get("BUILD-EXECUTE", {}).get("status") == "PASS" else None
    log_tail = activity_log.strip_context("\n".join(execution.get("logs", [])[-150:]))
    _telemetry(s, "VERIFY", "ACTUAL_AI_API" if ai_available() else "LOCAL_HEURISTIC",
               f"Self-healing retry {attempt}/{r['max']}: " + ("revising the fix with the user's correction"
                                                             if feedback else "diagnosing the failure"),
               feedback[-1] if feedback else "", display_model() if ai_available() else "Deterministic self-healer")
    heal = verifier.self_heal(s["analysis"]["steps"], s["parameters"], s["target_url"], execution, verification,
                              execution.get("logs", []), s["healing_notes"], user_feedback=feedback,
                              extra_urls=target_urls_of(s)[1:])
    p: Dict[str, Any] = {
        "id": "heal-" + uuid.uuid4().hex[:8], "attempt": attempt, "max": r["max"], "revision": revision,
        "max_revisions": MAX_HEAL_REVISIONS, "failure": why,
        "failure_reason": str(execution.get("failure_reason") or (s.get("verification") or {}).get("summary") or why)[:600],
        "failed_step": execution.get("failed_step"), "category": heal["category"], "diagnosis": heal["diagnosis"],
        "changes": heal.get("changes") or [], "notes": heal.get("new_notes") or [],
        "question": heal.get("question_for_user") or "", "feedback": feedback,
        "feedback_applied": heal.get("user_feedback_applied") or "", "agent_source": heal["agent_source"],
        "model": heal.get("model"), "strategy_override": heal.get("strategy_override"), "steps": heal["steps"],
        "kind": "script", "repair_plan": None, "script_diff": [], "created_at": _now(),
        "warning": r.get("duplicate_warning") or ""}
    if heal["category"] == "ENVIRONMENT" and not s.get("strategy_override"):
        plan = _plan_runtime(s, execution, log_tail, feedback)
        if plan["exhausted"]:
            r["suggest_reference"] = True        # nothing left to repair: the existing "your decision" path
            s["current_step"] = 5
            _ask_user(s, plan["explanation"])
            return
        p["kind"] = "runtime"
        p["repair_plan"] = {k: plan.get(k) for k in ("cause", "repair_id", "explanation", "env", "reinstall",
                                                     "install_browsers", "strip_driver_path", "path_prepend",
                                                     "download_runtime", "java_release", "source", "ai_diagnosis",
                                                     "switch_version", "run_args", "clean_project_files", "ask_admin")}
        if plan.get("ask_admin"):
            p["needs_admin"] = plan["ask_admin"]
        if plan.get("source") == "ai":
            p["agent_source"], p["model"] = plan.get("agent_source", "ACTUAL_AI_API"), plan.get("model")
        if feedback:
            p["feedback_applied"] = p["feedback_applied"] or ("Used your correction: " + plan["explanation"])
        p["diagnosis"] = heal["diagnosis"].split(" Understudy will repair")[0] + " Proposed repair: " + plan["explanation"]
    else:
        notes = (list(s["healing_notes"]) + list(p["notes"]))[-10:]
        p["healing_notes"] = notes
        try:
            from core.wizard import build_agent           # 9.0 heal agent: repairs the CODE against the live page
            fb = build_agent.failure_feedback(execution, (s.get("suite") or {}).get("files") or {}, log_tail[-3000:],
                                              heal.get("diagnosis") or "", feedback)
            out = build_agent.generate(spec, heal["steps"], s["parameters"], s["analysis"].get("api"),
                                       target_urls_of(s), notes, profile=_fresh_profile(s) or s.get("page_profile"),
                                       feedback=fb, log=lambda m: _activity(s, "STEP", m[:300], "", stage="VERIFY"),
                                       complexity=s.get("complexity"), task_context=_task_context(s))
            if out.get("diagnosis") and heal["agent_source"] == "ACTUAL_AI_API":
                p["diagnosis"] = p["diagnosis"] + " Code fix: " + out["diagnosis"]
            p["agent"] = out.get("agent")
            out["files"] = dependency_lock.apply_to_files(_effective_for(s, out)[1], out["files"])
            from core.wizard import robot_syntax
            out["files"], _fixed = robot_syntax.repair_files(out["files"])
            p["suite"] = out
            p["script_diff"] = _diff_files((s.get("suite") or {}).get("files") or {}, out["files"])
            p["generator"] = {"generator": out.get("generator"), "agent_source": out.get("agent_source"),
                              "model": out.get("model")}
            p["blocking"] = [f for f in out.get("findings") or [] if f.get("severity") == "blocking"]
        except ValueError as e:
            p["build_error"] = str(e)
    r["proposal"] = p
    r["awaiting_approval"] = True
    s["status"] = "awaiting_approval"
    changed = ", ".join(d["file"] for d in p["script_diff"]) or ("runtime only" if p["kind"] == "runtime" else "no file")
    _activity(s, "STEP", f"Self-healing fix for retry {attempt} ready - waiting for the user's approval",
              p["diagnosis"][:600], {"proposal_id": p["id"], "changes": p["changes"], "files_changed": changed,
                                     "question": p["question"], "revision": revision}, stage="VERIFY")
    _telemetry(s, "VERIFY", heal["agent_source"], f"Proposed fix ({heal['category']}) - waiting for approval",
               f"{p['diagnosis'][:300]} | files: {changed}" + (f" | question: {p['question']}" if p["question"] else ""),
               heal.get("model"))


def _require_proposal(s: Dict[str, Any], proposal_id: Optional[str]) -> Dict[str, Any]:
    r = s["retry"]
    if not r.get("awaiting_approval") or not r.get("proposal"):
        raise WizardError("There is no self-healing fix waiting for your approval.", 409)
    p = r["proposal"]
    if proposal_id and proposal_id != p["id"]:
        raise WizardError("The proposed fix has changed since you loaded it - please review the new one.", 409,
                          {"proposal_id": p["id"]})
    return p


def approve_heal(sid: str, proposal_id: Optional[str] = None, background: bool = True) -> Dict[str, Any]:
    """The user approved the proposed fix: apply exactly what was reviewed, re-check the rules, run and verify."""
    s = get(sid)
    p = _require_proposal(s, proposal_id)
    if p.get("build_error"):
        raise WizardError("This fix can't run because the script could not be generated: " + p["build_error"] +
                          " Correct the AI or stop.", 422)
    if p.get("blocking"):
        raise WizardError("This fix can't run because the regenerated script failed the safety scan. Correct the AI or "
                          "stop.", 422, {"findings": p["blocking"]})
    s["retry"].update(awaiting_approval=False, proposal=None)
    s["status"] = "healing"
    _activity(s, "USER_ACTION", f"User approved the self-healing fix for retry {p['attempt']}",
              p["diagnosis"][:400], {"proposal_id": p["id"], "revisions": p["revision"]})
    _run_job(s, "retry", lambda: _approved_heal_job(s, p), background)
    return s


def _approved_heal_job(s: Dict[str, Any], p: Dict[str, Any]):
    r = s["retry"]
    r["count"] += 1
    attempt = r["count"]
    record = {"attempt": attempt, "category": p["category"], "diagnosis": p["diagnosis"], "changes": p["changes"],
              "agent_source": p["agent_source"], "strategy_override": p.get("strategy_override"), "repair": None,
              "at": _now(), "outcome": "RUNNING", "approved_by": "user", "revisions": p["revision"],
              "feedback": p["feedback"], "files_changed": [d["file"] for d in p["script_diff"]]}
    r["history"].append(record)
    r["last_heal"] = record
    if p["kind"] == "runtime":
        execution = s.get("execution") or {}
        repair = _apply_runtime_repair(s, execution, activity_log.strip_context("\n".join(execution.get("logs", [])[-150:])),
                                       plan=p.get("repair_plan"))
        record["repair"] = repair
        if repair["exhausted"]:
            record["outcome"] = "REPAIR_EXHAUSTED"
            r["count"] -= 1
            r["suggest_reference"] = True
            s["current_step"] = 5
            _ask_user(s, repair["explanation"])
            return
        _run_after_heal(s, r, record, attempt, approved_suite=s.get("suite"))
        return
    s["analysis"]["steps"] = p["steps"]
    s["healing_notes"] = p.get("healing_notes") or s["healing_notes"]
    if p.get("strategy_override"):
        s["strategy_override"] = p["strategy_override"]
    _run_after_heal(s, r, record, attempt, approved_suite=p.get("suite"))


def revise_heal(sid: str, feedback: str, proposal_id: Optional[str] = None, background: bool = True) -> Dict[str, Any]:
    """The user says what the AI got wrong (or answers its question): the AI re-diagnoses with ALL corrections so far
    and prepares a new fix for review. Nothing runs until the user approves."""
    s = get(sid)
    p = _require_proposal(s, proposal_id)
    text = str(feedback or "").strip()
    if not text:
        raise WizardError("Please tell the AI what it got wrong, or answer its question.", 400, {"field": "feedback"})
    if len(text) > MAX_CLARIFICATION_CHARS:
        raise WizardError(f"Please keep the correction under {MAX_CLARIFICATION_CHARS} characters.", 400,
                          {"field": "feedback"})
    if p["revision"] >= MAX_HEAL_REVISIONS:
        raise WizardError(f"The AI has already revised this fix {MAX_HEAL_REVISIONS} times. Approve it, edit the steps "
                          f"or values yourself, or stop.", 409)
    tv = screen_text(text, "HEAL_FEEDBACK")
    if not tv.is_safe:
        _security_event(s, "HEAL_FEEDBACK", tv.pattern, text, tv.friendly_message)
        raise WizardError(tv.friendly_message, 400, {"field": "feedback", "security": True})
    fb = list(p["feedback"]) + [tv.sanitized]
    s["retry"].update(awaiting_approval=False, proposal=None)
    s["status"] = "healing"
    _activity(s, "USER_ACTION", f"User corrected the self-healing fix for retry {p['attempt']} "
              f"(revision {p['revision'] + 1})", tv.sanitized, {"proposal_id": p["id"]})
    _run_job(s, "heal_revise", lambda: _propose_heal(s, p["failure"], fb, p["revision"] + 1), background)
    return s


def _make_export(s: Dict[str, Any]):
    if not all(s["pillars"].get(p, {}).get("status") == "PASS" for p in PILLARS):
        raise WizardError("All five steps must pass before the ZIP can be created.", 403)
    spec = _spec(s)
    data = exporter.build_zip(s, spec)
    fname = f"understudy_{spec.id}_{s['session_id']}.zip"
    GENERATED_ZIP_CACHE[s["session_id"]] = data
    s["export"] = {"ready": True, "filename": fname, "size_bytes": len(data),
                   "download_url": f"/api/wizard/sessions/{s['session_id']}/export", "created_at": _now()}
    s["status"] = "completed"
    _sync_report(s)
    _telemetry(s, "EXPORT", "LOCAL_HEURISTIC", f"5/5 steps passed - packaged {fname} ({len(data)} bytes)", "",
               "Package Exporter")


# ---------------------------------------------------------------------------
# 8.3: runtime-help requests to the admin (escalations.create_help)
# ---------------------------------------------------------------------------
def run_identity(s: Dict[str, Any]) -> str:
    ex = s.get("execution") or {}
    return str(ex.get("run_dir") or ex.get("started_at") or "")


def set_runtime_help(sid: str, summary: Optional[Dict[str, Any]]) -> None:
    """8.3: the latest 'runtime help' request this session sent to the admin (shown in Step 4/5)."""
    s = get(sid)
    with _lock(sid):
        s["runtime_help"] = summary
        _persist(s)
    _publish_session(s, "runtime_help", status=(summary or {}).get("status"))


def admin_rerun(sid: str, admin: str) -> Dict[str, Any]:
    """8.3: after fixing a runtime problem the admin starts the user's Step 4 again (in the background)."""
    s = get(sid)
    if s.get("job", {}).get("status") == "running":
        raise WizardError("A run is already in progress.", 409)
    s["retry"].update(awaiting_decision=False, prompt=None, awaiting_approval=False, proposal=None, count=0)
    _activity(s, "USER_ACTION", f"Admin '{admin}' fixed the runtime and started the run again", "")
    return execute(sid, background=True)


def export_zip(sid: str) -> Dict[str, Any]:
    s = get(sid)
    if not all(s["pillars"].get(p, {}).get("status") == "PASS" for p in PILLARS):
        failed = next((p for p in PILLARS if s["pillars"].get(p, {}).get("status") != "PASS"), None)
        raise WizardError(f"The ZIP is locked until all 5 steps pass (step '{failed}' has not passed).", 403)
    if s["session_id"] not in GENERATED_ZIP_CACHE:
        _make_export(s)
    _activity(s, "USER_ACTION", "User downloaded the ZIP", s["export"].get("filename", ""))
    return {"filename": s["export"]["filename"], "data": GENERATED_ZIP_CACHE[s["session_id"]]}



# ---------------------------------------------------------------------------
# 8.0 zero-trust tool registry: SOP-required tools, PENDING_ADMIN_APPROVAL, resume / release
# ---------------------------------------------------------------------------
PENDING_STATUS = "pending_admin_approval"


def _detect_tool_requirement(s: Dict[str, Any]) -> None:
    from core.wizard import tool_requirements
    req = tool_requirements.detect(s.get("sop_text") or "", (s.get("analysis") or {}).get("sop_tool_requirement"))
    old = s.get("tool_requirement")
    s["tool_requirement"] = req
    if not req:
        return
    if old and old.get("tool_id") == req["tool_id"] and old.get("overridden"):
        req["overridden"] = old["overridden"]
    st = tool_requirements.status(req)
    what = req["name"] + (f" {req['version']}" if req.get("version") else "")
    _telemetry(s, "WATCH", "LOCAL_HEURISTIC" if req.get("source") == "sop" else "ACTUAL_AI_API",
               f"The SOP requires {what}: " + {"active": "approved and ready", "pending": "waiting for the admin",
                                                "version_not_allowed": "this version is not allowed yet",
                                                "blocked": "blocked by the admin",
                                                "unsupported": "Unsupported Framework - not enabled on this installation"
                                                }.get(st["status"], st["status"]),
               req.get("quote", ""), "SOP tool requirement")


def _require_not_pending(s: Dict[str, Any]) -> None:
    p = s.get("pending_request")
    if s.get("status") == PENDING_STATUS and p:
        raise WizardError(f"This session is waiting for the admin to enable {p.get('tool_name')}"
                          + (f" {p['version']}" if p.get("version") else "") + f" ({p.get('id')}). It continues "
                          "automatically once the tool is active - or pick an approved tool instead.", 409,
                          {"pending_admin_approval": True, "request": p})


def _publish_session(s: Dict[str, Any], what: str, **extra) -> None:
    try:
        from core.wizard import events
        events.publish("session.updated", {"session_id": s["session_id"], "what": what, "status": s.get("status"),
                                           "session_name": s.get("session_name"), "project_id": s.get("project_id"),
                                           **extra}, users=[s.get("owner") or "local"], admin=True)
    except Exception:
        pass


def _pause_for_request(s: Dict[str, Any], req: Dict[str, Any], what: str, reason: str) -> None:
    """The pipeline pauses: the session waits (PENDING_ADMIN_APPROVAL) and Mission Control's inbox gets the request."""
    from core.wizard import events
    created = len(req.get("sessions") or []) <= 1 and not req.get("conversation")
    s["request_outcome"] = None
    s["pending_request"] = {"id": req["id"], "tool_id": req["tool_id"], "tool_name": req.get("tool_name"),
                            "version": req.get("version"), "kind": req.get("kind", "tool"), "requested_at": _now(),
                            "step": s.get("current_step", 1), "reason": reason}
    s["status"] = PENDING_STATUS
    who = s.get("owner") or "local"
    try:
        admin_policy.add_message(req["id"], "user", "request",
                                 f"{who} asked for {what}" + (f": {reason}" if reason else "")
                                 + f" (session '{s.get('session_name') or s['session_id']}'"
                                 + (f", project '{s['project_name']}'" if s.get("project_name") else "") + ")",
                                 {"session_id": s["session_id"], "user": who})
    except admin_policy.PolicyError:
        pass
    events.publish("tool_request.created" if created else "tool_request.updated",
                   {"request": req["id"], "tool_id": req["tool_id"], "tool_name": req.get("tool_name"),
                    "version": req.get("version"), "status": req.get("status"), "user": who,
                    "session_id": s["session_id"], "reason": reason,
                    "registry_status": admin_policy.REGISTRY_STATUS.get(req.get("status", "pending"), "REQUESTED")},
                   users=[who], admin=True)
    _telemetry(s, "WATCH", "LOCAL_HEURISTIC", f"Session paused: waiting for the admin to enable {what} ({req['id']})",
               reason, "Zero-trust tool registry")
    _publish_session(s, "paused", request=req["id"])


def cancel_pending(sid: str) -> Dict[str, Any]:
    """The user stops waiting (the request stays with the admin) and continues with an approved tool."""
    s = get(sid)
    if not s.get("pending_request"):
        raise WizardError("This session isn't waiting for the admin.", 409)
    _activity(s, "USER_ACTION", "User stopped waiting for the admin", s["pending_request"].get("tool_name", ""),
              {"pending_request": s["pending_request"]})
    s["pending_request"] = None
    s["status"] = "active"
    _publish_session(s, "unpaused")
    _persist(s)
    return s


def _sessions_waiting_for(r: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for sid in r.get("sessions") or []:
        try:
            s = get(sid)
        except WizardError:
            continue
        if (s.get("pending_request") or {}).get("id") == r["id"]:
            out.append(s)
    return out


def resume_after_approval(r: Dict[str, Any]) -> List[str]:
    """The tool (or version) is ACTIVE: every session that paused for this request continues - the tool is selected
    (with the requested version) and its dependencies install - Step 2 is next."""
    resumed = []
    for s in _sessions_waiting_for(r):
        sid = s["session_id"]
        s["pending_request"] = None
        s["status"] = "active"
        s["request_outcome"] = {"id": r["id"], "status": "approved", "tool_id": r["tool_id"], "tool_name": r["tool_name"],
                                "version": r.get("version"), "note": r.get("admin_note", ""), "at": _now()}
        _activity(s, "STEP", f"{r['tool_name']}{' ' + r['version'] if r.get('version') else ''} is now active - "
                              "the session resumes", r.get("admin_note", ""), {"request": r["id"]})
        try:
            select_framework(sid, r["tool_id"], background=True, version=r.get("version") or None,
                             override_sop_tool=True)
            s["notes"].append(f"Your admin enabled {r['tool_name']} ({r['id']}); it is selected and installing.")
        except WizardError as e:
            s["notes"].append(f"{r['tool_name']} is active, but it couldn't be selected automatically: {e}")
        _persist(s)
        _publish_session(s, "resumed", request=r["id"], tool_id=r["tool_id"])
        _notify_owner(s, "tool_active", f"{r['tool_name']} is active - '{s.get('session_name') or sid}' continues",
                      f"Your admin enabled {r['tool_name']}{' ' + r['version'] if r.get('version') else ''} ({r['id']}). "
                      "The session resumed and is installing it.", "success")
        resumed.append(sid)
    return resumed


def _notify_owner(s: Dict[str, Any], kind: str, title: str, body: str, level: str = "info", email: bool = False) -> None:
    """8.3: a Studio notification (bell) for the session's owner."""
    try:
        from core.wizard import notifications
        notifications.notify(s.get("owner") or "local", kind, title, body, {"session_id": s["session_id"]}, level, email)
    except Exception:
        pass


def release_after_rejection(r: Dict[str, Any]) -> List[str]:
    out = []
    for s in _sessions_waiting_for(r):
        s["pending_request"] = None
        s["status"] = "active"
        s["request_outcome"] = {"id": r["id"], "status": "rejected", "tool_id": r["tool_id"], "tool_name": r["tool_name"],
                                "version": r.get("version"), "note": r.get("admin_note", ""), "at": _now()}
        note = f"Your admin rejected the request for {r['tool_name']}" + (f": {r['admin_note']}" if r.get("admin_note") else "") \
            + ". Pick an approved tool to continue."
        s["notes"].append(note)
        _activity(s, "STEP", note, "", {"request": r["id"]}, level="warn")
        _persist(s)
        _publish_session(s, "released", request=r["id"])
        _notify_owner(s, "tool_rejected", f"Your admin rejected {r['tool_name']}", note, "error", email=True)
        out.append(s["session_id"])
    return out


# ---------------------------------------------------------------------------
# 8.0 workspace explorer: duplicate a session
# ---------------------------------------------------------------------------
_COPY_KEYS = ("target_url", "target_urls", "pages", "pages_rejected", "pages_confirmed", "pages_meta", "pages_removed",
              "sop_text", "inventory", "analysis", "analysis_meta", "framework_options", "parameters",
              "parameter_problems", "rules", "rules_meta", "history_context", "history_refs", "tool_requirement",
              "clarifications", "healing_notes", "applied_skills", "runtime_env", "page_profile", "complexity",
              "inspection_plan")


def duplicate_session(sid: str, owner: Optional[str] = None) -> Dict[str, Any]:
    """A copy of the session's inputs and Step 1 result (recording, URL, SOP, analysis, payload, rules - not the code,
    the run or the evidence), as a new session in the same project, ready to continue from Step 2."""
    from core.wizard import projects
    src = get(sid)
    if owner is not None and src.get("owner") not in (owner, None):
        raise WizardError("Session not found", 404)
    if (src.get("job") or {}).get("status") == "running":
        raise WizardError("Wait until the session's current step finishes, then duplicate it.", 409)
    base = (src.get("session_name") or "Session") + " (copy)"
    name, n = base[:80], 2
    while src.get("project_id") and projects.session_name_taken(src.get("owner"), src["project_id"], name):
        name = f"{base[:74]} {n}"
        n += 1
    s = new_session(src.get("owner"), src.get("project_id"), name)
    for k in _COPY_KEYS:
        if k in src:
            s[k] = copy.deepcopy(src[k])
    if s.get("tool_requirement"):
        s["tool_requirement"].pop("overridden", None)
    if src.get("media"):
        m = copy.deepcopy(src["media"])
        if m.get("path") and os.path.exists(m["path"]):
            d = os.path.join(UPLOAD_ROOT, s["session_id"])
            os.makedirs(d, exist_ok=True)
            m["path"] = os.path.join(d, os.path.basename(m["path"]))
            shutil.copyfile(src["media"]["path"], m["path"])
        s["media"] = m
    s["rules_confirmed"] = False
    s["status"] = "active" if s.get("analysis") else "draft"
    if src.get("framework") and admin_policy.is_tool_allowed(src["framework"]["id"]):
        s["framework"] = copy.deepcopy(src["framework"])
        s["framework_base"] = copy.deepcopy(src.get("framework_base") or src["framework"])
        if src.get("pillars", {}).get("WATCH"):
            s["pillars"]["WATCH"] = copy.deepcopy(src["pillars"]["WATCH"])
        s["current_step"] = 2
        start_dependency_install(s, approved=True, background=True)
    else:
        s["current_step"] = 1
    _activity(s, "USER_ACTION", f"User duplicated the session '{src.get('session_name') or sid}'", "",
              {"source_session": sid})
    _sync_report(s)
    _persist(s)
    return s


def execution_logs(sid: str) -> Dict[str, Any]:
    """Everything a 'View Execution Logs' window needs: run log, dependency log, attempts, egress summary."""
    s = get(sid)
    ex = s.get("execution") or {}
    deps = s.get("dependencies") or {}
    return {"session_id": sid, "session_name": s.get("session_name"), "label": context_label(s),
            "status": ex.get("status"), "attempt": ex.get("attempt"), "strategy": ex.get("strategy"),
            "started_at": ex.get("started_at"), "lines": list(ex.get("logs") or []),
            "dependency_lines": list(deps.get("logs") or []), "dependency_status": deps.get("status"),
            "egress": ex.get("egress"), "retries": (s.get("retry") or {}).get("history") or []}
