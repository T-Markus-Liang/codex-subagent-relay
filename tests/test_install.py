import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class InstallTests(unittest.TestCase):
    def test_install_launcher_uses_selected_python(self):
        with tempfile.TemporaryDirectory() as temporary_home:
            environment = {**os.environ, "HOME": temporary_home}
            installed = subprocess.run(
                ["make", "install-local", f"PYTHON={sys.executable}"],
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            launcher = Path(temporary_home) / ".local/bin/deepseek-worker"
            self.assertTrue(launcher.is_file())
            version = subprocess.run([str(launcher), "--version"], text=True, capture_output=True, check=False)
            self.assertEqual(version.returncode, 0, version.stderr)
            self.assertEqual(version.stdout.strip(), "0.7.1")


if __name__ == "__main__":
    unittest.main()
