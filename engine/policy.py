from __future__ import annotations

import dataclasses
import shlex
from typing import Any, Dict, Optional


@dataclasses.dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: Optional[str] = None


class PolicyEvaluator:
    @staticmethod
    def _normalize_command(command: Any) -> list[str]:
        if isinstance(command, (list, tuple)):
            return [str(part) for part in command]
        if isinstance(command, str):
            return shlex.split(command)
        return []

    @classmethod
    def _normalize_policy_context(cls, policy_context: Any) -> tuple[bool, Optional[str]]:
        if policy_context is None:
            return False, "policy_context.missing"
        if not isinstance(policy_context, dict):
            return False, "policy_context.invalid"
        if "approved" not in policy_context:
            return False, "policy_context.missing"
        if policy_context.get("approved") is False:
            if isinstance(policy_context.get("reason"), str):
                return False, policy_context["reason"]
            return False, "policy_context.not_approved"
        if "policy_ids" not in policy_context or policy_context.get("policy_ids") is None:
            return False, "policy_context.incomplete"
        return True, None

    @classmethod
    def _command_allowed(cls, command: Any, allowed_commands: Any) -> bool:
        if not allowed_commands:
            return False
        requested = cls._normalize_command(command)
        if not requested:
            return False
        for candidate in allowed_commands:
            allowed_tokens = cls._normalize_command(candidate)
            if not allowed_tokens:
                continue
            if requested[: len(allowed_tokens)] == allowed_tokens:
                return True
        return False

    @staticmethod
    def _contains_network_access(command: Any) -> bool:
        tokens = PolicyEvaluator._normalize_command(command)
        if not tokens:
            return False
        network_tools = {"curl", "wget", "nc", "ncat", "netcat", "ssh", "scp", "sftp", "ftp", "telnet", "ping"}
        if tokens[0].lower() in network_tools:
            return True
        for token in tokens:
            lowered = str(token).lower()
            if "http://" in lowered or "https://" in lowered or "ftp://" in lowered:
                return True
        return False

    def evaluate(self, action: str, context: Dict[str, Any]) -> PolicyDecision:
        policy_context = context.get("policy_context", {}) or {}
        normalized, reason = self._normalize_policy_context(policy_context)
        if action == "invoke_tool" and not normalized:
            return PolicyDecision(False, reason)

        constraints = context.get("step_constraints", {}) or {}
        if constraints.get("allow_execution") is False:
            return PolicyDecision(False, "step_constraints.deny_execution")

        allowed_tools = constraints.get("allowed_tools")
        if action == "invoke_tool":
            tool_id = context.get("tool_id")
            if allowed_tools is not None and tool_id not in allowed_tools:
                return PolicyDecision(False, "step_constraints.tool_not_allowed")

            tool_constraints = context.get("tool_constraints", {}) or {}
            if tool_id == "shell":
                command = tool_constraints.get("command")
                allowed_commands = tool_constraints.get("allowed_commands")
                if command is None:
                    return PolicyDecision(False, "tool_constraints.command_required")
                if allowed_commands is not None and not self._command_allowed(command, allowed_commands):
                    return PolicyDecision(False, "tool_constraints.command_not_allowed")
            elif tool_id == "git":
                action = context.get("tool_action")
                allowed_actions = tool_constraints.get("allowed_actions")
                if action is None:
                    return PolicyDecision(False, "tool_constraints.action_required")
                if allowed_actions is not None and action not in allowed_actions:
                    return PolicyDecision(False, "tool_constraints.action_not_allowed")

        if allowed_tools is not None and len(allowed_tools) == 0:
            return PolicyDecision(False, "step_constraints.no_allowed_tools")

        return PolicyDecision(True)

    def evaluate_tool_request(
        self,
        *,
        tool_id: str,
        tool_action: str,
        policy_context: Any,
        step_constraints: Optional[Dict[str, Any]] = None,
        tool_constraints: Optional[Dict[str, Any]] = None,
        strict_policy_context: bool = False,
        extra_context: Optional[Dict[str, Any]] = None,
    ) -> PolicyDecision:
        normalized, reason = self._normalize_policy_context(policy_context)
        if strict_policy_context and not normalized:
            return PolicyDecision(False, reason)
        if not normalized and policy_context is None:
            return PolicyDecision(False, "policy_context.missing")
        if not normalized:
            return PolicyDecision(False, reason)

        constraints = dict(step_constraints or {})
        if constraints.get("allow_execution") is False:
            return PolicyDecision(False, "step_constraints.deny_execution")

        allowed_tools = constraints.get("allowed_tools")
        if allowed_tools is not None and tool_id not in allowed_tools:
            return PolicyDecision(False, "step_constraints.tool_not_allowed")

        tool_constraints = dict(tool_constraints or {})
        if tool_id == "shell":
            command = tool_constraints.get("command")
            allowed_commands = tool_constraints.get("allowed_commands")
            if command is None:
                return PolicyDecision(False, "tool_constraints.command_required")
            if allowed_commands is not None and not self._command_allowed(command, allowed_commands):
                return PolicyDecision(False, "tool_constraints.command_not_allowed")
        elif tool_id == "git":
            action = tool_action
            allowed_actions = tool_constraints.get("allowed_actions")
            if action is None:
                return PolicyDecision(False, "tool_constraints.action_required")
            if allowed_actions is not None and action not in allowed_actions:
                return PolicyDecision(False, "tool_constraints.action_not_allowed")

        if allowed_tools is not None and len(allowed_tools) == 0:
            return PolicyDecision(False, "step_constraints.no_allowed_tools")

        risk_level = (extra_context or {}).get("tool_risk_level")
        if risk_level in {"destructive", "privileged", "external_network"} and strict_policy_context:
            return PolicyDecision(True)

        sandbox_profile = (extra_context or {}).get("sandbox_profile")
        if sandbox_profile == "strict" and tool_id == "shell":
            env = tool_constraints.get("env") if isinstance(tool_constraints, dict) else None
            if isinstance(env, dict) and env:
                return PolicyDecision(False, "environment variable not allowed")
            if self._contains_network_access(tool_constraints.get("command")):
                return PolicyDecision(False, "sandbox: network access is not allowed in strict mode")

        if sandbox_profile == "strict" and tool_id == "filesystem":
            action = tool_action
            mutating_actions = {"write_file", "create_file", "create_directory", "delete_file", "move_file", "copy_file"}
            if action in mutating_actions:
                return PolicyDecision(False, "sandbox: mutating filesystem actions are not allowed in strict mode")

        capability_contract = (extra_context or {}).get("capability_contract") or {}
        if isinstance(capability_contract, dict):
            allowed_roots = capability_contract.get("allowed_roots") or []
            if tool_id == "shell":
                command = tool_constraints.get("command")
                allowed_commands = capability_contract.get("allowed_commands")
                if isinstance(allowed_commands, list) and allowed_commands and command is not None and not self._command_allowed(command, allowed_commands):
                    return PolicyDecision(False, "capability_contract.command_not_allowed")
                env = tool_constraints.get("env")
                allowed_env_keys = capability_contract.get("allowed_env_keys")
                if isinstance(env, dict) and isinstance(allowed_env_keys, list) and allowed_env_keys:
                    unexpected_keys = sorted(set(env) - set(str(key) for key in allowed_env_keys))
                    if unexpected_keys:
                        return PolicyDecision(False, "capability_contract.environment_variable_not_allowed")
                if capability_contract.get("allow_network") is False and self._contains_network_access(command):
                    return PolicyDecision(False, "capability_contract.network_access_not_allowed")
                cwd = tool_constraints.get("cwd")
                if isinstance(allowed_roots, list) and allowed_roots and cwd is not None:
                    normalized_cwd = str(cwd)
                    if not any(normalized_cwd.startswith(str(candidate)) for candidate in [str(item) for item in allowed_roots]):
                        return PolicyDecision(False, "capability_contract.path_not_allowed")
            if tool_id == "filesystem":
                action = tool_action
                mutating_actions = {"write_file", "create_file", "create_directory", "delete_file", "move_file", "copy_file"}
                if action in mutating_actions and "writable_paths" in capability_contract:
                    writable_paths = capability_contract.get("writable_paths") or []
                    if isinstance(writable_paths, list):
                        if not writable_paths:
                            return PolicyDecision(False, "capability_contract.path_not_writable")
                        path = (tool_constraints.get("path") or tool_constraints.get("destination") or tool_constraints.get("source"))
                        if path is not None and not any(str(path).startswith(str(candidate)) for candidate in [str(item) for item in writable_paths]):
                            return PolicyDecision(False, "capability_contract.path_not_writable")
                if isinstance(allowed_roots, list) and allowed_roots:
                    path = (tool_constraints.get("path") or tool_constraints.get("destination") or tool_constraints.get("source"))
                    if path is not None and not any(str(path).startswith(str(candidate)) for candidate in [str(item) for item in allowed_roots]):
                        return PolicyDecision(False, "capability_contract.path_not_allowed")

        capability_requirements = (extra_context or {}).get("capability_requirements") or []
        if capability_requirements:
            required_capabilities = [str(value) for value in capability_requirements if str(value)]
            if required_capabilities:
                tool_capabilities = (extra_context or {}).get("tool_capabilities") or []
                tool_capability_set = {str(value) for value in tool_capabilities if str(value)}
                missing = [capability for capability in required_capabilities if capability not in tool_capability_set]
                if missing:
                    return PolicyDecision(False, f"capability requirements not satisfied: {', '.join(missing)}")

        return PolicyDecision(True)
