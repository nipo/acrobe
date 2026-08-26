"""Frame schema for the wire protocol.

Frames are CBOR-encoded arrays whose first element identifies the
frame kind. The remaining elements are kind-specific.

Kind layout:

    Catalog        [1, node_uuid_bytes, node_tag, {type_uuid_bytes: tag}]
    Request        [2, req_id, [op, ...]]
    Response       [3, req_id, {idx: result_value}, {idx: error_value}]
    ProtocolError  [4, req_id_or_null, kind_str, payload_or_null]

Inside Request and Response, individual op / result / error values
are encoded by the active `Session`: registered Transportables become
CBOR tagged values whose tag is the session-local tag negotiated in
the catalog. Primitives pass through unchanged.

The Catalog frame is special — it carries no tagged values (the tag
table is what it's *establishing*), so it can be decoded without a
Session.
"""

import uuid as uuid_lib
from dataclasses import dataclass, field
from typing import Any

import cbor2


KIND_CATALOG = 1
KIND_REQUEST = 2
KIND_RESPONSE = 3
KIND_PROTOCOL_ERROR = 4


class FrameError(Exception):
    """Raised when a frame can't be parsed or encoded.

    These are protocol-level failures (malformed bytes, unknown frame
    kind, missing session for a non-catalog frame) — not application
    errors carried inside a Response.
    """


@dataclass
class Catalog:
    node_uuid: uuid_lib.UUID
    node_tag: int
    types: dict[uuid_lib.UUID, int] = field(default_factory=dict)


@dataclass
class Request:
    req_id: int
    batch: list[Any] = field(default_factory=list)


@dataclass
class Response:
    req_id: int
    results: dict[int, Any] = field(default_factory=dict)
    errors: dict[int, Any] = field(default_factory=dict)


@dataclass
class ProtocolError:
    req_id: int | None
    kind: str
    payload: Any = None


def encode_catalog(catalog: Catalog) -> bytes:
    return cbor2.dumps([
        KIND_CATALOG,
        catalog.node_uuid.bytes,
        catalog.node_tag,
        {uid.bytes: tag for uid, tag in catalog.types.items()},
    ])


def encode_request(request: Request, session) -> bytes:
    return cbor2.dumps([
        KIND_REQUEST,
        request.req_id,
        [session.encode_value(op) for op in request.batch],
    ])


def encode_response(response: Response, session) -> bytes:
    return cbor2.dumps([
        KIND_RESPONSE,
        response.req_id,
        {i: session.encode_value(v) for i, v in response.results.items()},
        {i: session.encode_value(e) for i, e in response.errors.items()},
    ])


def encode_protocol_error(err: ProtocolError) -> bytes:
    return cbor2.dumps([
        KIND_PROTOCOL_ERROR,
        err.req_id,
        err.kind,
        err.payload,
    ])


def decode_frame(data: bytes, session=None):
    """Decode any frame kind from raw CBOR bytes.

    `session` is required for frame kinds that contain session-tagged
    values (Request, Response). Catalog and ProtocolError decode
    without it.
    """
    try:
        raw = cbor2.loads(data)
    except cbor2.CBORDecodeError as exc:
        raise FrameError(f"malformed CBOR: {exc}") from exc

    if not isinstance(raw, list) or not raw:
        raise FrameError(f"frame must be a non-empty array, got {type(raw).__name__}")

    kind = raw[0]

    if kind == KIND_CATALOG:
        if len(raw) != 4:
            raise FrameError(f"Catalog frame: expected 4 elements, got {len(raw)}")
        try:
            return Catalog(
                node_uuid=uuid_lib.UUID(bytes=raw[1]),
                node_tag=raw[2],
                types={uuid_lib.UUID(bytes=k): v for k, v in raw[3].items()})
        except (ValueError, TypeError) as exc:
            raise FrameError(f"Catalog frame: bad payload — {exc}") from exc

    if kind == KIND_PROTOCOL_ERROR:
        if len(raw) != 4:
            raise FrameError(
                f"ProtocolError frame: expected 4 elements, got {len(raw)}")
        return ProtocolError(req_id=raw[1], kind=raw[2], payload=raw[3])

    if session is None:
        raise FrameError(
            f"frame kind {kind} requires a Session to decode")

    if kind == KIND_REQUEST:
        if len(raw) != 3:
            raise FrameError(f"Request frame: expected 3 elements, got {len(raw)}")
        return Request(
            req_id=raw[1],
            batch=[session.decode_value(o) for o in raw[2]])

    if kind == KIND_RESPONSE:
        if len(raw) != 4:
            raise FrameError(f"Response frame: expected 4 elements, got {len(raw)}")
        return Response(
            req_id=raw[1],
            results={k: session.decode_value(v) for k, v in raw[2].items()},
            errors={k: session.decode_value(v) for k, v in raw[3].items()})

    raise FrameError(f"unknown frame kind {kind}")
