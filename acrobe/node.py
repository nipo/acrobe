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
import logging
from contextlib import contextmanager

from .db import NoMatch
from .log import get_progress


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
    - Pre-populated children are created in start() and live in
      self._children — visible via .children and listed by
      `acrobe resource ls`.
    - On-demand children (e.g. `as(type=...)`, ELF symbols) are
      not in self._children but reachable via child_spawn /
      child_summon.
    """

    def __init__(self, name: str):
        self._name = name
        self._parent = None
        self._children = []
        self._started = False
        self._metadata = {}
        # Single-flight bookkeeping so concurrent child_summon /
        # start_tree calls don't open the same hardware twice. Lazy
        # to avoid bind-to-loop on Nodes that are constructed before
        # an event loop exists.
        self._summon_inflight: dict[str, asyncio.Future] = {}
        self._start_lock: asyncio.Lock | None = None

    def __str__(self):
        return self._name

    def __repr__(self):
        return f"<{self.__class__.__name__} '{self._name}'>"

    @property
    def name(self) -> str:
        return self._name

    @property
    def parent(self):
        return self._parent

    @property
    def children(self) -> list:
        """Pre-populated children only. On-demand children
        (e.g. `as(...)`, ELF symbols) are not listed here."""
        return list(self._children)

    @property
    def fqdn(self) -> str:
        """Dotted name of this node from the root.

        This is the *logger* name — Python's logging hierarchy
        relies on '.' for parent/child relationships and prefix
        filters. Use `path` for display in CLI / messages."""
        parts = []
        node = self
        while node is not None:
            parts.append(node._name)
            node = node._parent
        parts.reverse()
        return ".".join(parts)

    @property
    def path(self) -> str:
        """Slash-separated path from the root, matching the
        VFS path syntax (`a/b/c`). Suitable for display."""
        parts = []
        node = self
        while node is not None:
            parts.append(node._name)
            node = node._parent
        parts.reverse()
        return "/".join(parts)

    @property
    def logger(self) -> logging.Logger:
        return logging.getLogger(self.fqdn)

    @property
    def started(self) -> bool:
        return self._started

    @property
    def metadata(self) -> dict:
        """Free-form metadata for inspection (e.g. by
        `acrobe resource info`). Format-specific subclasses
        populate self._metadata in start(); subclasses with typed
        attributes (e.g. ElfSection.flags, Pof.tool) usually mirror
        them into this dict.
        """
        return self._metadata

    def _child_attach(self, child: "Node"):
        """Attach child without auto-start. Used by child_summon."""
        assert child._parent is None, f"{child.fqdn} already has a parent"
        child._parent = self
        self._children.append(child)
        self.children_changed()

    def child_add(self, child: "Node"):
        self._child_attach(child)
        if self._started:
            asyncio.ensure_future(child.start_tree())

    async def child_remove(self, child: "Node"):
        assert child._parent is self, f"{child.fqdn} is not a child of {self.fqdn}"
        await child.stop_tree()
        self._children.remove(child)
        child._parent = None
        self.children_changed()

    def children_changed(self):
        pass

    def children_find(self, predicate, include_self=False) -> list:
        result = []
        if include_self and predicate(self):
            result.append(self)
        for child in self._children:
            result.extend(child.children_find(predicate, include_self=True))
        return result

    def children_of_class(self, klass, include_self=False) -> list:
        return self.children_find(lambda c: isinstance(c, klass), include_self=include_self)

    def parent_of_class(self, klass):
        node = self._parent
        while node is not None:
            if isinstance(node, klass):
                return node
            node = node._parent
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
            return self._parent
        if name == "*":
            if len(self._children) == 1:
                return self._children[0]
            return None
        try:
            return self._children[int(name)]
        except (ValueError, IndexError):
            pass
        # Exact match wins over substring (avoids false ambiguity when
        # names share a prefix, e.g. STAPL vars J2, J23, J24).
        for c in self._children:
            if c._name == name:
                return c
        matches = [c for c in self._children
                   if name.lower() in c._name.lower()]
        if len(matches) == 1:
            return matches[0]
        return None

    async def child_spawn(self, name):
        """Create a child by name. Override in subclasses."""
        raise NoMatch("child", name)

    async def _child_spawn_mro(self, name):
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
    def _parse_options(name):
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
        bare_name, opts = self._parse_options(raw_name)
        child = await self._lookup_or_spawn(bare_name)
        for k, v in opts.items():
            child.option_set(k, v)
        await child._ensure_started()
        if not rest:
            return child
        return await child.child_summon(*rest)

    async def _lookup_or_spawn(self, name):
        """Return the child named *name*, spawning + attaching once
        across concurrent callers."""
        child = self.child_lookup(name)
        if child is not None:
            return child
        fut = self._summon_inflight.get(name)
        if fut is not None:
            return await fut
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._summon_inflight[name] = fut
        try:
            try:
                child = self.child_lookup(name)
                if child is None:
                    child = await self._child_spawn_mro(name)
                    if child._parent is None:
                        self._child_attach(child)
                fut.set_result(child)
            except BaseException as exc:
                if not fut.done():
                    fut.set_exception(exc)
        finally:
            self._summon_inflight.pop(name, None)
        # Always consume `fut` — concurrent callers `await fut` and
        # retrieve its exception/result; this path is the producer's
        # own retrieval, without which asyncio logs "Future exception
        # was never retrieved" whenever no concurrent caller raced us.
        return await fut

    async def _ensure_started(self):
        """Idempotent, concurrency-safe start. Multiple awaits race
        through one ``start()`` call."""
        if self._started:
            return
        if self._start_lock is None:
            self._start_lock = asyncio.Lock()
        async with self._start_lock:
            if self._started:
                return
            await self.start()
            self._started = True

    async def start(self):
        pass

    async def stop(self):
        pass

    async def start_tree(self):
        await self._ensure_started()
        for child in self._children:
            await child.start_tree()

    async def stop_tree(self):
        await self.stop()
        self._started = False
        for child in self._children:
            await child.stop_tree()
