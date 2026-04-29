"""CLI: expose a local SerialPort over RFC 2217."""

import asyncclick as click

from . import base
from ..adapter.model import make_hw_root
from ..protocol.serial import SerialPort
from ..rfc2217 import Rfc2217Listener


@base.cli.command(name="serial-server",
                  help="Expose a serial port over RFC 2217")
@click.option("-r", "--root", "root_path", required=True,
              help="Component path to a SerialPort (e.g. tty-ttyUSB0/serial)")
@click.option("-p", "--port", "tcp_port", type=int, default=2217,
              help="TCP port to listen on (default 2217)")
@click.option("-H", "--host", type=str, default="0.0.0.0",
              help="Bind address (default 0.0.0.0)")
async def serial_server(root_path, tcp_port, host):
    parts = root_path.strip("/").split("/")
    hw_root = make_hw_root()
    leaf = await hw_root.child_summon(*parts)
    if not isinstance(leaf, SerialPort):
        raise click.ClickException(
            f"{root_path!r} does not resolve to a SerialPort "
            f"(got {type(leaf).__name__})")
    await leaf.start_tree()
    listener = Rfc2217Listener(leaf, host=host, port=tcp_port)
    click.echo(f"RFC 2217 server on {host}:{tcp_port} for {leaf.fqdn}")
    await listener.serve_forever()
