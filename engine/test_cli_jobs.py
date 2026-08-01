import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class TestCliJobs(unittest.TestCase):
    def test_cli_can_create_and_list_jobs(self):
        with tempfile.TemporaryDirectory(prefix="hermes-cli-jobs-", dir="/tmp") as temp_dir:
            completed = subprocess.run(
                [
                    sys.executable,
                    "hermes_cli.py",
                    "job",
                    "create",
                    "Inspect the repository",
                    "--data-dir",
                    temp_dir,
                    "--schedule",
                    "manual",
                    "--runtime-budget-seconds",
                    "30",
                ],
                cwd="/home/jjrdev/hermes-core",
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            output = completed.stdout.strip()
            self.assertIn("job-", output)

            list_completed = subprocess.run(
                [
                    sys.executable,
                    "hermes_cli.py",
                    "job",
                    "list",
                    "--data-dir",
                    temp_dir,
                ],
                cwd="/home/jjrdev/hermes-core",
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(list_completed.returncode, 0, list_completed.stderr)
            self.assertIn("Inspect the repository", list_completed.stdout)


if __name__ == "__main__":
    unittest.main()
