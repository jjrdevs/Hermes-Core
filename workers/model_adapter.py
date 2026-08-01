from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib import error, request


@dataclass(frozen=True)
class ModelAdapterConfig:
    provider: str = "stub"
    model_name: Optional[str] = None
    endpoint: Optional[str] = None
    api_key: Optional[str] = None
    timeout_seconds: float = 30.0
    temperature: float = 0.2
    max_tokens: int = 1024
    options: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderProfile:
    provider: str
    cost_class: str = "cheap"
    available: bool = True
    healthy: bool = True
    model_name: Optional[str] = None
    endpoint: Optional[str] = None
    api_key: Optional[str] = None
    timeout_seconds: float = 30.0
    options: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RoutingDecision:
    provider: str
    adapter: "ModelAdapter"
    fallback_used: bool
    reason: str
    health_summary: Dict[str, Any] = field(default_factory=dict)


class ModelAdapter(ABC):
    def __init__(self, config: Optional[ModelAdapterConfig] = None) -> None:
        self.config = config or ModelAdapterConfig()

    @abstractmethod
    def generate(self, prompt: str, *, temperature: float = 0.2, max_tokens: int = 1024) -> str:
        raise NotImplementedError

    @abstractmethod
    def capabilities(self) -> Dict[str, Any]:
        raise NotImplementedError


class StubModelAdapter(ModelAdapter):
    def __init__(self, config: Optional[ModelAdapterConfig] = None) -> None:
        super().__init__(config)
        self._capabilities = {
            "name": self.config.model_name or "stub-model",
            "provider": "stub",
            "capabilities": {
                "code": True,
                "reasoning": True,
                "planning": False,
                "tool_use": False,
                "vision": False,
                "structured_output": True,
            },
            "context_window": 4096,
            "max_tokens": 1024,
        }

    def generate(self, prompt: str, *, temperature: float = 0.2, max_tokens: int = 1024) -> str:
        return f"[stub response] {prompt}"

    def capabilities(self) -> Dict[str, Any]:
        return self._capabilities


class OllamaModelAdapter(ModelAdapter):
    def __init__(self, config: Optional[ModelAdapterConfig] = None) -> None:
        super().__init__(config)
        self._capabilities = {
            "name": self.config.model_name or "llama3.1",
            "provider": "ollama",
            "capabilities": {
                "code": True,
                "reasoning": True,
                "planning": True,
                "tool_use": False,
                "vision": False,
                "structured_output": True,
            },
            "context_window": 32768,
            "max_tokens": self.config.max_tokens,
        }

    def generate(self, prompt: str, *, temperature: float = 0.2, max_tokens: int = 1024) -> str:
        endpoint = (self.config.endpoint or "http://localhost:11434").rstrip("/")
        payload = {
            "model": self.config.model_name or self._capabilities["name"],
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        if self.config.options:
            payload["options"].update(self.config.options)
        data = json.dumps(payload).encode("utf-8")
        req = request.Request(
            f"{endpoint}/api/generate",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=self.config.timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except error.URLError as exc:
            raise RuntimeError(f"Ollama request failed: {exc}") from exc

        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Ollama returned invalid JSON") from exc

        return parsed.get("response", "")

    def capabilities(self) -> Dict[str, Any]:
        return self._capabilities


class ModelAdapterRouter:
    def __init__(
        self,
        profiles: Optional[List[ProviderProfile]] = None,
        preferred_provider: Optional[str] = None,
        health_registry: Optional[Any] = None,
    ) -> None:
        self.profiles = profiles or [ProviderProfile(provider="stub", cost_class="cheap", available=True)]
        self.preferred_provider = preferred_provider
        self.health_registry = health_registry
        # if provided, register profiles for background probing
        if self.health_registry is not None:
            try:
                self.health_registry.register_profiles([{"provider": p.provider, **(p.__dict__ if hasattr(p, '__dict__') else {})} for p in self.profiles])
                if hasattr(self.health_registry, "start"):
                    self.health_registry.start()
            except Exception:
                pass

    def _cost_rank(self, provider: ProviderProfile) -> int:
        rank_map = {"cheap": 0, "local": 1, "remote": 2, "expensive": 3}
        return rank_map.get(provider.cost_class.lower(), 99)

    def _select_provider(self, task_complexity: str) -> ProviderProfile:
        complexity = (task_complexity or "simple").lower()
        available_profiles = [profile for profile in self.profiles if profile.available and getattr(profile, "healthy", True)]
        if not available_profiles:
            return ProviderProfile(provider="stub", cost_class="cheap", available=True, healthy=True)

        if self.preferred_provider:
            preferred = next((profile for profile in available_profiles if profile.provider == self.preferred_provider), None)
            if preferred is not None:
                return preferred

        if complexity == "simple":
            available_profiles.sort(key=self._cost_rank)
            return available_profiles[0]

        available_profiles.sort(key=self._cost_rank)
        return available_profiles[0]

    @staticmethod
    def _timeout_classification(timeout_seconds: Optional[float]) -> str:
        if timeout_seconds is None:
            return "unknown"
        try:
            timeout_value = float(timeout_seconds)
        except (TypeError, ValueError):
            return "unknown"
        if timeout_value <= 5.0:
            return "fast"
        if timeout_value <= 30.0:
            return "moderate"
        return "slow"

    @staticmethod
    def _cost_per_token(profile: ProviderProfile) -> Optional[float]:
        options = profile.options or {}
        cost_value = options.get("cost_per_token")
        if cost_value is None:
            return None
        try:
            return float(cost_value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _health_status(profile: ProviderProfile) -> str:
        if not profile.available:
            return "unavailable"
        if not getattr(profile, "healthy", True):
            return "degraded"
        return "healthy"

    @staticmethod
    def _retry_classification(profile: ProviderProfile) -> str:
        if not profile.available:
            return "skipped"
        if not getattr(profile, "healthy", True):
            return "deferred"
        return "immediate"

    def route(self, prompt: str, *, task_complexity: str = "simple") -> RoutingDecision:
        selected_profile = self._select_provider(task_complexity)
        config = ModelAdapterConfig(
            provider=selected_profile.provider,
            model_name=selected_profile.model_name,
            endpoint=selected_profile.endpoint,
            api_key=selected_profile.api_key,
            timeout_seconds=selected_profile.timeout_seconds,
            options=selected_profile.options,
        )
        adapter = ModelAdapterFactory.create(config)
        fallback_used = bool(self.preferred_provider and selected_profile.provider != self.preferred_provider)
        preferred_candidates = [profile for profile in self.profiles if profile.available and getattr(profile, "healthy", True)]
        provider_health = {}
        for profile in self.profiles:
            summary = None
            if self.health_registry is not None:
                try:
                    summary = self.health_registry.get_summary(profile.provider)
                except Exception:
                    summary = None

            if summary is not None:
                provider_health[profile.provider] = {
                    "available": profile.available,
                    "healthy": summary.get("healthy", getattr(profile, "healthy", True)),
                    "cost_class": profile.cost_class,
                    "endpoint": profile.endpoint,
                    "timeout_seconds": profile.timeout_seconds,
                    "timeout_classification": self._timeout_classification(profile.timeout_seconds),
                    "cost_per_token": self._cost_per_token(profile),
                    "health_status": "healthy" if summary.get("healthy") else "degraded",
                    "retry_classification": self._retry_classification(profile),
                    "circuit_state": summary.get("circuit_state"),
                    "failures": summary.get("failures"),
                }
            else:
                provider_health[profile.provider] = {
                    "available": profile.available,
                    "healthy": getattr(profile, "healthy", True),
                    "cost_class": profile.cost_class,
                    "endpoint": profile.endpoint,
                    "timeout_seconds": profile.timeout_seconds,
                    "timeout_classification": self._timeout_classification(profile.timeout_seconds),
                    "cost_per_token": self._cost_per_token(profile),
                    "health_status": self._health_status(profile),
                    "retry_classification": self._retry_classification(profile),
                }
        reason = f"Routed {prompt!r} to provider {selected_profile.provider}"
        if self.preferred_provider and self.preferred_provider not in {p.provider for p in preferred_candidates}:
            reason = f"Preferred provider {self.preferred_provider!r} was unavailable or unhealthy; routed {prompt!r} to provider {selected_profile.provider}"
        health_summary = {
            "preferred_provider": self.preferred_provider,
            "selected_provider": selected_profile.provider,
            "provider_health": provider_health,
            "fallback_used": fallback_used,
            "reason": reason,
        }
        return RoutingDecision(
            provider=selected_profile.provider,
            adapter=adapter,
            fallback_used=fallback_used,
            reason=reason,
            health_summary=health_summary,
        )


class ModelAdapterFactory:
    @staticmethod
    def create(config: Optional[ModelAdapterConfig] = None) -> ModelAdapter:
        config = config or ModelAdapterConfig()
        if config.provider == "ollama":
            return OllamaModelAdapter(config)
        return StubModelAdapter(config)
