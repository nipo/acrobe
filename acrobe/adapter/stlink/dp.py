"""ST-Link Dp variants: JTAG / SWD wire mode.

ST-Link doesn't expose general bit-bang JTAG. Instead, its USB
protocol gives us **DP/AP register read/write transactions
directly** — the wire details (DPACC/APACC, line reset, ACK retries,
WAIT handling, RDBUFF pipelining) are owned by ST-Link's firmware.

We slot in above the wire layer: :class:`StLinkJtagDp` and
:class:`StLinkSwDp` are :class:`Dp` subclasses whose ``flush_ops``
translates batched ``ApRead`` / ``ApWrite`` / ``DpRead`` / ``DpWrite``
ops into ST-Link USB commands. The standard ``Dp.start`` lifecycle
(read DPIDR, power up, enumerate APs) works unchanged — it just
issues those ops through this overridden ``flush_ops``.

Side benefit: ADIv6 chips work too — ST-Link handles DPv3 on the
wire, so we get DPv3 support without needing a JtagDpV3 wire-level
implementation in acrobe.
"""

from __future__ import annotations

from ...component.arm import dp as dpmod
from ...component.arm.ap import Ap, ApIdr
from . import protocol
from .transport import StLinkTransport


class StLinkDp(dpmod.Dp):
    """Common machinery for ST-Link DP variants. Subclasses set
    :attr:`MODE_NAME` and :meth:`_enter_mode`.

    State held: a set of AP indices for which ``init_ap`` has been
    called. Non-IDR AP accesses go through ``init_ap`` first; we
    cache so each AP costs one extra USB round-trip total."""

    MODE_NAME: str = ""  # subclass: "jtag" or "swd"

    def __init__(self, transport: StLinkTransport, name: str | None = None):
        super().__init__(name=name or self.MODE_NAME or "dap")
        self._transport = transport
        self._opened_aps: set[int] = set()

    async def _enter_mode(self) -> None:
        raise NotImplementedError

    async def start(self):
        """Enter the wire mode, then run the standard
        :class:`Dp.start` (DPIDR + power-up + AP enumeration).

        Leave first so we don't trip over a previous session that
        was killed mid-run — ST-Link rejects ``enter_*`` when it's
        already in the requested mode (status 0x05)."""
        try:
            await self._transport.exit_debug()
        except Exception:
            pass
        await self._enter_mode()
        await super().start()

    async def stop(self):
        """Best-effort: leave debug mode and release any APs we
        opened."""
        for ap_num in list(self._opened_aps):
            try:
                await self._transport.close_ap(ap_num)
            except Exception:
                pass
        self._opened_aps.clear()
        try:
            await self._transport.exit_debug()
        except Exception:
            pass

    async def _enumerate_aps(self):
        """Like the base Dp._enumerate_aps, but: when the IDR
        identifies a MEM-AP, instantiate :class:`StLinkMemAp` rather
        than the standard :class:`MemAp` so memory accesses go
        through ST-Link's bulk commands (CSW manipulation via
        WRITE_DAP_REG is rejected by the firmware)."""
        from .mem_ap import StLinkMemAp

        if self.adi_version >= 6:
            self.logger.info(
                "ADIv6 chip — falling back to APSEL walk via ST-Link "
                "(BASEPTR-based ADIv6 enumeration deferred)")

        futures = [(apsel, self._discover_one_ap(apsel, StLinkMemAp))
                   for apsel in self.AP_PROBE_INDICES]
        for apsel, coro in futures:
            try:
                ap = await coro
            except Exception as exc:
                self.logger.warning(
                    "AP discovery at APSEL %d crashed: %s",
                    apsel, exc, exc_info=True)
                continue
            if ap is not None:
                self.child_add(ap)
                self.logger.info(
                    "AP%d discovered: idr=0x%08x class=0x%x type=0x%x",
                    apsel, ap.idr, ap.klass, ap.type)

    async def _discover_one_ap(self, apsel: int, mem_ap_class):
        """Read IDR; if it's a MEM-AP return ``mem_ap_class``;
        otherwise delegate to :meth:`Ap.discover` for the standard
        Ap.db lookup. Empty / power-gated APSELs return None."""
        base = apsel << 24
        try:
            idr = await self.post(dpmod.ApRead(addr=base + Ap.IDR))
        except dpmod.DpAccessFailure as exc:
            self.logger.protocol(
                "AP IDR read at APSEL %d failed: %s", apsel, exc)
            return None
        except protocol.StLinkError as exc:
            # BAD_AP_ERROR = "no AP at this index". DP_WAIT / DP_FAULT
            # can mean the AP exists but is power-gated or otherwise
            # unresponsive — same outcome from our perspective: skip.
            if exc.status in (protocol.BAD_AP_ERROR,
                              protocol.SWD_DP_WAIT, protocol.SWD_DP_FAULT,
                              protocol.SWD_AP_WAIT, protocol.SWD_AP_FAULT):
                self.logger.protocol(
                    "APSEL %d: %s — skipping",
                    apsel, protocol.status_name(exc.status))
                return None
            raise
        if idr == 0:
            return None
        ap_idr = ApIdr.from_idr(idr)
        if ap_idr.klass == Ap.CLASS_MEM_AP:
            return mem_ap_class(dp=self, base=base, idr=idr)
        # Non-MEM-AP: fall through to the standard discovery path.
        return await Ap.discover(self, base=base)

    async def _ensure_ap_open(self, ap_num: int) -> None:
        """Call INIT_AP once per AP. Failures are non-fatal: on
        ADIv6 chips, ST-Link's INIT_AP can return error codes
        (observed 0x05 on STM32MP2) yet the underlying AP register
        accesses still work through ``read_dap_reg`` /
        ``write_dap_reg``. Cache so we don't retry every transaction.
        """
        if ap_num in self._opened_aps:
            return
        try:
            await self._transport.init_ap(ap_num)
        except protocol.StLinkError as exc:
            self.logger.info(
                "INIT_AP %d returned %s; proceeding without (likely "
                "an ADIv6 AP that ST-Link can't pre-initialize, but "
                "direct register access usually still works)",
                ap_num, protocol.status_name(exc.status))
        self._opened_aps.add(ap_num)

    async def flush_ops(self, batch):
        """Translate batched DP/AP ops to ST-Link USB transactions.

        Each op is a single round-trip to the adapter — ST-Link's
        protocol is intrinsically per-transaction, so we don't get
        the JtagDp pending-read pipelining benefits. For typical use
        (CoreSight enumeration, occasional register pokes) the cost
        is fine; bulk memory accesses go through dedicated MEM-AP
        commands later."""
        for op, future in batch:
            try:
                if isinstance(op, dpmod.DpRead):
                    val = await self._transport.read_dap_reg(
                        protocol.DAP_PORT_DP, op.addr)
                    future.set_result(val)
                elif isinstance(op, dpmod.DpWrite):
                    await self._transport.write_dap_reg(
                        protocol.DAP_PORT_DP, op.addr, op.data)
                    future.set_result(None)
                elif isinstance(op, dpmod.ApRead):
                    ap_num = (op.addr >> 24) & 0xFF
                    reg_addr = op.addr & 0x00FFFFFF
                    if reg_addr != Ap.IDR:
                        await self._ensure_ap_open(ap_num)
                    val = await self._transport.read_dap_reg(
                        ap_num, reg_addr)
                    future.set_result(val)
                elif isinstance(op, dpmod.ApWrite):
                    ap_num = (op.addr >> 24) & 0xFF
                    reg_addr = op.addr & 0x00FFFFFF
                    if reg_addr != Ap.IDR:
                        await self._ensure_ap_open(ap_num)
                    await self._transport.write_dap_reg(
                        ap_num, reg_addr, op.data)
                    future.set_result(None)
                elif isinstance(op, dpmod.Abort):
                    # ST-Link manages sticky-error state on its own.
                    # Surface as a no-op so Dp.start's clear-stickies
                    # call doesn't fail.
                    future.set_result(None)
                elif isinstance(op, dpmod.Run):
                    # Idle TCK/SWCLK cycles aren't meaningful at this
                    # abstraction level — ST-Link issues them as part
                    # of each transaction.
                    future.set_result(None)
                else:
                    future.set_exception(TypeError(
                        f"StLinkDp can't lower {type(op).__name__}"))
            except Exception as exc:
                if not future.done():
                    future.set_exception(exc)


class StLinkJtagDp(StLinkDp):
    """ARM Debug Port over ST-Link in JTAG wire mode."""

    MODE_NAME = "jtag"

    async def _enter_mode(self) -> None:
        await self._transport.enter_jtag()


class StLinkSwDp(StLinkDp):
    """ARM Debug Port over ST-Link in SWD wire mode."""

    MODE_NAME = "swd"

    async def _enter_mode(self) -> None:
        await self._transport.enter_swd()
