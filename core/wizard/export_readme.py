"""The ZIP's README.md, written by Gemini (9.9) - how to install and run THIS package: a traditional script or an
agentic one, every environment variable it needs, and how to change the data.

``facts`` collects what is true about the package (tool, kind, entry file, run command, launchers, install steps,
environment variables with their meaning, payload keys, pages, steps, rules, dependency versions, files). Secret values
are never part of the facts. ``write`` asks the ``EXPORT_README`` agent for the Markdown and checks it:

* every environment variable of ``.env.example`` and the run command must be in it - a missing one is added in an
  "Environment variables (checked by Understudy)" / "Run command" section rather than silently dropped;
* no secret value, no API key, no other host than the pages, Google's API-key page and the package registries;
* at most ``MAX_CHARS``.

A README that fails the security checks, or no AI, falls back to Understudy's own README (``exporter._readme``). The
result is cached on the session per fact set (re-downloading the ZIP does not call Gemini again).
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from core.wizard.llm import generate_json, record_fallback

MAX_CHARS = 30000
ENV_MEANING = {
    "TARGET_URL": "address of the page where the process starts",
    "TARGET_URLS": "every page address, space-separated",
    "UNDERSTUDY_PAYLOAD": "path of the JSON file with the business values (default payload.json)",
    "HEADLESS": "true = no browser window; false = watch the browser",
    "UNDERSTUDY_SECRET_GEMINI_API_KEY": "your Gemini API key - the agent calls Gemini at run time (agentic only; "
                                        "GEMINI_API_KEY also works). Never commit it",
    "UNDERSTUDY_AGENT_MODEL": "the Gemini model the agent uses (agentic only)",
    "UNDERSTUDY_AGENT_MAX_CALLS": "most Gemini calls one run may make (agentic only, default 80)",
    "UNDERSTUDY_AGENT_MAX_TRIES": "attempts per step before the run fails (agentic only, default 3)",
    "UNDERSTUDY_AGENT_DRIVER": "the browser library the agent drives: playwright | selenium (agentic only)",
    "UNDERSTUDY_AGENT_BROWSER": "the browser: chromium / chrome / msedge / firefox / webkit (Playwright) or chrome / "
                                "edge / firefox (Selenium) (agentic only)",
    "FIREFOX_BINARY": "path of a specific Firefox install",
    "SELENIUM_BROWSER": "chrome (default) | edge | firefox",
    "CHROME_BINARY": "path of a specific Chrome install",
    "EDGE_BINARY": "path of a specific Edge install",
}
_SAFE_HOSTS = {"aistudio.google.com", "ai.google.dev", "www.python.org", "python.org", "nodejs.org", "pypi.org",
               "www.npmjs.com", "playwright.dev", "maven.apache.org", "adoptium.net", "cloud.google.com"}
_KEY_RX = re.compile(r"AIza[0-9A-Za-z_-]{20,}|\bsk-[A-Za-z0-9]{20,}|\bghp_[A-Za-z0-9]{30,}")


def _env_vars(env_lines: List[str]) -> List[Dict[str, str]]:
    out = []
    for line in env_lines:
        m = re.match(r"^\s*#?\s*([A-Z][A-Z0-9_]+)=(.*)$", line)
        if not m:
            continue
        name, value = m.group(1), m.group(2)
        if any(e["name"] == name for e in out):
            continue
        optional = line.lstrip().startswith("#")
        meaning = ENV_MEANING.get(name) or (
            f"address of page {name.rsplit('_', 1)[-1]} of the process" if re.match(r"^TARGET_URL_\d+$", name) else
            f"secret value '{name[len('UNDERSTUDY_SECRET_'):].lower()}' (never written into the code)"
            if name.startswith("UNDERSTUDY_SECRET_") else "setting")
        example = "" if name.startswith("UNDERSTUDY_SECRET_") else value
        out.append({"name": name, "meaning": meaning, "required": "no" if optional else
                    ("yes" if name in ("TARGET_URL", "UNDERSTUDY_SECRET_GEMINI_API_KEY") or
                     name.startswith("UNDERSTUDY_SECRET_") or re.match(r"^TARGET_URL_\d+$", name) else "no"),
                    "example": example})
    return out


def facts(s: Dict[str, Any], spec, lock: Dict[str, Any], env_lines: List[str], files: List[str],
          launchers: Dict[str, str]) -> Dict[str, Any]:
    from core.wizard import agent_stacks, dependency_resolver
    agentic = spec.category == "agentic"
    params = s.get("parameters") or []
    run_cmd = " ".join(t.replace("{entry}", spec.entry_file) for t in spec.run_command)
    install = dependency_resolver.launcher_steps(spec)["sh"] + [run_cmd]
    d = s.get("difficulty") or {}
    return {
        "process": {"name": (s.get("analysis") or {}).get("process_name") or "",
                    "summary": (s.get("analysis") or {}).get("summary") or ""},
        "tool": {"name": spec.name, "language": spec.language, "ecosystem": spec.ecosystem,
                 "kind": "agentic" if agentic else "traditional", "entry_file": spec.entry_file,
                 "run_command": run_cmd,
                 "toolchain": dependency_resolver.TOOLCHAINS.get(spec.ecosystem, []),
                 "manifests": sorted(dependency_resolver.manifest_files(spec).keys())},
        "agentic": {"runtime_library": "understudy_agent.py", "script": spec.entry_file,
                    "what_happens": "Gemini performs each agent.step of the script on the live page at run time "
                                    "(decisions logged as 'AGENT step n: ...'), types values from payload.json itself "
                                    "(secrets never reach the model); the run passes when every step passed",
                    "key": "UNDERSTUDY_SECRET_GEMINI_API_KEY (or GEMINI_API_KEY)",
                    "model": str(((s.get("execution") or {}).get("agent_model")) or (spec.agent or {}).get("model")
                                 or "gemini-flash-latest"),
                    "browser_stack": agent_stacks.stack_of(spec)}
        if agentic else None,
        "launchers": {"macOS / Linux": "bash run.sh", "Windows": "run.bat"},
        "launcher_steps": install,
        "env_vars": _env_vars(env_lines),
        "pages": [u for u in [s.get("target_url")] + [u for u in s.get("target_urls") or [] if u != s.get("target_url")] if u],
        "steps": [f"{st.get('order')}. {st.get('description') or st.get('action')}"
                  for st in (s.get("analysis") or {}).get("steps") or []][:40],
        "payload": [{"key": p["key"], "label": p.get("label", ""), "type": p.get("type", "text"),
                     "secret": p.get("type") == "secret"} for p in params],
        "rules": [r.get("plain_english") for r in s.get("rules") or [] if r.get("enabled", True)][:20],
        "dependency_versions": [{"name": p.get("name"), "version": p.get("installed") or p.get("requested")}
                                for p in (lock.get("packages") or [])][:20],
        "assessment": {"difficulty": d.get("score"), "level": d.get("level"),
                       "recommended": (d.get("recommendation") or {}).get("label")} if d else None,
        "files": sorted(files)[:60],
        "governance": {"rules_file": "governance.json", "checked": "before step 1, against payload.json, by the script "
                       "itself (understudy_governance.py / .js / UnderstudyGovernance.java)",
                       "output": "'GOVERN OK: <n> SOP rule(s) checked' or 'GOVERN FAILED: <why>' (the run stops)"},
        "output": "first 'GOVERN OK' (the SOP rules), then every step prints 'STEP <n> OK' or 'STEP <n> FAILED: "
                  "<reason>'; the exit code is non-zero on failure",
        "reports": REPORTS_FACT,
    }


def _hosts_ok(md: str, pages: List[str]) -> List[str]:
    allowed = {(urlsplit(u).hostname or "").lower() for u in pages} | _SAFE_HOSTS | {"localhost", "127.0.0.1"}
    return sorted({h.lower() for h in re.findall(r"https?://([A-Za-z0-9.-]+)", md)} - allowed)


REPORTS_FACT = {
    "folder": "results/<run id>/ (one folder per run; results/latest.html opens the newest report)",
    "files": {"understudy-report.html": "the run as a page: pass/fail, SOP rules check, every step with its time, "
                                        "what failed, the data used and the full log (works offline)",
              "understudy-report.json": "the same as JSON, for CI or dashboards",
              "run.log": "the console output with a time per line; secret values are masked",
              "junit.xml": "pytest suites only - JUnit XML for CI"},
    "native": "reports the tool writes itself (Maven Surefire, Gradle, Robot Framework, Playwright) are linked from "
              "the page",
}
REPORTS_MD = ("\n\n## Reports (checked by Understudy)\nEvery `bash run.sh` / `run.bat` run writes a report to "
              "`results/<run id>/` - open `results/latest.html` for the newest one:\n\n"
              "- `understudy-report.html` - pass/fail, the SOP rules check, every step with its time, what failed, the "
              "data used and the full log (works offline)\n"
              "- `understudy-report.json` - the same as JSON, for CI or dashboards\n"
              "- `run.log` - the console output with a time per line; secret values are masked\n"
              "- `junit.xml` - pytest suites only, for CI\n\n"
              "Reports the tool writes itself (Maven Surefire, Gradle, Robot Framework, Playwright) are linked from the "
              "page.\n")


def check(md: str, f: Dict[str, Any], secret_values: List[str]) -> Dict[str, Any]:
    """{ok, markdown (completed), problems, added}. ``ok`` False means: do not ship this text."""
    problems, added = [], []
    if not md.strip():
        return {"ok": False, "problems": ["empty"], "markdown": md, "added": []}
    if len(md) > MAX_CHARS:
        problems.append(f"longer than {MAX_CHARS} characters")
    if _KEY_RX.search(md):
        problems.append("contains something that looks like an API key")
    for v in secret_values:
        if v and len(v) >= 4 and v in md:
            problems.append("contains a secret value")
            break
    bad_hosts = _hosts_ok(md, f["pages"])
    if bad_hosts:
        problems.append("links to other sites: " + ", ".join(bad_hosts[:4]))
    if problems:
        return {"ok": False, "problems": problems, "markdown": md, "added": []}
    missing = [e for e in f["env_vars"] if e["name"] not in md]
    if missing:
        md += ("\n\n## Environment variables (checked by Understudy)\nSet these in `.env` (copy `.env.example`):\n\n"
               "| Variable | Required | Meaning |\n|---|---|---|\n"
               + "\n".join(f"| `{e['name']}` | {e['required']} | {e['meaning']} |" for e in missing) + "\n")
        added.append("environment variables: " + ", ".join(e["name"] for e in missing))
    if f["tool"]["run_command"] not in md:
        md += f"\n\n## Run command (checked by Understudy)\n```\n{f['tool']['run_command']}\n```\n" \
              "`bash run.sh` / `run.bat` install the dependencies first and then run it.\n"
        added.append("run command")
    if "understudy-report" not in md:
        md += REPORTS_MD
        added.append("reports")
    return {"ok": True, "problems": [], "markdown": md, "added": added}


def write(s: Dict[str, Any], f: Dict[str, Any], fallback: str) -> Dict[str, Any]:
    """The README for the ZIP: Gemini's (checked) or Understudy's. Cached on the session per fact set."""
    key = hashlib.sha256(json.dumps(f, sort_keys=True, default=str).encode()).hexdigest()[:16]
    cached = s.get("export_readme") or {}
    if cached.get("key") == key and cached.get("markdown"):
        return cached
    secret_values = [str(p.get("value")) for p in s.get("parameters") or [] if p.get("type") == "secret" and p.get("value")]
    res = generate_json("EXPORT_README", {"facts": json.dumps(f, indent=1, ensure_ascii=False, default=str)},
                        untrusted=["facts"])
    out: Optional[Dict[str, Any]] = None
    if res.ok and isinstance(res.data, dict):
        md = str(res.data.get("readme") or "").replace("\r\n", "\n").strip()
        c = check(md, f, secret_values)
        if c["ok"]:
            note = (f"> README written by Gemini ({res.model}) from this package's facts and checked by Understudy"
                    + (f" (added: {'; '.join(c['added'])})" if c["added"] else "") + ".\n\n")
            out = {"key": key, "markdown": note + c["markdown"] + "\n", "source": res.agent_source, "model": res.model,
                   "added": c["added"], "problems": []}
        else:
            record_fallback("EXPORT_README", "; ".join(c["problems"]), {"chars": len(md)},
                            "Shipped Understudy's own README instead.")
            out = {"key": key, "markdown": fallback, "source": "LOCAL_HEURISTIC", "model": "Understudy README",
                   "added": [], "problems": c["problems"]}
    if out is None:
        record_fallback("EXPORT_README", res.error or "AI unavailable", None, "Shipped Understudy's own README.")
        out = {"key": key, "markdown": fallback, "source": "LOCAL_HEURISTIC", "model": "Understudy README",
               "added": [], "problems": [str(res.error or "AI unavailable")[:200]]}
    s["export_readme"] = out
    return out
