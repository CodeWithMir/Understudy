"""Which automation tool does the SOP require? (8.0, zero-trust tool registry)

"Use Robot Framework for this API test", "Automate it with Selenium 4.25", "Framework: Cypress" - when the SOP names a
tool, the session must use it. ``detect`` finds it (the WATCH agent's ``sop_tool_requirement`` first, then a
deterministic reading of the SOP) and ``status`` says whether it can be used:

* ``active`` - on the admin's list (and the version, if one is named, is allowed)
* ``version_not_allowed`` - the tool is active but not that version
* ``pending`` - an enablement request is already waiting for the admin
* ``blocked`` - the admin blocked it (can't be requested)
* ``unsupported`` - unknown to this installation: the user is asked to submit an enablement request
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional

from core.wizard import admin_policy
from core.wizard.frameworks import CATALOG, FrameworkSpec, match_catalog

# name in the SOP -> catalog id (None = not in the catalog; the request goes through the AI setup)
ALIASES: Dict[str, Optional[str]] = {
    "playwright": "playwright-python", "playwright python": "playwright-python",
    "playwright js": "playwright-js", "playwright javascript": "playwright-js", "playwright typescript": "playwright-js",
    "playwright node": "playwright-js", "selenium": "selenium-python", "selenium python": "selenium-python",
    "selenium webdriver": "selenium-python", "selenium java": "selenium-java", "cypress": "cypress",
    "puppeteer": "puppeteer", "robot framework": "robot-framework", "robotframework": "robot-framework",
    "rest assured": "rest-assured", "restassured": "rest-assured", "rest-assured": "rest-assured",
    "python requests": "requests-api", "webdriverio": "webdriverio", "wdio": "webdriverio",
    "appium": None, "testcafe": None, "karate": None, "k6": None, "jmeter": None, "newman": None, "postman": None,
    "katalon": None, "nightwatch": None, "codeceptjs": None, "behave": None, "cucumber": None, "gauge": None,
    "taiko": None, "pytest": None, "uipath": None, "tosca": None,
}
_NAMES = sorted(ALIASES, key=len, reverse=True)
_NAME_RX = "|".join(re.escape(n).replace(r"\ ", r"[\s_-]*") for n in _NAMES)
_TRIGGER = (r"(?:\buse|\busing|\bwith|\bvia|\bin|\bautomate[ds]?\b[^.\n]{0,30}?\b(?:with|using|in)|\bframework\s*[:=-]|"
            r"\btool\s*[:=-]|\bmust be (?:automated|scripted|written) (?:with|in|using))")
_RX = re.compile(rf"(?i){_TRIGGER}\s+(?:the\s+)?({_NAME_RX})\b(?:\s+(?:version\s+|v)?(\d+(?:\.\d+){{0,2}}))?")
_VERSION_AFTER = re.compile(r"(?i)\bversion\s+(\d+(?:\.\d+){0,2})")


def _canon(name: str) -> str:
    return re.sub(r"[\s_-]+", " ", (name or "").strip().lower())


def resolve(name: str) -> Dict[str, Any]:
    """{tool_id, name, known} for a tool name from an SOP."""
    key = _canon(name)
    if key in ALIASES and ALIASES[key]:
        spec = CATALOG[ALIASES[key]]
        return {"tool_id": spec.id, "name": spec.name, "known": True}
    hit = match_catalog(name)
    if hit and key not in ALIASES:
        return {"tool_id": hit.id, "name": hit.name, "known": True}
    slug = re.sub(r"[^a-z0-9-]+", "-", key).strip("-")[:40] or "custom-tool"
    return {"tool_id": slug, "name": name.strip()[:80].title() if name.islower() else name.strip()[:80], "known": False}


def detect(sop_text: str, ai_hint: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """The tool the SOP requires, or None. ai_hint = the WATCH agent's sop_tool_requirement."""
    text = sop_text or ""
    if ai_hint and str(ai_hint.get("name") or "").strip():
        name = str(ai_hint["name"]).strip()
        quote = str(ai_hint.get("quote") or "")[:240]
        # the AI's claim must be backed by the SOP text (never a tool the model made up)
        if _canon(name).split(" ")[0] in _canon(text):
            out = resolve(name)
            out.update(version=str(ai_hint.get("version") or "").strip() or None, quote=quote or name, source="ai")
            return out
    m = _RX.search(text) or None
    if not m:
        return None
    raw, version = m.group(1), m.group(2)
    if not version:
        after = _VERSION_AFTER.search(text[m.end():m.end() + 40])
        version = after.group(1) if after else None
    start = max(text.rfind("\n", 0, m.start()) + 1, 0)
    end = text.find("\n", m.end())
    quote = text[start:end if end > 0 else len(text)].strip()[:240]
    out = resolve(raw)
    out.update(version=version, quote=quote, source="sop")
    return out


def status(req: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not req:
        return {}
    tid, version = req.get("tool_id"), req.get("version")
    if admin_policy.is_tool_allowed(tid):
        pol = admin_policy.tool_version_policy(tid)
        if version and pol["allowed"] and not any(a == version or a.startswith(version + ".") for a in pol["allowed"]):
            pending = admin_policy.request_status_for(tid, version)
            return {"status": "pending" if pending and pending["status"] in admin_policy.ACTIVE_REQUEST_STATES
                    else "version_not_allowed", "allowed_versions": pol["allowed"], "request": pending}
        return {"status": "active", "allowed_versions": pol["allowed"], "request": None}
    spec = FrameworkSpec.from_dict(CATALOG[tid].to_dict()) if tid in CATALOG else None
    blk = admin_policy.tool_block_for(spec, req.get("name") or "", version)
    if blk and (not blk.get("versions") or version):
        return {"status": "blocked", "blocked": {"name": blk["name"], "note": blk.get("note", "")}, "request": None}
    pending = admin_policy.request_status_for(tid)
    if pending and pending["status"] in admin_policy.ACTIVE_REQUEST_STATES:
        return {"status": "pending", "request": pending}
    return {"status": "unsupported", "request": pending}
