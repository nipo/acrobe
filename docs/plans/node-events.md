# PLAN — Node-level events (global pub/sub bus)

Living document. Captures the model and the slicing for a generic
event bus that lets components publish state changes and lifecycle
moments, and lets unrelated subscribers react.

## Status

Slices 1–6 landed. Slice 7 (progress reshape) deferred.

| Slice | Subject                              | Status |
|-------|--------------------------------------|--------|
| 1     | bus + path helpers + emit/dispatch   | done   |
| 2     | Node integration                     | done   |
| 3     | auto-wired lifecycle emits           | done   |
| 4     | first real domain users              | done   |
| 5     | `FsWatcher`                          | done   |
| 6     | hotplug `connected`/`disconnected`   | done   |
| 7     | progress reshape                     | deferred |

In-tree code:

* `acrobe/event/__init__.py` — public surface re-exports.
* `acrobe/event/event.py` — `Event`, `Phase` (StrEnum),
  `Action` (constants class), `Notifier`.
* `acrobe/event/bus.py` — `EventBus`, `Subscription`,
  `get_bus`, `reset_for_tests`.
* `acrobe/event/fs_watcher.py` — `FsWatcher` + watchdog
  bridge.
* `acrobe/node.py` — `Path` utility class (predicates +
  canonicalise), `Node.emit` / `Node.subscribe` /
  `Node.event_emitter` / `Node.notified`, auto-wired lifecycle
  emits, deferred attach mechanism.
* `acrobe/adapter/model.py` — `UsbEnumerator.start_watch` /
  `stop_watch` and the hotplug handler.

Tests (across `tests/test_node_path.py`,
`tests/test_event_bus.py`, `tests/test_node_events.py`,
`tests/test_domain_events.py`, `tests/test_fs_watcher.py`,
`tests/test_hotplug.py`): 96 added, full suite 1556 passing.

## Goal

Several distinct features need the same shape: a publisher
announces something happened on or about a Node; one or more
subscribers elsewhere react. Driving cases:

* **Auto-reload of firmware.** Programmer announces "I'm about to
  re-flash this chip" so consoles detach in time; then "done" so
  they reattach. See `docs/plans/auto-reload.md`.
* **Hotplug.** USB enumerator sees a device disappear or appear.
  Today this surfaces as the next operation raising `IOError`
  (best-effort invalidation, per the target framework). A proper
  signal would let Targets prune themselves, and let long-running
  clients reattach to replacement hardware.
* **JTAG chain reshape.** `Chain.tlr_and_refresh()` already knows
  it has rediscovered the chain; anything that cached a TAP
  reference needs to either re-resolve or invalidate.
* **Target lifecycle.** Reset / halted / resumed from a Debuggable
  — useful for monitors, for GDB, for log timestamps.
* **Progress reporting (future).** The existing
  `Node.progress(label, total, unit)` API becomes a publisher;
  the terminal bar, a future GUI, and a GDB `monitor`
  pass-through all become subscribers. Lets one operation report
  progress to many sinks without coupling.
* **File-system change notifications.** Auto-reload of firmware
  rebuilds requires a watcher on the firmware file. Rather than
  building a bespoke notifier, fold OS-level file events
  (watchdog / inotify / FSEvents) onto the same bus. Same
  canonical actions (`attach`, `detach`, `mutate`); subscribers
  use the same `subscribe()` API regardless of whether the
  change is hardware-side or filesystem-side.

Two requirements shape this strongly:

1. **Subscribers must not hold Node references.** Two reasons:
   * **Stale references.** A Node may still be reachable as a
     Python object after the hardware behind it is unplugged —
     the reference outlives the live thing it points at.
   * **Future references.** A subscriber may want to wait for a
     Node that doesn't exist yet (e.g. the USB-CDC console at
     `proby-9/usb-cdc-tty` after a firmware re-enumeration).
     There is no Node to subscribe against.

   The address space therefore is **paths**, not objects.
   Subscribers register an interest in a path (exact, or any
   descendant of it); the bus dispatches events to whoever
   matches when they fire. Node names are stable (USB serial,
   MCU UID, etc.), so a path-based match is reliable across
   re-enumeration. The Node instance that emits at a given path
   may be a wholly different Python object than the one a
   subscriber initially saw — that's fine, the subscription is
   keyed on the path, not the object.

2. **The bus is global.** Not per-Node, not per-HwRoot. One
   process, one bus. Subscribers attach to the bus directly;
   the bus knows nothing about parenthood. There is no
   bubbling — replaced by subtree path-prefix matching on the
   subscriber side.

3. **Path is the address space; tree topology is not.** The
   bus matches strings; whether an event's source path lives
   under `HwRoot` or somewhere else is invisible to it. This
   matters because acrobe today has *two* disjoint Node
   trees — the hardware tree rooted at `HwRoot`, and a fresh
   per-call `FsRoot` minted by `_summon()` in the CLI's VFS
   walk (`acrobe/cli/loadable.py`). The bus accommodates both
   without requiring a structural merge: each emitter uses
   whatever path string identifies the resource. See "Path
   namespace conventions" below for the agreed encoding;
   merging the two trees is a separate concern, tracked
   independently and not a prerequisite here.

## Model

### Event

```python
@dataclass(frozen=True, slots=True)
class Event:
    source: str                 # canonical path at emit time
    action: str                 # what happened, e.g. "program", "changed"
    phase: str | None = None    # "pre", "post", "progress", or None
    properties: Mapping[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.monotonic)
```

The split between `action` and `phase` is deliberate.

* **`action`** identifies the *thing happening* — `program`,
  `start`, `reset`, `mutate` for our own tree; `created`,
  `changed`, `deleted`, `moved` for filesystem observations.
  Free-form string; canonical sets defined in
  `acrobe/event/actions.py` (intent-shaped) and tabulated under
  "Filesystem events" below (observation-shaped).
* **`phase`** identifies *when* along the action's lifetime —
  `pre` (about to start), `post` (just finished), `progress`
  (in flight), or `None` for actions with no temporal split.
  Restricted set defined in `acrobe/event/phases.py` as
  `StrEnum` (`Phase.PRE`, `Phase.POST`, `Phase.PROGRESS`).

  Two kinds of events differ on whether phase is meaningful:

  * **Intent events** — fired by acrobe code around something
    it's about to do or just did (program a chip, attach a
    child, refresh a chain). Pre/post is meaningful because
    subscribers may want to prepare before the work starts.
  * **Observation events** — fired in response to something
    that already happened outside our control (OS filesystem
    event, USB hotplug notification). There is no "about to
    happen" hook; phase is `None`.

  Subscribers filter by phase or omit the filter. A subscriber
  that wants only phase-less events filters with a predicate.

Two reasons to keep them separate instead of mashing them into
one string (`will_program` / `did_program`):

* Subscribers can filter on either axis independently. "All
  `pre` events for tracing" is as easy as "all phases of
  `program`".
* Enables the context-manager / decorator sugar below: the
  caller declares one action, the framework handles the
  begin/end phases automatically.

`source` is the slash path (`Node.path` form for hardware,
absolute POSIX path for filesystem), not the dotted FQDN.
Filters match the same string verbatim — there is no fuzzy
matching at subscription time. Subscribers are responsible for
passing **canonical** paths; see "Path canonicalisation"
below.

### Path helpers

Static module functions in `acrobe/event/path.py`:

```python
def is_descendant_or_self(path: str, ancestor: str) -> bool: ...
def parent_of(path: str) -> str | None: ...
def parts(path: str) -> tuple[str, ...]: ...

def canonicalize_fs(path: str | Path) -> str:
    """Resolve symlinks in `path` to the extent components exist.

    Returns an absolute path with symlinks resolved up to the
    last existing prefix; non-existing trailing components are
    appended literally (uncanonicalised — they don't exist yet,
    so there's nothing to resolve).

    Required when subscribing to filesystem events: the OS
    notifier emits paths under the real (canonical) directory,
    so a subscriber giving a symlinked form would never match."""

def canonicalize_hw(root: Node, path: str) -> str:
    """Walk the hardware tree from `root`, resolving each path
    segment through `child_lookup` to the canonical child name
    (the Node's actual `name`, not whatever shorthand or
    substring the caller used).

    Stops at the first segment with no match — non-existing
    trailing segments are appended literally. Lookup is
    non-spawning: no `child_spawn` calls, no IO. If the caller
    needs to canonicalise through unsummoned subtrees, do an
    explicit `await root.child_summon(...)` first and use the
    resulting node's `.path`."""
```

Subscribers compose `is_descendant_or_self` / `parent_of` /
`parts` into custom predicates when the structured filters
aren't enough. They use `canonicalize_fs` / `canonicalize_hw`
before passing a `source=` to `subscribe()`.

### Subscription

```python
class Subscription:
    """Handle for one subscription. Sync context-manager form for
    scoped use; `cancel()` for explicit release."""

    def cancel(self) -> None: ...
    def __enter__(self) -> "Subscription": ...
    def __exit__(self, *exc) -> None: ...
```

Held strongly by the caller; dropped on `cancel()` or context
exit.

### EventBus

```python
class EventBus:
    """Process-global event dispatcher. Single instance accessed
    via acrobe.event.get_bus()."""

    async def emit(self, event: Event) -> None:
        """Dispatch `event` to every matching subscriber. Awaits
        all async handlers; calls sync handlers inline. Per-handler
        exceptions are caught and logged at WARNING, never
        propagated. Returns once every handler has completed."""

    def subscribe(self,
                  handler: Callable[[Event], None | Awaitable[None]],
                  *,
                  action: str | Iterable[str] | None = None,
                  phase: str | Iterable[str] | None = None,
                  source: str | None = None,
                  source_match: Literal["exact", "subtree"] = "exact",
                  predicate: Callable[[Event], bool] | None = None,
                  ) -> Subscription:
        """Register `handler`. All filters AND together.

        `action`   — single action string or iterable; None matches any.
        `phase`    — single phase string or iterable; None matches any.
        `source`   — path filter; None matches any source.
        `source_match` — `exact` (default) requires source == path;
                     `subtree` requires `is_descendant_or_self
                     (event.source, source)`.
        `predicate` — optional callable for cases the structured
                     filters don't cover; called only after the
                     other filters pass.
        """
```

`get_bus()` returns a lazily-created module-level singleton; a
`reset_for_tests()` hook recreates it so tests get isolation.

### Node integration

`Node` gains four convenience methods. None of them couples
Node objects to subscriptions or to events on the wire — they're
all syntactic sugar over the bus and `self.path` at call time.

```python
class Node:
    async def emit(self, action: str, phase: str = Phase.POST,
                   **properties) -> None:
        """Publish `(self.path, action, phase, properties)` on the
        global bus."""

    def subscribe(self, handler, *, action=None, phase=None,
                  source_match="subtree", predicate=None,
                  ) -> Subscription:
        """Convenience: subscribe with `source=self.path`. Default
        `source_match` is `subtree` because the typical Node-side
        use is 'watch me and my descendants'. The Node instance is
        only used to capture `self.path` — once registered, the
        subscription is path-based and survives Node replacement
        at that path."""
        return get_bus().subscribe(
            handler, action=action, phase=phase,
            source=self.path, source_match=source_match,
            predicate=predicate)

    @asynccontextmanager
    async def event_emitter(self, action: str, **base_properties):
        """Async context manager: emits `(action, Phase.PRE)` on
        enter, `(action, Phase.POST)` on exit. Yields a Notifier
        whose `progress(**props)` emits `(action, Phase.PROGRESS)`
        events sharing the same base properties."""
        await self.emit(action, phase=Phase.PRE, **base_properties)
        notifier = Notifier(self, action, base_properties)
        success = True
        error: BaseException | None = None
        try:
            yield notifier
        except BaseException as exc:
            success = False
            error = exc
            raise
        finally:
            extra = {"success": success}
            if error is not None:
                extra["error_class"] = type(error).__name__
            await self.emit(action, phase=Phase.POST,
                            **base_properties, **extra)
```

`Notifier`:

```python
class Notifier:
    def __init__(self, node, action, base_properties): ...
    async def progress(self, **properties) -> None:
        await self.node.emit(self.action, phase=Phase.PROGRESS,
                              **self.base_properties, **properties)
```

Decorator form for the simplest case:

```python
class Node:
    @staticmethod
    def notified(action: str):
        """Decorator: wraps an async method so pre/post events
        fire automatically around it. The wrapped method receives
        no notifier — use `event_emitter()` directly when you need
        progress events or extra base properties."""
        def deco(method):
            @functools.wraps(method)
            async def wrapper(self, *args, **kwargs):
                async with self.event_emitter(action):
                    return await method(self, *args, **kwargs)
            return wrapper
        return deco
```

Usage:

```python
class Loadable(Node):
    @Node.notified("program")
    async def write(self, source, **kwargs):
        ...
```

For richer use cases with progress / per-call properties, drop
the decorator and use the context manager:

```python
class Loadable(Node):
    async def write(self, source, **kwargs):
        async with self.event_emitter("program",
                                        target=self.parent.path) as notifier:
            for page in pages:
                await self.__write_page(page)
                await notifier.progress(current=page.idx,
                                         total=len(pages))
```

## Canonical actions

Exported as constants in `acrobe/event/actions.py` (also a
`StrEnum` for IDE help; equality with plain strings works).
Third-party publishers may emit any action string; these are the
ones the core ships and the ones cross-cutting subscribers know.

| Action       | Phases               | Source              | Properties                                 | Notes |
|--------------|----------------------|---------------------|---------------------------------------------|-------|
| `attach`     | pre / post           | the node            | `{parent: <path>}`                          | Fired when a Node enters the tree (`child_add`, `_child_attach`). Replaces the previous `node-appeared`. |
| `detach`     | pre / post           | the node            | `{parent: <path>}`                          | Fired when a Node leaves the tree (`child_remove`). Replaces `node-disappeared`. |
| `start`      | pre / post           | the node            | `{success: bool}`                           | Fired around the successful `start()` path in `_ensure_started`. |
| `stop`       | pre / post           | the node            | `{success: bool}`                           | Fired around `stop_tree`. |
| `program`    | pre / post / progress| the Loadable        | `{target, success, current, total, unit}`   | Auto-wired via `@Node.notified` on `Loadable.write`; progress emitted from within. |
| `reset`      | pre / post           | Debuggable / target | `{kind: "system"\|"cpu"\|"watchdog", success}` | `kind` disambiguates the scope; one action, many kinds. |
| `mutate`     | pre / post / progress| the chain or AP     | `{committed: bool, changes}`                | The chain's shape changed (TLR refresh, IcePick gate). `committed=False` on the `pre` event when subscribers can still influence the action; `committed=True` when the change was detected post-hoc and `pre` is advisory only. Also used for filesystem content changes — see "Filesystem events" below. |
| `connected`  | None                 | the adapter         | `{bus, address, vendor_id, product_id}`     | Hardware hotplug: USB device matching a registered `AdapterInfo` came online. Source = `HwRoot/<adapter-name>` (the path the adapter would have if summoned). Observation-shaped — no pre/post. Subscribers wanting "anything appeared anywhere" subscribe to `action=("attach","connected"), phase=("post",None)`. |
| `disconnected`| None                | the adapter         | `{bus, address}`                            | Hardware hotplug: a previously-`connected` device went away. Lookup goes through a per-enumerator `(bus, address) → name` map populated on connect, so disconnect events arrive with the right name even though the descriptor may be partially gone. |

Notes on the design:

* **Progress is a phase, not an action.** Any action with a
  long body can emit progress events. There is no standalone
  `progress` action; subscribers wanting "any progress
  anywhere" subscribe with `phase="progress"`.
* **`reset` disambiguates by property, not by action.** A
  subscriber that cares about every reset filters
  `action="reset"`; one that cares only about CPU resets adds
  a `predicate=lambda e: e.properties.get("kind") == "cpu"`.
  Avoids exploding the action namespace for variants of the
  same logical thing.
* **`mutate` and the committed flag.** Some structural changes
  are detected after the fact (a TLR happened, we re-discovered
  the chain, then we emit). In that case the `pre` event fires
  *after* the change is irreversible; the `committed=True`
  property tells subscribers "do your post-mutation work now,
  the `pre` is informational only". Subscribers that want
  cancellable mutations check `committed=False` on `pre`.

## Path namespace conventions

The bus matches strings; the publisher chooses what string to
use as `source`. To keep events from one namespace from
accidentally matching subscribers in another, the agreed
encoding is:

| Subsystem            | Source-path form                                     | Examples |
|----------------------|------------------------------------------------------|----------|
| Hardware Node tree   | `HwRoot/<node-path>` (i.e. `Node.path` as today)     | `HwRoot/proby-9/jtag/chain/0/dap` |
| Filesystem           | Absolute filesystem path (POSIX form, leading `/`)   | `/Users/nipo/projects/acrobe/firmware.elf` |
| (future) Remote wire | TBD — likely `wire/<host>/<remote-path>`             | |

These two namespaces don't collide because hardware paths never
start with `/` and filesystem paths always do (on POSIX) or are
explicitly drive-rooted (on Windows, when we get there).
Subtree matching works inside each namespace without further
ceremony.

Why no global prefix like `hw:/...` / `fs:/...`: subtree
matching is `startswith` on the source string. A prefix would
work but would force every existing CLI invocation to gain a
prefix too (`acrobe info enumerate -r hw:/proby-9`), which is
a lot of churn for no functional gain. Defer prefixing until a
third namespace appears that needs disambiguation.

Tree unification (parenting `FsRoot` mounts under `HwRoot`, or
introducing a new top-level `Root` that hosts both) is a
separate, larger concern. It would make `acrobe info
enumerate` a single tree walk and might let CLI path parsing
become uniform. But it's not a precondition for the bus —
emitters and subscribers agree on strings, not on tree
topology. Pursued (or not) on its own merits in a separate
plan.

## Path canonicalisation

The bus matches `source` strings verbatim. There is no fuzzy
match, no symlink resolution, no substring search. Subscribers
must hand in **the same string the publisher will emit** —
which means canonicalised, for both filesystem and hardware
namespaces.

Both `Node.child_lookup` (hardware) and shell tab-completion
(filesystem) tolerate non-canonical forms. The bus deliberately
doesn't, because tolerant matching at subscription time would
hide bugs: a subscriber that *thought* it was watching
`/a/b/d/foo` would silently see no events when symlink `d`
resolves elsewhere, and the failure would look like "nothing
ever happens".

### Filesystem

`fsnotify` / `watchdog` / `inotify` / `FSEvents` do not resolve
symlinks. If `d -> ../c` and `/a/b/c/foo` changes:

* OS event fires under `/a/b/c/foo` (the real path).
* A subscriber listening on `/a/b/d/foo` never sees it.
* A watcher attached to `/a/b/d/` likewise sees nothing — the
  OS reports the event under `/a/b/c/`.

`canonicalize_fs(path)` resolves symlinks for the existing
prefix and treats the non-existing tail literally. The "still
canonicalise even if `foo` doesn't exist yet" part matters
because the auto-reload use case may set up subscriptions
before the first build produces the file.

```python
canonicalize_fs("/a/b/d/foo")    # → "/a/b/c/foo"  if d -> ../c, foo absent
canonicalize_fs("/a/b/d/foo")    # → "/a/b/c/foo"  if d -> ../c, foo present
canonicalize_fs("/nope/foo")     # → "/nope/foo"  literally; nothing to resolve
```

Publishers (`FsWatcher`) canonicalise their `base_dir`;
subscribers canonicalise the path they want to watch. They
meet on the same string.

### Hardware

`Node.child_lookup` supports short names, integer indices, and
case-insensitive substring matches (per `docs/node-model.md`).
Convenient for CLI, dangerous for subscriptions: a user
subscribing to `"proby"` would not match events emitted by the
node whose actual `name` is `"proby-9"`.

`canonicalize_hw(root, path)` walks the tree segment by segment,
resolving each through `child_lookup` to the actual `name`.
Stops at the first missing segment; remaining segments are
appended literally (analogous to the FS case for nodes that
don't exist yet — e.g. waiting for a USB hotplug). Lookup is
non-spawning and IO-free.

```python
canonicalize_hw(root, "proby/jtag")       # → "HwRoot/proby-9/jtag"
canonicalize_hw(root, "proby-9/jtag")     # → "HwRoot/proby-9/jtag"
canonicalize_hw(root, "proby-9/usb-cdc")  # → "HwRoot/proby-9/usb-cdc"
                                          #   (literal — node not yet present)
```

When the subscriber already has a Node in hand,
`node.path` is canonical by construction — skip the helper.

### Why this isn't done inside `subscribe()`

Two reasons not to canonicalise inside the bus:

* **No `root` available.** Hardware canonicalisation needs the
  root Node; the bus is decoupled from the tree by design.
  Adding a root parameter to every `subscribe()` call
  contradicts that decoupling.
* **Failure should be loud, not silent.** A non-canonical
  subscription that returns the same `Subscription` object as
  a canonical one would hide the bug under "the handler isn't
  firing". Forcing the caller to canonicalise explicitly makes
  the contract visible at the call site.

For sloppy interactive use (e.g. an `acrobe info events
--watch <path>` debug command), the CLI layer canonicalises on
the caller's behalf before subscribing.

## Filesystem events

Filesystem changes are observation-shaped, not intent-shaped —
the OS reports what already happened. Domain-specific action
vocabulary, distinct from the intent actions above, and
`phase=None` throughout:

| OS event                | Action     | Phase | Source                    | Properties                          |
|-------------------------|------------|-------|---------------------------|--------------------------------------|
| File / dir created      | `created`  | None  | canonical path of entry   | `{is_dir: bool}`                     |
| File / dir deleted      | `deleted`  | None  | canonical path of entry   | `{is_dir: bool}`                     |
| File content changed    | `changed`  | None  | canonical path of file    | `{}`                                 |
| File metadata changed   | `touched`  | None  | canonical path of file    | `{}` — optional; many consumers won't care |
| Rename / move           | `moved`    | None  | canonical path, new name  | `{from: <canonical path, old>}`      |

Subscribers wanting "anything happened to this file" filter on
`action=("created", "changed", "deleted", "moved", "touched")`.
"Just content changes" — `action="changed"`.

Why a separate vocabulary from the intent actions:

* `attach`/`detach` carry a pre/post promise. A
  hypothetical `attach` event with `phase=None` would be
  surprising and force every consumer of `attach` to handle
  the missing-pre case. Distinct actions sidestep this.
* Subscribers wiring up FS-only behaviour benefit from a clean
  filter — `action=("created","changed","deleted")` is
  self-documenting.
* Future "live VFS" code that reflects FS changes into the Node
  tree may still emit `attach`/`detach` *intent* events as it
  invalidates Nodes, separate from the observation events that
  triggered the invalidation.

### `FsWatcher`

```python
class FsWatcher:
    """Bridges watchdog.Observer to the event bus. Standalone —
    not a Node, not parented to any tree. Lifecycle is the
    caller's responsibility.

    Canonicalises `base_dir` via canonicalize_fs(); all emitted
    `source` paths are likewise canonical. This is forced rather
    than optional because watchdog itself never resolves
    symlinks: paths it reports are under the real directory.
    Subscribers that gave a symlinked path would never match,
    so canonicalisation is the whole game.

    Watches a base directory (recursively or not), coalesces
    bursts via a debounce timer so editor atomic-rename and
    multi-event saves fire one bus emit per logical change."""

    def __init__(self, base_dir: str | Path,
                 *,
                 recursive: bool = True,
                 debounce_ms: int = 100): ...

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
```

`start()` spins up a `watchdog.Observer` thread on the
canonicalised `base_dir`; observer callbacks are bridged onto
the asyncio loop with `loop.call_soon_threadsafe`, debounced
per path, and dispatched to `get_bus().emit(...)` with the
canonical actions above.

The watcher is deliberately **not a Node**. Two reasons:

* It doesn't represent a *thing*; it's an instrument observing
  an external system. Treating it as a Node would invite
  attempts to summon it as a child of HwRoot, which raises the
  tree-unification question this plan defers.
* Its lifetime is bound to one CLI invocation (or one library
  consumer). Module-level instantiation by the caller is
  natural; tree parenting would only add ceremony.

Subscribers don't care about the FsWatcher's existence — they
just subscribe to a filesystem path. Multiple consumers in one
process can share a single FsWatcher on a directory (or each
can spin up its own; the bus deduplicates implicitly because
subscribers filter on path).

### Using FsWatcher

```python
from acrobe.event import get_bus
from acrobe.event.path import canonicalize_fs

# Consumer side: watch one file for content changes.
async def reload_on_change(firmware_path: str, on_change):
    abs_path = canonicalize_fs(firmware_path)
    watch_dir = os.path.dirname(abs_path)

    watcher = FsWatcher(watch_dir, recursive=False)
    sub = get_bus().subscribe(
        on_change,
        action="changed",
        source=abs_path, source_match="exact")
    await watcher.start()
    try:
        await ...  # consumer's main loop
    finally:
        sub.cancel()
        await watcher.stop()
```

The same shape covers "watch a directory for new firmware
artifacts" (`action="created"`, subtree match on the dir) or
"react when a config file is deleted" (`action="deleted"`).

Note both `watch_dir` and the subscription `source` are derived
from the canonicalised path — without that step, a user passing
`/a/b/d/foo` where `d` is a symlink to `c` would see the
watcher attached to a directory the OS never reports under,
and zero events would fire.

### Relationship to `FsRoot`

`FsRoot` does not gain watching behaviour. The Node tree
abstraction is about navigating already-existing content;
`FsWatcher` is about reacting to changes from the outside. They
can be used together (a user might want both an `FsRoot` view
of a build directory AND a watcher to know when artifacts
appear), but neither depends on the other. A future "live
VFS" where FsRoot Nodes self-invalidate on filesystem changes
is plausible — it would consume `FsWatcher` events the same
way any other subscriber does.

## Emit semantics

`bus.emit(event)`:

1. Iterates matching subscriptions (filter pass over the bus's
   subscription list). Registration order for tie-breaks but
   subscribers must not rely on it.
2. For each match:
   * Calls `handler(event)`.
   * If the return value is awaitable, schedules it as a task in
     the bus's gather pool.
   * Any synchronous exception is caught, logged at WARNING (with
     event action/phase, source, subscription repr, traceback),
     and does not propagate.
3. `await asyncio.gather(*tasks, return_exceptions=True)` waits
   for all async handlers; per-task exceptions are likewise
   logged and not propagated.
4. Returns once every handler is done.

Publisher contract: after `await emit(...)` returns, every
subscriber has had its chance to run. For the auto-reload
`(action="program", phase="pre")` case, that means consoles have
finished detaching before the programmer proceeds.

There is no cross-emit serialisation. Two concurrent emit calls
run their dispatches concurrently; subscribers that need
inter-event ordering handle it themselves.

## Handler error semantics

Per-handler exceptions are **caught and logged, never
propagated** to the publisher. The bus is a notification
channel, not an RPC; a buggy subscriber must not be able to
abort the publisher's work.

Subscribers that want retry implement it inside the handler.
The bus offers no retry policy — that's a domain concern.

Logging:

* Level WARNING for any exception.
* Logger name `acrobe.event` plus the source path as a child
  segment so `--silent-re` filtering can mute event-handler
  errors per subtree.
* Message includes action, phase, source, subscription
  identity, and the exception class. Full traceback at DEBUG.

## Worked examples

### Programmer fires program lifecycle

The decorator form covers the simplest case:

```python
class Loadable(Node):
    @Node.notified("program")
    async def write(self, source, **kwargs):
        ...
```

When progress events or extra base properties are needed:

```python
class Loadable(Node):
    async def write(self, source, do_start=False, **kwargs):
        target_path = self.parent.path if self.parent else ""
        async with self.event_emitter("program",
                                        target=target_path) as notifier:
            pages = list(plan_pages(source))
            for i, page in enumerate(pages):
                await self.__write_page(page)
                await notifier.progress(current=i + 1,
                                         total=len(pages),
                                         unit="page")
```

### Console subscribes via path

```python
class RttConsole:
    def __init__(self, target_path: str):
        self.target_path = target_path
        bus = get_bus()
        self.__sub_pre = bus.subscribe(
            self.__on_program_pre,
            action="program", phase=Phase.PRE,
            source=target_path, source_match="subtree")
        self.__sub_post = bus.subscribe(
            self.__on_program_post,
            action="program", phase=Phase.POST,
            source=target_path, source_match="subtree")

    async def __on_program_pre(self, event):
        await self.__detach()

    async def __on_program_post(self, event):
        if event.properties.get("success"):
            await self.__reattach()
```

The subscription holds the *path string* `target_path`. If the
Loadable Node at that path is replaced (hotplug, target
re-discovery), the same subscription fires for the new
instance — no re-binding needed.

### Subscribing through a Node convenience

```python
target = await root.child_summon("proby-9", "dap")
# Subtree match by default — sees events from target and all
# descendants.
sub = target.subscribe(my_handler, action="program")
```

Equivalent to:

```python
get_bus().subscribe(my_handler, action="program",
                    source=target.path, source_match="subtree")
```

The `target` reference is consumed only to extract `target.path`.
After `subscribe` returns, `target` may be garbage-collected or
replaced under the same path; the subscription persists.

### Waiting for a not-yet-existing node

```python
from acrobe.event.path import canonicalize_hw

async def wait_for_appear(root, expected_path: str, timeout: float):
    canonical = canonicalize_hw(root, expected_path)
    event = asyncio.Event()
    appeared: list[Event] = []

    async def on_attach(evt):
        appeared.append(evt)
        event.set()

    sub = get_bus().subscribe(
        on_attach,
        action="attach", phase=Phase.POST,
        source=canonical, source_match="exact")
    try:
        await asyncio.wait_for(event.wait(), timeout)
    finally:
        sub.cancel()
    return appeared[0]
```

`canonicalize_hw` here promotes a non-canonical input (e.g.
`"proby/usb-cdc-tty"`) to the canonical path that the
publisher will use when the node arrives. If
`usb-cdc-tty` doesn't exist yet (typical for hotplug waits),
the helper leaves it literal and the subscription still
matches once the node appears under that exact name.

USB-CDC reattach after a firmware reset goes through this
shape — subscribe before the firmware re-enumerates, wait for
the path to appear.

### Cross-cutting tracer

```python
get_bus().subscribe(
    lambda evt: log.info("%s/%s on %s: %s",
                          evt.action, evt.phase, evt.source,
                          evt.properties))
# No filters — sees everything. Useful for `--trace-events`
# debugging.
```

## Auto-wired lifecycle emits

The Node base class emits the structural and lifecycle events
itself so subclasses don't have to:

* `_ensure_started` wraps `start()` in an `event_emitter
  ("start")` context. The `success` property on the post event
  is False if `start()` raised; the exception still propagates
  to the caller (this is one of the few places where the
  publisher does want exceptions out).
* `stop_tree` likewise wraps `stop()` in `event_emitter("stop")`.
* `child_add` / `_child_attach` emit `(attach, post)` after the
  attach completes (no `pre`: attach is atomic, there's nothing
  to schedule against).
* `child_remove` emits `(detach, pre)` before tearing the
  subtree down and `(detach, post)` after.

Subscribers held by a stopping Node are auto-cancelled when its
`stop_tree` runs — match this via a small bookkeeping list on
the Node base for subscriptions registered through
`Node.subscribe`. Subscriptions registered directly on the bus
(via `get_bus().subscribe`) are unaffected; their lifetime is
the caller's responsibility.

## Reshaping progress reporting (later)

The existing `Node.progress(label, total, unit)` context manager
becomes:

```python
@contextmanager
def progress(self, label, total, unit=""):
    # Sync wrapper around the bus's progress phase. Creates a
    # bridge to call notifier.progress() from sync code (typical
    # for tight loops).
    ...
```

Existing callers keep their imperative shape; under the hood
they emit `(action="program"|..., phase="progress")` events on
the bus. The terminal `ProgressDelegate` becomes one
subscriber; GDB-monitor pass-through, future GUI, structured
log sinks all plug in without touching publishers.

Concrete migration question for later: should `progress()` keep
its own per-operation `label`/`unit`, or derive them from the
active `event_emitter` action? Probably keep them — `label` is
human-facing, `action` is machine-facing. Pin during the
reshape slice.

## Slicing

### Slice 1 — bus + path helpers + emit/dispatch — **done**

Landed file layout (different from the original outline below
— path utilities live with the Node, not in `event/`, and
`Action`/`Phase` collapse into `event.py`):

* `acrobe/event/__init__.py` — re-exports `Event`, `Phase`,
  `Action`, `Notifier`, `Subscription`, `EventBus`, `get_bus`,
  `reset_for_tests`, `FsWatcher`.
* `acrobe/event/event.py` — `Event` (frozen dataclass with
  `source_is(path)` / `source_under(path)` helpers), `Phase`
  (StrEnum), `Action` (constants class), `Notifier`.
* `acrobe/event/bus.py` — `EventBus`, `Subscription`,
  `get_bus`, `reset_for_tests`.
* `acrobe/node.py::Path` — static utility class holding
  `parts`, `parent_of`, `is_descendant_or_self`,
  `canonicalize_fs`, `canonicalize_hw`. Per
  feedback during implementation: predicates are general
  string operations on Node paths, not event-bus-specific;
  belong with `Node`. Same logic for the canonicalise
  helpers since they operate on the same path vocabulary.

Test files: `tests/test_node_path.py` (33 tests),
`tests/test_event_bus.py` (34 tests).

Implementation notes:

* `Event.phase` defaults to `None` so observation-shaped
  events (FS, hotplug) don't have to set it. Subscribers
  filtering on `phase=None` parameter mean "no filter";
  match-only-phase-less is done via predicate.
* `bus.subscribe(phase=(None,))` works for the rare
  "phase-less only" case.
* Handler errors logged under `acrobe.event.<dotted-source>`
  so per-subtree silencing works (`--silent-re
  acrobe.event.proby-9`).
* Subscription snapshot-at-dispatch-entry semantics:
  subscribe/cancel during an emit doesn't affect that emit
  but is visible to the next one.

### Slice 2 — Node integration — **done**

* `Node.emit(action, phase=None, **properties)` — late-imports
  `Event` / `get_bus` to break the node ↔ event circular dep.
* `Node.subscribe(handler, *, action, phase, source_match,
  predicate)` — defaults to `source_match="subtree"` (the
  Node-side convention; bare bus subscribe still defaults to
  `"exact"`). Tracks subscriptions in `self.__subscriptions`
  for auto-cancel.
* `Node.event_emitter(action, **base_properties)` —
  `@asynccontextmanager`, fires PRE on enter / POST on exit.
  POST carries `success=True` on clean exit, `success=False` +
  `error_class=<type-name>` on exception; exception still
  propagates. Yields a `Notifier`.
* `Node.notified(action)` — staticmethod decorator wrapping
  an async method with `event_emitter(action)`. Uses
  `functools.wraps`.
* `Notifier.progress(**properties)` merges base + per-tick
  properties, emits PROGRESS phase.

Test file: `tests/test_node_events.py` (covers Slices 2 + 3,
total 32 tests).

### Slice 3 — auto-wired lifecycle emits — **done**

* `Node.ensure_started` wraps `start()` in
  `self.event_emitter("start")` inside the existing
  `__start_lock`. Idempotent — emits exactly one (pre, post)
  pair per actual start.
* `Node.stop_tree` wraps `stop()` in
  `self.event_emitter("stop")` **gated on `self.__started`** —
  symmetric with `ensure_started`, only emits when there's
  something to stop. Then cancels tracked subscriptions and
  recurses into children regardless of self's state (children
  may have been started independently).
* `Node.__child_attach` records a `(parent_path,)` pending
  attach on the child instead of emitting directly. Refinement
  added during implementation: the initial fire-and-forget
  design dropped events for nodes attached during sync setup
  (before any event loop). Pending-attach is drained in:
   * `ensure_started` (before start, so attach POST always
     precedes start PRE) and
   * `child_remove` (so a child added then removed without ever
     being started still gets a paired attach + detach).
  Atomic read-and-clear in the drainer prevents concurrent
  double-emit.
* `Node.child_remove` emits `(detach, pre)` before `stop_tree`
  and `(detach, post)` after structural detach, with `source`
  captured **before** detach (so the post event reports the
  path the child had while attached, not the dangling path
  after parenthood was cleared).
* No attach/detach events for `child_transplant_to` — VFS
  format auto-detection's transient-reparent path; neither
  full attach nor detach fits the semantic.

### Slice 4 — first real domain users — **done**

* `acrobe/target/loadable.py::Loadable.write` — wrapped in
  `event_emitter("program", target=<parent.path>, do_erase,
  do_verify)`. Per-region progress via `notifier.progress
  (region=<region.path>, written=N, total=N)` between
  per-region write loops. Source is the Loadable's path;
  subtree match on the target's path picks up everything
  target-scoped.
* `acrobe/protocol/jtag.py::Chain.tlr_and_refresh` — emits
  one `(mutate, post)` after the refresh with
  `committed=True`, `changed=<before-vs-after diff>`,
  `tap_count`. Snapshot helper `__snapshot_chain` returns
  `{(tap.name, enabled)}` for the before/after diff.
* `acrobe/target/arm/cortex_m.py::CortexMCore.reset` — wrapped
  in `event_emitter("reset", kind="cpu", stop=stop)`. The
  `kind` property reserves room for `"system"` / `"watchdog"`
  later without action-namespace explosion.

Test file: `tests/test_domain_events.py` (10 tests). Synthetic
fixtures: `FakeRegion`, `FailingRegion`, scripted JTAG iface
(reused from `tests/test_jtag.py`), `_MockScs`.

Reset emits on `Core`, not `Debuggable` — that's where the
existing reset method lives. Plan text said "Debuggable /
target"; in practice the Cortex-M path's reset is on the Core.

### Slice 5 — `FsWatcher` — **done**

* `watchdog` added to `pyproject.toml` dependencies.
* `acrobe/event/fs_watcher.py::FsWatcher` — standalone (not a
  Node), wraps `watchdog.observers.Observer`. `start()` grabs
  the asyncio loop and spins up the observer thread;
  `stop()` joins via `asyncio.to_thread` so the loop doesn't
  block. `enter_event(src, action, properties)` is the
  cross-thread handoff via `call_soon_threadsafe`. Per-(path,
  action) debounce in `__schedule_emit` coalesces multi-event
  saves to one bus emit per logical change.
* `_Handler` (private subclass of
  `watchdog.events.FileSystemEventHandler`) canonicalises
  every path before handoff. Directory `modified` events
  dropped as noise.
* `observer_class` constructor kwarg accepts an alternate
  Observer class — tests inject `PollingObserver` with a
  short interval for deterministic behaviour without
  FSEvents/inotify latency. Default is `None` (watchdog
  picks per platform).

Test file: `tests/test_fs_watcher.py` (14 tests including
the symlink regression guard). Tests run against
`PollingObserver`, ~5 s total for the file.

Action vocabulary used: `created` / `changed` / `deleted` /
`moved`. `touched` (metadata-only changes) reserved in the
plan but not emitted by `_Handler` yet — add when a consumer
asks.

### Slice 6 — hotplug-driven `connected` / `disconnected` — **done**

* `acrobe/adapter/model.py::UsbEnumerator.start_watch()` /
  `stop_watch()` — opt-in async lifecycle. `start_watch`
  re-creates the ausb Context with `enable_hotplug=True`
  (replacing the polling-only context built with
  `enable_hotplug=False`), seeds the `(bus, address) →
  adapter-name` map from currently-attached recognised
  adapters, and spins up an `asyncio.ensure_future` watch
  loop draining `Context.hotplug_events()`. Both methods are
  idempotent.
* `__handle_hotplug(event)` does the actual translation —
  `ConnectionEvent` with a matching `AdapterInfo`: probes for
  serial, builds the canonical name via `make_adapter_name`,
  records `(bus, address) → name`, emits `(connected, None,
  source=HwRoot/<name>, properties={bus, address, vendor_id,
  product_id})`. `DisconnectionEvent` looks up the address,
  pops it, emits `(disconnected, None)` with `{bus,
  address}`. Unrecognised devices and unknown-address
  disconnects are silent. Per-event exceptions caught/logged.
* Action vocabulary updated from the plan's original
  `attach`/`detach` to `connected`/`disconnected`. Reason:
  the FS-events discussion established observation-shaped
  events should have distinct vocabulary from intent-shaped
  tree events. The Slice 6 text predated that decision; both
  the canonical actions table and this slice text have been
  updated to match.

Test file: `tests/test_hotplug.py` (8 tests). Mocked at the
handler level — synthetic `ConnectionEvent` /
`DisconnectionEvent` built via `__new__` (bypassing the real
libusb device construction), `FakeDescriptor` mimicking the
descriptor surface. No real USB hardware or libusb thread in
tests. Per-test isolation of `adapter_db.registry` via a
snapshot/restore fixture.

Deferred to a future hotplug-plan:
* Auto-pruning live tree Nodes when their backing USB device
  disconnects.
* Topology-based identity (bus + port-chain) for cross-mode
  device tracking (RP2-style firmware/BL device pair).
* Wiring `start_watch` into a default CLI / lifecycle path
  (currently opt-in via the API).

### Slice 7 — progress reshape (later)

* `Node.progress` rewired onto the bus's progress phase.
* Terminal `ProgressDelegate` becomes a subscriber.
* Optional GDB-monitor pass-through subscriber.

Slices 1–3 are foundational. Slice 4 unblocks the auto-reload
plan's `Loadable.write` side; Slice 5 unblocks its file-watch
side. Slices 6–7 are independent and slot in as their
respective consumers materialise.

## Open questions

* **`pre` exception propagation.** The bus always swallows
  handler exceptions, but the *publisher's* own work inside an
  `event_emitter` block can raise — the context manager
  re-raises and emits `post` with `success=False`. That's the
  shape callers want for `program` / `reset`. For
  `start`/`stop` the question is whether a failure in `start()`
  should leave the Node in a known state (probably keep the
  current behaviour: exception out, the lock keeps the state
  consistent, `_ensure_started` doesn't mark started).
* **Pre with cancellability for `mutate`?** The current design
  says subscribers can't veto; the `committed` flag is purely
  informational. If a real use case wants veto semantics on
  `mutate-pre`, we'd need a sentinel-return contract from
  handlers and to break the catch-and-log invariant. Defer
  until asked.
* **Backpressure.** A slow handler stalls `emit` until it
  finishes. Per-subscription `spawn_task=True` (fire-and-forget)
  would help; add when measured.
* **Event coalescing for progress.** Progress events from a
  tight loop will flood the bus. A per-subscription throttle
  (drop arrivals within N ms of the last) keeps the terminal
  bar responsive without saturating less-interested subscribers.
  Add during Slice 6 if needed.
* **Cross-process bus.** The wire layer (`acrobe/wire/`)
  exposes parts of one acrobe tree to another. Events
  arguably ought to flow across — but path-namespace ownership
  and link-drop semantics are open. Out of scope.
* **Wildcard / glob source filters.** Today filtering is
  exact-or-subtree. Predicate filters cover anything more
  complex; add a glob shortcut when a real cross-cutting
  subscriber wants one.
* **Subscription introspection.** A `bus.list_subscriptions()`
  for `acrobe info events` debugging. Add when needed.
