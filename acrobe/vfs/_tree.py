"""Helper for building a Node hierarchy from flat archive entries.

ZIP, tar, and similar formats store entries as flat paths
("a/b/c.bin"). This module turns that into a tree where intermediate
directory Nodes are synthesised on demand.
"""

from ..node import Node


class DirectoryNode(Node):
    """Synthesised intermediate directory node for archive entries."""


def build_archive_tree(target: Node, entries):
    """Attach a tree of children to `target` from a flat list of
    `(path, node_factory)` entries.

    `path` is a "/"-separated archive path. Path components are
    split; any non-leaf component yields a `DirectoryNode` (created
    once, reused). The final component invokes `node_factory(name)`
    to produce the leaf.

    Empty path components ("a//b"), "." or ".." segments are rejected.
    """
    for path, factory in entries:
        if not path or path.startswith("/"):
            raise ValueError(f"invalid archive entry path: {path!r}")
        parts = path.rstrip("/").split("/")
        for p in parts:
            if not p or p in (".", ".."):
                raise ValueError(f"invalid archive entry component: {p!r}")
        cur = target
        for part in parts[:-1]:
            existing = None
            for c in cur._children:
                if c._name == part:
                    existing = c
                    break
            if existing is None:
                d = DirectoryNode(part)
                cur._child_attach(d)
                cur = d
            else:
                if not isinstance(existing, DirectoryNode):
                    raise ValueError(
                        f"archive entry {path!r} conflicts with "
                        f"existing non-directory child {part!r}")
                cur = existing
        leaf = factory(parts[-1])
        cur._child_attach(leaf)
