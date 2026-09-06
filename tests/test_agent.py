import asyncio
import hashlib
import json
import os
import socket
import stat
import tempfile
import threading
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import uvicorn
from fastmcp import FastMCP
from fastmcp.utilities.types import Image
from sse_starlette import sse

from hotdesk.agent import execute
from hotdesk.server import DeskService, create_app


@asynccontextmanager
async def serve(app, port=0):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", port))
        sock.listen(128)
        server = uvicorn.Server(uvicorn.Config(app, log_level="critical", lifespan="on"))
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            for _ in range(100):
                if server.started:
                    break
                if task.done():
                    await task
                await asyncio.sleep(0.01)
            assert server.started
            yield f"http://127.0.0.1:{sock.getsockname()[1]}"
        finally:
            server.should_exit = True
            await task


class AgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # SSE keeps shutdown watchers per thread; unittest creates a loop per test.
        self.enterContext(patch.object(sse, "_thread_state", threading.local()))
        self.enterContext(patch.object(sse.AppStatus, "should_exit", False))

    async def test_cli_sessions_use_real_mcp_and_preserve_ownership(self):
        backend = FastMCP("agent fixture")
        calls = []
        entered = asyncio.Event()
        finish = asyncio.Event()

        @backend.tool()
        async def browser_echo(value: str) -> dict:
            calls.append(value)
            if value == "wait":
                entered.set()
                await finish.wait()
            if value == "fail":
                raise ValueError("fixture failure after dispatch")
            return {"value": value}

        @backend.tool()
        def computer_screenshot() -> Image:
            return Image(data=b"fixture-image", format="png")

        async with serve(backend.http_app(path="/mcp", stateless_http=True)) as upstream:
            with tempfile.TemporaryDirectory() as directory:
                config = SimpleNamespace(
                    state_dir=Path(directory),
                    path=Path(directory) / "hotdesk.toml",
                    desktops={"alpha": {}, "beta": {}},
                    project="agent-fixture",
                )
                service = DeskService(config)
                service.endpoint = AsyncMock(return_value=upstream)
                service.observed = AsyncMock(
                    return_value=[
                        {"name": "alpha", "status": "running"},
                        {"name": "beta", "status": "running"},
                    ]
                )
                service.runtime.gateway_token = Mock(return_value="fixture-guest-secret")
                service.runtime.set_viewer_control = Mock()
                with socket.socket() as sock:
                    sock.bind(("127.0.0.1", 0))
                    port = sock.getsockname()[1]
                async with serve(create_app(config, "admin", port, service), port) as url:
                    connection = {"url": url, "token": "admin"}
                    with (
                        patch("hotdesk.agent.ensure_manager", return_value=connection),
                        patch.dict(
                            os.environ,
                            {"HTTP_PROXY": "http://127.0.0.1:1", "NO_PROXY": ""},
                        ),
                    ):
                        listing = await execute(config, "research", "alpha", "tools")
                        self.assertIn("browser_echo", json.dumps(listing))
                        self.assertEqual(service.state.row("alpha")["state"], "idle")
                        first = await execute(
                            config, "research", "alpha", "call", "browser_echo", {"value": "first"}
                        )
                        self.assertIn("first", json.dumps(first))
                        record = (
                            config.state_dir
                            / "agent-sessions"
                            / (hashlib.sha256(b"research").hexdigest()[:24] + ".json")
                        )
                        self.assertEqual(stat.S_IMODE(record.stat().st_mode), 0o600)
                        self.assertEqual(stat.S_IMODE(record.parent.stat().st_mode), 0o700)
                        with self.assertRaises(ValueError):
                            await execute(config, "research", "beta", "tools")
                        generation = service.state.row("alpha")["generation"]
                        await execute(
                            config, "research", "alpha", "call", "browser_echo", {"value": "second"}
                        )
                        self.assertGreater(service.state.row("alpha")["generation"], generation)
                        self.assertEqual(
                            service.state.db.execute("SELECT COUNT(*) FROM agents").fetchone()[0], 1
                        )
                        shot = await execute(
                            config, "research", "alpha", "call", "computer_screenshot", {}
                        )
                        self.assertIn("image/png", json.dumps(shot))
                        self.assertNotIn("admin", json.dumps(shot))
                        image = next(block for block in shot["content"] if block["type"] == "image")
                        self.assertNotIn("data", image)
                        self.assertEqual(Path(image["path"]).read_bytes(), b"fixture-image")
                        blocked = await execute(
                            config, "other", "alpha", "call", "browser_echo", {"value": "blocked"}
                        )
                        self.assertTrue(blocked["isError"])
                        self.assertEqual(calls, ["first", "second"])
                        await execute(config, "research", "alpha", "release")
                        self.assertEqual(service.state.row("alpha")["state"], "idle")
                        self.assertFalse(record.exists())
                        await execute(
                            config, "other", "alpha", "call", "browser_echo", {"value": "available"}
                        )
                        await execute(config, "other", "alpha", "release")
                        self.assertEqual(calls, ["first", "second", "available"])
                        cached = (
                            config.state_dir
                            / "agent-sessions"
                            / (hashlib.sha256(b"rotation").hexdigest()[:24] + ".json")
                        )
                        await execute(config, "rotation", "beta", "tools")
                        previous = json.loads(cached.read_text())
                        previous["manager"] = "previous-manager-fingerprint"
                        cached.write_text(json.dumps(previous))
                        await execute(config, "rotation", None, "tools")
                        self.assertNotEqual(
                            json.loads(cached.read_text())["token"], previous["token"]
                        )
                        running = asyncio.create_task(
                            execute(
                                config, "rotation", None, "call", "browser_echo", {"value": "wait"}
                            )
                        )
                        try:
                            await asyncio.wait_for(entered.wait(), 5)
                            with self.assertRaisesRegex(RuntimeError, "command running"):
                                await execute(config, "rotation", None, "tools")
                        finally:
                            finish.set()
                            await running
                        await execute(config, "rotation", None, "release")
                        await service.control("alpha", "human")
                        human = await execute(
                            config,
                            "research",
                            "alpha",
                            "call",
                            "browser_echo",
                            {"value": "human-blocked"},
                        )
                        self.assertTrue(human["isError"])
                        self.assertNotIn("human-blocked", calls)
                        await service.control("alpha", "agent")
                        failed = await execute(
                            config, "research", "alpha", "call", "browser_echo", {"value": "fail"}
                        )
                        self.assertTrue(failed["isError"])
                        self.assertIn("fixture failure after dispatch", json.dumps(failed))
                        self.assertEqual(calls.count("fail"), 1)
                        self.assertEqual(service.state.row("alpha")["state"], "reserved")
                        self.assertEqual(
                            service.state.history()["operations"][0]["outcome"], "failed"
                        )
                        inspected = await execute(
                            config,
                            "research",
                            "alpha",
                            "call",
                            "browser_echo",
                            {"value": "inspect"},
                        )
                        self.assertFalse(inspected["isError"])
                        self.assertIn("inspect", json.dumps(inspected))
                        self.assertEqual(calls[-2:], ["fail", "inspect"])
                        await execute(config, "research", "alpha", "release")
