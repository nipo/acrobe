import asyncclick as click

from . import base
from .. import wire
from ..adapter.model import make_hw_root


@base.cli.group(help="Wire (client/server transport) tools")
async def wire_grp():
    pass


# `wire` collides with the imported module name; expose the group under
# the noun in the CLI but keep the Python identifier distinct.
wire_grp.name = "wire"


@wire_grp.command(name="dump-idl",
                  help="Print the IDL of every registered Transportable")
async def dump_idl():
    text = wire.dump_idl()
    if not text.strip():
        click.echo("(registry is empty — no Transportables loaded)")
        return
    click.echo(text)


@wire_grp.command(name="serve",
                  help="Run the wire server (REST enumeration; WS later)")
@click.option("--host", default="0.0.0.0",
              help="Bind address (default: all interfaces)")
@click.option("--port", default=8080, type=int,
              help="Bind port (default: 8080)")
async def serve(host, port):
    from aiohttp import web
    from ..wire.server import make_app

    hw_root = make_hw_root()
    app = make_app(hw_root)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    click.echo(f"acrobe wire serving on http://{host}:{port}/v1/node")
    try:
        # Sleep forever; aiohttp handles requests in background tasks.
        import asyncio
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


@wire_grp.command(name="enumerate",
                  help="Fetch a node's metadata + children from a remote server")
@click.option("-r", "--root", "url", required=True,
              help="Server base URL, e.g. http://host:8080")
@click.option("-p", "--path", default="",
              help="Node path (empty for root)")
async def enumerate_cmd(url, path):
    import json
    from ..wire.client import EnumerationClient, NodeNotFound

    async with EnumerationClient(url) as client:
        try:
            payload = await client.enumerate(path)
        except NodeNotFound as exc:
            raise click.ClickException(str(exc))
    click.echo(json.dumps(payload, indent=2))
