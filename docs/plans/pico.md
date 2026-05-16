# PLAN — Raspberry Pi RP-series follow-ups

Deferred work on the RP2040 / RP2350 stack. Items here are
gated on either hardware availability or work that's intentionally
scheduled in another session.

## SWD-side RP2040 target

The PICOBOOT path gives flash programming but no `Debuggable`,
so `acrobe debug`, `acrobe gdb-server`, and run-control don't
work today.

Plan: a sibling target file `acrobe/target/arm/rp2040_swd.py`
that does `@Target.register(Dp, precedence=...)` against an
RP2040 reached via SWD (Proby, ST-Link, J-Link, etc.). Re-use
the existing `Rp2040Target` class for shared shape (puppet
ownership, SPI child spawn). Differences:

- **No `PicobootXipFlash`.** Flash programming via SWD requires
  an on-target stub (driving SSI through the Mem-AP would be
  thousands of writes per page); the SpiFlash component over
  the puppet's SPI passthrough is the natural choice. The same
  SPI stub bytes we already ship would do the work.
- **`CortexMDebuggable`** built from the chip's ROM table /
  SCS. RP2040 has two M0+ cores reachable through DP[0]/DP[1];
  the existing `CortexMDebuggable.from_romtable` factory should
  produce both. `info cpu` and `gdb-server` follow naturally.
- **Dual-core surfacing.** RP2040 exposes core0 and core1 via
  separate APs (1 and 2). Both should appear as `Core` children
  of the Debuggable. The `CortexMDebuggable` already supports
  N-core targets (nRF53 was the template).
- **Reset-on-program.** Without PICOBOOT's REBOOT, post-program
  reset is via AIRCR.SYSRESETREQ through the SCS. `CortexMTarget`
  already handles this.

Status: **gated on hardware availability** (board with SWD).

## RP2350

New target file `acrobe/target/arm/rp2350.py`. Key differences
from RP2040:

- USB PID is `0x000f` (vs `0x0003`).
- PICOBOOT v2 protocol: adds `PC_GET_INFO`, `PC_OTP_READ`,
  `PC_OTP_WRITE`, `PC_EXEC2` with the existing 32-byte command
  framing. The current `PicobootUsbTransport` extends naturally.
- 520 KiB SRAM, dual Cortex-M33 + dual Hazard3 RISC-V. The
  `PicobootPuppet` trampoline is fine for M33 (Thumb2 still
  understands Thumb1), but a RISC-V puppet variant would need
  its own trampoline.
- TrustZone-NS / signed boot / partition table semantics —
  optional, the bootrom hides most of this for unsigned dev
  workflows.
- The flash-programming and SPI passthrough stubs may need
  re-compilation against the M33 SDK (different ROM helper
  addresses).

Status: **gated on hardware availability** (RP2350 board).

## OTP / partition tables

RP2040 has 4 KiB of one-time programmable fuses; RP2350 has
8 KiB with permissions and signed-boot anchors. Picotool
exposes both.

Plan: an `Otp` region under the Loadable (with `NotUpdatable`
override unless the user explicitly opts in via a flag).
PICOBOOT v2 has `OTP_READ`/`OTP_WRITE`; on RP2040 it's reachable
via the bootrom OTP helper functions, called through a puppet
stub.

Partition tables (RP2350 specifically) are a partition-aware
Loadable layer — multiple programmable regions per chip, each
with its own size and protection. Probably belongs on top of
the existing Loadable rather than replacing it.

Status: **gated on demand** — the dev-board reflashing flow
doesn't need OTP. Defer until someone asks.

## Pico reboot protocol (host-side request to enter BOOTSEL)

`pico-sdk`'s `pico_stdio_usb` template exposes a USB "reset"
interface (class 0xFF, subclass 0, protocol 1) carrying two
control requests: `RESET_REQUEST_BOOTSEL` (0x01) and
`RESET_REQUEST_FLASH` (0x02). picotool's `reboot -f -u` uses
this to kick a running firmware back into BOOTSEL mode without
a physical button.

The protocol may be present on any USB device built with
pico-sdk, including ones with custom VID/PID — so an adapter
matching on Raspberry Pi VID 0x2e8a is the wrong shape. See
the "thoughts" section below for the CLI design.

Status: design in progress.

## UF2 family-ID filtering at load time

UF2 files can bundle blocks for multiple chip families
(`UF2_FLAG_FAMILY_ID_PRESENT` with the `fileSize` field
carrying the family ID). A multi-family file fed to `chip
program` against an RP2040 target currently accepts every
block — including RP2350 blocks that would corrupt the flash
view.

Fix: at the parser → MemoryMap conversion step (or in the
Loadable), filter blocks to the target's family ID. The target
needs a way to declare its family (e.g. `Rp2040Target.family_id
= UF2_FAMILY_RP2040`). When the Loadable consumes a UF2's
MemoryMap, it asks the parser for blocks matching its family.

Status: small task, no hardware dependency. Land when a
multi-family file shows up as a regression.

## Wedge recovery diagnostics

When a puppet stub hangs (rare since the trampoline fixes, but
not impossible for buggy custom stubs), the bootrom stops
responding on every endpoint, including control. The user sees
a generic `PicobootError: TransferTimeout`. Surfacing this as
"chip appears wedged; physical replug or BOOTSEL button needed"
would save debugging time.

Status: cosmetic. One-line message improvement.

## THUNK_CODE byte-order convention

The trampoline bytes live as hex literals in
`PicobootPuppet.THUNK_CODE`. A byte-swap typo on one
instruction wasted a session's worth of cycles before being
spotted. Moving the trampoline to an assembler-verified `.bin`
loaded at module load time, alongside the `.s` source, would
make this class of bug impossible.

Status: cleanup. Worth doing alongside the SWD work since
that'll add another trampoline.
