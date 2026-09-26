import asyncio
import hmac
import os
from contextlib import asynccontextmanager

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from websockets.asyncio.client import connect


@asynccontextmanager
async def lifespan(app):
    async with httpx.AsyncClient(timeout=None, trust_env=False) as client:
        app.state.http = client
        yield


def authorized(headers):
    return hmac.compare_digest(
        headers.get("authorization", "").encode("latin-1"),
        ("Bearer " + os.environ["HOTDESK_GATEWAY_TOKEN"]).encode(),
    )


def upstream(path):
    kind, _, rest = path.partition("/")
    ports = {"computer": 8000, "browser": 8931, "viewer": 6901, "valet": 14401}
    if kind not in ports:
        return None
    return f"http://127.0.0.1:{ports[kind]}/{rest}"


async def proxy(request):
    if request.path_params["path"] == "health" and request.method == "GET":
        return JSONResponse({"ready": True})
    if not authorized(request.headers):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    url = upstream(request.path_params["path"])
    if url is None:
        return JSONResponse({"error": "Unknown endpoint"}, status_code=404)
    if request.url.query:
        url += "?" + request.url.query
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in {"host", "authorization", "connection", "content-length"}
    }
    client = request.app.state.http
    response = await client.send(
        client.build_request(request.method, url, headers=headers, content=await request.body()),
        stream=True,
    )

    async def chunks():
        try:
            async for chunk in response.aiter_raw():
                yield chunk
        finally:
            await response.aclose()

    return StreamingResponse(
        chunks(),
        status_code=response.status_code,
        headers={
            k: v
            for k, v in response.headers.items()
            if k.lower() not in {"transfer-encoding", "connection"}
        },
    )


async def socket_proxy(ws):
    if not authorized(ws.headers):
        await ws.close(code=1008)
        return
    await ws.accept(subprotocol="binary" if "binary" in ws.scope["subprotocols"] else None)
    async with connect(
        "ws://127.0.0.1:6901/websockify", subprotocols=["binary"], max_size=None, compression=None
    ) as remote:

        async def send():
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    return
                await remote.send(message.get("bytes") or message.get("text") or b"")

        async def receive():
            async for data in remote:
                if isinstance(data, bytes):
                    await ws.send_bytes(data)
                else:
                    await ws.send_text(data)

        tasks = [asyncio.create_task(send()), asyncio.create_task(receive())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


app = Starlette(
    lifespan=lifespan,
    routes=[
        WebSocketRoute("/viewer/websockify", socket_proxy),
        Route("/{path:path}", proxy, methods=["GET", "POST", "DELETE", "PUT", "PATCH", "OPTIONS"]),
    ],
)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001, log_level="warning")
