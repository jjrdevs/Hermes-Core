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

    def evaluate(self, action: str, context: Dict[str, Any]) -> PolicyDecision:
        policy_context = context.get("policy_context", {}) or {}
        if policy_context.get("approved") is False:
            return PolicyDecision(False, "policy_context.not_approved")

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
