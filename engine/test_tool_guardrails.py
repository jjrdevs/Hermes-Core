import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from engine.models import ToolRequest
from engine.tool_runtime import FilesystemTool, ShellTool


class TestToolGuardrails(unittest.TestCase):
    def test_filesystem_tool_rejects_paths_outside_allowlist(self):
        with tempfile.TemporaryDirectory(prefix="hermes-guard-", dir="/tmp") as temp_dir:
            allowed = Path(temp_dir)
            tool = FilesystemTool(allowed_roots=[str(allowed)])
            request = ToolRequest(tool_id="filesystem", action="read_file", parameters={"path": "/tmp/other.txt"})
            result = tool.execute(request)
            self.assertEqual(result.status, "error")
            self.assertIn("outside the allowed workspace roots", result.error)

    def test_shell_tool_rejects_disallowed_commands(self):
        tool = ShellTool(allowed_commands=["pytest", "python"])
        request = ToolRequest(tool_id="shell", action="run", parameters={"command": "rm -rf /tmp/test"})
        result = tool.execute(request)
        self.assertEqual(result.status, "error")
        self.assertIn("not allowed", result.error)

    def test_shell_tool_rejects_working_directory_outside_allowed_roots(self):
        with tempfile.TemporaryDirectory(prefix="hermes-shell-root-", dir="/tmp") as temp_dir:
            allowed_root = Path(temp_dir) / "allowed"
            allowed_root.mkdir()
            tool = ShellTool(
                allowed_commands=["python"],
                metadata={"allowed_cwds": [str(allowed_root)]},
            )
            request = ToolRequest(
                tool_id="shell",
                action="run",
                parameters={"command": "python -c 'print(1)'", "cwd": "/tmp"},
            )
            result = tool.execute(request)
            self.assertEqual(result.status, "error")
            self.assertIn("working directory", result.error)

    def test_shell_tool_rejects_environment_keys_outside_allowlist(self):
        tool = ShellTool(
            allowed_commands=["python"],
            metadata={"allowed_env_keys": ["PATH"]},
        )
        request = ToolRequest(
            tool_id="shell",
            action="run",
            parameters={"command": "python -c 'print(1)'", "env": {"SECRET_TOKEN": "value"}},
        )
        result = tool.execute(request)
        self.assertEqual(result.status, "error")
        self.assertIn("environment variable", result.error)

    def test_shell_tool_inherits_only_allowlisted_environment_keys(self):
        tool = ShellTool(
            allowed_commands=["python"],
            metadata={"allowed_env_keys": ["PATH"]},
        )
        request = ToolRequest(
            tool_id="shell",
            action="run",
            parameters={"command": "python -c 'import os; print(\"HOME\" in os.environ, \"PATH\" in os.environ)'"},
        )
        result = tool.execute(request)
        self.assertEqual(result.status, "ok")
        self.assertIn("False", result.data.get("stdout", ""))
        self.assertIn("True", result.data.get("stdout", ""))

    def test_shell_tool_sanitizes_environment_by_default(self):
        with patch.dict(os.environ, {"SENSITIVE_TOKEN": "secret", "PATH": "/tmp/allowed-bin"}, clear=True):
            tool = ShellTool(allowed_commands=["python"])
            request = ToolRequest(
                tool_id="shell",
                action="run",
                parameters={"command": "python -c 'import os; print(\"SENSITIVE_TOKEN\" in os.environ, \"PATH\" in os.environ)'"},
            )
            result = tool.execute(request)
            self.assertEqual(result.status, "ok")
            self.assertIn("False", result.data.get("stdout", ""))
            self.assertIn("True", result.data.get("stdout", ""))

    def test_shell_tool_limits_child_output(self):
        tool = ShellTool(
            allowed_commands=["python"],
            metadata={"max_output_bytes": 32},
        )
        request = ToolRequest(
            tool_id="shell",
            action="run",
            parameters={"command": "python -c 'print(\"x\" * 128)'"},
        )
        result = tool.execute(request)
        self.assertEqual(result.status, "error")
        self.assertIn("output limit", result.error)

    def test_shell_tool_applies_cpu_limit_to_child_process(self):
        tool = ShellTool(
            allowed_commands=["python"],
            metadata={"max_cpu_seconds": 1},
        )
        request = ToolRequest(
            tool_id="shell",
            action="run",
            parameters={"command": "python -c 'while True: pass'", "timeout": 5},
        )
        result = tool.execute(request)
        self.assertEqual(result.status, "error")
        self.assertFalse(result.data.get("succeeded"))
        self.assertIsNotNone(result.data.get("exit_code"))

    def test_shell_tool_rejects_network_access_in_strict_sandbox(self):
        tool = ShellTool(
            allowed_commands=["curl"],
            metadata={"sandbox_profile": "strict"},
        )
        request = ToolRequest(
            tool_id="shell",
            action="run",
            parameters={"command": "curl -I https://example.com"},
        )
        result = tool.execute(request)
        self.assertEqual(result.status, "error")
        self.assertIn("network", result.error.lower())

    def test_shell_tool_rejects_network_access_when_capability_contract_disallows_it(self):
        tool = ShellTool(
            allowed_commands=["curl"],
            metadata={"capability_contract": {"allow_network": False}},
        )
        request = ToolRequest(
            tool_id="shell",
            action="run",
            parameters={"command": "curl -I https://example.com"},
        )
        result = tool.execute(request)
        self.assertEqual(result.status, "error")
        self.assertIn("network", result.error.lower())

    def test_shell_tool_respects_capability_contract_command_allowlist(self):
        tool = ShellTool(metadata={"capability_contract": {"allowed_commands": ["python"]}})
        request = ToolRequest(
            tool_id="shell",
            action="run",
            parameters={"command": "bash -c 'echo hello'"},
        )
        result = tool.execute(request)
        self.assertEqual(result.status, "error")
        self.assertIn("not allowed", result.error.lower())

    def test_filesystem_tool_rejects_mutating_actions_outside_capability_writable_paths(self):
        with tempfile.TemporaryDirectory(prefix="hermes-capability-writable-", dir="/tmp") as temp_dir:
            root = Path(temp_dir)
            allowed = root / "allowed"
            allowed.mkdir()
            outside = root / "outside"
            outside.mkdir()
            tool = FilesystemTool(
                allowed_roots=[str(root)],
                metadata={"capability_contract": {"writable_paths": [str(allowed)]}},
            )
            request = ToolRequest(
                tool_id="filesystem",
                action="write_file",
                parameters={"path": str(outside / "note.txt"), "content": "hello"},
            )
            result = tool.execute(request)
            self.assertEqual(result.status, "error")
            self.assertIn("writable", result.error.lower())

    def test_filesystem_tool_rejects_mutating_actions_when_writable_paths_is_empty(self):
        with tempfile.TemporaryDirectory(prefix="hermes-capability-readonly-", dir="/tmp") as temp_dir:
            root = Path(temp_dir)
            tool = FilesystemTool(
                allowed_roots=[str(root)],
                metadata={"capability_contract": {"writable_paths": []}},
            )
            request = ToolRequest(
                tool_id="filesystem",
                action="write_file",
                parameters={"path": str(root / "note.txt"), "content": "hello"},
            )
            result = tool.execute(request)
            self.assertEqual(result.status, "error")
            self.assertIn("writable", result.error.lower())

    def test_shell_tool_timeout_terminates_child_process_group(self):
        with tempfile.TemporaryDirectory(prefix="hermes-shell-process-group-", dir="/tmp") as temp_dir:
            marker = Path(temp_dir) / "descendant-finished.txt"
            descendant_code = f"import pathlib,time; time.sleep(0.3); pathlib.Path({str(marker)!r}).write_text('done')"
            parent_code = f"import subprocess,sys,time; subprocess.Popen([sys.executable, '-c', {descendant_code!r}]); time.sleep(5)"
            tool = ShellTool(allowed_commands=["python"])

            result = tool.execute(
                ToolRequest(
                    tool_id="shell",
                    action="run",
                    parameters={"command": ["python", "-c", parent_code], "timeout": 0.05},
                )
            )

            self.assertEqual(result.status, "error")
            self.assertIn("timed out", result.error.lower())
            time.sleep(0.4)
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
