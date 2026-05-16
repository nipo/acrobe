# PLAN — iTap probe support

iTap probe is an MCU-based USB debug probe. One USB device exposes a
CDC ACM (UART side) **and** a vendor interface (bulk endpoint pair)
that carries NSL transactor command streams for JTAG, SWD or I2C
depending on a mode set via a control request. The vendor interface
is a plain byte-stream — no framing, the host knows the response
size for each queued command.

Goal of this PLAN: implement iTap support in acrobe (as a
plugin), and along the way fix the layering that crept out of line
during the synchronous→asyncio port.

## Layering target

```
acrobe.protocol.pipe         — abstract Pipe (byte stream)
acrobe.protocol.datagram     — abstract Datagram (framed + routing context)

  ↑ implemented by
acrobe.component.nsl.bnoc.framed.Framed   — concrete Datagram impl
                                              (9-bit JTAG FIFO framing)
acrobe_plugin.itap.pipe.UsbBulkPipe        — concrete Pipe impl
                                              (USB bulk EP pair; short
                                              USB packets mark frame
                                              boundary on the cable but
                                              acrobe-side semantics stay
                                              stream — transactors know
                                              expected response size)

acrobe.component.nsl.transactor.<proto>
  → pure batch codec classes (no transport, no Batcher)
  → JtagTransactor, SwdTransactor, I2cTransactor
  → stateful (dirty-divisor, dirty-turnaround) but transport-free
  → exposes:
      encode(batch) -> (cmd_bytes, response_size, state)
      decode(batch, response_bytes, state) -> None  # resolves futures

acrobe.protocol.{jtag,swd,i2c}.Interface
  ↑ subclassed by
acrobe_plugin.itap.{jtag,swd,i2c}.Itap*Interface
  → the Batcher; owns a transactor codec and a transport (Pipe or
    Datagram instance)
  → flush_ops: transactor.encode(batch) → (cmd, rsp_size, state),
                await pipe.write(cmd), rsp = await pipe.read(rsp_size),
                transactor.decode(batch, rsp, state)
```

## Pipe / Datagram shape (op dataclasses + futures)

Both base classes mirror the existing `acrobe.protocol.swd.Interface`
/ `acrobe.protocol.jtag.JtagInterface` pattern: a `Batcher + Node` base
class, op dataclasses, `post(op) → Future`, `flush_ops(batch)` to be
implemented by concrete subclasses.

```python
# acrobe.protocol.pipe

@dataclass(frozen=True, slots=True)
class Write:
    data: bytes

@dataclass(frozen=True, slots=True)
class Read:
    size: int

class Pipe(Batcher, Node):
    db = Db("Pipe handler")
    def write(self, data) -> Future[None]: ...
    def read(self, size) -> Future[bytes]: ...
    async def flush_ops(self, batch): raise NotImplementedError
```

```python
# acrobe.protocol.datagram

@dataclass(frozen=True, slots=True)
class Send:
    data: bytes
    context: Any = None

@dataclass(frozen=True, slots=True)
class Recv:
    context: Any = None
    # Future resolves to (data, recv_context); concrete impls with no
    # context return (data, None).

class Datagram(Batcher, Node):
    db = Db("Datagram handler")
    def send(self, data, context=None) -> Future[None]: ...
    def recv(self, context=None) -> Future[tuple[bytes, Any]]: ...
    async def flush_ops(self, batch): raise NotImplementedError
```

Call-site syntax is unchanged: `data = await pipe.read(n)` works
because awaiting a Future yields its value.

## Phases

### Phase 1 — protocol layer cleanup

1. Rewrite `acrobe/protocol/pipe.py`: `Pipe(Batcher, Node)` with `Read`
   / `Write` op dataclasses. Update `TelnetPipe` and rfc2217
   `_StreamPipe` to implement via `flush_ops`. Adjust
   `tests/test_telnet.py` and `tests/test_rfc2217.py`.
2. Rewrite `acrobe/protocol/datagram.py`: `Datagram(Batcher, Node)`
   with `Send` / `Recv` op dataclasses carrying `context`.
3. Refactor `acrobe/component/nsl/bnoc/framed.py`: make `Framed` a
   `Datagram` subclass. Keep the 9-bit FIFO encoding/decoding logic
   in `JtagFramed`. `bnoc.routed.Router` rebases on `Datagram.context`
   rather than its own `Context` dataclass (or keep its dataclass as
   a typed context value — TBD during impl).
4. Verify existing tests pass: `pytest -x` for the affected areas.

### Phase 2 — NSL transactor codecs

In `acrobe/component/nsl/transactor/`, add `jtag.py`, `swd.py`,
`i2c.py` — each exposing a pure batch-codec class. No `Batcher`, no
transport reference, no abstract transport methods. The codec is
fed a batch, returns the wire bytes + the response size it expects;
later it is fed those response bytes and resolves the futures.
Whoever owns the transport (the adapter-side `Interface`) glues the
two halves.

Common shape:

```python
class JtagTransactor:
    def __init__(self, base_freq, *, max_chunk=1024): ...
    def freq_update(self, freq) -> float: ...
    def encode(self, batch) -> tuple[bytes, int, State]: ...
      # → (cmd_bytes, response_size, opaque_state)
    def decode(self, batch, response: bytes, state) -> None: ...
      # → resolves each (op, future) in the batch
```

`SwdTransactor.__init__` takes `divisor_width=1|2` to support both
the iTap 1-byte form (`half_cycle_us = trn->data + 1`) and the older
2-byte form. Default is `divisor_width=1` (iTap-native).

Unit tests under `tests/test_nsl_*.py` against firmware-doc examples
(known op → expected wire bytes; known response bytes → expected
results).

Existing `acrobe/component/nsl/transactor/spi.py` stays untouched in
this work; it predates the Target framework and will be reworked
later.

### Phase 3 — `acrobe_plugin.itap` plugin

Layout (in `/Users/nipo/work/hardware/itap_probe/acrobe_plugin/`):

```
setup.py                            # name "itap_acrobe"
acrobe_plugin/                       # PEP-420 namespace (no __init__.py)
  itap/
    __init__.py                      # imports submodules so registration fires
    adapter.py                       # ItapAdapter: USB, vendor reqs,
                                     #              mode mgmt, vio control
    pipe.py                          # UsbBulkPipe(Pipe)
    jtag.py                          # ItapJtagInterface(jtag.JtagInterface)
    swd.py                           # ItapSwdInterface(swd.Interface)
    i2c.py                           # ItapI2cInterface(i2c.Interface)
```

`itap/__init__.py` imports `adapter`, `jtag`, `swd`, `i2c` so the
`adapter_db.register(...)` calls run at plugin-load time.

Adapter recognises USB VID:PID `0x1500:0x5e02` (matches the firmware
config). On `open()` it:

- claims the vendor interface, locates the bulk EP pair;
- reads build-id / board-name / build-date / current vio / current
  mode via control transfers, for logging;
- defers `mode_set` until `child_spawn` knows which interface the
  user wants.

`child_spawn("swd"|"jtag"|"i2c")` constructs the matching
`Itap*Interface`. The vio options live on that interface (see Phase
4); when the interface `start()`s, it:

- applies its pending vio settings by calling back into the adapter
  (`adapter.vref_set(...)` etc.);
- issues `mode_set` on the adapter for the protocol it represents;
- constructs `UsbBulkPipe` over the EP pair as its transport.

Closing the adapter sets mode back to `UART_NONE` and releases the
USB handle, via `on_shutdown(self.close)`.

### Phase 4 — options

VIO settings live on the protocol **interface** (not the adapter),
matching crobe convention — a future plugin's interface that isn't
voltage-selectable simply won't accept these keys.

`option_set` overrides on `Itap*Interface` recognise:

- `vtrack=1` (default) — follow VCC pin as VIO reference.
- `vref=<volts>` — force VIO, do not supply target VCC.
- `vsupply=<volts>` — force VIO and supply target VCC.

`vref` and `vsupply` are mutually exclusive; the last one wins.
Applied during `start()`, before `mode_set` so the level shifters are
already at the requested level when the protocol pins go live.

`fmax=<hz>` on interfaces is already handled by `FreqCapper`.

Path examples:

- `acrobe -r itap-XYZ/swd` — default tracking VIO, SWD mode.
- `acrobe -r 'itap-XYZ/swd(vsupply=3.3)'` — supply 3.3 V to target.
- `acrobe -r 'itap-XYZ/jtag(vref=1.8)'` — VIO 1.8 V, no supply.

### Phase 5 — smoke tests against real hardware

User has an LPC43S67 wired to the iTap probe (undriven but enumerates).

1. `acrobe info adapters` lists the iTap with mangled-serial name.
   Logs include build-id / board-name / VIO state.
2. `acrobe info enumerate -r itap/swd` brings up SWD, expects to see
   the LPC43S67's SW-DP IDCODE in the discovery output.
3. `acrobe info enumerate -r itap/jtag` switches to JTAG, expects to
   see the chip's TAP IDCODE(s) discovered through the JTAG chain.

If 2 or 3 works, the protocol stack is end-to-end. If only 2 works,
investigate the JTAG transactor encoding (the readme is known to
lag the firmware on details — firmware is authoritative).

## SWD wire-format reminders (from firmware)

- `CMD_RUN = 0b00xx_xxxx`, count = bits[5:0] + 1.
- `CMD_RUN_DIO = 0b01xx_xxxx`, count = bits[5:0] + 1 (SWDIO high
  during cycles — used for Wakeup / LineReset).
- `CMD_RW = 0b10xx_xxxx`, AP = bit[5], R = bit[4], reg = bits[3:0]
  (4-bit address; existing crobe encoder only uses bits[1:0], which
  is fine — upper bits are unused on the wire).
- `CMD_MGMT = 0b11xx_xxxx`. Subcommands:
  - `CMD_TURNAROUND = 0b1101_00xx`, count = bits[1:0] + 1.
  - `CMD_BITBANG = 0b111x_xxxx`, count = bits[4:0] + 1, **4 bytes
    payload always** (firmware reads them then sends `count` bits
    LSB-first).
  - `CMD_RESET = 0b1101_100x`, assert = bit[0].
  - `CMD_ABORT = 0b1100_0000`.
  - `CMD_DIVISOR = 0b1100_0001`, **1 byte payload** for iTap firmware
    (firmware does `half_cycle_us = trn->data + 1`).
- Response: `0b0000_pacw` where `p` is parity error and `acw` is
  ACK[2:0]. For Read, a 4-byte LSB-first payload follows the
  response byte even on error.

## Open items deferred

- `acrobe.component.nsl.transactor.spi.py` rework.
- Routed context (`bnoc.routed.Router`) reconciliation with the new
  `Datagram.context` — settle during phase 1 step 3.
- Other iTap modes (UART_FULL, UART_DTR, UART_IO_PTRACE) — not needed
  for debug probe use; out of scope here.
