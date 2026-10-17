"""Outgoing email (OTP codes, account notices) - any SMTP server, no extra dependency (Python's ``smtplib``).

Free options (setup in README section 2.0.2):
* **Gmail / Google Workspace** - smtp.gmail.com:587 (STARTTLS) with a Google *App Password* (free, ~500 mails/day).
* **Brevo** (free plan, 300 mails/day) - smtp-relay.brevo.com:587 with an SMTP key.
* Not Outlook.com personal accounts: Microsoft retired password-based SMTP there in September 2025 (OAuth only).

Configuration (``.env`` / environment - the password never goes to the admin policy, telemetry, logs or the ZIP):

    UNDERSTUDY_MAIL_MODE=smtp | outbox | off     default: smtp when UNDERSTUDY_SMTP_HOST is set, otherwise outbox
    UNDERSTUDY_SMTP_HOST=smtp.gmail.com
    UNDERSTUDY_SMTP_PORT=587
    UNDERSTUDY_SMTP_SECURITY=starttls | ssl | none
    UNDERSTUDY_SMTP_USER=you@gmail.com
    UNDERSTUDY_SMTP_PASSWORD=<app password>
    UNDERSTUDY_MAIL_FROM=you@gmail.com           default: the SMTP user
    UNDERSTUDY_MAIL_FROM_NAME=Understudy
    UNDERSTUDY_ADMIN_CONTACT=it-admin@corp.com   shown in "blocked" mails (default: the From address)

``outbox`` (local development only): messages are appended to ``generated/mail/outbox.jsonl``
(``UNDERSTUDY_MAIL_OUTBOX``) on the server and can be read with ``python scripts/mail_outbox.py``. The API never returns
an OTP and Mission Control never shows one - the admin can't reset a user's password.
"""
from __future__ import annotations

import json
import os
import re
import smtplib
import ssl
import threading
import time
import uuid
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from typing import Any, Callable, Dict, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EMAIL_RX = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                      r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")
_LOCK = threading.Lock()
_STATS: Dict[str, Any] = {"sent": 0, "failed": 0, "last_error": None, "last_error_at": None, "last_sent_at": None}
_TRANSPORT: Optional[Callable[[EmailMessage, Dict[str, Any]], None]] = None      # tests


class MailError(Exception):
    pass


def normalize_email(value: str) -> str:
    e = (value or "").strip()
    if len(e) > 254 or not EMAIL_RX.match(e):
        raise MailError("Enter a valid email address, e.g. name@company.com.")
    local, _, domain = e.rpartition("@")
    return f"{local}@{domain.lower()}".lower()


def mask(email: str) -> str:
    local, _, domain = (email or "").partition("@")
    if not domain:
        return "***"
    shown = local[:2] if len(local) > 3 else local[:1]
    return f"{shown}{'*' * max(2, len(local) - len(shown))}@{domain}"


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def config() -> Dict[str, Any]:
    host = _env("UNDERSTUDY_SMTP_HOST")
    mode = _env("UNDERSTUDY_MAIL_MODE").lower() or ("smtp" if host else "outbox")
    if mode not in ("smtp", "outbox", "off"):
        mode = "outbox"
    security = _env("UNDERSTUDY_SMTP_SECURITY", "starttls").lower()
    port = int(_env("UNDERSTUDY_SMTP_PORT") or (465 if security == "ssl" else 587))
    user = _env("UNDERSTUDY_SMTP_USER")
    sender = _env("UNDERSTUDY_MAIL_FROM") or user or "understudy@localhost"
    return {"mode": mode, "host": host, "port": port, "security": security, "user": user,
            "has_password": bool(_env("UNDERSTUDY_SMTP_PASSWORD")), "from": sender,
            "from_name": _env("UNDERSTUDY_MAIL_FROM_NAME", "Understudy"),
            "admin_contact": _env("UNDERSTUDY_ADMIN_CONTACT") or sender,
            "outbox": _env("UNDERSTUDY_MAIL_OUTBOX") or os.path.join(ROOT, "generated", "mail", "outbox.jsonl")}


def status() -> Dict[str, Any]:
    c = config()
    problem = ""
    if c["mode"] == "smtp" and not c["host"]:
        problem = "UNDERSTUDY_SMTP_HOST is not set."
    elif c["mode"] == "smtp" and c["user"] and not c["has_password"]:
        problem = "UNDERSTUDY_SMTP_PASSWORD is not set."
    elif c["mode"] == "outbox":
        problem = ("Development mode: mails are written to the server's outbox file instead of being sent. Configure "
                   "SMTP (README 2.0.2) before users rely on signup and password resets.")
    elif c["mode"] == "off":
        problem = "Email is switched off: signup and password resets can't send codes."
    with _LOCK:
        stats = dict(_STATS)
    return {"mode": c["mode"], "host": c["host"], "port": c["port"], "security": c["security"], "user": c["user"],
            "from": c["from"], "from_name": c["from_name"], "admin_contact": c["admin_contact"],
            "password_set": c["has_password"], "delivering": c["mode"] == "smtp" and not problem,
            "problem": problem, "stats": stats}


def set_transport(fn: Optional[Callable[[EmailMessage, Dict[str, Any]], None]]) -> None:
    global _TRANSPORT
    _TRANSPORT = fn


def _build(to: str, subject: str, text: str, html: Optional[str], c: Dict[str, Any]) -> EmailMessage:
    m = EmailMessage()
    m["From"] = formataddr((c["from_name"], c["from"]))
    m["To"] = to
    m["Subject"] = subject
    m["Message-ID"] = make_msgid(domain=(c["from"].rpartition("@")[2] or "understudy.local"))
    m["X-Mailer"] = "Understudy"
    m.set_content(text)
    if html:
        m.add_alternative(html, subtype="html")
    return m


def _smtp_send(m: EmailMessage, c: Dict[str, Any]) -> None:
    password = _env("UNDERSTUDY_SMTP_PASSWORD")
    ctx = ssl.create_default_context()
    if c["security"] == "ssl":
        server: smtplib.SMTP = smtplib.SMTP_SSL(c["host"], c["port"], timeout=20, context=ctx)
    else:
        server = smtplib.SMTP(c["host"], c["port"], timeout=20)
    try:
        server.ehlo()
        if c["security"] == "starttls":
            server.starttls(context=ctx)
            server.ehlo()
        if c["user"]:
            server.login(c["user"], password)
        server.send_message(m)
    finally:
        try:
            server.quit()
        except Exception:
            pass


def _friendly(e: Exception) -> str:
    s = str(e)
    if isinstance(e, smtplib.SMTPAuthenticationError):
        return ("The mail server rejected the login. For Gmail use an App Password (Google Account > Security > "
                "2-Step Verification > App passwords), not your normal password.")
    if isinstance(e, (TimeoutError, OSError)) and not isinstance(e, smtplib.SMTPException):
        return f"Can't reach the mail server ({type(e).__name__}: {s[:160]}). Check host, port and outbound network."
    return f"{type(e).__name__}: {s[:200]}"


def send(to: str, subject: str, text: str, html: Optional[str] = None, kind: str = "notice") -> Dict[str, Any]:
    """Sends one mail. Raises MailError when it can't be delivered (the caller decides what the user sees)."""
    c = config()
    to = normalize_email(to)
    m = _build(to, subject, text, html, c)
    try:
        if _TRANSPORT is not None:
            _TRANSPORT(m, {"to": to, "subject": subject, "text": text, "kind": kind})
        elif c["mode"] == "off":
            raise MailError("Email is switched off on this server (UNDERSTUDY_MAIL_MODE=off).")
        elif c["mode"] == "outbox":
            path = c["outbox"]
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with _LOCK, open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"id": uuid.uuid4().hex[:10], "at": time.time(), "to": to, "subject": subject,
                                     "kind": kind, "text": text}) + "\n")
        else:
            if not c["host"]:
                raise MailError("Email isn't configured (UNDERSTUDY_SMTP_HOST is missing).")
            _smtp_send(m, c)
    except MailError as e:
        _record(False, str(e))
        raise
    except Exception as e:
        msg = _friendly(e)
        _record(False, msg)
        raise MailError(msg)
    _record(True, None)
    return {"ok": True, "mode": "test" if _TRANSPORT else c["mode"], "to": mask(to)}


def _record(ok: bool, err: Optional[str]) -> None:
    with _LOCK:
        if ok:
            _STATS["sent"] += 1
            _STATS["last_sent_at"] = time.time()
        else:
            _STATS["failed"] += 1
            _STATS["last_error"] = err
            _STATS["last_error_at"] = time.time()


def outbox(limit: int = 20) -> list:
    """Local development helper (scripts/mail_outbox.py) - never exposed over HTTP."""
    path = config()["outbox"]
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh if line.strip()]
    return rows[-limit:]


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
def _html(title: str, body: str) -> str:
    return ("<div style=\"font-family:Arial,Helvetica,sans-serif;max-width:520px;margin:auto;padding:24px;"
            "border:1px solid #e2e8f0;border-radius:12px;color:#0f172a\">"
            "<div style=\"font-weight:700;font-size:18px;margin-bottom:12px\">Understudy</div>"
            f"<div style=\"font-weight:600;font-size:16px;margin-bottom:8px\">{title}</div>{body}"
            "<p style=\"color:#64748b;font-size:12px;margin-top:24px\">This message was sent by Understudy. "
            "If you didn't expect it, you can ignore it.</p></div>")


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


PURPOSE_TEXT = {
    "signup": ("Confirm your email", "Use this code to finish creating your Understudy account"),
    "reset": ("Reset your password", "Use this code to reset your Understudy password"),
    "forgot": ("Reset your password", "Use this code to reset your Understudy password"),
    "login": ("Your sign-in code", "Your password was accepted. Use this code to finish signing in to Understudy"),
    "2fa_on": ("Turn on two-step sign-in", "Use this code to turn on two-step sign-in for your Understudy account"),
}


def otp_mail(to: str, code: str, purpose: str, username: str, minutes: int) -> Dict[str, Any]:
    title, lead = PURPOSE_TEXT.get(purpose, PURPOSE_TEXT["reset"])
    text = (f"Hello {username},\n\n{lead}:\n\n    {code}\n\nThe code is valid for {minutes} minutes and can be used once. "
            "Never share it - Understudy staff and your admin will never ask for it.\n\nIf you didn't request this, "
            "ignore this mail; nothing changes.\n")
    html = _html(title, f"<p>Hello {_esc(username)},</p><p>{lead}:</p>"
                 f"<p style=\"font-size:30px;font-weight:700;letter-spacing:8px;margin:16px 0\">{code}</p>"
                 f"<p>The code is valid for <b>{minutes} minutes</b> and can be used once. Never share it - Understudy "
                 "staff and your admin will never ask for it.</p>")
    return send(to, f"Understudy: {code} is your code ({title.lower()})", text, html, kind=f"otp-{purpose}")


def two_factor_mail(to: str, username: str, on: bool, by: str = "") -> Dict[str, Any]:
    """9.6: two-step sign-in was turned on or off for the account."""
    if on:
        title, line = "Two-step sign-in is on", ("From now on, after your password we email you a 6-digit code to finish "
                                                  "signing in.")
    else:
        who = f"Your admin ({by}) turned it off" if by else "You turned it off"
        title, line = "Two-step sign-in is off", f"{who}. Signing in now needs only your password."
    text = (f"Hello {username},\n\n{title}. {line}\n\nIf this wasn't you, reset your password and contact your admin.\n")
    html = _html(title, f"<p>Hello {_esc(username)},</p><p><b>{_esc(title)}.</b> {_esc(line)}</p>"
                 "<p>If this wasn't you, reset your password and contact your admin.</p>")
    return send(to, f"Understudy: {title.lower()}", text, html, kind="2fa-on" if on else "2fa-off")


def welcome_mail(to: str, username: str, admin: str, needs_password: bool) -> Dict[str, Any]:
    step = ("Your admin didn't set a password for you. Open Understudy, choose \"Forgot password\", enter this email "
            "address and set your own password with the code we send you."
            if needs_password else
            "Log in with this username and the password your admin gave you. You can change it at any time in "
            "Profile > Reset password (a code is sent to this address).")
    text = f"Hello {username},\n\n{admin} created an Understudy account for you.\n\nUsername: {username}\n\n{step}\n"
    html = _html("Your Understudy account", f"<p>Hello {_esc(username)},</p><p>{_esc(admin)} created an Understudy "
                 f"account for you.</p><p>Username: <b>{_esc(username)}</b></p><p>{_esc(step)}</p>")
    return send(to, "Your Understudy account", text, html, kind="welcome")


def blocked_mail(to: str, username: str, reason: str) -> Dict[str, Any]:
    contact = config()["admin_contact"]
    text = (f"Hello {username},\n\nYour Understudy account has been blocked by your administrator.\n\nReason: {reason}\n\n"
            f"To have it unblocked, contact your administrator at {contact}.\n")
    html = _html("Your account has been blocked", f"<p>Hello {_esc(username)},</p><p>Your Understudy account has been "
                 f"blocked by your administrator.</p><p><b>Reason:</b> {_esc(reason)}</p><p>To have it unblocked, contact "
                 f"your administrator at <a href=\"mailto:{_esc(contact)}\">{_esc(contact)}</a>.</p>")
    return send(to, "Your Understudy account has been blocked", text, html, kind="blocked")


def deleted_mail(to: str, username: str, reason: str) -> Dict[str, Any]:
    contact = config()["admin_contact"]
    text = (f"Hello {username},\n\nYour Understudy account has been deleted by your administrator.\n\nReason: {reason}\n\n"
            f"Your projects and sessions are kept for audit. If you think this is a mistake, contact your administrator "
            f"at {contact}.\n")
    html = _html("Your account has been deleted", f"<p>Hello {_esc(username)},</p><p>Your Understudy account has been "
                 f"deleted by your administrator.</p><p><b>Reason:</b> {_esc(reason)}</p><p>Your projects and sessions "
                 f"are kept for audit. If you think this is a mistake, contact your administrator at "
                 f"<a href=\"mailto:{_esc(contact)}\">{_esc(contact)}</a>.</p>")
    return send(to, "Your Understudy account has been deleted", text, html, kind="deleted")


def unblocked_mail(to: str, username: str) -> Dict[str, Any]:
    text = f"Hello {username},\n\nYour Understudy account has been unblocked. You can log in again.\n"
    return send(to, "Your Understudy account is active again", text,
                _html("Your account is active again", f"<p>Hello {_esc(username)},</p><p>Your Understudy account has "
                      "been unblocked. You can log in again.</p>"), kind="unblocked")


def password_changed_mail(to: str, username: str) -> Dict[str, Any]:
    contact = config()["admin_contact"]
    text = (f"Hello {username},\n\nThe password of your Understudy account was just changed, and every open session "
            f"was signed out.\n\nIf this wasn't you, contact your administrator at {contact} immediately.\n")
    return send(to, "Your Understudy password was changed", text,
                _html("Your password was changed", f"<p>Hello {_esc(username)},</p><p>The password of your Understudy "
                      "account was just changed, and every open session was signed out.</p><p>If this wasn't you, "
                      f"contact your administrator at {_esc(contact)} immediately.</p>"), kind="password-changed")


def reset_requested_mail(to: str, username: str, admin: str) -> Dict[str, Any]:
    text = (f"Hello {username},\n\n{admin} asked you to set a new Understudy password. Open Understudy, choose "
            "\"Forgot password\" (or Profile > Reset password when you are logged in), and follow the steps - a code "
            "is sent to this address. Your admin can't see or set your password.\n")
    return send(to, "Please reset your Understudy password", text,
                _html("Please reset your password", f"<p>Hello {_esc(username)},</p><p>{_esc(admin)} asked you to set a "
                      "new Understudy password. Open Understudy, choose <b>Forgot password</b> (or <b>Profile &gt; Reset "
                      "password</b> when you are logged in) and follow the steps - a code is sent to this address. Your "
                      "admin can't see or set your password.</p>"), kind="reset-requested")


def test_mail(to: str, admin: str) -> Dict[str, Any]:
    return send(to, "Understudy test email", f"This is a test email sent by {admin} from Mission Control. Email works.\n",
                _html("Test email", f"<p>This is a test email sent by {_esc(admin)} from Mission Control. "
                      "Email works.</p>"), kind="test")
