"""Tests for UsbEnumerator hotplug emit (Slice 6).

Mock at the event-handler level — actually running an ausb
Context with hotplug enabled would require real USB hardware
and a libusb background thread we don't want in a unit suite.
The handler does the real work; the surrounding watch loop is
trivial glue.
"""

import pytest

import ausb

from acrobe.adapter.model import (
    Adapter, AdapterInfo, UsbEnumerator, adapter_db, make_adapter_name,
)
from acrobe.event import Event, get_bus, reset_for_tests


# ----- Fixtures -----

@pytest.fixture(autouse=True)
def isolated_bus():
    reset_for_tests()
    yield
    reset_for_tests()


@pytest.fixture(autouse=True)
def isolated_adapter_db():
    """Snapshot the global adapter_db registry around each test."""
    saved = {k: list(v) for k, v in adapter_db.registry.items()}
    try:
        yield
    finally:
        adapter_db.registry.clear()
        adapter_db.registry.update(saved)


# ----- Fakes -----

class FakeHandle:
    def close(self):
        pass


class FakeDevice:
    def __init__(self, serial):
        self.serial = serial
        self.handle = FakeHandle()


class FakeDescriptor:
    """Quacks like an ausb device descriptor for the bits the
    enumerator uses."""

    def __init__(self, *, bus=1, address=42, vid=0x1234, pid=0xabcd,
                 serial="DEAD"):
        self.bus = bus
        self.address = address
        self.vendor_id = vid
        self.product_id = pid
        self.manufacturer = ""
        self.product = ""
        self.__serial = serial

    def open(self):
        return FakeDevice(self.__serial)


def make_conn_event(descriptor):
    """ausb.ConnectionEvent built without going through its
    __init__ (which expects a real libusb device object)."""
    evt = ausb.ConnectionEvent.__new__(ausb.ConnectionEvent)
    evt.device = descriptor
    return evt


def make_disc_event(descriptor):
    evt = ausb.DisconnectionEvent.__new__(ausb.DisconnectionEvent)
    evt.device = descriptor
    return evt


# ----- Tests -----

class TestHotplugEmit:
    @pytest.mark.asyncio
    async def test_connection_event_for_known_adapter_emits(self):
        info = AdapterInfo("testadp", vid=0x1234, pid=0xabcd)
        adapter_db.registry.setdefault(info, []).append(Adapter)

        desc = FakeDescriptor(bus=1, address=42, vid=0x1234, pid=0xabcd,
                              serial="DEAD")
        events: list[Event] = []
        get_bus().subscribe(lambda e: events.append(e), action="connected")

        enum = UsbEnumerator()
        await enum._UsbEnumerator__handle_hotplug(make_conn_event(desc))

        assert len(events) == 1
        evt = events[0]
        assert evt.source == "HwRoot/" + make_adapter_name(info, "DEAD")
        assert evt.phase is None
        assert evt.properties["bus"] == 1
        assert evt.properties["address"] == 42
        assert evt.properties["vendor_id"] == 0x1234
        assert evt.properties["product_id"] == 0xabcd

    @pytest.mark.asyncio
    async def test_connection_event_for_unknown_device_is_silent(self):
        # No matching AdapterInfo registered → no emit.
        desc = FakeDescriptor(vid=0xdead, pid=0xbeef)
        events: list[Event] = []
        get_bus().subscribe(lambda e: events.append(e))

        enum = UsbEnumerator()
        await enum._UsbEnumerator__handle_hotplug(make_conn_event(desc))

        assert events == []

    @pytest.mark.asyncio
    async def test_disconnection_after_connection_emits_disconnected(self):
        info = AdapterInfo("testadp", vid=0x1234, pid=0xabcd)
        adapter_db.registry.setdefault(info, []).append(Adapter)

        desc = FakeDescriptor(bus=2, address=7, serial="ABC")
        connected: list[Event] = []
        disconnected: list[Event] = []
        get_bus().subscribe(lambda e: connected.append(e), action="connected")
        get_bus().subscribe(lambda e: disconnected.append(e),
                            action="disconnected")

        enum = UsbEnumerator()
        await enum._UsbEnumerator__handle_hotplug(make_conn_event(desc))
        await enum._UsbEnumerator__handle_hotplug(make_disc_event(desc))

        assert len(connected) == 1
        assert len(disconnected) == 1
        assert connected[0].source == disconnected[0].source
        assert disconnected[0].phase is None
        assert disconnected[0].properties["bus"] == 2
        assert disconnected[0].properties["address"] == 7

    @pytest.mark.asyncio
    async def test_disconnect_for_unknown_address_is_silent(self):
        # Disconnect for a (bus, address) we never saw connected
        # — silent, no spurious emit.
        desc = FakeDescriptor(bus=99, address=99)
        events: list[Event] = []
        get_bus().subscribe(lambda e: events.append(e),
                            action="disconnected")

        enum = UsbEnumerator()
        await enum._UsbEnumerator__handle_hotplug(make_disc_event(desc))

        assert events == []

    @pytest.mark.asyncio
    async def test_disconnect_clears_address_mapping(self):
        # After disconnect, a re-connect on the same (bus, address)
        # behaves freshly — and a second disconnect emits nothing.
        info = AdapterInfo("testadp", vid=0x1234, pid=0xabcd)
        adapter_db.registry.setdefault(info, []).append(Adapter)

        desc = FakeDescriptor(bus=3, address=4, serial="X")
        disconnected: list[Event] = []
        get_bus().subscribe(lambda e: disconnected.append(e),
                            action="disconnected")

        enum = UsbEnumerator()
        await enum._UsbEnumerator__handle_hotplug(make_conn_event(desc))
        await enum._UsbEnumerator__handle_hotplug(make_disc_event(desc))
        await enum._UsbEnumerator__handle_hotplug(make_disc_event(desc))
        # Second disconnect: silent (address already cleared).
        assert len(disconnected) == 1

    @pytest.mark.asyncio
    async def test_source_path_is_canonical_adapter_path(self):
        # Subscribers subscribing to HwRoot/<name> with exact match
        # see the connect event.
        info = AdapterInfo("myadp", vid=0x1111, pid=0x2222)
        adapter_db.registry.setdefault(info, []).append(Adapter)

        desc = FakeDescriptor(vid=0x1111, pid=0x2222, serial="42")
        expected_name = make_adapter_name(info, "42")
        expected_source = f"HwRoot/{expected_name}"

        seen: list[str] = []
        get_bus().subscribe(
            lambda e: seen.append(e.source),
            action="connected",
            source=expected_source, source_match="exact")

        enum = UsbEnumerator()
        await enum._UsbEnumerator__handle_hotplug(make_conn_event(desc))

        assert seen == [expected_source]

    @pytest.mark.asyncio
    async def test_handler_silent_on_device_open_failure(self):
        # Descriptor.open() raising means the device declines
        # probing — treated as not-recognised, no emit.
        info = AdapterInfo("flaky", vid=0x9999, pid=0x8888)
        adapter_db.registry.setdefault(info, []).append(Adapter)

        class FlakyDesc(FakeDescriptor):
            def open(self):
                raise OSError("permission denied")

        desc = FlakyDesc(vid=0x9999, pid=0x8888)
        events: list[Event] = []
        get_bus().subscribe(lambda e: events.append(e))

        enum = UsbEnumerator()
        await enum._UsbEnumerator__handle_hotplug(make_conn_event(desc))

        assert events == []

    @pytest.mark.asyncio
    async def test_event_with_no_device_is_skipped(self):
        # Defensive: a hotplug event whose device is None (ausb's
        # base class default) must not crash the handler.
        evt = ausb.DisconnectionEvent.__new__(ausb.DisconnectionEvent)
        evt.device = None

        events: list[Event] = []
        get_bus().subscribe(lambda e: events.append(e))

        enum = UsbEnumerator()
        await enum._UsbEnumerator__handle_hotplug(evt)

        assert events == []
