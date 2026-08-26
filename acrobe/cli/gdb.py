"""GDB Remote Serial Protocol server CLI command.

Usage:
  acrobe gdb-server -r <component_path> [--port 3333] [--host localhost]
                    [--target <name>] [--core <name>] [--loadable <name>]
"""

import sys
import asyncclick as click

from . import base
from ..target import Loadable, Target
from ..target.debuggable import Debuggable
from ..target.gdb import GdbServer


@base.cli.command(help="Serve a GDB Remote Serial Protocol session")
@click.option('-r', '--root', 'root_paths', required=True, multiple=True,
              help="Component path the Target derives from")
@click.option('--host', default="localhost",
              help="Bind address (default: localhost)")
@click.option('--port', default=3333, type=int,
              help="TCP port (default: 3333)")
@click.option('--target', 'target_sel', default=None,
              help="Target index or name when several are discovered")
@click.option('--loadable', 'loadable_sel', default=None,
              help="Loadable child name when the Target has several "
                   "(controls vFlashErase/Write routing)")
@click.pass_context
async def gdb_server(ctx, root_paths, host, port,
                     target_sel, loadable_sel):
    hw_root = ctx.obj.hw_root
    for path in root_paths:
        await ctx.obj.resolve(path)
    await hw_root.discover_targets()

    targets = hw_root.children_of_class(Target)
    if not targets:
        click.echo("No Targets discovered under the resolved roots.",
                   err=True)
        sys.exit(1)

    target = _pick_target(targets, target_sel)
    if target is None:
        click.echo(f"Target {target_sel!r} not found. Available:", err=True)
        for i, t in enumerate(targets):
            click.echo(f"  {i}: {t.name}", err=True)
        sys.exit(1)

    debuggables = target.children_of_class(Debuggable)
    if not debuggables:
        click.echo(f"Target {target.name!r} has no Debuggable.",
                   err=True)
        sys.exit(1)
    debuggable = debuggables[0]

    loadable = _pick_loadable(target, loadable_sel)
    if loadable_sel is not None and loadable is None:
        click.echo(f"Loadable {loadable_sel!r} not found in target.",
                   err=True)
        sys.exit(1)

    click.echo(f"Target:     {target.name}")
    click.echo(f"Debuggable: {debuggable.name}  "
               f"(cores: {[c.name for c in debuggable.cores]})")
    if loadable is not None:
        click.echo(f"Loadable:   {loadable.name}")
    click.echo(f"Listening on {host}:{port}")

    await debuggable.attach()
    server = GdbServer(debuggable, loadable, host=host, port=port)
    try:
        await server.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        click.echo("Shutting down.")
    finally:
        await server.close()


def _pick_target(targets, sel):
    if sel is None:
        return targets[0]
    try:
        return targets[int(sel)]
    except (ValueError, IndexError):
        pass
    for t in targets:
        if sel.lower() in t.name.lower():
            return t
    return None


def _pick_loadable(target, sel):
    loadables = target.children_of_class(Loadable)
    if not loadables:
        return None
    if sel is None:
        return loadables[0] if len(loadables) == 1 else None
    for l in loadables:
        if l.name == sel:
            return l
    return None
