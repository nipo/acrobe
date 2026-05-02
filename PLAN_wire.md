# Wire: client/server transport for acrobe

Concurrent multi-client service exposing acrobe Batcher nodes over the
network, with friendly LAN-first ergonomics and seams for later authz.

## Goals

* Remote adapter discovery (parity with `acrobe info enumerate`).
* Open any *Batcher* node from a remote client; receive a local proxy
  that mimics the server-side API.
* Multiple concurrent clients on a single server, each with its own
  set of opened refs.
* Same module is the client library and the server entry point.

Non-goals for now: authn/authz enforcement, TLS, mDNS discovery,
remote enumeration triggering hardware probes from untrusted callers,
plain (non-Batcher) Node transport.

## Architecture

### Transport split

* **REST/HTTP for enumeration.** `GET /node/<path>` returns metadata
  + child list. JSON by default; CBOR on `Accept: application/cbor`.
  URL paths map 1:1 to acrobe hardware paths. Browsable with curl.
* **WebSocket per opened node.** `WS /node/<path>?token=<token>` opens
  one Batcher ref. Connection lifetime = ref lifetime = adapter
  ownership lifetime. No multiplexing across nodes; the kernel
  handles fairness and HOL avoidance via independent sockets.
* **Protocol version in the URL path:** `/v1/node/...` and `/v1/ws/...`.

### Serialization

CBOR everywhere on the wire. Tagged objects use *session-local* tags
issued by the server in the handshake; canonical identity of a type
is its UUID, declared at class definition time.

### Auth (deferred but seam-wired)

* Capability URL pattern: REST enumeration response embeds a
  `connect_url` carrying an HMAC-signed token in the query string.
* `OpenAuthBackend` is the only implementation for now: empty token,
  `Principal.anonymous()`, all scopes granted, `True` for every
  `authorize()`.
* Code threads `Principal` and `Scope` through every entry point from
  day one. No `if auth_enabled:` branches anywhere.
* `audit_log(principal, node, action, outcome)` exists as a no-op hook
  at the relevant call sites.

## IDL

### Decorators

```python
@wire.op("FCD615F4-...")
@dataclass
class Run(JtagOperation):
    cycles: int = 0

@wire.error("3F1A...")
@dataclass
class OpenChain(JtagError):
    detected: int
    expected: int

@wire.node("A8F082D4-...",
           uses=[Run, Shift, CaptureDr, CaptureIr, Reset, SwdToJtag,
                 OpenChain, ClosedChain])
class JtagInterface(Node, Batcher):
    ...
```

### Decorator behavior

* UUID parsed and uniqueness-checked against the global registry at
  import time. Duplicates raise.
* `@wire.op` / `@wire.error` require the class to already be a
  dataclass (`__dataclass_fields__` present). Reject otherwise.
* Field types must be primitives (`int`, `str`, `bytes`, `bool`,
  `float`, `None`) or other registered Transportables. Untyped or
  `Any` fields rejected.
* `@wire.node` requires the class to subclass `Node`; if `commands` is
  non-empty, also `Batcher`.
* `uses=[...]` entries must each already be `@wire.op` or
  `@wire.error` registered. Mixed list, no semantic split at the
  node level.
* Same Op or Error class may appear in multiple nodes' lists.

Only Batchers are transportable for v1. Plain-Node transport is
deferred until a concrete need surfaces.

### Op / response model

Operations carry only input fields. Posting an op returns a future
resolving to the response value — a primitive, `None`, or another
registered Transportable. The legacy "op carries result via
mutation" pattern (e.g. `Shift.tdo` populated by the executor) is
not used in new wire-targeted code; existing JTAG ops are
refactored to drop it when phase 4 lands.

### Non-dataclass Transportables

Classes that can't reasonably be dataclasses (computed fields,
existing public API, externally-imposed shape) may be registered
without `@dataclass`. They MUST then implement `__cbor_encode__` /
`__cbor_decode__` classmethods. The decorator allows this branch
explicitly; the field-introspection branch is rejected.

### Codec

* Default ser/deser walks `dataclasses.fields(cls)` in declaration
  order, encodes as CBOR array (positional) wrapped in the
  session-local tag.
* Override hook: `__cbor_encode__` / `__cbor_decode__` classmethods
  for types needing custom layout (e.g. zero-copy bstr).
* No backward-compat shims. Mismatched UUIDs between client and
  server registries are hard errors at handshake time.

## Wire protocol

### Frame schema (CBOR)

```
Request:      {tag=req,    req_id, batch: [op, ...]}
Response:     {tag=resp,   req_id, results: {idx: value}, errors: {idx: err}}
Error:        {tag=err,    req_id, kind, payload?}     # request-level failure
Notification: {tag=notif,  event, payload}             # not used in v1
Cancel:       not implemented; cancellation is local-only
```

* Multiple in-flight batches per socket, correlated by `req_id`.
* Within a batch, `results` and `errors` are sparse maps keyed by
  batch index; ops without a future produce no entry.
* Ops are identified by their session-local tag; no separate `method`
  field — the op class *is* the operation.

### Session handshake

After WS upgrade, server sends a single catalog frame describing the
node's interface scoped to that connection:

```
{
  node_uuid: "A8F082D4-...",
  node_tag:  100,
  ops:       {"FCD615F4-...": 101, "...": 102, ...},
  errors:    {"3F1A-...": 200, ...},
}
```

Client matches the catalog against its local registry. Unknown
node_uuid → close socket with a clear error. Unknown op/error UUIDs
are tolerated — operations the client doesn't know simply aren't
callable on its side.

Tags are session-local; reconnection re-issues the catalog with
fresh tag numbers.

### Lifecycle

* Connection drop = ref gone. No automatic reconnection. Client
  surfaces the error to the caller, same as a local hardware
  disconnect.
* Built-in WebSocket ping/pong handles keepalive; no app-level
  heartbeat.
* Adapter ownership: no lock; the server-side parent serializes
  batches between concurrent owners (matches local Batcher
  semantics today).
* Cancellation: a client-side awaiter cancellation drops the local
  Future; the server keeps running and its eventual response is
  ignored. No `cancel` frame.

## Module layout (proposed)

```
acrobe/wire/
    __init__.py       # public re-exports: wire.op, wire.node, wire.error
    registry.py       # UUID registry, decorator implementations
    codec.py          # CBOR encoding/decoding, dataclass introspection
    handshake.py      # session catalog construction & negotiation
    frame.py          # frame schema, encoding/decoding
    auth.py           # AuthBackend interface, OpenAuthBackend
    principal.py      # Principal and Scope value objects
    server/
        __init__.py
        rest.py       # GET /node/... handlers
        ws.py         # WS /node/... handlers, batch dispatch
        app.py        # ASGI/aiohttp app wiring
    client/
        __init__.py
        rest.py       # REST enumeration client
        ws.py         # WS connection + batch send/receive
        proxy.py      # Proxy class generation for remote Batchers
    debug.py          # dump_idl(), introspection helpers
```

## Phased implementation

### Phase 1: IDL plumbing (no sockets)

* `wire.op`, `wire.error`, `wire.node` decorators + global registry.
* CBOR codec with dataclass introspection + override hook.
* `Principal`, `Scope`, `AuthBackend`, `OpenAuthBackend` skeletons.
* `audit_log` no-op hook.
* Synthetic `EchoNode` example exercising the full surface: a
  trivial `EchoOp(value: int)`, an op with a nested registered
  type, and a registered error. Smoke target for the codec and the
  dump-idl command.
* `wire dump-idl` CLI command — walks the registry, prints the IDL
  for each registered node type.
* Roundtrip test: encode an Op instance, decode, assert equality.

### Phase 2: Wire protocol (in-process)

* Frame schema implementation.
* Session handshake: build catalog from a node class, negotiate
  tags, encode/decode frames using the negotiated tag table.
* In-process loopback test: client and server in the same process,
  exchange Frames over an asyncio Queue pair. Post a batch, receive
  results.

### Phase 3: REST enumeration

* `GET /node/...` handlers. JSON output. CBOR with content negotiation.
* HATEOAS: response includes `connect_url` produced by `AuthBackend`.
* Probe-serialization on adapter-touching paths (reuse existing local
  enumeration code, single asyncio lock per adapter).
* `acrobe info enumerate -r http://host:port/` parity with local CLI.

### Phase 4: WebSocket transport + JTAG decoration

* Refactor JTAG operation classes (`Shift`, `Run`, `Reset`, ...)
  to dataclasses or to provide explicit `__cbor_encode__` /
  `__cbor_decode__`. Drop the result-via-mutation pattern; have
  the JTAG batcher resolve futures with response values directly.
* Apply `@wire.op` / `@wire.error` / `@wire.node` to JTAG.
* Server: WS upgrade handler at `/v1/node/...`, opens a Batcher ref,
  emits catalog, then runs the request/response loop.
* Client: WS open, parse catalog, build proxy, expose Batcher API
  identical to the local one.
* End-to-end test: spin up server in a fixture, run an existing
  acrobe script against the remote URL, get the same results.

### Phase 5: CLI integration

* `-r <url>` flag on `acrobe info enumerate` and friends.
* Connection string handling: bare URL, with `?token=` once auth
  is wired later.

## Deferred (not in v1)

* HmacAuthBackend with real token issuance/validation.
* TLS/wss.
* mDNS discovery (`_acrobe._tcp`).
* Plain-Node transport (non-Batcher).
* Server-pushed notifications and subscribe/unsubscribe.
* Multi-tenant isolation, rate limiting, quotas.
* Schema evolution / forward-compat. UUID mismatches stay hard errors.
* Cross-batch result streaming. CBOR indefinite-length bstr is
  available in the codec but ops are chunked at API design time.

## Open questions

* Catalog frame: standalone CBOR object or wrapped in the same frame
  envelope as requests/responses? Probably standalone, distinct phase.
* REST output format: full child objects vs. references-only with
  follow-up GETs? Probably full for v1 to keep round trips down.
* Error frame at the request level (`tag=err`) vs. always reporting
  errors via `Response.errors`. Lean toward the latter for batch-level
  errors; reserve `tag=err` for protocol-level failures (malformed
  frame, unknown ref, etc).
