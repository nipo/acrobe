# PLAN — auto firmware reload

Living document. Captures the model and the slicing for the
"watch a firmware file, re-flash on change" workflow, and the
coordination contract that lets long-lived clients (consoles,
monitors, test runners) survive the reflash.

## Status

Prerequisites both landed; the workflow surface (CLI command,
client subscribers, V2 BL path) is the remaining work.

| Slice | Subject                                       | Status |
|-------|-----------------------------------------------|--------|
| 1     | `Loadable.write` emits `program` events       | done   |
| 2     | `acrobe chip auto-program` CLI                | pending |
| 3     | RTT console auto-reattach                     | pending |
| 4     | V2 USB-bootloader programmer                  | pending |

Prerequisite slices in `docs/plans/node-events.md`:

| Need                                          | node-events slice | Status |
|-----------------------------------------------|-------------------|--------|
| `program` events on `Loadable.write`          | 4                 | done   |
| `FsWatcher` for file-change notifications     | 5                 | done   |
| Hotplug `connected`/`disconnected` for V2     | 6                 | done   |

Both consumer-side pieces (the CLI command and the RTT
auto-reattach) are unblocked. V2 USB-bootloader needs more
hotplug work than Slice 6 of node-events delivered
(topology-based identity, multi-mode device tracking).

## Goal

The canonical embedded inner loop is edit → recompile → reflash
→ run → observe. Acrobe should fold the "reflash" step into a
background watcher so the developer never leaves their console.

The pattern is **not chip-family-specific**:

* Cortex-M with a debug probe — halt, write Flash via Loadable,
  reset.
* FPGA — write SRAM or NVCM via Loadable, reconfigure.
* Anything with a USB bootloader (RP2-class) — drop the running
  firmware into BL, flash via the BL adapter, reset to firmware.
* SPI flash, I²C EEPROM behind a transactor — same shape.

All of these already program through `Loadable.write(memory_map)`,
so the auto-reload command itself is target-agnostic: it watches a
file, walks it through the VFS, hands the resulting MemoryMap to
the target's Loadable.

What's genuinely new is **cooperative handoff**: the long-lived
clients reading from the target (RTT console, USB-CDC console,
FPGA register-map test runner, GDB session, …) must step out of
the way during programming and reattach after. Acrobe already
supports composing two CLI commands in one invocation via the
chain dispatcher (`&` parallel, `;` sequential, shared `HwRoot`
across segments — see `acrobe/cli/chain.py`). The natural shape is
therefore:

```
acrobe chip auto-program -r <target> RESOURCE   &   acrobe rtt console -r <target> ...
```

Two independent commands, sharing the same Node tree, coordinating
through Node-level events (see `docs/plans/node-events.md`). The
auto-program command is *only* the watch+reload half; consoles and
other long-lived clients are separate commands that subscribe to
programming events on their target.

## Prerequisite

The cooperative handoff hinges on `docs/plans/node-events.md` —
typed events with bubbling and per-handler retry. Auto-reload is
the first real user of that mechanism. Slice 1 of node-events
must land before slice 3 below.

## Model

Three pieces, none of them new Node subclasses:

### File watching via `FsWatcher` + bus subscription

Filesystem change notification is provided by the
`FsWatcher` shipped in the node-events plan (Slice 5). The
auto-program command does not own a bespoke watcher; it spins
up an `FsWatcher` on the firmware file's parent directory and
subscribes to `changed` events on the canonical path of the
file:

```python
from acrobe.event import get_bus
from acrobe.event.path import canonicalize_fs

canonical = canonicalize_fs(resource_path)
watch_dir = os.path.dirname(canonical)

watcher = FsWatcher(watch_dir, recursive=False)
sub = get_bus().subscribe(
    on_change,
    action="changed",
    source=canonical, source_match="exact")
await watcher.start()
```

Both `watch_dir` and the subscription `source` are derived
from `canonicalize_fs(resource_path)` — without that step, a
user passing `build/firmware.elf` where `build` is a symlink
into a sibling tree would see the watcher attached to a
directory the OS never reports under, and zero events would
fire (watchdog / inotify / FSEvents never resolve symlinks).

Atomic saves (write `.tmp`, rename over) are handled by
`FsWatcher`'s debounce; multi-event bursts coalesce to one bus
emit per logical change. A rename rather than a content
overwrite arrives as `action="moved"` (with the prior path in
`from`); for the auto-reload use case treat `moved` to the
firmware path the same as `changed`.

### Programming session on `Target` + `Loadable`

The publisher uses the `program` action defined in
`node-events.md`. Source is the Loadable's path; subscribers
that care about whole-target coordination use a `subtree` match
on the parent Target's path (or filter on the `target` property
the Loadable adds). Subscribers interested in a specific
Loadable use an `exact` match — e.g. option-byte programming
vs main flash.

The simplest publisher form is the auto-wired decorator:

```python
class Loadable(Node):
    @Node.notified("program")
    async def write(self, source, **kwargs):
        ...
```

For progress + target-path property, the context-manager form:

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
                                         total=len(pages))
```

`event_emitter` emits `(program, pre)` on entry, `(program,
post)` on exit (with `success=True` on clean return,
`success=False` on exception, and re-raising the exception),
and exposes `notifier.progress()` for in-flight ticks.

`Loadable.write` itself opens the `event_emitter("program")`
block (or carries the `@Node.notified("program")` decorator when
no progress events are needed), so every existing call to
`loadable.write(...)` automatically participates — not just the
auto-program command. A user running a one-shot `acrobe chip
program` benefits from the same coordination if a console
happens to be attached in the same chained invocation.

### Programmer

A thin wrapper around `Loadable.write` whose only V1
responsibility is to re-resolve the VFS node from disk on each
reload (the file content has changed, so the cached parse is
stale).

```python
class DebuggerProgrammer:
    def __init__(self, target, loadable, resource_ref): ...

    async def program(self):
        # Re-summon the VFS node — file has changed.
        node = await self.resource_ref.resolve()
        memory_map = await MemoryMap.from_node(node)
        await self.loadable.write(memory_map, do_start=True)
```

`UsbBootloaderProgrammer` is the V2 variant; see the V2 section.

## CLI surface (V1)

One new command:

### `acrobe chip auto-program -r <target> RESOURCE [--no-initial] [--debounce-ms N]`

* `RESOURCE` is the standard `ResourceRef` from `acrobe chip
  program` — ELF / UF2 / IHEX / RBF / anything the VFS handles.
* Initial reflash on startup (skipped with `--no-initial`).
* After initial reflash, holds an `FsWatcher` on the firmware's
  canonical directory and a bus subscription on the firmware's
  canonical path. Reflashes on every coalesced `changed` (or
  `moved`-onto-target) event.
* Selecting target / loadable: same flags as `chip program`
  (`-t`, `--loadable`).

That is the entire V1 command surface. Consoles, monitors, GDB
sessions, etc. are separate commands and chain in alongside:

```
acrobe chip auto-program -r myboard fw.elf            \
   '&' acrobe rtt console -r myboard --symbol _SEGGER_RTT
```

Existing console commands (RTT console, serial-server, etc.)
gain auto-reattach behaviour by subscribing to `(program, pre)` /
`(program, post)` on their target. That work is per-client and not
part of this plan — each client's plan slot adds the subscription
when convenient. The auto-reload command itself doesn't know
consoles exist; it just emits the events.

## Failure handling

Two failure modes to think about:

### Subscriber's detach handler fails

The bus catches and logs every handler exception (see node-events
plan, "Handler error semantics"). The programmer never sees
subscriber failures — the `program` action proceeds regardless.
Rationale: failing a reflash because a console is buggy is worse
than the buggy console missing a detach. The console will see the
matching `(program, post)` shortly and can try to recover then.

Subscribers that want retry on their own detach work do it
inside their handler — the bus does not retry. Typical shape
for a subscriber with retry needs:

```python
async def __on_program_begin(self, event):
    for attempt in range(3):
        try:
            await self.__detach()
            return
        except TransientError:
            await asyncio.sleep(0.5 * (attempt + 1))
    log.warning("detach failed after retries; "
                "console may misbehave during reflash")
```

### Reflash itself fails

Programmer's exception propagates out of `loadable.write`. The
`event_emitter` context manager still emits `(program, post)` in
its `finally`, with `success=False` and `error_class` set to the
exception type name. Subscribers can read those properties and decide
whether to reattach or stay detached. The auto-program command
logs the failure and keeps watching — next file change triggers
a retry. No exponential backoff for retries; the user knows when
they recompiled.

## Concurrency: reload-during-reload

If the user recompiles a second time while the previous reflash
is still running, the watcher fires a second time. The
orchestrator collapses:

* If no reload is in progress: start one.
* If a reload is in progress: set a "another change happened"
  flag; do not start a second concurrent reload.
* When the current reload finishes: if the flag is set, clear
  it and start a new reload immediately.

Avoids queue-and-replay-all-N-versions semantics; we always
chase the newest content. Implementation: a single
`asyncio.Lock` + a `pending` boolean on the orchestrator.

## V2 — USB-bootloader programmer

Sketch only; the V1 abstractions are forward-compatible.

The RP2-class dance:

1. **Trigger entry to bootloader.** Per-firmware-class hook;
   for the Pico-SDK CDC stack, set baud=1200 + pulse DTR.
   Registered against a `Db` keyed on the running firmware's
   USB descriptor.
2. **Wait for the bootloader device** at the same USB topology
   (bus number + port-chain). Captured before triggering BL
   entry so we can recognise the replacement device.
3. **Program** via the BL adapter's existing Loadable path.
4. **Reset** via the BL adapter.
5. **Wait for the firmware device** to re-enumerate at the same
   topology. The TTY/USB-CDC console reattaches once the device
   is back.

This becomes:

```python
class UsbBootloaderProgrammer:
    def __init__(self, fw_descriptor_db, bl_factory): ...

    async def program(self, source):
        topology = self.__capture_topology()
        await self.__enter_bootloader(topology)
        bl_adapter = await self.__wait_for_bl(topology)
        try:
            bl_target = await self.__discover_target(bl_adapter)
            bl_loadable = bl_target.children_of_class(Loadable)[0]
            # bl_loadable.write itself emits the program events
            # (decorator/event_emitter on Loadable.write); same
            # handlers as the debugger-programmer path.
            await bl_loadable.write(
                await MemoryMap.from_node(source),
                do_start=True)
        finally:
            await bl_adapter.close()
```

The topology-based device matching is shared infrastructure
(needed by the USB-CDC console reattach anyway); centralise it
in the USB enumerator as `enumerator.wait_for_topology(topology,
timeout)`.

## Deferred — hotplug and post-program rediscovery

Whether `chip auto-program` should call
`chain.tlr_and_refresh()` + `root.discover_targets()` after each
reflash is unsettled, and the answer is bound up in a larger
issue: **acrobe doesn't really handle hotplug today**. The
target framework's "best-effort invalidation" (cross-tree refs
surface IOError on dead components, per `docs/plans/target.md`)
is a known shortcut. Auto-reload exercises this path on every
cycle:

* **FPGA reconfig.** The JTAG chain shape can change (HPS DP
  appears or disappears). The matcher in
  `Chain._match_identities` handles the simple cases (see
  `docs/jtag-refresh.md`); reflashing repeatedly with no
  rediscovery would leave stale TAP references in subscribers.
* **USB-bootloader cycle.** The firmware USB device disappears,
  the BL device appears, then vice-versa. The USB enumerator
  doesn't currently observe device-gone events as a first-class
  signal.
* **CDC console reattach.** The TTY path may change across
  re-enumeration; the console needs `wait_for_topology` to
  re-resolve.

V1 of `chip auto-program` does NOT call rediscovery
automatically. Targets that need it can subscribe to
`(program, post)` and trigger refresh themselves, or the user can
chain an explicit refresh command. A proper fix is a hotplug
plan in its own right — the auto-reload command will be the
first feature that genuinely makes hotplug pain visible.

Concretely-deferred items, each gating its own follow-up plan:

* **Hotplug event surface** on the USB enumerator
  (`DeviceAppeared` / `DeviceGone` emits — design in
  `node-events.md` slice 4).
* **Topology-based USB lookup** (`wait_for_topology`).
* **Post-program tree refresh** — when it's safe to call, what
  to invalidate, how subscribers find out their references are
  stale.
* **USB-CDC console reattach** — gated on the two above.

## Slicing

This plan depends entirely on node-events. Its slices line up
with node-events slices:

* Auto-reload needs `program` events emitted by `Loadable.write`
  → blocked on node-events Slice 4.
* Auto-reload needs file-change notifications → blocked on
  node-events Slice 5 (`FsWatcher`).

The two prerequisites are independent and can land in either
order.

### Slice 1 — `Loadable.write` emits program events — **done**

Landed as part of node-events Slice 4. See
`docs/plans/node-events.md` for the full implementation note;
abridged here:

* `acrobe/target/loadable.py::Loadable.write` wrapped in
  `self.event_emitter("program", target=<parent.path>,
  do_erase, do_verify)`. Per-region progress emitted via
  `notifier.progress(region=<region.path>, written=N,
  total=N)` between region write loops.
* Source = Loadable's path; target's path carried as a property
  so subtree subscribers can filter on it. `pre_program` /
  `post_program` hooks run inside the emit block.
* Failure path: write raises → `event_emitter` `finally` emits
  POST with `success=False` and `error_class=<type>`, then
  the exception propagates to the caller.
* Tests in `tests/test_domain_events.py` cover the pre/post
  pair, property carry-through, per-region progress, and the
  failure path.

### Slice 2 — `acrobe chip auto-program` — **pending**

Unblocked: both Slice 1 (above) and node-events Slice 5
(`FsWatcher`) have landed.

* `acrobe/cli/auto_program.py` (or extend `acrobe/cli/chip.py`)
  with the new command. Spins up an `FsWatcher` on the firmware
  canonical directory; subscribes to `changed` on the
  canonical firmware path. Re-canonicalises on each watcher
  restart in case the symlink target changed between
  invocations.
* Reload-during-reload coalescing.
* Smoke test against a real probe + target (manual).

### Slice 3 — RTT console auto-reattach — **pending**

* `Rtt.rebind(addr)` + `Rtt.ready_event()` (incremental on the
  existing `acrobe/target/rtt.py`).
* Whatever CLI command fronts RTT today subscribes to
  `(program, pre)` (detach) / `(program, post)` (re-resolve symbol,
  rebind, reattach).
* Independent of Slice 2; lands when the RTT command gets the
  attention. `(reset, post)` from `CortexMCore.reset` (node-
  events Slice 4) is also available as a secondary trigger —
  RTT pump may want to re-rescan after a non-program reset.

### Slice 4 — V2 USB-bootloader programmer — **pending**

* Per-chip BL-entry hook registry.
* `UsbBootloaderProgrammer`.
* `enumerator.wait_for_topology` on the USB enumerator.

Partial unblock: node-events Slice 6 added
`UsbEnumerator.start_watch` and `(connected, None)` /
`(disconnected, None)` emits. That's enough to *observe*
hardware appearance, but not enough for this slice — we still
need topology-based identity (bus + port-chain) so the same
physical port can be tracked across the firmware → BL → firmware
mode transitions where VID/PID differs.

Slices 1–2 are V1. Slice 3 is the second user. Slice 4 is V2
and gated on the hotplug work above.

## Open questions

* **Where does the BL-entry hook live in the tree?** When the
  firmware exposes itself as a USB CDC, the host has a `tty.*`
  Node or a `usb.*` Node for it. The BL-entry method (baud=1200
  pulse for RP2, vendor request for others) is firmware-class
  specific. Probably a small `Db` keyed on USB descriptor,
  consulted by `UsbBootloaderProgrammer.__enter_bootloader`.
  Pin during Slice 5.
* **Post-`(program, post)` ordering for the chip itself.** After a
  Cortex-M reset, the CPU runs `_start`, then user code, then
  eventually calls `SEGGER_RTT_Init`. The RTT console's
  reattach must tolerate "not yet ready" — `Rtt.__main_loop`
  already handles that (it retries phase 1). For a USB-CDC
  console, the device may take seconds to enumerate; the
  console's `attach(timeout=...)` must be generous.
* **Multi-loadable atomicity.** A user reflashing both main
  flash and option bytes in one command should fire one
  `(program, pre)` and one `(program, post)` for the whole sequence,
  not two of each — otherwise subscribers detach-reattach
  twice. Probably wrap the user-level operation in an outer
  `event_emitter("program")` on the Target (source = target
  path), with the per-Loadable events nested inside for
  finer-grained subscribers. The subtree match means a
  subscriber filtering on target path sees the outer pair plus
  the inner pairs; consoles that only care about whole-target
  coordination use `phase=PRE` with an
  `event.source == target_path` predicate to ignore the inner
  ones. Pin when the second loadable target lands.
