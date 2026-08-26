"""Linux i2c-dev adapter (`acrobe.adapter.linux.i2cdev`).

The fake device decodes I2C_RDWR the way the kernel does -- follows
the ``msgs`` pointer, reads write buffers, fills read buffers -- so
the whole lowering runs without hardware.
"""

import ctypes
import errno
import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="Linux ioctl ABI")

from acrobe.adapter.linux.char_device import CharDevice  # noqa: E402
from acrobe.adapter.linux.i2cdev import (  # noqa: E402
    I2C_FUNC_I2C, I2C_FUNCS, I2C_M_RD, I2C_RDWR, I2C_RETRIES, I2C_TIMEOUT,
    I2cBusInfo, I2cMsg, I2cRdwrIoctlData, I2cdevAdapter, I2cdevEnumerator,
    I2cdevInterface,
)
from acrobe.node import Node  # noqa: E402
from acrobe.protocol import i2c  # noqa: E402


def apply_option(node, key, value):
    """Apply an option the way `child_summon` does, via Node's MRO walk."""
    Node._Node__option_set(node, key, value)


class Message:
    """One decoded ``struct i2c_msg`` as it reached the kernel."""

    def __init__(self, msg: I2cMsg):
        self.addr = msg.addr
        self.flags = msg.flags
        self.len = msg.len
        self.data = ctypes.string_at(msg.buf, msg.len) if msg.len else b""

    @property
    def reads(self) -> bool:
        return bool(self.flags & I2C_M_RD)

    def __repr__(self):
        kind = "r" if self.reads else "w"
        return f"<{kind} 0x{self.addr:02x} {self.len}B {self.data.hex()}>"


class FakeI2cDevice(CharDevice):
    """i2c-dev far enough to drive the lowering.

    ``script`` is consumed one entry per I2C_RDWR: ``None`` for a
    plain success, an exception to raise, an int to return in place of
    the message count, or bytes to serve to the read message.
    """

    def __init__(self, path="/dev/i2c-1", *, funcs=I2C_FUNC_I2C):
        super().__init__(path)
        self.funcs = funcs
        self.calls = []
        self.retries = None
        self.timeout = None
        self.script = []
        self.default_read = b""
        self.__opened = False

    async def open(self):
        self.__opened = True

    async def close(self):
        self.__opened = False

    @property
    def opened(self):
        return self.__opened

    def call(self, request, arg, mutate=True):
        if request == I2C_FUNCS:
            arg.value = self.funcs
            return 0
        if request == I2C_RETRIES:
            self.retries = arg
            return 0
        if request == I2C_TIMEOUT:
            self.timeout = arg
            return 0
        if request == I2C_RDWR:
            return self.__rdwr(arg)
        raise AssertionError(f"unexpected ioctl 0x{request:08x}")

    def __rdwr(self, request: I2cRdwrIoctlData):
        array = ctypes.cast(
            request.msgs, ctypes.POINTER(I2cMsg * request.nmsgs)).contents
        decoded = [Message(array[i]) for i in range(request.nmsgs)]
        self.calls.append(decoded)

        outcome = self.script.pop(0) if self.script else None
        if isinstance(outcome, BaseException):
            raise outcome
        done = outcome if isinstance(outcome, int) else request.nmsgs
        served = outcome if isinstance(outcome, bytes) else self.default_read
        for index in range(min(done, request.nmsgs)):
            msg = array[index]
            if msg.flags & I2C_M_RD and msg.len:
                chunk = (served + bytes(msg.len))[:msg.len]
                ctypes.memmove(msg.buf, chunk, msg.len)
        return done

    @property
    def flat(self):
        """Every message, flattened across ioctls."""
        return [m for call in self.calls for m in call]


async def make_interface(device, **options):
    iface = I2cdevInterface(device.path, device=device,
                            info=I2cBusInfo(device.path, sysfs_dir="/nonexistent",
                                            devices_dir="/nonexistent"))
    for key, value in options.items():
        apply_option(iface, key, value)
    await iface.start()
    return iface


class TestStartup:
    async def test_smbus_only_adapter_is_refused(self):
        # A funcs mask without I2C_FUNC_I2C is the desktop SMBus case.
        dev = FakeI2cDevice(funcs=0x0001_0000)
        with pytest.raises(RuntimeError, match="I2C_FUNC_I2C"):
            await make_interface(dev)

    async def test_options_reach_the_kernel(self):
        dev = FakeI2cDevice()
        await make_interface(dev, retries="3", timeout="0.2")
        assert dev.retries == 3
        # I2C_TIMEOUT counts in units of 10 ms.
        assert dev.timeout == 20

    async def test_stop_closes_the_device(self):
        dev = FakeI2cDevice()
        iface = await make_interface(dev)
        await iface.stop()
        assert not dev.opened

    async def test_frequency_is_not_claimed_when_unknown(self):
        dev = FakeI2cDevice()
        iface = await make_interface(dev, fmax="100k")
        # No device tree, so no clock to report -- and certainly not
        # the requested one, which i2c-dev cannot apply.
        assert iface.freq is None


class TestTransfers:
    async def test_write_only_is_one_message(self):
        dev = FakeI2cDevice()
        iface = await make_interface(dev)
        assert await iface.post(
            i2c.Transfer(0x50, data_w=b"\x01\x02")) is None
        assert len(dev.calls) == 1
        [msg] = dev.calls[0]
        assert (msg.addr, msg.reads, msg.data) == (0x50, False, b"\x01\x02")

    async def test_read_only_is_one_message(self):
        dev = FakeI2cDevice()
        dev.default_read = b"\xaa\xbb\xcc\xdd"
        iface = await make_interface(dev)
        assert await iface.post(i2c.Transfer(0x50, size_r=4)) == \
            b"\xaa\xbb\xcc\xdd"
        [msg] = dev.calls[0]
        assert (msg.reads, msg.len) == (True, 4)

    async def test_write_then_read_is_one_ioctl_with_two_messages(self):
        dev = FakeI2cDevice()
        dev.default_read = b"\x42"
        iface = await make_interface(dev)
        assert await iface.post(
            i2c.Transfer(0x50, data_w=b"\x00\x10", size_r=1)) == b"\x42"
        # One ioctl: the repeated START between the two messages is
        # exactly what Transfer means.
        assert len(dev.calls) == 1
        write, read = dev.calls[0]
        assert (write.reads, write.data) == (False, b"\x00\x10")
        assert (read.reads, read.len) == (True, 1)

    async def test_oversized_transfer_is_rejected(self):
        dev = FakeI2cDevice()
        iface = await make_interface(dev)
        with pytest.raises(ValueError, match="16-bit"):
            await iface.post(i2c.Transfer(0x50, data_w=bytes(0x10000)))
        assert dev.calls == []


class TestTransactions:
    async def test_each_item_is_its_own_ioctl(self):
        dev = FakeI2cDevice()
        iface = await make_interface(dev)
        result = await iface.post(i2c.Transaction((
            i2c.Transfer(0x50, data_w=b"\x01"),
            i2c.Transfer(0x50, data_w=b"\x02"),
            i2c.Transfer(0x50, size_r=2),
        )))
        # Fusing them into one ioctl would drop the STOPs between.
        assert len(dev.calls) == 3
        assert result == (None, None, bytes(2))

    async def test_failure_cancels_the_rest_of_its_transaction(self):
        dev = FakeI2cDevice()
        dev.script = [None, OSError(errno.ENXIO, "No such device or address")]
        iface = await make_interface(dev)
        with pytest.raises(i2c.AddressNack):
            await iface.post(i2c.Transaction((
                i2c.Transfer(0x50, data_w=b"\x01"),
                i2c.Transfer(0x51, data_w=b"\x02"),
                i2c.Transfer(0x50, data_w=b"\x03"),
            )))
        # The third item was predicated on the second.
        assert len(dev.calls) == 2

    async def test_a_failing_transaction_spares_its_siblings(self):
        dev = FakeI2cDevice()
        dev.script = [OSError(errno.ENXIO, "nope"), None]
        iface = await make_interface(dev)
        bad = iface.post(i2c.Transfer(0x51, data_w=b"\x01"))
        good = iface.post(i2c.Transfer(0x50, data_w=b"\x02"))
        with pytest.raises(i2c.AddressNack):
            await bad
        # An I2C transaction ends with a STOP, so a NACK leaves the bus
        # defined and the next transaction is still safe.
        assert await good is None


class TestErrorMapping:
    @pytest.mark.parametrize("code,expected", [
        (errno.ENXIO, i2c.AddressNack),
        (errno.EREMOTEIO, i2c.DataNack),
    ])
    async def test_nack_errnos_become_protocol_exceptions(self, code, expected):
        dev = FakeI2cDevice()
        dev.script = [OSError(code, "nack")]
        iface = await make_interface(dev)
        with pytest.raises(expected):
            await iface.post(i2c.Transfer(0x50, data_w=b"\x01"))

    async def test_other_errnos_stay_oserror_with_context(self):
        dev = FakeI2cDevice()
        dev.script = [OSError(errno.ETIMEDOUT, "Connection timed out")]
        iface = await make_interface(dev)
        with pytest.raises(OSError, match="I2C_RDWR at 0x50"):
            await iface.post(i2c.Transfer(0x50, data_w=b"\x01"))

    async def test_no_message_through_is_an_address_nack(self):
        dev = FakeI2cDevice()
        dev.script = [0]
        iface = await make_interface(dev)
        with pytest.raises(i2c.AddressNack):
            await iface.post(i2c.Transfer(0x50, data_w=b"\x01", size_r=1))

    async def test_partial_transfer_is_a_data_nack(self):
        dev = FakeI2cDevice()
        dev.script = [1]
        iface = await make_interface(dev)
        with pytest.raises(i2c.DataNack):
            await iface.post(i2c.Transfer(0x50, data_w=b"\x01", size_r=1))


class TestWaitAck:
    async def test_polls_until_the_slave_answers(self):
        dev = FakeI2cDevice()
        nack = OSError(errno.ENXIO, "nack")
        dev.script = [nack, nack, nack, None]
        iface = await make_interface(dev)

        assert await iface.post(
            i2c.WaitAck(0x50, timeout_s=5.0, interval_s=0.001)) is None

        assert len(dev.calls) == 4
        # A zero-length write is START + addr + STOP and nothing else.
        assert all(m.len == 0 and not m.reads for m in dev.flat)

    async def test_timeout_raises(self):
        dev = FakeI2cDevice()
        dev.script = [OSError(errno.ENXIO, "nack")] * 1000
        iface = await make_interface(dev)
        with pytest.raises(i2c.WaitAckTimeout):
            await iface.post(
                i2c.WaitAck(0x50, timeout_s=0.02, interval_s=0.001))

    async def test_timeout_cancels_the_rest_of_its_transaction(self):
        dev = FakeI2cDevice()
        dev.script = [OSError(errno.ENXIO, "nack")] * 1000
        iface = await make_interface(dev)
        with pytest.raises(i2c.WaitAckTimeout):
            await iface.post(i2c.Transaction((
                i2c.WaitAck(0x50, timeout_s=0.02, interval_s=0.001),
                i2c.Transfer(0x50, data_w=b"\x01"),
            )))
        assert all(m.len == 0 for m in dev.flat)

    async def test_rejected_zero_length_write_switches_to_a_read_probe(self):
        dev = FakeI2cDevice()
        dev.script = [OSError(errno.EOPNOTSUPP, "not supported"), None]
        iface = await make_interface(dev)

        await iface.post(i2c.WaitAck(0x50, timeout_s=5.0, interval_s=0.001))

        assert len(dev.calls) == 2
        assert dev.flat[0].len == 0 and not dev.flat[0].reads
        assert dev.flat[1].reads and dev.flat[1].len == 1

        # The answer is remembered, so it is never asked again.
        dev.calls.clear()
        await iface.post(i2c.WaitAck(0x50, timeout_s=5.0, interval_s=0.001))
        assert dev.flat[0].reads

    async def test_probe_option_forces_the_read_form(self):
        dev = FakeI2cDevice()
        iface = await make_interface(dev, wait_probe="read")
        await iface.post(i2c.WaitAck(0x50, timeout_s=5.0, interval_s=0.001))
        assert dev.flat[0].reads and dev.flat[0].len == 1

    async def test_bad_probe_option_is_rejected(self):
        dev = FakeI2cDevice()
        iface = I2cdevInterface(dev.path, device=dev)
        with pytest.raises(ValueError, match="zlen or read"):
            apply_option(iface, "wait_probe", "quick")


class TestI2cMemIntegration:
    async def test_eeprom_preset_reads_through_the_real_component(self):
        dev = FakeI2cDevice()
        dev.default_read = bytes(range(32))
        iface = await make_interface(dev)

        mem = await iface.child_summon("24lc64(saddr=0x50)")
        data = await mem.read(0, 8)

        assert data == bytes(range(8))
        # A 2-byte word address, then the read: one ioctl, two messages.
        write, read = dev.calls[-1]
        assert (write.addr, write.data) == (0x50, b"\x00\x00")
        assert read.reads and read.len == 8


class TestEnumeration:
    async def test_buses_sort_numerically(self, tmp_path):
        for index in (0, 1, 2, 10, 11):
            (tmp_path / f"i2c-{index}").touch()
        enumerator = I2cdevEnumerator(dev_glob=str(tmp_path / "i2c-*"))
        assert [p.rsplit("/", 1)[1] for p in enumerator.paths()] == [
            "i2c-0", "i2c-1", "i2c-2", "i2c-10", "i2c-11"]

    async def test_populate_is_idempotent(self, tmp_path):
        from acrobe.adapter.model import HwRoot
        (tmp_path / "i2c-1").touch()
        root = HwRoot()
        enumerator = I2cdevEnumerator(dev_glob=str(tmp_path / "i2c-*"))
        await enumerator.populate(root)
        await enumerator.populate(root)
        assert [c.name for c in root.children] == ["i2c-1"]

    async def test_ident_carries_the_driver_label(self, tmp_path):
        sysfs = tmp_path / "sys"
        (sysfs / "i2c-1").mkdir(parents=True)
        (sysfs / "i2c-1" / "name").write_text("bcm2835 (i2c@7e804000)\n")
        info = I2cBusInfo("/dev/i2c-1", sysfs_dir=str(sysfs),
                          devices_dir=str(tmp_path / "devices"))
        adapter = I2cdevAdapter("i2c-1", "/dev/i2c-1", info=info)
        assert adapter.ident == "/dev/i2c-1 (bcm2835 (i2c@7e804000))"
        assert adapter.child_hints() == ["i2c"]

    async def test_ident_is_just_the_path_when_unlabelled(self):
        info = I2cBusInfo("/dev/i2c-1", sysfs_dir="/nonexistent",
                          devices_dir="/nonexistent")
        adapter = I2cdevAdapter("i2c-1", "/dev/i2c-1", info=info)
        assert adapter.ident == "/dev/i2c-1"

    async def test_clock_frequency_is_read_big_endian(self, tmp_path):
        node = tmp_path / "devices" / "i2c-1" / "of_node"
        node.mkdir(parents=True)
        (node / "clock-frequency").write_bytes((100_000).to_bytes(4, "big"))
        info = I2cBusInfo("/dev/i2c-1", sysfs_dir="/nonexistent",
                          devices_dir=str(tmp_path / "devices"))
        assert info.clock_hz == 100_000


class TestStructLayout:
    def test_i2c_msg_matches_the_kernel(self):
        # Three u16, a pad, then a pointer: 12 bytes on 32-bit, 16 on 64.
        assert ctypes.sizeof(I2cMsg) == 8 + ctypes.sizeof(ctypes.c_void_p)
        offsets = {name: getattr(I2cMsg, name).offset
                   for name, _ in I2cMsg._fields_}
        assert offsets["addr"] == 0
        assert offsets["flags"] == 2
        assert offsets["len"] == 4
        assert offsets["buf"] == ctypes.sizeof(ctypes.c_void_p)

    def test_rdwr_ioctl_data_matches_the_kernel(self):
        offsets = {name: getattr(I2cRdwrIoctlData, name).offset
                   for name, _ in I2cRdwrIoctlData._fields_}
        assert offsets["msgs"] == 0
        assert offsets["nmsgs"] == ctypes.sizeof(ctypes.c_void_p)

