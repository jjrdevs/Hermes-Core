from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Union
from urllib import error, request

DEFAULT_ENDPOINT = "http://localhost:11434"


# Executor contract for generate_with_tools:
#   (tool_id: str, action: str, params: Dict[str, Any]) -> str | Dict[str, Any]
# Returned value is JSON-encoded into the tool message fed back to the model.
ToolExecutor = Callable[[str, str, Dict[str, Any]], Union[str, Dict[str, Any]]]


@dataclass(frozen=True)
class ChatResult:
    """Return value of ModelAdapter.generate_with_tools.

    text:
        The final assistant text after the loop converged on finish_reason
        without further tool_calls (may be empty string if the model only
        ever emitted tool_calls and then stopped).
    tool_calls:
        One entry per tool dispatch in execution order:
        ``[tool_id, action, params, executor_output_json]``.
    iterations:
        Number of POST /v1/chat/completions round-trips (>= 1).
    finish_reason:
        The finish_reason value of the final assistant turn.
        If the loop hits max_iterations before the model stops emitting
        tool_calls, this is "max_iterations_reached".
    """

    text: str
    tool_calls: List[List[Any]]
    iterations: int
    finish_reason: str


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

    def generate_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
        *,
        tool_executor: Optional[ToolExecutor] = None,
        max_iterations: int = 8,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> ChatResult:
        """Run a multi-turn tool-calling loop against the provider.

        `messages` is the initial message history:
            [{"role": "system", "content": "..."}, {"role": "user", "content": "..."}, ...]

        `tools` is a list of OpenAI-style tool schemas:
            [{"type": "function",
              "function": {"name": "<tool_id>",
                          "description": "...",
                          "parameters": {"type": "object", "properties": {...}, "required": [...]}}},
             ...]
        Tool `name` MUST equal `ToolRequest.tool_id`. Parameters are the
        parameter dict (typically `{"action": <str>}` plus any action
        parameters).

        `tool_executor(tool_id, action, parameters) -> str | dict` is what
        actually runs each tool call (in production this should be bound
        to the kernel's `request_tool` + approval gate + `execute_tool_request`
        path).

        `max_iterations` bounds the number of /chat/completions round-trips.
        """
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

    def generate_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
        *,
        tool_executor: Optional[ToolExecutor] = None,
        max_iterations: int = 8,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> ChatResult:
        # Offline-safe: the stub never emits tool calls. If an executor is
        # provided and the caller wants a deterministic one-call round trip,
        # we can honor exactly one call for the first declared tool.
        last_user = next((m for m in reversed(messages) if m.get("role") == "user"), None)
        last_text = (last_user or {}).get("content", "")
        if tools and tool_executor is not None and messages and len(messages) == 1:
            first = tools[0]
            func = (first or {}).get("function") or {}
            name = func.get("name") or "stub_tool"
            out = tool_executor(name, "invoke", {})
            out_json = out if isinstance(out, str) else json.dumps(out or {})
            return ChatResult(
                text=f"[stub response] {last_text} (dispatched {name})",
                tool_calls=[[name, "invoke", {}, out_json]],
                iterations=1,
                finish_reason="tool_calls",
            )
        return ChatResult(text=f"[stub response] {last_text}", tool_calls=[], iterations=1, finish_reason="stop")


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
                "tool_use": True,
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

    def _chat_completion(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
        *,
        temperature: float,
        max_tokens: int,
    ) -> Dict[str, Any]:
        """One round-trip against /v1/chat/completions. Returns the parsed JSON body.

        Raises RuntimeError on network failure or invalid JSON. Does NOT send
        `tool_choice` (Ollama's Go backend errors on unknown top-level keys).
        """
        endpoint = (self.config.endpoint or DEFAULT_ENDPOINT).rstrip("/")
        if endpoint.endswith("/v1"):
            url = f"{endpoint}/chat/completions"
        elif "/api/" in endpoint:
            url = endpoint.rsplit("/api", 1)[0].rstrip("/") + "/v1/chat/completions"
        else:
            url = f"{endpoint}/v1/chat/completions"

        payload: Dict[str, Any] = {
            "model": self.config.model_name or self._capabilities["name"],
            "messages": messages,
            "stream": False,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        data = json.dumps(payload).encode("utf-8")
        req = request.Request(url, data=data, headers=headers, method="POST")
        try:
            with request.urlopen(req, timeout=self.config.timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except error.URLError as exc:
            raise RuntimeError(f"Ollama request failed: {exc}") from exc
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Ollama returned invalid JSON") from exc

    def generate_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
        *,
        tool_executor: Optional[ToolExecutor] = None,
        max_iterations: int = 8,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> ChatResult:
        if not messages:
            raise ValueError("messages must be non-empty")
        temp = self.config.temperature if temperature is None else temperature
        tok = self.config.max_tokens if max_tokens is None else max_tokens

        history = [dict(m) for m in messages]
        dispatched: List[List[Any]] = []
        iterations = 0
        final_text = ""
        final_reason = "stop"

        for _ in range(max(1, max_iterations)):
            iterations += 1
            body = self._chat_completion(history, tools, temperature=temp, max_tokens=tok)
            try:
                choice = body["choices"][0]
            except (KeyError, IndexError, TypeError) as exc:
                raise RuntimeError(f"Ollama returned unexpected shape: {str(body)[:200]}") from exc
            message = choice.get("message") or {}
            content = message.get("content")
            final_text = content if isinstance(content, str) else ("" if content is None else str(content))
            tool_calls = message.get("tool_calls") or []
            final_reason = choice.get("finish_reason") or ("tool_calls" if tool_calls else "stop")

            if not tool_calls:
                break  # model produced a final answer

            # Record the assistant's tool_call turn, then dispatch each call.
            history.append({"role": "assistant", "content": final_text, "tool_calls": tool_calls})
            for call in tool_calls:
                func = call.get("function") or {}
                tool_id = func.get("name") or ""
                raw_args = func.get("arguments") or "{}"
                if not isinstance(raw_args, str):
                    raw_args = json.dumps(raw_args)
                try:
                    params = json.loads(raw_args) if raw_args.strip() else {}
                    if not isinstance(params, dict):
                        params = {"value": params}
                except (ValueError, TypeError):
                    params = {}
                action = str(params.pop("action", "invoke") or "invoke")
                if tool_executor is None:
                    executor_out: Any = f"tool_executor not provided for {tool_id!r}"
                else:
                    executor_out = tool_executor(tool_id, action, params)
                executor_json = executor_out if isinstance(executor_out, str) else json.dumps(executor_out or {})
                dispatched.append([tool_id, action, params, executor_json])
                call_id = call.get("id") or f"call_{iterations}_{len(tool_calls)}"
                history.append({"role": "tool", "tool_call_id": call_id, "content": executor_json})
        else:
            # for...else: loop ran to max_iterations without the model stopping.
            final_reason = "max_iterations_reached"

        return ChatResult(text=final_text, tool_calls=dispatched, iterations=iterations, finish_reason=final_reason)

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
    """Provider -> concrete adapter registry.

    Providers that speak the OpenAI-compatible /v1/chat/completions HTTP API
    (including Ollama's, OpenAI, OpenRouter, and any custom local or hosted
    backend) are backed by the same concrete adapter, which implements
    generate_with_tools on top of that API. We keep the legacy name
    "OllamaModelAdapter" for it to stay a minimal-diff refactor; if we add
    a provider with a different wire protocol we'd add a new class.

    Only `stub` (in-process canned responses, no network) and `openai`,
    `openrouter`, `anthropic` (not yet mapped: falls through to stub with
    a clear comment — wire up once we have a real key + endpoint for it)
    get their own routing.
    """

    _OPENAI_COMPAT_PROVIDERS = frozenset({"ollama", "openai", "openrouter", "custom", "local"})

    @classmethod
    def create(cls, config: Optional[ModelAdapterConfig] = None) -> ModelAdapter:
        config = config or ModelAdapterConfig()
        if config.provider in cls._OPENAI_COMPAT_PROVIDERS:
            return OllamaModelAdapter(config)
        return StubModelAdapter(config)
