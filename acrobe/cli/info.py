import asyncclick as click

from . import base
from ..adapter.model import HwRoot, UsbEnumerator, make_adapter_name
from ..node import Node


def component_dump(comp, prefix=""):
    click.echo(f"{prefix}{comp}")
    if isinstance(comp, Node):
        for c in comp.children:
            component_dump(c, prefix + "  ")


@base.cli.group(help="Informational")
async def info():
    pass


def _make_hw_root():
    """Create HwRoot with USB enumerator."""
    root = HwRoot()
    root.add_enumerator(UsbEnumerator())
    return root


@info.command(help="List recognized USB adapters")
async def adapters():
    enum = UsbEnumerator()
    found = await enum.scan()
    if not found:
        click.echo("No recognized adapters found.")
        return
    for info, adapter_cls, desc, serial in found:
        name = make_adapter_name(info, serial)
        interfaces = ", ".join(adapter_cls.supported_interfaces)
        click.echo(
            f"  {name}  "
            f"{desc.vendor_id:04x}:{desc.product_id:04x}  "
            f"interfaces: {interfaces}"
        )


@info.command(help="Resolve root path, discover, dump component tree")
@click.option('-r', '--root', 'root_path', required=True,
              help="Component path (e.g. proby-9/jtag)")
async def enumerate(root_path):
    from ..protocol.jtag import Chain

    parts = root_path.strip("/").split("/")
    hw_root = _make_hw_root()

    leaf = await hw_root.child_summon(*parts)

    # Start the leaf's subtree so children are populated
    # (e.g. Chain.start() discovers TAPs)
    if isinstance(leaf, Node):
        await leaf.start_tree()

    click.echo("Node tree:")
    component_dump(leaf, "  ")

    # If leaf is a raw JTAG interface (Batcher but not Component), discover chain
    if hasattr(leaf, 'post') and not isinstance(leaf, Node):
        chain = Chain(leaf)
        click.echo("\nDiscovering JTAG chain...")
        await chain.discover()
        click.echo(f"Found {len(chain.children)} device(s):")
        for tap in chain.children:
            click.echo(
                f"  {tap.name}  "
                f"IDCODE=0x{tap.idcode:08x}  "
                f"irlen={tap.irlen}  "
                f"({type(tap).__name__})"
            )


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
