"""SPI adapter for Altera SDM SPI flash passthrough.

Translates acrobe spi.Cs/Shift operations into SDM VIR/VDR commands
using the 0x0D SPI marker format.

The SDM handles one atomic SPI transaction per command:
CS assert → clock opcode + address + data → CS deassert.

Stack: SdmSpiAdapter → spi.Interface → spi.Target → flash driver
"""

from ...engine import Batcher
from ...protocol.spi import Cs, Shift
from ...node import Node
from ...protocol import spi
from .sdm import Command

class SdmSpiAdapter(spi.Interface):
    """Translates SPI ops to SDM SPI passthrough commands.

    Each CS-held transaction (between Cs(assert) and Cs(None))
    becomes a single SDM SPI command.  All Shift ops within the
    transaction are concatenated into one SPI byte stream.
    """

    SPI_MARKER = 0x0D

    def __init__(self, sdm):
        super().__init__("spi")
        self._sdm = sdm

        self.child_add(spi.Target(self, cs=0, mode=0, name="cs0"))

    async def start(self):
        await self._sdm.qspi_open()
        await self._sdm.qspi_set_cs(0)

    async def flush_ops(self, batch):
        # Group shifts between CS assert/deassert into transactions
        transactions = []
        current_shifts = []

        for op, future in batch:
            if isinstance(op, Cs):
                if op.value is None and current_shifts:
                    transactions.append(current_shifts)
                    current_shifts = []
                future.set_result(op)
            elif isinstance(op, Shift):
                current_shifts.append((op, future))

        for shifts in transactions:
            await self._execute_transaction(shifts)

    async def _execute_transaction(self, shifts):
        """Execute one CS-held SPI transaction via SDM."""
        # Concatenate MOSI data from all shifts
        mosi = bytearray()
        miso_map = []  # (offset, length, read_miso, shift_idx)
        for i, (op, future) in enumerate(shifts):
            offset = len(mosi)
            mosi.extend(bytes(op.mosi))
            miso_map.append((offset, op.byte_count, op.read_miso, i))

        total_bytes = len(mosi)
        need_miso = any(op.read_miso for op, _ in shifts)

        # Build VIR command: SPI marker header + opcode/data words
        vir_words = self._build_vir(mosi, need_miso)
        await self._tr.vir_write(vir_words, atomic=True)

        # Read VDR response
        if need_miso:
            miso_all = await self._read_miso(total_bytes)
        else:
            # Just read ack
            await self._tr.vdr_read(2)
            miso_all = None

        # Distribute MISO to individual Shift ops
        for offset, length, read_miso, idx in miso_map:
            op, future = shifts[idx]
            if read_miso and miso_all is not None:
                op.miso = bytes(miso_all[offset:offset + length])
            else:
                op.miso = None
            future.set_result(op)

    def _build_vir(self, mosi, need_response):
        """Build VIR words for SDM SPI passthrough.

        From STAPL analysis, the format is:
          Word[0]: header — payload[30]=1, [15:8]=byte_count, [7:0]=0x0D
          Word[1]: raw[7:0] = SPI opcode, higher bits = address/data
          Word[2+]: more data bytes
          Final word: "execute" command (payload=0x80000001) if reading
        """
        p = self._tr.pack_word
        total = len(mosi)

        # Header word
        header_payload = (1 << 30) | ((total & 0xFF) << 8) | self.SPI_MARKER
        words = [p(header_payload, first=True)]

        # Data words: pack mosi bytes into raw 34-bit words.
        # Word[1] raw[7:0] = opcode, raw[15:8] = byte 1, etc.
        # Each subsequent word packs 4 bytes.
        offset = 0
        while offset < total:
            chunk = mosi[offset:offset + 4]
            raw_word = 0
            for i, b in enumerate(chunk):
                raw_word |= b << (i * 8)
            # Set first bit on first data word
            if offset == 0:
                raw_word |= 0x1  # FIRST
            words.append(raw_word)
            offset += 4

        # "Execute and read" word if we need MISO
        if need_response:
            words.append(p(0x80000001))

        # Set LAST on final word
        words[-1] |= 0x2

        return words

    async def _read_miso(self, total_bytes):
        """Read MISO response bytes from VDR.

        The SDM returns response data in VDR words.  We read words
        one at a time, collecting valid responses.
        """
        # Read enough words: ack words + data words
        n_words = (total_bytes + 3) // 4 + 4  # generous margin
        responses = await self._tr.vdr_read(n_words)

        # Collect payload bytes from valid response words
        miso = bytearray()
        for payload, valid, last in responses:
            if valid and payload != 0:
                # Extract 4 bytes from payload (little-endian)
                miso.extend(payload.to_bytes(4, 'little'))
            if valid and last and len(miso) >= total_bytes:
                break

        return bytes(miso[:total_bytes])
