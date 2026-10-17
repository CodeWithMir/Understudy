"""Self-healing for ENVIRONMENT failures: repair the user's chosen runtime instead of abandoning it.

When a run fails because the framework runtime itself can't start (browser not found, driver/browser version mismatch,
missing package, browser binaries not downloaded ...), Understudy now tries to make *that* framework work, one repair
per user-approved retry, in this order:

  1. Diagnose the sub-cause from the failure reason and the log tail (``diagnose``).
  2. Pick the next untried repair for it (``plan_repair``):
       BROWSER_NOT_FOUND / DRIVER_MISMATCH (Selenium, Robot Framework)
           -> point the tool at a real browser found on this machine: Google Chrome, then Microsoft Edge, then Chromium,
              then Playwright's bundled Chromium (CHROME_BINARY / SELENIUM_BROWSER / EDGE_BINARY), and for a
              driver/browser version mismatch hide stale chromedriver copies on PATH so Selenium Manager fetches the
              matching driver.
       PUPPETEER_BROWSER -> PUPPETEER_EXECUTABLE_PATH to a detected Chrome/Edge/Chromium.
       PLAYWRIGHT_BROWSER -> download the browser build that the installed Playwright expects.
       CYPRESS_BINARY -> ``cypress install``.
       MISSING_PACKAGE / UNKNOWN -> wipe the isolated environment and reinstall the dependencies.
       TOOLCHAIN_MISSING -> nothing can be repaired automatically: tell the user exactly what to install.
       JDK_MISMATCH (8.2: "invalid target release: 17", UnsupportedClassVersionError, JAVA_HOME pointing at an old JDK)
           -> a JDK path the user typed in their correction, then every JDK on this machine that is new enough
              (JAVA_HOME + PATH), then compile for the JDK that is there (maven.compiler.release), then download a
              checksum-verified Temurin JDK into Understudy's runtimes folder.
  2b. (8.2) When the rules don't recognise the failure, have run out of repairs, or the user typed a correction, the
      RUNTIME_REPAIR agent proposes ONE repair from the same closed set of actions (environment variable pointing at an
      existing path, PATH folder, runtime download, compiler release, reinstall, browser download). Its answer is
      validated like an admin's answer in the tool setup - nothing the model invents runs (``ai_plan_repair``).
  3. When every repair for the sub-cause has been tried, report ``exhausted``: the user is then offered (never forced)
     to verify the recorded process with Understudy's built-in Playwright driver, while the generated code for their
     chosen tool is still exported.
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import sys
from typing import Any, Dict, List, Optional, Tuple

BROWSER_NOT_FOUND = re.compile(
    r"(?i)browser path is not a valid file|cannot find (?:Chrome|Chromium) binary|no chrome binary|"
    r"Unable to obtain driver|NoSuchDriverException|driver_location|Chrome failed to start|DevToolsActivePort|"
    r"chrome not reachable|Could not find Chrome|Failed to launch the browser process|SessionNotCreated|"
    r"Could not find expected browser|unknown error: cannot find|msedge.*not found")
DRIVER_MISMATCH = re.compile(r"(?i)only supports (?:Chrome|Microsoft Edge|MSEdge) version|This version of \w*driver")
PLAYWRIGHT_BROWSER = re.compile(r"(?i)Executable doesn't exist|playwright install|browserType\.launch")
CYPRESS_BINARY = re.compile(r"(?i)Cypress (?:binary|executable) (?:is )?(?:not found|missing)|cypress install|Cypress failed to start")
MISSING_PACKAGE = re.compile(r"(?i)ModuleNotFoundError|No module named|Cannot find module|ClassNotFoundException|"
                             r"Could not resolve dependencies|ImportError|MODULE_NOT_FOUND|package .* is not installed")
TOOLCHAIN = re.compile(r"(?i)WinError 2|command not found|is not recognized as an internal or external command|"
                       r"Runner not found|No such file or directory: '(?:node|npx|npm|mvn|gradle|java|ruby|go|dotnet)'")

JDK_MISMATCH = re.compile(r"(?i)invalid (?:target|source) release|release version \d+ not supported|"
                          r"UnsupportedClassVersionError|compiled by a more recent version of the Java Runtime|"
                          r"source option \d+ is no longer supported|No compiler is provided in this environment|"
                          r"running on a JRE rather than a JDK|JAVA_HOME (?:environment variable )?is not defined correctly|"
                          r"JAVA_HOME is set to an invalid directory")

NATIVE_BUILD = re.compile(r"(?i)Microsoft Visual C\+\+ \d+(\.\d+)? or greater is required|Failed building wheel for|"
                          r"failed-wheel-build|Failed to build installable wheels|Failed to build [A-Za-z0-9_.-]+$|"
                          r"error: command '?(gcc|cc|cl\.exe|clang)'? failed|gyp ERR! (build|stack)")
PROJECT_AMBIGUOUS = re.compile(r"(?i)MSB1011|more than one project or solution file|"
                               r"Specify which project or solution file to use|more than one project file")
SELENIUM_LIKE = {"selenium-python", "robot-framework"}
JAVA_ECOSYSTEM_HINT = re.compile(r"(?i)maven|mvn|\.java\b|javac|surefire|junit|testng|org\.apache|gradle")
_PATH_IN_TEXT = re.compile(r"(?<![\w:/.])((?:/[A-Za-z0-9._+ -]+)+/?|[A-Za-z]:\\[A-Za-z0-9 ._()\\+-]+)")


# ---------------------------------------------------------------------------
# Browser discovery
# ---------------------------------------------------------------------------
def _first_file(paths: List[str]) -> Optional[str]:
    for p in paths:
        if p and os.path.isfile(p):
            return p
    return None


def _win_candidates(rel: str) -> List[str]:
    roots = [os.environ.get(k) for k in ("PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "LOCALAPPDATA")]
    roots += [r"C:\Program Files", r"C:\Program Files (x86)"]
    return [os.path.join(r, rel) for r in roots if r]


def playwright_chromium() -> Optional[str]:
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or (
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "ms-playwright") if os.name == "nt"
        else os.path.expanduser("~/Library/Caches/ms-playwright") if sys.platform == "darwin"
        else os.path.expanduser("~/.cache/ms-playwright"))
    pats = ["chromium-*/chrome-win/chrome.exe", "chromium-*/chrome-linux/chrome",
            "chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium"]
    hits: List[str] = []
    for pat in pats:
        hits += glob.glob(os.path.join(root, pat))
    return sorted(hits)[-1] if hits else None


def detect_browsers() -> Dict[str, Optional[str]]:
    """Real browser executables available on the machine running the backend."""
    found: Dict[str, Optional[str]] = {}
    if os.name == "nt":
        found["chrome"] = _first_file(_win_candidates(r"Google\Chrome\Application\chrome.exe"))
        found["edge"] = _first_file(_win_candidates(r"Microsoft\Edge\Application\msedge.exe"))
        found["chromium"] = _first_file(_win_candidates(r"Chromium\Application\chrome.exe"))
        found["firefox"] = _first_file(_win_candidates(r"Mozilla Firefox\firefox.exe"))
    elif sys.platform == "darwin":
        found["chrome"] = _first_file(["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                                       os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")])
        found["edge"] = _first_file(["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"])
        found["chromium"] = _first_file(["/Applications/Chromium.app/Contents/MacOS/Chromium"])
        found["firefox"] = _first_file(["/Applications/Firefox.app/Contents/MacOS/firefox"])
    else:
        found["chrome"] = next((p for p in (shutil.which(n) for n in ("google-chrome", "google-chrome-stable")) if p), None) \
            or _first_file(["/opt/google/chrome/chrome"])
        found["edge"] = next((p for p in (shutil.which(n) for n in ("microsoft-edge", "microsoft-edge-stable", "msedge")) if p), None)
        found["chromium"] = next((p for p in (shutil.which(n) for n in ("chromium", "chromium-browser")) if p), None)
        found["firefox"] = shutil.which("firefox")
    env_chrome = os.environ.get("CHROME_BINARY")
    if env_chrome and os.path.isfile(env_chrome):
        found["chrome"] = env_chrome
    found["playwright_chromium"] = playwright_chromium()
    return found


# 9.9: a setup plan names a browser the Linux way ("google-chrome"); on Windows Chrome is chrome.exe under Program
# Files and never on PATH, so a plain PATH lookup said "not installed" on a machine that has it.
BROWSER_NAMES = {"google-chrome": "chrome", "google-chrome-stable": "chrome", "chrome": "chrome", "chrome.exe": "chrome",
                 "chromium": "chromium", "chromium-browser": "chromium", "msedge": "edge", "msedge.exe": "edge",
                 "microsoft-edge": "edge", "microsoft-edge-stable": "edge", "firefox": "firefox", "firefox.exe": "firefox"}
_BROWSER_ENV = {"chrome": ("CHROME_BIN", "CHROME_BINARY"), "chromium": ("CHROME_BIN", "CHROME_BINARY"),
                "edge": ("EDGE_BINARY",), "firefox": ("FIREFOX_BINARY",)}


def browser_kind(name: str) -> Optional[str]:
    return BROWSER_NAMES.get(os.path.basename(str(name or "")).strip().lower())


def find_browser(name: str, env: Optional[Dict[str, str]] = None) -> Optional[str]:
    """The installed browser a dependency name means, on any OS: an explicit *_BINARY / CHROME_BIN setting first,
    then the standard install places (Program Files, /Applications, PATH). A Chromium build counts for Chrome and the
    other way round."""
    kind = browser_kind(name)
    if not kind:
        return None
    for k in _BROWSER_ENV[kind]:
        v = (env or {}).get(k) or os.environ.get(k)
        if v and os.path.isfile(v):
            return v
    found = detect_browsers()
    order = {"chrome": ("chrome", "chromium"), "chromium": ("chromium", "chrome", "playwright_chromium")}.get(kind, (kind,))
    return next((found[k] for k in order if found.get(k)), None)


def selenium_manager_fetches_browsers(packages: List[Any]) -> bool:
    """Selenium 4.11+ (its Selenium Manager) downloads Chrome for Testing / Firefox itself when none is installed."""
    for p in packages or []:
        name = (getattr(p, "name", None) or (p.get("name") if isinstance(p, dict) else "") or "").lower()
        if name not in ("selenium", "selenium-webdriver", "org.seleniumhq.selenium:selenium-java"):
            continue
        v = re.match(r"^\D*(\d+)\.(\d+)", str(getattr(p, "version", None) or (p.get("version") if isinstance(p, dict) else "") or ""))
        return not v or (int(v.group(1)), int(v.group(2))) >= (4, 11)     # unpinned installs the newest
    return False


# ---------------------------------------------------------------------------
# Diagnosis + repair planning
# ---------------------------------------------------------------------------
def diagnose(execution: Dict[str, Any], log_tail: str) -> str:
    text = f"{execution.get('failure_reason') or ''}\n{log_tail[-6000:]}"
    if JDK_MISMATCH.search(text):
        return "JDK_MISMATCH"
    if NATIVE_BUILD.search(text):
        return "NATIVE_BUILD"
    if PROJECT_AMBIGUOUS.search(text):
        return "PROJECT_AMBIGUOUS"
    if TOOLCHAIN.search(text):
        return "TOOLCHAIN_MISSING"
    if DRIVER_MISMATCH.search(text):
        return "DRIVER_MISMATCH"
    if PLAYWRIGHT_BROWSER.search(text):
        return "PLAYWRIGHT_BROWSER"
    if CYPRESS_BINARY.search(text):
        return "CYPRESS_BINARY"
    if BROWSER_NOT_FOUND.search(text):
        return "BROWSER_NOT_FOUND"
    if MISSING_PACKAGE.search(text):
        return "MISSING_PACKAGE"
    return "UNKNOWN_RUNTIME"


def _browser_options(framework_id: str, found: Dict[str, Optional[str]]) -> List[Tuple[str, Dict[str, str], str]]:
    """(repair id, env, human description) candidates in preference order."""
    opts: List[Tuple[str, Dict[str, str], str]] = []
    if framework_id in SELENIUM_LIKE:
        if found.get("chrome"):
            opts.append(("chrome", {"SELENIUM_BROWSER": "chrome", "CHROME_BINARY": found["chrome"]},
                         f"Point Selenium at the installed Google Chrome ({found['chrome']})"))
        if found.get("edge"):
            opts.append(("edge", {"SELENIUM_BROWSER": "edge", "EDGE_BINARY": found["edge"]},
                         f"Use the installed Microsoft Edge instead ({found['edge']})"))
        if found.get("chromium"):
            opts.append(("chromium", {"SELENIUM_BROWSER": "chrome", "CHROME_BINARY": found["chromium"]},
                         f"Use the installed Chromium ({found['chromium']})"))
        if found.get("playwright_chromium"):
            opts.append(("playwright_chromium", {"SELENIUM_BROWSER": "chrome", "CHROME_BINARY": found["playwright_chromium"]},
                         f"Use the Chromium build bundled with Playwright ({found['playwright_chromium']})"))
    elif framework_id == "puppeteer":
        for name in ("chrome", "edge", "chromium", "playwright_chromium"):
            if found.get(name):
                opts.append((name, {"PUPPETEER_EXECUTABLE_PATH": found[name]},
                             f"Launch Puppeteer with the browser at {found[name]}"))
    return opts


def _empty(cause: str) -> Dict[str, Any]:
    return {"cause": cause, "repair_id": None, "env": {}, "strip_driver_path": False, "reinstall": False,
            "install_browsers": False, "exhausted": False, "explanation": "", "path_prepend": [],
            "download_runtime": None, "java_release": None, "source": "rules",
            # 8.3
            "switch_version": None, "run_args": [], "clean_project_files": False, "ask_admin": None}


def paths_in(feedback: Optional[List[str]]) -> List[str]:
    """Absolute paths the user typed in their corrections (newest first), e.g. 'my JDK is C:\\Program Files\\Java\\jdk-17'."""
    out: List[str] = []
    for text in reversed(list(feedback or [])):
        for p in _PATH_IN_TEXT.findall(str(text)):
            p = p.strip().rstrip("\\/.,;") or p
            if len(p) > 2 and p not in out:
                out.append(p)
    return out


def _jdk_home_from(path: str) -> Optional[str]:
    """A JDK home from a typed path: the home itself, its bin folder, or the java executable."""
    from core.wizard import jdk
    cands = [path, os.path.dirname(path), os.path.dirname(os.path.dirname(path))]
    for c in cands:
        if c and os.path.isfile(jdk._exe(c)):
            return c
    return None


def _browser_from(path: str) -> Optional[str]:
    base = os.path.basename(path).lower()
    if os.path.isfile(path) and re.match(r"^(chrome|google-chrome(-stable)?|chromium(-browser)?|msedge|microsoft-edge|"
                                         r"chrome\.exe|msedge\.exe)$", base):
        return path
    return None


def _jdk_options(text: str, feedback: Optional[List[str]], tried: List[str],
                 jdks: Optional[List[Dict[str, Any]]] = None,
                 current: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The JDK repair ladder (8.2)."""
    from core.wizard import jdk, runtimes
    out = _empty("JDK_MISMATCH")
    need = jdk.required_release(text) or jdk.DEFAULT_RELEASE
    # 1. a JDK the user pointed us at
    for p in paths_in(feedback):
        home = _jdk_home_from(p)
        if not home:
            continue
        info = jdk.probe(jdk._exe(home)) or {}
        key = f"jdk:user:{home}"
        if key in tried:
            continue
        out.update(repair_id=key, env={"JAVA_HOME": home}, path_prepend=[os.path.join(home, "bin")], explanation=(
            f"Use the JDK you pointed to ({home}" + (f", Java {info.get('major')}" if info.get("major") else "") +
            "): JAVA_HOME and PATH are set to it for this run, so Maven compiles with it."))
        return out
    # 2. a JDK on this machine that is new enough (Maven uses JAVA_HOME, which often points at an old JDK)
    jdks = jdks if jdks is not None else jdk.find_jdks(need)
    cur_home = os.path.realpath(str((current or {}).get("home") or "")) if current else ""
    for info in jdks:
        home = str(info["home"])
        key = f"jdk:{home}"
        if key in tried or (cur_home and os.path.realpath(home) == cur_home):
            continue
        out.update(repair_id=key, env={"JAVA_HOME": home}, path_prepend=[os.path.join(home, "bin")], explanation=(
            f"The build needs Java {need}+, but Maven is using "
            + (f"Java {current['major']} ({current['home']}, from {current.get('source')})" if current else "an older JDK")
            + f". Java {info['major']} is installed at {home}: Understudy points JAVA_HOME and PATH at it for this run."))
        return out
    # 3. compile for the JDK that is there (Selenium 4 / most tools need 11+)
    major = int((current or {}).get("major") or 0)
    if major >= 11 and major < need and f"java_release:{major}" not in tried:
        out.update(repair_id=f"java_release:{major}", java_release=major, explanation=(
            f"No Java {need}+ JDK was found on this machine. The generated code doesn't need Java {need} features, so "
            f"Understudy sets the compiler release in pom.xml to {major} (the JDK Maven uses) - the exported ZIP gets "
            f"the same pom.xml."))
        return out
    # 4. download a verified JDK into Understudy's runtimes folder
    key = "jdk-17" if need <= 17 else "jdk-21"
    if runtimes.is_supported(key) and f"download:{key}" not in tried:
        out.update(repair_id=f"download:{key}", download_runtime=key, explanation=(
            f"No usable Java {need}+ JDK was found on this machine. Understudy downloads Eclipse Temurin "
            f"{key.split('-')[1]} (checksum verified) into its own runtimes folder - nothing is installed system-wide - "
            f"and runs Maven with it."))
        return out
    out.update(exhausted=True, explanation=(
        f"The build needs Java {need} or newer, and Understudy has tried every JDK it could find"
        + (f", compiling for Java {major}" if major >= 11 else "") + " and downloading Temurin. Install a JDK "
        f"{need}+ (https://adoptium.net), set JAVA_HOME to it, then retry - or type its folder in your correction "
        "(e.g. 'my JDK is C:\\Program Files\\Java\\jdk-17') and the AI will use it."))
    return out


def plan_repair(framework_id: str, execution: Dict[str, Any], log_tail: str, tried: List[str],
                found: Optional[Dict[str, Optional[str]]] = None, feedback: Optional[List[str]] = None,
                jdks: Optional[List[Dict[str, Any]]] = None, current_jdk: Optional[Dict[str, Any]] = None,
                versions: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Next repair to try. Returns {cause, repair_id, env, path_prepend, strip_driver_path, reinstall,
    install_browsers, download_runtime, java_release, switch_version, run_args, clean_project_files, ask_admin,
    exhausted, explanation, source}. ``versions`` (8.3) = {current, allowed, newest, backend} of the tool's package."""
    cause = diagnose(execution, log_tail)
    out = _empty(cause)
    text = f"{execution.get('failure_reason') or ''}\n{log_tail[-6000:]}"
    if cause == "JDK_MISMATCH":
        return _jdk_options(text, feedback, tried, jdks, current_jdk)
    if cause == "PROJECT_AMBIGUOUS" and "clean_project_files" not in tried:
        out.update(repair_id="clean_project_files", clean_project_files=True, explanation=(
            "The run folder holds more than one project file (the generated code added its own next to Understudy's "
            "Automation.csproj), so 'dotnet test' doesn't know which one to build (MSB1011). Understudy removes the "
            "extra project / solution files and keeps only the one built from the approved packages."))
        return out
    if cause == "NATIVE_BUILD":
        v = versions or {}
        cur = str(v.get("current") or "")
        import platform
        py = platform.python_version()
        newer_allowed = [a for a in v.get("allowed") or [] if _vk(a) > _vk(cur)]
        if newer_allowed and f"version:{newer_allowed[0]}" not in tried:
            target = sorted(newer_allowed, key=_vk)[-1]
            out.update(repair_id=f"version:{target}", switch_version=target, explanation=(
                f"A package {v.get('package') or 'the tool'} {cur} depends on has no pre-built version for Python {py} "
                f"on this machine, so pip tried to compile it and needs Microsoft C++ Build Tools. Version {target}, "
                f"which your admin also allows, ships pre-built packages - Understudy switches this session to it."))
            return out
        want = v.get("backend") or v.get("newest")
        if want and _vk(want) > _vk(cur) and "ask_admin:allow_version" not in tried:
            out.update(repair_id="ask_admin:allow_version", ask_admin={
                "action": "allow_version", "version": want,
                "request": f"Please allow {v.get('package')} {want} for this tool: {cur} can't be installed on Python "
                           f"{py} without Microsoft C++ Build Tools (a dependency has no pre-built package)."},
                explanation=(f"{v.get('package') or 'The tool'} {cur} can't be installed on this machine's Python {py}: a "
                             f"dependency would have to be compiled (Microsoft C++ Build Tools). {want} works here, but "
                             "your admin hasn't allowed it. Approve this to ask your admin to allow it - or ask them to "
                             "install the build tools (https://visualstudio.microsoft.com/visual-cpp-build-tools/)."))
            return out
        out.update(exhausted=True, explanation=(
            f"A dependency must be compiled for Python {py} and this machine has no C/C++ build tools. Ask your admin to "
            "install Microsoft C++ Build Tools (https://visualstudio.microsoft.com/visual-cpp-build-tools/) or to allow "
            "a newer version of the tool, then retry."))
        return out
    # a browser binary the user pointed at (any browser-type failure)
    if cause in ("BROWSER_NOT_FOUND", "DRIVER_MISMATCH") and feedback:
        for p in paths_in(feedback):
            b = _browser_from(p)
            if b and f"browser:user:{b}" not in tried:
                env = ({"PUPPETEER_EXECUTABLE_PATH": b} if framework_id == "puppeteer" else
                       {"SELENIUM_BROWSER": "edge" if "edge" in b.lower() else "chrome",
                        ("EDGE_BINARY" if "edge" in b.lower() else "CHROME_BINARY"): b, "CHROME_BIN": b})
                out.update(repair_id=f"browser:user:{b}", env=env, explanation=f"Use the browser you pointed to ({b}).")
                return out
    if cause == "TOOLCHAIN_MISSING":
        m = re.search(r"(?i)Could not start '(\w+)|No such file or directory: '(\w+)'|'(\w+)' is not recognized|"
                      r"(\w+): command not found", text)
        prog = next((g for g in (m.groups() if m else []) if g), "")
        if prog in ("mvn", "gradle"):
            key = "maven-3" if prog == "mvn" else "gradle-8"
            if f"download:{key}" not in tried:
                label = "Apache Maven 3" if prog == "mvn" else "Gradle 8"
                out.update(repair_id=f"download:{key}", download_runtime=key, explanation=(
                    f"'{prog}' isn't on this machine's PATH. Understudy downloads the newest {label} (checksum verified) "
                    "into its own runtimes folder and runs the tool with it. Your admin can also register it for the "
                    "whole server in Mission Control > Package managers."))
                return out
        elif prog in ("java", "javac") or (not prog and JAVA_ECOSYSTEM_HINT.search(text)):
            return _jdk_options(text, feedback, tried, jdks, current_jdk)
        if prog in ("node", "npm", "npx") and "download:node-22" not in tried:
            out.update(repair_id="download:node-22", download_runtime="node-22", explanation=(
                "Node.js isn't on this machine's PATH. Understudy downloads the official Node.js 22 LTS (checksum "
                "verified) into its own runtimes folder and runs the tool with it."))
            return out
        out.update(exhausted=True, explanation=(
            "A command the tool needs (such as node/npx, java/mvn) is not installed on the machine running the Understudy "
            "backend, so there is nothing Understudy can repair by itself. Install it (see the dependency panel), restart "
            "the backend and retry."))
        return out
    if cause in ("BROWSER_NOT_FOUND", "DRIVER_MISMATCH") and framework_id in SELENIUM_LIKE | {"puppeteer"}:
        found = found if found is not None else detect_browsers()
        for rid, env, desc in _browser_options(framework_id, found):
            key = f"browser:{rid}" + (":nodriverpath" if cause == "DRIVER_MISMATCH" else "")
            if key in tried:
                continue
            out.update(repair_id=key, env=env, strip_driver_path=cause == "DRIVER_MISMATCH", explanation=(
                desc + (". A chromedriver on PATH doesn't match the browser version, so Understudy hides it and lets "
                        "Selenium Manager download the matching driver." if cause == "DRIVER_MISMATCH" else ".")))
            return out
        if "reinstall" not in tried:
            out.update(repair_id="reinstall", reinstall=True, explanation=(
                "No other usable browser was found on this machine; reinstalling the tool's packages in a fresh environment."))
            return out
        out.update(exhausted=True, explanation=(
            "Understudy tried every browser it could find on this machine and a clean reinstall, but the browser driver "
            "still can't start. Usually this means Selenium Manager can't download the driver (offline / proxy / firewall) "
            "or no Chrome/Edge is installed. Install Google Chrome or Microsoft Edge (or allow access to "
            "googlechromelabs.github.io and storage.googleapis.com), then retry - or type the browser's path in your "
            "correction and the AI will use it."))
        return out
    if cause == "PLAYWRIGHT_BROWSER" and "install_browsers" not in tried:
        out.update(repair_id="install_browsers", install_browsers=True,
                   explanation="Playwright's browser build is missing; downloading the version the installed Playwright expects.")
        return out
    if cause == "CYPRESS_BINARY" and "install_browsers" not in tried:
        out.update(repair_id="install_browsers", install_browsers=True,
                   explanation="The Cypress binary is missing; running 'cypress install'.")
        return out
    if "reinstall" not in tried:
        out.update(repair_id="reinstall", reinstall=True, explanation=(
            "A package the tool needs is missing or broken; reinstalling its dependencies in a fresh, isolated environment."
            if cause == "MISSING_PACKAGE" else
            "The tool's runtime failed before the first step; reinstalling its dependencies in a fresh environment."))
        return out
    out.update(exhausted=True, explanation=(
        "Understudy already reinstalled this tool's dependencies and it still can't start on this machine. Check the log "
        "above for the exact error, fix it and retry - or tell the AI in your correction what is different on this "
        "machine (a path, a proxy, a version) and it will prepare a repair for it."))
    return out


def _vk(v: str) -> Tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", str(v or ""))[:4])


def browser_install_command(framework_id: str) -> Optional[List[str]]:
    if framework_id == "playwright-python":
        return ["python", "-m", "playwright", "install", "chromium"]
    if framework_id == "playwright-js":
        return ["npx", "playwright", "install", "chromium"]
    if framework_id == "cypress":
        return ["npx", "cypress", "install"]
    if framework_id == "puppeteer":
        return ["npx", "puppeteer", "browsers", "install", "chrome"]
    return None


def strip_driver_dirs(path_value: str) -> str:
    """PATH without directories that contain a chromedriver/msedgedriver (stale drivers shadow Selenium Manager)."""
    keep = []
    for d in (path_value or "").split(os.pathsep):
        if not d:
            continue
        if any(os.path.isfile(os.path.join(d, n)) for n in
               ("chromedriver", "chromedriver.exe", "msedgedriver", "msedgedriver.exe")):
            continue
        keep.append(d)
    return os.pathsep.join(keep)


# ---------------------------------------------------------------------------
# 8.2: AI-advised repair (RUNTIME_REPAIR) - a closed set of actions, validated before the user approves it
# ---------------------------------------------------------------------------
AI_ENV_ALLOWED = {"JAVA_HOME", "CHROME_BINARY", "EDGE_BINARY", "CHROME_BIN", "SELENIUM_BROWSER",
                  "PUPPETEER_EXECUTABLE_PATH", "MAVEN_OPTS"}
_PATH_ENV = {"JAVA_HOME", "CHROME_BINARY", "EDGE_BINARY", "CHROME_BIN", "PUPPETEER_EXECUTABLE_PATH"}


def machine_facts(env: Optional[Dict[str, str]] = None, path_prepend: Optional[List[str]] = None) -> Dict[str, Any]:
    from core.wizard import jdk
    cur = jdk.maven_jdk(env, path_prepend)
    return {"os": os.name if os.name != "posix" else sys.platform,
            "maven_uses_jdk": {k: cur.get(k) for k in ("home", "major", "source")} if cur else None,
            "jdks_found": [{"home": i["home"], "major": i["major"]} for i in jdk.find_jdks(0)][:8],
            "browsers_found": {k: v for k, v in detect_browsers().items() if v},
            "on_path": {b: bool(shutil.which(b)) for b in ("java", "javac", "mvn", "node", "npm", "npx", "python3", "dotnet",
                                                           "ruby", "go")},
            "python": sys.version.split()[0], "platform": sys.platform,
            # 8.3: the AI used to blame "the system proxy" for Understudy's OWN per-run egress proxy
            "network": _network_fact()}


def _network_fact() -> str:
    try:
        from core.wizard import admin_policy
        mode = admin_policy.network_mode()
        bypass = ", ".join(admin_policy.proxy_bypass_hosts())
    except Exception:
        mode, bypass = "restricted", "localhost, 127.0.0.1, ::1"
    return (f"Every run goes through Understudy's own per-run egress proxy (HTTP_PROXY/HTTPS_PROXY set by Understudy, "
            f"network mode '{mode}'); NO_PROXY = {bypass}, so the tool's local driver on localhost is reached directly. "
            "A proxy or connection error in a run is therefore NOT the server's system proxy: for a target / host that is "
            "not allowed, ask_admin to add it to the allowed URLs or the run-proxy bypass hosts in Mission Control.")


def validate_ai_repair(data: Dict[str, Any], facts: Dict[str, Any], feedback: Optional[List[str]],
                       tried: List[str]) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """Turns the RUNTIME_REPAIR answer into a repair plan, or (None, problems)."""
    from core.wizard import runtimes
    problems: List[str] = []
    known_paths = set(paths_in(feedback))
    for j in facts.get("jdks_found") or []:
        known_paths.add(str(j["home"]))
    for b in (facts.get("browsers_found") or {}).values():
        known_paths.add(str(b))
    action = str(data.get("action") or "none")
    out = _empty(str(data.get("cause") or "AI"))
    out["source"] = "ai"
    expl = str(data.get("explanation") or "").strip()[:600]
    if action == "set_env":
        env: Dict[str, str] = {}
        for k, v in (data.get("env") or {}).items():
            k, v = str(k), str(v).strip()
            if k not in AI_ENV_ALLOWED:
                problems.append(f"{k} is not a variable self-healing may set")
                continue
            if k in _PATH_ENV:
                ok_path = os.path.isdir(v) if k == "JAVA_HOME" else os.path.isfile(v)
                if not ok_path:
                    problems.append(f"{v} does not exist on this machine")
                    continue
                if v not in known_paths and not v.startswith(runtimes.RUNTIMES_DIR):
                    problems.append(f"{v} was neither found by Understudy nor typed by you")
                    continue
            if k == "SELENIUM_BROWSER" and v not in ("chrome", "edge", "firefox"):
                problems.append("SELENIUM_BROWSER must be chrome, edge or firefox")
                continue
            if k == "MAVEN_OPTS" and re.search(r"[;&|`$<>]|-javaagent|-agentpath|-XX:OnError", v):
                problems.append("MAVEN_OPTS may only carry plain -D / -X options")
                continue
            env[k] = v
        if not env:
            return None, problems or ["the AI proposed no usable variable"]
        out["env"] = env
        if "JAVA_HOME" in env:
            out["path_prepend"] = [os.path.join(env["JAVA_HOME"], "bin")]
        out["repair_id"] = "ai:env:" + ",".join(f"{k}={v}" for k, v in sorted(env.items()))
    elif action == "path_prepend":
        dirs = [str(d) for d in (data.get("path_prepend") or [])[:4] if os.path.isdir(str(d))]
        if not dirs:
            return None, ["the AI proposed no existing folder"]
        out["path_prepend"] = dirs
        out["repair_id"] = "ai:path:" + os.pathsep.join(dirs)
    elif action == "download_runtime":
        key = str(data.get("download_runtime") or "")
        if not runtimes.is_supported(key):
            return None, [f"Understudy can download jdk-<N>, node-<N>, python-3.<N>, maven-3|4, gradle-<N>, dotnet-<N>, ruby-3, go-1 - not '{key}'"]
        from core.wizard import runtime_catalog                  # 9.9: a run's self-healing may apply it unattended -
        e = runtime_catalog.find(key)                            # only a line the official source lists as supported
        if not e or e["eol"]:
            return None, [f"'{key}' is not a currently supported release line - the admin can download it in "
                          "Mission Control > Environments"]
        out["download_runtime"] = key
        out["repair_id"] = f"download:{key}"
    elif action == "java_release":
        try:
            n = int(data.get("java_release"))
        except (TypeError, ValueError):
            return None, ["java_release must be a number"]
        if not 8 <= n <= 30:
            return None, ["java_release must be between 8 and 30"]
        out["java_release"] = n
        out["repair_id"] = f"java_release:{n}"
    elif action == "switch_version":                       # 8.3: only a version the admin allows
        v = str(data.get("version") or "").strip().lstrip("=v")
        allowed = [str(a) for a in (facts.get("versions") or {}).get("allowed") or []]
        if v not in allowed:
            return None, [f"version {v or '?'} is not one your admin allows ({', '.join(allowed) or 'none listed'}) - "
                          "use ask_admin to request it"]
        out.update(switch_version=v, repair_id=f"version:{v}")
    elif action == "run_args":                             # 8.3: e.g. dotnet test Automation.csproj
        from core.wizard.frameworks import _INLINE_CODE_FLAGS, _TOKEN_RE
        args = [str(a) for a in (data.get("run_args") or [])][:6]
        bad = [a for a in args if not _TOKEN_RE.match(a) or ".." in a or a.startswith("/") or a in _INLINE_CODE_FLAGS
               or a.lower().startswith(("http:", "https:", "file:"))]
        if not args or bad:
            return None, [f"unsafe or empty run arguments: {bad or args}"]
        out.update(run_args=args, repair_id="run_args:" + " ".join(args))
    elif action == "clean_project_files":
        out.update(clean_project_files=True, repair_id="clean_project_files")
    elif action == "ask_admin":                            # 8.3: the admin is the human in the loop for server changes
        req = str(data.get("admin_request") or data.get("human_steps") or "").strip()[:800]
        if len(req) < 10:
            return None, ["ask_admin needs admin_request: what exactly the admin must do"]
        act = {"action": "server_change", "request": req}
        v = str(data.get("version") or "").strip().lstrip("=v")
        if data.get("admin_action") == "allow_version" and re.match(r"^\d+(\.\d+){0,3}[A-Za-z0-9.-]*$", v):
            act.update(action="allow_version", version=v)
        out.update(ask_admin=act, repair_id=f"ask_admin:{act['action']}" + (f":{v}" if act["action"] == "allow_version" else ""))
    elif action == "reinstall":
        out.update(reinstall=True, repair_id="reinstall")
    elif action == "install_browsers":
        out.update(install_browsers=True, repair_id="install_browsers")
    else:
        steps = str(data.get("human_steps") or "").strip()[:800]
        out.update(exhausted=True, explanation=(expl + (" " + steps if steps else "")).strip()
                   or "The AI found nothing it can repair from here.")
        return out, []
    if out["repair_id"] in tried:
        return None, [f"'{out['repair_id']}' was already tried"]
    out["explanation"] = expl or f"AI repair: {out['repair_id']}"
    if data.get("diagnosis"):
        out["ai_diagnosis"] = str(data["diagnosis"])[:600]
    return out, []


def ai_plan_repair(spec, execution: Dict[str, Any], log_tail: str, tried: List[str], rule_cause: str,
                   feedback: Optional[List[str]] = None, facts: Optional[Dict[str, Any]] = None
                   ) -> Optional[Dict[str, Any]]:
    """One AI-advised repair, validated; None when the AI is unavailable or its answer is unusable."""
    try:
        from core.wizard.llm import ai_available, generate_json
        if not ai_available():
            return None
    except Exception:
        return None
    facts = facts if facts is not None else machine_facts()
    res = generate_json("RUNTIME_REPAIR", {
        "tool": json.dumps({"name": spec.name, "language": spec.language, "ecosystem": spec.ecosystem,
                            "run_command": spec.run_command, "packages": [f"{p.name} {p.version}" for p in spec.packages]}),
        "failure": str(execution.get("failure_reason") or "")[:1500],
        "log_tail": log_tail[-5000:], "rule_cause": rule_cause, "tried": ", ".join(tried) or "(none)",
        "facts": json.dumps(facts, default=str), "feedback": "\n".join(feedback or []) or "(none)",
    }, untrusted=["failure", "log_tail", "feedback"])
    if not res.ok or not isinstance(res.data, dict):
        return None
    data = dict(res.data, cause=rule_cause)
    plan, problems = validate_ai_repair(data, facts, feedback, tried)
    if plan is None:
        return None
    plan.update(agent_source=res.agent_source, model=res.model)
    return plan
