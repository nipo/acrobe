"""CLI: expose a local JtagInterface as a JoP server."""

import asyncclick as click

from . import base
from ..protocol.jtag import JtagInterface
from ..jop.listener import JopListener

@base.cli.group(help="Altera-related")
async def altera():
    pass


@altera.command(help="Expose a JTAG interface as an Altera JoP "
                    "(JTAG-over-Protocol) server")
@click.option("-r", "--root", "root_path", required=True,
              help="Component path to a JtagInterface "
                   "(e.g. proby-9/jtag)")
@click.option("-p", "--port", "tcp_port", type=int, default=1259,
              help="TCP port to listen on (default 1259, the Altera "
                   "etherlink default)")
@click.option("-H", "--host", type=str, default="0.0.0.0",
              help="Bind address (default 0.0.0.0)")
@click.option("--mgmt", "mgmt_support", is_flag=True, default=False,
              help="Advertise MGMT_SUPPORT=1 in the welcome banner. "
                   "Default off — we don't decode the MGMT side-channel "
                   "yet, and Quartus skips it when MGMT_SUPPORT=0.")
@click.pass_context
async def jop_server(ctx, root_path, tcp_port, host, mgmt_support):
    leaf = await ctx.obj.resolve(root_path)
    if not isinstance(leaf, JtagInterface):
        raise click.ClickException(
            f"{root_path!r} does not resolve to a JtagInterface "
            f"(got {type(leaf).__name__})")
    listener = JopListener(leaf, host=host, port=tcp_port,
                           mgmt_support=mgmt_support)
    click.echo(f"JoP server on {host}:{tcp_port} for {leaf.fqdn}")
    await listener.serve_forever()
