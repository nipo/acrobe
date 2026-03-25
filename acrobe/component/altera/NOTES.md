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

The CONFIG_DATA section is in Quartus **internal frame format**, not
RBF. The SOF-to-RBF conversion is non-trivial and proprietary:
different spatial layout, different bit counts (38K vs 69K set bits),
different byte-value distributions. Not a simple compression or
reordering.

### Status Register (732 bits)

The 732-bit register is mostly opaque configuration state, not a
sparse status word. Only bit 286 (CONF_DONE) is meaningful for
programming. Bit 285 also correlates with configuration state.

The register content changes with the loaded bitstream — most bits
are configuration-data-dependent, not status flags. USERCODE is NOT
embedded in the status register (it's a separate DR via IR 0x007).

Two consecutive reads of the same state produce identical results
(deterministic, no noise).


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

### Architecture Difference from Cyclone 10

Agilex 5 does NOT use direct JTAG bitstream shift. Instead, all
configuration goes through the **Secure Device Manager (SDM)** using
a Virtual JTAG bridge:

- **CONFIGURE** action: `_proc_dj161` — sends bitstream to SDM via
  the VIR/VDR protocol, SDM configures the FPGA fabric
- **PROGRAM** action: `_proc_dj0` — programs external SPI flash via
  the SDM's flash controller
- Cyclone 10's `_proc_l141` (direct JTAG shift) does not exist in
  the Agilex 5 code

### Flash Database (A148)

The STAPL includes a 65-entry SPI flash compatibility database with
device names, sizes, timing parameters, and programming options.
Supported flash families include: S25FL (Spansion/Infineon), EPCQ
(Altera), MX25U/MX25L (Macronix), MT25QU/MT25QL (Micron).

The flash programming uses the SDM's built-in SPI controller, accessed
through the same VIR/VDR mailbox protocol as configuration.


## SDM Communication Protocol

### Physical Transport (JTAG)

The SDM is accessed through Intel's Virtual JTAG (VJ) interface:

- **IR 0x201** (VIR): shifts command/address words into the SDM
- **IR 0x202** (VDR): shifts data words to/from the SDM
- **IR 0x002** (CONFIG): bulk bitstream streaming (bypasses VJ overhead)

Word format: **34 bits** per word:

```
[33:2] = 32-bit payload
[1]    = EOP (end of packet)
[0]    = SOP (start of packet)
```

This is **Avalon-ST** framing — the same streaming protocol used
internally by Intel FPGAs. The 32-bit payloads should be identical
across all SDM transports (JTAG, 8-bit FIFO, etc.).

### Transaction Pattern

Each SDM transaction follows this pattern (implemented by `j88`):

1. **Set VIR** (`j89`):
   - Switch to IR 0x201 (VIR)
   - Poll VDR to read FIFO free slots (bits [11:0])
   - Push command words into VDR (with SOP/EOP framing)
   - Wait for SDM to process

2. **Exchange VDR** (`j97`):
   - Switch to IR 0x202 (VDR)
   - Send data words, read responses
   - Compare responses against expected values with mask
   - Retry on mismatch (with timeout)

The VIR write uses a FIFO-based flow control: before sending
commands, the host reads the VDR to check how many slots are free
(12-bit counter in bits [11:0]). Commands are written in batches
that fit the available FIFO depth.

### SDM Mailbox Commands

The VIR command words encode SDM mailbox operations. All 26 j88 call
sites have been decoded. There are two command formats:

#### VIR Command Format

**Simple commands** (bit[31]=1): single VIR word

```
[31]    = 1 (simple command flag)
[30:8]  = 0 (reserved)
[7:0]   = command ID
```

Known command IDs:
| cmd_id | Name                | Purpose                        |
|--------|---------------------|--------------------------------|
| 0x01   | CONFIG_REQUEST      | Request SRAM configuration     |
| 0x0C   | ACCESS_CHECK        | Check/release flash access     |
| 0x1D   | SFDP_READ           | Read flash SFDP table          |

The SOP/EOP framing on simple commands varies:
- Config request: SOP=1, EOP=0
- Status query (same cmd_id 0x01): SOP=0, EOP=0
- Access check: SOP=0, EOP=1 (or SOP=1, EOP=1 for close)

**Multi-word SPI commands** (bit[31]=0): multiple VIR words

```
Word 0 (header):
  [31]    = 0 (SPI transaction)
  [30]    = 1 (always set)
  [23:16] = byte count of SPI transaction
  [15:8]  = 0
  [7:0]   = 0x0D (flash SPI command marker)

Word 1 (SPI opcode):
  [7:0]   = SPI opcode byte
  remaining bits = address/parameters

Word 2+: additional parameter words
```

**Dynamic commands** (built at runtime via type byte):

```
Word 0:
  [33:2] payload:
    [31:24] = 0x00
    [23:12] = word count (number of parameter words following)
    [11:0]  = 0
  [10:2]  = type byte (opcode) in bits [10:2] of raw 34-bit word
  [1:0]   = SOP/EOP

Type bytes:
  0x34 = Set SPI chip select
  0x36 = Write SPI register (WRNVCR, WRVCR)
  0x38 = Erase sector
  0x39 = Program page
  0x3A = Read and verify
  0x6E = Read and blank-check
```

#### VDR Response Format

Every VDR response word has:
- Bit 0 (SOP position in the 34-bit frame): valid/ready flag
- **Bit 23 of the 32-bit payload**: busy/pending indicator (always
  masked out — every single-word mask is 0xFF7FFFFF)
- Remaining bits: response data

For read operations (J85 > 1), captured data goes into J82 for
extraction by the caller.

#### Synchronization Sequence

Two transactions, performed at the start of both configuration
(dj161) and flash programming (dj0):

Transaction 1 — Reset/flush:
```
VIR: [0xC0000000, 0x80000000]  (no SOP/EOP — raw flush)
VDR: [0x00000000] SOP+EOP      mask=0xFF7FFFFF
Retry: 50-75
```

Transaction 2 — Handshake (device-specific signature):
```
VIR: [0x7C000400 SOP, (signature)]  EOP on last
VDR: [expected1, expected2]          with masks
```
The sync2 signature differs between configuration mode and flash
programming mode. This may select different SDM subsystems.

#### Configuration Flow (dj161)

```
1. Sync (transactions 1+2)
2. VIR: cmd_id=0x01 SOP=1  →  Configuration request
   VDR: ack probe           →  SDM accepts
3. Bitstream streaming (j127, see below)
4. Status polling (dj192, see below)
```

#### Bitstream Streaming (j127)

After the SDM accepts the configuration request, bitstream data is
streamed via IR 0x002 (CONFIG). This is a dedicated bulk path that
bypasses VIR/VDR for throughput.

The stream alternates between data chunks and status checks:

```
loop:
  1. Check status: IR = 0x208 (CONFIG_STATUS_VJ)
     DR scan: 37 bits (single device, no bypass)
       bit[0]     = DONE (configuration complete)
       bit[1]     = ERROR (abort with error code 10)
       bits[31:2] = progress (30 bits, in units of 32 bits consumed)
       bits[36:32] = FIFO free slots (5 bits)
     If DONE=1: break (success)
     If ERROR=1: abort

  2. Send data chunk: IR = 0x002 (CONFIG)
     DR frame layout (LSB shifted first):
       bits[63:0]          = Frame header (constant)
       bits[63+N:64]       = Bitstream data (N bits)
       bit[64+N]           = Trailer (1 bit)
     Total DR width = 65 + N bits

     Frame header (64 bits, constant):
       0xA17E2A00_FFFFFFFF
       Lower 32 = 0xFFFFFFFF (padding / sync)
       Upper 32 = 0xA17E2A00 (SDM frame marker)

  3. Adaptive chunk sizing:
     N starts at 32768 bits (4 KiB)
     On success (SDM consumed data): N *= 2
     On stall (DONE=1 but not finished): N /= 2
     N capped at maximum (device-specific)

  4. Repeat until progress == total bitstream size
```

First iteration has special handling: the status check DR sets
bits [0:2] = 0b111 (request data + start config + enable), then
subsequent iterations only set bit[0] when SDM stalls.

Total bitstream: J1[device] * 8 bits = 0x6E000 * 8 = 3,604,480
bits for the Agilex 5 FSBL.

#### Final Status Polling (dj192)

After streaming completes, poll for CONF_DONE via the VIR/VDR
mailbox (not the CONFIG_STATUS_VJ DR):

```
loop (up to 15 times, 100ms between):
  Wait 100ms
  VIR: cmd_id=0x01 (no SOP) → status query
  VDR: read 5 words (34 bits each = 170 bits)
    word[0] bits[24:14] = error status (DJ201)
    word[1] payload     = CONF_DONE register (DJ202)
      expected: 0x10000000 for success
    word[3] payload     = additional status (DJ203)
    word[4] payload     = additional status (DJ204)
  If all 5 words match expected/mask: CONF_DONE, break
  If iteration 14 and not done: check DJ202 for failure
```

#### Flash Programming Flow (dj0)

```
1. Sync (transactions 1+2, different sync2 signature)
2. Access check: cmd_id=0x0C
3. For each chip select:
   a. Set chip select: type=0x34
   b. Read flash JEDEC ID: SPI 0x9F + 0xAF
   c. Validate against A148 database
   d. Read SFDP: cmd_id=0x1D (if needed)
4. Configure flash registers:
   a. Read NVCR (SPI 0xB5), VCR (SPI 0x85)
   b. Write Enable (SPI 0x06)
   c. Write NVCR (SPI 0xB1), VCR (SPI 0x81)
   d. Verify writes
5. Erase sectors: type=0x38 (per 256K sector)
6. Blank-check: type=0x6E (per 64K block, 17 VDR words)
7. Program pages: type=0x39 (per 4K page, slow mode)
8. Verify: type=0x3A (per 64K block, compare against file)
9. Close: cmd_id=0x0C SOP+EOP
```

### Transport Abstraction

The SDM mailbox protocol has clear layering:

```
Application layer:  SDM commands (configure, status, flash ops)
                    ↕ 32-bit command/response words
Framing layer:      Avalon-ST (34-bit: SOP + EOP + 32-bit payload)
                    ↕ 34-bit framed words
Transport layer:    JTAG VIR/VDR (IR 0x201/0x202)
                    -or- 8-bit FIFO interface
                    -or- other SDM interfaces
```

The JTAG transport adds:
- FIFO flow control: read VDR to get free slot count before writing
- VIR/VDR IR switching overhead per transaction
- 34-bit word packing into DR shift registers with bypass padding

The 32-bit command/response payloads at the application layer should
be identical regardless of transport. The Avalon-ST SOP/EOP framing
is the packet boundary mechanism.

For the 8-bit FIFO interface, the mapping is likely:
- 4 bytes per 32-bit word (simple byte packing)
- SOP/EOP signaled by sideband signals on the FIFO interface
- No JTAG IR switching or FIFO level polling needed
- The bitstream streaming path (IR 0x002 with 37-bit frames) would
  use a dedicated FIFO channel or the same mailbox with a different
  opcode

### JTAG VDR Shift Register Semantics

**Critical implementation detail**: The VIR and VDR are **single-word
shift registers**, not FIFOs. Each DR scan of N bits simultaneously
shifts N bits in (TDI) and N bits out (TDO). The TDO value is the
**previous** DR content, not the response to the current TDI.

This means:
- Scan 0: TDI=word0, TDO=previous_state (discard)
- Scan 1: TDI=word1, TDO=response_to_word0
- Scan 2: TDI=word2, TDO=response_to_word1
- ...

The STAPL `j89` (VIR write) and `j97` (VDR exchange) both operate
**one word at a time** in a loop for this reason. Multi-word
transactions cannot be sent as a single large DR shift.

For VIR writes (`j89`):
1. First scan reads FIFO level (12-bit counter)
2. Subsequent scans push command words one at a time

For VDR exchanges (`j97`):
1. Each scan sends one data word and captures the response to the
   previous word
2. The response is checked against mask/expected values
3. If the check fails, the word is retried
4. The valid/ready bit (bit 0 at device offset) indicates whether
   the SDM has processed the command

### Open Questions

- The sync2 handshake signatures — what do the magic numbers encode?
  Are they device-specific or mode-specific?
- The `0xA17E2A00` bitstream frame header — SDM routing tag? CRC seed?
- VDR response bit 23 — confirmed as busy/pending, but what
  triggers it?
- Whether the 8-bit FIFO uses Avalon-ST framing or a simplified
  32-bit word protocol with sideband SOP/EOP signals
- Full SDM command set beyond configure/status/flash — what other
  services does the SDM expose?
- The 37-bit (not 34-bit) word size during bitstream streaming —
  what are the extra 3 bits?
