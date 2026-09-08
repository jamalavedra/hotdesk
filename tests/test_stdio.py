import asyncio
import base64
import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastmcp import FastMCP
from fastmcp.utilities.types import Image
from mcp.shared.memory import create_connected_server_and_client_session as connect
from mcp.types import Implementation, ServerNotification, ToolListChangedNotification
from sse_starlette import sse
from test_mcp import serve

from hotdesk.mcp import create_server
from hotdesk.server import DeskService, create_app


class StdioTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.object(sse, "_thread_state", threading.local()))
        self.enterContext(patch.object(sse.AppStatus, "should_exit", False))

    async def test_tools_images_ownership_errors_reconnect_and_disconnect(self):
        backend = FastMCP("stdio fixture")
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
                    project="stdio-fixture",
                )
                service = DeskService(config)
                service.endpoint = AsyncMock(return_value=upstream)
                service.observed = AsyncMock(return_value=[{"name": "alpha", "status": "running"}])
                service.runtime.gateway_token = Mock(return_value="fixture-guest-secret")
                service.runtime.set_viewer_control = Mock()
                service.runtime.checkpoint_info = Mock(return_value=None)
                with socket.socket() as sock:
                    sock.bind(("127.0.0.1", 0))
                    port = sock.getsockname()[1]
                async with serve(create_app(config, "admin", port, service), port) as url:
                    connection = {"url": url, "token": "admin"}
                    with patch("hotdesk.mcp.ensure_manager", return_value=connection):
                        with self.assertRaises(ValueError):
                            create_server(config, "missing")
                        with patch(
                            "hotdesk.mcp.httpx.AsyncClient.post",
                            return_value=httpx.Response(503, text="unavailable"),
                        ):
                            async with connect(create_server(config, "alpha")) as unavailable:
                                error = await unavailable.call_tool("workspace_status")
                                self.assertTrue(error.isError)
                                self.assertIn("Service Unavailable", error.content[0].text)
                        stale_config = SimpleNamespace(
                            **(vars(config) | {"desktops": {"removed": {}}})
                        )
                        async with connect(create_server(stale_config, "removed")) as stale:
                            error = await stale.call_tool("workspace_status")
                            self.assertTrue(error.isError)
                            self.assertIn("Unknown workspace", error.content[0].text)
                        refreshes = []
                        notified = asyncio.Event()

                        async def notification(message):
                            if isinstance(message, ServerNotification) and isinstance(
                                message.root, ToolListChangedNotification
                            ):
                                refreshes.append(asyncio.create_task(client.list_tools()))
                                notified.set()

                        async with connect(
                            create_server(config, "alpha"),
                            message_handler=notification,
                            client_info=Implementation(name="review-client", version="1"),
                        ) as client:
                            listing = await client.list_tools()
                            echo = next(t for t in listing.tools if t.name == "browser_echo")
                            self.assertEqual(echo.inputSchema["required"], ["value"])
                            self.assertEqual(service.state.row("alpha")["state"], "idle")
                            await client.call_tool("workspace_acquire")
                            await asyncio.wait_for(notified.wait(), 5)
                            refreshed = await refreshes[0]
                            self.assertIn("browser_echo", {t.name for t in refreshed.tools})
                            await client.call_tool("workspace_release")
                            first = await client.call_tool("browser_echo", {"value": "first"})
                            self.assertEqual(first.structuredContent, {"value": "first"})
                            shot = await client.call_tool("computer_screenshot")
                            self.assertEqual(shot.content[0].mimeType, "image/png")
                            self.assertEqual(
                                base64.b64decode(shot.content[0].data), b"fixture-image"
                            )
                            self.assertNotIn("admin", shot.model_dump_json())
                            self.assertTrue(
                                service.state.row("alpha")["owner"].startswith("review-client-")
                            )
                            self.assertEqual(
                                service.state.db.execute("SELECT COUNT(*) FROM agents").fetchone()[
                                    0
                                ],
                                1,
                            )
                            invalid = await client.call_tool("browser_echo", {"value": 123})
                            self.assertTrue(invalid.isError)
                            self.assertEqual(calls, ["first"])
                            async with connect(create_server(config, "alpha")) as other:
                                busy = await other.call_tool("browser_echo", {"value": "blocked"})
                                self.assertTrue(busy.isError)
                                self.assertEqual(busy.structuredContent["code"], "WORKSPACE_BUSY")
                                self.assertNotIn("blocked", calls)
                            self.assertEqual(service.state.row("alpha")["state"], "reserved")
                            listing_started = asyncio.Event()
                            finish_listing = asyncio.Event()
                            original_list = backend.list_tools

                            async def delayed_list(*args, **kwargs):
                                listing_started.set()
                                await finish_listing.wait()
                                return await original_list(*args, **kwargs)

                            with patch.object(backend, "list_tools", side_effect=delayed_list):
                                listing = asyncio.create_task(client.list_tools())
                                await asyncio.wait_for(listing_started.wait(), 5)
                                during_listing = asyncio.create_task(
                                    client.call_tool("browser_echo", {"value": "during-listing"})
                                )
                                try:
                                    with self.assertRaises(TimeoutError):
                                        await asyncio.wait_for(asyncio.shield(during_listing), 0.1)
                                finally:
                                    finish_listing.set()
                                    await listing
                                self.assertFalse((await during_listing).isError)

                            running = asyncio.create_task(
                                client.call_tool("browser_echo", {"value": "wait"})
                            )
                            try:
                                await asyncio.wait_for(entered.wait(), 5)
                                overlap = await client.call_tool(
                                    "browser_echo", {"value": "overlap"}
                                )
                                self.assertTrue(overlap.isError)
                                self.assertNotIn("overlap", calls)
                            finally:
                                finish.set()
                                await running
                            await service.control("alpha", "human")
                            human = await client.call_tool(
                                "browser_echo", {"value": "human-blocked"}
                            )
                            self.assertTrue(human.isError)
                            self.assertNotIn("human-blocked", calls)
                            await service.control("alpha", "agent")
                            failed = await client.call_tool("browser_echo", {"value": "fail"})
                            self.assertTrue(failed.isError)
                            self.assertEqual(calls.count("fail"), 1)
                            self.assertEqual(service.state.row("alpha")["state"], "reserved")
                            self.assertEqual(
                                service.state.history()["operations"][0]["outcome"], "failed"
                            )
                            await client.call_tool("workspace_release")
                            self.assertEqual(service.state.row("alpha")["state"], "idle")
                            # Removing and recreating a workspace revokes credentials on the same manager.
                            service.state.db.execute("DELETE FROM agents")
                            expired = await client.call_tool("workspace_status")
                            self.assertTrue(expired.isError)
                            self.assertIn("credentials expired", expired.content[0].text)
                            reconnected = await client.call_tool("workspace_status")
                            self.assertFalse(reconnected.isError, reconnected)
                            self.assertEqual(
                                service.state.db.execute("SELECT COUNT(*) FROM agents").fetchone()[
                                    0
                                ],
                                1,
                            )
                            # A replacement manager changes the endpoint and revokes credentials.
                            replacement = DeskService(config)
                            replacement.endpoint = service.endpoint
                            replacement.observed = service.observed
                            replacement.runtime.gateway_token = service.runtime.gateway_token
                            with socket.socket() as sock:
                                sock.bind(("127.0.0.1", 0))
                                new_port = sock.getsockname()[1]
                            async with serve(
                                create_app(config, "new-admin", new_port, replacement), new_port
                            ) as new_url:
                                connection.update(url=new_url, token="new-admin")
                                resumed = await client.call_tool(
                                    "browser_echo", {"value": "resumed"}
                                )
                                self.assertFalse(resumed.isError, resumed)
                                self.assertEqual(calls.count("resumed"), 1)
                                await client.call_tool("workspace_release")
                            connection.update(url=url, token="admin")
                            await client.call_tool("browser_echo", {"value": "before-disconnect"})
                        self.assertEqual(service.state.row("alpha")["state"], "idle")
                        self.assertNotIn(
                            "fixture-guest-secret", json.dumps(service.state.history())
                        )
