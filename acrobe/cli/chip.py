"""Chip programming CLI commands.

Usage:
  acrobe chip -r <root_path> [-r ...] [-t <target>] program|check|readback|reset
"""

import sys
import asyncclick as click

from . import base
from ..adapter.model import make_hw_root
from ..target import Target, Field
from ..node import Node


@base.cli.group(help="Chip programming")
@click.option('-r', '--root', 'root_paths', required=True, multiple=True,
              help="Component path (e.g. proby-9/jtag-pt)")
@click.option('-t', '--target', 'target_sel', default="0",
              help="Target index or name (default: 0)")
@click.pass_context
async def chip(ctx, root_paths, target_sel):
    hw_root = make_hw_root()

    roots = []
    for path in root_paths:
        parts = path.strip("/").split("/")
        leaf = await hw_root.child_summon(*parts)
        if isinstance(leaf, Node):
            await leaf.start_tree()
        roots.append(leaf)

    field = Field()
    await field.discover(*roots)

    targets = field.children_of_class(Target)
    if not targets:
        click.echo("No targets found.", err=True)
        sys.exit(1)

    # Select target by index or name
    target = None
    try:
        idx = int(target_sel)
        target = targets[idx]
    except (ValueError, IndexError):
        for t in targets:
            if target_sel.lower() in t.name.lower():
                target = t
                break

    if target is None:
        click.echo(f"Target {target_sel!r} not found. Available:", err=True)
        for i, t in enumerate(targets):
            click.echo(f"  {i}: {t.name}", err=True)
        sys.exit(1)

    click.echo(f"Target: {target.name}")
    ctx.ensure_object(dict)
    ctx.obj["target"] = target
    ctx.obj["hw_root"] = hw_root


@chip.command(help="Program target with a single resource")
@click.argument('resource', type=base.RESOURCE)
@click.option('-e', '--erase', is_flag=True, help="Erase before programming")
@click.option('-c', '--check', 'verify', is_flag=True, help="Verify after programming")
@click.option('--run', is_flag=True, help="Reset after programming")
@click.option('-C', '--assume-clean', is_flag=True, help="Assume flash is blank")
@click.pass_context
async def program(ctx, resource, erase, verify, run, assume_clean):
    target = ctx.obj["target"]
    node = await resource.resolve()
    await target.write(node, do_erase=erase, do_verify=verify,
                       do_start=run, assume_clean=assume_clean)
    click.echo("Done.")


@chip.command(help="Verify target contents against a resource")
@click.argument('resource', required=True, type=base.RESOURCE)
@click.pass_context
async def check(ctx, resource):
    target = ctx.obj["target"]
    node = await resource.resolve()
    ok = await target.verify(node)
    if ok:
        click.echo("Verify OK.")
    else:
        click.echo("Verify FAILED.", err=True)
        sys.exit(1)


@chip.command(help="Read back target contents to a file")
@click.argument('filename', type=click.Path())
@click.option('--begin', type=base.HEX, default=0, help="Start address (hex)")
@click.option('--end', type=base.HEX, default=None, help="End address (hex)")
@click.pass_context
async def readback(ctx, filename, begin, end):
    from ..memory_map import save
    target = ctx.obj["target"]
    m = await target.read(begin=begin, end=end)
    save(m, filename)
    click.echo(f"Read back to {filename}.")


@chip.command(help="Reset target")
@click.pass_context
async def reset(ctx):
    target = ctx.obj["target"]
    await target.reset()
    click.echo("Reset.")
