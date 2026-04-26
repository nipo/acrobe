"""Intel HEX (.hex / .ihex) as a VFS Node container.

ihex stores discrete addressed regions. Per design:

- Root node's read() returns a single contiguous blob
  [min_addr .. max_addr], gaps filled with 0xFF. load_address =
  min_addr.
- Children: region/N for each contiguous parsed region (each
  Readable + Addressable).
- Metadata: entry point if a record-type-05 was present.
"""

from ..node import Node, Readable, Addressable
from ..db import NoMatch
from . import FormatNode, register_format


class IhexRegion(Node, Readable, Addressable):
    """A single contiguous parsed region from an ihex file."""

    def __init__(self, name, address, data: bytes):
        super().__init__(name)
        self._address = address
        self._data = data

    @property
    def size(self) -> int:
        return len(self._data)

    async def read(self, offset, size):
        if offset < 0 or offset > len(self._data):
            raise ValueError(f"offset {offset} out of range")
        avail = len(self._data) - offset
        n = min(size, avail)
        if n <= 0:
            return b""
        return self._data[offset:offset + n]

    @property
    def load_address(self) -> int:
        return self._address


@register_format("ihex",
                 exts=["hex", "ihex"],
                 mimes=["application/x-ihex"])
class Ihex(FormatNode):
    """Intel HEX file parser.

    Children:
    - data: IhexMerged with merged [min..max] view (0xFF-filled).
    - region/N: per-region IhexRegion (each Readable + Addressable).

    Metadata: entry (if present), region_count, min/max_address.
    """

    GAP_FILL = 0xFF

    async def start(self):
        text_bytes = await self._source.read(0, self._source.size)
        text = text_bytes.decode("ascii", errors="replace")
        regions, entry = self._parse(text)
        if not regions:
            raise NoMatch("ihex", "no data records")

        # region/N pre-populated children — one Addressable leaf per
        # contiguous parsed region. We deliberately do NOT add a
        # merged "data" view child: program_view walks every
        # Readable+Addressable descendant, and a merged view at the
        # same parent would double-count bytes during loading.
        # Callers that want a merged view can build it from the
        # regions (see acrobe.program_view).
        region_container = Node("region")
        for i, (addr, data) in enumerate(regions):
            region_container._child_attach(IhexRegion(str(i), addr, data))
        self._child_attach(region_container)

        min_addr = min(a for a, _ in regions)
        max_end = max(a + len(d) for a, d in regions)
        if entry is not None:
            self._metadata["entry"] = entry
        self._metadata["region_count"] = len(regions)
        self._metadata["min_address"] = min_addr
        self._metadata["max_address"] = max_end

    @staticmethod
    def _parse(text):
        """Parse ihex text into (regions, entry).

        regions: list of (address, bytes), each contiguous.
        entry: int or None.
        """
        regions = []
        current_address = None
        current_data = bytearray()
        extended = 0
        entry = None

        for line_no, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            if not line.startswith(":"):
                raise ValueError(
                    f"ihex line {line_no}: missing leading ':'")
            try:
                raw = bytes.fromhex(line[1:])
            except ValueError as exc:
                raise ValueError(
                    f"ihex line {line_no}: {exc}") from exc
            if len(raw) < 5:
                raise ValueError(
                    f"ihex line {line_no}: record too short")
            byte_count = raw[0]
            address = (raw[1] << 8) | raw[2]
            rtype = raw[3]
            data = raw[4:4 + byte_count]
            checksum = raw[4 + byte_count]
            if len(data) != byte_count:
                raise ValueError(
                    f"ihex line {line_no}: short data")
            computed = (~sum(raw[:4 + byte_count]) + 1) & 0xFF
            if checksum != computed:
                raise ValueError(
                    f"ihex line {line_no}: checksum mismatch")

            if rtype == 0x00:
                full = extended + address
                if (current_address is not None and
                        full == current_address + len(current_data)):
                    current_data.extend(data)
                else:
                    if current_data:
                        regions.append(
                            (current_address, bytes(current_data)))
                    current_address = full
                    current_data = bytearray(data)
            elif rtype == 0x01:
                break
            elif rtype == 0x02:
                extended = ((data[0] << 8) | data[1]) << 4
            elif rtype == 0x04:
                extended = ((data[0] << 8) | data[1]) << 16
            elif rtype == 0x05:
                entry = int.from_bytes(data, "big")
            else:
                raise ValueError(
                    f"ihex line {line_no}: unsupported "
                    f"record type 0x{rtype:02x}")

        if current_data:
            regions.append((current_address, bytes(current_data)))
        return regions, entry
