"""BUILD for the agentic tools on Understudy's agent (9.8; 9.9: Understudy Gemini Agent and the agentic tools the admin
sets up on the same agent): instead of a selector script, the suite is an agentic script plus the vetted runtime.

* ``gemini-agent``  - 9.9: an agentic script for the process (``run_agent.py``; Gemini writes it in BUILD -
                      ``agentic_script.generate``; this template is the version written from the recorded steps) on
                      the vetted runtime library (``understudy_agent.py``). Gemini decides every action at run time.

Common contract (the same as the generated scripts): TARGET_URL / TARGET_URL_<n>, payload.json (UNDERSTUDY_PAYLOAD),
secrets as UNDERSTUDY_SECRET_<KEY>, "STEP <n> OK|FAILED" lines. The agent's Gemini key arrives only as
UNDERSTUDY_SECRET_GEMINI_API_KEY for the run (Understudy's Step 4) - never in a file. No value is written into the
code: the script holds the steps, labels and payload KEYS. The SOP rules come with it (governance.json, 9.9).
"""
from __future__ import annotations

from typing import Dict, Tuple

def _engine_source() -> str:
    from core.wizard import agentic_script
    return agentic_script.runtime_source()


def gemini_agent(steps, params, api, spec, context=None) -> Tuple[Dict[str, str], str]:
    """The agentic script written from the recorded steps (the AI-written one comes from agentic_script.generate)."""
    from core.wizard import agentic_script
    ctx = context or {}
    code = agentic_script.render(steps, ctx.get("process_name") or "", ctx.get("verification") or {},
                                 ctx.get("difficulty"))
    return {spec.entry_file: code, agentic_script.RUNTIME_FILE: _engine_source()}, spec.entry_file


TEMPLATES = {"gemini-agent": gemini_agent}


def is_vetted_runner(content: str) -> bool:
    """9.8: the fixed agent runners never contain payload values - their words ("label", "success", "model" ...) must
    not be read as a hard-coded value. A modified runner is scanned like any code."""
    return content == _engine_source()
