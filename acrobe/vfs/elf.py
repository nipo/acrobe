"""ELF object file as a VFS Node container.

Per design D2: ELF root is a pure structural Node — not Readable
itself; the parent file Node's Readable already covers the raw
ELF bytes.

Pre-populated children (D9):
- program/N      — PT_LOAD program-header segments (Readable +
                   Addressable; addresses include vma, lma,
                   file_offset, load=lma)
- section/<name> — sections (Readable + Addressable; .bss-style
                   NOBITS sections synthesise zeros).

On-demand children:
- symbol/<name>  — spawned by name from the symbol table (D9).

Reverse lookups: methods on the Elf root return references to the
same Nodes that pre-populated children expose.
"""

import struct

from ..node import Node, Readable, Addressable
from ..db import NoMatch
from . import FormatNode, register_format, register_magic


# --- ELF constants ---

ELF_MAGIC = b"\x7fELF"

ELFCLASS32 = 1
ELFCLASS64 = 2

ELFDATA2LSB = 1
ELFDATA2MSB = 2

# Program header types
PT_NULL = 0
PT_LOAD = 1
PT_DYNAMIC = 2
PT_INTERP = 3

# Section header types
SHT_NULL = 0
SHT_PROGBITS = 1
SHT_SYMTAB = 2
SHT_STRTAB = 3
SHT_NOBITS = 8
SHT_DYNSYM = 11

# Section flags
SHF_WRITE = 0x1
SHF_ALLOC = 0x2
SHF_EXECINSTR = 0x4
SHF_TLS = 0x400

# Symbol bindings/types
STB_LOCAL = 0
STB_GLOBAL = 1
STB_WEAK = 2
STT_NOTYPE = 0
STT_OBJECT = 1
STT_FUNC = 2
STT_SECTION = 3
STT_FILE = 4
SHN_UNDEF = 0
SHN_ABS = 0xfff1
SHN_COMMON = 0xfff2


# --- Helpers ---

def _section_type_name(t):
    return {
        SHT_NULL: "null", SHT_PROGBITS: "progbits",
        SHT_SYMTAB: "symtab", SHT_STRTAB: "strtab",
        SHT_NOBITS: "nobits", SHT_DYNSYM: "dynsym",
    }.get(t, f"unknown({t})")


def _section_flags(f):
    out = []
    if f & SHF_ALLOC:
        out.append("alloc")
    if f & SHF_WRITE:
        out.append("write")
    if f & SHF_EXECINSTR:
        out.append("exec")
    if f & SHF_TLS:
        out.append("tls")
    return frozenset(out)


def _symbol_type_name(t):
    return {
        STT_NOTYPE: "notype", STT_OBJECT: "object",
        STT_FUNC: "func", STT_SECTION: "section", STT_FILE: "file",
    }.get(t, f"unknown({t})")


def _symbol_binding_name(b):
    return {
        STB_LOCAL: "local", STB_GLOBAL: "global", STB_WEAK: "weak",
    }.get(b, f"unknown({b})")


# --- Node types ---

class ElfSegment(Node, Readable, Addressable):
    """A PT_LOAD program-header segment."""

    def __init__(self, name, source, file_offset, file_size,
                 vaddr, paddr, mem_size, flags):
        super().__init__(name)
        self._source = source
        self._file_offset = file_offset
        self._file_size = file_size
        self._vaddr = vaddr
        self._paddr = paddr
        self._mem_size = mem_size
        self._flags = flags

    @property
    def size(self) -> int:
        # Expose mem_size so .bss-style padding is part of the view;
        # bytes beyond file_size are synthesised as zeros.
        return self._mem_size

    async def read(self, offset, size):
        if offset < 0 or offset > self._mem_size:
            raise ValueError(f"offset {offset} out of range")
        avail = self._mem_size - offset
        n = min(size, avail)
        if n <= 0:
            return b""
        # Bytes from [offset, offset + n) within the segment.
        # Up to file_size are read from source; the rest is zeros.
        if offset >= self._file_size:
            return b"\x00" * n
        from_file_n = min(n, self._file_size - offset)
        from_file = await self._source.read(
            self._file_offset + offset, from_file_n)
        if from_file_n == n:
            return from_file
        return from_file + b"\x00" * (n - from_file_n)

    @property
    def load_address(self) -> int:
        return self._paddr

    @property
    def addresses(self) -> dict:
        return {
            "vma": self._vaddr,
            "lma": self._paddr,
            "file_offset": self._file_offset,
            "load": self._paddr,
        }

    @property
    def metadata(self) -> dict:
        return {
            "vma": self._vaddr,
            "lma": self._paddr,
            "file_offset": self._file_offset,
            "file_size": self._file_size,
            "mem_size": self._mem_size,
            "flags": self._flags,
            **self._metadata,
        }


class ElfSection(Node, Readable):
    """An ELF section.

    Readable but not `Addressable`. Section headers only carry the
    runtime address (VMA) in `sh_addr` — there is no LMA field on a
    section header, so a section's load address can only be derived
    by finding which PT_LOAD segment contains it. To avoid wrong-
    LMA chunks polluting `MemoryMap.from_node` (e.g. `.contextdata`
    advertises `sh_addr=0` while its loaded copy lives in a PT_LOAD
    segment at flash 0x1d228), sections never claim to be loadable.

    Loading uses program segments (`ElfSegment`) exclusively, which
    do carry both VMA and LMA. `addresses` / `metadata` on sections
    still expose `sh_addr` under `vma` / `lma` for inspection
    (`acrobe loadable info`); those are descriptive fields, not load
    claims.
    """

    def __init__(self, name, source, file_offset, size, vaddr,
                 lma, flags, sh_type):
        super().__init__(name)
        self._source = source
        self._file_offset = file_offset
        self._size = size
        self._vaddr = vaddr
        self._lma = lma
        self._flags = flags  # frozenset of strings
        self._sh_type = sh_type

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
        # NOBITS sections (e.g. .bss) have no file backing —
        # synthesise zeros.
        if self._sh_type == SHT_NOBITS:
            return b"\x00" * n
        return await self._source.read(self._file_offset + offset, n)

    @property
    def addresses(self) -> dict:
        return {
            "vma": self._vaddr,
            "lma": self._lma,
            "file_offset": self._file_offset,
            "load": self._lma,
        }

    @property
    def section_type(self) -> str:
        return _section_type_name(self._sh_type)

    @property
    def flags(self) -> frozenset:
        return self._flags

    @property
    def metadata(self) -> dict:
        return {
            "vma": self._vaddr,
            "lma": self._lma,
            "file_offset": self._file_offset,
            "size": self._size,
            "flags": list(self._flags),
            "section_type": _section_type_name(self._sh_type),
            **self._metadata,
        }


class ElfSymbol(Node, Addressable):
    """A symbol-table entry. Addressable; not Readable (its bytes
    live in the containing section)."""

    def __init__(self, name, value, size, sym_type, binding,
                 section_name):
        super().__init__(name)
        self._value = value
        self._size = size
        self._sym_type = sym_type
        self._binding = binding
        self._section_name = section_name

    @property
    def load_address(self) -> int:
        return self._value

    @property
    def metadata(self) -> dict:
        return {
            "value": self._value,
            "size": self._size,
            "type": _symbol_type_name(self._sym_type),
            "binding": _symbol_binding_name(self._binding),
            "section": self._section_name,
            **self._metadata,
        }


class ElfSymbols(Node):
    """Symbol-table namespace. Children are spawned on demand
    by name; the table is parsed in start() and cached."""

    def __init__(self, name, table):
        super().__init__(name)
        # table: dict[str, ElfSymbol]
        self._table = table

    async def child_spawn(self, name):
        sym = self._table.get(name)
        if sym is None:
            raise NoMatch("symbol", name)
        # Re-attach an existing Node into this parent. The on-demand
        # path attaches it as a child the first time it's looked up.
        if sym._parent is None:
            return sym
        # Already attached (looked up before); return it directly.
        return sym


# --- ELF parser ---

@register_format("elf",
                 exts=["elf", "axf", "out"],
                 mimes=["application/x-elf"])
class Elf(FormatNode):
    """ELF object file. Pre-populates program/, section/, symbol/
    namespaces; symbol/<name> is on-demand."""

    async def start(self):
        head = await self._source.read(0, min(self._source.size, 64))
        if head[:4] != ELF_MAGIC:
            raise NoMatch("elf", "magic")
        if len(head) < 16:
            raise ValueError(f"{self.fqdn}: ELF header too short")

        ei_class = head[4]
        ei_data = head[5]

        if ei_class == ELFCLASS32:
            is_64 = False
            ehdr_fmt = "HHI III I HHH HHH"
            ehdr_size = 52
        elif ei_class == ELFCLASS64:
            is_64 = True
            ehdr_fmt = "HHI QQQ I HHH HHH"
            ehdr_size = 64
        else:
            raise ValueError(f"{self.fqdn}: bad ei_class {ei_class}")

        if ei_data == ELFDATA2LSB:
            endian = "<"
        elif ei_data == ELFDATA2MSB:
            endian = ">"
        else:
            raise ValueError(f"{self.fqdn}: bad ei_data {ei_data}")

        ehdr_bytes = await self._source.read(16, ehdr_size - 16)
        ehdr = struct.unpack(endian + ehdr_fmt, ehdr_bytes)
        # Field order: e_type, e_machine, e_version, e_entry,
        # e_phoff, e_shoff, e_flags, e_ehsize, e_phentsize, e_phnum,
        # e_shentsize, e_shnum, e_shstrndx
        (e_type, e_machine, e_version, e_entry, e_phoff, e_shoff,
         e_flags, e_ehsize, e_phentsize, e_phnum, e_shentsize,
         e_shnum, e_shstrndx) = ehdr

        self._metadata.update({
            "class": "ELF64" if is_64 else "ELF32",
            "endian": "little" if endian == "<" else "big",
            "type": e_type,
            "machine": e_machine,
            "entry": e_entry,
        })

        # --- Program headers ---
        program_container = Node("program")
        if is_64:
            phdr_fmt = endian + "II QQQQQQ"
        else:
            phdr_fmt = endian + "IIIIIIII"

        ph_idx = 0
        for i in range(e_phnum):
            phdr_off = e_phoff + i * e_phentsize
            phdr_bytes = await self._source.read(phdr_off, e_phentsize)
            phdr = struct.unpack(phdr_fmt, phdr_bytes[:struct.calcsize(phdr_fmt)])
            if is_64:
                p_type, p_flags, p_offset, p_vaddr, p_paddr, p_filesz, p_memsz, _ = phdr
            else:
                p_type, p_offset, p_vaddr, p_paddr, p_filesz, p_memsz, p_flags, _ = phdr
            if p_type != PT_LOAD:
                continue
            if p_filesz == 0 and p_memsz == 0:
                continue
            seg = ElfSegment(
                str(ph_idx), self._source,
                file_offset=p_offset, file_size=p_filesz,
                vaddr=p_vaddr, paddr=p_paddr, mem_size=p_memsz,
                flags=p_flags)
            program_container._child_attach(seg)
            ph_idx += 1
        self._child_attach(program_container)

        # --- Section headers ---
        section_container = Node("section")
        sections = []
        if e_shnum > 0 and e_shoff > 0:
            if is_64:
                shdr_fmt = endian + "IIQQ QQII QQ"
            else:
                shdr_fmt = endian + "IIII IIII II"
            shdr_size = struct.calcsize(shdr_fmt)
            shstrtab_data = None
            # Parse section headers
            raw_shdrs = []
            for i in range(e_shnum):
                shdr_off = e_shoff + i * e_shentsize
                shdr_bytes = await self._source.read(shdr_off, shdr_size)
                shdr = struct.unpack(shdr_fmt, shdr_bytes)
                # ELF Shdr field order:
                # sh_name, sh_type, sh_flags, sh_addr, sh_offset,
                # sh_size, sh_link, sh_info, sh_addralign, sh_entsize
                raw_shdrs.append(shdr)

            # Read shstrtab
            if 0 < e_shstrndx < e_shnum:
                _, _, _, _, sh_offset, sh_size, _, _, _, _ = raw_shdrs[e_shstrndx]
                shstrtab_data = await self._source.read(sh_offset, sh_size)

            # Build named sections from shdrs (skip SHT_NULL).
            for i, shdr in enumerate(raw_shdrs):
                (sh_name, sh_type, sh_flags, sh_addr, sh_offset,
                 sh_size, sh_link, sh_info, sh_addralign,
                 sh_entsize) = shdr
                if sh_type == SHT_NULL:
                    continue
                if shstrtab_data is not None and sh_name < len(shstrtab_data):
                    end = shstrtab_data.find(b"\x00", sh_name)
                    if end < 0:
                        end = len(shstrtab_data)
                    name = shstrtab_data[sh_name:end].decode(
                        "ascii", errors="replace") or f"shdr{i}"
                else:
                    name = f"shdr{i}"
                section = ElfSection(
                    name, self._source, sh_offset, sh_size,
                    vaddr=sh_addr, lma=sh_addr,
                    flags=_section_flags(sh_flags),
                    sh_type=sh_type)
                section_container._child_attach(section)
                sections.append({
                    "index": i, "name": name, "sh_type": sh_type,
                    "sh_offset": sh_offset, "sh_size": sh_size,
                    "sh_link": sh_link, "sh_entsize": sh_entsize,
                })
            self._child_attach(section_container)

            # --- Symbol table ---
            sym_table = {}
            symtab_section = next(
                (s for s in sections if s["sh_type"] == SHT_SYMTAB),
                None)
            if symtab_section is not None:
                sh_offset = symtab_section["sh_offset"]
                sh_size = symtab_section["sh_size"]
                sh_link = symtab_section["sh_link"]
                sh_entsize = symtab_section["sh_entsize"]
                # Linked strtab for symbol names
                strtab_data = b""
                if 0 < sh_link < len(raw_shdrs):
                    _, _, _, _, sl_off, sl_size, _, _, _, _ = raw_shdrs[sh_link]
                    strtab_data = await self._source.read(sl_off, sl_size)

                if is_64:
                    # Elf64_Sym: st_name(4) st_info(1) st_other(1)
                    # st_shndx(2) st_value(8) st_size(8) = 24 bytes
                    sym_fmt = endian + "I BB H QQ"
                    sym_size = 24
                else:
                    # Elf32_Sym: st_name(4) st_value(4) st_size(4)
                    # st_info(1) st_other(1) st_shndx(2) = 16 bytes
                    sym_fmt = endian + "I II BB H"
                    sym_size = 16

                count = sh_size // sym_size if sh_entsize == 0 else sh_size // sh_entsize
                sym_blob = await self._source.read(sh_offset, sh_size)
                for i in range(count):
                    rec = sym_blob[i * sym_size:(i + 1) * sym_size]
                    if len(rec) < sym_size:
                        break
                    if is_64:
                        st_name, st_info, st_other, st_shndx, st_value, st_size = \
                            struct.unpack(sym_fmt, rec)
                    else:
                        st_name, st_value, st_size, st_info, st_other, st_shndx = \
                            struct.unpack(sym_fmt, rec)
                    binding = (st_info >> 4) & 0xF
                    sym_type = st_info & 0xF
                    if st_name < len(strtab_data):
                        end = strtab_data.find(b"\x00", st_name)
                        if end < 0:
                            end = len(strtab_data)
                        name = strtab_data[st_name:end].decode(
                            "ascii", errors="replace")
                    else:
                        name = ""
                    if not name:
                        continue
                    section_name = ""
                    if st_shndx == SHN_UNDEF:
                        section_name = "UND"
                    elif st_shndx == SHN_ABS:
                        section_name = "ABS"
                    elif st_shndx == SHN_COMMON:
                        section_name = "COMMON"
                    elif 0 < st_shndx < len(raw_shdrs):
                        sec_node = section_container._children[st_shndx - 1] \
                            if st_shndx - 1 < len(section_container._children) else None
                        section_name = sec_node.name if sec_node else f"shdr{st_shndx}"
                    sym_table[name] = ElfSymbol(
                        name, st_value, st_size, sym_type, binding,
                        section_name)

            symbols_container = ElfSymbols("symbol", sym_table)
            self._child_attach(symbols_container)

    # --- Reverse lookups ---
    # After populate_format, our children live on _target (the
    # auto-detected file Node). Methods walk _target.children.

    def _container(self, name):
        owner = getattr(self, "_target", self)
        for c in owner._children:
            if c.name == name:
                return c
        return None

    def section_at(self, address: int):
        """Return the ElfSection covering `address` (by VMA), or None."""
        sc = self._container("section")
        if sc is None:
            return None
        for sec in sc.children:
            if sec._vaddr <= address < sec._vaddr + sec._size:
                return sec
        return None

    def symbol_at(self, address: int):
        """Return an ElfSymbol whose value == address, or None."""
        sc = self._container("symbol")
        if sc is None:
            return None
        for sym in sc._table.values():
            if sym._value == address:
                return sym
        return None

    def symbols_in(self, start: int, end: int):
        """Return all symbols whose value is in [start, end)."""
        sc = self._container("symbol")
        if sc is None:
            return []
        return [s for s in sc._table.values()
                if start <= s._value < end]


@register_magic
def _elf_magic(head: bytes):
    if head[:4] == ELF_MAGIC:
        return "elf"
    return None
