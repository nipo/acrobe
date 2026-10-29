"""Run-control CLI commands.

Usage:
  acrobe debug -r <root_path> [-d <debuggable>] [-c <core>] halt|resume|step|reset|state
  acrobe debug -r <root_path> [-d <debuggable>] [-c <core>] reg read [<name>...]
  acrobe debug -r <root_path> [-d <debuggable>] [-c <core>] reg write <name>=<value>...
  acrobe debug -r <root_path> [-d <debuggable>] mem read <addr> [<size>] [-o <file>]
  acrobe debug -r <root_path> [-d <debuggable>] mem write <addr> (<word>... | -i <file>)

The `-r` path identifies a component (typically a DAP / DP) the
discovery sweep can reach. `-d` picks a Debuggable across every
discovered Target by index or name (default: the first one) — e.g.
``debug-core1`` on chips with one Debuggable per core; `-c` picks
among that Debuggable's cores by index or name. State is a one-shot
inspection — no continuous run.

Attaching halts the Debuggable's cores. Commands other than halt /
resume / step / reset hand them back as found: cores that were
running are resumed, and if halting debug (Cortex-M DHCSR.C_DEBUGEN)
was off before, it's turned off again — left on, a BKPT the firmware
or bootrom executes later halts the core instead of faulting. The
same restore of C_DEBUGEN follows ``resume``. ``state`` reports the
state from before the attach.
"""

import sys
import asyncclick as click

from . import base
from ..target import Target
from ..target.debuggable import CoreState, Debuggable


def _select(nodes, sel, kind):
    """Pick one of ``nodes`` by index, exact name, or name substring
    (case-insensitive, first match wins)."""
    try:
        return nodes[int(sel)]
    except (ValueError, IndexError):
        pass
    for match in (lambda n: n.name.lower() == sel.lower(),
                  lambda n: sel.lower() in n.name.lower()):
        for n in nodes:
            if match(n):
                return n
    names = ", ".join(n.name for n in nodes)
    click.echo(f"{kind} {sel!r} not found. Available: {names}", err=True)
    sys.exit(1)


async def _resolve_debuggable(ctx, root_paths, debug_sel, core_sel):
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
    if debug_sel is None:
        debug = debuggables[0]
    else:
        debug = _select(debuggables, debug_sel, "Debuggable")

    cores = debug.cores
    if not cores:
        click.echo(f"Debuggable {debug.name!r} has no Core children.",
                   err=True)
        sys.exit(1)

    if core_sel is None:
        if len(cores) > 1:
            names = ", ".join(c.name for c in cores)
            click.echo(
                f"Debuggable has multiple Cores ({names}); select "
                f"with -c.", err=True)
            sys.exit(1)
        core = cores[0]
    else:
        core = _select(cores, core_sel, "Core")
    return debug, core


@base.cli.group(help="Cortex-M run-control")
@click.option('-r', '--root', 'root_paths', required=True, multiple=True,
              help="Component path the Target derives from")
@click.option('-d', '--debuggable', 'debug_sel', default=None,
              help="Debuggable index or name (default: the first one)")
@click.option('-c', '--core', 'core_sel', default=None,
              help="Core index or name when the Debuggable has many")
@click.pass_context
async def debug(ctx, root_paths, debug_sel, core_sel):
    debug, core = await _resolve_debuggable(
        ctx, root_paths, debug_sel, core_sel)
    before = {c: (await c.state(), await _debugen(c)) for c in debug.cores}
    await debug.attach()
    ctx.obj.debug = debug
    ctx.obj.core = core
    ctx.obj.state_before = before[core][0]
    # Set by commands that put the core in a run state on purpose.
    ctx.obj.run_control = False

    async def restore():
        try:
            if not ctx.obj.run_control:
                for c, (state, _) in before.items():
                    if state != CoreState.HALT:
                        await c.resume()
            if (all(en is False for _, en in before.values())
                    and all([await c.state() != CoreState.HALT
                             for c in debug.cores])):
                await debug.detach()
        except Exception as exc:
            click.echo(f"Restoring core state failed: {exc}", err=True)
    ctx.call_on_close(restore)


async def _debugen(core):
    """Whether halting debug is enabled on ``core``, or None if the
    core doesn't expose it (non-Cortex-M)."""
    scs = getattr(core, "scs", None)
    if scs is None:
        return None
    return bool(await scs.read_dhcsr() & scs.DHCSR_C_DEBUGEN)


@debug.command(help="Halt the core")
@click.pass_context
async def halt(ctx):
    ctx.obj.run_control = True
    await ctx.obj.core.halt()
    state = await ctx.obj.core.state()
    click.echo(f"State: {state.name}")


@debug.command(help="Resume the core")
@click.option('--no-interrupts', is_flag=True, default=False,
              help="Mask interrupts while running")
@click.pass_context
async def resume(ctx, no_interrupts):
    ctx.obj.run_control = True
    await ctx.obj.core.resume(allow_interrupts=not no_interrupts)
    state = await ctx.obj.core.state()
    click.echo(f"State: {state.name}")


@debug.command(help="Single-step the core")
@click.pass_context
async def step(ctx):
    ctx.obj.run_control = True
    await ctx.obj.core.step()
    state = await ctx.obj.core.state()
    click.echo(f"State: {state.name}")


@debug.command(help="Reset the target")
@click.option('--run', is_flag=True, default=False,
              help="Don't catch reset; let the core start running")
@click.pass_context
async def reset(ctx, run):
    ctx.obj.run_control = True
    await ctx.obj.core.reset(stop=not run)
    state = await ctx.obj.core.state()
    click.echo(f"State: {state.name}")


@debug.command(help="Report core state + halt cause")
@click.pass_context
async def state(ctx):
    # attach() just halted the core; report what it was doing before
    state = ctx.obj.state_before
    click.echo(f"State:  {state.name}")
    if state == CoreState.HALT:
        cause = await ctx.obj.core.halt_cause()
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


def _parse_int(raw, what):
    try:
        return int(raw, 0)
    except ValueError:
        click.echo(f"Bad {what} {raw!r}", err=True)
        sys.exit(1)


@debug.group(help="Read or write memory through the Debuggable's bus")
@click.pass_context
async def mem(ctx):
    pass


@mem.command("read", help="Read <size> bytes (default 4) at <addr>; "
                          "hex dump, or raw bytes to a file with -o")
@click.argument('addr')
@click.argument('size', default="4")
@click.option('-o', '--output', type=click.File('wb'), default=None,
              help="Write the raw bytes to this file instead")
@click.pass_context
async def mem_read(ctx, addr, size, output):
    from ..util.hexdump import hexdump
    addr = _parse_int(addr, "address")
    size = _parse_int(size, "size")
    data = await ctx.obj.debug.mem_read(addr, size)
    if output is not None:
        output.write(data)
        click.echo(f"Read {len(data)} byte(s) from 0x{addr:08x} "
                   f"to {output.name}.")
    else:
        hexdump(addr, data, printer=click.echo)


@mem.command("write", help="Write 32-bit little-endian words at <addr>, "
                           "or a file's raw bytes with -i")
@click.argument('addr')
@click.argument('words', nargs=-1)
@click.option('-i', '--input', 'input_', type=click.File('rb'),
              default=None, help="Write this file's raw bytes")
@click.pass_context
async def mem_write(ctx, addr, words, input_):
    addr = _parse_int(addr, "address")
    if (input_ is None) == (not words):
        click.echo("Give either words or -i <file>", err=True)
        sys.exit(1)
    if input_ is not None:
        data = input_.read()
    else:
        data = b"".join(
            (_parse_int(w, "word") & 0xffffffff).to_bytes(4, "little")
            for w in words)
    await ctx.obj.debug.mem_write(addr, data)
    click.echo(f"Wrote {len(data)} byte(s) at 0x{addr:08x}.")
