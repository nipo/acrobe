import asyncio

from ..db import Db
from ..node import Node


class AdapterInfo:
    """USB identity for adapter pre-filtering.

    Matches on VID/PID (always available from descriptor) and optionally
    on manufacturer/product strings (require device open internally,
    may fail on inaccessible devices).
    """

    def __init__(self, name, *, vid=None, pid=None, manufacturer=None, product=None):
        self.name = name
        self.vid = vid
        self.pid = pid
        self.manufacturer = manufacturer
        self.product = product

    def matches(self, descriptor):
        if self.vid is not None and self.vid != descriptor.vendor_id:
            return False
        if self.pid is not None and self.pid != descriptor.product_id:
            return False
        if self.manufacturer is not None:
            try:
                if self.manufacturer.lower() not in descriptor.manufacturer.lower():
                    return False
            except Exception:
                return False
        if self.product is not None:
            try:
                if self.product.lower() not in descriptor.product.lower():
                    return False
            except Exception:
                return False
        return True


adapter_db = Db("adapter", eq_func=AdapterInfo.matches)

# Enumerator factories register here so the standard set is pluggable
# (out-of-tree enumerators add themselves via the `acrobe_plugin`
# import path). Keyed by a short name; the value is a zero-argument
# callable returning an Enumerator instance. `make_hw_root` walks the
# registry and instantiates every entry.
enumerator_db = Db("enumerator")


def make_adapter_name(info, serial):
    """Build a lowercase component name from adapter info and serial."""
    if serial:
        return f"{info.name}-{serial}".lower()
    return info.name.lower()


class Adapter(Node):
    """Base adapter — a live but unopened Node holding device identity.

    An adapter is attached to `HwRoot` by its enumerator with identity
    only (name, `AdapterInfo`, USB descriptor); it holds no device
    handle yet. `start()` (run when the adapter is attached to the
    started root) may do a transient open to interrogate the device
    and cache the result for `child_hints()`. The long-lived session
    handle is acquired lazily when an interface child is first
    summoned — held by the adapter when the device multiplexes
    concurrent interfaces, or by the interface child when it must be a
    singleton.

    Subclasses keep the `(name, info, descriptor)` constructor shape
    so `UsbEnumerator` can build them generically.
    """

    def __init__(self, name, info=None, descriptor=None):
        super().__init__(name)
        self.info = info
        self.descriptor = descriptor

    @classmethod
    def serial_mangle(cls, serial):
        """Transform raw USB serial string. Override per adapter."""
        return serial

    @classmethod
    async def check(cls, device):
        """Runtime check with open device handle. Return True if compatible."""
        return True

    def child_hints(self):
        """Interface (or board) names that can be summoned from this
        adapter. Replaces the former `supported_interfaces` class
        attribute. Sync, no IO — read off state cached by `start()`."""
        return []

    @property
    def ident(self):
        """Short identity string for `info adapters` (e.g. `0403:6015`
        for USB). Empty when the medium has no descriptor."""
        d = self.descriptor
        if d is not None and hasattr(d, "vendor_id"):
            return f"{d.vendor_id:04x}:{d.product_id:04x}"
        return ""

    async def close(self):
        """Release resources. Override in subclass."""
        pass


class Enumerator:
    """Strategy that attaches child Nodes to `HwRoot` at start.

    Two flavours, one contract:

    * Listing enumerators (USB, TTY) scan their medium and attach one
      unopened `Adapter` per discovered device.
    * Broker enumerators (wire, tcp, udp, aji, xvc) attach a single
      namespace Node that resolves host/endpoint children on demand.

    `populate` is idempotent — it is the rescan path too, so it must
    skip children already present (matched by name).
    """

    async def populate(self, hw_root):
        raise NotImplementedError



class UsbEnumerator(Enumerator):
    """Scans the USB bus for known adapters and attaches one unopened
    `Adapter` per match under `HwRoot`."""

    def __init__(self):
        self.__ctx = None
        # Hotplug-watch state. Lazily populated by start_watch();
        # cleared by stop_watch.
        self.__watch_task = None
        self.__hotplug_iter = None
        # (bus, address) → adapter name, populated on connect and
        # consulted on disconnect so the disconnected emit uses
        # the right name.
        self.__known_by_addr: dict[tuple[int, int], str] = {}

    def __ensure_ctx(self):
        if self.__ctx is None:
            import ausb
            self.__ctx = ausb.Context(enable_hotplug=False)
            # Hook the context's close into the lifecycle so it
            # doesn't leak when the process exits without a tree-level
            # teardown. The context persists for the process lifetime
            # otherwise.
            from ..lifecycle import on_shutdown
            on_shutdown(self.__close_ctx)

    async def __close_ctx(self):
        if self.__ctx is not None:
            self.__ctx.close()
            self.__ctx = None

    def __iter_matches(self):
        """Yield (AdapterInfo, adapter_cls, descriptor) for static descriptor matches."""
        self.__ensure_ctx()
        for desc in self.__ctx.device_filter():
            for info, adapters in adapter_db.registry.items():
                if info.matches(desc):
                    for adapter_cls in adapters:
                        yield info, adapter_cls, desc

    async def __probe(self, descriptor, adapter_cls):
        """Open device briefly to read serial and run runtime check.

        Returns mangled serial (may be None if device has no serial),
        or _SKIP if the device should be ignored.
        """
        try:
            device = descriptor.open()
        except Exception:
            return _SKIP
        try:
            if not await adapter_cls.check(device):
                return _SKIP
            try:
                serial_raw = device.serial
            except Exception:
                serial_raw = None
            return adapter_cls.serial_mangle(serial_raw)
        finally:
            device.handle.close()

    async def populate(self, hw_root):
        """Attach one unopened `Adapter` per recognised USB device.

        Each candidate is briefly opened to read its serial (for the
        component name) and run the adapter's runtime `check`, then
        closed — the adapter holds the descriptor and opens its own
        session handle later, on first interface summon. Idempotent:
        adapters already present (by name) are left untouched.
        """
        for info, adapter_cls, desc in self.__iter_matches():
            serial = await self.__probe(desc, adapter_cls)
            if serial is _SKIP:
                continue
            name = make_adapter_name(info, serial)
            if hw_root.has_child(name):
                continue
            hw_root.child_add(adapter_cls(name, info, desc))

    async def __resolve_name(self, desc) -> str | None:
        """Find an adapter name that descriptor `desc` would
        produce if summoned. None if no registered AdapterInfo
        matches or if the device declines probing."""
        for info, adapters in adapter_db.registry.items():
            if not info.matches(desc):
                continue
            for adapter_cls in adapters:
                serial = await self.__probe(desc, adapter_cls)
                if serial is _SKIP:
                    continue
                return make_adapter_name(info, serial)
        return None

    @staticmethod
    def __source_path(name: str, root_name: str = "HwRoot") -> str:
        return f"{root_name}/{name}"

    async def __seed_known_devices(self):
        """Populate the (bus, address) → name map with currently-
        attached recognised adapters. Lets disconnect events that
        happen shortly after watch-start be matched to a name."""
        for desc in self.__ctx.device_filter():
            name = await self.__resolve_name(desc)
            if name is not None:
                self.__known_by_addr[(desc.bus, desc.address)] = name

    async def __handle_hotplug(self, event) -> None:
        """Translate one ausb hotplug event into an event-bus emit."""
        import ausb
        from ..event import Event, get_bus
        device = getattr(event, "device", None)
        if device is None:
            return
        key = (device.bus, device.address)
        if isinstance(event, ausb.ConnectionEvent):
            name = await self.__resolve_name(device)
            if name is None:
                return
            self.__known_by_addr[key] = name
            await get_bus().emit(Event(
                source=self.__source_path(name),
                action="connected",
                phase=None,
                properties={
                    "bus": device.bus,
                    "address": device.address,
                    "vendor_id": device.vendor_id,
                    "product_id": device.product_id,
                }))
        elif isinstance(event, ausb.DisconnectionEvent):
            name = self.__known_by_addr.pop(key, None)
            if name is None:
                return
            await get_bus().emit(Event(
                source=self.__source_path(name),
                action="disconnected",
                phase=None,
                properties={
                    "bus": device.bus,
                    "address": device.address,
                }))

    async def __watch_loop(self):
        """Drain hotplug events from ausb and dispatch each to the
        bus. Per-event exceptions are caught so one bad event
        doesn't kill the watcher."""
        try:
            async for event in self.__hotplug_iter:
                try:
                    await self.__handle_hotplug(event)
                except BaseException:
                    import logging
                    logging.getLogger("acrobe.adapter.usb").warning(
                        "hotplug handler failed for %r",
                        event, exc_info=True)
        except asyncio.CancelledError:
            pass

    async def start_watch(self):
        """Enable USB hotplug observation. Emits `(connected, None)`
        and `(disconnected, None)` on the bus for recognised
        adapters; unrecognised USB devices are ignored.

        Source path is `HwRoot/<adapter-name>` — the path the
        adapter would have if summoned. Subscribers interested in
        a specific adapter subscribe by that path.

        Idempotent — calling twice is a no-op."""
        if self.__watch_task is not None:
            return
        # Re-create the context with hotplug enabled. The original
        # was built `enable_hotplug=False` to avoid the background
        # libusb thread on the polling-only path.
        if self.__ctx is not None:
            await self.__close_ctx()
        import ausb
        from ..lifecycle import on_shutdown
        self.__ctx = ausb.Context(enable_hotplug=True)
        on_shutdown(self.__close_ctx)
        self.__known_by_addr = {}
        await self.__seed_known_devices()
        self.__hotplug_iter = self.__ctx.hotplug_events()
        self.__watch_task = asyncio.ensure_future(self.__watch_loop())

    async def stop_watch(self):
        """Stop the watcher. Idempotent."""
        if self.__hotplug_iter is not None:
            self.__hotplug_iter.close()
            self.__hotplug_iter = None
        if self.__watch_task is not None:
            self.__watch_task.cancel()
            try:
                await self.__watch_task
            except (asyncio.CancelledError, BaseException):
                pass
            self.__watch_task = None
        self.__known_by_addr.clear()


class HwRoot(Node):
    """Root of the unified tree.

    Adapters appear as direct children (proby-9/jtag, not
    USB/proby-9/jtag). Targets also live flat under the root, as
    siblings of adapters, deposited there by `TargetDiscovery`.

    `child_spawn` is delegated to registered enumerators
    (strategies). `request_discovery()` schedules a target-discovery
    sweep with coalescing — multiple calls before the next event
    loop turn run at most one sweep.
    """

    def __init__(self):
        super().__init__("HwRoot")
        self.enumerators = []
        self.__discovery = None
        self.__discovery_task = None
        self.__discovery_needs_run = False

    def add_enumerator(self, enumerator):
        self.enumerators.append(enumerator)

    def has_child(self, name):
        """Exact-name membership test for enumerator dedup. Unlike
        `child_lookup`, this never substring-matches — two adapters
        whose names share a prefix (`jlink-ob-123` / `jlink-ob-1234`)
        must both attach."""
        return any(c.name == name for c in self.children)

    async def start(self):
        """Populate the tree from every enumerator, then start each
        attached child.

        Enumerator population and per-adapter start are isolated: one
        medium that fails to scan, or one device that refuses a
        transient interrogation, degrades to a warning and leaves the
        rest of the tree intact. Children are started here (rather than
        via the `child_add` auto-start path) so that by the time
        `start()` returns every adapter has run `start()` and cached
        whatever `child_hints()` needs.
        """
        for enum in self.enumerators:
            try:
                await enum.populate(self)
            except Exception:
                self.logger.warning(
                    "enumerator %s failed to populate",
                    type(enum).__name__, exc_info=True)
        children = list(self.children)
        results = await asyncio.gather(
            *(c.ensure_started() for c in children),
            return_exceptions=True)
        for child, result in zip(children, results):
            if isinstance(result, BaseException):
                self.logger.warning(
                    "adapter %s failed to start: %s", child.name, result)

    def request_discovery(self):
        """Schedule a `TargetDiscovery` sweep over this tree.

        Coalescing: if a sweep is already pending or running, the
        request raises a re-run flag rather than starting a second
        task. Callers may await the returned Task to wait for the
        active sweep (including any re-run triggered during it).
        """
        self.__discovery_needs_run = True
        if self.__discovery_task is None or self.__discovery_task.done():
            self.__discovery_task = asyncio.ensure_future(self.__run_discovery())
        return self.__discovery_task

    async def discover_targets(self):
        """Run discovery to fixed point and await completion."""
        await self.request_discovery()

    def __ensure_discovery(self):
        if self.__discovery is None:
            from ..target.discovery import TargetDiscovery
            self.__discovery = TargetDiscovery()
        return self.__discovery

    async def __run_discovery(self):
        await asyncio.sleep(0)
        while self.__discovery_needs_run:
            self.__discovery_needs_run = False
            await self.__ensure_discovery().run(self)


enumerator_db.register("usb")(UsbEnumerator)


def _import_standard_enumerators():
    """Import the standard enumerator modules so their
    `enumerator_db.register` calls fire. Each is optional — a missing
    optional dependency drops that medium, not the whole root."""
    import importlib
    for module in (".tty", ".aji", ".xvc", ".tcp", ".udp",
                   ".stream_endpoint", ".linux.spidev", ".linux.i2cdev"):
        try:
            importlib.import_module(module, package=__package__)
        except ImportError:
            pass
    # The wire enumerator can't self-register: it is imported during
    # `protocol.jtag`'s bootstrap and must not pull the adapter package
    # in at top level. Register it here instead, after imports settle.


def make_hw_root():
    """Build an HwRoot wired with every registered enumerator.

    The standard set (USB, TTY, AJI, XVC, TCP, UDP, wire) registers
    itself in `enumerator_db`; out-of-tree adapters add more through
    the `acrobe_plugin` import path. This is the builder behind the
    `get_hw_root()` singleton and is also used directly by tests that
    need a throwaway root.
    """
    _import_standard_enumerators()
    root = HwRoot()
    for factories in enumerator_db.registry.values():
        for factory in factories:
            try:
                root.add_enumerator(factory())
            except Exception:
                root.logger.warning(
                    "enumerator factory %r failed", factory, exc_info=True)
    return root


__hw_root = None


def get_hw_root():
    """Process-wide singleton HwRoot.

    Built lazily on first call (registrations only — no hardware is
    touched until the root is started). The CLI and library entry
    points share this instance so one USB scan and one handle per
    adapter serve every command in the process.
    """
    global __hw_root
    if __hw_root is None:
        __hw_root = make_hw_root()
    return __hw_root


def reset_hw_root_for_tests():
    """Drop the singleton so the next `get_hw_root()` rebuilds it.
    Tests that exercise the singleton call this in teardown."""
    global __hw_root
    __hw_root = None


_SKIP = object()
