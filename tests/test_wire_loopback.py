"""Phase-2 in-process loopback test.

Builds a real `EchoNode` Batcher with three op types — Echo, Shout,
Boom — runs the full client→server→client cycle through the frame
codec and `handle_request`, and asserts on the decoded Response.

Demonstrates:
- Server allocates session tags via `build_catalog`, client mirrors
  via `apply_catalog`.
- Request frames carry CBOR-tagged ops; Response frames carry mixed
  primitive results and Transportable errors.
- Declared errors (`Boom`) survive the round trip as themselves.
- Undeclared exceptions get wrapped in `InternalError`.

The test uses an isolated `Registry` so it doesn't entangle with the
default registry's contents.
"""

from dataclasses import dataclass

import pytest

from acrobe.engine import Batcher
from acrobe.node import Node
from acrobe.wire import (
    InternalError,
    Registry,
    Request,
    Session,
    decode_frame,
    encode_request,
    encode_response,
    handle_request,
)


# ----- Test types -----

@dataclass
class Echo:
    value: int


@dataclass
class Shout:
    text: str
    times: int = 1


@dataclass
class Whisper:
    """Op with no return value — its future resolves to None."""
    text: str


@dataclass
class Boom(Exception):
    reason: str

    def __str__(self):
        return f"Boom({self.reason})"


@dataclass
class Surprise(Exception):
    """Not registered — server raises this and the dispatcher must
    wrap it into InternalError before transit."""
    note: str


class EchoNode(Node, Batcher):
    """Batcher that interprets Echo / Shout / Whisper / Boom directly."""

    def __init__(self):
        Node.__init__(self, "echo")
        Batcher.__init__(self)

    async def flush_ops(self, batch):
        for op, fut in batch:
            if fut.done():
                continue
            if isinstance(op, Echo):
                fut.set_result(op.value * 2)
            elif isinstance(op, Shout):
                fut.set_result((op.text.upper() + "! ") * op.times)
            elif isinstance(op, Whisper):
                fut.set_result(None)
            elif isinstance(op, Boom):
                fut.set_exception(op)
            elif isinstance(op, _RaiseSurprise):
                fut.set_exception(Surprise(note=op.note))
            else:
                fut.set_exception(
                    RuntimeError(f"unknown op {type(op).__name__}"))


@dataclass
class _RaiseSurprise:
    """Carrier op that triggers an unhandled server-side exception."""
    note: str


def _make_registry():
    reg = Registry()
    reg.register(Echo, "op", "30000000-0000-4000-8000-000000000001")
    reg.register(Shout, "op", "30000000-0000-4000-8000-000000000002")
    reg.register(Whisper, "op", "30000000-0000-4000-8000-000000000003")
    reg.register(_RaiseSurprise, "op", "30000000-0000-4000-8000-000000000004")
    reg.register(Boom, "error", "30000000-0000-4000-8000-0000000000fe")
    # InternalError is also registered in the default global registry,
    # but each Registry is independent; we re-register it here so the
    # test runs in isolation. Same UUID/class on both sides.
    reg.register(InternalError, "error", str(InternalError.__wire_uuid__))
    reg.register(EchoNode, "node", "30000000-0000-4000-8000-0000000000ff",
                 uses=[Echo, Shout, Whisper, _RaiseSurprise, Boom,
                       InternalError])
    return reg


# ----- The loopback -----

@pytest.mark.asyncio
async def test_loopback_results_and_declared_error():
    reg = _make_registry()

    server = Session(reg)
    catalog = server.build_catalog(EchoNode)
    client = Session(reg)
    client.apply_catalog(catalog)

    node = EchoNode()

    request = Request(
        req_id=1,
        batch=[
            Echo(value=21),                     # → 42
            Shout(text="hi", times=2),          # → "HI! HI! "
            Whisper(text="psst"),               # → None
            Boom(reason="kaboom"),              # → Boom error
        ])

    request_bytes = encode_request(request, client)
    received = decode_frame(request_bytes, server)

    response = await handle_request(node, server, received)

    response_bytes = encode_response(response, server)
    decoded = decode_frame(response_bytes, client)

    assert decoded.req_id == 1
    assert decoded.results == {0: 42, 1: "HI! HI! ", 2: None}
    assert set(decoded.errors.keys()) == {3}
    err = decoded.errors[3]
    assert isinstance(err, Boom)
    assert err.reason == "kaboom"


@pytest.mark.asyncio
async def test_loopback_wraps_unhandled_exception():
    reg = _make_registry()

    server = Session(reg)
    client = Session(reg)
    client.apply_catalog(server.build_catalog(EchoNode))

    node = EchoNode()
    request = Request(req_id=2, batch=[_RaiseSurprise(note="oops")])
    received = decode_frame(encode_request(request, client), server)
    response = await handle_request(node, server, received)
    decoded = decode_frame(encode_response(response, server), client)

    assert decoded.results == {}
    assert set(decoded.errors.keys()) == {0}
    wrapped = decoded.errors[0]
    assert isinstance(wrapped, InternalError)
    assert "Surprise" in wrapped.representation
    assert "oops" in wrapped.representation


@pytest.mark.asyncio
async def test_loopback_concurrent_batches_in_flight():
    """Two batches posted before either is awaited — req_ids correlate
    correctly. (One socket, multiple in-flight requests.)"""
    reg = _make_registry()
    server = Session(reg)
    client = Session(reg)
    client.apply_catalog(server.build_catalog(EchoNode))

    node = EchoNode()

    r1 = Request(req_id=10, batch=[Echo(value=1), Echo(value=2)])
    r2 = Request(req_id=20, batch=[Echo(value=100)])

    # Both batches dispatched, then responses encoded in reverse order
    # to demonstrate req_id is the only correlation key.
    decoded_r1 = decode_frame(encode_request(r1, client), server)
    decoded_r2 = decode_frame(encode_request(r2, client), server)

    resp2 = await handle_request(node, server, decoded_r2)
    resp1 = await handle_request(node, server, decoded_r1)

    out2 = decode_frame(encode_response(resp2, server), client)
    out1 = decode_frame(encode_response(resp1, server), client)

    assert out1.req_id == 10
    assert out1.results == {0: 2, 1: 4}
    assert out2.req_id == 20
    assert out2.results == {0: 200}
