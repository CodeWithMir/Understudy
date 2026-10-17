"""Automation complexity (9.0, 9.4): how hard is this automation - shown BEFORE the user picks a tool, and handed to
the build / heal agent that writes the code (``for_prompt``).

9.4: the evidence is the automation-aware INSPECTION (``core/wizard/inspection.py``) - only what the steps need was
checked, and only findings that matter for this automation count (an iframe no step touches adds nothing). A process
that visits several pages, several sites, a page that needs a sign-in first, a link that opens a new tab or a button
that pops up a dialog are scored too.

A junior developer can't tell whether a recording of "fill a form" is a 10-minute job or a fight with a page whose
fields move on every load. After WATCH, Understudy scores the automation from evidence:

* the live page (``page_probe``): ids / names that change between loads, fields that move, iframes (cross-origin ones
  especially), shadow DOM, a single-page-app framework, CAPTCHA, logins, file uploads, unlabelled fields;
* the process: number of steps and values, repeated rounds / data rows in the SOP.

The deterministic score (1-10, Simple / Moderate / Complex / Very complex) never depends on the AI. The
``AUTOMATION_COMPLEXITY`` agent explains it in plain words, says how Understudy handles each challenge and how well
each tool the user may pick fits - its level can move the score by at most one band.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from core.wizard.llm import generate_json

LEVELS = [(3, "Simple"), (5, "Moderate"), (7, "Complex"), (10, "Very complex")]
_DATA_DRIVEN = re.compile(r"(?i)\b(\d+\s+(rounds?|rows?|records?|times)|each (row|record|line)|every (row|record)|"
                          r"excel|spreadsheet|csv|for all)\b")


def level_for(score: int) -> str:
    return next(name for top, name in LEVELS if score <= top)


def _factor(name: str, points: int, severity: str, detail: str, handled: str) -> Dict[str, Any]:
    return {"name": name, "points": points, "severity": severity, "detail": detail, "how_handled": handled}


def baseline(steps: List[Dict[str, Any]], params: List[Dict[str, Any]], sop: str,
             profile: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    sig = (profile or {}).get("signals") or {}
    probed = bool(profile) and profile.get("renderer") == "chromium"
    f: List[Dict[str, Any]] = []
    n = len(steps)
    if n > 15:
        f.append(_factor("Long process", 2, "medium", f"{n} steps", "Each step logs OK / FAILED, so a failure points "
                         "at the exact step."))
    elif n > 8:
        f.append(_factor("Several steps", 1, "low", f"{n} steps", "Each step is logged separately."))
    if len(params) > 6:
        f.append(_factor("Many input values", 1, "low", f"{len(params)} values",
                         "All values come from payload.json - nothing is hard-coded."))
    pages = int(sig.get("pages") or 1)
    if pages > 3:
        f.append(_factor("Many pages", 2, "medium", f"The process visits {pages} pages.",
                         "Each page's address comes from TARGET_URL_<n>; every step waits for its page before acting."))
    elif pages > 1:
        f.append(_factor("Several pages", 1, "low", f"The process visits {pages} pages.",
                         "Each page's address comes from TARGET_URL_<n>; every step waits for its page before acting."))
    if int(sig.get("hosts") or 1) > 1:
        f.append(_factor("More than one site", 1, "medium", f"The pages are on {sig['hosts']} different sites - a "
                         "sign-in or cookie on one does not carry over to the other.",
                         "The code signs in / sets up each site where the recording does; the run's network allows "
                         "exactly these sites."))
    if sig.get("needs_login_first"):
        f.append(_factor("A page needs a sign-in first", 1, "medium", "Opening one of the pages directly redirects to "
                         "a sign-in page.", "The code follows the recorded order (sign in first) instead of opening "
                         "that page directly."))
    if sig.get("missing_targets"):
        f.append(_factor("Elements not on the page yet", 1, "medium", f"{sig['missing_targets']} element(s) the steps "
                         "use were not on their page when it opened - they appear after an earlier step, or on another "
                         "page.", "The code waits for each element after the step before it; self-healing re-reads the "
                         "page if one is still missing."))
    if sig.get("new_window"):
        f.append(_factor("Opens a new tab", 1, "medium", "A step clicks a link that opens a new tab / window.",
                         "The code waits for the new tab and continues in it."))
    if sig.get("dialogs"):
        f.append(_factor("Pop-up dialogs", 1, "medium", "A button pops up a browser confirm / alert.",
                         "The code accepts (or dismisses) the dialog the way the recording does."))
    if sig.get("dynamic_ids") or sig.get("dynamic_names"):
        f.append(_factor("Element ids change on every load", 2, "high",
                         "The page gives its fields new random ids / names each time it loads, so id-based scripts "
                         "break on the next run.", "Every field is located by its visible label - never by id or name "
                         "(checked on a fresh page load before the code runs)."))
    if sig.get("shuffled_fields"):
        f.append(_factor("Fields change position", 2, "high", "The fields appear in a different order / place on "
                         "every load (like rpachallenge.com).", "No positions, indexes or field order are used; the "
                         "fields are found again after every submit."))
    if sig.get("cross_origin_iframes"):
        f.append(_factor("Content in a cross-origin iframe", 2, "high", f"{sig['cross_origin_iframes']} iframe(s) from "
                         "another site", "The code must switch into the frame; some tools (e.g. Cypress) can't reach "
                         "other sites' frames."))
    elif sig.get("iframes"):
        f.append(_factor("Content in an iframe", 1, "medium", f"{sig['iframes']} iframe(s)",
                         "The code switches into the frame before it uses its fields."))
    if sig.get("shadow_hosts"):
        f.append(_factor("Shadow DOM", 1, "medium", "Some elements live inside web components' shadow roots.",
                         "Locators that pierce shadow DOM (Playwright does this by default)."))
    if sig.get("framework"):
        f.append(_factor(f"{sig['framework']} single-page app", 1, "low", "The page is built by JavaScript after it "
                         "loads.", "Explicit waits for each element instead of fixed sleeps."))
    if sig.get("captcha"):
        f.append(_factor("CAPTCHA", 3, "high", "The page has a CAPTCHA - it is designed to stop automation.",
                         "Can't be automated reliably; use a test environment without the CAPTCHA."))
    if sig.get("password_inputs"):
        f.append(_factor("Login", 1, "medium", "The flow signs in.", "Passwords come from secret environment "
                         "variables, never from the code or the payload."))
    if sig.get("file_inputs"):
        f.append(_factor("File upload", 1, "medium", "The flow uploads a file.", "The file path comes from the payload."))
    if sig.get("unlabelled_fields"):
        f.append(_factor("Fields without a label", 1, "medium", f"{sig['unlabelled_fields']} field(s) have no visible "
                         "label.", "Their stable attributes (or placeholder) are used instead."))
    if _DATA_DRIVEN.search(sop or "") or sig.get("repeated_rows"):
        f.append(_factor("Repeated data rows", 1, "medium", "The SOP describes several records / rounds.",
                         "The generated code fills one record per run from payload.json; loop over your data in CI."))
    if not probed:
        f.append(_factor("Page not probed", 0, "low", ((profile or {}).get("error") or "the live page was not checked")[:160],
                         "The assessment is based on the recording and the SOP only."))
    score = max(1, min(10, 1 + sum(x["points"] for x in f)))
    return {"score": score, "level": level_for(score), "factors": f, "probed": probed}


def _fit_baseline(tool: Dict[str, Any], sig: Dict[str, Any]) -> Dict[str, Any]:
    name = (tool.get("name") or tool.get("id") or "").lower()
    cat = (tool.get("category") or "").lower()
    if cat == "api" or any(k in name for k in ("requests", "rest assured", "newman", "postman", "k6")):
        return {"fit": "poor", "why": "an API tool - it does not drive the page's form"}
    if "cypress" in name and sig.get("cross_origin_iframes"):
        return {"fit": "ok", "why": "Cypress can't reach another site's iframe"}
    if any(k in name for k in ("playwright", "webdriverio", "cypress", "puppeteer")):
        return {"fit": "good", "why": "waits for elements automatically - handles pages that render late or change"}
    if any(k in name for k in ("selenium", "robot")):
        return {"fit": "good" if not (sig.get("shadow_hosts") or sig.get("framework")) else "ok",
                "why": "works well with label-based XPath; needs explicit waits on dynamic pages"}
    return {"fit": "ok", "why": "can automate the page with label-based locators"}


def assess(s: Dict[str, Any], profile: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    analysis = s.get("analysis") or {}
    steps = analysis.get("steps") or []
    params = s.get("parameters") or []
    base = baseline(steps, params, s.get("sop_text") or "", profile)
    sig = (profile or {}).get("signals") or {}
    tools = [t for t in (s.get("framework_options") or []) if t.get("id")][:10]
    fits = {t["id"]: dict(_fit_baseline(t, sig), tool_id=t["id"], name=t.get("name") or t["id"]) for t in tools}
    out: Dict[str, Any] = dict(base, summary=_summary(base), tool_fit=list(fits.values()), tips=[],
                               agent_source="LOCAL_HEURISTIC", model="Complexity rules")
    res = generate_json("AUTOMATION_COMPLEXITY", {
        "process": json.dumps({"name": analysis.get("process_name") or "", "steps": [
            {"order": st.get("order"), "action": st.get("action"), "label": st.get("label") or st.get("description")}
            for st in steps[:30]], "values": len(params)}, indent=1),
        "page": json.dumps({"signals": {k: v for k, v in sig.items() if v}, "probed": base["probed"],
                            "title": (profile or {}).get("title"),
                            "pages": [{k: p.get(k) for k in ("index", "url", "title", "loads", "reload_why", "error")}
                                      for p in (profile or {}).get("pages") or []],
                            "checks": [{k: c.get(k) for k in ("page", "title", "why", "finding", "relevant")}
                                       for c in (profile or {}).get("checks") or []][:30],
                            "focus": ((profile or {}).get("plan") or s.get("inspection_plan") or {}).get("focus")
                            or []}, default=str),
        "baseline": json.dumps({"score": base["score"], "level": base["level"],
                                "factors": [{k: x[k] for k in ("name", "severity", "detail")} for x in base["factors"]]}),
        "tools": json.dumps([{"tool_id": t["id"], "name": t.get("name"), "language": t.get("language"),
                              "category": t.get("category")} for t in tools]),
        "sop": (s.get("sop_text") or "")[:1500]}, untrusted=["process", "sop", "page"])
    if res.ok and isinstance(res.data, dict):
        d = res.data
        try:
            ai_score = int(d.get("score") or base["score"])
        except (TypeError, ValueError):
            ai_score = base["score"]
        # the AI may refine the score by at most 2 points - the evidence decides the band
        score = max(1, min(10, max(base["score"] - 2, min(base["score"] + 2, ai_score))))
        out.update(score=score, level=level_for(score), agent_source=res.agent_source, model=res.model,
                   summary=str(d.get("summary") or out["summary"])[:700],
                   tips=[str(x)[:240] for x in (d.get("tips") or [])][:4])
        for ch in d.get("challenges") or []:
            if not isinstance(ch, dict):
                continue
            hit = next((x for x in out["factors"] if x["name"].lower() == str(ch.get("name", "")).lower()), None)
            if hit and ch.get("how_handled"):
                hit["how_handled"] = str(ch["how_handled"])[:300]
            elif not hit and ch.get("name"):
                out["factors"].append(_factor(str(ch["name"])[:80], 0, str(ch.get("severity") or "low")[:10],
                                              str(ch.get("detail") or "")[:300], str(ch.get("how_handled") or "")[:300]))
        for tf in d.get("tool_fit") or []:
            if isinstance(tf, dict) and tf.get("tool_id") in fits and tf.get("fit") in ("good", "ok", "poor"):
                fits[tf["tool_id"]].update(fit=tf["fit"], why=str(tf.get("why") or "")[:240])
        out["tool_fit"] = list(fits.values())
    return out


def for_prompt(c: Optional[Dict[str, Any]]) -> str:
    """9.4: the assessment as the build / heal agent reads it - the same one the user saw in Step 1."""
    if not c:
        return "(not assessed)"
    lines = [f"{c.get('level')} - {c.get('score')}/10. {c.get('summary') or ''}".strip()]
    rel = [x for x in c.get("factors") or [] if x.get("name") != "Page not probed"]
    if rel:
        lines.append("CHALLENGES (handle each one):")
        lines += [f"- [{x.get('severity')}] {x.get('name')}: {x.get('detail') or ''} -> {x.get('how_handled') or ''}"
                  for x in rel[:12]]
    else:
        lines.append("CHALLENGES: none - keep the code simple.")
    if c.get("tips"):
        lines.append("TIPS: " + " | ".join(c["tips"][:3]))
    hard = [x for x in c.get("difficulty") or [] if (x.get("value") or 0) >= 4]
    if hard:                                   # 9.8: the difficulty parameters, as the user may have tuned them
        lines.append("DIFFICULTY PARAMETERS (0-10" + (", tuned by the user" if any(x.get("tuned") for x in hard) else "")
                     + "): " + "; ".join(f"{x['name']} {x['value']}" for x in hard[:8]))
    return "\n".join(lines)


def _summary(base: Dict[str, Any]) -> str:
    high = [x["name"] for x in base["factors"] if x["severity"] == "high"]
    if base["level"] == "Simple":
        return "A straightforward automation: a stable page and a short process."
    if high:
        return f"{base['level']}: " + ", ".join(high) + ". Understudy's build agent anchors every field on its label " \
               "and checks the locators on a fresh page load before the code runs."
    return f"{base['level']}: " + ", ".join(x["name"] for x in base["factors"][:3]) + "."
