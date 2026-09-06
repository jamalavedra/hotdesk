import json
import subprocess
import sys
import tempfile
import unittest
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace

from hotdesk.config import load_config
from hotdesk.server import DeskService


class RegressionTests(unittest.TestCase):
    def test_projects_in_one_directory_have_separate_state(self):
        with tempfile.TemporaryDirectory() as directory:
            configs = []
            for project in ("first", "second"):
                path = Path(directory) / f"{project}.toml"
                path.write_text(
                    f'[project]\nname="{project}"\n[profiles.work]\n[desktops.work]\nprofile="work"\n'
                )
                configs.append(load_config(path))
            self.assertNotEqual(configs[0].state_dir, configs[1].state_dir)

    def test_invalid_legacy_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            config = SimpleNamespace(state_dir=Path(directory), desktops={"work": {}})
            for value in ([], {"work": "unknown"}):
                (config.state_dir / "control.json").write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    DeskService(config)

    def test_version_works_without_config(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-m", "hotdesk", "--version"],
                cwd=directory,
                text=True,
                capture_output=True,
                check=True,
            )
            self.assertEqual(result.stdout.strip(), version("hotdesk"))
