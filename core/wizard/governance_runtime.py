"""Govern inside the script (9.9): the SOP rules the user confirmed in Step 2 become part of every generated script -
traditional or agentic - and are checked against ``payload.json`` BEFORE step 1, every time the script runs (in
Understudy and wherever the exported ZIP runs). Change the data to something the SOP forbids and the script stops
before it touches the application.

What a suite gets (``files_for``):

* ``governance.json`` - the enabled rules (field, type, values / limit / dates / pattern, plain English, the SOP sentence
  it came from) and the payload keys that are secrets (their values come from ``UNDERSTUDY_SECRET_<KEY>``);
* a fixed, vetted checker in the script's language, copied unchanged like the agent runtime:
  ``understudy_governance.py`` (Python tools, Robot Framework as a keyword library, the agentic tools),
  ``understudy_governance.js`` (Node tools - Playwright JS, Puppeteer, Cypress via ``cypress.config.js``, WebdriverIO),
  ``UnderstudyGovernance.java`` (Java tools, next to the test, no extra dependency).
  Other languages get ``governance.json`` and the code generator implements the same check (``ai_instructions``).

Every checker prints ``GOVERN OK: <n> SOP rule(s) checked`` or ``GOVERN FAILED: <why>`` and stops the run (exit code
1 / a failing test). The rules are evaluated exactly like Understudy's rule engine (``governance.evaluate_compliance``):
allowed / denied values (case- and space-insensitive), max / min (exclusive or not; "12.5k" = 12500), date ranges, regex
(search), required, role access. The executor requires ``GOVERN OK`` before the first step (``executor``).
"""
from __future__ import annotations

import hashlib
import json
import posixpath
import re
from typing import Any, Dict, List

from core.wizard.frameworks import FrameworkSpec

RULES_FILE = "governance.json"
PY_FILE = "understudy_governance.py"
JS_FILE = "understudy_governance.js"
JAVA_CLASS = "UnderstudyGovernance"
OK_RX = re.compile(r"\bGOVERN OK\b")
FAILED_RX = re.compile(r"\bGOVERN FAILED:\s*(.*)")
RULE_KEYS = ("rule_id", "field", "rule_type", "values", "limit", "exclusive", "start_date", "end_date", "pattern",
             "plain_english", "source_text")


def document(rules: List[Dict[str, Any]], params: List[Dict[str, Any]], sop_text: str = "") -> Dict[str, Any]:
    """The content of governance.json - only enabled rules that are linked to a payload key."""
    keys = {p["key"] for p in params}
    out = []
    for r in rules or []:
        if not r.get("enabled", True):
            continue
        out.append({k: r.get(k) for k in RULE_KEYS})
    return {"about": "SOP rules confirmed in Understudy's Govern step - checked against payload.json before step 1",
            "sop_sha256": hashlib.sha256((sop_text or "").encode("utf-8")).hexdigest()[:16],
            "secret_keys": sorted(p["key"] for p in params if p.get("type") == "secret"),
            "payload_keys": sorted(keys), "rules": out}


def kind(spec: FrameworkSpec) -> str:
    """python | js | java | other - which vetted checker the suite gets."""
    lang = (spec.language or "").lower()
    if spec.ecosystem == "pip" or lang in ("python", "robot"):
        return "python"
    if spec.ecosystem == "npm" or lang in ("javascript", "typescript"):
        return "js"
    if spec.ecosystem in ("maven", "gradle") or lang in ("java", "kotlin"):
        return "java" if lang != "kotlin" else "other"
    return "other"


def _java_dir_and_package(files: Dict[str, str], entry: str) -> (str, str):
    src = files.get(entry) or ""
    m = re.search(r"^\s*package\s+([\w.]+)\s*;", src, re.M)
    return posixpath.dirname(entry), (m.group(1) if m else "")


def files_for(spec: FrameworkSpec, rules: List[Dict[str, Any]], params: List[Dict[str, Any]], sop_text: str,
              files: Dict[str, str], entry: str) -> Dict[str, str]:
    """The governance files to add to a suite (governance.json + the checker for its language)."""
    out = {RULES_FILE: json.dumps(document(rules, params, sop_text), indent=2, ensure_ascii=False) + "\n"}
    k = kind(spec)
    if k == "python":
        out[PY_FILE] = PY_CHECKER
        out.update(companions(spec, files))
    elif k == "js":
        out[JS_FILE] = JS_CHECKER
        out.update(companions(spec, dict(files, **out)))
    elif k == "java":
        d, pkg = _java_dir_and_package(files, entry)
        out[posixpath.join(d, JAVA_CLASS + ".java") if d else JAVA_CLASS + ".java"] = \
            (f"package {pkg};\n\n" if pkg else "") + JAVA_CHECKER
        out.update(companions(spec, dict(files, **out)))
    return out


PYTEST_INI = "pytest.ini"
PYTEST_INI_TEXT_V1 = ("[pytest]\n# Written by Understudy: makes this folder the test root, so pytest never picks up the "
                      "settings of a folder above it.\n")
# 9.9.1: '-s' so the 'GOVERN OK' / 'STEP n OK' lines are printed for a passing test too (pytest hides them otherwise)
PYTEST_INI_TEXT = PYTEST_INI_TEXT_V1 + ("# -s: show the GOVERN / STEP lines live, also when the test passes.\n"
                                        "addopts = -s\n")


def companions(spec: FrameworkSpec, files: Dict[str, str]) -> Dict[str, str]:
    """9.9: what a Python suite needs so its tests can import the checker wherever they are: a test in a sub-folder
    (tests/test_x.py) only has ITS folder on the import path under pytest / python, so the same vetted checker goes
    next to every Python / Robot file that uses it (it finds governance.json above it); a suite run by pytest gets its
    own pytest.ini so pytest never reads the settings of a folder above it. Called at build and before every run
    (self-healing may move tests)."""
    if kind(spec) == "java":
        return _java_companions(files)
    if kind(spec) == "js":
        return _js_companions(files)
    if kind(spec) != "python":
        return {}
    out: Dict[str, str] = {}
    for p, src in files.items():
        d = posixpath.dirname(p)
        if d and p.endswith((".py", ".robot", ".resource")) and "understudy_governance" in (src or "") \
                and posixpath.basename(p) != PY_FILE and files.get(posixpath.join(d, PY_FILE)) != PY_CHECKER:
            out[posixpath.join(d, PY_FILE)] = PY_CHECKER
    if uses_pytest(spec) and not _has_pytest_config(files):
        out[PYTEST_INI] = PYTEST_INI_TEXT
    return out


JS_SPEC_RX = re.compile(r"""(?:require\s*\(\s*|from\s+|import\s*\(\s*|import\s+)["'](\.{1,2}/[^"']*understudy_governance[^"']*)["']""")
JS_ESM_WRAPPER = ("// Understudy SOP governance check - ES-module entry for this project (\"type\": \"module\"); the check itself is\n"
                  "// understudy_governance.cjs, copied unchanged from Understudy.\n"
                  "import { createRequire } from \"module\";\n"
                  "const g = createRequire(import.meta.url)(\"./understudy_governance.cjs\");\n"
                  "export const { check, checkFile, violations, parseNumber, parseDate } = g;\n"
                  "export default g;\n")


def _is_esm(files: Dict[str, str]) -> bool:
    try:
        return json.loads(files.get("package.json") or "{}").get("type") == "module"
    except ValueError:
        return False


def _js_companions(files: Dict[str, str]) -> Dict[str, str]:
    """9.9: a JS / TS file loads the checker with a path relative to ITSELF (``./understudy_governance`` from tests/
    means tests/understudy_governance.js) - the vetted checker goes wherever such a path points inside the suite. In an
    ES-module project (package.json "type": "module") the .js entry is a small ESM wrapper around the .cjs checker."""
    esm = _is_esm(files)
    out: Dict[str, str] = {}
    for p, src in files.items():
        if not p.endswith((".js", ".cjs", ".mjs", ".ts", ".mts", ".cts", ".jsx", ".tsx")) or \
                posixpath.basename(p).startswith("understudy_governance."):
            continue
        for m in JS_SPEC_RX.finditer(src or ""):
            target = posixpath.normpath(posixpath.join(posixpath.dirname(p), m.group(1)))
            if target.startswith(".."):
                continue                                   # outside the suite
            base, ext = posixpath.splitext(target)
            base = target if ext not in (".js", ".cjs", ".mjs") else base
            if ext == ".cjs":
                want = {base + ".cjs": JS_CHECKER}
            elif ext == ".mjs" or esm:
                want = {base + ".cjs": JS_CHECKER, base + (".mjs" if ext == ".mjs" else ".js"): JS_ESM_WRAPPER}
            else:
                want = {base + ".js": JS_CHECKER}
            for f, content in want.items():
                if files.get(f) != content:
                    out[f] = content
    if esm and files.get(JS_FILE) == JS_CHECKER:          # the root checker itself must load in an ES-module project
        out[JS_FILE] = JS_ESM_WRAPPER
        out["understudy_governance.cjs"] = JS_CHECKER
    return out


def _java_companions(files: Dict[str, str]) -> Dict[str, str]:
    """9.9: Java finds a class by its package, not its folder - a test in another package that calls
    UnderstudyGovernance.check(...) without importing it gets the same vetted class in ITS package (folder)."""
    have = {}
    for p, src in files.items():
        if posixpath.basename(p) == JAVA_CLASS + ".java" and (src or "").endswith(JAVA_CHECKER):
            m = re.search(r"^\s*package\s+([\w.]+)\s*;", src, re.M)
            have[m.group(1) if m else ""] = p
    out: Dict[str, str] = {}
    for p, src in files.items():
        if not p.endswith(".java") or posixpath.basename(p) == JAVA_CLASS + ".java" or \
                not re.search(r"\bUnderstudyGovernance\s*\.", src or ""):
            continue
        m = re.search(r"^\s*package\s+([\w.]+)\s*;", src, re.M)
        pkg = m.group(1) if m else ""
        imported = re.search(r"^\s*import\s+(static\s+)?([\w.]+)\.UnderstudyGovernance\b", src, re.M)
        if pkg in have or (imported and imported.group(2) in have):
            continue
        target = posixpath.join(posixpath.dirname(p), JAVA_CLASS + ".java")
        out[target] = (f"package {pkg};\n\n" if pkg else "") + JAVA_CHECKER
        have[pkg] = target
    return out


def uses_pytest(spec: FrameworkSpec) -> bool:
    """The tool runs its suite with pytest (run command or packages)."""
    cmd = " ".join(spec.run_command or []).lower()
    return "pytest" in cmd or any((p.name or "").lower().startswith("pytest") for p in spec.packages or [])


def _has_pytest_config(files: Dict[str, str]) -> bool:
    for p, src in files.items():
        if posixpath.dirname(p):
            continue
        name = posixpath.basename(p)
        if name == PYTEST_INI or (name == "pyproject.toml" and "[tool.pytest" in (src or "")) or \
                (name in ("tox.ini", "setup.cfg") and re.search(r"^\[(tool:)?pytest\]", src or "", re.M)):
            return True
    return False


def is_vetted(path: str, content: str) -> bool:
    """A governance file Understudy wrote (its rule values are not hard-coded payload values)."""
    name = posixpath.basename(path)
    if name == RULES_FILE:
        return True
    if path == PYTEST_INI:
        return content in (PYTEST_INI_TEXT, PYTEST_INI_TEXT_V1)
    if name == PY_FILE:
        return content == PY_CHECKER
    if name == JS_FILE:
        return content in (JS_CHECKER, JS_ESM_WRAPPER)
    if name in ("understudy_governance.cjs", "understudy_governance.mjs"):
        return content in (JS_CHECKER, JS_ESM_WRAPPER)
    if name == JAVA_CLASS + ".java":
        return content.endswith(JAVA_CHECKER)
    return False


def uses_checker(files: Dict[str, str], spec: FrameworkSpec) -> bool:
    """Does the (AI-written) code check the rules before step 1?"""
    code = "\n".join(v for p, v in files.items() if not is_vetted(p, v))
    k = kind(spec)
    if k == "python":
        return "understudy_governance" in code
    if k == "js":
        return "understudy_governance" in code
    if k == "java":
        return re.search(r"\bUnderstudyGovernance\s*\.\s*check\s*\(", code) is not None
    return RULES_FILE in code and "GOVERN OK" in code


def ai_instructions(spec: FrameworkSpec) -> str:
    """How the code generator must call the check for this tool (BUILD_GENERATE_SUITE input)."""
    k = kind(spec)
    common = ("Understudy adds governance.json (the SOP rules the user confirmed) and a vetted checker to the suite - do "
              "NOT write either file. Call the check right after the payload is loaded and BEFORE step 1 (before any "
              "browser / request), with the payload INCLUDING the secret values read from the environment. It prints "
              "'GOVERN OK: ...' or 'GOVERN FAILED: ...' and stops the run itself. ")
    if k == "python":
        if (spec.language or "").lower() == "robot":
            return common + ("Robot Framework: add `Library    understudy_governance.py` to *** Settings *** and call "
                             "`Check Governance    ${payload}` right after the payload dictionary is built.")
        return common + "Python: `from understudy_governance import check as govern` and `govern(payload)`."
    if k == "js":
        if spec.id.startswith("cypress") or "cypress" in (spec.entry_file or ""):
            return common + ("Cypress: in cypress.config.js (Node side), before returning the config, call "
                             "`require(\"./understudy_governance\").checkFile(process.env.UNDERSTUDY_PAYLOAD || "
                             "\"payload.json\")`.")
        return common + ("JavaScript / TypeScript: the checker is understudy_governance.js in the suite folder - load it "
                         "with a path relative to YOUR file (from tests/x.spec.js: `require(\"../understudy_governance\")`; "
                         "in an ES-module project: `import { check } from \"../understudy_governance.js\"`) and call "
                         "`check(payload);`. Understudy puts the vetted checker wherever that path points inside the suite.")
    if k == "java":
        return common + (f"Java: `{JAVA_CLASS}.check(payload);` where payload is a Map<String, Object> - the class is "
                         "in the same package and folder as the test.")
    return ("Understudy adds governance.json (the SOP rules) to the suite - do NOT write it. Before step 1, read it "
            "(from the working folder, else up to two folders above - tests may run from a sub-folder) and "
            "check the payload: for each rule {field, rule_type, values, limit, exclusive, start_date, end_date, "
            "pattern}: allowed_values / role_access = the value must be one of values (case- and space-insensitive); "
            "denied_values = must not equal or contain any of values; max / min = numeric limit (exclusive = strict); "
            "date_range = between start_date and end_date; regex = the pattern must match; required = not empty. A rule "
            "whose field is not in the payload is skipped (required / role_access fail). Print 'GOVERN OK: <n> SOP "
            "rule(s) checked' or 'GOVERN FAILED: <reasons>' and stop with a non-zero exit code on failure.")


def summary_lines(rules: List[Dict[str, Any]]) -> List[str]:
    """Plain-English list for script headers / the README."""
    return [str(r.get("plain_english") or "")[:200] for r in rules or [] if r.get("enabled", True)]


# ---------------------------------------------------------------------------------------------------------------------
# The vetted checkers. Keep them in step with governance.evaluate_compliance (tests/test_v99_governed_scripts.py
# runs the same cases through all three and the rule engine).
# ---------------------------------------------------------------------------------------------------------------------
PY_CHECKER = r'''"""Understudy SOP governance check - generated by Understudy, copied unchanged into every suite.

The rules you confirmed in Understudy's Govern step are in governance.json. The automation calls check(payload) before
step 1: a payload the SOP forbids stops the run before anything is typed or sent.

    from understudy_governance import check as govern
    govern(payload)                      # prints "GOVERN OK: ..." or "GOVERN FAILED: ..." (and stops)

Robot Framework: `Library    understudy_governance.py` and `Check Governance    ${payload}`.
Standalone check of payload.json:  python understudy_governance.py [payload.json]
"""
import json
import os
import re
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))


def _find_rules():
    # next to this file, else the suite folder above it (a copy sits next to tests in a sub-folder, e.g. tests/)
    for d in (HERE, os.path.dirname(HERE), os.path.dirname(os.path.dirname(HERE))):
        if os.path.isfile(os.path.join(d, "governance.json")):
            return os.path.join(d, "governance.json")
    return os.path.join(HERE, "governance.json")


RULES_PATH = os.environ.get("UNDERSTUDY_GOVERNANCE") or _find_rules()
_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d.%m.%Y", "%d %b %Y", "%d %B %Y", "%b %d %Y", "%B %d %Y",
                 "%Y/%m/%d", "%d-%m-%Y", "%Y-%m-%dT%H:%M:%S")


def _out(msg):
    stream = sys.__stdout__ or sys.stdout
    stream.write(msg + "\n")
    stream.flush()


def _norm(s):
    return re.sub(r"\s+", " ", "" if s is None else str(s)).strip().lower()


def parse_number(s):
    if isinstance(s, bool):
        return None
    if isinstance(s, (int, float)):
        return float(s)
    m = re.search(r"-?[0-9][0-9,]*(?:\.[0-9]+)?", "" if s is None else str(s))
    if not m:
        return None
    try:
        val = float(m.group(0).replace(",", ""))
    except ValueError:
        return None
    suffix = str(s)[m.end():m.end() + 2].strip().lower()
    if suffix.startswith("k") and not suffix.startswith("kg"):
        val *= 1000
    elif suffix.startswith("m") and not suffix.startswith("mo"):
        val *= 1000000
    return val


def parse_date(s):
    t = re.sub(r"(\d)(st|nd|rd|th)", r"\1", ("" if s is None else str(s)).strip().rstrip(".").replace(",", ""))
    t = re.sub(r"\bsept\b", "sep", t, flags=re.I)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(t[:19], fmt).date()
        except ValueError:
            continue
    return None


def load_rules(path=None):
    with open(path or RULES_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def violations(payload, doc):
    """The broken rules as plain sentences (empty = the payload complies)."""
    out = []
    for r in doc.get("rules") or []:
        f, t = r.get("field"), r.get("rule_type")
        if f not in payload:
            if t in ("required", "role_access"):
                out.append(f"'{f}' is required by the SOP ({r.get('plain_english')}) but is not in the payload")
            continue
        v = payload.get(f)
        sv = "" if v is None else str(v).strip()
        if t in ("allowed_values", "role_access"):
            if _norm(sv) not in [_norm(x) for x in r.get("values") or []]:
                out.append(f"'{sv or '(empty)'}' is not an approved value for {f} ({r.get('plain_english')})")
        elif t == "denied_values":
            for bad in r.get("values") or []:
                if _norm(bad) and (_norm(bad) == _norm(sv) or _norm(bad) in _norm(sv)):
                    out.append(f"'{sv}' is on the SOP's blocked list for {f} ({r.get('plain_english')})")
                    break
        elif t in ("max", "min"):
            num, lim = parse_number(v), float(r.get("limit") or 0)
            if num is None:
                out.append(f"{f} must be a number to check the SOP {'limit' if t == 'max' else 'minimum'} ('{sv}')")
            elif t == "max" and (num >= lim if r.get("exclusive") else num > lim):
                out.append(f"{f} = {sv} is over the SOP limit ({r.get('plain_english')})")
            elif t == "min" and (num <= lim if r.get("exclusive") else num < lim):
                out.append(f"{f} = {sv} is under the SOP minimum ({r.get('plain_english')})")
        elif t == "date_range":
            d, s, e = parse_date(v), parse_date(r.get("start_date")), parse_date(r.get("end_date"))
            if d is None:
                out.append(f"{f} ('{sv}') is not a valid date")
            elif (s and d < s) or (e and d > e):
                out.append(f"{f} = {d.isoformat()} is outside the SOP window ({r.get('plain_english')})")
        elif t == "regex":
            try:
                if not re.search(r.get("pattern") or "", sv[:2000]):
                    out.append(f"{f} ('{sv}') does not match the SOP format {r.get('pattern')}")
            except re.error:
                pass
        elif t == "required":
            if not sv:
                out.append(f"{f} is required by the SOP but is empty")
    return out


def check(payload, path=None, exit_on_fail=True):
    """Checks the payload against governance.json. Prints GOVERN OK / GOVERN FAILED; on failure exits with code 1
    (or raises RuntimeError when exit_on_fail is False)."""
    try:
        doc = load_rules(path)
    except FileNotFoundError:
        problems = ["governance.json is missing - the SOP rules can't be checked"]
        doc = {"rules": []}
    else:
        data = dict(payload or {})
        for key in doc.get("secret_keys") or []:
            if not data.get(key):
                data[key] = os.environ.get("UNDERSTUDY_SECRET_" + key.upper(), data.get(key, ""))
        problems = violations(data, doc)
    if problems:
        msg = "GOVERN FAILED: " + "; ".join(problems)
        _out(msg)
        if exit_on_fail:
            sys.exit(1)
        raise RuntimeError(msg)
    n = len(doc.get("rules") or [])
    _out(f"GOVERN OK: {n} SOP rule(s) checked against the payload")
    return True


def check_governance(payload):
    """Robot Framework keyword: Check Governance    ${payload}"""
    return check(payload, exit_on_fail=False)


if __name__ == "__main__":
    p = sys.argv[1] if len(sys.argv) > 1 else (os.environ.get("UNDERSTUDY_PAYLOAD") or os.path.join(HERE, "payload.json"))
    with open(p, encoding="utf-8") as fh:
        check(json.load(fh))
'''

JS_CHECKER = r'''// Understudy SOP governance check - generated by Understudy, copied unchanged into every suite.
// The rules you confirmed in Understudy's Govern step are in governance.json. The automation calls check(payload)
// before step 1: a payload the SOP forbids stops the run before anything is typed or sent.
//   require("./understudy_governance").check(payload);          // prints GOVERN OK / GOVERN FAILED (and stops)
//   require("./understudy_governance").checkFile("payload.json"); // Cypress: in cypress.config.js
// Standalone:  node understudy_governance.js [payload.json]
"use strict";
const fs = require("fs");
const path = require("path");

// next to this file, else the suite folder above it (when it sits next to tests in a sub-folder)
const RULES_PATH = process.env.UNDERSTUDY_GOVERNANCE || [__dirname, path.dirname(__dirname), path.dirname(path.dirname(__dirname))]
  .map((d) => path.join(d, "governance.json")).find((p) => fs.existsSync(p)) || path.join(__dirname, "governance.json");
const MONTHS = { jan: 0, feb: 1, mar: 2, apr: 3, may: 4, jun: 5, jul: 6, aug: 7, sep: 8, oct: 9, nov: 10, dec: 11 };

function norm(s) { return String(s === null || s === undefined ? "" : s).replace(/\s+/g, " ").trim().toLowerCase(); }

function parseNumber(s) {
  if (typeof s === "boolean") return null;
  if (typeof s === "number") return s;
  const str = String(s === null || s === undefined ? "" : s);
  const m = /-?[0-9][0-9,]*(?:\.[0-9]+)?/.exec(str);
  if (!m) return null;
  let val = parseFloat(m[0].replace(/,/g, ""));
  if (Number.isNaN(val)) return null;
  const suffix = str.slice(m.index + m[0].length, m.index + m[0].length + 2).trim().toLowerCase();
  if (suffix.startsWith("k") && !suffix.startsWith("kg")) val *= 1000;
  else if (suffix.startsWith("m") && !suffix.startsWith("mo")) val *= 1000000;
  return val;
}

function ymd(y, m, d) {                                   // a real calendar date as YYYY-MM-DD, else null
  const dt = new Date(Date.UTC(y, m, d));
  if (dt.getUTCFullYear() !== y || dt.getUTCMonth() !== m || dt.getUTCDate() !== d) return null;
  return dt.toISOString().slice(0, 10);
}

function parseDate(s) {
  let t = String(s === null || s === undefined ? "" : s).trim().replace(/\.$/, "").replace(/,/g, "");
  t = t.replace(/(\d)(st|nd|rd|th)/g, "$1").replace(/\bsept\b/ig, "sep");
  let m;
  if ((m = /^(\d{4})-(\d{2})-(\d{2})(?:T\d{2}:\d{2}:\d{2})?$/.exec(t))) return ymd(+m[1], +m[2] - 1, +m[3]);
  if ((m = /^(\d{1,2})\/(\d{1,2})\/(\d{4})$/.exec(t))) return ymd(+m[3], +m[2] - 1, +m[1]) || ymd(+m[3], +m[1] - 1, +m[2]);
  if ((m = /^(\d{1,2})\.(\d{1,2})\.(\d{4})$/.exec(t))) return ymd(+m[3], +m[2] - 1, +m[1]);
  if ((m = /^(\d{4})\/(\d{1,2})\/(\d{1,2})$/.exec(t))) return ymd(+m[1], +m[2] - 1, +m[3]);
  if ((m = /^(\d{1,2})-(\d{1,2})-(\d{4})$/.exec(t))) return ymd(+m[3], +m[2] - 1, +m[1]);
  if ((m = /^(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})$/.exec(t)) && MONTHS[m[2].slice(0, 3).toLowerCase()] !== undefined)
    return ymd(+m[3], MONTHS[m[2].slice(0, 3).toLowerCase()], +m[1]);
  if ((m = /^([A-Za-z]+)\s+(\d{1,2})\s+(\d{4})$/.exec(t)) && MONTHS[m[1].slice(0, 3).toLowerCase()] !== undefined)
    return ymd(+m[3], MONTHS[m[1].slice(0, 3).toLowerCase()], +m[2]);
  return null;
}

function violations(payload, doc) {
  const out = [];
  for (const r of doc.rules || []) {
    const f = r.field, t = r.rule_type;
    if (!Object.prototype.hasOwnProperty.call(payload, f)) {
      if (t === "required" || t === "role_access") out.push(`'${f}' is required by the SOP (${r.plain_english}) but is not in the payload`);
      continue;
    }
    const v = payload[f];
    const sv = String(v === null || v === undefined ? "" : v).trim();
    if (t === "allowed_values" || t === "role_access") {
      if (!(r.values || []).map(norm).includes(norm(sv))) out.push(`'${sv || "(empty)"}' is not an approved value for ${f} (${r.plain_english})`);
    } else if (t === "denied_values") {
      for (const bad of r.values || []) {
        if (norm(bad) && (norm(bad) === norm(sv) || norm(sv).includes(norm(bad)))) {
          out.push(`'${sv}' is on the SOP's blocked list for ${f} (${r.plain_english})`); break;
        }
      }
    } else if (t === "max" || t === "min") {
      const num = parseNumber(v), lim = parseFloat(r.limit || 0);
      if (num === null) out.push(`${f} must be a number to check the SOP ${t === "max" ? "limit" : "minimum"} ('${sv}')`);
      else if (t === "max" && (r.exclusive ? num >= lim : num > lim)) out.push(`${f} = ${sv} is over the SOP limit (${r.plain_english})`);
      else if (t === "min" && (r.exclusive ? num <= lim : num < lim)) out.push(`${f} = ${sv} is under the SOP minimum (${r.plain_english})`);
    } else if (t === "date_range") {
      const d = parseDate(v), s = r.start_date ? parseDate(r.start_date) : null, e = r.end_date ? parseDate(r.end_date) : null;
      if (d === null) out.push(`${f} ('${sv}') is not a valid date`);
      else if ((s && d < s) || (e && d > e)) out.push(`${f} = ${d} is outside the SOP window (${r.plain_english})`);
    } else if (t === "regex") {
      let rx = null;
      try { rx = new RegExp(r.pattern || ""); } catch (e) { rx = null; }
      if (rx && !rx.test(sv.slice(0, 2000))) out.push(`${f} ('${sv}') does not match the SOP format ${r.pattern}`);
    } else if (t === "required") {
      if (!sv) out.push(`${f} is required by the SOP but is empty`);
    }
  }
  return out;
}

function check(payload, opts) {
  const exitOnFail = !opts || opts.exit !== false;
  let doc, problems;
  try {
    doc = JSON.parse(fs.readFileSync(RULES_PATH, "utf-8"));
    const data = Object.assign({}, payload || {});
    for (const key of doc.secret_keys || []) {
      if (!data[key]) data[key] = process.env["UNDERSTUDY_SECRET_" + key.toUpperCase()] || data[key] || "";
    }
    problems = violations(data, doc);
  } catch (e) {
    doc = { rules: [] };
    problems = [e.code === "ENOENT" ? "governance.json is missing - the SOP rules can't be checked" : "governance.json: " + e.message];
  }
  if (problems.length) {
    const msg = "GOVERN FAILED: " + problems.join("; ");
    console.log(msg);
    if (exitOnFail) process.exit(1);
    throw new Error(msg);
  }
  console.log(`GOVERN OK: ${(doc.rules || []).length} SOP rule(s) checked against the payload`);
  return true;
}

function checkFile(payloadPath, opts) {
  const p = payloadPath || process.env.UNDERSTUDY_PAYLOAD || "payload.json";
  // a relative path: the working folder, else next to this file or the suite folder above it
  const found = path.isAbsolute(p) ? p : [process.cwd(), __dirname, path.dirname(__dirname), path.dirname(path.dirname(__dirname))]
    .map((d) => path.join(d, p)).find((f) => fs.existsSync(f)) || path.join(__dirname, p);
  return check(JSON.parse(fs.readFileSync(found, "utf-8")), opts);
}

module.exports = { check, checkFile, violations, parseNumber, parseDate };

if (require.main === module) checkFile(process.argv[2]);
'''

JAVA_CHECKER = r'''import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.time.LocalDate;
import java.time.format.DateTimeFormatter;
import java.time.format.DateTimeParseException;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.regex.PatternSyntaxException;

/**
 * Understudy SOP governance check - generated by Understudy, copied unchanged into every suite.
 * The rules you confirmed in Understudy's Govern step are in governance.json. The test calls
 * {@code UnderstudyGovernance.check(payload)} before step 1: a payload the SOP forbids fails the run before anything is
 * typed or sent. Prints "GOVERN OK: ..." or "GOVERN FAILED: ..." (and throws). No extra dependency.
 */
public final class UnderstudyGovernance {
    private UnderstudyGovernance() {}

    /** governance.json in the working folder (the project root under Maven / Gradle), else up to two folders above. */
    static String findRules() {
        java.nio.file.Path d = Paths.get("").toAbsolutePath();
        for (int i = 0; i < 3 && d != null; i++, d = d.getParent()) {
            if (Files.isRegularFile(d.resolve("governance.json"))) return d.resolve("governance.json").toString();
        }
        return "governance.json";
    }

    public static void check(Map<String, Object> payload) {
        String path = System.getenv().getOrDefault("UNDERSTUDY_GOVERNANCE", findRules());
        Map<String, Object> doc;
        List<String> problems;
        try {
            doc = asMap(new Json(new String(Files.readAllBytes(Paths.get(path)), StandardCharsets.UTF_8)).value());
            Map<String, Object> data = new HashMap<>(payload == null ? Map.of() : payload);
            for (Object k : asList(doc.get("secret_keys"))) {
                String key = String.valueOf(k);
                Object cur = data.get(key);
                if (cur == null || String.valueOf(cur).isEmpty()) {
                    data.put(key, System.getenv().getOrDefault("UNDERSTUDY_SECRET_" + key.toUpperCase(Locale.ROOT), ""));
                }
            }
            problems = violations(data, doc);
        } catch (java.io.IOException e) {
            doc = Map.of();
            problems = List.of("governance.json is missing - the SOP rules can't be checked");
        }
        if (!problems.isEmpty()) {
            String msg = "GOVERN FAILED: " + String.join("; ", problems);
            System.out.println(msg);
            throw new IllegalStateException(msg);
        }
        System.out.println("GOVERN OK: " + asList(doc.get("rules")).size() + " SOP rule(s) checked against the payload");
    }

    static String norm(Object s) {
        return (s == null ? "" : String.valueOf(s)).replaceAll("\\s+", " ").trim().toLowerCase(Locale.ROOT);
    }

    static Double parseNumber(Object s) {
        if (s instanceof Boolean) return null;
        if (s instanceof Number) return ((Number) s).doubleValue();
        String str = s == null ? "" : String.valueOf(s);
        Matcher m = Pattern.compile("-?[0-9][0-9,]*(?:\\.[0-9]+)?").matcher(str);
        if (!m.find()) return null;
        double val;
        try { val = Double.parseDouble(m.group().replace(",", "")); } catch (NumberFormatException e) { return null; }
        String suffix = str.substring(m.end(), Math.min(str.length(), m.end() + 2)).trim().toLowerCase(Locale.ROOT);
        if (suffix.startsWith("k") && !suffix.startsWith("kg")) val *= 1000;
        else if (suffix.startsWith("m") && !suffix.startsWith("mo")) val *= 1000000;
        return val;
    }

    private static final String[] DATE_FORMATS = {"yyyy-MM-dd", "d/M/yyyy", "M/d/yyyy", "d.M.yyyy", "d MMM yyyy",
        "d MMMM yyyy", "MMM d yyyy", "MMMM d yyyy", "yyyy/M/d", "d-M-yyyy", "yyyy-MM-dd'T'HH:mm:ss"};

    static LocalDate parseDate(Object s) {
        String t = (s == null ? "" : String.valueOf(s)).trim().replaceAll("\\.$", "").replace(",", "");
        t = t.replaceAll("(\\d)(st|nd|rd|th)", "$1").replaceAll("(?i)\\bsept\\b", "Sep");
        for (String f : DATE_FORMATS) {
            try {
                return LocalDate.parse(t.length() > 19 ? t.substring(0, 19) : t,
                    DateTimeFormatter.ofPattern(f, Locale.ENGLISH).withResolverStyle(java.time.format.ResolverStyle.SMART));
            } catch (DateTimeParseException e) {
                // next format
            }
        }
        return null;
    }

    static List<String> violations(Map<String, Object> payload, Map<String, Object> doc) {
        List<String> out = new ArrayList<>();
        for (Object o : asList(doc.get("rules"))) {
            Map<String, Object> r = asMap(o);
            String f = String.valueOf(r.get("field"));
            String t = String.valueOf(r.get("rule_type"));
            String pe = String.valueOf(r.get("plain_english"));
            if (!payload.containsKey(f)) {
                if (t.equals("required") || t.equals("role_access")) {
                    out.add("'" + f + "' is required by the SOP (" + pe + ") but is not in the payload");
                }
                continue;
            }
            Object v = payload.get(f);
            String sv = v == null ? "" : String.valueOf(v).trim();
            switch (t) {
                case "allowed_values":
                case "role_access": {
                    boolean ok = false;
                    for (Object a : asList(r.get("values"))) if (norm(a).equals(norm(sv))) ok = true;
                    if (!ok) out.add("'" + (sv.isEmpty() ? "(empty)" : sv) + "' is not an approved value for " + f + " (" + pe + ")");
                    break;
                }
                case "denied_values":
                    for (Object b : asList(r.get("values"))) {
                        String nb = norm(b);
                        if (!nb.isEmpty() && (nb.equals(norm(sv)) || norm(sv).contains(nb))) {
                            out.add("'" + sv + "' is on the SOP's blocked list for " + f + " (" + pe + ")");
                            break;
                        }
                    }
                    break;
                case "max":
                case "min": {
                    Double num = parseNumber(v);
                    Double lim = parseNumber(r.get("limit"));
                    double l = lim == null ? 0 : lim;
                    boolean excl = Boolean.TRUE.equals(r.get("exclusive"));
                    if (num == null) out.add(f + " must be a number to check the SOP " + (t.equals("max") ? "limit" : "minimum") + " ('" + sv + "')");
                    else if (t.equals("max") && (excl ? num >= l : num > l)) out.add(f + " = " + sv + " is over the SOP limit (" + pe + ")");
                    else if (t.equals("min") && (excl ? num <= l : num < l)) out.add(f + " = " + sv + " is under the SOP minimum (" + pe + ")");
                    break;
                }
                case "date_range": {
                    LocalDate d = parseDate(v);
                    LocalDate s = r.get("start_date") == null ? null : parseDate(r.get("start_date"));
                    LocalDate e = r.get("end_date") == null ? null : parseDate(r.get("end_date"));
                    if (d == null) out.add(f + " ('" + sv + "') is not a valid date");
                    else if ((s != null && d.isBefore(s)) || (e != null && d.isAfter(e))) out.add(f + " = " + d + " is outside the SOP window (" + pe + ")");
                    break;
                }
                case "regex":
                    try {
                        Object p = r.get("pattern");
                        if (!Pattern.compile(p == null ? "" : String.valueOf(p)).matcher(sv.length() > 2000 ? sv.substring(0, 2000) : sv).find()) {
                            out.add(f + " ('" + sv + "') does not match the SOP format " + p);
                        }
                    } catch (PatternSyntaxException e) {
                        // invalid pattern: skipped, like Understudy's rule engine
                    }
                    break;
                case "required":
                    if (sv.isEmpty()) out.add(f + " is required by the SOP but is empty");
                    break;
                default:
                    break;
            }
        }
        return out;
    }

    @SuppressWarnings("unchecked")
    static Map<String, Object> asMap(Object o) { return o instanceof Map ? (Map<String, Object>) o : Map.of(); }

    @SuppressWarnings("unchecked")
    static List<Object> asList(Object o) { return o instanceof List ? (List<Object>) o : List.of(); }

    /** A minimal JSON reader for governance.json (written by Understudy) - no extra dependency. */
    static final class Json {
        private final String s;
        private int i;

        Json(String s) { this.s = s; }

        Object value() {
            ws();
            char c = s.charAt(i);
            if (c == '{') return obj();
            if (c == '[') return arr();
            if (c == '"') return str();
            if (s.startsWith("true", i)) { i += 4; return Boolean.TRUE; }
            if (s.startsWith("false", i)) { i += 5; return Boolean.FALSE; }
            if (s.startsWith("null", i)) { i += 4; return null; }
            int st = i;
            while (i < s.length() && "+-0123456789.eE".indexOf(s.charAt(i)) >= 0) i++;
            return Double.parseDouble(s.substring(st, i));
        }

        private void ws() { while (i < s.length() && Character.isWhitespace(s.charAt(i))) i++; }

        private Map<String, Object> obj() {
            Map<String, Object> m = new LinkedHashMap<>();
            i++;
            ws();
            if (s.charAt(i) == '}') { i++; return m; }
            while (true) {
                ws();
                String k = str();
                ws();
                i++;                                   // ':'
                m.put(k, value());
                ws();
                if (s.charAt(i++) == '}') return m;    // else ','
            }
        }

        private List<Object> arr() {
            List<Object> l = new ArrayList<>();
            i++;
            ws();
            if (s.charAt(i) == ']') { i++; return l; }
            while (true) {
                l.add(value());
                ws();
                if (s.charAt(i++) == ']') return l;    // else ','
            }
        }

        private String str() {
            StringBuilder b = new StringBuilder();
            i++;                                       // opening quote
            while (true) {
                char c = s.charAt(i++);
                if (c == '"') return b.toString();
                if (c != '\\') { b.append(c); continue; }
                char e = s.charAt(i++);
                switch (e) {
                    case 'n': b.append('\n'); break;
                    case 't': b.append('\t'); break;
                    case 'r': b.append('\r'); break;
                    case 'b': b.append('\b'); break;
                    case 'f': b.append('\f'); break;
                    case 'u': b.append((char) Integer.parseInt(s.substring(i, i + 4), 16)); i += 4; break;
                    default: b.append(e);
                }
            }
        }
    }
}
'''
