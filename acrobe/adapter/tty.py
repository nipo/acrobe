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
from ..protocol.serial import (
    SerialPort, SerialConfig, Parity, StopBits, FlowControl, Signals,
)
from .model import Adapter, AdapterInfo, make_adapter_name


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
        self._path = path
        self._fd: int | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._read_buf = bytearray()
        self._read_event = asyncio.Event()
        self._signal_task: asyncio.Task | None = None
        self._last_signals = Signals()
        # Cached rate for platforms where kernel can't report an arbitrary one.
        self._custom_baud: int | None = None

    async def start(self):
        fd = os.open(self._path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        self._fd = fd
        self._loop = asyncio.get_running_loop()
        self._loop.add_reader(fd, self._on_readable)
        # Probe for modem-line support; ptys and some devices return ENOTTY.
        self._modem_supported = True
        try:
            self._last_signals = self._read_signals_raw()
        except OSError:
            self._modem_supported = False
        if self._modem_supported:
            self._signal_task = asyncio.create_task(self._signal_poll_loop())

    async def stop(self):
        if self._signal_task is not None:
            self._signal_task.cancel()
            try:
                await self._signal_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._fd is not None and self._loop is not None:
            try:
                self._loop.remove_reader(self._fd)
            except Exception:
                pass
            os.close(self._fd)
            self._fd = None

    # ------------------------------------------------------------------
    # Data plane
    # ------------------------------------------------------------------

    def _on_readable(self):
        try:
            data = os.read(self._fd, 4096)
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return
            # Hard error: treat as EOF
            self._loop.remove_reader(self._fd)
            self._read_event.set()
            return
        if not data:
            self._loop.remove_reader(self._fd)
            self._read_event.set()
            return
        self._read_buf.extend(data)
        self._read_event.set()

    async def read(self, size: int) -> bytes:
        out = bytearray()
        while len(out) < size:
            if self._read_buf:
                take = min(size - len(out), len(self._read_buf))
                out += self._read_buf[:take]
                del self._read_buf[:take]
                continue
            self._read_event.clear()
            await self._read_event.wait()
            if self._fd is None:
                raise EOFError("tty closed")
        return bytes(out)

    async def write(self, data: bytes) -> None:
        offset = 0
        while offset < len(data):
            try:
                n = os.write(self._fd, data[offset:])
                offset += n
            except BlockingIOError:
                fut = self._loop.create_future()
                self._loop.add_writer(
                    self._fd, lambda: fut.done() or fut.set_result(None))
                try:
                    await fut
                finally:
                    self._loop.remove_writer(self._fd)

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    async def config_set(self, cfg: SerialConfig) -> SerialConfig:
        attrs = termios.tcgetattr(self._fd)
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
        termios.tcsetattr(self._fd, termios.TCSANOW,
                          [iflag, oflag, cflag, lflag, speed, speed, cc])

        if standard:
            self._custom_baud = None
            return cfg

        applied = _set_baud_arbitrary(self._fd, cfg.baud)
        self._custom_baud = applied
        return cfg.with_(baud=applied)

    async def config_get(self) -> SerialConfig:
        attrs = termios.tcgetattr(self._fd)
        iflag, oflag, cflag, lflag, ispeed, ospeed, cc = attrs
        # Resolve baud rate: try arbitrary-rate ioctl first (Linux only),
        # then standard table, then fall back to last-applied cached rate.
        rate = _get_baud_arbitrary(self._fd)
        if rate is None:
            rate = next((r for r, c in _BAUD_TABLE.items() if c == ospeed), 0)
        if rate == 0 and self._custom_baud is not None:
            rate = self._custom_baud
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
            fcntl.ioctl(self._fd, _TIOCSBRK, 0)
        else:
            fcntl.ioctl(self._fd, _TIOCCBRK, 0)

    async def dtr_set(self, on: bool) -> None:
        self._modem_bit(TIOCM_DTR, on)

    async def rts_set(self, on: bool) -> None:
        self._modem_bit(TIOCM_RTS, on)

    def _modem_bit(self, bit: int, on: bool):
        buf = struct.pack("I", bit)
        fcntl.ioctl(self._fd, TIOCMBIS if on else TIOCMBIC, buf)

    def _read_signals_raw(self) -> Signals:
        buf = fcntl.ioctl(self._fd, TIOCMGET, struct.pack("I", 0))
        v = struct.unpack("I", buf)[0]
        return Signals(
            cts=bool(v & TIOCM_CTS),
            dsr=bool(v & TIOCM_DSR),
            ri=bool(v & TIOCM_RI),
            dcd=bool(v & TIOCM_CD),
        )

    def _read_signals(self) -> Signals:
        if not getattr(self, "_modem_supported", True):
            return Signals()
        try:
            return self._read_signals_raw()
        except OSError:
            return Signals()

    async def signals_get(self) -> Signals:
        return self._read_signals()

    async def flush(self, tx: bool = True, rx: bool = True) -> None:
        which = None
        if tx and rx:
            which = termios.TCIOFLUSH
        elif tx:
            which = termios.TCOFLUSH
        elif rx:
            which = termios.TCIFLUSH
        if which is not None:
            termios.tcflush(self._fd, which)

    async def _signal_poll_loop(self):
        try:
            while True:
                await asyncio.sleep(self._POLL_INTERVAL)
                if self._fd is None:
                    return
                try:
                    new = self._read_signals()
                except OSError:
                    return
                if new != self._last_signals:
                    old = self._last_signals
                    self._last_signals = new
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

    supported_interfaces = ["serial"]

    def __init__(self, name: str, path: str):
        super().__init__(name)
        self._path = path

    async def child_spawn(self, name):
        if name.lower() == "serial":
            port = TtySerialPort(name="serial", path=self._path)
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


class TtyEnumerator:
    """Scans the local filesystem for serial devices."""

    async def spawn(self, name):
        matches = [(n, p) for n, p in _list_tty_paths()
                   if name.lower() in n.lower()]
        if not matches:
            raise NoMatch("adapter", name)
        if len(matches) > 1:
            names = ", ".join(n for n, _ in matches)
            raise NoMatch("adapter", f"{name} (ambiguous: {names})")
        comp_name, path = matches[0]
        adapter = TtyAdapter(comp_name, path)
        return adapter

    async def scan(self):
        """Yield (info, adapter_cls, descriptor, serial) like UsbEnumerator.

        For ttys there's no USB-style descriptor, and the component
        name already fully identifies the device — we pass None as
        the serial so make_adapter_name returns info.name unchanged.
        We stash the path in a custom attribute for the CLI to show.
        """
        results = []
        for comp_name, path in _list_tty_paths():
            info = AdapterInfo(comp_name)
            info.path = path
            results.append((info, TtyAdapter, None, None))
        return results
