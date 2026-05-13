"""CLI: expose a local SerialPort over RFC 2217."""

import asyncclick as click

from . import base
from ..protocol.serial import SerialPort
from ..rfc2217 import Rfc2217Listener


@base.cli.command(name="serial-server",
                  help="Expose a serial port over RFC 2217")
@click.option("-a", "--adapter", "adapter_paths", multiple=True,
              help="Component path to resolve + run target discovery "
                   "from before resolving --root. Use when --root "
                   "addresses a Target-tree node (e.g. an RTT pipe) "
                   "and no other chained command is bringing the "
                   "Target online")
@click.option("-r", "--root", "root_path", required=True,
              help="Path to a SerialPort (e.g. tty-ttyUSB0/serial or "
                   "nrf52840/memory/sram/rtt(up=0,down=0))")
@click.option("-p", "--port", "tcp_port", type=int, default=2217,
              help="TCP port to listen on (default 2217)")
@click.option("-H", "--host", type=str, default="0.0.0.0",
              help="Bind address (default 0.0.0.0)")
@click.pass_context
async def serial_server(ctx, adapter_paths, root_path, tcp_port, host):
    # Resolve adapters first (populates component tree) and let
    # target discovery run before --root tries to walk past a
    # to-be-discovered Target name.
    for ap in adapter_paths:
        await ctx.obj.resolve(ap)
    if adapter_paths:
        await ctx.obj.hw_root.discover_targets()

    leaf = await ctx.obj.resolve(root_path)
    if not isinstance(leaf, SerialPort):
        raise click.ClickException(
            f"{root_path!r} does not resolve to a SerialPort "
            f"(got {type(leaf).__name__})")
    listener = Rfc2217Listener(leaf, host=host, port=tcp_port)
    click.echo(f"RFC 2217 server on {host}:{tcp_port} for {leaf.fqdn}")
    await listener.serve_forever()
