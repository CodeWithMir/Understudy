"""Downloaded runtimes on every instance (9.9 - Cloud Run).

The admin's tool setups are shared (Firestore): approvals, run settings such as ``JAVA_HOME``, registered toolchains.
What a setup *downloaded* - a JDK, Maven, Node.js ... into ``generated/runtimes/`` - is on one machine's disk. On
Cloud Run that disk is per instance and temporary: a restart, a new revision or a second instance has none of it, so
a tool that passed its smoke test showed "needs java" and its runs failed.

This module puts them back: it collects the runtimes the shared settings point at (registered toolchains and every
approved tool's run settings), and downloads the missing ones again with the same verified downloader
(``runtimes.provision``: official source, checksum checked before extraction) - no AI, no admin step.

* ``start()`` - at start-up, in the background.
* ``ensure()`` - before a tool setup, an install or a run: waits for a running restore, or restores now.
* Paths keep working when the re-download is a newer patch release: ``runtimes.resolve_path`` maps
  ``jdk-21/jdk-21.0.12+1/...`` onto the release this machine has (used by ``admin_policy.tool_runtime`` and the
  toolchain lookups).
* A registered toolchain whose home moved is re-registered with the new home (one Firestore write).
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Dict, List, Set

_LOCK = threading.Lock()
_STATE: Dict[str, Any] = {"status": "idle", "restored": [], "failed": {}, "at": None}
_RUNNING = threading.Event()


def _paths_in(rt: Dict[str, Any]) -> List[str]:
    out = [str(p) for p in rt.get("path_prepend") or []]
    out += [str(v) for v in (rt.get("env_vars") or {}).values()]
    if rt.get("java_home"):
        out.append(str(rt["java_home"]))
    return out


def wanted() -> Set[str]:
    """Every downloaded runtime the shared settings point at."""
    from core.wizard import admin_policy as ap, runtimes
    keys: Set[str] = set()
    for tc in ap.toolchains().values():
        for p in [tc.get("home"), tc.get("bin")] + list(tc.get("extra_bins") or []):
            k = runtimes.key_of_path(p or "")
            if k:
                keys.add(k)
    for tid, e in ap.whitelist().items():
        rt = (e.get("setup") or {}).get("runtime") or {}
        for p in _paths_in(rt):
            for part in str(p).split(os.pathsep):
                k = runtimes.key_of_path(part)
                if k:
                    keys.add(k)
    return keys


def missing() -> List[str]:
    from core.wizard import runtimes
    out = []
    for k in sorted(wanted()):
        have = runtimes.installed(k)
        if not (have and os.path.isdir(have.get("bin") or have.get("home") or "")):
            out.append(k)
    return out


def _reregister(key: str, info: Dict[str, Any]) -> None:
    """A registered toolchain of this kind pointed at a release folder that is gone: point it at the new one."""
    from core.wizard import admin_policy as ap, runtimes
    for name, tc in ap.toolchains().items():
        if runtimes.key_of_path(tc.get("home") or "") == key and tc.get("home") != info.get("home") \
                and not os.path.isdir(tc.get("home") or ""):
            ap.set_toolchain(name, dict(tc, home=info.get("home"), bin=info.get("bin"),
                                        version=info.get("version") or tc.get("version")), tc.get("by") or "restore")


def restore(log=lambda m: None) -> Dict[str, Any]:
    """Downloads every missing runtime (blocking). Returns the state."""
    from core.wizard import activity_log, runtimes
    with _LOCK:
        if _RUNNING.is_set():
            return dict(_STATE)
        _RUNNING.set()
        _STATE.update(status="restoring", restored=[], failed={}, at=time.time())
    try:
        todo = missing()
        for key in todo:
            try:
                log(f"Restoring {key} on this server (downloaded by an earlier setup; checksum verified) ...")
                info = runtimes.provision(key, log)
                _reregister(key, info)
                _STATE["restored"].append(key)
            except Exception as e:
                _STATE["failed"][key] = f"{type(e).__name__}: {e}"[:300]
        if todo:
            activity_log.record("RUNTIME", f"Restored {len(_STATE['restored'])} downloaded runtime(s) on this server"
                                + (f", {len(_STATE['failed'])} failed" if _STATE["failed"] else ""),
                                "This server didn't have what earlier tool setups downloaded (Cloud Run instances "
                                "start empty): " + ", ".join(todo) + "."
                                + ("".join(f"\n{k}: {v}" for k, v in _STATE["failed"].items())),
                                {"restored": _STATE["restored"], "failed": _STATE["failed"]}, source="LOCAL_HEURISTIC",
                                level="warn" if _STATE["failed"] else "info")
        _STATE["status"] = "failed" if _STATE["failed"] else "done"
        return dict(_STATE)
    finally:
        _RUNNING.clear()


def start() -> None:
    """At start-up: restore in the background (never delays the server)."""
    if (os.environ.get("UNDERSTUDY_RUNTIME_RESTORE") or "on").lower() in ("off", "0", "false", "no"):
        return

    def run():
        try:
            restore()
        except Exception as e:                           # pragma: no cover - never break start-up
            _STATE.update(status="failed", error=f"{type(e).__name__}: {e}"[:300])
    threading.Thread(target=run, daemon=True, name="runtime-restore").start()


def ensure(log=lambda m: None, timeout_s: float = 900) -> Dict[str, Any]:
    """Before a setup, install or run: the runtimes it may need are on this machine (waits for a running restore)."""
    if (os.environ.get("UNDERSTUDY_RUNTIME_RESTORE") or "on").lower() in ("off", "0", "false", "no"):
        return dict(_STATE)
    deadline = time.time() + timeout_s
    while _RUNNING.is_set() and time.time() < deadline:
        time.sleep(0.5)
    try:
        if missing():
            return restore(log)
    except Exception as e:
        log(f"Could not restore the downloaded runtimes: {type(e).__name__}: {e}")
    return dict(_STATE)


def status() -> Dict[str, Any]:
    return dict(_STATE, running=_RUNNING.is_set())
