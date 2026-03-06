import logging
import asyncclick as click

from .. import log


@click.group()
@click.option('-v', '--verbose', count=True, help="More verbosity")
@click.option('-q', '--quiet', count=True, help="Less verbosity")
@click.option('-t', '--timestamp', is_flag=True, help="Add timestamps to log")
@click.option('-b', '--no-color', is_flag=True, help="Don't color log")
@click.option('--silent', multiple=True, type=str,
              help="Silent one component by name")
@click.option('--silent-re', type=str, default=None,
              help="Silent components by regex")
@click.option('--only-re', type=str, default=None,
              help="Only show components matching regex")
async def cli(verbose, quiet, timestamp, no_color, silent, silent_re, only_re):
    base_index = log.LEVELS.index(logging.ERROR)
    target = min(max(0, base_index + verbose - quiet), len(log.LEVELS) - 1)
    level = log.LEVELS[target]

    log.setup(
        level=level,
        color=not no_color,
        timestamp=timestamp,
        silent=silent,
        silent_re=silent_re,
        only_re=only_re,
        progress=log.TqdmProgress() if level <= log.TRACE else None,
    )


# --- Custom param types ---

class HexParamType(click.ParamType):
    name = "hex"

    def convert(self, value, param, ctx):
        if isinstance(value, int):
            return value
        try:
            return int(value, 16)
        except ValueError:
            self.fail(f"{value!r} is not a valid hex integer", param, ctx)


HEX = HexParamType()


class ProgramParamType(click.ParamType):
    name = "program"

    def convert(self, value, param, ctx):
        from ..loadable import Program
        return Program.from_file(value)


PROGRAM = ProgramParamType()
