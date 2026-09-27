import asyncio
import hmac
import json
import tempfile
import time
from contextlib import AsyncExitStack, asynccontextmanager
from functools import partial
from importlib.metadata import version
from pathlib import Path

import httpx
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.middleware import Middleware
from fastmcp.server.providers.proxy import ProxyProvider
from fastmcp.tools.tool import ToolResult
from mcp.types import CallToolResult
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from websockets.asyncio.client import connect as websocket_connect

from hotdesk.config import load_config
from hotdesk.runtime import CapacityError, Runtime
from hotdesk.state import ControlConflict, State


def save_json(path: Path, value: object):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        try:
            json.dump(value, file)
            file.flush()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


async def settle_thread(function, *args, **kwargs):
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()
        raise


BUSY_MESSAGE = (
    "Hot Desk is starting, stopping, saving, or restoring this desktop. Retry in a few seconds."
)


class BusyResult(ToolResult):
    def to_mcp_result(self):
        return CallToolResult(
            content=self.content, structuredContent=self.structured_content, isError=True
        )


class DeskService:
    def __init__(self, config):
        old = config.state_dir / "control.json"
        modes = json.loads(old.read_text()) if old.exists() else {}
        if not isinstance(modes, dict) or any(
            mode not in ("human", "agent") for mode in modes.values()
        ):
            raise ValueError("Invalid legacy control state; restore it before starting.")
        self.config = config
        self.runtime = Runtime(config)
        self.state = State(config)
        self.locks = {name: asyncio.Lock() for name in config.desktops}
        self.lifecycle_lock = asyncio.Lock()
        self.snapshot_lock = asyncio.Lock()
        self.snapshot = []
        self.snapshot_at = 0
        self.call_timeout = 90
        self.reconfigure = None
        self.shutting_down = False
        self.active_requests = 0
        if old.exists():
            for name, mode in modes.items():
                if name in self.locks and mode == "human":
                    self.state.set_mode(name, "human")
            old.rename(old.with_suffix(".migrated"))

    async def observed(self, fresh=False):
        async with self.snapshot_lock:
            if fresh or time.monotonic() - self.snapshot_at > 2:
                self.snapshot = await asyncio.to_thread(self.runtime.status)
                self.snapshot_at = time.monotonic()
            return self.snapshot

    async def desktops(self):
        rows = []
        for observed in await self.observed():
            if observed["name"] not in self.locks:
                continue
            row = {k: v for k, v in observed.items() if not k.endswith("_url")}
            row.update(self.state.public(row["name"]))
            rows.append(row)
        return rows

    def busy(self, name):
        if self.state.row(name)["state"] == "busy":
            return BusyResult(
                structured_content={
                    "code": "WORKSPACE_BUSY",
                    "workspace": name,
                    **self.state.public(name),
                    "requires_user_approval": False,
                    "retry": BUSY_MESSAGE,
                }
            )
        checkpoint = self.runtime.checkpoint_info(name)
        data = {
            "code": "WORKSPACE_BUSY",
            "workspace": name,
            **self.state.public(name),
            "checkpoint": {"available": checkpoint is not None},
            "requires_user_approval": True,
            "ask_user": (
                "Wait for this desktop, or create a temporary clone from the checkpoint? "
                "Later source changes will be absent. Clone changes never merge back. "
                "Save outputs before discarding the clone. Online account actions still persist."
                if checkpoint
                else "Wait for this desktop. No checkpoint exists; release and stop it before "
                "creating a consistent checkpoint. Do not interrupt its current owner."
            ),
        }
        if checkpoint:
            data["checkpoint"].update({key: checkpoint[key] for key in ("id", "created_at")})
        return BusyResult(structured_content=data)

    async def acquire(self, agent, seconds):
        if type(seconds) is not int or not 10 <= seconds <= 3600:
            raise ValueError("Reservation duration must be 10-3600 seconds")
        name = agent["workspace"]
        current = self.state.row(name)
        if current["state"] != "idle" and not (
            current["state"] == "reserved" and current["agent"] == agent["id"]
        ):
            return self.busy(name)
        try:
            row = next(row for row in await self.observed() if row["name"] == name)
            if row["status"] != "running":
                await self.lifecycle(name, "start")
            await self.endpoint(name, "computer")
            return ToolResult(structured_content=self.state.acquire(agent, seconds))
        except ControlConflict:
            if self.state.row(name)["state"] != "idle":
                return self.busy(name)
            raise

    async def checkpoint(self, name):
        if name not in self.locks:
            raise ValueError("Unknown workspace")
        async with self.lifecycle_lock:
            if self.state.row(name)["state"] != "idle" or self.state.unsettled(name):
                raise ControlConflict("Release this workspace before creating a checkpoint.")
            self.state.set_mode(name, "busy")
            try:
                return await settle_thread(self.runtime.checkpoint, name)
            finally:
                self.state.set_mode(name, "idle")

    async def clone(self, source, name, checkpoint_id, user_approved):
        if user_approved is not True:
            raise ValueError("Ask the user before cloning; user_approved must be true.")
        if source not in self.locks:
            raise ValueError("Unknown source workspace")
        async with self.lifecycle_lock:
            try:
                result = await settle_thread(self.runtime.clone, source, name, checkpoint_id)
            except BaseException:
                await self.reconfigure(self.runtime.config)
                self.snapshot_at = 0
                if name in self.locks:
                    self.state.set_mode(name, "recovery")
                raise
            await self.reconfigure(self.runtime.config)
            self.snapshot_at = 0
            self.state.set_mode(name, "idle")
            return result

    async def discard(self, name, outputs_saved):
        if outputs_saved is not True:
            raise ValueError("Save required outputs before discarding; outputs_saved must be true.")
        if name not in self.locks or not self.runtime.clone_info(name):
            raise ValueError("Only temporary clones can be discarded")
        async with self.lifecycle_lock:
            if self.state.row(name)["state"] not in ("idle", "recovery") or self.state.unsettled(
                name
            ):
                raise ControlConflict("Release this clone before discarding it.")
            self.state.set_mode(name, "busy")
            async with self.locks[name]:
                try:
                    result = await settle_thread(self.runtime.discard_clone, name)
                except BaseException:
                    self.state.set_mode(name, "recovery")
                    raise
                finally:
                    await self.reconfigure(self.runtime.config)
                    self.snapshot_at = 0
            return result

    async def endpoint(self, name, kind):
        if name not in self.locks or kind not in ("computer", "browser", "viewer", "valet"):
            raise ValueError("Unknown workspace or tool kind")
        row = next(row for row in await self.observed() if row["name"] == name)
        url = row.get(f"{kind}_url")
        if kind == "valet" and row["status"] == "running" and "valet" not in row["components"]:
            raise ControlConflict(
                "Valet is not enabled in this desktop image (build with HOTDESK_VALET=1)."
            )
        if not url or row["status"] != "running":
            raise ControlConflict(f"Workspace {name} is stopped. Run hotdesk start {name}.")
        if kind != "viewer" and row.get("health") != "healthy":
            raise ControlConflict(f"Workspace {name} is not ready; check component health.")
        required = {
            "computer": ("computer", "x11", "gateway"),
            "browser": ("browser", "chromium", "x11", "gateway"),
            "viewer": ("viewer", "x11", "gateway"),
            "valet": ("valet", "gateway"),
        }[kind]
        failed = [
            component
            for component in required
            if row.get("components", {}).get(component) == "failed"
        ]
        if failed:
            raise ControlConflict("Workspace components are not ready: " + ", ".join(failed))
        return url.rstrip("/")

    def agent(self, name):
        authorization = get_http_headers(include={"authorization"}).get("authorization", "")
        return self.state.authenticate(authorization.removeprefix("Bearer "), name)

    async def execute(self, agent, tool, action):
        name = agent["workspace"]
        initial = self.state.check(agent)
        async with self.locks[name]:
            current = self.state.check(agent)
            if current["generation"] != initial["generation"]:
                raise ControlConflict(
                    "Reservation changed while waiting; retry only after inspection."
                )
            operation = self.state.begin(agent, tool)
            try:
                async with asyncio.timeout(self.call_timeout):
                    result = await action()
            except asyncio.CancelledError:
                self.state.finish(operation, "unknown")
                raise
            except Exception as exc:
                # ProxyTool raises unchained ToolError for an upstream isError response.
                if isinstance(exc, ToolError) and exc.__cause__ is None and exc.__context__ is None:
                    self.state.finish(operation, "failed")
                    raise
                self.state.finish(operation, "unknown")
                if isinstance(exc, TimeoutError):
                    raise TimeoutError(
                        f"Operation {operation} timed out; outcome unknown. Inspect and recover the workspace."
                    ) from None
                raise ControlConflict(
                    f"Operation {operation} has an unknown outcome. Inspect and recover the workspace."
                ) from exc
            else:
                self.state.finish(operation, "completed")
                return result

    async def control(self, name, mode):
        if name not in self.locks or mode not in ("human", "agent"):
            raise ValueError("Unknown workspace or control mode")
        row = self.state.row(name)
        if row["state"] == "recovery":
            raise ControlConflict(
                "Outcome unknown. Inspect the desktop, then restart it to recover."
            )
        if row["state"] == "busy":
            raise ControlConflict(BUSY_MESSAGE)
        if mode == "human":
            self.state.set_mode(name, "takeover")
            async with self.locks[name]:
                if self.state.row(name)["state"] == "recovery" or self.state.unsettled(name):
                    self.state.set_mode(name, "recovery")
                    raise ControlConflict(
                        "Previous action outcome is unknown. Recovery is required."
                    )
                try:
                    await settle_thread(self.runtime.set_viewer_control, name, True)
                except BaseException:
                    self.state.set_mode(name, "recovery")
                    raise
                self.state.set_mode(name, "human")
        else:
            async with self.locks[name]:
                if self.state.row(name)["state"] != "human":
                    raise ControlConflict("Only human control can be returned to agents.")
                await settle_thread(self.runtime.set_viewer_control, name, False)
                self.state.set_mode(name, "idle")
        return self.state.public(name)

    async def lifecycle(self, name, action):
        if name not in self.locks or action not in ("start", "stop", "recover"):
            raise ValueError("Unknown workspace or action")
        async with self.lifecycle_lock:
            row = self.state.row(name)
            if action == "recover":
                if row["state"] != "recovery":
                    raise ControlConflict("Recovery is only available for uncertain work.")
            elif row["state"] not in ("idle", "human") and not (
                action == "stop" and row["state"] == "recovery"
            ):
                raise ControlConflict(
                    f"Workspace is {row['state']}; release it before lifecycle changes."
                )
            self.state.set_mode(name, "busy")
            async with self.locks[name]:
                try:
                    if action in ("stop", "recover"):
                        await settle_thread(self.runtime.stop, name)
                        self.state.db.execute(
                            "UPDATE operations SET outcome='unknown',finished=? "
                            "WHERE workspace=? AND outcome='running'",
                            (time.time(), name),
                        )
                    if action in ("start", "recover"):
                        await settle_thread(self.runtime.up, build=False, name=name)
                        if row["state"] == "human":
                            await settle_thread(self.runtime.set_viewer_control, name, True)
                except CapacityError:
                    self.state.set_mode(name, row["state"])
                    self.snapshot_at = 0
                    raise
                except BaseException:
                    self.state.set_mode(name, "recovery")
                    self.snapshot_at = 0
                    raise
                self.state.set_mode(name, "human" if row["state"] == "human" else "idle")
                self.snapshot_at = 0
        return self.state.public(name)

    async def apply(self, build=False, down=False):
        async with self.lifecycle_lock:
            rows = [self.state.row(name) for name in self.locks]
            if any(row["state"] != "idle" for row in rows):
                raise ControlConflict(
                    "Release agent reservations and return human control before applying configuration."
                )
            updated = load_config(self.config.path)
            if updated.project != self.config.project:
                raise ValueError("Use a separate manager for a different project.name")
            for name in self.locks:
                self.state.set_mode(name, "busy")
            previous_config = self.runtime.config
            dispatched = False
            try:
                self.runtime.config = updated
                if not down:
                    await settle_thread(self.runtime.validate_start)
                self.runtime.config = previous_config
                dispatched = True
                for name in self.config.desktops.keys() - updated.desktops.keys():
                    await settle_thread(self.runtime.stop, name)
                self.runtime.config = updated
                if down:
                    await settle_thread(self.runtime.down)
                else:
                    await settle_thread(self.runtime.up, build=build)
                if self.reconfigure:
                    await self.reconfigure(updated)
                self.config = updated
                for name in self.locks:
                    self.state.set_mode(name, "idle")
            except CapacityError:
                self.runtime.config = previous_config
                for name in self.locks:
                    self.state.set_mode(name, "idle")
                raise
            except BaseException:
                self.runtime.config = previous_config
                for name in self.locks:
                    self.state.set_mode(name, "recovery" if dispatched else "idle")
                raise
            finally:
                self.snapshot_at = 0
        return await self.desktops()

    async def archive(self, name, action, path):
        if not isinstance(path, str) or not path:
            raise ValueError("An archive path is required")
        async with self.lifecycle_lock:
            if self.state.row(name)["state"] != "idle":
                raise ControlConflict("Release this workspace before backup or restore.")
            self.state.set_mode(name, "busy")
            try:
                result = await settle_thread(getattr(self.runtime, action), name, Path(path))
            finally:
                self.state.set_mode(name, "idle")
                self.snapshot_at = 0
            return result


VALET_TOOLS = frozenset({"list_handles", "request_grant", "http_call", "browser_fill", "pay"})


def tool_kind(tool):
    if tool in VALET_TOOLS:
        return "valet"
    return "browser" if tool.startswith("browser_") else "computer"


class ValetProvider(ProxyProvider):
    """ProxyProvider that lists nothing until the desktop reports Valet ready.

    FastMCP skips a provider whose list call raises, but logs a warning each
    time, so desktops built without Valet would log one per tool listing.
    Caching stays off (cache_ttl=0) so a rebuilt desktop shows the tools on
    the next listing.
    """

    def __init__(self, client_factory, service, name, **kwargs):
        super().__init__(client_factory, **kwargs)
        self._service = service
        self._name = name

    async def _valet_ready(self):
        for row in await self._service.observed():
            if row["name"] == self._name:
                return row.get("components", {}).get("valet") == "ready"
        return False

    async def _list_tools(self):
        if not await self._valet_ready():
            return []
        return await super()._list_tools()

    async def _list_resources(self):
        if not await self._valet_ready():
            return []
        return await super()._list_resources()

    async def _list_resource_templates(self):
        if not await self._valet_ready():
            return []
        return await super()._list_resource_templates()

    async def _list_prompts(self):
        if not await self._valet_ready():
            return []
        return await super()._list_prompts()


def workspace_server(service, name):
    class Ownership(Middleware):
        async def on_message(self, context, call_next):
            service.agent(name)
            return await call_next(context)

        async def on_call_tool(self, context, call_next):
            if context.message.name in {
                "workspace_status",
                "workspace_acquire",
                "workspace_renew",
                "workspace_release",
            }:
                return await call_next(context)
            await service.endpoint(name, tool_kind(context.message.name))
            return await service.execute(
                service.agent(name), context.message.name, lambda: call_next(context)
            )

        async def on_read_resource(self, context, call_next):
            return await service.execute(
                service.agent(name), "resource_read", lambda: call_next(context)
            )

        async def on_get_prompt(self, context, call_next):
            return await service.execute(
                service.agent(name), "prompt_get", lambda: call_next(context)
            )

    server = FastMCP(
        f"Hot Desk {name}",
        version=version("hotdesk"),
        middleware=[Ownership()],
        instructions="Use workspace_acquire before computer or browser tools. Renew before expiry. "
        "Release when finished. Human takeover revokes your reservation. Never retry an uncertain action.",
        mask_error_details=False,
    )
    for kind in ("computer", "browser", "valet"):

        async def factory(kind=kind):
            url = await service.endpoint(name, kind)
            return Client(
                StreamableHttpTransport(
                    url + "/mcp",
                    headers={"Authorization": "Bearer " + service.runtime.gateway_token(name)},
                    httpx_client_factory=partial(
                        httpx.AsyncClient, trust_env=False, timeout=httpx.Timeout(30, read=180)
                    ),
                )
            )

        if kind == "valet":
            server.add_provider(ValetProvider(factory, service, name, cache_ttl=0))
        else:
            server.add_provider(ProxyProvider(factory, cache_ttl=0))

    @server.tool()
    async def workspace_status() -> dict:
        """Read this workspace's ownership and readiness."""
        return next(row for row in await service.desktops() if row["name"] == name)

    @server.tool(output_schema=None)
    async def workspace_acquire(seconds: int = 300) -> ToolResult:
        """Reserve this workspace for your task, or report its current owner."""
        return await service.acquire(service.agent(name), seconds)

    @server.tool()
    async def workspace_renew(seconds: int = 300) -> dict:
        """Extend your current reservation before it expires."""
        return service.state.renew(service.agent(name), seconds)

    @server.tool()
    async def workspace_release() -> dict:
        """Release your reservation after all task actions have finished."""
        return service.state.release(service.agent(name))

    return server


def create_app(config, token: str, port: int, service=None, shutdown=None):
    service = service or DeskService(config)
    origin = f"http://127.0.0.1:{port}"
    cookie_name = f"hotdesk-session-{port}"
    static = Path(__file__).with_name("static")
    mcp_apps = {
        name: workspace_server(service, name).http_app(path="/", stateless_http=True)
        for name in config.desktops
    }
    child_closers = {}

    async def close_children():
        for close in list(child_closers.values()):
            await close()

    @asynccontextmanager
    async def lifespan(app):
        async with AsyncExitStack() as stack:
            stack.callback(service.state.db.close)
            stack.push_async_callback(close_children)
            for name, child in mcp_apps.items():
                child_closers[name] = await start_child(child)
            app.state.http = await stack.enter_async_context(
                httpx.AsyncClient(timeout=30, trust_env=False)
            )
            try:
                rows = await service.observed(fresh=True)
            except RuntimeError:
                rows = []
            for row in rows:
                if row.get("viewer_url"):
                    await settle_thread(
                        service.runtime.set_viewer_control,
                        row["name"],
                        service.state.row(row["name"])["state"] == "human",
                    )
            yield

    async def api(request):
        name = request.path_params.get("name")
        action = request.path_params["action"]
        writes = {
            "session": set(),
            "shutdown": set(),
            "agents": {"workspace", "owner", "task"},
            "control": {"control"},
            "lifecycle": {"action"},
            "apply": {"build"},
            "down": set(),
            "backup": {"path"},
            "restore": {"path"},
            "checkpoint": set(),
            "clone": {"name", "checkpoint_id", "user_approved"},
            "discard": {"outputs_saved"},
        }
        if action in writes and request.method != "POST":
            return JSONResponse({"error": "Use POST for this operation"}, status_code=405)
        body = await request.json() if request.method == "POST" else {}
        if not isinstance(body, dict):
            raise ValueError("Expected a JSON object")
        if action in writes and body.keys() - writes[action]:
            raise ValueError("Unknown request fields")
        if action == "apply" and type(body.get("build", False)) is not bool:
            raise ValueError("build must be a boolean")
        if action == "shutdown":
            if shutdown is None:
                raise RuntimeError("This manager does not support graceful shutdown")
            if (
                service.active_requests
                or service.lifecycle_lock.locked()
                or any(
                    service.state.row(name)["state"] != "idle"
                    or service.state.unsettled(name)
                    or lock.locked()
                    for name, lock in service.locks.items()
                )
            ):
                raise ControlConflict(
                    "Release reservations, return human control, and resolve recovery before stopping the manager."
                )
            service.shutting_down = True
            return JSONResponse({"stopping": True}, background=BackgroundTask(shutdown))
        if action == "health":
            return JSONResponse(
                {
                    "project": config.project,
                    "config": str(config.path),
                    "version": version("hotdesk"),
                }
            )
        if action == "session":
            response = JSONResponse({"authenticated": True})
            response.set_cookie(cookie_name, token, httponly=True, samesite="strict", path="/")
            return response
        if action == "desktops":
            result = await service.desktops()
        elif action == "capacity":
            result = await asyncio.to_thread(service.runtime.capacity, await service.observed())
        elif action == "events":
            result = service.state.history()
        elif action == "agents":
            if body.get("workspace") not in service.config.desktops:
                raise ValueError("Unknown workspace")
            credential = service.state.register(
                body.get("workspace"), body.get("owner"), body.get("task", "")
            )
            result = {"token": credential, "url": f"{origin}/mcp/{body['workspace']}/"}
        elif action == "control":
            result = await service.control(name, body.get("control"))
        elif action == "lifecycle":
            result = await service.lifecycle(name, body.get("action"))
        elif action in ("apply", "down"):
            result = await service.apply(build=body.get("build", False), down=action == "down")
        elif action in ("backup", "restore"):
            result = await service.archive(name, action, body.get("path"))
        elif action == "checkpoint":
            result = await service.checkpoint(name)
        elif action == "clone":
            result = await service.clone(
                name, body.get("name"), body.get("checkpoint_id"), body.get("user_approved")
            )
        elif action == "discard":
            result = await service.discard(name, body.get("outputs_saved"))
        else:
            return JSONResponse({"error": "Unknown operation"}, status_code=404)
        return JSONResponse(result)

    async def viewer(request):
        name = request.path_params["name"]
        path = request.path_params.get("path", "")
        if not path:
            if name not in service.config.desktops:
                return Response(status_code=404)
            return HTMLResponse(
                '<!doctype html><html lang="en"><head><meta name="viewport" content="width=device-width">'
                '<title>Hot Desk viewer</title><link rel="stylesheet" href="/static/viewer.css">'
                '</head><body><header><p id="status" role="status">Connecting to workspace...</p>'
                '<button id="return-control" hidden>Return control to agents</button></header><div id="screen"></div>'
                '<script type="module" src="/static/viewer.js"></script></body></html>'
            )
        base = await service.endpoint(name, "viewer")
        if path == "session":
            interactive = request.query_params.get("interactive") == "1"
            row = service.state.row(name)
            if interactive and row["state"] != "human":
                raise ControlConflict("Take control before opening an interactive viewer.")
            return JSONResponse(
                {
                    "password": service.runtime.viewer_password(name, read_only=not interactive),
                    "generation": row["generation"],
                }
            )
        if path == "websockify":
            return Response(status_code=400)
        response = await request.app.state.http.get(
            base + "/" + path,
            headers={"Authorization": "Bearer " + service.runtime.gateway_token(name)},
        )
        return Response(
            response.content,
            status_code=response.status_code,
            media_type=response.headers.get("content-type"),
        )

    async def viewer_socket(ws):
        name = ws.path_params["name"]
        if name not in service.locks:
            await ws.close(code=1008)
            return
        interactive = ws.query_params.get("interactive") == "1"
        async with service.locks[name]:
            row = service.state.row(name)
            await settle_thread(service.runtime.set_viewer_control, name, row["state"] == "human")
        generation = str(row["generation"])
        if interactive and (
            row["state"] != "human" or ws.query_params.get("generation") != generation
        ):
            await ws.close(code=1008)
            return
        url = (await service.endpoint(name, "viewer")).replace("http:", "ws:") + "/websockify"
        async with websocket_connect(
            url,
            additional_headers={"Authorization": "Bearer " + service.runtime.gateway_token(name)},
            subprotocols=["binary"],
            max_size=None,
            compression=None,
        ) as remote:
            await ws.accept(subprotocol="binary" if "binary" in ws.scope["subprotocols"] else None)

            async def send():
                while True:
                    message = await ws.receive()
                    if message["type"] == "websocket.disconnect":
                        return
                    await remote.send(message.get("bytes") or message.get("text") or b"")

            async def receive():
                async for data in remote:
                    await ws.send_bytes(data) if isinstance(data, bytes) else await ws.send_text(
                        data
                    )

            async def ownership():
                while True:
                    await asyncio.sleep(0.2)
                    current = service.state.row(name)
                    if interactive and (
                        current["state"] != "human" or str(current["generation"]) != generation
                    ):
                        await ws.close(code=1008, reason="Control changed")
                        return

            tasks = [
                asyncio.create_task(send()),
                asyncio.create_task(receive()),
                asyncio.create_task(ownership()),
            ]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def conflict(request, exc):
        return JSONResponse({"error": str(exc)}, status_code=409)

    async def invalid(request, exc):
        return JSONResponse({"error": str(exc)}, status_code=400)

    async def unavailable(request, exc):
        return JSONResponse({"error": str(exc)}, status_code=502)

    app = Starlette(
        lifespan=lifespan,
        exception_handlers={
            ControlConflict: conflict,
            ValueError: invalid,
            TypeError: invalid,
            RuntimeError: unavailable,
            OSError: unavailable,
        },
        routes=[
            Mount("/static", StaticFiles(directory=static)),
            Route("/api/desktops/{name}/{action}", api, methods=["GET", "POST"]),
            Route("/api/{action}", api, methods=["GET", "POST"]),
            WebSocketRoute("/viewer/{name}/websockify", viewer_socket),
            Route("/viewer/{name}/{path:path}", viewer),
            *[Mount(f"/mcp/{name}", child) for name, child in mcp_apps.items()],
        ],
    )
    app.state.service = service

    async def start_child(child):
        ready = asyncio.get_running_loop().create_future()
        stop = asyncio.Event()

        async def run():
            try:
                async with child.lifespan(child):
                    ready.set_result(None)
                    await stop.wait()
            except BaseException as exc:
                if not ready.done():
                    ready.set_exception(exc)
                raise

        task = asyncio.create_task(run())

        async def close():
            stop.set()
            await task

        try:
            await ready
        except BaseException:
            await close()
            raise
        return close

    async def reconfigure(updated):
        removed = service.config.desktops.keys() - updated.desktops.keys()
        added = updated.desktops.keys() - service.config.desktops.keys()
        for name in removed:
            service.state.db.execute("DELETE FROM agents WHERE workspace=?", (name,))
            service.locks.pop(name)
            app.router.routes[:] = [
                route for route in app.router.routes if getattr(route, "path", "") != f"/mcp/{name}"
            ]
            if name in child_closers:
                await child_closers.pop(name)()
        for name in added:
            service.state.db.execute("INSERT OR IGNORE INTO desks(name) VALUES (?)", (name,))
            service.state.set_mode(name, "busy")
            service.locks[name] = asyncio.Lock()
            child = workspace_server(service, name).http_app(path="/", stateless_http=True)
            child_closers[name] = await start_child(child)
            app.router.routes.append(Mount(f"/mcp/{name}", child))
        service.config = updated

    service.reconfigure = reconfigure

    class Guard:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] not in ("http", "websocket"):
                return await self.app(scope, receive, send)
            headers = {
                k.decode("latin-1").lower(): v.decode("latin-1")
                for k, v in scope.get("headers", [])
            }
            path = scope["path"]
            error, status = None, 403
            if headers.get("host") != f"127.0.0.1:{port}":
                error = "Invalid Host"
            elif headers.get("origin") not in (None, origin):
                error = "Foreign browser origin refused"
            elif path.startswith("/api/"):
                if not hmac.compare_digest(
                    headers.get("authorization", "").encode("latin-1"), f"Bearer {token}".encode()
                ):
                    error, status = (
                        "Manager authentication failed. Reconnect with hotdesk open.",
                        401,
                    )
            elif path.startswith("/viewer/") and not (
                scope["type"] == "http" and path.count("/") == 3 and path.endswith("/")
            ):
                from http.cookies import SimpleCookie

                cookie = SimpleCookie()
                cookie.load(headers.get("cookie", ""))
                value = cookie.get(cookie_name)
                if not value or not hmac.compare_digest(
                    value.value.encode("utf-8"), token.encode()
                ):
                    error, status = "Run hotdesk open WORKSPACE to authenticate this viewer.", 401
            elif path.startswith("/mcp/"):
                try:
                    if path.split("/")[2] not in service.config.desktops:
                        raise ControlConflict("Unknown workspace")
                    service.state.authenticate(
                        headers.get("authorization", "").removeprefix("Bearer "), path.split("/")[2]
                    )
                except ControlConflict as exc:
                    error, status = str(exc), 401
            if not error and service.shutting_down:
                error, status = "Manager is stopping. Run hotdesk open to reconnect.", 503
            if error:
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                    return
                return await JSONResponse({"error": error}, status_code=status)(
                    scope, receive, send
                )

            async def secure_send(message):
                if message["type"] == "http.response.start":
                    message["headers"] += [
                        (b"cache-control", b"no-store"),
                        (b"referrer-policy", b"no-referrer"),
                        (
                            b"content-security-policy",
                            b"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self' ws://127.0.0.1:*; img-src 'self' data: blob:; frame-ancestors 'none'",
                        ),
                    ]
                await send(message)

            tracked = scope["type"] == "http" and (
                path.startswith("/mcp/")
                or (
                    path.startswith("/api/")
                    and scope.get("method") == "POST"
                    and path not in ("/api/shutdown", "/api/session")
                )
            )
            if tracked:
                service.active_requests += 1
            try:
                await self.app(scope, receive, secure_send)
            finally:
                if tracked:
                    service.active_requests -= 1

    app.add_middleware(Guard)
    return app
