import tempfile
import unittest
from pathlib import Path

from engine.models import ToolContract, ToolExecutionEnvelope, ToolRequest
from engine.runtime_service import RuntimeService
from engine.tool_runtime import FilesystemTool


class TestToolContracts(unittest.TestCase):
    def test_runtime_service_wraps_tool_results_in_envelopes(self):
        with tempfile.TemporaryDirectory(prefix="hermes-contract-", dir="/tmp") as temp_dir:
            workspace = Path(temp_dir)
            marker = workspace / "hello.txt"
            marker.write_text("hello\n", encoding="utf-8")

            service = RuntimeService(data_dir=str(workspace))
            try:
                tool = FilesystemTool(allowed_roots=[str(workspace)])
                request = ToolRequest(tool_id="filesystem", action="read_file", parameters={"path": str(marker)})
                envelopes = service.execute_tool_request(request)
                self.assertEqual(len(envelopes), 1)
                self.assertEqual(envelopes[0].status, "ok")
                self.assertIn("content", envelopes[0].output)
            finally:
                service.shutdown()

    def test_tool_contract_round_trips_to_dict(self):
        contract = ToolContract(
            tool_id="filesystem",
            name="Filesystem",
            description="Read and write workspace files",
            actions=["read_file", "write_file"],
            allowed_roles=["developer"],
            metadata={"sandbox": "local"},
            risk_level="low",
            timeout_seconds=10,
        )
        self.assertEqual(contract.to_dict()["risk_level"], "low")

    def test_tool_execution_envelope_serializes(self):
        envelope = ToolExecutionEnvelope(
            request=ToolRequest(tool_id="filesystem", action="read_file", parameters={"path": "/tmp/file"}),
            status="ok",
            output={"path": "/tmp/file"},
            error=None,
            metadata={"attempt": 1},
        )
        self.assertEqual(envelope.to_dict()["status"], "ok")


if __name__ == "__main__":
    unittest.main()
