from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Optional
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


class ModelAdapterFactory:
    @staticmethod
    def create(config: Optional[ModelAdapterConfig] = None) -> ModelAdapter:
        config = config or ModelAdapterConfig()
        if config.provider == "ollama":
            return OllamaModelAdapter(config)
        return StubModelAdapter(config)
