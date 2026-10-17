"""The sandbox contract (8.0): every place Understudy installs a tool or runs code goes through a ``BaseSandboxRunner``.

The implementation is ``LocalProcessSandboxRunner`` (created by ``sandbox.factory``): isolated directories in the
Cloud Run instance (a ``python -m venv`` per tool and version, ``npm install --prefix <isolated_env>`` for Node), child
processes with an allow-listed environment, an OS timeout and a memory monitor that kills the whole process tree.

Lifecycle: ``setup_environment()`` -> ``install_dependencies()`` -> ``execute_script()`` (any number of times) ->
``teardown()``. Runners are context managers, so ``teardown`` always runs.
"""
from __future__ import annotations

import abc
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.wizard.frameworks import FrameworkSpec

LogFn = Callable[[str], None]

# Never let these reach a sandbox, whatever the manifest says.
SECRET_ENV_RX = re.compile(r"(?i)(GEMINI|GOOGLE_API|OPENAI|ANTHROPIC|UNDERSTUDY_ADMIN|API[_-]?KEY|SECRET|PASSWORD|PASSWD|"
                           r"TOKEN|CREDENTIAL|PRIVATE[_-]?KEY|AWS_|AZURE_|GCP_|GOOGLE_APPLICATION|SESSION[_-]?KEY)")
# Variables a manifest may never set: they would let it hijack the interpreter, the loader or the sandbox itself.
FORBIDDEN_ENV = {"LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH",
                 "PYTHONPATH", "PYTHONSTARTUP", "PYTHONHOME", "NODE_OPTIONS", "PERL5OPT", "RUBYOPT", "BASH_ENV", "ENV",
                 "PROMPT_COMMAND", "IFS", "PATH", "HOME", "SHELL", "UNDERSTUDY_SANDBOX"}
ENV_NAME_RX = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class SandboxError(RuntimeError):
    pass


class SandboxUnavailable(SandboxError):
    """The runner can't work on this server (e.g. a required toolchain is missing)."""


@dataclass
class SandboxLimits:
    timeout_s: int = 300            # one execute_script
    install_timeout_s: int = 900    # one install command
    memory_mb: int = 2048           # whole process tree

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ExecResult:
    command: str
    exit_code: int
    output: str = ""                # stdout + stderr, interleaved (tail kept)
    duration_ms: int = 0
    timed_out: bool = False
    memory_exceeded: bool = False
    peak_memory_mb: Optional[float] = None
    runner: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.memory_exceeded

    def tail(self, lines: int = 60) -> str:
        return "\n".join(self.output.splitlines()[-lines:])

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["output"] = self.tail(80)
        d["ok"] = self.ok
        return d


@dataclass
class InstallOutcome:
    status: str                     # READY | FAILED | TOOLCHAIN_MISSING | DRY_RUN
    message: str
    failed: Optional[ExecResult] = None
    steps: List[Dict[str, Any]] = field(default_factory=list)
    toolchain_missing: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in ("READY", "DRY_RUN")

    def to_dict(self) -> Dict[str, Any]:
        return {"status": self.status, "message": self.message, "ok": self.ok, "steps": self.steps,
                "toolchain_missing": self.toolchain_missing,
                "failed": self.failed.to_dict() if self.failed else None}


def clean_manifest_env(env: Optional[Dict[str, str]]) -> Dict[str, str]:
    """The manifest's environment variables, minus anything unsafe (raises on a forbidden name)."""
    out: Dict[str, str] = {}
    for k, v in (env or {}).items():
        k = str(k).strip()
        v = str(v)
        if not ENV_NAME_RX.match(k):
            raise SandboxError(f"'{k}' is not a valid environment variable name (A-Z, 0-9, _).")
        if k in FORBIDDEN_ENV:
            raise SandboxError(f"The setup may not set {k} - it would change how the sandbox itself runs.")
        if SECRET_ENV_RX.search(k):
            raise SandboxError(f"{k} looks like a secret. Secrets never go into a tool setup.")
        if "\n" in v or "\r" in v or "\x00" in v or len(v) > 1000:
            raise SandboxError(f"The value of {k} must be a single line (max 1000 characters).")
        out[k] = v
    return out


class BaseSandboxRunner(abc.ABC):
    """One isolated environment for one tool (spec, version, package sources)."""

    kind = "base"

    def __init__(self, spec: FrameworkSpec, limits: Optional[SandboxLimits] = None, log: Optional[LogFn] = None,
                 env: Optional[Dict[str, str]] = None, interpreter: Optional[str] = None,
                 path_prepend: Optional[List[str]] = None, name: str = "", network: str = "setup"):
        self.spec = spec
        self.limits = limits or SandboxLimits()
        self._log = log or (lambda m: None)
        self.manifest_env = clean_manifest_env(env)
        self.interpreter = interpreter or None
        self.path_prepend = [p for p in (path_prepend or []) if p]
        self.name = re.sub(r"[^A-Za-z0-9_.-]", "-", name or spec.id)[:60]
        self.network = network               # "setup" (tool install + smoke test) | "egress" (runs: via the egress proxy)
        self.ready = False
        self.path_filter: Optional[Callable[[str], str]] = None   # e.g. self-healing hides stale drivers from PATH

    # -- the contract -----------------------------------------------------------------------------------------------
    @classmethod
    def available(cls) -> Tuple[bool, str]:
        return True, ""

    @abc.abstractmethod
    def setup_environment(self) -> Dict[str, Any]:
        """Create the isolated environment (directories). Idempotent."""

    @abc.abstractmethod
    def install_dependencies(self) -> InstallOutcome:
        """Install the spec's packages (and post-install steps) into the environment."""

    @abc.abstractmethod
    def execute_script(self, files: Dict[str, str], argv: List[str], env: Optional[Dict[str, str]] = None,
                       timeout_s: Optional[int] = None, on_line: Optional[LogFn] = None,
                       cwd: Optional[str] = None) -> ExecResult:
        """Write ``files`` into a fresh script directory and run ``argv`` (``{entry}`` = the spec's entry file) there."""

    @abc.abstractmethod
    def teardown(self) -> None:
        """Release what the run created (processes, scratch directories)."""

    # -- helpers ------------------------------------------------------------------------------------------------------
    def check_dependency(self, check: str, value: str) -> Tuple[bool, str]:
        """OS dependency check from the manifest: ('binary', 'java') or ('env', 'JAVA_HOME'). Default: not supported."""
        return True, ""

    def describe(self) -> Dict[str, Any]:
        return {"kind": self.kind, "name": self.name, "limits": self.limits.to_dict(), "network": self.network,
                "interpreter": self.interpreter, "env": sorted(self.manifest_env)}

    def log(self, m: str) -> None:
        try:
            self._log(m[:600])
        except Exception:
            pass

    def __enter__(self):
        self.setup_environment()
        return self

    def __exit__(self, *exc):
        try:
            self.teardown()
        except Exception as e:                      # teardown must never hide the real error
            self.log(f"teardown: {type(e).__name__}: {e}")
        return False
