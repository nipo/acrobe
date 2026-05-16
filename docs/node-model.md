# The Node model

`acrobe.node.Node` is the single tree primitive in acrobe. Every
adapter, every JTAG TAP, every CoreSight component, every Target,
every parsed file format is a Node. This document covers the
lifecycle, parenting, navigation, options, and teardown contract
that all of them share.

The companion doc `docs/vfs-design.md` covers the byte-access
mixins (`Readable`, `Writable`, `Addressable`) layered on top of
Node. The doc here covers Node itself.

## Identity and naming

A Node carries a `name` (set at construction) and exposes two
derived path properties:

* `node.fqdn` — dotted name from the root
  (`proby-9.jtag.chain.0.dap.ahb-ap@0`). This is the **logger**
  name; Python's logging hierarchy relies on `.` for parent/child
  relationships and prefix filtering. See `docs/logging.md`.
* `node.path` — slash-separated path matching the VFS / CLI path
  syntax (`a/b/c`). Use this for display.

`node.logger` returns `logging.getLogger(node.fqdn)` — never use
`logging.getLogger(__name__)` from a Node, you'd lose the
per-tree filtering.

`node.metadata` is a free-form dict for introspection (consumed
by `acrobe loadable info`). Subclasses with typed attributes
usually mirror them into this dict during `start()`.

## Parenting

The tree is strictly a tree. A Node has at most one parent; cross-tree
references (e.g. a Target holding a `MemAp` ref) are plain Python
attributes that **do not affect parenthood**.

Three methods attach / detach:

* `parent.child_add(child)` — eager attach. If the parent is already
  started, the child's `start_tree()` is scheduled automatically via
  `asyncio.ensure_future`. Safe to call from `__init__` (parent isn't
  started yet at that point).
* `parent._child_attach(child)` — attach without auto-start. Used by
  `child_summon` (which then explicitly starts the child itself, so it
  can do so under the per-parent inflight lock). Format parsers that
  pre-populate structural children in their own `start()` also use
  this to attach without triggering the auto-start path.
* `await parent.child_remove(child)` — stops the child's entire
  subtree (`stop_tree`), then detaches it.

After every attach / detach, `parent.children_changed()` fires.
The default is a no-op; subclasses override to react to
structural changes (e.g. invalidating cached views).

`parent.children` returns the list of **pre-populated** children
only. On-demand children (see "Pre-populated vs on-demand" below)
are not listed here.

## Lifecycle: `start()` and `stop()`

A Node has three lifecycle states: not started, started, stopped.
Two methods drive transitions:

* `async def start(self)` — bring this Node live. Open hardware,
  parse a file header, discover children. Default is a no-op.
  Override in subclasses that need it.
* `async def stop(self)` — tear this Node down. Close handles,
  finalise written-back state, release resources. Default is a
  no-op.

Per-Node lifecycle is wrapped by tree-walking helpers:

* `await node.start_tree()` — top-down: start `self` (via
  `_ensure_started`, which is idempotent and concurrency-safe),
  then recurse into existing children.
* `await node.stop_tree()` — top-down: stop `self`, mark not
  started, then recurse into children. Stop propagates; a live
  reference to a descendant after the parent has stopped MUST
  NOT be used (it may assert or raise from stale state).

`node.started` is the boolean state flag.

### Idempotence and concurrency

`_ensure_started` guards `start()` with an `asyncio.Lock` and an
already-started early-exit. Multiple awaits race through one
`start()` call:

```python
await asyncio.gather(node._ensure_started(),
                     node._ensure_started())   # start() runs once
```

`child_summon` and `start_tree` both go through this path, so two
parallel commands targeting the same chain share one spawned
chain and one `start()` call.

### Non-Node resources

Anything holding a background context (sockets, USB handles,
aiohttp sessions, subprocesses) follows the symmetric pattern
documented in `docs/conventions.md`:

```python
def __init__(self, ...):
    ...
    on_shutdown(self.close)

async def close(self):
    cancel_shutdown(self.close)
    ...
```

The Node tree's `stop_tree()` cascades naturally; the lifecycle
module is the catch-all when `stop_tree` wasn't called.

## Pre-populated vs on-demand children

Every Node has both:

* **Pre-populated children** — created in `start()` (or attached
  via `child_add`), live in `self._children`, visible via
  `node.children` and listed by `acrobe loadable ls`.
* **On-demand children** — not in `_children`, not listed by
  `ls`, but reachable via `child_spawn` / `child_summon`. Used
  for namespaces that would explode if pre-enumerated (`as`,
  ELF symbols) or for parameter-dispatched names.

`child_hints()` (sync, no side effects, must not touch hardware)
returns the names of children that *could* be summoned but
aren't materialized yet. Subclasses override to expose static
manifests; the default returns `[]`. Dynamic discovery (probing
a USB bus, scanning a JTAG chain) is a separate concern.

## Lookup: `child_lookup`

`node.child_lookup(name)` finds an existing pre-populated child.
It is **sync**, performs no IO, and never spawns. Returns `None`
on miss. Lookup order:

1. `".."` → parent.
2. `"*"` → the only child (if exactly one; else `None`).
3. Integer index into `children`.
4. Exact name match (case sensitive).
5. Case-insensitive substring match (only when exactly one child
   matches — ambiguous matches return `None`).

Rule 4 is what disambiguates names that share a prefix, e.g.
STAPL variables `J2`, `J23`, `J24` — `child_lookup("J2")` returns
the exact one, not an ambiguity error.

## Spawn: `child_spawn`

`async parent.child_spawn(name)` creates a child by name.
Override in subclasses; the base raises `NoMatch`.

The MRO walk (`_child_spawn_mro`) lets each class in the
hierarchy define its own `child_spawn`. Each class's
`__dict__["child_spawn"]` is tried in MRO order; raising
`NoMatch` defers to the next class. This composes cleanly when
a subclass adds a new spawning rule without shadowing its
parent's rules.

The reserved name `as` is handled in `_child_spawn_mro` before
the MRO walk: any Node accepts an `as` child for format
reinterpretation (see `docs/vfs-design.md` D3).

## Path resolution: `child_summon`

`await parent.child_summon(*parts)` walks a path of names,
looking up or spawning at each step. This is what
`-r proby-9/jtag/chain/0/dap` resolves to in the CLI.

For each part:

1. Parse `name(key=value,...)` options off the part — grammar in
   `docs/vfs-design.md` D10 (always `key=value`; quoted values
   supported).
2. `_lookup_or_spawn(bare_name)` — `child_lookup` first; if
   missing, `_child_spawn_mro` then attach via `_child_attach`.
   Concurrent callers for the same `(parent, name)` are
   single-flight via `_summon_inflight`: spawn-and-attach
   happens once, both callers get the same Node.
3. For each parsed option, `child.option_set(key, value)`. Options
   are applied **after** spawn / lookup and **before** start.
4. `await child._ensure_started()` — starts the child if needed,
   under a per-Node lock.
5. Recurse into the next part.

Spawned Nodes are added to the tree, which makes them visible to
`children` and to later `child_lookup` calls. Each node along
the path is started before navigating deeper, so that `start()`
can populate further children (e.g. `Chain.start()` discovers
TAPs that `child_summon` then descends into).

The empty-parts case (`await node.child_summon()`) returns
`node` unchanged — useful as a no-op end of recursion.

## Options: `option_set`

`node.option_set(key, value)` applies one option to a Node.
Called by `child_summon` after spawn / lookup and before
`start()`. The base raises `ValueError(f"Unknown option:
{key}={value!r}")`; subclasses override to accept their own
keys and `super().option_set(key, value)` to defer.

Mixin classes that want to cooperate on options must sit
**before** `Node` in the MRO so their cooperative override is
reached before Node's terminal raise. `FreqCapper` is the
canonical example: it consumes `fmax=` and otherwise calls
`super().option_set` to let Node's chain handle the rest.

Options interpret right away or defer to `start()` as the
subclass sees fit — a slice's `offset=` / `size=` are stashed
on the Node and consumed in `start()` once the source's `size`
is known; an adapter's `serial=` may be applied immediately.

## Navigating an existing tree

A few small helpers find Nodes already attached to the tree —
they do not spawn:

* `node.children_find(predicate, include_self=False)` — depth-first
  walk returning every descendant where `predicate(node)` is true.
* `node.children_of_class(klass, include_self=False)` — shortcut for
  `children_find(lambda c: isinstance(c, klass))`. The Target
  framework uses this constantly:
  `target.children_of_class(Loadable)`,
  `dp.children_of_class(MemAp)`, etc.
* `node.parent_of_class(klass)` — walks ancestors until one matches.
  Raises `LookupError` if no ancestor of that class is found.

These cross structural boundaries (a Target's `Debuggable` finds
its own `Core` children with `children_of_class(Core)`) without
relying on attribute names.

## Progress

`with node.progress(label, total, unit="") as handle:` is the
canonical way to report progress from inside a long-running
operation. Delegates to the global `ProgressDelegate` set via
`log.set_progress` — quiet mode (`-q`) suppresses output, the
CLI default renders a bar tagged with `node.fqdn`.

## What NOT to put on a Node

A few patterns to avoid:

* **Don't free-function helpers that "are about a Node"** —
  attach them as methods. The project convention (see
  `docs/conventions.md`) is class-based grouping.
* **Don't add back-references to walk the tree by attribute
  name.** Use `parent_of_class` / `children_of_class`. A view
  that needs siblings calls `self._parent.children_of_class
  (SiblingClass)`.
* **Don't `await` inside `__init__`** — `__init__` is sync. Any
  IO happens in `start()`. Constructors only set up Python
  state.
* **Don't return placeholder values from un-implemented spawn
  branches.** Raise `NoMatch` (project convention — see the
  global instructions) so the MRO walk can fall through to the
  next class.

## Quick reference

| Operation | Signature |
|-----------|-----------|
| Construct | `Node(name)` |
| Attach eager | `parent.child_add(child)` |
| Attach silent | `parent._child_attach(child)` |
| Detach + stop | `await parent.child_remove(child)` |
| Pre-populated children | `node.children` |
| Static hints | `node.child_hints()` |
| Find by name | `node.child_lookup(name)` |
| Create on demand | `await node.child_spawn(name)` |
| Walk path | `await node.child_summon(*parts)` |
| Find descendants | `node.children_find(pred)` / `children_of_class(cls)` |
| Find ancestor | `node.parent_of_class(cls)` |
| Apply option | `node.option_set(key, value)` |
| Start self only | `await node._ensure_started()` |
| Start subtree | `await node.start_tree()` |
| Stop subtree | `await node.stop_tree()` |
| Logger name | `node.fqdn` |
| Display path | `node.path` |
| Inspection dict | `node.metadata` |
