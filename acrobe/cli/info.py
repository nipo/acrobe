import asyncclick as click

from . import base
from ..adapter.model import make_adapter_name
from ..node import Node
from ..protocol.jtag import Chain, Tap


def _tap_annotation(tap):
    """If `tap` lives in a Chain, return a bracketed annotation
    describing whether it's currently in the scan chain and (if so)
    which TAP gates it. Empty string for taps not in a Chain or for
    bare chain-owned attached TAPs (the default, doesn't need
    noise)."""
    parent = tap._parent
    if not isinstance(parent, Chain):
        return ""
    ctx = parent._contexts.get(tap)
    if ctx is None:
        return ""
    bits = []
    if not ctx.enabled:
        bits.append("detached")
    if ctx.controller is not None:
        bits.append(f"gated by {ctx.controller.name!r}")
    if not bits:
        return ""
    return f"  [{', '.join(bits)}]"


def component_dump(comp, prefix=""):
    annotation = _tap_annotation(comp) if isinstance(comp, Tap) else ""
    click.echo(f"{prefix}{comp}{annotation}")
    if isinstance(comp, Node):
        for c in comp.children:
            component_dump(c, prefix + "  ")


@base.cli.group(help="Informational")
async def info():
    pass


@info.command(help="List recognized adapters")
@click.pass_context
async def adapters(ctx):
    hw_root = ctx.obj.hw_root
    any_found = False
    for enum in hw_root._enumerators:
        found = await enum.scan()
        if not found:
            continue
        any_found = True
        for info, adapter_cls, desc, serial in found:
            name = make_adapter_name(info, serial)
            interfaces = ", ".join(adapter_cls.supported_interfaces)
            if desc is not None and hasattr(desc, "vendor_id"):
                ident = f"{desc.vendor_id:04x}:{desc.product_id:04x}"
            elif hasattr(info, "path"):
                ident = info.path
            else:
                ident = ""
            click.echo(f"  {name}  {ident}  interfaces: {interfaces}")
    if not any_found:
        click.echo("No recognized adapters found.")


@info.command(help="Resolve root path, discover, dump component tree")
@click.option('-r', '--root', 'root_path', required=True,
              help="Component path (e.g. proby-9/jtag)")
@click.pass_context
async def enumerate(ctx, root_path):
    leaf = await ctx.obj.resolve(root_path)

    click.echo("Node tree:")
    component_dump(leaf, "  ")


@info.command(help="Walk subtree, dump CPU identification "
                    "(CPUID + feature registers) for each Cortex-M SCS")
@click.option('-r', '--root', 'root_path', required=True,
              help="Component path (e.g. lpc-link2-A0/swd/dap)")
@click.option('--full/--summary', 'full', default=False,
              help="--full prints every feature register field by "
                   "field via Bitfield.dump_pretty; default is a "
                   "one-line headline summary")
@click.pass_context
async def cpu(ctx, root_path, full):
    leaf = await ctx.obj.resolve(root_path)

    # Lazy import — keeps the CLI import-cheap when ARM components
    # aren't loaded as a plugin.
    from ..component.arm.coresight.scs import Scs

    found = leaf.children_find(lambda n: isinstance(n, Scs),
                               include_self=True) if isinstance(leaf, Node) else []
    if not found:
        click.echo(f"No SCS instances found under {leaf.fqdn}")
        return
    for scs in found:
        click.echo(f"=== {scs.fqdn} @ 0x{scs.base:x}")
        for line in await scs.dump_cpu(verbose=full):
            click.echo(line)


@info.command(help="List loaded plugins")
async def plugins():
    import traceback
    from io import StringIO
    from ..plugin import plugins as plugin_list

    if not plugin_list:
        click.echo("No plugins loaded.")
        return
    for p in plugin_list:
        status = "OK" if p.module else "Error"
        click.echo(f"{p.name} at '{p.path}' ({status})")
        if not p.module:
            click.echo("  loading error:")
            fout = StringIO()
            traceback.print_exception(*p.loading_exc_info, file=fout)
            for line in fout.getvalue().split("\n"):
                click.echo(f"    {line}")
