"""Run-control CLI commands.

Usage:
  acrobe debug -r <root_path> [-c <core>] halt|resume|step|reset|state
  acrobe debug -r <root_path> [-c <core>] reg read [<name>...]
  acrobe debug -r <root_path> [-c <core>] reg write <name>=<value>...

The `-r` path identifies a component (typically a DAP / DP) the
discovery sweep can reach. The first Target with a Debuggable child
under the root is selected; `-c` picks among multiple cores by index
or name. State is a one-shot inspection — no continuous run.
"""

import sys
import asyncclick as click

from . import base
from ..target import Target
from ..target.debuggable import Debuggable


async def _resolve_debuggable(ctx, root_paths, core_sel):
    hw_root = ctx.obj.hw_root
    for path in root_paths:
        await ctx.obj.resolve(path)
    await hw_root.discover_targets()

    debuggables = []
    for target in hw_root.children_of_class(Target):
        debuggables.extend(target.children_of_class(Debuggable))
    if not debuggables:
        click.echo("No Debuggable found under any Target.", err=True)
        sys.exit(1)
    debug = debuggables[0]

    cores = debug.cores
    if not cores:
        click.echo(f"Debuggable {debug.name!r} has no Core children.",
                   err=True)
        sys.exit(1)

    core = None
    if core_sel is None:
        if len(cores) > 1:
            names = ", ".join(c.name for c in cores)
            click.echo(
                f"Debuggable has multiple Cores ({names}); select "
                f"with -c.", err=True)
            sys.exit(1)
        core = cores[0]
    else:
        try:
            core = cores[int(core_sel)]
        except (ValueError, IndexError):
            for c in cores:
                if core_sel.lower() in c.name.lower():
                    core = c
                    break
        if core is None:
            names = ", ".join(c.name for c in cores)
            click.echo(f"Core {core_sel!r} not found. Available: {names}",
                       err=True)
            sys.exit(1)
    return debug, core


@base.cli.group(help="Cortex-M run-control")
@click.option('-r', '--root', 'root_paths', required=True, multiple=True,
              help="Component path the Target derives from")
@click.option('-c', '--core', 'core_sel', default=None,
              help="Core index or name when the Debuggable has many")
@click.pass_context
async def debug(ctx, root_paths, core_sel):
    debug, core = await _resolve_debuggable(ctx, root_paths, core_sel)
    await debug.attach()
    ctx.obj.debug = debug
    ctx.obj.core = core


@debug.command(help="Halt the core")
@click.pass_context
async def halt(ctx):
    await ctx.obj.core.halt()
    state = await ctx.obj.core.state()
    click.echo(f"State: {state.name}")


@debug.command(help="Resume the core")
@click.option('--no-interrupts', is_flag=True, default=False,
              help="Mask interrupts while running")
@click.pass_context
async def resume(ctx, no_interrupts):
    await ctx.obj.core.resume(allow_interrupts=not no_interrupts)
    state = await ctx.obj.core.state()
    click.echo(f"State: {state.name}")


@debug.command(help="Single-step the core")
@click.pass_context
async def step(ctx):
    await ctx.obj.core.step()
    state = await ctx.obj.core.state()
    click.echo(f"State: {state.name}")


@debug.command(help="Reset the target")
@click.option('--run', is_flag=True, default=False,
              help="Don't catch reset; let the core start running")
@click.pass_context
async def reset(ctx, run):
    await ctx.obj.core.reset(stop=not run)
    state = await ctx.obj.core.state()
    click.echo(f"State: {state.name}")


@debug.command(help="Report core state + halt cause")
@click.pass_context
async def state(ctx):
    state = await ctx.obj.core.state()
    cause = await ctx.obj.core.halt_cause()
    click.echo(f"State:  {state.name}")
    click.echo(f"Cause:  {cause.name}")


@debug.group(help="Read or write CPU registers")
@click.pass_context
async def reg(ctx):
    pass


@reg.command("read", help="Read one or more registers (default: all)")
@click.argument('names', nargs=-1)
@click.pass_context
async def reg_read(ctx, names):
    core = ctx.obj.core
    if not names:
        names = [r.name for r in core.registers]
    values = await core.reg_read(list(names))
    for r in sorted(values, key=lambda r: r.number):
        click.echo(f"  {r.name:<5} 0x{values[r]:08x}")


@reg.command("write", help="Write registers: name=value [...]")
@click.argument('assignments', nargs=-1, required=True)
@click.pass_context
async def reg_write(ctx, assignments):
    core = ctx.obj.core
    pairs = {}
    for a in assignments:
        if "=" not in a:
            click.echo(f"Bad assignment {a!r}; expected name=value",
                       err=True)
            sys.exit(1)
        name, raw = a.split("=", 1)
        try:
            value = int(raw, 0)
        except ValueError:
            click.echo(f"Bad value {raw!r} for {name}", err=True)
            sys.exit(1)
        pairs[name.strip()] = value
    await core.reg_write(pairs)
    click.echo(f"Wrote {len(pairs)} register(s).")
