"""Sandbox factory: which runner a tool setup or an automation run gets.

Understudy runs on Google Cloud Run, where there is no Docker daemon (no privileged containers), so every tool setup,
smoke test and automation run uses ``LocalProcessSandboxRunner``: an isolated folder per tool and version, an
allow-listed environment, a time limit and a process-tree memory limit, inside the Cloud Run instance's own gVisor
sandbox. The admin sets the limits in Mission Control > Access & tools > Sandbox.

9.9.1: the Docker sandbox was removed. A sandbox mode saved by an older version ("docker" / "auto") is read as
``local``.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.wizard.frameworks import FrameworkSpec
from core.wizard.sandbox.base import BaseSandboxRunner, LogFn, SandboxLimits
from core.wizard.sandbox.local import LocalProcessSandboxRunner

MODES = ("local",)
DEFAULTS = {"mode": "local", "memory_mb": 2048, "timeout_s": 300, "install_timeout_s": 900}
BOUNDS = {"memory_mb": (256, 32768), "timeout_s": (10, 3600), "install_timeout_s": (60, 7200)}


def settings() -> Dict[str, Any]:
    """The effective sandbox settings (the admin's limits, then the defaults). The mode is always ``local``."""
    out = dict(DEFAULTS)
    try:
        from core.wizard import admin_policy
        saved = admin_policy.sandbox_setting() or {}
    except Exception:
        saved = {}
    for k, v in saved.items():
        if k in out and k != "mode":
            out[k] = v
    out["source"] = "admin" if saved else "default"
    return out


def limits(s: Optional[Dict[str, Any]] = None) -> SandboxLimits:
    s = s or settings()
    return SandboxLimits(timeout_s=int(s["timeout_s"]), install_timeout_s=int(s["install_timeout_s"]),
                         memory_mb=int(s["memory_mb"]))


def resolve_kind(mode: Optional[str] = None) -> str:
    """Always ``local`` (kept for callers and API compatibility)."""
    return "local"


def create_runner(spec: FrameworkSpec, *, purpose: str = "setup", log: Optional[LogFn] = None,
                  env: Optional[Dict[str, str]] = None, path_prepend: Optional[List[str]] = None,
                  mode: Optional[str] = None, name: str = "", workdir: Optional[str] = None) -> BaseSandboxRunner:
    """purpose: 'setup' (tool install + smoke test) | 'run' (automation, whose traffic goes through the run's egress
    proxy)."""
    s = settings()
    kw: Dict[str, Any] = dict(limits=limits(s), log=log, env=env, path_prepend=path_prepend, name=name or spec.id,
                              network="setup" if purpose == "setup" else "egress")
    if purpose == "run":
        kw["limits"].timeout_s = max(kw["limits"].timeout_s, 30)
    return LocalProcessSandboxRunner(spec, **kw)


def describe() -> Dict[str, Any]:
    return {"settings": settings(), "effective": "local", "problem": "", "modes": list(MODES), "bounds": BOUNDS}
