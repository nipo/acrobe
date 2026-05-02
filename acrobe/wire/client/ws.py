"""WebSocket client.

`WireClient.connect(url, registry)` opens a WS to the wire server,
reads the initial Catalog frame, builds a `Session`, and starts a
reader task that drains incoming frames and resolves per-req_id
futures.

`send_batch(batch)` returns the decoded `Response` for the batch.
Concurrent batches are tracked by req_id; they may complete in
any order on the wire.

Typically wrapped by `RemoteBatcher` (see `proxy.py`) which mimics
the local Batcher API. Direct use is fine when the caller wants
explicit control.
"""

import asyncio
import itertools
from typing import Optional

import aiohttp

from ..frame import (
    Catalog,
    FrameError,
    ProtocolError,
    Request,
    Response,
    decode_frame,
    encode_request,
)
from ..registry import Registry
from ..session import Session


class WireClientError(Exception):
    """Raised when the server reports a protocol-level failure (frame
    malformed, dispatch crashed, etc). Application-level errors come
    back inside Response.errors and are re-raised by RemoteBatcher,
    not by WireClient."""

    def __init__(self, kind: str, payload):
        super().__init__(f"{kind}: {payload}")
        self.kind = kind
        self.payload = payload


class WireClient:
    """Low-level WS client. Handshakes, sends Requests, receives
    Responses correlated by req_id. Lifetime is tied to the
    connection; on close, all pending futures are cancelled."""

    def __init__(self, ws: aiohttp.ClientWebSocketResponse,
                 session: Session,
                 http_session: Optional[aiohttp.ClientSession] = None):
        self._ws = ws
        self._session = session
        self._http_session = http_session
        self._owns_http_session = http_session is not None
        self._pending: dict[int, asyncio.Future] = {}
        self._req_ids = itertools.count(1)
        self._reader_task = asyncio.create_task(self._read_loop())
        self._closed = False

    @property
    def session(self) -> Session:
        return self._session

    @classmethod
    async def connect(cls, url: str, registry: Registry,
                      *, http_session: Optional[aiohttp.ClientSession] = None
                      ) -> "WireClient":
        owns_session = http_session is None
        session_to_use = http_session or aiohttp.ClientSession()
        try:
            ws = await session_to_use.ws_connect(url)
            first = await ws.receive()
            if first.type != aiohttp.WSMsgType.BINARY:
                raise WireClientError(
                    "handshake_failed",
                    f"expected BINARY catalog frame, got {first.type!r}")
            catalog = decode_frame(first.data)
            if not isinstance(catalog, Catalog):
                raise WireClientError(
                    "handshake_failed",
                    f"first frame must be Catalog, got {type(catalog).__name__}")
            session = Session(registry)
            session.apply_catalog(catalog)
            return cls(ws, session,
                       http_session=session_to_use if owns_session else None)
        except Exception:
            if owns_session:
                await session_to_use.close()
            raise

    async def _read_loop(self):
        try:
            async for msg in self._ws:
                if msg.type != aiohttp.WSMsgType.BINARY:
                    continue
                try:
                    frame = decode_frame(msg.data, self._session)
                except FrameError:
                    continue

                if isinstance(frame, Response):
                    fut = self._pending.pop(frame.req_id, None)
                    if fut is not None and not fut.done():
                        fut.set_result(frame)
                elif isinstance(frame, ProtocolError):
                    err = WireClientError(frame.kind, frame.payload)
                    if frame.req_id is not None:
                        fut = self._pending.pop(frame.req_id, None)
                        if fut is not None and not fut.done():
                            fut.set_exception(err)
        finally:
            # On socket close, fail every still-pending future so
            # awaiters wake up rather than hang forever.
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(
                        WireClientError("connection_closed",
                                        "WS closed before response"))
            self._pending.clear()

    async def send_batch(self, batch: list) -> Response:
        if self._closed:
            raise WireClientError("closed", "client already closed")
        req_id = next(self._req_ids)
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._pending[req_id] = fut
        request = Request(req_id=req_id, batch=batch)
        await self._ws.send_bytes(encode_request(request, self._session))
        return await fut

    async def close(self):
        if self._closed:
            return
        self._closed = True
        await self._ws.close()
        try:
            await self._reader_task
        except Exception:
            pass
        if self._owns_http_session and self._http_session is not None:
            await self._http_session.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()
