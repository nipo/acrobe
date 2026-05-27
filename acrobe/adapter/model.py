import asyncio

from ..db import Db, NoMatch
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


def make_adapter_name(info, serial):
    """Build a lowercase component name from adapter info and serial."""
    if serial:
        return f"{info.name}-{serial}".lower()
    return info.name.lower()


class Adapter(Node):
    """Base adapter. Subclasses override open() and child_spawn()."""

    supported_interfaces = []

    @classmethod
    def serial_mangle(cls, serial):
        """Transform raw USB serial string. Override per adapter."""
        return serial

    @classmethod
    async def check(cls, device):
        """Runtime check with open device handle. Return True if compatible."""
        return True

    @classmethod
    async def open(cls, descriptor):
        """Open adapter from USB descriptor. Override in subclass."""
        raise NotImplementedError

    async def close(self):
        """Release resources. Override in subclass."""
        pass



class UsbEnumerator:
    """Scans USB bus for known adapters. Not a Node — used as a
    spawning strategy by HwRoot."""

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

    async def spawn(self, name):
        """Find and open an adapter matching name.

        Scans USB, probes serials, matches by component name (substring).
        Raises NoMatch if no match or ambiguous.
        """
        matches = []
        for info, adapter_cls, desc in self.__iter_matches():
            serial = await self.__probe(desc, adapter_cls)
            if serial is _SKIP:
                continue
            component_name = make_adapter_name(info, serial)
            if name.lower() in component_name:
                matches.append((adapter_cls, desc, component_name))

        if not matches:
            raise NoMatch("adapter", name)
        if len(matches) > 1:
            names = ", ".join(m[2] for m in matches)
            raise NoMatch("adapter", f"{name} (ambiguous: {names})")

        adapter_cls, desc, _name = matches[0]
        return await adapter_cls.open(desc)

    async def scan(self):
        """List all recognized USB adapters with serial numbers.

        Opens each device briefly to read serial and run check,
        then closes. Returns list of (AdapterInfo, adapter_cls, descriptor, serial).
        """
        results = []
        for info, adapter_cls, desc in self.__iter_matches():
            serial = await self.__probe(desc, adapter_cls)
            if serial is _SKIP:
                continue
            results.append((info, adapter_cls, desc, serial))
        return results

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

    async def child_spawn(self, name):
        errors = []
        for enum in self.enumerators:
            try:
                return await enum.spawn(name)
            except NoMatch as e:
                errors.append(e)
        raise NoMatch("adapter", name)

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


def make_hw_root():
    """Build an HwRoot wired with the standard set of enumerators.

    Always includes USB. Adds the TTY enumerator on platforms that
    support it (POSIX); silently skipped elsewhere. Adds the AJI
    enumerator so paths starting with ``aji/<host>`` resolve to a
    remote AJI server (e.g. Quartus jtagd). Adds the XVC enumerator
    for ``xvc/<host>[:port]/<chain>/...`` paths against a remote
    Xilinx Virtual Cable server. This is the canonical entry point
    for CLI commands that take a `-r` path.
    """
    root = HwRoot()
    root.add_enumerator(UsbEnumerator())
    try:
        from .tty import TtyEnumerator
        root.add_enumerator(TtyEnumerator())
    except ImportError:
        pass
    try:
        from .aji import AjiEnumerator
        root.add_enumerator(AjiEnumerator())
    except ImportError:
        pass
    try:
        from .xvc import XvcEnumerator
        root.add_enumerator(XvcEnumerator())
    except ImportError:
        pass
    try:
        from ..wire.enumerator import WireEnumerator
        root.add_enumerator(WireEnumerator())
    except ImportError:
        pass
    from .tcp import TcpEnumerator
    root.add_enumerator(TcpEnumerator())
    from .udp import UdpEnumerator
    root.add_enumerator(UdpEnumerator())
    return root


_SKIP = object()
