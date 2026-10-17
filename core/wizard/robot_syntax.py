"""Robot Framework syntax repair and lint (8.4.1).

AI-written ``.robot`` files keep failing on layout, not logic - Robot Framework separates a keyword from its arguments
with two or more spaces ON THE SAME LINE, and a call may only continue on the next line after ``...``. A model that
writes::

    Fill Text
    id=name    Understudy

gets "Keyword 'Browser.Fill Text' expected 2 to 3 arguments, got 0" (the next line becomes a call of a keyword named
``id=name``), and asking the same model again usually repeats it. So Understudy:

* ``repair(text)``: fixes the unambiguous cases itself - arguments on the following line(s) without ``...`` are joined
  with ``...``; a known keyword written with single spaces (``Fill Text id=name Understudy``) gets real separators;
  non-breaking spaces become spaces. Only calls of known library keywords that have too few arguments are touched.
* ``lint(text)``: what is still wrong (a known keyword with too few arguments), in words the AI can act on - used by
  the smoke-test check, so a broken test is rewritten before it ever runs.
"""
from __future__ import annotations

import posixpath
import re
from typing import Dict, List, Optional, Tuple

# minimum number of arguments of common library keywords (Browser, SeleniumLibrary, BuiltIn, RequestsLibrary)
MIN_ARGS: Dict[str, int] = {
    # Browser (Playwright)
    "fill text": 2, "type text": 2, "fill secret": 2, "type secret": 2, "click": 1, "go to": 1, "get text": 1,
    "wait for elements state": 1, "check checkbox": 1, "uncheck checkbox": 1, "select options by": 2,
    "press keys": 2, "get element": 1, "get attribute": 2, "hover": 1, "focus": 1, "set browser timeout": 1,
    "get element count": 1, "wait for condition": 1, "get property": 2, "upload file by selector": 2,
    # SeleniumLibrary
    "open browser": 1, "input text": 2, "input password": 2, "click element": 1, "click button": 1, "click link": 1,
    "element text should be": 2, "element should contain": 2, "element should be visible": 1,
    "wait until element is visible": 1, "wait until page contains": 1, "wait until page contains element": 1,
    "page should contain": 1, "page should contain element": 1, "create webdriver": 1, "select from list by value": 2,
    "select from list by label": 2, "title should be": 1, "location should be": 1, "textfield value should be": 2,
    # BuiltIn
    "should be equal": 2, "should be equal as strings": 2, "should be equal as integers": 2, "should contain": 2,
    "should not be equal": 2, "should not contain": 2, "should be true": 1, "should start with": 2,
    "should end with": 2, "should match": 2, "should match regexp": 2, "log": 1, "log to console": 1, "sleep": 1,
    "get environment variable": 1, "set suite variable": 1, "set test variable": 1, "set global variable": 1,
    "evaluate": 1, "run keyword": 1, "run keyword and return status": 1, "run keyword and expect error": 2,
    "length should be": 2, "should be empty": 1, "should not be empty": 1, "get length": 1, "convert to integer": 1,
    "wait until keyword succeeds": 3, "fail": 0, "set variable": 0,
    # RequestsLibrary
    "create session": 2, "get on session": 2, "post on session": 2, "status should be": 1, "get": 1, "post": 1,
    "request should be successful": 0,
}
# keywords whose arguments are optional - known, so a following line is never taken as their argument
ZERO_OK = {"new browser", "new page", "new context", "close browser", "close page", "close context", "get title",
           "get url", "take screenshot", "wait for load state", "close all browsers", "maximize browser window",
           "capture page screenshot", "reload", "go back", "log source", "no operation"}
LIBS = ("browser.", "seleniumlibrary.", "builtin.", "requestslibrary.", "collections.", "string.", "operatingsystem.")
CONTROL = {"FOR", "END", "IF", "ELSE", "ELSE IF", "WHILE", "TRY", "EXCEPT", "FINALLY", "BREAK", "CONTINUE", "RETURN",
           "VAR", "IN", "IN RANGE", "IN ENUMERATE", "IN ZIP", "GROUP"}
_SEP = re.compile(r" {2,}|\t+")
_SECTION = re.compile(r"^\*+\s*([A-Za-z ]+?)\s*\**\s*$")
_ASSIGN = re.compile(r"^[$@&]\{[^}]+\}\s*=?$")


def _norm(name: str) -> str:
    n = re.sub(r"[\s_]+", " ", name.strip().lower())
    for lib in LIBS:
        if n.startswith(lib):
            n = n[len(lib):]
    return n.strip()


def _cells(line: str) -> Tuple[str, List[str]]:
    """(indent, cells) of a body line; the indent is kept so the repaired file looks like the original."""
    body = line.lstrip(" \t")
    return line[:len(line) - len(body)], [c for c in _SEP.split(body.rstrip()) if c != ""]


def _call(cells: List[str]) -> Tuple[Optional[str], List[str]]:
    """(normalized keyword, its arguments) of a call line - skipping ``${x}=`` assignments."""
    i = 0
    while i < len(cells) and _ASSIGN.match(cells[i]):
        i += 1
    if i >= len(cells):
        return None, []
    return _norm(cells[i]), cells[i + 1:]


def _user_keywords(lines: List[str]) -> set:
    out, sec = set(), ""
    for line in lines:
        m = _SECTION.match(line.strip())
        if m:
            sec = m.group(1).lower()
            continue
        if sec.startswith("keyword") and line and not line[0].isspace() and not line.startswith("#"):
            out.add(_norm(line.strip()))
    return out


def _is_new_statement(cells: List[str], user_kws: set) -> bool:
    first = cells[0]
    if first == "..." or first.upper() in CONTROL or first.startswith("[") or first.startswith("#"):
        return True
    kw, _ = _call(cells)
    return kw is not None and (kw in MIN_ARGS or kw in ZERO_OK or kw in user_kws)


def _body_sections(lines: List[str]) -> List[bool]:
    """For each line: is it inside a *** Test Cases *** / *** Tasks *** / *** Keywords *** section?"""
    flags, sec = [], ""
    for line in lines:
        m = _SECTION.match(line.strip())
        if m:
            sec = m.group(1).lower()
            flags.append(False)
            continue
        flags.append(sec.startswith(("test case", "task", "keyword")))
    return flags


# A cell that starts with "#" is a COMMENT in Robot Framework - the rest of the row is ignored. CSS id selectors
# ("#name") therefore vanish: "Fill Text    #name    Understudy" reaches the library with 0 arguments. A real comment
# has a space after "#" ("# check the page"); a selector doesn't.
_HASH_SELECTOR = re.compile(r"^#[A-Za-z_][\w-]*(?:[>.:\[~+ ].*)?$")


def _escape_hash_selectors(lines: List[str], in_body: List[bool], notes: List[str]) -> None:
    """Escapes ``#selector`` arguments of known keywords as ``\\#selector`` (Robot's escape - the library gets "#name")."""
    last_known = False
    for i, line in enumerate(lines):
        if not in_body[i] or not line[:1].isspace() or not line.strip() or line.lstrip().startswith("#"):
            if not line.strip():
                last_known = False
            continue
        indent, cells = _cells(line)
        if cells[0] == "...":
            start, known = 1, last_known
        else:
            kw, args = _call(cells)
            start = len(cells) - len(args)
            known = bool(kw) and (kw in MIN_ARGS or kw in ZERO_OK)
            last_known = known
        if not known:
            continue
        fixed = []
        for j in range(start, len(cells)):
            if _HASH_SELECTOR.match(cells[j]) and not cells[j].startswith("# "):
                fixed.append(cells[j].split(" ")[0])
                cells[j] = "\\" + cells[j]
            elif cells[j].startswith("#"):
                break                                    # a genuine trailing comment
        if fixed:
            lines[i] = indent + "    ".join(cells)
            notes.append(f"line {i + 1}: escaped the selector{'s' if len(fixed) > 1 else ''} {', '.join(fixed)} as "
                         f"\\# - a cell starting with # is a comment in Robot Framework, so the keyword got no "
                         f"arguments")


def repair(text: str) -> Tuple[str, List[str]]:
    """(repaired text, one note per fix). Text that needs nothing comes back unchanged."""
    if not text:
        return text, []
    notes: List[str] = []
    fixed = text.replace(" ", " ")
    if fixed != text:
        notes.append("replaced non-breaking spaces with spaces")
    lines = fixed.split("\n")
    in_body = _body_sections(lines)
    user_kws = _user_keywords(lines)
    _escape_hash_selectors(lines, in_body, notes)
    out: List[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not in_body[i] or not line[:1].isspace() or not line.strip() or line.lstrip().startswith("#"):
            out.append(line)
            i += 1
            continue
        indent, cells = _cells(line)
        kw, args = _call(cells)
        k = len(cells) - len(args) - 1                       # index of the keyword cell
        # "Fill Text id=name Understudy": a known keyword followed by single-spaced arguments
        if kw and not args and kw not in MIN_ARGS and kw not in ZERO_OK and kw not in user_kws:
            words = cells[k].split(" ")
            for n in range(len(words) - 1, 0, -1):
                head, rest = " ".join(words[:n]), words[n:]
                need = MIN_ARGS.get(_norm(head))
                if need and len(rest) == need and not any(q in w for w in rest for q in "\"'"):
                    cells = cells[:k] + [head] + rest
                    line = indent + "    ".join(cells)
                    notes.append(f"line {i + 1}: put four spaces between '{head}' and its arguments")
                    kw, args = _call(cells)
                    break
        need = MIN_ARGS.get(kw or "", 0)
        out.append(line)
        i += 1
        if not need or len(args) >= need:
            continue
        got = len(args)
        joined = 0
        while got < need and i < len(lines) and in_body[i]:
            nxt = lines[i]
            if not nxt.strip() or not nxt[:1].isspace() or nxt.lstrip().startswith("#"):
                break
            nindent, ncells = _cells(nxt)
            if _is_new_statement(ncells, user_kws):
                break
            out.append(nindent + "...    " + "    ".join(ncells))
            got += len(ncells)
            joined += 1
            i += 1
        if joined:
            notes.append(f"line {i - joined}: the arguments of '{cells[len(cells) - len(args) - 1]}' were on the next "
                         f"line without '...' - joined them")
    return "\n".join(out), notes


def _until_comment(cells: List[str]) -> List[str]:
    out = []
    for c in cells:
        if c.startswith("#"):
            break
        out.append(c)
    return out


def lint(text: str) -> List[str]:
    """Known keywords called with too few arguments (after ``...`` continuations), as instructions for a rewrite."""
    problems: List[str] = []
    lines = (text or "").replace(" ", " ").split("\n")
    in_body = _body_sections(lines)
    i = 0
    while i < len(lines):
        line = lines[i]
        if not in_body[i] or not line[:1].isspace() or not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        _, cells = _cells(line)
        if cells and cells[0] == "...":
            i += 1
            continue
        kw, args = _call(cells)
        visible = _until_comment(args)
        hidden = len(visible) < len(args)
        n, j = len(visible), i + 1
        while j < len(lines) and in_body[j] and lines[j].strip().startswith("..."):
            more = _until_comment(_cells(lines[j])[1][1:])
            hidden = hidden or len(more) < len(_cells(lines[j])[1]) - 1
            n += len(more)
            j += 1
        need = MIN_ARGS.get(kw or "", 0)
        if need and n < need:
            name = cells[len(cells) - len(args) - 1]
            problems.append(f"line {i + 1}: '{name}' is called with {n} argument(s) but needs at least {need} - "
                            + ("an argument starts with '#', which Robot Framework reads as a COMMENT: write CSS id "
                               "selectors as id=name, css=#name or \\#name; " if hidden else "")
                            + "in Robot Framework the arguments go on the SAME line as the keyword, separated by 4 "
                              "spaces (e.g. 'Fill Text    id=name    Understudy'); continue a long call on the next "
                              "line only after '...'")
        i = j
    return problems


# 9.9.1: local imports in the Settings table. Robot Framework resolves a path in 'Library', 'Resource' or 'Variables'
# relative to the .robot file that imports it - an AI writing tests/test.robot often uses '../../run_automation.py'
# (one folder too many) and the run fails with "Library '..\..\run_automation.py' does not exist" and then
# "No keyword with name ... found".
_SETTINGS_HEADER = re.compile(r"(?i)^\*+\s*settings?\s*\**\s*$")
_ANY_HEADER = re.compile(r"^\*+\s*\w")
_IMPORT_SETTINGS = ("library", "resource", "variables")
_LOCAL_EXT = (".py", ".robot", ".resource", ".txt", ".yaml", ".yml", ".json")


def _local_import(value: str) -> Optional[str]:
    """The suite-relative part of a path-like import value ('${CURDIR}/x.py' -> 'x.py'), None for a library name."""
    v = value.replace("\\", "/")
    if v.upper().startswith("${CURDIR}"):
        v = v[len("${CURDIR}"):].lstrip("/")
    if "${" in v or "%{" in v or not v.lower().endswith(_LOCAL_EXT) or posixpath.isabs(v) or re.match(r"^[A-Za-z]:", v):
        return None
    return v


def _imports(files: Dict[str, str]):
    """(robot file, line index, setting cells, local path) for every local import in a Settings table."""
    for path, content in files.items():
        if not path.lower().endswith((".robot", ".resource")):
            continue
        in_settings = False
        for i, line in enumerate((content or "").splitlines()):
            if _ANY_HEADER.match(line):
                in_settings = bool(_SETTINGS_HEADER.match(line))
                continue
            if not in_settings or not line.strip() or line.lstrip().startswith("#"):
                continue
            _ind, cells = _cells(line)
            if len(cells) >= 2 and cells[0].strip().lower() in _IMPORT_SETTINGS:
                local = _local_import(cells[1])
                if local is not None:
                    yield path, i, cells, local


def _target(robot_path: str, local: str) -> str:
    return posixpath.normpath(posixpath.join(posixpath.dirname(robot_path), local))


def repair_imports(files: Dict[str, str]) -> Tuple[Dict[str, str], List[str]]:
    """Rewrites a local import whose path does not exist in the suite to the one file with that name that does
    (relative to the importing .robot file, '/' separators - Robot accepts them on Windows too)."""
    out, notes = dict(files), []
    names: Dict[str, List[str]] = {}
    for p in files:
        names.setdefault(posixpath.basename(p).lower(), []).append(p)
    edits: Dict[str, Dict[int, str]] = {}
    for path, i, cells, local in _imports(files):
        if _target(path, local) in files:
            continue
        cands = names.get(posixpath.basename(local).lower()) or []
        if len(cands) != 1:
            continue
        rel = posixpath.relpath(cands[0], posixpath.dirname(path) or ".")
        new_cells = [cells[0], rel] + cells[2:]
        edits.setdefault(path, {})[i] = "    ".join(new_cells)
        notes.append(f"{path}: {cells[0]} '{cells[1]}' does not exist - it is '{rel}' (paths are relative to the "
                     f".robot file)")
    for path, by_line in edits.items():
        lines = out[path].splitlines()
        for i, text in by_line.items():
            lines[i] = text
        out[path] = "\n".join(lines) + ("\n" if out[path].endswith("\n") else "")
    return out, notes


def lint_imports(files: Dict[str, str]) -> List[str]:
    """Local imports that point at no file of the suite (after repair_imports)."""
    return [f"{path}: {cells[0]} '{cells[1]}' does not exist in the suite - Robot Framework resolves it relative to "
            f"{path} (there it would be {_target(path, local)})"
            for path, _i, cells, local in _imports(files) if _target(path, local) not in files]


# 9.9.1: a Python keyword library (imported by a .robot file) runs INSIDE Robot Framework: sys.exit() there stops the
# whole run ("Execution stopped by user", no report), and print() goes to Robot's log, not the console - so Understudy
# would never see the STEP / GOVERN lines.
_SYS_EXIT = re.compile(r"(?m)^[^#\n]*\b(sys\.exit|os\._exit|exit|quit)\s*\(")
_PRINT_MARKER = re.compile(r"(?m)^[^#\n]*\bprint\s*\((?![^\n]*file\s*=\s*sys\.__stdout__)[^\n]*\b(STEP|GOVERN)\b")


def library_problems(files: Dict[str, str]) -> List[str]:
    out: List[str] = []
    libs = {_target(p, local) for p, _i, cells, local in _imports(files)
            if cells[0].strip().lower() == "library" and local.lower().endswith(".py")}
    for lib in sorted(libs):
        src = files.get(lib)
        if src is None or "understudy_governance" in posixpath.basename(lib):
            continue
        if _SYS_EXIT.search(src):
            out.append(f"{lib} is a Robot Framework keyword library: never call sys.exit()/exit() in it (it stops the "
                       "whole Robot run with 'Execution stopped by user' and no report) - raise AssertionError('...') "
                       "to fail the test")
        if _PRINT_MARKER.search(src):
            out.append(f"{lib} is a Robot Framework keyword library: print() there goes to Robot's log, not the "
                       "console - print the STEP / GOVERN lines with robot.api.logger.console('STEP 1 OK: ...')")
    return out


def repair_files(files: Dict[str, str]) -> Tuple[Dict[str, str], List[str]]:
    """repair() for every .robot / .resource file, then repair_imports(); (files, notes prefixed with the file name)."""
    out, notes = dict(files), []
    for path, content in files.items():
        if path.lower().endswith((".robot", ".resource")):
            fixed, n = repair(content)
            if n:
                out[path] = fixed
                notes += [f"{path}: {x}" for x in n]
    out, imp = repair_imports(out)
    return out, notes + imp


ERROR_RX = re.compile(r"(?i)expected \d+(?: to \d+)? arguments?, got \d+|No keyword with name .{1,120} found|"
                      r"Non-existing setting|Unrecognized section header|Invalid syntax|Keyword name cannot be empty|"
                      r"forgot to use enough whitespace")

RULES = ("Robot Framework syntax: a cell starting with '#' is a COMMENT - never write a CSS id selector as #name; "
         "use id=name (Browser), id:name (SeleniumLibrary), css=#name or \\#name. "
         "Put every argument on the SAME line as its keyword, separated by FOUR spaces "
         "('Fill Text    id=name    Understudy'); a call may continue on the next line only after '...'; never put "
         "a keyword alone on a line with its arguments below it. Browser library example: 'New Browser    chromium    "
         "headless=True', 'New Page    %{SMOKE_URL}', 'Fill Text    id=name    Understudy', 'Click    id=go', "
         "'Get Text    id=status    ==    CLICKED:Understudy', 'Close Browser'. "
         "Paths in Library / Resource / Variables are relative to the .robot file (tests/x.robot imports a file in the "
         "suite root as ../file.py). In a Python keyword library, never call sys.exit() - raise AssertionError - and "
         "print the STEP / GOVERN lines with robot.api.logger.console(...), because print() goes to Robot's log.")
