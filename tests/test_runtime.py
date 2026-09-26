import contextlib
import io
import json
import tarfile
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from hotdesk.config import load_config, render_compose
from hotdesk.runtime import CapacityError, Runtime


class RuntimeTests(unittest.TestCase):
    def make_checkpoint(self):
        manifest = {
            "workspace": "one",
            "created_at": "2026-09-06T12:00:00+00:00",
            "image_id": "sha256:" + "a" * 64,
        }

        def backup(name, path):
            Path(path).write_bytes(b"checkpoint")
            return manifest

        with patch.object(self.runtime, "backup", side_effect=backup):
            return self.runtime.checkpoint("one")

    def test_clone_requires_checkpoint_and_preserves_source(self):
        with self.assertRaisesRegex(ValueError, "No checkpoint"):
            self.runtime.clone("one", "copy", "a" * 32)
        checkpoint = self.make_checkpoint()
        original = self.runtime.config
        with self.assertRaisesRegex(ValueError, "checkpoint changed"):
            self.runtime.clone("one", "copy", "b" * 32)
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.runtime.clone("one", "two", checkpoint["id"])
        with (
            patch.object(self.runtime, "validate_start") as validate,
            patch.object(self.runtime, "restore") as restore,
            patch.object(self.runtime, "up") as up,
        ):
            result = self.runtime.clone("one", "copy", checkpoint["id"])
        validate.assert_called_once_with("copy")
        restore.assert_called_once_with("copy", checkpoint["path"])
        up.assert_called_once_with(build=False, name="copy")
        self.assertEqual(result["source"], "one")
        self.assertEqual(self.runtime.config.desktops["one"], original.desktops["one"])
        self.assertEqual(self.runtime.config.profiles["one"], original.profiles["one"])
        self.assertNotEqual(self.runtime.volume_name("copy"), self.runtime.volume_name("one"))
        restarted = load_config(original.path)
        self.assertEqual(restarted.clones, self.runtime.config.clones)
        service = render_compose(restarted)["services"]["desktop-copy"]
        self.assertEqual(service["image"], checkpoint["image_id"])
        self.assertNotIn("build", service)
        self.assertEqual((original.state_dir / "clones.json").stat().st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(ValueError, "not a temporary clone"):
            self.runtime.discard_clone("one")
        with (
            patch.object(self.runtime, "stop") as stop,
            patch.object(self.runtime, "_compose") as compose,
        ):
            self.runtime.discard_clone("copy")
        stop.assert_called_once_with("copy")
        compose.assert_called_once_with("rm", "--force", "desktop-copy", timeout=180)
        self.assertNotIn("copy", load_config(original.path).desktops)
        self.assertTrue(Path(checkpoint["path"]).exists())

    def test_checkpoint_replaces_previous_archive(self):
        previous = self.make_checkpoint()
        current = self.make_checkpoint()
        self.assertFalse(Path(previous["path"]).exists())
        self.assertEqual(self.runtime.checkpoint_info("one"), current)

    def test_clone_rejects_retained_original_resources(self):
        checkpoint = self.make_checkpoint()
        volume = SimpleNamespace(
            name="old-original-home",
            attrs={
                "Labels": {
                    "io.hotdesk.config": self.runtime.config.provenance,
                    "io.hotdesk.profile": "copy",
                }
            },
        )
        network = SimpleNamespace(name="runtime-test_desktop-copy", attrs={})
        scenarios = (
            (self.client.volumes, [volume]),
            (self.client.volumes, [SimpleNamespace(name="runtime-test_home-copy", attrs={})]),
            (self.client.containers, [self.container("copy", "exited")]),
            (self.client.networks, [network]),
        )
        for listing, resources in scenarios:
            with (
                self.subTest(resources=resources),
                patch.object(listing, "list", return_value=resources),
            ):
                with self.assertRaisesRegex(ValueError, "retained resources"):
                    self.runtime.clone("one", "copy", checkpoint["id"])
                self.assertNotIn("copy", self.runtime.config.desktops)
                self.assertFalse((self.runtime.config.state_dir / "clones.json").exists())
        with patch.object(self.runtime, "_volume_overrides", return_value={"copy": "old-home"}):
            with self.assertRaisesRegex(ValueError, "retained resources"):
                self.runtime.clone("one", "copy", checkpoint["id"])

    def test_failed_discard_cannot_restart_a_partial_home(self):
        checkpoint = self.make_checkpoint()
        with (
            patch.object(self.runtime, "validate_start"),
            patch.object(self.runtime, "restore"),
            patch.object(self.runtime, "up"),
        ):
            self.runtime.clone("one", "copy", checkpoint["id"])
        network = MagicMock()
        network.attrs = {"Labels": {"io.hotdesk.config": "another-project"}}
        self.client.networks.list.return_value = [network]
        with (
            patch.object(self.runtime, "stop"),
            patch.object(self.runtime, "_compose"),
            self.assertRaisesRegex(RuntimeError, "network ownership"),
        ):
            self.runtime.discard_clone("copy")
        network.remove.assert_not_called()
        restarted = Runtime(load_config(self.runtime.config.path))
        with self.assertRaisesRegex(ValueError, "did not finish restoring"):
            restarted.validate_start("copy")

    def test_failed_clone_cannot_start_empty_profile(self):
        checkpoint = self.make_checkpoint()
        with (
            patch.object(self.runtime, "validate_start"),
            patch.object(self.runtime, "restore", side_effect=RuntimeError("extraction failed")),
            self.assertRaisesRegex(RuntimeError, "extraction failed"),
        ):
            self.runtime.clone("one", "copy", checkpoint["id"])
        restarted = Runtime(load_config(self.runtime.config.path))
        self.assertFalse(restarted.clone_info("copy")["ready"])
        with self.assertRaisesRegex(ValueError, "did not finish restoring"):
            restarted.validate_start("copy")
        with self.assertRaisesRegex(ValueError, "did not finish restoring"):
            restarted.validate_start()

    def test_discard_retains_registration_when_checkpoint_cleanup_fails(self):
        checkpoint = self.make_checkpoint()
        with (
            patch.object(self.runtime, "validate_start"),
            patch.object(self.runtime, "restore"),
            patch.object(self.runtime, "up"),
        ):
            self.runtime.clone("one", "copy", checkpoint["id"])
        directory = self.runtime.config.state_dir / "checkpoints" / "copy"
        directory.mkdir()
        archive = directory / "checkpoint.tar"
        archive.write_bytes(b"private profile")
        with (
            patch.object(self.runtime, "stop"),
            patch.object(self.runtime, "_compose"),
            patch("hotdesk.runtime.shutil.rmtree", side_effect=PermissionError("denied")),
            self.assertRaises(PermissionError),
        ):
            self.runtime.discard_clone("copy")
        self.assertTrue(archive.exists())
        self.assertFalse(load_config(self.runtime.config.path).clones["copy"]["ready"])
        with patch.object(self.runtime, "stop"), patch.object(self.runtime, "_compose"):
            self.runtime.discard_clone("copy")
        self.assertFalse(directory.exists())
        self.assertNotIn("copy", load_config(self.runtime.config.path).clones)

    def test_capacity_failure_does_not_register_clone(self):
        checkpoint = self.make_checkpoint()
        with (
            patch.object(self.runtime, "validate_start", side_effect=CapacityError("full")),
            self.assertRaisesRegex(CapacityError, "full"),
        ):
            self.runtime.clone("one", "copy", checkpoint["id"])
        self.assertNotIn("copy", self.runtime.config.desktops)
        self.assertFalse((self.runtime.config.state_dir / "clones.json").exists())

    def test_checkpoint_metadata_rejects_path_escape(self):
        checkpoint = self.make_checkpoint()
        pointer = Path(checkpoint["path"]).parent / "latest.json"
        checkpoint["id"] = "../escape"
        pointer.write_text(json.dumps(checkpoint))
        with self.assertRaisesRegex(ValueError, "Invalid checkpoint"):
            self.runtime.checkpoint_info("one")

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        path = Path(self.directory.name) / "hotdesk.toml"
        path.write_text(
            '[project]\nname="runtime-test"\nmax_running=1\nmemory_budget="3g"\n[profiles.one]\n[profiles.two]\n[desktops.one]\nprofile="one"\n[desktops.two]\nprofile="two"\n'
        )
        self.runtime = Runtime(load_config(path))
        self.client = MagicMock()
        self.client.containers.list.return_value = []
        self.client.volumes.list.return_value = []
        self.client.info.return_value = {"MemTotal": 8 * 1024**3, "NCPU": 4}
        mock = patch.object(self.runtime, "_client", lambda: contextlib.nullcontext(self.client))
        mock.start()
        self.addCleanup(mock.stop)

    def container(self, name="one", status="running"):
        item = MagicMock()
        item.id = name
        item.labels = {
            "com.docker.compose.service": f"desktop-{name}",
            "io.hotdesk.config": self.runtime.config.provenance,
        }
        item.attrs = {
            "Id": name,
            "Image": "sha256:" + "a" * 64,
            "State": {
                "Status": status,
                "OOMKilled": False,
                "Health": {
                    "Status": "healthy",
                    "Log": [{"Output": json.dumps({"browser": "ready", "chromium": "failed"})}],
                },
            },
            "NetworkSettings": {
                "Ports": {"8001/tcp": [{"HostIp": "127.0.0.1", "HostPort": "12345"}]}
            },
        }
        item.stats.return_value = {
            "memory_stats": {"usage": 1000, "stats": {"inactive_file": 100}},
            "cpu_stats": {},
            "precpu_stats": {},
        }
        return item

    def test_observation_never_writes_credentials_or_compose(self):
        self.client.containers.list.return_value = [self.container()]
        with patch.object(
            self.runtime, "write_compose", side_effect=AssertionError("observation wrote config")
        ):
            row = self.runtime.status()[0]
            self.assertEqual(row["computer_url"], "http://127.0.0.1:12345/computer")
            self.assertEqual(row["memory_usage"], 900)
            self.assertEqual(row["components"]["chromium"], "failed")
            self.assertNotIn("valet", row["components"])
            self.assertNotIn("password", row["viewer_url"])
            self.assertFalse(self.runtime.config.state_dir.exists())

    def test_valet_component_appears_only_when_the_desktop_reports_it(self):
        item = self.container()
        item.attrs["State"]["Health"]["Log"] = [{"Output": json.dumps({"valet": "failed"})}]
        self.client.containers.list.return_value = [item]
        row = self.runtime.status()[0]
        self.assertEqual(row["components"]["valet"], "failed")
        self.assertEqual(row["valet_url"], "http://127.0.0.1:12345/valet")

    def test_sdk_and_cli_pin_context_even_when_global_selection_changes(self):
        context = [{"Endpoints": {"docker": {"Host": "unix:///chosen.sock"}}}]
        with (
            patch.dict(
                "os.environ",
                {"DOCKER_CONTEXT": "chosen", "DOCKER_HOST": "unix:///wrong.sock"},
                clear=True,
            ),
            patch(
                "hotdesk.runtime.subprocess.check_output", return_value=json.dumps(context)
            ) as inspect,
            patch("hotdesk.runtime.docker.from_env", return_value=self.client) as connect,
            patch(
                "hotdesk.runtime.subprocess.run",
                return_value=SimpleNamespace(returncode=0, stdout="ok", stderr=""),
            ) as command,
        ):
            runtime = Runtime(self.runtime.config)
            with runtime._client() as client:
                self.assertIs(client, self.client)
            environment = connect.call_args.kwargs["environment"]
            self.assertEqual(environment["DOCKER_HOST"], "unix:///chosen.sock")
            self.assertNotIn("DOCKER_CONTEXT", environment)
            inspect.return_value = json.dumps(
                [{"Endpoints": {"docker": {"Host": "unix:///other.sock"}}}]
            )
            runtime._compose("ps")
            self.assertEqual(command.call_args.kwargs["env"]["DOCKER_HOST"], "unix:///chosen.sock")
            self.assertEqual(inspect.call_count, 1)
            self.client.close.assert_called_once()

    def test_capacity_and_scoped_lifecycle(self):
        with patch.object(self.runtime, "_compose") as compose:
            with self.assertRaisesRegex(CapacityError, "Workspace limit"):
                self.runtime.up(build=False)
            compose.assert_not_called()
            compose.side_effect = lambda *args, **kwargs: setattr(
                self.client.containers.list, "return_value", [self.container()]
            )
            self.runtime.up(build=False, name="one")
            self.assertIn("desktop-one", compose.call_args.args)
            self.assertIn("--no-build", compose.call_args.args)
            self.runtime.stop("one")
            self.assertEqual(compose.call_args.args, ("stop", "desktop-one"))
            self.runtime.down()
            self.assertNotIn("--volumes", compose.call_args.args)
        self.client.containers.list.return_value = [self.container()]
        with patch.object(self.runtime, "_compose") as compose:
            with self.assertRaisesRegex(CapacityError, "Workspace limit"):
                self.runtime.up(name="two")
            compose.assert_not_called()
        self.client.volumes.list.return_value = [
            SimpleNamespace(
                name="old-home",
                attrs={"Labels": {"io.hotdesk.config": self.runtime.config.provenance}},
            )
        ]
        self.assertEqual(self.runtime.capacity()["retained_volumes"], ["old-home"])

    def test_start_checks_actual_health_after_compose_returns_success(self):
        item = self.container()
        item.attrs["State"]["Health"]["Status"] = "starting"
        self.client.containers.list.return_value = [item]
        with patch.object(self.runtime, "_compose", return_value=""):
            with self.assertRaisesRegex(RuntimeError, "did not become healthy"):
                self.runtime.up(build=False, name="one")

    def test_provenance_and_stopped_backup(self):
        item = self.container()
        item.labels["io.hotdesk.config"] = "another-config"
        self.client.containers.list.return_value = [item]
        with self.assertRaisesRegex(RuntimeError, "another configuration"):
            self.runtime.status()
        item.labels["io.hotdesk.config"] = self.runtime.config.provenance
        with self.assertRaisesRegex(RuntimeError, "Stop the workspace"):
            self.runtime.backup("one", Path(self.directory.name) / "backup.tar")

    def test_memory_increases_and_other_docker_workloads_obey_capacity(self):
        item = self.container()
        item.attrs["HostConfig"] = {"Memory": 2 * 1024**3}
        self.client.containers.list.return_value = [item]
        self.runtime.config.profiles["one"]["memory"] = "4g"
        with patch.object(self.runtime, "_compose") as compose:
            with self.assertRaisesRegex(CapacityError, "Memory budget"):
                self.runtime.up(name="one")
            compose.assert_not_called()
        self.runtime.config.profiles["one"]["memory"] = "2g"
        other = self.container("unrelated")
        other.stats.return_value = {"memory_stats": {"usage": 7 * 1024**3}}
        self.client.containers.list.side_effect = lambda **kwargs: [] if kwargs else [other]
        with patch.object(self.runtime, "_compose") as compose:
            with self.assertRaisesRegex(CapacityError, "insufficient free"):
                self.runtime.up(name="one")
            compose.assert_not_called()

    def test_scoped_start_budgets_untouched_container_limits(self):
        self.runtime.config = replace(self.runtime.config, max_running=2)
        item = self.container()
        item.attrs["HostConfig"] = {"Memory": 2 * 1024**3}
        self.client.containers.list.return_value = [item]
        self.runtime.config.profiles["one"]["memory"] = "1g"
        with patch.object(self.runtime, "_compose") as compose:
            with self.assertRaisesRegex(CapacityError, "Memory budget"):
                self.runtime.up(name="two")
            compose.assert_not_called()
        self.runtime.validate_start()
        item.attrs["HostConfig"]["Memory"] = 1024**3
        self.runtime.config.profiles["one"]["memory"] = "2g"
        self.runtime.validate_start("two")

    def test_only_local_docker_endpoints_are_supported(self):
        for endpoint in (
            "unix:///var/run/docker.sock",
            "tcp://127.0.0.1:2375",
            "https://localhost:2376",
        ):
            self.runtime._local_endpoint(endpoint)
        for endpoint in ("ssh://example.com", "tcp://192.168.1.20:2375"):
            with self.assertRaisesRegex(RuntimeError, "remote Docker"):
                self.runtime._local_endpoint(endpoint)

    def test_restore_normalizes_internal_home_links(self):
        data, normalized = io.BytesIO(), io.BytesIO()
        with tarfile.open(fileobj=data, mode="w") as archive:
            entry = tarfile.TarInfo(".config/xfce4/desktop/latest")
            entry.type = tarfile.SYMTYPE
            entry.linkname = "/home/cua/.config/xfce4/desktop/icons"
            archive.addfile(entry)
            entry = tarfile.TarInfo(".config/shared")
            entry.type = tarfile.SYMTYPE
            entry.linkname = "../shared"
            archive.addfile(entry)
        self.runtime._validate_home(data, normalized)
        with tarfile.open(fileobj=normalized) as archive:
            self.assertEqual(archive.getmember(".config/xfce4/desktop/latest").linkname, "icons")
            self.assertEqual(archive.getmember(".config/shared").linkname, "../shared")

    def test_archive_rejects_traversal_links_and_devices(self):
        for name, kind, link in (
            ("../escape", tarfile.REGTYPE, ""),
            ("/absolute", tarfile.REGTYPE, ""),
            ("link", tarfile.SYMTYPE, "/etc/passwd"),
            ("link", tarfile.LNKTYPE, "../outside"),
            ("device", tarfile.CHRTYPE, ""),
        ):
            data = io.BytesIO()
            with tarfile.open(fileobj=data, mode="w") as archive:
                entry = tarfile.TarInfo(name)
                entry.type, entry.linkname = kind, link
                archive.addfile(entry)
            with self.assertRaises(ValueError):
                self.runtime._validate_home(data, io.BytesIO())
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w") as archive:
            archive.addfile(tarfile.TarInfo(".config/safe"))
        self.runtime._validate_home(data, io.BytesIO())


if __name__ == "__main__":
    unittest.main()
