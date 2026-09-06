import concurrent.futures
import json
import tempfile
import unittest
from pathlib import Path

from hotdesk.config import load_config, render_compose
from hotdesk.runtime import Runtime


class ConfigTests(unittest.TestCase):
    def test_config_and_private_rendering(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hotdesk.toml"
            valid = '[project]\nname="test-desk"\n[profiles.work]\nlabel="Work"\n[desktops.one]\nprofile="work"\n'
            path.write_text(valid)
            config = load_config(path)
            rendered = render_compose(config)
            service = rendered["services"]["desktop-one"]
            self.assertEqual(rendered, render_compose(config))
            self.assertEqual(service["volumes"], ["home-work:/home/cua"])
            self.assertEqual(service["ports"], ["127.0.0.1::8001"])
            self.assertEqual(service["restart"], "unless-stopped")
            runtime = Runtime(config)
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                passwords = list(pool.map(lambda _: runtime.viewer_password("one"), range(8)))
            self.assertEqual(len(set(passwords)), 1)
            password = passwords[0]
            self.assertRegex(password, r"^[A-Za-z0-9_-]{8}$")
            self.assertNotEqual(password, runtime.viewer_password("one", read_only=True))
            self.assertNotIn(password, json.dumps(rendered))
            actual = json.loads(runtime.write_compose().read_text())
            self.assertEqual(actual["services"]["desktop-one"]["environment"]["VNC_PW"], password)
            for filename in ("compose.json", "viewer-password-one", "gateway-one"):
                self.assertEqual((config.state_dir / filename).stat().st_mode & 0o777, 0o600)
            self.assertEqual(config.state_dir.stat().st_mode & 0o777, 0o700)
            for content in (
                valid + '[desktops.two]\nprofile="work"\n',
                valid.replace('profile="work"', 'profile="missing"'),
                valid.replace('label="Work"', "cpus=true"),
                valid.replace('label="Work"', "cpus=nan"),
                valid.replace('label="Work"', 'memory="0g"'),
                valid.replace('label="Work"', "typo=1"),
                valid.replace("test-desk", "../escape"),
                valid.replace("[project]", "[project]\nmax_running=true"),
                valid.replace("[project]", '[project]\nmemory_budget="bad"'),
                valid.replace("[project]", '[project]\nimage="bad image"'),
            ):
                path.write_text(content)
                with self.assertRaises(ValueError):
                    load_config(path)
            path.write_text(
                valid.replace(
                    "[project]",
                    '[project]\nmax_running=1\nmemory_budget="3g"\nimage="example/image@sha256:'
                    + "a" * 64
                    + '"',
                )
            )
            config = load_config(path)
            self.assertEqual(config.memory_budget, 3 * 1024**3)
            self.assertNotIn("build", render_compose(config)["services"]["desktop-one"])


if __name__ == "__main__":
    unittest.main()
