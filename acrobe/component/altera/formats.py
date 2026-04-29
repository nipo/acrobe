"""Altera bitstream format parsers (POF, SOF, RBF) as VFS Nodes.

Migrated from the old `acrobe.loadable.altera` to live next to the
Altera hardware components.

Path semantics:

- `file.pof/partition/N`           → PofPartition (raw partition bytes
                                     from the POF flash image).
- `file.pof/partition/N/as(type=rbf)/bitstream`
                                   → RBF view (JTAG-bit-order with
                                     auto-bitswap).
- `file.rbf/as(type=rbf)/bitstream` → same view, applied to a raw
                                     .rbf file.
- `file.sof/config_data`           → raw CONFIG_DATA section bytes.

`acrobe.component.altera.__init__` imports this module so the
registries are populated when Altera support is loaded.
"""

import struct
import gzip
import enum

from ...db import NoMatch
from ...node import Node, Readable, Addressable
from ...util.endian import bitswap8
from ...vfs import (
    FormatNode,
    register_format,
    register_magic,
)


# --- Magic constants ---

POF_MAGIC = b"POF\x00"
SOF_MAGIC = b"SOF\x00"

# --- Section tags shared by POF and SOF ---

class SofSection(enum.IntEnum):
    TOOL = 0x01
    DEVICE = 0x02  # SOF: target device; POF: flash device
    DESIGN = 0x03
    END = 0x08
    CONFIG_DATA = 0x11
    CONFIG_INFO = 0x12
    METADATA = 0x13
    CHECKSUM = 0x15
    BOOT_INFO = 0x1a
    EMBEDDED = 0x24
    BOOTLOADER_DATA = 0x3e  # HPS bootloader payload (Agilex+HPS designs)


# --- Helpers ---

def _parse_sections(blob: bytes):
    """Iterate over POF/SOF tag-length-value sections.

    Yields (tag, flags, data). Stops on END tag or when blob ends."""
    idx = 12  # skip magic(4) + version(4) + count(4)
    while idx + 6 <= len(blob):
        tag = blob[idx]
        flags = blob[idx + 1]
        length = struct.unpack_from("<I", blob, idx + 2)[0]
        data = blob[idx + 6:idx + 6 + length]
        idx += 6 + length
        yield tag, flags, data
        if tag == SofSection.END:
            break


def _decode_string(data: bytes) -> str:
    return data.rstrip(b"\x00").decode("ascii", errors="replace")


def _decode_bootloader_data(section: bytes) -> bytes:
    """Decode a SOF Bootloader Data section (tag 0x3e) to its original
    payload bytes (typically the contents of an ihex file passed to
    Quartus' `quartus_pfg -c X.sof Y.sof -o hps_path=Z.hex`).

    Empirically reverse-engineered against quartus_pfg output; matches
    the original ihex byte-for-byte.

    Section layout
    --------------
    - 16-byte file-level header (opaque metadata).
    - One or more blocks holding the payload. Each pair of consecutive
      bytes encodes one payload byte: one of the two bytes is the
      constant marker 0x11, the other is the data byte. Which slot
      holds the data alternates per block; the parity is detected from
      the first two words of the block.
    - Block 0's first decoded byte is a 0x00 sentinel (stripped here).
      To accommodate the sentinel, block 0 carries one extra payload
      byte as a lone byte (no marker) right before the first
      sub-header.
    - 11-byte sub-header between blocks, signature
      ``34 12 00 00 NN 00 12`` followed by a u32-LE giving the next
      block's decoded-byte size. NN is a 1-based block index.
    - Last block runs to the end of the section.
    """
    if len(section) < 16:
        raise ValueError("bootloader-data section too short for 16-byte header")
    body = section[16:]

    # Locate sub-headers by signature.
    sub_positions = []
    i = 0
    while i + 11 <= len(body):
        if (body[i:i + 4] == b"\x34\x12\x00\x00"
                and body[i + 5] == 0x00
                and body[i + 6] == 0x12):
            sub_positions.append(i)
            i += 11
        else:
            i += 1

    def detect_parity(off: int) -> int:
        """Return 1 if data is at low byte (marker at high), 0 otherwise.
        Uses two consecutive words at `off`."""
        if off + 4 > len(body):
            raise ValueError(f"can't detect parity at body[{off:#x}]: short")
        a0, b0 = body[off], body[off + 1]
        a1, b1 = body[off + 2], body[off + 3]
        if b0 == 0x11 and b1 == 0x11:
            return 1
        if a0 == 0x11 and a1 == 0x11:
            return 0
        raise ValueError(f"can't detect parity at body[{off:#x}]")

    out = bytearray()

    def decode_words(start: int, end: int, parity: int) -> None:
        for j in range(start, end - 1, 2):
            a, b = body[j], body[j + 1]
            if parity == 1 and b == 0x11:
                out.append(a)
            elif parity == 0 and a == 0x11:
                out.append(b)
            else:
                raise ValueError(
                    f"bootloader data: bad pair at body[{j:#x}]: "
                    f"{a:#04x} {b:#04x} (parity {parity})")

    cursor = 0
    for idx, sub_pos in enumerate(sub_positions):
        parity = detect_parity(cursor)
        if idx == 0:
            decode_words(cursor, sub_pos - 1, parity)
            out.append(body[sub_pos - 1])  # lone byte (only after block 0)
        else:
            decode_words(cursor, sub_pos, parity)
        cursor = sub_pos + 11
    if cursor < len(body):
        parity = detect_parity(cursor)
        decode_words(cursor, len(body), parity)

    # Drop the leading sentinel byte added by the encoder.
    return bytes(out[1:])


def _parse_boot_info(data: bytes):
    """Parse BOOT_INFO string into partition entries.

    Format: "NAME ADDR SIZE;NAME ADDR SIZE;..."
    Returns list of (name, address, size).
    """
    text = _decode_string(data)
    partitions = []
    for entry in text.split(";"):
        tokens = entry.split()
        if len(tokens) >= 3:
            try:
                name = tokens[0]
                addr = int(tokens[1], 16)
                size = int(tokens[2], 16)
                partitions.append((name, addr, size))
            except ValueError:
                continue
    return partitions


# --- PofPartition ---

class PofPartition(Node, Readable, Addressable):
    """One partition inside a POF flash image.

    Bytes are read directly from the source (the POF's parent
    Readable) at the partition's flash offset.
    """

    def __init__(self, name, source, flash_address, size):
        super().__init__(name)
        self._source = source
        self._flash_address = flash_address
        self._size = size

    @property
    def size(self) -> int:
        return self._size

    @property
    def load_address(self) -> int:
        return self._flash_address

    async def read(self, offset, size):
        if offset < 0 or offset > self._size:
            raise ValueError(f"offset {offset} out of range")
        avail = self._size - offset
        n = min(size, avail)
        if n <= 0:
            return b""
        return await self._source.read(self._flash_address + offset, n)

    @property
    def metadata(self) -> dict:
        return {
            "flash_address": self._flash_address,
            "size": self._size,
            **self._metadata,
        }


# --- POF ---

@register_format("altera_pof",
                 exts=["pof"],
                 mimes=["application/x-altera-pof"])
class Pof(FormatNode):
    """POF flash-image container.

    POF stores tool/design/flash strings in TLV sections, plus a
    BOOT_INFO section listing partitions inside the embedded flash
    image (CONFIG_DATA section). Each partition becomes a
    PofPartition child whose read() pulls bytes from the source's
    CONFIG_DATA region.
    """

    def __init__(self, name, source):
        super().__init__(name, source)
        self.tool = None
        self.flash = None
        self.design = None

    async def start(self):
        # Read whole POF blob (POF files are small enough — KBs
        # to a few MB; they're flash images for typical FPGAs).
        blob = await self._source.read(0, self._source.size)
        if blob[:4] != POF_MAGIC:
            raise NoMatch("altera_pof", "magic")

        sections = list(_parse_sections(blob))

        # CONFIG_DATA byte range within the POF blob.
        config_offset = None
        config_size = None
        boot_info = None
        idx = 12
        for tag, flags, data in sections:
            if tag == SofSection.CONFIG_DATA:
                # data was sliced from blob; recompute its absolute
                # offset within the POF blob for source-relative
                # reads in PofPartition.
                # Each section header is 6 bytes; data follows.
                config_offset = idx + 6
                config_size = len(data)
                config_data = data
            if tag == SofSection.BOOT_INFO:
                boot_info = _parse_boot_info(data)
            if tag == SofSection.TOOL:
                self.tool = _decode_string(data)
            if tag == SofSection.DEVICE:
                self.flash = _decode_string(data)
            if tag == SofSection.DESIGN:
                self.design = _decode_string(data)
            idx += 6 + len(data)

        if config_offset is None:
            raise ValueError(f"{self.fqdn}: POF has no CONFIG_DATA section")

        self._metadata.update({
            "tool": self.tool,
            "flash": self.flash,
            "design": self.design,
            "config_data_offset": config_offset,
            "config_data_size": config_size,
        })

        # Partitions live inside CONFIG_DATA. Their addresses in
        # BOOT_INFO are relative to the start of CONFIG_DATA, so
        # source-absolute offset is config_offset + partition addr.
        # If no BOOT_INFO, expose CONFIG_DATA as a single
        # "partition/0" covering the bitstream after a 12-byte header.
        if boot_info is None:
            partition = PofPartition(
                "0", self._source,
                flash_address=config_offset + 12,
                size=config_size - 12,
            )
            container = self._make_partition_container([partition])
            self._child_attach(container)
            return

        # Sort by address; truncate ranges so they don't overlap.
        sorted_bi = sorted(boot_info, key=lambda p: p[1])
        partitions = []
        for i, (pname, paddr, psize) in enumerate(sorted_bi):
            if i + 1 < len(sorted_bi):
                region_end = min(paddr + psize, sorted_bi[i + 1][1])
            else:
                region_end = min(paddr + psize, config_size)
            region_size = region_end - paddr
            if region_size <= 0:
                continue
            partition = PofPartition(
                str(i), self._source,
                flash_address=config_offset + paddr,
                size=region_size,
            )
            partition._metadata["partition_name"] = pname
            partitions.append(partition)

        if not partitions:
            raise ValueError(f"{self.fqdn}: POF BOOT_INFO has no partitions")

        container = self._make_partition_container(partitions)
        self._child_attach(container)

    def _make_partition_container(self, partitions):
        """Wrap partitions under a `partition` namespace Node."""
        container = Node("partition")
        for p in partitions:
            container._child_attach(p)
        return container


@register_magic
def _pof_magic(head: bytes):
    if head[:4] == POF_MAGIC:
        return "altera_pof"
    return None


# --- SOF ---

class SofConfigData(Node, Readable):
    """Raw CONFIG_DATA section of a SOF.

    Format-internal Quartus frame data — NOT RBF bitstream. Useful
    for inspection; converting to RBF requires Quartus internals.
    """

    def __init__(self, name, source, offset, size):
        super().__init__(name)
        self._source = source
        self._offset = offset
        self._size = size

    @property
    def size(self) -> int:
        return self._size

    async def read(self, offset, size):
        if offset < 0 or offset > self._size:
            raise ValueError(f"offset {offset} out of range")
        avail = self._size - offset
        n = min(size, avail)
        if n <= 0:
            return b""
        return await self._source.read(self._offset + offset, n)


class SofBootloaderData(Node, Readable):
    """Decoded HPS bootloader payload from a SOF Bootloader Data section.

    Backed by an in-memory bytes buffer holding the framing-stripped
    payload. The bytes match the original ihex input that was inserted
    via ``quartus_pfg -c X.sof Y.sof -o hps_path=Z.hex``.
    """

    def __init__(self, name, data: bytes):
        super().__init__(name)
        self._data = data

    @property
    def size(self) -> int:
        return len(self._data)

    async def read(self, offset, size):
        if offset < 0 or offset > len(self._data):
            raise ValueError(f"offset {offset} out of range")
        n = min(size, len(self._data) - offset)
        if n <= 0:
            return b""
        return self._data[offset:offset + n]


@register_format("altera_sof",
                 exts=["sof"],
                 mimes=["application/x-altera-sof"])
class Sof(FormatNode):
    """SOF (Quartus output) container.

    Auto-decompresses gzip-wrapped SOF (Agilex) before parsing.
    Exposes a `config_data` child holding raw CONFIG_DATA bytes.
    Metadata: tool, device, design, usercode.
    """

    def __init__(self, name, source):
        super().__init__(name, source)
        self.tool = None
        self.device = None
        self.design = None
        self.usercode = None

    async def start(self):
        raw = await self._source.read(0, self._source.size)
        # Detect gzip wrapper. If present we operate on the
        # decompressed copy in memory and lose source-relative
        # offsets — config_data ends up backed by an in-memory
        # source instead of the original.
        if raw[:2] == b"\x1f\x8b":
            blob = gzip.decompress(raw)
            self._metadata["gzipped"] = True
            backing = _BytesReadable(blob)
        else:
            blob = raw
            backing = self._source

        if blob[:4] != SOF_MAGIC:
            raise NoMatch("altera_sof", "magic")

        idx = 12
        config_offset = None
        config_size = None
        bootloader_bytes = None
        for tag, flags, data in _parse_sections(blob):
            if tag == SofSection.CONFIG_DATA:
                config_offset = idx + 6
                config_size = len(data)
            elif tag == SofSection.TOOL:
                self.tool = _decode_string(data)
            elif tag == SofSection.DEVICE:
                self.device = _decode_string(data)
            elif tag == SofSection.DESIGN:
                self.design = _decode_string(data)
            elif tag == SofSection.METADATA and len(data) >= 16:
                self.usercode = struct.unpack_from("<I", data, 12)[0]
            elif tag == SofSection.BOOTLOADER_DATA:
                # Reconstruct the original bootloader payload (matches
                # the ihex passed to `quartus_pfg -o hps_path=...`).
                bootloader_bytes = _decode_bootloader_data(data)
            idx += 6 + len(data)

        self._metadata.update({
            "tool": self.tool,
            "device": self.device,
            "design": self.design,
            "usercode": self.usercode,
        })

        # Older Quartus SOFs carry their bitstream data in CONFIG_DATA
        # (tag 0x11); Agilex SOFs use EMBEDDED (tag 0x24) instead and
        # have no CONFIG_DATA at all. Expose `config_data` only when
        # present rather than failing parsing for the Agilex shape.
        if config_offset is not None:
            self._child_attach(SofConfigData(
                "config_data", backing, config_offset, config_size))

        if bootloader_bytes is not None:
            self._metadata["bootloader_size"] = len(bootloader_bytes)
            self._child_attach(SofBootloaderData("bootloader", bootloader_bytes))


@register_magic
def _sof_magic(head: bytes):
    if head[:4] == SOF_MAGIC:
        return "altera_sof"
    if head[:2] == b"\x1f\x8b":
        # Could be a gzip-wrapped SOF, but also a generic gzip
        # archive. We can't decide without decompressing — leave
        # to other detectors / explicit `as(type=...)`.
        return None
    return None


# --- RBF ---

class RbfBitstream(Node, Readable):
    """JTAG-bit-order view of a raw RBF byte stream.

    Detects bitswap from the position of the sync word (0x6af7f7f7).
    If the sync appears bitswapped in the source, every byte read
    is swapped on the way out.
    """

    def __init__(self, name, source, swapped):
        super().__init__(name)
        self._source = source
        self._swapped = swapped

    @property
    def size(self) -> int:
        return self._source.size

    async def read(self, offset, size):
        data = await self._source.read(offset, size)
        if self._swapped:
            data = bitswap8(data)
        return data

    @property
    def metadata(self) -> dict:
        return {"swapped": self._swapped, **self._metadata}


@register_format("altera_rbf",
                 exts=["rbf"],
                 mimes=["application/x-altera-rbf"])
class Rbf(FormatNode):
    """RBF format. Adds a single `bitstream` child exposing the
    canonical-byte-order view of `source`.

    Detects which device family wrote the file by scanning the
    first 4 KiB for any known sync word:

    - Cyclone classic / older Stratix / Arria: 0x6af7f7f7

    Each sync also has a bit-swapped counterpart (occurs when
    bytes were shifted through JTAG and re-packed in the
    alternate bit order). When the bit-swapped form is found,
    every byte read is swapped on the way out so the consumer
    sees canonical bytes.

    `swap=true|false|auto` can force the swap behaviour; default
    is `auto` (driven by the detected sync). If no known sync is
    present, parsing fails — the caller can either pick the
    right format or pass `swap=` to bypass detection.
    """

    # Cyclone-classic / older Stratix / Arria sync.
    RBF_SYNC = bytes([0x6a, 0xf7, 0xf7, 0xf7])
    _RBF_SYNCS = [
        (RBF_SYNC, False),
        (bitswap8(RBF_SYNC), True),
    ]

    def __init__(self, name, source):
        super().__init__(name, source)
        self._swap_override = None  # None=auto, True/False=forced

    def option_set(self, key, value):
        if key == "swap":
            v = value.lower()
            if v in ("true", "1", "yes"):
                self._swap_override = True
            elif v in ("false", "0", "no"):
                self._swap_override = False
            elif v == "auto":
                self._swap_override = None
            else:
                raise ValueError(
                    f"{self.fqdn}: swap must be true/false/auto, "
                    f"got {value!r}")
            return
        super().option_set(key, value)

    async def start(self):
        if self._swap_override is not None:
            swapped = self._swap_override
            family = "user-specified"
        else:
            head = await self._source.read(
                0, min(self._source.size, 4096))
            # Take the EARLIEST-matching sync from any family —
            # avoids false positives from later byte sequences.
            best = None
            for sync, sw in self._RBF_SYNCS:
                pos = head.find(sync)
                if pos >= 0 and (best is None or pos < best[0]):
                    best = (pos, sw, sync)
            if best is None:
                raise NoMatch(
                    "altera_rbf",
                    "no recognised sync word in first 4KiB")
            swapped = best[1]
            family = best[2].hex()
        self._metadata["sync_family"] = family
        view = RbfBitstream("bitstream", self._source, swapped)
        self._child_attach(view)

class CmfBitstream(Node, Readable):
    """JTAG-bit-order view of a raw CMF byte stream.

    Detects bitswap from the position of the sync word (0x6af7f7f7).
    If the sync appears bitswapped in the source, every byte read
    is swapped on the way out.
    """

    def __init__(self, name, source, swapped):
        super().__init__(name)
        self._source = source
        self._swapped = swapped

    @property
    def size(self) -> int:
        return self._source.size

    async def read(self, offset, size):
        data = await self._source.read(offset, size)
        if self._swapped:
            data = bitswap8(data)
        return data

    @property
    def metadata(self) -> dict:
        return {"swapped": self._swapped, **self._metadata}


@register_format("altera_cmf",
                 exts=["rbf"],
                 mimes=["application/x-altera-cmf"])
class Cmf(FormatNode):
    """CMF format. Adds a single `bitstream` child exposing the
    canonical-byte-order view of `source`.

    Detects which device family wrote the file by scanning the
    first 4 KiB for any known sync word:

    - Agilex / SDM: 0x95482962

    Each sync also has a bit-swapped counterpart (occurs when
    bytes were shifted through JTAG and re-packed in the
    alternate bit order). When the bit-swapped form is found,
    every byte read is swapped on the way out so the consumer
    sees canonical bytes.

    `swap=true|false|auto` can force the swap behaviour; default
    is `auto` (driven by the detected sync). If no known sync is
    present, parsing fails — the caller can either pick the
    right format or pass `swap=` to bypass detection.
    """

    # Agilex / SDM sync — appears at offset 0 of Agilex CMFs.
    CMF_SYNC = bytes([0x95, 0x48, 0x29, 0x62])
    _CMF_SYNCS = [
        (CMF_SYNC, False),
        (bitswap8(CMF_SYNC), True),
        ]

    def __init__(self, name, source):
        super().__init__(name, source)
        self._swap_override = None  # None=auto, True/False=forced

    def option_set(self, key, value):
        if key == "swap":
            v = value.lower()
            if v in ("true", "1", "yes"):
                self._swap_override = True
            elif v in ("false", "0", "no"):
                self._swap_override = False
            elif v == "auto":
                self._swap_override = None
            else:
                raise ValueError(
                    f"{self.fqdn}: swap must be true/false/auto, "
                    f"got {value!r}")
            return
        super().option_set(key, value)

    async def start(self):
        if self._swap_override is not None:
            swapped = self._swap_override
            family = "user-specified"
        else:
            head = await self._source.read(
                0, min(self._source.size, 4096))
            # Take the EARLIEST-matching sync from any family —
            # avoids false positives from later byte sequences.
            best = None
            for sync, sw in self._CMF_SYNCS:
                pos = head.find(sync)
                if pos >= 0 and (best is None or pos < best[0]):
                    best = (pos, sw, sync)
            if best is None:
                raise NoMatch(
                    "altera_cmf",
                    "no recognised sync word in first 4KiB")
            swapped = best[1]
            family = best[2].hex()
        self._metadata["sync_family"] = family
        view = CmfBitstream("bitstream", self._source, swapped)
        self._child_attach(view)

# Note: RBF magic detection is unreliable from raw bytes alone —
# the sync word can appear anywhere in the first KB. We don't
# register a generic magic detector for RBF; users either rely on
# the .rbf extension or use `as(type=rbf)` explicitly.


# --- Helpers ---

class _BytesReadable(Readable):
    """In-memory Readable backing for decompressed buffers."""

    def __init__(self, data: bytes):
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
