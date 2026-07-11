from __future__ import annotations

from typing import Any, Dict, Optional


class CapabilityRegistry:
    def __init__(self, profiles: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
        self._profiles = dict(profiles or {})

    def register(self, capability: str, profile: Dict[str, Any]) -> None:
        self._profiles[capability] = profile

    def get(self, capability: str) -> Dict[str, Any]:
        return dict(self._profiles.get(capability, {}))


class CapabilityResolver:
    """Resolve worker and model assignments from capability requirements."""

    DEFAULT_CAPABILITIES: Dict[str, Dict[str, Any]] = {
        "architect": {
            "required_features": ["planning", "reasoning"],
            "preferred_context_window": 65536,
            "requires_tool_support": True,
        },
        "developer": {
            "required_features": ["code", "reasoning", "test_generation"],
            "preferred_context_window": 65536,
            "requires_tool_support": True,
        },
        "researcher": {
            "required_features": ["reasoning", "analysis"],
            "preferred_context_window": 32768,
            "requires_tool_support": False,
        },
        "reviewer": {
            "required_features": ["review", "reasoning"],
            "preferred_context_window": 16384,
            "requires_tool_support": False,
        },
        "qa": {
            "required_features": ["testing", "review"],
            "preferred_context_window": 32768,
            "requires_tool_support": True,
        },
        "security_auditor": {
            "required_features": ["security", "review"],
            "preferred_context_window": 32768,
            "requires_tool_support": False,
        },
        "planner": {
            "required_features": ["planning", "reasoning"],
            "preferred_context_window": 32768,
            "requires_tool_support": False,
        },
        "operator": {
            "required_features": ["execution", "monitoring"],
            "preferred_context_window": 16384,
            "requires_tool_support": True,
        },
    }

    @staticmethod
    def _feature_alias(feature: str) -> str:
        aliases = {
            "tool_support": "tool_use",
            "tool_use": "tool_use",
            "code_generation": "code",
            "code": "code",
            "reasoning": "reasoning",
            "planning": "planning",
            "test_generation": "code",
            "analysis": "reasoning",
            "review": "reasoning",
            "execution": "code",
            "monitoring": "reasoning",
            "security": "reasoning",
        }
        return aliases.get(feature, feature)

    @classmethod
    def get_profile(cls, capability: str, objective: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        profile = dict(cls.DEFAULT_CAPABILITIES.get(capability, {}))
        if objective:
            required_capabilities = objective.get("required_capabilities", []) or []
            if isinstance(required_capabilities, list) and required_capabilities:
                existing = profile.get("required_features", [])
                profile["required_features"] = list(dict.fromkeys([*existing, *required_capabilities]))

            preferred_context_window = objective.get("preferred_context_window")
            if preferred_context_window is not None:
                profile["preferred_context_window"] = preferred_context_window

            if objective.get("tool_support") is not None:
                profile["requires_tool_support"] = bool(objective.get("tool_support"))

        return profile

    @classmethod
    def resolve_worker_assignment(cls, capability: str, objective: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return {
            "worker_type": capability,
            "capability": capability,
            "profile": cls.get_profile(capability, objective),
        }

    @classmethod
    def resolve_model_assignment(
        cls,
        capability: str,
        objective: Optional[Dict[str, Any]] = None,
        model_adapter: Optional[Any] = None,
    ) -> Dict[str, Any]:
        profile = cls.get_profile(capability, objective)
        adapter_capabilities = {}
        compatible = True
        reasons: list[str] = []

        if model_adapter is not None:
            adapter_capabilities = model_adapter.capabilities() if hasattr(model_adapter, "capabilities") else {}
            adapter_features = adapter_capabilities.get("capabilities", {}) or {}
            if not isinstance(adapter_features, dict):
                adapter_features = {}

            explicit_requirements = []
            if objective is not None:
                explicit_requirements.extend(objective.get("required_capabilities", []) or [])
                if objective.get("tool_support") is True:
                    explicit_requirements.append("tool_support")
                preferred_context_window = objective.get("preferred_context_window")
                context_window = adapter_capabilities.get("context_window")
                if preferred_context_window is not None and context_window is not None and context_window < preferred_context_window:
                    compatible = False
                    reasons.append("insufficient_context_window")

            if explicit_requirements:
                required_features = profile.get("required_features", [])
                combined_requirements = list(dict.fromkeys([*required_features, *explicit_requirements]))
                for feature in combined_requirements:
                    resolved_feature = cls._feature_alias(feature)
                    if feature == "tool_support":
                        if adapter_features.get("tool_use") is True:
                            continue
                        compatible = False
                        reasons.append("missing_tool_support")
                        continue
                    if adapter_features.get(resolved_feature) is True:
                        continue
                    compatible = False
                    reasons.append(f"missing_feature:{feature}")

        else:
            compatible = False
            reasons.append("no_model_adapter")

        return {
            "capability": capability,
            "provider": adapter_capabilities.get("provider", "stub"),
            "model_name": adapter_capabilities.get("name"),
            "compatible": compatible,
            "reason": reasons,
            "profile": profile,
            "adapter_capabilities": adapter_capabilities,
        }
