"""Post-setup smoke tests (8.0): a tool becomes ACTIVE only after a minimal script written for it passes in the sandbox.

The contract every smoke test gets (env vars, never hard-coded):

* ``SMOKE_URL`` - a local dummy page: an input ``#name``, a button ``#go`` and a ``#status`` paragraph that reads
  ``READY`` and changes to ``CLICKED:<name>`` when the button is clicked.
* ``SMOKE_API_URL`` - a local dummy API: ``GET {SMOKE_API_URL}/ping.json`` -> ``{"status": "ok", ...}``.

Browser tools must open the page, type ``Understudy``, click, assert ``CLICKED:Understudy`` and close the browser; API
tools must call the ping endpoint and assert ``status == ok``. Exit code 0 = pass. The page is served on 127.0.0.1 by
a throw-away server inside the sandbox.

The AI writes the test (``TOOL_SMOKE_TEST``, 8.2: always - there are no templates); the admin reviews it, and
``validate_files`` checks it before it runs.
"""
from __future__ import annotations

import functools
import http.server
import json
import os
import re
import tempfile
import threading
from typing import Any, Dict, List, Optional, Tuple

from core.wizard import dependency_resolver
from core.wizard.frameworks import FrameworkSpec
from core.wizard.sandbox.base import BaseSandboxRunner, ExecResult

EXPECTED = "CLICKED:Understudy"
SMOKE_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Understudy smoke test</title></head>
<body>
  <h1 id="title">Understudy smoke test</h1>
  <label for="name">Name</label> <input id="name" name="name" placeholder="Your name">
  <button id="go" type="button"
    onclick="document.getElementById('status').textContent='CLICKED:'+(document.getElementById('name').value||'')">Go</button>
  <p id="status">READY</p>
</body></html>
"""
PING = {"status": "ok", "service": "understudy-smoke"}

# 8.2: no built-in smoke-test templates. Nothing is pre-built for any framework (not even Playwright): the smoke test
# is always written by the AI (TOOL_SMOKE_TEST) for exactly the stack the admin selected, the admin reviews the code,
# and only then does it run. Dependency manifests are never taken from the AI - Understudy writes them, so the smoke
# test proves exactly what users will install.
MANIFEST_FILES = {"pom.xml", "package.json", "package-lock.json", "requirements.txt", "Gemfile", "Gemfile.lock",
                  "go.mod", "go.sum", "Automation.csproj", "build.gradle", "settings.gradle"}


def understudy_manifests(spec: FrameworkSpec, java_release: Optional[int] = None) -> Dict[str, str]:
    """The dependency manifest(s) Understudy writes next to the smoke test (Maven needs pom.xml in the run folder)."""
    if spec.ecosystem == "maven":
        return {"pom.xml": dependency_resolver.pom_xml(spec, java_release)}
    if spec.ecosystem == "gradle":
        return {"build.gradle": dependency_resolver.build_gradle(spec, java_release),
                "settings.gradle": "rootProject.name = 'understudy-automation'\n"}
    return {}


# ---------------------------------------------------------------------------------------------------------------
# Validation of an AI-written smoke test
# ---------------------------------------------------------------------------------------------------------------
_PATH_RX = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]{0,150}$")
_FORBIDDEN = [
    (re.compile(r"\b(subprocess|os\.system|os\.popen|os\.exec\w*|pty\.spawn)\b"), "starts other programs"),
    (re.compile(r"child_process|require\(['\"](?:child_process|net|dgram|cluster)['\"]\)"), "starts other programs"),
    (re.compile(r"Runtime\.getRuntime\(\)\.exec|ProcessBuilder"), "starts other programs"),
    (re.compile(r"\b(eval|exec)\s*\(\s*(?!\))"), "evaluates dynamic code"),
    (re.compile(r"\b(rm\s+-rf|shutil\.rmtree|fs\.rm(Sync)?\(|unlink)"), "deletes files"),
    (re.compile(r"https?://(?!127\.0\.0\.1|localhost)[A-Za-z0-9.-]+"), "contacts an external host"),
    (re.compile(r"(?i)(api[_-]?key|password|secret|token)\s*[:=]\s*['\"][^'\"]{6,}"), "contains a credential"),
    (re.compile(r"(?i)os\.environ(?!\[\s*['\"]SMOKE_|\.get\(\s*['\"]SMOKE_)|process\.env\.(?!SMOKE_)\w+"),
     "reads environment variables other than SMOKE_URL / SMOKE_API_URL"),
    (re.compile(r"\bCypress\.(?:env|expose)\s*\(|\bcy\.env\s*\("),
     "uses Cypress.env() / Cypress.expose() / cy.env() (removed in Cypress 16, missing before 15.10) - read SMOKE_URL "
     "in cypress.config.js and pass it to the spec with cy.task"),
]


def validate_files(files: Dict[str, str], spec: FrameworkSpec) -> List[str]:
    problems = []
    if not files:
        return ["no files"]
    if len(files) > 8:
        problems.append("more than 8 files")
    for path, content in files.items():
        if not _PATH_RX.match(path) or ".." in path.split("/"):
            problems.append(f"bad file path {path!r}")
        if len(content) > 30_000:
            problems.append(f"{path} is longer than 30,000 characters")
        if path.split("/")[-1] in MANIFEST_FILES:
            continue
        for rx, why in _FORBIDDEN:
            m = rx.search(content)
            if m:
                problems.append(f"{path} {why} ({m.group(0)[:60]!r})")
    if spec.entry_file and spec.entry_file not in files and spec.ecosystem not in ("maven", "gradle"):
        problems.append(f"the entry file {spec.entry_file} is missing - write the test in exactly that file "
                        f"(it is run with: {' '.join(spec.run_command).replace('{entry}', spec.entry_file)})")
    from core.wizard import robot_syntax          # 8.4.1: a Robot keyword without its arguments never reaches a run
    for path, content in files.items():
        if path.lower().endswith((".robot", ".resource")):
            problems += [f"{path} {p}" for p in robot_syntax.lint(content)[:4]]
    blob = "\n".join(files.values())
    if "SMOKE_URL" not in blob and "SMOKE_API_URL" not in blob:
        problems.append("it never uses SMOKE_URL or SMOKE_API_URL")
    return problems


# ---------------------------------------------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------------------------------------------
class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def write_assets(directory: str) -> None:
    os.makedirs(os.path.join(directory, "smoke", "api"), exist_ok=True)
    with open(os.path.join(directory, "smoke", "smoke.html"), "w", encoding="utf-8") as fh:
        fh.write(SMOKE_HTML)
    with open(os.path.join(directory, "smoke", "api", "ping.json"), "w", encoding="utf-8") as fh:
        json.dump(PING, fh)


def run(runner: BaseSandboxRunner, spec: FrameworkSpec, files: Dict[str, str],
        timeout_s: Optional[int] = None) -> Tuple[ExecResult, Dict[str, Any]]:
    """Runs the smoke test in ``runner`` (environment already installed). Returns (result, info)."""
    argv = list(spec.run_command)
    from core.wizard import robot_syntax          # 8.4.1: also repairs a test reviewed before this check existed
    files, repaired = robot_syntax.repair_files(files)
    info: Dict[str, Any] = {"runner": runner.kind, "files": sorted(files)}
    if repaired:
        info["robot_repairs"] = repaired
    directory = tempfile.mkdtemp(prefix="smoke-", dir=getattr(runner, "scratch", None) or tempfile.gettempdir())
    write_assets(directory)
    handler = functools.partial(_Quiet, directory=os.path.join(directory, "smoke"))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    env = {"SMOKE_URL": f"{base}/smoke.html", "SMOKE_API_URL": f"{base}/api",
           "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}
    info["smoke_url"] = env["SMOKE_URL"]
    try:
        res = runner.execute_script(files, argv, env=env, timeout_s=timeout_s, cwd=directory)
    finally:
        server.shutdown()
        server.server_close()
    return res, info
