from __future__ import annotations

from typing import Any, Dict, List, Optional

from .models import CapabilityRequest


class CapabilityRegistry:
    def __init__(self, profiles: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
        self._profiles = dict(profiles or {})
        self._workers: Dict[str, Dict[str, Any]] = {}
        self._models: Dict[str, Dict[str, Any]] = {}
        self._tools: Dict[str, Dict[str, Any]] = {}

    def register(self, capability: str, profile: Dict[str, Any]) -> None:
        self._profiles[capability] = profile

    def get(self, capability: str) -> Dict[str, Any]:
        return dict(self._profiles.get(capability, {}))

    def register_default_workers(self) -> None:
        self.register_worker(
            "local-worker-1",
            {
                "role": "developer",
                "worker_type": "developer",
                "capabilities": ["code", "reasoning"],
                "tool_support": True,
                "context_window": 65536,
                "privacy_class": "high",
            },
        )
        self.register_worker(
            "local-worker-2",
            {
                "role": "architect",
                "worker_type": "architect",
                "capabilities": ["planning", "reasoning"],
                "tool_support": True,
                "context_window": 65536,
                "privacy_class": "high",
            },
        )
        self.register_worker(
            "local-worker-3",
            {
                "role": "researcher",
                "worker_type": "researcher",
                "capabilities": ["reasoning", "analysis"],
                "tool_support": False,
                "context_window": 32768,
                "privacy_class": "medium",
            },
        )

    def register_worker(self, worker_id: str, metadata: Dict[str, Any]) -> None:
        self._workers[worker_id] = dict(metadata)

    def register_model(self, model_id: str, metadata: Dict[str, Any]) -> None:
        self._models[model_id] = dict(metadata)

    def register_tool(self, tool_id: str, metadata: Dict[str, Any]) -> None:
        self._tools[tool_id] = dict(metadata)

    def register_model_adapter(self, model_adapter: Any) -> None:
        if not hasattr(model_adapter, "capabilities"):
            return
        capabilities = model_adapter.capabilities() if callable(model_adapter.capabilities) else model_adapter.capabilities
        adapter_metadata = dict(capabilities)
        model_name = adapter_metadata.get("name", "stub-model")
        self.register_model(model_name, adapter_metadata)

    def resolve_worker(self, request: CapabilityRequest, execution_context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        candidate_workers: List[Dict[str, Any]] = []
        for worker_id, metadata in sorted(self._workers.items()):
            worker_type = metadata.get("worker_type", metadata.get("role"))
            capabilities = set(metadata.get("capabilities", []) or [])
            required = set(request.required_capabilities)
            if not required.issubset(capabilities):
                continue
            if request.tool_support is not None and bool(metadata.get("tool_support", False)) != bool(request.tool_support):
                continue
            preferred_context_window = request.preferred_context_window
            context_window = metadata.get("context_window")
            if preferred_context_window is not None and context_window is not None and context_window < preferred_context_window:
                continue
            if execution_context is not None:
                privacy_class = execution_context.get("privacy_class")
                if privacy_class is not None and metadata.get("privacy_class") not in {privacy_class, None}:
                    if metadata.get("privacy_class") == "low" and privacy_class == "high":
                        continue
            candidate_workers.append(
                {
                    "worker_id": worker_id,
                    "worker_type": worker_type,
                    "role": metadata.get("role", worker_type),
                    "capabilities": sorted(capabilities),
                    "compatible": True,
                    "privacy_class": metadata.get("privacy_class"),
                    "tool_support": bool(metadata.get("tool_support", False)),
                    "priority": request.priority,
                }
            )

        if not candidate_workers:
            return {
                "worker_id": None,
                "worker_type": request.role,
                "role": request.role,
                "capabilities": sorted(set(request.required_capabilities)),
                "compatible": False,
                "reason": "no_compatible_worker",
                "priority": request.priority,
            }

        candidate_workers.sort(key=lambda item: (
            -int(bool(item.get("tool_support"))),
            -int(item.get("privacy_class") == "high"),
            -int(item.get("priority") == "high"),
            item.get("worker_id", ""),
        ))
        return candidate_workers[0]

    def resolve_tool(self, request: CapabilityRequest, execution_context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        candidate_tools: List[Dict[str, Any]] = []
        for tool_id, metadata in sorted(self._tools.items()):
            tool_capabilities = set(metadata.get("capabilities", []) or [])
            required = set(request.required_capabilities)
            if not required.issubset(tool_capabilities):
                continue
            if request.tool_support is not None and bool(metadata.get("tool_support", False)) != bool(request.tool_support):
                continue
            if execution_context is not None:
                privacy_class = execution_context.get("privacy_class")
                if privacy_class is not None and metadata.get("privacy_class") not in {privacy_class, None}:
                    if metadata.get("privacy_class") == "low" and privacy_class == "high":
                        continue
            candidate_tools.append(
                {
                    "tool_id": tool_id,
                    "tool_name": metadata.get("name", tool_id),
                    "compatible": True,
                    "capabilities": sorted(tool_capabilities),
                    "allowed_actions": list(metadata.get("allowed_actions", []) or []),
                    "tool_support": bool(metadata.get("tool_support", False)),
                    "profile": {
                        "required_features": list(required),
                        "preferred_context_window": request.preferred_context_window,
                        "requires_tool_support": bool(request.tool_support),
                        "privacy_class": metadata.get("privacy_class"),
                    },
                }
            )

        if not candidate_tools:
            return {
                "tool_id": None,
                "tool_name": None,
                "compatible": False,
                "capabilities": sorted(set(request.required_capabilities)),
                "allowed_actions": [],
                "tool_support": bool(request.tool_support),
                "profile": {
                    "required_features": list(set(request.required_capabilities)),
                    "preferred_context_window": request.preferred_context_window,
                    "requires_tool_support": bool(request.tool_support),
                    "privacy_class": request.privacy_class,
                },
            }

        candidate_tools.sort(key=lambda item: (
            -int(bool(item.get("tool_support"))),
            -int(item.get("profile", {}).get("privacy_class") == "high"),
            item.get("tool_id", ""),
        ))
        return candidate_tools[0]

    def resolve_model(self, request: CapabilityRequest, execution_context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        candidate_models: List[Dict[str, Any]] = []
        for model_id, metadata in sorted(self._models.items()):
            adapter_capabilities = metadata.get("capabilities", {}) or {}
            if not isinstance(adapter_capabilities, dict):
                continue
            required = set(request.required_capabilities)
            available = set(adapter_capabilities.keys())
            if not required.issubset(available):
                continue
            if request.tool_support is not None and bool(adapter_capabilities.get("tool_use", False)) != bool(request.tool_support):
                continue
            preferred_context_window = request.preferred_context_window
            context_window = metadata.get("context_window")
            if preferred_context_window is not None and context_window is not None and context_window < preferred_context_window:
                continue
            if execution_context is not None:
                privacy_class = execution_context.get("privacy_class")
                if privacy_class is not None and metadata.get("privacy_class") not in {privacy_class, None}:
                    if metadata.get("privacy_class") == "low" and privacy_class == "high":
                        continue
            compatible = True
            candidate_models.append(
                {
                    "model_id": model_id,
                    "model_name": metadata.get("name", model_id),
                    "provider": metadata.get("provider", "stub"),
                    "capability": request.role,
                    "compatible": compatible,
                    "reason": [],
                    "profile": {
                        "required_features": list(required),
                        "preferred_context_window": preferred_context_window,
                        "requires_tool_support": bool(request.tool_support),
                        "latency_class": metadata.get("latency_class"),
                        "cost_class": metadata.get("cost_class"),
                        "privacy_class": metadata.get("privacy_class"),
                    },
                    "adapter_capabilities": metadata,
                }
            )

        if not candidate_models:
            return {
                "model_id": None,
                "model_name": None,
                "provider": None,
                "capability": request.role,
                "compatible": False,
                "reason": ["no_compatible_model"],
                "profile": {
                    "required_features": list(set(request.required_capabilities)),
                    "preferred_context_window": request.preferred_context_window,
                    "requires_tool_support": bool(request.tool_support),
                    "latency_class": request.latency_class,
                    "cost_class": request.cost_class,
                    "privacy_class": request.privacy_class,
                },
                "adapter_capabilities": {},
            }

        candidate_models.sort(key=lambda item: (
            -int(item.get("profile", {}).get("requires_tool_support", False)),
            -int(item.get("profile", {}).get("privacy_class") == "high"),
            -int(item.get("profile", {}).get("cost_class") == "low"),
            item.get("model_id", ""),
        ))
        return candidate_models[0]


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
    def resolve_worker_assignment(
        cls,
        capability: str,
        objective: Optional[Dict[str, Any]] = None,
        execution_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        profile = cls.get_profile(capability, objective)
        if execution_context:
            profile["execution_context"] = dict(execution_context)
        return {
            "worker_type": capability,
            "capability": capability,
            "profile": profile,
        }

    @classmethod
    def resolve_model_assignment(
        cls,
        capability: str,
        objective: Optional[Dict[str, Any]] = None,
        model_adapter: Optional[Any] = None,
        execution_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        profile = cls.get_profile(capability, objective)
        adapter_capabilities = {}
        compatible = True
        reasons: list[str] = []

        if execution_context:
            profile["execution_context"] = dict(execution_context)

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
