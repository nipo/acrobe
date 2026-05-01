"""CLI: expose a local JtagInterface as a JoP server."""

import asyncclick as click

from . import base
from ..adapter.model import make_hw_root
from ..protocol.jtag import JtagInterface
from ..jop.listener import JopListener


@base.cli.command(name="jop-server",
                  help="Expose a JTAG interface as an Altera JoP "
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
async def jop_server(root_path, tcp_port, host, mgmt_support):
    parts = root_path.strip("/").split("/")
    hw_root = make_hw_root()
    leaf = await hw_root.child_summon(*parts)
    if not isinstance(leaf, JtagInterface):
        raise click.ClickException(
            f"{root_path!r} does not resolve to a JtagInterface "
            f"(got {type(leaf).__name__})")
    await leaf.start_tree()
    listener = JopListener(leaf, host=host, port=tcp_port,
                           mgmt_support=mgmt_support)
    click.echo(f"JoP server on {host}:{tcp_port} for {leaf.fqdn}")
    await listener.serve_forever()
