from __future__ import annotations

import dataclasses
from typing import Any, Dict, Optional


@dataclasses.dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: Optional[str] = None


class PolicyEvaluator:
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

        if allowed_tools is not None and len(allowed_tools) == 0:
            return PolicyDecision(False, "step_constraints.no_allowed_tools")

        return PolicyDecision(True)
