"""Node — the unified tree base class plus byte-access mixins.

A `Node` is one element in acrobe's hierarchical model. The same class
covers hardware components (adapters, JTAG TAPs, regions, ...) and
file/format containers (POF, ZIP, ELF, ...). Composition naturally
mixes both: e.g. a flash region (Node) can have a POF (Node) child
that exposes its partitions (Nodes) as RBF (Nodes).

A Node MAY additionally implement one or more of the byte-access
mixins:

- `Readable`   — the node exposes one contiguous blob of bytes.
- `Writable`   — the node accepts in-place writes (passthrough only).
- `Addressable`— the node carries address metadata for target loading.

Mixins are independent; a Node opts in to whichever apply. See
`docs/vfs-design.md` for the full design.
"""

import asyncio
import functools
import logging
import os
from contextlib import asynccontextmanager, contextmanager

from .db import NoMatch
from .log import get_progress


class Path:
    """Static utilities for slash-separated paths used in the Node
    tree and on the event bus, plus the symlink-aware canonicaliser
    for filesystem paths.

    Used by:

    - `Node.path` callers needing structural queries (descendant,
      parent, components).
    - Event-bus subscribers, who must canonicalise the path they
      subscribe to so it matches what publishers will emit. The
      bus matches strings verbatim — no fuzzy resolution at
      subscription time.

    All helpers are static. The class is the namespace, not a
    constructible thing.
    """

    @staticmethod
    def parts(path: str) -> tuple[str, ...]:
        """Split into components. Empty path or `/` → `()`."""
        if not path:
            return ()
        return tuple(p for p in path.split("/") if p)

    @staticmethod
    def parent_of(path: str) -> str | None:
        """Path of the parent. Returns None when `path` is the
        root (`""`, `"/"`, or a single component without leading
        `/`)."""
        if not path:
            return None
        if path.startswith("/"):
            # Absolute (filesystem-style): root is "/".
            if path == "/":
                return None
            head = path.rsplit("/", 1)[0]
            return head or "/"
        # Relative (Node-tree-style): single segment has no parent.
        if "/" not in path:
            return None
        return path.rsplit("/", 1)[0]

    @staticmethod
    def is_descendant_or_self(path: str, ancestor: str) -> bool:
        """True if `path` equals `ancestor` or is a descendant
        of it. Empty ancestor matches every path (treated as the
        universal root)."""
        if ancestor == "":
            return True
        if path == ancestor:
            return True
        # Trailing-slash-insensitive: treat "a/b/" and "a/b" the same.
        anchor = ancestor.rstrip("/")
        return path.startswith(anchor + "/")

    @staticmethod
    def canonicalize_fs(path) -> str:
        """Resolve symlinks for the existing prefix of `path`.

        Returns an absolute path. Components that exist on disk
        are resolved through the kernel (so symlinks are followed
        to real paths); the first non-existing component and
        everything beyond it are appended literally.

        This is the form the OS notifier reports under: events
        fire under the real directory, never under symlink
        aliases. Subscribers on a non-canonical path would never
        match.
        """
        abs_path = os.path.abspath(os.fspath(path))
        # Walk component by component from the root.
        segments = abs_path.split(os.sep)
        # segments[0] is "" for an absolute path.
        resolved = os.sep
        existing = True
        for segment in segments[1:]:
            if not segment:
                continue
            candidate = os.path.join(resolved, segment)
            if existing and os.path.lexists(candidate):
                # Resolve any symlink at this level. Use realpath
                # so chained symlinks collapse to the real target.
                resolved = os.path.realpath(candidate)
            else:
                # Past the existing prefix — append literally.
                existing = False
                resolved = os.path.join(resolved, segment)
        return resolved

    @classmethod
    def canonicalize_hw(cls, root: "Node", path: str) -> str:
        """Walk the Node tree from `root` resolving each segment
        of `path` through `child_lookup` to its canonical name.

        Stops at the first segment with no match — remaining
        segments are appended literally (analogous to the FS
        case for nodes that don't exist yet, e.g. waiting for
        a hotplug to surface a USB child).

        Non-spawning: never calls `child_spawn`, never does IO.
        For canonicalisation through unsummoned subtrees, call
        `await root.child_summon(...)` first and use the
        resulting node's `.path`.

        The leading segment of the result is `root.name` (so the
        result is a fully-qualified path comparable with the
        `.path` of any descendant).
        """
        segments = cls.parts(path)
        # Strip leading root.name if the caller redundantly
        # included it — both forms accepted.
        if segments and segments[0] == root.name:
            segments = segments[1:]
        canonical = [root.name]
        node = root
        for segment in segments:
            if node is None:
                canonical.append(segment)
                continue
            child = node.child_lookup(segment)
            if child is None:
                canonical.append(segment)
                node = None
            else:
                canonical.append(child.name)
                node = child
        return "/".join(canonical)


class _NotKvList(Exception):
    """Raised by _parse_kv_list when content doesn't match the
    options grammar. Tells the caller to treat the input as a
    plain name."""


def _split_top_level_commas(content: str) -> list:
    """Split on commas at top paren-depth, respecting quotes."""
    depth = 0
    in_quote = False
    escaped = False
    parts = []
    start = 0
    for i, ch in enumerate(content):
        if in_quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_quote = False
            continue
        if ch == '"':
            in_quote = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                raise _NotKvList("unmatched ')'")
        elif ch == "," and depth == 0:
            parts.append(content[start:i])
            start = i + 1
    if depth != 0 or in_quote:
        raise _NotKvList("unbalanced state")
    parts.append(content[start:])
    return parts


def _decode_quoted(s: str) -> str:
    """Decode a quoted string. s starts with `"` and ends with `"`.

    Recognised escapes inside quotes: `\\"` and `\\\\`.
    """
    inner = s[1:-1]
    out = []
    i = 0
    while i < len(inner):
        ch = inner[i]
        if ch == "\\" and i + 1 < len(inner):
            nxt = inner[i + 1]
            if nxt == '"':
                out.append('"')
                i += 2
                continue
            if nxt == "\\":
                out.append("\\")
                i += 2
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _parse_kv_list(content: str) -> dict:
    """Parse "k=v,k=v,..." (with quoted values supported) into dict.

    Bare keys (no `=`) are a parse error. Empty content raises
    _NotKvList (callers handle the empty-options-escape case before
    calling here).
    """
    if not content:
        raise _NotKvList("empty kv list")
    entries = _split_top_level_commas(content)
    result = {}
    for entry in entries:
        # Find the FIRST '=' at top level (key cannot contain '=')
        eq_pos = -1
        depth = 0
        in_quote = False
        escaped = False
        for i, ch in enumerate(entry):
            if in_quote:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_quote = False
                continue
            if ch == '"':
                in_quote = True
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "=" and depth == 0:
                eq_pos = i
                break
        if eq_pos < 0:
            raise _NotKvList(f"missing '=' in {entry!r}")
        key = entry[:eq_pos]
        value = entry[eq_pos + 1:]
        # Keys: no whitespace, no special chars; just take literally
        # but reject empty.
        if not key:
            raise _NotKvList(f"empty key in {entry!r}")
        if any(c in key for c in " \t\"\\"):
            raise _NotKvList(f"invalid char in key {key!r}")
        # Values: quoted or bare. Bare values reject whitespace and
        # quotes (per D10).
        if value.startswith('"') and value.endswith('"') and len(value) >= 2:
            decoded = _decode_quoted(value)
        elif '"' in value or any(c in value for c in " \t"):
            raise _NotKvList(f"bare value has whitespace or quote: {value!r}")
        else:
            decoded = value
        result[key] = decoded
    return result


class Readable:
    """A node exposing one contiguous blob of bytes.

    The contract:

    - `size` is sync, populated during start(). Async size queries on
      every range check would hurt readability for nothing.
    - `read(offset, size)` is async: this is where actual IO happens
      (file handle, USB transfer, JTAG cycles, ...).
    - `read` follows POSIX-pread semantics: returns at most `size`
      bytes; may return fewer at end of node. ValueError if
      offset < 0 or offset > self.size.
    """

    @property
    def size(self) -> int:
        """Total bytes addressable. Set during start()."""
        raise NotImplementedError

    async def read(self, offset: int, size: int) -> bytes:
        """Read up to `size` bytes from `offset`.

        Returns at most `size` bytes; may return fewer at end of node.
        Raises ValueError if offset < 0 or offset > self.size.
        """
        raise NotImplementedError


class Writable(Readable):
    """A node accepting in-place writes.

    Available only when every container in the chain is passthrough —
    byte offsets in the child map directly to bytes in the parent's
    storage. Containers that transform bytes (compression, CRC,
    rewriting headers) MUST NOT implement Writable; they finalise
    in stop() if at all.
    """

    async def write(self, offset: int, data: bytes) -> None:
        """Write `data` at `offset`.

        Must satisfy offset + len(data) <= self.size; raises
        ValueError otherwise. Finalisation (CRC, recompression)
        happens in stop(), not on each write.
        """
        raise NotImplementedError


class Addressable:
    """A node carrying address metadata for target loading.

    `load_address` is the canonical address — where this content
    should be placed in target memory. Use cases:

    - Flash region: flash address.
    - ihex region: record address.
    - ELF segment: LMA.

    `addresses` exposes named aliases. Default contains just
    `{'load': load_address}`; subclasses may expose more (e.g. ELF
    sections add 'vma', 'lma', 'file_offset').
    """

    @property
    def load_address(self) -> int:
        """Canonical address for loading into target memory."""
        raise NotImplementedError

    @property
    def addresses(self) -> dict:
        """Named address aliases. Default exposes just 'load'."""
        return {"load": self.load_address}


class Node:
    """Base class for the acrobe tree.

    Children lifecycle:
    - child_add(child) attaches a child. If the parent is already
      started, the child's start_tree() is scheduled automatically
      via ensure_future. Safe to call from __init__ (parent won't
      be started yet).
    - child_remove(child) is async: it stops the child's entire
      subtree (stop_tree), then detaches it.
    - child_transplant_to(new_parent) moves every child of this
      node onto new_parent without stopping anything (used by the
      VFS format auto-detection: a parser is built transiently and
      its discovered children are reparented onto the host Node).
    - start_tree() walks top-down: calls start() on self, marks
      started, then recurses into existing children.
    - stop_tree() walks top-down: calls stop() on self, clears
      started, then recurses into children.

    Path resolution:
    - child_lookup(name) finds an existing pre-populated child by
      name (substring match), index, "*" (single child), or
      ".." (parent).
    - child_spawn(name) creates a new child on demand. Override in
      subclasses. Raises NoMatch by default.
    - child_summon(*parts) walks a path, looking up or spawning at
      each step. Spawned Nodes are added to the tree (which
      triggers auto-start if parent is started).

    Pre-populated vs on-demand children:
    - Pre-populated children are created in start() and live under
      this node — visible via .children and listed by
      `acrobe loadable ls`.
    - On-demand children (e.g. `as(type=...)`, ELF symbols) are
      not in .children but reachable via child_spawn /
      child_summon.
    """

    def __init__(self, name: str):
        self.__name = name
        self.__parent = None
        self.__children = []
        self.__started = False
        # Underlying storage stays as `_metadata` so subclasses that
        # override the `metadata` property to produce a merged view
        # (e.g. FileNode) can still mutate the base dict.
        self._metadata: dict = {}
        # Single-flight bookkeeping so concurrent child_summon /
        # start_tree calls don't open the same hardware twice. Lazy
        # to avoid bind-to-loop on Nodes that are constructed before
        # an event loop exists.
        self.__summon_inflight: dict[str, asyncio.Future] = {}
        self.__start_lock: asyncio.Lock | None = None
        # Event-bus subscriptions registered through self.subscribe()
        # — auto-cancelled when this Node's stop_tree runs.
        self.__subscriptions: list = []
        # Pending attach event, set by __child_attach when this
        # Node enters the tree. Drained on the next async path
        # through this Node (ensure_started or child_remove) so
        # the lifecycle is consistent regardless of whether
        # attach happened in sync setup or in a live tree.
        # Tuple `(parent_path,)` or None.
        self.__pending_attach: tuple | None = None

    def __str__(self):
        return self.__name

    def __repr__(self):
        return f"<{self.__class__.__name__} '{self.__name}'>"

    @property
    def name(self) -> str:
        return self.__name

    @name.setter
    def name(self, value: str) -> None:
        # fqdn / path / logger are computed fresh on each access, so
        # renaming the node immediately reflects in the next logger
        # lookup. Subclasses that discover their final name during
        # start() rely on this.
        self.__name = value

    @property
    def parent(self):
        return self.__parent

    @property
    def children(self) -> list:
        """Pre-populated children only. On-demand children
        (e.g. `as(...)`, ELF symbols) are not listed here.
        Returns a fresh copy each call — mutating it does not
        affect the underlying tree."""
        return list(self.__children)

    @property
    def fqdn(self) -> str:
        """Dotted name of this node from the root.

        This is the *logger* name — Python's logging hierarchy
        relies on '.' for parent/child relationships and prefix
        filters. Use `path` for display in CLI / messages."""
        parts = []
        node = self
        while node is not None:
            parts.append(node.__name)
            node = node.__parent
        parts.reverse()
        return ".".join(parts)

    @property
    def path(self) -> str:
        """Slash-separated path from the root, matching the
        VFS path syntax (`a/b/c`). Suitable for display."""
        parts = []
        node = self
        while node is not None:
            parts.append(node.__name)
            node = node.__parent
        parts.reverse()
        return "/".join(parts)

    @property
    def logger(self) -> logging.Logger:
        return logging.getLogger(self.fqdn)

    @property
    def started(self) -> bool:
        return self.__started

    @property
    def metadata(self) -> dict:
        """Free-form metadata for inspection (`acrobe loadable info`).
        Subclasses may override this as a read-only property that
        returns a computed view; in that case they keep populating
        the base `_metadata` storage in start()."""
        return self._metadata

    @metadata.setter
    def metadata(self, value: dict) -> None:
        self._metadata = value

    def __child_attach(self, child: "Node"):
        """Silent attach. Internal — used by __lookup_or_spawn so
        that child_summon can take the inflight lock and then start
        the child itself without racing the auto-start path.

        Records a pending `(attach, post)` on the child. The emit
        actually fires on the next async path through the child:

        - `ensure_started` drains it before emitting `start`, so
          every node sees `attach → start` in order.
        - `child_remove` drains it before emitting `detach`, so a
          child added then removed without ever starting still
          gets a paired `attach → detach`.

        This avoids the sync/async mismatch around `__init__` —
        Nodes are constructed sync (no loop yet), but their
        attach events still reach subscribers once the tree
        becomes live.
        """
        assert child.__parent is None, f"{child.fqdn} already has a parent"
        child.__parent = self
        self.__children.append(child)
        self.children_changed()
        child.__pending_attach = (self.path,)

    async def __drain_pending_attach(self) -> None:
        """Emit the deferred attach POST, if any. Atomic
        read-and-clear so concurrent callers can't double-emit."""
        pending = self.__pending_attach
        self.__pending_attach = None
        if pending is None:
            return
        from .event import Event, Phase, get_bus
        (parent_path,) = pending
        await get_bus().emit(Event(
            source=self.path, action="attach",
            phase=Phase.POST, properties={"parent": parent_path}))

    def child_add(self, child: "Node"):
        """Public eager-attach. Auto-starts the child if this parent
        is already started; otherwise the start happens when this
        parent's start_tree() recurses."""
        self.__child_attach(child)
        if self.__started:
            asyncio.ensure_future(child.start_tree())

    async def child_remove(self, child: "Node"):
        """Stop the child's subtree, then detach.

        Drains any pending attach on the child first — so a child
        added then removed without ever starting still emits a
        paired `attach → detach` lifecycle.

        Emits `(detach, pre)` before stop, `(detach, post)` after
        detach. The POST event's `source` is the path the child
        had while attached — by then, `child.path` no longer
        reflects where it lived.
        """
        from .event import Event, Phase, get_bus
        assert child.__parent is self, f"{child.fqdn} is not a child of {self.fqdn}"
        await child.__drain_pending_attach()
        child_path = child.path
        parent_path = self.path
        await child.emit("detach", phase=Phase.PRE,
                         parent=parent_path)
        await child.stop_tree()
        self.__children.remove(child)
        child.__parent = None
        self.children_changed()
        await get_bus().emit(Event(
            source=child_path, action="detach",
            phase=Phase.POST, properties={"parent": parent_path}))

    def child_transplant_to(self, new_parent: "Node") -> None:
        """Move every child of this node onto `new_parent`, in order,
        without stopping anything. Used by the VFS format auto-
        detection path: a parser is built transiently, its discovered
        children are then reparented onto the host node.
        """
        moved = list(self.__children)
        self.__children.clear()
        for child in moved:
            child.__parent = new_parent
            new_parent.__children.append(child)
        self.children_changed()
        new_parent.children_changed()

    def children_changed(self):
        pass

    def children_find(self, predicate, include_self=False) -> list:
        result = []
        if include_self and predicate(self):
            result.append(self)
        for child in self.__children:
            result.extend(child.children_find(predicate, include_self=True))
        return result

    def children_of_class(self, klass, include_self=False) -> list:
        return self.children_find(lambda c: isinstance(c, klass), include_self=include_self)

    def parent_of_class(self, klass):
        node = self.__parent
        while node is not None:
            if isinstance(node, klass):
                return node
            node = node.__parent
        raise LookupError(f"No ancestor of class {klass.__name__} found from {self.fqdn}")

    @contextmanager
    def progress(self, label, total, unit=""):
        """Context manager for progress tracking.

        Delegates to the global ProgressDelegate (set via log.set_progress).
        """
        handle = get_progress().create(self.fqdn, label, total, unit)
        try:
            yield handle
        finally:
            handle.close()

    def option_set(self, key, value):
        """Apply an option to this node. Override in subclasses.

        Called after spawn (or after lookup of a pre-populated child)
        and before start(). Subclasses can defer interpretation to
        their start() if the option's effect requires construction.
        """
        raise ValueError(f"Unknown option: {key}={value!r}")

    def child_hints(self) -> list[str]:
        """Names of children that *could* be summoned but aren't
        materialized yet.

        Sync, no side effects — must not touch hardware. Subclasses
        override to expose static manifests (a known list of protocol
        names, etc.). Default returns []. Dynamic discovery (probing
        a USB bus, scanning a JTAG chain) is a separate concern, not
        covered by this method.
        """
        return []

    def child_lookup(self, name):
        """Find existing pre-populated child by name.

        Lookup order:
        1. ".." → parent.
        2. "*"  → the only child (if exactly one).
        3. Integer index into children.
        4. Exact-name match (case sensitive).
        5. Case-insensitive substring match (unique match only —
           returns None on ambiguity).

        Returns None if not found.
        """
        if name == "..":
            return self.__parent
        if name == "*":
            if len(self.__children) == 1:
                return self.__children[0]
            return None
        try:
            return self.__children[int(name)]
        except (ValueError, IndexError):
            pass
        # Exact match wins over substring (avoids false ambiguity when
        # names share a prefix, e.g. STAPL vars J2, J23, J24).
        for c in self.__children:
            if c.__name == name:
                return c
        matches = [c for c in self.__children
                   if name.lower() in c.__name.lower()]
        if len(matches) == 1:
            return matches[0]
        return None

    async def child_spawn(self, name):
        """Create a child by name. Override in subclasses."""
        raise NoMatch("child", name)

    async def __child_spawn_mro(self, name):
        """Walk the MRO trying each class's child_spawn from __dict__.

        Each class in the hierarchy can define its own child_spawn
        with its own Db. NoMatch causes fallback to the next class.

        The reserved name `as` is handled here, before the MRO walk.
        Any Readable Node accepts an `as` child for format
        reinterpretation (see docs/vfs-design.md D3).
        """
        if name == "as":
            from .vfs import AsNode
            return AsNode("as")
        for cls in type(self).__mro__:
            try:
                method = cls.__dict__["child_spawn"]
            except KeyError:
                continue
            try:
                return await method(self, name)
            except NoMatch:
                continue
        raise NoMatch("child", name)

    @staticmethod
    def __parse_options(name):
        """Parse "name(key=value,key="quoted value")" into
        (bare_name, options_dict).

        Grammar (see docs/vfs-design.md D10):

        - Names must contain balanced parens (forward depth scan,
          never negative, ends at 0 before the trailing options).
        - Options are recognised as the trailing `(...)` group whose
          content parses as a non-empty list of `key=value` entries.
        - `()` (empty parens) at end is the explicit "no options"
          escape (used when the name ends in balanced parens).
        - Bare keys (without `=`) are NOT supported (parse error).
        - Quoted values via `"..."` with `\\"` and `\\\\` escapes.

        If the input doesn't match the options grammar, the whole
        string is treated as a name with no options.

        Returns (name_str, options_dict).
        """
        # Forward scan: track quote state and paren depth.
        # Record positions of all top-level (...) groups.
        depth = 0
        in_quote = False
        escaped = False
        groups = []  # list of (open_pos, close_pos)
        open_pos = -1

        for i, ch in enumerate(name):
            if in_quote:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_quote = False
                continue
            if ch == '"':
                in_quote = True
            elif ch == "(":
                depth += 1
                if depth == 1:
                    open_pos = i
            elif ch == ")":
                if depth == 0:
                    # Unmatched ): treat as part of name
                    return name, {}
                depth -= 1
                if depth == 0:
                    groups.append((open_pos, i))

        if in_quote or depth != 0:
            return name, {}

        # The trailing options group, if any, is a top-level (...) at
        # end of string.
        if not groups or groups[-1][1] != len(name) - 1:
            return name, {}

        op, cp = groups[-1]
        content = name[op + 1:cp]

        # Empty () escape: explicit "no options"
        if not content:
            return name[:op], {}

        # Try to parse as kv-list. On failure, treat as name.
        try:
            opts = _parse_kv_list(content)
        except _NotKvList:
            return name, {}
        return name[:op], opts

    async def child_summon(self, *parts):
        """Resolve a path through the node tree, spawning as needed.

        Spawned children that are Nodes are added to the tree.
        Each node along the path is started (if not already) before
        navigating deeper, so that start() can populate children
        (e.g. Chain.start() discovers TAPs).

        Supports "name(key=value,...)" syntax (D10): options are
        applied via option_set() before the node is started.

        Concurrent calls for the same ``parts`` are single-flight:
        spawn-and-attach happens once per (parent, name), and
        ``start()`` is exclusive per node. Two parallel commands
        targeting the same chain therefore share one spawned chain
        and one ``start()`` call.
        """
        if not parts:
            return self
        raw_name, *rest = parts
        bare_name, opts = Node.__parse_options(raw_name)
        child = await self.__lookup_or_spawn(bare_name)
        for k, v in opts.items():
            child.option_set(k, v)
        await child.ensure_started()
        if not rest:
            return child
        return await child.child_summon(*rest)

    async def __lookup_or_spawn(self, name):
        """Return the child named *name*, spawning + attaching once
        across concurrent callers."""
        child = self.child_lookup(name)
        if child is not None:
            return child
        fut = self.__summon_inflight.get(name)
        if fut is not None:
            return await fut
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self.__summon_inflight[name] = fut
        try:
            try:
                child = self.child_lookup(name)
                if child is None:
                    child = await self.__child_spawn_mro(name)
                    if child.parent is None:
                        self.__child_attach(child)
                fut.set_result(child)
            except BaseException as exc:
                if not fut.done():
                    fut.set_exception(exc)
        finally:
            self.__summon_inflight.pop(name, None)
        # Always consume `fut` — concurrent callers `await fut` and
        # retrieve its exception/result; this path is the producer's
        # own retrieval, without which asyncio logs "Future exception
        # was never retrieved" whenever no concurrent caller raced us.
        return await fut

    async def ensure_started(self):
        """Idempotent, concurrency-safe start. Multiple awaits race
        through one ``start()`` call.

        Public — subclasses that need to start a borrowed-reference
        sibling node before using it call this directly.

        Drains the pending attach event (deferred from
        `__child_attach`) so subscribers see `attach POST` before
        `start PRE`. Then fires `(start, pre)`, runs `start()`,
        emits `(start, post)` with `success=True/False` and
        re-raises on failure."""
        if self.__started:
            return
        await self.__drain_pending_attach()
        if self.__start_lock is None:
            self.__start_lock = asyncio.Lock()
        async with self.__start_lock:
            if self.__started:
                return
            async with self.event_emitter("start"):
                await self.start()
                self.__started = True

    async def start(self):
        pass

    async def stop(self):
        pass

    async def start_tree(self):
        await self.ensure_started()
        for child in self.__children:
            await child.start_tree()

    async def stop_tree(self):
        """Top-down stop. Emits `(stop, pre/post)` around `stop()`
        on this Node only if it was actually started — symmetric
        with `ensure_started`, which emits `start` only when
        `start()` runs. Cancels subscriptions held against this
        Node (`subscribe()` scoped to this Node's lifetime), then
        recurses into children."""
        if self.__started:
            async with self.event_emitter("stop"):
                await self.stop()
                self.__started = False
        for sub in self.__subscriptions:
            sub.cancel()
        self.__subscriptions.clear()
        for child in self.__children:
            await child.stop_tree()

    # ----- Event-bus integration -----

    async def emit(self, action: str, phase: str | None = None,
                   **properties) -> None:
        """Publish on the global event bus with `source=self.path`.

        Convenience over `acrobe.event.get_bus().emit(Event(...))`
        — captures the path string at call time and forwards. The
        bus is path-keyed, so the Python identity of `self` is
        irrelevant to subscribers; they match on the string.
        """
        from .event import Event, get_bus
        await get_bus().emit(Event(
            source=self.path, action=action, phase=phase,
            properties=properties))

    def subscribe(self, handler, *,
                  action=None, phase=None,
                  source_match: str = "subtree",
                  predicate=None):
        """Subscribe to bus events with `source=self.path`.

        Defaults to `source_match="subtree"` because the typical
        Node-side use is "watch me and my descendants" — opposite
        to the bare-bus default (`"exact"`).

        The Node instance is consumed only to capture `self.path`.
        After this call returns, the subscription is path-based;
        a fresh Node replacing this one at the same path will
        still trigger the handler.

        The returned `Subscription` is tracked on this Node and
        auto-cancelled on `stop_tree`. Callers wanting a
        longer-lived subscription should go through
        `acrobe.event.get_bus().subscribe(...)` directly.
        """
        from .event import get_bus
        sub = get_bus().subscribe(
            handler,
            action=action, phase=phase,
            source=self.path, source_match=source_match,
            predicate=predicate)
        self.__subscriptions.append(sub)
        return sub

    @asynccontextmanager
    async def event_emitter(self, action: str, **base_properties):
        """Async context manager: emits `(action, PRE)` on enter,
        `(action, POST)` on exit, yields a `Notifier` whose
        `progress(**props)` emits `(action, PROGRESS)` events
        sharing the base properties.

        The POST event carries `success=True` on clean exit and
        `success=False` + `error_class=<exception type name>` on
        exception. Exceptions still propagate to the caller
        after the POST is emitted.
        """
        from .event import Notifier, Phase
        await self.emit(action, phase=Phase.PRE, **base_properties)
        notifier = Notifier(self, action, base_properties)
        success = True
        error: BaseException | None = None
        try:
            yield notifier
        except BaseException as exc:
            success = False
            error = exc
            raise
        finally:
            extra = {"success": success}
            if error is not None:
                extra["error_class"] = type(error).__name__
            await self.emit(action, phase=Phase.POST,
                            **base_properties, **extra)

    @staticmethod
    def notified(action: str):
        """Decorator: wrap an async method so pre/post events fire
        automatically around it.

        Equivalent to wrapping the body in
        `async with self.event_emitter(action): ...`. For richer
        cases (progress events, per-call base properties), use
        `event_emitter` directly.
        """
        def deco(method):
            @functools.wraps(method)
            async def wrapper(self, *args, **kwargs):
                async with self.event_emitter(action):
                    return await method(self, *args, **kwargs)
            return wrapper
        return deco
