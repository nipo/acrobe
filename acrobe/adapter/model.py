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
        self._ctx = None

    def _ensure_ctx(self):
        if self._ctx is None:
            import ausb
            self._ctx = ausb.Context(enable_hotplug=False)

    def _iter_matches(self):
        """Yield (AdapterInfo, adapter_cls, descriptor) for static descriptor matches."""
        self._ensure_ctx()
        for desc in self._ctx.device_filter():
            for info, adapters in adapter_db._registry.items():
                if info.matches(desc):
                    for adapter_cls in adapters:
                        yield info, adapter_cls, desc

    async def _probe(self, descriptor, adapter_cls):
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
        for info, adapter_cls, desc in self._iter_matches():
            serial = await self._probe(desc, adapter_cls)
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
        for info, adapter_cls, desc in self._iter_matches():
            serial = await self._probe(desc, adapter_cls)
            if serial is _SKIP:
                continue
            results.append((info, adapter_cls, desc, serial))
        return results


class HwRoot(Node):
    """Root of the hardware component tree.

    Delegates child_spawn to registered enumerators (strategies).
    Adapters appear as direct children: proby-9/jtag, not USB/proby-9/jtag.
    """

    def __init__(self):
        super().__init__("HwRoot")
        self._enumerators = []

    def add_enumerator(self, enumerator):
        self._enumerators.append(enumerator)

    async def child_spawn(self, name):
        errors = []
        for enum in self._enumerators:
            try:
                return await enum.spawn(name)
            except NoMatch as e:
                errors.append(e)
        raise NoMatch("adapter", name)


_SKIP = object()
