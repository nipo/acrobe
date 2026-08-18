"""Interactive root helper.

Provides a convenience function to resolve a component path string
into a started component, suitable for use in scripts and interactive
sessions (e.g. ``python -i``, IPython, ptpython).

A script defines an ``async def main()`` and is run with
``acrobe run script.py [args…]``: acrobe brings the event loop,
logging, plugin loading and lifecycle teardown, sets ``sys.argv``,
and awaits ``main()``. Usage::

    from acrobe.root import root

    async def main():
        tap = await root("proby-9/jtag/tap0")
        # … interact with tap …
"""

from .adapter.model import get_hw_root
from .node import Node
from .plugin import load_plugins


async def roots(*paths):
    """Resolve multiple component paths and return them as a list.

    Each *path* is a ``"/"``-separated component path such as
    ``"proby-9/jtag/tap0"`` with optional ``"name(opt1,opt2)"`` syntax.

    Plugins are loaded, a :class:`HwRoot` with the standard set of
    enumerators is set up, and every leaf component's tree is
    started before it is returned.
    """
    load_plugins()

    hw_root = get_hw_root()
    await hw_root.ensure_started()

    result = []
    for path in paths:
        parts = path.strip("/").split("/")
        leaf = await hw_root.child_summon(*parts)
        if isinstance(leaf, Node):
            await leaf.start_tree()
        result.append(leaf)
    return result


async def root(path):
    """Resolve a single component path and return the leaf component.

    Shorthand for ``(await roots(path))[0]``.
    """
    return (await roots(path))[0]
