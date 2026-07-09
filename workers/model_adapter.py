from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict


class ModelAdapter(ABC):
    @abstractmethod
    def generate(self, prompt: str, *, temperature: float = 0.2, max_tokens: int = 1024) -> str:
        raise NotImplementedError

    @abstractmethod
    def capabilities(self) -> Dict[str, Any]:
        raise NotImplementedError


class StubModelAdapter(ModelAdapter):
    def __init__(self) -> None:
        self._capabilities = {
            "name": "stub-model",
            "provider": "local",
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
