# Wire: client/server transport for acrobe

Concurrent multi-client service exposing acrobe Batcher nodes over the
network, with friendly LAN-first ergonomics and seams for later authz.

## Status

Phases 1–5 plus follow-on polish are landed. Today the wire stack:

* Carries `JtagInterface` end-to-end (REST enumeration + WS-transported
  bit-level ops) through a hybrid local/remote tree.
* Uses **cutoff-aware summon**: walking `wire/<server>/<path>` finds
  the deepest segment whose `wire_uuid` matches a locally-registered
  `@wire.node`, opens WS there, walks the remainder locally on a
  proxy that IS-A the registered class (so subclass `child_spawn` /
  `db` / instance methods all work via MRO).
* Has a process-wide **lifecycle** registry (`acrobe.shutdown()`,
  `acrobe.on_shutdown(cb)`) — wire client, USB context, FTDI
  transport, AJI client and XVC client all register cleanup.
* Configures servers via `~/.config/acrobe.conf` (YAML), exposed as
  `wire.servers.<name>.base`, materialized as children of a
  `WireEnumerator` that lives next to USB/AJI/XVC enumerators on
  `HwRoot`.

Test suite: 1029 passed. All wire layers covered by integration
tests (synthetic JTAG over a real socket via aiohttp's TestServer).

## Goals

* Remote adapter discovery (parity with `acrobe info enumerate`).
* Open any *Batcher* node from a remote client; receive a local proxy
  that mimics the server-side API.
* Multiple concurrent clients on a single server, each with its own
  set of opened refs.
* Same module is the client library and the server entry point.

Non-goals for v1: authn/authz enforcement, TLS, mDNS discovery,
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

## Deferred work

The pieces below are intentionally out of scope today. Each entry
captures the rationale and a sketch of the design direction so we
can resume without rebuilding context.

### Chain / Tap transport (blocked on dynamic geometry)

JtagInterface is the only `@wire.node` along the JTAG stack today.
The natural extension is to also transport `Chain` and `Tap` so the
cutoff lands deeper for cleaner per-tap operations and less
client-side wire traffic for tap-level work. Initial sketch:

* `@wire.op` on `_TapShift`, `_TapRun`, `_TapIrStatus`.
* `Tap.__init__` populates `self._metadata = {idcode, irlen, ...}`
  so REST surfaces the info needed to pick the subclass.
* `_wire_proxy_spec(remote_info) -> (concrete_class, init_kwargs)`
  hook on the `@wire.node` class. `Tap` overrides to look up the
  right subclass through `Tap.db` from the IDCODE; the proxy is
  then `make_remote_proxy(Agilex5E, ...)` not generic Tap.
* Server-side: ops post to the real `Tap` subclass, which forwards
  through its local `Chain` → `JtagInterface` → hardware exactly
  as today.

**Blocker — dynamic chain geometry.** Modern SoC/FPGA families
(Agilex, Zynq, ZynqMP, …) reconfigure the JTAG chain at runtime:
IR length, device count and the chain's logical topology shift as
the SDM/PSU loads firmware. Today's `Chain.discover` runs once
and caches geometry; the wire layer would inherit a stale view as
soon as the chain changes.

Until the local code has a clean answer for re-discovery (events?
explicit refresh? continuous monitoring?), the wire layer can't
faithfully expose Chain/Tap state. JtagInterface stays the only
cutoff, and Chain+Tap remain locally instantiated on top of the
JtagInterface proxy — which gracefully tolerates re-discovery on
the client side.

Resume condition: a design for tracking topology changes lands in
the local Chain/Tap code. Then wire transport is a small follow-up.

### Real authentication — `HmacAuthBackend`

Seam is wired (`AuthBackend`, `Principal`, `Scope`, `audit_log`,
`OpenAuthBackend` as the no-auth default). What's missing is a
backend that actually mints and verifies credentials.

Design sketch:

* Capability URL pattern (already exposed in REST as `connect_url`):
  `wss://host:port/v1/node/<path>?token=<HMAC-token>`.
* Token format: `base64url(payload || HMAC_SHA256(payload, secret))`
  where `payload` is CBOR `{node_path, scope, exp, issued_to}`.
  Short-lived (~60s) — token's job is the WS upgrade handshake;
  the connection itself outlives it.
* `HmacAuthBackend.issue_connect_token` builds the URL; REST
  enumeration calls it for every `@wire.node` it describes.
  `validate_connect_token` does HMAC verify + path/exp check at
  the WS upgrade.
* Secret loading: pluggable, but a sensible default reads from
  `~/.config/acrobe.conf` under `wire.auth.hmac_secret`.

Server-side log hygiene matters: token query strings end up in
HTTP access logs. The aiohttp app should redact the `token` param
before logging (or skip access logs for the WS path entirely).

Resume condition: a deployment outside the LAN-friendly trust
boundary surfaces.

### TLS / `wss://`

aiohttp serves both HTTPS and `wss://` from the same port via
`ssl_context=`. Wiring is mechanical:

* `acrobe wire serve` accepts `--cert` / `--key` paths.
* Certificate management out of scope — operators bring their own
  (LE, internal CA, mTLS).
* `RemoteServerRoot.base_url` already supports `https://`; the
  REST `connect_url` derivation already picks `wss://` when the
  inbound request is TLS.

Trust boundaries: with TLS the WS endpoint is no longer
trivially MITM-able, so capability tokens stop leaking on the
wire. Combine with the auth backend above for a usable
"production" mode.

### mDNS discovery (`_acrobe._tcp`)

Today users add `wire.servers.<name>.base` entries by hand.
Auto-discovery lets the `WireNamespace` learn about LAN peers
without config:

* Server: announce `_acrobe._tcp` with TXT records carrying
  `path=/v1/node`, `version=v1`, `tls=true|false`. `python-zeroconf`
  is the obvious dep.
* Client: `WireEnumerator` (or a sibling `MdnsWireEnumerator`)
  browses for the same service type and yields ephemeral entries
  alongside config-declared ones.
* Naming: respect operator overrides — config-declared names win
  if both sources advertise the same host.

Resume condition: a multi-server LAN setup where hand-editing
config becomes painful.

### Server-pushed notifications & subscribe/unsubscribe

Frame schema already has `Notification` reserved (frame kind 4).
Use cases worth covering when they appear:

* Hardware events (USB hot-plug, JTAG cable disconnect, fault).
* Long-running command progress (programming, erase) without
  polling.
* Topology change events — the dependency above on Chain/Tap
  re-discovery would consume these naturally.

Design sketch:

* New `Subscribe(event_kind: str)` and `Unsubscribe(...)` ops on
  any node type that wants to publish.
* Server-side dispatch table: `subscriptions: dict[ws, set[event_kind]]`.
* Server pushes `Notification` frames; client's reader task
  routes them to per-subscription queues exposed as async
  iterators.
* Out-of-order with respect to Request/Response — req_id
  correlation already supports interleaving.

### Plain-Node transport

Today only Batchers are transportable (`@wire.node` enforces
`Batcher` subclassing). Pure Nodes that expose state but no
batch-able ops (configuration views, status snapshots) can't be
opened over the wire — only enumerated via REST.

Design path:

* Drop the Batcher requirement on `@wire.node`.
* For non-Batcher nodes, the WS protocol degenerates to one-shot
  property reads / method calls without batching. Either
  re-purpose the existing Request frame (single-op batches) or
  add a `Call(method_name, args) -> Response` shape.
* Methods must be explicitly marked transportable: a
  `@wire.method` decorator on individual coroutines, similar in
  spirit to `@wire.op` but for synchronous-style RPC rather than
  batched ops.

Resume condition: a real node type needs read-only remote access
and the round-trip cost per call is acceptable.

### Streaming response chunks within a batch

Each batch today produces one Response with all results bundled.
For very large reads (memory dumps, full-bitstream readback) this
forces server-side buffering and a single big frame.

CBOR has indefinite-length byte strings; the codec already uses
`cbor2`. Streaming would mean breaking results into multiple
frames keyed by req_id and chunk index, with a terminator.

Today the JTAG ops are chunked at the API design level (`Shift`
takes a bit-string of bounded size; large reads are issued as
many small ops in a batch). That's enough for v1; revisit if a
new op type wants megabyte-scale single results.

### Multi-tenant isolation, rate limiting, quotas

None of these have a use case yet. Pre-requisites:

* Real authn (above) so requests are attributable.
* `audit_log` becomes more than a no-op — counts, latency,
  per-principal aggregates.
* Decide whether per-server (single shared lock) or per-adapter
  (today's choice) is the right rate-limit granularity.

Resume condition: someone runs an acrobe server as a shared
service.

### Schema evolution / forward-compat

UUID mismatches between client and server are hard errors today.
That's correct: it's better to fail loudly than to silently
deserialize wrong data. The cost is that adding a new op to a
node forces client/server lockstep upgrades.

Possible approaches when this becomes painful:

* Per-class **schema version** in the registry. Catalog includes
  versions; mismatched-but-compatible types use the older
  schema's codec.
* **Capability negotiation**: client advertises what it knows;
  server filters its catalog to the intersection. Already partly
  implemented (unknown UUIDs are silently skipped on the client
  side); making it bidirectional means the server can advertise
  ops the client doesn't know about and just not let them be
  posted.

Resume condition: in-place rolling upgrades become a deployment
requirement.

### `Node.child_hints` proper API

The REST response includes a `hints` field, populated from
`Node.child_hints() -> list[str]`. Default is empty; subclasses
override with literal lists. Today there's no systematic source —
the user explicitly flagged that the `Db`-driven approach is
wrong (Db keys are an internal routing detail, not a user-facing
contract).

Design path: a separate Node-level mechanism that any subclass
can populate at construction time, decoupled from any storage
implementation. Possibly a `_child_hints: list[str]` instance
attribute manipulated by `_child_hint_register("name")` — no
code knows about Db.

This is local-tree work, not wire-specific, but the wire layer
benefits because the `hints` field becomes meaningfully populated
for adapter roots and protocol-level Nodes.

### CLI / library polish

* `acrobe info enumerate` doesn't itself run `stop_tree` on the
  hw_root; the lifecycle drain catches RemoteServerRoot's
  `ClientSession`. A more principled pattern is for the CLI to
  drive `start_tree` / `stop_tree` explicitly. Cosmetic; current
  output is clean.
* `Tap.db._registry.clear()` in `test_jtag.py`'s teardown wipes
  global state and breaks test ordering. Pre-existing; harmless
  in default ordering. Better fix is for the test to save/restore
  the dict.
* CBOR over REST (`Accept: application/cbor`) was scoped in the
  original plan but skipped — JSON is enough for the debug
  interface, CBOR is a polish item.

## Open questions resolved during implementation

* **Catalog frame**: standalone CBOR object, distinct from the
  request/response envelopes. Decoded without a Session.
* **REST output format**: full child objects (one level deep).
  HATEOAS link to deeper paths; no separate "expand" round trip
  needed for the common browse case.
* **Error frame at request level vs `Response.errors`**: both —
  application-level errors (raised by an op) live in
  `Response.errors` keyed by batch index; protocol-level failures
  (malformed frame, dispatch crash) use the standalone
  `ProtocolError` frame.
* **WS routing**: REST and WS share the same URL space; a
  unified handler dispatches on the `Upgrade` header.
* **Subclass identity for proxies**: WS handler walks MRO to find
  the deepest `@wire.node` ancestor. REST `describe()` does the
  same so concrete adapter subclasses (`JtagMpsse`, `Agilex5E`,
  …) report their parent's wire identity. The cutoff scanner
  consumes this and the proxy is built via `types.new_class` as
  a subclass of the registered ancestor — full MRO preserved.
