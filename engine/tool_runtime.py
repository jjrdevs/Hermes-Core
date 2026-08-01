from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import ToolRequest

try:
    import resource
except ImportError:  # pragma: no cover - platform fallback
    resource = None


class ToolResult:
    def __init__(self, *, status: str, data: Optional[Dict[str, Any]] = None, error: Optional[str] = None) -> None:
        self.status = status
        self.data = data or {}
        self.error = error

    def to_dict(self) -> Dict[str, Any]:
        return {"status": self.status, "data": self.data, "error": self.error}


class BaseTool:
    def __init__(self, metadata: Optional[Dict[str, Any]] = None) -> None:
        self.metadata = metadata or {}

    def execute(self, request: ToolRequest) -> ToolResult:
        raise NotImplementedError


class FilesystemTool(BaseTool):
    def __init__(self, allowed_roots: Optional[List[str]] = None, metadata: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(metadata=metadata)
        self.allowed_roots = [Path(root).resolve() for root in (allowed_roots or [])]

    def _resolve_path(self, path: str) -> Path:
        candidate = Path(path).expanduser().resolve()
        if self.allowed_roots and not any(candidate == root or root in candidate.parents for root in self.allowed_roots):
            raise ValueError(f"Path is outside the allowed workspace roots: {path}")
        return candidate

    def _resolve_path_allow_external(self, path: str) -> Path:
        # Resolve without enforcing allowed_roots — used for source inputs when
        # the destination is the only mutating target governed by the capability contract.
        return Path(path).expanduser().resolve()

    def _capability_contract_allows_action(self, action: str, parameters: Dict[str, Any]) -> Optional[str]:
        capability_contract = self.metadata.get("capability_contract") or {}
        if not isinstance(capability_contract, dict):
            return None

        writable_paths = capability_contract.get("writable_paths") or []
        mutating_actions = {"write_file", "create_file", "create_directory", "copy_file", "move_file", "delete_file"}
        if action in mutating_actions and "writable_paths" in capability_contract:
            if not isinstance(writable_paths, list):
                return None
            if not writable_paths:
                return "Path is outside the writable paths allowed by the capability contract"
            # For copy/move, the destination is the mutating target; for other mutating
            # actions use the explicit `path` parameter. Fall back to source/destination
            # if the expected key is missing.
            if action in {"copy_file", "move_file"}:
                target_path = parameters.get("destination") or parameters.get("path") or parameters.get("source")
            else:
                target_path = parameters.get("path") or parameters.get("destination") or parameters.get("source")
            if target_path is not None:
                resolved_target = Path(str(target_path)).expanduser().resolve()
                allowed = [Path(str(root)).expanduser().resolve() for root in writable_paths if str(root)]
                if not any(resolved_target == root or root in resolved_target.parents for root in allowed):
                    return "Path is outside the writable paths allowed by the capability contract"

        return None

    def execute(self, request: ToolRequest) -> ToolResult:
        action = request.action
        parameters = request.parameters or {}
        if action == "read":
            action = "read_file"
        elif action == "write":
            action = "write_file"

        capability_error = self._capability_contract_allows_action(action, parameters)
        if capability_error is not None:
            return ToolResult(status="error", data={}, error=capability_error)

        try:
            if action == "read_file":
                path = self._resolve_path(parameters["path"])
                if not path.exists():
                    return ToolResult(status="ok", data={"path": str(path), "content": "", "exists": False})
                if path.is_dir():
                    return ToolResult(
                        status="ok",
                        data={
                            "path": str(path),
                            "content": "",
                            "exists": True,
                            "is_directory": True,
                            "entries": [entry.name for entry in path.iterdir()],
                        },
                    )
                return ToolResult(status="ok", data={"path": str(path), "content": path.read_text(encoding="utf-8"), "exists": True})
            if action == "write_file":
                path = self._resolve_path(parameters["path"])
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(parameters.get("content", ""), encoding="utf-8")
                return ToolResult(status="ok", data={"path": str(path), "written": True})
            if action == "create_file":
                path = self._resolve_path(parameters["path"])
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.exists():
                    raise ValueError(f"File already exists: {path}")
                path.write_text(parameters.get("content", ""), encoding="utf-8")
                return ToolResult(status="ok", data={"path": str(path), "created": True})
            if action == "create_directory":
                path = self._resolve_path(parameters["path"])
                path.mkdir(parents=True, exist_ok=True)
                return ToolResult(status="ok", data={"path": str(path), "created": True})
            if action == "list_directory":
                path = self._resolve_path(parameters["path"])
                return ToolResult(status="ok", data={"path": str(path), "entries": [entry.name for entry in path.iterdir()]})
            if action == "search_text":
                path = self._resolve_path(parameters["path"])
                query = parameters.get("query", "")
                matches = []
                for file_path in path.rglob("*"):
                    if file_path.is_file():
                        try:
                            text = file_path.read_text(encoding="utf-8")
                        except (OSError, UnicodeDecodeError):
                            continue
                        if query in text:
                            matches.append(str(file_path))
                return ToolResult(status="ok", data={"path": str(path), "query": query, "matches": matches})
            if action == "search_files":
                path = self._resolve_path(parameters["path"])
                pattern = parameters.get("pattern", "")
                matches = [str(candidate) for candidate in path.rglob(pattern)] if pattern else [str(candidate) for candidate in path.rglob("*")]
                return ToolResult(status="ok", data={"path": str(path), "pattern": pattern, "matches": matches})
            if action == "copy_file":
                src = self._resolve_path_allow_external(parameters["source"])
                dest = self._resolve_path(parameters["destination"])
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
                return ToolResult(status="ok", data={"source": str(src), "destination": str(dest)})
            if action == "move_file":
                src = self._resolve_path_allow_external(parameters["source"])
                dest = self._resolve_path(parameters["destination"])
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dest))
                return ToolResult(status="ok", data={"source": str(src), "destination": str(dest)})
            if action == "delete_file":
                path = self._resolve_path(parameters["path"])
                path.unlink(missing_ok=True)
                return ToolResult(status="ok", data={"path": str(path), "deleted": True})
            raise ValueError(f"Unsupported filesystem action: {action}")
        except Exception as exc:  # pragma: no cover - defensive guardrail path
            return ToolResult(status="error", data={}, error=str(exc))


class ShellTool(BaseTool):
    def __init__(self, allowed_commands: Optional[List[str]] = None, metadata: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(metadata=metadata)
        self.allowed_commands = allowed_commands or []

    @staticmethod
    def _normalize_command(command: Any) -> List[str]:
        if isinstance(command, (list, tuple)):
            return [str(part) for part in command]
        if isinstance(command, str):
            return shlex.split(command)
        return []

    @staticmethod
    def _contains_network_access(command: List[str]) -> bool:
        if not command:
            return False
        network_tools = {"curl", "wget", "nc", "ncat", "netcat", "ssh", "scp", "sftp", "ftp", "telnet", "ping"}
        if command[0].lower() in network_tools:
            return True
        for token in command:
            normalized = str(token).lower()
            if "http://" in normalized or "https://" in normalized or "ftp://" in normalized:
                return True
        return False

    @classmethod
    def _command_allowed(cls, command: List[str], allowed_commands: Optional[List[str]]) -> bool:
        if not command or not allowed_commands:
            return False
        requested = command
        for candidate in allowed_commands:
            allowed_tokens = cls._normalize_command(candidate)
            if not allowed_tokens:
                continue
            if requested[: len(allowed_tokens)] == allowed_tokens:
                return True
        return False

    def _build_preexec_fn(self):
        if resource is None:
            return None
        limits = {}
        max_cpu_seconds = self.metadata.get("max_cpu_seconds")
        if isinstance(max_cpu_seconds, (int, float)) and max_cpu_seconds > 0:
            limits[resource.RLIMIT_CPU] = (int(max_cpu_seconds), int(max_cpu_seconds))
        max_memory_bytes = self.metadata.get("max_memory_bytes")
        if isinstance(max_memory_bytes, int) and max_memory_bytes > 0 and hasattr(resource, "RLIMIT_AS"):
            limits[resource.RLIMIT_AS] = (max_memory_bytes, max_memory_bytes)
        if not limits or os.name != "posix":
            return None

        def apply_limits() -> None:
            for limit, values in limits.items():
                resource.setrlimit(limit, values)

        return apply_limits

    def execute(self, request: ToolRequest) -> ToolResult:
        parameters = request.parameters or {}
        command = self._normalize_command(parameters.get("command"))
        if not command:
            return ToolResult(status="error", data={}, error="A shell command is required")

        requested_command = list(command)
        capability_contract = self.metadata.get("capability_contract") or {}
        if not isinstance(capability_contract, dict):
            capability_contract = {}
        if str(self.metadata.get("sandbox_profile", "")).lower() == "strict" and self._contains_network_access(requested_command):
            return ToolResult(
                status="error",
                data={"command": requested_command},
                error="Network access is not allowed in strict sandbox mode",
            )
        if capability_contract.get("allow_network") is False and self._contains_network_access(requested_command):
            return ToolResult(
                status="error",
                data={"command": requested_command},
                error="Network access is not allowed by the capability contract",
            )
        capability_allowed_commands = capability_contract.get("allowed_commands")
        if isinstance(capability_allowed_commands, list) and capability_allowed_commands and not self._command_allowed(requested_command, capability_allowed_commands):
            return ToolResult(
                status="error",
                data={"command": requested_command},
                error=f"Shell command {requested_command[0]!r} is not allowed by the capability contract",
            )
        if self.allowed_commands and not self._command_allowed(requested_command, self.allowed_commands):
            return ToolResult(status="error", data={"command": requested_command}, error=f"Shell command {requested_command[0]!r} is not allowed")
        resolved_command = list(command)
        if resolved_command[0] in {"python", "python3"}:
            resolved_command = [sys.executable] + resolved_command[1:]

        cwd = parameters.get("cwd")
        allowed_cwds = [Path(root).expanduser().resolve() for root in self.metadata.get("allowed_cwds", [])]
        if cwd is None and allowed_cwds:
            cwd = str(allowed_cwds[0])
        if cwd is not None and allowed_cwds:
            resolved_cwd = Path(str(cwd)).expanduser().resolve()
            if not any(resolved_cwd == root or root in resolved_cwd.parents for root in allowed_cwds):
                return ToolResult(status="error", data={"cwd": str(cwd)}, error="Shell working directory is outside the allowed roots")

        timeout = parameters.get("timeout")
        max_timeout = self.metadata.get("max_timeout_seconds")
        if isinstance(max_timeout, (int, float)) and max_timeout > 0:
            if timeout is None or not isinstance(timeout, (int, float)) or timeout <= 0:
                timeout = max_timeout
            else:
                timeout = min(timeout, max_timeout)

        env = parameters.get("env") or None
        allowed_env_keys = self.metadata.get("allowed_env_keys")
        if isinstance(allowed_env_keys, list):
            allowed_env_keys = [str(key) for key in allowed_env_keys]
        else:
            allowed_env_keys = ["PATH", "HOME", "LANG", "LC_ALL", "PYTHONPATH"]
        inherited_env = {key: os.environ[key] for key in allowed_env_keys if key in os.environ}
        if env is None:
            env = inherited_env
        else:
            unexpected_keys = sorted(set(env) - set(allowed_env_keys))
            if unexpected_keys:
                return ToolResult(
                    status="error",
                    data={"unexpected_environment_keys": unexpected_keys},
                    error="Shell environment variable is not allowed",
                )
            env = {**inherited_env, **env}

        started_at = time.perf_counter()
        process = None
        try:
            process = subprocess.Popen(
                resolved_command,
                cwd=str(cwd) if cwd is not None else None,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                shell=False,
                preexec_fn=self._build_preexec_fn(),
                start_new_session=os.name == "posix",
            )
            stdout, stderr = process.communicate(timeout=timeout)
            return_code = process.returncode
        except subprocess.TimeoutExpired as exc:
            if process is not None:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                stdout, stderr = process.communicate()
            else:
                stdout, stderr = exc.stdout or "", exc.stderr or ""
            duration_ms = round((time.perf_counter() - started_at) * 1000, 3)
            return ToolResult(
                status="error",
                data={
                    "exit_code": None,
                    "stdout": stdout or "",
                    "stderr": stderr or "",
                    "succeeded": False,
                    "duration_ms": duration_ms,
                    "metadata": {
                        "command": requested_command,
                        "cwd": cwd,
                        "timeout": timeout,
                        "allowed_commands": self.allowed_commands,
                    },
                },
                error=f"Command timed out after {timeout}s (timeout policy)",
            )
        duration_ms = round((time.perf_counter() - started_at) * 1000, 3)
        succeeded = return_code == 0
        max_output_bytes = self.metadata.get("max_output_bytes")
        output_size = len((stdout or "").encode("utf-8")) + len((stderr or "").encode("utf-8"))
        if isinstance(max_output_bytes, int) and max_output_bytes > 0 and output_size > max_output_bytes:
            return ToolResult(
                status="error",
                data={
                    "exit_code": return_code,
                    "stdout": (stdout or "")[:max_output_bytes],
                    "stderr": (stderr or "")[:max_output_bytes],
                    "succeeded": False,
                    "duration_ms": duration_ms,
                    "metadata": {
                        "command": requested_command,
                        "cwd": cwd,
                        "timeout": timeout,
                        "allowed_commands": self.allowed_commands,
                        "output_size_bytes": output_size,
                        "max_output_bytes": max_output_bytes,
                    },
                },
                error=f"Command output exceeded the {max_output_bytes}-byte output limit",
            )
        data = {
            "exit_code": return_code,
            "stdout": stdout or "",
            "stderr": stderr or "",
            "succeeded": succeeded,
            "duration_ms": duration_ms,
            "metadata": {
                "command": requested_command,
                "cwd": cwd,
                "timeout": timeout,
                "allowed_commands": self.allowed_commands,
            },
        }
        return ToolResult(status="ok" if succeeded else "error", data=data)


class GitTool(BaseTool):
    def __init__(self, allowed_actions: Optional[List[str]] = None, metadata: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(metadata=metadata)
        self.allowed_actions = allowed_actions or []

    @staticmethod
    def _normalize_command(command: Any) -> List[str]:
        if isinstance(command, (list, tuple)):
            return [str(part) for part in command]
        if isinstance(command, str):
            return shlex.split(command)
        return []

    @staticmethod
    def _validate_repo_path(repo_path: Any) -> Path:
        resolved_repo = Path(repo_path).expanduser().resolve()
        if not resolved_repo.exists():
            raise ValueError(f"Repository path does not exist: {repo_path}")
        if not resolved_repo.is_dir():
            raise ValueError(f"Repository path is not a directory: {repo_path}")
        return resolved_repo

    def execute(self, request: ToolRequest) -> ToolResult:
        parameters = request.parameters or {}
        action = request.action
        if action not in self.allowed_actions:
            raise ValueError(f"Git action not allowed: {action}")

        repo_path = self._validate_repo_path(parameters.get("repo_path") or ".")
        if action == "inspect":
            return self._inspect(repo_path)
        if action == "status":
            return self._status(repo_path)
        if action == "diff":
            return self._diff(repo_path)
        if action == "commit":
            return self._commit(repo_path, parameters)
        if action == "checkout":
            return self._checkout(repo_path, parameters)
        raise ValueError(f"Unsupported git action: {action}")

    def _inspect(self, repo_path: Path) -> ToolResult:
        command = ["git", "rev-parse", "--show-toplevel"]
        completed = subprocess.run(command, cwd=str(repo_path), capture_output=True, text=True, shell=False)
        return self._build_result(completed, repo_path, "inspect", {
            "repo_path": str(repo_path),
        })

    def _status(self, repo_path: Path) -> ToolResult:
        command = ["git", "status", "--short"]
        completed = subprocess.run(command, cwd=str(repo_path), capture_output=True, text=True, shell=False)
        return self._build_result(completed, repo_path, "status", {
            "repo_path": str(repo_path),
        })

    def _diff(self, repo_path: Path) -> ToolResult:
        command = ["git", "diff", "--", "."]
        completed = subprocess.run(command, cwd=str(repo_path), capture_output=True, text=True, shell=False)
        return self._build_result(completed, repo_path, "diff", {
            "repo_path": str(repo_path),
        })

    def _commit(self, repo_path: Path, parameters: Dict[str, Any]) -> ToolResult:
        files = parameters.get("files") or []
        message = parameters.get("message") or "hermes commit"
        if not isinstance(files, list) or not files:
            raise ValueError("Git commit requires at least one file in 'files'")
        add_command = ["git", "add", *files]
        add_completed = subprocess.run(add_command, cwd=str(repo_path), capture_output=True, text=True, shell=False)
        if add_completed.returncode != 0:
            return self._build_result(add_completed, repo_path, "commit", {"files": files, "message": message})

        command = ["git", "commit", "-m", message]
        completed = subprocess.run(command, cwd=str(repo_path), capture_output=True, text=True, shell=False)
        result = self._build_result(completed, repo_path, "commit", {"files": files, "message": message})
        if completed.returncode == 0:
            hash_completed = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo_path), capture_output=True, text=True, shell=False)
            if hash_completed.returncode == 0:
                result.data["commit_hash"] = hash_completed.stdout.strip()
        return result

    def _checkout(self, repo_path: Path, parameters: Dict[str, Any]) -> ToolResult:
        branch = parameters.get("branch")
        if not branch:
            raise ValueError("Git checkout requires a 'branch' parameter")
        existing = subprocess.run(["git", "rev-parse", "--verify", branch], cwd=str(repo_path), capture_output=True, text=True, shell=False)
        if existing.returncode != 0:
            create_completed = subprocess.run(["git", "checkout", "-b", branch], cwd=str(repo_path), capture_output=True, text=True, shell=False)
            return self._build_result(create_completed, repo_path, "checkout", {"branch": branch})
        checkout_completed = subprocess.run(["git", "checkout", branch], cwd=str(repo_path), capture_output=True, text=True, shell=False)
        return self._build_result(checkout_completed, repo_path, "checkout", {"branch": branch})

    def _build_result(self, completed: subprocess.CompletedProcess[str], repo_path: Path, action: str, metadata: Dict[str, Any]) -> ToolResult:
        stdout = completed.stdout.strip()
        stderr = completed.stderr.strip()
        if action == "checkout" and not stdout and stderr:
            stdout = stderr
            stderr = ""

        duration_ms = 0.0
        data = {
            "exit_code": completed.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "succeeded": completed.returncode == 0,
            "duration_ms": duration_ms,
            "metadata": {
                "action": action,
                "repo_path": str(repo_path),
                "allowed_actions": self.allowed_actions,
                **metadata,
            },
        }
        return ToolResult(status="ok" if completed.returncode == 0 else "error", data=data)
