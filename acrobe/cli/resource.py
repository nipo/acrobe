"""`acrobe resource` — VFS browsing CLI commands.

Subcommands:
- ls    — list pre-populated children at a path
- info  — dump class, mixins, addresses, metadata
- cp    — copy bytes from a Readable node to a destination (- for stdout)
- hexdump — formatted hex dump with addresses if Addressable
- tree  — recursive ls with box-drawing
"""

import os
import sys

import asyncclick as click

from . import base
from ..node import Node, Readable, Addressable, Writable
from ..vfs import FsRoot


def _split_path(path):
    """Split a VFS path into parts, stripping the leading "/" if any.

    Returns (root_path, parts) where root_path is "" (cwd) or an
    absolute filesystem path / directory prefix, and parts is the
    remaining VFS path components.

    Heuristic: take the longest filesystem-existing prefix as the
    root, then split the rest.
    """
    # Normalize: replace any "//" with "/", strip trailing "/".
    norm = path.rstrip("/")
    parts = norm.split("/")
    # Find the longest prefix that's an existing path.
    root_parts = []
    rest_parts = list(parts)
    while rest_parts:
        candidate = "/".join(root_parts + [rest_parts[0]]) if root_parts else rest_parts[0]
        if not candidate:
            # leading "/" → start with absolute root
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
            # root_dir is a file; take its parent as the FsRoot
            # and the file name as the first VFS part.
            parent = os.path.dirname(root_dir) or "."
            base_name = os.path.basename(root_dir)
            return parent, [base_name] + rest_parts
        return root_dir, rest_parts
    # No prefix matched — assume cwd
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
    tags = _mixin_tag(node)
    parts = [tags]
    if isinstance(node, Readable):
        parts.append(f"size={node.size}")
    if isinstance(node, Addressable):
        parts.append(f"@0x{node.load_address:x}")
    parts.append(node.name)
    return "  ".join(parts)


@base.cli.group(help="VFS resource browsing")
async def resource():
    pass


@resource.command(help="List pre-populated children at a path")
@click.argument("path")
@click.option("-l", "long_format", is_flag=True,
              help="Long format: mixin tags, size, load_address")
@click.option("-r", "recursive", is_flag=True,
              help="Recurse into children")
@click.option("--depth", type=int, default=None,
              help="Max recursion depth (with -r)")
async def ls(path, long_format, recursive, depth):
    node = await _summon(path)

    def _walk(n, indent=0, current_depth=0):
        for c in n.children:
            line = _format_long(c) if long_format else c.name
            click.echo(" " * indent + line)
            if recursive and (depth is None or current_depth < depth - 1):
                _walk(c, indent + 2, current_depth + 1)

    _walk(node)


@resource.command(help="Show metadata for a node")
@click.argument("path")
async def info(path):
    node = await _summon(path)
    click.echo(f"Path:     {node.path}")
    click.echo(f"Class:    {type(node).__name__}")
    click.echo(f"Mixins:   {_mixin_tag(node)}")
    if isinstance(node, Readable):
        click.echo(f"Size:     {node.size}")
    if isinstance(node, Addressable):
        click.echo(f"Address:  0x{node.load_address:x}")
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


@resource.command(help="Copy bytes from a Readable node to a file")
@click.argument("src")
@click.argument("dst", default="-")
@click.option("--offset", type=str, default="0",
              help="Byte offset within source (decimal or 0x...)")
@click.option("--size", type=str, default=None,
              help="Bytes to copy (default: rest of source)")
async def cp(src, dst, offset, size):
    node = await _summon(src)
    if not isinstance(node, Readable):
        raise click.ClickException(
            f"{src}: node {type(node).__name__} is not Readable")
    off = int(offset, 0)
    if size is None:
        n = node.size - off
    else:
        n = int(size, 0)
    data = await node.read(off, n)
    if dst == "-":
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    else:
        with open(dst, "wb") as f:
            f.write(data)
        click.echo(f"Wrote {len(data)} bytes to {dst}", err=True)


@resource.command(help="Hex dump bytes from a Readable node")
@click.argument("path")
@click.option("--offset", type=str, default="0",
              help="Byte offset within source (decimal or 0x...)")
@click.option("--size", type=str, default=None,
              help="Bytes to dump (default: full)")
async def hexdump(path, offset, size):
    from ..util.hexdump import hexdump as do_hexdump
    node = await _summon(path)
    if not isinstance(node, Readable):
        raise click.ClickException(
            f"{path}: node {type(node).__name__} is not Readable")
    off = int(offset, 0)
    if size is None:
        n = node.size - off
    else:
        n = int(size, 0)
    data = await node.read(off, n)
    base_addr = node.load_address if isinstance(node, Addressable) else 0
    do_hexdump(base_addr + off, data, printer=click.echo)


@resource.command(help="Recursive listing with box-drawing")
@click.argument("path")
@click.option("--depth", type=int, default=None,
              help="Max recursion depth")
async def tree(path, depth):
    node = await _summon(path)

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
