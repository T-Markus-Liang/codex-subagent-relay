import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class InstallTests(unittest.TestCase):
    def test_source_entrypoint_requires_python_311(self):
        first_line = (ROOT / "deepseek-worker").read_text(encoding="utf-8").splitlines()[0]
        self.assertEqual(first_line, "#!/usr/bin/env python3.11")

    def test_install_launcher_uses_selected_python(self):
        with tempfile.TemporaryDirectory() as temporary_home:
            environment = {**os.environ, "HOME": temporary_home}
            launcher = Path(temporary_home) / ".local/bin/deepseek-worker"
            launcher.parent.mkdir(parents=True)
            launcher.symlink_to(ROOT / "deepseek-worker")
            installed = subprocess.run(
                ["make", "install-local", f"PYTHON={sys.executable}"],
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            self.assertTrue(launcher.is_file())
            self.assertFalse(launcher.is_symlink())
            version = subprocess.run([str(launcher), "--version"], text=True, capture_output=True, check=False)
            self.assertEqual(version.returncode, 0, version.stderr)
        self.assertEqual(version.stdout.strip(), "0.10.22")


if __name__ == "__main__":
    unittest.main()
