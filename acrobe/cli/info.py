import asyncclick as click

from . import base
from ..adapter.model import make_adapter_name
from ..node import Node
from ..protocol.jtag import Chain, Tap
from ..util.pretty import base2


def _tap_annotation(tap):
    """If `tap` lives in a Chain, return a bracketed annotation
    describing whether it's currently in the scan chain and (if so)
    which TAP gates it. Empty string for taps not in a Chain or for
    bare chain-owned attached TAPs (the default, doesn't need
    noise)."""
    parent = tap._parent
    if not isinstance(parent, Chain):
        return ""
    ctx = parent.contexts.get(tap)
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
    for enum in hw_root.enumerators:
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


@info.command(help="Run target discovery and list spawned Targets")
@click.option('-r', '--root', 'root_paths', required=True, multiple=True,
              help="Component path to resolve before discovery")
@click.option('-v', '--verbose', is_flag=True, default=False,
              help="Show each Target's view children: Loadable regions, "
                   "Debuggable cores + memory map, etc.")
@click.pass_context
async def target(ctx, root_paths, verbose):
    from ..target import Loadable, Target
    from ..target.debuggable import Core, Debuggable
    from ..target.region import Flash, Region

    hw_root = ctx.obj.hw_root
    for path in root_paths:
        await ctx.obj.resolve(path)

    await hw_root.discover_targets()

    targets = hw_root.children_of_class(Target)
    if not targets:
        click.echo("No targets found.")
        return

    for t in targets:
        if not verbose:
            click.echo(f"  {t.name}")
            for loadable in t.children_of_class(Loadable):
                click.echo(f"    loadable: {loadable.name}")
            continue

        click.echo(f"{t.name}  [{type(t).__name__}]")
        claimed = sorted(c.fqdn for c in t.claimed_components())
        if claimed:
            click.echo(f"  references:")
            for c in claimed:
                click.echo(f"    - {c}")
        for child in t.children:
            _dump_view(child)


def _dump_view(view):
    from ..target import Loadable
    from ..target.debuggable import Core, Debuggable
    from ..target.memory import Memory
    from ..target.region import Flash, Region

    if isinstance(view, Memory):
        click.echo(f"  memory:   {view.name}  [{type(view).__name__}]")
        regions = sorted(view.children_of_class(Region),
                         key=lambda r: r.address)
        for r in regions:
            span = f"0x{r.address:08x}-0x{r.end:08x}  ({base2(r.size, 'B')})"
            click.echo(f"    {type(r).__name__:<10} {r.name:<8} {span}")
        return

    if isinstance(view, Loadable):
        click.echo(f"  loadable: {view.name}  [{type(view).__name__}]")
        regions = sorted(view.regions, key=lambda r: r.address)
        if not regions:
            click.echo(f"    (no regions)")
        for r in regions:
            kind = type(r).__name__
            span = f"0x{r.address:08x}-0x{r.end:08x}  ({base2(r.size, 'B')})"
            extras = []
            if isinstance(r, Flash):
                extras.append(f"write_page={base2(r.write_page_size, 'B')}")
                extras.append(f"erase_pages={'/'.join(base2(p, 'B') for p in r.erase_page_sizes)}")
                extras.append(f"blank={r.is_blank}")
            tail = ("  " + "  ".join(extras)) if extras else ""
            click.echo(f"    {kind:<14} {r.name:<10} {span}{tail}")
        return

    if isinstance(view, Debuggable):
        click.echo(f"  debug:    {view.name}  [{type(view).__name__}]")
        cores = view.cores
        if not cores:
            click.echo(f"    (no cores)")
        for c in cores:
            click.echo(
                f"    core      {c.name:<10}  "
                f"feature={c.gdb_feature_name!r}  "
                f"byteorder={c.gdb_byteorder}  "
                f"#regs={len(c.registers)}")
        mm = view.memory_map
        if mm:
            click.echo(f"    memory_map:")
            for r in sorted(mm, key=lambda r: r.address):
                click.echo(f"      {type(r).__name__:<14} {r.name:<10} "
                           f"0x{r.address:08x}-0x{r.end:08x}")
        return

    # Generic child (Puppet, DebugAuth, custom)
    click.echo(f"  child:    {view.name}  [{type(view).__name__}]")


@info.command(help="Walk the target tree, dump CPU identification "
                    "for every Core in every Debuggable")
@click.option('-r', '--root', 'root_path', required=True,
              help="Component path the Target derives from")
@click.option('--full/--summary', 'full', default=False,
              help="--full prints every feature register field; "
                   "default is a one-line headline summary")
@click.pass_context
async def cpu(ctx, root_path, full):
    from ..target import Target
    from ..target.debuggable import Core, Debuggable

    hw_root = ctx.obj.hw_root
    await ctx.obj.resolve(root_path)
    await hw_root.discover_targets()

    any_core = False
    for target in hw_root.children_of_class(Target):
        for debug in target.children_of_class(Debuggable):
            for core in debug.children_of_class(Core):
                any_core = True
                click.echo(f"=== {core.fqdn}")
                if hasattr(core, "dump_cpu"):
                    for line in await core.dump_cpu(verbose=full):
                        click.echo(line)
                else:
                    click.echo(
                        f"  (no dump available for {type(core).__name__})")
    if not any_core:
        click.echo(f"No Cores found under {root_path}")


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
