import subprocess
import tempfile
import unittest
from pathlib import Path


class TestPackagingSmoke(unittest.TestCase):
    def test_packaged_binary_smoke_run(self):
        repo_root = Path(__file__).resolve().parent.parent
        binary = repo_root / "dist" / "hermes"

        if not binary.exists():
            self.skipTest("Packaged binary not built; run build.sh first")

        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data"
            validate = subprocess.run(
                [str(binary), "validate", str(repo_root / "examples" / "hello_world.json")],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(validate.returncode, 0, validate.stderr)
            self.assertIn("is valid", validate.stdout)

            run = subprocess.run(
                [str(binary), "run", str(repo_root / "examples" / "hello_world.json"), "--data-dir", str(data_dir)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn("Workflow execution:", run.stdout)
            self.assertTrue((data_dir / "events.db").exists())

            missing = subprocess.run(
                [str(binary), "validate", str(repo_root / "examples" / "does_not_exist.json")],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(missing.returncode, 0)


if __name__ == "__main__":
    unittest.main()
