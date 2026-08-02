from __future__ import annotations

import tempfile

from engine.bridge_helper import handoff_payload, handoff_task


def test_handoff_task_returns_runtime_summary(tmp_path):
    data_dir = tmp_path / "bridge-data"
    result = handoff_task(
        "inspect the repository",
        workspace_path=str(tmp_path),
        data_dir=str(data_dir),
        context={"dry_run": True},
    )

    assert "status" in result
    assert "summary" in result


def test_handoff_payload_supports_run_task_action(tmp_path):
    data_dir = tmp_path / "bridge-data"
    result = handoff_payload(
        {
            "action": "run_task",
            "task": "inspect the repository",
            "workspace_path": str(tmp_path),
            "context": {"dry_run": True},
        },
        data_dir=str(data_dir),
    )

    assert "status" in result
    assert "summary" in result
