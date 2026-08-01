import tempfile
import unittest
from pathlib import Path

from engine.runtime_service import RuntimeService


class TestVerificationReporting(unittest.TestCase):
    def test_runtime_service_reports_verification_summary(self):
        with tempfile.TemporaryDirectory(prefix="hermes-verify-", dir="/tmp") as temp_dir:
            workspace = Path(temp_dir)
            (workspace / "tests").mkdir()
            (workspace / "tests" / "test_sample.py").write_text("def test_sample():\n    assert True\n", encoding="utf-8")

            service = RuntimeService(data_dir=str(workspace))
            result = service.run_task(
                "Run a lightweight verification check",
                workspace_path=str(workspace),
                context={"max_iterations": 1, "dry_run": False},
            )

            self.assertEqual(result["status"], "COMPLETED")
            summary = result["summary"]
            self.assertIn("verification_report", summary)
            self.assertEqual(summary["verification_report"]["status"], "passed")
            self.assertEqual(summary["verification_report"]["attempts"], 1)


if __name__ == "__main__":
    unittest.main()
