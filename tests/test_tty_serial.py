"""TtySerialPort tested against a pty pair.

The master side is the unit under test; the slave side mimics a device.
"""

import asyncio
import os
import pty
import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform not in ("linux", "darwin"),
    reason="TTY support requires Linux or macOS",
)


from acrobe.adapter.tty import TtySerialPort
from acrobe.protocol.serial import SerialConfig, Parity, StopBits, FlowControl


def _pty_pair():
    """Return (master_fd, slave_path).

    The test keeps master_fd open as the "other side"; TtySerialPort
    opens the slave path as its device.
    """
    master_fd, slave_fd = pty.openpty()
    slave_path = os.ttyname(slave_fd)
    # Don't keep the slave fd: the UUT will open it by path.
    os.close(slave_fd)
    # Master fd must stay open until teardown so the slave remains valid.
    return master_fd, slave_path


async def test_roundtrip_data_and_config():
    master_fd, slave_path = _pty_pair()
    try:
        # Master non-blocking so os.read doesn't block the test thread
        os.set_blocking(master_fd, False)

        port = TtySerialPort(name="test", path=slave_path)
        await port.start()
        try:
            cfg = SerialConfig(baud=115200, data_bits=8, parity=Parity.NONE,
                               stop_bits=StopBits.ONE,
                               flow_control=FlowControl.NONE)
            applied = await port.config_set(cfg)
            assert applied.baud == 115200
            got_cfg = await port.config_get()
            assert got_cfg.baud == 115200
            assert got_cfg.data_bits == 8
            assert got_cfg.parity == Parity.NONE

            # Inject bytes via master → port.read()
            os.write(master_fd, b"hello")
            data = await asyncio.wait_for(port.read(5), timeout=1.0)
            assert data == b"hello"

            # Send bytes via port.write() → read from master
            await port.write(b"world")
            buf = bytearray()
            for _ in range(50):
                try:
                    chunk = os.read(master_fd, 128)
                    buf.extend(chunk)
                    if bytes(buf) == b"world":
                        break
                except BlockingIOError:
                    pass
                await asyncio.sleep(0.01)
            assert bytes(buf) == b"world"
        finally:
            await port.stop()
    finally:
        os.close(master_fd)


async def test_arbitrary_baud_rate():
    """Non-standard rate is accepted on both Linux and macOS (ptys on
    Linux honour TCSETS2; on macOS they may reject IOSSIOSPEED with
    ENOTTY since ptys aren't real UARTs — skip gracefully there)."""
    master_fd, slave_path = _pty_pair()
    try:
        port = TtySerialPort(name="test", path=slave_path)
        await port.start()
        try:
            try:
                applied = await port.config_set(SerialConfig(baud=250000))
            except OSError as e:
                pytest.skip(f"pty rejected arbitrary baud ioctl: {e}")
            assert applied.baud in (250000,)  # driver may round
            got = await port.config_get()
            assert got.baud == applied.baud
        finally:
            await port.stop()
    finally:
        os.close(master_fd)
