from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import ToolRequest


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

    def execute(self, request: ToolRequest) -> ToolResult:
        action = request.action
        parameters = request.parameters or {}
        if action == "read":
            action = "read_file"
        elif action == "write":
            action = "write_file"

        if action == "read_file":
            path = self._resolve_path(parameters["path"])
            if not path.exists():
                return ToolResult(status="ok", data={"path": str(path), "content": "", "exists": False})
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
            src = self._resolve_path(parameters["source"])
            dest = self._resolve_path(parameters["destination"])
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            return ToolResult(status="ok", data={"source": str(src), "destination": str(dest)})
        if action == "move_file":
            src = self._resolve_path(parameters["source"])
            dest = self._resolve_path(parameters["destination"])
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
            return ToolResult(status="ok", data={"source": str(src), "destination": str(dest)})
        if action == "delete_file":
            path = self._resolve_path(parameters["path"])
            path.unlink(missing_ok=True)
            return ToolResult(status="ok", data={"path": str(path), "deleted": True})
        raise ValueError(f"Unsupported filesystem action: {action}")


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

    def execute(self, request: ToolRequest) -> ToolResult:
        parameters = request.parameters or {}
        command = self._normalize_command(parameters.get("command"))
        if not command:
            raise ValueError("A shell command is required")

        cwd = parameters.get("cwd")
        timeout = parameters.get("timeout")
        env = parameters.get("env") or None
        started_at = time.perf_counter()
        completed = subprocess.run(
            command,
            cwd=str(cwd) if cwd is not None else None,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
        )
        duration_ms = round((time.perf_counter() - started_at) * 1000, 3)
        succeeded = completed.returncode == 0
        data = {
            "exit_code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "succeeded": succeeded,
            "duration_ms": duration_ms,
            "metadata": {
                "command": command,
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

    def execute(self, request: ToolRequest) -> ToolResult:
        parameters = request.parameters or {}
        action = request.action
        if action not in self.allowed_actions:
            raise ValueError(f"Git action not allowed: {action}")

        repo_path = parameters.get("repo_path") or "."
        command = ["git", action]
        if action == "status":
            command.append("--short")
        started_at = time.perf_counter()
        completed = subprocess.run(
            command,
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            shell=False,
        )
        duration_ms = round((time.perf_counter() - started_at) * 1000, 3)
        data = {
            "exit_code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "succeeded": completed.returncode == 0,
            "duration_ms": duration_ms,
            "metadata": {
                "action": action,
                "repo_path": repo_path,
                "allowed_actions": self.allowed_actions,
            },
        }
        return ToolResult(status="ok" if completed.returncode == 0 else "error", data=data)
