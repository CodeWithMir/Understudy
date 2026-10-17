"""FastAPI router for Understudy, mounted at /api/wizard (see server.py).

All endpoints are plain ``def`` so FastAPI runs them in its thread pool (the wizard performs blocking
I/O: HTTP discovery, subprocesses, Playwright). ``?wait=true`` runs the step synchronously (tests/CLI);
by default long steps run in the background and the UI polls ``GET /sessions/{id}``.
"""
from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from core.wizard import WIZARD_VERSION, dependency_resolver, session as wz, uploads
from core.wizard.frameworks import CATALOG
from core.wizard.guardrails import parse_url_list
from core.wizard.llm import ai_available, display_model
from core.wizard.prompts import PROMPTS
from core.wizard.user_api import current_user

router = APIRouter(prefix="/api/wizard", tags=["Understudy"])


def _ok(s: Dict[str, Any], **extra) -> Dict[str, Any]:
    return {"session": wz.public_view(s), **extra}


def _err(e: wz.WizardError):
    raise HTTPException(status_code=e.status_code, detail={"message": str(e), "details": e.details})


def _own(sid: str, user: Optional[str]) -> None:
    """A user only ever reaches their own sessions; anything else looks like it does not exist."""
    try:
        s = wz.get(sid)
    except wz.WizardError as e:
        _err(e)
    if user is not None and s.get("owner") != user:
        raise HTTPException(404, detail={"message": "Session not found", "details": None})


@router.get("/info")
def wizard_info():
    return {"version": WIZARD_VERSION, "ai_mode": "ACTUAL_AI_API" if ai_available() else "LOCAL_HEURISTIC",
            "model": display_model() if ai_available() else None,
            **uploads.limit_info(), "max_self_heal_retries": wz.max_retries(),
            "dependency_install_mode": dependency_resolver.install_mode(),
            "sample_media_available": os.path.exists(wz.SAMPLE_MEDIA), "platform": _platform_info(),
            "user_login_required": _login_required(),
            "learned": _learned_stats()}


def _login_required() -> bool:
    from core.wizard import users
    return users.login_required()


def _learned_stats():
    try:
        from core.wizard import recall
        return recall.stats()
    except Exception:
        return {"enabled": False, "total": 0, "by_step": {}, "reuses": 0}


def _platform_info():
    """Where the AI runs (Vertex AI / AI Studio) and where state lives (local / Firestore) - no secrets."""
    from core.wizard import gemini, store
    g = gemini.describe()
    return {"ai": {k: g.get(k) for k in ("mode", "sdk", "platform", "project", "location")},
            "gcs_bucket": bool(g.get("gcs_bucket")), "media_cache": g.get("media_cache"),
            "store": store.describe()["kind"], "store_degraded": store.describe().get("degraded"),
            "cloud_run": bool(os.environ.get("K_SERVICE"))}


@router.get("/frameworks")
def framework_catalog(_user: Optional[str] = Depends(current_user)):
    """Tools users may pick (admin whitelist) plus the vetted catalog. Anything else needs an access request."""
    from core.wizard import admin_policy
    return {"allowed": [e["spec"] for t, e in admin_policy.whitelist().items() if admin_policy.user_may_use(t, _user)],
            "catalog": [dict(f.to_dict(), allowed=admin_policy.is_tool_allowed(f.id)) for f in CATALOG.values()],
            "note": "Only admin-approved tools can be used. Request any other tool and an admin can approve it."}


@router.get("/targets")
def quick_targets(_user: Optional[str] = Depends(current_user)):
    """Admin-approved target URLs (Mission Control URL exception list), shown as one-click targets in Step 1."""
    from core.wizard import admin_policy
    return {"targets": admin_policy.quick_targets()}


@router.get("/prompts")
def prompt_registry(_user: Optional[str] = Depends(current_user)):
    return {"prompts": [{"prompt_id": p.prompt_id, "stage": p.stage, "purpose": p.purpose, "system": p.system,
                         "user_template": p.user_template, "context_window": p.context_window,
                         "output_schema": p.output_schema, "called_from": p.called_from, "fallback": p.fallback}
                        for p in PROMPTS.values()]}


def _receive(media: Optional[UploadFile]):
    """Streams an upload to a temp file with the size cap. Returns (tmp_path, filename)."""
    if media is None or not media.filename:
        return None, None
    fd, tmp_path = tempfile.mkstemp(prefix="wizard-upload-")
    size = 0
    with os.fdopen(fd, "wb") as out:
        while True:
            chunk = media.file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > uploads.max_bytes():
                out.close()
                os.remove(tmp_path)
                raise HTTPException(413, detail={"message": f"File is larger than {uploads.max_bytes() // (1024 * 1024)} MB"})
            out.write(chunk)
    return tmp_path, media.filename


def _media(media: Optional[UploadFile], upload_id: str, user: Optional[str]):
    """The recording: a direct upload, or (9.9) one the browser put straight into Cloud Storage (``upload_id``)."""
    if (upload_id or "").strip():
        try:
            return uploads.take(upload_id.strip(), user)
        except uploads.UploadError as e:
            raise HTTPException(e.status_code, detail={"message": str(e), "details": {"field": "media"}})
    return _receive(media)


class UploadBody(BaseModel):
    filename: str = Field(..., max_length=300)
    size: int = Field(..., ge=0)
    origin: str = Field("", max_length=300)


@router.post("/uploads")
def begin_upload(body: UploadBody, request: Request, user: Optional[str] = Depends(current_user)):
    """9.9: how to upload a recording. With Cloud Storage: a resumable upload session the browser PUTs the file to
    directly (Cloud Run refuses request bodies over 32 MiB). Otherwise ``mode: direct`` - send it with Step 1."""
    p = uploads.plan(body.filename, body.size)
    if p["mode"] != "gcs":
        return p
    try:
        return uploads.begin(user, body.filename, body.size, request.headers.get("origin") or body.origin)
    except uploads.UploadError as e:
        raise HTTPException(e.status_code, detail={"message": str(e), "details": {"field": "media"}})


def _refs(context_refs: str) -> List[Dict[str, Any]]:
    """context_refs form field: JSON list of {type: session|project, id}."""
    if not (context_refs or "").strip():
        return []
    try:
        refs = json.loads(context_refs)
    except ValueError:
        raise HTTPException(400, detail={"message": "context_refs must be a JSON list.", "details": {"field": "context_refs"}})
    if not isinstance(refs, list) or not all(isinstance(r, dict) for r in refs):
        raise HTTPException(400, detail={"message": "context_refs must be a JSON list of {type, id} objects.",
                                         "details": {"field": "context_refs"}})
    return refs


@router.post("/sessions")
def create_session(target_url: str = Form(""), sop_text: str = Form(""), use_sample_media: bool = Form(False),
                   media: Optional[UploadFile] = File(None), wait: bool = Query(False),
                   project_id: str = Form(""), session_name: str = Form(""), context_refs: str = Form(""),
                   target_urls: str = Form(""), upload_id: str = Form(""),
                   user: Optional[str] = Depends(current_user)):
    """New session + Step 1 in one call. With user login on, the session must belong to one of your projects.
    9.4: ``target_urls`` - every page the process visits, in order (JSON list or one per line); ``target_url`` alone
    still works for a one-page process."""
    from core.wizard import users
    if users.login_required() and not project_id:
        raise HTTPException(400, detail={"message": "Choose a project first (the session is named for you).",
                                         "details": {"field": "project_id"}})
    refs = _refs(context_refs)
    tmp_path, fname = _media(media, upload_id, user)
    try:
        try:
            s = wz.create_session(target_url, sop_text, tmp_path, fname, use_sample_media=use_sample_media,
                                  background=not wait, owner=user, project_id=project_id or None,
                                  session_name=session_name or None, context_refs=refs,
                                  target_urls=parse_url_list(target_urls))
        except wz.WizardError as e:
            _err(e)
        return _ok(s)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)


@router.post("/sessions/{sid}/start")
def start_session(sid: str, target_url: str = Form(""), sop_text: str = Form(""), use_sample_media: bool = Form(False),
                  media: Optional[UploadFile] = File(None), wait: bool = Query(False), context_refs: str = Form(""),
                  target_urls: str = Form(""), upload_id: str = Form(""),
                  user: Optional[str] = Depends(current_user)):
    """Step 1 of a named session created in a project: recording + URL(s) + SOP + (optional) previous-session context.
    9.4: ``target_urls`` lists every page the process visits, in order (the first is where it starts)."""
    _own(sid, user)
    refs = _refs(context_refs)
    tmp_path, fname = _media(media, upload_id, user)
    try:
        try:
            s = wz.start_session(sid, target_url, sop_text, tmp_path, fname, use_sample_media=use_sample_media,
                                 background=not wait, context_refs=refs, target_urls=parse_url_list(target_urls))
        except wz.WizardError as e:
            _err(e)
        return _ok(s)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)


class HistoryBody(BaseModel):
    refs: List[Dict[str, Any]] = Field(default_factory=list, max_length=20)


@router.put("/sessions/{sid}/history")
def set_history(sid: str, body: HistoryBody, user: Optional[str] = Depends(current_user)):
    """Change the previous-session context; the next WATCH re-analysis ('Tell the AI') uses it."""
    _own(sid, user)
    try:
        return _ok(wz.set_history_context(sid, body.refs))
    except wz.WizardError as e:
        _err(e)


@router.delete("/sessions/{sid}")
def delete_session(sid: str, user: Optional[str] = Depends(current_user)):
    _own(sid, user)
    try:
        return wz.delete_session(sid, user)
    except wz.WizardError as e:
        _err(e)


@router.get("/sessions/{sid}")
def get_session(sid: str, user: Optional[str] = Depends(current_user)):
    _own(sid, user)
    try:
        return _ok(wz.get(sid))
    except wz.WizardError as e:
        _err(e)


@router.post("/sessions/{sid}/pending/cancel")
def cancel_pending(sid: str, user: Optional[str] = Depends(current_user)):
    """8.0: stop waiting for the admin (the enablement request stays) and continue with an approved tool."""
    _own(sid, user)
    try:
        return _ok(wz.cancel_pending(sid))
    except wz.WizardError as e:
        _err(e)


@router.post("/sessions/{sid}/duplicate")
def duplicate(sid: str, user: Optional[str] = Depends(current_user)):
    """8.0 workspace explorer: copy the session's inputs and Step 1 result into a new session of the same project."""
    _own(sid, user)
    try:
        return _ok(wz.duplicate_session(sid, user))
    except wz.WizardError as e:
        _err(e)


@router.get("/sessions/{sid}/execution-logs")
def execution_logs(sid: str, user: Optional[str] = Depends(current_user)):
    """8.0 workspace explorer: 'View Execution Logs' - run log, dependency log, retries, egress summary."""
    _own(sid, user)
    try:
        return wz.execution_logs(sid)
    except wz.WizardError as e:
        _err(e)


@router.get("/events")
def recent_events(after: float = 0.0, user: Optional[str] = Depends(current_user)):
    """Polling fallback for the live events (WebSocket /api/wizard/ws): this user's recent events."""
    from core.wizard import events
    return {"events": events.recent("user", user or "local", after), "now": __import__("time").time()}


@router.get("/sessions/{sid}/logs")
def get_logs(sid: str, since: int = 0, user: Optional[str] = Depends(current_user)):
    _own(sid, user)
    try:
        s = wz.get(sid)
    except wz.WizardError as e:
        _err(e)
    logs = (s.get("execution") or {}).get("logs", [])
    return {"since": since, "next": len(logs), "lines": logs[since:], "job": s.get("job"),
            "execution_status": (s.get("execution") or {}).get("status")}


class FrameworkChoice(BaseModel):
    framework_id: Optional[str] = None
    version: Optional[str] = Field(None, max_length=40)
    custom_name: Optional[str] = Field(None, max_length=200)
    override_sop_tool: bool = Field(False, description="8.0: use this tool although the SOP requires another one")


class ToolRequestBody(FrameworkChoice):
    reason: str = Field("", max_length=500)


@router.post("/sessions/{sid}/framework")
def choose_framework(sid: str, body: FrameworkChoice, wait: bool = Query(False), user: Optional[str] = Depends(current_user)):
    """Select an admin-whitelisted tool. Non-whitelisted tools return 403 with details.needs_approval."""
    _own(sid, user)
    if not body.framework_id and not body.custom_name:
        raise HTTPException(400, detail={"message": "Pick a framework or type the name of a tool."})
    try:
        return _ok(wz.select_framework(sid, body.framework_id, body.custom_name, background=not wait,
                                       version=body.version, override_sop_tool=body.override_sop_tool))
    except wz.WizardError as e:
        _err(e)


@router.post("/sessions/{sid}/tool-request")
def request_tool(sid: str, body: ToolRequestBody, user: Optional[str] = Depends(current_user)):
    """Ask the admin to approve a tool that isn't whitelisted yet."""
    _own(sid, user)
    if not body.framework_id and not body.custom_name:
        raise HTTPException(400, detail={"message": "Pick a framework or type the name of a tool."})
    try:
        return _ok(wz.request_tool_access(sid, body.framework_id, body.custom_name, body.reason, body.version))
    except wz.WizardError as e:
        _err(e)


class ClarifyBody(BaseModel):
    feedback: str = Field(..., min_length=1, max_length=4000)


@router.post("/sessions/{sid}/watch/clarify")
def clarify_watch(sid: str, body: ClarifyBody, wait: bool = Query(False), user: Optional[str] = Depends(current_user)):
    """Tell the AI what it missed. Screened by Model Armor; WATCH re-runs with every earlier correction as context."""
    _own(sid, user)
    try:
        return _ok(wz.clarify_watch(sid, body.feedback, background=not wait))
    except wz.WizardError as e:
        _err(e)


class PagesBody(BaseModel):
    urls: List[str] = Field(default_factory=list, max_length=20)
    rewatch: bool = False
    note: str = Field("", max_length=2000)


@router.put("/sessions/{sid}/pages")
def set_pages(sid: str, body: PagesBody, wait: bool = Query(False), user: Optional[str] = Depends(current_user)):
    """9.4: the user's checked / edited list of the pages the process visits (in order). ``rewatch`` asks the AI to
    watch the recording again with them (and ``note``)."""
    _own(sid, user)
    try:
        return _ok(wz.set_pages(sid, body.urls, rewatch=body.rewatch, note=body.note, background=not wait))
    except wz.WizardError as e:
        _err(e)


@router.post("/sessions/{sid}/pages/confirm")
def confirm_pages(sid: str, user: Optional[str] = Depends(current_user)):
    """9.4: the pages the AI read from the recording are right - Build may start."""
    _own(sid, user)
    try:
        return _ok(wz.confirm_pages(sid))
    except wz.WizardError as e:
        _err(e)


@router.post("/sessions/{sid}/watch/confirm")
def confirm_watch(sid: str, user: Optional[str] = Depends(current_user)):
    _own(sid, user)
    try:
        return _ok(wz.confirm_watch(sid))
    except wz.WizardError as e:
        _err(e)


class DifficultyParam(BaseModel):
    key: str = Field(..., max_length=40)
    value: Optional[float] = Field(None, ge=0, le=10)
    weight: Optional[float] = Field(None, ge=0, le=100)
    applicable: Optional[bool] = None


class DifficultyBody(BaseModel):
    parameters: List[DifficultyParam] = Field(default_factory=list, max_length=10)
    reset: bool = False


@router.post("/sessions/{sid}/difficulty/recalculate")
def recalculate_difficulty(sid: str, body: DifficultyBody, user: Optional[str] = Depends(current_user)):
    """9.8: the user's values / weights for the ten difficulty parameters -> new score and recommendation.
    ``reset`` goes back to the assessed values and the default weights."""
    _own(sid, user)
    try:
        return _ok(wz.recalculate_difficulty(sid, [p.model_dump() for p in body.parameters], reset=body.reset))
    except wz.WizardError as e:
        _err(e)


@router.get("/frameworks/{framework_id}/versions")
def framework_versions(framework_id: str, refresh: bool = False, _user: Optional[str] = Depends(current_user)):
    """Versions a user may pick for an approved tool: the admin's allowed list, or - when the admin set no
    restriction - every stable version currently in the tool's package registry."""
    try:
        return wz.versions_for_user(framework_id, refresh=refresh, user=_user)
    except wz.WizardError as e:
        _err(e)


class ParametersBody(BaseModel):
    parameters: List[Dict[str, Any]]


@router.put("/sessions/{sid}/parameters")
def put_parameters(sid: str, body: ParametersBody, user: Optional[str] = Depends(current_user)):
    _own(sid, user)
    try:
        return _ok(wz.update_parameters(sid, body.parameters))
    except wz.WizardError as e:
        _err(e)


@router.post("/sessions/{sid}/build")
def build(sid: str, wait: bool = Query(False), user: Optional[str] = Depends(current_user)):
    _own(sid, user)
    try:
        return _ok(wz.build_suite(sid, background=not wait))
    except wz.WizardError as e:
        _err(e)


@router.post("/sessions/{sid}/govern/extract")
def govern_extract(sid: str, user: Optional[str] = Depends(current_user)):
    _own(sid, user)
    try:
        return _ok(wz.extract_rules(sid))
    except wz.WizardError as e:
        _err(e)


class RulesBody(BaseModel):
    rules: List[Dict[str, Any]]
    confirmed: bool = False


@router.put("/sessions/{sid}/govern/rules")
def govern_rules(sid: str, body: RulesBody, user: Optional[str] = Depends(current_user)):
    _own(sid, user)
    try:
        return _ok(wz.update_rules(sid, body.rules, body.confirmed))
    except wz.WizardError as e:
        _err(e)


class SopBody(BaseModel):
    sop_text: str = Field(..., max_length=20000)


@router.post("/sessions/{sid}/govern/sop")
def govern_sop(sid: str, body: SopBody, user: Optional[str] = Depends(current_user)):
    _own(sid, user)
    try:
        return _ok(wz.update_sop(sid, body.sop_text))
    except wz.WizardError as e:
        _err(e)


@router.post("/sessions/{sid}/govern/check")
def govern_check(sid: str, user: Optional[str] = Depends(current_user)):
    _own(sid, user)
    try:
        return _ok(wz.compliance_check(sid))
    except wz.WizardError as e:
        _err(e)


@router.post("/sessions/{sid}/execute")
def execute(sid: str, wait: bool = Query(False), ignore_requirements: bool = Query(False),
            user: Optional[str] = Depends(current_user)):
    """8.4: a run whose tool doesn't meet its runtime requirements is held (``runtime_check``) unless the user chose
    ``ignore_requirements`` (run anyway)."""
    _own(sid, user)
    try:
        return _ok(wz.execute(sid, background=not wait, ignore_requirements=ignore_requirements))
    except wz.WizardError as e:
        _err(e)


class RuntimeFixBody(BaseModel):
    kind: str = Field(..., max_length=20)


@router.post("/sessions/{sid}/runtime-fix")
def request_runtime_fix(sid: str, body: RuntimeFixBody, user: Optional[str] = Depends(current_user)):
    """8.4: send the held run's one-click runtime fix (switch / download a version) to the admin for approval."""
    _own(sid, user)
    try:
        return _ok(wz.request_runtime_fix(sid, body.kind))
    except wz.WizardError as e:
        _err(e)


class RenameBody(BaseModel):
    name: str = Field(..., max_length=200)


@router.put("/sessions/{sid}/name")
def rename_session(sid: str, body: RenameBody, user: Optional[str] = Depends(current_user)):
    """9.1: rename a session (the AI names it after Step 1; a name you give is kept)."""
    _own(sid, user)
    try:
        return _ok(wz.rename_session(sid, body.name))
    except wz.WizardError as e:
        _err(e)


@router.post("/sessions/{sid}/export")
def create_export(sid: str, wait: bool = Query(False), user: Optional[str] = Depends(current_user)):
    """9.0 Step 5: create (or re-create) the ZIP - no page re-check, no escalation."""
    _own(sid, user)
    try:
        return _ok(wz.export(sid, background=not wait))
    except wz.WizardError as e:
        _err(e)


class RetryBody(BaseModel):
    feedback: Optional[str] = Field(None, max_length=4000,
                                    description="8.2: what is different on this machine, e.g. 'my JDK 17 is in /opt/jdk-17'")


@router.post("/sessions/{sid}/retry")
def retry(sid: str, body: Optional[RetryBody] = None, wait: bool = Query(False), mode: str = Query("heal"),
          user: Optional[str] = Depends(current_user)):
    _own(sid, user)
    try:
        return _ok(wz.retry(sid, background=not wait, mode=mode, feedback=(body.feedback if body else None)))
    except wz.WizardError as e:
        _err(e)


class HealApproveBody(BaseModel):
    proposal_id: Optional[str] = Field(None, max_length=40)


class HealReviseBody(BaseModel):
    proposal_id: Optional[str] = Field(None, max_length=40)
    feedback: str = Field(..., max_length=4000)


@router.post("/sessions/{sid}/heal/approve")
def heal_approve(sid: str, body: HealApproveBody, wait: bool = Query(False), user: Optional[str] = Depends(current_user)):
    """Run the self-healing fix the user just reviewed (diagnosis, step changes, script diff)."""
    _own(sid, user)
    try:
        return _ok(wz.approve_heal(sid, body.proposal_id, background=not wait))
    except wz.WizardError as e:
        _err(e)


@router.post("/sessions/{sid}/heal/revise")
def heal_revise(sid: str, body: HealReviseBody, wait: bool = Query(False), user: Optional[str] = Depends(current_user)):
    """Tell the AI what its fix got wrong (or answer its question); it prepares a new fix for review."""
    _own(sid, user)
    try:
        return _ok(wz.revise_heal(sid, body.feedback, body.proposal_id, background=not wait))
    except wz.WizardError as e:
        _err(e)


@router.post("/sessions/{sid}/retry/decline")
def decline(sid: str, user: Optional[str] = Depends(current_user)):
    _own(sid, user)
    try:
        return _ok(wz.decline_retry(sid))
    except wz.WizardError as e:
        _err(e)


@router.get("/sessions/{sid}/export")
def export(sid: str, user: Optional[str] = Depends(current_user)):
    _own(sid, user)
    try:
        out = wz.export_zip(sid)
    except wz.WizardError as e:
        _err(e)
    return Response(content=out["data"], media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{out["filename"]}"',
                             "Access-Control-Expose-Headers": "Content-Disposition"})


@router.get("/sessions/{sid}/media")
def session_media(sid: str, user: Optional[str] = Depends(current_user)):
    """Streams the session's own recording back (so the UI can jump to each step's moment in the video)."""
    _own(sid, user)
    from fastapi.responses import FileResponse
    import mimetypes
    try:
        s = wz.get(sid)
    except wz.WizardError as e:
        _err(e)
    path = (s.get("media") or {}).get("path")
    if not path or not os.path.exists(path) or not os.path.abspath(path).startswith(os.path.abspath(wz.UPLOAD_ROOT)):
        raise HTTPException(404, detail={"message": "No recording available"})
    return FileResponse(path, media_type=mimetypes.guess_type(path)[0] or "application/octet-stream",
                        headers={"Cache-Control": "private, max-age=3600"})


# ---------------------------------------------------------------------------
# 8.3: the user's notifications (bell)
# ---------------------------------------------------------------------------
@router.get("/notifications")
def list_notifications(limit: int = Query(50, ge=1, le=100), user: Optional[str] = Depends(current_user)):
    from core.wizard import notifications
    u = user or "local"
    return {"notifications": notifications.items(u, limit), "unread": notifications.unread_count(u)}


class NotificationsReadBody(BaseModel):
    ids: Optional[List[str]] = Field(None, max_length=100, description="omit to mark all as read")


@router.post("/notifications/read")
def read_notifications(body: NotificationsReadBody, user: Optional[str] = Depends(current_user)):
    from core.wizard import notifications
    u = user or "local"
    changed = notifications.mark_read(u, body.ids)
    return {"changed": changed, "unread": notifications.unread_count(u), "notifications": notifications.items(u)}
