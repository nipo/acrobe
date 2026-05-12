"""TI ICE-Pick router TAP driver.

ICE-Pick is the router TAP found at the head of TI CC13xx / CC26xx
(and other) JTAG chains. A bare chain only enumerates the ICE-Pick
itself; debug and test sub-TAPs are gated behind router writes that
physically insert them into the JTAG scan chain.

The driver:

* Issues the protocol ``CONNECT`` so the router becomes responsive.
* Reads ``ICEPICK_ID`` to know how many secondary taps exist.
* Exposes :meth:`tap_enable` so chip-specific subclasses can plug in
  the actual TAPs they care about. Each enable / disable mutates the
  parent ``Chain``'s geometry via ``tap_insert`` / ``tap_remove``.

Subclasses populate ``TAPS`` mapping ``(Block, index)`` to an
``(idcode, Tap_subclass)`` pair. Lower keys land closer to TDI
(higher ``ir_pre``); higher keys land closer to TDO. This is the
convention used by the crobe driver and is preserved here so that
chips with multiple enabled sub-TAPs (e.g. CC2650 with DP + AON)
produce a stable, deterministic chain layout independent of enable
ordering.
"""

from enum import IntEnum

from ...bitfield import Bitfield, Field, BooleanField, MappingField, EnumField
from ...protocol import jtag


class Block(IntEnum):
    """ICE-Pick block selector for the ``ROUTER`` register."""

    IcePick = 0
    TestTap = 1
    DebugTap = 2


class IcePickBlock(IntEnum):
    """Sub-register selector inside the ``IcePick`` block."""

    AllZero = 0
    Control = 1
    LinkingMode = 2


class IcePickId(Bitfield):
    version = Field(24, 8)
    test_taps = Field(20, 4)
    emu_taps = Field(16, 4)
    icepick_type = Field(4, 12)
    capabilities = Field(0, 4)


class Connect(Bitfield):
    write_en = BooleanField(7)
    res = Field(4, 3)
    key = MappingField(0, 4, {9: "connected", 6: "disconnected"})


class UserCode(Bitfield):
    version = Field(28, 4)
    variant = Field(12, 16)
    res = Field(1, 11)
    one = Field(0, 1)


class SecondaryTap(Bitfield):
    visible_tap = BooleanField(9)
    select_tap = BooleanField(8)
    tap_accessible = BooleanField(1)
    tap_present = BooleanField(0)


class SecondaryTestTap(SecondaryTap):
    pass


class SecondaryDebugTap(SecondaryTap):
    inhibit_sleep = BooleanField(20)
    in_reset = BooleanField(17)
    reset_control = Field(14, 3)
    force_active = BooleanField(3)


class Router(Bitfield):
    write_en = BooleanField(31)
    block = EnumField(28, 3, Block)
    register = Field(24, 4)
    value = Field(0, 24)


class IcePick(jtag.Tap):
    """Base ICE-Pick router TAP.

    ``max_freq`` caps the parent JTAG interface at 10 MHz when this
    TAP is in the chain. Connect / router operations additionally
    take a local 100 kHz cap via ``freq_capped`` — the router runs on
    a slow internal clock and corrupts at higher rates.
    """

    irlen = 6
    max_freq = 10e6

    # Idle TCKs in Run-Test/Idle after each router shift. The router
    # latches the shifted command on its slow internal clock; without
    # this dwell, subsequent shifts may race the latch.
    ROUTER_RUN = 10

    CONNECT_REG = jtag.Dr(8)
    DEVICE_ID = jtag.Dr(32)
    ICEPICK_ID_REG = jtag.Dr(32)
    ROUTER_REG = jtag.Dr(32)
    USERCODE_REG = jtag.Dr(32)

    ROUTER = jtag.Instruction(2, "ROUTER_REG")
    IDCODE = jtag.Instruction(4, "DEVICE_ID")
    ICEPICKCODE = jtag.Instruction(5, "ICEPICK_ID_REG")
    CONNECT = jtag.Instruction(7, "CONNECT_REG")
    USERCODE = jtag.Instruction(8, "USERCODE_REG")

    # Subclasses override: {(Block, index): (idcode_or_None, Tap_subclass)}
    TAPS: dict = {}

    def __init__(self, idcode=None, irlen=None, name=None):
        if name is None:
            name = "TI ICE-Pick"
        super().__init__(idcode=idcode, irlen=irlen, name=name)
        # (Block, index) → currently-enabled child Tap instance.
        self.taps: dict = {}
        self.icepick_id: IcePickId | None = None
        self.user_code: UserCode | None = None
        self.tap_keys: list = []

    @property
    def chain(self) -> "jtag.Chain":
        return self.parent_of_class(jtag.Chain)

    @property
    def iface(self) -> "jtag.JtagInterface":
        return self.chain.parent_of_class(jtag.JtagInterface)

    async def start(self):
        """Connect to the router, read identifiers, list secondary taps."""
        with self.iface.freq_capped("icepick", 1e5):
            self.CONNECT(int(Connect(write_en=True, key="connected")),
                         read_tdo=False, post_dr_run=self.ROUTER_RUN)
            self.IDCODE()
            icepick_id_fut = self.ICEPICKCODE()
            user_code_fut = self.USERCODE()
            icepick_id_tdo = await icepick_id_fut
            user_code_tdo = await user_code_fut

        self.icepick_id = IcePickId(int(icepick_id_tdo))
        self.user_code = UserCode(int(user_code_tdo))

        self.logger.note("ICEPick-ID: %s", self.icepick_id)
        self.logger.note("User Code: %s", self.user_code)

        self.tap_keys = (
            [(Block.TestTap, t) for t in range(self.icepick_id.test_taps)]
            + [(Block.DebugTap, t) for t in range(self.icepick_id.emu_taps)]
        )

        await self.state_dump()

    async def state_dump(self):
        """Log the visibility / selection state of every secondary tap."""
        await self.block_dump(Block.TestTap, SecondaryTestTap,
                              self.icepick_id.test_taps)
        await self.block_dump(Block.DebugTap, SecondaryDebugTap,
                              self.icepick_id.emu_taps)

    async def block_dump(self, block, rtype, count):
        """Read & log every secondary tap of a given block. Returns the
        set of indices whose ``select_tap`` bit was set."""
        if count == 0:
            return set()
        # Issue reads sequentially; each router_read is itself batched
        # internally.
        enabled = set()
        for i in range(count):
            raw = await self.router_read(block, i)
            decoded = rtype(raw)
            self.logger.note("Secondary %s tap %d: %s", block.name, i, decoded)
            if decoded.select_tap:
                enabled.add(i)
        return enabled

    async def router_write(self, block, register, value):
        with self.iface.freq_capped("icepick", 1e5):
            await self.ROUTER(int(Router(write_en=True,
                                         block=int(block),
                                         register=int(register),
                                         value=int(value))),
                              read_tdo=False,
                              post_dr_run=self.ROUTER_RUN)

    async def router_read(self, block, register):
        """Two-shift read: first shift selects ``(block, register)``,
        second shift captures the router's response on TDO while
        re-shifting the same selector."""
        with self.iface.freq_capped("icepick", 1e5):
            selector = int(Router(block=int(block), register=int(register)))
            # Select shift; throw TDO away. ROUTER_RUN idle TCKs let
            # the router latch and prepare the response.
            self.ROUTER(selector, read_tdo=False, post_dr_run=self.ROUTER_RUN)
            # Capture shift; same selector keeps the address pinned
            # while we read the response data.
            tdo = await self.ROUTER(selector, read_tdo=True, post_dr_run=1)
        return Router(int(tdo)).value

    # --- Chain geometry helpers --------------------------------------

    def _insertion_position(self, key):
        """Return ``(ir_pre, dr_pre)`` where a newly-enabled tap with
        the given key should be inserted in the chain.

        Convention (matches crobe): lower keys land closer to TDI
        (higher ``ir_pre``); higher keys land closer to TDO. Concretely,
        the new tap goes just after every currently-enabled sub-TAP
        whose key is lower than ours, counted from the IcePick's
        current ``ir_pre``.

        Detached entries in ``self.taps`` are skipped — they no
        longer contribute to the hardware scan chain.
        """
        ctx = self.chain.context(self)
        ir_delta = 0
        dr_delta = 0
        for k, tap in self.taps.items():
            if k >= key:
                continue
            sub_ctx = self.chain._contexts.get(tap)
            if sub_ctx is None or not sub_ctx.enabled:
                continue
            ir_delta += tap.irlen
            dr_delta += 1
        return ctx.ir_pre - ir_delta, ctx.dr_pre - dr_delta

    def _enabled_tap(self, key):
        """Return the sub-TAP at ``key`` if it's currently in the
        scan chain (enabled), else ``None``. A key whose value lives
        in ``self.taps`` as a detached Tap counts as not-currently-
        enabled."""
        tap = self.taps.get(key)
        if tap is None:
            return None
        ctx = self.chain._contexts.get(tap)
        if ctx is None or not ctx.enabled:
            return None
        return tap

    async def tap_enable(self, block, index, enable=True):
        """Enable / disable a secondary TAP, mutating the parent
        chain.

        Returns the sub-TAP. Calling with the current state is a
        no-op and returns the cached tap (or ``None`` if it never
        existed).

        Disable is non-destructive: the sub-TAP stays in
        ``self.taps`` and in the Node tree but is detached from the
        chain via ``Chain.tap_detach``. A subsequent
        ``tap_enable(...,True)`` reattaches the same object,
        preserving any host-side state (DAP SELECT cache, ROM-table
        children, etc.). This is the basis for IcePick's
        cool-down-and-reuse pattern and for surviving a JTAG TLR
        intact — see :meth:`pre_tlr` / :meth:`post_tlr`.
        """
        key = (block, index)
        if key not in self.TAPS:
            raise KeyError(
                f"{key} is not registered in {type(self).__name__}.TAPS")

        existing = self.taps.get(key)
        currently_enabled = (existing is not None
                             and self.chain.context(existing).enabled)
        if currently_enabled == bool(enable):
            return existing

        cur_raw = await self.router_read(block, index)
        rtype = (SecondaryDebugTap if block == Block.DebugTap
                 else SecondaryTestTap)
        cur = rtype(cur_raw)
        cur.select_tap = enable
        await self.router_write(block, index, int(cur))

        if enable:
            ir_pre, dr_pre = self._insertion_position(key)
            if existing is not None:
                self.logger.trace(
                    "Re-enable TAP %s/%d (%s) at ir_pre=%d dr_pre=%d",
                    block.name, index, existing.name, ir_pre, dr_pre)
                self.chain.tap_reattach(
                    existing, ir_pre, dr_pre, controller=self)
                return existing
            idcode, cls = self.TAPS[key]
            self.logger.trace(
                "Enable TAP %s/%d (%s, irlen=%d) at ir_pre=%d dr_pre=%d",
                block.name, index, cls.__name__, cls.irlen, ir_pre, dr_pre)
            new_tap = self.chain.tap_insert(
                idcode, cls.irlen, ir_pre, dr_pre, base=cls,
                controller=self)
            self.taps[key] = new_tap
            return new_tap

        self.logger.trace(
            "Disable TAP %s/%d (%s)", block.name, index, type(existing).__name__)
        self.chain.tap_detach(existing)
        return existing

    # --- TLR lifecycle -----------------------------------------------

    async def pre_tlr(self):
        """Snapshot the keys of currently-enabled sub-TAPs so
        :meth:`post_tlr` can restore them after the TLR clears the
        router's CONNECT state and detaches every gated sub-TAP."""
        chain = self.chain
        self.tlr_active = [
            key for key, tap in self.taps.items()
            if chain.context(tap).enabled
        ]
        self.logger.trace(
            "pre_tlr: %d sub-TAP(s) to restore", len(self.tlr_active))

    async def post_tlr(self):
        """Re-CONNECT the router and re-enable every sub-TAP that
        was active before the TLR.

        Restoration happens in sorted-key order so that
        :meth:`_insertion_position` reconstructs the same chain
        geometry the user had before the TLR. Each sub-TAP is
        reattached via ``Chain.tap_reattach`` — same object, same
        children, only the JTAG geometry was lost across the
        reset."""
        active = getattr(self, "tlr_active", None)
        if not active:
            return
        self.tlr_active = []

        await self.CONNECT(
            int(Connect(write_en=True, key="connected")),
            read_tdo=False, post_dr_run=self.ROUTER_RUN)

        for key in sorted(active):
            sub_tap = self.taps.get(key)
            if sub_tap is None:
                continue
            block, index = key
            cur_raw = await self.router_read(block, index)
            rtype = (SecondaryDebugTap if block == Block.DebugTap
                     else SecondaryTestTap)
            cur = rtype(cur_raw)
            cur.select_tap = True
            await self.router_write(block, index, int(cur))

            ir_pre, dr_pre = self._insertion_position(key)
            self.chain.tap_reattach(
                sub_tap, ir_pre, dr_pre, controller=self)
            self.logger.note(
                "Restored gated TAP %s/%d at ir_pre=%d after TLR",
                block.name, index, ir_pre)
