"""Platform TTY serial port adapter (Linux/macOS).

Implements SerialPort over a POSIX tty fd using:
  - termios for line config (parity, stop, flow, standard baud rates)
  - termios2 / TCSETS2 on Linux for arbitrary baud rates
  - IOSSIOSPEED on macOS for arbitrary baud rates
  - TIOCM ioctls for modem lines
  - loop.add_reader / write via non-blocking fd
  - 50ms polling of TIOCMGET to detect signal edges
"""

import asyncio
import errno
import fcntl
import glob
import logging
import os
import struct
import sys
import termios
import tty  # noqa: F401 — ensure termios constants populated
from pathlib import Path

from ..db import NoMatch
from ..protocol.pipe import Read, ReadSome, Write
from ..protocol.serial import (
    SerialPort, SerialConfig, Parity, StopBits, FlowControl, Signals,
)
from .model import Adapter, Enumerator, enumerator_db


# Baud rate → termios constant. Unions of Linux/macOS constants; only
# those common to both platforms are guaranteed.
def _build_baud_table():
    candidates = [
        50, 75, 110, 134, 150, 200, 300, 600, 1200, 1800, 2400, 4800,
        9600, 19200, 38400, 57600, 115200, 230400, 460800, 500000,
        576000, 921600, 1000000, 1152000, 1500000, 2000000, 2500000,
        3000000, 3500000, 4000000,
    ]
    table = {}
    for rate in candidates:
        attr = f"B{rate}"
        const = getattr(termios, attr, None)
        if const is not None:
            table[rate] = const
    return table


_BAUD_TABLE = _build_baud_table()


# Platform-specific ioctls for modem lines (same numbers on Linux and macOS)
if sys.platform == "linux":
    TIOCMGET = 0x5415
    TIOCMSET = 0x5418
    TIOCMBIS = 0x5416
    TIOCMBIC = 0x5417
    TIOCM_DTR = 0x002
    TIOCM_RTS = 0x004
    TIOCM_CTS = 0x020
    TIOCM_DSR = 0x100
    TIOCM_RI  = 0x080
    TIOCM_CD  = 0x040

    # termios2 for arbitrary baud rates (ioctl TCGETS2 / TCSETS2).
    # struct termios2 { u32 iflag, oflag, cflag, lflag; u8 c_line, c_cc[19];
    #                   u32 c_ispeed, c_ospeed; }   — 44 bytes, packed.
    _TERMIOS2_FMT = "=IIIIB19sII"
    _TERMIOS2_SIZE = 44  # must match format
    TCGETS2 = 0x802C542A
    TCSETS2 = 0x402C542B
    CBAUD   = 0x100F  # mask of all baud-rate bits in c_cflag
    BOTHER  = 0x1000  # "read literal rate from c_ispeed/c_ospeed"

elif sys.platform == "darwin":
    TIOCMGET = 0x4004746A
    TIOCMSET = 0x8004746D
    TIOCMBIS = 0x8004746C
    TIOCMBIC = 0x8004746B
    TIOCM_DTR = 0x002
    TIOCM_RTS = 0x004
    TIOCM_CTS = 0x020
    TIOCM_DSR = 0x100
    TIOCM_RI  = 0x080
    TIOCM_CD  = 0x040

    # IOSSIOSPEED = _IOW('T', 2, speed_t); speed_t is unsigned long (8 bytes).
    IOSSIOSPEED = 0x80085402
else:
    raise ImportError(f"acrobe.adapter.tty not supported on {sys.platform}")


def _set_baud_arbitrary(fd: int, rate: int) -> int:
    """Program an arbitrary baud rate via platform-specific ioctl.

    Returns the rate actually applied (may differ if the driver rounds).
    """
    if sys.platform == "linux":
        buf = fcntl.ioctl(fd, TCGETS2, b"\x00" * _TERMIOS2_SIZE)
        iflag, oflag, cflag, lflag, c_line, c_cc, ispeed, ospeed = \
            struct.unpack(_TERMIOS2_FMT, buf)
        cflag = (cflag & ~CBAUD) | BOTHER
        out = struct.pack(_TERMIOS2_FMT, iflag, oflag, cflag, lflag,
                          c_line, c_cc, rate, rate)
        fcntl.ioctl(fd, TCSETS2, out)
        # Read back to report actually-applied rate
        buf = fcntl.ioctl(fd, TCGETS2, b"\x00" * _TERMIOS2_SIZE)
        _, _, _, _, _, _, _ispeed, ospeed = struct.unpack(_TERMIOS2_FMT, buf)
        return ospeed
    elif sys.platform == "darwin":
        fcntl.ioctl(fd, IOSSIOSPEED, struct.pack("Q", rate))
        return rate


def _get_baud_arbitrary(fd: int) -> int | None:
    """Return the currently-configured baud rate via termios2 on Linux.

    Returns None if the rate is a standard one (and should be read via
    the normal termios path instead) or if termios2 isn't usable.
    """
    if sys.platform == "linux":
        try:
            buf = fcntl.ioctl(fd, TCGETS2, b"\x00" * _TERMIOS2_SIZE)
        except OSError:
            return None
        _, _, cflag, _, _, _, _, ospeed = struct.unpack(_TERMIOS2_FMT, buf)
        if (cflag & CBAUD) == BOTHER:
            return ospeed
        return None
    return None  # macOS has no matching "get": caller must rely on cache


class TtySerialPort(SerialPort):
    """Async SerialPort backed by a POSIX tty fd."""

    _POLL_INTERVAL = 0.050  # signal-change poll period (seconds)

    def __init__(self, name: str, path: str):
        super().__init__(name)
        self.__path = path
        self.__fd: int | None = None
        self.__loop: asyncio.AbstractEventLoop | None = None
        self.__read_buf = bytearray()
        self.__read_event = asyncio.Event()
        self.__eof = False
        # FIFO-fair locks keep concurrent read / write ops delivering
        # bytes in post order even though each runs in its own task.
        self.__rx_lock = asyncio.Lock()
        self.__tx_lock = asyncio.Lock()
        self.__signal_task: asyncio.Task | None = None
        self.__last_signals = Signals()
        # Cached rate for platforms where kernel can't report an arbitrary one.
        self.__custom_baud: int | None = None
        self.__modem_supported = True

    async def start(self):
        fd = os.open(self.__path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        self.__fd = fd
        self.__eof = False
        self.__loop = asyncio.get_running_loop()
        self.__loop.add_reader(fd, self.__on_readable)
        # Probe for modem-line support; ptys and some devices return ENOTTY.
        try:
            self.__last_signals = self.__read_signals_raw()
        except OSError:
            self.__modem_supported = False
        if self.__modem_supported:
            self.__signal_task = asyncio.create_task(self.__signal_poll_loop())

    async def stop(self):
        if self.__signal_task is not None:
            self.__signal_task.cancel()
            try:
                await self.__signal_task
            except (asyncio.CancelledError, Exception):
                pass
            self.__signal_task = None
        self.__teardown()

    def __teardown(self):
        """Release the fd and put the port into a permanent closed state.
        Idempotent. Called by stop() and by the hard-error paths (device
        unplugged / re-enumerated): once the fd is dead we drop it and unblock
        readers, so further ops fast-fail on the `__fd is None` guard instead
        of writing to a stale fd forever. A fresh port (re-summoned after the
        device returns) is a new object with a new fd -- this one is done."""
        if self.__loop is not None and self.__fd is not None:
            for remove in (self.__loop.remove_reader, self.__loop.remove_writer):
                try:
                    remove(self.__fd)
                except Exception:
                    pass
        if self.__fd is not None:
            try:
                os.close(self.__fd)
            except OSError:
                pass
            self.__fd = None
        self.__eof = True
        self.__read_event.set()

    # ------------------------------------------------------------------
    # Data plane
    # ------------------------------------------------------------------

    def __on_readable(self):
        if self.__fd is None:
            return
        try:
            data = os.read(self.__fd, 4096)
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return
            self.__teardown()   # hard error (device gone): drop the fd
            return
        if not data:
            self.__teardown()   # EOF
            return
        self.__read_buf.extend(data)
        self.__read_event.set()

    async def flush_ops(self, batch):
        # Both directions can block indefinitely (rx: no data yet;
        # tx: kernel buffer full on a slow line), so every op runs in
        # its own task — the per-direction locks keep byte order.
        for op, future in batch:
            if isinstance(op, Write):
                asyncio.create_task(self.__write_task(op.data, future))
            elif isinstance(op, (Read, ReadSome)):
                asyncio.create_task(self.__read_task(op, future))
            else:
                if not future.done():
                    future.set_exception(TypeError(
                        f"TtySerialPort: unsupported op {type(op).__name__}"))

    async def __read_task(self, op, future):
        try:
            async with self.__rx_lock:
                data = await self.__deliver(op)
        except Exception as exc:
            if not future.done():
                future.set_exception(exc)
            return
        if not future.done():
            future.set_result(data)

    async def __rx_wait(self) -> bool:
        """Wait for buffered bytes. Returns False on EOF with an
        empty buffer."""
        while not self.__read_buf:
            if self.__eof:
                return False
            self.__read_event.clear()
            await self.__read_event.wait()
        return True

    async def __deliver(self, op) -> bytes:
        if isinstance(op, ReadSome):
            if not await self.__rx_wait():
                return b""
            take = min(op.max_size, len(self.__read_buf))
            out = bytes(self.__read_buf[:take])
            del self.__read_buf[:take]
            return out
        if op.size is None:
            if not await self.__rx_wait():
                raise EOFError("tty closed")
            out = bytes(self.__read_buf)
            self.__read_buf.clear()
            return out
        out = bytearray()
        while len(out) < op.size:
            if not await self.__rx_wait():
                raise EOFError("tty closed")
            take = min(op.size - len(out), len(self.__read_buf))
            out += self.__read_buf[:take]
            del self.__read_buf[:take]
        return bytes(out)

    async def __write_task(self, data, future):
        try:
            async with self.__tx_lock:
                await self.__write_all(data)
        except Exception as exc:
            if not future.done():
                future.set_exception(exc)
            return
        if not future.done():
            future.set_result(None)

    async def __write_all(self, data: bytes):
        offset = 0
        while offset < len(data):
            if self.__fd is None:
                raise EOFError("tty closed")
            try:
                n = os.write(self.__fd, data[offset:])
                offset += n
            except BlockingIOError:
                fut = self.__loop.create_future()
                self.__loop.add_writer(
                    self.__fd, lambda: fut.done() or fut.set_result(None))
                try:
                    await fut
                finally:
                    if self.__fd is not None:
                        self.__loop.remove_writer(self.__fd)
            except InterruptedError:
                continue   # EINTR: retry the write
            except OSError as e:
                # Device gone (ENXIO "Device not configured", ENODEV, EIO, ...).
                # Drop the fd so this and every later op fail fast rather than
                # re-issuing os.write on a dead handle indefinitely.
                self.__teardown()
                raise EOFError(f"tty write failed: {e}") from e

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    async def config_set(self, cfg: SerialConfig) -> SerialConfig:
        attrs = termios.tcgetattr(self.__fd)
        iflag, oflag, cflag, lflag, ispeed, ospeed, cc = attrs

        # Clear/raw config bits
        cflag |= termios.CREAD | termios.CLOCAL
        iflag &= ~(termios.IGNBRK | termios.BRKINT | termios.PARMRK
                   | termios.ISTRIP | termios.INLCR | termios.IGNCR
                   | termios.ICRNL | termios.IXON | termios.IXOFF | termios.IXANY)
        oflag &= ~(termios.OPOST)
        lflag &= ~(termios.ECHO | termios.ECHONL | termios.ICANON
                   | termios.ISIG | termios.IEXTEN)

        # Data bits
        cflag &= ~termios.CSIZE
        cflag |= {5: termios.CS5, 6: termios.CS6,
                  7: termios.CS7, 8: termios.CS8}[cfg.data_bits]

        # Parity
        if cfg.parity is Parity.NONE:
            cflag &= ~termios.PARENB
            cflag &= ~termios.PARODD
        elif cfg.parity is Parity.EVEN:
            cflag |= termios.PARENB
            cflag &= ~termios.PARODD
        elif cfg.parity is Parity.ODD:
            cflag |= termios.PARENB
            cflag |= termios.PARODD
        else:
            raise NotImplementedError(
                f"parity {cfg.parity} not supported via termios on this platform")

        # Stop bits
        if cfg.stop_bits is StopBits.ONE:
            cflag &= ~termios.CSTOPB
        elif cfg.stop_bits is StopBits.TWO:
            cflag |= termios.CSTOPB
        else:
            raise NotImplementedError("1.5 stop bits not supported via termios")

        # Flow control
        # CRTSCTS exists on Linux+macOS but under slightly different constants;
        # termios module exposes it where available.
        crtscts = getattr(termios, "CRTSCTS", 0)
        if cfg.flow_control is FlowControl.NONE:
            cflag &= ~crtscts
            iflag &= ~(termios.IXON | termios.IXOFF)
        elif cfg.flow_control is FlowControl.RTS_CTS:
            if not crtscts:
                raise NotImplementedError("CRTSCTS not available")
            cflag |= crtscts
            iflag &= ~(termios.IXON | termios.IXOFF)
        elif cfg.flow_control is FlowControl.XON_XOFF:
            cflag &= ~crtscts
            iflag |= termios.IXON | termios.IXOFF
        else:
            raise NotImplementedError(
                f"flow control {cfg.flow_control} not supported")

        # If the target rate is standard, program it via termios; otherwise
        # apply a placeholder standard rate (B9600) here, then override with
        # the platform-specific ioctl after tcsetattr.
        standard = cfg.baud in _BAUD_TABLE
        speed = _BAUD_TABLE[cfg.baud] if standard else termios.B9600
        cc[termios.VMIN] = 0
        cc[termios.VTIME] = 0
        termios.tcsetattr(self.__fd, termios.TCSANOW,
                          [iflag, oflag, cflag, lflag, speed, speed, cc])

        if standard:
            self.__custom_baud = None
            return cfg

        applied = _set_baud_arbitrary(self.__fd, cfg.baud)
        self.__custom_baud = applied
        return cfg.with_(baud=applied)

    async def config_get(self) -> SerialConfig:
        attrs = termios.tcgetattr(self.__fd)
        iflag, oflag, cflag, lflag, ispeed, ospeed, cc = attrs
        # Resolve baud rate: try arbitrary-rate ioctl first (Linux only),
        # then standard table, then fall back to last-applied cached rate.
        rate = _get_baud_arbitrary(self.__fd)
        if rate is None:
            rate = next((r for r, c in _BAUD_TABLE.items() if c == ospeed), 0)
        if rate == 0 and self.__custom_baud is not None:
            rate = self.__custom_baud
        size_bits = {termios.CS5: 5, termios.CS6: 6,
                     termios.CS7: 7, termios.CS8: 8}[cflag & termios.CSIZE]
        if not (cflag & termios.PARENB):
            parity = Parity.NONE
        elif cflag & termios.PARODD:
            parity = Parity.ODD
        else:
            parity = Parity.EVEN
        stop = StopBits.TWO if (cflag & termios.CSTOPB) else StopBits.ONE
        crtscts = getattr(termios, "CRTSCTS", 0)
        if crtscts and (cflag & crtscts):
            flow = FlowControl.RTS_CTS
        elif iflag & (termios.IXON | termios.IXOFF):
            flow = FlowControl.XON_XOFF
        else:
            flow = FlowControl.NONE
        return SerialConfig(
            baud=rate, data_bits=size_bits, parity=parity,
            stop_bits=stop, flow_control=flow,
        )

    # ------------------------------------------------------------------
    # Control lines
    # ------------------------------------------------------------------

    async def break_set(self, on: bool) -> None:
        if on:
            # termios has no "start break"; use TIOCSBRK/TIOCCBRK on Linux
            # and ioctl on macOS. These constants differ per-platform.
            fcntl.ioctl(self.__fd, _TIOCSBRK, 0)
        else:
            fcntl.ioctl(self.__fd, _TIOCCBRK, 0)

    async def dtr_set(self, on: bool) -> None:
        self.__modem_bit(TIOCM_DTR, on)

    async def rts_set(self, on: bool) -> None:
        self.__modem_bit(TIOCM_RTS, on)

    def __modem_bit(self, bit: int, on: bool):
        buf = struct.pack("I", bit)
        fcntl.ioctl(self.__fd, TIOCMBIS if on else TIOCMBIC, buf)

    def __read_signals_raw(self) -> Signals:
        buf = fcntl.ioctl(self.__fd, TIOCMGET, struct.pack("I", 0))
        v = struct.unpack("I", buf)[0]
        return Signals(
            cts=bool(v & TIOCM_CTS),
            dsr=bool(v & TIOCM_DSR),
            ri=bool(v & TIOCM_RI),
            dcd=bool(v & TIOCM_CD),
        )

    def __read_signals(self) -> Signals:
        if not self.__modem_supported:
            return Signals()
        try:
            return self.__read_signals_raw()
        except OSError:
            return Signals()

    async def signals_get(self) -> Signals:
        return self.__read_signals()

    async def flush(self, tx: bool = True, rx: bool = True) -> None:
        which = None
        if tx and rx:
            which = termios.TCIOFLUSH
        elif tx:
            which = termios.TCOFLUSH
        elif rx:
            which = termios.TCIFLUSH
        if which is not None:
            termios.tcflush(self.__fd, which)

    async def __signal_poll_loop(self):
        try:
            while True:
                await asyncio.sleep(self._POLL_INTERVAL)
                if self.__fd is None:
                    return
                try:
                    new = self.__read_signals()
                except OSError:
                    return
                if new != self.__last_signals:
                    old = self.__last_signals
                    self.__last_signals = new
                    self._emit_signals(old, new)
        except asyncio.CancelledError:
            raise


# TIOCSBRK / TIOCCBRK are platform-specific
if sys.platform == "linux":
    _TIOCSBRK = 0x5427
    _TIOCCBRK = 0x5428
elif sys.platform == "darwin":
    _TIOCSBRK = 0x2000747B
    _TIOCCBRK = 0x2000747A


# ----------------------------------------------------------------------
# Adapter + enumerator
# ----------------------------------------------------------------------

class TtyAdapter(Adapter):
    """Adapter wrapping a single platform serial device path."""

    def __init__(self, name: str, path: str):
        super().__init__(name)
        self.__path = path

    @property
    def ident(self):
        return self.__path

    def child_hints(self):
        return ["serial"]

    async def child_spawn(self, name):
        if name.lower() == "serial":
            port = TtySerialPort(name="serial", path=self.__path)
            return port
        raise NoMatch("interface", name)

    async def close(self):
        pass


def _list_tty_paths() -> list[tuple[str, str]]:
    """Return list of (component_name, device_path)."""
    results = []
    seen_paths = set()

    if sys.platform == "linux":
        # Prefer stable names in /dev/serial/by-id (udev)
        by_id = sorted(glob.glob("/dev/serial/by-id/*"))
        for link in by_id:
            try:
                target = os.path.realpath(link)
            except OSError:
                continue
            if target in seen_paths:
                continue
            seen_paths.add(target)
            ident = os.path.basename(link)
            results.append((f"tty-{ident}", target))

        # Fall back to direct globs for anything not covered above.
        # /dev/ttyS\d+ are pseudo-terminals from the Linux 8250 driver —
        # 32 mostly-empty slots; skip them.
        for pattern in ("/dev/ttyUSB*", "/dev/ttyACM*"):
            for path in sorted(glob.glob(pattern)):
                if path in seen_paths:
                    continue
                seen_paths.add(path)
                results.append((f"tty-{os.path.basename(path)}", path))

    elif sys.platform == "darwin":
        for pattern in ("/dev/cu.usbmodem*", "/dev/cu.usbserial*",
                        "/dev/cu.SLAB_*", "/dev/cu.*"):
            for path in sorted(glob.glob(pattern)):
                if path in seen_paths:
                    continue
                seen_paths.add(path)
                results.append((f"tty-{os.path.basename(path)}", path))

    return results


class TtyEnumerator(Enumerator):
    """Scans the local filesystem for serial devices and attaches one
    `TtyAdapter` per discovered path."""

    async def populate(self, hw_root):
        for comp_name, path in _list_tty_paths():
            if hw_root.has_child(comp_name):
                continue
            hw_root.child_add(TtyAdapter(comp_name, path))


enumerator_db.register("tty")(TtyEnumerator)
