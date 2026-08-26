"""`acrobe loadable` — VFS resource browsing and conversion.

Each command takes one resource path. Paths are filesystem prefix +
optional VFS sub-children (e.g. ``file.sof/bootloader``). See
``docs/vfs-design.md``.

Subcommands:

- ``info``    — class, mixins, addresses, metadata, children, memory map.
- ``ls``      — list pre-populated children at a path.
- ``tree``    — recursive ``ls`` with box-drawing.
- ``cp``      — copy bytes from a Readable node to a file (``-`` for stdout).
- ``hexdump`` — formatted hex dump (whole memory map, or a Readable subset
  via ``--offset/--size``).
- ``to-bin``, ``to-hex``, ``to-c-blob``, ``to-vhdl-blob`` — converters
  driven by the resource's MemoryMap.
"""

import os
import re
import sys

import asyncclick as click

from . import base
from ..node import Node, Readable, Addressable, Writable
from ..vfs import FsRoot
from ..memory_map import save_bin, save_hex


# --- Path resolution helpers (also used by base.ResourceRef) ---

def _split_path(path):
    """Split a VFS path into (root_dir, parts).

    Heuristic: take the longest filesystem-existing prefix as the
    FsRoot, then split the rest as VFS path components. ``root_dir``
    is "" (cwd) or an absolute filesystem path / directory prefix.
    """
    norm = path.rstrip("/")
    parts = norm.split("/")
    root_parts = []
    rest_parts = list(parts)
    while rest_parts:
        candidate = "/".join(root_parts + [rest_parts[0]]) if root_parts else rest_parts[0]
        if not candidate:
            rest_parts.pop(0)
            root_parts.append("")
            continue
        if os.path.exists(candidate) or (
                candidate.startswith("/") and os.path.isdir(
                    "/".join(root_parts + [rest_parts[0]]))):
            root_parts.append(rest_parts.pop(0))
        else:
            break
    if root_parts:
        root_dir = "/".join(root_parts)
        if not os.path.isdir(root_dir):
            parent = os.path.dirname(root_dir) or "."
            base_name = os.path.basename(root_dir)
            return parent, [base_name] + rest_parts
        return root_dir, rest_parts
    return ".", parts


async def _summon(path):
    root_dir, parts = _split_path(path)
    root = FsRoot(root_dir)
    await root.start_tree()
    if not parts:
        return root
    return await root.child_summon(*parts)


def _mixin_tag(node):
    tags = []
    if isinstance(node, Readable):
        tags.append("R")
    if isinstance(node, Writable):
        tags.append("W")
    if isinstance(node, Addressable):
        tags.append("A")
    return "".join(tags) or "-"


def _format_long(node):
    parts = [_mixin_tag(node)]
    if isinstance(node, Readable):
        parts.append(f"size={node.size}")
    if isinstance(node, Addressable):
        parts.append(f"@0x{node.load_address:x}")
    parts.append(node.name)
    return "  ".join(parts)


# --- Group ---

@base.cli.group(help="Resource browsing and conversion")
async def loadable():
    pass


# --- Inspection ---

@loadable.command(help="Show metadata, children and memory map for a node")
@click.argument("resource", type=base.RESOURCE)
async def info(resource):
    node = await resource.resolve()

    click.echo(f"Path:    {node.path}")
    click.echo(f"Class:   {type(node).__name__}")
    click.echo(f"Mixins:  {_mixin_tag(node)}")
    if isinstance(node, Readable):
        click.echo(f"Size:    {node.size}")
    if isinstance(node, Addressable):
        click.echo(f"Address: 0x{node.load_address:x}")
        for k, v in node.addresses.items():
            if k == "load":
                continue
            click.echo(f"  {k}: 0x{v:x}")
    md = node.metadata
    if md:
        click.echo("Metadata:")
        for k, v in md.items():
            click.echo(f"  {k}: {v!r}")
    if node.children:
        click.echo(f"Children ({len(node.children)}):")
        for c in node.children:
            click.echo(f"  {c.name}  [{_mixin_tag(c)}]")

    m = (await resource.memory_map()).simplified()
    if m.chunks:
        click.echo("MemoryMap:")
        for addr, data in m:
            click.echo(
                f"  <0x{addr:08x}:0x{addr + len(data):08x} "
                f"({len(data)} bytes)>")


@loadable.command(help="List children at a path")
@click.argument("resource", type=base.RESOURCE)
@click.option("-l", "long_format", is_flag=True,
              help="Long format: mixin tags, size, load_address")
@click.option("-r", "recursive", is_flag=True,
              help="Recurse into children")
@click.option("--depth", type=int, default=None,
              help="Max recursion depth (with -r)")
async def ls(resource, long_format, recursive, depth):
    node = await resource.resolve()

    def _walk(n, indent=0, current_depth=0):
        for c in n.children:
            line = _format_long(c) if long_format else c.name
            click.echo(" " * indent + line)
            if recursive and (depth is None or current_depth < depth - 1):
                _walk(c, indent + 2, current_depth + 1)

    _walk(node)


@loadable.command(help="Recursive listing with box-drawing")
@click.argument("resource", type=base.RESOURCE)
@click.option("--depth", type=int, default=None, help="Max recursion depth")
async def tree(resource, depth):
    node = await resource.resolve()

    def _walk(n, prefix="", current_depth=0):
        kids = n.children
        for i, c in enumerate(kids):
            last = (i == len(kids) - 1)
            connector = "└── " if last else "├── "
            click.echo(prefix + connector + c.name + f"  [{_mixin_tag(c)}]")
            if depth is None or current_depth < depth - 1:
                ext = "    " if last else "│   "
                _walk(c, prefix + ext, current_depth + 1)

    click.echo(node.path)
    _walk(node)


@loadable.command(help="Copy bytes from a Readable node to a file")
@click.argument("resource", type=base.RESOURCE)
@click.argument("dst", default="-")
@click.option("--offset", type=str, default="0",
              help="Byte offset within source (decimal or 0x...)")
@click.option("--size", type=str, default=None,
              help="Bytes to copy (default: rest of source)")
async def cp(resource, dst, offset, size):
    node = await resource.resolve()
    if not isinstance(node, Readable):
        raise click.ClickException(
            f"{resource.path}: node {type(node).__name__} is not Readable")
    off = int(offset, 0)
    n = node.size - off if size is None else int(size, 0)
    data = await node.read(off, n)
    if dst == "-":
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    else:
        with open(dst, "wb") as f:
            f.write(data)
        click.echo(f"Wrote {len(data)} bytes to {dst}", err=True)


@loadable.command(help="Hex dump")
@click.argument("resource", type=base.RESOURCE)
@click.option("--offset", type=str, default=None,
              help="Byte offset within source (decimal or 0x...)")
@click.option("--size", type=str, default=None,
              help="Bytes to dump (default: full)")
async def hexdump(resource, offset, size):
    from ..util.hexdump import hexdump as do_hexdump
    if offset is not None or size is not None:
        # Single-Readable subset.
        node = await resource.resolve()
        if not isinstance(node, Readable):
            raise click.ClickException(
                f"{resource.path}: node {type(node).__name__} is not Readable")
        off = int(offset or "0", 0)
        n = int(size, 0) if size is not None else node.size - off
        data = await node.read(off, n)
        base_addr = node.load_address if isinstance(node, Addressable) else 0
        do_hexdump(base_addr + off, data, printer=click.echo)
    else:
        m = await resource.memory_map()
        for addr, data in m:
            do_hexdump(addr, data, printer=click.echo)


# --- Converters ---

@loadable.command(name="to-bin", help="Convert resource to binary")
@click.argument("resource", type=base.RESOURCE)
@click.argument("output", type=click.Path(dir_okay=False))
async def to_bin(resource, output):
    m = (await resource.memory_map()).simplified()
    if len(m) == 0:
        with open(output, "wb"):
            pass
        return
    if len(m) > 1:
        raise click.ClickException(
            "Cannot save multi-chunk MemoryMap as flat binary; "
            "use within() first")
    save_bin(m, output)


@loadable.command(name="to-hex", help="Convert resource to Intel HEX")
@click.argument("resource", type=base.RESOURCE)
@click.argument("output", type=click.Path(dir_okay=False))
@click.option("--paged", type=int, default=0, help="Page-align chunks")
async def to_hex(resource, output, paged):
    m = await resource.memory_map()
    if paged:
        m = m.paged(paged)
    save_hex(m, output)


@loadable.command(name="to-c-blob", help="C blob file generator")
@click.argument("resource", type=base.RESOURCE)
@click.argument("output", type=click.File("w"))
@click.option("-n", "--name", type=str, default="blob", help="Variable name")
@click.option("--align", type=int, default=1, help="Alignment constraint")
@click.option("--section", type=str, default=None, help="Target section")
@click.option("--static", is_flag=True, default=False, help="Static symbol")
@click.option("--extern", is_flag=True, default=False,
              help="Only emit forward declarations")
@click.option("-S", "--size", is_flag=True, default=False,
              help="Also emit size constant")
async def to_c_blob(resource, output, name, align, section, size,
                    static, extern):
    m = (await resource.memory_map()).simplified()
    if len(m) > 1:
        raise click.ClickException("MemoryMap has more than one chunk")
    if len(m) == 0:
        raise click.ClickException("MemoryMap is empty")
    data = m[0][1]

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
@click.argument("resource", type=base.RESOURCE)
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
async def to_vhdl_blob(resource, output, name, package_name,
                       hex_string, byte_string, slv_array, size, endian_):
    m = (await resource.memory_map()).simplified()
    if len(m) > 1:
        raise click.ClickException("MemoryMap has more than one chunk")
    if len(m) == 0:
        raise click.ClickException("MemoryMap is empty")
    data = m[0][1]

    if slv_array:
        mode = "slv"
    elif byte_string:
        mode = "byte"
    elif hex_string:
        mode = "hex"
    else:
        raise click.ClickException(
            "Specify --hex-string, --byte-string, or --slv-array")

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

    pre_info_str = "\n-- ".join(["", f"Extracted from: {resource.path}", ""])

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
