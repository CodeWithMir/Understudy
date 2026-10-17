"""STEP 2 - BUILD: editable payload table + automation-suite generation for the chosen framework.

``prepare_parameters`` - BUILD_PARAMETERIZE agent (or WATCH output + SOP flags offline) produces
                         the parameter rows the user edits in the preview table.
``generate_suite``     - BUILD_GENERATE_SUITE agent writes the suite for ANY framework; the
                         deterministic template is used when AI is unavailable, when the AI output is
                         invalid, or when the AI output fails the safety/parameterisation scan.
                         Parameter VALUES are never sent to the code generator (keys + types only), and
                         every suite is scanned for hardcoded values before it is accepted.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional

from core.wizard import dependency_lock, templates
from core.wizard.frameworks import FrameworkSpec
from core.wizard.governance import parse_sop_rules, scan_suite
from core.wizard.guardrails import normalize_param_key, screen_parameters
from core.wizard.llm import generate_json, record_fallback

_ALLOWED_TYPES = ("text", "number", "date", "email", "select", "boolean", "secret")


def prepare_parameters(analysis: Dict[str, Any], framework: FrameworkSpec, sop_text: str) -> Dict[str, Any]:
    base = [dict(p) for p in analysis.get("parameters") or []]
    meta = {"agent_source": "LOCAL_HEURISTIC", "model": "WATCH parameters + SOP parser", "notes": []}
    res = generate_json("BUILD_PARAMETERIZE",
                        {"framework_name": framework.name, "steps": analysis.get("steps") or [], "parameters": base,
                         "sop_text": sop_text or ""},
                        untrusted=["parameters", "sop_text"], item_schema_wrapper="parameters")
    params = base
    if res.ok:
        ai_params = []
        base_keys = {p["key"] for p in base}
        for p in res.data.get("parameters", []):
            q = dict(p)
            q["key"] = normalize_param_key(q.get("key", ""))
            q["type"] = q.get("type") if q.get("type") in _ALLOWED_TYPES else "text"
            q.setdefault("source", "video")
            if q["type"] == "secret":
                q["value"] = ""
            ai_params.append(q)
        # Never lose a parameter the step plan depends on.
        ai_keys = {p["key"] for p in ai_params}
        for p in base:
            if p["key"] not in ai_keys:
                ai_params.append(p)
        if base_keys <= {p["key"] for p in ai_params}:
            params = ai_params
            meta.update({"agent_source": res.agent_source, "model": res.model, "recall": res.recall})
    else:
        meta["notes"].append(f"AI parameter refinement unavailable: {res.error}")
        record_fallback("BUILD_PARAMETERIZE", res.error or "AI unavailable", base, "Kept the WATCH parameters as-is.")
    if res.ok and meta["agent_source"] == "LOCAL_HEURISTIC":
        record_fallback("BUILD_PARAMETERIZE", "AI output dropped a parameter the step plan needs", base,
                        "Kept the WATCH parameters as-is.")
    # The page's own constraints (and the control type they imply) come from grounding in WATCH; the refinement agent
    # must not drop them.
    page = {p["key"]: p for p in base if p.get("constraints")}
    for p in params:
        b = page.get(p["key"])
        if b:
            p["constraints"] = b["constraints"]
            if b.get("type") in ("select", "boolean", "secret") and p.get("type") != b["type"]:
                p["type"] = b["type"]
                if b["type"] == "secret":
                    p["value"] = ""
        else:
            p.pop("constraints", None)
    sop_fields = {r["field"] for r in parse_sop_rules(sop_text, params)}
    for p in params:
        p.setdefault("label", p["key"].replace("_", " ").title())
        p.setdefault("required", True)
        p.setdefault("description", "")
        p.setdefault("verify", p.get("type") in ("text", "number", "date", "select", "email"))
        p["sop_dependent"] = bool(p.get("sop_dependent")) or p["key"] in sop_fields
    clean, problems = screen_parameters(params)
    return {"parameters": clean, "problems": problems, **meta}


_CODE_EXT = (".py", ".js", ".mjs", ".cjs", ".ts", ".java", ".cs", ".rb", ".go", ".kt")
_STEP_OK_LINE = re.compile(r"STEP\s+(\d+)\s+OK\b")
_STEP_COMMENT = re.compile(r"(?i)^\s*(?:#|//|/\*|\*)\s*step\s*(\d+)\b")
_UNNUMBERED_FAILED = re.compile(r"STEP\s+FAILED\b")


def step_report_problems(files: Dict[str, str]) -> List[str]:
    """9.9.1: the STEP lines are how Understudy knows which step worked. Two patterns make them lie:
    'STEP <n> OK' printed as the FIRST thing in step n (straight after its '# Step n' comment, before the action ran),
    and a bare 'STEP FAILED' with no step number (the failing step can't be told). Both are rejected."""
    out: List[str] = []
    for path, src in files.items():
        if not path.endswith(_CODE_EXT) or "understudy_governance" in path.rsplit("/", 1)[-1]:
            continue
        lines = (src or "").splitlines()
        early: List[int] = []
        for i, line in enumerate(lines):
            m = _STEP_OK_LINE.search(line)
            if not m:
                continue
            j = i - 1
            while j >= 0 and not lines[j].strip():
                j -= 1
            c = _STEP_COMMENT.match(lines[j]) if j >= 0 else None
            if c and c.group(1) == m.group(1):
                early.append(int(m.group(1)))
        if early:
            out.append(f"{path} prints 'STEP n OK' BEFORE doing step n (steps {', '.join(map(str, early[:8]))}) - print "
                       "it only after the step's action (and its wait / check) succeeded")
        if any(_UNNUMBERED_FAILED.search(l) for l in lines):
            out.append(f"{path} prints 'STEP FAILED' without a step number - keep the current step number in a "
                       "variable and print 'STEP <n> FAILED: <reason>' for the step that failed")
    return out


def _syntax_check(files: Dict[str, str], spec: FrameworkSpec) -> List[str]:
    errs = []
    for path, content in files.items():
        if path.endswith(".py"):
            try:
                compile(content, path, "exec")
            except SyntaxError as e:
                errs.append(f"{path}:{e.lineno}: {e.msg}")
        elif path.endswith((".js", ".cjs")) and shutil.which("node"):
            import tempfile
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
                fh.write(content)
                tmp = fh.name
            try:
                proc = subprocess.run(["node", "--check", tmp], capture_output=True, text=True, timeout=20)
                if proc.returncode != 0:
                    errs.append(f"{path}: {proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else 'syntax error'}")
            except Exception:
                pass
            finally:
                os.unlink(tmp)
    return errs


class AttemptRejected(ValueError):
    """9.0: the AI's code failed Understudy's gates; the build agent sends ``problems`` back for another round."""

    def __init__(self, problems: List[str], files: Dict[str, str]):
        super().__init__("; ".join(problems))
        self.problems = problems
        self.files = files


def pages_text(urls: List[str]) -> str:
    """9.4: the PAGES block of the code-generation prompt."""
    if len(urls) < 2:
        return f"1. {urls[0] if urls else '(target)'} - read from TARGET_URL (a one-page process)"
    return "\n".join(f"{i}. {u} - read from TARGET_URL_{i}" + (" (= TARGET_URL, the start page)" if i == 1 else "")
                     for i, u in enumerate(urls, 1))


def _governance_files(spec: FrameworkSpec, files: Dict[str, str], entry: str, params: List[Dict[str, Any]],
                      task_context: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """9.9: the SOP rules confirmed in Govern (governance.json) + the vetted checker for this tool's language."""
    from core.wizard import governance_runtime
    ctx = task_context or {}
    return governance_runtime.files_for(spec, ctx.get("rules") or [], params, ctx.get("sop") or "", files, entry)


def _governance_prompt(spec: FrameworkSpec, task_context: Optional[Dict[str, Any]]) -> str:
    from core.wizard import governance_runtime
    rules = governance_runtime.summary_lines((task_context or {}).get("rules") or [])
    return ("Rules (plain English):\n" + ("\n".join(f"- {r}" for r in rules) or "- (no SOP rules - the check still "
            "runs and prints 'GOVERN OK: 0 SOP rule(s)')") + "\n\nHow to check them:\n"
            + governance_runtime.ai_instructions(spec))


def _agentic_suite(spec: FrameworkSpec, steps: List[Dict[str, Any]], params: List[Dict[str, Any]],
                   api: Optional[Dict[str, Any]], urls: List[str], task_context: Optional[Dict[str, Any]],
                   feedback: Optional[List[str]] = None) -> Dict[str, Any]:
    """9.8: an agentic tool's suite is the recorded task + the tool's runner - no code generation, the AI acts at run
    time. The same safety / parameterisation scan as any suite."""
    n = len([s for s in steps if s.get("action") not in ("navigate", "wait")])
    if spec.template == "gemini-agent":            # 9.9: Gemini writes the agentic script for THIS process
        from core.wizard import agentic_script
        out = agentic_script.generate(steps, params, urls, task_context, feedback)
        files, entry = out["files"], out["entry_file"]
        if entry != spec.entry_file:
            spec.entry_file = entry
        meta = {"agent_source": out["agent_source"], "model": out["model"],
                "agent": {"rounds": out["rounds"], "status": "not run", "verified_locators": 0, "page_loads": 0}}
        notes = out["notes"]
    else:
        files, entry = templates.render(spec.template, steps, params, api, spec, page_urls=urls, context=task_context)
        meta = {"agent_source": "LOCAL_HEURISTIC", "model": f"{spec.name} task"}
        notes = []
    from core.wizard import governance_runtime
    rules = governance_runtime.summary_lines((task_context or {}).get("rules") or [])
    if entry in files and spec.template == "gemini-agent":   # 9.9: the SOP rules are part of the agentic script too
        files[entry] = ("# SOP rules from Understudy's Govern step (governance.json) - the agent checks payload.json "
                        "against them\n# before step 1 and stops on a violation:\n"
                        + ("".join(f"#   - {r}\n" for r in rules) or "#   (no SOP rules)\n") + files[entry])
    files.update(_governance_files(spec, files, entry, params, task_context))
    syntax = _syntax_check(files, spec)
    if syntax:
        raise ValueError("The agentic script is invalid: " + "; ".join(syntax))
    lock = dependency_lock.detect(spec, files)
    return dict(meta, files=files, entry_file=entry, generator="agentic", findings=scan_suite(files, params, urls, steps),
                extra_packages=lock["extras"],
                notes=notes + [f"Agentic script: Gemini performs the {len(steps)} recorded step(s) on the live page at "
                               f"run time ({n} decided by the AI). Values come from payload.json, secrets from "
                               "environment variables. In Understudy each run uses your organisation's Gemini; the exported ZIP runs with your own key."] + lock["notes"])


def generate_suite(spec: FrameworkSpec, steps: List[Dict[str, Any]], params: List[Dict[str, Any]],
                   api: Optional[Dict[str, Any]], target_url: Any, healing_notes: List[str],
                   page_profile: str = "", feedback: Optional[List[str]] = None,
                   allow_template: bool = True, complexity: str = "",
                   task_context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Returns {files, entry_file, generator, agent_source, model, notes, findings, locators, diagnosis}. Raises
    ValueError. 9.0: ``page_profile`` (the live page probe) and ``feedback`` (problems with the previous attempt, from
    the build / heal agent) go to the AI; ``allow_template=False`` raises AttemptRejected instead of falling back.
    9.4: ``target_url`` may be the list of pages the process visits; ``complexity`` is the assessment text the AI
    adapts the code to (``complexity.for_prompt``)."""
    from core.wizard.templates import page_tokens
    urls = [target_url] if isinstance(target_url, str) else [u for u in (target_url or []) if u]
    steps = page_tokens(steps, urls)             # navigate to page N -> ${TARGET_URL_N} (never a hard-coded address)
    if spec.category == "agentic" and spec.template:
        return _agentic_suite(spec, steps, params, api, urls, task_context, feedback)
    notes: List[str] = []
    key_view = [{"key": p["key"], "type": p.get("type", "text"), "label": p.get("label", "")} for p in params]
    res = generate_json("BUILD_GENERATE_SUITE",
                        {"framework": {k: v for k, v in spec.to_dict().items() if k not in ("pros", "cons", "beginner_summary")},
                         "steps": steps, "parameter_keys": key_view, "api_plan": api or {},
                         "healing_notes": healing_notes or ["(none)"],
                         "page_profile": page_profile or "(no live page profile)",
                         "pages": pages_text(urls), "complexity": complexity or "(not assessed)",
                         "agent_feedback": "\n".join(f"{i}. {f}" for i, f in enumerate(feedback or [], 1)) or "(first attempt)",
                         "governance": _governance_prompt(spec, task_context)},
                        untrusted=["page_profile", "agent_feedback", "complexity"])
    if res.ok:
        files = {}
        for f in res.data.get("files", []):
            path = str(f.get("path", "")).lstrip("./").strip()
            if path and path not in ("payload.json", ".env"):
                files[path] = str(f.get("content", ""))
        entry = str(res.data.get("entry_file") or spec.entry_file).lstrip("./")
        files.update(_governance_files(spec, files, entry, params, task_context))   # 9.9: Understudy's, never the AI's
        problems: List[str] = []
        if entry not in files:
            problems.append(f"entry file '{entry}' missing from AI output")
        findings = scan_suite(files, params, urls, steps)
        blocking = [f for f in findings if f["severity"] == "blocking"]
        if blocking:
            problems.append("; ".join(f"{b['kind']} in {b['file']}" for b in blocking[:5]))
        lock = dependency_lock.detect(spec, files, res.data.get("extra_packages"))
        if lock["untrusted"]:
            problems.append("needs packages outside the trusted list (" + ", ".join(lock["untrusted"][:6]) +
                            ") - request them as a tool in Mission Control if you really need them")
        syntax = _syntax_check(files, spec)
        if syntax:
            problems.append("syntax: " + "; ".join(syntax[:3]))
        problems += step_report_problems(files)                     # 9.9.1: STEP lines must tell the truth
        if any(p.lower().endswith((".robot", ".resource")) for p in files):
            from core.wizard import robot_syntax                    # 9.9.1: imports resolve, libraries behave
            files, _fixed = robot_syntax.repair_files(files)
            problems += robot_syntax.lint_imports(files) + robot_syntax.library_problems(files)
        from core.wizard import governance_runtime
        if not governance_runtime.uses_checker(files, spec):          # 9.9: Govern is part of the script
            problems.append("GOVERNANCE_MISSING: the code does not check the SOP rules (governance.json) before step 1")
        if not problems:
            if entry != spec.entry_file:
                spec.entry_file = entry
            learned = res.agent_source != "ACTUAL_AI_API"
            out_notes = [str(res.data.get("notes") or "")[:500]] if res.data.get("notes") else []
            if learned:
                out_notes.insert(0, f"No AI right now ({(res.recall or {}).get('why_no_ai', 'offline')}): re-used the "
                                    f"suite learned earlier for the same steps ({(res.recall or {}).get('label', '')}), "
                                    "re-checked by the safety and syntax gates.")
            locs = [l for l in (res.data.get("locators") or []) if isinstance(l, dict) and l.get("value")][:40]
            return {"files": files, "entry_file": entry, "generator": "learned" if learned else "ai",
                    "agent_source": res.agent_source, "model": res.model, "notes": out_notes + lock["notes"],
                    "findings": findings, "recall": res.recall, "extra_packages": lock["extras"], "locators": locs,
                    "diagnosis": str(res.data.get("diagnosis") or "")[:800]}
        if not allow_template:
            raise AttemptRejected(problems, files)
        notes.append(("Learned" if res.agent_source != "ACTUAL_AI_API" else "AI-generated") + " code was rejected by the safety/quality gate (" + " | ".join(problems)[:400] +
                     ") - falling back to the vetted template.")
    else:
        notes.append(f"AI code generation unavailable: {res.error}")
        if not allow_template:
            raise AttemptRejected([f"the AI could not answer: {res.error}"], {})
    if not spec.template:
        record_fallback("BUILD_GENERATE_SUITE", "; ".join(notes), None, f"{spec.name} has no offline template - build stopped.")
        raise ValueError(f"{spec.name} has no offline template, and AI code generation is not available "
                         f"({'; '.join(notes)}). Fix the AI problem or choose a framework marked 'offline template'.")
    files, entry = templates.render(spec.template, steps, params, api, spec, page_urls=urls)
    files.update(_governance_files(spec, files, entry, params, task_context))
    record_fallback("BUILD_GENERATE_SUITE", "; ".join(notes) or "AI unavailable",
                    {"entry_file": entry, "files": {k: v[:4000] for k, v in files.items()}},
                    f"Rendered the vetted offline template '{spec.template}'.")
    findings = scan_suite(files, params, urls, steps)
    syntax = _syntax_check(files, spec)
    if syntax:
        raise ValueError("Template produced invalid code: " + "; ".join(syntax))
    lock = dependency_lock.detect(spec, files)
    return {"files": files, "entry_file": entry, "generator": "template", "agent_source": "LOCAL_HEURISTIC",
            "model": f"Template '{spec.template}'", "notes": notes, "findings": findings,
            "extra_packages": lock["extras"]}
