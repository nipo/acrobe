"""Chip programming CLI commands.

Usage:
  acrobe chip -r <root_path> [-r ...] [-t <target>] [--loadable <name>]
              program|check|readback|reset
"""

import sys
import asyncclick as click

from . import base
from ..target import Loadable, Target


@base.cli.group(help="Chip programming")
@click.option('-r', '--root', 'root_paths', required=True, multiple=True,
              help="Component path (e.g. proby-9/jtag-pt)")
@click.option('-t', '--target', 'target_sel', default="0",
              help="Target index or name (default: 0)")
@click.option('--loadable', 'loadable_sel', default=None,
              help="Loadable child name when the Target has several")
@click.pass_context
async def chip(ctx, root_paths, target_sel, loadable_sel):
    hw_root = ctx.obj.hw_root
    for path in root_paths:
        await ctx.obj.resolve(path)

    await hw_root.discover_targets()

    targets = hw_root.children_of_class(Target)
    if not targets:
        click.echo("No targets found.", err=True)
        sys.exit(1)

    target = None
    try:
        target = targets[int(target_sel)]
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

    loadables = target.children_of_class(Loadable)
    if not loadables:
        click.echo(f"Target {target.name!r} has no Loadable child.", err=True)
        sys.exit(1)

    loadable = None
    if loadable_sel is None:
        if len(loadables) > 1:
            names = ", ".join(l.name for l in loadables)
            click.echo(
                f"Target {target.name!r} has multiple Loadables ({names}); "
                f"select one with --loadable.", err=True)
            sys.exit(1)
        loadable = loadables[0]
    else:
        for l in loadables:
            if l.name == loadable_sel:
                loadable = l
                break
        if loadable is None:
            names = ", ".join(l.name for l in loadables)
            click.echo(
                f"Loadable {loadable_sel!r} not found in {target.name!r}. "
                f"Available: {names}", err=True)
            sys.exit(1)

    click.echo(f"Target: {target.name}  Loadable: {loadable.name}")
    ctx.obj.target = target
    ctx.obj.loadable = loadable


@chip.command(help="Program target with a single resource")
@click.argument('resource', type=base.RESOURCE)
@click.option('-e', '--erase', is_flag=True, help="Erase before programming")
@click.option('-c', '--check', 'verify', is_flag=True, help="Verify after programming")
@click.option('--run', is_flag=True, help="Reset after programming")
@click.option('-C', '--assume-clean', is_flag=True, help="Assume flash is blank")
@click.pass_context
async def program(ctx, resource, erase, verify, run, assume_clean):
    loadable = ctx.obj.loadable
    node = await resource.resolve()
    await loadable.write(node, do_erase=erase, do_verify=verify,
                         do_start=run, assume_clean=assume_clean)
    click.echo("Done.")


@chip.command(help="Verify target contents against a resource")
@click.argument('resource', required=True, type=base.RESOURCE)
@click.pass_context
async def check(ctx, resource):
    loadable = ctx.obj.loadable
    node = await resource.resolve()
    ok = await loadable.verify(node)
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
    loadable = ctx.obj.loadable
    m = await loadable.read(begin=begin, end=end)
    save(m, filename)
    click.echo(f"Read back to {filename}.")


@chip.command(help="Reset target")
@click.pass_context
async def reset(ctx):
    loadable = ctx.obj.loadable
    await loadable.reset()
    click.echo("Reset.")


@chip.command("erase-all", help="Mass-erase the target (uses vendor "
                                "mass-erase when available — fast, "
                                "and on Nordic also clears APPROTECT)")
@click.pass_context
async def erase_all(ctx):
    loadable = ctx.obj.loadable
    await loadable.erase_all()
    click.echo("Erased.")
