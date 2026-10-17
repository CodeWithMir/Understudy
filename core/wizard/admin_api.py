"""Admin API (used by Mission Control): login, target-URL exceptions, tool whitelist / versions / access requests,
settings, telemetry, learned skills and code health.

Every endpoint except /login requires ``X-Admin-Token``: a session token from ``POST /api/admin/login`` (user
``admin``; the password is generated on the first start - core/wizard/admin_password.py) or the optional static ``UNDERSTUDY_ADMIN_TOKEN``.
"""
from __future__ import annotations

import json
from typing import List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from core.wizard import activity_log, admin_auth, code_health, dependency_resolver, skills, tool_versions, users
from core.wizard import admin_policy as ap
from core.wizard.frameworks import CATALOG, FrameworkSpec

router = APIRouter(prefix="/api/admin", tags=["Admin (Mission Control)"])


def require_admin(x_admin_token: Optional[str] = Header(None)) -> str:
    sess = admin_auth.session_for(x_admin_token)
    if not sess:
        raise HTTPException(401, detail={"message": "Please log in to Mission Control.", "login_required": True})
    return sess["user"]


class LoginBody(BaseModel):
    username: str = Field(..., max_length=80)
    password: str = Field(..., max_length=200)


@router.post("/login")
def login(body: LoginBody, request: Request):
    try:
        res = admin_auth.login(body.username, body.password, client=request.client.host if request.client else "")
    except admin_auth.AuthError as e:
        activity_log.record("ADMIN", "Failed Mission Control login", f"username '{body.username[:40]}'",
                            source="ADMIN", level="warn")
        raise HTTPException(e.status_code, detail={"message": str(e)})
    activity_log.record("ADMIN", f"Admin '{res['username']}' logged in", "", source="ADMIN")
    return res


@router.post("/logout")
def logout(x_admin_token: Optional[str] = Header(None)):
    admin_auth.logout(x_admin_token)
    return {"ok": True}


@router.post("/gcp/check")
def gcp_check(network: bool = True, who: str = Depends(require_admin)):
    """Google Cloud / Gemini set-up check: configuration, credentials and (network=true) a real Gemini call,
    Firestore and Cloud Storage round-trips. Each failure carries the command that fixes it."""
    from core.wizard import gcp_check as _chk
    rep = _chk.check(network=network)
    activity_log.record("ADMIN", f"Admin '{who}' ran the Google Cloud set-up check: {rep['status']}",
                        "; ".join(f"{c['name']}={c['status']}" for c in rep["checks"]), source="ADMIN",
                        level="info" if rep["status"] == "PASS" else "warn")
    return rep


@router.get("/me")
def me(who: str = Depends(require_admin)):
    return dict(admin_auth.status(), user=who)


class PasswordBody(BaseModel):
    current_password: str = Field(..., max_length=200)
    new_password: str = Field(..., max_length=200)
    new_username: Optional[str] = Field(None, max_length=40)


@router.put("/password")
def change_password(body: PasswordBody, who: str = Depends(require_admin), x_admin_token: Optional[str] = Header(None)):
    """Change the admin username/password. Kept in memory only: a restart falls back to the saved password."""
    try:
        st = admin_auth.change_credentials(x_admin_token or "", body.current_password, body.new_password,
                                           body.new_username)
    except admin_auth.AuthError as e:
        raise HTTPException(e.status_code, detail={"message": str(e)})
    _audit(f"Admin '{who}' changed the Mission Control credentials (until the next restart)")
    return st


def _policy_view():
    p = ap.snapshot()
    return {
        "url_exceptions": p["url_exceptions"],
        "url_blocklist": p.get("url_blocklist", []),
        "always_blocked": sorted(ap.NEVER_EXCEPTABLE_HOSTS),
        "tools": {
            "whitelist": [dict(e["spec"], approved_by=e["approved_by"], approved_at=e["approved_at"], whitelist_source=e["source"], builtin=bool(e.get("builtin")),
                               versions=ap.tool_version_policy(e["spec"]["id"]),
                               runtime=ap.tool_runtime(e["spec"]["id"]), access=ap.tool_access(e["spec"]["id"]),
                               toolchain_missing=dependency_resolver.check_toolchain(e["spec"].get("ecosystem", "none"),
                                                                                     e["spec"]["id"]))
                          for e in p["tool_whitelist"].values() if not e.get("builtin")],   # 9.9: built in - not managed here
            "catalog": [dict(f.to_dict(), allowed=f.id in p["tool_whitelist"],
                             blocked=bool((ap.tool_block_for(f) or {}).get("id")) and not (ap.tool_block_for(f) or {}).get("versions"))
                        for f in CATALOG.values() if not ap.is_builtin(f.id)],
            "requests": ap.requests(),
            "pending_count": len(ap.requests("pending")),
            "registry": ap.registry(),
            "blocklist": p.get("tool_blocklist", []),
            "default_tools": ap.default_whitelist(),
        },
        "settings": dict(p.get("settings", {}), self_heal_approval=ap.self_heal_approval(),
                         setup_fix_approval=ap.setup_fix_approval(), proxy_bypass=ap.proxy_bypass_hosts(),
                         setup_agent=ap.setup_agent_settings(), setup_lessons=len(ap.setup_lessons()),
                         ai_available=_ai_on()),
        "token_required": True,
        "auth": admin_auth.status(),
    }


def _ai_on() -> bool:
    try:
        from core.wizard.llm import ai_available
        return bool(ai_available())
    except Exception:
        return False


def _audit(title: str, detail: str = "", level: str = "info") -> None:
    activity_log.record("ADMIN", title, detail, source="ADMIN", level=level)


def _fail(e: Exception):
    raise HTTPException(400, detail={"message": str(e)})


@router.get("/policy")
def get_policy(_: str = Depends(require_admin)):
    return _policy_view()


class UrlBody(BaseModel):
    url: str = Field(..., max_length=2048)
    note: str = Field("", max_length=200)


@router.post("/url-exceptions")
def add_url(body: UrlBody, who: str = Depends(require_admin)):
    try:
        ap.add_url_exception(body.url, body.note, added_by=who)
    except ap.PolicyError as e:
        _fail(e)
    _audit(f"Admin added URL exception {body.url}", body.note)
    return _policy_view()


@router.delete("/url-exceptions/{entry_id}")
def remove_url(entry_id: str, _: str = Depends(require_admin)):
    try:
        ap.remove_url_exception(entry_id)
    except ap.PolicyError as e:
        _fail(e)
    _audit(f"Admin removed URL exception {entry_id}")
    return _policy_view()


@router.post("/url-blocklist")
def add_blocked(body: UrlBody, who: str = Depends(require_admin)):
    try:
        ap.add_blocked_url(body.url, body.note, added_by=who)
    except ap.PolicyError as e:
        _fail(e)
    _audit(f"Admin added {body.url} to the blocked URL list", body.note)
    return _policy_view()


@router.delete("/url-blocklist/{entry_id}")
def remove_blocked(entry_id: str, _: str = Depends(require_admin)):
    try:
        ap.remove_blocked_url(entry_id)
    except ap.PolicyError as e:
        _fail(e)
    _audit(f"Admin removed {entry_id} from the blocked URL list")
    return _policy_view()


class NetworkBody(BaseModel):
    mode: str = Field(..., max_length=20)


@router.put("/network-mode")
def set_network_mode(body: NetworkBody, who: str = Depends(require_admin)):
    try:
        ap.set_network_mode(body.mode)
    except ap.PolicyError as e:
        _fail(e)
    _audit(f"Admin set the execution network mode to '{body.mode}'")
    return _policy_view()


class ProxyBypassBody(BaseModel):
    hosts: list[str] = Field(default_factory=list, max_length=30)


@router.put("/proxy-bypass")
def put_proxy_bypass(body: ProxyBypassBody, who: str = Depends(require_admin)):
    """8.3: hosts runs reach without the per-run egress proxy (localhost / 127.0.0.1 / ::1 are always included)."""
    try:
        hosts = ap.set_proxy_bypass_hosts(body.hosts, who)
    except ap.PolicyError as e:
        _fail(e)
    _audit("Admin set the run-proxy bypass hosts", ", ".join(hosts))
    return _policy_view()


class ToolRuntimeBody(BaseModel):
    env_vars: Optional[dict[str, str]] = None
    path_prepend: Optional[list[str]] = Field(None, max_length=8)


@router.put("/tools/{tool_id}/runtime")
def put_tool_runtime(tool_id: str, body: ToolRuntimeBody, who: str = Depends(require_admin)):
    """8.3: the admin edits an approved tool's run settings (environment variables, PATH folders) - no AI needed."""
    try:
        rt = ap.set_tool_runtime(tool_id, body.env_vars, body.path_prepend, who)
    except ap.PolicyError as e:
        _fail(e)
    _audit(f"Admin changed the run settings of {tool_id}", json.dumps({"env": sorted((body.env_vars or {}).keys()),
                                                                     "path": body.path_prepend})[:300])
    return dict(_policy_view(), runtime=rt)


@router.post("/tools/{tool_id}")
def allow_catalog_tool(tool_id: str, who: str = Depends(require_admin)):
    try:
        ap.whitelist_catalog_tool(tool_id, approved_by=who)
    except ap.PolicyError as e:
        _fail(e)
    _audit(f"Admin whitelisted tool {tool_id}")
    return _policy_view()


@router.delete("/tools/{tool_id}")
def revoke_tool(tool_id: str, who: str = Depends(require_admin)):
    """9.0: deletes the tool for everyone and frees its isolated environments on the server."""
    try:
        res = ap.delete_tool(tool_id, who)
    except ap.PolicyError as e:
        _fail(e)
    _audit(f"Admin '{who}' deleted tool {tool_id}",
           f"{len(res['environments_removed'])} environment(s) removed from the server")
    return dict(_policy_view(), deleted=res)


class ToolAccessBody(BaseModel):
    mode: str = Field("local", max_length=10)        # 9.9.1: always 'local' (Cloud Run has no Docker)
    users: list[str] = Field(default_factory=list, max_length=500)


@router.put("/tools/{tool_id}/access")
def put_tool_access(tool_id: str, body: ToolAccessBody, who: str = Depends(require_admin)):
    """9.0: an approved tool is open to all users, only to the users the admin selected, or (9.9) to no user."""
    try:
        acc = ap.set_tool_access(tool_id, body.mode, body.users, who)
    except ap.PolicyError as e:
        _fail(e)
    _audit(f"Admin '{who}' set access to {tool_id}: " + {"all": "all users", "none": "no user"}.get(
        acc["mode"], f"{len(acc['users'])} selected user(s)"),
           ", ".join(acc["users"])[:300])
    return dict(_policy_view(), access=acc)


class DecisionBody(BaseModel):
    note: str = Field("", max_length=500)


# ---------------------------------------------------------------------------
# 7.2: blocked tools + AI tool setup (plan -> admin instructions -> approve -> sandbox install -> approved list)
# ---------------------------------------------------------------------------
class BlockToolBody(BaseModel):
    name: str = Field(..., max_length=80)
    versions: list[str] = Field(default_factory=list, max_length=30)
    note: str = Field("", max_length=200)


@router.post("/tool-blocklist")
def block_tool(body: BlockToolBody, who: str = Depends(require_admin)):
    try:
        e = ap.add_blocked_tool(body.name, body.versions, body.note, added_by=who)
    except ap.PolicyError as ex:
        _fail(ex)
    _audit(f"Admin blocked the tool '{e['name']}'" + (f" versions {', '.join(e['versions'])}" if e["versions"] else ""),
           body.note)
    return _policy_view()


@router.delete("/tool-blocklist/{entry_id}")
def unblock_tool(entry_id: str, _: str = Depends(require_admin)):
    try:
        ap.remove_blocked_tool(entry_id)
    except ap.PolicyError as e:
        _fail(e)
    _audit(f"Admin unblocked a tool ({entry_id})")
    return _policy_view()


def _setup_call(fn):
    from core.wizard import tool_setup
    try:
        return fn()
    except tool_setup.SetupError as e:
        raise HTTPException(e.status_code, detail={"message": str(e)})
    except ap.ToolBlocked as e:
        raise HTTPException(409, detail={"message": str(e), "blocked": True})
    except ap.PolicyError as e:
        _fail(e)


class AddToolBody(BaseModel):
    name: str = Field(..., max_length=200)
    version: Optional[str] = Field(None, max_length=40)
    instruction: Optional[str] = Field(None, max_length=2000)
    assistant: bool = Field(True, description="8.2: ask the AI for the stack first (languages, package managers, "
                                              "companion frameworks, versions); false = prepare the setup plan directly")


@router.post("/tool-setup")
def add_tool_with_ai(body: AddToolBody, who: str = Depends(require_admin)):
    """'Add a tool with AI': creates an admin request; the AI install assistant suggests the stack (8.2), or - with
    assistant=false - prepares the setup plan directly."""
    from core.wizard import tool_setup
    r = _setup_call(lambda: tool_setup.admin_add(body.name, who, body.version or None, body.instruction or None,
                                                 stack=body.assistant))
    return {"request": r, **_policy_view()}


# ---------------------------------------------------------------------------
# 9.9: agentic tools on Understudy's own agent - the admin picks the browser stack; install -> agent smoke test
# ---------------------------------------------------------------------------
class AgenticToolBody(BaseModel):
    driver: str = Field(..., max_length=20)
    browser: str = Field(..., max_length=20)
    name: str = Field("", max_length=60)
    driver_version: str = Field("", max_length=40)
    genai_version: str = Field("", max_length=40)
    extra_packages: List[str] = Field(default_factory=list, max_length=8)
    model: str = Field("", max_length=60)


@router.get("/agentic-tools/options")
def agentic_tool_options(who: str = Depends(require_admin)):
    from core.wizard import agent_stacks
    return agent_stacks.options()


@router.post("/agentic-tools")
def create_agentic_tool(body: AgenticToolBody, wait: bool = Query(False), who: str = Depends(require_admin)):
    """Sets up an agentic tool on Understudy's agent with the chosen browser stack: installs it in the sandbox, runs
    Understudy's agent smoke test (self-test mode, no Gemini key) and activates it when the test passes."""
    from core.wizard import agent_stacks
    from core.wizard.guardrails import screen_text
    if body.name:
        tv = screen_text(body.name, "TOOL_NAME")
        if not tv.is_safe:
            raise HTTPException(400, detail={"message": tv.friendly_message})
    try:
        r = agent_stacks.create(who, body.driver, body.browser, body.name, body.driver_version, body.genai_version,
                                body.extra_packages, body.model, background=not wait)
    except agent_stacks.StackError as e:
        raise HTTPException(e.status_code, detail={"message": str(e)})
    _audit(f"Admin set up an agentic tool on Understudy's agent: {r.get('tool_name')}",
           f"{body.driver} / {body.browser} - request {r.get('id')}")
    return {"request": r, **_policy_view()}


# ---------------------------------------------------------------------------
# 8.2: AI install assistant (stack suggestion -> admin selection) and the smoke-test review gate
# ---------------------------------------------------------------------------
def _stack_call(fn):
    from core.wizard import tool_stack
    try:
        return _setup_call(fn)
    except tool_stack.StackError as e:
        raise HTTPException(e.status_code, detail={"message": str(e)})


class StackSuggestBody(BaseModel):
    note: Optional[str] = Field(None, max_length=1000, description="e.g. 'we only use Java' or 'BDD with Cucumber'")


@router.post("/tool-requests/{request_id}/stack/suggest")
def stack_suggest(request_id: str, body: StackSuggestBody = StackSuggestBody(), who: str = Depends(require_admin)):
    from core.wizard import tool_stack
    r = _stack_call(lambda: tool_stack.suggest(request_id, who, body.note))
    return {"request": r, **_policy_view()}


@router.get("/tool-requests/{request_id}/stack/versions")
def stack_versions(request_id: str, language: str, package_manager: str, refresh: bool = False,
                   _: str = Depends(require_admin)):
    """Real versions (Maven Central / PyPI / npm) of every suggested component for this language + package manager."""
    from core.wizard import tool_stack
    return _stack_call(lambda: tool_stack.versions(request_id, language, package_manager, refresh))


class StackSelectBody(BaseModel):
    language: str = Field(..., max_length=20)
    package_manager: str = Field(..., max_length=20)
    package_managers_available: list[str] = Field(default_factory=list, max_length=8)
    components: list[str] = Field(default_factory=list, max_length=12)
    versions: dict[str, str] = Field(default_factory=dict)


@router.post("/tool-requests/{request_id}/stack/select")
def stack_select(request_id: str, body: StackSelectBody, who: str = Depends(require_admin)):
    """The admin's stack choice: Understudy builds the tool spec from it and the AI prepares the setup plan."""
    from core.wizard import tool_stack
    r = _stack_call(lambda: tool_stack.select(request_id, body.model_dump(), who))
    return {"request": r, **_policy_view()}


# ---------------------------------------------------------------------------
# 8.2: package managers, build tools and runtimes (Mission Control > Access & tools > Package managers)
# ---------------------------------------------------------------------------
def _pm_call(fn):
    from core.wizard import package_managers as pms
    try:
        return fn()
    except pms.PackageManagerError as e:
        raise HTTPException(e.status_code, detail={"message": str(e), "details": getattr(e, "details", {})})


@router.get("/package-managers")
def get_package_managers(_: str = Depends(require_admin)):
    """Every package manager / build tool Understudy can install with: enabled?, found on the server (with version),
    downloads it can do, the toolchains registered for the whole server and running downloads."""
    from core.wizard import package_managers as pms
    return pms.status()


class PmEnabledBody(BaseModel):
    enabled: bool


@router.put("/package-managers/{pm_id}")
def put_package_manager(pm_id: str, body: PmEnabledBody, who: str = Depends(require_admin)):
    from core.wizard import package_managers as pms
    return _pm_call(lambda: pms.set_enabled(pm_id, body.enabled, who))


class ToolchainDownloadBody(BaseModel):
    key: str = Field(..., max_length=60, description="9.9: jdk-<N> | node-<N> | python-3.<N> | maven-3|4 | gradle-<N> | "
                                                     "dotnet-<N> | ruby-3 | go-1 | conda-<package> (8.3)")
    confirm_eol: bool = Field(False, description="9.9: the admin confirmed an end-of-life release line")
    expect: list[str] = Field(default_factory=list, max_length=8, description="programs that must start afterwards")
    advice_id: Optional[str] = Field(None, max_length=20)


@router.post("/toolchains/download")
def download_toolchain(body: ToolchainDownloadBody, wait: bool = False, who: str = Depends(require_admin)):
    """Downloads a toolchain (checksum verified) into generated/runtimes and registers it for every install and run."""
    from core.wizard import package_managers as pms
    return _pm_call(lambda: pms.download(body.key, who, background=not wait, expect=body.expect or None,
                                         advice_id=body.advice_id, confirm_eol=body.confirm_eol))


class RuntimeAdviceBody(BaseModel):
    programs: list[str] = Field(..., min_length=1, max_length=6)
    context: str = Field("", max_length=300)


@router.post("/toolchains/advise")
def advise_toolchain(body: RuntimeAdviceBody, who: str = Depends(require_admin)):
    """8.3: the AI picks which checksum-verified download provides the missing programs on this server (e.g. ruby +
    gem -> Ruby 3 with RubyGems and Bundler) and explains it. Nothing is downloaded until the admin confirms."""
    from core.wizard import runtime_advisor
    try:
        return {"advice": runtime_advisor.advise(body.programs, body.context, who)}
    except ValueError as e:
        raise HTTPException(400, detail={"message": str(e)})


class ToolchainPathBody(BaseModel):
    kind: str = Field(..., max_length=10, description="jdk | maven | gradle | node | ruby | go | dotnet")
    path: str = Field(..., max_length=500)


@router.put("/toolchains")
def register_toolchain(body: ToolchainPathBody, who: str = Depends(require_admin)):
    """Points Understudy at an existing install (home folder, its bin folder, or the program)."""
    from core.wizard import package_managers as pms
    return _pm_call(lambda: pms.register_path(body.kind, body.path, who))


@router.delete("/toolchains/{kind}")
def remove_toolchain(kind: str, who: str = Depends(require_admin)):
    from core.wizard import package_managers as pms
    return _pm_call(lambda: pms.unregister(kind, who))


@router.post("/tool-requests/{request_id}/smoke/approve")
def smoke_approve(request_id: str, wait: bool = False, who: str = Depends(require_admin)):
    """The admin reviewed the AI-written smoke test: run it in the sandbox."""
    from core.wizard import tool_setup
    r = _setup_call(lambda: tool_setup.approve_smoke(request_id, who, background=not wait))
    return {"request": r, **_policy_view()}


class SmokeReviseBody(BaseModel):
    feedback: str = Field(..., max_length=2000)


@router.post("/tool-requests/{request_id}/smoke/revise")
def smoke_revise(request_id: str, body: SmokeReviseBody, wait: bool = False, who: str = Depends(require_admin)):
    """The admin tells the AI what to change in the smoke test; it comes back for review."""
    from core.wizard import tool_setup
    r = _setup_call(lambda: tool_setup.revise_smoke(request_id, who, body.feedback, background=not wait))
    return {"request": r, **_policy_view()}


class PlanBody(BaseModel):
    instruction: Optional[str] = Field(None, max_length=2000, description="e.g. 'download from https://mirror/simple'")


@router.post("/tool-requests/{request_id}/plan")
def plan_request(request_id: str, body: PlanBody = PlanBody(), who: str = Depends(require_admin)):
    from core.wizard import tool_setup
    r = _setup_call(lambda: tool_setup.prepare_plan(request_id, who, body.instruction))
    return {"request": r, **_policy_view()}


class SetupBody(BaseModel):
    allowed: Optional[list[str]] = Field(None, max_length=30)
    default: Optional[str] = Field(None, max_length=40)
    force: bool = False


@router.post("/tool-requests/{request_id}/setup")
def setup_request(request_id: str, body: SetupBody = SetupBody(), wait: bool = False, who: str = Depends(require_admin)):
    """The admin approves the plan: Understudy installs and checks the tool in the sandbox, then approves it."""
    from core.wizard import tool_setup
    r = _setup_call(lambda: tool_setup.apply_plan(request_id, who, body.allowed, body.default, body.force,
                                                   background=not wait))
    return {"request": r, **_policy_view()}


@router.get("/tool-requests/{request_id}")
def get_tool_request(request_id: str, _: str = Depends(require_admin)):
    try:
        return {"request": ap.get_request(request_id)}
    except ap.PolicyError as e:
        raise HTTPException(404, detail={"message": str(e)})


@router.post("/tool-requests/{request_id}/approve")
def approve(request_id: str, body: DecisionBody = DecisionBody(), who: str = Depends(require_admin)):
    try:
        ap.decide_request(request_id, True, body.note, decided_by=who)
    except ap.PolicyError as e:
        _fail(e)
    _audit(f"Admin approved tool request {request_id}", body.note)
    return _policy_view()


@router.post("/tool-requests/{request_id}/reject")
def reject(request_id: str, body: DecisionBody = DecisionBody(), who: str = Depends(require_admin)):
    from core.wizard import tool_setup
    _setup_call(lambda: tool_setup.reject(request_id, who, body.note))
    return _policy_view()


class AnswerBody(BaseModel):
    text: str = Field("", max_length=2000)
    option: str = Field("", max_length=40)


@router.post("/tool-requests/{request_id}/answer")
def answer_request(request_id: str, body: AnswerBody, wait: bool = False, who: str = Depends(require_admin)):
    """8.0 clarification loop: the admin answers the setup's question; the AI applies it and the setup resumes."""
    from core.wizard import tool_setup
    r = _setup_call(lambda: tool_setup.answer(request_id, who, body.text, body.option, background=not wait))
    return {"request": r, **_policy_view()}


@router.get("/tool-registry")
def tool_registry(_: str = Depends(require_admin)):
    """8.0 zero-trust registry: every tool with its lifecycle status (ACTIVE, REQUESTED, PROPOSED, INSTALLING,
    SMOKE_TESTING, NEEDS_ADMIN, FAILED, REJECTED)."""
    return {"tools": ap.registry(), "pending_count": len(ap.requests("pending"))}


@router.post("/tools/{tool_id}/smoke")
def rerun_smoke(tool_id: str, who: str = Depends(require_admin)):
    from core.wizard import tool_setup
    res = _setup_call(lambda: tool_setup.rerun_smoke(tool_id, who))
    return {"smoke": res, **_policy_view()}


@router.get("/inbox")
def inbox(after: float = 0.0, _: str = Depends(require_admin)):
    """Mission Control inbox: open tool requests + recent live events (polling fallback for /api/admin/ws)."""
    import time as _t
    from core.wizard import events
    open_reqs = ap.requests("pending")
    return {"open": [{k: r.get(k) for k in ("id", "kind", "tool_id", "tool_name", "version", "status", "requested_by",
                                            "sessions", "created_at", "reason")} | {"question": (r.get("clarification") or {}).get("question"),
                                                                                   "registry_status": ap.REGISTRY_STATUS.get(r["status"])}
                     for r in open_reqs],
            "needs_admin": len([r for r in open_reqs if r["status"] in ("pending", "stack_ready", "plan_ready", "smoke_review", "awaiting_admin")])
                           + _open_escalations(),
            "escalations_open": _open_escalations(),
            "events": events.recent("admin", None, after), "now": _t.time(),
            "live_sockets": events.subscriber_count()}


class SandboxBody(BaseModel):
    mode: str = Field("local", max_length=10)        # 9.9.1: always 'local' (Cloud Run has no Docker)
    memory_mb: Optional[int] = None
    timeout_s: Optional[int] = None
    install_timeout_s: Optional[int] = None


@router.get("/sandbox")
def get_sandbox(_: str = Depends(require_admin)):
    from core.wizard.sandbox import describe
    return describe()


@router.put("/sandbox")
def put_sandbox(body: SandboxBody, who: str = Depends(require_admin)):
    from core.wizard.sandbox import describe
    try:
        ap.set_sandbox(body.mode, by=who, memory_mb=body.memory_mb, timeout_s=body.timeout_s,
                       install_timeout_s=body.install_timeout_s)
    except ap.PolicyError as e:
        _fail(e)
    d = describe()
    _audit("Admin changed the sandbox limits", f"{d['settings']['memory_mb']} MB, {d['settings']['timeout_s']} s per run, "
           f"{d['settings']['install_timeout_s']} s per install")
    return d


class SettingsBody(BaseModel):
    max_self_heal_retries: int = Field(..., ge=1, le=50, description="automatic self-healing retries per run (1-50)")


@router.put("/settings")
def put_settings(body: SettingsBody, _: str = Depends(require_admin)):
    try:
        ap.set_max_self_heal_retries(body.max_self_heal_retries)
    except ap.PolicyError as e:
        _fail(e)
    _audit(f"Admin set the automatic self-healing retry limit to {body.max_self_heal_retries}")
    return _policy_view()


class HealApprovalBody(BaseModel):
    mode: str = Field(..., max_length=20)


@router.put("/self-heal-approval")
def put_heal_approval(body: HealApprovalBody, _: str = Depends(require_admin)):
    try:
        ap.set_self_heal_approval(body.mode)
    except ap.PolicyError as e:
        _fail(e)
    _audit("Admin set self-healing to " + ("ask the user before every retry" if body.mode == "ask"
                                           else "retry automatically without asking"))
    return _policy_view()


@router.put("/setup-fix-approval")
def put_setup_fix_approval(body: HealApprovalBody, _: str = Depends(require_admin)):
    """8.3: 'ask' - the AI's fix for a failed tool setup waits for the admin's click; 'automatic' - safe fixes apply."""
    try:
        ap.set_setup_fix_approval(body.mode)
    except ap.PolicyError as e:
        _fail(e)
    _audit("Admin set tool-setup fixes to " + ("ask before applying" if body.mode == "ask" else "apply safe fixes automatically"))
    return _policy_view()


class SetupAgentBody(BaseModel):
    enabled: Optional[bool] = None
    escalation_model: Optional[str] = Field(None, max_length=80)


@router.put("/setup-agent")
def put_setup_agent(body: SetupAgentBody, who: str = Depends(require_admin)):
    """8.5: the setup repair agent - on/off and the stronger model OFFERED for an error that comes back after a fix
    (8.5.1: only used when the admin clicks it)."""
    try:
        st = ap.set_setup_agent(body.enabled, body.escalation_model, who)
    except ap.PolicyError as e:
        _fail(e)
    _audit("Admin changed the setup agent", json.dumps(st)[:300])
    return _policy_view()


@router.post("/tool-requests/{request_id}/agent/again")
def setup_agent_again(request_id: str, body: dict, wait: bool = False, who: str = Depends(require_admin)):
    """8.5: the agent investigates the same failure again with the admin's hint (nothing is changed)."""
    from core.wizard import tool_setup
    try:
        return {"request": tool_setup.agent_again(request_id, who, str((body or {}).get("hint") or "")[:2000],
                                                   background=not wait, stronger=bool((body or {}).get("stronger")))}
    except tool_setup.SetupError as e:
        raise HTTPException(e.status_code, detail={"message": str(e)})


# ---------------------------------------------------------------------------
# Google Cloud Model Armor (7.2): configure, test and create a template from Mission Control
# ---------------------------------------------------------------------------
class ModelArmorBody(BaseModel):
    enabled: bool
    project: str = Field("", max_length=63)
    location: str = Field("", max_length=40)
    template_id: str = Field("", max_length=63)
    fail_closed: bool = False
    force: bool = Field(False, description="save fail-closed even though the connection test failed")


class ModelArmorTestBody(BaseModel):
    project: str = Field("", max_length=63)
    location: str = Field("", max_length=40)
    template_id: str = Field("", max_length=63)


class ModelArmorCreateBody(ModelArmorTestBody):
    filters: list[str] = Field(default_factory=lambda: list(_DEFAULT_ARMOR_FILTERS), max_length=4)
    confidence: str = Field("MEDIUM_AND_ABOVE", max_length=20)


_DEFAULT_ARMOR_FILTERS = ("pi_and_jailbreak", "malicious_uri", "rai")


def _armor_view(test: Optional[dict] = None) -> dict:
    from core.wizard import cloud_armor, gemini
    c = cloud_armor.config()
    parts = cloud_armor.split(c["template"]) if c["template"] else {}
    env_tpl = cloud_armor._env_template()
    return {
        **c, **parts,
        "suggested": {"project": parts.get("project") or gemini.vertex_project() or "",
                      "location": parts.get("location") or gemini.vertex_location() or "us-central1",
                      "template_id": parts.get("template_id") or "understudy-inputs"},
        "env_template": env_tpl, "env_fail_closed": cloud_armor._env_fail_closed(),
        "stats": dict(cloud_armor.STATS), "filters_available": list(cloud_armor.FILTERS),
        "default_filters": list(_DEFAULT_ARMOR_FILTERS), "service_account": cloud_armor.service_account(),
        "samples": {"clean": cloud_armor.SAMPLE_CLEAN, "attack": cloud_armor.SAMPLE_ATTACK},
        "test": test,
    }


def _armor_template(project: str, location: str, template_id: str) -> str:
    from core.wizard import cloud_armor
    try:
        return cloud_armor.template_name(project, location, template_id)
    except ValueError as e:
        _fail(e)


@router.get("/model-armor")
def get_model_armor(_: str = Depends(require_admin)):
    return _armor_view()


@router.put("/model-armor")
def put_model_armor(body: ModelArmorBody, who: str = Depends(require_admin)):
    """Turn Google Cloud Model Armor on/off. Turning it on runs the connection test first; fail-closed with a failing
    test is refused (409, needs_force) because every user input would then be blocked."""
    from core.wizard import cloud_armor
    tpl = _armor_template(body.project, body.location, body.template_id) if (body.enabled or body.template_id) else ""
    test = cloud_armor.test_template(tpl) if body.enabled else None
    if body.enabled and body.fail_closed and not (test or {}).get("reachable") and not body.force:
        raise HTTPException(409, detail={"message": "Model Armor could not be reached, and fail-closed would then block "
                                                    "every user input. Fix the connection first, or save anyway.",
                                         "needs_force": True, "test": test})
    try:
        ap.set_model_armor(body.enabled, tpl, body.fail_closed, by=who)
    except ap.PolicyError as e:
        _fail(e)
    _audit(f"Admin turned Google Cloud Model Armor {'ON' if body.enabled else 'OFF'}",
           (f"{tpl} · {'fail-closed' if body.fail_closed else 'fail-open'}" if tpl else "built-in Model Armor only")
           + (f" · test {'passed' if test.get('ok') else 'FAILED: ' + str(test.get('fix') or test.get('error'))}" if test else ""))
    return _armor_view(test)


@router.delete("/model-armor")
def reset_model_armor(_: str = Depends(require_admin)):
    """Forget the Mission Control setting: UNDERSTUDY_MODEL_ARMOR_TEMPLATE (if set) decides again."""
    ap.clear_model_armor()
    _audit("Admin reset Google Cloud Model Armor to the environment setting")
    return _armor_view()


@router.post("/model-armor/test")
def test_model_armor(body: ModelArmorTestBody, _: str = Depends(require_admin)):
    from core.wizard import cloud_armor
    tpl = _armor_template(body.project, body.location, body.template_id)
    test = cloud_armor.test_template(tpl)
    _audit("Admin tested Google Cloud Model Armor", f"{tpl}: " + ("passed" if test["ok"] else
                                                                  f"failed - {test.get('fix') or test.get('error')}"))
    return _armor_view(test)


@router.post("/model-armor/template")
def create_model_armor_template(body: ModelArmorCreateBody, _: str = Depends(require_admin)):
    """Creates a template in the admin's Google Cloud project (the server's identity needs roles/modelarmor.admin)."""
    from core.wizard import cloud_armor
    _armor_template(body.project, body.location, body.template_id)
    try:
        res = cloud_armor.create_template(body.project, body.location, body.template_id, body.filters, body.confidence)
    except ValueError as e:
        _fail(e)
    _audit("Admin created a Google Cloud Model Armor template" if res["ok"] and not res["already_existed"]
           else "Admin tried to create a Google Cloud Model Armor template",
           f"{res['template']}: " + ("created" if res["ok"] and not res["already_existed"] else
                                     "already existed" if res["already_existed"] else f"failed - {res['fix']}"))
    return {"create": res, **_armor_view()}


@router.get("/tools/{tool_id}/versions")
def list_versions(tool_id: str, refresh: bool = False, prereleases: bool = False, _: str = Depends(require_admin)):
    """Versions of the tool's primary package available in its registry right now (PyPI, npm, Maven Central, ...)."""
    entry = ap.whitelist().get(tool_id)
    spec = FrameworkSpec.from_dict(entry["spec"]) if entry else (CATALOG[tool_id] if tool_id in CATALOG else None)
    if spec is None:
        raise HTTPException(404, detail={"message": "Unknown tool"})
    out = tool_versions.available_versions(spec, refresh=refresh, include_prereleases=prereleases)
    out["policy"] = ap.tool_version_policy(tool_id)
    return out


class VersionsBody(BaseModel):
    allowed: list[str] = Field(default_factory=list, max_length=30)
    default: Optional[str] = Field(None, max_length=40)


@router.put("/tools/{tool_id}/versions")
def put_versions(tool_id: str, body: VersionsBody, who: str = Depends(require_admin)):
    try:
        pol = ap.set_tool_versions(tool_id, body.allowed, body.default, set_by=who)
    except ap.PolicyError as e:
        _fail(e)
    activity_log.record("ADMIN", f"Admin set allowed versions for {tool_id}",
                        ", ".join(pol["allowed"]) or "no restriction", {"tool_id": tool_id, "versions": pol}, source="ADMIN")
    return _policy_view()


@router.get("/telemetry")
def telemetry(after: int = 0, session_id: Optional[str] = None, kinds: Optional[str] = None, q: Optional[str] = None,
              limit: int = 500, user: Optional[str] = None, project: Optional[str] = None,
              _: str = Depends(require_admin)):
    """Detailed activity log: user actions, exact AI prompts and responses, errors, fallbacks, runtime and security.
    Every event of a run carries its context ([User] | [Project Name] | [Session Name]); filter with user/project."""
    res = activity_log.query(after=after, session_id=session_id or None,
                             kinds=[k.strip().upper() for k in kinds.split(",")] if kinds else None,
                             text=q or None, limit=max(1, min(limit, 2000)), user=user or None, project=project or None)
    res["stats"] = activity_log.stats()
    return res


@router.delete("/telemetry")
def clear_telemetry(_: str = Depends(require_admin)):
    activity_log.clear()
    return {"ok": True}


@router.get("/skills")
def list_skills(_: str = Depends(require_admin)):
    """Learned skills written by Understudy from real errors (folder: UNDERSTUDY_SKILLS_DIR, default ./skills)."""
    return {"dir": skills.skills_dir(), "skills": skills.list_skills()}


@router.get("/skills/{skill_id}")
def get_skill(skill_id: str, _: str = Depends(require_admin)):
    try:
        return skills.read_skill(skill_id)
    except KeyError:
        raise HTTPException(404, detail={"message": "Unknown skill"})


@router.delete("/skills/{skill_id}")
def delete_skill(skill_id: str, _: str = Depends(require_admin)):
    try:
        skills.delete_skill(skill_id)
    except KeyError:
        raise HTTPException(404, detail={"message": "Unknown skill"})
    _audit(f"Admin deleted learned skill {skill_id}")
    return {"dir": skills.skills_dir(), "skills": skills.list_skills()}


@router.get("/recall")
def list_recall(_: str = Depends(require_admin)):
    """Validated AI answers Understudy re-uses offline (core/wizard/recall.py) - no secrets, no raw SOP/page text."""
    from core.wizard import recall
    return {"dir": recall.root(), "stats": recall.stats(), "memories": recall.list_memories()}


@router.get("/recall/{memory_id}")
def get_recall(memory_id: str, _: str = Depends(require_admin)):
    from core.wizard import recall
    try:
        return recall.read_memory(memory_id)
    except KeyError:
        raise HTTPException(404, detail={"message": "Unknown memory"})


@router.delete("/recall/{memory_id}")
def delete_recall(memory_id: str, _: str = Depends(require_admin)):
    from core.wizard import recall
    try:
        recall.delete_memory(memory_id)
    except KeyError:
        raise HTTPException(404, detail={"message": "Unknown memory"})
    _audit(f"Admin deleted learned AI answer {memory_id}")
    return {"dir": recall.root(), "stats": recall.stats(), "memories": recall.list_memories()}


@router.delete("/recall")
def clear_recall(_: str = Depends(require_admin)):
    from core.wizard import recall
    n = recall.clear()
    _audit(f"Admin cleared {n} learned AI answers")
    return {"dir": recall.root(), "stats": recall.stats(), "memories": []}


# ---------------------------------------------------------------------------------------------------------------
# Code health (AI self-heal of start-up warnings)
# ---------------------------------------------------------------------------------------------------------------
def _ch_err(e: Exception):
    if isinstance(e, KeyError):
        raise HTTPException(404, detail={"message": "Unknown finding"})
    raise HTTPException(400, detail={"message": str(e)})


@router.get("/code-health")
def code_health_state(_: str = Depends(require_admin)):
    return code_health.state()


@router.post("/code-health/run")
def code_health_run(who: str = Depends(require_admin)):
    """Scan, then (with an AI key) propose + verify fixes; applies them only when auto-apply is on."""
    started = code_health.start_background("cycle")
    _audit(f"Admin '{who}' started a code health check" + ("" if started else " (already running)"))
    return code_health.state()


@router.post("/code-health/env/{fid}/advise")
def code_health_env_advise(fid: str, _: str = Depends(require_admin)):
    """Upgrade advice for a runtime/dependency warning (AI with a Gemini key, built-in steps otherwise)."""
    if fid not in {f["id"] for f in code_health.state()["environment"]}:
        raise HTTPException(404, detail={"message": "Unknown finding"})
    code_health.start_background("advise", fid=fid)
    return code_health.state()


@router.post("/code-health/env/{fid}/ignore")
def code_health_env_ignore(fid: str, _: str = Depends(require_admin)):
    try:
        return code_health.env_ignore(fid)
    except KeyError as e:
        _ch_err(e)


@router.post("/code-health/{fid}/propose")
def code_health_propose(fid: str, _: str = Depends(require_admin)):
    if fid not in {f["id"] for f in code_health.state()["findings"]}:
        raise HTTPException(404, detail={"message": "Unknown finding"})
    code_health.start_background("propose", fid=fid)
    return code_health.state()


@router.post("/code-health/{fid}/apply")
def code_health_apply(fid: str, who: str = Depends(require_admin)):
    try:
        return code_health.apply(fid, by=who)
    except (KeyError, ValueError) as e:
        _ch_err(e)


@router.post("/code-health/{fid}/ignore")
def code_health_ignore(fid: str, _: str = Depends(require_admin)):
    try:
        return code_health.ignore(fid)
    except KeyError as e:
        _ch_err(e)


@router.get("/code-health/history")
def code_health_history(_: str = Depends(require_admin)):
    """Every AI code change: who applied it (admin or auto-apply), why, which lines changed (diff), the backup, and
    whether the change is still in place / reverted / edited since."""
    return {"changes": code_health.history(), "summary": code_health.changes_summary(),
            "backups_dir": code_health.state()["backups_dir"]}


@router.get("/code-health/history/{hid}")
def code_health_change(hid: str, _: str = Depends(require_admin)):
    try:
        return code_health.history_detail(hid)
    except KeyError:
        raise HTTPException(404, detail={"message": "Unknown change"})


@router.get("/code-health/history/{hid}/backup")
def code_health_backup(hid: str, _: str = Depends(require_admin)):
    """The original file as it was before the AI changed it (download)."""
    from fastapi.responses import Response
    try:
        h = code_health.history_detail(hid)
        body = code_health.backup_content(hid)
    except KeyError:
        raise HTTPException(404, detail={"message": "Unknown change"})
    except FileNotFoundError:
        raise HTTPException(404, detail={"message": "The backup file no longer exists"})
    name = h["file"].replace("/", "_") + f".before-{hid}"
    return Response(content=body, media_type="text/plain; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"',
                             "Access-Control-Expose-Headers": "Content-Disposition"})


@router.post("/code-health/history/{hid}/rollback")
def code_health_rollback(hid: str, force: bool = False, who: str = Depends(require_admin)):
    """Revert an AI code change from its backup. 409 when the file was edited afterwards - retry with force=true
    (the current file is saved beside the backup first)."""
    try:
        return code_health.rollback(hid, by=who, force=force)
    except code_health.RollbackConflict as e:
        raise HTTPException(409, detail={"message": str(e), "needs_force": True})
    except KeyError:
        raise HTTPException(404, detail={"message": "Unknown change"})
    except ValueError as e:
        raise HTTPException(400, detail={"message": str(e)})


class CodeHealthSettings(BaseModel):
    auto_apply: bool


@router.put("/code-health/settings")
def code_health_settings(body: CodeHealthSettings, who: str = Depends(require_admin)):
    ap.set_code_health_auto_apply(body.auto_apply)
    _audit(f"Admin '{who}' turned code-health auto-apply {'on' if body.auto_apply else 'off'}")
    if body.auto_apply:
        code_health.start_background("cycle")
    return code_health.state()


# ---------------------------------------------------------------------------
# Studio users (core/wizard/users.py): only the admin creates accounts
# ---------------------------------------------------------------------------
class NewUserBody(BaseModel):
    username: str = Field(..., max_length=40)
    email: str = Field(..., max_length=254, description="Required (8.1); one email = one account")
    password: Optional[str] = Field(None, max_length=200,
                                    description="Optional. Empty = the user sets their own password with an emailed code")
    display_name: Optional[str] = Field(None, max_length=80)


class ReasonBody(BaseModel):
    reason: str = Field(..., max_length=500)


class EmailBody(BaseModel):
    email: str = Field(..., max_length=254)


class TestMailBody(BaseModel):
    to: str = Field(..., max_length=254)


def _uerr(e: "users.UserError"):
    raise HTTPException(e.status_code, detail={"message": str(e)})


@router.get("/users")
def list_users(_: str = Depends(require_admin)):
    from core.wizard import projects
    rows = list(store_iter("session_index"))
    out = []
    for u in users.list_users():
        mine = [r for r in rows if r.get("owner") == u["username"]]
        out.append(dict(u, projects=len([p for p in store_iter(projects.PROJECTS) if p.get("owner") == u["username"]]),
                        sessions=len(mine), completed=len([r for r in mine if r.get("status") == "completed"])))
    from core.wizard import mailer
    return {"users": sorted(out, key=lambda u: u["username"].lower()), "login_required": users.login_required(),
            "signup_enabled": users.signup_enabled(), "signup": users.signup_status(), "mail": mailer.status()}


def store_iter(collection: str):
    from core.wizard import store
    return store.iter_docs(collection)


@router.post("/users")
def create_user(body: NewUserBody, who: str = Depends(require_admin)):
    try:
        res = users.create_user(body.username, body.password, created_by=who, display_name=body.display_name or "",
                                email=body.email)
    except users.UserError as e:
        _uerr(e)
    _audit(f"Admin '{who}' created the Studio user '{res['user']['username']}'",
           f"email {res['user']['email']}; " + ("password set by the admin" if body.password else
                                                 "no password - the user sets one with an emailed code")
           + ("" if res["mail_sent"] else f"; welcome mail not sent: {res['mail_error']}"))
    return res


@router.post("/users/{username}/password-reset")
def ask_password_reset(username: str, who: str = Depends(require_admin)):
    try:
        res = users.request_reset(username, who)
    except users.UserError as e:
        _uerr(e)
    _audit(f"Admin '{who}' asked '{username}' to reset their password", "notice mailed; the user resets it with a code")
    return res


@router.put("/users/{username}/email")
def set_user_email(username: str, body: EmailBody, who: str = Depends(require_admin)):
    try:
        res = users.set_email(username, body.email, who)
    except users.UserError as e:
        _uerr(e)
    _audit(f"Admin '{who}' added the email of '{username}'", res["user"]["email"])
    return res


@router.post("/users/{username}/block")
def block_user(username: str, body: ReasonBody, who: str = Depends(require_admin)):
    try:
        res = users.block_user(username, body.reason, who)
    except users.UserError as e:
        _uerr(e)
    _audit(f"Admin '{who}' blocked the Studio user '{username}'", f"reason: {res['user']['blocked']['reason']}"
           + ("; mail sent" if res["mail_sent"] else f"; mail NOT sent: {res['mail_error']}"), level="warn")
    return res


@router.post("/users/{username}/two-factor/disable")
def disable_user_two_factor(username: str, who: str = Depends(require_admin)):
    """9.6: turns a user's two-step sign-in off (e.g. they lost access to their mailbox)."""
    try:
        user = users.disable_two_factor(username, by=who)
    except users.UserError as e:
        _uerr(e)
    _audit(f"Admin '{who}' turned two-step sign-in off for the Studio user '{username}'", "", level="warn")
    return {"user": user}


@router.post("/users/{username}/unblock")
def unblock_user(username: str, who: str = Depends(require_admin)):
    try:
        res = users.unblock_user(username, who)
    except users.UserError as e:
        _uerr(e)
    _audit(f"Admin '{who}' unblocked the Studio user '{username}'",
           "mail sent" if res["mail_sent"] else f"mail NOT sent: {res['mail_error']}")
    return res


@router.delete("/users/{username}")
def delete_user(username: str, body: Optional[ReasonBody] = None, reason: str = "", who: str = Depends(require_admin)):
    try:
        out = users.delete_user(username, (body.reason if body else "") or reason, by=who)
    except users.UserError as e:
        _uerr(e)
    _audit(f"Admin '{who}' deleted the Studio user '{username}'",
           f"{out['projects_archived']} project(s) and {out['sessions_archived']} session(s) kept for audit as "
           f"'{out['archived_as']}'" + ("; mail sent" if out["mail_sent"] else f"; mail NOT sent: {out['mail_error']}"),
           level="warn")
    return out


class SignupBody(BaseModel):
    enabled: Optional[bool] = Field(None, description="true / false; null = use UNDERSTUDY_SIGNUP again")


@router.get("/signup")
def get_signup(_: str = Depends(require_admin)):
    return users.signup_status()


@router.put("/signup")
def put_signup(body: SignupBody, who: str = Depends(require_admin)):
    ap.set_signup(body.enabled, by=who)
    st = users.signup_status()
    _audit(f"Admin '{who}' {'allowed' if st['enabled'] else 'turned off'} self-signup",
           "back to the UNDERSTUDY_SIGNUP default" if body.enabled is None else "")
    return st


class SignupDomainsBody(BaseModel):
    domains: Optional[list[str]] = Field(None, max_length=50,
                                         description="e.g. ['acme.com'] - only these email domains may self-sign-up; "
                                                     "[] = any domain; null = use UNDERSTUDY_SIGNUP_DOMAINS again")


@router.put("/signup/domains")
def put_signup_domains(body: SignupDomainsBody, who: str = Depends(require_admin)):
    try:
        ap.set_signup_domains(body.domains, by=who)
    except ap.PolicyError as e:
        _fail(e)
    st = users.signup_status()
    _audit(f"Admin '{who}' " + (f"limited self-signup to {', '.join('@' + d for d in st['domains'])}" if st["domains"]
                                else "allowed self-signup from any email domain"),
           "back to the UNDERSTUDY_SIGNUP_DOMAINS default" if body.domains is None else "")
    return st


@router.get("/mail")
def mail_status(_: str = Depends(require_admin)):
    from core.wizard import mailer
    return mailer.status()


@router.post("/mail/test")
def mail_test(body: TestMailBody, who: str = Depends(require_admin)):
    from core.wizard import mailer
    try:
        res = mailer.test_mail(body.to, who)
    except mailer.MailError as e:
        raise HTTPException(502, detail={"message": str(e), "mail": mailer.status()})
    _audit(f"Admin '{who}' sent a test email", res["to"])
    return dict(res, mail=mailer.status())


@router.post("/reset")
def reset(_: str = Depends(require_admin)):
    ap.reset_to_defaults()
    _audit("Admin reset the policy to defaults")
    return _policy_view()


# ---------------------------------------------------------------------------
# 8.3: escalations - a run that passed step 4 but failed the AI's verification, sent by the user for a human check
# ---------------------------------------------------------------------------
def _open_escalations() -> int:
    try:
        from core.wizard import escalations
        return escalations.open_count()
    except Exception:
        return 0


def _esc_call(fn):
    from core.wizard import escalations
    try:
        return fn()
    except escalations.EscalationError as e:
        raise HTTPException(e.status_code, detail={"message": str(e)})


@router.get("/escalations")
def list_escalations(status: Optional[str] = None, _: str = Depends(require_admin)):
    """All escalations (newest first). status=open | info_requested | confirmed | rejected | open_any."""
    from core.wizard import escalations
    rows = escalations.for_admin(status)
    return {"escalations": [{k: d.get(k) for k in ("id", "status", "owner", "project_name", "session_name", "session_id",
                                                  "created_at", "updated_at", "decided_by", "message", "question",
                                                  "kind")}
                            | {"attachments": len(d.get("attachments") or []),
                               "tool": (d.get("evidence") or {}).get("tool"),
                               "verify_summary": ((d.get("evidence") or {}).get("verify") or {}).get("summary")}
                            for d in rows],
            "open": sum(1 for d in rows if d.get("status") == "open")}


@router.get("/escalations/{esc_id}")
def get_escalation(esc_id: str, _: str = Depends(require_admin)):
    from core.wizard import escalations
    return {"escalation": _esc_call(lambda: escalations.get_for_admin(esc_id))}


class EscalationDecisionBody(BaseModel):
    decision: str = Field(..., max_length=10, description="confirm | reject | ask")
    note: str = Field("", max_length=2000)
    actions: list[dict] = Field(default_factory=list, max_length=8,
                                description="8.3 runtime help: allow_version {version} | set_env {name, value} | "
                                            "path_prepend {path} | proxy_bypass {host} | network_mode {mode}")
    rerun: bool = Field(False, description="8.3 runtime help: start the user's run again after the fix")
    scope: str = Field("everyone", max_length=10,
                       description="9.3 runtime help: 'everyone' (the tool for all users) or 'user' (only the user who "
                                   "asked: packages, allowed versions, run variables, PATH folders)")


@router.post("/escalations/{esc_id}/decide")
def decide_escalation(esc_id: str, body: EscalationDecisionBody, who: str = Depends(require_admin)):
    """confirm: VERIFY passes (confirmed by the admin), the ZIP is built and the user is notified. reject: the user is
    told why. ask: the user is asked for more information (they answer with text and attachments)."""
    from core.wizard import escalations
    return {"escalation": _esc_call(lambda: escalations.decide(esc_id, who, body.decision, body.note, body.actions,
                                                               body.rerun, body.scope))}


@router.get("/user-overrides")
def list_user_overrides(_: str = Depends(require_admin)):
    """9.3: every tool change the admin approved for one user only."""
    return {"overrides": ap.all_user_overrides()}


class UserOverrideDelete(BaseModel):
    user: str = Field(..., max_length=120)
    tool_id: str = Field(..., max_length=120)
    kind: str = Field(..., max_length=10)
    key: str = Field(..., max_length=500)


@router.post("/user-overrides/remove")
def remove_user_override(body: UserOverrideDelete, who: str = Depends(require_admin)):
    """9.3: take back a per-user change (the user's next run no longer gets it)."""
    changed = ap.remove_user_override(body.tool_id, body.user, body.kind, body.key)
    if not changed:
        raise HTTPException(404, detail={"message": "That per-user change doesn't exist (any more)."})
    return {"overrides": ap.all_user_overrides()}


@router.get("/escalations/{esc_id}/files/{file_id}")
def escalation_file(esc_id: str, file_id: str, _: str = Depends(require_admin)):
    from fastapi.responses import Response
    from core.wizard import escalations
    doc = _esc_call(lambda: escalations.get_for_admin(esc_id))
    path, att = _esc_call(lambda: escalations.attachment_path(doc, file_id))
    inline = att["content_type"].startswith("image/") and att["ext"] != ".svg"
    with open(path, "rb") as fh:
        return Response(content=fh.read(), media_type=att["content_type"] if inline else "application/octet-stream",
                        headers={"Content-Disposition": f'{"inline" if inline else "attachment"}; filename="{att["name"]}"',
                                 "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'"})


@router.get("/escalations/{esc_id}/evidence/{which}")
def escalation_evidence(esc_id: str, which: str, _: str = Depends(require_admin)):
    """The run's own before / after screenshots (PNG), as the verifier captured them."""
    import os as _os
    from fastapi.responses import Response
    from core.wizard import escalations, session as wz
    if which not in ("before", "after"):
        raise HTTPException(404, detail={"message": "Unknown evidence image"})
    doc = _esc_call(lambda: escalations.get_for_admin(esc_id))
    try:
        s = wz.get(doc["session_id"])
    except wz.WizardError as e:
        raise HTTPException(e.status_code, detail={"message": str(e)})
    path = (((s.get("verification") or {}).get("evidence") or {}).get(f"{which}_screenshot"))
    if not path or not _os.path.exists(path) or not _os.path.abspath(path).startswith(_os.path.abspath(wz.WORK_ROOT)):
        raise HTTPException(404, detail={"message": "No screenshot available"})
    with open(path, "rb") as fh:
        return Response(content=fh.read(), media_type="image/png")


# ---------------------------------------------------------------------------------------------------------------
# 8.4: Environments - runtime versions on the server, per tool, and the versions each tool needs
# ---------------------------------------------------------------------------------------------------------------
def _env_call(fn):
    from core.wizard import environments
    try:
        return fn()
    except environments.EnvironmentError_ as e:
        raise HTTPException(e.status_code, detail={"message": str(e), "details": getattr(e, "details", {})})


@router.get("/environments")
def get_environments(refresh: bool = False, _: str = Depends(require_admin)):
    """Every installed version of Java, Python, Node.js, Ruby, Go, .NET, Maven and Gradle on the server (path, source,
    which one is the default), browsers, and for every approved tool the versions it uses and needs."""
    from core.wizard import environments
    return environments.overview(refresh)


class EnvSelectBody(BaseModel):
    kind: str = Field(..., max_length=20)
    install_id: Optional[str] = Field(None, max_length=40)
    verify: bool = True


@router.post("/environments/tools/{tool_id}/select")
def env_select(tool_id: str, body: EnvSelectBody, who: str = Depends(require_admin)):
    """Point one tool at another installed version (install_id from GET /environments), or back to the default."""
    from core.wizard import environments
    return _env_call(lambda: {"tool": environments.select(tool_id, body.kind, body.install_id, who, body.verify)})


class EnvRequirementsBody(BaseModel):
    requirements: dict[str, str] = Field(..., max_length=10)


@router.put("/environments/tools/{tool_id}/requirements")
def env_requirements(tool_id: str, body: EnvRequirementsBody, who: str = Depends(require_admin)):
    """The admin's version requirements for a tool: {"java": "17+", "python": ">=3.9,<3.13", "node": ""} - an empty
    value goes back to Understudy's rule, "any" turns the check off for that runtime."""
    from core.wizard import environments
    return _env_call(lambda: {"tool": environments.set_requirements(tool_id, body.requirements, who)})


@router.get("/environments/tools/{tool_id}/check")
def env_check(tool_id: str, _: str = Depends(require_admin)):
    from core.wizard import environments
    return _env_call(lambda: environments.check(tool_id))


# ---------------------------------------------------------------------------------------------------------------
# 9.9: mirrors for runtime downloads (an internal Artifactory / Nexus) - core/wizard/runtime_mirrors.py
# ---------------------------------------------------------------------------------------------------------------
class MirrorBody(BaseModel):
    url: str = Field("", max_length=400, description="https://... base address of the mirror; empty = the official site")


def _mirror_call(fn):
    from core.wizard import runtime_mirrors
    try:
        return fn()
    except runtime_mirrors.MirrorError as e:
        raise HTTPException(e.status_code, detail={"message": str(e)})


@router.get("/runtime-mirrors")
def get_runtime_mirrors(_: str = Depends(require_admin)):
    """Where each runtime source (Adoptium, nodejs.org, Maven Central ...) is fetched from."""
    from core.wizard import runtime_mirrors
    return {"sources": runtime_mirrors.view()}


@router.put("/runtime-mirrors/{source}")
def put_runtime_mirror(source: str, body: MirrorBody, who: str = Depends(require_admin)):
    """Sets (or with an empty url removes) the mirror of one source. Recorded in the audit log."""
    from core.wizard import runtime_mirrors
    return _mirror_call(lambda: {"sources": runtime_mirrors.set_mirror(source, body.url, who)})


@router.post("/runtime-mirrors/{source}/test")
def test_runtime_mirror(source: str, body: MirrorBody, _: str = Depends(require_admin)):
    """Fetches one small known file of the source through the mirror (body.url: test an address before saving it)."""
    from core.wizard import runtime_mirrors
    return _mirror_call(lambda: runtime_mirrors.test(source, body.url or None))


class EnvDownloadBody(BaseModel):
    key: str = Field(..., max_length=40)
    tool_id: Optional[str] = Field(None, max_length=120)
    confirm_eol: bool = False                     # 9.9: the admin confirmed an end-of-life release line


@router.post("/environments/download")
def env_download(body: EnvDownloadBody, who: str = Depends(require_admin)):
    """Install a version side by side (checksum verified). With tool_id the tool switches to it when it's ready."""
    from core.wizard import environments
    _audit(f"Admin started downloading {body.key} side by side" + (f" for {body.tool_id}" if body.tool_id else ""))
    return _env_call(lambda: environments.download(body.key, who, tool_id=body.tool_id, confirm_eol=body.confirm_eol))
