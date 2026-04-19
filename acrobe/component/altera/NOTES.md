# Altera/Intel FPGA Programming — Reverse Engineering Notes

These notes are derived from analysis of STAPL (JAM) transpiled output,
SVF files, openFPGALoader source, and live hardware experiments with
Cyclone 10 LP and Agilex 5 devices.

## STAPL INTEGER Array Init Order

**JESD71 specifies that INTEGER array init values are listed from
highest index to lowest**:

    INTEGER A[n] = v(n-1), v(n-2), ..., v(1), v(0);

This means the FIRST listed value goes to `A[n-1]` and the LAST
listed value goes to `A[0]`.  Our parser and interpreter reverse the
init list before storing.

Impact: all multi-element INTEGER arrays in the STAPL have reversed
storage order compared to naive parsing.  Size-1 arrays and BOOLEAN
arrays are unaffected.

**Affected arrays**: A0, A1, A5, A6, A7, A8 (device type lookup
tables, 46 entries each), A61 (IDCODE list), A148 (flash database,
2015 entries), and any multi-element INTEGER arrays in procedures.

**Not affected**: size-1 arrays (A12, A13, A25, A43, A59, A60,
A147, A217, J0, J1, J4, J5, J19, J20), BOOLEAN arrays (J2, J77,
J80, J81 — all VIR/VDR command/response data), and constants
derived from SVF or live hardware.

All SDM driver constants (IR codes, sync signatures, VIR/VDR command
words, CONF_DONE positions) were derived from size-1 INTEGER arrays,
BOOLEAN arrays, SVF analysis, or live hardware — they are correct.


## JTAG Chain Framework (ALG_VERSION 68)

All Altera devices share the same generic JTAG chain management
framework. The STAPL algorithm version 68 implements:

- Multi-device chain discovery with bypass padding
- Capability-driven dispatch (A13 bitmask per device)
- IR assembly for multi-device chains (_proc_l458)
- IDCODE verification against expected values (_proc_l108)
- Pre/post configuration JTAG sequences (_proc_l39)
- Cleanup and finalization (_proc_l93)

### Device Capability Bits (A13)

A13 is a size-1 per-device array (not affected by init reversal).

| Bit | Mask     | Name              | Meaning                                      |
|-----|----------|-------------------|----------------------------------------------|
| 2   | 0x004    | DEVICE_ACTIVE     | Master enable, checked with every other bit   |
| 5   | 0x020    | HAS_SRAM_CONFIG   | Modern SRAM configuration (Cyclone, Stratix)  |
| 6   | 0x040    | HAS_NCONFIG       | Has NCONFIG pulse capability                  |
| 7   | 0x080    | HAS_HALT_CC       | Has halt-on-chip-CC feature                   |
| 15  | 0x8000   | HAS_FLASH_PROG    | Flash programming via JTAG bridge (Agilex)    |

Bits 0, 1, 8, 9, 11, 14, 17 are for older ISP/flash-based devices
(MAX, older Cyclone) and are not relevant for Cyclone 10 LP or
Agilex 5.

### IR Instructions (10-bit, shared across families)

These values are from BOOLEAN arrays and SVF — not affected by
init reversal.

| IR    | Name              | Used for                                    |
|-------|-------------------|---------------------------------------------|
| 0x002 | CONFIG            | SRAM config data shift / bitstream streaming |
| 0x003 | STARTUP           | Initialize device after config               |
| 0x004 | CHECK_STATUS      | Read config status register                  |
| 0x006 | IDCODE            | Read JTAG IDCODE                             |
| 0x007 | USERCODE          | Read user code register                      |
| 0x00D | HALT_ON_CHIP_CC   | Debug: halt on chip CC                       |
| 0x071 | ISC_ENABLE        | Enter ISC mode (not for SRAM-only devices)   |
| 0x079 | ISC_DISABLE       | Exit ISC mode                                |
| 0x201 | VIR               | Virtual IR — SDM command channel             |
| 0x202 | VDR               | Virtual DR — SDM data channel                |
| 0x208 | CONFIG_STATUS_VJ  | Config status via virtual JTAG               |
| 0x281 | PULSE_NCONFIG     | Pulse NCONFIG (reset configuration)          |
| 0x2EE | INTOSC_BYPASS_SET | Internal oscillator bypass (enable)          |
| 0x1EE | INTOSC_BYPASS_CLR | Internal oscillator bypass (disable)         |
| 0x332 | VERIFY_ENABLE     | Enter verify mode                            |
| 0x3FF | BYPASS            | Standard JTAG bypass                         |

### Device Type Lookup Tables (A0, A1, A5, etc.)

**WARNING**: These are multi-element INTEGER arrays (46 entries).
Earlier analysis referenced values at specific indices (e.g.,
"device type 12") but those indices were based on pre-reversal
data. The values cited in annotations on the transpiled program.py
files may be wrong. Re-derive from the interpreter or re-generate
the transpiled output if needed.


## Cyclone 10 LP (10CL025Y, IDCODE 0x020F30DD)

### Device Constants

All values below are from size-1 arrays — verified correct.

| Parameter          | Value       | Notes                         |
|--------------------|-------------|-------------------------------|
| IR length          | 10 bits     |                               |
| A13 (capabilities) | 0x0024      | ACTIVE + HAS_SRAM_CONFIG      |
| A147 (flags)       | 0           | No special flags              |
| Bitstream length   | 5,748,760 bits | ~702 KiB                  |
| Status DR length   | 732 bits    | Mostly opaque config state    |
| CONF_DONE bit      | 286         |                               |
| Max JTAG freq      | 12 MHz      |                               |

### Programming Sequence (SRAM Configuration)

Cyclone 10 LP uses **direct JTAG bitstream shift** — no SDM, no
bridge. The bitstream is shifted directly into the device's
configuration RAM via the CONFIG data register.

```
1. IR = 0x002 (CONFIG)
2. Wait 12000 TCK in Run-Test/Idle
3. DR-scan: shift entire RBF bitstream (5,748,760 bits)
   - RBF bytes are already in JTAG wire order (LSB-first per byte)
   - No bit-swapping needed (openFPGALoader reverses because its
     JTAG layer is MSB-first; acrobe's FTDI MPSSE is LSB-first)
4. IR = 0x004 (CHECK_STATUS)
5. Wait 60 TCK
6. DR-scan: read 732-bit status register
7. Check bit 286 = CONF_DONE
8. IR = 0x003 (STARTUP)
9. Wait 49152 + 512 TCK
10. IR = 0x3FF (BYPASS)
11. Wait 12000 TCK
```

### RBF Format

RBF (Raw Binary File) is the bitstream format for JTAG/passive serial
configuration.

- Starts with 0xFF preamble bytes (typically 32 bytes)
- Sync word: `0x6AF7F7F7` (at byte offset 32 for Cyclone 10)
- Followed by configuration opcodes and fabric data
- File is ~95% zeros (sparse — most FPGA resources unused)
- The entire file including preamble must be sent for configuration
- Bytes are in JTAG wire order (LSB-first per byte for acrobe)

### SOF Format

SOF (SRAM Object File) is Quartus' container format.

Header: `SOF\0` + version(4 LE) + section_count(4 LE)

Each section: tag(1) + flags(1) + length(4 LE) + data(length)

| Tag  | Name          | Content                                  |
|------|---------------|------------------------------------------|
| 0x01 | TOOL          | Quartus version string                   |
| 0x02 | DEVICE        | Target device (e.g. "10CL025YU256C8G")   |
| 0x03 | DESIGN        | Design name                              |
| 0x11 | CONFIG_DATA   | Configuration data (Quartus internal)    |
| 0x12 | CONFIG_INFO   | 40 bytes of config register values       |
| 0x13 | METADATA      | 16 bytes, includes USERCODE at offset 12 |
| 0x15 | CHECKSUM      | CRC/checksum data                        |
| 0x24 | EMBEDDED      | Embedded files (hashes, JDI, SLD)        |
| 0x08 | END           | End marker                               |

The CONFIG_DATA section is in Quartus **internal representation**, not
RBF. The SOF-to-RBF conversion is non-trivial and proprietary:
different spatial layout, different bit counts, different byte-value
distributions. Not a simple compression or reordering.

### Status Register (732 bits)

The 732-bit register is mostly opaque configuration state, not a
sparse status word. Only bit 286 (CONF_DONE) is meaningful for
programming. Bit 285 also correlates with configuration state.

The register content changes with the loaded bitstream — most bits
are configuration-data-dependent, not status flags. USERCODE is NOT
embedded in the status register (it's a separate DR via IR 0x007).


## Agilex 5 (A5ED065BB32AR0, IDCODE 0x0364F0DD)

### Device Constants

All values below are from size-1 arrays — verified correct.

| Parameter          | Value        | Notes                            |
|--------------------|--------------|----------------------------------|
| IR length          | 10 bits      | Same as Cyclone 10              |
| A13 (capabilities) | 0x8024       | ACTIVE + SRAM_CONFIG + FLASH    |
| A147 (flags)       | 0x80         | Has SDM                         |
| Bitstream length   | 3,604,688 bits | ~440 KiB (FSBL/SPL only)     |
| Status DR length   | 492 bits     | Shorter than Cyclone 10         |
| CONF_DONE bit      | 13           | Much lower than Cyclone 10's 286|
| Max JTAG freq      | 12 MHz       | Same as Cyclone 10              |
| Flash size         | 64 MB        | External SPI NOR                |

### Architecture

Agilex 5 does NOT use direct JTAG bitstream shift. All configuration
goes through the **Secure Device Manager (SDM)** using Virtual JTAG.

The bitstream is in Agilex-native SDM format (header `0x62294895` LE),
not RBF (no `0x6AF7F7F7` sync word). It ends with `dummy_hash_block`.

### Flash Database (A148)

**WARNING**: A148 is a 2015-entry INTEGER array. Earlier analysis
that decoded flash part names from this array was based on
pre-reversal data and is WRONG. Re-derive if needed.


## SDM Communication Protocol

### Physical Transport (JTAG)

The SDM is accessed through Intel's Virtual JTAG (VJ) interface:

- **IR 0x201** (VIR): shifts command/address words into the SDM
- **IR 0x202** (VDR): shifts data words to/from the SDM
- **IR 0x002** (CONFIG): bulk bitstream streaming (bypasses VJ)
- **IR 0x208** (CONFIG_STATUS_VJ): streaming status check

### 34-bit Word Format (asymmetric by direction)

The two low bits have different semantics for commands vs responses:

**VIR command words (host → SDM):**
```
[33:2] = 32-bit command payload
[1]    = LAST  (end of multi-word command / release)
[0]    = FIRST (start of command / write request)
```

Encoding:
- `00` = read-only query (no new command for SDM)
- `01` = write request / first word of multi-word command
- `10` = last word of multi-word command
- `11` = complete single-word write+close command

Evidence: config request and status query use IDENTICAL payload
`0x80000001` — they differ only in bit[0] (FIRST=1 for request,
FIRST=0 for query). Similarly access check vs close use same
payload `0x80000003` with different modifier bits.

**VDR response words (SDM → host):**
```
[33:2] = 32-bit response payload
[0]    = VALID (response data present)
[1]    = LAST  (end of response packet)
```

Evidence: j97 checks only bit[0] to decide whether to capture
response data. Bit 23 of the payload is always masked out
(busy/pending indicator).

### JTAG VIR/VDR Shift Register Semantics

**Critical**: VIR and VDR are **single-word shift registers**, not
FIFOs. Each DR scan of 34 bits simultaneously shifts one word in
(TDI) and one word out (TDO).

For VIR writes (STAPL j89 approach):
1. First scan reads FIFO level (12-bit counter in bits [11:0])
2. Subsequent scans push command words one at a time

For VDR reads (STAPL j97 approach):
1. Host always shifts ZEROS into VDR
2. SDM loads response data asynchronously
3. j97 retries (with 20ms delays) until valid response appears
4. J80/J81 are expected/mask values for CHECKING, not data to send

**Atomic vs word-by-word VIR**: SPI bridge commands require all
VIR words packed into a single DR scan (N×34 bits). Sync/config
commands work with word-by-word VIR. The `SdmJtagTransport`
supports both modes via the `atomic` parameter.

### SDM Access Control

After power cycle, the SDM starts with flash access **denied**.
The access check command (`cmd_id=0x0C`) returns an error indicating
access must be unlocked. Quartus programmer performs additional
authorization before flash access. This is separate from SRAM
configuration which only needs sync + config request.

### 8-bit Avalon-ST SDM Interface

The SDM also has a parallel 8-bit interface with signals:
VALID, READY, DATA[7:0], Clock. No SOP/EOP/FIRST/LAST framing —
just raw bytes with flow control. The 34-bit JTAG framing
(FIRST/LAST/VALID bits) is JTAG-specific and not part of the
Avalon-ST protocol itself.

### Transaction Pattern (j88)

```
1. Set IR to VIR (0x201)
2. Poll VIR DR for FIFO free slots: bits[11:0] = used count
3. Push command words into VIR DR one at a time
4. Set IR to VDR (0x202)
5. Shift zeros, check responses against mask, retry with delays
```

### SDM Command Formats

**Simple commands** (bit[31]=1): single VIR word

```
[31]    = 1
[7:0]   = command ID
```

| cmd_id | Name             | Purpose                        |
|--------|------------------|--------------------------------|
| 0x01   | CONFIG_REQUEST   | Request SRAM configuration     |
| 0x0C   | ACCESS_CHECK     | Check/release flash access     |
| 0x1D   | SFDP_READ        | Read flash SFDP table          |

**Multi-word SPI commands** (bit[31]=0): for flash access

**Dynamic commands**: type-byte encoded operations
(0x34=chip_select, 0x38=erase, 0x39=program, 0x3A=verify,
0x6E=blank_check)

### Configuration Flow (dj161)

```
1. Sync phase 1: flush/reset
   VIR: [0xC0000000, 0x80000000]  (no FIRST/LAST = read-only flush)
   VDR: read until valid ack

2. Sync phase 2: device-specific handshake
   Config mode and flash mode use DIFFERENT sync2 signatures.
   VIR: [signature words with FIRST/LAST]
   VDR: check response against expected values

3. Config request: cmd_id=0x01, FIRST=1
   VDR: ack probe

4. Bitstream streaming (see below)

5. Final status polling via VIR/VDR cmd_id=0x01 (FIRST=0 = query)
   VDR: read 5 words, check CONF_DONE
   Poll up to 15× at 100ms intervals
```

### Bitstream Streaming (j127)

After the SDM accepts the configuration request, bitstream data is
streamed via IR 0x002 (CONFIG) with interleaved status checks.

**Status check (j125)**: IR = 0x208 (CONFIG_STATUS_VJ), 37-bit DR

```
DR TDI bits:
  [0] = request_data  (kick SDM to continue processing)
  [1] = start_config  (first iteration only)
  [2] = enable         (first iteration only)

DR TDO bits:
  [0]     = DONE (SDM finished current buffer, needs more data or complete)
  [1]     = ERROR (fatal, abort)
  [31:2]  = progress (30 bits, in units of 32 bits consumed)
  [36:32] = FIFO free count (5 bits)
```

**Data frame**: IR = 0x002, variable-length DR

```
DR layout (LSB shifted first):
  [63:0]          = Frame header: 0xA17E2A00_FFFFFFFF (constant)
  [63+N:64]       = Bitstream data (N bits, variable)
  [64+N]          = Trailer (1 bit)
  Total: 65 + N bits
```

**Flow control loop** (from STAPL j127):

```
chunk_size = 32768 bits (initial), max = 524288 (J120)
loop:
  1. Status check (j125)
  2. If DONE=1: do NOT send data, set request_data on next check
     Halve chunk_size. Loop to 1.
     (GOTO J131 in STAPL — critical back-pressure mechanism)
  3. If DONE=0: send one chunk via CONFIG DR
     Double chunk_size (up to J120 max). Loop to 1.
  4. If progress == total: done
  5. If ERROR: abort
```

### Current Status

**Working**:
- Cyclone 10 LP: full SRAM configuration via direct JTAG bitstream
- Agilex 5 SDM: sync, config request, bitstream streaming (95.5%)
- STAPL interpreter with `AcrobePlayer`: CHECK_IDCODE verified
- STAPL transpiler with corrected GOTO handling and init ordering

**Remaining issues**:
- Agilex 5 streaming stalls at 95.5% — likely VIR atomic vs
  word-by-word interaction with the CONFIG DR path
- Flash access denied after power cycle — needs authorization
  sequence from Quartus trace

### Transport Abstraction

```
Application layer:  SDM commands (configure, status, flash ops)
                    ↕ 32-bit command/response words
Framing layer:      34-bit words (FIRST/LAST for cmds, VALID/LAST for resp)
                    ↕ JTAG-specific framing
Transport layer:    JTAG VIR/VDR (IR 0x201/0x202)
                    -or- 8-bit Avalon-ST FIFO (VALID/READY/DATA/CLK)
```

Implementation:
- `sdm_jtag.py`: `SdmJtagTransport` — word-at-a-time or atomic VIR
- `sdm_spi.py`: `SdmSpiAdapter` — SPI target API over SDM bridge
- `agilex5.py`: `SdmMailbox` — protocol layer, transport-agnostic

### Open Questions

- Why does streaming stall at 95.5%?
- The `0xA17E2A00` frame header meaning
- How Quartus unlocks flash access after power cycle
- Full SDM command set beyond configure/status/flash
- Whether the 8-bit FIFO interface packs the 32-bit payloads as
  4 bytes each, or uses a different framing
