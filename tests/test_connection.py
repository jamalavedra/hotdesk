import asyncio
import contextlib
import io
import json
import os
import socketserver
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from hotdesk.__main__ import main
from hotdesk.config import load_config
from hotdesk.connection import pin_docker_context, project_dir, project_lock, read_connection


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        path = root / "hotdesk.toml"
        path.write_text(
            '[project]\nname="connection-test"\n[profiles.test]\n[desktops.test]\nprofile="test"\n'
        )
        self.config = load_config(path)
        self.state = root / "manager"
        self.state.mkdir()
        self.connection = {"url": "http://127.0.0.1:17890", "token": "test-secret"}
        self.record = self.state / "connection.json"
        self.record.write_text(json.dumps(self.connection))

    def test_discovery_rejects_external_urls_before_sending_credentials(self):
        for url in (
            "https://example.com",
            "http://127.0.0.1:17890@evil.example",
            "http://127.0.0.1:17890/?token=x",
            "http://127.0.0.1:17890/#x",
        ):
            self.record.write_text(json.dumps({**self.connection, "url": url}))
            with (
                patch("hotdesk.connection.project_dir", return_value=self.state),
                patch("hotdesk.connection.httpx.AsyncClient.get") as get,
            ):
                self.assertIsNone(read_connection(self.config))
                get.assert_not_called()

    def test_discovery_health_identity_staleness_and_proxy_bypass(self):
        response = httpx.Response(
            200,
            json={"project": self.config.project, "config": str(self.config.path)},
            request=httpx.Request("GET", self.connection["url"]),
        )
        with (
            patch("hotdesk.connection.project_dir", return_value=self.state),
            patch("hotdesk.connection.httpx.AsyncClient.get", return_value=response) as get,
        ):
            self.assertEqual(read_connection(self.config), self.connection)
            self.assertEqual(
                get.call_args.kwargs["headers"], {"Authorization": "Bearer test-secret"}
            )
            response = httpx.Response(
                200,
                json={"project": self.config.project, "config": "/other/config.toml"},
                request=httpx.Request("GET", self.connection["url"]),
            )
            get.return_value = response
            with self.assertRaisesRegex(RuntimeError, "another configuration"):
                read_connection(self.config)
            get.side_effect = httpx.ConnectError("stale")
            self.assertIsNone(read_connection(self.config))

    def test_open_can_reuse_config_manager_after_context_change(self):
        self.config.state_dir.mkdir(parents=True)
        local = dict(self.connection, token="current", docker_context="desktop-linux")
        (self.config.state_dir / "connection.json").write_text(json.dumps(local))
        stale = httpx.Response(401, request=httpx.Request("GET", self.connection["url"]))
        healthy = httpx.Response(
            200,
            json={"project": self.config.project, "config": str(self.config.path)},
            request=httpx.Request("GET", self.connection["url"]),
        )
        with (
            patch("hotdesk.connection.project_dir", return_value=self.state),
            patch("hotdesk.connection.httpx.AsyncClient.get", return_value=healthy),
        ):
            self.assertEqual(read_connection(self.config, allow_other_context=True), local)
        with (
            patch("hotdesk.connection.project_dir", return_value=self.state),
            patch("hotdesk.connection.httpx.AsyncClient.get", side_effect=[stale, healthy]),
        ):
            with self.assertRaisesRegex(RuntimeError, "DOCKER_CONTEXT=desktop-linux"):
                read_connection(self.config)

    def test_health_deadline_cancels_slow_headers_and_body_without_worker_leaks(self):
        payload = json.dumps(
            {"project": self.config.project, "config": str(self.config.path)}
        ).encode()

        class Handler(socketserver.BaseRequestHandler):
            def handle(handler):
                handler.request.recv(4096)
                headers = (
                    b"HTTP/1.1 200 OK\r\nContent-Length: "
                    + str(len(payload)).encode()
                    + b"\r\n\r\n"
                )
                prefix, slow = (
                    (b"", headers + payload) if handler.server.slow_headers else (headers, payload)
                )
                try:
                    handler.request.sendall(prefix)
                    for value in slow:
                        handler.request.sendall(bytes([value]))
                        time.sleep(0.025)
                except OSError:
                    pass

        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.config.state_dir.mkdir(parents=True)
        connection = dict(self.connection, url=f"http://127.0.0.1:{server.server_address[1]}")
        (self.config.state_dir / "connection.json").write_text(json.dumps(connection))
        try:
            for slow_headers in (True, False):
                server.slow_headers = slow_headers
                for inside_loop in (False, True):

                    def check():
                        started = time.monotonic()
                        self.assertIsNone(
                            read_connection(
                                self.config, allow_other_context=True, deadline=started + 0.15
                            )
                        )
                        self.assertLess(time.monotonic() - started, 0.5)

                    if inside_loop:

                        async def check_async():
                            check()

                        asyncio.run(check_async())
                    else:
                        check()
                    self.assertFalse(
                        any(
                            thread.name.startswith("hotdesk-health")
                            for thread in threading.enumerate()
                        )
                    )
        finally:
            server.shutdown()
            server.server_close()
            worker.join()

    def test_lock_identity_uses_docker_endpoint_and_project(self):
        other_path = self.config.path.parent / "other.toml"
        other_path.write_text(self.config.path.read_text())
        other = load_config(other_path)
        with (
            patch("hotdesk.connection.Path.home", return_value=self.config.path.parent),
            patch.dict(os.environ, {"DOCKER_HOST": "unix:///one.sock"}, clear=True),
        ):
            first = project_dir(self.config)
            self.assertEqual(first, project_dir(other))
            with (
                project_lock(self.config),
                self.assertRaisesRegex(RuntimeError, "already has a manager"),
            ):
                with project_lock(other):
                    self.fail("Duplicate manager acquired the lock")
            with patch.dict(os.environ, {"DOCKER_HOST": "unix:///two.sock"}):
                self.assertNotEqual(first, project_dir(self.config))
            with (
                patch.dict(os.environ, {"DOCKER_CONTEXT": "chosen"}),
                patch(
                    "hotdesk.connection.subprocess.run",
                    return_value=subprocess.CompletedProcess([], 0, "unix:///context.sock\n"),
                ) as run,
            ):
                self.assertNotEqual(first, project_dir(self.config))
                run.assert_called_once()

    def test_context_pin_preserves_overrides_and_config_lock_crosses_engines(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch(
                "hotdesk.connection.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, "desktop-linux\n"),
            ) as run,
        ):
            pin_docker_context()
            self.assertEqual(os.environ["DOCKER_CONTEXT"], "desktop-linux")
            pin_docker_context()
            run.assert_called_once()
        with (
            patch.dict(os.environ, {"DOCKER_HOST": "unix:///explicit.sock"}, clear=True),
            patch("hotdesk.connection.subprocess.run") as run,
        ):
            pin_docker_context()
            run.assert_not_called()
        other_engine = self.state.parent / "other-engine"
        other_engine.mkdir()
        with (
            patch("hotdesk.connection.project_dir", return_value=self.state),
            project_lock(self.config),
        ):
            with (
                patch("hotdesk.connection.project_dir", return_value=other_engine),
                self.assertRaisesRegex(RuntimeError, "already has a manager"),
            ):
                with project_lock(self.config):
                    self.fail("The same SQLite state acquired two managers")

    def test_open_and_serve_reuse_manager_without_browser_or_token(self):
        for command in (["open"], ["serve"]):
            output = io.StringIO()
            with (
                patch("sys.argv", ["hotdesk", "--config", str(self.config.path), *command]),
                patch("hotdesk.__main__.read_connection", return_value=self.connection),
                patch("hotdesk.__main__.ensure_manager", return_value=self.connection),
                patch("hotdesk.__main__.webbrowser.open") as browser,
                patch("hotdesk.__main__.project_lock") as lock,
                contextlib.redirect_stdout(output),
            ):
                main()
                browser.assert_not_called()
                lock.assert_not_called()
            self.assertNotIn("test-secret", output.getvalue())

    def test_doctor_distinguishes_docker_failure_from_stale_manager(self):
        output = io.StringIO()
        with (
            patch("sys.argv", ["hotdesk", "--config", str(self.config.path), "doctor"]),
            patch("hotdesk.__main__.read_connection", return_value=None),
            patch(
                "hotdesk.__main__.subprocess.run",
                return_value=subprocess.CompletedProcess([], 1, "", "not available"),
            ),
            patch("hotdesk.__main__.Runtime.status", side_effect=RuntimeError("Start Docker")),
            contextlib.redirect_stdout(output),
        ):
            main()
        report = json.loads(output.getvalue())
        self.assertIn("Start Docker", report["docker"])
        self.assertIn("stale", report["manager"])
        self.assertIn("Start Docker", report["readiness"])

    def test_checkpoint_clone_and_discard_cli_require_explicit_choices(self):
        for command in (
            ["clone", "test", "copy", "--checkpoint", "saved-id"],
            ["clone", "test", "copy", "--user-approved"],
            ["discard", "copy"],
        ):
            with (
                self.subTest(command=command),
                patch("sys.argv", ["hotdesk", *command]),
                patch("hotdesk.__main__.request") as request,
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as error,
            ):
                main()
            self.assertEqual(error.exception.code, 2)
            request.assert_not_called()
        for command, path, body in (
            (["checkpoint", "test"], "desktops/test/checkpoint", {}),
            (
                ["clone", "test", "copy", "--checkpoint", "saved-id", "--user-approved"],
                "desktops/test/clone",
                {"name": "copy", "checkpoint_id": "saved-id", "user_approved": True},
            ),
            (
                ["discard", "copy", "--outputs-saved"],
                "desktops/copy/discard",
                {"outputs_saved": True},
            ),
        ):
            with (
                self.subTest(command=command),
                patch("sys.argv", ["hotdesk", "--config", str(self.config.path), *command]),
                patch("hotdesk.__main__.read_connection", return_value=self.connection),
                patch("hotdesk.__main__.request", return_value={"ok": True}) as request,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                main()
                request.assert_called_once_with(self.connection, path, body)


if __name__ == "__main__":
    unittest.main()
