"""CLI: expose a local JtagInterface as an XVC server."""

import asyncclick as click

from . import base
from ..adapter.model import make_hw_root
from ..protocol.jtag import JtagInterface
from ..xvc.listener import XvcListener


@base.cli.command(name="xvc-server",
                  help="Expose a JTAG interface as a Xilinx Virtual "
                       "Cable (XVC) server")
@click.option("-r", "--root", "root_path", required=True,
              help="Component path to a JtagInterface "
                   "(e.g. proby-9/jtag)")
@click.option("-p", "--port", "tcp_port", type=int,
              default=XvcListener.DEFAULT_PORT,
              help=f"TCP port to listen on (default "
                   f"{XvcListener.DEFAULT_PORT})")
@click.option("-H", "--host", type=str, default="0.0.0.0",
              help="Bind address (default 0.0.0.0)")
async def xvc_server(root_path, tcp_port, host):
    parts = root_path.strip("/").split("/")
    hw_root = make_hw_root()
    leaf = await hw_root.child_summon(*parts)
    if not isinstance(leaf, JtagInterface):
        raise click.ClickException(
            f"{root_path!r} does not resolve to a JtagInterface "
            f"(got {type(leaf).__name__})")
    await leaf.start_tree()
    listener = XvcListener(leaf, host=host, port=tcp_port)
    click.echo(f"XVC server on {host}:{tcp_port} for {leaf.fqdn}")
    await listener.serve_forever()
