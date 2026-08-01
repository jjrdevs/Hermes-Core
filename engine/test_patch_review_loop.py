import tempfile
import unittest
from pathlib import Path

from engine.runtime_service import RuntimeService


class TestPatchReviewLoop(unittest.TestCase):
    def test_runtime_service_writes_and_reports_patch(self):
        with tempfile.TemporaryDirectory(prefix="hermes-patch-", dir="/tmp") as temp_dir:
            workspace = Path(temp_dir)
            marker = workspace / "notes.txt"
            marker.write_text("hello\n", encoding="utf-8")

            service = RuntimeService(data_dir=str(workspace))
            result = service.run_task(
                "Append a review note to the workspace file",
                workspace_path=str(workspace),
                context={"max_iterations": 2, "dry_run": False},
            )

            self.assertEqual(result["status"], "COMPLETED")
            summary = result["summary"]
            self.assertIn("patch_result", summary)
            self.assertTrue(summary["patch_result"]["applied"])
            self.assertIn("diff", summary["patch_result"])
            self.assertIn("review note", marker.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
