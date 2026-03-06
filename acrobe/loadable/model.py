import os

from ..db import Db, NoMatch


class Segment:
    def __init__(self, address=0, data=b"", name=None):
        self.data = bytearray(data)
        self.address = address
        self.name = name

    def __getitem__(self, index):
        return self.data[index]

    def __setitem__(self, index, data):
        self.data[index] = data

    def __len__(self):
        return len(self.data)

    @property
    def end(self):
        return self.address + len(self.data)

    def __lt__(self, other):
        return self.address < other.address

    def __eq__(self, other):
        return self.address == other.address

    def __str__(self):
        s = f"<0x{self.address:08x}:0x{self.end:08x} ({len(self)} bytes)"
        if self.name:
            s += f" '{self.name}'"
        return s + ">"


class Program:
    ext_db = Db("loadable_ext")
    format_db = Db("loadable_format")

    def __init__(self, source=None):
        self.segments = []
        self.info = {}
        self.sources = []
        if source is not None:
            self.sources.append(source)

    def append(self, seg):
        self.segments.append(seg)

    def segment_at(self, addr):
        for seg in self.segments:
            if seg.address <= addr < seg.end:
                return seg
        return None

    def within(self, begin, end):
        p = Program()
        for seg in self.segments:
            # Skip segments entirely outside the range
            if seg.end <= begin or seg.address >= end:
                continue
            # Clip to range
            start = max(seg.address, begin)
            stop = min(seg.end, end)
            offset = start - seg.address
            length = stop - start
            p.append(Segment(start, seg.data[offset:offset + length]))
        return p

    def read(self, address, size):
        result = bytearray(size)
        for seg in self.segments:
            # Overlap between [address, address+size) and [seg.address, seg.end)
            start = max(address, seg.address)
            stop = min(address + size, seg.end)
            if start >= stop:
                continue
            src_offset = start - seg.address
            dst_offset = start - address
            length = stop - start
            result[dst_offset:dst_offset + length] = seg.data[src_offset:src_offset + length]
        return bytes(result)

    def __getitem__(self, index):
        return self.segments[index]

    def __len__(self):
        return len(self.segments)

    def __iter__(self):
        return iter(self.segments)

    @property
    def size(self):
        return sum(len(s) for s in self.segments)

    @property
    def address(self):
        return min(seg.address for seg in self.segments)

    @property
    def end(self):
        return max(seg.end for seg in self.segments)

    def __add__(self, other):
        p = Program()
        p.segments = list(self.segments) + list(other.segments)
        p.info.update(self.info)
        p.info.update(other.info)
        p.sources = self.sources + other.sources
        return p

    def __iadd__(self, other):
        self.segments.extend(other.segments)
        self.info.update(other.info)
        self.sources.extend(other.sources)
        return self

    def paged(self, page_size, fill=b"\xff"):
        p = Program()
        p.info = dict(self.info)
        p.sources = list(self.sources)
        simplified = self.simplified()
        for seg in simplified:
            # Align start down to page boundary
            page_start = (seg.address // page_size) * page_size
            # Align end up to page boundary
            page_end = ((seg.end + page_size - 1) // page_size) * page_size
            total = page_end - page_start
            data = bytearray(fill * ((total + len(fill) - 1) // len(fill)))[:total]
            offset = seg.address - page_start
            data[offset:offset + len(seg)] = seg.data
            p.append(Segment(page_start, data, seg.name))
        return p

    def simplified(self):
        if not self.segments:
            return Program()
        p = Program()
        p.info = dict(self.info)
        p.sources = list(self.sources)
        segs = sorted(self.segments)
        current = Segment(segs[0].address, segs[0].data, segs[0].name)
        for seg in segs[1:]:
            if seg.address <= current.end:
                # Overlapping or adjacent: merge
                new_end = max(current.end, seg.end)
                if new_end > current.end:
                    current.data.extend(b"\x00" * (new_end - current.end))
                # Overwrite with new segment's data at the right offset
                offset = seg.address - current.address
                current.data[offset:offset + len(seg)] = seg.data
            else:
                p.append(current)
                current = Segment(seg.address, seg.data, seg.name)
        p.append(current)
        return p

    def save(self, filename):
        _, ext = os.path.splitext(filename)
        ext = ext.lstrip(".")
        if ext == "bin":
            self.save_bin(filename)
        elif ext in ("hex", "ihex"):
            self.save_hex(filename)
        else:
            raise ValueError(f"Unknown extension: {ext!r}")

    def save_bin(self, filename):
        simplified = self.simplified()
        if len(simplified) == 0:
            with open(filename, "wb") as f:
                pass
            return
        if len(simplified) > 1:
            raise ValueError(
                "Cannot save multi-segment program as flat binary; "
                "use simplified() or within() first"
            )
        with open(filename, "wb") as f:
            f.write(bytes(simplified[0].data))

    def save_hex(self, filename):
        def hex_record(rtype, address, data=b""):
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
        simplified = self.simplified()
        last_upper = None
        for seg in sorted(simplified):
            offset = 0
            while offset < len(seg):
                full_addr = seg.address + offset
                upper = (full_addr >> 16) & 0xFFFF
                if upper != last_upper:
                    lines.append(hex_record(
                        0x04, 0x0000,
                        upper.to_bytes(2, "big"),
                    ))
                    last_upper = upper
                lower = full_addr & 0xFFFF
                chunk_size = min(16, len(seg) - offset)
                lines.append(hex_record(
                    0x00, lower,
                    bytes(seg.data[offset:offset + chunk_size]),
                ))
                offset += chunk_size
        lines.append(hex_record(0x01, 0x0000))
        with open(filename, "w") as f:
            f.write("\n".join(lines) + "\n")

    @classmethod
    def from_file(cls, filename, offset=0):
        # Parse suffixes from the right: data:format:+offset
        # e.g. "file.bin:+100", "file.bin:format", "deadbeef:literal:+0x100"
        parts = filename
        fmt = None

        # Strip :+offset suffixes (may be multiple)
        while ":+" in parts:
            idx = parts.rindex(":+")
            offset += int(parts[idx + 2:], 16)
            parts = parts[:idx]

        # Strip :format suffix — try rightmost colon as format
        while ":" in parts and not os.path.exists(parts):
            idx = parts.rindex(":", 2)
            candidate_fmt = parts[idx + 1:]
            candidate_data = parts[:idx]
            try:
                cls.format_db.get(candidate_fmt)
                fmt = candidate_fmt
                parts = candidate_data
                break
            except NoMatch:
                break

        filename = parts

        if fmt is not None:
            return cls.format_db.call(fmt, filename, offset=offset)

        # Try compound extension first (e.g. "fs.gz"), then single
        basename = os.path.basename(filename)
        dot = basename.find(".")
        if dot >= 0:
            compound_ext = basename[dot + 1:]
            try:
                return cls.ext_db.call(compound_ext, filename, offset=offset)
            except NoMatch:
                pass
        _, ext = os.path.splitext(filename)
        ext = ext.lstrip(".")
        return cls.ext_db.call(ext, filename, offset=offset)

    @classmethod
    def from_files(cls, filenames):
        programs = [cls.from_file(f) for f in filenames]
        return cls.from_programs(programs)

    @classmethod
    def from_programs(cls, programs):
        result = cls()
        for p in programs:
            result += p
        return result
