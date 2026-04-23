# SDM JTAG Transport Protocol

## Overview

The SDM (Secure Device Manager) on Agilex 5 FPGAs communicates via
two JTAG-accessible FIFOs:

| IR Code | Name    | Direction  | Purpose                    |
|---------|---------|------------|----------------------------|
| 0x201   | SDM_CMD | Host → SDM | Command FIFO: push words   |
| 0x202   | SDM_RSP | SDM → Host | Response FIFO: read words  |

Each DR scan shifts 34 bits. The 32-bit SDM word and 2-bit framing
are packed differently depending on direction.

## 34-bit DR Format

### CMD (host → SDM, via TDI)

Framing bits at MSB [33:32] (last bits shifted in JTAG):

```
[31:0]  = 32-bit SDM command/data word
[33:32] = framing enum:
    00 = idle (no data for SDM)
    01 = valid, more words follow
    10 = valid, last word of frame
    11 = single-word frame (used for flush/reset)
```

### RSP (SDM → host, via TDO)

Framing bits at LSB [1:0] (first bits shifted out in JTAG):

```
[1:0]  = framing enum:
    00 = idle (no response data)
    01 = valid, more words follow
    11 = valid, last word of frame
    10 = reserved/unknown
[33:2] = 32-bit SDM response word
```

The asymmetry is because JTAG shifts LSB first. Each direction
puts its framing where the receiver sees it first/last relative
to the shift order.

## SDM Command Word Format (32-bit)

```
[31:28]  Upper nibble (0 normally, 0xF for SYNC command)
[27:24]  ID tag — echoed in response, for matching
[23]     0
[22:12]  Length — number of argument words following this header
[11]     0
[10:0]   Command opcode
```

## SDM Response Header Format (32-bit)

```
[31:28]  Upper nibble (echoed from command)
[27:24]  ID tag (echoed from command)
[23]     0
[22:12]  Length — number of data words following this header
[11]     0
[10:0]   Error code (0 = OK)
```

## Known Opcodes

| Opcode | Name                | Args | Response Words | Notes |
|--------|---------------------|------|----------------|-------|
| 0x000  | NOOP                | 0    | 0              | |
| 0x001  | SYNC                | 1    | 1              | Nonce echo, upper=0xF |
| 0x004  | CONFIG_STATUS_ALT   | 0    | 6              | Agilex-specific? |
| 0x005  | CONFIG_REQUEST      | 0    | 0              | Start configuration |
| 0x006  | CONFIG_STATUS       | 0    | 6              | Standard |
| 0x010  | GET_IDCODE          | 0    | 1              | Returns JTAG IDCODE |
| 0x012  | GET_CHIPID          | 0    | 2              | |
| 0x013  | GET_USERCODE        | 0    | 1              | |
| 0x018  | GET_VOLTAGE         | 1    | N              | Arg: channel bitmask |
| 0x019  | GET_TEMPERATURE     | 1    | N              | |
| 0x032  | QSPI_OPEN           | 0    | 0              | Enable flash access |
| 0x033  | QSPI_CLOSE          | 0    | 0              | Disable flash access |
| 0x034  | QSPI_SET_CS         | 1    | 0              | [31:28]=CS line (0-3) |
| 0x035  | QSPI_READ_DEVICE_REG| 2    | N              | Args: opcode, byte_count |
| 0x038  | QSPI_ERASE          | 2    | 0              | Args: addr, word_count |
| 0x039  | QSPI_WRITE          | 2+N  | 0              | Args: addr, count, data |
| 0x03A  | QSPI_READ           | 2    | N              | Args: addr, word_count |
| 0x05B  | RSU_STATUS          | 0    | 9              | |

## Communication Sequence

### Sync Handshake

1. **Flush**: Send zeros with SINGLE framing (11) then LAST (10)
   to clear stale FIFO state.
2. **Read flush response**: Shift RSP, expect idle.
3. **SYNC command**: Send header (opcode=1, len=1, upper=0xF)
   + nonce word. SDM echoes the nonce in response.
4. Nonce is device-specific (from STAPL per part number).

### Command/Response

1. Load IR = 0x201 (SDM_CMD)
2. Shift idle word (framing 00) to flush
3. Shift command header with framing 01 (more) or 10 (last if no args)
4. Shift argument words with framing 01 (more) / 10 (last)
5. Load IR = 0x202 (SDM_RSP)
6. Shift zeros until TDO framing shows valid (01 or 11)
7. Response header word has error code in [10:0], data count in [22:12]
8. Continue reading until framing shows LAST (11) or expected count

### Bitstream Configuration

After SYNC + CONFIG_REQUEST, bitstream data is streamed via a
separate DR path (IR 0x002, CONFIG instruction), not through
the SDM_CMD/SDM_RSP FIFOs. The streaming uses a 37-bit status
register at IR 0x208 for flow control:

```
Status DR [36:0]:
  [0]    = DONE (SDM busy/processing)
  [1]    = ERROR
  [31:2] = progress (words consumed × 32 = bits)
  [36:32] = FIFO free slots
```

Each data chunk is framed with a 64-bit header (0xA17E2A00_FFFFFFFF)
and a 1-bit trailer.

## Device-specific Constants

| Part               | IDCODE     | Sync Nonce   |
|--------------------|------------|--------------|
| A5EA013BB23B (DE25)| 0x4362C0DD | 0xAB92C300   |
| A5ED065BB32AR0     | 0x4364F0DD | 0x7F38963E   |
