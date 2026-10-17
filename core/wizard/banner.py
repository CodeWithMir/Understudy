"""Startup banner printed by the backend (server.py) so the terminal shows the real, current state."""
from __future__ import annotations

from typing import Any, Dict


def _os_env(name: str) -> str:
    import os
    return os.environ.get(name, "")


def startup_banner(key_info: Dict[str, Any]) -> str:
    from core.wizard import WIZARD_VERSION, admin_policy, dependency_resolver
    has_key = key_info.get("has_key", False)
    model = key_info.get("selected_model") or "gemini-flash-latest"
    from core.wizard import gemini, store
    g = gemini.describe()
    if g.get("mode") == "vertex":
        ai = f"ONLINE  (Vertex AI, project {g.get('project')} / {g.get('location')}, model {model}, google-genai SDK)"
    elif has_key:
        ai = f"ONLINE  (key {key_info.get('masked_key')}, model {model}, {g.get('sdk') or 'SDK missing'})"
    else:
        ai = "OFFLINE - local heuristics (add a Gemini key in Mission Control > AI, security & account)"
    try:
        st = store.describe()
        state = "Cloud Firestore (shared by every instance)" if st["kind"] == "firestore" else f"local files ({st['location']})"
        if st.get("degraded"):
            state = (f"LOCAL FILES - Firestore is switched on but unavailable ({st['degraded'][:90]}). "
                     f"Run: python scripts/gcp_check.py")
    except Exception as e:  # pragma: no cover
        state = f"unavailable ({type(e).__name__})"
    try:
        wl = admin_policy.whitelist()
        tools = []
        for tid, e in sorted(wl.items()):
            spec = e["spec"]
            missing = dependency_resolver.check_toolchain(spec.get("ecosystem", "none"))
            ver = (e.get("versions") or {}).get("default")
            tools.append(f"    - {spec.get('name', tid):<34} {('v' + ver) if ver else 'any version':<14} "
                         f"{'MISSING: ' + ', '.join(missing) if missing else 'ready'}")
        tool_lines = "\n".join(tools) or "    (none - approve tools in Mission Control)"
        retries = admin_policy.max_self_heal_retries()
    except Exception as e:  # pragma: no cover - banner must never stop the server
        tool_lines, retries = f"    (could not read the admin policy: {e})", 0
    hosting = (f"Google Cloud Run (service {_os_env('K_SERVICE')}, revision {_os_env('K_REVISION')})"
               if _os_env("K_SERVICE") else "self-hosted (deploy to Google Cloud Run: ./deploy_cloudrun.sh <project-id>)")
    mode = dependency_resolver.install_mode()
    from core.wizard import admin_auth
    import os as _os
    from core.wizard import admin_password as _ap
    if _ap.GENERATED_THIS_START:
        pw = f"{_ap.GENERATED_THIS_START}   <- FIRST START: generated, saved in .env, shown ONCE"
    elif _os.environ.get("UNDERSTUDY_ADMIN_PASSWORD") and _os.environ.get("K_SERVICE"):
        pw = "(Secret Manager - gcloud secrets versions access latest --secret=understudy-admin-password)"
    elif _os.environ.get("UNDERSTUDY_ADMIN_PASSWORD"):
        pw = "(saved - show it: python scripts/admin_password.py show)"
    else:
        pw = f"{admin_auth.DEFAULT_PASSWORD}   <- PUBLISHED DEFAULT: set UNDERSTUDY_ADMIN_PASSWORD"
    login = f"{admin_auth.default_username()} / {pw}"
    runtime = _runtime_line()
    try:
        heal = admin_policy.self_heal_approval()
    except Exception:
        heal = "ask"
    heal_line = (f"the user approves each fix, up to {retries} retries per run" if heal == "ask"
                 else f"automatic, up to {retries} retries per run")
    from core.wizard import users as _users
    try:
        _all = _users.list_users()
        n_users, n_2fa = len(_all), sum(1 for u in _all if u.get("two_factor"))
    except Exception:
        n_users, n_2fa = 0, 0
    studio = (f"login required - {n_users} account(s), {n_2fa} with two-step sign-in; Mission Control > Users"
              + ("; self-signup on" if _users.signup_enabled() else "; self-signup off")
              if _users.login_required() else "login OFF (UNDERSTUDY_USER_LOGIN=off, single-user mode)")
    try:
        from core.wizard import mailer as _mailer
        _ms = _mailer.status()
        mail_line = (f"SMTP {_ms['host']}:{_ms['port']} as {_ms['from']}" if _ms["delivering"] else
                     "OUTBOX - not sent, written to generated/mail/outbox.jsonl (configure SMTP)" if _ms["mode"] == "outbox"
                     else f"{_ms['mode'].upper()} - {_ms['problem'][:70]}")
    except Exception as e:                                         # pragma: no cover
        mail_line = f"unavailable ({type(e).__name__})"
    return f"""
================================================================================
  UNDERSTUDY v{WIZARD_VERSION}  //  Watch -> Govern -> Build -> Execute -> Export
================================================================================
  Gemini AI:            {ai}
  Hosting:              {hosting}
  State:                {state}
  Dependency installs:  {mode}   (UNDERSTUDY_DEPENDENCY_INSTALL=auto|approve|dry-run|off)
  Self-healing:         {heal_line} (set in Mission Control)
  Runtimes:             {runtime}
  Admin login:          {login}
                        (Mission Control; a password changed there lasts until the next restart)
  Studio users:         {studio}
  Email (OTP, notices): {mail_line}   (README 2.0.2)

  Tools users may run (admin whitelist):
{tool_lines}

  OPEN IN YOUR BROWSER
    Understudy Studio        ->  http://localhost:3000/wizard   (log in with an account from Mission Control > Users)
    Mission Control (admin)  ->  http://localhost:3000
    Demo ERP portals         ->  http://localhost:8000/portal1  /portal2  /portal3
    Backend API docs         ->  http://localhost:8000/docs
================================================================================
"""


def _runtime_line() -> str:
    """Python/Node versions with an end-of-life note (built-in table; no network at start-up)."""
    import platform
    import sys
    from datetime import date, datetime
    from core.wizard import env_health
    parts = []
    for product, label, ver, cycle in [("python", "Python", platform.python_version(),
                                        f"{sys.version_info[0]}.{sys.version_info[1]}")] + (
            [("nodejs", "Node.js", nv, nv.split(".")[0])] if (nv := env_health._node_version()) else []):
        eol = env_health.EOL_FALLBACK[product].get(cycle)
        note = ""
        if eol:
            days = (datetime.strptime(eol, "%Y-%m-%d").date() - date.today()).days
            if days < 0:
                note = f" (END OF LIFE since {eol})"
            elif days <= env_health.WARN_DAYS:
                note = f" (end of life {eol})"
        parts.append(f"{label} {ver}{note}")
    line = ", ".join(parts)
    if "end of life" in line.lower():
        line += "  -> upgrade advice in Mission Control > Code health"
    return line
