"""Regenerates the prompt-registry sections of ARCHITECTURE.md from the source code.

    python scripts/generate_prompt_docs.py

* Wizard prompts come from the registry in core/wizard/prompts.py (single source of truth).
* The remaining non-registry prompt (the API key check) is copied verbatim out of the module that sends it, so
  the documentation can never drift from what the code actually sends to the model.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.wizard.prompts import SECURITY_PREAMBLE, render_prompt_catalog_markdown  # noqa: E402

# Prompts sent outside the wizard's prompt registry: only the Gemini key check in Mission Control > AI, security &
# account (core/wizard/api_keys.py).
LEGACY = [
    # (title, stage, file, start marker, end marker, call details, context window, expected output)
    ("API key validation ping", "Configuration (Mission Control)", "core/wizard/api_keys.py",
     "\"Respond with 'OK' only.\"", "\"Respond with 'OK' only.\"", "`validate_api_key()` · temperature 0.0 · max 5 tokens", "none", "`OK`"),
    # 9.8: the agentic run's prompts - sent by the stand-alone agent (run_agent.py) with the user's own key
    ("Agent step decision", "EXECUTE (agentic run, user's key)", "core/wizard/agent_runtime.py",
     "SYSTEM = (", '<one short sentence>\\"}"\n)', "`Brain.ask()` per step · temperature 0 · JSON · screenshot on a retry",
     "the step (action, label, payload key, non-secret value), every visible element (id, tag, role, label, state), "
     "page text excerpt (marked as data), the previous attempt's error", "`{action, element, value_key, text, reason}`"),
    ("Agent page question (agent.sees)", "EXECUTE (agentic run, user's key)", "core/wizard/agent_runtime.py",
     "SEES_SYSTEM = (", '<one sentence: what on the page shows it>\\"}"\n)',
     "`Agent.sees()` · temperature 0 · JSON · screenshot",
     "the script's yes/no question, page text (marked as data), a screenshot", "`{answer, evidence}`"),
]


def extract(path: str, start: str, end: str) -> str:
    with open(os.path.join(ROOT, path), encoding="utf-8") as fh:
        src = fh.read()
    i = src.find(start)
    if i < 0:
        raise SystemExit(f"start marker not found in {path}: {start[:60]}")
    j = src.find(end, i + (0 if start == end else len(start)))
    if j < 0:
        raise SystemExit(f"end marker not found in {path}: {end[:60]}")
    return src[i:j + len(end)].rstrip()


def legacy_markdown() -> str:
    out = []
    for title, stage, path, start, end, call, ctx, output in LEGACY:
        out.append(f"#### {title} — {stage}\n")
        out.append(f"*Source:* `{path}` · {call}  \n*Input context:* {ctx}  \n*Expected output:* {output}\n")
        out.append("```python\n" + extract(path, start, end) + "\n```\n")
    return "\n".join(out)


def main():
    arch = os.path.join(ROOT, "ARCHITECTURE.md")
    with open(arch, encoding="utf-8") as fh:
        doc = fh.read()
    wizard = "```text\n" + SECURITY_PREAMBLE + "\n```\n\n" + render_prompt_catalog_markdown()
    doc = re.sub(r"<!-- WIZARD_PROMPTS -->.*?<!-- /WIZARD_PROMPTS -->|<!-- WIZARD_PROMPTS -->",
                 lambda m: "<!-- WIZARD_PROMPTS -->\n" + wizard + "\n<!-- /WIZARD_PROMPTS -->", doc, flags=re.S)
    doc = re.sub(r"<!-- LEGACY_PROMPTS -->.*?<!-- /LEGACY_PROMPTS -->|<!-- LEGACY_PROMPTS -->",
                 lambda m: "<!-- LEGACY_PROMPTS -->\n" + legacy_markdown() + "\n<!-- /LEGACY_PROMPTS -->", doc, flags=re.S)
    with open(arch, "w", encoding="utf-8") as fh:
        fh.write(doc)
    print("ARCHITECTURE.md prompt sections regenerated")


if __name__ == "__main__":
    main()
