"""Automation Difficulty Assessment (9.8, reworked in 9.9, WATCH): Gemini watches the recording and decides which TEN
parameters decide how hard THIS workflow is to automate - the way an enterprise automation CoE assesses a use case -
scores each, weights them, and decides ONE thing: traditional automation (a generated script) or agentic automation
(an AI agent at run time).

How the parameters are chosen (the enterprise toolkit is a reference for the thinking, not an output)
-----------------------------------------------------------------------------------------------------
* **Categories** - System & Data Readiness 30 · Process Characteristics 25 · AI & Automation Risk Governance 20 ·
  Scalability & Cost-to-Serve 15 · Organizational & Delivery Readiness 10 (``CATEGORIES``).
* **No fixed parameter list.** ``LIBRARY`` is a *reference* of candidate parameters (the toolkit's
  parameters plus web-automation ones) with what Understudy measured for each (``candidates``: live-page inspection,
  steps, SOP). The ``AUTOMATION_DIFFICULTY`` agent watches the recording and picks the ten that matter for this
  process - from the candidates or its own, process-specific ones - with a category, a weight, a 0-10 difficulty
  value, why it matters HERE (citing the moment in the video), evidence, and whether it is a blocker. A candidate's
  value may move at most ``AI_MAX_SHIFT`` from what was measured on an inspected page. Offline, the ten most relevant
  candidates are chosen by the rules and weighted by their category.
* **Dynamic reweighting** - a parameter marked "doesn't apply" drops out and its weight is redistributed.
* **Score** (difficulty 0-10) - ``cbrt(sum(w * v^3) / sum(w))`` over the applicable parameters: a weighted cubic mean,
  so one hard parameter is not averaged away; each parameter's share is its contribution (they add up to the score).
  A blocker of 7+ lifts the score to at least 0.8 x its value.
* **The decision** - ``recommend`` (rules; the AI's answer when available) returns ``approach``: ``traditional`` or
  ``agentic``, nothing else. ``solution_fit`` checks that the tool the user picked is of the recommended kind.
* **Tuning** - the user can change any value or weight, or mark a parameter as not applicable, and recalculate
  (``recalculate``; ``AUTOMATION_ROUTING`` redoes the recommendation, rules offline); ``reset`` restores the assessment.

The assessment lives in ``session["difficulty"]``; its score and level also drive ``session["complexity"]`` (what the
build / heal agents read), so there is one score.
"""
from __future__ import annotations

import json
import math
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from core.wizard.llm import generate_json

AI_MAX_SHIFT = 3                      # how far the AI may move a measured value when the page was inspected
AI_MAX_SHIFT_UNPROBED = 6
CRITICAL_FLOOR = 0.8
MAX_WEIGHT = 30
TOP_N = 10
POWER = 3
LEVELS = [(2.5, "Simple"), (4.5, "Moderate"), (6.5, "Complex"), (10.0, "Very complex")]

CATEGORIES: List[Dict[str, Any]] = [
    {"name": "System & Data Readiness", "weight": 30,
     "covers": "stability of the UIs / pages / APIs, access barriers, data quality, integrations, data sensitivity"},
    {"name": "Process Characteristics", "weight": 25,
     "covers": "rule-based vs judgement, exceptions, interaction and process complexity, volume, variability"},
    {"name": "AI & Automation Risk Governance", "weight": 20,
     "covers": "explainability / audit, cost of a wrong output, model drift, regulation"},
    {"name": "Scalability & Cost-to-Serve", "weight": 15,
     "covers": "cost per run at volume, latency, infrastructure"},
    {"name": "Organizational & Delivery Readiness", "weight": 10,
     "covers": "skills, change management, sponsorship"},
]
CATEGORY_NAMES = [c["name"] for c in CATEGORIES]
_SYS, _PROC, _RISK, _SCALE, _ORG = CATEGORY_NAMES

# The reference library - candidates, never a fixed list. "core" ones are worth considering for any web process.
LIBRARY: List[Dict[str, Any]] = [
    {"key": "dom_stability", "name": "UI / DOM stability", "category": _SYS, "core": True,
     "measures": "Do element ids, names and positions stay the same between loads?"},
    {"key": "async_loading", "name": "Asynchronous loading", "category": _SYS, "core": False,
     "measures": "Content built after load, late elements, spinners."},
    {"key": "frames_shadow", "name": "iframes & shadow DOM", "category": _SYS, "core": False,
     "measures": "Fields inside (cross-site) iframes or shadow roots."},
    {"key": "captcha_bot", "name": "CAPTCHA & bot protection", "category": _SYS, "core": False, "blocker": True,
     "measures": "Checks built to stop automation."},
    {"key": "auth_2fa", "name": "Authentication & 2FA", "category": _SYS, "core": False, "blocker": True,
     "measures": "Sign-in, one-time codes, authenticator apps, SSO."},
    {"key": "integration", "name": "Integration complexity", "category": _SYS, "core": True,
     "measures": "How many pages, systems and sites the process crosses."},
    {"key": "data_quality", "name": "Data quality & structure", "category": _SYS, "core": False,
     "measures": "Structured fields vs free text, PDFs, e-mails, scans."},
    {"key": "data_sensitivity", "name": "Data security & sensitivity", "category": _SYS, "core": False,
     "measures": "Passwords, PII, financial or confidential data handled."},
    {"key": "process_structure", "name": "Process structure (rule-based vs judgement)", "category": _PROC, "core": True,
     "measures": "Can every decision be written as a rule, or does a person judge?"},
    {"key": "exception_volume", "name": "Exception volume / variability", "category": _PROC, "core": True,
     "measures": "How often the path changes: errors, retries, alternative branches."},
    {"key": "interaction", "name": "Interaction complexity", "category": _PROC, "core": True,
     "measures": "Uploads, dialogs, new tabs, drop-downs, drag & drop, date pickers, canvas."},
    {"key": "process_logic", "name": "Process length & branching", "category": _PROC, "core": True,
     "measures": "Number of steps and decisions that depend on the page."},
    {"key": "data_variability", "name": "Data variability", "category": _PROC, "core": True,
     "measures": "Number of input values; one record or many."},
    {"key": "volume_frequency", "name": "Volume / transaction frequency", "category": _PROC, "core": False,
     "measures": "How often the process runs and how many records."},
    {"key": "visual_validation", "name": "Visual validation need", "category": _PROC, "core": False,
     "measures": "Is success judged by how the page looks (charts, images, layout, colours)?"},
    {"key": "explainability", "name": "Explainability / audit traceability", "category": _RISK, "core": False,
     "measures": "Must every decision be traceable for an audit?"},
    {"key": "wrong_output_cost", "name": "Cost of a wrong output", "category": _RISK, "core": False,
     "measures": "What a wrong value or a wrong action costs (payments, legal, safety)."},
    {"key": "regulatory", "name": "Regulatory / compliance constraint", "category": _RISK, "core": False,
     "measures": "GDPR, HIPAA, PCI, SOX, KYC and similar rules."},
    {"key": "latency", "name": "Latency / real-time need", "category": _SCALE, "core": False,
     "measures": "Must the process finish within seconds?"},
    {"key": "cost_to_serve", "name": "Cost-to-serve at volume", "category": _SCALE, "core": False,
     "measures": "Per-run cost (e.g. AI calls) multiplied by the volume."},
    {"key": "change_management", "name": "Change management complexity", "category": _ORG, "core": False,
     "measures": "Does the automation change how people work?"},
]
LIB_BY_KEY = {p["key"]: p for p in LIBRARY}


_RX = {
    "captcha": re.compile(r"(?i)\b(re)?captcha\b|hcaptcha|i'?m not a robot|bot (check|protection|detection)|cloudflare challenge"),
    "otp": re.compile(r"(?i)\b(otp|2fa|mfa|two[- ]?(factor|step)|one[- ]?time (code|password|pin)|authenticator|"
                      r"verification code|sms code|security code)\b"),
    "sso": re.compile(r"(?i)\b(sso|single sign[- ]?on|sign in with (google|microsoft|okta|azure)|okta|saml)\b"),
    "login": re.compile(r"(?i)\b(log ?in|sign ?in|password)\b"),
    "async": re.compile(r"(?i)\b(loading|spinner|wait (until|for)|takes? (a few|several) seconds|lazy|infinite scroll|"
                        r"refresh(es)? (automatically|itself)|polling)\b"),
    "rich": re.compile(r"(?i)\b(drag|drop|hover|right[- ]click|double[- ]click|canvas|slider|date ?picker|calendar "
                       r"widget|rich text|wysiwyg|signature pad|map)\b"),
    "visual": re.compile(r"(?i)\b(screenshot|visual(ly)?|looks?|appearance|colou?r|chart|graph|image|logo|layout|pdf|"
                         r"canvas|icon|highlight(ed)?|banner|badge|position(ed)?|render(ed|s)?|pixel|font)\b"),
    "branch": re.compile(r"(?i)\b(if|otherwise|else|depending on|unless|in case|whichever|exception|retry|"
                         r"when .{1,30} appears)\b"),
    "data": re.compile(r"(?i)\b(\d+\s+(rounds?|rows?|records?|times|entries)|each (row|record|line|entry)|every "
                       r"(row|record)|excel|spreadsheet|csv|for all|bulk)\b"),
}


def _clamp(v: Any, lo: float = 0, hi: float = 10) -> int:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return int(lo)
    if math.isnan(x):
        return int(lo)
    return int(round(max(lo, min(hi, x))))


def _distinct(rx: re.Pattern, text: str) -> List[str]:
    seen: List[str] = []
    for m in rx.finditer(text or ""):
        w = m.group(0).lower()
        if w not in seen:
            seen.append(w)
    return seen


# ---------------------------------------------------------------------------
# 1. Evidence
# ---------------------------------------------------------------------------
def page_evidence(steps: List[Dict[str, Any]], params: List[Dict[str, Any]], sop: str,
                    profile: Optional[Dict[str, Any]], verification: Any = None) -> Dict[str, Dict[str, Any]]:
    """{key: {value, evidence: [..]}} from the inspection, the steps and the SOP - no AI."""
    sig = (profile or {}).get("signals") or {}
    probed = bool(profile) and (profile or {}).get("renderer") == "chromium"
    words = " ".join([sop or "", json.dumps(verification or "", default=str)] +
                     [str(st.get("description") or "") + " " + str(st.get("label") or "") for st in steps])
    actions = [str(st.get("action") or "") for st in steps]
    out: Dict[str, Dict[str, Any]] = {}

    def put(key: str, value: float, ev: List[str]) -> None:
        out[key] = {"value": _clamp(value), "evidence": [e for e in ev if e][:5] or ["nothing found"]}

    # DOM stability
    v, ev = (1, ["ids and field order were the same on every load"]) if probed else (3, ["the live page was not inspected"])
    if sig.get("dynamic_ids") or sig.get("dynamic_names"):
        v += 5
        ev.append("element ids / names change on every load")
    if sig.get("shuffled_fields"):
        v += 3
        ev.append("fields change position between loads")
    if sig.get("unlabelled_fields"):
        v += min(2, int(sig["unlabelled_fields"]))
        ev.append(f"{sig['unlabelled_fields']} field(s) without a visible label")
    if sig.get("missing_targets"):
        v += 1
        ev.append(f"{sig['missing_targets']} element(s) not on the page when it opened")
    if len(ev) > 1 and ev[0].startswith("ids and field order"):
        ev.pop(0)
    put("dom_stability", v, ev)

    # Asynchronous loading
    v, ev = 1, []
    if sig.get("framework"):
        v += 3
        ev.append(f"{sig['framework']} single-page app (built by JavaScript after load)")
    if sig.get("missing_targets"):
        v += 2
        ev.append("elements appear only after an earlier step")
    if sig.get("dialogs"):
        v += 1
        ev.append("a step pops up a dialog")
    hits = _distinct(_RX["async"], words)
    if hits:
        v += 2
        ev.append("the SOP / steps mention " + ", ".join(hits[:3]))
    if "wait" in actions:
        v += 1
        ev.append(f"{actions.count('wait')} explicit wait step(s) in the recording")
    put("async_loading", v, ev or ["the page is ready when it loads"])

    # iframes & shadow DOM
    v, ev = 0, []
    if sig.get("cross_origin_iframes"):
        v += 7
        ev.append(f"{sig['cross_origin_iframes']} iframe(s) from another site")
    elif sig.get("iframes"):
        v += 4
        ev.append(f"{sig['iframes']} iframe(s)")
    if sig.get("shadow_hosts"):
        v += 4
        ev.append("elements inside shadow DOM")
    put("frames_shadow", v, ev or ["no iframe or shadow DOM the steps use"])

    # CAPTCHA
    v, ev = 0, []
    if sig.get("captcha"):
        v, ev = 9, ["a CAPTCHA was found on the page"]
    hits = _distinct(_RX["captcha"], words)
    if hits:
        v = max(v, 8)
        ev.append("the SOP / steps mention " + ", ".join(hits[:2]))
    put("captcha_bot", v, ev or ["no CAPTCHA or bot check found"])

    # Authentication & 2FA
    v, ev = 0, []
    if sig.get("password_inputs") or sig.get("needs_login_first"):
        v = 4
        ev.append("the flow signs in" + (" (a page redirects to a sign-in first)" if sig.get("needs_login_first") else ""))
    elif any(p.get("type") == "secret" for p in params) or _RX["login"].search(words):
        v = 3
        ev.append("the process uses a password / sign-in")
    hits = _distinct(_RX["otp"], words)
    if hits:
        v = max(v, 9)
        ev.append("one-time code / 2FA: " + ", ".join(hits[:3]))
    hits = _distinct(_RX["sso"], words)
    if hits:
        v = min(10, max(v, 4) + 2)
        ev.append("single sign-on: " + ", ".join(hits[:2]))
    put("auth_2fa", v, ev or ["no sign-in"])

    # Interaction complexity
    v, ev = 1, []
    if sig.get("file_inputs"):
        v += 2
        ev.append("a file upload")
    if sig.get("dialogs"):
        v += 2
        ev.append("browser confirm / alert dialogs")
    if sig.get("new_window"):
        v += 2
        ev.append("a link opens a new tab")
    if "select" in actions:
        v += 1
        ev.append(f"{actions.count('select')} drop-down choice(s)")
    if "press" in actions:
        v += 1
        ev.append("keyboard keys")
    hits = _distinct(_RX["rich"], words)
    if hits:
        v += 3
        ev.append("rich interactions: " + ", ".join(hits[:3]))
    put("interaction", v, ev or ["plain typing and clicking"])

    # Navigation
    pages = int(sig.get("pages") or 1)
    v = 1 if pages <= 1 else 3 if pages <= 3 else 5
    ev = [f"{pages} page(s)"]
    if int(sig.get("hosts") or 1) > 1:
        v += 2
        ev.append(f"{sig['hosts']} different sites")
    if sig.get("new_window"):
        v += 1
        ev.append("continues in a new tab")
    if sig.get("needs_login_first"):
        v += 1
        ev.append("a sign-in redirect")
    put("navigation", v, ev)

    # Data variability
    n = len(params)
    v = 1 if n <= 3 else 3 if n <= 6 else 5
    ev = [f"{n} input value(s)"]
    hits = _distinct(_RX["data"], sop or "")
    if hits or sig.get("repeated_rows"):
        v += 3
        ev.append("repeats for many records" + (f" ({', '.join(hits[:2])})" if hits else ""))
    put("data_variability", v, ev)

    # Visual validation
    hits = _distinct(_RX["visual"], (sop or "") + " " + json.dumps(verification or "", default=str))
    v = 1 + 2 * min(4, len(hits))
    if isinstance(verification, dict) and verification.get("method") == "visual":
        v = max(v, 7)
        hits = hits + ["a visual check of the result"]
    put("visual_validation", v, ["success / checks mention " + ", ".join(hits[:4])] if hits
        else ["success can be checked from the page's text"])

    # Process length & branching
    n = len(steps)
    v = 1 if n <= 5 else 3 if n <= 10 else 4 if n <= 15 else 6
    ev = [f"{n} step(s)"]
    hits = _distinct(_RX["branch"], sop or "")
    if hits:
        v += min(4, 2 * len(hits))
        ev.append("decisions in the SOP: " + ", ".join(hits[:3]))
    put("process_logic", v, ev)
    return out


_RX2 = {
    "judgement": re.compile(r"(?i)\b(decide|judge|judg?ment|assess|review|evaluate|approve|reject|discretion|"
                            r"looks? (right|wrong|suspicious)|reasonable|appropriate|categori[sz]e|classify)\b"),
    "exception": re.compile(r"(?i)\b(exception|error|fails?|retry|if not|otherwise|missing|mismatch|escalate|"
                            r"manual(ly)? (check|fix)|edge case)\b"),
    "unstructured": re.compile(r"(?i)\b(pdf|e-?mail|attachment|scan(ned)?|handwrit|free[- ]text|ocr|document|"
                               r"invoice image|photo|unstructured)\b"),
    "sensitive": re.compile(r"(?i)\b(ssn|aadhaar|pan card|passport|account number|iban|card number|salary|patient|"
                            r"medical|confidential|personal data|pii|date of birth|dob)\b"),
    "money": re.compile(r"(?i)\b(payment|pay|invoice|amount|transfer|refund|approve|credit|debit|price|order|"
                        r"\$|€|₹|inr|usd|eur)\b"),
    "audit": re.compile(r"(?i)\b(audit|trace(able|ability)?|evidence|sign[- ]off|approval|four[- ]eyes|maker[- ]checker)\b"),
    "regulation": re.compile(r"(?i)\b(gdpr|hipaa|pci|sox|kyc|aml|rbi|sebi|fda|iso ?27001|compliance|regulat\w*)\b"),
    "latency": re.compile(r"(?i)\b(real[- ]time|immediately|within (\d+ )?seconds?|instant(ly)?|sla|urgent)\b"),
    "volume": re.compile(r"(?i)\b(daily|hourly|every (day|hour|minute|week)|\d{3,}\s+(invoices|records|orders|rows|"
                         r"times|tickets)|per (day|hour|month)|thousands?|hundreds?)\b"),
    "change": re.compile(r"(?i)\b(team|users? will|training|roll[- ]?out|adoption|replace (the )?manual|new process)\b"),
}


def _process_evidence(steps: List[Dict[str, Any]], params: List[Dict[str, Any]], sop: str,
                      profile: Optional[Dict[str, Any]], page: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """The enterprise candidates that can be read from the recording's steps and the SOP."""
    sig = (profile or {}).get("signals") or {}
    words = " ".join([sop or ""] + [str(st.get("description") or "") + " " + str(st.get("label") or "") for st in steps])
    out: Dict[str, Dict[str, Any]] = {}

    def put(key: str, value: float, ev: List[str], hit: bool) -> None:
        out[key] = {"value": _clamp(value), "evidence": [e for e in ev if e][:5] or ["nothing found"], "hit": hit}

    hosts, pages = int(sig.get("hosts") or 1), int(sig.get("pages") or 1)
    v = 1 + (2 if pages > 1 else 0) + (2 if pages > 3 else 0) + (3 if hosts > 1 else 0)
    put("integration", v, [f"{pages} page(s) on {hosts} site(s)"], pages > 1 or hosts > 1)
    hits = _distinct(_RX2["unstructured"], words)
    put("data_quality", 2 + 2 * min(3, len(hits)), ["unstructured inputs: " + ", ".join(hits[:3])] if hits
        else ["the values are typed into structured fields"], bool(hits))
    hits = _distinct(_RX2["sensitive"], words)
    secrets = [p["key"] for p in params if p.get("type") == "secret"]
    v = (3 if secrets else 0) + 2 * min(3, len(hits))
    put("data_sensitivity", v, ([f"{len(secrets)} secret value(s)"] if secrets else []) +
        (["sensitive data: " + ", ".join(hits[:3])] if hits else []), bool(hits or secrets))
    hits = _distinct(_RX2["judgement"], words)
    put("process_structure", 1 + 2 * min(4, len(hits)), ["judgement in the SOP / steps: " + ", ".join(hits[:3])] if hits
        else ["every step follows a fixed rule"], bool(hits))
    hits = _distinct(_RX2["exception"], words)
    put("exception_volume", 1 + 2 * min(4, len(hits)), ["exceptions / alternative paths: " + ", ".join(hits[:3])]
        if hits else ["one straight path"], bool(hits))
    hits = _distinct(_RX2["volume"], sop or "")
    put("volume_frequency", 2 + 2 * min(3, len(hits)), ["volume: " + ", ".join(hits[:3])] if hits
        else ["one run at a time"], bool(hits))
    money = _distinct(_RX2["money"], words)
    put("wrong_output_cost", 2 + 2 * min(3, len(money)), ["money / commitments involved: " + ", ".join(money[:3])]
        if money else ["no money or commitments involved"], bool(money))
    hits = _distinct(_RX2["audit"], sop or "")
    put("explainability", 2 + 2 * min(3, len(hits)), ["audit needs: " + ", ".join(hits[:3])] if hits
        else ["no audit requirement stated"], bool(hits))
    hits = _distinct(_RX2["regulation"], sop or "")
    put("regulatory", 2 + 3 * min(2, len(hits)), ["regulation: " + ", ".join(hits[:3])] if hits
        else ["no regulation named"], bool(hits))
    hits = _distinct(_RX2["latency"], sop or "")
    put("latency", 2 + 3 * min(2, len(hits)), ["time pressure: " + ", ".join(hits[:3])] if hits
        else ["no time limit stated"], bool(hits))
    vol = out["volume_frequency"]["value"]
    put("cost_to_serve", min(10, 1 + vol // 2 + (2 if len(steps) > 10 else 0)),
        [f"volume {vol}/10 x {len(steps)} step(s) per run"], out["volume_frequency"]["hit"])
    hits = _distinct(_RX2["change"], sop or "")
    put("change_management", 1 + 2 * min(3, len(hits)), ["people affected: " + ", ".join(hits[:3])] if hits
        else ["nothing about who is affected"], bool(hits))
    return out


def candidates(steps: List[Dict[str, Any]], params: List[Dict[str, Any]], sop: str,
               profile: Optional[Dict[str, Any]], verification: Any = None) -> Dict[str, Dict[str, Any]]:
    """Every library parameter with what Understudy measured: {key: {value, evidence, relevance}}."""
    page = page_evidence(steps, params, sop, profile, verification)
    proc = _process_evidence(steps, params, sop, profile, page)
    out: Dict[str, Dict[str, Any]] = {}
    for lib in LIBRARY:
        k = lib["key"]
        e = dict(page.get(k) or proc.get(k) or {"value": 0, "evidence": ["not measured"]})
        hit = e.pop("hit", None)
        if hit is None:                                       # page evidence: anything beyond the baseline is a hit
            hit = not any(x.startswith(("nothing", "no ", "plain", "ids and field", "the page is ready",
                                        "success can be", "1 page", "the live page was not")) for x in e["evidence"])
        rel = e["value"] / 10.0 + (0.25 if hit else 0.0) + (0.15 if lib.get("core") else 0.0)
        out[k] = dict(e, relevance=round(rel, 3))
    return out


def _normalize_weights(params: List[Dict[str, Any]], field: str = "weight") -> None:
    """Integer weights that add up to 100 (largest remainder); a positive weight never rounds to 0."""
    raw = [max(0.0, float(p[field])) for p in params]
    tot = sum(raw)
    if tot <= 0:
        return
    exact = [r * 100.0 / tot for r in raw]
    ints = [int(math.floor(x)) for x in exact]
    for i in sorted(range(len(raw)), key=lambda i: -(exact[i] - ints[i]))[:100 - sum(ints)]:
        ints[i] += 1
    for i, prm in enumerate(params):
        prm[field] = max(1, ints[i]) if raw[i] > 0 else 0


def _rule_choice(cands: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Offline: the ten most relevant candidates, weighted by their category (dynamic reweighting)."""
    order = {lib["key"]: i for i, lib in enumerate(LIBRARY)}
    top = sorted(cands, key=lambda k: (-cands[k]["relevance"], order[k]))[:TOP_N]
    per_cat: Dict[str, int] = {}
    for k in top:
        per_cat[LIB_BY_KEY[k]["category"]] = per_cat.get(LIB_BY_KEY[k]["category"], 0) + 1
    cat_w = {c["name"]: c["weight"] for c in CATEGORIES}
    out = []
    for k in top:
        lib = LIB_BY_KEY[k]
        out.append(_param(k, lib["name"], lib["category"], lib["measures"], cands[k]["value"],
                          cat_w[lib["category"]] / per_cat[lib["category"]], cands[k]["evidence"],
                          why="", source="rules", library_key=k, measured=cands[k]["value"],
                          blocker=bool(lib.get("blocker"))))
    _normalize_weights(out)
    return out


def _param(key: str, name: str, category: str, measures: str, value: int, weight: float, evidence: List[str], why: str,
           source: str, library_key: Optional[str] = None, measured: Optional[int] = None, blocker: bool = False,
           video_time: str = "", ai_value: Optional[int] = None) -> Dict[str, Any]:
    return {"key": key, "name": name, "category": category, "measures": measures, "why": why, "evidence": evidence,
            "video_time": video_time, "source": source, "library_key": library_key, "measured_value": measured,
            "ai_value": ai_value, "value": value, "weight": weight, "default_weight": weight, "blocker": blocker,
            "applicable": True, "user_value": None, "user_weight": None, "user_applicable": None}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")[:40]


_TIME_RX = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")


def _ai_params(items: Any, cands: Dict[str, Dict[str, Any]], probed: bool) -> List[Dict[str, Any]]:
    """The AI's ten parameters, validated: known category, numeric value and weight, unique, evidence-bounded."""
    shift = AI_MAX_SHIFT if probed else AI_MAX_SHIFT_UNPROBED
    out: List[Dict[str, Any]] = []
    seen = set()
    for x in items if isinstance(items, list) else []:
        if not isinstance(x, dict):
            continue
        v, w = x.get("value"), x.get("weight")
        if not all(isinstance(n, (int, float)) and not isinstance(n, bool) and not math.isnan(float(n)) for n in (v, w)):
            continue
        name = re.sub(r"\s+", " ", str(x.get("name") or "")).strip()[:80]
        key = _slug(str(x.get("key") or name))
        if not name or not key or key in seen or float(w) <= 0:
            continue
        lib = LIB_BY_KEY.get(key)
        category = str(x.get("category") or "")
        if category not in CATEGORY_NAMES:
            category = lib["category"] if lib else _PROC
        ai = _clamp(v)
        measured = cands[key]["value"] if lib else None
        value = max(measured - shift, min(measured + shift, ai)) if lib else ai
        ev = [str(e)[:200] for e in (x.get("evidence") or []) if str(e).strip()][:4]
        if lib:
            ev = ev + [e for e in cands[key]["evidence"] if e not in ev][:2]
        vt = str(x.get("video_time") or "").strip()
        out.append(_param(key, name, category, str(x.get("measures") or (lib or {}).get("measures") or "")[:200],
                          value, min(MAX_WEIGHT, float(w)), ev or ["(the agent gave no evidence)"],
                          why=str(x.get("why_here") or x.get("why") or "")[:300], source="ai",
                          library_key=key if lib else None, measured=measured,
                          blocker=bool(x.get("blocker")) or bool((lib or {}).get("blocker")),
                          video_time=vt if _TIME_RX.match(vt) else "", ai_value=ai))
        seen.add(key)
        if len(out) == TOP_N:
            break
    return out


# ---------------------------------------------------------------------------
# Score
# ---------------------------------------------------------------------------
def level_for(score: float) -> str:
    return next(name for top, name in LEVELS if score <= top)


def compute(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """rows: [{key, value, weight, applicable?, blocker?, name?}] -> {score, level, rows, floor}.
    Parameters that don't apply drop out and their weight is redistributed (dynamic reweighting)."""
    live = [r for r in rows if r.get("applicable", True) and r["weight"] > 0]
    total_w = sum(r["weight"] for r in live)
    if total_w <= 0:
        raise ValueError("At least one applicable parameter needs a weight above 0.")
    cube = {r["key"]: r["weight"] * float(r["value"]) ** POWER for r in live}
    total = sum(cube.values())
    score = (total / total_w) ** (1.0 / POWER)
    floor = None
    for r in live:
        if r.get("blocker") and r["value"] >= 7:
            f = CRITICAL_FLOOR * r["value"]
            if f > score:
                score, floor = f, {"key": r["key"], "name": r.get("name") or r["key"], "value": r["value"],
                                   "why": f"{r.get('name') or r['key']} is {r['value']}/10 - a blocker for any "
                                          f"automation, so the score is at least {f:.1f}."}
    score = round(min(10.0, score), 1)
    out_rows = []
    for r in rows:
        on = r in live
        share = cube.get(r["key"], 0.0) / total if total and on else 0.0
        out_rows.append(dict(r, weight_pct=round(100.0 * r["weight"] / total_w, 1) if on else 0.0,
                             share_pct=round(100.0 * share, 1), contribution=round(score * share, 2)))
    return {"score": score, "level": level_for(score), "rows": out_rows, "floor": floor,
            "formula": "score = cube root of ( sum(weight x value^3) / sum(weight) ) over the parameters that apply - "
                       "a parameter marked 'doesn't apply' gives its weight to the others; strong challenges count "
                       "more than mild ones; a blocker of 7+ lifts the score to at least 0.8 x its value"}


# ---------------------------------------------------------------------------
# Traditional or agentic (rules - the offline answer and the cross-check)
# ---------------------------------------------------------------------------
APPROACHES = ["traditional", "agentic"]
APPROACH_LABEL = {"traditional": "Traditional automation", "agentic": "Agentic automation"}
APPROACH_MEANING = {
    "traditional": "a generated script with fixed locators - deterministic, readable, free per run",
    "agentic": "an AI agent reads the live page at run time and decides each action - adapts, costs a Gemini call per step",
}
TRADITIONAL_STRENGTHS = [
    "Generated code you can read, review and keep in your repository",
    "Deterministic: the same input gives the same run - easy to debug in CI",
    "Fast and free per run - no AI call, no API key needed at run time",
]
AGENTIC_STRENGTHS = [
    "An AI agent reads the page at run time, so changed layouts and moved fields don't break it",
    "Understands the page visually - can judge charts, images and layout",
    "Handles surprises (an extra dialog, a different path) by reasoning instead of failing",
]
AGENTIC_COSTS = ["Each run calls Gemini (your organisation's, some latency and cost per step)",
                 "Less deterministic - review the run log (every decision the agent made)"]


def recommend(values: Dict[str, int], score: float) -> Dict[str, Any]:
    v = values
    agentic: List[Tuple[int, str]] = []
    traditional: List[Tuple[int, str]] = []
    if v.get("visual_validation", 0) >= 7:
        agentic.append((3, f"Visual validation need is {v['visual_validation']}/10 - success has to be judged by how "
                           "the page looks, which an AI agent with vision can do and a selector script can't."))
    if v.get("process_structure", 0) >= 7:
        agentic.append((3, f"Process structure is {v['process_structure']}/10 - decisions need judgement, not rules."))
    if v.get("dom_stability", 0) >= 8 and v.get("async_loading", 0) >= 6:
        agentic.append((2, "The DOM is unpredictable and loads late - an agent re-reads the page every step instead of "
                           "relying on locators."))
    elif v.get("dom_stability", 0) >= 8:
        traditional.append((1, f"UI / DOM stability is {v['dom_stability']}/10, but changing ids are handled by "
                               "label-based locators in generated code."))
    if v.get("interaction", 0) >= 7:
        agentic.append((1, f"Interaction complexity is {v['interaction']}/10 (rich widgets) - an agent adapts to them."))
    if v.get("exception_volume", 0) >= 7 or v.get("process_logic", 0) >= 7:
        agentic.append((1, "Many exceptions / branches that depend on what the page shows suit an agent that reasons."))
    if score >= 6.5:
        agentic.append((2, f"Overall difficulty {score}/10 - a fixed script is likely to need constant repairs."))
    if score <= 3.5:
        traditional.append((3, f"Overall difficulty {score}/10 - a generated script is reliable, fast and free to run."))
    elif score <= 5:
        traditional.append((1, f"Overall difficulty {score}/10 - manageable with generated code and self-healing."))
    if v.get("data_variability", 0) >= 6 or v.get("volume_frequency", 0) >= 7:
        traditional.append((2, "High volume / many records run fastest and cheapest as a deterministic script "
                               "(the cost per run of an agent grows with volume)."))
    if v.get("latency", 0) >= 7:
        traditional.append((2, f"Latency need is {v['latency']}/10 - multi-step agent calls are slower than a script."))
    if v.get("dom_stability", 0) <= 4 and v.get("visual_validation", 0) <= 4 and v.get("process_structure", 0) <= 4:
        traditional.append((1, "Stable pages, rule-based decisions and text-checked results - classic selectors work."))
    a, t = sum(p for p, _ in agentic), sum(p for p, _ in traditional)
    approach = "agentic" if a >= 2 and a > t else "traditional"
    notes = []
    if v.get("captcha_bot", 0) >= 7:
        notes.append("CAPTCHA / bot protection: neither approach should bypass it - automate a test environment where "
                     "it is switched off.")
    if v.get("auth_2fa", 0) >= 7:
        notes.append("2FA: use a test account without one-time codes, or a test OTP secret your admin provides.")
    if approach == "agentic" and max(v.get("wrong_output_cost", 0), v.get("regulatory", 0),
                                     v.get("explainability", 0)) >= 7:
        notes.append("High stakes: review the agent's run log (every decision it made) before relying on the result.")
    margin = abs(a - t)
    return {"approach": approach, "label": APPROACH_LABEL[approach], "meaning": APPROACH_MEANING[approach],
            "confidence": "high" if margin >= 3 else "medium" if margin >= 1 else "low",
            "why": [w for _, w in sorted(agentic if approach == "agentic" else traditional, key=lambda x: -x[0])]
            or (["No parameter calls for an agent - a generated script with Understudy's self-healing is the simpler, "
                 "cheaper choice."]
                if approach == "traditional" else []),
            "against": [w for _, w in sorted(traditional if approach == "agentic" else agentic, key=lambda x: -x[0])],
            "notes": notes, "points": {"agentic": a, "traditional": t}}


def solution_fit(d: Dict[str, Any], chosen: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Does the kind of the chosen tool (traditional / agentic) match the recommendation?"""
    if not d or not chosen:
        return {"status": "not chosen", "message": "Pick a tool - Understudy then checks it matches the recommendation."}
    kind = "agentic" if (chosen.get("category") or "").lower() == "agentic" else "traditional"
    rec = d.get("recommendation") or {}
    want, score, name = rec.get("approach"), d.get("score", 0), chosen.get("name") or chosen.get("id")
    if want in APPROACHES and kind != want:
        return {"status": "mismatch", "kind": kind, "recommended": want,
                "message": f"{name} is {APPROACH_LABEL[kind].lower()}, but {APPROACH_LABEL[want].lower()} was "
                           f"recommended for this workflow (difficulty {score}/10)."
                           + (" A generated script would be cheaper, faster and fully traceable." if want == "traditional"
                              else " Expect frequent repairs, or pick an agentic tool.")}
    return {"status": "fit", "kind": kind, "recommended": want,
            "message": f"{name} is {APPROACH_LABEL[kind].lower()}, as recommended ({score}/10)."}


def _tool_kind(t: Dict[str, Any]) -> str:
    return "agentic" if (t.get("category") or "").lower() == "agentic" else "traditional"


def _fit(tool: Dict[str, Any], values: Dict[str, int], sig: Dict[str, Any]) -> Dict[str, str]:
    name = f"{tool.get('id', '')} {tool.get('name', '')}".lower()
    cat = (tool.get("category") or "").lower()
    if cat == "agentic":
        a = tool.get("agent") or {}
        if tool.get("id") == "gemini-agent":
            return {"fit": "good", "why": "built into Understudy - Gemini writes an agentic script for this process "
                                          "and decides each action on the live page at run time"}
        if tool.get("template") == "gemini-agent":
            return {"fit": "good", "why": f"Understudy's agent on {a.get('driver', 'playwright')} / "
                                          f"{a.get('browser', 'chromium')} (set up by your admin) - same agentic script "
                                          "and guard rails"}
        return {"fit": "ok", "why": "AI agent that drives the browser"}
    if cat == "api" or any(k in name for k in ("requests", "rest assured", "rest-assured", "newman", "k6")):
        return {"fit": "poor", "why": "an API tool - it does not drive the page"}
    if "cypress" in name and sig.get("cross_origin_iframes"):
        return {"fit": "ok", "why": "Cypress can't reach another site's iframe"}
    if any(k in name for k in ("playwright", "webdriverio", "cypress", "puppeteer")):
        return {"fit": "good", "why": "auto-waits for elements - handles pages that render late"}
    if any(k in name for k in ("selenium", "robot")):
        return {"fit": "good" if values.get("async_loading", 0) < 6 else "ok",
                "why": "label-based XPath works well; dynamic pages need explicit waits"}
    return {"fit": "ok", "why": "can automate the page with label-based locators"}


def tool_lists(options: List[Dict[str, Any]], values: Dict[str, int], sig: Dict[str, Any],
               ai_fit: Optional[Dict[str, Dict[str, str]]] = None) -> Dict[str, List[Dict[str, Any]]]:
    """The tools to choose from, grouped: what the AI / admin offer for this session (traditional), and the agentic
    tools on Understudy's agent - those the admin set up, else the built-in one (9.9). No other agentic tool."""
    from core.wizard.admin_policy import BUILTIN_TOOLS
    from core.wizard.agent_stacks import is_understudy_agent
    from core.wizard.frameworks import CATALOG
    # 9.9: the built-in agent only when no agentic tool the admin set up on the agent is offered
    own = any(is_understudy_agent(t) and t.get("id") not in BUILTIN_TOOLS for t in options)
    builtin = [] if own else [CATALOG[b].to_dict() for b in BUILTIN_TOOLS if b in CATALOG]
    seen, tools = set(BUILTIN_TOOLS) if own else set(), []
    for t in list(options) + builtin:
        tid = t.get("id")
        if not tid or tid in seen or (_tool_kind(t) == "agentic" and not is_understudy_agent(t)):
            continue
        seen.add(tid)
        f = dict(_fit(t, values, sig))
        if ai_fit and tid in ai_fit:
            f.update(ai_fit[tid])
        tools.append({"tool_id": tid, "name": t.get("name") or tid, "kind": _tool_kind(t),
                      "language": t.get("language") or "", "fit": f["fit"], "why": f["why"]})
    order = {"good": 0, "ok": 1, "poor": 2}
    tools.sort(key=lambda x: order.get(x["fit"], 3))
    return {"traditional": [t for t in tools if t["kind"] == "traditional"][:8],
            "agentic": [t for t in tools if t["kind"] == "agentic"][:6]}


# ---------------------------------------------------------------------------
# Assessment (Gemini watches the recording) and recalculation (the user)
# ---------------------------------------------------------------------------
def _eff(p: Dict[str, Any], field: str) -> Any:
    u = p.get("user_" + field)
    return u if u is not None else p[field]


def _rows_from(params: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [{"key": p["key"], "name": p["name"], "value": _eff(p, "value"), "weight": _eff(p, "weight"),
             "applicable": _eff(p, "applicable"), "blocker": p.get("blocker", False)} for p in params]


def _merge_rows(params: List[Dict[str, Any]], computed: Dict[str, Any]) -> None:
    by = {r["key"]: r for r in computed["rows"]}
    for p in params:
        r = by[p["key"]]
        p.update(effective_value=r["value"], effective_weight=r["weight"], effective_applicable=r["applicable"],
                 weight_pct=r["weight_pct"], share_pct=r["share_pct"], contribution=r["contribution"])


def _value_map(d: Dict[str, Any]) -> Dict[str, int]:
    """What the routing rules read: every measured candidate, overridden by the chosen parameters (as tuned)."""
    vals = {k: c["value"] for k, c in (d.get("candidates") or {}).items()}
    for p in d["parameters"]:
        k = p.get("library_key")
        if k:
            vals[k] = p["effective_value"] if p.get("effective_applicable", True) else 0
    return vals


def _media(s: Dict[str, Any]) -> Optional[str]:
    m = (s.get("media") or {}).get("path")
    return m if m and (m.startswith("gs://") or os.path.exists(m)) else None


def _ai_inputs(s: Dict[str, Any], profile: Optional[Dict[str, Any]]) -> Dict[str, str]:
    analysis = s.get("analysis") or {}
    steps = analysis.get("steps") or []
    sig = (profile or {}).get("signals") or {}
    return {
        "process": json.dumps({"name": analysis.get("process_name") or "", "summary": analysis.get("summary") or "",
                               "steps": [{"order": st.get("order"), "action": st.get("action"),
                                          "label": st.get("label") or "", "what": st.get("description") or "",
                                          "video_time": st.get("video_time") or st.get("timestamp") or ""}
                                         for st in steps[:30]],
                               "values": len(s.get("parameters") or []),
                               "verification": analysis.get("verification")}, indent=1, default=str)[:7000],
        "page": json.dumps({"inspected": bool(profile) and profile.get("renderer") == "chromium",
                            "signals": {k: v for k, v in sig.items() if v},
                            "checks": [{k: c.get(k) for k in ("page", "title", "finding", "relevant")}
                                       for c in (profile or {}).get("checks") or []][:25]}, default=str)[:6000],
        "sop": (s.get("sop_text") or "")[:2000],
    }


def assess(s: Dict[str, Any], profile: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    analysis = s.get("analysis") or {}
    steps = analysis.get("steps") or []
    probed = bool(profile) and (profile or {}).get("renderer") == "chromium"
    cands = candidates(steps, s.get("parameters") or [], s.get("sop_text") or "", profile, analysis.get("verification"))
    sig = (profile or {}).get("signals") or {}
    options = [t for t in (s.get("framework_options") or []) if t.get("id")]
    lists0 = tool_lists(options, {k: c["value"] for k, c in cands.items()}, sig)
    res = generate_json("AUTOMATION_DIFFICULTY", dict(_ai_inputs(s, profile), **{
        "categories": json.dumps(CATEGORIES, indent=1),
        "candidates": json.dumps([{"key": lib["key"], "name": lib["name"], "category": lib["category"],
                                   "measures": lib["measures"], "measured_value": cands[lib["key"]]["value"],
                                   "evidence": cands[lib["key"]]["evidence"],
                                   "relevance": cands[lib["key"]]["relevance"]} for lib in LIBRARY], indent=1),
        "tools": json.dumps([{"tool_id": t["tool_id"], "name": t["name"], "kind": t["kind"]}
                             for t in lists0["traditional"] + lists0["agentic"]])}),
        untrusted=["process", "sop", "page"], media_path=_media(s))
    params: List[Dict[str, Any]] = []
    ai_rec = ai_fit = None
    out: Dict[str, Any] = {"probed": probed, "agent_source": "LOCAL_HEURISTIC", "model": "Assessment rules",
                           "summary": "", "adjusted": False, "history": [], "candidates": cands,
                           "chosen_by": "rules"}
    if res.ok and isinstance(res.data, dict):
        params = _ai_params(res.data.get("parameters"), cands, probed)
        if params:
            out.update(agent_source=res.agent_source, model=res.model, chosen_by="ai",
                       summary=str(res.data.get("summary") or "")[:700])
            ai_rec = res.data.get("recommendation") if isinstance(res.data.get("recommendation"), dict) else None
            ai_fit = {str(t.get("tool_id")): {"fit": t["fit"], "why": str(t.get("why") or "")[:240]}
                      for t in res.data.get("tool_fit") or []
                      if isinstance(t, dict) and t.get("fit") in ("good", "ok", "poor")}
    if len(params) < TOP_N:                    # the AI gave fewer than ten usable ones: the rules complete the set
        have = {p["key"] for p in params}
        extra = [p for p in _rule_choice(cands) if p["key"] not in have][:TOP_N - len(params)]
        if params:
            mean_w = sum(p["weight"] for p in params) / len(params)
            for p in extra:
                p["weight"] = p["default_weight"] = mean_w / 2
        params += extra
    _normalize_weights(params)
    for p in params:
        p["default_weight"] = p["weight"]
    out["parameters"] = params
    _finish(out, s, profile, ai_rec, ai_fit, res.agent_source if res.ok and ai_rec else "LOCAL_HEURISTIC",
            res.model if res.ok and ai_rec else "Routing rules")
    out["assessed_score"] = out["score"]
    if not out["summary"]:
        out["summary"] = _summary(out)
    return out


def _finish(out: Dict[str, Any], s: Dict[str, Any], profile: Optional[Dict[str, Any]], ai_rec: Optional[Dict[str, Any]],
            ai_fit: Optional[Dict[str, Dict[str, str]]], source: str, model: str) -> None:
    computed = compute(_rows_from(out["parameters"]))
    _merge_rows(out["parameters"], computed)
    out.update({k: computed[k] for k in ("score", "level", "floor", "formula")})
    cats: Dict[str, float] = {}
    for p in out["parameters"]:
        cats[p["category"]] = round(cats.get(p["category"], 0.0) + p["weight_pct"], 1)
    out["categories"] = [{"name": c, "weight_pct": cats[c]} for c in CATEGORY_NAMES if c in cats]
    values = _value_map(out)
    rules = recommend(values, computed["score"])
    sig = (profile or {}).get("signals") or {}
    lists = tool_lists([t for t in (s.get("framework_options") or []) if t.get("id")], values, sig, ai_fit)
    rec = dict(rules, source="LOCAL_HEURISTIC", model="Routing rules", rules_approach=rules["approach"])
    if ai_rec and ai_rec.get("approach") in APPROACHES:
        a = ai_rec["approach"]
        rec.update(approach=a, label=APPROACH_LABEL[a], meaning=APPROACH_MEANING[a],
                   source=source, model=model,
                   confidence=ai_rec.get("confidence") if ai_rec.get("confidence") in ("high", "medium", "low")
                   else rules["confidence"],
                   why=[str(x)[:300] for x in (ai_rec.get("why") or [])][:5] or rules["why"],
                   against=[str(x)[:300] for x in (ai_rec.get("against") or [])][:4] or rules["against"])
        if ai_rec.get("summary"):
            rec["summary"] = str(ai_rec["summary"])[:500]
    rec.setdefault("summary", _rec_summary(rec, computed["score"]))
    rec["traditional"] = {"strengths": TRADITIONAL_STRENGTHS, "tools": lists["traditional"]}
    rec["agentic"] = {"strengths": AGENTIC_STRENGTHS, "costs": AGENTIC_COSTS, "tools": lists["agentic"]}
    out["recommendation"] = rec
    out["solution_fit"] = solution_fit(out, s.get("framework"))


def _rec_summary(rec: Dict[str, Any], score: float) -> str:
    if rec["approach"] == "agentic":
        return (f"Agentic automation: at {score}/10 this workflow needs an AI that reads the page while "
                "it runs. " + (rec["why"][0] if rec.get("why") else ""))
    return (f"Traditional automation: at {score}/10 a generated script handles this workflow reliably, "
            "fast and without an AI call per run.")


def _summary(out: Dict[str, Any]) -> str:
    top = sorted(out["parameters"], key=lambda p: -p.get("contribution", 0))[:3]
    drivers = ", ".join(f"{p['name'].lower()} ({p['effective_value']}/10)" for p in top if p.get("effective_value", 0) >= 4)
    return (f"{out['level']} - {out['score']}/10."
            + (f" Driven mostly by {drivers}." if drivers else " Nothing in this workflow is hard to automate."))


def recalculate(s: Dict[str, Any], changes: List[Dict[str, Any]], by: str = "user") -> Tuple[Dict[str, Any], List[str]]:
    """Applies the user's values / weights / applicability and recomputes the score and the recommendation. Returns
    (assessment, human-readable changes). Raises ValueError on bad input."""
    d = s.get("difficulty")
    if not d:
        raise ValueError("The difficulty has not been assessed yet - finish Step 1 first.")
    by_key = {p["key"]: p for p in d["parameters"]}
    seen = set()
    staged: Dict[str, Dict[str, Any]] = {}
    for c in changes:
        k = str(c.get("key") or "")
        if k not in by_key:
            raise ValueError(f"Unknown parameter '{k}'.")
        if k in seen:
            raise ValueError(f"Parameter '{k}' is listed twice.")
        seen.add(k)
        st: Dict[str, Any] = {}
        for field, hi in (("value", 10), ("weight", 100)):
            if c.get(field) is None:
                continue
            try:
                x = float(c[field])
            except (TypeError, ValueError):
                raise ValueError(f"{by_key[k]['name']}: {field} must be a number.")
            if math.isnan(x) or x < 0 or x > hi:
                raise ValueError(f"{by_key[k]['name']}: {field} must be between 0 and {hi}.")
            st[field] = int(round(x))
        if c.get("applicable") is not None:
            st["applicable"] = bool(c["applicable"])
        staged[k] = st
    trial = [dict(p) for p in d["parameters"]]
    for p in trial:
        for field, val in (staged.get(p["key"]) or {}).items():
            p["user_" + field] = None if val == p[field] else val
    compute(_rows_from(trial))                      # raises when nothing applicable with a weight is left
    described = []
    for old, new in zip(d["parameters"], trial):
        for field, label in (("value", "value"), ("weight", "weight")):
            o, n = _eff(old, field), _eff(new, field)
            if o != n:
                described.append(f"{new['name']} {label} {o} -> {n}")
        if _eff(old, "applicable") != _eff(new, "applicable"):
            described.append(f"{new['name']} " + ("applies again" if _eff(new, "applicable") else "marked as not applicable"))
    before = d.get("score")
    d["parameters"] = trial
    d["adjusted"] = any(p["user_value"] is not None or p["user_weight"] is not None or p["user_applicable"] is not None
                        for p in trial)
    _reroute(s, d)
    d.setdefault("history", []).append({"by": by, "changes": described, "score_before": before, "score_after": d["score"]})
    d["history"] = d["history"][-20:]
    return d, described


def reset(s: Dict[str, Any], by: str = "user") -> Dict[str, Any]:
    d = s.get("difficulty")
    if not d:
        raise ValueError("The difficulty has not been assessed yet - finish Step 1 first.")
    before = d.get("score")
    for p in d["parameters"]:
        p["user_value"] = p["user_weight"] = p["user_applicable"] = None
    d["adjusted"] = False
    _reroute(s, d)
    d.setdefault("history", []).append({"by": by, "changes": ["reset to the assessed values and weights"],
                                        "score_before": before, "score_after": d["score"]})
    d["history"] = d["history"][-20:]
    return d


def _reroute(s: Dict[str, Any], d: Dict[str, Any]) -> None:
    """Recomputes the score and redoes the recommendation (AI when available, rules offline)."""
    profile = s.get("page_profile")
    computed = compute(_rows_from(d["parameters"]))
    _merge_rows(d["parameters"], computed)
    res = generate_json("AUTOMATION_ROUTING", dict(_ai_inputs(s, profile), **{
        "assessment": json.dumps({"score": computed["score"], "level": computed["level"],
                                  "parameters": [
            {"name": p["name"], "category": p["category"], "value": p["effective_value"],
             "weight": p["effective_weight"], "applies": p["effective_applicable"],
             "changed_by_user": any(p.get("user_" + f) is not None for f in ("value", "weight", "applicable")),
             "assessed_value": p["value"]} for p in d["parameters"]]}, indent=1),
        "tools": json.dumps([{"tool_id": t["tool_id"], "name": t["name"], "kind": t["kind"]}
                             for k in ("traditional", "agentic")
                             for t in ((d.get("recommendation") or {}).get(k) or {}).get("tools") or []])}),
        untrusted=["process", "sop", "page"])
    ai_rec = res.data if res.ok and isinstance(res.data, dict) else None
    ai_fit = None
    if ai_rec:
        ai_fit = {str(t.get("tool_id")): {"fit": t["fit"], "why": str(t.get("why") or "")[:240]}
                  for t in ai_rec.get("tool_fit") or [] if isinstance(t, dict) and t.get("fit") in ("good", "ok", "poor")}
    _finish(d, s, profile, ai_rec, ai_fit, res.agent_source if res.ok else "LOCAL_HEURISTIC",
            res.model if res.ok else "Routing rules")
    d["summary"] = _summary(d) + (" (tuned by you)" if d["adjusted"] else "")


def refresh_fit(s: Dict[str, Any]) -> None:
    """After the user picks a tool: does its kind match the recommendation?"""
    if s.get("difficulty"):
        s["difficulty"]["solution_fit"] = solution_fit(s["difficulty"], s.get("framework"))


def apply_to_complexity(s: Dict[str, Any]) -> None:
    """One score: the build / heal agents read ``complexity``; its score and level follow the difficulty."""
    d, c = s.get("difficulty"), s.get("complexity")
    if not d or not c:
        return
    c["score"] = max(1, int(round(d["score"])))
    c["level"] = d["level"]
    c["difficulty_score"] = d["score"]
    c["difficulty"] = [{"name": p["name"], "value": p["effective_value"], "weight_pct": p["weight_pct"],
                        "tuned": any(p.get("user_" + f) is not None for f in ("value", "weight", "applicable"))}
                       for p in d["parameters"] if p.get("effective_applicable", True)]
