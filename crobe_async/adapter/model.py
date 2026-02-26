from ..db import Db, NoMatch
from ..component import Component


class AdapterInfo:
    """USB identity for adapter matching."""

    def __init__(self, vid, pid, name, interfaces):
        self.vid = vid
        self.pid = pid
        self.name = name
        self.interfaces = interfaces

    def matches(self, descriptor):
        return (self.vid == descriptor.vendor_id
                and self.pid == descriptor.product_id)


adapter_db = Db("adapter", eq_func=AdapterInfo.matches)


class Adapter(Component):
    """Base adapter. Subclasses override open() and child_spawn()."""

    supported_interfaces = []

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

    def _iter_known(self):
        """Yield (AdapterInfo, adapters, descriptor) for all recognized USB devices."""
        self._ensure_ctx()
        for desc in self._ctx.device_filter():
            for info, adapters in adapter_db._registry.items():
                if info.matches(desc):
                    yield info, adapters, desc

    async def child_spawn(self, name):
        """Spawn adapter by name: scans USB for matching adapter_db entry."""
        for info, adapters, desc in self._iter_known():
            if name.lower() in info.name.lower():
                adapter_cls = adapters[0]
                return await adapter_cls.open(desc)

        raise NoMatch("adapter", name)

    def scan(self):
        """List all recognized USB adapters (no opening).

        Returns list of (AdapterInfo, descriptor) pairs.
        """
        return [(info, desc) for info, _adapters, desc in self._iter_known()]
