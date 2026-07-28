import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class TestBuildArtifacts(unittest.TestCase):
    def test_build_script_produces_packaged_binary_and_examples(self):
        repo_root = Path(__file__).resolve().parent.parent
        build_script = repo_root / "build.sh"
        binary = repo_root / "dist" / "hermes"
        examples_dir = repo_root / "dist" / "examples"
        manifest_path = repo_root / "dist" / "release-manifest.json"

        if not build_script.exists():
            self.fail("build.sh is missing")

        with tempfile.TemporaryDirectory() as temp_dir:
            env = os.environ.copy()
            env["PATH"] = f"{repo_root / '.venv' / 'bin'}:{env['PATH']}"
            result = subprocess.run(
                ["bash", str(build_script)],
                cwd=repo_root,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
            self.assertTrue(binary.exists(), msg="packaged binary was not created")
            self.assertTrue(examples_dir.exists(), msg="examples bundle was not created")
            self.assertTrue(manifest_path.exists(), msg="release manifest was not created")

            version_result = subprocess.run(
                [str(binary), "--version"],
                cwd=repo_root,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(version_result.returncode, 0, msg=version_result.stderr or version_result.stdout)
            self.assertIn("Hermes Core", version_result.stdout)

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["binary"], "hermes")
            self.assertTrue(manifest["examples_dir"].endswith("examples"))


if __name__ == "__main__":
    unittest.main()
