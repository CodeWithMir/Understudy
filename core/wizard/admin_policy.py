"""Admin policy managed from Mission Control.

Persisted to a JSON file (``UNDERSTUDY_ADMIN_POLICY_PATH``, default ``generated/admin_policy.json``) or, with
``UNDERSTUDY_STORE=firestore``, to Cloud Firestore (document ``understudy_admin_policy/main``) so every Cloud Run
instance shares - and re-reads every few seconds - the same policy:

1. **Target URL exceptions.** Hosts that the SSRF guard would otherwise block (localhost,
   private/intranet addresses, names that don't resolve from the server, and so on) become
   testable once an admin adds them. Matching is by host name and ignores the port. Portal 1 and
   Portal 2 of the mock ERP are seeded by default. Cloud-metadata / link-local addresses can never be
   excepted: a user-driven browser pointed at them could leak the server's cloud credentials.

2. **Tool versions.** For every whitelisted tool the admin can choose which versions of its primary package users
   may run (picked from the live package registry, see ``core/wizard/tool_versions.py``) and which one is the
   default. With no versions chosen the tool uses the vetted spec's own constraint.

3. **Tool whitelist + access requests.** Users may only select frameworks the admin has whitelisted.
   Selecting anything else (a catalog tool that isn't whitelisted, or any tool typed by name) creates
   an access request. The admin sees the tool's packages and run command and approves or rejects it.
   Approval whitelists the exact validated spec, and also covers its dependencies (including packages
   outside the trusted list), so users are never asked to vet packages themselves.

4. **Blocked URL list (v6.2).** The mirror image of (1): hosts the admin never wants Understudy to touch - in the
   analysis browser, in the pre/post-run snapshots, in the generated automation's network traffic and in redirects.
   Matching is by host name (ignoring the port); ``*.example.com`` also blocks every sub-domain. **A host can never be
   on both lists**: adding it to one list while it is on the other (or while a wildcard of the other list covers it)
   is refused with an explanation. If an older policy file somehow holds an overlap, *blocked wins* at enforcement time.

5. **Network mode.** ``restricted`` (default): the generated automation may only reach the run's target host plus the
   admin's allowed list; ``public``: any public host except the blocked list. Private ranges and cloud-metadata are
   unreachable in both modes (see ``core/wizard/egress.py``).

Admin endpoints can be protected with ``UNDERSTUDY_ADMIN_TOKEN`` (sent as ``X-Admin-Token``).
"""
from __future__ import annotations

import copy
import ipaddress
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from core.wizard.frameworks import CATALOG, FrameworkSpec, FrameworkValidationError, Package, validate_spec

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_LOCK = threading.RLock()
_CACHE: Dict[str, Any] = {"path": None, "data": None, "t": 0.0}
SHARED_REFRESH_S = 5.0          # Firestore mode: re-read the shared policy so every instance sees admin changes


def _shared():
    """The durable store when it is shared between instances (Firestore); None for the local JSON file."""
    from core.wizard import store
    st = store.get_store()
    return st if getattr(st, "kind", "local") != "local" else None

DEFAULT_URL_EXCEPTIONS = [
    ("http://localhost:8000/portal1", "Portal 1 - Legacy Accounts Payable (demo)"),
    ("http://localhost:8000/portal2", "Portal 2 - Modernized CloudERP 360 (demo)"),
    ("http://localhost:8000/portal3", "Portal 3 - Shifting Input Forms, RPA-Challenge style (demo)"),
]
# 8.2 zero-trust tool registry: a fresh installation has NO active tool - nothing is pre-built, not even Playwright.
# Every tool (Playwright, Selenium, Cypress, ...) is added by the admin with the AI install assistant (or requested by a
# user): the AI suggests the stack, the admin selects it, the AI installs it in the sandbox and writes a smoke test that
# the admin reviews, and the tool only becomes ACTIVE after that smoke test passes. An existing policy keeps the tools
# its admin already approved.
# UNDERSTUDY_DEFAULT_TOOLS: none (default) | playwright (8.0/8.1 behaviour: Playwright Python pre-seeded) | catalog
# (every tool with an offline code template - used by older test suites and demos).
BASELINE_TOOL = "playwright-python"
# 9.9: tools built into Understudy - always approved for every user, no admin approval, setup or smoke test, and they
# cannot be removed. Understudy Gemini Agent is Understudy's own runtime (shipped as understudy_agent.py).
BUILTIN_TOOLS = ("gemini-agent",)


def is_builtin(tool_id: str) -> bool:
    return tool_id in BUILTIN_TOOLS


def builtin_agent_hidden(user: Optional[str] = None) -> bool:
    """9.9: the built-in agent is only the fallback - once the admin has set up an agentic tool on Understudy's agent
    that this user may use, users are offered that one and the built-in agent is no longer listed."""
    from core.wizard.agent_stacks import is_understudy_agent
    return any(not is_builtin(tid) and is_understudy_agent(e.get("spec") or {}) and user_may_use(tid, user)
               for tid, e in _load()["tool_whitelist"].items())


def _builtin_entry(fid: str) -> Dict[str, Any]:
    e = {"spec": CATALOG[fid].to_dict(), "approved_by": "built-in", "approved_at": _now(), "source": "builtin",
         "builtin": True,
         "setup": {"verified": True, "builtin": True,
                   "summary": "Built into Understudy - nothing for the admin to approve or set up. Each run uses the "
                              "organisation's Gemini. Not listed once the admin sets up an agentic tool of their own.",
                   "smoke": {"status": "BUILTIN", "detail": "part of Understudy, tested with every release"}}}
    try:
        from importlib import metadata
        v = metadata.version("playwright")           # the browsers the server already has
        e["versions"] = {"allowed": [v], "default": v, "set_by": "built-in", "set_at": _now()}
    except Exception:
        pass
    return e


def _ensure_builtin(data: Dict[str, Any]) -> None:
    wl = data.setdefault("tool_whitelist", {})
    for fid in BUILTIN_TOOLS:
        if fid in CATALOG and not (wl.get(fid) or {}).get("builtin"):
            wl[fid] = _builtin_entry(fid)


def default_whitelist() -> List[str]:
    mode = (os.environ.get("UNDERSTUDY_DEFAULT_TOOLS") or "none").strip().lower()
    if mode == "catalog":
        return [fid for fid, f in CATALOG.items() if f.template]
    if mode == "playwright":
        return [BASELINE_TOOL]
    return []


def _baseline_versions(fid: str) -> Optional[Dict[str, Any]]:
    """The baseline Playwright is pinned to the version the server already has (its browsers are provisioned)."""
    if fid != BASELINE_TOOL:
        return None
    try:
        from importlib import metadata
        v = metadata.version("playwright")
    except Exception:
        return None
    return {"allowed": [v], "default": v, "set_by": "default", "set_at": _now()}


def _seed_entry(fid: str) -> Dict[str, Any]:
    e = {"spec": CATALOG[fid].to_dict(), "approved_by": "default", "approved_at": _now(), "source": "catalog"}
    vers = _baseline_versions(fid)
    if vers and (os.environ.get("UNDERSTUDY_DEFAULT_TOOLS") or "none").strip().lower() == "playwright":
        e["versions"] = vers
        e["setup"] = {"verified": True, "baseline": True, "summary": "Pre-seeded baseline tool (8.0): Playwright with "
                      "the version and browsers already installed on the server.",
                      "smoke": {"status": "SEEDED", "detail": "baseline - re-run the smoke test any time"}}
    return e
# Self-healing retries run automatically (no user prompt), so they must be bounded: 1..50, default 5.
MIN_RETRIES, MAX_RETRIES, DEFAULT_RETRIES = 1, 50, 5


def _env_retries() -> int:
    try:
        v = int(os.environ.get("UNDERSTUDY_MAX_SELF_HEAL_RETRIES") or DEFAULT_RETRIES)
    except ValueError:
        v = DEFAULT_RETRIES
    return v if MIN_RETRIES <= v <= MAX_RETRIES else DEFAULT_RETRIES


NETWORK_MODES = ("restricted", "public")
DEFAULT_SETTINGS = {"max_self_heal_retries": _env_retries(),
                    "network_mode": os.environ.get("UNDERSTUDY_NETWORK_MODE", "restricted")
                    if os.environ.get("UNDERSTUDY_NETWORK_MODE", "restricted") in NETWORK_MODES else "restricted",
                    # Code health: apply AI fixes for start-up warnings automatically once they pass verification.
                    "code_health_auto_apply": (os.environ.get("UNDERSTUDY_CODE_HEALTH_AUTO_APPLY") or "false").lower()
                    in ("1", "true", "yes", "on")}
NEVER_EXCEPTABLE_HOSTS = {"metadata.google.internal", "metadata", "169.254.169.254", "fd00:ec2::254", "100.100.100.200"}


class PolicyError(ValueError):
    pass


def _now() -> str:
    return datetime.utcnow().isoformat()


def policy_path() -> str:
    return os.environ.get("UNDERSTUDY_ADMIN_POLICY_PATH") or os.path.join(_BASE, "generated", "admin_policy.json")


def _default_policy() -> Dict[str, Any]:
    return {
        "version": 1,
        "url_exceptions": [
            {"id": "url-" + uuid.uuid4().hex[:8], "url": u, "host": urlsplit(u).hostname, "note": n,
             "added_by": "default", "added_at": _now()} for u, n in DEFAULT_URL_EXCEPTIONS],
        "tool_whitelist": {fid: _seed_entry(fid) for fid in default_whitelist()},
        "url_blocklist": [],
        "tool_blocklist": [],
        "tool_requests": [],
        "user_overrides": {},
        "settings": dict(DEFAULT_SETTINGS),
    }


def _load() -> Dict[str, Any]:
    shared = _shared()
    path = policy_path() if shared is None else f"store:{shared.kind}"
    with _LOCK:
        if _CACHE["path"] == path and _CACHE["data"] is not None and (
                shared is None or time.time() - _CACHE["t"] < SHARED_REFRESH_S):
            _CACHE["data"].setdefault("url_blocklist", [])
            _CACHE["data"].setdefault("tool_blocklist", [])
            _ensure_builtin(_CACHE["data"])
            return _CACHE["data"]
        data = None
        if shared is not None:
            try:
                data = shared.get("admin_policy", "main")
            except Exception:
                data = _CACHE["data"] if _CACHE["path"] == path else None     # store unreachable: keep last copy
        elif os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
            except Exception:
                data = None
        if not data:
            data = _default_policy()
            _CACHE.update(path=path, data=data, t=time.time())
            _save()
        data.setdefault("url_blocklist", [])            # policy files from before v6.2 have no blocked list
        data.setdefault("tool_blocklist", [])           # ... and before 7.2 no blocked tools
        data.setdefault("user_overrides", {})           # ... and before 9.3 no per-user tool changes
        for k, v in DEFAULT_SETTINGS.items():          # policy files from older versions have no settings block
            data.setdefault("settings", {}).setdefault(k, v)
        try:                                            # v5.1/5.2 allowed 0 = unlimited; retries are automatic now
            if not MIN_RETRIES <= int(data["settings"]["max_self_heal_retries"]) <= MAX_RETRIES:
                data["settings"]["max_self_heal_retries"] = DEFAULT_RETRIES
        except (TypeError, ValueError):
            data["settings"]["max_self_heal_retries"] = DEFAULT_RETRIES
        _ensure_builtin(data)
        _CACHE.update(path=path, data=data, t=time.time())
        return data


def _save() -> None:
    shared = _shared()
    if shared is not None:
        with _LOCK:
            try:
                shared.put("admin_policy", "main", _CACHE["data"])
                _CACHE["t"] = time.time()
                return
            except Exception as e:                      # Firestore unreachable: keep the change locally too
                print(f"[admin policy] Firestore write failed ({type(e).__name__}); saved to the local file instead")
    path = policy_path()
    with _LOCK:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(_CACHE["data"], fh, indent=2)
        os.replace(tmp, path)


def reset_to_defaults() -> Dict[str, Any]:
    with _LOCK:
        shared = _shared()
        _CACHE.update(path=policy_path() if shared is None else f"store:{shared.kind}", data=_default_policy(),
                      t=time.time())
        _save()
        return snapshot()


def snapshot() -> Dict[str, Any]:
    with _LOCK:
        return copy.deepcopy(_load())


# ---------------------------------------------------------------------------
# URL exceptions
# ---------------------------------------------------------------------------
def excepted_hosts() -> set:
    return {e["host"].lower() for e in _load()["url_exceptions"] if e.get("host")}


def _norm_host(host: str) -> str:
    return (host or "").strip().lower().rstrip(".")


def pattern_matches(pattern: str, host: str) -> bool:
    """``example.com`` matches only itself; ``*.example.com`` matches every sub-domain (and not example.com itself)."""
    pattern, host = _norm_host(pattern), _norm_host(host)
    if not pattern or not host:
        return False
    if pattern.startswith("*."):
        return host.endswith(pattern[1:]) and len(host) > len(pattern) - 1
    return pattern == host


def _overlap(a: str, b: str) -> bool:
    """True when one pattern can match something the other matches (host vs host, host vs wildcard, wildcard vs wildcard)."""
    a, b = _norm_host(a), _norm_host(b)
    if a == b:
        return True
    ba, bb = a[2:] if a.startswith("*.") else None, b[2:] if b.startswith("*.") else None
    if ba and not bb:
        return pattern_matches(a, b)
    if bb and not ba:
        return pattern_matches(b, a)
    if ba and bb:
        return ba == bb or ba.endswith("." + bb) or bb.endswith("." + ba)
    return False


def is_host_excepted(host: str) -> bool:
    """Allowed-list membership. A host that is also blocked (only possible in a hand-edited policy) is NOT excepted."""
    h = _norm_host(host)
    return bool(h) and h in excepted_hosts() and not is_host_blocked(h)


def blocked_entries() -> List[Dict[str, Any]]:
    return list(_load().get("url_blocklist", []))


def blocked_patterns() -> List[str]:
    return [e["host"] for e in blocked_entries() if e.get("host")]


def is_host_blocked(host: str) -> bool:
    h = _norm_host(host)
    return bool(h) and any(pattern_matches(p, h) for p in blocked_patterns())


def blocking_entry(host: str) -> Optional[Dict[str, Any]]:
    h = _norm_host(host)
    for e in blocked_entries():
        if e.get("host") and pattern_matches(e["host"], h):
            return e
    return None


def _is_metadata(host: str) -> bool:
    if host in NEVER_EXCEPTABLE_HOSTS:
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_link_local
    except ValueError:
        return False


def _parse_target(url: str, allow_wildcard: bool = False) -> tuple:
    raw = (url or "").strip()
    if not raw:
        raise PolicyError("Enter a URL")
    if "://" not in raw:
        raw = "http://" + raw
    try:
        parts = urlsplit(raw)
        _ = parts.port
    except ValueError as e:
        raise PolicyError(f"Malformed URL ({e})")
    if parts.scheme.lower() not in ("http", "https"):
        raise PolicyError("Only http:// and https:// URLs can be added")
    if parts.username or parts.password:
        raise PolicyError("Remove the username/password from the URL")
    host = _norm_host(parts.hostname or "")
    if not host:
        # urlsplit rejects a leading "*" in some Python versions; recover "*.example.com" explicitly
        m = re.match(r"^[a-z]+://(\*\.[^/:?#]+)", raw.lower())
        host = m.group(1) if (m and allow_wildcard) else ""
    if not host:
        raise PolicyError("URL has no host name")
    if host.startswith("*."):
        if not allow_wildcard:
            raise PolicyError("Wildcards (*.example.com) are only supported on the blocked list")
        if "*" in host[2:] or "." not in host[2:]:
            raise PolicyError("Use a wildcard like *.example.com (one leading '*.' and a real domain)")
    elif "*" in host:
        raise PolicyError("Wildcards must look like *.example.com")
    return raw, host


def add_url_exception(url: str, note: str = "", added_by: str = "admin") -> Dict[str, Any]:
    raw, host = _parse_target(url)
    if _is_metadata(host):
        raise PolicyError(f"{host} is a cloud-metadata / link-local address and can never be excepted")
    with _LOCK:
        data = _load()
        for b in data.get("url_blocklist", []):
            if _overlap(b["host"], host):
                raise PolicyError(f"'{host}' is on the blocked list ({b['host']}). A link cannot be both allowed and "
                                  f"blocked - remove it from the blocked list first.")
        for e in data["url_exceptions"]:
            if e["url"].rstrip("/") == raw.rstrip("/"):
                return e
        entry = {"id": "url-" + uuid.uuid4().hex[:8], "url": raw, "host": host, "note": str(note or "")[:200],
                 "added_by": added_by, "added_at": _now()}
        data["url_exceptions"].append(entry)
        _save()
        return entry


def remove_url_exception(entry_id: str) -> None:
    with _LOCK:
        data = _load()
        before = len(data["url_exceptions"])
        data["url_exceptions"] = [e for e in data["url_exceptions"] if e["id"] != entry_id]
        if len(data["url_exceptions"]) == before:
            raise PolicyError("No such URL exception")
        _save()


def add_blocked_url(url: str, note: str = "", added_by: str = "admin") -> Dict[str, Any]:
    raw, host = _parse_target(url, allow_wildcard=True)
    with _LOCK:
        data = _load()
        for e in data["url_exceptions"]:
            if _overlap(e["host"], host):
                raise PolicyError(f"'{host}' is on the allowed list ({e['host']}). A link cannot be both allowed and "
                                  f"blocked - remove it from the allowed list first.")
        for b in data["url_blocklist"]:
            if b["host"] == host:
                return b
        entry = {"id": "blk-" + uuid.uuid4().hex[:8], "url": raw if not host.startswith("*.") else host, "host": host,
                 "note": str(note or "")[:200], "added_by": added_by, "added_at": _now()}
        data["url_blocklist"].append(entry)
        _save()
        return entry


def remove_blocked_url(entry_id: str) -> None:
    with _LOCK:
        data = _load()
        before = len(data["url_blocklist"])
        data["url_blocklist"] = [e for e in data["url_blocklist"] if e["id"] != entry_id]
        if len(data["url_blocklist"]) == before:
            raise PolicyError("No such blocked URL")
        _save()


def network_mode() -> str:
    m = _load()["settings"].get("network_mode", "restricted")
    return m if m in NETWORK_MODES else "restricted"


def set_network_mode(mode: str) -> str:
    if mode not in NETWORK_MODES:
        raise PolicyError(f"Network mode must be one of: {', '.join(NETWORK_MODES)}")
    with _LOCK:
        _load()["settings"]["network_mode"] = mode
        _save()
    return mode


LOOPBACK_BYPASS = ["localhost", "127.0.0.1", "::1"]
_BYPASS_RX = re.compile(r"^(\*\.)?[A-Za-z0-9]([A-Za-z0-9.-]{0,250})$|^[0-9a-fA-F:.]{2,45}$")


def proxy_bypass_hosts() -> List[str]:
    """8.3: hosts a run reaches WITHOUT Understudy's per-run egress proxy. Loopback is always included: the tool's own
    local driver (ChromeDriver, geckodriver, the WebDriver server) listens there - routing it through the proxy broke
    Selenium with 'RemoteDisconnected: Remote end closed connection without response' (Chromium never proxies loopback
    anyway). The admin may add more hosts in Mission Control > Network for automation runs."""
    extra = [h for h in (_load()["settings"].get("proxy_bypass") or []) if isinstance(h, str)]
    return list(dict.fromkeys(LOOPBACK_BYPASS + extra))


def set_proxy_bypass_hosts(hosts: List[str], by: str = "admin") -> List[str]:
    clean: List[str] = []
    for h in hosts or []:
        h = str(h).strip().lower()
        if not h or h in LOOPBACK_BYPASS:
            continue
        if not _BYPASS_RX.match(h) or h in _METADATA_BYPASS_DENY:
            raise PolicyError(f"'{h}' can't bypass the run proxy (a host name, *.domain or an IP address; never cloud "
                              "metadata addresses).")
        if h not in clean:
            clean.append(h)
    with _LOCK:
        _load()["settings"]["proxy_bypass"] = clean[:30]
        _save()
    return proxy_bypass_hosts()


_METADATA_BYPASS_DENY = {"169.254.169.254", "metadata.google.internal", "metadata", "fd00:ec2::254", "100.100.100.200"}


def set_tool_runtime(tool_id: str, env_vars: Optional[Dict[str, str]] = None, path_prepend: Optional[List[str]] = None,
                     by: str = "admin") -> Dict[str, Any]:
    """8.3: the admin edits an approved tool's run settings directly (no AI needed): environment variables and PATH
    folders every run of the tool gets. Validated like a setup manifest; PATH folders must exist on the server."""
    from core.wizard.sandbox.base import SandboxError, clean_manifest_env
    tid = resolve_tool_id(tool_id)
    with _LOCK:
        e = _load()["tool_whitelist"].get(tid)
        if e is None:
            raise PolicyError("This tool isn't approved.")
        rt = e.setdefault("setup", {}).setdefault("runtime", {})
        if env_vars is not None:
            try:
                rt["env_vars"] = clean_manifest_env({str(k): str(v) for k, v in env_vars.items() if str(k).strip()})
            except SandboxError as ex:
                raise PolicyError(str(ex))
        if path_prepend is not None:
            dirs = [str(p).strip() for p in path_prepend if str(p).strip()][:8]
            missing = [d for d in dirs if not os.path.isdir(d)]
            if missing:
                raise PolicyError(f"Not a folder on the Understudy server: {', '.join(missing)}")
            rt["path_prepend"] = dirs
        rt["edited_by"], rt["edited_at"] = by, _now()
        _save()
    return tool_runtime(tid)


DEFAULT_ESCALATION_MODEL = "gemini-pro-latest"


def setup_agent_settings() -> Dict[str, Any]:
    """8.5: the setup repair agent. {enabled, escalation_model, by, at}. enabled (default on): a failed tool setup is
    worked on by the agent (read-only checks, a pre-checked fix the admin approves, continue until it passes).
    escalation_model: the stronger model OFFERED to the admin when the same error comes back after a fix - used only
    when the admin clicks it, never switched automatically ('' = not offered).
    Env defaults: UNDERSTUDY_SETUP_AGENT (on/off), UNDERSTUDY_SETUP_AGENT_MODEL."""
    v = _load()["settings"].get("setup_agent") or {}
    env_on = os.environ.get("UNDERSTUDY_SETUP_AGENT", "on").strip().lower() not in ("0", "off", "false", "no")
    model = v.get("escalation_model")
    if model is None:
        model = os.environ.get("UNDERSTUDY_SETUP_AGENT_MODEL", DEFAULT_ESCALATION_MODEL)
    return {"enabled": bool(v["enabled"]) if "enabled" in v else env_on, "escalation_model": str(model or ""),
            "by": v.get("by"), "at": v.get("at")}


def set_setup_agent(enabled: Optional[bool] = None, escalation_model: Optional[str] = None,
                    by: str = "admin") -> Dict[str, Any]:
    import re as _re
    with _LOCK:
        v = _load()["settings"].setdefault("setup_agent", {})
        if enabled is not None:
            v["enabled"] = bool(enabled)
        if escalation_model is not None:
            m = str(escalation_model).strip()
            if m and not _re.match(r"^[A-Za-z0-9][A-Za-z0-9._/-]{1,80}$", m):
                raise PolicyError("Give a model name like gemini-pro-latest or gemini-2.5-pro.")
            v["escalation_model"] = m
        v.update(by=by, at=_now())
        _save()
    return setup_agent_settings()


def setup_lessons() -> List[Dict[str, Any]]:
    """8.5: fixes that solved a failed tool setup (newest last) - the setup agent reuses them."""
    return copy.deepcopy(_load()["settings"].get("setup_lessons") or [])


def add_setup_lesson(lesson: Dict[str, Any]) -> None:
    with _LOCK:
        lst = _load()["settings"].setdefault("setup_lessons", [])
        sig = lesson.get("signature")
        lst[:] = [x for x in lst if not (x.get("signature") == sig and x.get("ecosystem") == lesson.get("ecosystem"))]
        lst.append(dict(lesson, at=_now()))
        del lst[:-60]
        _save()


def signup_setting() -> Optional[Dict[str, Any]]:
    """Mission Control's self-signup switch (8.1): {enabled, by, at}, or None = use UNDERSTUDY_SIGNUP."""
    v = _load()["settings"].get("signup")
    return dict(v) if isinstance(v, dict) and "enabled" in v else None


def set_signup(enabled: Optional[bool], by: str = "admin") -> Optional[Dict[str, Any]]:
    """enabled=None clears the switch (back to UNDERSTUDY_SIGNUP)."""
    with _LOCK:
        st = _load()["settings"]
        if enabled is None:
            st.pop("signup", None)
        else:
            st["signup"] = {"enabled": bool(enabled), "by": by, "at": _now()}
        _save()
    return signup_setting()


# 8.2: self-signup can be limited to company email domains ("only @acme.com addresses may sign up").
_DOMAIN_RX = re.compile(r"^(\*\.)?(?=.{3,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def normalize_domain(value: str) -> str:
    """'@Acme.com' / 'acme.com' / '*.acme.com' -> 'acme.com' / '*.acme.com'. Raises PolicyError."""
    d = str(value or "").strip().lower().lstrip("@").rstrip(".")
    if d.startswith("*@"):
        d = d[2:]
    if not _DOMAIN_RX.match(d):
        raise PolicyError(f"'{value}' is not an email domain (e.g. acme.com, or *.acme.com for its subdomains).")
    return d


def _env_signup_domains() -> List[str]:
    out = []
    for d in (os.environ.get("UNDERSTUDY_SIGNUP_DOMAINS") or "").replace(";", ",").split(","):
        try:
            nd = normalize_domain(d) if d.strip() else ""
        except PolicyError:
            nd = ""
        if nd and nd not in out:
            out.append(nd)
    return out


def signup_domains_setting() -> Optional[Dict[str, Any]]:
    """{domains, by, at} set in Mission Control, or None = use UNDERSTUDY_SIGNUP_DOMAINS (default: any domain)."""
    v = _load()["settings"].get("signup_domains")
    return dict(v) if isinstance(v, dict) and isinstance(v.get("domains"), list) else None


def signup_domains() -> List[str]:
    """The email domains allowed to self-sign-up; [] = any domain."""
    s = signup_domains_setting()
    return list(s["domains"]) if s else _env_signup_domains()


def set_signup_domains(domains: Optional[List[str]], by: str = "admin") -> Optional[Dict[str, Any]]:
    """domains=None clears the admin setting (back to UNDERSTUDY_SIGNUP_DOMAINS); [] = any domain."""
    clean: List[str] = []
    if domains is not None:
        if len(domains) > 50:
            raise PolicyError("At most 50 domains.")
        for d in domains:
            if not str(d or "").strip():
                continue
            nd = normalize_domain(d)
            if nd not in clean:
                clean.append(nd)
    with _LOCK:
        st = _load()["settings"]
        if domains is None:
            st.pop("signup_domains", None)
        else:
            st["signup_domains"] = {"domains": clean, "by": by, "at": _now()}
        _save()
    return signup_domains_setting()


def email_domain_allowed(email: str, domains: Optional[List[str]] = None) -> bool:
    domains = signup_domains() if domains is None else domains
    if not domains:
        return True
    host = str(email or "").rsplit("@", 1)[-1].strip().lower().rstrip(".")
    for d in domains:
        if d.startswith("*."):
            if host == d[2:] or host.endswith("." + d[2:]):
                return True
        elif host == d:
            return True
    return False


# ---------------------------------------------------------------------------
# 8.2: package managers / build tools the admin enables, and toolchains registered for the whole server
# ---------------------------------------------------------------------------
def package_manager_settings() -> Dict[str, Dict[str, Any]]:
    """{pm_id: {enabled, by, at}} - only what the admin changed; everything else uses the catalog default."""
    v = _load()["settings"].get("package_managers")
    return copy.deepcopy(v) if isinstance(v, dict) else {}


def set_package_manager(pm_id: str, enabled: bool, by: str = "admin") -> Dict[str, Any]:
    with _LOCK:
        st = _load()["settings"].setdefault("package_managers", {})
        st[pm_id] = {"enabled": bool(enabled), "by": by, "at": _now()}
        _save()
        return dict(st[pm_id])


def toolchains() -> Dict[str, Dict[str, Any]]:
    """Toolchains registered in Mission Control: {key: {home, bin, env_name, version, source, by, at}}."""
    v = _load()["settings"].get("toolchains")
    return copy.deepcopy(v) if isinstance(v, dict) else {}


def set_toolchain(key: str, info: Optional[Dict[str, Any]], by: str = "admin") -> None:
    """info=None removes it."""
    with _LOCK:
        st = _load()["settings"].setdefault("toolchains", {})
        if info is None:
            st.pop(key, None)
        else:
            st[key] = {k: info.get(k) for k in ("home", "bin", "env_name", "version", "source", "kind", "extra_bins",
                                                "provides")}
            st[key].update(by=by, at=_now())
        _save()


def toolchain_bins() -> List[str]:
    """bin folders of the registered toolchains that still exist (put in front of PATH for installs and runs)."""
    from core.wizard.runtimes import resolve_path        # 9.9: the release this machine has (Cloud Run instances)
    out = []
    for e in toolchains().values():
        for b in [e.get("bin")] + list(e.get("extra_bins") or []):      # 8.3: conda prefixes have several bin folders
            b = resolve_path(b) if b else b
            if b and os.path.isdir(b) and b not in out:
                out.append(b)
    return out


def toolchain_homes() -> Dict[str, str]:
    from core.wizard.runtimes import resolve_path
    out = {}
    for e in toolchains().values():
        home = resolve_path(e["home"]) if e.get("home") else ""
        if e.get("env_name") and home and os.path.isdir(home):
            out[e["env_name"]] = home
    return out


def quick_targets() -> List[Dict[str, Any]]:
    """URLs shown to users as one-click targets in Step 1."""
    return [{"url": e["url"], "note": e.get("note", "")} for e in _load()["url_exceptions"]]


# ---------------------------------------------------------------------------
# Tool whitelist + requests
# ---------------------------------------------------------------------------
def whitelist() -> Dict[str, Dict[str, Any]]:
    return copy.deepcopy(_load()["tool_whitelist"])


def resolve_tool_id(tool_id: str) -> str:
    """8.2: a stack the admin composed may get another id than the tool a user/SOP asked for ("cypress" ->
    "cypress-javascript"); the approved entry keeps the requested id as an alias."""
    wl = _load()["tool_whitelist"]
    if tool_id in wl:
        return tool_id
    for fid, e in wl.items():
        if tool_id in (e.get("aliases") or []):
            return fid
    return tool_id


def add_tool_alias(tool_id: str, alias: str) -> None:
    with _LOCK:
        e = _load()["tool_whitelist"].get(tool_id)
        if e is None or not alias or alias == tool_id:
            return
        if alias not in e.setdefault("aliases", []):
            e["aliases"].append(alias)
            _save()


def is_tool_allowed(tool_id: str) -> bool:
    return resolve_tool_id(tool_id) in _load()["tool_whitelist"]


# ---------------------------------------------------------------------------------------------------------------
# 9.0: who may use an approved tool - every user (default), the users the admin selected, or (9.9) no user
# ---------------------------------------------------------------------------------------------------------------
ACCESS_MODES = ("all", "selected", "none")


def tool_access(tool_id: str) -> Dict[str, Any]:
    e = _load()["tool_whitelist"].get(resolve_tool_id(tool_id)) or {}
    a = e.get("access") or {}
    mode = a.get("mode") if a.get("mode") in ACCESS_MODES else "all"
    return {"mode": mode, "users": list(a.get("users") or []) if mode == "selected" else [],
            "by": a.get("by"), "at": a.get("at")}


def access_denied_message(name: str, tool_id: str) -> str:
    """What a user who may not use an approved tool is told (selected users only, or no user at all)."""
    if tool_access(tool_id)["mode"] == "none":
        return f"Your admin turned {name} off for all users for now."
    return f"Your admin made {name} available to selected users only."


def set_tool_access(tool_id: str, mode: str, users: Optional[List[str]] = None, by: str = "admin") -> Dict[str, Any]:
    """mode 'all' - every Studio user; 'selected' - only ``users`` (usernames); 'none' (9.9) - no user: the tool
    stays installed and approved, but nobody can pick or run it until the admin opens it again."""
    if mode not in ACCESS_MODES:
        raise PolicyError("Choose 'all', 'selected' or 'none'.")
    if is_builtin(tool_id):
        raise PolicyError("This tool is built into Understudy and always available to every user.")
    clean = []
    for u in users or []:
        u = str(u).strip().lower()
        if u and re.match(r"^[a-z0-9][a-z0-9._@+-]{0,119}$", u) and u not in clean:
            clean.append(u)
    if mode == "selected" and not clean:
        raise PolicyError("Pick at least one user - or allow all users.")
    tid = resolve_tool_id(tool_id)
    with _LOCK:
        e = _load()["tool_whitelist"].get(tid)
        if e is None:
            raise PolicyError("This tool isn't approved.")
        e["access"] = {"mode": mode, "users": clean if mode == "selected" else [], "by": by, "at": _now()}
        _save()
    return tool_access(tid)


def user_may_use(tool_id: str, user: Optional[str]) -> bool:
    """Approved AND (open to all users, or this user was selected). Without Studio accounts (user None / 'local')
    there is no one to restrict, so an approved tool is usable - unless the admin set it to no user (9.9)."""
    if not is_tool_allowed(tool_id):
        return False
    a = tool_access(tool_id)
    if a["mode"] == "none":
        return False
    if a["mode"] == "all" or not user or user == "local":
        return True
    return str(user).strip().lower() in a["users"]


def allowed_spec(tool_id: str, version: Optional[str] = None, user: Optional[str] = None) -> Optional[FrameworkSpec]:
    """The admin-approved spec, with the primary package pinned to ``version`` (must be one the admin allowed) or,
    when no version is requested, to the admin's default version. 9.3: ``user`` adds what the admin approved for that
    user only (extra allowed versions, extra packages)."""
    from core.wizard import tool_versions
    tool_id = resolve_tool_id(tool_id)
    entry = _load()["tool_whitelist"].get(tool_id)
    if not entry:
        return None
    spec = FrameworkSpec.from_dict(entry["spec"])
    mine = user_overrides(tool_id, user)
    if mine["packages"]:
        have = {p.name.lower() for p in spec.packages}
        extra = [Package(x["name"], x.get("version") or "") for x in mine["packages"] if x["name"].lower() not in have]
        if extra:
            import dataclasses
            spec = dataclasses.replace(spec, packages=list(spec.packages) + extra)
    pol = entry.get("versions") or {}
    allowed = (pol.get("allowed") or []) + [v for v in mine["versions"] if v not in (pol.get("allowed") or [])] \
        if pol.get("allowed") else []
    if version:
        if allowed and version not in allowed:
            raise PolicyError(f"Version {version} of {spec.name} is not allowed by your admin. "
                              f"Allowed: {', '.join(allowed)}")
        if not allowed:
            version = tool_versions.validate_version(version)
    else:
        version = pol.get("default") or None
    return tool_versions.apply_version(spec, version) if version else spec


def tool_version_policy(tool_id: str, user: Optional[str] = None) -> Dict[str, Any]:
    """The versions the admin allows. 9.3: with ``user``, plus the versions allowed for that user only
    (``user_allowed``); an empty ``allowed`` still means 'no restriction'."""
    entry = _load()["tool_whitelist"].get(resolve_tool_id(tool_id)) or {}
    pol = entry.get("versions") or {}
    allowed = list(pol.get("allowed") or [])
    mine = [v for v in user_overrides(tool_id, user)["versions"] if v not in allowed] if allowed and user else []
    return {"allowed": allowed + mine, "default": pol.get("default"), "user_allowed": mine,
            "set_by": pol.get("set_by"), "set_at": pol.get("set_at")}


def set_tool_versions(tool_id: str, allowed: List[str], default: Optional[str] = None,
                      set_by: str = "admin") -> Dict[str, Any]:
    """allowed = [] means 'no restriction' (the vetted spec's own constraint is used)."""
    from core.wizard import tool_versions
    with _LOCK:
        data = _load()
        entry = data["tool_whitelist"].get(tool_id)
        if not entry:
            raise PolicyError("Whitelist the tool before choosing its versions")
        try:
            clean = []
            for v in allowed or []:
                v = tool_versions.validate_version(v)
                if v not in clean:
                    clean.append(v)
            if len(clean) > 30:
                raise PolicyError("Choose at most 30 versions")
            default = tool_versions.validate_version(default) if default else None
        except ValueError as e:
            raise PolicyError(str(e))
        if default and clean and default not in clean:
            raise PolicyError("The default version must be one of the allowed versions")
        spec = FrameworkSpec.from_dict(entry["spec"])
        clash = [v for v in clean if tool_block_for(spec, version=v)]
        if clash:
            raise PolicyError(f"Version(s) {', '.join(clash)} of {spec.name} are on the blocked list - unblock them first.")
        if clean and not default:
            default = clean[0]
        entry["versions"] = {"allowed": clean, "default": default, "set_by": set_by, "set_at": _now()}
        _save()
        return tool_version_policy(tool_id)


def whitelist_tool(spec: FrameworkSpec, approved_by: str = "admin", source: str = "admin",
                   versions: Optional[Dict[str, Any]] = None, setup: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if is_builtin(spec.id):                              # 9.9: already approved for everyone, nothing to change
        return copy.deepcopy(_load()["tool_whitelist"][spec.id])
    validate_spec(spec)
    blk = tool_block_for(spec)
    if blk and not blk.get("versions"):
        raise PolicyError(f"{spec.name} is on the blocked tools list ({blk['name']}). Remove it from the blocked list "
                          f"first - a tool can't be both approved and blocked.")
    with _LOCK:
        data = _load()
        entry = {"spec": spec.to_dict(), "approved_by": approved_by, "approved_at": _now(), "source": source}
        if setup:
            entry["setup"] = setup                       # the AI setup plan the admin approved (7.2)
        old = data["tool_whitelist"].get(spec.id)
        if versions is not None:
            entry["versions"] = versions
        elif old and old.get("versions"):
            entry["versions"] = old["versions"]          # re-approving keeps the admin's version choices
        data["tool_whitelist"][spec.id] = entry
        _save()
        return copy.deepcopy(entry)


def add_tool_packages(tool_id: str, packages: List[Dict[str, str]], by: str = "admin") -> List[str]:
    """8.5.3: the admin adds packages to an approved tool (e.g. robotframework-jsonlibrary for a Robot suite that
    imports JSONLibrary). Names are validated like any AI fix; the next install / run installs them. Returns the names
    actually added."""
    from core.wizard import setup_doctor
    tid = resolve_tool_id(tool_id)
    with _LOCK:
        data = _load()
        e = data["tool_whitelist"].get(tid)
        if e is None:
            raise PolicyError("This tool isn't approved.")
        spec = FrameworkSpec.from_dict(e["spec"])
        problems: List[str] = []
        clean = setup_doctor.clean_add_packages(packages, spec, problems)
        if problems:
            raise PolicyError("; ".join(problems))
        before = {p.name.lower() for p in spec.packages}
        spec = setup_doctor.apply_overrides(spec, {"add_packages": clean})
        validate_spec(spec)
        added = [p.name for p in spec.packages if p.name.lower() not in before]
        e["spec"] = spec.to_dict()
        e.setdefault("added_packages", []).extend({"name": n, "by": by, "at": _now()} for n in added)
        _save()
    return added


def whitelist_catalog_tool(tool_id: str, approved_by: str = "admin") -> Dict[str, Any]:
    if tool_id not in CATALOG:
        raise PolicyError(f"'{tool_id}' is not in the catalog; approve it through a request instead")
    return whitelist_tool(FrameworkSpec.from_dict(CATALOG[tool_id].to_dict()), approved_by, source="catalog")


def remove_tool(tool_id: str) -> None:
    if is_builtin(tool_id):
        raise PolicyError("Understudy Gemini Agent is built into Understudy - it can't be removed.")
    with _LOCK:
        data = _load()
        if tool_id not in data["tool_whitelist"]:
            raise PolicyError("Tool is not whitelisted")
        del data["tool_whitelist"][tool_id]
        _save()


def delete_tool(tool_id: str, by: str = "admin") -> Dict[str, Any]:
    """9.0: removes an approved tool for everyone AND frees what its setup installed on the server (its isolated
    environments). Sessions that used it are told it was removed when they run next. Returns what was cleaned."""
    import glob
    import shutil
    tid = resolve_tool_id(tool_id)
    remove_tool(tid)
    freed = []
    try:
        from core.wizard.dependency_resolver import ENVS_DIR
        for d in glob.glob(os.path.join(ENVS_DIR, f"{tid}-*")):
            if os.path.isdir(d) and os.path.basename(d)[len(tid) + 1:].isalnum():
                shutil.rmtree(d, ignore_errors=True)
                freed.append(os.path.basename(d))
    except Exception:
        pass
    return {"tool_id": tid, "environments_removed": freed, "by": by}


# ---------------------------------------------------------------------------
# Blocked tools (7.2): a tool - or some of its versions - users may never use or request
# ---------------------------------------------------------------------------
def _tkey(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def tool_keys(spec: Optional[FrameworkSpec] = None, name: str = "") -> set:
    """Every name a tool is known by: id, display name (with and without the '(...)' part), package names."""
    keys = {_tkey(name)} if name else set()
    if spec is not None:
        keys |= {_tkey(spec.id), _tkey(spec.name), _tkey(spec.name.split("(")[0])}
        keys |= {_tkey(p.name.split(":")[-1]) for p in spec.packages}
    return {k for k in keys if k}


def _id_prefixes(spec: Optional[FrameworkSpec]) -> List[str]:
    return [spec.id.lower()] if spec is not None and spec.id else []


def _version_matches(pattern: str, version: str) -> bool:
    pattern, version = (pattern or "").strip(), (version or "").strip().lstrip("=")
    if not pattern or not version:
        return False
    if pattern.endswith("*"):
        return version.startswith(pattern[:-1])
    return pattern == version


def blocked_tools() -> List[Dict[str, Any]]:
    return copy.deepcopy(_load().get("tool_blocklist", []))


def tool_block_for(spec: Optional[FrameworkSpec] = None, name: str = "", version: Optional[str] = None
                   ) -> Optional[Dict[str, Any]]:
    """The blocked-list entry that covers this tool (and version), or None. An entry with no versions blocks the tool;
    one with versions blocks only those (``3.*`` = every 3.x). 9.9: a built-in tool is never blocked (blocking its
    packages, e.g. Playwright, blocks only the traditional tools)."""
    if spec is not None and is_builtin(spec.id):
        return None
    keys = tool_keys(spec, name)
    ids = _id_prefixes(spec)
    for e in _load().get("tool_blocklist", []):
        k = e.get("key") or ""
        # exact name / id / package, or the tool family: "selenium" blocks selenium-python and selenium-java
        hit = k in keys or any(i == k or i.split("-")[0] == k for i in ids)
        if not hit:
            continue
        vers = e.get("versions") or []
        if not vers:
            return copy.deepcopy(e)
        if version and any(_version_matches(v, version) for v in vers):
            return copy.deepcopy(e)
    return None


def add_blocked_tool(name: str, versions: Optional[List[str]] = None, note: str = "", added_by: str = "admin"
                     ) -> Dict[str, Any]:
    """Blocks a tool by name / id / package (e.g. 'Selenium', 'cypress') - optionally only some versions."""
    raw = (name or "").strip()
    key = _tkey(raw)
    if not key or len(raw) > 80:
        raise PolicyError("Enter the tool's name, id or package (e.g. Selenium, cypress, robotframework)")
    clean_versions = []
    for v in versions or []:
        v = str(v).strip()
        if not re.match(r"^[A-Za-z0-9.+_-]{1,40}\*?$", v):
            raise PolicyError(f"'{v}' is not a version (use 1.2.3, or 1.* for every 1.x)")
        if v not in clean_versions:
            clean_versions.append(v)
    if any(key in tool_keys(None, n) or key == _tkey(fid) for fid in BUILTIN_TOOLS if fid in CATALOG
           for n in (CATALOG[fid].name, CATALOG[fid].name.split("(")[0])):
        raise PolicyError("Understudy Gemini Agent is built into Understudy - it can't be blocked.")
    with _LOCK:
        data = _load()
        probe = {"key": key, "name": raw, "versions": clean_versions}
        data.setdefault("tool_blocklist", [])
        for tid, entry in data["tool_whitelist"].items():
            if entry.get("builtin"):
                continue
            spec = FrameworkSpec.from_dict(entry["spec"])
            data["tool_blocklist"].append(probe)
            try:
                if not clean_versions and tool_block_for(spec):
                    raise PolicyError(f"{spec.name} is on the approved tools list. A tool can't be both approved and "
                                      f"blocked - remove it from the approved list first.")
                allowed = (entry.get("versions") or {}).get("allowed") or []
                clash = [v for v in allowed if tool_block_for(spec, version=v)]
                if clash:
                    raise PolicyError(f"Version(s) {', '.join(clash)} of {spec.name} are allowed. Remove them from the "
                                      f"allowed versions first.")
            finally:
                data["tool_blocklist"].remove(probe)
        for e in data["tool_blocklist"]:
            if e["key"] == key and sorted(e.get("versions") or []) == sorted(clean_versions):
                return copy.deepcopy(e)
        entry = {"id": "tblk-" + uuid.uuid4().hex[:8], "key": key, "name": raw, "versions": clean_versions,
                 "note": str(note or "")[:200], "added_by": added_by, "added_at": _now()}
        data["tool_blocklist"].append(entry)
        # pending requests for what is now blocked are closed
        for r in data["tool_requests"]:
            if r["status"] in ACTIVE_REQUEST_STATES:
                spec = FrameworkSpec.from_dict(r["spec"]) if r.get("spec") else None
                if tool_block_for(spec, r.get("tool_name", ""), r.get("version")):
                    r.update(status="rejected", decided_at=_now(), decided_by=added_by,
                             admin_note=f"Blocked by the admin: {raw}" + (f" {', '.join(clean_versions)}" if clean_versions else ""))
        _save()
        return copy.deepcopy(entry)


def remove_blocked_tool(entry_id: str) -> None:
    with _LOCK:
        data = _load()
        before = len(data.get("tool_blocklist", []))
        data["tool_blocklist"] = [e for e in data.get("tool_blocklist", []) if e["id"] != entry_id]
        if len(data["tool_blocklist"]) == before:
            raise PolicyError("No such blocked tool")
        _save()


class ToolBlocked(PolicyError):
    def __init__(self, entry: Dict[str, Any], what: str):
        self.entry = entry
        why = f" ({entry['note']})" if entry.get("note") else ""
        super().__init__(f"{what} is blocked by your admin{why}. It can't be used or requested.")


# ---------------------------------------------------------------------------
# Tool and version requests (7.2: users AND the admin start them; the AI prepares the setup - core/wizard/tool_setup.py)
# ---------------------------------------------------------------------------
# 8.0: installing -> smoke_testing -> approved (ACTIVE); a failure on the way -> awaiting_admin (the AI asks the admin a
# question and resumes with the answer).
# 8.2 adds the AI install assistant in front (suggesting -> stack_ready: the admin picks language, package managers,
# components and versions) and a review gate in the middle (smoke_review: the admin reads the AI-written smoke test
# before it runs).
ACTIVE_REQUEST_STATES = ("pending", "suggesting", "stack_ready", "planning", "plan_ready", "installing", "smoke_review",
                         "smoke_testing", "awaiting_admin", "setup_failed")
REGISTRY_STATUS = {"pending": "REQUESTED", "suggesting": "REQUESTED", "stack_ready": "CHOOSE_STACK",
                   "planning": "REQUESTED", "plan_ready": "PROPOSED", "installing": "INSTALLING",
                   "smoke_review": "SMOKE_REVIEW", "smoke_testing": "SMOKE_TESTING", "awaiting_admin": "NEEDS_ADMIN",
                   "setup_failed": "FAILED", "approved": "ACTIVE", "rejected": "REJECTED"}


def requests(status: Optional[str] = None) -> List[Dict[str, Any]]:
    items = copy.deepcopy(_load()["tool_requests"])
    if status == "pending":
        items = [r for r in items if r["status"] in ACTIVE_REQUEST_STATES]
    elif status:
        items = [r for r in items if r["status"] == status]
    return sorted(items, key=lambda r: r["created_at"], reverse=True)


def get_request(request_id: str) -> Dict[str, Any]:
    for r in _load()["tool_requests"]:
        if r["id"] == request_id:
            return r
    raise PolicyError("No such request")


def update_request(request_id: str, **fields) -> Dict[str, Any]:
    with _LOCK:
        r = get_request(request_id)
        r.update(fields)
        _save()
        return copy.deepcopy(r)


def request_tool(spec: FrameworkSpec, session_id: str, reason: str = "", source: str = "user",
                 requested_by: Optional[str] = None, typed_name: str = "", version: Optional[str] = None) -> Dict[str, Any]:
    """Creates (or reuses) an access request for a tool that is neither approved nor blocked. ``version``: the
    version the user (or the SOP) asked for - the AI setup proposes it first."""
    validate_spec(spec)
    blk = tool_block_for(spec, typed_name)
    if blk and not blk.get("versions"):
        raise ToolBlocked(blk, spec.name)
    with _LOCK:
        data = _load()
        if spec.id in data["tool_whitelist"]:
            return {"id": None, "tool_id": spec.id, "status": "approved", "already_whitelisted": True}
        for r in data["tool_requests"]:
            if r["tool_id"] == spec.id and r.get("kind", "tool") == "tool" and r["status"] in ACTIVE_REQUEST_STATES:
                if session_id and session_id not in r["sessions"]:
                    r["sessions"].append(session_id)
                if requested_by and requested_by not in r.setdefault("requested_by", []):
                    r["requested_by"].append(requested_by)
                if version and not r.get("version") and r["status"] in ("pending", "planning"):
                    r["version"] = version
                _save()
                return copy.deepcopy(r)
        r = {"id": "REQ-" + uuid.uuid4().hex[:6].upper(), "kind": "tool", "tool_id": spec.id, "tool_name": spec.name,
             "version": version or None, "spec": spec.to_dict(), "reason": str(reason or "")[:500], "source": source,
             "requested_by": [requested_by] if requested_by else [], "sessions": [session_id] if session_id else [],
             "status": "pending", "created_at": _now(), "decided_at": None, "decided_by": None, "admin_note": "",
             "plan": None, "plan_history": [], "instructions": [], "setup": None}
        data["tool_requests"].append(r)
        _save()
        return copy.deepcopy(r)


def request_version(tool_id: str, version: str, session_id: str, reason: str = "",
                    requested_by: Optional[str] = None) -> Dict[str, Any]:
    """A user wants a version of an approved tool that the admin hasn't allowed (and hasn't blocked)."""
    from core.wizard import tool_versions
    try:
        version = tool_versions.validate_version(version)
    except ValueError as e:
        raise PolicyError(str(e))
    with _LOCK:
        data = _load()
        entry = data["tool_whitelist"].get(tool_id)
        if not entry:
            raise PolicyError("Request the tool itself first - it isn't approved yet.")
        spec = FrameworkSpec.from_dict(entry["spec"])
        blk = tool_block_for(spec, version=version)
        if blk:
            raise ToolBlocked(blk, f"{spec.name} {version}")
        allowed = (entry.get("versions") or {}).get("allowed") or []
        if not allowed or version in allowed:
            return {"id": None, "kind": "version", "tool_id": tool_id, "version": version, "status": "approved",
                    "already_allowed": True}
        for r in data["tool_requests"]:
            if r.get("kind") == "version" and r["tool_id"] == tool_id and r.get("version") == version \
                    and r["status"] in ACTIVE_REQUEST_STATES:
                if session_id and session_id not in r["sessions"]:
                    r["sessions"].append(session_id)
                if requested_by and requested_by not in r.setdefault("requested_by", []):
                    r["requested_by"].append(requested_by)
                _save()
                return copy.deepcopy(r)
        r = {"id": "REQ-" + uuid.uuid4().hex[:6].upper(), "kind": "version", "tool_id": tool_id,
             "tool_name": spec.name, "version": version, "spec": spec.to_dict(), "reason": str(reason or "")[:500],
             "source": "user", "requested_by": [requested_by] if requested_by else [],
             "sessions": [session_id] if session_id else [], "status": "pending", "created_at": _now(),
             "decided_at": None, "decided_by": None, "admin_note": "", "plan": None, "plan_history": [],
             "instructions": [], "setup": None}
        data["tool_requests"].append(r)
        _save()
        return copy.deepcopy(r)


def decide_request(request_id: str, approve: bool, note: str = "", decided_by: str = "admin") -> Dict[str, Any]:
    """Direct decision (scripts / API). Mission Control approves through the AI setup plan (core/wizard/tool_setup)."""
    with _LOCK:
        r = get_request(request_id)
        if r["status"] not in ACTIVE_REQUEST_STATES:
            raise PolicyError(f"Request already {r['status']}")
        if approve:
            try:
                if r.get("kind") == "version":
                    pol = tool_version_policy(r["tool_id"])
                    set_tool_versions(r["tool_id"], pol["allowed"] + [r["version"]], pol["default"], set_by=decided_by)
                else:
                    whitelist_tool(FrameworkSpec.from_dict(r["spec"]), decided_by, source=f"request {r['id']}")
            except FrameworkValidationError as e:
                raise PolicyError(f"Tool spec failed validation: {e}")
        r.update(status="approved" if approve else "rejected", decided_at=_now(), decided_by=decided_by,
                 admin_note=str(note or "")[:500])
        _save()
        return copy.deepcopy(r)


def request_status_for(tool_id: str, version: Optional[str] = None) -> Optional[Dict[str, Any]]:
    latest = [r for r in _load()["tool_requests"] if r["tool_id"] == tool_id and
              (r.get("kind", "tool") == "version" and r.get("version") == version if version else r.get("kind", "tool") == "tool")]
    if not latest:
        return None
    r = sorted(latest, key=lambda x: x["created_at"])[-1]
    return {"id": r["id"], "status": r["status"], "admin_note": r.get("admin_note", ""), "version": r.get("version")}


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def max_self_heal_retries() -> int:
    """How many automatic self-healing retries a run may make (1..50)."""
    try:
        v = int(_load()["settings"].get("max_self_heal_retries", DEFAULT_RETRIES))
    except (TypeError, ValueError):
        return DEFAULT_RETRIES
    return v if MIN_RETRIES <= v <= MAX_RETRIES else DEFAULT_RETRIES


def set_max_self_heal_retries(value: int) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        raise PolicyError(f"Retry limit must be a whole number between {MIN_RETRIES} and {MAX_RETRIES}")
    if not MIN_RETRIES <= v <= MAX_RETRIES:
        raise PolicyError(f"Retry limit must be between {MIN_RETRIES} and {MAX_RETRIES}")
    with _LOCK:
        _load()["settings"]["max_self_heal_retries"] = v
        _save()
    return v


HEAL_APPROVAL_MODES = ("ask", "automatic")


def self_heal_approval() -> str:
    """'ask' (default since 6.3): after a failure the AI's diagnosis, step changes and script diff are shown to the user,
    who approves them or corrects the AI before the retry runs. 'automatic': the pre-6.3 behaviour - fixes are applied
    and re-run without asking, up to the retry limit. The admin's choice wins; otherwise UNDERSTUDY_SELF_HEAL_APPROVAL."""
    v = _load()["settings"].get("self_heal_approval")
    if v in HEAL_APPROVAL_MODES:
        return v
    env = (os.environ.get("UNDERSTUDY_SELF_HEAL_APPROVAL") or "ask").lower()
    return env if env in HEAL_APPROVAL_MODES else "ask"


def set_self_heal_approval(mode: str) -> str:
    if mode not in HEAL_APPROVAL_MODES:
        raise PolicyError(f"Self-healing approval must be one of: {', '.join(HEAL_APPROVAL_MODES)}")
    with _LOCK:
        _load()["settings"]["self_heal_approval"] = mode
        _save()
    return mode


def setup_fix_approval() -> str:
    """8.3: how the AI's fixes for a failed TOOL SETUP are applied. 'ask' (default): the fix is shown to the admin as a
    one-click "Apply the AI's fix" with exactly what it changes; nothing is applied until the admin clicks.
    'automatic': safe fixes (missing packages, missing init steps, a rewritten smoke test that still needs review) are
    applied by themselves, at most 4 per setup. Env default: UNDERSTUDY_SETUP_FIX_APPROVAL."""
    v = _load()["settings"].get("setup_fix_approval")
    if v in HEAL_APPROVAL_MODES:
        return v
    env = (os.environ.get("UNDERSTUDY_SETUP_FIX_APPROVAL") or "ask").lower()
    return env if env in HEAL_APPROVAL_MODES else "ask"


def set_setup_fix_approval(mode: str) -> str:
    if mode not in HEAL_APPROVAL_MODES:
        raise PolicyError(f"Setup fix approval must be one of: {', '.join(HEAL_APPROVAL_MODES)}")
    with _LOCK:
        _load()["settings"]["setup_fix_approval"] = mode
        _save()
    return mode


def code_health_auto_apply() -> bool:
    return bool(_load()["settings"].get("code_health_auto_apply", False))


def set_code_health_auto_apply(value: bool) -> bool:
    with _LOCK:
        _load()["settings"]["code_health_auto_apply"] = bool(value)
        _save()
    return bool(value)


# ---------------------------------------------------------------------------
# Google Cloud Model Armor (7.2) - set in Mission Control, used by core/wizard/cloud_armor.py
# ---------------------------------------------------------------------------
def model_armor_setting() -> Optional[Dict[str, Any]]:
    """The admin's saved setting, or None when the admin never saved one (then the environment decides)."""
    v = _load()["settings"].get("model_armor")
    return copy.deepcopy(v) if isinstance(v, dict) else None


def set_model_armor(enabled: bool, template: str, fail_closed: bool, by: str = "admin",
                    filters: Optional[List[str]] = None) -> Dict[str, Any]:
    from core.wizard import cloud_armor
    template = (template or "").strip()
    if template and not cloud_armor.valid_template(template):
        raise PolicyError("The template must look like projects/<project>/locations/<region>/templates/<template-id>.")
    if enabled and not template:
        raise PolicyError("Enter the project, location and template before turning Google Cloud Model Armor on.")
    entry = {"enabled": bool(enabled), "template": template, "fail_closed": bool(fail_closed),
             "updated_by": by, "updated_at": _now()}
    if filters is not None:
        entry["filters"] = [f for f in filters if f in cloud_armor.FILTERS]
    elif (model_armor_setting() or {}).get("filters") and (model_armor_setting() or {}).get("template") == template:
        entry["filters"] = model_armor_setting()["filters"]
    with _LOCK:
        _load()["settings"]["model_armor"] = entry
        _save()
    return copy.deepcopy(entry)


def clear_model_armor() -> None:
    """Forget the admin's setting: the environment (UNDERSTUDY_MODEL_ARMOR_TEMPLATE) decides again."""
    with _LOCK:
        _load()["settings"].pop("model_armor", None)
        _save()


# ---------------------------------------------------------------------------
# Sandbox (8.0) - see core/wizard/sandbox/factory.py
# ---------------------------------------------------------------------------
def sandbox_setting() -> Optional[Dict[str, Any]]:
    v = _load()["settings"].get("sandbox")
    return copy.deepcopy(v) if isinstance(v, dict) else None


def set_sandbox(mode: str = "local", by: str = "admin", **limits) -> Dict[str, Any]:
    """The sandbox limits (9.9.1: the mode is always 'local' - Understudy runs on Cloud Run, which has no Docker)."""
    from core.wizard.sandbox import factory
    if (mode or "local") not in factory.MODES:
        raise PolicyError("The sandbox is always 'local': Understudy runs on Cloud Run, where Docker is not available.")
    mode = "local"
    entry: Dict[str, Any] = {"mode": mode, "updated_by": by, "updated_at": _now()}
    for k, (lo, hi) in factory.BOUNDS.items():
        if limits.get(k) is None:
            continue
        try:
            v = int(limits[k])
        except (TypeError, ValueError):
            raise PolicyError(f"{k} must be a number")
        if not lo <= v <= hi:
            raise PolicyError(f"{k} must be between {lo} and {hi}")
        entry[k] = v
    with _LOCK:
        old = _load()["settings"].get("sandbox") or {}
        merged = {k: v for k, v in old.items() if k in factory.BOUNDS}
        merged.update(entry)
        _load()["settings"]["sandbox"] = merged
        _save()
    return copy.deepcopy(merged)


# ---------------------------------------------------------------------------
# Tool registry view (8.0): every tool with its lifecycle status
# ---------------------------------------------------------------------------
def registry() -> List[Dict[str, Any]]:
    data = _load()
    out = []
    for fid, e in data["tool_whitelist"].items():
        if e.get("builtin"):
            continue                                              # 9.9: built in - nothing for the admin to manage
        setup = e.get("setup") or {}
        out.append({"tool_id": fid, "name": e["spec"].get("name", fid), "status": "ACTIVE", "access": tool_access(fid),
                    "versions": tool_version_policy(fid), "source": e.get("source"), "approved_by": e.get("approved_by"),
                    "approved_at": e.get("approved_at"), "smoke": setup.get("smoke"), "verified": setup.get("verified"),
                    "baseline": bool(setup.get("baseline")), "request": setup.get("request"),
                    "sandbox": setup.get("sandbox")})
    active = set(data["tool_whitelist"])
    for r in sorted(data["tool_requests"], key=lambda x: x["created_at"], reverse=True):
        if r["status"] == "approved" or (r.get("kind", "tool") == "tool" and r["tool_id"] in active):
            continue
        if r.get("kind") == "version" and r["status"] not in ACTIVE_REQUEST_STATES:
            continue
        if any(o["tool_id"] == r["tool_id"] and o.get("request") == r["id"] for o in out):
            continue
        out.append({"tool_id": r["tool_id"], "name": r["tool_name"] + (f" {r['version']}" if r.get("version") else ""),
                    "status": REGISTRY_STATUS.get(r["status"], r["status"].upper()), "request": r["id"],
                    "kind": r.get("kind", "tool"), "version": r.get("version"), "sessions": len(r.get("sessions") or []),
                    "requested_by": r.get("requested_by") or [], "created_at": r["created_at"]})
    return out


def add_message(request_id: str, role: str, kind: str, text: str, data: Optional[Dict[str, Any]] = None
                ) -> Dict[str, Any]:
    """Appends to a request's conversation (the Mission Control setup chat): role ai | admin | system | user."""
    msg = {"id": "m-" + uuid.uuid4().hex[:8], "role": role, "kind": kind, "text": str(text)[:4000], "at": _now()}
    if data:
        msg["data"] = data
    with _LOCK:
        r = get_request(request_id)
        conv = r.setdefault("conversation", [])
        conv.append(msg)
        if len(conv) > 200:
            del conv[:len(conv) - 200]
        _save()
    return copy.deepcopy(msg)


def record_smoke(tool_id: str, result: Dict[str, Any]) -> None:
    with _LOCK:
        e = _load()["tool_whitelist"].get(tool_id)
        if not e:
            raise PolicyError("Tool is not active")
        e.setdefault("setup", {})["smoke"] = {k: v for k, v in result.items() if k != "files"}
        if result.get("status") == "PASSED":
            e["setup"]["verified"] = True
        _save()


def tool_runtime(tool_id: str, user: Optional[str] = None) -> Dict[str, Any]:
    """Environment the tool's setup established (JAVA_HOME, PATH additions) - used by every run.
    9.3: with ``user``, that user's own run settings are layered on top (their variables win)."""
    e = _load()["tool_whitelist"].get(resolve_tool_id(tool_id)) or {}
    rt = (e.get("setup") or {}).get("runtime") or {}
    mine = user_overrides(tool_id, user) if user else {"env_vars": {}, "path_prepend": []}
    from core.wizard.runtimes import resolve_path        # 9.9: a downloaded runtime's release on THIS machine
    env = {k: resolve_path(v) if isinstance(v, str) else v for k, v in dict(rt.get("env_vars") or {}, **mine["env_vars"]).items()}
    path = mine["path_prepend"] + [p for p in rt.get("path_prepend") or [] if p not in mine["path_prepend"]]
    return {"env_vars": env, "path_prepend": [resolve_path(p) for p in path],
            "java_release": rt.get("java_release"),
            "java_home": resolve_path(rt.get("java_home") or ""), "versions": copy.deepcopy(rt.get("versions") or {})}


def tool_requirements(tool_id: str) -> Dict[str, Any]:
    """8.4: the runtime versions an approved tool needs, as stored: {kind: {spec, source, reason, by, at}}."""
    e = _load()["tool_whitelist"].get(resolve_tool_id(tool_id)) or {}
    return copy.deepcopy((e.get("setup") or {}).get("requirements") or {})


def tool_tested_with(tool_id: str) -> Dict[str, Any]:
    e = _load()["tool_whitelist"].get(resolve_tool_id(tool_id)) or {}
    return copy.deepcopy((e.get("setup") or {}).get("tested_with") or {})


def set_tool_requirements(tool_id: str, requirements: Dict[str, Any], by: str = "admin",
                          replace: bool = False) -> Dict[str, Any]:
    """8.4: {kind: {spec, source, reason}} - a kind with an empty spec is removed. ``replace`` drops the others."""
    tid = resolve_tool_id(tool_id)
    with _LOCK:
        e = _load()["tool_whitelist"].get(tid)
        if e is None:
            raise PolicyError("This tool isn't approved.")
        cur = {} if replace else dict((e.setdefault("setup", {})).get("requirements") or {})
        for kind, req in (requirements or {}).items():
            if not req or not str((req or {}).get("spec") or "").strip():
                cur.pop(kind, None)
            else:
                cur[kind] = {"spec": str(req["spec"])[:60], "source": str(req.get("source") or "admin")[:20],
                             "reason": str(req.get("reason") or "")[:300], "by": by, "at": _now()}
        e.setdefault("setup", {})["requirements"] = cur
        _save()
        return copy.deepcopy(cur)


def set_tool_tested_with(tool_id: str, tested: Dict[str, Any]) -> None:
    tid = resolve_tool_id(tool_id)
    with _LOCK:
        e = _load()["tool_whitelist"].get(tid)
        if e is not None:
            e.setdefault("setup", {})["tested_with"] = dict(tested or {}, at=_now())
            _save()


def set_tool_runtime_choice(tool_id: str, kind: str, choice: Optional[Dict[str, Any]], by: str = "admin") -> Dict[str, Any]:
    """8.4: which installed version of a runtime (java, python, node, ruby, go, dotnet, maven, gradle) every run of the
    tool uses. ``choice`` = {install_id, version, path, home, bin, env: {NAME: value}} or None (= the server default).
    The previous choice's PATH folder and variables are taken out first, so switching back and forth never piles up.
    Python is applied when the tool's isolated environment is built (dependency_resolver.plan_for)."""
    tid = resolve_tool_id(tool_id)
    with _LOCK:
        e = _load()["tool_whitelist"].get(tid)
        if e is None:
            raise PolicyError("This tool isn't approved.")
        rt = e.setdefault("setup", {}).setdefault("runtime", {})
        chosen = rt.setdefault("versions", {})
        env = dict(rt.get("env_vars") or {})
        pp = list(rt.get("path_prepend") or [])
        old = chosen.pop(kind, None)
        if old:
            if old.get("bin"):
                pp = [p for p in pp if p != old["bin"]]
            for k, v in (old.get("env") or {}).items():
                if env.get(k) == v:
                    env.pop(k)
        if kind == "java":
            # the JDK the setup pinned (and the compiler release it passed with) no longer applies
            if rt.get("java_home") and env.get("JAVA_HOME") == rt.get("java_home"):
                env.pop("JAVA_HOME")
                pp = [p for p in pp if p != os.path.join(rt["java_home"], "bin")]
            rt.pop("java_home", None)
            rt.pop("java_release", None)
        if choice:
            if choice.get("bin") and kind != "python":
                if not os.path.isdir(choice["bin"]):
                    raise PolicyError(f"Not a folder on the Understudy server: {choice['bin']}")
                pp = [choice["bin"]] + [p for p in pp if p != choice["bin"]]
            env.update({str(k): str(v) for k, v in (choice.get("env") or {}).items()})
            chosen[kind] = {k: choice.get(k) for k in ("install_id", "version", "path", "home", "bin", "env", "source")}
            chosen[kind].update(by=by, at=_now())
        rt["env_vars"], rt["path_prepend"] = env, pp[:8]
        rt["edited_by"], rt["edited_at"] = by, _now()
        _save()
    return tool_runtime(tid)



# ---------------------------------------------------------------------------
# 9.3: per-user tool changes - what the admin approved in an escalation "for this user only"
# ---------------------------------------------------------------------------
OVERRIDE_KINDS = ("package", "version", "env", "path")


def _ukey(user: Optional[str]) -> str:
    return str(user or "local").strip().lower() or "local"


def user_overrides(tool_id: str, user: Optional[str]) -> Dict[str, Any]:
    """{packages: [{name, version, by, at}], versions: [...], env_vars: {}, path_prepend: []} of one user and tool."""
    d = ((_load().get("user_overrides") or {}).get(_ukey(user)) or {}).get(resolve_tool_id(tool_id)) or {}
    return {"packages": copy.deepcopy(d.get("packages") or []), "versions": list(d.get("versions") or []),
            "env_vars": dict(d.get("env_vars") or {}), "path_prepend": list(d.get("path_prepend") or [])}


def all_user_overrides() -> List[Dict[str, Any]]:
    """Every per-user change, flat, for Mission Control: [{user, tool_id, kind, key, value, by, at}]."""
    out: List[Dict[str, Any]] = []
    for user, tools in sorted((_load().get("user_overrides") or {}).items()):
        for tid, d in sorted(tools.items()):
            meta = d.get("meta") or {}
            for p in d.get("packages") or []:
                out.append({"user": user, "tool_id": tid, "kind": "package", "key": p["name"], "value": p.get("version") or "",
                            "by": p.get("by"), "at": p.get("at")})
            for v in d.get("versions") or []:
                m = meta.get("version:" + v) or {}
                out.append({"user": user, "tool_id": tid, "kind": "version", "key": v, "value": "", "by": m.get("by"), "at": m.get("at")})
            for k, v in (d.get("env_vars") or {}).items():
                m = meta.get("env:" + k) or {}
                out.append({"user": user, "tool_id": tid, "kind": "env", "key": k, "value": v, "by": m.get("by"), "at": m.get("at")})
            for pth in d.get("path_prepend") or []:
                m = meta.get("path:" + pth) or {}
                out.append({"user": user, "tool_id": tid, "kind": "path", "key": pth, "value": "", "by": m.get("by"), "at": m.get("at")})
    return out


def add_user_override(tool_id: str, user: Optional[str], kind: str, key: str, value: str = "", by: str = "admin") -> bool:
    """Approve a change for ONE user: a package, an allowed version, a run variable or a PATH folder. Validated exactly
    like the tool-wide change. Returns False when that user already had it."""
    from core.wizard import tool_versions
    if kind not in OVERRIDE_KINDS:
        raise PolicyError(f"Unknown per-user change '{kind}'.")
    tid = resolve_tool_id(tool_id)
    key, value = str(key or "").strip(), str(value or "").strip()
    with _LOCK:
        data = _load()
        e = data["tool_whitelist"].get(tid)
        if e is None:
            raise PolicyError("This tool isn't approved.")
        d = data.setdefault("user_overrides", {}).setdefault(_ukey(user), {}).setdefault(tid, {})
        meta = d.setdefault("meta", {})
        stamp = {"by": by, "at": _now()}
        if kind == "package":
            from core.wizard import setup_doctor
            spec = FrameworkSpec.from_dict(e["spec"])
            problems: List[str] = []
            clean = setup_doctor.clean_add_packages([{"name": key, "version": value}], spec, problems)
            if problems:
                raise PolicyError("; ".join(problems))
            if not clean:
                return False                                   # the tool already has it
            name = clean[0]["name"]
            pk = d.setdefault("packages", [])
            if any(p["name"].lower() == name.lower() for p in pk) or any(p.name.lower() == name.lower() for p in spec.packages):
                return False
            pk.append(dict(clean[0], **stamp))
        elif kind == "version":
            v = tool_versions.validate_version(key.lstrip("=v"))
            vs = d.setdefault("versions", [])
            if v in vs or v in ((e.get("versions") or {}).get("allowed") or []):
                return False
            vs.append(v)
            meta["version:" + v] = stamp
        elif kind == "env":
            from core.wizard.sandbox.base import SandboxError, clean_manifest_env
            try:
                clean_env = clean_manifest_env({key: value})
            except SandboxError as ex:
                raise PolicyError(str(ex))
            if not clean_env:
                raise PolicyError(f"{key} can't be set this way.")
            d.setdefault("env_vars", {}).update(clean_env)
            meta["env:" + key] = stamp
        else:
            if not os.path.isdir(key):
                raise PolicyError(f"Not a folder on the Understudy server: {key}")
            pp = d.setdefault("path_prepend", [])
            if key in pp:
                return False
            pp.insert(0, key)
            meta["path:" + key] = stamp
        _save()
    return True


def remove_user_override(tool_id: str, user: str, kind: str, key: str) -> bool:
    tid = resolve_tool_id(tool_id)
    with _LOCK:
        data = _load()
        d = ((data.get("user_overrides") or {}).get(_ukey(user)) or {}).get(tid)
        if not d:
            return False
        before = json.dumps(d, sort_keys=True)
        if kind == "package":
            d["packages"] = [p for p in d.get("packages") or [] if p["name"].lower() != key.lower()]
        elif kind == "version":
            d["versions"] = [v for v in d.get("versions") or [] if v != key]
        elif kind == "env":
            (d.get("env_vars") or {}).pop(key, None)
        elif kind == "path":
            d["path_prepend"] = [p for p in d.get("path_prepend") or [] if p != key]
        (d.get("meta") or {}).pop(f"{kind}:{key}", None)
        changed = json.dumps(d, sort_keys=True) != before
        if changed:
            _save()
    return changed
