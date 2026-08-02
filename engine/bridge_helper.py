from __future__ import annotations

import argparse
import json
import os
from typing import Any, Dict, Optional

from engine.api import HermesApi


def handoff_payload(payload: Dict[str, Any], *, data_dir: Optional[str] = None) -> Dict[str, Any]:
    """Execute a structured handoff payload against the Hermes Core runtime.

    The payload is intentionally thin and agent-friendly. It accepts the same
    action names supported by HermesApi.handle so an external agent can hand off
    work without importing Hermes Core internals directly.
    """

    api = HermesApi(data_dir=data_dir)
    try:
        response = api.handle(payload)
    finally:
        api.shutdown()

    if isinstance(response, dict):
        summary = response.get("summary") or response.get("status") or response.get("run_id")
        return {
            "status": response.get("status", "ok"),
            "summary": summary,
            "response": response,
        }
    return {"status": "ok", "summary": str(response), "response": response}


def handoff_task(
    task: str,
    *,
    workspace_path: Optional[str] = None,
    data_dir: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return handoff_payload(
        {
            "action": "run_task",
            "task": task,
            "workspace_path": workspace_path,
            "context": context or {},
        },
        data_dir=data_dir,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Hand off a task to Hermes Core")
    parser.add_argument("task", nargs="?", default="")
    parser.add_argument("--workspace-path")
    parser.add_argument("--data-dir")
    parser.add_argument("--payload", help="JSON handoff payload")
    parser.add_argument("--context", help="JSON context object")
    args = parser.parse_args()

    if args.payload:
        payload = json.loads(args.payload)
        result = handoff_payload(payload, data_dir=args.data_dir)
    else:
        task = args.task or os.environ.get("HERMES_BRIDGE_TASK", "")
        if not task:
            raise SystemExit("Provide a task or --payload")
        context = json.loads(args.context) if args.context else None
        result = handoff_task(task, workspace_path=args.workspace_path, data_dir=args.data_dir, context=context)

    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
