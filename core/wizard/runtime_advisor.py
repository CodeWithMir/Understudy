"""AI-guided runtime installs (8.3).

Before 8.3, Mission Control said "Install ruby and gem on the Understudy server (Understudy can't download it), then
refresh". Now a missing program is something Understudy fixes itself, guided by the AI:

1. ``advise(programs)``: the ``RUNTIME_INSTALL_ADVICE`` agent picks which of Understudy's checksum-verified downloads
   provides the missing programs on THIS operating system (Ruby -> RubyInstaller on Windows / conda-forge elsewhere,
   Go -> go.dev, .NET -> Microsoft, Java -> Temurin, Node.js -> nodejs.org, ...), explains it and lists the programs
   that must start afterwards. Offline, a built-in mapping gives the same answer without the explanation.
2. The admin clicks **Download & register** (``package_managers.download``): Understudy downloads, verifies the
   checksum, unpacks into ``generated/runtimes``, registers the toolchain for every install / smoke test / run and
   starts every expected program once.
3. A failed download or check goes back to the agent (``diagnose_failure``): it quotes the decisive log line and
   proposes the next attempt (another build, conda-forge, or what the admin must configure - e.g. a proxy).

The AI only chooses among keys Understudy accepts (``runtimes.is_supported``); it never runs anything.
"""
from __future__ import annotations

import platform
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.wizard import activity_log, runtimes

_ADVICE: Dict[str, Dict[str, Any]] = {}
_PROG_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,30}$")
_SOURCE = {"jdk": "Eclipse Temurin (api.adoptium.net)", "node": "nodejs.org", "maven": "Maven Central",
           "gradle": "services.gradle.org", "go": "go.dev", "dotnet": "Microsoft (dotnetcli release metadata)",
           "ruby": "RubyInstaller on GitHub (Windows) / conda-forge (Linux, macOS)"}


def _now() -> str:
    return datetime.utcnow().isoformat()


def _os() -> str:
    o, a = runtimes._os_arch()
    return f"{o} ({platform.platform(terse=True)}), {a}"


def candidates_for(programs: List[str]) -> List[str]:
    from core.wizard.package_managers import PROGRAM_DOWNLOADS
    out: List[str] = []
    for p in programs:
        for k in PROGRAM_DOWNLOADS.get(p, []):
            if k not in out:
                out.append(k)
    return out


def _label(key: str) -> str:
    from core.wizard.package_managers import label
    return label(key)


def _fallback(programs: List[str], cands: List[str]) -> Dict[str, Any]:
    rec = cands[0] if cands else ""
    exp = _expected(rec, programs)
    if not rec:
        return {"recommended": "manual", "alternatives": [], "expected_programs": programs,
                "summary": f"Understudy has no download for {', '.join(programs)}. Install it on the server and register "
                           f"its folder below.", "steps": [], "manual": ""}
    return {"recommended": rec, "alternatives": cands[1:3], "expected_programs": exp,
            "summary": f"Download {_label(rec)} from {_SOURCE.get(runtimes.kind_of(rec), 'its official site')}; it "
                       f"provides {', '.join(exp)}.",
            "steps": ["Download the official build for this operating system.", "Verify its published checksum.",
                      "Unpack it into Understudy's runtimes folder (nothing is installed system-wide).",
                      "Register it for every install, smoke test and run.", f"Start {', '.join(exp)} once to check them."],
            "manual": ""}


def _expected(key: str, programs: List[str]) -> List[str]:
    from core.wizard.package_managers import PROVIDES
    prov = PROVIDES.get(runtimes.kind_of(key)) or []
    return list(dict.fromkeys([p for p in programs if not prov or p in prov] + prov[:3])) or programs


def _ask_ai(programs: List[str], context: str, cands: List[str], failure: str) -> Optional[Dict[str, Any]]:
    from core.wizard.llm import ai_available, generate_json
    from core.wizard.package_managers import programs as probe
    if not ai_available():
        return None
    found = probe(sorted({"java", "node", "npm", "mvn", "gradle", "ruby", "gem", "go", "dotnet", "python"} | set(programs)))
    res = generate_json("RUNTIME_INSTALL_ADVICE", {
        "programs": ", ".join(programs), "context": context or "(not given)", "os": _os(),
        "found": "\n".join(f"- {n}: {v['version'] or 'found'}" for n, v in found.items() if v["found"]) or "(none)",
        "candidates": "\n".join(f"- {k} - {_label(k)} - {_SOURCE.get(runtimes.kind_of(k), '')}" for k in cands) or "(none)",
        "conda": ", ".join(sorted(runtimes.CONDA_ALLOWED)), "failure": failure or "none",
    }, untrusted=["context", "failure"])
    if not res.ok or not isinstance(res.data, dict):
        return None
    d = dict(res.data)
    d["agent_source"], d["model"] = res.agent_source, res.model
    return d


def _clean(d: Dict[str, Any], programs: List[str], cands: List[str]) -> Dict[str, Any]:
    base = _fallback(programs, cands)
    rec = str(d.get("recommended") or "").strip()
    if rec != "manual" and not runtimes.is_supported(rec):
        rec = base["recommended"]
    alts = [a for a in (str(x).strip() for x in d.get("alternatives") or []) if runtimes.is_supported(a) and a != rec][:3]
    exp = [p for p in (str(x).strip() for x in d.get("expected_programs") or []) if _PROG_RX.match(p)][:6] or \
        _expected(rec, programs)
    return {"recommended": rec, "alternatives": alts or [a for a in base["alternatives"] if a != rec],
            "expected_programs": exp, "summary": str(d.get("summary") or base["summary"])[:600],
            "diagnosis": str(d.get("diagnosis") or "")[:600], "steps": [str(x)[:200] for x in d.get("steps") or []][:6]
            or base["steps"], "manual": str(d.get("manual") or "")[:600],
            "agent_source": d.get("agent_source", "LOCAL_HEURISTIC"), "model": d.get("model", "Built-in mapping")}


def advise(programs: List[str], context: str = "", admin: str = "admin") -> Dict[str, Any]:
    """Which download fixes these missing programs (AI-chosen, validated). Stored for the Mission Control card."""
    progs = [p for p in dict.fromkeys(str(x).strip().lower() for x in programs or []) if _PROG_RX.match(p)][:6]
    if not progs:
        raise ValueError("Name the missing program(s), e.g. ruby, gem.")
    cands = candidates_for(progs)
    ai = _ask_ai(progs, context, cands, "")
    adv = _clean(ai, progs, cands) if ai else dict(_fallback(progs, cands), agent_source="LOCAL_HEURISTIC",
                                                   model="Built-in mapping", diagnosis="")
    adv.update(id="ADV-" + uuid.uuid4().hex[:6].upper(), programs=progs, context=str(context or "")[:300], at=_now(),
               by=admin, os=_os(), labels={k: _label(k) for k in [adv["recommended"]] + adv["alternatives"]
                                            if k != "manual"})
    _ADVICE[",".join(progs)] = adv
    activity_log.record("ADMIN", f"AI runtime advice for {', '.join(progs)}: {adv['recommended']}", adv["summary"],
                        {"advice": adv["id"], "agent_source": adv["agent_source"]}, source="ADMIN")
    return adv


def diagnose_failure(key: str, job: Dict[str, Any]) -> Dict[str, Any]:
    """A download / check failed: the agent explains it and proposes the next attempt."""
    progs = list((job.get("verified") or {}).keys()) or _expected(key, [])
    cands = [k for k in candidates_for(progs) if k != key]
    for c in ("conda-" + {"jdk": "openjdk", "node": "nodejs"}.get(runtimes.kind_of(key), runtimes.kind_of(key)),):
        if runtimes.is_supported(c) and c != key and c not in cands:
            cands.append(c)
    log = "\n".join((job.get("log") or [])[-25:]) + "\nERROR: " + str(job.get("error") or "")
    ai = _ask_ai(progs, f"download of {key} failed", cands, log[-5000:])
    if ai:
        d = _clean(ai, progs, cands)
    else:
        err = str(job.get("error") or "")
        net = re.search(r"(?i)urlopen|timed out|getaddrinfo|Name or service|Connection|SSL|CERTIFICATE|403|407|proxy", err)
        d = dict(_fallback(progs, cands), agent_source="LOCAL_HEURISTIC", model="Built-in mapping",
                 diagnosis=("The server could not reach the download site - set HTTPS_PROXY / SSL_CERT_FILE on the "
                            "server if you are behind a proxy, or install it yourself and register the folder."
                            if net else f"The download failed: {err[:200]}"))
        if net:
            d["recommended"] = "manual"
    d.update(failed_key=key, at=_now())
    return d


def latest() -> Dict[str, Any]:
    return {k: v for k, v in list(_ADVICE.items())[-10:]}
