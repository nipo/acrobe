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
    """Create HwRoot with USB and TTY enumerators."""
    root = HwRoot()
    root.add_enumerator(UsbEnumerator())
    try:
        from ..adapter.tty import TtyEnumerator
        root.add_enumerator(TtyEnumerator())
    except ImportError:
        pass
    return root


@info.command(help="List recognized adapters")
async def adapters():
    hw_root = _make_hw_root()
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
async def enumerate(root_path):
    parts = root_path.strip("/").split("/")
    hw_root = _make_hw_root()

    leaf = await hw_root.child_summon(*parts)

    # Start the leaf's subtree so children are populated
    # (e.g. Chain.start() discovers TAPs)
    if isinstance(leaf, Node):
        await leaf.start_tree()

    click.echo("Node tree:")
    component_dump(leaf, "  ")


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
