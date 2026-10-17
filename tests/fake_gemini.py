"""How the agentic-run tests' stand-in for Gemini decides (9.8; 9.9: used through the run's relay as the AGENT_STEP
answer of tests.wizard_helpers.FakeLLM).

It answers like a careful browser agent: for a step it picks the element whose label matches the step's field or
button; for ``agent.sees`` it answers yes unless the question is about a duplicate."""
from typing import Any, Dict


def _norm(s: Any) -> str:
    return " ".join(str(s or "").lower().replace("*", "").split())


def decide(prompt: Dict[str, Any]) -> Dict[str, Any]:
    if "question" in prompt:                                   # agent.sees(...)
        q = str(prompt["question"]).lower()
        return {"answer": "duplicate" not in q, "evidence": "what the page shows"}
    step = prompt["step"]
    label = _norm(step.get("field_or_button"))
    els = prompt.get("elements") or []
    want = {"fill": ("input", "textarea"), "select": ("select",), "check": ("input",), "click": ("button", "a", "input")}
    tags = want.get(step.get("do"), ())
    hit = next((e for e in els if e["tag"] in tags and label and _norm(e.get("name")) == label), None) or \
        next((e for e in els if e["tag"] in tags and label and label in _norm(e.get("name"))), None)
    if not hit:
        return {"action": "fail", "element": "", "value_key": "", "text": "", "reason": f"no element labelled {label}"}
    act = {"fill": "fill", "select": "select", "check": "check", "click": "click"}[step["do"]]
    return {"action": act, "element": hit["id"], "value_key": step.get("value_key") or "", "text": "",
            "reason": f"the {hit['tag']} labelled '{hit.get('name')}'"}
