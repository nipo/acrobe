"""Frame and Session unit tests.

Catalog roundtrip (no Session needed), Session encode/decode of
mixed values, build_catalog/apply_catalog symmetry, and Session
rejection of unknown classes / tags.
"""

from dataclasses import dataclass

import cbor2
import pytest

from acrobe.engine import Batcher
from acrobe.node import Node
from acrobe.wire import (
    Catalog,
    FrameError,
    ProtocolError,
    Registry,
    Request,
    Response,
    Session,
    SessionError,
    decode_frame,
    encode_catalog,
    encode_protocol_error,
    encode_request,
    encode_response,
)


# ----- Test fixtures: build an isolated Registry per test -----

def _make_registry_with_node():
    reg = Registry()

    @dataclass
    class Op:
        value: int

    @dataclass
    class WrappedOp:
        op: Op
        label: str

    @dataclass
    class Boom(Exception):
        reason: str

    class N(Node, Batcher):
        def __init__(self):
            Node.__init__(self, "n")
            Batcher.__init__(self)

    reg.register(Op, "op", "10000000-0000-4000-8000-000000000001")
    reg.register(WrappedOp, "op", "10000000-0000-4000-8000-000000000002")
    reg.register(Boom, "error", "10000000-0000-4000-8000-0000000000fe")
    reg.register(N, "node", "10000000-0000-4000-8000-0000000000ff",
                 uses=[Op, WrappedOp, Boom])
    return reg, Op, WrappedOp, Boom, N


# ----- Catalog frame -----

def test_catalog_frame_roundtrip():
    reg, Op, WrappedOp, Boom, N = _make_registry_with_node()
    server = Session(reg)
    catalog = server.build_catalog(N)

    raw = encode_catalog(catalog)
    decoded = decode_frame(raw)
    assert isinstance(decoded, Catalog)
    assert decoded.node_uuid == catalog.node_uuid
    assert decoded.node_tag == catalog.node_tag
    assert decoded.types == catalog.types


def test_apply_catalog_mirrors_server_tags():
    reg, Op, WrappedOp, Boom, N = _make_registry_with_node()
    server = Session(reg)
    catalog = server.build_catalog(N)

    client = Session(reg)
    client.apply_catalog(catalog)

    for cls in (N, Op, WrappedOp, Boom):
        assert client.class_to_tag[cls] == server.class_to_tag[cls]


def test_apply_catalog_rejects_unknown_node():
    reg_a, Op, _, _, N_a = _make_registry_with_node()
    server = Session(reg_a)
    catalog = server.build_catalog(N_a)

    # Fresh registry without that node UUID.
    reg_b = Registry()

    @dataclass
    class Other:
        v: int

    reg_b.register(Other, "op", "20000000-0000-4000-8000-000000000001")

    client = Session(reg_b)
    with pytest.raises(SessionError, match="not in registry"):
        client.apply_catalog(catalog)


def test_apply_catalog_skips_unknown_use_types():
    """Client missing some op classes should still install the rest."""
    reg_full, Op, WrappedOp, Boom, N = _make_registry_with_node()
    server = Session(reg_full)
    catalog = server.build_catalog(N)

    # Build a client registry that only knows Op and N (not WrappedOp/Boom).
    reg_lite = Registry()

    @dataclass
    class Op2:
        value: int

    class N2(Node, Batcher):
        def __init__(self):
            Node.__init__(self, "n")
            Batcher.__init__(self)

    reg_lite.register(Op2, "op", "10000000-0000-4000-8000-000000000001")
    reg_lite.register(N2, "node", "10000000-0000-4000-8000-0000000000ff",
                      uses=[Op2])

    client = Session(reg_lite)
    client.apply_catalog(catalog)

    assert Op2 in client.class_to_tag
    assert N2 in client.class_to_tag
    # Unknown UUIDs were skipped.
    assert len(client.class_to_tag) == 2


# ----- Session value encoding -----

def test_encode_decode_primitive_passthrough():
    reg, Op, _, _, N = _make_registry_with_node()
    s = Session(reg)
    s.build_catalog(N)
    for v in (None, 0, 42, "x", b"abc", True, False, 1.5):
        assert s.decode_value(s.encode_value(v)) == v


def test_encode_decode_registered_transportable():
    reg, Op, WrappedOp, Boom, N = _make_registry_with_node()
    s = Session(reg)
    s.build_catalog(N)

    inst = Op(value=7)
    encoded = s.encode_value(inst)
    assert isinstance(encoded, cbor2.CBORTag)
    assert encoded.tag == s.class_to_tag[Op]
    assert s.decode_value(encoded) == inst


def test_encode_decode_nested_structures():
    reg, Op, WrappedOp, _, N = _make_registry_with_node()
    s = Session(reg)
    s.build_catalog(N)

    payload = [
        Op(value=1),
        {"k": Op(value=2)},
        [Op(value=3), Op(value=4)],
    ]
    decoded = s.decode_value(s.encode_value(payload))
    assert decoded == payload


def test_encode_unregistered_class_raises():
    reg, _, _, _, N = _make_registry_with_node()
    s = Session(reg)
    s.build_catalog(N)

    class Unrelated:
        pass

    with pytest.raises(SessionError, match="not a registered Transportable"):
        s.encode_value(Unrelated())


def test_decode_unknown_tag_raises():
    reg, _, _, _, N = _make_registry_with_node()
    s = Session(reg)
    s.build_catalog(N)

    with pytest.raises(SessionError, match="not in session catalog"):
        s.decode_value(cbor2.CBORTag(9999, [1]))


# ----- Request / Response frames -----

def test_request_frame_roundtrip():
    reg, Op, WrappedOp, _, N = _make_registry_with_node()
    server = Session(reg)
    catalog = server.build_catalog(N)

    client = Session(reg)
    client.apply_catalog(catalog)

    request = Request(
        req_id=42,
        batch=[Op(value=1), WrappedOp(op=Op(value=2), label="k")])
    raw = encode_request(request, client)

    decoded = decode_frame(raw, server)
    assert decoded.req_id == 42
    assert decoded.batch == request.batch


def test_response_frame_roundtrip_with_results_and_errors():
    reg, Op, WrappedOp, Boom, N = _make_registry_with_node()
    server = Session(reg)
    client = Session(reg)
    client.apply_catalog(server.build_catalog(N))

    response = Response(
        req_id=7,
        results={0: 100, 2: "ok"},
        errors={1: Boom(reason="nope")})
    raw = encode_response(response, server)
    decoded = decode_frame(raw, client)

    assert decoded.req_id == 7
    assert decoded.results == {0: 100, 2: "ok"}
    assert isinstance(decoded.errors[1], Boom)
    assert decoded.errors[1].reason == "nope"


def test_protocol_error_frame_roundtrip():
    raw = encode_protocol_error(
        ProtocolError(req_id=11, kind="bad_frame", payload="went wrong"))
    decoded = decode_frame(raw)
    assert isinstance(decoded, ProtocolError)
    assert decoded.req_id == 11
    assert decoded.kind == "bad_frame"
    assert decoded.payload == "went wrong"


def test_decode_request_without_session_raises():
    reg, Op, _, _, N = _make_registry_with_node()
    server = Session(reg)
    server.build_catalog(N)
    raw = encode_request(Request(req_id=1, batch=[Op(value=1)]), server)
    with pytest.raises(FrameError, match="requires a Session"):
        decode_frame(raw)


def test_decode_malformed_bytes_raises():
    with pytest.raises(FrameError):
        decode_frame(b"\xff\xff\xff\xff")
