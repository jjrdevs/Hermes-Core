from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from engine.models import Artifact, ExecutionContext, TaskEnvelope, WorkflowDefinition, ToolContract, ToolExecutionEnvelope, ToolRequest, WorkerRequest, _now_iso
from engine.runtime import RuntimeKernel
from engine.storage import SQLiteMemoryStore, SQLiteRunStore
from engine.tool_runtime import FilesystemTool
from engine.workflow_loader import build_tool_definitions, build_workflow_definition, load_json_file, resolve_data_paths, resolve_input_path
from workers.local_worker import LocalWorker
from workers.model_adapter import ModelAdapterConfig, ModelAdapterFactory, ModelAdapterRouter, ProviderProfile


class RuntimeService:
    def __init__(self, data_dir: Optional[str] = None) -> None:
        self.data_dir = data_dir
        self.paths = resolve_data_paths(data_dir)
        self.kernel = RuntimeKernel(self.paths["event_db"], self.paths["artifact_db"], self.paths["workflow_definition_db"])
        self.run_store = SQLiteRunStore(self.paths["event_db"].parent / "runs.db")
        self.memory_store = SQLiteMemoryStore(self.paths["event_db"].parent / "memory.db", max_entries=50)
        self._background_threads: Dict[str, threading.Thread] = {}
        self._state_lock = threading.RLock()
        self._resume_pending_runs()

    def _update_liveness_and_recovery(self, run_id: str, run_record: Dict[str, Any]) -> None:
        now = _now_iso()
        liveness = dict(run_record.setdefault("liveness", {}))
        liveness["last_heartbeat_at"] = now
        liveness["status"] = run_record.get("status")
        recovery = dict(run_record.setdefault("recovery", {}))
        recovery.setdefault("restart_count", 0)
        run_record["liveness"] = liveness
        run_record["recovery"] = recovery
        run_record["last_updated_at"] = now
        self.run_store.update(run_id, run_record)

    def _load_workflow(self, workflow_path: str) -> WorkflowDefinition:
        resolved_path = resolve_input_path(workflow_path)
        data = load_json_file(resolved_path)
        workflow_definition = build_workflow_definition(data)
        tools = build_tool_definitions(data)

        for tool in tools:
            self.kernel.register_tool(tool)
        self.kernel.register_workflow_definition(workflow_definition)
        return workflow_definition

    def _build_model_router(self, provider: str = "stub", model_name: Optional[str] = None, endpoint: Optional[str] = None) -> ModelAdapterRouter:
        return ModelAdapterRouter(
            profiles=[
                ProviderProfile(provider="stub", cost_class="cheap", available=True, healthy=True, timeout_seconds=2.0, options={"cost_per_token": 0.0}),
                ProviderProfile(provider="ollama", cost_class="local", available=True, healthy=True, timeout_seconds=30.0, options={"cost_per_token": 0.000002}),
            ],
            preferred_provider=provider,
        )

    def _route_provider(self, prompt: str, provider: str = "stub", model_name: Optional[str] = None, endpoint: Optional[str] = None):
        router = self._build_model_router(provider=provider, model_name=model_name, endpoint=endpoint)
        decision = router.route(prompt, task_complexity="simple")
        if provider not in {profile.provider for profile in router.profiles}:
            decision.health_summary.setdefault("provider_health", {})[provider] = {
                "available": False,
                "healthy": False,
                "cost_class": "unknown",
                "endpoint": None,
                "timeout_seconds": None,
                "timeout_classification": "unknown",
                "cost_per_token": None,
                "health_status": "unavailable",
                "retry_classification": "skipped",
            }
        return decision

    def _build_model_adapter(self, provider: str = "stub", model_name: Optional[str] = None, endpoint: Optional[str] = None):
        decision = self._route_provider("default runtime task", provider=provider, model_name=model_name, endpoint=endpoint)
        return decision.adapter

    def _execute_step(self, execution_id: str, step_execution_id: str, model_adapter: Any) -> None:
        execution = self.kernel.step_executions[step_execution_id]
        workflow_definition = self.kernel.workflow_definitions[self.kernel.workflow_executions[execution_id].workflow_definition_id]
        step_definition = workflow_definition.steps[0]
        for candidate in workflow_definition.steps:
            if candidate.id == execution.step_id:
                step_definition = candidate
                break

        decision = self.kernel.assign_execution(
            step_execution_id,
            model_adapter=model_adapter,
            capability=step_definition.role,
            objective=step_definition.objective,
        )
        if not decision.allowed:
            raise RuntimeError(f"Assignment denied: {decision.reason}")

        decision = self.kernel.start_execution(step_execution_id)
        if not decision.allowed:
            raise RuntimeError(f"Start denied: {decision.reason}")

        worker = LocalWorker(model_adapter)
        request = WorkerRequest(
            execution_id=step_execution_id,
            workflow_id=execution.workflow_id,
            role=step_definition.role,
            objective=step_definition.objective,
            context={
                "artifact_refs": execution.input_artifacts,
                "expected_outputs": step_definition.outputs,
            },
            constraints=step_definition.constraints,
        )
        response = worker.execute(request)
        for recommendation in response.recommendations:
            if recommendation is not None:
                self.kernel.request_tool(step_execution_id, recommendation.tool_id, recommendation.action, recommendation.parameters)
        self.kernel.complete_execution(step_execution_id, response.artifacts_created)

    def _advance_workflow(
        self,
        execution_id: str,
        *,
        provider: str = "stub",
        model_name: Optional[str] = None,
        endpoint: Optional[str] = None,
        stop_on_pause: bool = False,
    ) -> None:
        model_adapter = self._build_model_adapter(provider=provider, model_name=model_name, endpoint=endpoint)

        while not self.kernel.workflow_complete(execution_id):
            step_execution_id = self.kernel.schedule_next_step(execution_id)
            if step_execution_id is None:
                if self.kernel.get_workflow_status(execution_id) == "WAITING_APPROVAL":
                    return
                if self.kernel.workflow_complete(execution_id):
                    break
                if stop_on_pause:
                    return
                raise RuntimeError("Workflow is not complete and no ready steps remain")

            self._execute_step(execution_id, step_execution_id, model_adapter)
            if stop_on_pause and self.kernel.get_workflow_status(execution_id) in {"WAITING_APPROVAL", "COMPLETED", "FAILED"}:
                break

        if self.kernel.workflow_complete(execution_id):
            self.kernel.complete_workflow(execution_id)

    def _inspect_workspace(self, workspace_path: Optional[str], context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        resolved_path = Path(workspace_path or self.data_dir or ".").expanduser().resolve()
        inspection_context = context or {}
        max_files = max(1, int(inspection_context.get("max_inspection_files", 20)))
        max_directories = max(1, int(inspection_context.get("max_inspection_directories", 20)))
        max_depth = max(0, int(inspection_context.get("max_inspection_depth", 0)))
        max_bytes = max(0, int(inspection_context.get("max_inspection_bytes", 50_000)))
        if not resolved_path.exists():
            return {"workspace_path": str(resolved_path), "exists": False, "files": [], "directories": []}
        files: List[str] = []
        directories: List[str] = []
        bytes_read = 0
        file_previews: Dict[str, str] = {}
        pending = [(resolved_path, 0)]
        while pending and len(files) < max_files:
            current_path, depth = pending.pop(0)
            entries = sorted(current_path.iterdir())
            for entry in entries:
                relative_name = str(entry.relative_to(resolved_path))
                if entry.is_file() and len(files) < max_files:
                    files.append(relative_name)
                    if max_bytes and bytes_read < max_bytes:
                        try:
                            remaining_bytes = max_bytes - bytes_read
                            raw_content = entry.read_bytes()[:remaining_bytes]
                            bytes_read += len(raw_content)
                            try:
                                file_previews[relative_name] = raw_content.decode("utf-8")
                            except UnicodeDecodeError:
                                pass
                        except OSError:
                            pass
                elif entry.is_dir() and len(directories) < max_directories:
                    directories.append(relative_name)
                    if depth < max_depth:
                        pending.append((entry, depth + 1))
        marker_files = [name for name in ["pyproject.toml", "setup.py", "requirements.txt", "package.json", "Makefile", "pytest.ini"] if (resolved_path / name).exists()]
        return {
            "workspace_path": str(resolved_path),
            "exists": True,
            "files": files,
            "directories": directories,
            "marker_files": marker_files,
            "file_previews": file_previews,
            "inspection_limits": {"max_files": max_files, "max_directories": max_directories, "max_depth": max_depth, "max_bytes": max_bytes},
            "inspection_usage": {"files_scanned": len(files), "directories_scanned": len(directories), "bytes_read": bytes_read},
        }

    def _evaluate_verification(self, workspace_path: Optional[str]) -> Dict[str, Any]:
        resolved_path = Path(workspace_path or self.data_dir or ".").expanduser().resolve()
        marker_files = [name for name in ["pyproject.toml", "setup.py", "requirements.txt", "package.json", "Makefile", "pytest.ini"] if (resolved_path / name).exists()]
        if marker_files:
            return {
                "status": "passed",
                "details": f"Found verification markers: {', '.join(marker_files)}",
            }
        return {
            "status": "not_run",
            "details": "No standard verification markers were found for this workspace.",
        }

    def _derive_next_steps(self, workspace_summary: Dict[str, Any]) -> List[str]:
        next_steps: List[str] = []
        files = set(workspace_summary.get("files", []))
        directories = set(workspace_summary.get("directories", []))
        marker_files = set(workspace_summary.get("marker_files", []))
        all_entries = files | directories

        if "tests" in directories or any(entry.startswith("test") for entry in all_entries):
            next_steps.append("Inspect the test layout and run the repository's preferred verification command for the relevant test suite.")
            next_steps.append("Run the relevant pytest or project test command for the discovered test files.")

        if any(entry.endswith(".py") for entry in files):
            next_steps.append("Review the Python entry points and any existing test modules before making changes.")

        if "Makefile" in marker_files:
            next_steps.append("Use the repository's Makefile target as the primary verification entry point.")
        elif "package.json" in marker_files:
            next_steps.append("Use the package.json test script as the primary verification entry point.")
        elif "pyproject.toml" in marker_files:
            next_steps.append("Use the pyproject.toml-based test command for verification.")

        if "requirements.txt" in marker_files or "setup.py" in marker_files:
            next_steps.append("Confirm the local environment matches the repository's declared dependencies before running verification.")

        if not next_steps:
            next_steps.append("Create a small verification command for this repository and record it in the task summary.")
        return next_steps

    def _discover_verification_command(self, workspace_path: Optional[str]) -> Optional[List[str]]:
        resolved_path = Path(workspace_path or self.data_dir or ".").expanduser().resolve()
        if not resolved_path.exists():
            return None

        makefile = resolved_path / "Makefile"
        if makefile.exists():
            return ["make", "test"]

        package_json = resolved_path / "package.json"
        if package_json.exists():
            try:
                package_data = json.loads(package_json.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                package_data = {}
            scripts = package_data.get("scripts", {}) if isinstance(package_data, dict) else {}
            if isinstance(scripts, dict) and "test" in scripts:
                return ["npm", "test"]

        pyproject_toml = resolved_path / "pyproject.toml"
        if pyproject_toml.exists():
            return ["pytest", "-q"]

        return None

    def _resolve_explicit_verification_command(self, workspace_path: Optional[str], command: Any, capability_contract: Any = None) -> tuple[Optional[List[str]], Optional[str]]:
        if isinstance(command, str):
            command = shlex.split(command)
        if isinstance(command, tuple):
            command = [str(part) for part in command]
        if not isinstance(command, list) or not command or not all(isinstance(part, str) and part.strip() for part in command):
            return None, "verification_command_invalid"
        resolved_workspace = Path(workspace_path or self.data_dir or ".").expanduser().resolve()
        executable = command[0]
        if executable not in {"python", "python3", "pytest", "make", "npm"}:
            return None, "verification_command_not_allowed"

        validation_error = self._validate_verification_command(command, workspace_path, capability_contract)
        if validation_error is not None:
            return None, validation_error

        return list(command), None

    def _validate_verification_command(self, command: List[str], workspace_path: Optional[str], capability_contract: Any = None) -> Optional[str]:
        resolved_workspace = Path(workspace_path or self.data_dir or ".").expanduser().resolve()
        executable = command[0]

        normalized_contract = self._normalize_capability_contract(capability_contract)
        allowed_commands = normalized_contract.get("allowed_commands") or []
        if isinstance(allowed_commands, list) and allowed_commands:
            if not any(self._is_command_allowed_by_capability(executable, command, allowed_commands) for _ in [0]):
                return "verification_command_not_allowed"

        if normalized_contract.get("allow_network") is False and self._command_contains_network_access(command):
            return "verification_command_not_allowed"

        path_operands = []
        if executable in {"python", "python3"} and len(command) > 1 and not command[1].startswith("-"):
            path_operands.append(command[1])
        elif executable == "pytest":
            path_operands.extend(part for part in command[1:] if not part.startswith("-") and ("/" in part or "\\" in part or "." in part))
        elif executable == "make":
            path_operands.extend(part for part in command[1:] if not part.startswith("-") and ("/" in part or "\\" in part))
        elif executable == "npm":
            path_operands.extend(part for part in command[1:] if part.startswith("./") or part.startswith("../") or part.startswith("/"))

        for operand in path_operands:
            target = (resolved_workspace / operand).resolve()
            if resolved_workspace not in target.parents and target != resolved_workspace:
                return "verification_command_outside_workspace"

        return None

    @staticmethod
    def _command_contains_network_access(command: List[str]) -> bool:
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

    @staticmethod
    def _is_command_allowed_by_capability(executable: str, command: List[str], allowed_commands: List[str]) -> bool:
        if not allowed_commands:
            return True
        requested_tokens = [str(token) for token in command if str(token)]
        if not requested_tokens:
            return False
        for candidate in allowed_commands:
            candidate_tokens = shlex.split(str(candidate))
            if not candidate_tokens:
                continue
            if requested_tokens[: len(candidate_tokens)] == candidate_tokens:
                return True
        return executable in {str(item) for item in allowed_commands if str(item)}

    def _run_verification(
        self,
        workspace_path: Optional[str],
        explicit_command: Optional[List[str]] = None,
        max_output_bytes: Optional[int] = None,
        timeout_seconds: int = 30,
        capability_contract: Any = None,
    ) -> Dict[str, Any]:
        resolved_path = Path(workspace_path or self.data_dir or ".").expanduser().resolve()
        if not resolved_path.exists():
            return {"status": "not_run", "details": "Workspace does not exist", "command": None, "stdout": "", "stderr": "", "return_code": None}

        command = explicit_command or self._discover_verification_command(resolved_path)
        if command is None:
            test_files = [str(path) for path in resolved_path.rglob("test_*.py") if path.is_file()]
            if not test_files:
                return {"status": "not_run", "details": "No pytest tests were found", "command": None, "stdout": "", "stderr": "", "return_code": None}
            command = ["pytest", "-q", *test_files]

        validation_error = self._validate_verification_command(command, workspace_path, capability_contract)
        if validation_error is not None:
            return {
                "status": "failed",
                "details": validation_error,
                "command": command,
                "stdout": "",
                "stderr": "",
                "return_code": None,
            }

        resolved_command = list(command)
        if resolved_command:
            if resolved_command[0] in {"python", "python3"}:
                resolved_command = [sys.executable] + resolved_command[1:]
            elif resolved_command[0] == "pytest":
                resolved_command = [sys.executable, "-m", "pytest"] + resolved_command[1:]
        env = os.environ.copy()
        venv_bin = str(Path(sys.executable).parent)
        if venv_bin and venv_bin not in env.get("PATH", ""):
            env["PATH"] = f"{venv_bin}{os.pathsep}{env.get('PATH', '')}"

        try:
            completed = subprocess.run(
                resolved_command,
                cwd=str(resolved_path),
                env=env,
                capture_output=True,
                text=True,
                timeout=max(1, min(30, int(timeout_seconds))),
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return {"status": "failed", "details": f"Verification command failed: {exc}", "command": resolved_command, "stdout": "", "stderr": str(exc), "return_code": None}

        stdout = completed.stdout.strip()
        stderr = completed.stderr.strip()
        output_size = len(stdout.encode("utf-8")) + len(stderr.encode("utf-8"))
        if isinstance(max_output_bytes, int) and max_output_bytes > 0 and output_size > max_output_bytes:
            return {
                "status": "failed",
                "details": "verification_output_exceeded",
                "command": command,
                "stdout": stdout[:max_output_bytes],
                "stderr": stderr[:max_output_bytes],
                "return_code": completed.returncode,
                "output_size_bytes": output_size,
            }
        if completed.returncode == 0:
            return {"status": "passed", "details": "Verification passed", "command": command, "stdout": stdout, "stderr": stderr, "return_code": completed.returncode}
        return {"status": "failed", "details": "Verification failed", "command": command, "stdout": stdout, "stderr": stderr, "return_code": completed.returncode}

    def _measure_elapsed_ms(self, started_at: Optional[str]) -> Optional[int]:
        if not started_at:
            return None
        try:
            from datetime import datetime

            started_dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            completed_dt = datetime.now(started_dt.tzinfo)
            delta_ms = int((completed_dt - started_dt).total_seconds() * 1000)
            return max(0, delta_ms)
        except Exception:
            return None

    def _resolve_tool_contract(self, request: ToolRequest) -> ToolContract:
        if request.tool_id == "filesystem":
            return ToolContract(
                tool_id="filesystem",
                name="Filesystem",
                description="Read and write workspace files",
                actions=["read_file", "write_file", "list_directory", "create_file", "create_directory", "delete_file", "move_file", "copy_file"],
                allowed_roles=["developer"],
                metadata={"sandbox": "local"},
                risk_level="low",
                timeout_seconds=10,
            )
        if request.tool_id == "shell":
            return ToolContract(
                tool_id="shell",
                name="Shell",
                description="Run shell commands in the workspace",
                actions=["run"],
                allowed_roles=["developer"],
                metadata={
                    "sandbox": "local",
                    "allowed_commands": ["pytest", "python", "python3"],
                    "allowed_env_keys": ["PATH", "HOME", "LANG", "LC_ALL", "PYTHONPATH"],
                },
                risk_level="medium",
                timeout_seconds=30,
            )
        return ToolContract(
            tool_id=request.tool_id,
            name=request.tool_id,
            description="Generic tool",
            actions=[],
            allowed_roles=["developer"],
            metadata={},
            risk_level="low",
            timeout_seconds=10,
        )

    def _classify_tool_risk(self, request: ToolRequest, contract: ToolContract) -> str:
        action_name = str(request.action or "").lower()
        if request.tool_id == "filesystem" and action_name in {"delete_file", "move_file", "copy_file"}:
            return "destructive"
        if request.tool_id == "git" and action_name in {"commit", "checkout", "reset", "merge", "push"}:
            return "destructive"
        if request.tool_id == "shell":
            return "write_safe"
        if action_name in {"read_file", "list_directory", "search_text", "search_files", "inspect", "status", "diff"}:
            return "read_only"

        normalized_risk = str(contract.risk_level or "").lower()
        if normalized_risk in {"read_only", "write_safe", "destructive", "privileged", "external_network"}:
            return normalized_risk
        if normalized_risk in {"low", "minor"}:
            return "read_only"
        if normalized_risk in {"medium", "moderate"}:
            return "write_safe"
        if normalized_risk in {"high", "critical"}:
            return "destructive"
        return "write_safe"

    def _find_step_execution_id_for_run(self, run_id: Optional[str]) -> Optional[str]:
        if not run_id:
            return None
        run_record = self.run_store.get(run_id)
        if run_record is None:
            return None
        execution_id = run_record.get("execution_id")
        workflow_execution = self.kernel.workflow_executions.get(execution_id)
        if workflow_execution is None:
            return None
        for step_execution in self.kernel.step_executions.values():
            if step_execution.workflow_id == workflow_execution.execution_id:
                return step_execution.execution_id
        return None

    def _resolve_step_execution_id(self, run_id: Optional[str], step_execution_id: Optional[str] = None) -> Optional[str]:
        if step_execution_id:
            return step_execution_id
        return self._find_step_execution_id_for_run(run_id)

    def _handle_tool_approval_gate(self, request: ToolRequest, *, run_id: Optional[str], risk_level: str, step_execution_id: Optional[str] = None) -> Optional[ToolExecutionEnvelope]:
        if run_id is None:
            return None
        if risk_level not in {"destructive", "privileged", "external_network"}:
            return None
        run_record = self.run_store.get(run_id)
        if run_record is None:
            return None
        workflow_execution = self.kernel.workflow_executions.get(run_record.get("execution_id"))
        if workflow_execution is None:
            return None
        resolved_step_execution_id = self._resolve_step_execution_id(run_id, step_execution_id)
        if not resolved_step_execution_id:
            return None

        # If the capability contract explicitly allows the mutating target, skip approval
        if request.tool_id == "filesystem":
            capability_contract = self._service_capability_contract(run_id, step_execution_id=step_execution_id)
            writable_paths = list(capability_contract.get("writable_paths") or [])
            if writable_paths and isinstance(request.parameters, dict):
                target = request.parameters.get("path") or request.parameters.get("destination") or request.parameters.get("source")
                try:
                    if target:
                        resolved_target_path = Path(str(target)).expanduser().resolve()
                        allowed_roots = [Path(str(root)).expanduser().resolve() for root in writable_paths if str(root)]
                        for root_path in allowed_roots:
                            if resolved_target_path == root_path or root_path in resolved_target_path.parents:
                                return None
                except Exception:
                    # Fall through to normal approval behavior on any resolution error
                    pass

        request_fingerprint = self.kernel._tool_request_fingerprint(request.tool_id, request.action, request.parameters)
        approval_id = f"tool-approval-{request_fingerprint[:16]}"
        approval_state = self.kernel._tool_approval_state(workflow_execution, approval_id, request_fingerprint)
        if approval_state == "denied":
            return ToolExecutionEnvelope(
                request=request,
                status="error",
                output={},
                error="tool approval denied",
                metadata={"approval_id": approval_id, "risk_level": risk_level, "request_fingerprint": request_fingerprint},
            )
        if approval_state == "granted":
            return None

        self.kernel._emit_tool_approval_required(
            resolved_step_execution_id,
            request.tool_id,
            request.action,
            request.parameters,
            risk_level,
            approval_id,
            request_fingerprint,
        )
        self._record_run_event(
            run_id,
            "TOOL_APPROVAL_REQUIRED",
            {
                "approval_id": approval_id,
                "tool_id": request.tool_id,
                "action": request.action,
                "risk_level": risk_level,
                "request_fingerprint": request_fingerprint,
            },
        )
        return ToolExecutionEnvelope(
            request=request,
            status="pending_approval",
            output={"approval_id": approval_id, "risk_level": risk_level, "request_fingerprint": request_fingerprint},
            error=None,
            metadata={"approval_id": approval_id, "risk_level": risk_level, "request_fingerprint": request_fingerprint},
        )

    def _load_tool_execution_ledger_entry(self, run_id: Optional[str], fingerprint: str) -> Optional[ToolExecutionEnvelope]:
        if run_id is None:
            return None
        run_record = self.run_store.get(run_id)
        entry = (run_record or {}).get("tool_execution_ledger", {}).get(fingerprint)
        if not isinstance(entry, dict):
            return None
        return ToolExecutionEnvelope(
            request=ToolRequest(
                tool_id=entry.get("request", {}).get("tool_id", ""),
                action=entry.get("request", {}).get("action", ""),
                parameters=entry.get("request", {}).get("parameters", {}),
            ),
            status=entry.get("status", "error"),
            output=entry.get("output", {}),
            error=entry.get("error"),
            metadata=entry.get("metadata", {}),
        )

    def _store_tool_execution_ledger_entry(self, run_id: Optional[str], fingerprint: str, envelope: ToolExecutionEnvelope) -> None:
        if run_id is None:
            return
        run_record = self.run_store.get(run_id)
        if run_record is None:
            return
        ledger = dict(run_record.get("tool_execution_ledger", {}))
        ledger[fingerprint] = envelope.to_dict()
        run_record["tool_execution_ledger"] = ledger
        self.run_store.update(run_id, run_record)

    def _evaluate_service_tool_policy(self, request: ToolRequest, contract: ToolContract, risk_level: str, run_id: Optional[str], step_execution_id: Optional[str] = None) -> Optional[str]:
        if risk_level in {"destructive", "privileged", "external_network"} and run_id is None:
            return "tool_approval_context_required"
        if run_id is None:
            return None
        resolved_step_execution_id = self._resolve_step_execution_id(run_id, step_execution_id)
        if resolved_step_execution_id is None:
            return "tool_policy_context_missing"
        metadata = contract.metadata or {}
        policy_context = self._service_tool_policy_context(run_id, step_execution_id=step_execution_id)
        decision = self.kernel.policy_evaluator.evaluate_tool_request(
            tool_id=request.tool_id,
            tool_action=request.action,
            policy_context=policy_context,
            step_constraints={"allow_execution": True, "allowed_tools": [request.tool_id]},
            tool_constraints={
                "command": request.parameters.get("command"),
                "allowed_commands": metadata.get("allowed_commands", []),
                "allowed_actions": metadata.get("allowed_actions", []),
                "env": request.parameters.get("env"),
                "path": request.parameters.get("path"),
                "source": request.parameters.get("source"),
                "destination": request.parameters.get("destination"),
            },
            strict_policy_context=True,
            extra_context={
                "tool_risk_level": risk_level,
                "sandbox_profile": self._service_sandbox_profile(run_id, step_execution_id=step_execution_id),
                "capability_requirements": self._service_capability_requirements(run_id, step_execution_id=step_execution_id),
                "tool_capabilities": self._service_tool_capabilities(request.tool_id, request.action),
                "capability_contract": self._service_capability_contract(run_id, step_execution_id=step_execution_id),
            },
        )
        return None if decision.allowed else (decision.reason or "policy.denied")

    def _service_tool_policy_context(self, run_id: Optional[str], step_execution_id: Optional[str] = None) -> Dict[str, Any]:
        resolved_step_execution_id = self._resolve_step_execution_id(run_id, step_execution_id)
        if resolved_step_execution_id is not None:
            return dict(self.kernel.step_executions[resolved_step_execution_id].policy_context or {})

        if run_id is None:
            return {}
        run_record = self.run_store.get(run_id)
        if run_record is None:
            return {}
        workflow_execution = self.kernel.workflow_executions.get(run_record.get("execution_id"))
        if workflow_execution is None:
            return {}
        return dict(workflow_execution.policy_context or {})

    def _service_sandbox_profile(self, run_id: Optional[str], step_execution_id: Optional[str] = None) -> Optional[str]:
        if run_id is None:
            return None
        run_record = self.run_store.get(run_id)
        if run_record is None:
            return None
        context = run_record.get("context") or {}
        execution_context = context.get("execution_context") or {}
        if isinstance(execution_context, dict):
            sandbox_profile = execution_context.get("sandbox_profile")
            if isinstance(sandbox_profile, str) and sandbox_profile:
                return sandbox_profile
        return context.get("sandbox_profile")

    def _service_capability_requirements(self, run_id: Optional[str], step_execution_id: Optional[str] = None) -> List[str]:
        if run_id is None:
            return []
        run_record = self.run_store.get(run_id)
        if run_record is None:
            return []
        context = run_record.get("context") or {}
        capabilities = context.get("capability_requirements") or []
        if isinstance(capabilities, list):
            return [str(value) for value in capabilities if str(value)]
        return []

    @staticmethod
    def _normalize_capability_contract(capability_contract: Any) -> Dict[str, Any]:
        if not isinstance(capability_contract, dict):
            return {"allow_network": False}

        normalized: Dict[str, Any] = {"allow_network": False}
        if "allowed_commands" in capability_contract:
            normalized["allowed_commands"] = [str(item) for item in list(capability_contract.get("allowed_commands") or [])]
        if "allowed_roots" in capability_contract:
            normalized["allowed_roots"] = [str(item) for item in list(capability_contract.get("allowed_roots") or [])]
        if "allowed_env_keys" in capability_contract:
            normalized["allowed_env_keys"] = [str(item) for item in list(capability_contract.get("allowed_env_keys") or [])]
        if "allow_network" in capability_contract:
            normalized["allow_network"] = bool(capability_contract.get("allow_network", True))
        if "writable_paths" in capability_contract:
            normalized["writable_paths"] = [str(item) for item in list(capability_contract.get("writable_paths") or [])]
        return normalized

    def _service_capability_contract(self, run_id: Optional[str], step_execution_id: Optional[str] = None) -> Dict[str, Any]:
        if run_id is None:
            return self._normalize_capability_contract({})
        run_record = self.run_store.get(run_id)
        if run_record is None:
            return self._normalize_capability_contract({})
        context = run_record.get("context") or {}
        return self._normalize_capability_contract(context.get("capability_contract"))

    def _service_tool_capabilities(self, tool_id: str, action: str) -> List[str]:
        if tool_id == "filesystem":
            return ["workspace_inspection", "repo_editing"]
        if tool_id == "shell":
            return ["workspace_execution"]
        return []

    def _validate_tool_request(self, request: ToolRequest, contract: ToolContract) -> Optional[str]:
        if not request.parameters:
            if request.tool_id == "filesystem" and request.action == "read_file":
                return "Missing required path parameter"
            if request.tool_id == "shell" and request.action == "run":
                return "Missing required command parameter"

        if request.tool_id == "filesystem" and request.action == "read_file":
            path = request.parameters.get("path")
            if not isinstance(path, str) or not path.strip():
                return "Missing required path parameter"

        if request.tool_id == "shell" and request.action == "run":
            command = request.parameters.get("command")
            if not isinstance(command, str) or not command.strip():
                return "Missing required command parameter"

        return None

    def _service_tool_runtime_metadata(self, run_id: Optional[str], step_execution_id: Optional[str] = None) -> Dict[str, Any]:
        capability_contract = self._service_capability_contract(run_id, step_execution_id=step_execution_id)
        sandbox_profile = self._service_sandbox_profile(run_id, step_execution_id=step_execution_id)
        default_root = str(Path(self.data_dir or ".").expanduser().resolve())
        allowed_roots = [default_root]
        for raw_root in capability_contract.get("allowed_roots") or []:
            candidate = str(Path(str(raw_root)).expanduser().resolve())
            if candidate not in allowed_roots:
                allowed_roots.append(candidate)
        # Also include any declared writable_paths as allowed roots for destination resolution
        for raw_root in capability_contract.get("writable_paths") or []:
            candidate = str(Path(str(raw_root)).expanduser().resolve())
            if candidate not in allowed_roots:
                allowed_roots.append(candidate)
        return {
            "sandbox_profile": sandbox_profile,
            "capability_contract": capability_contract,
            "allowed_cwds": allowed_roots,
            "allowed_env_keys": list(capability_contract.get("allowed_env_keys") or ["PATH", "HOME", "LANG", "LC_ALL", "PYTHONPATH"]),
            "max_output_bytes": 1024 * 1024,
            "max_timeout_seconds": 30,
        }, allowed_roots

    def execute_tool_request(self, request: ToolRequest, max_retries: int = 1, run_id: Optional[str] = None, step_execution_id: Optional[str] = None) -> List[ToolExecutionEnvelope]:
        contract = self._resolve_tool_contract(request)
        if not contract.actions or request.action not in contract.actions:
            envelope = ToolExecutionEnvelope(request=request, status="error", output={}, error=f"Unsupported action {request.action!r} for tool {request.tool_id}")
            if run_id is not None:
                self._record_run_event(run_id, "TOOL_EXECUTION_FAILED", {"tool_id": request.tool_id, "action": request.action, "error": envelope.error, "attempt": 1})
            return [envelope]

        if run_id is not None:
            run_record = self.run_store.get(run_id)
            if run_record is not None and run_record.get("status") == "CANCELLED":
                envelope = ToolExecutionEnvelope(request=request, status="error", output={}, error="Tool execution cancelled because the run was cancelled")
                self._record_run_event(run_id, "TOOL_EXECUTION_FAILED", {"tool_id": request.tool_id, "action": request.action, "error": envelope.error, "attempt": 1})
                return [envelope]

        validation_error = self._validate_tool_request(request, contract)
        if validation_error is not None:
            envelope = ToolExecutionEnvelope(request=request, status="error", output={}, error=validation_error)
            if run_id is not None:
                self._record_run_event(run_id, "TOOL_EXECUTION_FAILED", {"tool_id": request.tool_id, "action": request.action, "error": envelope.error, "attempt": 1})
            return [envelope]

        budget_error = self._consume_run_tool_call_budget(run_id)
        if budget_error is not None:
            envelope = ToolExecutionEnvelope(request=request, status="error", output={}, error=budget_error)
            if run_id is not None:
                self._record_run_event(
                    run_id,
                    "TOOL_EXECUTION_BUDGET_DENIED",
                    {"tool_id": request.tool_id, "action": request.action, "reason": budget_error},
                )
            return [envelope]

        if run_id is not None:
            run_record = self.run_store.get(run_id)
            if run_record is not None and run_record.get("status") == "CANCELLED":
                envelope = ToolExecutionEnvelope(request=request, status="error", output={}, error="Tool execution cancelled because the run was cancelled")
                self._record_run_event(run_id, "TOOL_EXECUTION_FAILED", {"tool_id": request.tool_id, "action": request.action, "error": envelope.error, "attempt": 1})
                return [envelope]

        risk_level = self._classify_tool_risk(request, contract)
        policy_context = self._service_tool_policy_context(run_id, step_execution_id=step_execution_id)
        policy_error = self._evaluate_service_tool_policy(request, contract, risk_level, run_id, step_execution_id=step_execution_id)
        if policy_error is not None:
            envelope = ToolExecutionEnvelope(request=request, status="error", output={}, error=policy_error, metadata={"risk_level": risk_level})
            if run_id is not None:
                self._record_run_event(
                    run_id,
                    "TOOL_EXECUTION_POLICY_DENIED",
                    {
                        "tool_id": request.tool_id,
                        "action": request.action,
                        "risk_level": risk_level,
                        "reason": policy_error,
                        "policy_decision": {"allowed": False, "reason": policy_error},
                        "policy_context": policy_context,
                    },
                )
            return [envelope]
        request_fingerprint = self.kernel._tool_request_fingerprint(request.tool_id, request.action, request.parameters)
        approval_envelope = self._handle_tool_approval_gate(request, run_id=run_id, risk_level=risk_level, step_execution_id=step_execution_id)
        if approval_envelope is not None:
                if run_id is not None:
                    self._record_run_event(
                        run_id,
                        "TOOL_EXECUTION_PENDING_APPROVAL",
                        {
                            "tool_id": request.tool_id,
                            "action": request.action,
                            "approval_id": approval_envelope.metadata.get("approval_id"),
                            "risk_level": risk_level,
                            "policy_decision": {"allowed": False, "reason": "tool_approval_required"},
                            "policy_context": policy_context,
                        },
                    )
                return [approval_envelope]
        if risk_level in {"destructive", "privileged", "external_network"}:
            recorded_result = self._load_tool_execution_ledger_entry(run_id, request_fingerprint)
            if recorded_result is not None:
                if run_id is not None:
                    self._record_run_event(
                        run_id,
                        "TOOL_EXECUTION_REPLAYED",
                        {"tool_id": request.tool_id, "action": request.action, "request_fingerprint": request_fingerprint},
                    )
                return [recorded_result]
        attempts = 0
        last_result = None
        while attempts <= max_retries:
            attempts += 1
            if run_id is not None:
                self._record_run_event(
                    run_id,
                    "TOOL_EXECUTION_STARTED",
                    {
                        "tool_id": request.tool_id,
                        "action": request.action,
                        "attempt": attempts,
                        "parameters": request.parameters,
                        "risk_level": risk_level,
                        "policy_decision": {"allowed": True, "reason": None},
                        "policy_context": policy_context,
                    },
                )

            started_at = _now_iso()
            if run_id is not None:
                run_record = self.run_store.get(run_id)
                if run_record is not None and run_record.get("status") == "CANCELLED":
                    envelope = ToolExecutionEnvelope(request=request, status="error", output={}, error="Tool execution cancelled because the run was cancelled")
                    self._record_run_event(run_id, "TOOL_EXECUTION_FAILED", {"tool_id": request.tool_id, "action": request.action, "error": envelope.error, "attempt": attempts})
                    return [envelope]

            if request.tool_id == "filesystem":
                if run_id is not None:
                    run_record = self.run_store.get(run_id)
                    if run_record is not None and run_record.get("status") == "CANCELLED":
                        envelope = ToolExecutionEnvelope(request=request, status="error", output={}, error="Tool execution cancelled because the run was cancelled")
                        self._record_run_event(run_id, "TOOL_EXECUTION_FAILED", {"tool_id": request.tool_id, "action": request.action, "error": envelope.error, "attempt": attempts})
                        return [envelope]
                tool_metadata, allowed_roots = self._service_tool_runtime_metadata(run_id, step_execution_id=step_execution_id)
                tool = FilesystemTool(allowed_roots=allowed_roots, metadata=tool_metadata)
                result = tool.execute(request)
                metadata = {
                    "attempts": attempts,
                    "tool_id": request.tool_id,
                    "action": request.action,
                    "duration_ms": 0,
                    "classification": "success" if result.status == "ok" else "failure",
                    "risk_level": risk_level,
                }
                last_result = ToolExecutionEnvelope(
                    request=request,
                    status=result.status,
                    output=result.data,
                    error=result.error,
                    metadata=metadata,
                )
            elif request.tool_id == "shell":
                from engine.tool_runtime import ShellTool

                tool_metadata, allowed_roots = self._service_tool_runtime_metadata(run_id, step_execution_id=step_execution_id)
                capability_contract = tool_metadata.get("capability_contract") or {}
                tool = ShellTool(
                    allowed_commands=list(capability_contract.get("allowed_commands") or ["pytest", "python", "python3"]),
                    metadata=tool_metadata,
                )
                timeout_seconds = contract.timeout_seconds or 30
                timeout_value = request.parameters.get("timeout") if isinstance(request.parameters, dict) else None
                if isinstance(timeout_value, (int, float)) and timeout_value > 0:
                    timeout_seconds = max(1, int(timeout_value))
                params = dict(request.parameters or {})
                params["timeout"] = timeout_seconds
                request_for_execution = ToolRequest(tool_id=request.tool_id, action=request.action, parameters=params)
                result = tool.execute(request_for_execution)
                if result.status == "error" and result.error and "timed out" in result.error.lower():
                    timed_out = ToolExecutionEnvelope(request=request, status="error", output={}, error=result.error)
                    if run_id is not None:
                        self._record_run_event(run_id, "TOOL_EXECUTION_FAILED", {"tool_id": request.tool_id, "action": request.action, "error": timed_out.error, "attempt": attempts})
                    return [timed_out]
                metadata = {
                    "attempts": attempts,
                    "tool_id": request.tool_id,
                    "action": request.action,
                    "duration_ms": 0,
                    "classification": "success" if result.status == "ok" else "failure",
                    "risk_level": risk_level,
                }
                last_result = ToolExecutionEnvelope(
                    request=request,
                    status=result.status,
                    output=result.data,
                    error=result.error,
                    metadata=metadata,
                )
            else:
                envelope = ToolExecutionEnvelope(request=request, status="error", output={}, error="Unknown tool")
                if run_id is not None:
                    self._record_run_event(run_id, "TOOL_EXECUTION_FAILED", {"tool_id": request.tool_id, "action": request.action, "error": envelope.error, "attempt": attempts})
                return [envelope]

            if result.status == "ok":
                completed_metadata = {
                    **(last_result.metadata or {}),
                    "duration_ms": max(0, int((self._measure_elapsed_ms(started_at) or 0))),
                    "classification": "success",
                }
                last_result = ToolExecutionEnvelope(
                    request=last_result.request,
                    status=last_result.status,
                    output=last_result.output,
                    error=last_result.error,
                    metadata=completed_metadata,
                )
                if run_id is not None:
                    self._record_run_event(
                        run_id,
                        "TOOL_EXECUTION_COMPLETED",
                        {
                            "tool_id": request.tool_id,
                            "action": request.action,
                            "attempt": attempts,
                            "status": last_result.status,
                            "output": last_result.output,
                            "error": last_result.error,
                            "duration_ms": last_result.metadata.get("duration_ms"),
                            "classification": last_result.metadata.get("classification"),
                            "risk_level": last_result.metadata.get("risk_level"),
                            "policy_decision": {"allowed": True, "reason": None},
                            "policy_context": policy_context,
                        },
                    )
                    self._store_tool_execution_ledger_entry(run_id, request_fingerprint, last_result)
                return [last_result]

            if attempts <= max_retries and (result.error and "timeout" in result.error.lower()):
                retry_metadata = {
                    **(last_result.metadata or {}),
                    "duration_ms": max(0, int((self._measure_elapsed_ms(started_at) or 0))),
                    "classification": "retryable_failure",
                }
                last_result = ToolExecutionEnvelope(
                    request=last_result.request,
                    status=last_result.status,
                    output=last_result.output,
                    error=last_result.error,
                    metadata=retry_metadata,
                )
                if run_id is not None:
                    self._record_run_event(run_id, "TOOL_EXECUTION_RETRYING", {"tool_id": request.tool_id, "action": request.action, "attempt": attempts, "error": result.error, "duration_ms": last_result.metadata.get("duration_ms"), "classification": last_result.metadata.get("classification")})
                continue

            failed_metadata = {
                **(last_result.metadata or {}),
                "duration_ms": max(0, int((self._measure_elapsed_ms(started_at) or 0))),
                "classification": "validation_error" if result.status == "error" and result.error and "required" in result.error.lower() else "failure",
            }
            last_result = ToolExecutionEnvelope(
                request=last_result.request,
                status=last_result.status,
                output=last_result.output,
                error=last_result.error,
                metadata=failed_metadata,
            )
            if run_id is not None:
                self._record_run_event(run_id, "TOOL_EXECUTION_FAILED", {"tool_id": request.tool_id, "action": request.action, "attempt": attempts, "status": last_result.status, "output": last_result.output, "error": last_result.error, "duration_ms": last_result.metadata.get("duration_ms"), "classification": last_result.metadata.get("classification")})
                if risk_level in {"destructive", "privileged", "external_network"}:
                    self._store_tool_execution_ledger_entry(run_id, request_fingerprint, last_result)
            return [last_result]

        return [last_result or ToolExecutionEnvelope(request=request, status="error", output={}, error="Tool execution failed")]

    def _build_task_envelope(self, task: str, context: Optional[Dict[str, Any]] = None) -> TaskEnvelope:
        runtime_context = dict(context or {})
        allowed_tools = runtime_context.get("allowed_tools") or ["workspace_inspection", "artifact_creation", "repo_editing"]
        budget = self._build_task_budget(runtime_context)
        policy_context = self._normalize_policy_context(runtime_context, strict=bool(runtime_context.get("require_policy_context", False)))
        task_spec = self._build_repository_task_spec(task, runtime_context.get("workspace_path"), runtime_context, budget)
        return TaskEnvelope(
            objective={
                "description": task,
                "success_criteria": ["Produce a useful summary", "Capture verification status"],
            },
            constraints={
                "dry_run": bool(runtime_context.get("dry_run", False)),
                "max_iterations": int(runtime_context.get("max_iterations") or 1),
                "allowed_tools": list(allowed_tools),
                "workspace_path": runtime_context.get("workspace_path"),
            },
            acceptance_criteria=["A summary artifact is written", "Next steps are captured"],
            allowed_tools=list(allowed_tools),
            budget=budget,
            execution_context={
                "execution_mode": runtime_context.get("execution_mode", "autonomous"),
                "sandbox_profile": runtime_context.get("sandbox_profile", "standard"),
                "policy_profile": runtime_context.get("policy_profile", "default"),
            },
            policy_context=policy_context,
            capability_requirements=list(runtime_context.get("capability_requirements") or []),
            task_spec=task_spec,
        )

    def _build_repository_task_spec(self, task: str, workspace_path: Optional[str], context: Optional[Dict[str, Any]] = None, budget: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        runtime_context = dict(context or {})
        budget_payload = dict(budget or self._build_task_budget(runtime_context))
        expected_files = list(runtime_context.get("expected_files") or [])
        if not expected_files:
            workspace_files = runtime_context.get("workspace_files") or []
            expected_files = [str(item) for item in workspace_files if isinstance(item, str)][:10]
        if not expected_files and workspace_path:
            try:
                root = Path(workspace_path).expanduser().resolve()
                expected_files = [str(path.relative_to(root)) for path in sorted(root.rglob("*")) if path.is_file()][:10]
            except OSError:
                expected_files = []
        verification_command = runtime_context.get("verification_command")
        selected_files = list(runtime_context.get("selected_files") or expected_files)
        verification_expectations = {
            "required": bool(verification_command),
            "command": verification_command,
            "mode": runtime_context.get("verification_mode", "task_command"),
        }
        allowed_tools = runtime_context.get("allowed_tools") or ["workspace_inspection", "artifact_creation", "repo_editing"]
        capability_contract = runtime_context.get("capability_contract")
        normalized_contract = self._normalize_capability_contract(capability_contract)
        sandbox_profile = str(runtime_context.get("sandbox_profile") or "standard")
        sandbox_policy = {
            "profile": sandbox_profile,
            "enforced": sandbox_profile.lower() == "strict" or bool(normalized_contract.get("allowed_commands")) or bool(normalized_contract.get("allowed_roots")) or bool(normalized_contract.get("writable_paths")) or normalized_contract.get("allow_network") is False,
            "capability_contract": normalized_contract,
        }
        proposed_patch = runtime_context.get("proposed_patch")
        patch_preview = {
            "status": "not_ready",
            "reason": "patch proposal is generated only after inspection",
        }
        if isinstance(proposed_patch, dict):
            patch_path = proposed_patch.get("path")
            patch_content = proposed_patch.get("content")
            if isinstance(patch_path, str) and patch_path.strip() and isinstance(patch_content, str):
                patch_preview = {
                    "status": "ready",
                    "path": patch_path,
                    "content": patch_content,
                    "reason": "explicit patch proposal supplied in context",
                }
        plan_artifact = self._build_repository_plan(task, {
            "workspace_path": workspace_path,
            "files": expected_files,
            "directories": [],
            "marker_files": [],
        })
        return {
            "description": task,
            "workspace_path": workspace_path,
            "expected_files": expected_files,
            "selected_files": selected_files,
            "verification_command": verification_command,
            "verification_expectations": verification_expectations,
            "allowed_tools": list(allowed_tools),
            "constraints": {
                "dry_run": bool(runtime_context.get("dry_run", False)),
                "max_iterations": int(runtime_context.get("max_iterations") or budget_payload.get("limits", {}).get("max_iterations", 1)),
                "workspace_path": workspace_path,
            },
            "policy_context": self._normalize_policy_context(runtime_context, strict=bool(runtime_context.get("require_policy_context", False))),
            "budget": budget_payload,
            "stop_reason": runtime_context.get("stop_reason"),
            "capability_contract": normalized_contract,
            "sandbox_profile": sandbox_profile,
            "sandbox_policy": sandbox_policy,
            "patch_preview": patch_preview,
            "plan_artifact": plan_artifact,
        }

    @staticmethod
    def _build_task_budget(context: Dict[str, Any]) -> Dict[str, Any]:
        defaults = {
            "max_iterations": 1,
            "max_corrections": 1,
            "max_tool_calls": 20,
            "max_patch_files": 10,
            "max_patch_bytes": 100_000,
            "max_verification_attempts": 2,
            "max_wall_time_seconds": 300,
            "max_model_tokens": 4096,
            "max_output_bytes": 1_000_000,
        }
        aliases = {"max_patch_tokens": "max_model_tokens"}
        limits = {}
        for name, default in defaults.items():
            source_name = aliases.get(name, name)
            value = context.get(source_name, context.get(name, default))
            if name == "max_model_tokens" and "max_patch_tokens" in context and "max_model_tokens" not in context:
                value = context["max_patch_tokens"]
            try:
                value = int(value)
            except (TypeError, ValueError):
                value = default
            limits[name] = max(0, value)
        return {"limits": limits, "usage": {"iterations": 0, "corrections": 0, "tool_calls": 0, "patch_files": 0, "patch_bytes": 0, "verification_attempts": 0, "model_tokens": 0}}

    @staticmethod
    def _patch_budget_error(proposed_patch: Any, budget: Dict[str, Any]) -> Optional[str]:
        patches = proposed_patch if isinstance(proposed_patch, list) else ([proposed_patch] if isinstance(proposed_patch, dict) else [])
        patch_files = len(patches)
        patch_bytes = sum(len(str(patch.get("content", "")).encode("utf-8")) for patch in patches if isinstance(patch, dict))
        usage = budget.setdefault("usage", {})
        total_patch_files = int(usage.get("patch_files", 0)) + patch_files
        total_patch_bytes = int(usage.get("patch_bytes", 0)) + patch_bytes
        usage.update({"patch_files": total_patch_files, "patch_bytes": total_patch_bytes})
        limits = budget["limits"]
        if total_patch_files > limits["max_patch_files"] or total_patch_bytes > limits["max_patch_bytes"]:
            return "patch_budget_exceeded"
        return None

    def _normalize_policy_context(self, context: Optional[Dict[str, Any]], *, strict: bool = False) -> Dict[str, Any]:
        raw_context = (context or {}).get("policy_context")
        if raw_context is None:
            if strict:
                return {"approved": False, "policy_ids": [], "reason": "policy_context.missing"}
            return {"approved": True, "policy_ids": []}

        if not isinstance(raw_context, dict):
            return {"approved": False, "policy_ids": [], "reason": "policy_context.invalid"}

        normalized_context = dict(raw_context)
        if normalized_context.get("approved") is False:
            normalized_context.setdefault("policy_ids", [])
            normalized_context["reason"] = "policy_context.not_approved"
            return normalized_context

        if "policy_ids" not in normalized_context or normalized_context.get("policy_ids") is None:
            normalized_context["approved"] = False
            normalized_context["policy_ids"] = []
            normalized_context["reason"] = "policy_context.incomplete"
            return normalized_context

        normalized_context.setdefault("approved", True)
        normalized_context.setdefault("policy_ids", [])
        normalized_context["reason"] = None
        return normalized_context

    @staticmethod
    def _normalize_patch_proposal(proposal: Any, *, source: str = "context") -> Dict[str, Any]:
        if proposal is None:
            return {"status": "not_provided", "patches": [], "source": None}
        if isinstance(proposal, str):
            try:
                proposal = json.loads(proposal)
            except json.JSONDecodeError:
                return {"status": "invalid", "patches": [], "source": source, "reason": "planner_output_not_json"}
        patches = proposal if isinstance(proposal, list) else [proposal]
        if not patches or not all(isinstance(patch, dict) for patch in patches):
            return {"status": "invalid", "patches": [], "source": source, "reason": "proposal_not_object_or_list"}
        normalized = []
        for patch in patches:
            path = patch.get("path")
            content = patch.get("content")
            if not isinstance(path, str) or not path.strip() or not isinstance(content, str):
                return {"status": "invalid", "patches": [], "source": source, "reason": "patch_requires_path_and_content"}
            normalized.append({"path": path, "content": content})
        return {"status": "proposed", "patches": normalized, "source": source}

    @staticmethod
    def _patch_generation_prompt(task: str, workspace_summary: Dict[str, Any]) -> str:
        return (
            "Return only strict JSON for a Hermes patch proposal. The JSON must be either "
            "an object or an array of objects with string fields 'path' and 'content'. "
            "Use workspace-relative paths. Do not include Markdown fences or explanations.\n"
            f"Task: {task}\nWorkspace summary: {json.dumps(workspace_summary, sort_keys=True)}"
        )

    def _check_patch_path_capability(self, workspace_path: Optional[str], target_path: Path, capability_contract: Any) -> Optional[str]:
        normalized_contract = self._normalize_capability_contract(capability_contract)
        if not normalized_contract:
            return None

        allowed_roots = normalized_contract.get("allowed_roots") or []
        if isinstance(allowed_roots, list) and allowed_roots:
            resolved_allowed_roots = [Path(str(root)).expanduser().resolve() for root in allowed_roots if str(root)]
            if not any(target_path == root or root in target_path.parents for root in resolved_allowed_roots):
                return "capability_contract.path_not_allowed"

        if "writable_paths" in normalized_contract:
            writable_paths = normalized_contract.get("writable_paths") or []
            if isinstance(writable_paths, list):
                if not writable_paths:
                    return "capability_contract.path_not_writable"
                resolved_writable_paths = [Path(str(path)).expanduser().resolve() for path in writable_paths if str(path)]
                if not any(target_path == root or root in target_path.parents for root in resolved_writable_paths):
                    return "capability_contract.path_not_writable"

        return None

    @staticmethod
    def _patch_correction_prompt(task: str, verification: Dict[str, Any], workspace_summary: Dict[str, Any]) -> str:
        return (
            "Return only strict JSON for a Hermes corrective patch proposal. The JSON must be either "
            "an object or an array of objects with string fields 'path' and 'content'. "
            "Use workspace-relative paths. Do not include Markdown fences or explanations.\n"
            f"Task: {task}\nVerification failure: {json.dumps(verification, sort_keys=True)}\n"
            f"Workspace summary: {json.dumps(workspace_summary, sort_keys=True)}"
        )

    def _build_task_execution_envelope(
        self,
        task: str,
        task_status: str,
        workspace_summary: Dict[str, Any],
        task_envelope: TaskEnvelope,
        repository_plan: Dict[str, Any],
        execution_loop: Dict[str, Any],
        policy_context: Dict[str, Any],
        budget: Dict[str, Any],
        patch_result: Dict[str, Any],
        verification: Dict[str, Any],
        task_spec: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        sandbox_context = {
            "profile": task_envelope.execution_context.get("sandbox_profile") or "standard",
            "enforced": False,
            "capability_contract": {},
        }
        if task_spec:
            sandbox_context = {
                "profile": task_spec.get("sandbox_profile") or sandbox_context["profile"],
                "enforced": bool(task_spec.get("sandbox_policy", {}).get("enforced", False)),
                "capability_contract": dict(task_spec.get("capability_contract") or {}),
            }
        verification_expectations = {}
        if task_spec:
            verification_expectations = dict(task_spec.get("verification_expectations") or {})
        elif task_envelope.task_spec:
            verification_expectations = dict(task_envelope.task_spec.get("verification_expectations") or {})
        return {
            "schema_version": 1,
            "task": task,
            "task_status": task_status,
            "workspace_path": workspace_summary.get("workspace_path"),
            "phase": execution_loop.get("phase"),
            "iteration": execution_loop.get("iteration"),
            "max_iterations": execution_loop.get("max_iterations"),
            "verification_status": execution_loop.get("verification_status"),
            "stop_reason": execution_loop.get("stop_reason"),
            "next_action": execution_loop.get("next_action"),
            "repository_plan": repository_plan,
            "policy_context": policy_context,
            "budget": budget,
            "patch_result": patch_result,
            "verification": verification,
            "task_spec": task_spec or task_envelope.task_spec or {},
            "verification_expectations": verification_expectations,
            "task_transitions": [],
            "sandbox_context": sandbox_context,
        }

    def _build_preview_actions(self, task_envelope: TaskEnvelope) -> List[Dict[str, Any]]:
        allowed_tools = task_envelope.allowed_tools
        preview_actions = []
        if "workspace_inspection" in allowed_tools:
            preview_actions.append({"tool": "workspace_inspection", "action": "inspect_workspace", "status": "planned"})
        if "artifact_creation" in allowed_tools:
            preview_actions.append({"tool": "artifact_creation", "action": "write_summary_artifact", "status": "planned"})
        if "repo_editing" in allowed_tools:
            preview_actions.append({"tool": "repo_editing", "action": "apply_review_note", "status": "planned"})
        if not preview_actions:
            preview_actions.append({"tool": "noop", "action": "no_op", "status": "planned"})
        return preview_actions

    def _build_repository_plan(self, task: str, workspace_summary: Dict[str, Any]) -> Dict[str, Any]:
        files = [name for name in workspace_summary.get("files", []) if isinstance(name, str)]
        directories = [name for name in workspace_summary.get("directories", []) if isinstance(name, str)]
        candidate_files = [
            name for name in files
            if name.lower().endswith((".md", ".py", ".txt", ".json", ".toml", ".yml", ".yaml"))
        ][:10]

        plan_steps: List[str] = []
        if "README.md" in candidate_files:
            plan_steps.append("Review the README and any adjacent documentation before editing.")
        if any(name.endswith(".py") for name in candidate_files):
            plan_steps.append("Inspect the relevant Python modules to keep the change scoped and compatible.")
        if plan_steps:
            plan_steps.append("Prepare a minimal patch that satisfies the task and verify it with the repository's preferred checks.")
        else:
            plan_steps.append("Inspect the workspace contents and choose the smallest file change that satisfies the task.")

        preferred_target = None
        if "README.md" in candidate_files:
            preferred_target = "README.md"
        elif candidate_files:
            preferred_target = candidate_files[0]

        return {
            "workspace_path": workspace_summary.get("workspace_path"),
            "candidate_files": candidate_files,
            "preferred_target": preferred_target,
            "plan": plan_steps,
            "task": task,
            "directories": directories[:10],
        }

    @staticmethod
    def _workspace_snapshot(workspace_summary: Dict[str, Any]) -> Dict[str, Any]:
        workspace_path = Path(str(workspace_summary.get("workspace_path", "")))
        file_hashes: Dict[str, str] = {}
        for relative_path in workspace_summary.get("files", []):
            if relative_path == "task_checkpoints.json" or relative_path.endswith(".db"):
                continue
            target_path = workspace_path / relative_path
            try:
                file_hashes[relative_path] = hashlib.sha256(target_path.read_bytes()).hexdigest()
            except OSError:
                file_hashes[relative_path] = "missing"
        snapshot_payload = json.dumps(
            {"workspace_path": str(workspace_path), "file_hashes": file_hashes},
            sort_keys=True,
        ).encode("utf-8")
        return {
            "workspace_path": str(workspace_path),
            "file_hashes": file_hashes,
            "identity_hash": hashlib.sha256(snapshot_payload).hexdigest(),
        }

    @staticmethod
    def _extract_touched_paths(patch_result: Optional[Dict[str, Any]], workspace_path: Optional[str]) -> set[str]:
        if not isinstance(patch_result, dict):
            return set()
        workspace_root = Path(str(workspace_path or "")).expanduser().resolve()
        touched_paths: set[str] = set()
        if patch_result.get("reason") == "dry_run" and not patch_result.get("applied"):
            return touched_paths
        changes = patch_result.get("changes")
        if isinstance(changes, list):
            for change in changes:
                if isinstance(change, dict) and change.get("path"):
                    candidate_path = Path(str(change.get("path"))).expanduser()
                    try:
                        resolved_path = candidate_path.resolve()
                    except OSError:
                        resolved_path = candidate_path
                    if workspace_root and (resolved_path == workspace_root or workspace_root in resolved_path.parents):
                        try:
                            touched_paths.add(str(resolved_path.relative_to(workspace_root)))
                        except ValueError:
                            touched_paths.add(str(resolved_path))
                    else:
                        touched_paths.add(str(candidate_path))
        elif patch_result.get("path"):
            candidate_path = Path(str(patch_result.get("path"))).expanduser()
            try:
                resolved_path = candidate_path.resolve()
            except OSError:
                resolved_path = candidate_path
            if workspace_root and (resolved_path == workspace_root or workspace_root in resolved_path.parents):
                try:
                    touched_paths.add(str(resolved_path.relative_to(workspace_root)))
                except ValueError:
                    touched_paths.add(str(resolved_path))
            else:
                touched_paths.add(str(candidate_path))
        return touched_paths

    @staticmethod
    def _identity_hash(value: Any) -> str:
        serialized = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        return hashlib.sha256(serialized).hexdigest()

    @classmethod
    def _policy_identity_hash(cls, policy_context: Dict[str, Any]) -> str:
        identity_context = {
            "approved": bool(policy_context.get("approved", False)),
            "policy_ids": list(policy_context.get("policy_ids", []) or []),
        }
        return cls._identity_hash(identity_context)

    def _build_execution_loop_state(self, *, iteration: int, max_iterations: int, phase: str, verification_status: str, stop_reason: Optional[str] = None, next_action: Optional[str] = None) -> Dict[str, Any]:
        if stop_reason is None:
            if verification_status == "failed":
                stop_reason = "verification failed; retry once before stopping"
            elif verification_status == "not_run":
                stop_reason = "verification not run; mark task as unverified or add a repo-specific check"
            elif iteration >= max_iterations:
                stop_reason = "iteration budget reached"
            else:
                stop_reason = "continue"

        if next_action is None:
            if verification_status == "failed":
                next_action = "retry verification once and inspect the failure output"
            elif verification_status == "not_run":
                next_action = "record a verification gap and mark the task as not verified"
            elif iteration >= max_iterations:
                next_action = "inspect verification output and decide whether to retry or stop"
            else:
                next_action = "continue the bounded loop"

        return {
            "phase": phase,
            "status": "in_progress" if iteration < max_iterations else "completed",
            "iteration": iteration,
            "max_iterations": max_iterations,
            "verification_status": verification_status,
            "stop_reason": stop_reason,
            "next_action": next_action,
        }

    @staticmethod
    def _build_task_status_and_transitions(
        *,
        patch_result: Dict[str, Any],
        verification: Dict[str, Any],
        policy_blocked: bool,
        correction_count: int = 0,
    ) -> tuple[str, List[Dict[str, Any]]]:
        transitions: List[Dict[str, Any]] = []

        def add_transition(previous: str, current: str, reason: str) -> None:
            transitions.append({"from": previous, "to": current, "reason": reason, "timestamp": _now_iso()})

        add_transition("QUEUED", "PLANNING", "task accepted")
        if policy_blocked:
            add_transition("PLANNING", "BLOCKED_POLICY", patch_result.get("reason", "policy denied"))
            return "BLOCKED_POLICY", transitions

        budget_reasons = {
            "patch_budget_exceeded",
            "wall_time_budget_exceeded",
            "tool_call_budget_exceeded",
            "verification_output_exceeded",
        }
        if patch_result.get("reason") in budget_reasons or verification.get("details") in budget_reasons:
            add_transition("PLANNING", "PARTIAL", patch_result.get("reason") or verification.get("details", "budget exceeded"))
            return "PARTIAL", transitions

        if patch_result.get("applied"):
            add_transition("PLANNING", "APPLYING", "patch applied")
        verification_source = "APPLYING" if patch_result.get("applied") else "PLANNING"
        if correction_count:
            add_transition(verification_source, "CORRECTING", "verification correction attempted")
            verification_source = "CORRECTING"
        add_transition(verification_source, "VERIFYING", "verification evaluated")

        verification_status = verification.get("status")
        if verification_status == "failed":
            final_status = "FAILED_VERIFICATION"
        elif verification_status == "passed":
            final_status = "COMPLETED_VERIFIED"
        else:
            final_status = "COMPLETED_UNVERIFIED"
        add_transition("VERIFYING", final_status, verification_status or "not_run")
        return final_status, transitions

    def _preview_tool_execution(self, task_envelope: TaskEnvelope, workspace_summary: Dict[str, Any]) -> List[Dict[str, Any]]:
        if not task_envelope.constraints.get("dry_run", False):
            return []

        workspace_path = workspace_summary.get("workspace_path")
        preview_results: List[Dict[str, Any]] = []
        allowed_tools = task_envelope.allowed_tools
        if "workspace_inspection" in allowed_tools and workspace_path:
            tool = FilesystemTool(allowed_roots=[workspace_path])
            result = tool.execute(ToolRequest(tool_id="filesystem", action="list_directory", parameters={"path": workspace_path}))
            preview_results.append({
                "tool": "workspace_inspection",
                "action": "list_directory",
                "status": result.status,
                "result": result.to_dict(),
            })
        return preview_results

    def _apply_repo_edit(
        self,
        workspace_path: Optional[str],
        task: str,
        *,
        proposed_patch: Optional[Any] = None,
        dry_run: bool = False,
        capability_contract: Any = None,
    ) -> Dict[str, Any]:
        resolved_path = Path(workspace_path or self.data_dir or ".").expanduser().resolve()
        if not resolved_path.exists():
            return {"applied": False, "diff": "", "path": None, "reason": "workspace_missing"}

        if isinstance(proposed_patch, list):
            return self._apply_repo_patches(resolved_path, proposed_patch, dry_run=dry_run, capability_contract=capability_contract)

        if proposed_patch is not None:
            if not isinstance(proposed_patch, dict):
                return {"applied": False, "diff": "", "path": None, "reason": "invalid_patch"}
            relative_path = proposed_patch.get("path")
            new_text = proposed_patch.get("content")
            if not isinstance(relative_path, str) or not relative_path.strip() or not isinstance(new_text, str):
                return {"applied": False, "diff": "", "path": None, "reason": "invalid_patch"}
            target_path = (resolved_path / relative_path).resolve()
            if target_path != resolved_path and resolved_path not in target_path.parents:
                return {"applied": False, "diff": "", "path": str(target_path), "reason": "patch_outside_workspace"}
            if target_path.exists() and target_path.is_dir():
                return {"applied": False, "diff": "", "path": str(target_path), "reason": "target_is_directory"}
            capability_error = self._check_patch_path_capability(str(resolved_path), target_path, capability_contract)
            if capability_error is not None:
                return {"applied": False, "diff": "", "path": str(target_path), "reason": capability_error}
        else:
            target_path = resolved_path / "notes.txt"
            existing_text = target_path.read_text(encoding="utf-8") if target_path.exists() else ""
            new_text = existing_text.rstrip("\n")
            if new_text:
                new_text += "\n"
            new_text += f"review note: {task}\n"

        existing_text = target_path.read_text(encoding="utf-8") if target_path.exists() and target_path.is_file() else ""

        if existing_text == new_text:
            return {"applied": False, "diff": "", "path": str(target_path), "reason": "no_change", "change_id": str(uuid.uuid4())}

        previous_exists = target_path.exists()
        diff = "".join(
            difflib.unified_diff(
                existing_text.splitlines(keepends=True),
                new_text.splitlines(keepends=True),
                fromfile=str(target_path),
                tofile=str(target_path),
            )
        )
        before_hash = hashlib.sha256(existing_text.encode("utf-8")).hexdigest()
        after_hash = hashlib.sha256(new_text.encode("utf-8")).hexdigest()

        result = {
            "change_id": str(uuid.uuid4()),
            "applied": False,
            "diff": diff,
            "path": str(target_path),
            "previous_content": existing_text,
            "previous_exists": previous_exists,
            "before_hash": before_hash,
            "after_hash": after_hash,
            "reason": "dry_run" if dry_run else "planned",
        }
        if dry_run:
            return result

        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(new_text, encoding="utf-8")
        result.update({"applied": True, "reason": "edited", "summary": "Applied a proposed repository patch."})
        return result

    def _apply_repo_patches(self, workspace_path: Path, patches: List[Dict[str, Any]], *, dry_run: bool, capability_contract: Any = None) -> Dict[str, Any]:
        if not patches:
            return {"applied": False, "diff": "", "path": None, "reason": "invalid_patch"}

        changes: List[Dict[str, Any]] = []
        seen_paths = set()
        for patch in patches:
            if not isinstance(patch, dict):
                return {"applied": False, "diff": "", "path": None, "reason": "invalid_patch"}
            relative_path = patch.get("path")
            new_text = patch.get("content")
            if not isinstance(relative_path, str) or not relative_path.strip() or not isinstance(new_text, str):
                return {"applied": False, "diff": "", "path": None, "reason": "invalid_patch"}
            target_path = (workspace_path / relative_path).resolve()
            if target_path != workspace_path and workspace_path not in target_path.parents:
                return {"applied": False, "diff": "", "path": str(target_path), "reason": "patch_outside_workspace"}
            if target_path in seen_paths:
                return {"applied": False, "diff": "", "path": str(target_path), "reason": "duplicate_patch_path"}
            seen_paths.add(target_path)
            if target_path.exists() and target_path.is_dir():
                return {"applied": False, "diff": "", "path": str(target_path), "reason": "target_is_directory"}
            capability_error = self._check_patch_path_capability(str(workspace_path), target_path, capability_contract)
            if capability_error is not None:
                return {"applied": False, "diff": "", "path": str(target_path), "reason": capability_error}
            existing_text = target_path.read_text(encoding="utf-8") if target_path.exists() and target_path.is_file() else ""
            if existing_text == new_text:
                continue
            changes.append({
                "path": str(target_path),
                "previous_content": existing_text,
                "previous_exists": target_path.exists() and target_path.is_file(),
                "before_hash": hashlib.sha256(existing_text.encode("utf-8")).hexdigest(),
                "after_hash": hashlib.sha256(new_text.encode("utf-8")).hexdigest(),
                "content": new_text,
                "diff": "".join(difflib.unified_diff(
                    existing_text.splitlines(keepends=True),
                    new_text.splitlines(keepends=True),
                    fromfile=str(target_path),
                    tofile=str(target_path),
                )),
            })

        rollback_record = {
            "status": "prepared",
            "paths": [change["path"] for change in changes],
            "before_hashes": {change["path"]: change["before_hash"] for change in changes},
            "after_hashes": {change["path"]: change["after_hash"] for change in changes},
        }
        result = {
            "change_id": str(uuid.uuid4()),
            "applied": False,
            "changes": changes,
            "diff": "\n".join(change["diff"] for change in changes),
            "path": changes[0]["path"] if len(changes) == 1 else None,
            "reason": "dry_run" if dry_run else "planned",
            "rollback": rollback_record,
        }
        if dry_run or not changes:
            return result

        staged_changes: List[Dict[str, Any]] = []
        for change in changes:
            target_path = Path(change["path"])
            target_path.parent.mkdir(parents=True, exist_ok=True)
            staged_changes.append({
                **change,
                "target_path": target_path,
                "temp_path": target_path.with_suffix(target_path.suffix + ".tmp"),
            })

        try:
            for change in staged_changes:
                target_path = change["target_path"]
                temp_path = change["temp_path"]
                temp_path.write_text(change["content"], encoding="utf-8")
                os.replace(temp_path, target_path)
        except OSError as exc:
            for change in reversed(staged_changes):
                target_path = change["target_path"]
                change["temp_path"].unlink(missing_ok=True)
                if change.get("previous_exists"):
                    target_path.write_text(change.get("previous_content", ""), encoding="utf-8")
                else:
                    target_path.unlink(missing_ok=True)
            return {
                "applied": False,
                "changes": [],
                "diff": "",
                "path": None,
                "reason": "write_failed",
                "error": str(exc),
            }
        result.update({"applied": True, "reason": "edited", "summary": "Applied a multi-file repository patch."})
        return result

    def _load_checkpoint(self, checkpoint_id: Optional[str]) -> Optional[Dict[str, Any]]:
        if not checkpoint_id:
            return None
        checkpoint_path = Path(self.data_dir or ".").expanduser().resolve() / "task_checkpoints.json"
        if not checkpoint_path.exists():
            return None
        try:
            checkpoints = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        return checkpoints.get(checkpoint_id)

    def _save_checkpoint(self, checkpoint_id: str, payload: Dict[str, Any]) -> None:
        checkpoint_path = Path(self.data_dir or ".").expanduser().resolve() / "task_checkpoints.json"
        try:
            checkpoints = json.loads(checkpoint_path.read_text(encoding="utf-8")) if checkpoint_path.exists() else {}
        except (json.JSONDecodeError, OSError):
            checkpoints = {}
        checkpoints[checkpoint_id] = payload
        temporary_path = checkpoint_path.with_suffix(f".{checkpoint_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary_path.write_text(json.dumps(checkpoints, indent=2, sort_keys=True), encoding="utf-8")
            with temporary_path.open("rb") as handle:
                os.fsync(handle.fileno())
            os.replace(temporary_path, checkpoint_path)
        finally:
            temporary_path.unlink(missing_ok=True)

    def list_checkpoints(self) -> List[Dict[str, Any]]:
        checkpoint_path = Path(self.data_dir or ".").expanduser().resolve() / "task_checkpoints.json"
        if not checkpoint_path.exists():
            return []
        try:
            checkpoints = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        items = []
        for checkpoint_id, payload in checkpoints.items():
            if isinstance(payload, dict):
                items.append({"checkpoint_id": checkpoint_id, **payload})
        return sorted(items, key=lambda item: item.get("iteration", 0), reverse=True)

    def get_checkpoint(self, checkpoint_id: str) -> Optional[Dict[str, Any]]:
        checkpoint_path = Path(self.data_dir or ".").expanduser().resolve() / "task_checkpoints.json"
        if not checkpoint_path.exists():
            return None
        try:
            checkpoints = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        payload = checkpoints.get(checkpoint_id)
        if not isinstance(payload, dict):
            return None
        return {"checkpoint_id": checkpoint_id, **payload}

    def get_change_record(self, checkpoint_id: str) -> Optional[Dict[str, Any]]:
        checkpoint = self.get_checkpoint(checkpoint_id)
        if checkpoint is None:
            return None
        summary = checkpoint.get("summary") or {}
        patch_result = summary.get("patch_result") or {}
        return {
            "checkpoint_id": checkpoint_id,
            "change_id": patch_result.get("change_id"),
            "task": summary.get("task"),
            "patch_result": patch_result,
            "pre_verification": summary.get("pre_verification"),
            "verification": summary.get("verification"),
            "verification_report": summary.get("verification_report"),
            "rollback": checkpoint.get("rollback"),
        }

    def rollback_checkpoint(
        self,
        checkpoint_id: str,
        *,
        actor: str = "runtime-service",
        reason: str = "rollback requested",
    ) -> Dict[str, Any]:
        checkpoint = self.get_checkpoint(checkpoint_id)
        if checkpoint is None:
            return {"status": "NOT_FOUND", "checkpoint_id": checkpoint_id, "reason": "checkpoint_not_found"}

        summary = checkpoint.get("summary") or {}
        patch_result = dict(summary.get("patch_result") or {})
        if not patch_result.get("applied"):
            return {"status": "NO_CHANGE", "checkpoint_id": checkpoint_id, "reason": "no_applied_edit"}
        if patch_result.get("rolled_back"):
            return {"status": "NO_CHANGE", "checkpoint_id": checkpoint_id, "reason": "already_rolled_back"}

        workspace_path = Path(str((summary.get("workspace") or {}).get("workspace_path", ""))).expanduser().resolve()
        changes = patch_result.get("changes") or [patch_result]
        resolved_changes = []
        for change in changes:
            target_path = Path(str(change.get("path", ""))).expanduser().resolve()
            if not workspace_path or not str(workspace_path) or not (target_path == workspace_path or workspace_path in target_path.parents):
                return {"status": "BLOCKED", "checkpoint_id": checkpoint_id, "reason": "rollback_target_outside_workspace"}
            current_content = target_path.read_text(encoding="utf-8") if target_path.exists() else ""
            current_hash = hashlib.sha256(current_content.encode("utf-8")).hexdigest()
            expected_after_hash = change.get("after_hash")
            if expected_after_hash and current_hash != expected_after_hash:
                return {
                    "status": "CONFLICT",
                    "checkpoint_id": checkpoint_id,
                    "reason": "target_changed_after_patch",
                    "path": str(target_path),
                    "expected_after_hash": expected_after_hash,
                    "current_hash": current_hash,
                }
            resolved_changes.append((change, target_path))

        try:
            for change, target_path in resolved_changes:
                if change.get("previous_exists"):
                    target_path.write_text(str(change.get("previous_content", "")), encoding="utf-8")
                else:
                    target_path.unlink(missing_ok=True)
        except OSError as exc:
            return {"status": "FAILED", "checkpoint_id": checkpoint_id, "reason": str(exc)}

        rollback_record = {
            "status": "ROLLED_BACK",
            "actor": actor,
            "reason": reason,
            "paths": [str(target_path) for _, target_path in resolved_changes],
            "before_hashes": {str(target_path): change.get("after_hash") for change, target_path in resolved_changes},
            "after_hashes": {str(target_path): change.get("before_hash") for change, target_path in resolved_changes},
            "timestamp": _now_iso(),
        }
        if len(resolved_changes) == 1:
            rollback_record["path"] = rollback_record["paths"][0]
            rollback_record["before_hash"] = next(iter(rollback_record["before_hashes"].values()))
            rollback_record["after_hash"] = next(iter(rollback_record["after_hashes"].values()))
        patch_result.update({"applied": False, "rolled_back": True, "reason": "rolled_back", "rollback": rollback_record})
        summary["patch_result"] = patch_result
        checkpoint["summary"] = summary
        checkpoint["rollback"] = rollback_record
        self._save_checkpoint(checkpoint_id, checkpoint)
        return {"status": "ROLLED_BACK", "checkpoint_id": checkpoint_id, **rollback_record}

    def remember(self, entry_type: str, content: str, topic: str, source: str, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self.memory_store.add(entry_type, content, topic, source, metadata=metadata)

    def recall(self, topic: Optional[str] = None, limit: int = 10) -> List[Dict[str, Any]]:
        return self.memory_store.list(topic=topic, limit=limit)

    def _select_relevant_memories(self, task: str, workspace_summary: Dict[str, Any], limit: int = 3) -> List[Dict[str, Any]]:
        topic = workspace_summary.get("workspace_path")
        memories = self.recall(topic=topic, limit=max(limit * 4, 8))
        if not memories:
            memories = self.recall(limit=max(limit * 4, 8))

        task_terms = {term.lower() for term in re.findall(r"[a-zA-Z0-9_]+", task)}
        scored_memories: List[tuple[float, Dict[str, Any]]] = []
        for memory in memories:
            content = str(memory.get("content", "")).lower()
            metadata = memory.get("metadata") or {}
            text = f"{content} {' '.join(str(value).lower() for value in metadata.values())}"
            overlap = sum(1 for term in task_terms if term and term in text)
            if overlap == 0:
                continue
            recency_bonus = max(0.0, 1.0 - (len(scored_memories) / max(len(memories), 1)))
            confidence_bonus = float(metadata.get("confidence", 0.0) or 0.0)
            outcome_bonus = 0.0
            outcome = str(metadata.get("outcome") or "").lower()
            if outcome == "helpful":
                outcome_bonus = 1.5
            elif outcome == "neutral":
                outcome_bonus = 0.25
            elif outcome == "harmful":
                outcome_bonus = -0.75
            usage_bonus = min(1.0, float(metadata.get("usage_count", 0) or 0) / 5.0)
            score = overlap + (1 if topic and memory.get("topic") == topic else 0) + recency_bonus + confidence_bonus + outcome_bonus + usage_bonus
            scored_memories.append((score, memory))

        scored_memories.sort(key=lambda item: item[0], reverse=True)
        ranked_memories = [memory for _, memory in scored_memories[:limit]]
        if ranked_memories:
            return ranked_memories
        return memories[:limit]

    @staticmethod
    def _derive_memory_influence(task: str, memories: List[Dict[str, Any]], verification: Dict[str, Any]) -> Dict[str, Any]:
        del task
        memory_text = " ".join(str(memory.get("content", "")).lower() for memory in memories)
        if any(term in memory_text for term in ("pytest", "verification", "verify", "test")):
            return {
                "strategy": "repository_verification",
                "next_action": "Use pytest or the repository verification command before retrying or declaring success.",
                "verification_status": verification.get("status"),
                "source_count": len(memories),
            }
        if memories:
            return {
                "strategy": "memory_guided_review",
                "next_action": "Review the selected repository memories before choosing the next bounded task action.",
                "verification_status": verification.get("status"),
                "source_count": len(memories),
            }
        return {
            "strategy": "default",
            "next_action": "Follow the repository plan and verification result.",
            "verification_status": verification.get("status"),
            "source_count": 0,
        }

    @staticmethod
    def _memory_feedback_outcome(verification: Dict[str, Any]) -> str:
        status = str(verification.get("status") or "unknown").lower()
        if status == "passed":
            return "helpful"
        if status == "failed":
            return "harmful"
        return "neutral"

    def _mark_relevant_memories_used(self, memories: List[Dict[str, Any]], verification: Dict[str, Any]) -> List[Dict[str, Any]]:
        outcome = self._memory_feedback_outcome(verification)
        updated: List[Dict[str, Any]] = []
        for memory in memories:
            memory_id = memory.get("memory_id")
            if not memory_id:
                updated.append(memory)
                continue
            refreshed = self.memory_store.record_usage(memory_id, outcome=outcome)
            if refreshed is not None:
                updated.append(refreshed)
            else:
                updated.append(memory)
        return updated

    @staticmethod
    def _annotate_memory_advisory(memories: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        annotated: List[Dict[str, Any]] = []
        for memory in memories:
            payload = dict(memory)
            metadata = dict(payload.get("metadata") or {})
            metadata.setdefault("advisory", True)
            metadata.setdefault("advisory_scope", "task_guidance")
            metadata.setdefault("provenance", memory.get("source", "memory_store"))
            metadata.setdefault("confidence", 0.5)
            metadata.setdefault("expires_at", None)
            metadata.setdefault("task_type", "repository_task")
            payload["metadata"] = metadata
            annotated.append(payload)
        return annotated

    def _build_resume_conflict_result(
        self,
        checkpoint_id: str,
        checkpoint: Dict[str, Any],
        workspace_summary: Dict[str, Any],
        workspace_snapshot: Dict[str, Any],
        policy_context: Dict[str, Any],
        task_status: str,
        stop_reason: str,
    ) -> Dict[str, Any]:
        conflict_summary = {
            "task": checkpoint.get("summary", {}).get("task"),
            "workspace": workspace_summary,
            "status_schema_version": 1,
            "task_status": task_status,
            "stop_reason": stop_reason,
            "workspace_snapshot": workspace_snapshot,
            "previous_workspace_snapshot": checkpoint.get("workspace_snapshot"),
            "policy_context": policy_context,
            "budget": checkpoint.get("summary", {}).get("budget", self._build_task_budget({})),
            "patch_result": {"applied": False, "diff": "", "path": None, "reason": "resume_conflict"},
            "verification": {"status": "not_run", "details": stop_reason},
            "task_transitions": [{"from": "QUEUED", "to": task_status, "reason": stop_reason, "timestamp": _now_iso()}],
        }
        artifact = Artifact.create(
            artifact_type="task_summary",
            title="task_summary",
            content=conflict_summary,
            created_by="runtime_service",
            inputs=[],
            parent_version=None,
            status="CREATED",
            decision_record={"reason": "resume_conflict"},
            metadata={"task_type": "repository_task", "workspace_path": workspace_summary.get("workspace_path")},
        )
        self.kernel.artifact_store.add(artifact)
        checkpoint["summary"] = conflict_summary
        checkpoint["workspace_snapshot"] = workspace_snapshot
        checkpoint["artifact_id"] = artifact.artifact_id
        self._save_checkpoint(checkpoint_id, checkpoint)
        return {
            "status": "CONFLICT",
            "task_status": task_status,
            "status_schema_version": 1,
            "summary": conflict_summary,
            "verification": conflict_summary["verification"],
            "artifacts": [artifact.artifact_id],
            "workspace": workspace_summary.get("workspace_path"),
            "checkpoint_id": checkpoint_id,
            "checkpoint": checkpoint,
            "dry_run": True,
        }

    def run_task(
        self,
        task: str,
        *,
        workspace_path: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        context = dict(context or {})
        task_started_at = time.monotonic()
        workspace_summary = self._inspect_workspace(workspace_path, context)
        workspace_snapshot = self._workspace_snapshot(workspace_summary)
        budget = self._build_task_budget(context)
        checkpoint_id = context.get("resume_from")
        checkpoint = self._load_checkpoint(checkpoint_id) if checkpoint_id else None

        policy_context_for_identity = context
        if checkpoint and "policy_context" not in context:
            persisted_policy_context = (checkpoint.get("summary") or {}).get("policy_context")
            if isinstance(persisted_policy_context, dict):
                policy_context_for_identity = {**context, "policy_context": persisted_policy_context}
        requested_policy_context = self._normalize_policy_context(
            policy_context_for_identity,
            strict=bool(context.get("require_policy_context")),
        )
        task_identity_hash = self._identity_hash(task)
        policy_identity_hash = self._policy_identity_hash(requested_policy_context)

        if checkpoint and int(checkpoint.get("checkpoint_schema_version", 1)) > 1:
            return self._build_resume_conflict_result(
                checkpoint_id,
                checkpoint,
                workspace_summary,
                workspace_snapshot,
                requested_policy_context,
                "CHECKPOINT_CONFLICT",
                "unsupported_checkpoint_schema",
            )

        if checkpoint and checkpoint.get("workspace_snapshot"):
            previous_snapshot = checkpoint["workspace_snapshot"]
            touched_paths = self._extract_touched_paths(
                checkpoint.get("summary", {}).get("patch_result") if isinstance(checkpoint.get("summary"), dict) else None,
                workspace_summary.get("workspace_path"),
            )
            if previous_snapshot.get("identity_hash") != workspace_snapshot.get("identity_hash"):
                current_file_hashes = workspace_snapshot.get("file_hashes", {}) if isinstance(workspace_snapshot, dict) else {}
                previous_file_hashes = previous_snapshot.get("file_hashes", {}) if isinstance(previous_snapshot, dict) else {}
                if touched_paths:
                    conflict = False
                    for path in touched_paths:
                        if current_file_hashes.get(path) != previous_file_hashes.get(path):
                            conflict = True
                            break
                else:
                    conflict = True
                if conflict:
                    conflict_summary = {
                        "task": task,
                        "workspace": workspace_summary,
                        "status_schema_version": 1,
                        "task_status": "WORKSPACE_CONFLICT",
                        "stop_reason": "workspace_changed_since_checkpoint",
                        "workspace_snapshot": workspace_snapshot,
                        "previous_workspace_snapshot": previous_snapshot,
                        "policy_context": self._normalize_policy_context(context, strict=bool(context.get("require_policy_context"))),
                        "budget": budget,
                        "patch_result": {"applied": False, "diff": "", "path": None, "reason": "workspace_conflict"},
                        "verification": {"status": "not_run", "details": "Workspace changed since checkpoint"},
                        "task_transitions": [
                            {"from": "QUEUED", "to": "WORKSPACE_CONFLICT", "reason": "workspace_changed_since_checkpoint", "timestamp": _now_iso()},
                        ],
                        "stop_reason": "workspace_changed_since_checkpoint",
                    }
                    artifact = Artifact.create(
                        artifact_type="task_summary",
                        title="task_summary",
                        content=conflict_summary,
                        created_by="runtime_service",
                        inputs=[],
                        parent_version=None,
                        status="CREATED",
                        decision_record={"reason": "workspace_conflict"},
                        metadata={"task_type": "repository_task", "workspace_path": workspace_summary.get("workspace_path")},
                    )
                    self.kernel.artifact_store.add(artifact)
                    checkpoint["summary"] = conflict_summary
                    checkpoint["workspace_snapshot"] = workspace_snapshot
                    checkpoint["artifact_id"] = artifact.artifact_id
                    self._save_checkpoint(checkpoint_id, checkpoint)
                    return {
                        "status": "CONFLICT",
                        "summary": conflict_summary,
                        "verification": conflict_summary["verification"],
                        "artifacts": [artifact.artifact_id],
                        "workspace": workspace_summary.get("workspace_path"),
                        "checkpoint_id": checkpoint_id,
                        "checkpoint": checkpoint,
                        "dry_run": bool(context.get("dry_run", False)),
                    }
        if checkpoint and checkpoint.get("task_identity_hash") and checkpoint["task_identity_hash"] != task_identity_hash:
            return self._build_resume_conflict_result(
                checkpoint_id,
                checkpoint,
                workspace_summary,
                workspace_snapshot,
                requested_policy_context,
                "TASK_CONFLICT",
                "task_changed_since_checkpoint",
            )
        if checkpoint and checkpoint.get("policy_identity_hash") and checkpoint["policy_identity_hash"] != policy_identity_hash:
            return self._build_resume_conflict_result(
                checkpoint_id,
                checkpoint,
                workspace_summary,
                workspace_snapshot,
                requested_policy_context,
                "POLICY_CONFLICT",
                "policy_context_changed_since_checkpoint",
            )

        history = list(checkpoint.get("history", []) if checkpoint else [])
        iteration = int(checkpoint.get("iteration", 0) if checkpoint else 0)
        max_iterations = budget["limits"]["max_iterations"] or 1

        iteration += 1
        budget["usage"]["iterations"] = iteration
        history.append({
            "iteration": iteration,
            "task": task,
            "workspace_path": workspace_summary.get("workspace_path"),
        })

        explicit_verification_command, verification_command_error = self._resolve_explicit_verification_command(
            workspace_path,
            context.get("verification_command"),
            capability_contract=context.get("capability_contract"),
        ) if context.get("verification_command") is not None else (None, None)
        if verification_command_error:
            pre_verification = {
                "status": "failed",
                "details": verification_command_error,
                "command": context.get("verification_command"),
                "stdout": "",
                "stderr": "",
                "return_code": None,
            }
        else:
            pre_verification = self._run_verification(
                workspace_path,
                explicit_verification_command,
                budget["limits"]["max_output_bytes"],
                budget["limits"]["max_wall_time_seconds"],
                capability_contract=context.get("capability_contract"),
            )
        verification = pre_verification
        dry_run = bool(context.get("dry_run", False))
        strict_policy = bool(context.get("require_policy_context", False))
        policy_context_input = context
        if checkpoint and "policy_context" not in context:
            persisted_policy_context = (checkpoint.get("summary") or {}).get("policy_context")
            if isinstance(persisted_policy_context, dict):
                policy_context_input = {**context, "policy_context": persisted_policy_context}
        policy_context = self._normalize_policy_context(policy_context_input, strict=strict_policy)
        provider_routing = self._route_provider(
            task,
            provider=context.get("provider", "stub"),
            model_name=context.get("model_name"),
            endpoint=context.get("endpoint"),
        )
        provider_routing_summary = {
            "provider": provider_routing.provider,
            "fallback_used": provider_routing.fallback_used,
            "reason": provider_routing.reason,
            "task_complexity": "simple",
            "health_summary": getattr(provider_routing, "health_summary", {}),
        }
        explicit_proposal = context.get("patch_proposal", context.get("proposed_patch"))
        proposal_source = "context"
        planner_output = context.get("planner_output")
        if explicit_proposal is None and planner_output is None and context.get("generate_patch"):
            try:
                planner_output = provider_routing.adapter.generate(
                    self._patch_generation_prompt(task, workspace_summary),
                    max_tokens=budget["limits"]["max_model_tokens"],
                )
            except Exception as exc:
                planner_output = None
                context["planner_error"] = str(exc)
        if explicit_proposal is None and planner_output is not None:
            explicit_proposal = planner_output
            proposal_source = "planner_output"
        patch_proposal = self._normalize_patch_proposal(explicit_proposal, source=proposal_source)
        original_proposal = explicit_proposal
        if patch_proposal["status"] == "proposed":
            proposed_patch = patch_proposal["patches"][0] if isinstance(original_proposal, dict) else patch_proposal["patches"]
        else:
            proposed_patch = None

        repository_plan = self._build_repository_plan(task, workspace_summary)
        patch_budget_error = self._patch_budget_error(proposed_patch, budget) if patch_proposal["status"] == "proposed" else None
        wall_time_budget_error = None
        elapsed_seconds = time.monotonic() - task_started_at
        if budget["limits"]["max_wall_time_seconds"] <= 0 or elapsed_seconds >= budget["limits"]["max_wall_time_seconds"]:
            wall_time_budget_error = "wall_time_budget_exceeded"

        policy_blocked = strict_policy and not policy_context.get("approved", True)
        if wall_time_budget_error:
            patch_result = {
                "applied": False,
                "diff": "",
                "path": None,
                "reason": wall_time_budget_error,
            }
        elif patch_budget_error:
            patch_result = {
                "applied": False,
                "diff": "",
                "path": None,
                "reason": patch_budget_error,
            }
        elif policy_blocked:
            patch_result = {
                "applied": False,
                "diff": "",
                "path": None,
                "reason": policy_context.get("reason", "policy_context.missing"),
            }
        else:
            patch_result = self._apply_repo_edit(
                workspace_summary.get("workspace_path"),
                task,
                proposed_patch=proposed_patch,
                dry_run=dry_run,
                capability_contract=context.get("capability_contract"),
            )
        if patch_proposal["status"] == "invalid":
            patch_result = {"applied": False, "diff": "", "path": None, "reason": "invalid_patch_proposal"}
        if not dry_run and patch_result.get("applied") and verification_command_error is None:
            verification = self._run_verification(
                workspace_path,
                explicit_verification_command,
                budget["limits"]["max_output_bytes"],
                budget["limits"]["max_wall_time_seconds"],
                capability_contract=context.get("capability_contract"),
            )
        corrections: List[Dict[str, Any]] = []
        max_corrections = budget["limits"].get("max_corrections")
        if max_corrections is None:
            max_corrections = 1
        else:
            max_corrections = max(0, int(max_corrections))
        correction_attempt = 0
        while (
            not dry_run
            and verification.get("status") == "failed"
            and context.get("generate_patch")
            and correction_attempt < max_corrections
        ):
            correction_attempt += 1
            correction_record: Dict[str, Any] = {"attempt": correction_attempt}
            try:
                correction_output = provider_routing.adapter.generate(
                    self._patch_correction_prompt(task, verification, workspace_summary),
                    max_tokens=budget["limits"]["max_model_tokens"],
                )
                correction_proposal = self._normalize_patch_proposal(correction_output, source="correction_output")
                correction_record["proposal"] = correction_proposal
                if correction_proposal["status"] == "proposed":
                    correction_patch = correction_proposal["patches"][0] if isinstance(correction_output, dict) else correction_proposal["patches"]
                    correction_budget_error = self._patch_budget_error(correction_patch, budget)
                    if correction_budget_error:
                        correction_result = {
                            "applied": False,
                            "diff": "",
                            "path": None,
                            "reason": correction_budget_error,
                        }
                    else:
                        correction_result = self._apply_repo_edit(
                            workspace_summary.get("workspace_path"),
                            task,
                            proposed_patch=correction_patch,
                            dry_run=False,
                            capability_contract=context.get("capability_contract"),
                        )
                    correction_record["patch_result"] = correction_result
                    if correction_result.get("reason") == "patch_budget_exceeded":
                        patch_result = correction_result
                    if correction_result.get("applied"):
                        verification = self._run_verification(
                            workspace_path,
                            explicit_verification_command,
                            budget["limits"]["max_output_bytes"],
                            budget["limits"]["max_wall_time_seconds"],
                            capability_contract=context.get("capability_contract"),
                        )
                else:
                    correction_record["status"] = "not_applied"
            except Exception as exc:
                correction_record["error"] = str(exc)
            corrections.append(correction_record)
        recovery = {"attempts": 1, "retried": False}
        if (
            verification["status"] == "failed"
            and recovery["attempts"] < budget["limits"]["max_verification_attempts"]
            and verification.get("details") not in {
            "verification_command_invalid",
            "verification_command_outside_workspace",
            "verification_command_not_allowed",
            }
        ):
            retry_verification = self._run_verification(
                workspace_path,
                explicit_verification_command,
                budget["limits"]["max_output_bytes"],
                budget["limits"]["max_wall_time_seconds"],
                capability_contract=context.get("capability_contract"),
            )
            verification = retry_verification
            recovery = {"attempts": 2, "retried": True}

        task_status, task_transitions = self._build_task_status_and_transitions(
            patch_result=patch_result,
            verification=verification,
            policy_blocked=policy_blocked,
            correction_count=len(corrections),
        )

        task_envelope = self._build_task_envelope(task, context={**context, "workspace_path": workspace_summary.get("workspace_path")})
        task_envelope.budget = budget
        preview_actions = self._build_preview_actions(task_envelope)
        execution_phase = "plan"
        preview_tool_results = self._preview_tool_execution(task_envelope, workspace_summary)
        budget["usage"]["tool_calls"] = len(preview_tool_results)
        budget["usage"]["wall_time_seconds"] = round(time.monotonic() - task_started_at, 3)
        next_steps = self._derive_next_steps(workspace_summary)
        if repository_plan.get("preferred_target"):
            next_steps.append(f"Review the planned target {repository_plan['preferred_target']} before finalizing the patch.")
        relevant_memories = self._select_relevant_memories(task, workspace_summary, limit=3)
        relevant_memories = self._mark_relevant_memories_used(relevant_memories, verification)
        relevant_memories = self._annotate_memory_advisory(relevant_memories)
        memory_influence = self._derive_memory_influence(task, relevant_memories, verification)
        if memory_influence["strategy"] != "default":
            next_steps.append(f"Memory-informed next action: {memory_influence['next_action']}")
        progress_artifact = {
            "iteration": iteration,
            "status": "IN_PROGRESS" if iteration < max_iterations else "COMPLETED",
            "verification_status": verification["status"],
            "next_steps": next_steps,
        }
        review_plan = {
            "status": "planned",
            "summary": "Inspect the workspace, capture the current state, and prepare a compact implementation plan.",
            "steps": [
                "Review the repository layout and any existing task context.",
                "Identify the smallest actionable change that satisfies the task.",
                "Record verification expectations and any blockers.",
            ],
        }
        task_spec = self._build_repository_task_spec(task, workspace_summary.get("workspace_path"), context={**context, "workspace_files": workspace_summary.get("files", [])}, budget=budget)
        has_explicit_patch = patch_proposal.get("status") == "proposed" or bool(context.get("proposed_patch") or context.get("patch_proposal"))
        if patch_result.get("applied") and has_explicit_patch:
            execution_phase = "apply"
        elif patch_result.get("reason") in {"patch_budget_exceeded", "wall_time_budget_exceeded", "tool_call_budget_exceeded"}:
            execution_phase = "blocked"
        elif verification.get("status") == "failed" or len(corrections) > 0:
            execution_phase = "correct"
        elif verification.get("status") in {"passed", "failed"}:
            execution_phase = "verify"
        elif context.get("resume_from") and iteration > 1:
            execution_phase = "verify"
        else:
            execution_phase = "plan"
        stop_reason = None
        next_action = None
        execution_loop = self._build_execution_loop_state(
            iteration=iteration,
            max_iterations=max_iterations,
            phase=execution_phase,
            verification_status=verification["status"],
            stop_reason=stop_reason,
            next_action=next_action,
        )
        task_spec["stop_reason"] = execution_loop["stop_reason"]
        task_envelope.task_spec = task_spec
        verification_report = {
            "status": verification["status"],
            "attempts": recovery.get("attempts", 1),
            "retried": recovery.get("retried", False),
            "details": verification.get("details", ""),
            "command": verification.get("command"),
            "return_code": verification.get("return_code"),
        }
        memory_context = {
            "retrieval_topic": workspace_summary.get("workspace_path"),
            "retrieved_count": len(relevant_memories),
            "memories": relevant_memories,
            "influence": memory_influence,
        }
        budget["usage"]["corrections"] = len(corrections)
        budget["usage"]["verification_attempts"] = recovery.get("attempts", 1)
        task_execution = self._build_task_execution_envelope(
            task,
            task_status,
            workspace_summary,
            task_envelope,
            repository_plan,
            execution_loop,
            policy_context,
            budget,
            patch_result,
            verification,
            task_spec=task_spec,
        )
        task_execution["task_transitions"] = task_transitions
        summary_payload = {
            "task": task,
            "task_spec": task_spec,
            "sandbox_context": task_spec.get("sandbox_policy") or {},
            "workspace": workspace_summary,
            "status_schema_version": 1,
            "task_status": task_status,
            "task_transitions": task_transitions,
            "policy_context": policy_context,
            "budget": budget,
            "patch_proposal": patch_proposal,
            "corrections": corrections,
            "pre_verification": pre_verification,
            "verification": verification,
            "recovery": recovery,
            "notes": "Completed a lightweight task-planning pass for the requested work.",
            "next_steps": next_steps,
            "review_plan": review_plan,
            "repository_plan": repository_plan,
            "execution_loop": execution_loop,
            "provider_routing": provider_routing_summary,
            "memory_context": memory_context,
            "patch_result": patch_result,
            "verification_report": verification_report,
            "task_execution": task_execution,
            "task_envelope": task_envelope.to_dict(),
            "preview_actions": preview_actions,
            "tool_results": preview_tool_results,
            "progress_artifacts": [progress_artifact],
            "relevant_memories": relevant_memories,
        }
        artifact = Artifact.create(
            artifact_type="task_summary",
            title="task_summary",
            content=summary_payload,
            created_by="runtime_service",
            inputs=[],
            parent_version=None,
            status="CREATED",
            decision_record={"reason": "generated_by_run_task"},
            metadata={"task_type": "planning", "workspace_path": workspace_summary.get("workspace_path")},
        )
        self.kernel.artifact_store.add(artifact)
        self.remember(
            entry_type="task_summary",
            content=summary_payload["notes"],
            topic=workspace_summary.get("workspace_path") or "default",
            source="runtime_service",
            metadata={
                "task": task,
                "verification_status": verification["status"],
                "artifact_id": artifact.artifact_id,
                "provenance": "runtime_service",
                "confidence": 0.7,
                "expires_at": None,
                "outcome": self._memory_feedback_outcome(verification),
                "task_type": "repository_task",
            },
        )

        checkpoint_id = checkpoint_id or str(uuid.uuid4())
        workspace_snapshot = self._workspace_snapshot(self._inspect_workspace(workspace_path, context))
        progress_summary = {
            "phase": execution_loop["phase"],
            "status": execution_loop["status"],
            "task_status": task_status,
            "iteration": execution_loop["iteration"],
            "max_iterations": execution_loop["max_iterations"],
            "verification_status": execution_loop["verification_status"],
            "stop_reason": execution_loop["stop_reason"],
            "next_action": execution_loop["next_action"],
            "task": task,
            "artifact_id": artifact.artifact_id,
        }
        touched_paths = self._extract_touched_paths(summary_payload.get("patch_result"), workspace_summary.get("workspace_path"))
        touched_file_hashes = {
            path: workspace_snapshot.get("file_hashes", {}).get(path, "missing")
            for path in sorted(touched_paths)
        }
        checkpoint_payload = {
            "checkpoint_id": checkpoint_id,
            "checkpoint_schema_version": 1,
            "iteration": iteration,
            "task_identity_hash": task_identity_hash,
            "policy_identity_hash": policy_identity_hash,
            "history": history,
            "artifact_id": artifact.artifact_id,
            "summary": summary_payload,
            "progress_summary": progress_summary,
            "provider_routing": summary_payload.get("provider_routing"),
            "execution_summary": {
                "status": "IN_PROGRESS" if iteration < max_iterations else "COMPLETED",
                "last_action": task,
                "last_verification": verification["status"],
                "step_count": len(history),
            },
            "workspace_snapshot": workspace_snapshot,
            "workspace_identity": {
                "workspace_path": workspace_summary.get("workspace_path"),
                "identity_hash": workspace_snapshot.get("identity_hash"),
                "touched_file_hashes": touched_file_hashes,
            },
        }
        self._save_checkpoint(checkpoint_id, checkpoint_payload)

        if verification["status"] == "failed":
            return {
                "status": "COMPLETED",
                "task_status": task_status,
                "status_schema_version": 1,
                "summary": summary_payload,
                "verification": verification,
                "artifacts": [artifact.artifact_id],
                "workspace": workspace_summary.get("workspace_path"),
                "checkpoint_id": checkpoint_id,
                "checkpoint": checkpoint_payload,
                "dry_run": bool(task_envelope.constraints.get("dry_run", False)),
            }

        if iteration < max_iterations and not patch_result.get("applied", False) and verification["status"] == "not_run":
            return {
                "status": "IN_PROGRESS",
                "task_status": task_status,
                "status_schema_version": 1,
                "summary": summary_payload,
                "verification": verification,
                "artifacts": [artifact.artifact_id],
                "workspace": workspace_summary.get("workspace_path"),
                "checkpoint_id": checkpoint_id,
                "checkpoint": checkpoint_payload,
                "dry_run": bool(task_envelope.constraints.get("dry_run", False)),
            }

        if iteration == max_iterations and checkpoint is None and context.get("max_iterations") is not None and verification["status"] == "not_run":
            return {
                "status": "IN_PROGRESS",
                "task_status": task_status,
                "status_schema_version": 1,
                "summary": summary_payload,
                "verification": verification,
                "artifacts": [artifact.artifact_id],
                "workspace": workspace_summary.get("workspace_path"),
                "checkpoint_id": checkpoint_id,
                "checkpoint": checkpoint_payload,
                "dry_run": bool(task_envelope.constraints.get("dry_run", False)),
            }

        return {
            "status": "COMPLETED",
            "task_status": task_status,
            "status_schema_version": 1,
            "summary": summary_payload,
            "verification": verification,
            "artifacts": [artifact.artifact_id],
            "workspace": workspace_summary.get("workspace_path"),
            "checkpoint_id": checkpoint_id,
            "checkpoint": checkpoint_payload,
            "dry_run": bool(task_envelope.constraints.get("dry_run", False)),
        }

    def _build_run_payload(self, run_id: str, execution_id: str, workflow_name: str, workflow_path: str, status: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        workflow_execution = self.kernel.workflow_executions[execution_id]
        normalized_context = dict(context or {})
        return {
            "run_id": run_id,
            "execution_id": execution_id,
            "workflow_name": workflow_name,
            "workflow_path": workflow_path,
            "status": status,
            "started_at": workflow_execution.started_at,
            "completed_at": workflow_execution.completed_at,
            "active_controls": [],
            "pending_approval_id": None,
            "last_event_id": workflow_execution.events[-1] if workflow_execution.events else None,
            "completed_steps": [],
            "artifacts": [],
            "artifact_ids": [],
            "context": normalized_context,
            "budget": self._build_task_budget(normalized_context),
            "events": [],
            "last_updated_at": _now_iso(),
        }

    def _consume_run_tool_call_budget(self, run_id: Optional[str]) -> Optional[str]:
        if run_id is None:
            return None
        with self._state_lock:
            run_record = self.run_store.get(run_id)
            if run_record is None:
                return "run_not_found"
            budget = run_record.get("budget") or self._build_task_budget(run_record.get("context", {}))
            limits = budget.get("limits", {})
            usage = budget.setdefault("usage", {})
            used = int(usage.get("tool_calls", 0))
            maximum = int(limits.get("max_tool_calls", 20))
            if used >= maximum:
                run_record["budget"] = budget
                self.run_store.update(run_id, run_record)
                return "tool_call_budget_exceeded"
            usage["tool_calls"] = used + 1
            run_record["budget"] = budget
            self.run_store.update(run_id, run_record)
            return None

    def _find_existing_run(self, context: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not context:
            return None
        idempotency_key = context.get("idempotency_key") or context.get("run_idempotency_key")
        if not idempotency_key:
            return None
        for run_record in self.run_store.list():
            if run_record.get("idempotency_key") == idempotency_key:
                return run_record
        return None

    def _record_lifecycle_event_if_needed(self, run_id: str, previous_status: Optional[str], next_status: str) -> None:
        if previous_status == next_status:
            return
        if previous_status in {None, "STARTING", "QUEUED"} and next_status in {"RUNNING", "WAITING_APPROVAL", "COMPLETED", "FAILED", "CANCELLED"}:
            self._record_run_event(run_id, "RUN_STARTED", {"status": next_status})
        if previous_status in {None, "STARTING", "RUNNING"} and next_status == "WAITING_APPROVAL":
            self._record_run_event(run_id, "RUN_PAUSED", {"status": next_status})
        elif previous_status == "WAITING_APPROVAL" and next_status not in {"WAITING_APPROVAL", "COMPLETED", "FAILED", "CANCELLED"}:
            self._record_run_event(run_id, "RUN_RESUMED", {"status": next_status})
        elif next_status in {"COMPLETED", "FAILED", "CANCELLED"}:
            self._record_run_event(run_id, "RUN_FINALIZED", {"status": next_status})

    def _sync_run_state(self, run_id: str) -> Dict[str, Any]:
        with self._state_lock:
            run_record = self.run_store.get(run_id)
            if run_record is None:
                raise KeyError(f"Run '{run_id}' not found")

            if run_record.get("status") == "CANCELLED":
                run_record["active_controls"] = []
                self.run_store.update(run_id, run_record)
                return run_record

            execution_id = run_record["execution_id"]
            workflow_execution = self.kernel.workflow_executions.get(execution_id)
            if workflow_execution is None:
                return run_record

            workflow_definition = self.kernel.workflow_definitions.get(workflow_execution.workflow_definition_id)
            completed_steps = [self.kernel.step_executions[step_id].step_id for step_id in workflow_execution.completed_executions]
            artifacts = list(workflow_execution.produced_artifacts)
            previous_artifact_ids = set(run_record.get("artifact_ids", []))
            active_controls = []
            pending_approval_id = None
            if workflow_execution.status == "WAITING_APPROVAL":
                active_controls = ["respond_approval"]
                for event_id in reversed(workflow_execution.events):
                    event = self.kernel.event_log.get(event_id)
                    if event is not None and event.event_type == "APPROVAL_REQUIRED":
                        pending_approval_id = event.event_id
                        break
            elif workflow_execution.status in {"COMPLETED", "FAILED"}:
                active_controls = []
            else:
                active_controls = ["observe_run"]

            run_status = run_record.get("status")
            if run_status in {"COMPLETED", "FAILED", "CANCELLED"}:
                status = run_status
            elif run_record.get("queued_for_background") and run_status == "QUEUED" and workflow_execution.status in {"PLANNING", "CREATED"}:
                status = "QUEUED"
            else:
                status = workflow_execution.status

            previous_status = run_record.get("status")
            run_record.update({
                "workflow_name": workflow_definition.name if workflow_definition is not None else run_record.get("workflow_name", "<unknown>"),
                "status": status,
                "started_at": workflow_execution.started_at,
                "completed_at": workflow_execution.completed_at,
                "active_controls": active_controls if status not in {"COMPLETED", "FAILED", "CANCELLED"} else [],
                "pending_approval_id": pending_approval_id,
                "last_event_id": run_record.get("last_event_id") or (workflow_execution.events[-1] if workflow_execution.events else None),
                "completed_steps": completed_steps,
                "artifacts": artifacts,
                "context": run_record.get("context", {}),
            })
            new_artifact_ids = [artifact_id for artifact_id in artifacts if artifact_id not in previous_artifact_ids]
            for artifact_id in new_artifact_ids:
                self._record_run_event(run_id, "ARTIFACT_ADDED", {"artifact_id": artifact_id})

            self._record_lifecycle_event_if_needed(run_id, previous_status, status)
            self._update_liveness_and_recovery(run_id, run_record)
            run_record["artifact_ids"] = artifacts
            self.run_store.update(run_id, run_record)
            return run_record

    def _run_workflow_in_background(self, run_id: str, execution_id: str, *, provider: str = "stub", model_name: Optional[str] = None, endpoint: Optional[str] = None) -> None:
        with self._state_lock:
            try:
                self._sync_run_state(run_id)
                self._advance_workflow(execution_id, provider=provider, model_name=model_name, endpoint=endpoint)
            except Exception as exc:
                run_record = self.run_store.get(run_id)
                if run_record is not None:
                    run_record["status"] = "FAILED"
                    run_record["error"] = str(exc)
                    self.run_store.update(run_id, run_record)
            finally:
                try:
                    self._sync_run_state(run_id)
                except Exception:
                    pass

    def run_workflow(
        self,
        workflow_path: str,
        *,
        provider: str = "stub",
        model_name: Optional[str] = None,
        endpoint: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        with self._state_lock:
            workflow_definition = self._load_workflow(workflow_path)
            execution_context = ExecutionContext.from_dict(context) if context else ExecutionContext.default()
            normalized_policy_context = self._normalize_policy_context(context, strict=bool((context or {}).get("require_policy_context")))
            execution_id = self.kernel.start_workflow(
                workflow_definition.workflow_definition_id,
                execution_context,
                policy_context=normalized_policy_context,
            )
            # Determine provider routing for this run so it can be persisted
            routing_decision = self._route_provider("start_run routing", provider=provider, model_name=model_name, endpoint=endpoint)
            # Provider routing decision and health summary for observability
            routing_decision = self._route_provider("start_run routing", provider=provider, model_name=model_name, endpoint=endpoint)
            self._advance_workflow(execution_id, provider=provider, model_name=model_name, endpoint=endpoint)
            return self.get_execution_summary(execution_id)

    def start_run(
        self,
        workflow_path: str,
        *,
        provider: str = "stub",
        model_name: Optional[str] = None,
        endpoint: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        with self._state_lock:
            existing_run = self._find_existing_run(context)
            if existing_run is not None:
                return self.get_run(existing_run["run_id"])

            workflow_definition = self._load_workflow(workflow_path)
            execution_context = ExecutionContext.from_dict(context) if context else ExecutionContext.default()
            normalized_policy_context = self._normalize_policy_context(context, strict=bool((context or {}).get("require_policy_context")))
            execution_id = self.kernel.start_workflow(
                workflow_definition.workflow_definition_id,
                execution_context,
                policy_context=normalized_policy_context,
            )
            # Determine provider routing for this run so it can be persisted
            routing_decision = self._route_provider("start_run routing", provider=provider, model_name=model_name, endpoint=endpoint)
            run_id = str(uuid.uuid4())
            run_record = self._build_run_payload(run_id, execution_id, workflow_definition.name, workflow_path, "STARTING", context=context)
            run_record["provider_routing"] = routing_decision.health_summary if routing_decision is not None else {}
            run_record["selected_provider"] = routing_decision.provider if routing_decision is not None else provider
            run_record["idempotency_key"] = context.get("idempotency_key") if context else None
            if context and context.get("auto_approve"):
                run_record["auto_approve"] = True
            if context:
                for key in ["session_id", "agent_goal", "workflow_template", "runtime_hints", "user_intent_summary", "last_user_message"]:
                    if key in context:
                        run_record["context"][key] = context[key]
            self.run_store.create(run_id, run_record)
            # Persist routing decision as an event for later analysis
            self._record_run_event(run_id, "RUN_PROVIDER_ROUTED", {"provider_routing": run_record.get("provider_routing"), "selected_provider": run_record.get("selected_provider")}, source="bridge")
            self._record_run_event(run_id, "RUN_CREATED", {"workflow_path": workflow_path, "workflow_name": workflow_definition.name, "execution_id": execution_id}, source="bridge")
            self._advance_workflow(execution_id, provider=provider, model_name=model_name, endpoint=endpoint, stop_on_pause=True)
            self._sync_run_state(run_id)
            if self._should_auto_approve(run_record):
                self._auto_approve_if_needed(run_id, execution_id)
            self._update_liveness_and_recovery(run_id, run_record)
            thread = threading.Thread(
                target=self._run_workflow_in_background,
                args=(run_id, execution_id),
                kwargs={"provider": provider, "model_name": model_name, "endpoint": endpoint},
                daemon=True,
            )
            self._background_threads[run_id] = thread
            thread.start()
            return self._sync_run_state(run_id)

    def queue_run(
        self,
        workflow_path: str,
        *,
        provider: str = "stub",
        model_name: Optional[str] = None,
        endpoint: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        with self._state_lock:
            existing_run = self._find_existing_run(context)
            if existing_run is not None:
                return self.get_run(existing_run["run_id"])

            workflow_definition = self._load_workflow(workflow_path)
            execution_context = ExecutionContext.from_dict(context) if context else ExecutionContext.default()
            normalized_policy_context = self._normalize_policy_context(context, strict=bool((context or {}).get("require_policy_context")))
            execution_id = self.kernel.start_workflow(
                workflow_definition.workflow_definition_id,
                execution_context,
                policy_context=normalized_policy_context,
            )
            run_id = str(uuid.uuid4())
            run_record = self._build_run_payload(run_id, execution_id, workflow_definition.name, workflow_path, "QUEUED", context=context)
            run_record["idempotency_key"] = context.get("idempotency_key") if context else None
            if context and context.get("auto_approve"):
                run_record["auto_approve"] = True
            if context:
                for key in ["session_id", "agent_goal", "workflow_template", "runtime_hints", "user_intent_summary", "last_user_message"]:
                    if key in context:
                        run_record["context"][key] = context[key]
            run_record["queued_for_background"] = True
            self.run_store.create(run_id, run_record)
            self._record_run_event(run_id, "RUN_QUEUED", {"workflow_path": workflow_path, "workflow_name": workflow_definition.name, "execution_id": execution_id}, source="bridge")
            self._update_liveness_and_recovery(run_id, run_record)
            thread = threading.Thread(
                target=self._run_queued_workflow,
                args=(run_id, execution_id),
                kwargs={"provider": provider, "model_name": model_name, "endpoint": endpoint},
                daemon=True,
            )
            self._background_threads[run_id] = thread
            thread.start()
            return {
                "run_id": run_id,
                "execution_id": execution_id,
                "workflow_name": workflow_definition.name,
                "workflow_path": workflow_path,
                "status": "QUEUED",
                "context": context or {},
            }

    def _record_run_event(
        self,
        run_id: str,
        event_type: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        actor: Optional[str] = None,
        correlation_id: Optional[str] = None,
        status: Optional[str] = None,
        source: str = "bridge",
    ) -> Optional[Dict[str, Any]]:
        with self._state_lock:
            run_record = self.run_store.get(run_id)
            if run_record is None:
                return None

            event_id = str(uuid.uuid4())
            event_payload = dict(payload or {})
            if actor is not None:
                event_payload["actor"] = actor
            if correlation_id is not None:
                event_payload["correlation_id"] = correlation_id
            event_payload.setdefault("run_id", run_id)
            event_payload.setdefault("execution_id", run_record.get("execution_id"))
            event_payload.setdefault("source", source)
            event_payload.setdefault("status_after", status or run_record.get("status"))
            event_record = {
                "event_id": event_id,
                "event_type": event_type,
                "timestamp": _now_iso(),
                "payload": event_payload,
            }
            run_record.setdefault("events", []).append(event_record)
            run_record["last_event_id"] = event_id
            run_record["last_updated_at"] = event_record["timestamp"]
            if status is not None:
                run_record["status"] = status
            self.run_store.update(run_id, run_record)
            return event_record

    def _resume_pending_runs(self) -> None:
        for run_record in self.run_store.list():
            run_id = run_record.get("run_id")
            if not run_id:
                continue
            if not run_record.get("queued_for_background"):
                continue
            if run_record.get("status") in {"COMPLETED", "FAILED", "CANCELLED"}:
                continue
            execution_id = run_record.get("execution_id")
            if not execution_id:
                continue
            workflow_status = self.kernel.get_workflow_status(execution_id)
            if workflow_status in {"COMPLETED", "FAILED", "CANCELLED"}:
                continue
            recovery = dict(run_record.setdefault("recovery", {}))
            recovery["restart_count"] = recovery.get("restart_count", 0) + 1
            run_record["recovery"] = recovery
            self.run_store.update(run_id, run_record)
            thread = threading.Thread(
                target=self._run_queued_workflow,
                args=(run_id, execution_id),
                daemon=True,
            )
            self._background_threads[run_id] = thread
            thread.start()

    def _should_auto_approve(self, run_record: Dict[str, Any]) -> bool:
        return bool(run_record.get("auto_approve") or run_record.get("context", {}).get("auto_approve"))

    def _auto_approve_if_needed(self, run_id: str, execution_id: str) -> None:
        run_record = self.run_store.get(run_id)
        if run_record is None:
            return
        if self.kernel.get_workflow_status(execution_id) != "WAITING_APPROVAL":
            return
        try:
            self.kernel.approve_workflow(execution_id, approved_by="auto-approval", reason="auto-approved for unattended execution")
            self._record_run_event(run_id, "CONTROL_RESPONDED", {"choice": "approve", "mode": "auto"}, status="RUNNING", source="bridge")
            self._advance_workflow(execution_id)
        except Exception:
            pass

    def _run_queued_workflow(self, run_id: str, execution_id: str, *, provider: str = "stub", model_name: Optional[str] = None, endpoint: Optional[str] = None) -> None:
        try:
            self._sync_run_state(run_id)
            self._advance_workflow(execution_id, provider=provider, model_name=model_name, endpoint=endpoint, stop_on_pause=True)
            if self._should_auto_approve(self.run_store.get(run_id) or {}):
                self._auto_approve_if_needed(run_id, execution_id)
            self._sync_run_state(run_id)
        except Exception as exc:
            run_record = self.run_store.get(run_id)
            if run_record is not None:
                run_record["status"] = "FAILED"
                run_record["error"] = str(exc)
                self.run_store.update(run_id, run_record)
        finally:
            try:
                self._sync_run_state(run_id)
            except Exception:
                pass

    def get_execution_summary(self, execution_id: str) -> Dict[str, Any]:
        workflow_execution = self.kernel.workflow_executions[execution_id]
        workflow_definition = self.kernel.workflow_definitions.get(workflow_execution.workflow_definition_id)
        completed_steps = [self.kernel.step_executions[step_id].step_id for step_id in workflow_execution.completed_executions]
        artifacts = list(workflow_execution.produced_artifacts)
        return {
            "execution_id": workflow_execution.execution_id,
            "workflow_name": workflow_definition.name if workflow_definition is not None else "<unknown>",
            "status": workflow_execution.status,
            "started_at": workflow_execution.started_at,
            "completed_at": workflow_execution.completed_at,
            "completed_steps": completed_steps,
            "artifacts": artifacts,
        }

    def _build_run_summary(self, run_record: Dict[str, Any]) -> Dict[str, Any]:
        pending_approval_role = None
        if run_record.get("status") == "WAITING_APPROVAL":
            pending_approval_role = "human_operator"
        return {
            "status": run_record.get("status"),
            "artifact_count": len(run_record.get("artifacts", [])),
            "completed_steps": run_record.get("completed_steps", []),
            "pending_approval_role": pending_approval_role,
            "active_controls": run_record.get("active_controls", []),
            "last_event_id": run_record.get("last_event_id"),
            "last_updated_at": run_record.get("last_updated_at"),
            "recovery_restart_count": run_record.get("recovery", {}).get("restart_count", 0),
        }

    def _build_resume_hint(self, run_record: Dict[str, Any]) -> Dict[str, Any]:
        status = str(run_record.get("status") or "").upper()
        recovery = run_record.get("recovery") or {}
        checkpoint_id = run_record.get("checkpoint_id") or run_record.get("latest_checkpoint_id")
        can_resume = status not in {"COMPLETED", "FAILED", "CANCELLED"}
        return {
            "can_resume": can_resume,
            "resume_target": checkpoint_id,
            "reason": "checkpoint available" if checkpoint_id else "no checkpoint persisted yet",
            "recovery_restart_count": recovery.get("restart_count", 0),
        }

    def get_run(self, run_id: str) -> Dict[str, Any]:
        with self._state_lock:
            run_record = self._sync_run_state(run_id)
            checkpoint = None
            checkpoint_id = run_record.get("checkpoint_id") or run_record.get("latest_checkpoint_id")
            if checkpoint_id:
                checkpoint = self.get_checkpoint(checkpoint_id)
            return {
                "run_id": run_record["run_id"],
                "execution_id": run_record["execution_id"],
                "workflow_name": run_record["workflow_name"],
                "workflow_path": run_record.get("workflow_path"),
                "status": run_record["status"],
                "started_at": run_record["started_at"],
                "completed_at": run_record["completed_at"],
                "completed_steps": run_record["completed_steps"],
                "artifacts": run_record["artifacts"],
                "active_controls": run_record.get("active_controls", []),
                "pending_approval_id": run_record.get("pending_approval_id"),
                "last_event_id": run_record.get("last_event_id"),
                "events": run_record.get("events", []),
                "context": run_record.get("context", {}),
                "budget": run_record.get("budget", self._build_task_budget(run_record.get("context", {}))),
                "error": run_record.get("error"),
                "liveness": run_record.get("liveness", {}),
                "recovery": run_record.get("recovery", {}),
                "checkpoint": checkpoint,
                "latest_checkpoint_id": checkpoint_id,
                "resume_hint": self._build_resume_hint(run_record),
                "provider_routing": run_record.get("provider_routing"),
                "selected_provider": run_record.get("selected_provider"),
                "summary": self._build_run_summary(run_record),
            }

    def observe_run(self, run_id: str) -> Dict[str, Any]:
        with self._state_lock:
            run_record = self._sync_run_state(run_id)
            workflow_execution = self.kernel.workflow_executions[run_record["execution_id"]]
            events = []
            for event_id in workflow_execution.events:
                event = self.kernel.event_log.get(event_id)
                if event is not None:
                    events.append({
                        "event_id": event.event_id,
                        "event_type": event.event_type,
                        "payload": event.payload,
                    })
            for event in run_record.get("events", []):
                if not any(existing.get("event_id") == event.get("event_id") for existing in events):
                    payload = dict(event.get("payload", {}))
                    event_source = event.get("source", payload.get("source", "bridge"))
                    event_status_after = event.get("status_after", payload.get("status_after"))
                    events.append({
                        "event_id": event.get("event_id"),
                        "event_type": event.get("event_type"),
                        "payload": payload,
                        "source": event_source,
                        "status_after": event_status_after,
                    })
            if not any(event.get("source") == "bridge" for event in events):
                for event in events:
                    if isinstance(event.get("payload"), dict):
                        event.setdefault("source", event["payload"].get("source", "bridge"))
                        event.setdefault("status_after", event["payload"].get("status_after"))
            checkpoint_id = run_record.get("checkpoint_id") or run_record.get("latest_checkpoint_id")
            checkpoint = self.get_checkpoint(checkpoint_id) if checkpoint_id else None
            return {
                "run_id": run_id,
                "status": run_record["status"],
                "events": events,
                "cursor": str(len(events)),
                "checkpoint": checkpoint,
                "latest_checkpoint_id": checkpoint_id,
                "resume_hint": self._build_resume_hint(run_record),
                "summary": self._build_run_summary(run_record),
            }

    def get_artifacts(self, execution_id: str) -> List[Dict[str, Any]]:
        workflow_execution = self.kernel.workflow_executions[execution_id]
        artifacts = []
        for artifact_id in workflow_execution.produced_artifacts:
            artifact = self.kernel.artifact_store.get(artifact_id)
            if artifact is not None:
                artifacts.append({
                    "artifact_id": artifact.artifact_id,
                    "artifact_type": artifact.artifact_type,
                    "title": artifact.title,
                    "version": artifact.version,
                    "status": artifact.status,
                })
        return artifacts

    def get_run_artifacts(self, run_id: str) -> List[Dict[str, Any]]:
        with self._state_lock:
            run_record = self.run_store.get(run_id)
            if run_record is None:
                raise KeyError(f"Run '{run_id}' not found")
            return self.get_artifacts(run_record["execution_id"])

    def append_event(self, run_id: str, event: Dict[str, Any]) -> Dict[str, Any]:
        with self._state_lock:
            run_record = self.run_store.get(run_id)
            if run_record is None:
                raise KeyError(f"Run '{run_id}' not found")

            event_id = event.get("event_id") or str(uuid.uuid4())
            normalized_event = {"event_id": event_id, **event}
            normalized_event.setdefault("timestamp", _now_iso())

            payload = normalized_event.get("payload") or {}
            payload = dict(payload) if isinstance(payload, dict) else {"value": payload}
            payload.setdefault("source", "bridge")
            payload.setdefault("status_after", run_record.get("status"))
            normalized_event["payload"] = payload
            normalized_event.setdefault("source", payload["source"])
            normalized_event.setdefault("status_after", payload["status_after"])

            run_record.setdefault("events", []).append(normalized_event)
            run_record["last_event_id"] = event_id
            run_record["last_updated_at"] = normalized_event["timestamp"]
            self.run_store.update(run_id, run_record)
            return {"accepted": True, "status": "appended", "run_id": run_id, "event_id": event_id}

    def handle_action(
        self,
        run_id: str,
        action: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        actor: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._state_lock:
            run_record = self.run_store.get(run_id)
            if run_record is None:
                raise KeyError(f"Run '{run_id}' not found")

            action_name = str(action or "").strip().lower()
            payload = payload or {}
            applied_controls = run_record.setdefault("applied_controls", [])
            if correlation_id is not None and any(item.get("correlation_id") == correlation_id and item.get("action") == action_name for item in applied_controls):
                return {"accepted": False, "status": "duplicate", "run_id": run_id, "message": "control action already applied"}

            if action_name in {"queue_message", "message", "enqueue_message"}:
                message_text = payload.get("text") or payload.get("message") or ""
                event_payload = {"text": message_text, "actor": actor, "correlation_id": correlation_id}
                result = self.append_event(run_id, {"event_type": "MESSAGE_ENQUEUED", "payload": event_payload})
                if result.get("accepted"):
                    run_record = self.run_store.get(run_id)
                    if run_record is None:
                        raise KeyError(f"Run '{run_id}' not found")
                    applied_controls = run_record.setdefault("applied_controls", [])
                    applied_controls.append({"action": action_name, "correlation_id": correlation_id, "actor": actor})
                    run_record["applied_controls"] = applied_controls
                    self.run_store.update(run_id, run_record)
                return result

            if action_name in {"update_goal", "set_goal"}:
                goal_value = payload.get("goal") or payload.get("text") or payload.get("value")
                if goal_value is None:
                    return {"accepted": False, "status": "invalid", "run_id": run_id, "message": "goal payload is required"}
                context = run_record.setdefault("context", {})
                agent_context = context.setdefault("agent_context", {})
                agent_context["goal"] = goal_value
                context["agent_context"] = agent_context
                run_record["context"] = context
                run_record["goal"] = goal_value
                self.run_store.update(run_id, run_record)
                self._record_run_event(run_id, "GOAL_UPDATED", {"goal": goal_value}, actor=actor, correlation_id=correlation_id)
                run_record = self.run_store.get(run_id)
                if run_record is None:
                    raise KeyError(f"Run '{run_id}' not found")
                applied_controls = run_record.setdefault("applied_controls", [])
                applied_controls.append({"action": action_name, "correlation_id": correlation_id, "actor": actor})
                run_record["applied_controls"] = applied_controls
                self.run_store.update(run_id, run_record)
                return {"accepted": True, "status": "updated", "run_id": run_id, "message": "goal updated"}

            if action_name in {"clarify_request", "clarify"}:
                event_payload = {"request": payload.get("request") or payload.get("text") or "", "actor": actor, "correlation_id": correlation_id}
                result = self.append_event(run_id, {"event_type": "CONTROL_REQUESTED", "payload": event_payload})
                if result.get("accepted"):
                    run_record = self.run_store.get(run_id)
                    if run_record is None:
                        raise KeyError(f"Run '{run_id}' not found")
                    applied_controls = run_record.setdefault("applied_controls", [])
                    applied_controls.append({"action": action_name, "correlation_id": correlation_id, "actor": actor})
                    run_record["applied_controls"] = applied_controls
                    self.run_store.update(run_id, run_record)
                return result

            if action_name in {"append_event", "append"}:
                event_data = payload.get("event", payload)
                result = self.append_event(run_id, event_data)
                if result.get("accepted"):
                    run_record = self.run_store.get(run_id)
                    if run_record is None:
                        raise KeyError(f"Run '{run_id}' not found")
                    applied_controls = run_record.setdefault("applied_controls", [])
                    applied_controls.append({"action": action_name, "correlation_id": correlation_id, "actor": actor})
                    run_record["applied_controls"] = applied_controls
                    self.run_store.update(run_id, run_record)
                return result

            if action_name in {"finalize_run", "finalize"}:
                result = self.finalize_run(run_id, payload.get("status"))
                if result.get("accepted"):
                    run_record = self.run_store.get(run_id)
                    if run_record is None:
                        raise KeyError(f"Run '{run_id}' not found")
                    applied_controls = run_record.setdefault("applied_controls", [])
                    applied_controls.append({"action": action_name, "correlation_id": correlation_id, "actor": actor})
                    run_record["applied_controls"] = applied_controls
                    self.run_store.update(run_id, run_record)
                return result

            if action_name in {"cancel_run", "cancel"}:
                result = self.cancel_run(run_id)
                if result.get("accepted"):
                    run_record = self.run_store.get(run_id)
                    if run_record is None:
                        raise KeyError(f"Run '{run_id}' not found")
                    applied_controls = run_record.setdefault("applied_controls", [])
                    applied_controls.append({"action": action_name, "correlation_id": correlation_id, "actor": actor})
                    run_record["applied_controls"] = applied_controls
                    self.run_store.update(run_id, run_record)
                return result

            if action_name in {"respond_approval", "approve"}:
                result = self.respond_approval(run_id, str(payload.get("choice", "approve")))
                if result.get("accepted"):
                    run_record = self.run_store.get(run_id)
                    if run_record is None:
                        raise KeyError(f"Run '{run_id}' not found")
                    applied_controls = run_record.setdefault("applied_controls", [])
                    applied_controls.append({"action": action_name, "correlation_id": correlation_id, "actor": actor})
                    run_record["applied_controls"] = applied_controls
                    self.run_store.update(run_id, run_record)
                return result

            if action_name in {"resume_run", "resume"}:
                synced_run = self._sync_run_state(run_id)
                current_status = str(synced_run.get("status") or "").upper()
                if current_status in {"COMPLETED", "FAILED", "CANCELLED"}:
                    return {"accepted": False, "status": current_status, "run_id": run_id, "message": "run is already terminal"}
                if current_status == "WAITING_APPROVAL":
                    return {"accepted": False, "status": current_status, "run_id": run_id, "message": "run is waiting for approval"}
                self._record_run_event(run_id, "RUN_RESUMED", {"source": "control"}, status="RUNNING", source="bridge")
                run_record = self.run_store.get(run_id)
                if run_record is None:
                    raise KeyError(f"Run '{run_id}' not found")
                applied_controls = run_record.setdefault("applied_controls", [])
                applied_controls.append({"action": action_name, "correlation_id": correlation_id, "actor": actor})
                run_record["applied_controls"] = applied_controls
                self.run_store.update(run_id, run_record)
                return {"accepted": True, "status": "RUNNING", "run_id": run_id, "message": "run resumed"}

            return {"accepted": False, "status": "unsupported", "run_id": run_id, "message": f"unsupported control action: {action}"}

    def finalize_run(self, run_id: str, status: Optional[str] = None) -> Dict[str, Any]:
        with self._state_lock:
            run_record = self.run_store.get(run_id)
            if run_record is None:
                raise KeyError(f"Run '{run_id}' not found")

            next_status = str(status or "COMPLETED").upper()
            run_record["status"] = next_status
            run_record["completed_at"] = None
            run_record["active_controls"] = []
            run_record["error"] = None
            self.run_store.update(run_id, run_record)
            self._record_run_event(run_id, "RUN_FINALIZED", {"status": next_status}, status=next_status, source="bridge")
            return {"accepted": True, "status": next_status, "run_id": run_id, "message": "run finalized"}

    def cancel_run(self, run_id: str) -> Dict[str, Any]:
        with self._state_lock:
            run_record = self.run_store.get(run_id)
            if run_record is None:
                raise KeyError(f"Run '{run_id}' not found")

            execution_id = run_record["execution_id"]
            workflow_status = self.kernel.get_workflow_status(execution_id)
            if workflow_status in {"COMPLETED", "FAILED", "CANCELLED"}:
                return {"accepted": False, "status": workflow_status, "run_id": run_id, "message": "workflow is already terminal"}

            run_record["status"] = "CANCELLED"
            run_record["completed_at"] = None
            run_record["active_controls"] = []
            run_record["error"] = "cancelled by user"
            self.run_store.update(run_id, run_record)
            self._record_run_event(run_id, "RUN_FINALIZED", {"status": "CANCELLED"}, status="CANCELLED", source="bridge")
            return {"accepted": True, "status": "CANCELLED", "run_id": run_id, "message": "run cancelled"}

    def respond_approval(self, run_id: str, choice: str) -> Dict[str, Any]:
        with self._state_lock:
            summary = self.get_run(run_id)
            if summary["status"] != "WAITING_APPROVAL":
                return {
                    "accepted": False,
                    "status": summary["status"],
                    "message": "workflow is not waiting for approval",
                }

            normalized_choice = str(choice or "").strip().lower()
            if normalized_choice != "approve":
                return {
                    "accepted": False,
                    "status": summary["status"],
                    "message": "unsupported approval choice",
                }

            execution_id = summary["execution_id"]
            self.kernel.approve_workflow(execution_id, approved_by="ui-user", reason="approved", comment=None)
            self._record_run_event(run_id, "CONTROL_RESPONDED", {"choice": "approve"}, status="RUNNING", source="bridge")
            self._advance_workflow(execution_id)
            updated = self.get_run(run_id)
            return {
                "accepted": True,
                "status": updated["status"],
                "run_id": run_id,
                "message": "approval recorded",
            }

    def approve_tool_request(
        self,
        run_id: str,
        approval_id: str,
        *,
        approved_by: str = "api-user",
        reason: str = "approved",
        comment: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._state_lock:
            run_record = self.run_store.get(run_id)
            if run_record is None:
                raise KeyError(f"Run '{run_id}' not found")
            workflow_execution = self.kernel.workflow_executions[run_record["execution_id"]]
            pending_event = next(
                (
                    self.kernel.event_log.get(event_id)
                    for event_id in reversed(workflow_execution.events)
                    if self.kernel.event_log.get(event_id) is not None
                    and self.kernel.event_log.get(event_id).event_type == "TOOL_APPROVAL_REQUIRED"
                    and self.kernel.event_log.get(event_id).payload.get("approval_id") == approval_id
                ),
                None,
            )
            if pending_event is None:
                return {"accepted": False, "status": "NOT_FOUND", "run_id": run_id, "message": "tool approval not found"}

            decision = self.kernel.approve_tool_request(
                pending_event.payload["step_execution_id"],
                approval_id,
                approved_by=approved_by,
                reason=reason,
                comment=comment,
            )
            self._sync_run_state(run_id)
            self._record_run_event(
                run_id,
                "TOOL_APPROVAL_GRANTED",
                {
                    "approval_id": approval_id,
                    "tool_id": pending_event.payload.get("tool_id"),
                    "action": pending_event.payload.get("action"),
                    "risk_level": pending_event.payload.get("risk_level"),
                    "request_fingerprint": pending_event.payload.get("request_fingerprint"),
                    "step_execution_id": pending_event.payload.get("step_execution_id"),
                    "approved_by": approved_by,
                    "reason": reason,
                    "comment": comment,
                    "approval_required_event_id": pending_event.event_id,
                },
            )
            if decision.allowed:
                pending_request = ToolRequest(
                    tool_id=pending_event.payload.get("tool_id", ""),
                    action=pending_event.payload.get("action", ""),
                    parameters=pending_event.payload.get("parameters", {}),
                )
                self.execute_tool_request(
                    pending_request,
                    run_id=run_id,
                    step_execution_id=pending_event.payload.get("step_execution_id"),
                )
            return {
                "accepted": decision.allowed,
                "status": "APPROVED" if decision.allowed else "REJECTED",
                "run_id": run_id,
                "approval_id": approval_id,
                "message": decision.reason,
            }

    def deny_tool_request(
        self,
        run_id: str,
        approval_id: str,
        *,
        denied_by: str = "api-user",
        reason: str = "denied",
        comment: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._state_lock:
            run_record = self.run_store.get(run_id)
            if run_record is None:
                raise KeyError(f"Run '{run_id}' not found")
            workflow_execution = self.kernel.workflow_executions[run_record["execution_id"]]
            pending_event = next(
                (
                    self.kernel.event_log.get(event_id)
                    for event_id in reversed(workflow_execution.events)
                    if self.kernel.event_log.get(event_id) is not None
                    and self.kernel.event_log.get(event_id).event_type == "TOOL_APPROVAL_REQUIRED"
                    and self.kernel.event_log.get(event_id).payload.get("approval_id") == approval_id
                ),
                None,
            )
            if pending_event is None:
                return {"accepted": False, "status": "NOT_FOUND", "run_id": run_id, "message": "tool approval not found"}

            decision = self.kernel.deny_tool_request(
                pending_event.payload["step_execution_id"],
                approval_id,
                denied_by=denied_by,
                reason=reason,
                comment=comment,
            )
            self._sync_run_state(run_id)
            self._record_run_event(
                run_id,
                "TOOL_APPROVAL_DENIED",
                {
                    "approval_id": approval_id,
                    "tool_id": pending_event.payload.get("tool_id"),
                    "action": pending_event.payload.get("action"),
                    "risk_level": pending_event.payload.get("risk_level"),
                    "request_fingerprint": pending_event.payload.get("request_fingerprint"),
                    "step_execution_id": pending_event.payload.get("step_execution_id"),
                    "denied_by": denied_by,
                    "reason": reason,
                    "comment": comment,
                    "approval_required_event_id": pending_event.event_id,
                },
            )
            return {
                "accepted": not decision.allowed and decision.reason == "tool_approval_denied",
                "status": "DENIED" if not decision.allowed else "REJECTED",
                "run_id": run_id,
                "approval_id": approval_id,
                "message": decision.reason,
            }

    def list_pending_tool_approvals(self, run_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._state_lock:
            runs = [self.run_store.get(run_id)] if run_id else self.run_store.list()
            pending: List[Dict[str, Any]] = []
            for run_record in runs:
                if not run_record:
                    continue
                execution_id = run_record.get("execution_id")
                workflow_execution = self.kernel.workflow_executions.get(execution_id)
                if workflow_execution is None:
                    continue
                approval_state: Dict[str, str] = {}
                for event_id in workflow_execution.events:
                    event = self.kernel.event_log.get(event_id)
                    if event is None:
                        continue
                    payload = event.payload or {}
                    approval_id = payload.get("approval_id")
                    if not approval_id:
                        continue
                    if event.event_type == "TOOL_APPROVAL_REQUIRED":
                        approval_state[approval_id] = "pending"
                    elif event.event_type == "TOOL_APPROVAL_GRANTED":
                        approval_state[approval_id] = "approved"
                    elif event.event_type == "TOOL_APPROVAL_DENIED":
                        approval_state[approval_id] = "denied"
                for event_id in workflow_execution.events:
                    event = self.kernel.event_log.get(event_id)
                    if event is None or event.event_type != "TOOL_APPROVAL_REQUIRED":
                        continue
                    payload = event.payload or {}
                    approval_id = payload.get("approval_id")
                    if not approval_id or approval_state.get(approval_id) != "pending":
                        continue
                    parameters = payload.get("parameters") or {}
                    redacted_parameters = {
                        key: "[REDACTED]" if any(token in key.lower() for token in ("token", "secret", "password", "api_key")) else value
                        for key, value in parameters.items()
                    }
                    pending.append({
                        "run_id": run_record.get("run_id"),
                        "execution_id": execution_id,
                        "step_execution_id": payload.get("step_execution_id"),
                        "approval_id": approval_id,
                        "tool_id": payload.get("tool_id"),
                        "action": payload.get("action"),
                        "risk_level": payload.get("risk_level"),
                        "parameters": redacted_parameters,
                        "reason": payload.get("reason"),
                        "requested_at": payload.get("requested_at"),
                    })
            return pending

    def list_policy_denials(self, run_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._state_lock:
            runs = [self.run_store.get(run_id)] if run_id else self.run_store.list()
            denials: List[Dict[str, Any]] = []
            denial_types = {"STEP_EXECUTION_POLICY_DENIED", "POLICY_DENIED", "TOOL_FAILED"}
            for run_record in runs:
                if not run_record:
                    continue
                execution_id = run_record.get("execution_id")
                workflow_execution = self.kernel.workflow_executions.get(execution_id)
                if workflow_execution is None:
                    continue
                for event_id in workflow_execution.events:
                    event = self.kernel.event_log.get(event_id)
                    if event is None or event.event_type not in denial_types:
                        continue
                    payload = event.payload or {}
                    if event.event_type == "TOOL_FAILED" and not str(payload.get("reason", "")).startswith(("tool_registry.", "tool_constraints.", "policy.")):
                        continue
                    denials.append({
                        "run_id": run_record.get("run_id"),
                        "execution_id": execution_id,
                        "event_id": event.event_id,
                        "event_type": event.event_type,
                        "timestamp": event.timestamp,
                        "step_execution_id": payload.get("execution_id"),
                        "tool_id": payload.get("tool_id"),
                        "action": payload.get("action"),
                        "risk_level": payload.get("risk_level"),
                        "reason": payload.get("reason"),
                    })
            return denials

    def __enter__(self) -> "RuntimeService":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.shutdown()

    def shutdown(self) -> None:
        for thread in list(self._background_threads.values()):
            if thread.is_alive():
                thread.join(timeout=0.1)
        try:
            metrics_sink = getattr(self.kernel, "metrics", None)
            if metrics_sink is not None:
                metrics_sink.close()
        except Exception:
            pass
        self.run_store.close()
        self.kernel.shutdown()
