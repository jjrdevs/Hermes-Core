from __future__ import annotations

import dataclasses
from typing import Dict, List, Optional

from .models import Tool


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        existing = self._tools.get(tool.tool_id)
        if existing is not None and existing != tool:
            raise ValueError(f"Tool conflict for id {tool.tool_id}")
        self._tools[tool.tool_id] = tool

    def replace(self, tool: Tool) -> None:
        """Unconditionally overwrite any existing entry for tool.tool_id.
        Use for workflow re-declaration overrides, not for external
        callers that must detect conflicts.
        """
        self._tools[tool.tool_id] = tool

    def get(self, tool_id: str) -> Optional[Tool]:
        return self._tools.get(tool_id)

    def list_tools(self) -> List[Tool]:
        return list(self._tools.values())

    def authorize(self, role: str, tool_id: str, action: str) -> bool:
        tool = self.get(tool_id)
        if tool is None:
            return False
        if action not in tool.actions:
            return False
        if tool.allowed_roles and role not in tool.allowed_roles:
            return False
        return True
