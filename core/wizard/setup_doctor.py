"""Setup doctor (8.3): why the AI kept asking the same question, and how the setup now fixes it itself.

Field reports from 8.2 - all of them loops where "retry" could never succeed:

* **WebdriverIO**: the stack had ``webdriverio`` + ``mocha`` + ``chai`` and the run command ``npx wdio run
  wdio.conf.js``, but the WDIO test runner also needs ``@wdio/cli``, ``@wdio/local-runner``, ``@wdio/mocha-framework``
  and ``@wdio/spec-reporter``. The AI kept asking "should I add @wdio/local-runner to the manifest?" - and no answer
  could add a package, because the resolve step had no such change.
* **Selenium (Python)** with pytest: the AI wrote ``conftest.py`` + ``test_smoke.py`` while the run command expected
  ``tests/test_automation.py``; with two candidate files Understudy could not pick the entry file, the test was
  rejected, and the AI asked about "the correct filename".
* **Robot Framework + Browser library**: ``robotframework-browser`` needs ``rfbrowser init`` (its Node.js side and
  Playwright browsers) after ``pip install``. Without it every run ends with "Calling method '_end_suite' of listener
  'Browser' failed".
* **"The sandbox is isolated and cannot download browser binaries"**: wrong for Understudy's sandbox, which has the
  server's network (the AI didn't know which sandbox ran - SERVER FACTS now says so).

What this module adds:

1. ``ensure_companions(spec)`` - before anything is installed, the packages / post-install steps the chosen RUN
   COMMAND needs are added deterministically (WDIO runner packages in the same version line as webdriverio, pytest for
   a pytest run, ``python -m Browser.entry init`` for the Browser library, ...).
2. ``known_fix(failure, spec)`` - for a failed install / smoke test, a deterministic fix from the output: a missing
   module -> ``add_packages``; a missing entry file -> rewrite the test with exact instructions; Browser library not
   initialised -> add its init step.
3. ``clean_changes`` - the change keys ``add_packages``, ``post_install`` and ``rewrite_smoke``
   that the AI's resolve step and the admin's answers can now use (validated: package grammar, runner allow-list).

``tool_setup._halt`` applies an automatic fix (deterministic first, then the AI's ``auto_fix``) up to
``MAX_AUTO_FIXES`` times per setup before it asks the admin anything - and says in the chat what it changed.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from core.wizard.frameworks import _PKG_RE, RUNNER_ALLOWLIST, FrameworkSpec, Package

MAX_AUTO_FIXES = 4
MAX_ADDED_PACKAGES = 6

# Python import name -> PyPI distribution (only where they differ or are commonly needed by automation tools)
PY_MODULE_TO_DIST = {
    "selenium": "selenium", "pytest": "pytest", "yaml": "PyYAML", "webdriver_manager": "webdriver-manager",
    "bs4": "beautifulsoup4", "dotenv": "python-dotenv", "requests": "requests", "behave": "behave",
    "robot": "robotframework", "SeleniumLibrary": "robotframework-seleniumlibrary", "Browser": "robotframework-browser",
    "RequestsLibrary": "robotframework-requests", "playwright": "playwright", "pytest_bdd": "pytest-bdd",
    "pytest_html": "pytest-html", "allure": "allure-pytest", "httpx": "httpx", "jsonschema": "jsonschema",
    "appium": "Appium-Python-Client", "PIL": "pillow", "lxml": "lxml", "faker": "Faker", "locust": "locust",
}
_NPM_BUILTINS = {"assert", "buffer", "child_process", "crypto", "events", "fs", "http", "https", "net", "os", "path",
                 "process", "stream", "url", "util", "zlib", "timers", "readline", "worker_threads", "node:test"}
_WDIO_FRAMEWORK = {"mocha": "@wdio/mocha-framework", "jasmine": "@wdio/jasmine-framework",
                   "cucumber": "@wdio/cucumber-framework"}


def _names(spec: FrameworkSpec) -> Dict[str, str]:
    return {p.name.lower(): p.version for p in spec.packages}


def _plain_version(v: str) -> str:
    return re.sub(r"^[=v^~<>! ]+", "", (v or "").strip())


def _major(v: str) -> Optional[int]:
    m = re.match(r"^(\d+)", _plain_version(v))
    return int(m.group(1)) if m else None


def companion_version(ecosystem: str, name: str, anchor: str) -> str:
    """The companion's version: the anchor version itself when the registry has it (WDIO packages are released in
    lockstep), else the newest release of the same major line, else the anchor (offline)."""
    want = _plain_version(anchor)
    if not want:
        return ""
    try:
        from core.wizard.tool_stack import package_versions
        vs = package_versions(ecosystem, name).get("versions") or []
    except Exception:
        vs = []
    if not vs or want in vs:
        return want
    mj = _major(want)
    line = [v for v in vs if _major(v) == mj]
    return line[0] if line else want


def _add(spec: FrameworkSpec, name: str, version: str, notes: List[str], why: str) -> bool:
    if name.lower() in _names(spec) or len(spec.packages) >= 15:
        return False
    v = version
    if spec.ecosystem == "pip" and v and v[0] not in "<>=!~":
        v = "==" + v
    spec.packages.append(Package(name, v))
    notes.append(f"Added {name}{' ' + _plain_version(version) if version else ''}: {why}.")
    return True


def _run_text(spec: FrameworkSpec) -> str:
    return " ".join(spec.run_command).lower()


def ensure_companions(spec: FrameworkSpec, lookup_versions: bool = True) -> Tuple[FrameworkSpec, List[str]]:
    """Adds what the RUN COMMAND needs but the stack forgot. Returns (spec, notes). Never removes anything."""
    notes: List[str] = []
    names = _names(spec)
    run = _run_text(spec)
    eco = spec.ecosystem
    ver = (lambda n, a: companion_version(eco, n, a)) if lookup_versions else (lambda n, a: _plain_version(a))
    if eco == "npm":
        wdio_v = names.get("webdriverio") or names.get("@wdio/cli") or ""
        uses_runner = bool(re.search(r"(^|\s)(wdio|@wdio/cli)(\s|$)", run)) or "wdio.conf" in run
        if uses_runner and (names.get("webdriverio") is not None or names.get("@wdio/cli") is not None):
            fw = "cucumber" if "@cucumber/cucumber" in names else "jasmine" if "jasmine" in names else "mocha"
            for pkg, why in (("@wdio/cli", "the run command starts the WebdriverIO test runner (wdio)"),
                             ("@wdio/local-runner", "the WebdriverIO runner starts the tests through it"),
                             (_WDIO_FRAMEWORK[fw], f"WebdriverIO runs {fw} tests through this adapter"),
                             ("@wdio/spec-reporter", "prints the test results")):
                _add(spec, pkg, ver(pkg, wdio_v) if wdio_v else "", notes, why)
        if re.search(r"(^|\s)mocha(\s|$)", run) and "mocha" not in names:
            _add(spec, "mocha", "", notes, "the run command starts mocha")
        if re.search(r"(^|\s)jest(\s|$)", run) and "jest" not in names:
            _add(spec, "jest", "", notes, "the run command starts jest")
        if "cucumber-js" in run and "@cucumber/cucumber" not in names:
            _add(spec, "@cucumber/cucumber", "", notes, "the run command starts cucumber-js")
        if re.search(r"playwright\s+test", run) and "@playwright/test" not in names:
            _add(spec, "@playwright/test", names.get("playwright", ""), notes, "the run command is 'playwright test'")
    elif eco == "pip":
        if (spec.run_command[:1] == ["pytest"] or "-m pytest" in run) and "pytest" not in names:
            _add(spec, "pytest", "", notes, "the run command starts pytest")
        if spec.run_command[:1] == ["robot"] and "robotframework" not in names:
            _add(spec, "robotframework", "", notes, "the run command starts robot")
        if spec.run_command[:1] == ["behave"] and "behave" not in names:
            _add(spec, "behave", "", notes, "the run command starts behave")
        if "robotframework-browser" in names and "robotframework-browser-batteries" not in names \
                and not any("Browser.entry" in " ".join(c) or "rfbrowser" in " ".join(c)
                                                         for c in spec.post_install):
            spec.post_install = list(spec.post_install) + [["python", "-m", "Browser.entry", "init"]]
            notes.append("Added 'rfbrowser init' (python -m Browser.entry init): the Browser library needs its Node.js "
                         "side and Playwright browsers after pip install.")
    elif eco == "gem":
        if spec.run_command[:1] == ["bundle"] and "bundler" not in names:
            _add(spec, "bundler", "", notes, "the run command uses bundle")
        if re.search(r"(^|\s)rspec(\s|$)", run) and "rspec" not in names:
            _add(spec, "rspec", "", notes, "the run command starts rspec")
        if re.search(r"(^|\s)cucumber(\s|$)", run) and "cucumber" not in names:
            _add(spec, "cucumber", "", notes, "the run command starts cucumber")
    return spec, notes


def os_dependencies(spec: FrameworkSpec) -> List[Dict[str, str]]:
    """Programs a stack needs beyond its ecosystem's own (shown in the manifest, checked before the install)."""
    out = []
    if spec.ecosystem == "pip" and "robotframework-browser" in _names(spec) \
            and "robotframework-browser-batteries" not in _names(spec):
        out += [{"name": "Node.js", "check": "binary", "value": "node", "why": "the Browser library runs Playwright on Node.js"},
                {"name": "npm", "check": "binary", "value": "npm", "why": "rfbrowser init installs the Browser library's Node.js side"}]
    return out


# ---------------------------------------------------------------------------------------------------------------
# Known failures -> automatic fixes
# ---------------------------------------------------------------------------------------------------------------
_NPM_MISSING = [re.compile(r"Cannot find module ['\"]((?:@[a-z0-9._~-]+/)?[a-z0-9._~-]+)(?:/[^'\"]*)?['\"]"),
                re.compile(r"Cannot find package ['\"]((?:@[a-z0-9._~-]+/)?[a-z0-9._~-]+)['\"]"),
                re.compile(r"Couldn'?t (?:find|initiali[sz]e) (?:plugin |service )?\"?((?:@wdio/)[a-z0-9-]+)"),
                re.compile(r"Couldn'?t find plugin \"([a-z0-9-]+)\" (framework|reporter|service)"),
                re.compile(r"ERR_MODULE_NOT_FOUND.*?['\"]((?:@[a-z0-9._~-]+/)?[a-z0-9._~-]+)['\"]")]
_PY_MISSING = re.compile(r"ModuleNotFoundError: No module named ['\"]([A-Za-z0-9_]+)")
_RB_MISSING = re.compile(r"cannot load such file -- ([a-z0-9_-]+)")
_ENTRY_MISSING = re.compile(
    r"(?i)(file or directory not found: \S+|can't open file ['\"]?[^'\"\s]+|No such file or directory: ['\"][^'\"]+\.(?:py|js|ts|rb|robot)"
    r"|no (?:test )?specs? (?:files? )?(?:were )?found|No test files found|spec file\(s\) not found|Pattern .* did not match any file"
    r"|Error: No test files found|File or directory to execute does not exist|Parsing '[^']+' failed|collected 0 items"
    r"|Cannot find module ['\"](?:\.{0,2}/|[A-Za-z]:\\)[^'\"]+['\"]|no tests ran)")
_RF_BROWSER = re.compile(r"(?i)listener 'Browser' failed|rfbrowser init|Browser library is not initiali|playwright-process|"
                         r"Could not connect to the playwright process|wrapper/node_modules")
_RF_INIT_FAIL = re.compile(r"(?i)Problem installing node dependencies|rfbrowser init|Browser\.entry")
_PW_BROWSER = re.compile(r"(?i)Executable doesn't exist at|please run the following command to download new browsers|"
                         r"npx playwright install|playwright install")
_DRIVER_DOWNLOAD = re.compile(r"(?i)Unable to obtain (?:driver|browser)|selenium.?manager.*(?:fail|error)|"
                              r"Unable to download|Failed to download|Download failure|error sending request for url|"
                              r"(googlechromelabs|storage\.googleapis\.com|chrome-for-testing|edgedl|msedgedriver\.azureedge)"
                              r".*(ENOTFOUND|EAI_AGAIN|getaddrinfo|timed? ?out|refused|unreachable)|"
                              r"(ENOTFOUND|EAI_AGAIN|getaddrinfo).*(googlechromelabs|storage\.googleapis|chrome-for-testing)|"
                              r"cannot download browser|can ?not download (the )?(browser|driver)|isolated.*download|"
                              r"network is unreachable")


def _npm_package_root(name: str) -> str:
    return name if name.startswith("@") else name.split("/")[0]


def missing_packages(output: str, spec: FrameworkSpec) -> List[str]:
    """Package names the failed output says are missing (only ones that look like real packages of this ecosystem)."""
    out: List[str] = []
    text = output or ""
    have = _names(spec)
    if spec.ecosystem == "npm":
        for rx in _NPM_MISSING:
            for m in rx.finditer(text):
                n = m.group(1)
                if len(m.groups()) >= 2 and m.group(2) in ("framework", "reporter", "service"):
                    n = f"@wdio/{n}-{m.group(2)}"
                n = _npm_package_root(n)
                if n in _NPM_BUILTINS or n.startswith("node:") or n.lower() in have or not _PKG_RE["npm"].match(n):
                    continue
                if n not in out:
                    out.append(n)
    elif spec.ecosystem == "pip":
        for m in _PY_MISSING.finditer(text):
            mod = m.group(1)
            dist = PY_MODULE_TO_DIST.get(mod) or PY_MODULE_TO_DIST.get(mod.split(".")[0])
            if dist and dist.lower() not in have and dist not in out:
                out.append(dist)
    elif spec.ecosystem == "gem":
        for m in _RB_MISSING.finditer(text):
            first = m.group(1).split("/")[0]
            n = {"selenium": "selenium-webdriver", "selenium-webdriver": "selenium-webdriver"}.get(first, first.replace("_", "-"))
            if n.lower() not in have and _PKG_RE["gem"].match(n) and n not in out:
                out.append(n)
    return out[:MAX_ADDED_PACKAGES]


_NATIVE_BUILD = re.compile(r"(?i)Microsoft Visual C\+\+ \d|Failed building wheel for ([A-Za-z0-9_.-]+)|"
                           r"failed-wheel-build|error: command '?(gcc|cc|cl\.exe|clang)'? failed|"
                           r"Failed to build ([A-Za-z0-9_.-]+)|gyp ERR|node-gyp")
_SAFETY_REJECTED = re.compile(r"(?i)rejected by Understudy'?s safety check")


def _robot_used(spec: FrameworkSpec) -> bool:
    return spec.run_command[:1] == ["robot"] or "robotframework" in _names(spec) or spec.entry_file.endswith(".robot")


def _robot_syntax_error(text: str) -> bool:
    from core.wizard import robot_syntax
    return bool(robot_syntax.ERROR_RX.search(text or ""))


def rewrite_hints(problems_text: str, spec: FrameworkSpec) -> str:
    """Concrete instructions for the AI when Understudy rejected its smoke test, so the rewrite doesn't repeat it."""
    names = _names(spec)
    hints = []
    t = problems_text or ""
    if "starts other programs" in t:
        if "robotframework" in names:
            hints.append("Do not use subprocess: in a Python entry file run the suite with the Robot Framework API - "
                         "`import robot; rc = robot.run('smoke.robot', outputdir='.', loglevel='INFO'); sys.exit(rc)` - "
                         "and write the test itself in smoke.robot.")
        elif spec.ecosystem == "pip" and "pytest" in names:
            hints.append("Do not use subprocess: call pytest from Python with `import pytest; sys.exit(pytest.main(['-q', "
                         "__file__]))` or put the test in a test_*.py file.")
        else:
            hints.append("Do not start other programs (no subprocess / child_process / ProcessBuilder): use the tool's own "
                         "library API in the entry file.")
    if "reads environment variables" in t:
        hints.append("Read only SMOKE_URL / SMOKE_API_URL from the environment.")
    if "contacts an external host" in t:
        hints.append("Do not put any http(s) URL other than localhost in the code; take the page URL from SMOKE_URL.")
    if "entry file" in t:
        hints.append(f"Write the test in exactly {spec.entry_file}.")
    if _robot_used(spec) and ("argument(s) but needs" in t or _robot_syntax_error(t)):
        from core.wizard import robot_syntax
        hints.append(robot_syntax.RULES)
    return " ".join(hints)


def newest_version(ecosystem: str, name: str, current: str) -> Optional[str]:
    """The newest published version of a package that is newer than ``current`` (registry lookup; None offline)."""
    try:
        from core.wizard.tool_stack import package_versions
        vs = package_versions(ecosystem, name).get("versions") or []
    except Exception:
        return None
    cur = _plain_version(current)
    for v in vs:                                             # newest first
        if not cur or _ver_key(v) > _ver_key(cur):
            return v
    return None


def _ver_key(v: str) -> Tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:4])


def known_fix(failure: Dict[str, Any], spec: FrameworkSpec, sandbox_kind: str = "local",
              tried: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    """A deterministic fix for a failed setup step, or None. {key, summary, changes, needs_review}."""
    tried = list(tried or [])
    stage = failure.get("stage")
    text = f"{failure.get('message', '')}\n{failure.get('output', '')}"
    if stage not in ("smoke_test", "install"):
        return None
    pk = missing_packages(text, spec)
    if pk:
        key = "add:" + ",".join(sorted(pk))
        if key not in tried:
            anchor = (_names(spec).get("webdriverio") or "") if spec.ecosystem == "npm" else ""
            adds = [{"name": n, "version": companion_version(spec.ecosystem, n, anchor) if n.startswith("@wdio/") and anchor else ""}
                    for n in pk]
            return {"key": key, "changes": {"add_packages": adds},
                    "summary": f"The test needs {', '.join(pk)}, which the stack didn't install. I added "
                               f"{'it' if len(pk) == 1 else 'them'} to the setup and am installing again - the reviewed "
                               f"smoke test is kept."}
    if spec.ecosystem == "pip" and "robotframework-browser" in _names(spec) and stage == "install" \
            and ("Browser.entry" in str(failure.get("command") or "") or _RF_INIT_FAIL.search(text)) \
            and "robotframework-browser-batteries" not in _names(spec):
        key = "rfbrowser-batteries"
        if key not in tried:
            v = _plain_version(_names(spec).get("robotframework-browser") or "")
            return {"key": key,
                    "changes": {"add_packages": [{"name": "robotframework-browser-batteries", "version": v}],
                                "post_install_remove": [RF_INIT], "post_install": [RF_BATTERIES_INSTALL]},
                    "summary": "'rfbrowser init' could not install the Browser library's Node.js dependencies with npm "
                               "on this server. I switched to robotframework-browser-batteries, which ships those "
                               "dependencies pre-built (no npm install needed), and only download Chromium - installing "
                               "again."}
    if spec.ecosystem == "pip" and "robotframework-browser" in _names(spec) and _RF_BROWSER.search(text):
        key = "rfbrowser-init"
        if key not in tried and not any("Browser.entry" in " ".join(c) for c in spec.post_install):
            return {"key": key, "changes": {"post_install": [["python", "-m", "Browser.entry", "init"]]},
                    "summary": "The Robot Framework Browser library was installed but never initialised (it needs "
                               "'rfbrowser init' for its Node.js side and browsers). I added that step and am installing "
                               "again."}
    if _PW_BROWSER.search(text) and not any("playwright" in " ".join(c) and "install" in c for c in spec.post_install):
        key = "playwright-install"
        cmd = ["npx", "playwright", "install", "chromium"] if spec.ecosystem == "npm" else \
            ["python", "-m", "playwright", "install", "chromium"] if spec.ecosystem == "pip" else None
        if cmd and key not in tried:
            return {"key": key, "changes": {"post_install": [cmd]},
                    "summary": "Playwright's browser build was missing. I added the browser download to the install."}
    if stage == "smoke_test" and _SAFETY_REJECTED.search(text):
        key = "safety-rewrite"
        if key not in tried:
            hint = rewrite_hints(text, spec) or "Keep every Understudy rule."
            return {"key": key, "needs_review": True, "changes": {"rewrite_smoke": "Understudy's safety check rejected "
                    "your test: " + text.strip()[:400] + " " + hint},
                    "summary": "Understudy's safety check rejected the AI's smoke test. I asked the AI to rewrite it with "
                               "precise instructions (" + (hint[:160] or "same rules") + ") - please review it again."}
    if stage == "install" and spec.ecosystem in ("pip", "npm") and _NATIVE_BUILD.search(text) and spec.packages:
        # A package must be compiled because no pre-built wheel exists for this Python / Node (e.g. greenlet for an old
        # Playwright on Python 3.14 -> "Microsoft Visual C++ 14.0 or greater is required"). A newer release of the
        # tool usually ships pre-built binaries; the version is the admin's decision, so this is offered, never forced.
        key = "native-build"
        if key not in tried:
            core = spec.packages[0]
            newer = newest_version(spec.ecosystem, core.name, core.version)
            import platform
            where = f"Python {platform.python_version()}" if spec.ecosystem == "pip" else "this Node.js"
            if newer:
                return {"key": key, "needs_admin": True, "changes": {"version": newer},
                        "summary": f"A dependency of {core.name} {_plain_version(core.version) or ''} has no pre-built "
                                   f"package for {where} on this server, so it would have to be compiled (that needs "
                                   f"C/C++ build tools). {core.name} {newer} ships pre-built packages - install that "
                                   f"version instead?"}
    if stage == "smoke_test" and _robot_used(spec) and _robot_syntax_error(text):
        # 8.4.1: the AI's .robot file is laid out wrongly (keyword and arguments on different lines, single spaces,
        # wrong argument names). Re-running it can't help and a plain "rewrite" repeats it - the rewrite gets the exact
        # Robot error and the layout rules; Understudy also repairs the unambiguous cases itself before every run.
        key = "robot-syntax"
        if key not in tried:
            from core.wizard import robot_syntax
            err = "; ".join(dict.fromkeys(m.group(0) for m in robot_syntax.ERROR_RX.finditer(text)))[:300]
            return {"key": key, "needs_review": True,
                    "changes": {"rewrite_smoke": f"Robot Framework rejected the test's syntax ({err}). "
                                                 + robot_syntax.RULES + " Use the argument names of the INSTALLED "
                                                 "library version (Browser 'Fill Text' takes selector and txt) - or "
                                                 "simply pass them positionally."},
                    "summary": f"Robot Framework rejected the smoke test's syntax ({err[:120]}) - a layout mistake in "
                               f"the AI's test, not a server problem, so retrying the same test can't pass. I asked the "
                               f"AI to rewrite it with the exact Robot Framework rules - please review it again."}
    if stage == "smoke_test" and _ENTRY_MISSING.search(text):
        key = "entry-file"
        if key not in tried:
            run = " ".join(spec.run_command).replace("{entry}", spec.entry_file)
            extra = ""
            if spec.run_command[:1] == ["pytest"] or "-m pytest" in run:
                extra = " Name the test function test_smoke(); put fixtures in the same file (no conftest.py)."
            elif spec.run_command[:1] == ["robot"]:
                extra = " It must be a .robot file with a *** Test Cases *** section."
            return {"key": key, "needs_review": True,
                    "changes": {"rewrite_smoke": f"The run command '{run}' could not find the test. Write the whole "
                                                 f"smoke test in exactly {spec.entry_file} (path relative to the "
                                                 f"script folder) and keep config files to the ones the run command "
                                                 f"reads.{extra}"},
                    "summary": f"The run command could not find the test file. I asked the AI to rewrite the smoke test "
                               f"in exactly {spec.entry_file} - please review it again."}
    return None


# ---------------------------------------------------------------------------------------------------------------
# Change validation (shared by the AI resolve step, the admin's answers and automatic fixes)
# ---------------------------------------------------------------------------------------------------------------
_VER_OK = re.compile(r"^[A-Za-z0-9.+_-]{1,40}$")


def clean_add_packages(items: Any, spec: FrameworkSpec, problems: List[str]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    rx = _PKG_RE.get(spec.ecosystem)
    have = _names(spec)
    for it in (items if isinstance(items, list) else [])[:MAX_ADDED_PACKAGES]:
        if isinstance(it, str):
            it = {"name": it}
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").strip()
        ver = _plain_version(str(it.get("version") or ""))
        if not name or (rx and not rx.match(name)):
            problems.append(f"'{name[:60]}' is not a valid {spec.ecosystem} package name.")
            continue
        if ver and not _VER_OK.match(ver):
            problems.append(f"'{ver[:40]}' is not a valid version for {name}.")
            continue
        if name.lower() in have:
            continue
        out.append({"name": name, "version": ver})
    return out


def clean_post_install(items: Any, problems: List[str]) -> List[List[str]]:
    from core.wizard.frameworks import _INLINE_CODE_FLAGS, _TOKEN_RE
    out: List[List[str]] = []
    for cmd in (items if isinstance(items, list) else [])[:3]:
        if not isinstance(cmd, list) or not cmd:
            continue
        cmd = [str(t) for t in cmd][:10]
        if cmd[0] not in RUNNER_ALLOWLIST:
            problems.append(f"A post-install step must start with one of the allowed runners (got '{cmd[0]}').")
            continue
        if any(not _TOKEN_RE.match(t) or ".." in t or t.startswith("/") or t in _INLINE_CODE_FLAGS
               or t.lower().startswith(("http:", "https:", "file:", "git+")) for t in cmd):
            problems.append(f"Unsafe post-install step: {' '.join(cmd)[:80]}")
            continue
        out.append(cmd)
    return out


def apply_overrides(spec: FrameworkSpec, ov: Dict[str, Any]) -> FrameworkSpec:
    """Applies the add_packages / post_install overrides of a setup to its spec (idempotent)."""
    have = _names(spec)
    for p in ov.get("add_packages") or []:
        if p["name"].lower() not in have and len(spec.packages) < 15:
            v = p.get("version") or ""
            if spec.ecosystem == "pip" and v and v[0] not in "<>=!~":
                v = "==" + v
            spec.packages.append(Package(p["name"], v))
            have[p["name"].lower()] = v
    drop = [list(c) for c in ov.get("post_install_remove") or []]
    if drop:
        spec.post_install = [c for c in spec.post_install if list(c) not in drop]
    for cmd in ov.get("post_install") or []:
        if cmd not in spec.post_install:
            spec.post_install = list(spec.post_install) + [list(cmd)]
    return spec


# ---------------------------------------------------------------------------------------------------------------
# Robot Framework Browser library: the real npm error lives in rfbrowser.log
# ---------------------------------------------------------------------------------------------------------------
RF_INIT = ["python", "-m", "Browser.entry", "init"]
RF_BATTERIES_INSTALL = ["python", "-m", "Browser.entry", "install", "chromium"]


def rfbrowser_log_tail(env_dir: str, lines: int = 60) -> str:
    """'rfbrowser init' prints only 'RuntimeError: Problem installing node dependencies'; npm's actual error (proxy,
    certificate, Node version, long path, EPERM ...) is written to rfbrowser.log inside the Browser package."""
    import glob
    import os
    hits = []
    for pat in ("**/Browser/rfbrowser.log", "**/Browser/**/rfbrowser.log", "rfbrowser.log"):
        hits += glob.glob(os.path.join(env_dir, pat), recursive=True)
    hits = sorted(set(hits), key=lambda p: os.path.getmtime(p), reverse=True)
    if not hits:
        return ""
    try:
        with open(hits[0], encoding="utf-8", errors="replace") as fh:
            tail = fh.read().splitlines()[-lines:]
    except OSError:
        return ""
    return "\n--- rfbrowser.log (npm's own output) ---\n" + "\n".join(tail)
