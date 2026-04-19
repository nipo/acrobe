import os

import asyncclick as click

from . import base


def repl_config(repl):
    repl.show_signature = True
    repl.show_docstring = True
    repl.highlight_matching_parenthesis = True
    repl.wrap_lines = True
    repl.prompt_style = "classic"
    repl.confirm_exit = False
    repl.color_depth = "DEPTH_24_BIT"
    repl.enable_syntax_highlighting = True


@base.cli.command(help="Interactive async REPL")
async def repl():
    from ptpython.repl import embed

    from ..root import root, roots
    from ..component import Component
    from ..adapter.model import HwRoot, UsbEnumerator, Adapter
    from ..target import Target, Field
    from ..target.memory import Region, Flash, Ram
    from ..protocol import jtag, swd, spi, i2c
    from ..loadable import Program, Segment

    history_dir = click.get_app_dir("acrobe")
    os.makedirs(history_dir, exist_ok=True)
    history_filename = history_dir + "/repl.history"

    await embed(
        globals=None,
        locals={
            "root": root,
            "roots": roots,
            "Component": Component,
            "HwRoot": HwRoot,
            "UsbEnumerator": UsbEnumerator,
            "Adapter": Adapter,
            "Target": Target,
            "Field": Field,
            "Region": Region,
            "Flash": Flash,
            "Ram": Ram,
            "jtag": jtag,
            "swd": swd,
            "spi": spi,
            "i2c": i2c,
            "Program": Program,
            "Segment": Segment,
        },
        configure=repl_config,
        title="Acrobe",
        history_filename=history_filename,
        return_asyncio_coroutine=True,
    )
