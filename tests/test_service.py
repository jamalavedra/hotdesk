import os
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from hotdesk import service
from hotdesk.config import load_config


class ServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="hotdesk service ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        path = self.root / "config.toml"
        path.write_text(
            '[project]\nname="service-test"\n[profiles.test]\n[desktops.test]\nprofile="test"\n'
        )
        self.config = load_config(path)
        for name, value in [("sys.platform", "darwin"), ("Path.home", lambda: self.root)]:
            patcher = patch("hotdesk.service." + name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_plist_private_paths_environment_and_no_restart_loop(self):
        with (
            patch.dict(
                os.environ,
                {"DOCKER_CONTEXT": "chosen", "PATH": "/usr/bin:/bin", "SECRET": "hidden"},
                clear=True,
            ),
            patch("hotdesk.service.sys.executable", "/venv with spaces/bin/python"),
        ):
            definition = service._plist(self.config)
        self.assertEqual(definition["ProgramArguments"][0], "/venv with spaces/bin/python")
        self.assertEqual(definition["ProgramArguments"][-1], "serve")
        self.assertEqual(definition["ProgramArguments"][-2], str(self.config.path))
        self.assertTrue(definition["RunAtLoad"])
        self.assertNotIn("KeepAlive", definition)
        self.assertEqual(
            definition["EnvironmentVariables"],
            {"DOCKER_CONTEXT": "chosen", "PATH": "/usr/bin:/bin"},
        )
        self.assertEqual(Path(definition["StandardOutPath"]).stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.config.state_dir.stat().st_mode & 0o777, 0o700)
        self.assertTrue(Path(definition["WorkingDirectory"]).is_absolute())

    def test_install_unmanaged_does_not_start_competitor_and_is_idempotent(self):
        loaded = []

        def launch(*args):
            loaded.append(plistlib.loads(Path(args[-1]).read_bytes()))

        with (
            patch.dict(os.environ, {"DOCKER_HOST": "unix:///chosen.sock"}),
            patch("hotdesk.service.read_connection", return_value={"pid": 123}),
            patch("hotdesk.service._job", return_value=(False, None)),
            patch("hotdesk.service._launchctl", side_effect=launch),
        ):
            service.install(self.config)
        self.assertFalse(loaded[0]["RunAtLoad"])
        self.assertTrue(plistlib.loads(service.service_path(self.config).read_bytes())["RunAtLoad"])
        with (
            patch.dict(os.environ, {"DOCKER_HOST": "unix:///chosen.sock"}),
            patch("hotdesk.service.read_connection", return_value={"pid": 123}),
            patch("hotdesk.service._job", return_value=(True, None)),
            patch("hotdesk.service._launchctl") as run,
        ):
            result = service.install(self.config)
        run.assert_not_called()
        self.assertTrue(result["unmanaged_manager"])

    def test_registered_start_uses_launchctl_and_never_restarts_running_job(self):
        with (
            patch("hotdesk.service._job", return_value=(True, None)),
            patch("hotdesk.service._launchctl") as run,
        ):
            self.assertTrue(service.start_registered(self.config))
            self.assertEqual(run.call_args.args, ("kickstart", service._target(self.config)))
            self.assertGreater(run.call_args.kwargs["timeout"], 0)
        with (
            patch("hotdesk.service._job", return_value=(True, 42)),
            patch("hotdesk.service._launchctl") as run,
        ):
            self.assertTrue(service.start_registered(self.config))
            run.assert_not_called()
        with patch("hotdesk.service._job", return_value=(False, None)):
            self.assertFalse(service.start_registered(self.config))

    def test_uninstall_busy_or_unknown_process_preserves_registration(self):
        for connection in (None, {"pid": 99}, {"pid": 42}):
            stop = Mock(side_effect=RuntimeError("busy"))
            with (
                patch("hotdesk.service._job", return_value=(True, 42)),
                patch("hotdesk.service.read_connection", return_value=connection),
                patch("hotdesk.service._launchctl") as run,
            ):
                with self.assertRaises(RuntimeError):
                    service.uninstall(self.config, stop)
                run.assert_not_called()

    def test_uninstall_stops_managed_only_and_leaves_unmanaged_manager(self):
        for managed in (True, False):
            path = service.service_path(self.config)
            service._write(path, {})
            stop = Mock()
            with (
                patch(
                    "hotdesk.service._job",
                    side_effect=[
                        (True, 42 if managed else None),
                        *(([(True, None)]) if managed else []),
                        (False, None),
                    ],
                ),
                patch("hotdesk.service.read_connection", return_value={"pid": 42}),
                patch("hotdesk.service._launchctl") as run,
            ):
                result = service.uninstall(self.config, stop)
            self.assertEqual(stop.call_count, int(managed))
            run.assert_called_once_with("bootout", service._target(self.config))
            self.assertFalse(path.exists())
            self.assertTrue(result["unmanaged_manager"])

    def test_status_reports_missing_interpreter_and_docker_without_restart(self):
        service._write(service.service_path(self.config), {"ProgramArguments": ["/missing/python"]})
        with (
            patch("hotdesk.service._job", return_value=(True, None)),
            patch(
                "hotdesk.service.read_connection", side_effect=FileNotFoundError("Docker missing")
            ),
            patch("hotdesk.service._launchctl") as run,
        ):
            result = service.status(self.config)
        self.assertTrue(result["registered"])
        self.assertFalse(result["manager_ready"])
        self.assertIn("Installed Python", result["diagnostics"][0])
        self.assertIn("Docker missing", result["diagnostics"])
        run.assert_not_called()

    def test_status_reports_malformed_login_job(self):
        service._write(service.service_path(self.config), {"ProgramArguments": 42})
        with (
            patch("hotdesk.service._job", return_value=(False, None)),
            patch("hotdesk.service.read_connection", return_value=None),
        ):
            self.assertIn("Invalid login job", service.status(self.config)["diagnostics"][0])

    def test_existing_manager_context_wins_and_launch_deadline_is_bounded(self):
        with patch.dict(os.environ, {"DOCKER_CONTEXT": "other"}):
            definition = service._plist(self.config, {"docker_context": "original"})
        self.assertEqual(definition["EnvironmentVariables"]["DOCKER_CONTEXT"], "original")
        with (
            patch("hotdesk.service._job", return_value=(True, None)) as job,
            patch("hotdesk.service.time.monotonic", side_effect=[10, 12]),
            patch("hotdesk.service._launchctl") as run,
        ):
            self.assertTrue(service.start_registered(self.config, timeout=3))
            job.assert_called_once_with(self.config, timeout=3)
            run.assert_called_once_with("kickstart", service._target(self.config), timeout=1)
        with (
            patch("hotdesk.service._job", return_value=(True, None)),
            patch("hotdesk.service.time.monotonic", side_effect=[10, 14]),
            patch("hotdesk.service._launchctl") as run,
        ):
            with self.assertRaises(TimeoutError):
                service.start_registered(self.config, timeout=3)
            run.assert_not_called()

    def test_launchctl_uses_arrays_and_parses_pid(self):
        with patch(
            "hotdesk.service.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, "job = {\n  pid = 42\n}\n", ""),
        ) as run:
            self.assertEqual(service._job(self.config), (True, 42))
            self.assertEqual(run.call_args.args[0][:2], ["/bin/launchctl", "print"])
        with patch("hotdesk.service.sys.platform", "linux"):
            self.assertFalse(service.start_registered(self.config))
            with self.assertRaisesRegex(RuntimeError, "macOS-only"):
                service.status(self.config)


if __name__ == "__main__":
    unittest.main()
