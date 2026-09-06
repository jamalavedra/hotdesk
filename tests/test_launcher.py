import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from contextlib import chdir
from pathlib import Path
from unittest.mock import Mock, patch

import httpx

from hotdesk.__main__ import open_workspace, stop_manager
from hotdesk.config import load_config
from hotdesk.connection import ensure_manager, select_config
from hotdesk.server import DeskService, create_app, save_json


class LauncherTests(unittest.TestCase):
    def test_direct_viewer_starts_takes_control_and_observes(self):
        connection = {"url": "http://127.0.0.1:7890", "token": "secret"}
        for observe in (False, True):
            with (
                patch(
                    "hotdesk.__main__.request",
                    side_effect=[[{"name": "test", "status": "stopped"}], {}, {}],
                ) as request,
                patch("hotdesk.__main__.webbrowser.open", return_value=True) as browser,
            ):
                open_workspace(connection, "test", observe)
                self.assertEqual(
                    request.call_args_list[1].args,
                    (connection, "desktops/test/lifecycle", {"action": "start"}),
                )
                self.assertEqual(request.call_count, 2 if observe else 3)
                if not observe:
                    self.assertEqual(
                        request.call_args.args,
                        (connection, "desktops/test/control", {"control": "human"}),
                    )
                browser.assert_called_once_with(
                    connection["url"]
                    + "/viewer/test/?view_only="
                    + ("true" if observe else "false")
                    + "#token=secret"
                )

    def test_selection_and_browser_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch("hotdesk.connection.Path.home", return_value=root),
                chdir(root),
            ):
                self.assertEqual(
                    select_config("/missing/explicit.toml"), Path("/missing/explicit.toml")
                )
                with self.assertRaisesRegex(ValueError, "Choose a configuration"):
                    select_config()
                saved = root / "chosen.toml"
                save_json(root / ".local/state/hotdesk/default.json", {"config": str(saved)})
                self.assertEqual(select_config(), saved)
                local = root / "hotdesk.toml"
                local.write_text("invalid toml [")
                self.assertEqual(select_config(), Path("hotdesk.toml"))
                with self.assertRaises(ValueError):
                    load_config(select_config())
                local.unlink()
                local.symlink_to(root / "missing.toml")
                self.assertEqual(select_config(), Path("hotdesk.toml"))
                with self.assertRaises(OSError):
                    load_config(select_config())
                with (
                    patch("hotdesk.__main__.webbrowser.open", return_value=False),
                    patch(
                        "hotdesk.__main__.request",
                        return_value=[{"name": "test", "status": "running"}],
                    ),
                ):
                    with self.assertRaisesRegex(RuntimeError, "Browser could not"):
                        open_workspace({"url": "http://127.0.0.1:7890", "token": "secret"}, "test")

    def test_cold_parallel_launch_port_conflict_and_shutdown(self):
        with tempfile.TemporaryDirectory(prefix="hotdesk launcher ") as directory:
            root = Path(directory)
            path = root / "hotdesk.toml"
            path.write_text(
                '[project]\nname="launcher-test"\n[profiles.test]\n[desktops.test]\nprofile="test"\n'
            )
            config = load_config(path)
            env = dict(os.environ, DOCKER_HOST="unix://" + str(root / "absent.sock"))
            env.pop("DOCKER_CONTEXT", None)
            code = (
                "import json,sys; from hotdesk.config import load_config; "
                "from hotdesk.connection import ensure_manager; "
                "c=ensure_manager(load_config(sys.argv[1])); "
                'print(json.dumps({k:c[k] for k in ("url","pid")}))'
            )
            holder = socket.socket()
            try:
                holder.bind(("127.0.0.1", 7890))
                holder.listen()
            except OSError:
                holder.close()
            clients = []
            connection = None
            try:
                with patch.dict(os.environ, env, clear=True):
                    with self.assertRaisesRegex(RuntimeError, "Manager exited"):
                        ensure_manager(config, port=7890)
                self.assertFalse((config.state_dir / "connection.json").exists())
                for _ in range(5):
                    clients.append(
                        subprocess.Popen(
                            [sys.executable, "-c", code, str(path)],
                            cwd=root,
                            env=env,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            text=True,
                        )
                    )
                results = []
                for client in clients:
                    out, err = client.communicate(timeout=40)
                    self.assertEqual(client.returncode, 0, err)
                    results.append(json.loads(out))
                self.assertTrue(all(result == results[0] for result in results))
                self.assertNotIn(":7890", results[0]["url"])
                connection = json.loads((config.state_dir / "connection.json").read_text())
                self.assertEqual((config.state_dir / "manager.log").stat().st_mode & 0o777, 0o600)
                with patch.dict(os.environ, env, clear=True):
                    self.assertEqual(ensure_manager(config)["pid"], results[0]["pid"])
                with httpx.Client(base_url=connection["url"], trust_env=False) as client:
                    self.assertEqual(client.post("/api/shutdown", json={}).status_code, 401)
                    response = client.post(
                        "/api/shutdown",
                        json={},
                        headers={"Authorization": "Bearer " + connection["token"]},
                    )
                    self.assertEqual(response.status_code, 200, response.text)
                import time

                for _ in range(100):
                    if not (config.state_dir / "connection.json").exists():
                        break
                    time.sleep(0.05)
                self.assertFalse((config.state_dir / "connection.json").exists())

                cold = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "hotdesk",
                        "--config",
                        str(config.path),
                        "agent",
                        "cold-agent",
                        "--workspace",
                        "test",
                        "call",
                        "workspace_status",
                    ],
                    cwd=root,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=40,
                )
                self.assertEqual(cold.returncode, 1)
                self.assertTrue(json.loads(cold.stdout)["isError"])
                self.assertIn("Cannot connect to Docker", cold.stdout)
                with patch.dict(os.environ, env, clear=True):
                    with self.assertRaisesRegex(RuntimeError, "another port"):
                        ensure_manager(config, port=7890)
                    stop_manager(config)
                self.assertFalse((config.state_dir / "connection.json").exists())
            finally:
                holder.close()
                for client in clients:
                    if client.poll() is None:
                        client.terminate()
                    client.wait(timeout=5)
                # This fixture owns this exact child, even if a startup assertion failed.
                record = config.state_dir / "connection.json"
                if record.exists():
                    import signal
                    import time

                    connection = json.loads(record.read_text())
                    for _ in range(50):
                        if not record.exists():
                            break
                        time.sleep(0.1)
                    else:
                        os.kill(connection["pid"], signal.SIGTERM)


class ShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_busy_shutdown_and_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hotdesk.toml"
            path.write_text(
                '[project]\nname="shutdown-test"\n[profiles.test]\n[desktops.test]\nprofile="test"\n'
            )
            config = load_config(path)
            service = DeskService(config)
            shutdown = Mock()
            app = create_app(config, "secret", 7890, service=service, shutdown=shutdown)
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://127.0.0.1:7890",
                    headers={"Authorization": "Bearer secret"},
                ) as client:
                    for state in ("human", "recovery", "takeover"):
                        service.state.set_mode("test", state)
                        self.assertEqual(
                            (await client.post("/api/shutdown", json={})).status_code, 409
                        )
                    service.state.set_mode("test", "idle")
                    token = service.state.register("test", "busy-agent", "work")
                    agent = service.state.authenticate(token, "test")
                    service.state.acquire(agent)
                    self.assertEqual((await client.post("/api/shutdown", json={})).status_code, 409)
                    service.state.release(agent)
                    service.active_requests = 1
                    self.assertEqual((await client.post("/api/shutdown", json={})).status_code, 409)
                    service.active_requests = 0
                    shutdown.assert_not_called()
                    self.assertEqual((await client.post("/api/shutdown", json={})).status_code, 200)
                    shutdown.assert_called_once()
                    self.assertEqual(
                        (
                            await client.post(
                                "/api/agents", json={"workspace": "test", "owner": "late"}
                            )
                        ).status_code,
                        503,
                    )
            finally:
                service.state.db.close()
