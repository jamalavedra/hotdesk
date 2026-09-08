import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from functools import partial
from importlib.metadata import version
from pathlib import Path

import anyio
import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from mcp.server import NotificationOptions, Server
from mcp.server.stdio import stdio_server

from hotdesk.connection import ensure_manager


def create_server(config, workspace):
    if workspace not in config.desktops:
        raise ValueError("Unknown workspace: " + workspace)
    owner_suffix = secrets.token_hex(4)
    lock = asyncio.Lock()
    # Discovery waits its turn; overlapping actions must not queue against stale desktop state.
    action_lock = asyncio.Lock()
    identity = None
    transport = None
    reserved = False

    @asynccontextmanager
    async def client():
        nonlocal identity, transport, reserved
        async with lock:
            connection = await asyncio.to_thread(ensure_manager, config)
            current = (connection["url"], connection["token"])
            if current != identity:
                params = server.request_context.session.client_params
                name = params.clientInfo.name if params else "mcp"
                async with httpx.AsyncClient(trust_env=False, timeout=30) as http:
                    response = await http.post(
                        connection["url"] + "/api/agents",
                        headers={"Authorization": "Bearer " + connection["token"]},
                        json={"workspace": workspace, "owner": f"{name[:80]}-{owner_suffix}"},
                    )
                    if not response.is_success:
                        try:
                            reason = response.json().get("error", response.reason_phrase)
                        except ValueError:
                            reason = response.reason_phrase
                        raise RuntimeError(reason)
                    access = response.json()
                transport = StreamableHttpTransport(
                    connection["url"] + "/mcp/" + workspace + "/",
                    headers={"Authorization": "Bearer " + access["token"]},
                    httpx_client_factory=partial(
                        httpx.AsyncClient, trust_env=False, timeout=httpx.Timeout(30, read=180)
                    ),
                )
                identity = current
                reserved = False
            try:
                async with Client(transport) as upstream:
                    yield upstream
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 401:
                    identity = None
                    raise RuntimeError(
                        "Workspace credentials expired. The next request will reconnect; "
                        "inspect workspace_status before continuing."
                    ) from exc
                raise

    @asynccontextmanager
    async def lifespan(server):
        try:
            yield
        finally:
            if reserved:
                with anyio.move_on_after(5, shield=True):
                    try:
                        async with Client(transport) as upstream:
                            result = await upstream.call_tool_mcp("workspace_release", {})
                            if result.isError:
                                logging.warning(
                                    "Hot Desk could not release the desktop; inspect status."
                                )
                    except Exception:
                        logging.warning("Hot Desk disconnected before release; inspect status.")

    server = Server(
        "Hot Desk",
        version=version("hotdesk"),
        instructions=(
            f"Connected workspace: {workspace}.\n\n"
            + (Path(__file__).parent / "skills/hotdesk/SKILL.md")
            .read_text()
            .split("---", 2)[-1]
            .strip()
        ),
        lifespan=lifespan,
    )

    @server.list_tools()
    async def list_tools():
        async with client() as upstream:
            return await upstream.list_tools()

    @server.call_tool()
    async def call_tool(name, arguments):
        nonlocal reserved
        if action_lock.locked():
            raise RuntimeError("This MCP connection already has a command running. Wait for it.")
        async with action_lock, client() as upstream:
            if not name.startswith("workspace_"):
                acquired = await upstream.call_tool_mcp("workspace_acquire", {"seconds": 300})
                if acquired.isError:
                    return acquired
                reserved = True
            result = await upstream.call_tool_mcp(name, arguments)
            if not result.isError:
                if name == "workspace_acquire":
                    reserved = True
                elif name == "workspace_release":
                    reserved = False
        if name == "workspace_acquire" and not result.isError:
            await server.request_context.session.send_tool_list_changed()
        return result

    return server


async def run(config, workspace):
    server = create_server(config, workspace)
    async with stdio_server() as (read, write):
        await server.run(
            read,
            write,
            server.create_initialization_options(NotificationOptions(tools_changed=True)),
        )
