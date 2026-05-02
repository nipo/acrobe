"""WebSocket upgrade handler.

URL space mirrors REST: `WS /v1/node/<path>?token=<token>`. The
upgrade phase resolves the node, validates the token via the
AuthBackend, opens a per-connection Session, and sends the catalog
as the first frame.

After the handshake, the connection runs a concurrent dispatch
loop: every incoming Request frame is handed to a fresh asyncio
task, so multiple in-flight batches don't serialize. Each task
sends its own Response when ready. A bounded send queue serializes
writes to keep frame ordering well-defined; reading and writing
run as two cooperating tasks.

Connection lifetime = ref lifetime: when the WS closes, the
implicit hold on the node ends. The node itself is shared with
the rest of the server; the WS just borrows it.
"""

import asyncio

import aiohttp
from aiohttp import web

from ..dispatch import handle_request
from ..frame import (
    FrameError,
    ProtocolError,
    Request,
    decode_frame,
    encode_catalog,
    encode_protocol_error,
    encode_response,
)
from ..session import Session
from .rest import WIRE_SERVER_KEY


async def handle_ws(request: web.Request) -> web.WebSocketResponse:
    server = request.app[WIRE_SERVER_KEY]

    raw_path = request.match_info.get("path", "")
    parts = [p for p in raw_path.strip("/").split("/") if p]

    try:
        node = await server.resolve_node(parts)
    except Exception as exc:
        raise web.HTTPNotFound(reason=f"node not found: {exc}")

    entry = server.registry.try_lookup_by_class(type(node))
    if entry is None or entry.kind != "node":
        raise web.HTTPBadRequest(
            reason=f"{type(node).__name__} is not a registered "
                   f"@wire.node — cannot open over the wire")

    token = request.query.get("token", "")
    try:
        principal, scope = server.auth.validate_connect_token(token, node)
    except Exception as exc:
        raise web.HTTPUnauthorized(reason=str(exc))

    ws = web.WebSocketResponse()
    await ws.prepare(request)

    session = Session(server.registry)
    catalog = session.build_catalog(type(node))
    await ws.send_bytes(encode_catalog(catalog))

    send_queue: asyncio.Queue = asyncio.Queue()
    inflight: set[asyncio.Task] = set()

    async def writer():
        while True:
            data = await send_queue.get()
            if data is None:
                return
            await ws.send_bytes(data)

    writer_task = asyncio.create_task(writer())

    async def dispatch_one(req: Request):
        try:
            response = await handle_request(node, session, req)
            await send_queue.put(encode_response(response, session))
        except Exception as exc:
            err = ProtocolError(req_id=req.req_id, kind="dispatch_failed",
                                payload=repr(exc))
            await send_queue.put(encode_protocol_error(err))

    try:
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.BINARY:
                continue

            try:
                frame = decode_frame(msg.data, session)
            except FrameError as exc:
                err = ProtocolError(req_id=None, kind="frame_error",
                                    payload=str(exc))
                await send_queue.put(encode_protocol_error(err))
                continue

            if isinstance(frame, Request):
                task = asyncio.create_task(dispatch_one(frame))
                inflight.add(task)
                task.add_done_callback(inflight.discard)
            else:
                err = ProtocolError(
                    req_id=getattr(frame, "req_id", None),
                    kind="unexpected_frame",
                    payload=type(frame).__name__)
                await send_queue.put(encode_protocol_error(err))
    finally:
        # Wait for any in-flight dispatch to complete so its Response
        # makes it onto the wire before close.
        if inflight:
            await asyncio.gather(*inflight, return_exceptions=True)
        await send_queue.put(None)
        await writer_task
        if not ws.closed:
            await ws.close()

    return ws


