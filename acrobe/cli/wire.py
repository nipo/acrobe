import asyncclick as click

from . import base
from .. import wire


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
