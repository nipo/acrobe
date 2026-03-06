import re
import asyncclick as click

from . import base
from ..loadable import Program


@base.cli.group(help="Loadable manipulation")
async def loadable():
    pass


@loadable.command(help="Dump file parsing results")
@click.argument("programs", type=base.PROGRAM, nargs=-1)
async def dump(programs):
    p = Program.from_programs(programs).simplified()
    click.echo(f"{p.__class__.__name__}:")
    for k, v in sorted(p.info.items()):
        click.echo(f"  {k}: {v}")
    for seg in p:
        click.echo(f"  {seg}")


@loadable.command(help="Hex dump image")
@click.argument("programs", type=base.PROGRAM, nargs=-1)
async def hexdump(programs):
    from ..util.hexdump import hexdump as do_hexdump
    p = Program.from_programs(programs)
    for s in p:
        if s.name:
            click.echo(f"{s.name}:")
        do_hexdump(s.address, s.data, printer=click.echo)


@loadable.command(name="to-bin", help="Convert to binary")
@click.argument("programs", type=base.PROGRAM, nargs=-1)
@click.argument("output", type=click.File("wb"))
async def to_bin(programs, output):
    p = Program.from_programs(programs).simplified()
    if len(p) == 0:
        return
    if len(p) > 1:
        raise click.ClickException(
            "Cannot save multi-segment program as flat binary; "
            "use within() first"
        )
    output.write(bytes(p[0].data))


@loadable.command(name="to-hex", help="Convert to Intel HEX")
@click.argument("programs", type=base.PROGRAM, nargs=-1)
@click.argument("output", type=click.Path(dir_okay=False))
@click.option("--paged", type=int, default=0, help="Page-align segments")
async def to_hex(programs, output, paged):
    p = Program.from_programs(programs)
    if paged:
        p = p.paged(paged)
    p.save_hex(output)


@loadable.command(name="to-c-blob", help="C blob file generator")
@click.argument("programs", type=base.PROGRAM, nargs=-1)
@click.argument("output", type=click.File("w"))
@click.option("-n", "--name", type=str, default="blob", help="Variable name")
@click.option("--align", type=int, default=1, help="Alignment constraint")
@click.option("--section", type=str, default=None, help="Target section")
@click.option("--static", is_flag=True, default=False, help="Static symbol")
@click.option("--extern", is_flag=True, default=False,
              help="Only emit forward declarations")
@click.option("-S", "--size", is_flag=True, default=False,
              help="Also emit size constant")
async def to_c_blob(programs, output, name, align, section, size,
                    static, extern):
    p = Program.from_programs(programs).simplified()
    if len(p) > 1:
        raise click.ClickException("Loadable has more than one segment")
    data = p[0].data

    guard_name = re.sub("[^A-Z]+", "_", name.upper()) + "_DECLARED"

    if static and extern:
        raise click.ClickException("Cannot be extern and static at the same time")

    output.write(
        f"#ifndef {guard_name}\n"
        f"#define {guard_name}\n"
        f"\n"
        f"#include <stdint.h>\n"
        f"#include <stdlib.h>\n"
        f"\n"
    )

    if not extern:
        if section:
            output.write(f'__attribute__((section "{section}"))\n')
        if align != 1:
            output.write(f"__attribute__((aligned ({align})))\n")
    if static:
        output.write("static ")
    elif extern:
        output.write("extern ")
    output.write(f"const uint8_t {name}[]")
    if not extern:
        output.write(" = {\n")
        for off in range(0, len(data), 16):
            subset = data[off:min(len(data), off + 16)]
            line = ", ".join(f"{v:#04x}" for v in subset)
            output.write(f" /* {off:#010x}: */ {line},\n")
        output.write("}")
    output.write(";\n")

    if size:
        if static:
            output.write("static ")
        elif extern:
            output.write("extern ")
        output.write(f"const size_t {name}_size")
        if not extern:
            output.write(f" = {len(data):#x}")
        output.write(";\n\n")

    output.write("#endif\n")


@loadable.command(name="to-vhdl-blob", help="VHDL blob file generator")
@click.argument("programs", type=base.PROGRAM, nargs=-1)
@click.argument("output", type=click.File("w"))
@click.option("-n", "--name", type=str, default="blob", help="Variable name")
@click.option("-p", "--package-name", type=str, default="pack",
              help="Package name")
@click.option("--hex-string", is_flag=True, default=False,
              help="Emit a hex string constant")
@click.option("--byte-string", is_flag=True, default=False,
              help="Emit a byte_string constant")
@click.option("--slv-array", is_flag=True, default=False,
              help="Emit std_logic_vector array constant")
@click.option("-S", "--size", type=int, default=8,
              help="SLV array item width (bit count, multiple of 8)")
@click.option("-e", "--endian", "endian_", type=str, default="little",
              help="Multi-byte endianness")
async def to_vhdl_blob(programs, output, name, package_name,
                       hex_string, byte_string, slv_array, size, endian_):
    p = Program.from_programs(programs).simplified()
    if len(p) > 1:
        raise click.ClickException("Loadable has more than one segment")
    data = p[0].data

    if slv_array:
        mode = "slv"
    elif byte_string:
        mode = "byte"
    elif hex_string:
        mode = "hex"
    else:
        raise click.ClickException("Specify --hex-string, --byte-string, or --slv-array")

    if mode == "slv":
        header = (
            "library ieee;\n"
            "use ieee.std_logic_1164.all;\n"
        )
        pre_declaration = (
            f"\n  type byte_array is array(integer range <>) "
            f"of std_logic_vector({size}-1 downto 0);"
        )
        word_size = size // 8
        padded = data + b"\x00" * ((-len(data)) % word_size)
        word_count = len(padded) // word_size
        datatype = f"byte_array(0 to {word_count - 1})"
        words = []
        for addr in range(0, len(padded), word_size):
            w = int.from_bytes(padded[addr:addr + word_size], endian_)
            words.append(f"{w:0{2 * word_size}x}")
        init = "(\n"
        for woff in range(0, len(words), 4):
            init += "    "
            for i, w in enumerate(words[woff:woff + 4], start=woff):
                init += f'x"{w}"'
                if i < len(words) - 1:
                    init += ", "
            init += "\n"
        init += "  )"
    elif mode == "byte":
        header = (
            "library nsl_data;\n"
            "use nsl_data.bytestream.all;\n"
        )
        pre_declaration = ""
        datatype = "byte_string"
        init = 'from_hex(""\n'
        for woff in range(0, len(data), 32):
            init += f'    & "{data[woff:woff + 32].hex()}"\n'
        init += "  )"
    elif mode == "hex":
        header = ""
        pre_declaration = ""
        datatype = "string"
        init = '""\n'
        for woff in range(0, len(data), 32):
            init += f'    & "{data[woff:woff + 32].hex()}"\n'
        init += "  )"

    pre_info = ["", "Extracted from:", ""]
    for prog in programs:
        pre_info.append("- " + ", ".join(prog.sources))
        for seg in prog:
            pre_info.append(f"  {seg}")
    pre_info_str = "\n-- ".join(pre_info)

    output.write(
        f"{header}\n"
        f"\n"
        f"{pre_info_str}\n"
        f"\n"
        f"package {package_name} is\n"
        f"\n"
        f"{pre_declaration}\n"
        f"  constant {name} : {datatype} := {init};\n"
        f"\n"
        f"end package {package_name};\n"
    )
