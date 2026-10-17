"""Framework specs: ANY automation tool, described declaratively and validated strictly.

There is no hardcoded list of *allowed* tools. ``CATALOG`` is a set of trusted, pre-vetted
*hints* (with offline code templates) that the WATCH step can suggest without an AI key.
Any other tool the user or the AI names is turned into a ``FrameworkSpec`` by the
``FRAMEWORK_RESOLVE`` agent and then passes the same validation:

  * ecosystem must be one of pip | npm | maven | gem | go | nuget | none,
  * package names / versions must match the ecosystem's grammar (blocks shell/argument injection),
  * run / post-install commands are argv lists (never shell strings) whose first token is an
    allow-listed interpreter and whose tokens contain no shell metacharacters,
  * entry-file paths must be relative with no ``..``.

Packages outside the trusted set are marked ``requires_approval``: the UI shows them to the user
(typosquatting / supply-chain protection) and nothing is installed until the user approves.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple

ECOSYSTEMS = ("pip", "npm", "maven", "gradle", "gem", "go", "nuget", "none")      # gradle: 8.2

RUNNER_ALLOWLIST = {"python", "python3", "node", "npx", "robot", "pytest", "mvn", "java", "ruby", "bundle", "go",
                    "dotnet", "behave", "k6", "newman", "bash", "gem", "gradle"}

_PKG_RE = {
    "pip": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}(\[[A-Za-z0-9,_-]{1,60}\])?$"),
    "npm": re.compile(r"^(@[a-z0-9][a-z0-9._~-]{0,60}/)?[a-z0-9][a-z0-9._~-]{0,100}$"),
    "maven": re.compile(r"^[A-Za-z0-9_.-]{1,100}:[A-Za-z0-9_.-]{1,100}$"),
    "gradle": re.compile(r"^[A-Za-z0-9_.-]{1,100}:[A-Za-z0-9_.-]{1,100}$"),       # Maven coordinates
    "gem": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$"),
    "go": re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}(/[A-Za-z0-9._~-]+){1,6}$"),
    "nuget": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,100}$"),
}
_VERSION_RE = re.compile(r"^[A-Za-z0-9.*^~<>=!, _+-]{0,40}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_./:@=,+{}%-]{1,200}$")
_ENTRY_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]{0,150}$")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
_INLINE_CODE_FLAGS = {"-c", "-e", "--eval", "-p", "--print", "--require", "-r", "--import", "--loader"}


AGENT_KEYS = ("driver", "browser", "model")


@dataclass
class Package:
    name: str
    version: str = ""

    def pip_spec(self) -> str:
        v = self.version.strip()
        if not v:
            return self.name
        return self.name + (v if v[0] in "<>=!~" else f"=={v}")


@dataclass
class FrameworkSpec:
    id: str
    name: str
    language: str
    ecosystem: str
    packages: List[Package]
    run_command: List[str]
    entry_file: str
    category: str = "browser"
    beginner_summary: str = ""
    pros: List[str] = field(default_factory=list)
    cons: List[str] = field(default_factory=list)
    difficulty: str = "beginner"
    recommended: bool = False
    post_install: List[List[str]] = field(default_factory=list)
    template: Optional[str] = None          # id of the offline template, if any
    trusted: bool = False                   # every package is in the trusted set
    requires_approval: bool = False
    untrusted_packages: List[str] = field(default_factory=list)
    source: str = "catalog"                 # catalog | ai | user
    # 7.2: where the packages come from, set by the admin's AI tool setup: {"index_url": "https://mirror/...",
    # "local_dir": "/opt/wheels"} - a private registry / mirror, or a directory of package files on the server.
    sources: Dict[str, str] = field(default_factory=dict)
    # 9.9: an agentic tool built on Understudy's agent - its browser stack ({"driver": "playwright" | "selenium",
    # "browser": ..., "model": default Gemini model}); empty for every other tool.
    agent: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["packages"] = [asdict(p) for p in self.packages]
        d["template_available"] = bool(self.template)
        return d

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "FrameworkSpec":
        pk = [Package(name=str(p.get("name", "")), version=str(p.get("version", "") or "")) if isinstance(p, dict)
              else Package(name=str(p)) for p in d.get("packages", [])]
        return FrameworkSpec(
            id=str(d.get("id", "")), name=str(d.get("name", "")), language=str(d.get("language", "")),
            ecosystem=str(d.get("ecosystem", "none")), packages=pk,
            run_command=[str(t) for t in d.get("run_command", [])], entry_file=str(d.get("entry_file", "")),
            category=str(d.get("category", "other")), beginner_summary=str(d.get("beginner_summary", "")),
            pros=[str(x) for x in d.get("pros", [])][:6], cons=[str(x) for x in d.get("cons", [])][:6],
            difficulty=str(d.get("difficulty", "intermediate")), recommended=bool(d.get("recommended", False)),
            post_install=[[str(t) for t in c] for c in d.get("post_install", []) if isinstance(c, list)],
            template=d.get("template"), trusted=bool(d.get("trusted", False)),
            requires_approval=bool(d.get("requires_approval", False)),
            untrusted_packages=list(d.get("untrusted_packages", [])), source=str(d.get("source", "ai")),
            sources={k: str(v) for k, v in (d.get("sources") or {}).items() if k in SOURCE_KEYS and v},
            agent={k: str(v)[:60] for k, v in (d.get("agent") or {}).items() if k in AGENT_KEYS and v}
            if isinstance(d.get("agent"), dict) else {},
        )


def _fw(**kw) -> FrameworkSpec:
    kw["packages"] = [Package(*p) for p in kw["packages"]]
    kw.setdefault("trusted", True)
    return FrameworkSpec(**kw)


CATALOG: Dict[str, FrameworkSpec] = {f.id: f for f in [
    _fw(id="playwright-python", name="Playwright (Python)", language="Python", ecosystem="pip", category="browser",
        packages=[("playwright", ">=1.42")], post_install=[["python", "-m", "playwright", "install", "chromium"]],
        run_command=["python", "{entry}"], entry_file="run_automation.py", template="playwright-python",
        difficulty="beginner",
        beginner_summary="Drives a real Chrome browser for you. It waits for pages automatically, so scripts break less. "
                         "Great first choice if you know a little Python.",
        pros=["Auto-waits for elements", "Very readable Python", "One command installs the browser"],
        cons=["Needs a browser download (~150 MB) the first time"]),
    _fw(id="selenium-python", name="Selenium WebDriver (Python)", language="Python", ecosystem="pip", category="browser",
        packages=[("selenium", ">=4.15")], run_command=["python", "{entry}"], entry_file="run_automation.py",
        template="selenium-python", difficulty="beginner",
        beginner_summary="The classic, most widely used browser automation library. Lots of tutorials online.",
        pros=["Huge community", "Works with every browser"], cons=["You must think about waits yourself"]),
    _fw(id="playwright-js", name="Playwright (JavaScript)", language="JavaScript", ecosystem="npm", category="browser",
        packages=[("playwright", "^1.47.0")], post_install=[["npx", "playwright", "install", "chromium"]],
        run_command=["node", "{entry}"], entry_file="run_automation.js", template="playwright-js",
        difficulty="beginner",
        beginner_summary="Same powerful browser automation as Playwright Python, written in JavaScript/Node.js.",
        pros=["Auto-waits", "Fits web developers who know JS"], cons=["Needs Node.js 18+"]),
    _fw(id="puppeteer", name="Puppeteer", language="JavaScript", ecosystem="npm", category="browser",
        packages=[("puppeteer", "^23.0.0")], run_command=["node", "{entry}"], entry_file="run_automation.js",
        template="puppeteer", difficulty="intermediate",
        beginner_summary="Google's library for controlling Chrome from Node.js. Simple and fast for Chrome-only tasks.",
        pros=["Official Chrome tooling", "Fast"], cons=["Chrome/Firefox only", "Fewer built-in waits than Playwright"]),
    _fw(id="cypress", name="Cypress", language="JavaScript", ecosystem="npm", category="browser",
        packages=[("cypress", "^13.0.0")], run_command=["npx", "cypress", "run", "--spec", "{entry}"],
        entry_file="cypress/e2e/automation.cy.js", template="cypress", difficulty="intermediate",
        beginner_summary="A friendly end-to-end testing tool with a visual test runner that shows every step.",
        pros=["Great visual debugger", "Popular with front-end teams"], cons=["Large download", "One browser tab at a time"]),
    _fw(id="robot-framework", name="Robot Framework (SeleniumLibrary)", language="Robot", ecosystem="pip", category="rpa",
        packages=[("robotframework", ">=7.0"), ("robotframework-seleniumlibrary", ">=6.2")],
        run_command=["robot", "--outputdir", "results", "{entry}"], entry_file="automation.robot",
        template="robot-framework", difficulty="beginner",
        beginner_summary="Write automation as plain-English keywords in a table. Popular for RPA and QA teams.",
        pros=["Readable by non-programmers", "Nice HTML reports"], cons=["Keyword syntax is new to learn"]),
    _fw(id="requests-api", name="Python Requests (REST API)", language="Python", ecosystem="pip", category="api",
        packages=[("requests", ">=2.31")], run_command=["python", "{entry}"], entry_file="run_api.py",
        template="requests-api", difficulty="beginner",
        beginner_summary="Skips the screen and talks to the application's API directly. Fastest and most stable, "
                         "but only works when the app has an API.",
        pros=["Very fast", "No browser needed"], cons=["Needs an API endpoint and token"]),
    _fw(id="rest-assured", name="REST Assured (Java)", language="Java", ecosystem="maven", category="api",
        packages=[("io.rest-assured:rest-assured", "5.4.0"), ("org.junit.jupiter:junit-jupiter", "5.10.2"),
                  ("com.fasterxml.jackson.core:jackson-databind", "2.17.1")],
        run_command=["mvn", "-q", "test"], entry_file="src/test/java/automation/AutomationTest.java",
        template="rest-assured", difficulty="intermediate",
        beginner_summary="The standard Java library for API testing, run as a JUnit test with Maven.",
        pros=["Industry standard in Java shops", "Fluent readable assertions"], cons=["Needs Java 17 + Maven"]),
    _fw(id="webdriverio", name="WebdriverIO", language="JavaScript", ecosystem="npm", category="browser",
        packages=[("webdriverio", "^9.0.0")], run_command=["node", "{entry}"], entry_file="run_automation.js",
        template=None, difficulty="intermediate",
        beginner_summary="A flexible Node.js automation framework (code is written by the AI agent).",
        pros=["Very extensible"], cons=["More setup options to understand"]),
    _fw(id="selenium-java", name="Selenium WebDriver (Java)", language="Java", ecosystem="maven", category="browser",
        packages=[("org.seleniumhq.selenium:selenium-java", "4.21.0"), ("org.junit.jupiter:junit-jupiter", "5.10.2")],
        run_command=["mvn", "-q", "test"], entry_file="src/test/java/automation/AutomationTest.java", template=None,
        difficulty="intermediate", beginner_summary="Selenium for Java teams (code is written by the AI agent).",
        pros=["Common in enterprise Java"], cons=["More verbose than Python"]),
    # 9.8/9.9: the AGENTIC tool - built into Understudy (no admin approval or setup): Gemini writes an agentic script
    # and performs its steps on the live page at run time; in Understudy each run uses the organisation's Gemini (agent_relay); the ZIP uses the user's own key.
    _fw(id="gemini-agent", name="Understudy Gemini Agent", language="Python", ecosystem="pip", category="agentic",
        packages=[("google-genai", ">=1.0.0"), ("playwright", ">=1.42")],
        post_install=[["python", "-m", "playwright", "install", "chromium"]],
        run_command=["python", "{entry}"], entry_file="run_agent.py", template="gemini-agent", difficulty="beginner",
        agent={"driver": "playwright", "browser": "chromium"},
        beginner_summary="Understudy's own AI agent: Gemini reads the live page at every step, picks the field or "
                         "button by its meaning, and adapts when the page changes. Built in - nothing for your admin to set up.",
        pros=["Survives changed layouts and moved fields", "Reads the page itself - no selectors to maintain",
              "Secret values never reach the model"],
        cons=["Each run calls Gemini - needs your API key", "Slower and less deterministic than a script"]),
]}

TRUSTED_PACKAGES: Dict[str, set] = {eco: set() for eco in ECOSYSTEMS}
for _f in CATALOG.values():
    for _p in _f.packages:
        TRUSTED_PACKAGES[_f.ecosystem].add(_p.name.lower())
TRUSTED_PACKAGES["pip"].update({"pytest", "behave", "pytest-playwright", "requests", "robotframework-browser",
                                "pillow", "python-dotenv", "robotframework-requests",
                                # 8.5.3: keyword libraries Robot suites commonly import (Library    JSONLibrary)
                                "robotframework-jsonlibrary", "robotframework-seleniumlibrary"})
TRUSTED_PACKAGES["npm"].update({"@playwright/test", "testcafe", "nightwatch", "chromedriver", "mocha", "chai",
                                "@wdio/cli", "@wdio/local-runner", "@wdio/mocha-framework", "axios", "newman", "dotenv"})
TRUSTED_PACKAGES["maven"].update({"org.testng:testng", "com.fasterxml.jackson.core:jackson-databind",
                                  "io.github.bonigarcia:webdrivermanager", "com.microsoft.playwright:playwright"})
TRUSTED_PACKAGES["gradle"].update(TRUSTED_PACKAGES["maven"])                     # same Maven Central artifacts
TRUSTED_PACKAGES["gem"].update({"selenium-webdriver", "capybara", "watir", "rspec"})


class FrameworkValidationError(ValueError):
    pass


SOURCE_KEYS = ("index_url", "local_dir", "interpreter")
# which package sources each ecosystem's installer can use (pip: --index-url / --find-links; npm: .npmrc registry;
# maven: <repositories>; gem: --source)
# 8.0: "interpreter" - the Python executable the tool's isolated venv is created with (admin: "use the local python3.10")
SOURCE_SUPPORT = {"pip": {"index_url", "local_dir", "interpreter"}, "npm": {"index_url"}, "maven": {"index_url"}, "gradle": {"index_url"}, "gem": {"index_url"},
                  "go": set(), "nuget": set(), "none": set()}
_INDEX_RE = re.compile(r"^https?://[A-Za-z0-9.-]+(:\d{1,5})?(/[A-Za-z0-9._~%/+-]*)?$")
_DIR_RE = re.compile(r"^(/|[A-Za-z]:[\\/])[A-Za-z0-9 ._/\\-]{0,300}$")


_INTERP_NAME_RE = re.compile(r"^python3(\.\d{1,2})?$")


def validate_interpreter(v: str) -> List[str]:
    """An absolute path to a python executable, or a bare python3 / python3.X looked up on PATH."""
    import os
    import shutil
    v = (v or "").strip()
    if _INTERP_NAME_RE.match(v):
        return [] if shutil.which(v) else [f"'{v}' is not installed on the Understudy server (not on PATH)"]
    if not _DIR_RE.match(v) or ".." in v:
        return ["The interpreter must be an absolute path to a Python executable (or python3 / python3.X)"]
    base = os.path.basename(v).lower()
    if not re.match(r"^python(3(\.\d{1,2})?)?(\.exe)?$", base):
        return [f"'{base}' doesn't look like a Python interpreter (python3, python3.10, python.exe)"]
    if not (os.path.isfile(v) and os.access(v, os.X_OK)):
        return [f"{v} does not exist on the Understudy server or is not executable"]
    return []


def resolve_interpreter(v: str) -> str:
    import shutil
    return (shutil.which(v) or v) if _INTERP_NAME_RE.match(v or "") else v


def validate_sources(spec: FrameworkSpec) -> List[str]:
    problems: List[str] = []
    for k, v in (spec.sources or {}).items():
        if k not in SOURCE_KEYS:
            problems.append(f"Unknown package source '{k}'")
            continue
        if k not in SOURCE_SUPPORT.get(spec.ecosystem, set()):
            problems.append(f"A {k.replace('_', ' ')} is not supported for {spec.ecosystem} packages")
            continue
        if k == "index_url":
            if not _INDEX_RE.match(v):
                problems.append("The package index must be a plain http(s) URL without a user name or password "
                                "(put credentials in pip.conf / .npmrc / settings.xml on the server)")
                continue
            from urllib.parse import urlsplit
            host = (urlsplit(v).hostname or "").lower()
            try:
                from core.wizard import admin_policy
                if host in admin_policy.NEVER_EXCEPTABLE_HOSTS or admin_policy.is_host_blocked(host):
                    problems.append(f"The package index host {host} is blocked")
            except Exception:
                pass
        if k == "interpreter":
            problems += validate_interpreter(v)
            continue
        if k == "local_dir" and (not _DIR_RE.match(v) or ".." in v):
            problems.append("The package directory must be an absolute path on the Understudy server (no '..')")
    return problems


def default_entry_name(spec: FrameworkSpec) -> str:
    """The file name Understudy uses when a run command points at a folder (8.2)."""
    cmd = " ".join(spec.run_command).lower()
    if spec.ecosystem in ("maven", "gradle"):
        return "AutomationTest.java"
    if spec.ecosystem == "npm":
        if "cypress" in cmd:
            return "automation.cy.js"
        if "playwright" in cmd and " test" in cmd or "jest" in cmd or "vitest" in cmd or "mocha" in cmd:
            return "automation.spec.js"
        return "run_automation.js"
    if spec.ecosystem == "pip":
        if "pytest" in cmd:
            return "test_automation.py"
        if "robot" in cmd:
            return "automation.robot"
        if "behave" in cmd:
            return "automation.feature"
        return "run_automation.py"
    return "run_automation"


def normalize_entry(spec: FrameworkSpec) -> FrameworkSpec:
    """8.2 field fix: the AI sometimes names a FOLDER (e.g. Playwright Test's 'tests/') or a glob as the entry file.
    Understudy needs one real file it can write and check, so the folder gets a file inside it
    ('tests/' -> 'tests/automation.spec.js'); the run command still receives that path."""
    e = (spec.entry_file or "").strip().replace("\\", "/")
    if not e:
        return spec
    last = e.rstrip("/").split("/")[-1]
    if e.endswith("/") or "*" in e or "." not in last:
        folder = e.rstrip("/").split("*")[0].rstrip("/")
        if spec.ecosystem in ("maven", "gradle") and not folder.startswith("src/test/java"):
            folder = "src/test/java/automation"
        spec.entry_file = (folder + "/" if folder else "") + default_entry_name(spec)
    return spec


def ensure_browser_download(spec: FrameworkSpec) -> FrameworkSpec:
    """Playwright needs its own browser build after the package install; a stack the AI composed without that step
    would fail every smoke test with "Executable doesn't exist" (8.2)."""
    names = {p.name for p in spec.packages}
    has_install = any("playwright" in " ".join(c) and "install" in c for c in spec.post_install)
    if not has_install:
        if spec.ecosystem == "npm" and names & {"@playwright/test", "playwright", "playwright-core"}:
            spec.post_install = list(spec.post_install) + [["npx", "playwright", "install", "chromium"]]
        elif spec.ecosystem == "pip" and names & {"playwright", "pytest-playwright"}:
            spec.post_install = list(spec.post_install) + [["python", "-m", "playwright", "install", "chromium"]]
    return spec


def validate_spec(spec: FrameworkSpec) -> FrameworkSpec:
    problems: List[str] = []
    normalize_entry(spec)
    ensure_browser_download(spec)
    if not _ID_RE.match(spec.id or ""):
        spec.id = re.sub(r"[^a-z0-9-]+", "-", (spec.name or spec.id or "custom").lower()).strip("-")[:40] or "custom-tool"
    if spec.ecosystem not in ECOSYSTEMS:
        problems.append(f"Unsupported package ecosystem '{spec.ecosystem}'")
    if len(spec.packages) > 15:
        problems.append("Too many packages (max 15)")
    eco_re = _PKG_RE.get(spec.ecosystem)
    for p in spec.packages:
        if eco_re and not eco_re.match(p.name):
            problems.append(f"Package name '{p.name}' is not valid for {spec.ecosystem}")
        if not _VERSION_RE.match(p.version or ""):
            problems.append(f"Version '{p.version}' of {p.name} is not valid")
    for cmd_list, label in [([spec.run_command], "run command")] + [([c], "post-install command") for c in spec.post_install]:
        for cmd in cmd_list:
            if not cmd:
                problems.append(f"Empty {label}")
                continue
            if cmd[0] not in RUNNER_ALLOWLIST:
                problems.append(f"{label} must start with one of {sorted(RUNNER_ALLOWLIST)} (got '{cmd[0]}')")
            for tok in cmd:
                if not _TOKEN_RE.match(tok) or ".." in tok or tok.startswith("/"):
                    problems.append(f"Unsafe token in {label}: '{tok}'")
                if tok.lower().startswith(("http:", "https:", "file:", "git+")) or tok in _INLINE_CODE_FLAGS:
                    problems.append(f"Disallowed token in {label}: '{tok}' (inline code / remote sources are not allowed)")
    if not _ENTRY_RE.match(spec.entry_file or "") or ".." in spec.entry_file:
        problems.append(f"Entry file '{spec.entry_file}' must be a simple relative path")
    problems += validate_sources(spec)
    if problems:
        raise FrameworkValidationError("; ".join(problems))
    trusted = TRUSTED_PACKAGES.get(spec.ecosystem, set())
    spec.untrusted_packages = [p.name for p in spec.packages if p.name.lower() not in trusted]
    spec.trusted = not spec.untrusted_packages
    spec.requires_approval = bool(spec.untrusted_packages)
    if spec.template and spec.template not in CATALOG_TEMPLATE_IDS:
        spec.template = None
    return spec


CATALOG_TEMPLATE_IDS = {f.template for f in CATALOG.values() if f.template}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def match_catalog(name: str) -> Optional[FrameworkSpec]:
    n = _norm(name)
    if not n:
        return None
    best, score = None, 0.0
    for f in CATALOG.values():
        for cand in (f.id, f.name, f.name.split("(")[0]):
            c = _norm(cand)
            s = 1.0 if c == n else SequenceMatcher(None, n, c).ratio()
            if n in c or c in n:
                s = max(s, 0.85)
            if s > score:
                best, score = f, s
    return FrameworkSpec.from_dict(best.to_dict()) if best and score >= 0.8 else None


def resolve_custom_framework(tool_name: str, app_type: str = "web_ui") -> Tuple[Optional[FrameworkSpec], Dict[str, Any]]:
    """Resolve any user-typed tool. Returns (spec | None, meta{agent_source, message, ...})."""
    from core.wizard.guardrails import screen_text
    from core.wizard.llm import generate_json

    verdict = screen_text(tool_name, "TOOL_NAME")
    if not verdict.is_safe:
        return None, {"blocked": True, "message": verdict.friendly_message, "pattern": verdict.pattern}
    name = verdict.sanitized.strip()
    hit = match_catalog(name)
    res = generate_json("FRAMEWORK_RESOLVE", {"tool_name": name, "app_type": app_type}, untrusted=["tool_name"])
    if res.ok:
        try:
            spec = FrameworkSpec.from_dict(res.data)
            spec.source = "ai"
            if hit and _norm(hit.id) == _norm(spec.id):
                spec.template = hit.template
            validate_spec(spec)
            return spec, {"agent_source": res.agent_source, "model": res.model,
                          "message": ("Learned skill" if res.recall else "AI") + f" resolved '{name}' to {spec.name} ({spec.ecosystem})."}
        except FrameworkValidationError as e:
            if hit:
                return hit, {"agent_source": "LOCAL_HEURISTIC", "message": f"AI spec rejected ({e}); using trusted catalog entry."}
            return None, {"agent_source": res.agent_source, "message": f"The AI's setup for '{name}' failed safety validation: {e}"}
    from core.wizard.llm import ai_available, record_fallback
    if hit:
        record_fallback("FRAMEWORK_RESOLVE", res.error or "AI result not usable", {"matched": hit.id},
                        "Matched against the trusted offline catalog.")
        return hit, {"agent_source": "LOCAL_HEURISTIC", "message": f"Matched '{name}' to trusted catalog entry {hit.name}."}
    record_fallback("FRAMEWORK_RESOLVE", res.error or "AI result not usable", None, f"'{name}' is not in the offline catalog.")
    how = ("The AI could not set it up: " + str(res.error)) if ai_available() else \
        "An admin can add a Gemini API key in Mission Control so the AI can set up any tool."
    return None, {"agent_source": "LOCAL_HEURISTIC",
                  "message": f"'{name}' isn't in the offline catalog. {how} Or pick one of the suggested frameworks.",
                  "ai_error": res.error}


def suggest_offline(app_type: str, has_api: bool) -> List[FrameworkSpec]:
    order = ["playwright-python", "selenium-python", "cypress", "playwright-js", "puppeteer", "robot-framework"]
    if app_type == "api":
        order = ["requests-api", "rest-assured", "playwright-python"]
    elif has_api:
        order = order[:4] + ["requests-api", "robot-framework"]
    out = []
    for i, fid in enumerate(order):
        spec = FrameworkSpec.from_dict(CATALOG[fid].to_dict())
        spec.recommended = i == 0
        spec.source = "catalog"
        out.append(spec)
    return out
