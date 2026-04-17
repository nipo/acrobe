# Altera/Intel FPGA Programming — Reverse Engineering Notes

These notes are derived from analysis of STAPL (JAM) transpiled output,
SVF files, openFPGALoader source, and live hardware experiments with
Cyclone 10 LP and Agilex 5 devices.

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


## Cyclone 10 LP (10CL025Y, IDCODE 0x020F30DD)

### Device Constants

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

| Parameter          | Value        | Notes                            |
|--------------------|--------------|----------------------------------|
| IR length          | 10 bits      | Same as Cyclone 10              |
| A13 (capabilities) | 0x8024       | ACTIVE + SRAM_CONFIG + FLASH    |
| A147 (flags)       | 0x80         | Has SDM                         |
| Bitstream length   | 3,604,688 bits | ~440 KiB (FSBL/SPL only)     |
| Status DR length   | 492 bits     | Shorter than Cyclone 10         |
| CONF_DONE bit      | 13           | Much lower than Cyclone 10's 286|
| Max JTAG freq      | 12 MHz       | Same as Cyclone 10              |
| Device type (A12)  | 12           | Same index as Cyclone 10!       |
| Alt type (A105)    | 45           | Flash programming variant       |
| Flash size         | 64 MB        | External SPI NOR                |

### Architecture

Agilex 5 does NOT use direct JTAG bitstream shift. All configuration
goes through the **Secure Device Manager (SDM)** using Virtual JTAG.

The bitstream is in Agilex-native SDM format (header `0x62294895` LE),
not RBF (no `0x6AF7F7F7` sync word). It ends with `dummy_hash_block`.


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

For VIR writes:
1. First scan reads FIFO level (12-bit counter in bits [11:0])
2. Subsequent scans push command words one at a time

For VDR exchanges:
1. Each scan sends one data word and captures the response
2. Responses are pipelined — may appear at any position in the
   response stream
3. Bit 23 of response payload = busy flag (always masked out)

### Transaction Pattern (j88)

```
1. Set IR to VIR (0x201)
2. Poll VIR DR for FIFO free slots: bits[11:0] = used count
3. Push command words into VIR DR one at a time
4. Set IR to VDR (0x202)
5. Exchange data words one at a time, check responses against mask
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
(0x34=chip_select, 0x38=erase, 0x39=program, 0x3A=verify, 0x6E=blank_check)

### Configuration Flow (dj161)

```
1. Sync phase 1: flush/reset
   VIR: [0xC0000000, 0x80000000]
   VDR: probe with mask 0xFF7FFFFF

2. Sync phase 2: device-specific handshake
   VIR: [device_signature...]
   VDR: [expected_response...]

3. Config request: cmd_id=0x01, SOP=1
   VDR: ack probe

4. Bitstream streaming (see below)

5. Final status polling via VIR/VDR cmd_id=0x01
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

**Flow control loop**:

```
chunk_size = 32768 bits (initial)
loop:
  1. Status check (j125)
  2. If DONE=1: do NOT send data, set request_data on next check
     Halve chunk_size. Loop to 1.
     (This is the critical back-pressure mechanism)
  3. If DONE=0: send one chunk via CONFIG DR
     Double chunk_size (up to max). Loop to 1.
  4. If progress == total: done
  5. If ERROR: abort
```

The `request_data` bit is asserted when:
- First iteration (along with `start_config` and `enable`)
- SDM stalled: DONE=1 AND FIFO free count == 0

**IMPORTANT**: When DONE=1, the GOTO J131 in the STAPL source skips
the data send entirely. This was missing in early implementations
and caused the SDM's internal buffer to overflow.

### Current Status (as of 2026-03-25)

**Working**:
- SDM sync (both phases)
- Config request (cmd_id=0x01, FIRST=1)
- Bitstream streaming with flow control (95.5% of data consumed)
- Back-pressure handling (DONE=1 → don't send, retry status)

**Known Issue**:
- SDM stalls at progress=3,441,152 out of 3,604,480 bits (163,328
  bits = ~20 KiB remaining)
- No error reported, DONE stays False, progress doesn't advance
- The stall point is consistent across runs
- Data at the stall point is normal (not padding or markers)
- Adding extra idle cycles between IR/DR does not help
- Zero-padding beyond J2.bin does not help
- All chunk boundaries are byte-aligned

Likely cause: subtle difference in chunk sizing/boundaries between
our implementation and the STAPL j127. The STAPL's adaptive sizing
may produce different chunk boundaries that keep the SDM's internal
pipeline happy. Or there may be something in the j125 status check
interaction (the request_data/start_config/enable bit encoding) that
we don't replicate exactly.

**Findings from STAPL JAM source comparison**:
- Transpiler has 5 dropped forward GOTOs (j89: 2, j97: 2, j127: 1)
- j89 GOTO J95 is critical: VIR word-send loop never executes
- j127 GOTO J131 is critical: data sent during SDM stall
- j97 sends ZEROS through VDR (A29), NOT the expected values (J80).
  J80/J81 are comparison patterns only.
- J120 = 524288 (max chunk size cap, was missing in our code)
- request_data condition: `J113==0 && J111==1` (fifo empty AND done)

**Remaining issue**: SDM stalls at 3,441,152/3,604,480 bits (95.5%).
All data has been sent. The SDM stops processing the last 163,328
bits despite having received them. This suggests a missing
end-of-stream signal, final commit, or different frame format for the
last chunk.

**CRITICAL FINDING (from SPI debugging)**:
VIR writes MUST be atomic — all command words packed into a single
DR scan of N×34 bits.  Word-by-word VIR shifts produce `0x1FF`
("unknown command") responses.  Atomic VIR writes produce correct
responses.  This likely also explains the bitstream streaming stall.

**SPI flash RDID successfully read**:
After atomic VIR fix, RDID returns JEDEC ID: manufacturer 0x01
(Spansion/Infineon), type 0x03, capacity 0x19 (256 Mbit = 32 MiB).
The SDM SPI passthrough is functional.

**IMPORTANT**: Atomic vs word-by-word VIR produces DIFFERENT SDM
responses.  Atomic VIR returns real multi-word SPI data (7 words for
RDID), while word-by-word returns single-word acks.  The STAPL uses
word-by-word (j89 pushes words one at a time after FIFO poll).

The SDM SPI response data with word-by-word VIR does NOT contain
the expected Micron JEDEC ID (0x20, 0xBB, 0x22 for MT25QU02G).
This suggests the word-by-word response is metadata/acks rather
than raw SPI MISO data, and the actual MISO data extraction requires
matching j97's exact state machine (phase 0 match → phase 1 capture).

The board has a **Micron MT25QU02GCBB8E12** (2 Gbit, marking RW251).
Expected JEDEC: manufacturer=0x20 (Micron), type=0xBB, density=0x22.

Next steps:
- Carefully replicate j97's two-phase read with retry/delay logic
- The VDR response with valid=0 between words is the flow control
  mechanism — j97 retries (with 20ms delays at J103) until valid
  data appears
- Need to distinguish between "SDM ack" words and "SPI MISO data"
  words in the response stream

### Transport Abstraction

```
Application layer:  SDM commands (configure, status, flash ops)
                    ↕ 32-bit command/response words
Framing layer:      Avalon-ST (34-bit: SOP + EOP + 32-bit payload)
                    ↕ 34-bit framed words
Transport layer:    JTAG VIR/VDR (IR 0x201/0x202)
                    -or- 8-bit FIFO interface
                    -or- other SDM interfaces
```

Implementation:
- `sdm_jtag.py`: `SdmJtagTransport` — word-at-a-time VIR/VDR
- `agilex5.py`: `SdmMailbox` — protocol layer, transport-agnostic

### Open Questions

- Why does streaming stall at 95.5%? Is it a timing issue, IR/DR
  state machine transition, or something else?
- The `0xA17E2A00` frame header meaning
- Whether the 8-bit FIFO uses Avalon-ST framing or a simpler protocol
- Full SDM command set beyond configure/status/flash
- Whether the STAPL inserts idle cycles between IR and DR shifts
  that are critical (j61 does `_wait('IDLE', 16, None, None)`)
