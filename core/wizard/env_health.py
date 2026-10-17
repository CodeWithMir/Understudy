"""Environment health: runtime and dependency warnings on the machine running Understudy.

Code health (code_health.py) fixes warnings that Understudy's own Python code raises. This module covers the other
kind of start-up/installation warning an admin sees in the terminal, which lives OUTSIDE the code base:

  PythonRuntime   the Python running the backend is end-of-life or close to it (e.g. Python 3.10 ends 2026-10-31)
  NodeRuntime     the Node.js used for the web UI and JavaScript tools is end-of-life or close to it
  NpmEngine       an npm package of the web UI declares an "engines.node" range this Node doesn't satisfy
                  (what ``npm WARN EBADENGINE`` reports during ``npm install``)
  NpmDeprecated   an npm package of the web UI is deprecated by its author (``npm WARN deprecated eslint@9...``)
  PipConflict     installed Python packages have broken requirements (``pip check``)

These can't be patched safely by editing a file - they need installing a newer runtime or changing dependencies - so
the result is ADVICE: a deterministic explanation plus, with a Gemini key, the ENV_UPGRADE_ADVICE agent's step-by-step
upgrade plan for this exact machine (commands for its OS). Nothing is executed automatically.

End-of-life dates come live from https://endoflife.date (cached for a day) with a built-in table as fallback.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WARN_DAYS = int(os.environ.get("UNDERSTUDY_EOL_WARN_DAYS") or 180)

# Fallback end-of-life table (cycle -> ISO date). Live data from endoflife.date wins when reachable.
EOL_FALLBACK = {
    "python": {"3.8": "2024-10-07", "3.9": "2025-10-31", "3.10": "2026-10-31", "3.11": "2027-10-31",
               "3.12": "2028-10-31", "3.13": "2029-10-31", "3.14": "2030-10-31"},
    "nodejs": {"16": "2023-09-11", "17": "2022-06-01", "18": "2025-04-30", "19": "2023-06-01", "20": "2026-04-30",
               "21": "2024-06-01", "22": "2027-04-30", "23": "2025-06-01", "24": "2028-04-30", "25": "2026-06-01"},
}
RECOMMENDED = {"python": "3.12", "nodejs": "22"}          # current, long-supported lines for the advice text
_EOL_CACHE: Dict[str, Tuple[float, Dict[str, str]]] = {}


def _eol_table(product: str) -> Tuple[Dict[str, str], str]:
    hit = _EOL_CACHE.get(product)
    if hit and time.time() - hit[0] < 86400:
        return hit[1], "endoflife.date"
    try:
        req = urllib.request.Request(f"https://endoflife.date/api/{product}.json",
                                     headers={"User-Agent": "Understudy-Mission-Control/6.0"})
        with urllib.request.urlopen(req, timeout=6) as resp:          # nosec - fixed public host
            rows = json.loads(resp.read(2_000_000))
        table = {str(r["cycle"]): str(r["eol"]) for r in rows if isinstance(r.get("eol"), str)}
        if table:
            _EOL_CACHE[product] = (time.time(), table)
            return table, "endoflife.date"
    except Exception:
        pass
    return EOL_FALLBACK.get(product, {}), "built-in table"


def _eol_finding(product: str, label: str, version: str, cycle: str, today: date) -> Optional[Dict[str, Any]]:
    table, source = _eol_table(product)
    eol = table.get(cycle)
    if not eol:
        return None
    try:
        eol_d = datetime.strptime(eol, "%Y-%m-%d").date()
    except ValueError:
        return None
    days = (eol_d - today).days
    if days > WARN_DAYS:
        return None
    rec = RECOMMENDED[product]
    state = "has reached end of life" if days < 0 else f"reaches end of life in {days} day(s)"
    return {"category": "PythonRuntime" if product == "python" else "NodeRuntime",
            "severity": "error" if days < 0 else "warn",
            "title": f"{label} {version} {state} ({eol})",
            "message": (f"{label} {cycle} {state} on {eol} (source: {source}). After that it gets no security fixes, "
                        f"and tools and IDE extensions stop supporting it. Upgrade to {label} {rec} or newer."),
            "facts": {"product": product, "version": version, "cycle": cycle, "eol": eol, "days_left": days,
                      "eol_source": source, "recommended": rec}}


# ---------------------------------------------------------------------------
# npm semver (the subset package engines use: || , spaces, >=, <=, >, <, =, ^, ~, x-ranges, hyphen ranges)
# ---------------------------------------------------------------------------
def _parse_v(v: str) -> Optional[Tuple[int, int, int]]:
    m = re.match(r"^v?(\d+)(?:\.(\d+|x|\*))?(?:\.(\d+|x|\*))?", v.strip())
    if not m:
        return None
    parts = [m.group(1), m.group(2) or "0", m.group(3) or "0"]
    return tuple(int(p) if p.isdigit() else 0 for p in parts)  # type: ignore[return-value]


def _cmp_one(ver: Tuple[int, int, int], comp: str) -> bool:
    comp = comp.strip()
    if comp in ("", "*", "x", "latest"):
        return True
    m = re.match(r"^(>=|<=|>|<|=|\^|~)?\s*v?(\d+|x|\*)(?:\.(\d+|x|\*))?(?:\.(\d+|x|\*))?", comp)
    if not m:
        return True                                   # unknown syntax: don't raise a false alarm
    op, a, b, c = m.group(1) or "", m.group(2), m.group(3), m.group(4)
    wild = lambda p: p is None or p in ("x", "*")    # noqa: E731
    base = (int(a) if not wild(a) else 0, int(b) if not wild(b) else 0, int(c) if not wild(c) else 0)
    if op in ("", "=") and (wild(b) or wild(c)):     # x-range: 18, 18.x, 18.2.x
        if wild(a):
            return True
        if wild(b):
            return ver[0] == base[0]
        return ver[:2] == base[:2]
    if op == "^":
        if base[0] > 0:
            return base <= ver < (base[0] + 1, 0, 0)
        if base[1] > 0:
            return base <= ver < (0, base[1] + 1, 0)
        return base <= ver < (0, 0, base[2] + 1)
    if op == "~":
        return base <= ver < (base[0], base[1] + 1, 0)
    if op in ("<=", ">") and (wild(b) or wild(c)):     # partial versions: <=20 means <21.0.0, >20 means >=21.0.0
        nxt = (base[0] + 1, 0, 0) if wild(b) else (base[0], base[1] + 1, 0)
        return ver < nxt if op == "<=" else ver >= nxt
    return {"": ver == base, "=": ver == base, ">=": ver >= base, "<=": ver <= base, ">": ver > base,
            "<": ver < base}[op]


def satisfies(version: str, rng: str) -> bool:
    ver = _parse_v(version)
    if not ver or not rng:
        return True
    for alt in str(rng).split("||"):
        alt = alt.strip()
        hy = re.match(r"^(\S+)\s+-\s+(\S+)$", alt)
        if hy:
            comps = [">=" + hy.group(1), "<=" + hy.group(2)]
        else:
            comps = re.findall(r"(?:>=|<=|>|<|=|\^|~)?\s*v?[\dx*]+(?:\.[\dx*]+){0,2}(?:-[\w.]+)?|\*", alt) or [alt]
        if all(_cmp_one(ver, c) for c in comps):
            return True
    return False


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------
def _node_version() -> Optional[str]:
    node = shutil.which("node")
    if not node:
        return None
    try:
        out = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=15, shell=False).stdout.strip()
        return out.lstrip("v") or None
    except Exception:
        return None


_NODE_OS = {"win32": "win32", "darwin": "darwin", "linux": "linux"}
_NODE_CPU = {"amd64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64", "x86": "ia32", "i386": "ia32",
             "i686": "ia32"}


def _platform_matches(meta: Dict[str, Any]) -> bool:
    """npm's os/cpu filters (["win32"], ["!win32"], ...) against this machine."""
    here = {"os": _NODE_OS.get(sys.platform, sys.platform), "cpu": _NODE_CPU.get(platform.machine().lower(),
                                                                                 platform.machine().lower())}
    for key in ("os", "cpu"):
        vals = meta.get(key)
        if not vals:
            continue
        pos = [v for v in vals if not str(v).startswith("!")]
        neg = [str(v)[1:] for v in vals if str(v).startswith("!")]
        if here[key] in neg or (pos and here[key] not in pos):
            return False
    return True


def _npm_findings(node_ver: Optional[str], ui_dir: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    lock = os.path.join(ui_dir, "package-lock.json")
    try:
        with open(lock, encoding="utf-8") as fh:
            pkgs = json.load(fh).get("packages", {})
        with open(os.path.join(ui_dir, "package.json"), encoding="utf-8") as fh:
            pj = json.load(fh)
    except Exception:
        return out
    direct = set((pj.get("dependencies") or {})) | set((pj.get("devDependencies") or {}))
    deprecated, engine_bad = [], []
    for path, meta in pkgs.items():
        if not path.startswith("node_modules/"):
            continue
        name = path.split("node_modules/")[-1]
        if meta.get("deprecated"):
            deprecated.append({"name": name, "version": meta.get("version"), "direct": name in direct,
                               "message": str(meta["deprecated"])[:300]})
        if not _platform_matches(meta):
            continue                               # optional platform-specific package npm won't install here
        rng = (meta.get("engines") or {}).get("node") if isinstance(meta.get("engines"), dict) else None
        if node_ver and rng and not satisfies(node_ver, rng):
            engine_bad.append({"name": name, "version": meta.get("version"), "requires": rng, "direct": name in direct})
    for d in sorted(deprecated, key=lambda x: (not x["direct"], x["name"]))[:20]:
        out.append({"category": "NpmDeprecated", "severity": "warn" if d["direct"] else "info",
                    "title": f"npm package {d['name']}@{d['version']} is deprecated"
                             + (" (a direct dependency of the web UI)" if d["direct"] else " (pulled in by another package)"),
                    "message": f"npm WARN deprecated {d['name']}@{d['version']}: {d['message']}",
                    "facts": {"package": d["name"], "version": d["version"], "direct": d["direct"],
                              "deprecation": d["message"], "manifest": "understudy-ui/package.json",
                              "declared": (pj.get("dependencies") or {}).get(d["name"])
                              or (pj.get("devDependencies") or {}).get(d["name"])}})
    if engine_bad:
        names = ", ".join(f"{e['name']}@{e['version']} (needs node {e['requires']})" for e in engine_bad[:8])
        out.append({"category": "NpmEngine", "severity": "warn",
                    "title": f"{len(engine_bad)} npm package(s) don't support Node {node_ver} (npm WARN EBADENGINE)",
                    "message": f"npm WARN EBADENGINE: {names}" + (" ..." if len(engine_bad) > 8 else ""),
                    "facts": {"node": node_ver, "packages": engine_bad[:30]}})
    return out


def _pip_check() -> Optional[Dict[str, Any]]:
    try:
        proc = subprocess.run([sys.executable, "-m", "pip", "check"], capture_output=True, text=True, timeout=120,
                              shell=False)
    except Exception:
        return None
    if proc.returncode == 0:
        return None
    lines = [l for l in (proc.stdout + proc.stderr).splitlines() if l.strip()][:20]
    return {"category": "PipConflict", "severity": "warn",
            "title": f"{len(lines)} broken Python package requirement(s) (pip check)",
            "message": "\n".join(lines), "facts": {"pip_check": lines}}


def scan(today: Optional[date] = None, ui_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    today = today or date.today()
    ui_dir = ui_dir or os.path.join(os.environ.get("UNDERSTUDY_CODE_HEALTH_ROOT") or _BASE, "understudy-ui")
    findings: List[Dict[str, Any]] = []
    pv = platform.python_version()
    f = _eol_finding("python", "Python", pv, f"{sys.version_info[0]}.{sys.version_info[1]}", today)
    if f:
        findings.append(f)
    nv = _node_version()
    if nv:
        f = _eol_finding("nodejs", "Node.js", nv, nv.split(".")[0], today)
        if f:
            findings.append(f)
    findings += _npm_findings(nv, ui_dir)
    pc = _pip_check()
    if pc:
        findings.append(pc)
    for f in findings:
        f["id"] = "env-" + hashlib.sha1(f"{f['category']}|{f['title']}".encode()).hexdigest()[:10]
        f["kind"] = "environment"
        f["facts"].update(os=f"{platform.system()} {platform.release()}", python=pv, node=nv)
    return findings


# ---------------------------------------------------------------------------
# Advice
# ---------------------------------------------------------------------------
def _is_windows() -> bool:
    return os.name == "nt"


def fallback_advice(f: Dict[str, Any]) -> Dict[str, Any]:
    fx, cat = f.get("facts", {}), f["category"]
    win = _is_windows()
    steps: List[Dict[str, str]] = []
    if cat == "PythonRuntime":
        rec = fx.get("recommended", "3.12")
        steps = [
            {"title": f"Install Python {rec}", "detail": "Download it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH' on Windows).",
             "command": f"winget install Python.Python.{rec}" if win else f"brew install python@{rec}  # macOS, or your distro's package"},
            {"title": "Recreate Understudy's virtual environment with it",
             "detail": "Stop Understudy, delete the venv folder, and run the start script again - setup reinstalls everything.",
             "command": "rmdir /s /q venv && run_demo.bat" if win else "rm -rf venv && ./run_demo.sh"},
            {"title": "Clear the tools' isolated environments", "detail": "They were built with the old Python.",
             "command": "rmdir /s /q generated\\wizard\\_envs" if win else "rm -rf generated/wizard/_envs"},
        ]
    elif cat == "NodeRuntime":
        rec = fx.get("recommended", "22")
        steps = [
            {"title": f"Install Node.js {rec} LTS", "detail": "From https://nodejs.org (LTS).",
             "command": "winget install OpenJS.NodeJS.LTS" if win else f"nvm install {rec} && nvm use {rec}"},
            {"title": "Reinstall the web UI's packages and rebuild", "detail": "Run in the understudy-ui folder, then restart Understudy.",
             "command": "cd understudy-ui && rmdir /s /q node_modules .next && npm install && npm run build" if win
             else "cd understudy-ui && rm -rf node_modules .next && npm install && npm run build"},
        ]
    elif cat == "NpmEngine":
        steps = [{"title": "Use a Node.js version the packages support",
                  "detail": "Every listed package states the Node versions it supports; install Node.js 22 LTS or newer.",
                  "command": "winget install OpenJS.NodeJS.LTS" if win else "nvm install 22"},
                 {"title": "Reinstall the web UI's packages", "command": "cd understudy-ui && npm install", "detail": ""}]
    elif cat == "NpmDeprecated":
        pkg = fx.get("package")
        if fx.get("direct"):
            steps = [{"title": f"Check the newest {pkg} and its migration notes", "command": f"cd understudy-ui && npm view {pkg} version",
                      "detail": fx.get("deprecation", "")},
                     {"title": f"Upgrade {pkg} in understudy-ui/package.json and rebuild",
                      "detail": "Then run the lint/build to make sure nothing breaks.",
                      "command": f"cd understudy-ui && npm install {pkg}@latest --save-dev && npm run lint && npm run build"}]
        else:
            steps = [{"title": "Update the packages that depend on it", "detail": "It is pulled in by another package; "
                      "updating that package usually drops it. See which one with the command.",
                      "command": f"cd understudy-ui && npm ls {pkg}"}]
    elif cat == "PipConflict":
        steps = [{"title": "Reinstall Understudy's Python requirements", "detail": "Run inside the venv.",
                  "command": ("venv\\Scripts\\python -m pip install -r requirements.txt" if win
                              else "venv/bin/python -m pip install -r requirements.txt")}]
    return {"summary": f.get("message", ""), "risk": "medium" if cat.endswith("Runtime") else "low", "steps": steps,
            "agent_source": "LOCAL_HEURISTIC", "at": datetime.utcnow().isoformat(timespec="seconds") + "Z"}


def ai_advice(f: Dict[str, Any]) -> Dict[str, Any]:
    from core.wizard.llm import generate_json, record_fallback
    res = generate_json("ENV_UPGRADE_ADVICE",
                        {"finding": {k: f[k] for k in ("category", "severity", "title", "message")},
                         "facts": f.get("facts", {}), "os": f"{platform.system()} {platform.release()}",
                         "shell": "Windows cmd / PowerShell" if _is_windows() else "bash"},
                        untrusted=["finding", "facts"])
    if res.ok:
        d = res.data
        return {"summary": str(d.get("summary") or "")[:1500], "risk": d.get("risk") or "medium",
                "steps": [{"title": str(s.get("title") or "")[:200], "detail": str(s.get("detail") or "")[:800],
                           "command": str(s.get("command") or "")[:400]} for s in (d.get("steps") or [])][:10],
                "file_changes": [{"file": str(c.get("file") or "")[:200], "change": str(c.get("change") or "")[:800]}
                                 for c in (d.get("file_changes") or [])][:6],
                "agent_source": res.agent_source, "model": res.model,
                "at": datetime.utcnow().isoformat(timespec="seconds") + "Z"}
    adv = fallback_advice(f)
    record_fallback("ENV_UPGRADE_ADVICE", res.error or "AI unavailable", adv, "Built-in upgrade steps.")
    adv["ai_error"] = res.error
    return adv
