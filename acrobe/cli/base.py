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


@cli.result_callback()
async def _drain_lifecycle(result, **kwargs):
    """Drain anything still registered with acrobe.lifecycle after a
    CLI command finishes. Library users call `acrobe.shutdown()`
    themselves; for the CLI, this hook makes it automatic."""
    from .. import lifecycle
    await lifecycle.shutdown()


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


class ResourceRef:
    """Deferred VFS resource resolver.

    Click's `ParamType.convert` is sync, so it can't await the VFS
    walk that turns a path into a started Node. ResourceRef holds
    the path string and exposes async helpers; commands await
    `.resolve()` (or `.memory_map()`) inside their async body.
    """

    def __init__(self, path: str):
        self.path = path
        self._node = None

    async def resolve(self):
        """Walk the VFS and return the started leaf Node."""
        if self._node is None:
            from .loadable import _summon
            self._node = await _summon(self.path)
        return self._node

    async def memory_map(self):
        """Resolve and build a MemoryMap by walking the subtree's
        Readable+Addressable descendants."""
        from ..memory_map import MemoryMap
        node = await self.resolve()
        return await MemoryMap.from_node(node)


class ResourceParamType(click.ParamType):
    name = "resource"

    def convert(self, value, param, ctx):
        if isinstance(value, ResourceRef):
            return value
        return ResourceRef(value)


RESOURCE = ResourceParamType()
