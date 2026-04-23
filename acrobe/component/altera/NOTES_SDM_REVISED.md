# SDM JTAG Transport — Revised Understanding

## JTAG Instructions

| IR Code | Name | Direction | Purpose |
|---------|------|-----------|---------|
| 0x201   | SDM_CMD | Host → SDM | Command FIFO: push command/data words |
| 0x202   | SDM_RSP | SDM → Host | Response FIFO: read response words |

Previous naming (VIR/VDR) was misleading — these are not
"Virtual IR/DR" in the SLD sense, but rather two unidirectional
FIFOs accessed via DR shifts.

## 34-bit DR Word Format

Each DR shift is 34 bits. The 32-bit SDM word occupies bits [33:2].
Bits [1:0] are transport framing — exact meaning TBD, candidates:
- VALID: word contains meaningful data
- READY: sender/receiver flow control
- SOF/EOF: frame boundaries (possibly redundant with header length)

### On SDM_CMD (0x201):
- TDI[33:2]: 32-bit command/data word to SDM
- TDI[1:0]: framing (FIRST/LAST of frame? or VALID?)
- TDO[33:2]: likely ignored or echo
- TDO[1:0]: possibly READY/credit from SDM

### On SDM_RSP (0x202):
- TDI[33:2]: possibly zeros, or READY/ACK to pop FIFO
- TDI[1:0]: possibly READY signal to SDM
- TDO[33:2]: 32-bit response word from SDM
- TDO[1:0]: VALID? LAST?

## SDM Command Word Format (32 bits)

```
[31:28]  Reserved (0)
[27:24]  ID — transaction tag, echoed in response
[23]     0
[22:12]  Length — number of argument words following this header
[11]     0
[10:0]   Command opcode
```

## Known Opcodes

| Opcode | Name | Args | Response Words | Notes |
|--------|------|------|----------------|-------|
| 0x000  | NOOP | 0 | 0 | |
| 0x006  | CONFIG_STATUS | 0 | 6 | |
| 0x010  | GET_IDCODE | 0 | 1 | |
| 0x012  | GET_CHIPID | 0 | 2 | |
| 0x013  | GET_USERCODE | 0 | 1 | |
| 0x018  | GET_VOLTAGE | 1 (bitmask) | N | |
| 0x019  | GET_TEMPERATURE | 1 | N | |
| 0x032  | QSPI_OPEN | 0 | 0 | |
| 0x033  | QSPI_CLOSE | 0 | 0 | |
| 0x034  | QSPI_SET_CS | 1 ([31:28]=CS#) | 0 | |
| 0x038  | QSPI_ERASE | 2 (addr, count) | 0 | count in 32-bit words |
| 0x039  | QSPI_WRITE | 2+N (addr, count, data...) | 0 | count in header AND as arg |
| 0x03A  | QSPI_READ | 2 (addr, count) | count | |
| 0x05B  | RSU_STATUS | 0 | 9 | |

## Cross-reference with STAPL do_sync_and_program

From the transpiled code, `do_sync_and_program` sends three SDM
commands via `sdm_command` (J88 = VIR shift then VDR shift):

### Command 1: Sync handshake
- VIR words: 2, constant = `\x00\x00\x00\x00\x03\x00\x00\x00\x08\x00`
  = 34-bit words: 0x000000000 | 0x003, 0x000000008 | 0x000
  Decoding: first word [33:2] = 0x00000000, [1:0] = 3 (FIRST|LAST?)
  Second word [33:2] = 0x00000002, [1:0] = 0

  But if [1:0] are framing and [33:2] is the SDM word:
  Word 0: SDM = 0x00000000, frame = 0b11
  Word 1: SDM = 0x00000002, frame = 0b00

  SDM word 0x00000000 → opcode 0, length 0 = NOOP
  SDM word 0x00000002 → opcode 2, length 0 = ??? (unknown opcode)

  This needs MITM capture to verify bit ordering.

### Command 2: Config request (?)
- VIR words: 2, constant has different pattern
- VDR expected response checked with mask

### Command 3: Another handshake
- VIR words: 1

The exact decoding depends on bit ordering within the 34-bit word,
which the MITM will reveal.

## Next Steps

1. Build MITM JTAG component to capture SDM_CMD/SDM_RSP traffic
2. Run the STAPL interpreter with MITM to capture actual traffic
3. Decode 34-bit words and determine framing bit semantics
4. Map captured commands to known opcode table
5. Verify response format matches expected structure
