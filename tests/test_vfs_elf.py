"""Tests for ELF VFS Node container."""

import struct

import pytest

from acrobe.vfs import FsRoot
from acrobe.vfs.elf import (
    Elf, ElfSegment, ElfSection, ElfSymbol, ElfSymbols,
    ELF_MAGIC, ELFCLASS32, ELFDATA2LSB,
)


def _make_elf32_le(segments, *, entry=0):
    """Build a minimal ELF32 LE binary with PT_LOAD segments only.

    `segments`: list of (paddr, bytes).
    Returns the file content. Sections / symbols not included.
    """
    endian = "<"
    e_phnum = len(segments)
    e_phoff = 52
    e_phentsize = 32

    ident = bytes([0x7f]) + b"ELF" + bytes([ELFCLASS32, ELFDATA2LSB, 1, 0]) + b"\x00" * 8
    ehdr_rest = struct.pack(
        endian + "HHIIIIIHHHHHH",
        2,           # e_type: ET_EXEC
        0,           # e_machine
        1,           # e_version
        entry,       # e_entry
        e_phoff,     # e_phoff
        0,           # e_shoff
        0,           # e_flags
        52,          # e_ehsize
        e_phentsize, # e_phentsize
        e_phnum,     # e_phnum
        0,           # e_shentsize
        0,           # e_shnum
        0,           # e_shstrndx
    )
    data_offset = 52 + e_phentsize * e_phnum
    phdrs = b""
    seg_data = b""
    for paddr, blob in segments:
        phdrs += struct.pack(
            endian + "IIIIIIII",
            1,                    # p_type: PT_LOAD
            data_offset,          # p_offset
            paddr,                # p_vaddr
            paddr,                # p_paddr
            len(blob),            # p_filesz
            len(blob),            # p_memsz
            5,                    # p_flags: PF_R|PF_X
            0,                    # p_align
        )
        seg_data += blob
        data_offset += len(blob)
    return ident + ehdr_rest + phdrs + seg_data


def _make_elf32_with_sections(*, text_addr=0x1000, text_data=b"\xaa" * 16,
                              bss_addr=0x2000, bss_size=64):
    """Build a slightly more complete ELF32 with sections + .symtab.

    Returns the file content."""
    endian = "<"
    ident = bytes([0x7f]) + b"ELF" + bytes(
        [ELFCLASS32, ELFDATA2LSB, 1, 0]) + b"\x00" * 8

    # Build section blob layout:
    # [0]  null section
    # [1]  .text (PROGBITS)
    # [2]  .bss (NOBITS)
    # [3]  .symtab (SYMTAB linked to [4])
    # [4]  .strtab (STRTAB)
    # [5]  .shstrtab (STRTAB)

    shstrtab = b"\x00.text\x00.bss\x00.symtab\x00.strtab\x00.shstrtab\x00"
    sh_text_name = shstrtab.index(b".text\x00")
    sh_bss_name = shstrtab.index(b".bss\x00")
    sh_symtab_name = shstrtab.index(b".symtab\x00")
    sh_strtab_name = shstrtab.index(b".strtab\x00")
    sh_shstrtab_name = shstrtab.index(b".shstrtab\x00")

    # symbols: NULL + main (in .text @ text_addr+0)
    strtab = b"\x00main\x00data\x00"
    sym_null = struct.pack(endian + "IIIBBH", 0, 0, 0, 0, 0, 0)
    # main: section index = 1 (.text), value = text_addr, type=FUNC, bind=GLOBAL
    sym_main = struct.pack(
        endian + "IIIBBH",
        strtab.index(b"main\x00"),  # st_name
        text_addr,                   # st_value
        4,                            # st_size
        (1 << 4) | 2,                 # st_info: GLOBAL | FUNC
        0,                            # st_other
        1,                            # st_shndx (.text)
    )
    sym_data = struct.pack(
        endian + "IIIBBH",
        strtab.index(b"data\x00"),
        bss_addr,
        bss_size,
        (1 << 4) | 1,                 # GLOBAL | OBJECT
        0,
        2,                            # st_shndx (.bss)
    )
    symtab_data = sym_null + sym_main + sym_data

    # Lay out file: ehdr (52) + .text + .symtab + .strtab + .shstrtab + shdrs
    # We'll skip phdrs for simplicity (no e_phnum).
    text_off = 52
    text_size = len(text_data)
    symtab_off = text_off + text_size
    symtab_size = len(symtab_data)
    strtab_off = symtab_off + symtab_size
    strtab_size = len(strtab)
    shstrtab_off = strtab_off + strtab_size
    shstrtab_size = len(shstrtab)
    shoff = shstrtab_off + shstrtab_size

    e_shentsize = 40  # ELF32 Shdr size
    shdrs = b""
    # [0] null
    shdrs += struct.pack(
        endian + "IIIIIIIIII",
        0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
    # [1] .text (PROGBITS, ALLOC|EXEC)
    shdrs += struct.pack(
        endian + "IIIIIIIIII",
        sh_text_name, 1, 0x6, text_addr, text_off, text_size,
        0, 0, 1, 0)
    # [2] .bss (NOBITS, ALLOC|WRITE)
    shdrs += struct.pack(
        endian + "IIIIIIIIII",
        sh_bss_name, 8, 0x3, bss_addr, 0, bss_size,
        0, 0, 1, 0)
    # [3] .symtab (SYMTAB, link to .strtab=4)
    shdrs += struct.pack(
        endian + "IIIIIIIIII",
        sh_symtab_name, 2, 0, 0, symtab_off, symtab_size,
        4, 0, 1, 16)
    # [4] .strtab (STRTAB)
    shdrs += struct.pack(
        endian + "IIIIIIIIII",
        sh_strtab_name, 3, 0, 0, strtab_off, strtab_size,
        0, 0, 1, 0)
    # [5] .shstrtab (STRTAB)
    shdrs += struct.pack(
        endian + "IIIIIIIIII",
        sh_shstrtab_name, 3, 0, 0, shstrtab_off, shstrtab_size,
        0, 0, 1, 0)

    ehdr_rest = struct.pack(
        endian + "HHIIIIIHHHHHH",
        2,            # e_type
        0,            # e_machine
        1,            # e_version
        text_addr,    # e_entry
        0,            # e_phoff
        shoff,        # e_shoff
        0,            # e_flags
        52,           # e_ehsize
        0,            # e_phentsize
        0,            # e_phnum
        e_shentsize,  # e_shentsize
        6,            # e_shnum
        5,            # e_shstrndx
    )

    return (ident + ehdr_rest + text_data + symtab_data + strtab
            + shstrtab + shdrs)


# --- Program-header tests ---

class TestElfPrograms:
    @pytest.mark.asyncio
    async def test_one_segment(self, tmp_path):
        elf = _make_elf32_le(
            [(0x08000000, b"\xaa\xbb\xcc\xdd")], entry=0x08000000)
        path = tmp_path / "a.elf"
        path.write_bytes(elf)
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        seg = await root.child_summon("a.elf", "program", "0")
        assert isinstance(seg, ElfSegment)
        assert seg.load_address == 0x08000000
        assert seg.size == 4
        assert (await seg.read(0, 4)) == b"\xaa\xbb\xcc\xdd"

    @pytest.mark.asyncio
    async def test_multiple_segments(self, tmp_path):
        elf = _make_elf32_le([
            (0x1000, b"\x01\x02"),
            (0x2000, b"\x03\x04"),
        ])
        path = tmp_path / "a.elf"
        path.write_bytes(elf)
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        names = [
            c.name for c in
            (await root.child_summon("a.elf", "program")).children]
        assert names == ["0", "1"]
        s1 = await root.child_summon("a.elf", "program", "1")
        assert s1.load_address == 0x2000
        assert (await s1.read(0, 2)) == b"\x03\x04"

    @pytest.mark.asyncio
    async def test_addresses_dict(self, tmp_path):
        elf = _make_elf32_le([(0x4000, b"data")])
        path = tmp_path / "a.elf"
        path.write_bytes(elf)
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        seg = await root.child_summon("a.elf", "program", "0")
        addrs = seg.addresses
        assert addrs["vma"] == 0x4000
        assert addrs["lma"] == 0x4000
        assert addrs["load"] == 0x4000
        assert "file_offset" in addrs


# --- Sections tests ---

class TestElfSections:
    @pytest.mark.asyncio
    async def test_text_section(self, tmp_path):
        elf = _make_elf32_with_sections(text_data=b"hello, world!!!!")
        path = tmp_path / "a.elf"
        path.write_bytes(elf)
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        text = await root.child_summon("a.elf", "section", ".text")
        assert isinstance(text, ElfSection)
        assert (await text.read(0, text.size)) == b"hello, world!!!!"
        assert "alloc" in text.flags
        assert "exec" in text.flags
        assert text.section_type == "progbits"

    @pytest.mark.asyncio
    async def test_bss_section_synthesises_zeros(self, tmp_path):
        elf = _make_elf32_with_sections(bss_size=128)
        path = tmp_path / "a.elf"
        path.write_bytes(elf)
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        bss = await root.child_summon("a.elf", "section", ".bss")
        assert bss.section_type == "nobits"
        assert bss.size == 128
        data = await bss.read(0, 128)
        assert data == b"\x00" * 128


# --- Symbols tests ---

class TestElfSymbols:
    @pytest.mark.asyncio
    async def test_symbol_lookup_on_demand(self, tmp_path):
        elf = _make_elf32_with_sections()
        path = tmp_path / "a.elf"
        path.write_bytes(elf)
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        # Walk to symbol/main — on-demand spawn from symbol table.
        sym = await root.child_summon("a.elf", "symbol", "main")
        assert isinstance(sym, ElfSymbol)
        assert sym.load_address == 0x1000
        assert sym.metadata["type"] == "func"
        assert sym.metadata["binding"] == "global"

    @pytest.mark.asyncio
    async def test_symbol_unknown_raises(self, tmp_path):
        elf = _make_elf32_with_sections()
        path = tmp_path / "a.elf"
        path.write_bytes(elf)
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        from acrobe.db import NoMatch
        with pytest.raises(NoMatch):
            await root.child_summon("a.elf", "symbol", "does_not_exist")

    @pytest.mark.asyncio
    async def test_symbol_namespace_not_pre_enumerated(self, tmp_path):
        # Symbols are on-demand: the `symbol` namespace exists, but
        # its children list is empty until an explicit lookup.
        elf = _make_elf32_with_sections()
        path = tmp_path / "a.elf"
        path.write_bytes(elf)
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        sym_ns = await root.child_summon("a.elf", "symbol")
        assert isinstance(sym_ns, ElfSymbols)
        assert sym_ns.children == []  # on-demand only


class TestElfReverseLookup:
    @pytest.mark.asyncio
    async def test_symbol_at(self, tmp_path):
        elf = _make_elf32_with_sections()
        path = tmp_path / "a.elf"
        path.write_bytes(elf)
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        leaf = await root.child_summon("a.elf")
        # Reverse-lookup methods live on the format parser; access
        # via format_parsers (set by populate_format).
        elf_parser = leaf.format_parsers[0]
        sym = elf_parser.symbol_at(0x1000)
        assert sym is not None
        assert sym.name == "main"

    @pytest.mark.asyncio
    async def test_section_at(self, tmp_path):
        elf = _make_elf32_with_sections(text_addr=0x4000)
        path = tmp_path / "a.elf"
        path.write_bytes(elf)
        root = FsRoot(str(tmp_path))
        await root.start_tree()
        leaf = await root.child_summon("a.elf")
        elf_parser = leaf.format_parsers[0]
        sec = elf_parser.section_at(0x4008)
        assert sec is not None
        assert sec.name == ".text"
