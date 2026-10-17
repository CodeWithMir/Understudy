"""BUILD for Understudy Gemini Agent (9.9): Gemini writes an AGENTIC SCRIPT for the recorded process.

The suite is two files:

* ``run_agent.py`` - written by Gemini (``AGENTIC_SCRIPT``) for THIS process and shown in Step 2 like any generated
  code: one ``agent.step(...)`` per recorded step with the goal in plain words, the field / button it is about, the
  payload key, hints from the recording and what the page should show afterwards; ``agent.sees(...)`` decisions where
  the recording or the SOP shows the path can change (9.9: no final screenshot check - the steps decide the
  result). Nothing in it is a selector:
  at run time Gemini decides every action on the live page, so the script adapts to the page.
* ``understudy_agent.py`` - the vetted runtime library (``agent_runtime.py``), copied unchanged; it holds the guard
  rails (actions must suit the step, values only from the step's payload key, secrets never shown to the model,
  elements only from the page, a call budget).

The AI's script must pass ``check_script`` (an AST allow-list: only ``Agent`` / ``run`` from the library, only the
library's methods, literal arguments, ``if agent.sees(...)`` as the only control flow, every recorded step present
with its recorded action and payload key, no addresses) plus the usual safety scan. A rejected script goes back to
Gemini with the problems; when the AI is unavailable or keeps failing, ``render`` writes the same kind of script from
the recorded steps (still agentic - Gemini acts at run time).
"""
from __future__ import annotations

import ast
import json
import os
import re
from typing import Any, Dict, List, Optional

from core.wizard.llm import generate_json, record_fallback

RUNTIME_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent_runtime.py")
RUNTIME_FILE = "understudy_agent.py"
SCRIPT_FILE = "run_agent.py"
METHODS = {"open", "step", "sees", "wait", "note"}
STEP_KWARGS = {"target", "value_key", "hints", "expect", "page", "ms", "key"}
MAX_ROUNDS = 2
MAX_LINES = 400


def runtime_source() -> str:
    with open(RUNTIME_PATH, encoding="utf-8") as fh:
        return fh.read()


def _q(s: Any) -> str:
    return json.dumps("" if s is None else str(s), ensure_ascii=False)


def _page_of(step: Dict[str, Any]) -> int:
    m = re.match(r"^\$\{TARGET_URL_(\d+)\}$", str(step.get("value") or ""))
    return int(m.group(1)) if m else 1


def _hints_for(step: Dict[str, Any], difficulty: Optional[Dict[str, Any]]) -> str:
    hints = []
    hot = {p.get("library_key") or p.get("key"): p.get("effective_value", p.get("value", 0))
           for p in (difficulty or {}).get("parameters") or []}
    if (hot.get("dom_stability") or 0) >= 6:
        hints.append("ids and positions change between loads - go by the visible label and meaning"
                     + (f" ('{step['label']}' in the recording; it may be worded slightly differently)"
                        if step.get("label") else ""))
    if (hot.get("async_loading") or 0) >= 6 and step.get("action") in ("click", "fill", "select"):
        hints.append("the page builds late - wait if the element is not there yet")
    return "; ".join(hints)


def render(steps: List[Dict[str, Any]], process_name: str, verification: Dict[str, Any],
           difficulty: Optional[Dict[str, Any]] = None) -> str:
    """The agentic script written from the recorded steps (no AI at build time; Gemini still acts at run time)."""
    lines = ["# Agentic script for the recorded process - written by Understudy from the recorded steps.",
             "# Every agent.step() is performed by Gemini on the live page at run time (understudy_agent.py): it finds",
             "# the field or button by its meaning, types the value from payload.json and adapts when the page changes.",
             "from understudy_agent import Agent, run",
             "",
             f"PROCESS = {_q(process_name or 'Recorded process')}",
             "",
             "",
             "def main():",
             "    with Agent(process=PROCESS) as agent:"]
    for st in steps:
        n, a = st.get("order"), st.get("action")
        goal = st.get("description") or f"{a} {st.get('label') or ''}".strip()
        args = [str(n), _q(goal), f"action={_q(a)}"]
        if a == "navigate":
            args.append(f"page={_page_of(st)}")
        elif a == "wait":
            try:
                args.append(f"ms={max(0, min(10000, int(float(st.get('value') or 1000))))}")
            except (TypeError, ValueError):
                args.append("ms=1000")
        else:
            if st.get("label"):
                args.append(f"target={_q(st['label'])}")
            if st.get("param_key"):
                args.append(f"value_key={_q(st['param_key'])}")
            if a == "press":
                args.append(f"key={_q(st.get('value') or 'Enter')}")
            h = _hints_for(st, difficulty)
            if h:
                args.append(f"hints={_q(h)}")
        one = f"        agent.step({', '.join(args)})"
        if len(one) <= 112:
            lines.append(one)
        else:                                     # long calls: one keyword per line, like a person would write it
            lines.append(f"        agent.step({', '.join(args[:3])},")
            lines += [f"                   {a}," for a in args[3:-1]] + [f"                   {args[-1]})"]
    lines += ["", "", 'if __name__ == "__main__":', "    run(main)", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The gate for an AI-written script
# ---------------------------------------------------------------------------
def _literal(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, (str, int, float, bool)) or node.value is None
    if isinstance(node, (ast.List, ast.Tuple)):
        return all(_literal(e) for e in node.elts)
    if isinstance(node, ast.Name):
        return node.id == "PROCESS"
    return False


def _const(node: Optional[ast.AST]) -> Any:
    return node.value if isinstance(node, ast.Constant) else None


def check_script(code: str, steps: List[Dict[str, Any]]) -> List[str]:
    """Problems with an AI-written agentic script ([] = accepted)."""
    problems: List[str] = []
    if len(code.splitlines()) > MAX_LINES:
        return [f"the script is longer than {MAX_LINES} lines"]
    if re.search(r"(?i)\b(https?|ftp|file)://", code):
        problems.append("the script contains an address - pages are opened with agent.step(n, ..., action='navigate', "
                        "page=<n>) / agent.open(page=<n>), never a URL")
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return problems + [f"syntax error line {e.lineno}: {e.msg}"]
    seen_steps: Dict[int, Dict[str, Any]] = {}
    main_found = False

    def check_body(body: List[ast.stmt], depth: int) -> None:
        for st in body:
            if isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant) and isinstance(st.value.value, str):
                continue                                                   # a docstring / comment string
            if isinstance(st, ast.Expr) and isinstance(st.value, ast.Call):
                check_call(st.value)
            elif isinstance(st, ast.If) and depth < 3:
                t = st.test.operand if isinstance(st.test, ast.UnaryOp) and isinstance(st.test.op, ast.Not) else st.test
                if not (isinstance(t, ast.Call) and isinstance(t.func, ast.Attribute) and t.func.attr == "sees"
                        and isinstance(t.func.value, ast.Name) and t.func.value.id == "agent"):
                    problems.append(f"line {st.lineno}: an if may only test agent.sees(\"...\")")
                else:
                    check_call(t)
                check_body(st.body, depth + 1)
                check_body(st.orelse, depth + 1)
            elif isinstance(st, ast.Pass):
                continue
            else:
                problems.append(f"line {getattr(st, 'lineno', '?')}: only agent.<method>(...) calls and "
                                f"'if agent.sees(...)' are allowed inside main() (found {type(st).__name__})")

    def check_call(call: ast.Call) -> None:
        f = call.func
        if not (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id == "agent"
                and f.attr in METHODS):
            problems.append(f"line {call.lineno}: only agent.{'/'.join(sorted(METHODS))}(...) may be called")
            return
        if not all(_literal(a) for a in call.args) or not all(_literal(k.value) for k in call.keywords):
            problems.append(f"line {call.lineno}: arguments must be plain text / numbers / lists")
            return
        if f.attr == "step":
            if len(call.args) < 2 or not isinstance(_const(call.args[0]), int):
                problems.append(f"line {call.lineno}: agent.step(n, goal, action=...) needs the step number first")
                return
            kw = {k.arg: _const(k.value) for k in call.keywords}
            bad = [k for k in kw if k not in STEP_KWARGS | {"action"}]
            if bad:
                problems.append(f"line {call.lineno}: unknown argument(s) {bad}")
            seen_steps.setdefault(_const(call.args[0]), dict(kw, line=call.lineno))

    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue
        if isinstance(node, ast.ImportFrom) and node.module == "understudy_agent" \
                and all(a.name in ("Agent", "run") and not a.asname for a in node.names):
            continue
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) \
                and node.targets[0].id == "PROCESS" and isinstance(_const(node.value), str):
            continue
        if isinstance(node, ast.FunctionDef) and node.name == "main" and not node.args.args and not node.decorator_list:
            main_found = True
            body = [b for b in node.body if not (isinstance(b, ast.Expr) and isinstance(b.value, ast.Constant))]
            ok = (len(body) == 1 and isinstance(body[0], ast.With) and len(body[0].items) == 1)
            if ok:
                item = body[0].items[0]
                c = item.context_expr
                ok = (isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == "Agent"
                      and not c.args and all(k.arg == "process" and _literal(k.value) for k in c.keywords)
                      and isinstance(item.optional_vars, ast.Name) and item.optional_vars.id == "agent")
            if not ok:
                problems.append("main() must be exactly: with Agent(process=PROCESS) as agent: <steps>")
            else:
                check_body(body[0].body, 0)
            continue
        if isinstance(node, ast.If) and isinstance(node.test, ast.Compare) and \
                ast.unparse(node.test).replace("'", '"') == '__name__ == "__main__"' and len(node.body) == 1 and \
                ast.unparse(node.body[0]) == "run(main)":
            continue
        problems.append(f"line {getattr(node, 'lineno', '?')}: not allowed at the top level "
                        f"({type(node).__name__}) - only the import, PROCESS, main() and run(main)")
    if not main_found:
        problems.append("no main() function")
    for st in steps:
        n, a = st.get("order"), st.get("action")
        got = seen_steps.get(n)
        if not got:
            problems.append(f"recorded step {n} ({a}: {st.get('description') or st.get('label') or ''}) is missing")
            continue
        if got.get("action") != a:
            problems.append(f"line {got['line']}: step {n} must keep the recorded action '{a}' (got '{got.get('action')}')")
        if a in ("fill", "select", "check") and st.get("param_key") and got.get("value_key") != st["param_key"]:
            problems.append(f"line {got['line']}: step {n} must use value_key='{st['param_key']}'")
        if got.get("value_key") and got.get("value_key") != st.get("param_key"):
            problems.append(f"line {got['line']}: step {n} has no payload key '{got['value_key']}'")
    extra = sorted(set(k for k in seen_steps if isinstance(k, int)) - {s.get("order") for s in steps})
    if extra:
        problems.append(f"steps {extra} were not recorded - use agent.note()/agent.sees() for extra checks")
    return problems


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------
API_DOC = """from understudy_agent import Agent, run          # the ONLY import

PROCESS = "<process name>"

def main():
    with Agent(process=PROCESS) as agent:
        agent.step(n, "<goal in plain words>", action="<recorded action>",
                   target="<field / button as the recording shows it>", value_key="<payload key or omit>",
                   hints="<what the recording shows about this step: where it is, how it looks, what may differ>",
                   expect="<what the page should show afterwards, optional>",
                   page=<n for action='navigate'>, ms=<ms for action='wait'>, key="<key for action='press'>")
        if agent.sees("<yes/no question about the current page>"):   # only where the path can really change
            agent.step(...)  / agent.note("...")

if __name__ == "__main__":
    run(main)
"""


def generate(steps: List[Dict[str, Any]], params: List[Dict[str, Any]], urls: List[str],
             context: Optional[Dict[str, Any]], feedback: Optional[List[str]] = None) -> Dict[str, Any]:
    """{files, entry_file, generator, agent_source, model, notes, rounds}. Never raises for AI problems."""
    ctx = context or {}
    ver = ctx.get("verification") or {}
    view = [{k: st.get(k) for k in ("order", "action", "description", "label", "param_key")
             if st.get(k) not in (None, "")} | ({"page": _page_of(st)} if st.get("action") == "navigate" else {})
            | ({"value": st.get("value")} if st.get("action") in ("press", "wait") else {}) for st in steps]
    keys = [{"key": p["key"], "type": p.get("type", "text"), "label": p.get("label", "")} for p in params]
    diff = ctx.get("difficulty") or {}
    hard = [f"{p['name']} {p.get('effective_value', p.get('value'))}/10: {p.get('why') or '; '.join(p.get('evidence') or [])}"
            for p in sorted(diff.get("parameters") or [], key=lambda x: -x.get("contribution", 0))[:6]]
    fb = list(feedback or [])
    rounds: List[Dict[str, Any]] = []
    for n in range(1, MAX_ROUNDS + 1):
        res = generate_json("AGENTIC_SCRIPT", {
            "process": json.dumps({"name": ctx.get("process_name") or "", "summary": ctx.get("summary") or ""}),
            "steps": json.dumps(view, indent=1, ensure_ascii=False),
            "parameter_keys": json.dumps(keys),
            "pages": "\n".join(f"{i}. page {i}" for i in range(1, len(urls) + 1)) or "1. page 1",
            "verification": json.dumps({"method": ver.get("method"), "success_text": ver.get("success_text"),
                                        "expect_param_keys": ver.get("expect_param_keys")}),
            "assessment": "\n".join(hard) or "(not assessed)",
            "sop": (ctx.get("sop") or "")[:1500],
            "api": API_DOC,
            "agent_feedback": "\n".join(f"{i}. {f}" for i, f in enumerate(fb, 1)) or "(first attempt)"},
            untrusted=["process", "steps", "sop", "agent_feedback"])
        if not res.ok:
            rounds.append({"round": n, "result": f"AI unavailable: {res.error}"})
            break
        code = str((res.data or {}).get("script") or "").replace("\r\n", "\n").strip() + "\n"
        problems = check_script(code, steps)
        if not problems:
            rounds.append({"round": n, "result": "accepted"})
            return {"files": {SCRIPT_FILE: code, RUNTIME_FILE: runtime_source()}, "entry_file": SCRIPT_FILE,
                    "generator": "agentic", "agent_source": res.agent_source, "model": res.model, "rounds": rounds,
                    "notes": [str((res.data or {}).get("notes") or "")[:500]] if (res.data or {}).get("notes") else []}
        rounds.append({"round": n, "result": f"{len(problems)} problem(s)", "problems": problems[:6]})
        fb = list(feedback or []) + [f"Understudy's gate rejected your script: {p}" for p in problems[:8]] + \
            ["Your previous script:\n" + code[:6000]]
    code = render(steps, ctx.get("process_name") or "", ver, diff)
    why = "; ".join(r["result"] for r in rounds) or "AI unavailable"
    record_fallback("AGENTIC_SCRIPT", why, {"rounds": rounds}, "Wrote the agentic script from the recorded steps.")
    return {"files": {SCRIPT_FILE: code, RUNTIME_FILE: runtime_source()}, "entry_file": SCRIPT_FILE,
            "generator": "agentic", "agent_source": "LOCAL_HEURISTIC", "model": "Agentic script from the recorded steps",
            "rounds": rounds, "notes": [f"Gemini did not write the script ({why[:200]}) - Understudy wrote it from the "
                                        "recorded steps; Gemini still decides every action at run time."]}

