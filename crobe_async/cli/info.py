import asyncclick as click

from . import base
from ..adapter.model import UsbEnumerator
from ..component import Component


def component_dump(comp, prefix=""):
    click.echo(f"{prefix}{comp}")
    if isinstance(comp, Component):
        for c in comp.children:
            component_dump(c, prefix + "  ")


@base.cli.group(help="Informational")
async def info():
    pass


def _import_adapters():
    """Import known adapter modules to populate adapter_db."""
    from ..adapter import proby as _  # noqa: F401


@info.command(help="List recognized USB adapters")
async def adapters():
    _import_adapters()
    enum = UsbEnumerator()
    found = await enum.scan()
    if not found:
        click.echo("No recognized adapters found.")
        return
    for info, adapter_cls, desc, serial in found:
        name = f"{info.name}-{serial}" if serial else info.name
        interfaces = ", ".join(adapter_cls.supported_interfaces)
        click.echo(
            f"  {name}  "
            f"{info.vid:04x}:{info.pid:04x}  "
            f"interfaces: {interfaces}"
        )


@info.command(help="Resolve root path, discover, dump component tree")
@click.option('-r', '--root', 'root_path', required=True,
              help="Component path (e.g. USB/Proby/jtag)")
async def enumerate(root_path):
    _import_adapters()
    from ..protocol.jtag import Chain

    parts = root_path.strip("/").split("/")

    hw_root = Component("HwRoot")
    enum = UsbEnumerator()
    hw_root.child_add(enum)

    leaf = await hw_root.child_summon(*parts)

    click.echo("Root resolution:")
    component_dump(hw_root, "  ")

    # If leaf looks like a JTAG interface, discover chain
    if hasattr(leaf, 'post'):
        chain = Chain(leaf)
        click.echo("\nDiscovering JTAG chain...")
        await chain.discover()
        click.echo(f"Found {len(chain.children)} device(s):")
        for tap in chain.children:
            click.echo(f"  IDCODE=0x{tap.idcode:08x} irlen={tap.irlen}")


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
