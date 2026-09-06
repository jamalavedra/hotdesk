import asyncio
import os
import socket
import tempfile
import unittest
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import uvicorn
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server import create_proxy
from fastmcp.utilities.types import Image

from hotdesk.server import DeskService, create_app


@asynccontextmanager
async def serve(app, port=None):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", port or 0))
        sock.listen(128)
        port = sock.getsockname()[1]
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
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
            await task


class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_http_proxy_scope_images_and_takeover(self):
        backend = FastMCP("fixture")
        calls = []

        @backend.tool()
        def computer_echo(value: str) -> dict:
            calls.append(value)
            return {"value": value}

        @backend.tool()
        def computer_screenshot() -> Image:
            return Image(data=b"fixture-image", format="png")

        @backend.resource("fixture://status")
        def status() -> str:
            return "ready"

        async with serve(backend.http_app(path="/mcp", stateless_http=True)) as upstream:
            with tempfile.TemporaryDirectory() as directory:
                config = SimpleNamespace(
                    state_dir=Path(directory),
                    desktops={"alpha": {}, "beta": {}},
                    project="fixture",
                    path=Path(directory) / "hotdesk.toml",
                )
                service = DeskService(config)
                service.endpoint = AsyncMock(return_value=upstream)
                service.observed = AsyncMock(return_value=[{"name": "alpha", "status": "running"}])
                service.runtime.gateway_token = Mock(return_value="guest-secret")
                service.runtime.set_viewer_control = Mock()
                service.runtime.checkpoint_info = Mock(return_value=None)
                with socket.socket() as reservation:
                    reservation.bind(("127.0.0.1", 0))
                    port = reservation.getsockname()[1]
                app = create_app(config, "admin", port, service)
                async with serve(app, port) as url:
                    async with httpx.AsyncClient(
                        base_url=url, headers={"Authorization": "Bearer admin"}
                    ) as http:
                        access = (
                            await http.post(
                                "/api/agents", json={"workspace": "alpha", "owner": "test"}
                            )
                        ).json()
                        transport = StreamableHttpTransport(
                            access["url"],
                            headers={"Authorization": "Bearer " + access["token"]},
                            httpx_client_factory=partial(httpx.AsyncClient, trust_env=False),
                        )
                        proxy = create_proxy(transport)
                        async with Client(proxy) as client:
                            names = {tool.name for tool in await client.list_tools()}
                            self.assertIn("workspace_acquire", names)
                            self.assertIn("computer_screenshot", names)
                            with self.assertRaises(Exception):
                                await client.call_tool("computer_echo", {"value": "blocked"})
                            self.assertEqual(calls, [])
                            await client.call_tool("workspace_acquire")
                            with patch.dict(
                                os.environ,
                                {
                                    "HTTP_PROXY": "http://127.0.0.1:1",
                                    "ALL_PROXY": "http://127.0.0.1:1",
                                    "NO_PROXY": "",
                                },
                            ):
                                result = await client.call_tool(
                                    "computer_echo", {"value": "preserved"}
                                )
                            self.assertEqual(result.structured_content, {"value": "preserved"})
                            shot = await client.call_tool("computer_screenshot")
                            self.assertEqual(shot.content[0].mimeType, "image/png")
                            resource = await client.read_resource("fixture://status")
                            self.assertEqual(resource[0].text, "ready")
                            response = await http.post(
                                "/api/desktops/alpha/control", json={"control": "human"}
                            )
                            self.assertEqual(response.status_code, 200)
                            async with Client(transport) as direct:
                                busy = await direct.call_tool(
                                    "workspace_acquire", raise_on_error=False
                                )
                                self.assertTrue(busy.is_error)
                                self.assertEqual(busy.structured_content["code"], "WORKSPACE_BUSY")
                            with self.assertRaises(Exception):
                                await client.call_tool("computer_echo", {"value": "forbidden"})
                            self.assertEqual(calls, ["preserved"])
                            await http.post(
                                "/api/desktops/alpha/control", json={"control": "agent"}
                            )
                            with self.assertRaises(Exception):
                                await client.call_tool("computer_echo", {"value": "stale"})
                            await client.call_tool("workspace_acquire")
                            await client.call_tool("computer_echo", {"value": "resumed"})
                            await client.call_tool("workspace_release")
                        denied = await http.get(
                            "/mcp/beta/", headers={"Authorization": "Bearer " + access["token"]}
                        )
                        self.assertEqual(denied.status_code, 401)
