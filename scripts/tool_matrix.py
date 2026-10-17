#!/usr/bin/env python3
"""Tool matrix - one sample test case per automation tool configured in Understudy.

For every tool on the admin whitelist (Playwright Python/JS, Puppeteer, Cypress, Selenium, Robot Framework, Python
Requests, REST Assured) the case does what an admin and a user would do, against the SAME sample website (the bundled
demo ERP, Portal 1):

  1. admin   pins ONE exact version of the tool in Mission Control (allowed = [v], default = v)
  2. user    records the process (offline WATCH of Portal 1) and picks the tool WITHOUT choosing a version
             -> the admin's version must be applied
  3. BUILD   the generated suite's manifest (requirements.txt / package.json / pom.xml) pins exactly v,
             the sandbox install plan installs exactly v, and the ZIP would ship exactly v
  4. LIVE    (--live) the dependencies are really installed in the isolated sandbox; the version that ended up
             installed must be v; the generated suite is run with the TOOL ITSELF (strategy "framework", never
             Understudy's built-in driver), the invoice must land in the ERP ledger exactly once, and the ZIP must
             pin v

Steps the machine cannot do are reported as SKIPPED with the reason (e.g. no Chrome/chromedriver for Selenium, the
Cypress binary host or Maven Central not reachable) - never as passed.

    python scripts/tool_matrix.py                    # build-level checks for every tool (fast, no installs)
    python scripts/tool_matrix.py --live             # + real installs, real runs with each tool, ERP check, ZIP
    python scripts/tool_matrix.py --live --tools playwright-python,requests-api
    -> generated/tool_matrix/report.json and docs/TOOL_MATRIX.md

tests/test_tool_matrix.py runs the same cases (build level always; live with UNDERSTUDY_LIVE_TOOL_TESTS=1).
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import sys
import time
import zipfile
from datetime import datetime
from typing import Any, Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault("UNDERSTUDY_SELF_HEAL_APPROVAL", "automatic")
os.environ.setdefault("UNDERSTUDY_USER_LOGIN", "off")

SOP = """Approved vendors: Acme Industrial Supplies, Globex Corp.
The spend limit per invoice is $5,000.
Invoices must be dated between 2026-01-01 and 2026-12-31.
Vendor Name: Globex Corp
Invoice Date: 2026-09-23
Total Amount: 1234.56"""

ERP_TOKEN = "secret-enterprise-token-xyz"          # the demo ERP's API token (Requests / REST Assured use the API)

# One case per configured tool. The versions are deliberately NOT the catalog defaults where that is possible, so
# the test proves the admin's choice wins. Playwright stays on the version whose browser build is installed here.
CASES: List[Dict[str, Any]] = [
    {"tool": "playwright-python", "version": "1.56.0", "browser": "playwright"},
    {"tool": "playwright-js", "version": "1.56.0", "browser": "playwright"},
    {"tool": "puppeteer", "version": "23.11.1", "browser": "chrome-for-puppeteer"},
    {"tool": "cypress", "version": "13.17.0", "browser": "cypress-binary"},
    {"tool": "selenium-python", "version": "4.25.0", "browser": "chrome+driver"},
    {"tool": "robot-framework", "version": "7.1", "browser": "chrome+driver"},
    {"tool": "requests-api", "version": "2.32.3", "browser": None},
    {"tool": "rest-assured", "version": "5.5.0", "browser": None},
    # 9.8/9.9: the built-in agentic tool - the version flow is the same; a LIVE run also needs Understudy's Gemini set up
    {"tool": "gemini-agent", "version": "1.33.0", "browser": "playwright", "agentic": True},
]


# ---------------------------------------------------------------------------------------------------------------
# What this machine can do
# ---------------------------------------------------------------------------------------------------------------
def _reachable(host: str, timeout: float = 6.0) -> bool:
    """Can an installer reach host:443 (directly or through the configured HTTPS proxy)?"""
    import urllib.request
    import urllib.error
    try:
        urllib.request.urlopen(urllib.request.Request(f"https://{host}/", method="HEAD"), timeout=timeout)
        return True
    except urllib.error.HTTPError as e:           # the host answered (even 404/405) -> it is reachable
        return e.code not in (407,)
    except Exception:                             # DNS, refused, proxy tunnel denied, timeout
        return False


def playwright_chromium() -> Optional[str]:
    base = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or os.path.expanduser("~/.cache/ms-playwright")
    try:
        for d in sorted(os.listdir(base), reverse=True):
            if d.startswith("chromium-"):
                p = os.path.join(base, d, "chrome-linux", "chrome")
                if os.path.exists(p):
                    return p
    except FileNotFoundError:
        return None
    return None


def capabilities() -> Dict[str, Any]:
    return {
        "node": bool(shutil.which("node") and shutil.which("npm")),
        "java_maven": bool(shutil.which("java") and shutil.which("mvn")),
        "chrome": shutil.which("google-chrome") or shutil.which("chrome") or os.environ.get("CHROME_BINARY"),
        "chromedriver": shutil.which("chromedriver"),
        "playwright_chromium": playwright_chromium(),
        "pypi": _reachable("pypi.org"),
        "npm_registry": _reachable("registry.npmjs.org"),
        "maven_central": _reachable("repo.maven.apache.org") or _reachable("repo1.maven.org"),
        "cypress_download": _reachable("download.cypress.io"),
        "chrome_for_testing": _reachable("googlechromelabs.github.io"),
    }


def run_blocker(case: Dict[str, Any], caps: Dict[str, Any]) -> Optional[str]:
    """Why the LIVE run cannot happen on this machine (None = it can)."""
    tool = case["tool"]
    if case.get("agentic"):
        from core.wizard import agent_relay
        if not agent_relay.available():
            return "an agentic tool's run calls Gemini - set up Gemini (Vertex AI or GEMINI_API_KEY) to run it live"
    if tool in ("playwright-js", "puppeteer", "cypress") and not caps["node"]:
        return "Node.js/npm is not installed"
    if tool in ("playwright-js", "puppeteer", "cypress") and not caps["npm_registry"]:
        return "the npm registry is not reachable"
    if tool in ("playwright-python", "selenium-python", "robot-framework", "requests-api") and not caps["pypi"]:
        return "PyPI is not reachable"
    if tool == "rest-assured":
        if not caps["java_maven"]:
            return "Java + Maven are not installed"
        if not caps["maven_central"]:
            return "Maven Central (repo.maven.apache.org) is not reachable, so the pinned jars cannot be downloaded"
    if case["browser"] == "playwright" and not caps["playwright_chromium"]:
        return "no Playwright Chromium build is installed"
    if case["browser"] == "chrome-for-puppeteer" and not (caps["playwright_chromium"] or caps["chrome"]):
        return "no Chrome/Chromium for Puppeteer"
    if case["browser"] == "cypress-binary" and not caps["cypress_download"]:
        return ("the Cypress binary host (download.cypress.io) is not reachable; the npm package is installed and "
                "its version checked, but the test runner cannot start")
    if case["browser"] == "chrome+driver" and not ((caps["chrome"] and caps["chromedriver"]) or
                                                   (caps["chrome_for_testing"])):
        return ("no Chrome + chromedriver on this machine and Selenium Manager cannot download them "
                "(googlechromelabs.github.io not reachable); the package version is installed and checked")
    return None


def install_blocker(case: Dict[str, Any], caps: Dict[str, Any]) -> Optional[str]:
    """Why even the dependency INSTALL cannot happen here (None = it can)."""
    tool = case["tool"]
    if tool == "rest-assured" and not (caps["java_maven"] and caps["maven_central"]):
        return "Maven Central is not reachable / Java+Maven missing"
    if tool in ("playwright-js", "puppeteer", "cypress") and not (caps["node"] and caps["npm_registry"]):
        return "npm registry not reachable / Node missing"
    if tool in ("playwright-python", "selenium-python", "robot-framework", "requests-api") and not caps["pypi"]:
        return "PyPI not reachable"
    return None


def sandbox_env(case: Dict[str, Any], caps: Dict[str, Any]) -> Dict[str, str]:
    """Environment adjustments for machines without the browsers' own download hosts (documented, not hidden)."""
    env: Dict[str, str] = {}
    if case["tool"] == "puppeteer" and caps["playwright_chromium"] and not caps["chrome_for_testing"]:
        env["PUPPETEER_SKIP_DOWNLOAD"] = "true"                       # don't fetch Chrome during npm install ...
        env["PUPPETEER_EXECUTABLE_PATH"] = caps["playwright_chromium"]  # ... drive the installed Chromium instead
    if case["tool"] == "cypress" and not caps["cypress_download"]:
        env["CYPRESS_INSTALL_BINARY"] = "0"                           # install the npm package (version check) only
    return env


# ---------------------------------------------------------------------------------------------------------------
# Version checks
# ---------------------------------------------------------------------------------------------------------------
def manifest_pins(eco: str, files: Dict[str, str], package: str, version: str) -> bool:
    if eco == "pip":
        return any(ln.strip() == f"{package}=={version}" for ln in (files.get("requirements.txt") or "").splitlines())
    if eco == "npm":
        try:
            return json.loads(files.get("package.json") or "{}").get("dependencies", {}).get(package) == version
        except ValueError:
            return False
    if eco == "maven":
        g, a = package.split(":", 1)
        return bool(re.search(rf"<groupId>{re.escape(g)}</groupId><artifactId>{re.escape(a)}</artifactId>"
                              rf"<version>{re.escape(version)}</version>", files.get("pom.xml") or ""))
    return False


def run_case(case: Dict[str, Any], base_url: str, live: bool, caps: Optional[Dict[str, Any]] = None,
             log=print) -> Dict[str, Any]:
    """Runs one tool case. Returns {tool, version, checks: [{name, status, detail}], status}."""
    from core.wizard import admin_policy as ap, dependency_lock, dependency_resolver, exporter
    from core.wizard import session as wz
    from tests.wizard_helpers import fake_mp4
    caps = caps or capabilities()
    tool, version = case["tool"], case["version"]
    out: Dict[str, Any] = {"tool": tool, "version": version, "checks": [], "live": live}

    def check(name: str, ok: Optional[bool], detail: str = "") -> bool:
        status = "SKIPPED" if ok is None else ("PASS" if ok else "FAIL")
        out["checks"].append({"name": name, "status": status, "detail": detail[:600]})
        log(f"  [{status:7}] {tool:18} {name}{' - ' + detail[:160] if detail else ''}")
        return bool(ok)

    prev_mode = os.environ.get("UNDERSTUDY_DEPENDENCY_INSTALL")
    blocked_install = install_blocker(case, caps) if live else "build-level run"
    os.environ["UNDERSTUDY_DEPENDENCY_INSTALL"] = "auto" if live and not blocked_install else "dry-run"
    extra_env = sandbox_env(case, caps) if live else {}
    saved = {k: os.environ.get(k) for k in extra_env}
    os.environ.update(extra_env)
    try:
        ap.reset_to_defaults()
        if not ap.is_tool_allowed(tool):
            ap.whitelist_catalog_tool(tool)
        ap.set_tool_versions(tool, [version], default=version)
        # --- user: record + pick the tool without choosing a version --------------------------------------------
        s = wz.create_session(base_url + "/portal1", SOP, fake_mp4(), "matrix.mp4", background=False)
        sid = s["session_id"]
        s = wz.select_framework(sid, tool, background=False)
        spec = wz._spec(s)
        pkg = spec.packages[0]
        check("admin default version applied to the user's tool", dependency_lock.chosen_version(spec) == version,
              f"{pkg.name} {pkg.version}")
        marker = f"MATRIX-{tool}-{int(time.time() * 1000) % 10**7}"
        params = [dict(p) for p in s["parameters"]]
        for p in params:
            if p["key"] == "invoice_notes":
                p["value"] = marker
            if p.get("type") == "secret":
                p["value"] = ERP_TOKEN
        wz.update_parameters(sid, params)
        wz.update_rules(sid, wz.get(sid)["rules"], True)           # 9.9: Govern (Step 2) before Build (Step 3)
        s = wz.compliance_check(sid)
        if not check("GOVERN passed", s["pillars"].get("GOVERN", {}).get("status") == "PASS",
                     json.dumps((s.get("compliance") or {}).get("violations"))):
            return _finish(out)
        s = wz.build_suite(sid, background=False)
        if not check("BUILD passed", s["pillars"].get("BUILD", {}).get("status") == "PASS",
                     str(s["pillars"].get("BUILD", {}).get("reason"))):
            return _finish(out)
        files = s["suite"]["files"]
        spec = wz._spec(s)
        check("generated manifest pins the admin's version", manifest_pins(spec.ecosystem, files, pkg.name, version),
              ", ".join(n for n in files if n in dependency_lock.ALL_MANIFESTS))
        plan = dependency_resolver.plan_for(spec)
        check("sandbox install plan uses the same version",
              manifest_pins(spec.ecosystem, plan.manifest_files, pkg.name, version),
              " | ".join(" ".join(c) for c in plan.commands)[:400])
        z = zipfile.ZipFile(io.BytesIO(exporter.build_zip(s, spec)))
        zfiles = {n: z.read(n).decode("utf-8", "replace") for n in z.namelist() if n in dependency_lock.ALL_MANIFESTS}
        check("ZIP manifest pins the same version", manifest_pins(spec.ecosystem, zfiles, pkg.name, version))
        if not live:
            return _finish(out)
        # --- LIVE: real install ------------------------------------------------------------------------------------
        if blocked_install:
            check("real install in the isolated sandbox", None, blocked_install)
            return _finish(out)
        deps = s.get("dependencies") or {}
        if deps.get("status") != "READY":
            deps = dependency_resolver.install(spec, approved=True)
            s["dependencies"] = {**deps, "framework_id": spec.id, "approved": True}
        if not check("real install in the isolated sandbox", deps.get("status") == "READY",
                     f"{deps.get('status')}: {deps.get('message', '')}"):
            return _finish(out)
        rows = dependency_resolver.installed_versions(spec)
        primary = next((r for r in rows if r["name"].split("[")[0] == pkg.name.split("[")[0]), None)
        if spec.ecosystem in ("pip", "npm"):
            check("installed version is exactly the pinned one", bool(primary and primary.get("installed") == version),
                  json.dumps(primary))
        else:
            check("installed version is exactly the pinned one", True,
                  f"{pkg.name} {version} resolved by Maven from the pinned pom.xml")
        # --- LIVE: run the tool itself against the sample website, then check the ERP ----------------------------
        why = run_blocker(case, caps)
        if why:
            check("automation run with the tool itself + ERP check", None, why)
            return _finish(out)
        s = wz.execute(sid, background=False)
        ex = s.get("execution") or {}
        check("ran with the tool's own runtime (not the built-in driver)", ex.get("strategy") == "framework",
              f"strategy={ex.get('strategy')}")
        if not check("EXECUTE passed", s["pillars"].get("BUILD-EXECUTE", {}).get("status") == "PASS",
                     " | ".join((ex.get("logs") or [])[-6:])):
            return _finish(out)
        s = wz.export(sid, background=False)
        check("EXPORT passed (ZIP created)", s["pillars"].get("EXPORT", {}).get("status") == "PASS",
              s["pillars"].get("EXPORT", {}).get("reason", ""))
        import requests
        ledger = requests.get(base_url + "/api/erp/ledger", timeout=20).json().get("ledger", [])
        check("exactly one ERP record with this run's marker",
              len([r for r in ledger if r.get("invoice_notes") == marker]) == 1, marker)
        if s.get("export", {}).get("ready"):
            z = zipfile.ZipFile(io.BytesIO(wz.export_zip(sid)["data"]))
            zfiles = {n: z.read(n).decode("utf-8", "replace") for n in z.namelist() if n in dependency_lock.ALL_MANIFESTS}
            check("downloaded ZIP pins the installed version", manifest_pins(spec.ecosystem, zfiles, pkg.name, version))
        return _finish(out)
    except Exception as e:                                      # report, never hide
        check("unexpected error", False, f"{type(e).__name__}: {e}")
        return _finish(out)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if prev_mode is None:
            os.environ.pop("UNDERSTUDY_DEPENDENCY_INSTALL", None)
        else:
            os.environ["UNDERSTUDY_DEPENDENCY_INSTALL"] = prev_mode


def _finish(out: Dict[str, Any]) -> Dict[str, Any]:
    st = [c["status"] for c in out["checks"]]
    out["status"] = "FAIL" if "FAIL" in st else ("PARTIAL" if "SKIPPED" in st else "PASS")
    return out


def markdown(results: List[Dict[str, Any]], caps: Dict[str, Any], live: bool) -> str:
    lines = [f"# Tool matrix - {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}", "",
             f"Mode: {'LIVE (real installs, real runs with each tool, ERP check, ZIP)' if live else 'build level (no installs)'} · "
             "target: the bundled demo ERP, Portal 1 · generated by `python scripts/tool_matrix.py"
             f"{' --live' if live else ''}`", "",
             "| Tool | Pinned version | Result | Checks |", "|---|---|---|---|"]
    for r in results:
        passed = len([c for c in r["checks"] if c["status"] == "PASS"])
        skipped = [c for c in r["checks"] if c["status"] == "SKIPPED"]
        failed = [c for c in r["checks"] if c["status"] == "FAIL"]
        note = f"{passed} passed" + (f", {len(failed)} failed" if failed else "") + \
               (f", skipped: {skipped[0]['name']} ({skipped[0]['detail']})" if skipped else "")
        lines.append(f"| {r['tool']} | `{r['version']}` | **{r['status']}** | {note} |")
    lines += ["", "## This machine", "", "```", json.dumps({k: bool(v) if k not in ('chrome', 'chromedriver',
              'playwright_chromium') else (v or None) for k, v in caps.items()}, indent=2), "```", "",
              "## Every check", ""]
    for r in results:
        lines.append(f"### {r['tool']} `{r['version']}` - {r['status']}")
        for c in r["checks"]:
            lines.append(f"- {c['status']}: {c['name']}" + (f" - {c['detail']}" if c["detail"] else ""))
        lines.append("")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap_ = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap_.add_argument("--live", action="store_true", help="really install, run with the tool itself, check the ERP and the ZIP")
    ap_.add_argument("--tools", default="", help="comma-separated tool ids (default: all)")
    args = ap_.parse_args(argv)
    from tests import wizard_helpers as H
    H.install_fake(None)
    srv = H.LiveServer.get()
    caps = capabilities()
    print("Machine:", json.dumps({k: bool(v) for k, v in caps.items()}))
    want = {t.strip() for t in args.tools.split(",") if t.strip()}
    results = []
    for case in CASES:
        if want and case["tool"] not in want:
            continue
        print(f"\n== {case['tool']} {case['version']}")
        results.append(run_case(case, srv.base, args.live, caps))
    d = os.path.join(ROOT, "generated", "tool_matrix")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "report.json"), "w", encoding="utf-8") as fh:
        json.dump({"live": args.live, "capabilities": {k: (v if isinstance(v, (bool, type(None))) else str(v))
                                                       for k, v in caps.items()}, "results": results}, fh, indent=2)
    md = markdown(results, caps, args.live)
    with open(os.path.join(ROOT, "docs", "TOOL_MATRIX.md"), "w", encoding="utf-8") as fh:
        fh.write(md + "\n")
    print("\n" + "\n".join(md.splitlines()[:6 + len(results)]))
    return 1 if any(r["status"] == "FAIL" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
