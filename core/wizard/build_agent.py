"""Build agent (9.0): Gemini writes the suite for the chosen tool, Understudy checks it, Gemini fixes what failed.

Before 9.0 BUILD was one shot: the AI wrote code from the step plan (selectors grounded once, usually ids), and code
that failed the safety / syntax gate fell back to a template. On a page like rpachallenge.com - new random ids and a new
field order on every load - that code could only work by luck. Now:

1. The AI gets the PAGE PROFILE (9.4: the automation-aware ``inspection`` - each page opened once, reloaded only when
   the automation needed ids / order compared): every element's label, which attributes are stable, label-anchored
   locators - and the AUTOMATION COMPLEXITY assessment (score, challenges, how to handle each), so the code is as
   defensive as this automation needs.
2. Its code goes through Understudy's gates (safety scan, parameterisation, packages, syntax). A rejection goes back to
   the AI as feedback instead of ending in a template.
3. Every locator the code uses (the AI lists them) is checked on ONE fresh load of its page: exactly one visible
   element, the right kind, the expected label - plus a static check against what the inspection already saw change
   (ids / names that differ between loads, positions on a page that shuffles). A locator that fails goes back to the
   AI with what it matched instead.
4. Up to ``MAX_ROUNDS`` rounds. The result carries the agent's trace (``out["agent"]``) for the UI and the ZIP.

Self-healing uses the same loop (``feedback`` = the failed run's error, failing step, log tail and previous code, plus a
fresh probe): the heal agent repairs the CODE against the live page, not just the step plan.
"""
from __future__ import annotations

import os
import re
from typing import Any, Callable, Dict, List, Optional

from core.wizard import build as build_mod, page_probe
from core.wizard.frameworks import FrameworkSpec

MAX_ROUNDS = int(os.environ.get("UNDERSTUDY_BUILD_AGENT_ROUNDS") or 3)


def probing_enabled() -> bool:
    return os.environ.get("UNDERSTUDY_PAGE_PROBE", "on").strip().lower() not in ("0", "off", "false", "no")


def probe(url: str) -> Optional[Dict[str, Any]]:
    """The live page profile (9.0 two-load probe), or None when probing is switched off. Kept for scripts; sessions
    use the automation-aware ``inspection`` (9.4)."""
    if not probing_enabled():
        return None
    return page_probe.probe(url)


def _strip_prefix(v: str) -> str:
    return re.sub(r"^(xpath=|css=|xpath:|css:)", "", v.strip())


def _used_in_code(loc: Dict[str, Any], files: Dict[str, str]) -> bool:
    v = _strip_prefix(str(loc.get("value") or ""))
    if not v:
        return False
    blob = "\n".join(files.values())
    if v in blob:
        return True
    if (loc.get("strategy") or "") == "role":
        name = v.split(":", 1)[-1]
        return bool(name) and name in blob
    alt = v.replace("'", '"')                  # quotes swapped to fit the language's string literal
    return alt in blob or v.replace('"', "'") in blob or v.replace("'", "\\'") in blob


def _previous(files: Dict[str, str], limit: int = 7000) -> str:
    out, used = [], 0
    for path, content in files.items():
        if used > limit:
            break
        chunk = content[: max(0, limit - used)]
        out.append(f"--- {path}\n{chunk}")
        used += len(chunk)
    return "\n".join(out)


def generate(spec: FrameworkSpec, steps: List[Dict[str, Any]], params: List[Dict[str, Any]],
             api: Optional[Dict[str, Any]], url: Any, healing_notes: List[str],
             profile: Optional[Dict[str, Any]] = None, feedback: Optional[List[str]] = None,
             log: Callable[[str], None] = lambda m: None, max_rounds: int = MAX_ROUNDS,
             complexity: Optional[Dict[str, Any]] = None,
             task_context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The verified suite (``build.generate_suite`` output + ``agent``). Raises ValueError like generate_suite.
    9.4: ``url`` may be the list of pages the process visits; ``complexity`` is the Step 1 assessment."""
    from core.wizard import complexity as complexity_mod
    urls = [url] if isinstance(url, str) else [u for u in (url or []) if u]
    profile_text = page_probe.profile_for_prompt(profile) if profile else ""
    complexity_text = complexity_mod.for_prompt(complexity) if complexity else ""
    fb = list(feedback or [])
    rounds: List[Dict[str, Any]] = []
    best: Optional[Dict[str, Any]] = None
    for n in range(1, max_rounds + 1):
        last = n == max_rounds
        try:
            out = build_mod.generate_suite(spec, steps, params, api, urls if len(urls) > 1 else urls[0],
                                           healing_notes, page_profile=profile_text, feedback=fb,
                                           allow_template=last and best is None, complexity=complexity_text,
                                           task_context=task_context)
        except build_mod.AttemptRejected as e:
            rounds.append({"round": n, "result": "rejected by Understudy's gates", "problems": e.problems[:6]})
            log(f"Build agent round {n}: the code was rejected ({'; '.join(e.problems)[:200]}) - asking the AI to fix it")
            fb = list(feedback or []) + [f"Understudy's gate rejected your previous code: {p}" for p in e.problems[:6]]
            if e.files:
                fb.append("Your previous code:\n" + _previous(e.files))
            continue
        if out["generator"] != "ai":                       # template / learned suite: nothing to iterate on
            out["agent"] = out.get("agent") or {"rounds": rounds + [{"round": n, "result": f"{out['generator']} suite (no AI loop)"}],
                                                "status": "not run", "verified_locators": 0, "page_loads": 0}
            return out
        locs = out.get("locators") or []
        unused = [l for l in locs if not _used_in_code(l, out["files"])]
        rec: Dict[str, Any] = {"round": n, "locators": len(locs)}
        problems: List[str] = []
        if unused:
            problems += [f"locator '{l.get('value')}' ({l.get('expect_label') or l.get('id')}) is listed but does not "
                         f"appear in your code - list exactly the locators the code uses" for l in unused[:4]]
        check: Dict[str, Any] = {"skipped": True}
        if profile and profile.get("renderer") == "chromium" and locs:
            # how many fresh loads per page: what the inspection's reload agent decided (1 when it said nothing)
            loads = {int(k): int(v) for k, v in (profile.get("verify_loads") or {}).items()} or 1
            check = page_probe.verify_locators(urls if len(urls) > 1 else urls[0], locs, loads=loads, profile=profile)
            rec.update(checked=check.get("checked", 0), page_loads=check.get("loads", 0),
                       failed=[r for r in check.get("results") or [] if not r["ok"]][:8])
            for r in check.get("results") or []:
                if not r["ok"]:
                    problems.append(f"locator {r['strategy']}='{r['value']}' did not hold on a fresh page load: "
                                    + "; ".join(r["problems"][:2]))
            unchecked = [r for r in check.get("results") or [] if r.get("unchecked")]
            if unchecked:
                rec["unchecked"] = len(unchecked)
            if check.get("error"):
                rec["note"] = check["error"]
        elif profile and profile.get("renderer") == "chromium" and not locs:
            problems.append("you did not list the locators your code uses in `locators` - list every one")
        rec["result"] = "verified" if not problems else f"{len(problems)} problem(s)"
        rounds.append(rec)
        verified = sum(1 for r in check.get("results") or [] if r.get("ok") and not r.get("unchecked"))
        out["agent"] = {"rounds": rounds, "verified_locators": verified, "page_loads": check.get("loads", 0),
                        "status": "verified" if not problems and not check.get("skipped") else
                        "unverified" if problems else "not checked",
                        "unresolved": problems[:8], "profile_summary": page_probe.strategy_summary(profile) if profile else "",
                        "complexity": {k: (complexity or {}).get(k) for k in ("score", "level")} if complexity else None,
                        "static_checks": bool(check.get("static_checks"))}
        best = out
        if not problems:
            log(f"Build agent round {n}: {verified} locator(s) held on {check.get('loads', 0)} fresh page load(s)"
                if not check.get("skipped") else f"Build agent round {n}: code accepted")
            return out
        log(f"Build agent round {n}: {len(problems)} problem(s) - " + problems[0][:200])
        if last:
            break
        fb = list(feedback or []) + problems[:10] + ["Your previous code:\n" + _previous(out["files"])]
    if best is None:
        raise ValueError("The build agent could not produce code that passes Understudy's gates: "
                         + "; ".join((rounds[-1].get("problems") or ["unknown"]) if rounds else ["no attempt"]))
    best.setdefault("notes", []).append(
        "The build agent could not make every locator hold on a fresh page load after "
        f"{len(rounds)} round(s): " + "; ".join((best.get("agent") or {}).get("unresolved") or [])[:400])
    return best


def failure_feedback(execution: Dict[str, Any], files: Dict[str, str], log_tail: str,
                     diagnosis: str = "", user_feedback: Optional[List[str]] = None) -> List[str]:
    """What the heal agent tells the AI about the run that failed."""
    fb = ["The previous code FAILED when it ran against the live page"
          + (f" at step {execution.get('failed_step')}" if execution.get("failed_step") else "")
          + f": {str(execution.get('failure_reason') or 'unknown error')[:600]}"]
    if diagnosis:
        fb.append(f"Understudy's diagnosis of the failure: {diagnosis[:600]}")
    if log_tail:
        fb.append("Last lines of the run log:\n" + log_tail[-2500:])
    for f in user_feedback or []:
        fb.append(f"The user says: {f[:500]}")
    if files:
        fb.append("The code that failed:\n" + _previous(files))
    return fb
