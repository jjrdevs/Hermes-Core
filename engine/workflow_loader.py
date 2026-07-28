from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from engine.models import StepDefinition, Tool, WorkflowDefinition


DEFAULT_DATA_DIR = Path.home() / ".hermes" / "data"


def _candidate_base_dirs() -> List[Path]:
    bundled_root = getattr(sys, "_MEIPASS", None)
    candidates: List[Path] = []

    if bundled_root:
        bundled_dir = Path(bundled_root).resolve()
        for candidate in [bundled_dir, bundled_dir / "app", bundled_dir / "examples"]:
            candidates.append(candidate)

    base_dir = Path(__file__).resolve().parent.parent
    for candidate in [base_dir, base_dir / "app", base_dir / "examples"]:
        candidates.append(candidate)

    seen = set()
    ordered: List[Path] = []
    for candidate in candidates:
        if candidate not in seen:
            ordered.append(candidate)
            seen.add(candidate)
    return ordered


def resolve_input_path(path_value: str) -> Path:
    requested = Path(path_value).expanduser()
    if requested.is_absolute():
        resolved = requested.resolve()
    else:
        resolved = requested.resolve()
        for base_dir in _candidate_base_dirs():
            candidate = (base_dir / requested).resolve()
            if candidate.exists():
                return candidate

    if resolved.exists():
        return resolved
    raise FileNotFoundError(f"Workflow file not found: {path_value}")


def load_json_file(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def resolve_data_paths(data_dir: Optional[str]) -> Dict[str, Path]:
    if data_dir is not None:
        root = Path(data_dir).expanduser()
    else:
        root = Path(os.environ.get("HERMES_DATA_DIR", str(DEFAULT_DATA_DIR))).expanduser()
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    return {
        "event_db": root / "events.db",
        "artifact_db": root / "artifacts.db",
        "workflow_definition_db": root / "workflow_definitions.db",
    }


def build_workflow_definition(data: Dict[str, Any]) -> WorkflowDefinition:
    workflow_definition_id = data.get("workflow_definition_id")
    workflow_id = data.get("workflow_id")
    steps = [
        StepDefinition(
            id=step["id"],
            role=step["role"],
            objective=step["objective"],
            depends_on=step["depends_on"],
            outputs=step["outputs"],
            constraints=step["constraints"],
        )
        for step in data["steps"]
    ]
    return WorkflowDefinition.create(
        name=data["name"],
        steps=steps,
        transitions=data.get("transitions", []),
        policy_refs=data.get("policy_refs", []),
        workflow_id=workflow_id,
        workflow_definition_id=workflow_definition_id,
    )


def build_tool_definitions(data: Dict[str, Any]) -> List[Tool]:
    tools: List[Tool] = []
    for tool_data in data.get("tools", []):
        tools.append(
            Tool(
                tool_id=tool_data["tool_id"],
                name=tool_data["name"],
                description=tool_data["description"],
                actions=tool_data["actions"],
                allowed_roles=tool_data["allowed_roles"],
                metadata=tool_data.get("metadata", {}),
            )
        )
    return tools
