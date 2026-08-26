"""CMF / RBF-SDM format — Agilex, Stratix 10 (SDM-class devices).

The file is a sequence of 4 KiB-aligned sections. Each section begins
with a 32-bit magic (CMF or OTHER). CMF carries the SDM firmware;
OTHER carries Main / IO / CORE / HPIO / HPS / PR / CERT payloads. A
descriptor format byte in the first CMF section tells us the family
(Stratix 10 vs Agilex) and the variant (non-VAB / VAB / SDM 1.5).

This parser walks the section boundary structure only — it does not
verify CRCs, decode signature chains, or decrypt anything. Each
section is exposed as a Readable child under ``section/<i>`` for
inspection.
"""

import struct

from ....db import NoMatch
from ....node import Node, Readable
from ....util.endian import bitswap8
from ....vfs import FormatNode, register_format


RBF_SYNC_AGILEX = bytes([0x95, 0x48, 0x29, 0x62])
RBF_SYNC_AGILEX_SWAPPED = bitswap8(RBF_SYNC_AGILEX)

CMF_SECTION_MAGIC = 0x62294895
OTHER_SECTION_MAGIC = 0x97566593


# Descriptor format byte at +4 of a CMF section. Maps each known
# value to (family, variant).
_FAMILY_FROM_CMF_DESC = {
    0x00: ("STRATIX10", "non-VAB"),
    0x10: ("STRATIX10", "non-VAB"),
    0x20: ("AGILEX", "non-VAB"),
    0x30: ("AGILEX", "VAB"),
    0x40: ("AGILEX", "VAB"),
    0x50: ("AGILEX", "SDM1.5"),
}

# Device-type nibble at MSB of dword at +0x28 of an OTHER section.
_FAMILY_FROM_OTHER_DEV = {
    0: ("STRATIX10", None),
    1: ("STRATIX10", None),
    2: ("AGILEX", "non-VAB"),
    3: ("AGILEX", "VAB"),
    4: ("AGILEX", "VAB"),
    5: ("AGILEX", "SDM1.5"),
}


class CmfBitstream(Node, Readable):
    """Bitswap-normalised view of a raw CMF byte stream."""

    def __init__(self, name, source, swapped):
        super().__init__(name)
        self.__source = source
        self.__swapped = swapped

    @property
    def size(self) -> int:
        return self.__source.size

    async def read(self, offset, size):
        data = await self.__source.read(offset, size)
        if self.__swapped:
            data = bitswap8(data)
        return data

    @property
    def metadata(self) -> dict:
        return {"swapped": self.__swapped, **self._metadata}


class CmfSection(Node, Readable):
    """One 4KiB-aligned section of a CMF stream.

    Slices into the parent CMF view (`source`). The node's name is its
    index in the stream (``"0"``, ``"1"``, ...); the 4-character
    section tag (``"Main"``, ``"CMF"``, ``"CERT"``, ``"PR"``, ...) and
    decoded fields live in ``metadata``.
    """

    def __init__(self, name, source, offset, size):
        super().__init__(name)
        self.__source = source
        self.__offset = offset
        self.__size = size

    @property
    def size(self) -> int:
        return self.__size

    async def read(self, offset, size):
        if offset < 0 or offset > self.__size:
            raise ValueError(f"offset {offset} out of range")
        n = min(size, self.__size - offset)
        if n <= 0:
            return b""
        return await self.__source.read(self.__offset + offset, n)


def _walk_sections(blob: bytes):
    """Walk top-level CMF/OTHER sections in `blob`.

    Yields dicts with parsed fields per section. Raises ValueError on
    structural inconsistencies; the caller should let those propagate
    rather than swallow them, since at this point format dispatch has
    already committed to CMF based on the sync word.
    """
    if len(blob) == 0 or len(blob) % 4096 != 0:
        raise ValueError(
            f"file size {len(blob)} is not a positive multiple of 4 KiB")

    offset = 0
    index = 0
    while offset < len(blob):
        if offset + 0x10 > len(blob):
            raise ValueError(f"truncated section header at {offset}")
        magic = struct.unpack_from("<I", blob, offset)[0]
        if magic == CMF_SECTION_MAGIC:
            kind = "CMF"
            inner = 0x400
            desc_fmt = struct.unpack_from("<I", blob, offset + 4)[0]
        elif magic == OTHER_SECTION_MAGIC:
            kind = "OTHER"
            inner = 0
            desc_fmt = None
        else:
            raise ValueError(
                f"section {index}: unexpected magic 0x{magic:08x} "
                f"at offset {offset}")

        if offset + inner + 0x10 > len(blob):
            raise ValueError(
                f"section {index} ({kind}): descriptor header truncated")
        version = struct.unpack_from("<I", blob, offset + inner + 4)[0]
        size = struct.unpack_from("<I", blob, offset + inner + 8)[0]
        name_bytes = blob[offset + inner + 12: offset + inner + 16]
        name = name_bytes.rstrip(b"\x00").decode("ascii", errors="replace")

        if size < 2 * 4096 or size % 4096 != 0:
            raise ValueError(
                f"section {index} ({name!r}): size {size} not >= 8 KiB "
                f"and 4 KiB-aligned")
        if offset + size > len(blob):
            raise ValueError(
                f"section {index} ({name!r}): extends past file end "
                f"({offset + size} > {len(blob)})")

        entry = {
            "index": index,
            "kind": kind,
            "offset": offset,
            "size": size,
            "name": name,
            "version": version,
        }

        if kind == "CMF":
            entry["descriptor_format"] = desc_fmt
            fam = _FAMILY_FROM_CMF_DESC.get(desc_fmt)
            if fam is not None:
                entry["family"], entry["variant"] = fam
            if desc_fmt in (0x20, 0x30, 0x40, 0x50):
                sig_block = struct.unpack_from(
                    "<I", blob, offset + 0xC4)[0]
                if 4096 <= sig_block <= 36864 and sig_block % 4096 == 0:
                    entry["signature_block_size"] = sig_block
        else:  # OTHER
            if name == "CERT":
                flag = struct.unpack_from("<I", blob, offset + 0x24)[0]
                entry["cert_flag"] = flag
                if flag == 2:
                    entry["cert_type"] = "RMA"
                elif (flag & 0x3000000) != 0 and (flag & 0xFCFFFFFF) == 0:
                    entry["cert_type"] = "DEBUG"
                else:
                    entry["cert_type"] = "unknown"
            else:
                device_type = (
                    struct.unpack_from("<I", blob, offset + 0x28)[0] >> 24)
                entry["device_type"] = device_type
                fam = _FAMILY_FROM_OTHER_DEV.get(device_type)
                if fam is not None:
                    entry["family"], entry["variant"] = fam
                if device_type in (2, 3, 4, 5):
                    sig_field = (struct.unpack_from(
                        "<I", blob, offset + 0x10)[0] >> 24) & 0x0F
                    entry["signature_block_size"] = (sig_field + 1) * 4096
                key_select = struct.unpack_from(
                    "<I", blob, offset + 0xFF0)[0]
                if key_select & 0x80000000:
                    entry["encrypted"] = True
                    entry["key_index"] = key_select & 0xFF
                else:
                    entry["encrypted"] = False

        yield entry
        offset += size
        index += 1

    if offset != len(blob):
        raise ValueError(
            f"section walk ended at {offset}, expected {len(blob)}")


@register_format("altera_cmf",
                 exts=["rbf"],
                 mimes=["application/x-altera-cmf"])
class Cmf(FormatNode):
    """CMF / SDM-class RBF container.

    Detects bitswap from the sync word (Agilex/Stratix-10 SDM
    sync = 0x95482962, i.e. CMF section magic seen as bytes), then
    walks the 4 KiB-aligned section table. Children:

    - ``bitstream`` — canonical-byte view of the whole file.
    - ``section/<i>`` — one ``CmfSection`` per top-level section.

    Top-level metadata records the family and variant inferred from
    the first CMF section's descriptor format. Section walking is
    pure observation; CRCs, signatures, and encrypted payloads are
    not verified or decrypted.

    ``swap=true|false|auto`` overrides bitswap detection.
    """

    __CMF_SYNCS = [
        (RBF_SYNC_AGILEX, False),
        (RBF_SYNC_AGILEX_SWAPPED, True),
    ]

    def __init__(self, name, source):
        super().__init__(name, source)
        self.__swap_override = None  # None=auto, True/False=forced

    def option_set(self, key, value):
        if key == "swap":
            v = value.lower()
            if v in ("true", "1", "yes"):
                self.__swap_override = True
            elif v in ("false", "0", "no"):
                self.__swap_override = False
            elif v == "auto":
                self.__swap_override = None
            else:
                raise ValueError(
                    f"{self.fqdn}: swap must be true/false/auto, "
                    f"got {value!r}")

    async def start(self):
        if self.__swap_override is not None:
            swapped = self.__swap_override
            sync_family = "user-specified"
        else:
            head = await self._source.read(
                0, min(self._source.size, 4096))
            best = None
            for sync, sw in self.__CMF_SYNCS:
                pos = head.find(sync)
                if pos >= 0 and (best is None or pos < best[0]):
                    best = (pos, sw, sync)
            if best is None:
                raise NoMatch(
                    "altera_cmf",
                    "no recognised sync word in first 4KiB")
            swapped = best[1]
            sync_family = best[2].hex()
        self.metadata["sync_family"] = sync_family

        view = CmfBitstream("bitstream", self._source, swapped)
        self.child_add(view)

        blob = await view.read(0, view.size)
        entries = list(_walk_sections(blob))

        container = Node("section")
        family = None
        variant = None
        for entry in entries:
            section = CmfSection(
                str(entry["index"]), view, entry["offset"], entry["size"])
            section.metadata.update({
                k: v for k, v in entry.items()
                if k not in ("index", "offset", "size")
            })
            container.child_add(section)
            if family is None and entry.get("family") is not None:
                family = entry["family"]
                variant = entry.get("variant")
        self.child_add(container)

        self.metadata.update({
            "family": family,
            "variant": variant,
            "section_count": len(entries),
            "section_names": [e["name"] for e in entries],
        })
