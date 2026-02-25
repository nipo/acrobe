import struct

from .model import Segment, Program


class ElfError(ValueError):
    pass


_ELF_MAGIC = b"\x7fELF"

# ELF class
_ELFCLASS32 = 1
_ELFCLASS64 = 2

# ELF data encoding
_ELFDATA2LSB = 1
_ELFDATA2MSB = 2

# Program header types
_PT_LOAD = 1


def _parse_elf(filename, offset=0):
    with open(filename, "rb") as f:
        data = f.read()

    if len(data) < 16:
        raise ElfError("File too small to be an ELF")

    if data[:4] != _ELF_MAGIC:
        raise ElfError("Not an ELF file")

    ei_class = data[4]
    ei_data = data[5]

    if ei_class == _ELFCLASS32:
        is_64 = False
    elif ei_class == _ELFCLASS64:
        is_64 = True
    else:
        raise ElfError(f"Unsupported ELF class: {ei_class}")

    if ei_data == _ELFDATA2LSB:
        endian = "<"
    elif ei_data == _ELFDATA2MSB:
        endian = ">"
    else:
        raise ElfError(f"Unsupported ELF data encoding: {ei_data}")

    if is_64:
        # ELF64 header: 64 bytes
        # e_type(2) e_machine(2) e_version(4) e_entry(8) e_phoff(8) e_shoff(8)
        # e_flags(4) e_ehsize(2) e_phentsize(2) e_phnum(2) ...
        ehdr_fmt = endian + "HHI QQQ I HHH HHH"
        ehdr_size = 64
    else:
        # ELF32 header: 52 bytes
        # e_type(2) e_machine(2) e_version(4) e_entry(4) e_phoff(4) e_shoff(4)
        # e_flags(4) e_ehsize(2) e_phentsize(2) e_phnum(2) ...
        ehdr_fmt = endian + "HHI III I HHH HHH"
        ehdr_size = 52

    if len(data) < ehdr_size:
        raise ElfError("ELF header truncated")

    fields = struct.unpack_from(ehdr_fmt, data, 16)
    # fields: e_type, e_machine, e_version, e_entry, e_phoff, e_shoff,
    #         e_flags, e_ehsize, e_phentsize, e_phnum, e_shentsize, e_shnum, e_shstrndx
    e_entry = fields[3]
    e_phoff = fields[4]
    e_phentsize = fields[8]
    e_phnum = fields[9]

    if is_64:
        # ELF64 phdr: p_type(4) p_flags(4) p_offset(8) p_vaddr(8) p_paddr(8)
        #             p_filesz(8) p_memsz(8) p_align(8)
        phdr_fmt = endian + "II QQQQQQ"
    else:
        # ELF32 phdr: p_type(4) p_offset(4) p_vaddr(4) p_paddr(4)
        #             p_filesz(4) p_memsz(4) p_flags(4) p_align(4)
        phdr_fmt = endian + "IIIIIIII"

    p = Program(filename)
    p.info["entry"] = e_entry + offset

    for i in range(e_phnum):
        phdr_offset = e_phoff + i * e_phentsize
        phdr = struct.unpack_from(phdr_fmt, data, phdr_offset)

        if is_64:
            p_type = phdr[0]
            p_offset = phdr[2]
            p_vaddr = phdr[3]
            p_paddr = phdr[4]
            p_filesz = phdr[5]
            p_memsz = phdr[6]
        else:
            p_type = phdr[0]
            p_offset = phdr[1]
            p_vaddr = phdr[2]
            p_paddr = phdr[3]
            p_filesz = phdr[4]
            p_memsz = phdr[5]

        if p_type != _PT_LOAD:
            continue

        if p_filesz == 0 and p_memsz == 0:
            continue

        seg_data = bytearray(data[p_offset:p_offset + p_filesz])
        # If memsz > filesz, pad with zeros (BSS)
        if p_memsz > p_filesz:
            seg_data.extend(b"\x00" * (p_memsz - p_filesz))

        p.append(Segment(p_paddr + offset, seg_data))

    return p


@Program.ext_db.register("elf", "axf", "out")
@Program.format_db.register("elf")
def load_elf(filename, offset=0):
    return _parse_elf(filename, offset)
