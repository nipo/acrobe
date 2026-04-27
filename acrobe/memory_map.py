"""In-memory addressed-bytes representation.

A `MemoryMap` is a list of (address, bytes) chunks. Used for:

- Building the picture of "what to write to a target" from a VFS
  Node subtree (`MemoryMap.from_node`).
- Receiving "what was on the chip" back from a target read.
- Output formatters (`save_bin`, `save_hex`).

This module replaces the legacy `acrobe.loadable` package
(Program/Segment). Chunks are plain `(int, bytes)` tuples — no
Segment class — to keep the API minimal.
"""

import os

from .node import Node, Readable, Addressable


def addressable_descendants(root: Node):
    """Yield every descendant of `root` (including `root` itself)
    that implements both `Readable` and `Addressable`."""
    stack = [root]
    while stack:
        node = stack.pop()
        if isinstance(node, Readable) and isinstance(node, Addressable):
            yield node
        # Visit children regardless — an addressable parent may
        # have addressable descendants (e.g. ELF section + symbol).
        stack.extend(reversed(node._children))


class MemoryMap:
    """Sorted/sortable list of (address, bytes) chunks.

    Iteration yields `(address, data)` tuples. Construct empty,
    via `from_node` (async, walking a VFS subtree), or by appending.
    """

    def __init__(self, chunks=()):
        self.chunks = [(int(a), bytes(d)) for a, d in chunks]
        self.info = {}
        self.sources = []

    # --- Construction ---

    @classmethod
    async def from_node(cls, node: Node, *, offset: int = 0) -> "MemoryMap":
        """Walk a started Node subtree and collect addressable
        leaves into a MemoryMap. `offset` is added to every chunk's
        address.

        Fallback: when the subtree has no Addressable+Readable leaf
        but `node` itself implements Readable (e.g. a bitstream
        view), the node's bytes are emitted as a single chunk at
        `offset`. This makes inspection commands (hexdump, dump)
        work on any Readable resource without requiring the user
        to wrap it in an Addressable like `as(type=bin)`.

        Metadata from `node.metadata` is merged into `info`; the
        node's path is recorded in `sources`.
        """
        m = cls()
        for d in addressable_descendants(node):
            data = await d.read(0, d.size)
            m.chunks.append((d.load_address + offset, bytes(data)))
        if not m.chunks and isinstance(node, Readable):
            data = await node.read(0, node.size)
            m.chunks.append((offset, bytes(data)))
        try:
            m.sources.append(node.path)
        except AttributeError:
            m.sources.append(node.name)
        m.info.update(node.metadata)
        return m

    def append(self, address, data):
        self.chunks.append((int(address), bytes(data)))

    # --- Sequence-like ---

    def __len__(self):
        return len(self.chunks)

    def __iter__(self):
        return iter(self.chunks)

    def __getitem__(self, i):
        return self.chunks[i]

    def __bool__(self):
        return bool(self.chunks)

    @property
    def size(self) -> int:
        return sum(len(d) for _, d in self.chunks)

    @property
    def address(self) -> int:
        return min(a for a, _ in self.chunks)

    @property
    def end(self) -> int:
        return max(a + len(d) for a, d in self.chunks)

    # --- Composition ---

    def __add__(self, other: "MemoryMap") -> "MemoryMap":
        out = MemoryMap()
        out.chunks = list(self.chunks) + list(other.chunks)
        out.info = {**self.info, **other.info}
        out.sources = list(self.sources) + list(other.sources)
        return out

    def __iadd__(self, other: "MemoryMap") -> "MemoryMap":
        self.chunks.extend(other.chunks)
        self.info.update(other.info)
        self.sources.extend(other.sources)
        return self

    # --- Slicing & merging ---

    def within(self, begin: int, end: int) -> "MemoryMap":
        """Return a new MemoryMap clipped to [begin, end)."""
        out = MemoryMap()
        out.info = dict(self.info)
        out.sources = list(self.sources)
        for addr, data in self.chunks:
            seg_end = addr + len(data)
            if seg_end <= begin or addr >= end:
                continue
            start = max(addr, begin)
            stop = min(seg_end, end)
            offset = start - addr
            length = stop - start
            out.chunks.append((start, bytes(data[offset:offset + length])))
        return out

    def simplified(self) -> "MemoryMap":
        """Return a new MemoryMap with overlapping/adjacent chunks
        merged. Later chunks overwrite earlier ones in overlap
        regions (matching legacy Program semantics)."""
        if not self.chunks:
            out = MemoryMap()
            out.info = dict(self.info)
            out.sources = list(self.sources)
            return out
        sorted_chunks = sorted(self.chunks, key=lambda c: c[0])
        out = MemoryMap()
        out.info = dict(self.info)
        out.sources = list(self.sources)
        cur_addr, cur = sorted_chunks[0]
        cur_data = bytearray(cur)
        for addr, data in sorted_chunks[1:]:
            cur_end = cur_addr + len(cur_data)
            if addr <= cur_end:
                new_end = max(cur_end, addr + len(data))
                if new_end > cur_end:
                    cur_data.extend(b"\x00" * (new_end - cur_end))
                offset = addr - cur_addr
                cur_data[offset:offset + len(data)] = data
            else:
                out.chunks.append((cur_addr, bytes(cur_data)))
                cur_addr = addr
                cur_data = bytearray(data)
        out.chunks.append((cur_addr, bytes(cur_data)))
        return out

    def paged(self, page_size: int, fill: bytes = b"\xff") -> "MemoryMap":
        """Page-align chunks, padding with `fill`."""
        out = MemoryMap()
        out.info = dict(self.info)
        out.sources = list(self.sources)
        for addr, data in self.simplified():
            page_start = (addr // page_size) * page_size
            page_end = ((addr + len(data) + page_size - 1)
                        // page_size) * page_size
            total = page_end - page_start
            page_data = bytearray(
                fill * ((total + len(fill) - 1) // len(fill)))[:total]
            offset = addr - page_start
            page_data[offset:offset + len(data)] = data
            out.chunks.append((page_start, bytes(page_data)))
        return out

    def read(self, address: int, size: int) -> bytes:
        """Read `size` bytes starting at `address`, filling gaps with
        zeros. Useful for materialising a contiguous view."""
        result = bytearray(size)
        for addr, data in self.chunks:
            seg_end = addr + len(data)
            start = max(address, addr)
            stop = min(address + size, seg_end)
            if start >= stop:
                continue
            src_offset = start - addr
            dst_offset = start - address
            length = stop - start
            result[dst_offset:dst_offset + length] = (
                data[src_offset:src_offset + length])
        return bytes(result)


# --- Output formatters ---

def save_bin(m: MemoryMap, filename: str) -> None:
    """Write a MemoryMap to a flat binary file. The map must have
    at most one chunk after simplified(); otherwise raises."""
    s = m.simplified()
    if len(s) == 0:
        with open(filename, "wb"):
            pass
        return
    if len(s) > 1:
        raise ValueError(
            "MemoryMap has multiple chunks; flat .bin needs a "
            "single contiguous chunk (use within() or simplified())")
    with open(filename, "wb") as f:
        f.write(s.chunks[0][1])


def save_hex(m: MemoryMap, filename: str) -> None:
    """Write a MemoryMap as Intel HEX (record types 0x00, 0x04, 0x01)."""
    def hex_record(rtype: int, address: int, data: bytes = b"") -> str:
        record = bytearray()
        record.append(len(data))
        record.append((address >> 8) & 0xFF)
        record.append(address & 0xFF)
        record.append(rtype)
        record.extend(data)
        checksum = (~sum(record) + 1) & 0xFF
        record.append(checksum)
        return ":" + record.hex().upper()

    lines = []
    last_upper = None
    for addr, data in sorted(m.simplified().chunks, key=lambda c: c[0]):
        offset = 0
        while offset < len(data):
            full_addr = addr + offset
            upper = (full_addr >> 16) & 0xFFFF
            if upper != last_upper:
                lines.append(hex_record(
                    0x04, 0x0000, upper.to_bytes(2, "big")))
                last_upper = upper
            lower = full_addr & 0xFFFF
            chunk_size = min(16, len(data) - offset)
            lines.append(hex_record(
                0x00, lower, data[offset:offset + chunk_size]))
            offset += chunk_size
    lines.append(hex_record(0x01, 0x0000))
    with open(filename, "w") as f:
        f.write("\n".join(lines) + "\n")


def save(m: MemoryMap, filename: str) -> None:
    """Pick output format from filename extension (.bin or .hex)."""
    _, ext = os.path.splitext(filename)
    ext = ext.lstrip(".").lower()
    if ext == "bin":
        save_bin(m, filename)
    elif ext in ("hex", "ihex"):
        save_hex(m, filename)
    else:
        raise ValueError(f"Unknown output extension: {ext!r}")
