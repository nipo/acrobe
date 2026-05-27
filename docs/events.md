# Events

Acrobe has a process-global, path-addressed pub/sub bus. Any
component can publish lifecycle moments or state changes;
unrelated subscribers — possibly in different modules, possibly
addressing Nodes that don't exist yet — react to them.

The bus is the cross-cutting glue for features that don't fit
the parent/child method-call axis: live-reload a firmware when
its file changes, reattach a console after a target reset,
re-resolve cached references when a JTAG chain reshapes, drive
a progress bar from any long-running operation, notice hardware
hotplug. None of these requires the producer to know about the
consumers.

The full design and history live in `docs/plans/node-events.md`.
This doc is the user-facing summary: what it gives you, how
acrobe modules use it, and the rules of the road.

## Core model

* **Event**: `Event(source, action, phase, properties,
  timestamp)`. Immutable. `source` is the canonical path of the
  emitter at emit time. `action` is what happened
  (`"program"`, `"changed"`, `"reset"`, …). `phase` is when in
  the action's lifetime (`Phase.PRE` / `Phase.POST` /
  `Phase.PROGRESS`) or `None` for observation-only events.
  `properties` is a free-form mapping the action documents.
* **Bus**: a process-global singleton accessed via
  `acrobe.event.get_bus()`. Subscribers register with a handler
  + filters; the bus dispatches matching events. Handlers may
  be sync or async; async handlers run concurrently.
* **Subscription**: cancel-anytime handle. Can be used as a
  sync context manager for scoped lifetimes.
* **Path is the address space.** Subscribers never hold Node
  references. They subscribe to a path string, and any Node
  emitting from that path (now or in the future) matches.

Two flavours of action:

| Flavour     | Phases meaningful?       | Examples                              |
|-------------|--------------------------|---------------------------------------|
| Intent      | yes — `PRE` / `POST` / `PROGRESS` | `program`, `start`, `stop`, `attach`, `detach`, `reset`, `mutate` |
| Observation | no — `phase=None`        | `created`, `changed`, `deleted`, `moved`, `connected`, `disconnected` |

Intent events fire because acrobe code is doing something;
subscribers can run logic before (`PRE`) and after (`POST`).
Observation events fire because the OS or hardware did
something we noticed after the fact; no pre/post split makes
sense.

## Publishing

Three convenience layers on `Node`, all routing through the
global bus with `source=self.path`:

```python
# 1. One-shot.
await self.emit("reset", phase=Phase.POST, kind="cpu")

# 2. Pre/post around a block, with progress within.
async with self.event_emitter("program",
                              target=target_path) as notifier:
    for page in pages:
        await self.write_page(page)
        await notifier.progress(current=page.idx, total=len(pages))

# 3. Decorator for the simplest pre/post-only case.
@Node.notified("program")
async def write(self, source, **kwargs):
    ...
```

`event_emitter` emits `(action, PRE)` on enter, `(action, POST,
success=True/False, error_class=...)` on exit; exceptions still
propagate to the caller.

Non-Node code (anywhere) publishes via `get_bus().emit(Event(
source=..., action=..., phase=..., properties=...))`. The
`source` must be a canonical path; see below.

## Subscribing

```python
from acrobe.event import get_bus, Phase

# Through a Node (defaults to subtree match):
sub = some_node.subscribe(
    on_program_post,
    action="program", phase=Phase.POST)

# Directly on the bus (defaults to exact match if source given):
sub = get_bus().subscribe(
    on_reset,
    action="reset",
    source="HwRoot/proby-9/dap/cm0",
    source_match="subtree")

# Cancel anytime.
sub.cancel()

# Or scoped via context manager.
with get_bus().subscribe(handler, action="changed") as sub:
    ...  # sub auto-cancels at exit
```

Filters AND together. `phase=None` parameter (the default)
means "no filter on phase" — matches every phase including
phase-less observation events. To filter for phase-less events
specifically, use a predicate (or pass `phase=(None,)`).

Subscriptions registered through `Node.subscribe` are tracked
on the Node and auto-cancelled when that Node's `stop_tree`
runs — they were scoped to the Node's lifetime. Subscriptions
registered directly on `get_bus()` are caller-owned.

### Handler errors are caught and logged

The bus catches every handler exception and logs at WARNING
under `acrobe.event.<dotted-source-path>` (so per-subtree
silencing via `--silent-re acrobe.event.proby-9` works).
Exceptions never propagate to the publisher. The bus is a
notification channel, not an RPC — a buggy subscriber must not
be able to abort the publisher's work.

Subscribers that want retry semantics implement them inside
their handler.

## Path canonicalisation

The bus matches `source` strings verbatim. There's no fuzzy
match, no symlink resolution, no substring search at
subscription time. Subscribers must hand in **the same string
the publisher will emit** — which means **canonical**.

`acrobe.node.Path` is the namespace for path helpers:

```python
from acrobe.node import Path

# Filesystem: resolve symlinks for the existing prefix; leave
# non-existing tail literal.
Path.canonicalize_fs("/a/b/d/foo")  # → "/a/b/c/foo" if d -> ../c

# Hardware: walk the Node tree via child_lookup, resolving
# substring/shorthand names to their canonical Node names.
Path.canonicalize_hw(root, "proby/jtag")
# → "HwRoot/proby-9/jtag"

# Plus structural helpers used inside subscriber predicates.
Path.is_descendant_or_self(event.source, "HwRoot/proby-9")
Path.parent_of("HwRoot/proby-9/jtag")
Path.parts("HwRoot/proby-9/jtag")
```

Why fuzzy matching is not done in `subscribe()`: a non-canonical
subscription that returns a working `Subscription` object would
silently see no events, looking exactly like "the handler isn't
firing". Forcing the caller to canonicalise explicitly puts the
contract at the call site.

When the subscriber already holds a Node, `node.path` is
canonical by construction — skip the helper.

## Canonical actions

Exported as constants on `acrobe.event.Action`. Free-form
strings are allowed — these are just the names the core uses.

### Lifecycle (intent — pre/post)

Auto-emitted by `Node` itself. Subscribers get them for free
on every node in every subtree.

| Action     | Phases     | Source         | Properties                              |
|------------|------------|----------------|------------------------------------------|
| `attach`   | post       | the node       | `{parent}` — child entered the tree     |
| `detach`   | pre / post | the node       | `{parent}` — child leaving the tree     |
| `start`    | pre / post | the node       | `{success, error_class?}`               |
| `stop`     | pre / post | the node       | `{success, error_class?}`               |

`attach` POST is deferred to the next async path through the
child (typically the first `ensure_started`) so sync
construction during `__init__` still produces an event later
when the tree comes alive. The lifecycle order for any node is
`attach POST → start PRE → start POST → … → stop PRE → stop
POST → detach PRE → detach POST`. Missing pieces only when the
node never reached the corresponding state (e.g. a node that
was added and immediately removed without ever starting emits
attach + detach but no start/stop).

### Domain (intent)

| Action     | Phases               | Source              | Properties                                  |
|------------|----------------------|---------------------|----------------------------------------------|
| `program`  | pre / post / progress| the Loadable        | `{target, do_erase, do_verify, success, error_class?, region?, written?, total?}` |
| `reset`    | pre / post           | the Core / target   | `{kind, stop, success}` — `kind` is `"cpu"`/`"system"`/`"watchdog"` |
| `mutate`   | post (today)         | the chain or AP     | `{committed, changed, tap_count}` for JTAG chain refresh; subscribers can also use this for any other structural change |

### Observation (no phase — `phase=None`)

| Action          | Source                                | Properties                                       | Producer                                |
|-----------------|---------------------------------------|--------------------------------------------------|------------------------------------------|
| `created`       | canonical FS path of new entry        | `{is_dir}`                                       | `FsWatcher`                              |
| `changed`       | canonical FS path of changed file     | `{}`                                             | `FsWatcher`                              |
| `deleted`       | canonical FS path of gone entry       | `{is_dir}`                                       | `FsWatcher`                              |
| `moved`         | canonical FS path of new location     | `{from, is_dir}` — `from` is the prior path      | `FsWatcher`                              |
| `connected`     | `HwRoot/<adapter-name>`               | `{bus, address, vendor_id, product_id}`          | `UsbEnumerator.start_watch`              |
| `disconnected`  | `HwRoot/<adapter-name>`               | `{bus, address}`                                 | `UsbEnumerator.start_watch`              |

## Filesystem watcher

`acrobe.event.FsWatcher(base_dir, *, recursive=True,
debounce_ms=100)` bridges `watchdog.Observer` to the bus.
Standalone — not a Node, lifetime is the caller's. Canonicalises
`base_dir` at construction; all emitted source paths are
canonical (symlinks resolved). Per-`(path, action)` debounce
coalesces editor atomic-rename bursts to one bus emit per
logical change.

```python
from acrobe.event import FsWatcher, get_bus
from acrobe.node import Path

abs_path = Path.canonicalize_fs("build/firmware.elf")
watcher = FsWatcher(Path.parent_of(abs_path))
sub = get_bus().subscribe(reload, action="changed",
                          source=abs_path, source_match="exact")
await watcher.start()
try:
    await ...  # consumer's main loop
finally:
    sub.cancel()
    await watcher.stop()
```

`FsWatcher` is deliberately not a Node — it instruments an
external system rather than representing a thing. `FsRoot`
(VFS) does *not* auto-watch; a future "live VFS" that
invalidates parsed Nodes on file change would consume
`FsWatcher` events the same way any other subscriber does.

## USB hotplug

`UsbEnumerator.start_watch()` enables `enable_hotplug` on the
ausb context and emits `connected` / `disconnected` for
recognised adapters (those matching a registered
`AdapterInfo`). Unrecognised USB devices stay silent. Source
path is `HwRoot/<adapter-name>` — what the adapter would have
if summoned — so subscribers wait for a specific adapter to
appear regardless of whether the Node has been built yet.

A `(bus, address) → adapter-name` map is seeded at start so
disconnects emit with the right name even after the descriptor
is partially gone.

```python
from acrobe.adapter.model import make_hw_root
from acrobe.event import get_bus

root = make_hw_root()
for enum in root.enumerators:
    if hasattr(enum, "start_watch"):
        await enum.start_watch()

get_bus().subscribe(on_probe_appeared,
                    action="connected",
                    source="HwRoot/proby", source_match="subtree")
```

`stop_watch()` is symmetric; both are idempotent.

## What this enables across modules

| Module / area               | What the bus brings                                                                 |
|------------------------------|--------------------------------------------------------------------------------------|
| `target/loadable`           | Per-write `program` events with per-region progress — consumed by terminal UI, GDB pass-through, GUI, auto-reload watchers, RTT consoles. |
| `target/arm/cortex_m`       | `reset` events with `kind` — consumed by RTT to re-resolve control block address, by GDB session tracking, by run-control monitors. |
| `protocol/jtag`             | `mutate` events on chain refresh with a `changed` flag — anything that cached a TAP reference can re-resolve or invalidate without explicit notification plumbing. |
| `adapter` (USB)             | `connected`/`disconnected` on hotplug — consoles can know to reattach, target discovery can re-run, the CLI's `info adapters` could live-update. |
| Auto-reload tooling         | Subscribes to FS `changed` on the firmware path AND `(program, post)` on the target — orchestrates the reflash + console reattach loop without bespoke plumbing in either side. See `docs/plans/auto-reload.md`. |
| VFS                          | `FsRoot` doesn't auto-watch today, but `FsWatcher` is available standalone; a future live VFS layer would consume the same events to invalidate parsed file Nodes. |
| Logging / observability     | A single subscriber with no filter sees every event; trivial to back a `--trace-events` CLI flag, or pipe events to a GUI inspector, GDB monitor, or structured log sink. |
| Plugins (`acrobe_plugin`)   | Third-party adapters / chips can emit their own free-form actions on the bus; cross-cutting subscribers don't need to import plugin code to react. |

## When NOT to use the bus

* **Direct method calls between parent and child.** Use them.
  The bus is for cross-cutting concerns that don't fit the
  tree's structural axis.
* **RPC.** Handler exceptions are swallowed. If you need a
  result back, call the method directly.
* **Hot inner loops.** Progress events from a tight loop will
  saturate the bus. Either coalesce at the publisher (e.g.
  one progress emit per page, not per byte) or accept the
  cost.
* **Replacing logging.** Use `self.logger` for diagnostic
  text. The bus is for state changes; if your subscriber's
  only reaction is to log, you wanted a log call.

## Quick reference

| Operation                                | Signature |
|------------------------------------------|-----------|
| Publish from a Node, one-shot            | `await self.emit(action, phase=None, **properties)` |
| Publish around a block                   | `async with self.event_emitter(action, **base): ...` |
| Decorator form                           | `@Node.notified(action)` on an async method |
| Mid-action progress                      | `await notifier.progress(**properties)` (yielded by `event_emitter`) |
| Subscribe via Node (subtree default)     | `node.subscribe(handler, action=, phase=, source_match=, predicate=)` |
| Subscribe via bus directly               | `get_bus().subscribe(handler, action=, phase=, source=, source_match=, predicate=)` |
| Cancel a subscription                    | `sub.cancel()` or `with sub:` |
| Filesystem path canonicalise             | `Path.canonicalize_fs(path)` |
| Hardware path canonicalise               | `Path.canonicalize_hw(root, path)` |
| Path-prefix predicate                    | `Path.is_descendant_or_self(event.source, prefix)` or `event.source_under(prefix)` |
| Reset the bus (tests)                    | `acrobe.event.reset_for_tests()` |
| Watch a directory                        | `await FsWatcher(base_dir).start()` |
| Watch USB hotplug                        | `await enumerator.start_watch()` |
