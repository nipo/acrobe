"""AjiHardware — one cable/board exposed by a remote AJI server.

Sits between :class:`AjiHost` and :class:`Tap` in the component tree:

    aji/<host>[:port]/<hardware>/<tap>

This level mirrors the libaji ``Hardware`` record returned by
``get_hardware()``. Each ``AjiHardware`` corresponds to a JTAG
cable (or attached board) the server has registered, and owns
everything that's per-cable on the wire:

* ``chain_id`` and the cable's name/port metadata.
* The chain lock (acquired briefly during scan, released before
  per-device locks).
* Per-tap ``open_id`` allocations.
* The TAP-level batcher: child Taps post :class:`TapOp` envelopes
  here; we translate each to ACCESS_IR / ACCESS_DR / RUN_TEST_IDLE
  / TEST_LOGIC_RESET on the parent host's :class:`AjiClient`.
* :class:`FreqCapper` integration: child taps' ``max_freq``
  aggregates here and is pushed to the server via
  ``SET_PARAMETER("JtagClock", …)``.

Hardwares that fail to scan (e.g. the cable side of a USB-Blaster
when the target board is powered off) still appear as empty nodes,
so users can see them in ``info enumerate`` and tell why no taps are
showing up.
"""

import logging
import re

from .client import (
    AjiClient,
    DR_FLAG_CAPTURE,
    IR_FLAG_CAPTURE,
)
from .client import Hardware as _LibajiHardware

from ...bitstring import BitString
from ...db import NoMatch
from ...engine import Batcher
from ...freq_capper import FreqCapper
from ...node import Node
from ...part_id import PartId
from ...protocol.jtag import (
    Tap, TapOp, _TapShift, _TapRun, _TapIrStatus,
)


_logger = logging.getLogger("aji.hardware")


def _mangle(s: str) -> str:
    out = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    return out or "hw"


def hardware_name(hw: _LibajiHardware) -> str:
    """Pick a path-friendly name for ``hw``.

    Prefer the descriptive ``hw_name`` ("DE25-Nano") over the
    less-readable ``port`` ("1-2.1"); fall back to ``port`` if the
    name is empty. Disambiguating multiple boards with the same
    ``hw_name`` is the user's problem for now.
    """
    return _mangle(hw.hw_name or hw.port or f"hw-{hw.chain_id:x}")


def _make_tap_for(device, position: int):
    """Look up a known Tap subclass and instantiate it; pick a
    path-friendly name. Subclass-supplied names (e.g.
    ``Agilex5E.__init__`` derives ``A5EA013BB23B`` from the idcode)
    are kept verbatim — they're informative and stable. Only the
    generic fallback gets a positional ``tap<N>`` name.
    """
    try:
        tap = Tap.db.call(PartId.from_idcode(device.idcode),
                          idcode=device.idcode,
                          irlen=device.irlen)
    except NoMatch:
        tap = Tap(idcode=device.idcode, irlen=device.irlen,
                  name=f"tap{position}")
    return tap


class AjiHardware(Batcher, FreqCapper, Node):
    """One ``Hardware`` entry from an AJI server.

    Constructed by :class:`AjiHost` after ``get_hardware()`` returns;
    its own ``start()`` does the scan, opens each device, and locks
    them for IR/DR access.

    Also a :class:`FreqCapper`: child taps' ``max_freq`` aggregates
    here through :meth:`FreqCapper.freq_cap_min`, and we push the
    resulting rate to the server with ``SET_PARAMETER('JtagClock')``.
    The push happens once during ``start()`` while we still hold the
    chain lock — ``jtagd`` rejects ``SET_PARAMETER`` with
    ``NOT_LOCKED`` otherwise, and re-acquiring the chain lock later
    would clash with the per-device locks we hold for IR/DR access.
    """

    def __init__(self, name: str, *, hw: _LibajiHardware) -> None:
        Batcher.__init__(self)
        Node.__init__(self, name)
        FreqCapper.__init__(self)
        self.__hw = hw
        # Tap → open_id (populated in start()).
        self.open_id_of: dict[Tap, int] = {}
        # Tap → cached current_ir, lets us skip redundant access_ir.
        self.last_ir: dict[Tap, int | None] = {}
        # Records what we successfully locked, so stop() can be lazy.
        self.locked_devices: set[int] = set()

    @property
    def chain_id(self) -> int:
        return self.__hw.chain_id

    @property
    def hw_name(self) -> str:
        return self.__hw.hw_name

    @property
    def port(self) -> str:
        return self.__hw.port

    # --- Lifecycle ------------------------------------------------------

    @property
    def __client(self) -> AjiClient:
        from .host import AjiHost
        host = self.parent_of_class(AjiHost)
        client = host.client
        if client is None:
            raise RuntimeError(
                f"AjiHost {host.name!r} is not connected; "
                f"cannot drive AjiHardware {self.name!r}")
        return client

    async def start(self) -> None:
        client = self.__client
        chain_id = self.__hw.chain_id

        # Lock the chain so we can scan + open devices on it.
        try:
            await client.lock_chain(chain_id, timeout_ms=10000)
        except Exception as e:
            _logger.warning("lock_chain(%d) on %r failed: %s",
                            chain_id, self.name, e)
            return

        try:
            devices = await client.scan_chain(chain_id)
        except Exception as e:
            _logger.warning("scan_chain(%d) on %r failed: %s",
                            chain_id, self.name, e)
            await self.__safe_unlock_chain(client, chain_id)
            return

        if not devices:
            await self.__safe_unlock_chain(client, chain_id)
            return

        # Open every device first; only then unlock the chain.
        # Locking a device while we still hold the chain lock returns
        # CHAIN_IN_USE; opening one without the chain lock returns
        # NOT_LOCKED. The libaji-canonical sequence is:
        #     lock_chain → scan → open_device(*) → unlock_chain → lock_device(*)
        opened: list[tuple[Tap, int]] = []
        seen_names: set[str] = set()
        for position, device in enumerate(devices):
            tap = _make_tap_for(device, position)
            # Disambiguate two same-idcode taps on the same chain.
            if tap.name in seen_names:
                tap._name = f"{tap.name}-{position}"
            seen_names.add(tap.name)
            try:
                open_id = await client.open_device(
                    chain_id, position, application_name="acrobe")
            except Exception as e:
                _logger.warning("open_device(%d, %d) failed: %s",
                                chain_id, position, e)
                continue
            opened.append((tap, open_id))

        # Aggregate per-tap max_freq into our FreqCapper stack. We
        # have to do this *before* unlocking the chain because
        # SET_PARAMETER requires the chain lock; pushing the rate
        # later would need a release/re-lock dance that conflicts
        # with the per-device locks.
        self.freq_cap_min(tap for tap, _ in opened)
        if self.freq is not None:
            rate = int(self.freq)
            try:
                await client.set_parameter(chain_id, "JtagClock", rate)
                _logger.info("set TCK on %r to %d Hz", self.name, rate)
            except Exception as e:
                _logger.warning("set_parameter(JtagClock=%d) on %r failed: %s",
                                rate, self.name, e)

        await self.__safe_unlock_chain(client, chain_id)

        for tap, open_id in opened:
            try:
                await client.lock_device(open_id, timeout_ms=10000)
            except Exception as e:
                _logger.warning("lock_device(%d) failed: %s", open_id, e)
                try:
                    await client.close_device(open_id)
                except Exception:
                    pass
                continue
            self.open_id_of[tap] = open_id
            self.last_ir[tap] = None
            self.locked_devices.add(open_id)
            self.child_add(tap)

    async def stop(self) -> None:
        try:
            client = self.__client
        except RuntimeError:
            # Host already disconnected; nothing to release.
            self.open_id_of.clear()
            self.last_ir.clear()
            self.locked_devices.clear()
            return
        for _tap, open_id in list(self.open_id_of.items()):
            if open_id in self.locked_devices:
                try:
                    await client.unlock_device(open_id)
                except Exception as e:
                    _logger.debug("unlock_device(%d): %s", open_id, e)
            try:
                await client.close_device(open_id)
            except Exception as e:
                _logger.debug("close_device(%d): %s", open_id, e)
        self.open_id_of.clear()
        self.last_ir.clear()
        self.locked_devices.clear()

    @staticmethod
    async def __safe_unlock_chain(client: AjiClient, chain_id: int) -> None:
        try:
            await client.unlock_chain(chain_id)
        except Exception as e:
            _logger.debug("unlock_chain(%d): %s", chain_id, e)

    # --- Path resolution -----------------------------------------------

    async def child_spawn(self, name: str):
        # Children are populated eagerly in start(); nothing to spawn.
        raise NoMatch("tap", name)

    # --- TapOp dispatch -------------------------------------------------

    async def flush_ops(self, batch: list) -> None:
        client = self.__client
        for top, future in batch:
            if not isinstance(top, TapOp):
                future.set_exception(
                    TypeError(f"AjiHardware expects TapOp, got {type(top).__name__}"))
                continue
            tap = top.tap
            op = top.op
            open_id = self.open_id_of.get(tap)
            if open_id is None:
                future.set_exception(
                    ValueError(f"Tap {tap.name!r} not registered with hardware "
                               f"{self.name!r}"))
                continue
            try:
                value = await self.__dispatch(client, tap, open_id, op)
            except Exception as e:
                if not future.done():
                    future.set_exception(e)
                continue
            if not future.done():
                future.set_result(value)

    async def __dispatch(self, client: AjiClient, tap: Tap,
                         open_id: int, op):
        """Execute one TapOp, returning the captured BitString (for
        reading shifts and IR status) or None."""
        if isinstance(op, _TapShift):
            if op.ir_value is not None and op.ir_value != self.last_ir.get(tap):
                await client.access_ir(open_id, op.ir_value)
                self.last_ir[tap] = op.ir_value
            if op.tdi is not None:
                length = len(op.tdi)
                tdi_bytes = bytes(op.tdi) if length else b""
                flags = DR_FLAG_CAPTURE if op.read_tdo else 0
                read_bytes = await client.access_dr(
                    open_id, length_dr=length,
                    write_bits=tdi_bytes,
                    flags=flags,
                    read_length=length if op.read_tdo else 0,
                )
                if op.read_tdo:
                    return BitString(read_bytes, length)
            return None
        if isinstance(op, _TapRun):
            await client.run_test_idle(open_id, op.cycles)
            return None
        if isinstance(op, _TapIrStatus):
            captured = await client.access_ir(
                open_id, instruction=(1 << tap.irlen) - 1,
                flags=IR_FLAG_CAPTURE)
            self.last_ir[tap] = (1 << tap.irlen) - 1
            return BitString(captured if captured is not None else 0,
                             tap.irlen)
        raise TypeError(f"Unknown tap op: {type(op).__name__}")

    def __repr__(self) -> str:
        return (f"<AjiHardware {self._name} chain_id={self.__hw.chain_id} "
                f"port={self.__hw.port!r} taps={len(self.open_id_of)}>")
