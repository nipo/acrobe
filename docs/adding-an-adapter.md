# Adding a new protocol adapter

This document helps adding a new adapter to acrobe.

A typical adapter is a USB device that exposes one or more wire
protocols — JTAG, SWD, I²C, SPI, UART, … — and that acrobe drives
through a vendor-defined transport. Some adapters are network
endpoints (Xilinx Virtual Cable servers, Altera `jtagd` brokers);
some are not adapters in the classical sense at all (an MCU's own
ROM bootloader, exposed as a USB device, like RP2040 PICOBOOT).

Whatever the underlying device, the integration boundary is the
same: register an entry that lets acrobe *discover* the device,
provide an `Adapter` subclass that knows how to *open* and *close*
it, and expose the per-protocol *interfaces* as children of that
`Adapter`. The rest of acrobe — `Chain`/`Tap` for JTAG, `SwDp`/`Ap`
for SWD, target classes for chips — plugs onto those interfaces
without caring which hardware sits underneath.

This guide walks through what each of those steps looks like in
practice, points at the existing adapters that are the cleanest
references for each pattern, and calls out the helpers worth
reusing.

## 1. Discovery and registration

### USB adapters

USB-attached adapters register against `adapter_db`
(`acrobe/adapter/model.py`). The registry stores one
`AdapterInfo` per supported variant; the `UsbEnumerator` walks
the bus and matches descriptors against the registered infos.

```python
from acrobe.adapter.model import (
    Adapter, AdapterInfo, adapter_db, make_adapter_name,
)

@adapter_db.register(AdapterInfo("myadp",     vid=0x1234, pid=0x0001))
@adapter_db.register(AdapterInfo("myadp-pro", vid=0x1234, pid=0x0002))
class MyAdpAdapter(Adapter):
    supported_interfaces = ["jtag", "swd"]

    def __init__(self, name, info, device, transport):
        super().__init__(name)
        ...

    @classmethod
    async def open(cls, descriptor) -> "MyAdpAdapter":
        device = descriptor.open()
        info = next(i for i in _MYADP_INFOS
                    if i.vid == descriptor.vendor_id
                    and i.pid == descriptor.product_id)
        name = make_adapter_name(info, device.serial)
        ...
        return cls(name, info, device, transport)

    async def child_spawn(self, name):
        ...

    async def close(self):
        ...
```

What each piece does:

* **`AdapterInfo(name, vid=, pid=, manufacturer=, product=)`** —
  pre-filter for the USB descriptor. `vid`/`pid` are always
  cheap (they live in the descriptor). `manufacturer`/`product`
  require opening the device to read the string descriptors and
  will silently drop devices that refuse to open. Use the
  string filters only when VID/PID alone are ambiguous (FTDI is
  the typical case: same VID/PID across hundreds of boards;
  match by manufacturer string to scope to the right vendor).

* **`adapter_db.register(info)`** decorator — multiple `AdapterInfo`
  entries can map to the same `Adapter` subclass. Decorator can be
  hooked multiple times if necessary.

* **`supported_interfaces`** — a list of strings advertised by
  `acrobe info adapters`. It does not drive dispatch (that's
  `child_spawn`); it's purely informational.

* **`@classmethod open(descriptor)`** — called by the
  enumerator after a descriptor matches. The factory must
  return a fully-initialised `Adapter` instance ready to spawn
  interface children. Anything that needs a live USB handle
  (reading firmware version, capabilities, mode probes) goes
  here; raising will cause the enumerator to skip this
  candidate.

* **`Adapter.check(device)`** — optional classmethod, called
  with an opened device handle before `open()`. Return `False`
  to reject a candidate at runtime (e.g. a board that shares an
  FTDI descriptor with something else but advertises a
  different EEPROM string).

* **`Adapter.serial_mangle(serial)`** — normalises the raw USB
  serial string. Used when the device packs extra metadata into
  the serial field (the Proby ships its serial as
  `"vendor;model;NN"` and uses `serial_mangle` to keep only the
  numeric suffix).

The component name visible in CLI paths and logging follows the
`make_adapter_name(info, serial)` convention:
`<info.name>-<serial>`, lower-cased. This is what `child_summon`
matches against substring-wise.

For module loading, just `from . import myadp` (or
`from .myadp import adapter`) under
`acrobe/adapter/__init__.py`. The auto-loader does not scan
packages; adapter modules must be imported at least once for
their `adapter_db.register` calls to fire.

### Non-USB hardware

The discovery system is built around enumerators
(`HwRoot.add_enumerator`). The shipped set covers more than USB:

* **TTY** — `acrobe/adapter/tty.py`. Picks up `/dev/cu.*` and
  `/dev/ttyUSB*`. Used for serial-only adapters.
* **AJI** — `acrobe/adapter/aji/`. Speaks Altera's
  `jtagd`/`jtagserver` protocol. Paths like `aji/<host>/...`
  reach a remote Quartus daemon; the AJI side exposes
  cables / chains / TAPs back to acrobe as normal Nodes.
* **XVC** — `acrobe/adapter/xvc/`. Xilinx Virtual Cable, the
  bit-bang JTAG protocol over TCP. Paths like
  `xvc/<host>[:port]/<chain>/...`.
* **Wire** — `acrobe/wire/enumerator.py`. Acrobe's own
  client/server transport for exposing arbitrary parts of a
  remote acrobe tree. See `acrobe/wire/` and `PLAN_wire.md` —
  out of scope for this document.

If the new adapter is one of these network protocols and there
is no enumerator for it, you write a new enumerator. The
`UsbEnumerator` is the reference shape: a class with
`async spawn(name)` (returns an `Adapter` instance or raises
`NoMatch`) and optionally `async scan()` (returns a listing for
`acrobe info adapters`). Wire it into `make_hw_root()`
(`acrobe/adapter/model.py`) alongside the others.

In most cases though, network-based gateways do not need a new
enumerator family — the AJI and XVC enumerators already
demonstrate the pattern of "a host:port endpoint that yields a
tree" and a new one of that flavour just hooks in beside them.

## 2. Per-protocol interface children

Adapters do not implement protocols directly; they hand out
*interface* children via `child_spawn`. Each interface subclasses the
abstract base for its protocol and registers as a Node child of the
adapter under a conventional name (`"jtag"`, `"swd"`, `"spi"`,
`"i2c"`, …). Name is usually a bare protocol name, all lowercase. In
case the adapter has multiple physical interfaces of the same type, it
may have multiple suffixes `"i2c-ext"`, `"i2c-int"` for instance.

Names that can be passed to `child_spawn` should be repeated in
`supported_interfaces` class member. They are a hint to users.

```python
async def child_spawn(self, name):
    if name == "jtag":
        from .jtag import MyAdpJtag
        return MyAdpJtag(self._transport, name="jtag")
    if name == "swd":
        from .swd import MyAdpSwd
        return MyAdpSwd(self._transport, name="swd")
    raise NoMatch("interface", name)
```

Adapter is responsible for not accepting to spawn mutually exlusive
protocol interface children.

The interface base classes are:

* `acrobe.protocol.jtag.JtagInterface` — bit-level JTAG. Sees
  `Shift`, `Reset`, `Run`, `CaptureDr`, `CaptureIr`,
  `SwdToJtag` ops. Owns the TAP FSM bookkeeping in the
  adapter-side state machine; the `Chain`/`Tap` layer above
  posts `_TapShift`/`_TapRun`/`_TapIrStatus` envelopes into a
  `Chain`, which Chain then lowers into the bit-level ops the
  interface consumes.
* `acrobe.protocol.swd.Interface` — SWD wire. Sees `Read`,
  `Write`, `Run`, `Wakeup`, `LineReset`, `JtagToSwd`. AP read
  pipelining (data lands on the *next* packet) is the
  interface's responsibility; callers post a `Read(ap=True)`
  and the resolved future already carries the real data.
  `Interface.start()` itself drives wire bring-up (line reset,
  JTAG-to-SWD switch, DPIDR read) and parents a typed `Dp`
  child at `"dp"` via `Interface.db` (keyed on DPIDR, REVISION
  masked). Adapter subclasses that need their own pre-bring-up
  setup (mode select on the firmware side, default clock, …)
  override `start` and call `await super().start()` at the end
  so the wire init runs once the adapter is ready.
* `acrobe.protocol.spi.Interface`, `acrobe.protocol.i2c.Interface`,
  `acrobe.protocol.serial.Interface` — same shape for the
  byte-stream protocols.

All of them mix `Batcher`, `FreqCapper`, and `Node`. Concrete
subclasses must implement `async flush_ops(batch)`; the
`Batcher` machinery takes care of the rest.

A new interface subclass for an existing protocol almost always
boils down to:

1. Translate each op in `batch` into the device's command
   format.
2. Issue the device transaction(s).
3. Resolve every batch future with its natural result value
   (`BitString` for a reading shift, `int` for an SWD read,
   `None` for everything else).

Reference implementations to copy from:

* Firmware does the SWD wire — `cmsisdap/swd.py`, `jlink/swd.py`,
  `stlink/dp.py`. These don't see SWD bits at all; the firmware
  packetises and they speak DP/AP transactions directly.
* Acrobe does the wire — `ftdi/swd.py`, `ftdi/jtag.py`. Bit-level
  ops get lowered to MPSSE primitives.
* Bit-bang JTAG behind a "give me an array of TMS/TDI, get TDO
  back" command — `jlink/jtag.py`. Single USB transaction per
  flushed batch.

## 3. Batching and pipelining

Acrobe relies on batching at every layer to keep round-trip
counts low. The mechanism is `acrobe.engine.Batcher`
(`acrobe/engine.py`):

* `post(op)` synchronously enqueues `op`, returns a Future,
  schedules a flush task if one is not pending.
* `post_no_wait(op)` enqueues without allocating a Future
  (use when an upstream Future is anchored on a different op in
  the same batch and resolution is driven by a single callback).
* `flush_ops(batch)` — the only method a subclass must
  implement. Receives a list of `(op, future)` tuples (futures
  may be `None` for `post_no_wait` entries) and is responsible
  for resolving every non-None Future.

### Pipelining batches across layers

Each layer (`JtagInterface`, `JtagMpsse`'s `MpsseEngine`,
`FtdiTransport`) is its own `Batcher`. The pattern when a layer
sits on top of another is:

1. Walk the incoming batch, translate each op into one or more
   lower-layer ops, `post` them on the lower layer.
2. Either:
   * `await` the lower futures and resolve the upper futures
     from them, **or**
   * skip the per-op `await` and attach a single
     `add_done_callback` to the *last* lower op's Future — the
     callback resolves every upper Future in one pass. Use this
     when the per-op result is recoverable from the op object
     itself (e.g. MPSSE shifts populate `op.data` via
     `rsp_handle`). It saves thousands of futures per batch.

`acrobe/adapter/ftdi/jtag.py` is the canonical example of the
single-callback pattern: `JtagMpsse.flush_ops` posts all but
the last MPSSE op via `post_no_wait`, anchors one
`add_done_callback` on the last, and the callback walks
`mpsse_ops[start:end]` to assemble each shift's `.data` into a
`BitString` result.

### What batches in practice

The Batcher collects everything posted between yield points in
the asyncio loop. Upper layers (Chain → JtagInterface → MPSSE →
USB) all coalesce, so a single user-facing call that fires
thousands of bits of JTAG traffic lands as one USB write. The
new code should:

* Avoid `await`ing inside an op-translation loop — that
  defeats coalescing.
* Avoid one-op-per-future patterns when the responses can be
  decoded en bloc.
* Trust the lower layer's batching; do not pre-buffer or
  pre-merge ops at the upper layer "for performance".

## 4. Reusing transactors and engines

Several adapters use the same transactor or core IP, and the
existing codebase factors that out. Pick the closest match
before writing a new adapter from scratch.

### FTDI MPSSE

`acrobe/adapter/ftdi/` factors MPSSE into three layers:

* `mpsse.py::MpsseEngine` — `Batcher` over the FTDI bulk
  transport. Serialises a batch of MPSSE `Operation`s into a
  single bytearray, issues one USB write + one read, dispatches
  per-op response bytes back via `Operation.rsp_handle`.
* `jtag.py::JtagMpsse` — implements `JtagInterface` over an
  `MpsseEngine`. JTAG FSM, idle TCK packing, frequency
  divisor selection, TDO bit reassembly.
* `swd.py::FtdiSwd` — implements `swd.Interface` over an
  `MpsseEngine`. Handles SWD framing and the OE-pin buffer
  flips a bidirectional SWDIO line needs.
* `jtag_adapter.py::FtdiJtagAdapter` — generic single-channel
  MPSSE JTAG adapter. Override class attributes
  (`_adapter_info`, `_channel`, `_gpio_oe`, `_gpio_val`,
  optional `_led`) and you have a working JTAG adapter in
  under ten lines.

The shortest possible adapter in the tree (`digilent.py`,
`trenz.py`, `altera.py`, `icepizero.py`, `sipeed.py`) are all
two-attribute subclasses of `FtdiJtagAdapter`. New FTDI-based
JTAG boards should go through this base class — never
re-instantiate `MpsseEngine`/`JtagMpsse` from scratch.

For non-JTAG MPSSE work (SPI, I²C, GPIO), reuse `MpsseEngine`
directly and write a new interface alongside `JtagMpsse` rather
than under `FtdiJtagAdapter`.

### FTDI MPSSE Activity LED

`ftdi/activity.py::ActivityLed` produces "bracket" MPSSE byte
sequences that `MpsseEngine.set_bracket(pre, post)` prepends
and appends to every batch. Pulses an LED for the duration of
each batch, with no per-op cost. If a board exposes an activity
indicator on a GPIO, wire it via `_led = ActivityLed(pin=N, …)`
in the adapter subclass — `FtdiJtagAdapter.open` picks it up.

### NSL transactors and FPGA-loaded firmware

The Proby (`acrobe/adapter/proby/`) is an FT2232H with a companion
FPGA. Its two channels carry, respectively, a JTAG master (channel B,
talking the chain that programs the FPGA) and a direct connection to
FPGA (channel A, exposing all pins from channel A). After programming
FPGA through channel B, channel A can either implement protocol with
MPSSE using FPGA as a I/O mux gate, or be reconfigured as FT245 Sync
fifo and convey a bidirectional command stream that will be
interpreted by FPGA. The relevant patterns from `proby` are:

* Open the secondary channel lazily — `child_spawn` of
  `jtag-pt` reprograms the FPGA, then opens channel A and
  attaches a fresh `JtagMpsse`.
* Reuse `FtdiTransport` + `MpsseEngine` + `JtagMpsse` on the
  loaded channel; the fact that there is custom logic on the
  other side is transparent to the higher layers.
* Cache the loaded mode (`_loaded_mode`) so a second spawn
  with the same mode skips the bitstream load.

When adding an adapter whose hardware is "an FTDI plus an FPGA
that exposes some other protocol", model it on Proby: keep the
FTDI side standard, ship the bitstream under `<adapter>/fw/`,
program it from `open` or lazily from `child_spawn`, and expose
whatever the FPGA implements as one more interface child.

### Vendor command-set firmware

When the firmware exposes high-level commands rather than a
bit-bang surface — CMSIS-DAP, ST-Link, J-Link, XDS110 — the
adapter implements a *transport* and one or more *interface*
subclasses that translate protocol ops directly into vendor
commands. Reference layout:

```
acrobe/adapter/<name>/
    adapter.py       Adapter subclass + AdapterInfo + register
    transport.py     USB/HID bulk transport
    protocol.py      command opcodes, capability constants
    jtag.py          Optional: JtagInterface subclass
    swd.py           Optional: swd.Interface subclass
    dp.py            Optional: high-level DP/AP shortcut
                     (ST-Link, where the firmware skips the
                     SWD wire entirely)
```

`cmsisdap/` and `jlink/` are the cleanest examples; `stlink/`
shows the variant where the firmware exposes DP/AP transactions
above the SWD wire (no `swd.Interface` at all, just a
direct-DP child returned from `child_spawn`).

## 5. Lifecycle and resources

`Adapter.close()` must release every resource the adapter
allocated. The canonical pattern is:

```python
async def close(self):
    await self._transport.close()
    self._device.handle.close()
```

For transports that hold long-lived state (USB endpoints,
sockets, threads), register `on_shutdown(self.close)` at
construction and `cancel_shutdown(self.close)` at the start of
`close()` so the process-wide lifecycle (`acrobe/lifecycle.py`)
will drain them on abrupt exit. The CLI's `result_callback`
calls `acrobe.shutdown()` automatically; library users do it
explicitly.

When the adapter claims a vendor-side connection slot
(J-Link's `CMD_REGISTER`, AJI's session handle, …) release it
in `close()` *before* tearing the USB down. Failing to do so
strands the slot until firmware times it out.

## 6. Checklist

Before declaring a new adapter "done":

* [ ] `AdapterInfo` registered in `adapter_db`.
* [ ] Module imported from `acrobe/adapter/__init__.py`.
* [ ] `supported_interfaces` accurate.
* [ ] `acrobe info adapters` lists the device with the
      expected name, serial, and capability summary.
* [ ] `child_spawn` returns a working interface for every
      advertised protocol, or `raises NoMatch` cleanly.
* [ ] Interface `flush_ops` resolves every batch Future,
      including the failure path (USB error, ACK=FAULT, …).
* [ ] `close()` releases USB handles, claimed interfaces, and
      any vendor session slot.
* [ ] An end-to-end smoke test: `acrobe chain enumerate -r
      <adapter>/jtag/chain` (or the equivalent SWD/SPI command)
      finishes without errors against a known target.
