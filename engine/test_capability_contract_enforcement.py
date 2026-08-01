import tempfile
import unittest
from pathlib import Path

from engine.models import ToolRequest
from engine.runtime_service import RuntimeService


class TestCapabilityContractEnforcement(unittest.TestCase):
    def test_shell_network_denied_by_capability_contract(self):
        with tempfile.TemporaryDirectory(prefix="hermes-capability-net-", dir="/tmp") as temp_dir:
            service = RuntimeService(data_dir=temp_dir)
            try:
                capability = {"allow_network": False}
                started = service.start_run(str(Path(__file__).resolve().parent.parent / "examples" / "hello_world.json"), context={"capability_contract": capability})
                request = ToolRequest(tool_id="shell", action="run", parameters={"command": "curl http://example.com"})
                envelopes = service.execute_tool_request(request, run_id=started["run_id"])
                self.assertEqual(len(envelopes), 1)
                self.assertEqual(envelopes[0].status, "error")
                # Policy rejects unsupported shell commands defined by tool contract
                self.assertIn("command_not_allowed", envelopes[0].error)
            finally:
                service.shutdown()

    def test_shell_env_key_not_allowed_by_capability_contract(self):
        with tempfile.TemporaryDirectory(prefix="hermes-capability-env-", dir="/tmp") as temp_dir:
            service = RuntimeService(data_dir=temp_dir)
            try:
                capability = {"allowed_env_keys": ["PATH"]}
                started = service.start_run(str(Path(__file__).resolve().parent.parent / "examples" / "hello_world.json"), context={"capability_contract": capability})
                request = ToolRequest(tool_id="shell", action="run", parameters={"command": "pytest -q", "env": {"SECRET": "x"}})
                envelopes = service.execute_tool_request(request, run_id=started["run_id"])
                self.assertEqual(len(envelopes), 1)
                self.assertEqual(envelopes[0].status, "error")
                self.assertIn("environment", envelopes[0].error)
            finally:
                service.shutdown()

    def test_filesystem_write_denied_without_writable_paths(self):
        with tempfile.TemporaryDirectory(prefix="hermes-capability-write-", dir="/tmp") as temp_dir:
            service = RuntimeService(data_dir=temp_dir)
            try:
                capability = {"writable_paths": []}
                started = service.start_run(str(Path(__file__).resolve().parent.parent / "examples" / "hello_world.json"), context={"capability_contract": capability})
                target = Path(temp_dir) / "out.txt"
                request = ToolRequest(tool_id="filesystem", action="write_file", parameters={"path": str(target), "content": "x"})
                envelopes = service.execute_tool_request(request, run_id=started["run_id"])
                self.assertEqual(len(envelopes), 1)
                self.assertEqual(envelopes[0].status, "error")
                self.assertIn("writable", envelopes[0].error)
            finally:
                service.shutdown()


if __name__ == "__main__":
    unittest.main()
