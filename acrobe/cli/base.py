import logging
import runpy

import asyncclick as click

from .. import log


_LOADED_EXTENSIONS: set[str] = set()


def _load_extension(path: str) -> None:
    """Execute *path* once per process.

    Used by the root group's ``-x`` option to register classes (Db
    handlers, target probes, …) defined in an ad-hoc script before
    the CLI walks any path. ``run_name`` is set to ``acrobe_ext`` so
    a ported crobe-style ``if __name__ == "__main__":`` block in the
    script stays dormant.
    """
    import os
    abs_path = os.path.abspath(path)
    if abs_path in _LOADED_EXTENSIONS:
        return
    _LOADED_EXTENSIONS.add(abs_path)
    runpy.run_path(abs_path, run_name="acrobe_ext")


def _exec_callback(ctx, param, value):
    # ``value`` is a tuple of paths (multiple=True). Eager: this
    # fires before the rest of the root-group options resolve, so
    # registrations land before subcommand dispatch and target
    # discovery.
    for path in value or ():
        _load_extension(path)
    return value


class AcrobeGroup(click.Group):
    """click.Group with two acrobe-wide defaults applied:

    * ``-h`` is accepted everywhere as an alias for ``--help``.
      Set via ``context_settings['help_option_names']``; Click
      propagates it down to every subcommand's Context, so a
      single declaration on the root group covers the whole tree.
    * Invoking a group without a subcommand prints help instead of
      exiting silently — ``no_args_is_help=True``. Click doesn't
      inherit this for nested groups, so :meth:`group` defaults
      ``cls=AcrobeGroup`` to keep the behaviour propagating as the
      tree grows.

    Subclasses passing their own ``context_settings`` /
    ``no_args_is_help`` win — these are setdefaults, not
    overrides.
    """

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("no_args_is_help", True)
        ctx_settings = dict(kwargs.get("context_settings") or {})
        ctx_settings.setdefault("help_option_names", ["-h", "--help"])
        kwargs["context_settings"] = ctx_settings
        super().__init__(*args, **kwargs)

    def group(self, *args, **kwargs):
        kwargs.setdefault("cls", AcrobeGroup)
        return super().group(*args, **kwargs)


class CliContext:
    """Shared per-invocation CLI state.

    Holds a lazily-built :class:`HwRoot` so that multiple subcommands
    in the same invocation — and, once command chaining lands,
    multiple commands run via operators like ``&`` or ``;`` —
    operate on the same hardware tree. One USB open per adapter,
    one ``Batcher`` per chain, and operations from concurrent
    commands naturally interleave through the asyncio engine.

    Subcommands access this via ``@click.pass_context`` and
    ``ctx.obj``. Group-specific state (e.g. ``chip``'s selected
    target) attaches as plain attributes.
    """

    def __init__(self):
        self.chained = False

    @property
    def hw_root(self):
        from ..adapter.model import get_hw_root
        return get_hw_root()

    async def resolve(self, path):
        """Walk *path* through the shared :class:`HwRoot`, start its
        subtree, and return the leaf :class:`Node`."""
        from ..node import Node
        hw_root = self.hw_root
        # Start the root first so its enumerators have populated the
        # adapter children child_summon will look up.
        await hw_root.ensure_started()
        parts = path.strip("/").split("/")
        leaf = await hw_root.child_summon(*parts)
        if isinstance(leaf, Node):
            await leaf.start_tree()
        return leaf


@click.group(cls=AcrobeGroup)
@click.option('-x', '--exec', 'exec_paths',
              multiple=True,
              type=click.Path(exists=True, dir_okay=False),
              is_eager=True, expose_value=False,
              callback=_exec_callback,
              help=("Load a Python file before running the command. "
                    "Useful to register one-off Db handlers (SwDp / Ap / "
                    "Datagram / Pipe subclasses) without packaging an "
                    "acrobe_plugin entry point. May be passed multiple "
                    "times; each path is loaded at most once."))
@click.option('-v', '--verbose', count=True, help="More verbosity")
@click.option('-q', '--quiet', count=True, help="Less verbosity (hide progress bars)")
@click.option('-t', '--timestamp', is_flag=True, help="Add timestamps to log")
@click.option('-s', '--short-name', is_flag=True, help="Log with short origin name")
@click.option('-b', '--no-color', is_flag=True, help="Don't color log")
@click.option('--silent', multiple=True, type=str,
              help="Silent one component by name")
@click.option('--silent-re', type=str, default=None,
              help="Silent components by regex")
@click.option('--only-re', type=str, default=None,
              help="Only show components matching regex")
@click.pass_context
async def cli(ctx, verbose, quiet, timestamp, no_color,
              silent, silent_re, only_re, short_name):
    if ctx.obj is None:
        ctx.obj = CliContext()

    # Drain anything registered with acrobe.lifecycle after the CLI
    # command finishes. Registered via ctx.call_on_close (not
    # cli.result_callback) so it runs on exception paths too — without
    # this, USB contexts leak past interpreter shutdown and ausb's
    # daemon event thread can be torn down mid-libusb-call, triggering
    # a pthread_mutex_destroy assertion from libusb.
    #
    # When chaining (`a & b ; c`), the dispatcher in :mod:`.chain`
    # pre-injects a shared :class:`CliContext` flagged ``chained`` and
    # drains itself once at the end — this hook then steps aside so
    # resources opened in segment N stay alive for segment N+1.
    cli_ctx_obj = ctx.obj
    async def _drain_lifecycle():
        if getattr(cli_ctx_obj, "chained", False):
            return
        from .. import lifecycle
        await lifecycle.shutdown()
    ctx.call_on_close(_drain_lifecycle)

    base_index = log.LEVELS.index(logging.ERROR)
    target = min(max(0, base_index + verbose - quiet), len(log.LEVELS) - 1)
    level = log.LEVELS[target]

    # Progress bars: shown only at exact default verbosity (no -v,
    # no -q). Any -v means the user wants log messages, which a tqdm
    # bar would interleave with badly; any -q means they want
    # quieter output — both cases want NullProgress.
    progress = log.TqdmProgress() if (verbose == 0 and quiet == 0) else None

    log.setup(
        level=level,
        color=not no_color,
        timestamp=timestamp,
        silent=silent,
        silent_re=silent_re,
        only_re=only_re,
        short_name=short_name,
        progress=progress,
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


class ResourceRef:
    """Deferred VFS resource resolver.

    Click's `ParamType.convert` is sync, so it can't await the VFS
    walk that turns a path into a started Node. ResourceRef holds
    the path string and exposes async helpers; commands await
    `.resolve()` (or `.memory_map()`) inside their async body.
    """

    def __init__(self, path: str):
        self.path = path
        self.__node = None

    async def resolve(self):
        """Walk the VFS and return the started leaf Node."""
        if self.__node is None:
            from .loadable import _summon
            self.__node = await _summon(self.path)
        return self.__node

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
