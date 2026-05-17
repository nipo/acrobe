"""Operator-based command chaining for the acrobe CLI.

Lets users run multiple commands per invocation, separated by
shell-style operator tokens that must appear as standalone argv
entries (escape them in the shell, e.g. ``\\&`` or ``'&'``):

* ``&`` — run the segments on either side concurrently.
* ``;`` — run them sequentially.

All segments share a single :class:`acrobe.cli.base.CliContext`, so
e.g. ``xvc-server -r adapter/jtag '&' jop-server -r adapter/jtag``
opens the adapter once and posts both servers' JTAG ops to the same
``Batcher`` — they interleave through the asyncio engine.

Mixed forms run as ``;``-separated groups of ``&``-parallel
segments: ``a '&' b ';' c`` runs ``a`` and ``b`` in parallel,
awaits both, then runs ``c``.

Global options (``-v``, ``--silent``, etc.) belong to the very
front of argv and are re-prepended to every segment so each
``cli.main`` call parses an internally-valid command line.
"""

import asyncio
import sys

import asyncclick as click

from . import base
from .. import lifecycle, plugin


PARALLEL = "&"
SEQUENTIAL = ";"
SUPPORTED_OPS = (PARALLEL, SEQUENTIAL)
RESERVED_OPS = ("&&", "||")


class ChainDispatcher:
    """Splits an argv into operator-separated segments and runs them
    against a shared :class:`CliContext`."""

    def __init__(self, argv):
        self.argv = list(argv)
        self.shared = base.CliContext()
        self.shared.chained = True

    @classmethod
    def has_operators(cls, argv):
        return any(t in SUPPORTED_OPS or t in RESERVED_OPS for t in argv)

    def __split(self):
        """Return ``[(op, [tokens]), ...]`` — first ``op`` is None."""
        segments = []
        current_op = None
        current = []
        for tok in self.argv:
            if tok in RESERVED_OPS:
                raise click.UsageError(
                    f"Operator {tok!r} not yet supported "
                    f"(supported: {', '.join(SUPPORTED_OPS)})")
            if tok in SUPPORTED_OPS:
                segments.append((current_op, current))
                current_op = tok
                current = []
            else:
                current.append(tok)
        segments.append((current_op, current))
        return segments

    async def __extract_globals(self, first_args):
        """Run the cli group's parser over *first_args* to peel off
        the leading global options. Returns
        ``(globals_argv, subcommand_argv)``."""
        if not first_args:
            return [], []
        parser_ctx = await base.cli.make_context(
            "acrobe", list(first_args), resilient_parsing=True)
        rest = list(parser_ctx.protected_args) + list(parser_ctx.args)
        n_globals = len(first_args) - len(rest)
        return list(first_args[:n_globals]), rest

    async def __run_segment(self, globals_argv, segment_args):
        args = list(globals_argv) + list(segment_args)
        await base.cli.main(
            args=args, prog_name="acrobe",
            obj=self.shared, standalone_mode=False)

    @classmethod
    def __group_by_sequential(cls, segments):
        """Bucket segments into ``;``-separated groups; each group is
        a list of ``&``-parallel segments."""
        groups = [[segments[0][1]]]
        for op, seg in segments[1:]:
            if op == SEQUENTIAL:
                groups.append([seg])
            else:
                groups[-1].append(seg)
        return groups

    async def run(self):
        segments = self.__split()
        if segments[0][0] is not None:
            raise click.UsageError("Operator before first command")
        for op, seg in segments:
            if not seg:
                raise click.UsageError(
                    "Empty segment around operator (check escaping)")

        first_op, first_args = segments[0]
        globals_argv, first_remaining = await self.__extract_globals(first_args)
        if not first_remaining:
            raise click.UsageError("Missing subcommand before operator")
        segments[0] = (first_op, first_remaining)

        groups = self.__group_by_sequential(segments)

        try:
            for group in groups:
                if len(group) == 1:
                    await self.__run_segment(globals_argv, group[0])
                else:
                    await asyncio.gather(
                        *[self.__run_segment(globals_argv, seg)
                          for seg in group])
        finally:
            await lifecycle.shutdown()


def main():
    plugin.load_plugins()

    argv = sys.argv[1:]
    if ChainDispatcher.has_operators(argv):
        try:
            asyncio.run(ChainDispatcher(argv).run())
        except click.ClickException as e:
            e.show()
            sys.exit(e.exit_code)
        except click.exceptions.Exit as e:
            sys.exit(e.exit_code)
        return

    base.cli(prog_name="acrobe")
