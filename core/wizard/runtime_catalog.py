"""Which runtime versions Understudy can download - read live from each official source (9.9).

Before 9.9 the downloadable versions were a fixed list (JDK 17 / 21, Node.js 20 / 22, Python 3.10-3.13 ...). Now a
download key names a release line (``jdk-11``, ``node-18``, ``python-3.9``, ``gradle-7``, ``dotnet-6``) and this
module lists the lines each source actually publishes, with their support status:

=========  ===============================================================  =====================================
kind       list from                                                        end of life
=========  ===============================================================  =====================================
Java       Adoptium ``/v3/info/available_releases`` (Eclipse Temurin)        LTS lines supported; a non-LTS line
                                                                            once a newer feature release exists
Node.js    the Node.js release schedule (``nodejs/Release`` schedule.json)  its ``end`` date
Python     python.org's yearly cadence (3.N: released Oct 2011+N, 5 years)  October 2016+N
.NET       Microsoft's ``releases-index.json``                              ``support-phase: eol``
Gradle     ``services.gradle.org/versions/current``                         older than the current major
Maven      Maven Central metadata (3.x, 4.x when released)                  -
Ruby / Go  the newest release (one line each)                               -
=========  ===============================================================  =====================================

The download itself (``runtimes.provision``) is unchanged: official archive, published checksum verified before
anything is extracted. A line that is end of life is offered too, but marked - the admin confirms it
(``package_managers.download(confirm_eol=True)``). Lists are cached for 6 hours; when a source can't be reached the
built-in list is used (``FALLBACK``). ``UNDERSTUDY_RUNTIME_CATALOG=offline`` skips the network (tests, air-gapped).
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

ADOPTIUM_RELEASES = "https://api.adoptium.net/v3/info/available_releases"
NODE_SCHEDULE = "https://raw.githubusercontent.com/nodejs/Release/main/schedule.json"
DOTNET_INDEX = "https://dotnetcli.blob.core.windows.net/dotnet/release-metadata/releases-index.json"
GRADLE_CURRENT = "https://services.gradle.org/versions/current"
MAVEN_META = "https://repo.maven.apache.org/maven2/org/apache/maven/apache-maven/maven-metadata.xml"
TTL_S = 6 * 3600
FAIL_TTL_S = 600
TIMEOUT_S = 10

# key -> (kind, number); the number is the major (the Python minor for python-3.N)
_KEY_RX = [
    (re.compile(r"^jdk-(\d{1,2})$"), "jdk", (8, 40)),
    (re.compile(r"^node-(\d{1,2})$"), "node", (12, 40)),
    (re.compile(r"^python-3\.(\d{1,2})$"), "python", (8, 20)),
    (re.compile(r"^maven-([34])$"), "maven", (3, 4)),
    (re.compile(r"^gradle-(\d{1,2})$"), "gradle", (6, 20)),
    (re.compile(r"^dotnet-(\d{1,2})$"), "dotnet", (6, 20)),
    (re.compile(r"^ruby-(3)$"), "ruby", (3, 3)),
    (re.compile(r"^go-(1)$"), "go", (1, 1)),
]
KIND_OF = {"jdk": "java"}            # download kind -> Environments kind

# used when a source can't be reached (the 9.8 list) - always within the supported lines
FALLBACK: Dict[str, List[str]] = {
    "jdk": ["jdk-25", "jdk-21", "jdk-17", "jdk-11"], "node": ["node-24", "node-22", "node-20"],
    "python": ["python-3.14", "python-3.13", "python-3.12", "python-3.11", "python-3.10"],
    "maven": ["maven-3"], "gradle": ["gradle-9", "gradle-8"], "dotnet": ["dotnet-10", "dotnet-8"],
    "ruby": ["ruby-3"], "go": ["go-1"]}

_LOCK = threading.Lock()
_CACHE: Dict[str, Tuple[float, Any]] = {}


def parse(key: str) -> Optional[Tuple[str, int]]:
    """(kind, number) of a download key, or None."""
    for rx, kind, (lo, hi) in _KEY_RX:
        m = rx.match(key or "")
        if m and lo <= int(m.group(1)) <= hi:
            return kind, int(m.group(1))
    return None


def label(key: str) -> str:
    p = parse(key)
    if not p:
        return key
    kind, n = p
    return {"jdk": f"Temurin JDK {n}", "node": f"Node.js {n}", "python": f"Python 3.{n} (standalone)",
            "maven": f"Apache Maven {n} (newest)", "gradle": f"Gradle {n} (newest)", "dotnet": f".NET {n} SDK",
            "ruby": "Ruby 3 + RubyGems + Bundler", "go": "Go (newest stable)"}[kind]


def nominal(key: str) -> str:
    """The newest version a line can bring (for requirement checks such as java >= 17)."""
    p = parse(key)
    if not p:
        return ""
    kind, n = p
    return f"3.{n}.999" if kind == "python" else f"{n}.999"


def _offline() -> bool:
    return (os.environ.get("UNDERSTUDY_RUNTIME_CATALOG") or "").strip().lower() in ("off", "offline", "0", "false")


def _fetch(url: str) -> bytes:
    from core.wizard import runtime_mirrors, runtimes                 # 9.9: the admin's mirror when one is set
    return runtimes._get_raw(runtime_mirrors.mirrored(url), timeout=TIMEOUT_S, limit=8 * 1024 * 1024)


def _cached(name: str, fn) -> Any:
    now = time.time()
    with _LOCK:
        hit = _CACHE.get(name)
        if hit and hit[0] > now:
            return hit[1]
    try:
        val = fn()
        ttl = TTL_S
    except Exception:
        val, ttl = None, FAIL_TTL_S                       # not reachable: the fallback, ask again in 10 minutes
    with _LOCK:
        _CACHE[name] = (now + ttl, val)
    return val


def _entry(key: str, eol: bool = False, eol_date: str = "", lts: bool = False, note: str = "") -> Dict[str, Any]:
    kind, n = parse(key)
    return {"key": key, "kind": KIND_OF.get(kind, kind), "label": label(key), "line": key.split("-", 1)[1],
            "eol": bool(eol), "eol_date": eol_date, "lts": bool(lts), "note": note, "nominal": nominal(key)}


def _java() -> List[Dict[str, Any]]:
    d = json.loads(_fetch(ADOPTIUM_RELEASES))
    lts = set(int(x) for x in d.get("available_lts_releases") or [])
    newest = int(d.get("most_recent_feature_release") or 0)
    out = []
    for n in sorted((int(x) for x in d.get("available_releases") or []), reverse=True):
        if parse(f"jdk-{n}"):
            out.append(_entry(f"jdk-{n}", eol=n not in lts and n < newest, lts=n in lts,
                              note="" if n in lts or n >= newest else "non-LTS, no more updates"))
    return out


def _node() -> List[Dict[str, Any]]:
    sched = json.loads(_fetch(NODE_SCHEDULE))
    today = date.today().isoformat()
    out = []
    for v, e in sched.items():
        m = re.match(r"^v(\d+)$", v)
        if not m or not parse(f"node-{m.group(1)}") or str(e.get("start", "9999")) > today:
            continue
        end = str(e.get("end") or "")
        out.append(_entry(f"node-{m.group(1)}", eol=bool(end) and end < today, eol_date=end, lts="lts" in e))
    return sorted(out, key=lambda x: -int(x["line"]))


def _python() -> List[Dict[str, Any]]:
    # CPython's yearly cadence (PEP 602): 3.N comes out in October 2011+N and gets fixes for 5 years
    today = date.today()
    out = []
    for n in range(20, 7, -1):
        if (2011 + n, 10) > (today.year, today.month) or not parse(f"python-3.{n}"):
            continue
        eol = f"{2016 + n}-10-31"
        out.append(_entry(f"python-3.{n}", eol=eol < today.isoformat(), eol_date=eol))
    return out


def _dotnet() -> List[Dict[str, Any]]:
    d = json.loads(_fetch(DOTNET_INDEX))
    out = []
    for c in d.get("releases-index") or []:
        m = re.match(r"^(\d+)\.0$", str(c.get("channel-version") or ""))
        phase = str(c.get("support-phase") or "")
        if not m or phase == "preview" or not parse(f"dotnet-{m.group(1)}"):
            continue
        out.append(_entry(f"dotnet-{m.group(1)}", eol=phase == "eol", eol_date=str(c.get("eol-date") or ""),
                          lts=c.get("release-type") == "lts"))
    return sorted(out, key=lambda x: -int(x["line"]))


def _gradle() -> List[Dict[str, Any]]:
    cur = int(str(json.loads(_fetch(GRADLE_CURRENT)).get("version") or "0").split(".")[0])
    return [_entry(f"gradle-{n}", eol=n < cur, note="" if n == cur else "older major - fixes only in the current one")
            for n in range(cur, max(cur - 3, 5), -1) if parse(f"gradle-{n}")]


def _maven() -> List[Dict[str, Any]]:
    meta = _fetch(MAVEN_META).decode("utf-8", "replace")
    out = [_entry("maven-3")]
    if re.search(r"<version>4\.\d+\.\d+</version>", meta):
        out.insert(0, _entry("maven-4"))
    return out


_SOURCES = {"jdk": _java, "node": _node, "python": _python, "dotnet": _dotnet, "gradle": _gradle, "maven": _maven}


def catalog(refresh: bool = False) -> Dict[str, List[Dict[str, Any]]]:
    """{kind: [entry]} - newest first; each entry has key, label, line, eol, eol_date, lts, note, nominal, source."""
    if refresh:
        with _LOCK:
            _CACHE.clear()
    out: Dict[str, List[Dict[str, Any]]] = {}
    for kind, keys in FALLBACK.items():
        if kind == "python":                             # computed from the date - no network needed
            out[kind] = [dict(e, source="cadence") for e in _python()]
            continue
        fn = _SOURCES.get(kind)
        live = _cached(kind, fn) if fn is not None and not _offline() else None
        out[kind] = [dict(e, source="live") for e in live] if live else \
            [dict(_entry(k), source="built-in") for k in keys]
    return out


def entries(refresh: bool = False) -> List[Dict[str, Any]]:
    return [e for es in catalog(refresh).values() for e in es]


def find(key: str) -> Optional[Dict[str, Any]]:
    return next((e for e in entries() if e["key"] == key), None)


def is_eol(key: str) -> Tuple[bool, str]:
    """(end of life?, why) - an unknown line that parses counts as supported."""
    e = find(key)
    if not e or not e["eol"]:
        return False, ""
    why = e.get("note") or (f"end of life since {e['eol_date']}" if e.get("eol_date") else "end of life")
    return True, f"{e['label']} is {why} - it gets no security fixes any more"
