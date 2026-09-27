import asyncio
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
from fastmcp.exceptions import ToolError

from hotdesk.runtime import CapacityError
from hotdesk.server import ControlConflict, DeskService, create_app, tool_kind


class ServiceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = SimpleNamespace(
            state_dir=Path(self.temp.name),
            desktops={"alpha": {}, "beta": {}},
            project="test",
            path=Path(self.temp.name) / "hotdesk.toml",
        )
        self.service = DeskService(self.config)
        self.service.runtime.set_viewer_control = Mock()
        self.service.runtime.checkpoint_info = Mock(return_value=None)

    def tearDown(self):
        self.service.state.db.close()
        self.temp.cleanup()

    def agent(self, name="alpha", owner="worker"):
        token = self.service.state.register(name, owner, "test task")
        agent = self.service.state.authenticate(token, name)
        self.service.state.acquire(agent)
        return agent

    async def test_pending_takeover_rejects_queued_actions(self):
        agent = self.agent()
        called = False

        async def action():
            nonlocal called
            called = True

        async with self.service.locks["alpha"]:
            queued = asyncio.create_task(self.service.execute(agent, "click", action))
            await asyncio.sleep(0)
            takeover = asyncio.create_task(self.service.control("alpha", "human"))
            await asyncio.sleep(0)
            await self.service.control("beta", "human")
            self.assertFalse(takeover.done())
        with self.assertRaises(ControlConflict):
            await queued
        await takeover
        self.assertFalse(called)
        self.assertEqual(self.service.state.row("alpha")["state"], "human")
        await self.service.control("alpha", "agent")
        with self.assertRaises(ControlConflict):
            self.service.state.check(agent)

    async def test_timeout_blocks_reuse_and_records_unknown(self):
        agent = self.agent()
        self.service.call_timeout = 0.01
        with self.assertRaises(TimeoutError):
            await self.service.execute(agent, "slow", lambda: asyncio.sleep(1))
        self.assertEqual(self.service.state.row("alpha")["state"], "recovery")
        self.assertEqual(self.service.state.history()["operations"][0]["outcome"], "unknown")
        with self.assertRaises(ControlConflict):
            self.service.state.acquire(agent)
        with self.assertRaises(ControlConflict):
            await self.service.control("alpha", "human")

    async def test_wrapped_transport_error_requires_recovery(self):
        agent = self.agent()

        async def action():
            try:
                raise httpx.ReadTimeout("response lost")
            except httpx.ReadTimeout as exc:
                raise ToolError("Upstream request timed out, please retry") from exc

        with self.assertRaises(ControlConflict):
            await self.service.execute(agent, "click", action)
        self.assertEqual(self.service.state.row("alpha")["state"], "recovery")
        self.assertEqual(self.service.state.history()["operations"][0]["outcome"], "unknown")

    async def test_cancelled_action_requires_recovery(self):
        agent = self.agent()
        entered = asyncio.Event()

        async def action():
            entered.set()
            await asyncio.Future()

        task = asyncio.create_task(self.service.execute(agent, "click", action))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.service.state.row("alpha")["state"], "recovery")
        self.assertEqual(self.service.state.history()["operations"][0]["outcome"], "unknown")

    async def test_auth_validation_scope_and_cookie(self):
        app = create_app(self.config, "secret", 7890, self.service)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:7890"
        ) as client:
            self.assertEqual((await client.get("/")).status_code, 404)
            self.assertEqual((await client.get("/static/app.js")).status_code, 404)
            shell = await client.get("/viewer/alpha/?view_only=false")
            self.assertEqual(shell.status_code, 200)
            self.assertNotIn("secret", shell.text)
            self.assertIn("return-control", shell.text)
            self.assertEqual((await client.get("/viewer/missing/")).status_code, 404)
            for path in ("session", "core/rfb.js", "websockify"):
                self.assertEqual((await client.get("/viewer/alpha/" + path)).status_code, 401)
            self.assertEqual((await client.get("/api/health")).status_code, 401)
            client.headers["Authorization"] = "Bearer secret"
            self.assertEqual((await client.get("/api/health")).status_code, 200)
            self.assertEqual(
                (
                    await client.get("/api/health", headers={"Origin": "https://evil.test"})
                ).status_code,
                403,
            )
            self.assertEqual((await client.get("/api/down")).status_code, 405)
            for body in ([], None, {"control": "human", "typo": 1}):
                response = await client.post("/api/desktops/alpha/control", json=body)
                self.assertEqual(response.status_code, 400)
            response = await client.post("/api/session", json={})
            self.assertIn("HttpOnly", response.headers["set-cookie"])
            self.service.endpoint = AsyncMock(return_value="http://127.0.0.1:1")
            self.service.runtime.viewer_password = Mock(return_value="viewer-secret")
            self.assertEqual(
                (await client.get("/viewer/alpha/session?interactive=1")).status_code, 409
            )
            self.assertEqual(
                (await client.get("/viewer/alpha/session?interactive=0")).json()["password"],
                "viewer-secret",
            )
            response = await client.post("/api/agents", json={"workspace": "alpha", "owner": "one"})
            credential = response.json()["token"]
            response = await client.get(
                "/mcp/beta/", headers={"Authorization": "Bearer " + credential}
            )
            self.assertEqual(response.status_code, 401)
            response = await client.get("/api/health", headers={b"authorization": b"Bearer \xff"})
            self.assertEqual(response.status_code, 401)

    async def test_expired_active_operation_requires_recovery(self):
        agent = self.agent()
        operation = self.service.state.begin(agent, "click")
        self.service.state.db.execute('UPDATE desks SET expires=0 WHERE name="alpha"')
        self.assertEqual(self.service.state.row("alpha")["state"], "recovery")
        self.service.state.finish(operation, "completed")
        self.assertEqual(self.service.state.row("alpha")["state"], "recovery")

    async def test_status_uses_declared_identity_and_hides_legacy_fields_and_guest_urls(self):
        self.service.state.db.execute("ALTER TABLE desks ADD COLUMN expected_account TEXT")
        self.service.state.db.execute("ALTER TABLE desks ADD COLUMN verified_at REAL")
        self.service.state.db.execute("UPDATE desks SET expected_account='legacy',verified_at=1")
        self.service.observed = AsyncMock(
            return_value=[
                {
                    "name": "alpha",
                    "status": "running",
                    "expected_account": "configured",
                    "viewer_url": "http://127.0.0.1:1234",
                    "computer_url": "http://127.0.0.1:1235",
                }
            ]
        )
        (row,) = await self.service.desktops()
        self.assertEqual(row["expected_account"], "configured")
        self.assertFalse(
            {"viewer_url", "computer_url", "human_viewer_url", "control", "verified_at", "agent"}
            & row.keys()
        )
        self.assertEqual(self.service.state.row("alpha")["expected_account"], "legacy")

    async def test_capacity_refusal_does_not_create_unknown_operation(self):
        self.service.runtime.up = Mock(side_effect=CapacityError("Memory budget exceeded"))
        with self.assertRaises(CapacityError):
            await self.service.lifecycle("alpha", "start")
        self.assertEqual(self.service.state.row("alpha")["state"], "idle")
        self.assertEqual(self.service.state.history()["operations"], [])

    async def test_busy_acquire_preserves_owner_and_requires_user_choice(self):
        owner = self.agent()
        token = self.service.state.register("alpha", "second", "another task")
        competitor = self.service.state.authenticate(token, "alpha")
        self.service.runtime.up = Mock()
        self.service.runtime.clone = Mock()
        result = (await self.service.acquire(competitor, 300)).to_mcp_result()
        self.assertTrue(result.isError)
        self.assertEqual(result.structuredContent["code"], "WORKSPACE_BUSY")
        self.assertEqual(result.structuredContent["owner"], "worker")
        self.assertFalse(result.structuredContent["checkpoint"]["available"])
        self.service.runtime.checkpoint_info.return_value = {
            "id": "a" * 32,
            "created_at": "2026-09-06T10:00:00+00:00",
            "path": "/private/archive",
        }
        result = (await self.service.acquire(competitor, 300)).to_mcp_result()
        self.assertTrue(result.structuredContent["requires_user_approval"])
        self.assertEqual(result.structuredContent["checkpoint"]["id"], "a" * 32)
        self.assertNotIn("path", result.structuredContent["checkpoint"])
        self.service.state.check(owner)
        self.service.runtime.up.assert_not_called()
        self.service.runtime.clone.assert_not_called()

    async def test_clone_and_discard_require_explicit_acknowledgment(self):
        self.service.runtime.clone = Mock()
        self.service.runtime.discard_clone = Mock()
        for approval in (None, False, "true", 1):
            with self.assertRaises(ValueError):
                await self.service.clone("alpha", "copy", "a" * 32, approval)
            with self.assertRaises(ValueError):
                await self.service.discard("alpha", approval)
        self.service.runtime.clone.assert_not_called()
        self.service.runtime.discard_clone.assert_not_called()

    async def test_busy_checkpoint_does_not_interrupt_owner(self):
        owner = self.agent()
        self.service.runtime.checkpoint = Mock()
        with self.assertRaises(ControlConflict):
            await self.service.checkpoint("alpha")
        self.service.runtime.checkpoint.assert_not_called()
        self.service.state.check(owner)

    async def test_lifecycle_in_progress_is_busy_not_recovery(self):
        entered, finish = threading.Event(), threading.Event()

        def stop(*args):
            entered.set()
            finish.wait(5)

        self.service.runtime.stop = stop
        task = asyncio.create_task(self.service.lifecycle("alpha", "stop"))
        await asyncio.to_thread(entered.wait, 2)
        self.assertEqual(self.service.state.row("alpha")["state"], "busy")
        with self.assertRaisesRegex(ControlConflict, "Retry in a few seconds"):
            await self.service.control("alpha", "human")
        token = self.service.state.register("alpha", "waiting", "test task")
        waiting = self.service.state.authenticate(token, "alpha")
        result = await self.service.acquire(waiting, 60)
        self.assertEqual(result.structured_content["state"], "busy")
        self.assertFalse(result.structured_content["requires_user_approval"])
        finish.set()
        await task
        self.assertEqual(self.service.state.row("alpha")["state"], "idle")

    async def test_failed_lifecycle_requires_recovery(self):
        self.service.runtime.stop = Mock(side_effect=RuntimeError("docker stop failed"))
        with self.assertRaisesRegex(RuntimeError, "docker stop failed"):
            await self.service.lifecycle("alpha", "stop")
        self.assertEqual(self.service.state.row("alpha")["state"], "recovery")

    async def test_unsettled_record_blocks_handoff_after_storage_failure(self):
        agent = self.agent()
        self.service.state.begin(agent, "click")
        with self.assertRaises(ControlConflict):
            await self.service.control("alpha", "human")
        self.assertEqual(self.service.state.row("alpha")["state"], "recovery")
        self.service.runtime.set_viewer_control.assert_not_called()

    async def test_cancelled_backup_holds_lifecycle_until_worker_finishes(self):
        entered, finish = threading.Event(), threading.Event()

        def backup(*args):
            entered.set()
            finish.wait(5)
            return {}

        self.service.runtime.backup = backup
        task = asyncio.create_task(self.service.archive("alpha", "backup", "/unused"))
        await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.assertTrue(self.service.lifecycle_lock.locked())
        self.assertEqual(self.service.state.row("alpha")["state"], "busy")
        finish.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.service.lifecycle_lock.locked())
        self.assertEqual(self.service.state.row("alpha")["state"], "idle")

    async def test_startup_failure_closes_state_database(self):
        self.service.observed = AsyncMock(
            return_value=[{"name": "alpha", "viewer_url": "http://127.0.0.1:1"}]
        )
        self.service.runtime.set_viewer_control.side_effect = RuntimeError("Viewer unavailable")
        app = create_app(self.config, "secret", 7890, self.service)
        with self.assertRaisesRegex(RuntimeError, "Viewer unavailable"):
            async with app.router.lifespan_context(app):
                self.fail("Startup should have failed")
        with self.assertRaises(sqlite3.ProgrammingError):
            self.service.state.row("alpha")

    async def test_dynamic_workspace_lifespans_close_in_their_own_context(self):
        self.service.observed = AsyncMock(return_value=[])
        app = create_app(self.config, "secret", 7890, self.service)
        updated = SimpleNamespace(
            **{**vars(self.config), "desktops": {"alpha": {}, "beta": {}, "copy": {}}}
        )
        async with app.router.lifespan_context(app):
            await asyncio.create_task(self.service.reconfigure(updated))
            token = self.service.state.register("copy", "worker", "task")
            await asyncio.create_task(self.service.reconfigure(self.config))
            self.assertNotIn("copy", self.service.locks)
            with self.assertRaises(ControlConflict):
                self.service.state.authenticate(token, "copy")
            await asyncio.create_task(self.service.reconfigure(updated))


class ToolKindTest(unittest.TestCase):
    def test_valet_tools_route_before_browser_prefix(self):
        self.assertEqual(tool_kind("browser_fill"), "valet")
        self.assertEqual(tool_kind("request_grant"), "valet")
        self.assertEqual(tool_kind("browser_fill_form"), "browser")
        self.assertEqual(tool_kind("screenshot"), "computer")


class ValetEndpointTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = SimpleNamespace(
            state_dir=Path(self.temp.name),
            desktops={"alpha": {}},
            project="test",
            path=Path(self.temp.name) / "hotdesk.toml",
        )
        self.service = DeskService(self.config)

    def tearDown(self):
        self.service.state.db.close()
        self.temp.cleanup()

    def row(self, **overrides):
        row = {
            "name": "alpha",
            "status": "running",
            "health": "healthy",
            "components": {"gateway": "ready"},
            "valet_url": None,
        }
        row.update(overrides)
        return row

    async def test_valet_not_enabled_reports_clean_error(self):
        self.service.observed = AsyncMock(return_value=[self.row()])
        with self.assertRaisesRegex(ControlConflict, "HOTDESK_VALET=1"):
            await self.service.endpoint("alpha", "valet")

    async def test_failed_valet_reports_component_not_missing_install(self):
        self.service.observed = AsyncMock(
            return_value=[
                self.row(
                    components={"valet": "failed", "gateway": "ready"},
                    valet_url="http://127.0.0.1:1234/valet",
                )
            ]
        )
        with self.assertRaisesRegex(ControlConflict, "not ready: valet"):
            await self.service.endpoint("alpha", "valet")

    async def test_valet_ready_returns_url(self):
        self.service.observed = AsyncMock(
            return_value=[
                self.row(
                    components={"valet": "ready", "gateway": "ready"},
                    valet_url="http://127.0.0.1:1234/valet",
                )
            ]
        )
        self.assertEqual(
            await self.service.endpoint("alpha", "valet"), "http://127.0.0.1:1234/valet"
        )
