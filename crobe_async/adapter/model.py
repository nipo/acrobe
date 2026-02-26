from ..db import Db, NoMatch
from ..component import Component


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


class Adapter(Component):
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


class UsbEnumerator(Component):
    """Scans USB bus, spawns adapters by VID/PID match via adapter_db."""

    def __init__(self):
        super().__init__("USB")
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

    async def child_spawn(self, name):
        """Spawn adapter by name: scans USB, probes serials, matches by component name."""
        matches = []
        for info, adapter_cls, desc in self._iter_matches():
            serial = await self._probe(desc, adapter_cls)
            if serial is _SKIP:
                continue
            component_name = f"{info.name}-{serial}" if serial else info.name
            if name.lower() in component_name.lower():
                matches.append((info, adapter_cls, desc, component_name))

        if not matches:
            raise NoMatch("adapter", name)
        if len(matches) > 1:
            names = ", ".join(m[3] for m in matches)
            raise NoMatch("adapter", f"{name} (ambiguous: {names})")

        _info, adapter_cls, desc, _name = matches[0]
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


_SKIP = object()
