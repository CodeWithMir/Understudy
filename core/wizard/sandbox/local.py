"""LocalProcessSandboxRunner (8.0): the sandbox every tool setup, smoke test and run uses (Cloud Run has no Docker).

* Environments: one directory per tool + version + package sources (shared with automation runs, so a tool that passed
  its smoke test is ready for every session): Python gets ``python -m venv`` (the backend's interpreter, or the admin's
  own, e.g. python3.10), Node gets ``npm install --prefix <isolated_env>``; Maven, RubyGems, Go and NuGet use their own
  per-environment directories (see ``dependency_resolver.plan_for``).
* Processes: started without a shell in their own process group / session, with an **allow-listed** environment
  (OS essentials, toolchain locations, proxy / CA settings - never API keys, tokens or passwords) plus the manifest's
  vetted variables.
* Limits: a wall-clock timeout and a memory monitor that samples the whole process tree (psutil) and kills it when it
  goes over the limit; on POSIX the tree is a process group, killed together.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from core.wizard import dependency_resolver
from core.wizard.sandbox.base import (SECRET_ENV_RX, BaseSandboxRunner, ExecResult, InstallOutcome, LogFn,
                                      SandboxError)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
SCRATCH_DIR = os.path.join(BASE_DIR, "generated", "sandboxes")
NPM_DONE = ".understudy-npm-installed"          # written after a complete npm install (9.7.2)

# The environment a sandboxed process may see (8.0). Everything else is dropped.
# Windows entries are the ones 5.2 found real tools need (Selenium Manager reads %PROGRAMFILES%, .cmd launchers need
# PATHEXT/ComSpec, npm/Maven read WINDIR/ProgramData).
ENV_ALLOWLIST = {
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "TZ", "TERM", "TMPDIR", "TEMP", "TMP",
    "SHELL", "DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR", "XDG_CACHE_HOME", "DBUS_SESSION_BUS_ADDRESS",
    # toolchains
    "JAVA_HOME", "MAVEN_HOME", "M2_HOME", "GOPATH", "GOROOT", "GOCACHE", "GOPROXY", "DOTNET_ROOT", "NVM_DIR",
    "PLAYWRIGHT_BROWSERS_PATH", "PUPPETEER_CACHE_DIR", "CYPRESS_CACHE_FOLDER", "CHROME_BIN", "CHROMEWEBDRIVER", "SE_CACHE_PATH",
    # package sources, corporate proxies and certificates
    "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "PIP_FIND_LINKS", "PIP_CERT", "NPM_CONFIG_REGISTRY", "NPM_CONFIG_CAFILE",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS", "CURL_CA_BUNDLE",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
    # Windows
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432",
    "PROGRAMDATA", "COMMONPROGRAMFILES", "COMMONPROGRAMFILES(X86)", "APPDATA", "LOCALAPPDATA", "USERPROFILE",
    "HOMEDRIVE", "HOMEPATH", "USERNAME", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS",
}


def restricted_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Allow-listed OS environment + ``extra``. Secret-looking names never pass, even when allow-listed or extra."""
    env = {}
    for k, v in os.environ.items():
        if (k in ENV_ALLOWLIST or k.upper() in ENV_ALLOWLIST) and not SECRET_ENV_RX.search(k):
            env[k] = v
    env["UNDERSTUDY_SANDBOX"] = "local"
    for k, v in (extra or {}).items():
        if SECRET_ENV_RX.search(k) and k not in ("UNDERSTUDY_PAYLOAD",) and not k.startswith("UNDERSTUDY_SECRET_"):
            continue
        env[k] = str(v)
    return env


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        import psutil
        try:
            parent = psutil.Process(proc.pid)
            kids = parent.children(recursive=True)
        except psutil.NoSuchProcess:
            kids = []
        for k in kids:
            try:
                k.kill()
            except psutil.NoSuchProcess:
                pass
    except ImportError:
        pass
    if os.name != "nt":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def _tree_rss_mb(pid: int) -> Optional[float]:
    try:
        import psutil
    except ImportError:
        return None
    try:
        p = psutil.Process(pid)
        total = p.memory_info().rss
        for c in p.children(recursive=True):
            try:
                total += c.memory_info().rss
            except psutil.NoSuchProcess:
                pass
        return total / (1024 * 1024)
    except psutil.NoSuchProcess:
        return None


def run_limited(argv: List[str], cwd: str, env: Dict[str, str], timeout_s: int, memory_mb: int,
                on_line: Optional[LogFn] = None, keep_chars: int = 200_000) -> ExecResult:
    """Runs argv (no shell) with a wall-clock timeout and a process-tree memory limit. Never raises for the child's
    own failures; a missing executable is exit code 127."""
    cmd = " ".join(argv)
    started = time.time()
    kw: Dict[str, Any] = {}
    if os.name == "nt":
        kw["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kw["start_new_session"] = True
    try:
        exe = dependency_resolver.resolve_executable(argv[0], env)
        proc = subprocess.Popen([exe] + list(argv[1:]), cwd=cwd, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True, bufsize=1,
                                errors="replace", shell=False, **kw)
    except FileNotFoundError as e:
        return ExecResult(cmd, 127, f"Could not start '{os.path.basename(argv[0])}': {e}", runner="local")
    except OSError as e:
        return ExecResult(cmd, 126, f"Could not start '{os.path.basename(argv[0])}': {e}", runner="local")
    state = {"timed_out": False, "oom": False, "peak": 0.0}
    done = threading.Event()

    def watchdog():
        while not done.wait(0.5):
            if time.time() - started > timeout_s:
                state["timed_out"] = True
                _kill_tree(proc)
                return
            rss = _tree_rss_mb(proc.pid)
            if rss is not None:
                state["peak"] = max(state["peak"], rss)
                if memory_mb and rss > memory_mb:
                    state["oom"] = True
                    _kill_tree(proc)
                    return
    threading.Thread(target=watchdog, daemon=True).start()
    buf: List[str] = []
    size = 0
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            if on_line:
                try:
                    on_line(line)
                except Exception:
                    pass
            buf.append(line)
            size += len(line) + 1
            while size > keep_chars and len(buf) > 1:
                size -= len(buf.pop(0)) + 1
        proc.wait()
    finally:
        done.set()
    code = proc.returncode if proc.returncode is not None else -1
    out = "\n".join(buf)
    if state["timed_out"]:
        out += f"\n[sandbox] killed after the {timeout_s}s time limit"
    if state["oom"]:
        out += f"\n[sandbox] killed: the process tree used more than {memory_mb} MB of memory"
    return ExecResult(cmd, code, out, int((time.time() - started) * 1000), state["timed_out"], state["oom"],
                      round(state["peak"], 1) if state["peak"] else None, "local")


class LocalProcessSandboxRunner(BaseSandboxRunner):
    kind = "local"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.plan = dependency_resolver.plan_for(self.spec)
        self.env_dir = self.plan.env_dir
        self.scratch = os.path.join(SCRATCH_DIR, f"{self.name}-{uuid.uuid4().hex[:8]}")
        self._dirs: List[str] = []

    # -- environment ------------------------------------------------------------------------------------------------
    def tool_env(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """PATH / NODE_PATH / GEM_HOME for the isolated environment + manifest variables + extra."""
        add: Dict[str, str] = {}
        path = []
        eco = self.spec.ecosystem
        if eco == "pip":
            path.append(os.path.dirname(dependency_resolver._venv_python(self.env_dir)))
        elif eco == "npm":
            nm = os.path.join(self.env_dir, "node_modules")
            add["NODE_PATH"] = nm
            path.append(os.path.join(nm, ".bin"))
        elif eco == "gem":
            add["GEM_HOME"] = add["GEM_PATH"] = os.path.join(self.env_dir, "gems")
            path.append(os.path.join(self.env_dir, "gems", "bin"))
        try:                                   # 8.2: toolchains registered in Mission Control > Package managers
            tc_bins, tc_env = admin_toolchains()
        except Exception:
            tc_bins, tc_env = [], {}
        path = self.path_prepend + path + tc_bins
        add.update(tc_env)
        add.update(self.manifest_env)
        add.update(extra or {})
        try:            # 9.4: a runtime missing / broken on PATH (e.g. an SDK-less .NET host) -> an install Understudy has
            from core.wizard import environments
            trust = environments.kinds_chosen_by_admin({"env_vars": self.manifest_env, "path_prepend": self.path_prepend})
            for pk in environments.runtimes_for_setup(eco, path, add, trust=trust):
                if not pk.get("rec"):
                    continue
                bins, envs = environments.runtime_settings(pk["rec"])
                path = [b for b in bins if b not in path] + path
                add.update(envs)              # only picked because the current one doesn't work
        except Exception:
            pass
        env = restricted_env(add)
        env["PATH"] = os.pathsep.join(path + [env.get("PATH", os.defpath)])
        if self.path_filter:
            env["PATH"] = self.path_filter(env["PATH"])
        return env

    def setup_environment(self) -> Dict[str, Any]:
        os.makedirs(self.env_dir, exist_ok=True)
        os.makedirs(self.scratch, exist_ok=True)
        self.ready = True
        self.log(f"[sandbox:local] environment {os.path.relpath(self.env_dir, BASE_DIR)}")
        return {"kind": self.kind, "env_dir": self.env_dir, "scratch": self.scratch}

    def check_dependency(self, check: str, value: str) -> Tuple[bool, str]:
        env = self.tool_env()
        if check == "binary":
            found = shutil.which(value, path=env.get("PATH"))
            if found:
                return True, found
            from core.wizard import runtime_repair
            if runtime_repair.browser_kind(value):                # 9.9: browsers live outside PATH on Windows / macOS
                b = runtime_repair.find_browser(value, env)
                if b:
                    return True, b
                if runtime_repair.browser_kind(value) in ("chrome", "chromium", "firefox") and \
                        runtime_repair.selenium_manager_fetches_browsers(self.spec.packages):
                    return True, (f"{value} is not installed - Selenium Manager downloads a matching browser at the "
                                  "first run")
                return False, (f"no {value} browser found on this server (looked on PATH and in the standard install "
                               "folders) - install it, or give its full path")
            return False, f"'{value}' is not installed or not on PATH"
        if check == "env":
            v = env.get(value) or os.environ.get(value)
            if not v:
                return False, f"{value} is not set"
            if value.endswith("_HOME") and not os.path.isdir(v):
                return False, f"{value} points to {v}, which does not exist"
            return True, v
        return True, ""

    def install_dependencies(self) -> InstallOutcome:
        if not self.ready:
            self.setup_environment()
        missing = dependency_resolver.check_toolchain(self.spec.ecosystem)
        missing = [m for m in missing if not shutil.which(m, path=self.tool_env().get("PATH"))]
        if missing:
            return InstallOutcome("TOOLCHAIN_MISSING", f"{', '.join(missing)} not found on the server",
                                  toolchain_missing=missing)
        mode = dependency_resolver.install_mode()
        if mode not in ("off", "dry-run"):
            self._clear_unfinished_npm_tree()       # before the manifests: a lock file the plan ships is kept
        for fname, content in self.plan.manifest_files.items():
            with open(os.path.join(self.env_dir, fname), "w", encoding="utf-8") as fh:
                fh.write(content)
        if mode in ("off", "dry-run"):
            return InstallOutcome("DRY_RUN", f"install mode '{mode}': {len(self.plan.commands)} command(s) planned",
                                  steps=[{"command": " ".join(c), "planned": True} for c in self.plan.commands])
        steps = []
        env = self.tool_env()
        for cmd in self.plan.commands:
            self.log("$ " + " ".join(cmd))
            res = run_limited(list(cmd), self.env_dir, env, self.limits.install_timeout_s, self.limits.memory_mb,
                              on_line=lambda ln: self.log("  " + ln))
            steps.append({"command": res.command, "exit_code": res.exit_code, "ms": res.duration_ms})
            if not res.ok:
                return InstallOutcome("FAILED", f"'{os.path.basename(cmd[0])} {' '.join(cmd[1:3])} ...' exited with "
                                                f"code {res.exit_code}", failed=res, steps=steps)
        if self.spec.ecosystem == "npm":
            with open(os.path.join(self.env_dir, NPM_DONE), "w", encoding="utf-8") as fh:
                fh.write("ok\n")
        return InstallOutcome("READY", f"{self.spec.name} installed in {os.path.relpath(self.env_dir, BASE_DIR)}",
                              steps=steps)

    def _clear_unfinished_npm_tree(self) -> None:
        """9.7.2: an npm install that failed half-way leaves node_modules / package-lock.json behind, and the retry
        reuses the same folder. npm then crashes on the broken tree ("Cannot read properties of null (reading
        'edgesOut')"), whatever runtime is used. Anything not marked as a finished install is removed first."""
        if self.spec.ecosystem != "npm" or os.path.isfile(os.path.join(self.env_dir, NPM_DONE)):
            return
        for name in ("node_modules", "package-lock.json"):
            target = os.path.join(self.env_dir, name)
            if os.path.isdir(target):
                self.log(f"  removing {name} left by an unfinished install")
                shutil.rmtree(target, ignore_errors=True)
            elif os.path.isfile(target):
                os.remove(target)

    def resolve_argv(self, argv: List[str], entry: str) -> List[str]:
        return dependency_resolver._resolve_argv(argv, self.spec, self.env_dir, entry)

    def execute_script(self, files: Dict[str, str], argv: List[str], env: Optional[Dict[str, str]] = None,
                       timeout_s: Optional[int] = None, on_line: Optional[LogFn] = None,
                       cwd: Optional[str] = None) -> ExecResult:
        if not self.ready:
            self.setup_environment()
        if cwd is None:
            cwd = tempfile.mkdtemp(prefix="run-", dir=self.scratch)
            self._dirs.append(cwd)
        for rel, content in (files or {}).items():
            if os.path.isabs(rel) or ".." in rel.replace("\\", "/").split("/"):
                raise SandboxError(f"Refused to write {rel}: outside the script directory")
            path = os.path.join(cwd, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
        full = self.resolve_argv(list(argv), self.spec.entry_file)
        run_env = self.tool_env(env)
        link = self._link_node_modules(cwd)
        try:
            return run_limited(full, cwd, run_env, timeout_s or self.limits.timeout_s, self.limits.memory_mb,
                               on_line=on_line)
        finally:
            if link:
                remove_link(link)

    def _link_node_modules(self, cwd: str) -> Optional[str]:
        """8.2 field fix: test runners that load the tool themselves (Playwright Test, Jest, Mocha ...) must find the
        SAME package copy from the test file as from their CLI. With only NODE_PATH, Playwright Test loads
        @playwright/test twice ("Playwright Test did not expect test() to be called here ... No tests found"). A
        ``node_modules`` link in the script folder that points at the tool's isolated environment fixes that; it is
        removed after the run (the environment itself is never touched)."""
        if self.spec.ecosystem != "npm":
            return None
        target = os.path.join(self.env_dir, "node_modules")
        link = os.path.join(cwd, "node_modules")
        if not os.path.isdir(target) or os.path.lexists(link):
            return None
        try:
            make_dir_link(target, link)
            return link
        except OSError as e:
            self.log(f"[sandbox:local] could not link node_modules into the script folder ({e}); using NODE_PATH only")
            return None

    def teardown(self) -> None:
        shutil.rmtree(self.scratch, ignore_errors=True)
        self.ready = False


def make_dir_link(target: str, link: str) -> None:
    """A directory link: a junction on Windows (no admin rights or developer mode needed), a symlink elsewhere."""
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(os.path.abspath(target), os.path.abspath(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def remove_link(link: str) -> None:
    """Removes ONLY the link - never what it points to."""
    try:
        if os.name == "nt" and os.path.isdir(link) and not os.path.islink(link):
            os.rmdir(link)                    # a junction: rmdir removes the link, not the target's contents
        else:
            os.unlink(link)
    except OSError:
        pass


def admin_toolchains() -> Tuple[List[str], Dict[str, str]]:
    from core.wizard import admin_policy
    return admin_policy.toolchain_bins(), admin_policy.toolchain_homes()


_ = sys  # sys is used by tests that patch the interpreter
