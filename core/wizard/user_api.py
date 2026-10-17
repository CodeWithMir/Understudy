"""Studio (user-facing) API: login, projects, sessions inside projects, and the Step-1 history search.

    POST   /api/auth/login                       username (or email) + password; with two-step sign-in on the
                                                 answer is {two_factor_required, challenge} and a code is mailed
    POST   /api/auth/login/verify                {challenge_id, code} -> logged in (two-step sign-in, 9.6)
    POST   /api/auth/logout
    GET    /api/auth/me
    GET    /api/auth/options                     public: is self-signup on, does email work
    POST   /api/auth/signup                      {username, email, password, confirm_password} -> code mailed
    POST   /api/auth/signup/verify               {challenge_id, code} -> account created + logged in
    POST   /api/auth/password/forgot             {email} -> 10-minute code mailed (no login needed)
    POST   /api/auth/code/verify                 {challenge_id, code} -> one-time ticket
    POST   /api/auth/code/resend                 {challenge_id}
    POST   /api/auth/password/reset              {ticket, new_password, confirm_password}
    GET    /api/auth/profile                     (logged in) username, email, ...
    POST   /api/auth/profile/password/start      (logged in) code mailed to the account's email
    POST   /api/auth/profile/password/verify     (logged in) {challenge_id, code} -> ticket
    POST   /api/auth/profile/password/reset      (logged in) {ticket, new_password, confirm_password}
    POST   /api/auth/profile/two-factor/start    (logged in) code mailed - proves the mailbox before turning it on
    POST   /api/auth/profile/two-factor/verify   (logged in) {challenge_id, code} -> two-step sign-in on
    POST   /api/auth/profile/two-factor/disable  (logged in) {password} -> two-step sign-in off
    GET    /api/projects                         your projects, each with its sessions (summaries)
    POST   /api/projects                         {name, description}
    GET    /api/projects/{project_id}
    PUT    /api/projects/{project_id}            rename / new description
    DELETE /api/projects/{project_id}            deletes the project and its sessions
    POST   /api/projects/{project_id}/sessions   {name?} -> a new draft session (named by the AI after Step 1)
    GET    /api/history/search?q=                your sessions and projects matching the name (Step 1 context picker)

Every call needs ``X-User-Token`` (from /api/auth/login). A few GET downloads (recording, screenshots, ZIP) also
accept ``?access_token=`` because browsers cannot add headers to <video src> / <a href>.
"""
from __future__ import annotations

import re
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from core.wizard import activity_log, projects, users

auth_router = APIRouter(prefix="/api/auth", tags=["Studio login"])
projects_router = APIRouter(prefix="/api/projects", tags=["Projects"])
history_router = APIRouter(prefix="/api/history", tags=["History context"])


# Only these downloads may carry the token in the URL (a <video src> / <a href> cannot send headers); everywhere else
# it must be the X-User-Token header, so tokens don't end up in logs and Referer headers.
_URL_TOKEN_PATHS = re.compile(r"^/api/wizard/sessions/wiz-[A-Za-z0-9]+/(media|export|evidence/(before|after))$")


def current_user(request: Request, x_user_token: Optional[str] = Header(None)) -> Optional[str]:
    """The logged-in Studio user. With UNDERSTUDY_USER_LOGIN=off an anonymous caller is allowed (returns None)."""
    token = x_user_token
    if not token and request.method == "GET" and _URL_TOKEN_PATHS.match(request.url.path):
        token = request.query_params.get("access_token")
    claims = users.verify(token) if token else None
    if claims:
        return claims["username"]
    if not users.login_required():
        return None
    raise HTTPException(401, detail={"message": "Please log in to Understudy Studio.",
                                     "login_required": True})


def _perr(e: projects.ProjectError):
    raise HTTPException(e.status_code, detail={"message": str(e), "details": e.details})


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------
class LoginBody(BaseModel):
    username: str = Field(..., max_length=80)
    password: str = Field(..., max_length=200)


@auth_router.post("/login")
def login(body: LoginBody, request: Request):
    try:
        res = users.login(body.username, body.password, client=request.client.host if request.client else "")
    except users.UserError as e:
        activity_log.record("USER_ACTION", "Failed Studio login", f"username '{body.username[:40]}'", source="USER",
                            level="warn", data={"username": body.username[:40]})
        raise HTTPException(e.status_code, detail={"message": str(e)})
    if res.get("two_factor_required"):
        activity_log.record("USER_ACTION", f"User '{res['username']}' entered the right password - sign-in code sent",
                            res["challenge"]["email"], source="USER", data={"username": res["username"]})
        return res
    activity_log.record("USER_ACTION", f"User '{res['username']}' logged in to the Studio", "", source="USER",
                        data={"username": res["username"]})
    return res


class LoginCodeBody(BaseModel):
    challenge_id: str = Field(..., max_length=80)
    code: str = Field(..., max_length=12)


@auth_router.post("/login/verify")
def login_verify(body: LoginCodeBody, request: Request):
    """9.6: the second step of a two-step sign-in."""
    try:
        res = users.finish_login(body.challenge_id, body.code, client=request.client.host if request.client else "")
    except users.UserError as e:
        activity_log.record("USER_ACTION", "Failed sign-in code", str(e)[:200], source="USER", level="warn")
        raise HTTPException(e.status_code, detail={"message": str(e)})
    activity_log.record("USER_ACTION", f"User '{res['username']}' logged in to the Studio (two-step sign-in)", "",
                        source="USER", data={"username": res["username"]})
    return res


def _client(request: Request) -> str:
    return request.client.host if request.client else ""


def _uerr(e: users.UserError):
    raise HTTPException(e.status_code, detail={"message": str(e)})


@auth_router.get("/options")
def auth_options():
    from core.wizard import mailer
    st = mailer.status()
    from core.wizard import admin_policy
    return {"signup_enabled": users.signup_enabled(), "email_mode": st["mode"], "email_delivering": st["delivering"],
            "otp_minutes": 10, "signup_domains": admin_policy.signup_domains()}


class SignupBody(BaseModel):
    username: str = Field(..., max_length=80)
    email: str = Field(..., max_length=254)
    password: str = Field(..., max_length=200)
    confirm_password: str = Field(..., max_length=200)


class CodeBody(BaseModel):
    challenge_id: str = Field(..., max_length=80)
    code: str = Field(..., max_length=12)


class ResendBody(BaseModel):
    challenge_id: str = Field(..., max_length=80)


class ForgotBody(BaseModel):
    email: str = Field(..., max_length=254)


class ResetBody(BaseModel):
    ticket: str = Field(..., max_length=120)
    new_password: str = Field(..., max_length=200)
    confirm_password: str = Field(..., max_length=200)


@auth_router.post("/signup")
def signup(body: SignupBody, request: Request):
    try:
        ch = users.start_signup(body.username, body.email, body.password, body.confirm_password, _client(request))
    except users.UserError as e:
        _uerr(e)
    activity_log.record("USER_ACTION", f"Signup started for '{body.username[:40]}'", f"code sent to {ch['email']}",
                        source="USER", data={"username": body.username[:40]})
    return {"challenge": ch, "message": f"We sent a 6-digit code to {ch['email']}. It is valid for 10 minutes."}


@auth_router.post("/signup/verify")
def signup_verify(body: CodeBody):
    try:
        res = users.finish_signup(body.challenge_id, body.code)
    except users.UserError as e:
        _uerr(e)
    activity_log.record("USER_ACTION", f"User '{res['username']}' signed up (email verified)", "", source="USER",
                        data={"username": res["username"]})
    return res


@auth_router.post("/password/forgot")
def forgot_password(body: ForgotBody, request: Request):
    try:
        ch = users.start_reset(email=body.email, client=_client(request))
    except users.UserError as e:
        activity_log.record("USER_ACTION", "Forgot password refused", str(e)[:200], source="USER", level="warn")
        _uerr(e)
    activity_log.record("USER_ACTION", "Forgot password: code sent", ch["email"], source="USER")
    return {"challenge": ch, "message": f"We sent a 6-digit code to {ch['email']}. It is valid for 10 minutes."}


@auth_router.post("/code/verify")
def verify_code(body: CodeBody):
    try:
        return users.verify_code(body.challenge_id, body.code)
    except users.UserError as e:
        _uerr(e)


@auth_router.post("/code/resend")
def resend_code(body: ResendBody, request: Request):
    try:
        ch = users.resend_code(body.challenge_id, _client(request))
    except users.UserError as e:
        _uerr(e)
    return {"challenge": ch, "message": f"We sent a new code to {ch['email']}."}


@auth_router.post("/password/reset")
def reset_password(body: ResetBody):
    try:
        res = users.reset_with_ticket(body.ticket, body.new_password, body.confirm_password)
    except users.UserError as e:
        _uerr(e)
    activity_log.record("USER_ACTION", f"User '{res['user']['username']}' reset their password with an email code",
                        "every open session ended", source="USER", data={"username": res["user"]["username"]})
    return {"ok": True, "message": "Your password was changed. Log in with the new password."}


def _need_login(user: Optional[str]) -> str:
    if not user:
        raise HTTPException(401, detail={"message": "Please log in.", "login_required": True})
    return user


@auth_router.get("/profile")
def get_profile(user: Optional[str] = Depends(current_user)):
    try:
        return {"profile": users.profile(_need_login(user))}
    except users.UserError as e:
        _uerr(e)


@auth_router.post("/profile/password/start")
def profile_reset_start(request: Request, user: Optional[str] = Depends(current_user)):
    try:
        ch = users.start_reset(username=_need_login(user), client=_client(request))
    except users.UserError as e:
        _uerr(e)
    return {"challenge": ch, "message": f"We sent a 6-digit code to {ch['email']}. It is valid for 10 minutes."}


@auth_router.post("/profile/password/verify")
def profile_reset_verify(body: CodeBody, user: Optional[str] = Depends(current_user)):
    try:
        return users.verify_code(body.challenge_id, body.code, username=_need_login(user))
    except users.UserError as e:
        _uerr(e)


@auth_router.post("/profile/password/reset")
def profile_reset(body: ResetBody, user: Optional[str] = Depends(current_user)):
    try:
        users.reset_with_ticket(body.ticket, body.new_password, body.confirm_password, username=_need_login(user))
    except users.UserError as e:
        _uerr(e)
    activity_log.record("USER_ACTION", f"User '{user}' reset their password (Profile, email code)",
                        "every open session ended", source="USER", data={"username": user})
    return {"ok": True, "message": "Your password was changed and you were signed out everywhere. Log in again."}


class TwoFactorOffBody(BaseModel):
    password: str = Field(..., max_length=200)


@auth_router.post("/profile/two-factor/start")
def two_factor_start(request: Request, user: Optional[str] = Depends(current_user)):
    try:
        ch = users.start_two_factor(_need_login(user), _client(request))
    except users.UserError as e:
        _uerr(e)
    return {"challenge": ch, "message": f"We sent a 6-digit code to {ch['email']}. It is valid for 10 minutes."}


@auth_router.post("/profile/two-factor/verify")
def two_factor_verify(body: CodeBody, user: Optional[str] = Depends(current_user)):
    try:
        prof = users.confirm_two_factor(_need_login(user), body.challenge_id, body.code)
    except users.UserError as e:
        _uerr(e)
    activity_log.record("USER_ACTION", f"User '{user}' turned two-step sign-in on", "", source="USER",
                        data={"username": user})
    return {"profile": prof, "message": "Two-step sign-in is on. Next time, enter the code we email you after your password."}


@auth_router.post("/profile/two-factor/disable")
def two_factor_disable(body: TwoFactorOffBody, user: Optional[str] = Depends(current_user)):
    try:
        users.disable_two_factor(_need_login(user), body.password)
        prof = users.profile(user)
    except users.UserError as e:
        _uerr(e)
    activity_log.record("USER_ACTION", f"User '{user}' turned two-step sign-in off", "", source="USER", level="warn",
                        data={"username": user})
    return {"profile": prof, "message": "Two-step sign-in is off."}


@auth_router.post("/logout")
def logout(x_user_token: Optional[str] = Header(None)):
    users.logout(x_user_token)
    return {"ok": True}


@auth_router.get("/me")
def me(user: Optional[str] = Depends(current_user)):
    return {"username": user, "login_required": users.login_required()}


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------
class ProjectBody(BaseModel):
    name: str = Field(..., max_length=200)
    description: str = Field(..., max_length=4000)


class ProjectUpdateBody(BaseModel):
    name: Optional[str] = Field(None, max_length=200)
    description: Optional[str] = Field(None, max_length=4000)


@projects_router.get("")
def list_projects(user: Optional[str] = Depends(current_user)):
    return {"projects": projects.list_projects(user), "user": user}


@projects_router.post("")
def create_project(body: ProjectBody, user: Optional[str] = Depends(current_user)):
    try:
        p = projects.create_project(user, body.name, body.description)
    except projects.ProjectError as e:
        _perr(e)
    activity_log.record("USER_ACTION", f"User '{user or 'local'}' created the project '{p['name']}'", "",
                        source="USER", data={"project_id": p["project_id"]})
    return {"project": dict(p, sessions=[], session_count=0, completed=0)}


@projects_router.get("/{project_id}")
def get_project(project_id: str, user: Optional[str] = Depends(current_user)):
    try:
        p = projects.get_project(user, project_id)
    except projects.ProjectError as e:
        _perr(e)
    rows = sorted(projects.sessions_of(user, project_id), key=lambda r: r.get("created_at") or "", reverse=True)
    return {"project": dict(p, sessions=rows, session_count=len(rows),
                            completed=len([r for r in rows if r.get("status") == "completed"]))}


@projects_router.put("/{project_id}")
def update_project(project_id: str, body: ProjectUpdateBody, user: Optional[str] = Depends(current_user)):
    try:
        p = projects.update_project(user, project_id, body.name, body.description)
    except projects.ProjectError as e:
        _perr(e)
    return {"project": p}


@projects_router.delete("/{project_id}")
def delete_project(project_id: str, user: Optional[str] = Depends(current_user)):
    from core.wizard import session as wz
    try:
        out = projects.delete_project(user, project_id)
    except projects.ProjectError as e:
        _perr(e)
    except wz.WizardError as e:
        raise HTTPException(e.status_code, detail={"message": str(e)})
    activity_log.record("USER_ACTION", f"User '{user or 'local'}' deleted the project '{out['name']}'",
                        f"{out['sessions_deleted']} session(s) deleted", source="USER")
    return out


class SessionBody(BaseModel):
    # 9.1: optional - without a name the session is "New session" until the AI names it after Step 1
    name: str = Field("", max_length=200)


@projects_router.post("/{project_id}/sessions")
def create_session(project_id: str, body: SessionBody, user: Optional[str] = Depends(current_user)):
    from core.wizard import session as wz
    try:
        s = wz.new_session(user, project_id, body.name)
    except wz.WizardError as e:
        raise HTTPException(e.status_code, detail={"message": str(e), "details": e.details})
    return {"session": wz.public_view(s)}


# ---------------------------------------------------------------------------
# History (Step 1 context picker)
# ---------------------------------------------------------------------------
@history_router.get("/search")
def search(q: str = "", exclude: str = "", user: Optional[str] = Depends(current_user)):
    return projects.search(user, q[:120], exclude_session=exclude)
