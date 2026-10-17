"""Agentic tools built on Understudy's own agent (9.9) - the admin's "agentic tool builder".

Understudy Gemini Agent (Python + Playwright + Chromium) is built in. In Mission Control the admin can set up more
agentic tools on the SAME agent - the same Gemini decisions, guard rails, secret masking and agentic script - with a
different browser stack, like a traditional tool stack:

* browser library: Playwright (Chromium, Chrome, Edge, Firefox, WebKit) or Selenium WebDriver (Chrome, Edge, Firefox);
* the library's version and the Gemini SDK (google-genai) version;
* extra pip packages (e.g. a company proxy helper), and the default Gemini model.

``create`` composes the spec (``compose``) and runs the normal tool setup (``core/wizard/tool_setup.py``): install in
the sandbox, then the agent smoke test - Understudy's own, fixed test (``smoke_files``): the agent opens the smoke page,
types "Understudy" into Name, clicks Go and checks the result, in self-test mode (decisions by label matching, no
Gemini key needed) - and the tool is ACTIVE only when it passes. Users then see it in the agentic column next to the
built-in agent; its runs get ``UNDERSTUDY_AGENT_DRIVER`` / ``UNDERSTUDY_AGENT_BROWSER`` (``run_env``) and the exported
ZIP carries them in ``.env.example``.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from core.wizard.frameworks import FrameworkSpec, Package, validate_spec

DRIVERS: Dict[str, Dict[str, Any]] = {
    "playwright": {
        "label": "Playwright", "package": "playwright", "default_version": ">=1.42",
        "browsers": {"chromium": "Chromium (downloaded by Playwright)", "chrome": "Google Chrome (installed on the machine)",
                     "msedge": "Microsoft Edge (installed on the machine)", "firefox": "Firefox (downloaded by Playwright)",
                     "webkit": "WebKit / Safari engine (downloaded by Playwright)"},
        "download": {"chromium", "firefox", "webkit"},           # `python -m playwright install <browser>`
    },
    "selenium": {
        "label": "Selenium WebDriver", "package": "selenium", "default_version": ">=4.25",
        "browsers": {"chrome": "Google Chrome", "edge": "Microsoft Edge", "firefox": "Firefox"},
        "download": set(),                                         # Selenium Manager finds / downloads the driver
    },
}
GENAI = ("google-genai", ">=1.0.0")
LANGUAGE = "Python"                    # the agent runtime is Python; the stack is the browser side
_PIP_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}(\[[A-Za-z0-9_,-]{1,40}\])?$")
_MODEL = re.compile(r"^[a-z0-9][a-z0-9.\-]{2,60}$")
MAX_EXTRA = 8


class StackError(ValueError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def options() -> Dict[str, Any]:
    """What the builder offers (Mission Control)."""
    return {"language": LANGUAGE, "genai_package": GENAI[0],
            "drivers": [{"id": k, "label": v["label"], "package": v["package"], "default_version": v["default_version"],
                         "browsers": [{"id": b, "label": lbl} for b, lbl in v["browsers"].items()]}
                        for k, v in DRIVERS.items()]}


def _version(v: str, what: str) -> str:
    from core.wizard import tool_versions
    v = (v or "").strip()
    if not v:
        return ""
    if re.match(r"^(>=|<=|==|~=|>|<)?\s*[0-9][0-9A-Za-z.*+-]{0,30}$", v):
        v = v.replace(" ", "")
        return "==" + v if v[0].isdigit() else v                 # a plain version pins it (and becomes the default)
    try:
        return tool_versions.validate_version(v)
    except ValueError:
        raise StackError(f"'{v}' is not a version for {what} (use 1.56.0 or >=1.42)")


def compose(driver: str, browser: str, name: str = "", driver_version: str = "", genai_version: str = "",
            extra_packages: Optional[List[str]] = None, model: str = "") -> FrameworkSpec:
    """The FrameworkSpec of an agentic tool on Understudy's agent. Raises StackError on bad input."""
    driver = (driver or "").strip().lower()
    browser = (browser or "").strip().lower()
    if driver not in DRIVERS:
        raise StackError(f"Choose a browser library: {', '.join(DRIVERS)}.")
    d = DRIVERS[driver]
    if browser not in d["browsers"]:
        raise StackError(f"{d['label']} works with: {', '.join(d['browsers'])}.")
    dv = _version(driver_version, d["label"]) or d["default_version"]
    gv = _version(genai_version, GENAI[0]) or GENAI[1]
    pkgs = [Package(d["package"], dv), Package(GENAI[0], gv)]
    seen = {p.name.lower() for p in pkgs}
    for raw in (extra_packages or [])[:MAX_EXTRA + 1]:
        raw = str(raw or "").strip()
        if not raw:
            continue
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._\[\],-]*?)\s*((?:==|>=|<=|~=|>|<)\s*[0-9][0-9A-Za-z.*+-]*)?$", raw)
        if not m or not _PIP_NAME.match(m.group(1)):
            raise StackError(f"'{raw[:60]}' is not a pip package (name, optionally with ==version)")
        if m.group(1).lower() in seen:
            continue
        seen.add(m.group(1).lower())
        pkgs.append(Package(m.group(1), (m.group(2) or "").replace(" ", "")))
    if len(pkgs) - 2 > MAX_EXTRA:
        raise StackError(f"At most {MAX_EXTRA} extra packages.")
    model = (model or "").strip()
    if model and not _MODEL.match(model):
        raise StackError(f"'{model[:40]}' is not a Gemini model name (e.g. gemini-flash-latest)")
    post = [["python", "-m", "playwright", "install", browser]] if driver == "playwright" and browser in d["download"] else []
    label = name.strip()[:60] or f"Understudy Agent ({d['label']} + {browser.capitalize()})"
    slug = re.sub(r"[^a-z0-9]+", "-", (name.strip().lower() or f"{driver}-{browser}")).strip("-")
    slug = re.sub(r"^(understudy-)?(gemini-)?agent-?", "", slug) or f"{driver}-{browser}"
    sid = "understudy-agent-" + slug[:30].strip("-")
    spec = FrameworkSpec(
        id=sid, name=label, language=LANGUAGE, ecosystem="pip", packages=pkgs, run_command=["python", "{entry}"],
        entry_file="run_agent.py", category="agentic", template="gemini-agent", difficulty="beginner", source="admin",
        post_install=post, agent={"driver": driver, "browser": browser, **({"model": model} if model else {})},
        beginner_summary=f"Understudy's own AI agent on {d['label']} with {d['browsers'][browser]}: Gemini reads the "
                         "live page at every step and decides each action. Set up by your admin.",
        pros=["Same agent and guard rails as the built-in one", f"Runs on {d['label']} / {browser}"],
        cons=["Each run calls Gemini - needs your API key", "Slower and less deterministic than a script"])
    if driver == "playwright" and browser not in d["download"]:
        spec.post_install = []                     # Chrome / Edge: the machine's own install (Playwright channel)
    validate_spec(spec)
    if driver == "playwright" and browser not in d["download"]:
        spec.post_install = []                     # validate_spec adds a Chromium download for any Playwright stack
    return spec


def is_understudy_agent(tool: Any) -> bool:
    """An agentic tool that runs on Understudy's agent (the built-in one or one the admin set up) - the only agentic
    tools Understudy offers."""
    d = tool.to_dict() if isinstance(tool, FrameworkSpec) else (tool or {})
    return (d.get("category") or "").lower() == "agentic" and d.get("template") == "gemini-agent"


def stack_of(spec_or_dict: Any) -> Dict[str, str]:
    """{driver, browser, model?} of an agentic tool (the built-in one: Playwright + Chromium)."""
    d = spec_or_dict.to_dict() if isinstance(spec_or_dict, FrameworkSpec) else (spec_or_dict or {})
    a = dict(d.get("agent") or {})
    a.setdefault("driver", "playwright")
    a.setdefault("browser", "chromium" if a["driver"] == "playwright" else "chrome")
    return a


def run_env(spec_or_dict: Any) -> Dict[str, str]:
    """The environment an agentic run of this tool gets (and its .env.example shows)."""
    a = stack_of(spec_or_dict)
    return {"UNDERSTUDY_AGENT_DRIVER": a["driver"], "UNDERSTUDY_AGENT_BROWSER": a["browser"]}


SMOKE_SCRIPT = '''# Understudy's agent smoke test (9.9): proves this agentic tool's browser stack. The agent runs in SELF-TEST mode
# (decisions by label matching - no Gemini call, no API key): open the smoke page, type the name, click Go, and check
# the page text reads CLICKED:Understudy.
import os

os.environ.setdefault("TARGET_URL", os.environ.get("SMOKE_URL", ""))
os.environ["UNDERSTUDY_AGENT_SELFTEST"] = "1"

from understudy_agent import Agent, run  # noqa: E402


def main():
    with Agent(process="Understudy agent smoke test") as agent:
        agent.step(1, "Open the smoke page", action="navigate", page=1)
        agent.step(2, "Type the name", action="fill", target="Name", value_key="name")
        agent.step(3, "Click Go", action="click", target="Go")
        agent.step(4, "Check the result", action="assert_text", target="CLICKED:Understudy")


if __name__ == "__main__":
    run(main)
'''


def smoke_files(spec: FrameworkSpec) -> Dict[str, str]:
    """Understudy's own smoke test for an agentic tool (fixed - not AI-written, so no admin review is needed)."""
    from core.wizard import agentic_script, dependency_resolver, governance_runtime
    files = {spec.entry_file: SMOKE_SCRIPT, agentic_script.RUNTIME_FILE: agentic_script.runtime_source(),
             "payload.json": json.dumps({"name": "Understudy"}, indent=2)}
    files.update(governance_runtime.files_for(spec, [], [{"key": "name"}], "", files, spec.entry_file))   # no rules
    files.update(dependency_resolver.manifest_files(spec))
    return files


def create(admin: str, driver: str, browser: str, name: str = "", driver_version: str = "", genai_version: str = "",
           extra_packages: Optional[List[str]] = None, model: str = "", background: bool = True) -> Dict[str, Any]:
    """Creates the setup request for a new agentic tool and starts it (install -> agent smoke test -> ACTIVE)."""
    from core.wizard import admin_policy as ap, tool_setup
    spec = compose(driver, browser, name, driver_version, genai_version, extra_packages, model)
    if ap.is_tool_allowed(spec.id):
        raise StackError(f"An agentic tool called '{spec.name}' already exists - give this one another name.", 409)
    blk = ap.tool_block_for(spec)
    if blk and not blk.get("versions"):
        raise StackError(f"{spec.name} uses a blocked tool ({blk['name']}).", 403)
    req = ap.request_tool(spec, "", reason="Agentic tool set up by the admin (Understudy agent)", source="admin",
                          requested_by=admin, typed_name=spec.name)
    if not req.get("id"):
        raise StackError(f"{spec.name} is already being set up.", 409)
    dv = spec.packages[0].version
    pinned = dv[2:] if dv.startswith("==") else ""
    plan = {"spec": spec.to_dict(), "versions": {"allowed": [pinned] if pinned else [], "default": pinned or None},
            "manifest": {"os_dependencies": [], "env_vars": run_env(spec), "path_prepend": []},
            "summary": f"{spec.name}: Understudy's agent on {DRIVERS[spec.agent['driver']]['label']} with "
                       f"{spec.agent['browser']}; packages "
                       + ", ".join(f"{p.name} {p.version}".strip() for p in spec.packages)
                       + ". Installed in the sandbox, then Understudy's agent smoke test (self-test mode, no key).",
            "agent_source": "LOCAL_HEURISTIC", "agentic": True}
    ap.update_request(req["id"], plan=plan, status="plan_ready")
    ap.add_message(req["id"], "admin", "agentic_tool", f"Set up {spec.name}: " + plan["summary"])
    try:
        return tool_setup.apply_plan(req["id"], admin, background=background)
    except tool_setup.SetupError as e:
        raise StackError(str(e), getattr(e, "status_code", 400))
