"""Utilities for treating Node subtrees as addressed memory maps.

A "program view" is an iterable of (load_address, bytes) pairs
collected by walking the Readable+Addressable descendants of a
Node subtree. It is the VFS-native replacement for the old
Program/Segment in-memory model.

Until Program/Segment are fully removed, `program_view.from_node`
is the bridge: it walks a started Node subtree and returns a
Program built from the addressable leaves.
"""

from .node import Node, Readable, Addressable


def addressable_descendants(root: Node):
    """Yield every descendant of `root` (including `root` itself)
    that implements both `Readable` and `Addressable`. Skips
    intermediate non-addressable container nodes."""
    stack = [root]
    while stack:
        node = stack.pop()
        if isinstance(node, Readable) and isinstance(node, Addressable):
            yield node
        # Children visited regardless — an addressable leaf may
        # have addressable descendants (e.g. ELF section + symbols).
        stack.extend(reversed(node._children))


async def collect_segments(root: Node):
    """Collect (load_address, bytes) pairs for every
    Readable+Addressable descendant of `root`. Convenient for
    building a Program from a started subtree."""
    out = []
    for node in addressable_descendants(root):
        data = await node.read(0, node.size)
        out.append((node.load_address, bytes(data)))
    return out


async def from_node(node: Node, offset: int = 0):
    """Build a `Program` from a started Node subtree.

    Each Readable+Addressable descendant becomes one Segment.
    `offset` is added to every segment's address (used by callers
    that want to relocate the entire view)."""
    from .loadable import Program, Segment
    p = Program(node.fqdn)
    for addr, data in await collect_segments(node):
        p.append(Segment(addr + offset, data, node.fqdn))
    return p
