"""Self-healing for a failed step-4 run (and the failure classifier used by learned skills).

Since 9.0 a run that passes step 4 is exported right away - the target page is no longer re-checked before / after
(the pre-9.0 ``verify`` step and its ``VERIFY_ASSESS`` prompt were removed in 9.4.1). What remains here:

* ``classify_failure`` - SELECTOR_DRIFT, TIMING, NAVIGATION, ENVIRONMENT, ... from the run's result and log tail.
* ``self_heal`` - diagnoses the failure, patches the step plan against a FRESH page inventory (AI: ``VERIFY_SELF_HEAL``;
  offline: ``heuristic_heal``), applies the user's corrections (``apply_user_feedback``) and appends healing notes that
  are fed into the BUILD code-generation prompt - i.e. the agent corrects its own prompt context before the suite is
  regenerated, re-governed and re-executed.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple

from core.wizard import dom_discovery
from core.wizard.governance import SYNONYMS
from core.wizard.llm import generate_json, record_fallback


# ---------------------------------------------------------------------------
# Self-healing
# ---------------------------------------------------------------------------
_ENV_RX = re.compile(r"(?i)ModuleNotFoundError|Cannot find module|Executable doesn't exist|chromedriver|command not found|"
                     r"No such file|ENOENT|Runner not found|Browser could not start|SessionNotCreated|"
                     r"Could not find (?:Cypress|Chrome)|Playwright is not installed|cannot open display|ImportError|"
                     r"NoSuchDriverException|Unable to obtain driver|browser path is not a valid file|driver_location|"
                     r"cannot find (?:Chrome|Chromium|Firefox) binary|Chrome instance exited|DevToolsActivePort|"
                     r"Failed to launch|Could not find expected browser|No usable sandbox|is not recognized as an internal or external command|"
                     r"JAVA_HOME|No module named")
_SELECTOR_RX = re.compile(r"(?i)Element not found|waiting for locator|NoSuchElement|no such element|Unable to locate|"
                          r"failed to find element|did not find|Timeout .*exceeded.*locator|strict mode violation|"
                          r"Expected condition failed|waiting for (?:visibility|presence|element)")
# 9.9.1: Selenium appends these to EVERY exception ("Capabilities {... chromedriverVersion ...}", "Driver info:
# ChromeDriver") - diagnostics of a browser that DID start, never a sign that the runtime is broken.
_SELENIUM_DUMP_RX = re.compile(r"(?im)^.*\b(?:Capabilities \{|Driver info:|Build info:|System info:|Session ID:).*$")
_NAV_RX = re.compile(r"(?i)net::ERR|ERR_CONNECTION|NS_ERROR|page\.goto|Navigation timeout")


def _tokens(s: str) -> set:
    words = set(re.findall(r"[a-z0-9]+", re.sub(r"([a-z])([A-Z])", r"\1 \2", s or "").lower().replace("_", " ")))
    out = set(words)
    for concept, syns in SYNONYMS.items():
        flat = {w for syn in syns for w in syn.split()}
        if words & flat or concept in words:
            out |= flat | {concept}
    return out - {"the", "a", "of", "e", "g", "eg", "to", "and", "or"}


def _score(step: Dict[str, Any], el: dom_discovery.DomElement) -> float:
    st = _tokens(" ".join([step.get("param_key") or "", step.get("label") or "", step.get("description") or ""]))
    et = _tokens(el.descriptor())
    if not st or not et:
        return 0.0
    jacc = len(st & et) / len(st | et)
    lab = SequenceMatcher(None, (step.get("label") or "").lower(), (el.label or el.text or "").lower()).ratio()
    return round(0.7 * jacc + 0.3 * lab, 3)


def heuristic_heal(steps: List[Dict[str, Any]], inv: dom_discovery.PageInventory, category: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[str]]:
    new_steps = [dict(s) for s in steps]
    changes: List[Dict[str, Any]] = []
    notes: List[str] = []
    if category in ("SELECTOR_DRIFT", "UNKNOWN", "TIMING") and inv.elements:
        taken = set()
        for s in new_steps:
            if s["action"] not in ("fill", "select", "check", "click"):
                continue
            existing = [x for x in [s.get("selector")] + list(s.get("fallback_selectors") or []) if x]
            if any(inv.selector_exists(x) for x in existing):
                for el in inv.elements:
                    if any(x in el.selectors() for x in existing):
                        taken.add(id(el))
                continue
            want_button = s["action"] == "click"
            cands = [e for e in inv.elements if (e.kind in ("button", "link")) == want_button and id(e) not in taken]
            if s["action"] == "select":
                cands = [e for e in cands if e.kind == "select"] or cands
            scored = sorted(((_score(s, e), e) for e in cands), key=lambda x: x[0], reverse=True)
            if want_button and (not scored or scored[0][0] < 0.15):
                subs = [e for e in cands if e.type == "submit"]
                scored = [(0.5, subs[0])] + scored if subs else scored
            if scored and scored[0][0] >= 0.12:
                el = scored[0][1]
                taken.add(id(el))
                sels = el.selectors() or [el.best_selector()]
                old = s.get("selector")
                s["selector"] = sels[0]
                s["fallback_selectors"] = (sels[1:] + [x for x in existing if x not in sels])[:5]
                if el.label or el.text:
                    s["label"] = (el.label or el.text)[:80]
                changes.append({"step": s["order"], "from": old, "to": s["selector"], "confidence": scored[0][0],
                                "why": f"'{old}' no longer exists; best match is '{el.descriptor()[:80]}'"})
        if changes:
            notes.append("Selectors drifted on the target page; prefer these current selectors: " +
                         ", ".join(f"step {c['step']} -> {c['to']}" for c in changes))
    if category in ("TIMING", "UNKNOWN") or (category == "SELECTOR_DRIFT" and not changes):
        clicks = [s for s in new_steps if s["action"] == "click"]
        if clicks:
            last = clicks[-1]
            idx = new_steps.index(last)
            nxt = new_steps[idx + 1] if idx + 1 < len(new_steps) else None
            if nxt and nxt["action"] == "wait" and int(float(nxt.get("value") or 1000)) >= 8000:
                pass                                  # already waiting the maximum: another wait would change nothing
            elif nxt and nxt["action"] == "wait":
                old = int(float(nxt.get("value") or 1000))
                nxt["value"] = str(min(8000, old + 2000))
                changes.append({"step": nxt["order"], "from": f"wait {old}ms", "to": f"wait {nxt['value']}ms",
                                "why": "Give the application more time to save before verifying"})
            else:
                new_steps.insert(idx + 1, {"order": 0, "action": "wait", "value": "3000", "selector": "",
                                           "fallback_selectors": [], "label": "",
                                           "description": "Wait for the application to finish saving"})
                changes.append({"step": last["order"] + 1, "from": None, "to": "wait 3000ms",
                                "why": "The run finished but the change was not visible yet - add a wait after submit"})
            notes.append("After the final submit click, wait for the application to persist the record before finishing.")
    for i, s in enumerate(new_steps, 1):
        s["order"] = i
    return new_steps, changes, notes


_UNFILLABLE_RX = re.compile(r'(?i)cannot be filled|type "?file"?|not an? <?(input|textarea|select)|is not editable|'
                            r'element is not (visible|editable|enabled)|ElementNotInteractable|not interactable')


def drop_unfillable_step(steps: List[Dict[str, Any]], params: List[Dict[str, Any]], execution: Dict[str, Any],
                         log_tail: str, inv: dom_discovery.PageInventory):
    """A step that types into something that can't take text (a file-upload control, a hidden field) and whose
    value is empty and optional can simply be left out. Returns (new_steps, change) or None."""
    failed = execution.get("failed_step")
    step = next((s for s in steps if s.get("order") == failed), None)
    if not step or step.get("action") not in ("fill", "select", "check") or not _UNFILLABLE_RX.search(log_tail[-3000:]):
        return None
    p = next((x for x in params if x.get("key") == step.get("param_key")), None)
    sels = [x for x in [step.get("selector")] + list(step.get("fallback_selectors") or []) if x]
    is_file = any(e.type == "file" and any(s in e.selectors() for s in sels) for e in inv.elements) or \
        'type "file"' in log_tail or "type=\"file\"" in log_tail
    empty_optional = p is None or (not str(p.get("value") or "").strip() and not p.get("required"))
    if not (empty_optional or (is_file and not str((p or {}).get("value") or "").strip())):
        return None
    new_steps = [dict(s) for s in steps if s is not step]
    for i, s in enumerate(new_steps, 1):
        s["order"] = i
    what = "a file-upload control" if is_file else "a field that does not accept typing"
    return new_steps, {"step": failed, "from": step.get("selector"), "to": "(step removed)",
                       "why": f"Step {failed} types into {what} and its value is empty - the step is not needed"}


def classify_failure(execution: Dict[str, Any], verification: Optional[Dict[str, Any]], log_tail: str) -> str:
    if execution.get("governance_missing"):
        # 9.9.1: the run finished but no 'GOVERN OK' line reached the log. That is about the script's output, never
        # the runtime - a log line such as "chromedriver" must not turn it into an ENVIRONMENT "reinstall".
        return "GOVERNANCE"
    # 9.9.1: Selenium's diagnostic dump ("Capabilities {... chromedriverVersion ...}") follows every exception of a
    # browser that DID start - it must not turn a page / timing failure into an ENVIRONMENT "reinstall".
    env_tail = _SELENIUM_DUMP_RX.sub("", log_tail)
    if execution.get("failure_category_hint") == "ENVIRONMENT" or (
            execution.get("status") == "FAIL" and _ENV_RX.search(env_tail)):
        return "ENVIRONMENT"
    if execution.get("status") == "FAIL":
        reason = str(execution.get("failure_reason") or "") + " " + log_tail[-2000:]
        if _NAV_RX.search(reason) and execution.get("failed_step") in (None, 1):
            return "NAVIGATION"
        if _SELECTOR_RX.search(reason):
            return "SELECTOR_DRIFT"
        return "UNKNOWN"
    if verification and verification.get("status") == "FAIL":
        return "TIMING"
    return "UNKNOWN"


_FB_SELECTOR = re.compile(r"(?i)\bstep\s*(\d+)\b[^\n]*?(?:selector|use|should\s+(?:be|use)|is|=|:|->)\s*[\"'`]?"
                          r"((?:#|\.|\[|//|text=|xpath=|css=)[^\s\"'`,;]+|[a-z]+\[[^\]]+\][^\s\"'`,;]*)")
_FB_WAIT = re.compile(r"(?i)\bwait\b[^\d]{0,20}(\d+(?:\.\d+)?)\s*(ms|milliseconds?|s|secs?|seconds?)?")


def apply_user_feedback(steps: List[Dict[str, Any]], feedback: List[str]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[str]]:
    """Offline (no AI): understands two kinds of plain-English correction, so a user can still steer the fix:
    "step 3 should use #vendor-name" (any CSS / xpath / text= selector) and "wait 3 seconds after submit".
    Anything else is kept as a healing note for the code generator. Returns (steps, changes, notes)."""
    new_steps = [dict(x) for x in steps]
    changes: List[Dict[str, Any]] = []
    notes: List[str] = []
    for fb in feedback or []:
        hit = False
        for m in _FB_SELECTOR.finditer(fb):
            n, sel = int(m.group(1)), m.group(2).rstrip(".")
            st = next((x for x in new_steps if x.get("order") == n), None)
            if st is not None and st.get("selector") != sel:
                old = st.get("selector")
                st["fallback_selectors"] = ([old] if old else []) + [x for x in (st.get("fallback_selectors") or []) if x not in (sel, old)][:4]
                st["selector"] = sel
                changes.append({"step": n, "from": old, "to": sel, "why": "your correction"})
                hit = True
        m = _FB_WAIT.search(fb)
        if m:
            v = float(m.group(1))
            ms = int(v if (m.group(2) or "").lower().startswith("m") else v * 1000)
            ms = max(250, min(ms, 10000))
            clicks = [x for x in new_steps if x["action"] == "click"]
            if clicks:
                idx = new_steps.index(clicks[-1])
                if idx + 1 < len(new_steps) and new_steps[idx + 1]["action"] == "wait":
                    new_steps[idx + 1]["value"] = str(ms)
                else:
                    new_steps.insert(idx + 1, {"order": 0, "action": "wait", "value": str(ms),
                                               "description": f"Wait {ms} ms after submitting (your correction)"})
                for i, x in enumerate(new_steps, 1):
                    x["order"] = i
                changes.append({"step": idx + 2, "from": None, "to": f"wait {ms} ms", "why": "your correction"})
                hit = True
        notes.append(f"User correction to apply: {fb[:300]}")
        if not hit:
            notes.append("(The built-in healer could not apply that correction by itself; it is passed to the code "
                         "generator as an instruction.)")
    return new_steps, changes, notes


_WAIT_TEXT_RX = re.compile(r"""(?i)(?:to contain text|text to be present[^"']*|toHaveText\(|contains?\s+text)\s*[:(]?\s*["']([^"'\n]{2,80})["']""")


def contextual_notes(steps: List[Dict[str, Any]], params: List[Dict[str, Any]], execution: Dict[str, Any],
                     verification: Optional[Dict[str, Any]], log_tail: str) -> List[str]:
    """8.2: two mistakes generated code makes on real sites, recognised from the run itself and turned into instructions
    for the next build (with or without AI):
    * a date / time field filled by TYPING - the browser's locale reorders the digits (a run saved '60923-02-20');
    * waiting after submit for a success text the page never shows (the AI paraphrased a message from the video)."""
    notes: List[str] = []
    missing = set((verification or {}).get("missing_keys") or [])
    for p in params:
        if p.get("type") in ("date", "time", "datetime", "datetime-local", "month") and (
                p.get("key") in missing or re.search(r"\b\d{5,6}-\d{2}-\d{2}\b", log_tail)):
            label = p.get("label") or p.get("key")
            notes.append(f"Fill the date/time field '{label}' with JavaScript, not by typing: set element.value to the "
                         f"ISO value from payload.json (yyyy-mm-dd for dates) and dispatch 'input' and 'change' events "
                         f"(Selenium: ((JavascriptExecutor) driver).executeScript(...)). Typing is locale-dependent and "
                         f"saved a wrong date.")
    m = _WAIT_TEXT_RX.search(str(execution.get("failure_reason") or "") + "\n" + log_tail[-6000:])
    if m:
        text = m.group(1).strip()
        planned = " ".join(str(st.get("value") or "") + " " + str(st.get("description") or "") for st in steps
                           if st.get("action") == "assert_text")
        values = " ".join(str(p.get("value") or "") for p in params)
        if text.lower() not in planned.lower() and text.lower() not in values.lower():
            notes.append(f"Do not wait for the text '{text}' after submitting - the page never shows it (it is not in "
                         f"the step plan). Confirm success only with the step plan's own checks, or by waiting until a "
                         f"submitted payload value (e.g. the vendor name) appears on the page.")
    return notes


def self_heal(steps: List[Dict[str, Any]], params: List[Dict[str, Any]], target_url: str, execution: Dict[str, Any],
              verification: Optional[Dict[str, Any]], logs: List[str], healing_notes: List[str],
              user_feedback: Optional[List[str]] = None, extra_urls: Optional[List[str]] = None) -> Dict[str, Any]:
    from core.wizard.activity_log import strip_context
    hints = contextual_notes(steps, params, execution, verification, strip_context("\n".join(logs[-200:])))
    result = _self_heal(steps, params, target_url, execution, verification, logs,
                        list(healing_notes or []) + hints, user_feedback or [], extra_urls)
    if hints:
        result["new_notes"] = list(dict.fromkeys(hints + list(result.get("new_notes") or [])))[:8]
        if result.get("agent_source") != "ACTUAL_AI_API":
            result["diagnosis"] = (str(result.get("diagnosis") or "") + " Also: " + " ".join(hints)).strip()
    if user_feedback and result.get("agent_source") != "ACTUAL_AI_API" and result.get("category") != "ENVIRONMENT":
        # No live AI read the correction (offline, or a learned answer): apply what can be applied deterministically.
        new_steps, ch, notes = apply_user_feedback(result["steps"], user_feedback)
        result["steps"] = new_steps
        result["changes"] = list(result.get("changes") or []) + ch
        result["new_notes"] = (list(result.get("new_notes") or []) + notes)[-8:]
        result["user_feedback_applied"] = (
            f"Applied {len(ch)} change(s) from your correction" if ch else
            "Your correction was saved as an instruction for the code generator (without AI, Understudy understands "
            "'step N should use <selector>' and 'wait N seconds after submit').")
    elif user_feedback:
        result["new_notes"] = (list(result.get("new_notes") or []) +
                               [f"User correction to apply: {f[:300]}" for f in user_feedback])[-8:]
    return result


def _self_heal(steps: List[Dict[str, Any]], params: List[Dict[str, Any]], target_url: str, execution: Dict[str, Any],
               verification: Optional[Dict[str, Any]], logs: List[str], healing_notes: List[str],
               user_feedback: List[str], extra_urls: Optional[List[str]] = None) -> Dict[str, Any]:
    from core.wizard.activity_log import strip_context
    log_tail = strip_context("\n".join(logs[-150:]))      # no user/project/session names in prompts or fingerprints
    category = classify_failure(execution, verification, log_tail)
    if execution.get("failure_html"):
        inv = dom_discovery.parse_html(execution.get("failure_url") or target_url, execution["failure_html"])
        inv_source = "page at the moment of failure"
    else:
        inv = dom_discovery.discover(target_url)
        inv_source = "fresh page inventory"
    result: Dict[str, Any] = {"category": category, "inventory_source": inv_source, "strategy_override": None,
                              "agent_source": "LOCAL_HEURISTIC", "model": "Deterministic self-healer"}
    if category == "GOVERNANCE":
        # No AI patch of the steps and no runtime repair: the step plan worked. The next BUILD regenerates the code with
        # this note, so the script calls the vetted checker before step 1 and prints its line to the console.
        result.update({"steps": steps, "changes": [],
                       "new_notes": ["Call understudy_governance check(payload) before step 1 and let its "
                                     "'GOVERN OK' line reach the console (do not swallow or redirect stdout)."],
                       "diagnosis": "The automation steps ran, but no 'GOVERN OK' line reached the log, so Understudy "
                                    "could not confirm the SOP rules were checked before step 1. The steps are kept; "
                                    "the code is regenerated so the script calls the SOP check and prints its result."})
        return result
    if category == "ENVIRONMENT":
        # The step plan is fine - the chosen tool's runtime (browser / driver / package / toolchain) could not start.
        # We do NOT switch tools here: the session applies a runtime repair (core/wizard/runtime_repair.py) and
        # re-runs the SAME framework. Switching to the built-in Playwright driver is only ever an explicit user choice.
        result.update({"steps": steps, "changes": [], "new_notes": [],
                       "diagnosis": "The step plan looks fine - the framework's runtime could not start (browser, driver, "
                                    "package or toolchain). Understudy will repair the runtime and re-run the same tool."})
        return result
    param_keys_used = {s.get("param_key") for s in steps if s.get("param_key")}
    ai = generate_json("VERIFY_SELF_HEAL",
                       {"steps": steps, "logs": log_tail, "verification": verification or {"status": "not run"},
                        "dom_inventory": dom_discovery.inventory_for_prompt(inv), "healing_notes": healing_notes or ["(none)"],
                        "user_feedback": "\n".join(f"{i}. {f}" for i, f in enumerate(user_feedback, 1)) or "(none)"},
                       untrusted=["logs", "dom_inventory", "user_feedback"])
    if ai.ok and user_feedback and ai.agent_source != "ACTUAL_AI_API":
        ai.ok = False            # a remembered answer can't have read this correction: use the deterministic path
        ai.error = "A learned answer cannot apply your new correction"
    if ai.ok:
        from core.wizard.watch import normalize_analysis
        raw = {"steps": ai.data.get("steps") or [], "parameters": params, "framework_options": []}
        norm, problems = normalize_analysis(raw, target_url, extra_urls)
        new_steps = norm["steps"]
        used_after = {s.get("param_key") for s in new_steps if s.get("param_key")}
        keeps_submit = any(s["action"] == "click" for s in new_steps) or not any(s["action"] == "click" for s in steps)
        ai_category = str(ai.data.get("failure_category") or "").upper()
        if ai_category == "ENVIRONMENT":
            # The AI agrees the runtime (browser/driver/package) is the problem, not the plan: keep the steps and let the
            # session repair the SAME framework's runtime. No silent tool switch.
            result.update({"steps": steps, "changes": [], "new_notes": [str(n)[:300] for n in ai.data.get("healing_notes") or []][:5],
                           "diagnosis": str(ai.data.get("diagnosis") or "")[:1200] +
                                        " Understudy will repair the runtime and re-run the same tool.",
                           "category": "ENVIRONMENT", "agent_source": ai.agent_source, "model": ai.model,
                           "recall": ai.recall})
            return result
        if param_keys_used <= used_after and keeps_submit and not problems:
            before = {s["order"]: s.get("selector") for s in steps}
            changes = [{"step": s["order"], "from": before.get(s["order"]), "to": s.get("selector") or s["action"],
                        "why": "AI patch"} for s in new_steps if before.get(s["order"]) != s.get("selector")]
            result.update({"steps": new_steps, "changes": changes,
                           "new_notes": [str(n)[:300] for n in ai.data.get("healing_notes") or []][:5],
                           "question_for_user": str(ai.data.get("question_for_user") or "").strip()[:400],
                           "user_feedback_applied": str(ai.data.get("user_feedback_applied") or "").strip()[:600],
                           "diagnosis": str(ai.data.get("diagnosis") or "")[:1200],
                           "category": ai.data.get("failure_category") or category,
                           "agent_source": ai.agent_source, "model": ai.model, "recall": ai.recall})
            return result
        result["ai_rejected"] = ("Learned fix" if ai.agent_source != "ACTUAL_AI_API" else "AI patch") + " rejected (changed parameters, dropped the submit step, or failed screening)."
    dropped = drop_unfillable_step(steps, params, execution, log_tail, inv)
    if dropped:
        new_steps, change = dropped
        diag = change["why"] + ". Understudy removed it and will run the rest of the plan again."
        result.update({"steps": new_steps, "changes": [change], "category": "DATA_REJECTED", "diagnosis": diag,
                       "new_notes": [f"Do not type into '{change['from']}': it is not a text field (file upload / hidden)."]})
        record_fallback("VERIFY_SELF_HEAL", result.get("ai_rejected") or ai.error or "AI unavailable",
                        {"category": "DATA_REJECTED", "diagnosis": diag, "changes": [change]},
                        "Deterministic self-healer (remove an optional step that targets a non-text control).")
        return result
    new_steps, changes, notes = heuristic_heal(steps, inv, category)
    diag = {
        "SELECTOR_DRIFT": f"Step {execution.get('failed_step')} could not find its element - the page's selectors changed. "
                          f"Understudy re-matched {len([c for c in changes if c.get('confidence')])} element(s) against the "
                          f"{inv_source} by label, name and placeholder.",
        "TIMING": "The automation finished but the change was not visible when Understudy checked. It will wait longer "
                  "after the final click so the application has time to save.",
        "NAVIGATION": "The target page could not be opened. Check the target URL is reachable; Understudy will retry as-is.",
        "UNKNOWN": "The cause is unclear. Understudy re-checked selectors against the live page and added a wait after "
                   "submit before retrying.",
    }.get(category, "Retrying with adjusted steps.")
    result.update({"steps": new_steps, "changes": changes, "new_notes": notes, "diagnosis": diag})
    record_fallback("VERIFY_SELF_HEAL", result.get("ai_rejected") or ai.error or "AI unavailable",
                    {"category": category, "diagnosis": diag, "changes": changes, "notes": notes},
                    "Deterministic self-healer (re-match selectors against the live page, add waits).")
    return result
