# VFS / unified Node tree — design note

## Status

Design accepted. Implementation tracked in `docs/vfs-plan.md`.
Private repo, no external users — no backward-compat constraints.

## Problem

`acrobe` already has two related-but-disjoint hierarchies:

1. **Component tree** (`acrobe/component/`) — async tree of hardware
   abstractions with autodiscovery (e.g. JTAG chain → TAP → DP → AP).
2. **Loadable model** (`acrobe/loadable/`) — file parsers producing
   `Program`/`Segment` memory maps; dispatched by extension or
   explicit format suffix `:fmt`.

The loadable layer collapses too much in a single step. Concrete pain
points:

- **Altera bitstreams** ship the same payload (RBF) in three
  encapsulations: raw RBF, POF (flash image with header + ToC +
  partitions, possibly several images), and STAPL (programs whose
  variables hold the RBF blob). Today each path is a separate loader;
  cross-cases (e.g. POF inside a ZIP) require new code.
- **Lattice MachXO2** in crobe needed three distinct JTAG-loadable
  segments. The crobe workaround was inventing fake addresses, which
  confused users looking for them in vendor docs.
- **No way to point at substructure**. A user cannot say "partition 1
  of this POF" or "the J2 variable initializer of this STAPL file"
  from the CLI; they must extract by hand.
- **No composition with live hardware**. Walking into "the bytes
  currently in this flash chip, interpreted as a POF" is not
  expressible.
- **Stacked encodings are unreachable**. An ihex file holding a ZIP
  archive can't be addressed: a `(type=zip)` option fails because
  the ZIP parser sees raw ihex text. We need a way to compose
  parsers, not select one.

## Goal

A single tree abstraction that uniformly covers:

- Hardware components (today's `Component` use cases),
- File-format containers (POF, ZIP, tar, ELF, STAPL, ...),
- Slices and reinterpretations of byte sources,
- Memory-map abstractions (today's Program/Segment),
- Mixes of the above (e.g. live flash chip → POF → partition → RBF).

With one path syntax for the CLI, one discovery/dispatch mechanism,
one async IO contract.

## Decisions

### D1. One tree class: `Node`. Code organised by vendor / generic.

Rename `Component` → `Node`. The base class moves from
`acrobe/component/__init__.py` to `acrobe/node.py`. The full Node
contract (lifecycle, parenting, lookup/spawn/summon, options,
navigation) is documented separately in `docs/node-model.md`;
this section only covers the VFS-relevant additions.

All current `Component` subclasses and all new file-format classes
inherit from `Node`. No `HardwareNode`/`FileNode` split: the API
is 100% shared, splitting would be artificial.

**Code layout.** Today's split (`acrobe/component/` for hardware,
`acrobe/loadable/` for file formats) was an artefact of the
pre-`Db` era. With unified Nodes, organise by domain instead of
by abstraction:

- `acrobe/node.py` — `Node` base class and core mixins
  (`Readable`, `Writable`, `Addressable`).
- `acrobe/vfs/` — VFS infrastructure (registries, `as`
  dispatch, fs root, helpers) and **generic** format parsers
  (`bin`, `ihex`, `elf`, `zip`, `tar`, `literals`).
- `acrobe/component/<vendor>.py` — vendor-specific hardware
  components AND that vendor's file-format parsers, kept
  together. E.g. `acrobe/component/altera.py` holds the FPGA
  hardware Nodes and the POF/SOF/RBF format Nodes.
- `acrobe/loadable/` — **deleted**. Its contents are migrated
  per the rule above.

Vendors with enough code to warrant it may split into a package
(`acrobe/component/altera/__init__.py`,
`acrobe/component/altera/formats.py`); the public import path
stays `acrobe.component.altera`.

*Rationale.* The composition wins (live flash → POF → RBF) require
identical class identity, not a bridge layer. "Component" reads
as "hardware" — but with vendor formats co-located, the package
name accurately describes "everything for vendor X".

### D2. A Node may expose at most one contiguous blob

A Node *may* implement `Readable` (D6) over at most one contiguous
range of bytes. It is also legal for a Node to implement no
byte-access interface at all — pure structural Nodes exist
(typically the roots of ZIP/ELF/tar parses, where the meaningful
bytes live in the children, not the root).

Multi-region or multi-section data is always expressed as
**children**, never as multiple ranges within a single Node. This
rule keeps the `read()` contract simple and the tree self-similar
(a "Program" with N segments is just a Node with N addressable
children — see D6).

Per-format choices for whether the root Node is `Readable`:

- **Encoding formats** (ihex, base64-like, gzip-decoded): root
  exposes `Readable` over the *decoded* byte stream.
- **Structural formats** (ZIP, ELF, tar): root has no `Readable`
  of its own — the parent's `Readable` (the file) already covers
  the same bytes; entries reference backing storage directly.
- **Mixed** (POF, SOF): root exposes `Readable` over the raw file
  bytes (so `/as(type=…)` reinterpretation remains possible) and
  populates structural children for the format's natural
  decomposition.

### D3. Structure and reinterpretation are both children — but reinterpretation goes through `as`

Two reasons a Node has children:

1. **Structural decomposition** — the Node's bytes (or the bytes
   it represents, for encoding formats) are interpreted in a
   known format and the children are its parts (POF →
   `partition/N`; ELF → `section/.text`, `program/0`; ZIP →
   per-entry; ihex → `region/N`).
2. **Reinterpretation** — the Node's bytes are fed to *another*
   format parser, which exposes its own children. Reinterpretation
   is reached via a single reserved child name, `as`, with the
   format selected by option:

   ```
   file.ihex/as(type=zip)/member.bin
   file.bin/as(mime-type=application/zip)/member.bin
   ```

This solves the stacked-encoding case (ihex-of-zip): each parser
only sees the `Readable` of its parent. It also keeps the child
namespace clean — there's no risk of a structural child named
`zip` colliding with a hypothetical `zip` reinterpretation.

`as` is **reserved** as a child name. Container formats that
would naturally have a child literally named `as` need to pick a
different name (or the user reaches it via the programmatic API).

The `as(...)` Node is a thin marker: it carries the chosen
format/mime, populates children by parsing the parent's
`Readable`, and does not expose a `read()` of its own (its bytes
would be identical to the parent's anyway).

### D4. Lifecycle stays `start()` / `stop()`

Discovery happens in async `start()`: open the source, sniff the
header, dispatch through a `Db`, populate children. `stop()` is the
`close()` analogue: drop parsed state, release file handles or
buffers. **Stop also serves as the writeback opportunity** for
container types that need finalisation (compression, CRC).

`start_tree()` / `stop_tree()` keep their current top-down
propagation. A spawned child auto-starts if its parent is started
(today's behaviour). Walking is lazy via `child_summon`.

`open()` / `close()` are *not* used: they bias toward the file
metaphor and read awkwardly for hardware nodes ("open the JTAG
TAP"). Subclasses may add aliases if they want.

### D5. Stop propagates; post-stop descendants are a programming error

When a parent stops, all descendants stop. A live reference to a
descendant after the parent has stopped MUST NOT be read from;
doing so is a programming error and may assert. We do not adopt a
ref-counted lifecycle — that would introduce GC-style cycles and a
deinit-order nightmare. Users are responsible for completing
workloads before tearing down a subtree.

### D6. `Readable`, `Writable`, `Addressable` mixins

Bytes and metadata are exposed via small mixins, applied where
applicable. Sync vs. async split: only `read`/`write` are async
(real IO); `size`, addresses, and metadata are sync properties
populated during `start()` — having to `await node.size` in
every range check would hurt readability for nothing.

```python
class Readable:
    @property
    def size(self) -> int:
        """Total bytes addressable. Set during start()."""
        ...

    async def read(self, offset: int, size: int) -> bytes:
        """Read up to `size` bytes from `offset`.

        POSIX-pread semantics: returns at most `size` bytes; may
        return fewer at end of node. Raises ValueError if
        offset < 0 or offset > self.size.
        """
        ...


class Writable(Readable):
    async def write(self, offset: int, data: bytes) -> None:
        """Write `data` at `offset`.

        Must satisfy offset + len(data) <= self.size; raises
        otherwise. Finalisation (CRC, recompression) happens in
        stop(), not on each write.
        """
        ...


class Addressable:
    @property
    def load_address(self) -> int:
        """Where this content is placed in target memory.

        Flash region: flash address. ihex region: record address.
        ELF segment: LMA. The single canonical address for code
        that just wants to know "where does this go".
        """
        ...

    @property
    def addresses(self) -> dict[str, int]:
        """Named address aliases. Default returns
        {'load': self.load_address}; ELF sections override to
        expose {'vma', 'lma', 'file_offset', 'load'}.
        """
        return {"load": self.load_address}
```

Combining mixins (concrete examples):

- Raw binary leaf with no inherent address: `Readable`.
- ihex region: `Readable + Addressable`.
- Flash chip region: `Readable + Writable + Addressable`.
- ELF section: `Readable + Addressable` (multi-address).
- ZIP root, ELF root, tar root: pure structural, no mixins —
  children point at the parent file's `Readable` directly.

This **replaces** Program/Segment. A "program" is just a Node
whose addressable descendants are the segments to load. Loading
a target means: walk the subtree, collect `Addressable +
Readable` leaves, push each to its `load_address`.

### D6.1. Generic metadata

Free-form metadata for inspection lives on `Node`:

```python
class Node:
    @property
    def metadata(self) -> dict[str, Any]:
        """Format-specific metadata for inspection. Populated
        during start(). Used by `acrobe loadable info`.

        Format subclasses additionally expose typed attributes
        (e.g. ElfSection.flags, Pof.tool, Pof.design) — those
        are canonical for code; the dict is for introspection.
        """
        ...
```

### D7. Write support is opt-in per leaf

A leaf is `Writable` iff **every container in its parent chain
supports passthrough writes** — byte offsets in the child map
directly to byte offsets in the parent's storage.

| Container             | Passthrough? |
|-----------------------|--------------|
| Raw slice (offset/size view) | Yes |
| Flash region (live chip) | Yes |
| mmap'd file slice     | Yes |
| Uncompressed tar entry | Yes |
| ZIP entry             | No (compression / CRC) |
| POF partition         | No (header / image CRCs) |
| STAPL variable initializer | No (procedural) |

Containers that need finalisation do their work in `stop()`, not
on every write — see D4.

### D8. Output formatters are NOT VFS writes

Producing a file from a memory map (e.g. dump flash → `dump.hex`,
serialize a tree to a binary blob) is a separate concern handled
by output formatters. The VFS tree's `Writable` is solely for
in-place patches of leaves whose byte view maps to mutable
storage.

Output formatters live in their own module and consume a Node
subtree by walking its `Addressable + Readable` descendants.

### D9. Two kinds of children: pre-populated and on-demand

Every Node has both:

- **Pre-populated children** — created during `start()`, listed
  in `node.children`, visible to `acrobe loadable ls`. These
  represent the format's "natural decomposition" — the children
  a user would expect to see when browsing.
- **On-demand children** — *not* in `node.children`, *not*
  listed by `ls`, but reachable via `child_summon(name, **opts)`
  → `child_spawn(name, **opts)`. Used for namespaces that would
  explode the tree if pre-enumerated, or that are
  parameter-dispatched.

The same `child_lookup` / `child_spawn` machinery handles both:
`child_lookup` walks pre-populated children; `child_spawn` is the
fall-through that knows about on-demand patterns.

Concrete policy by format:

| Format | Pre-populated | On-demand |
|--------|---------------|-----------|
| Any `Readable` | (auto-detected structural) | `as(type=…)` |
| ELF | `program/N`, `section/<name>` | `symbol/<name>`, `as(...)` |
| POF | `partition/N` | `as(...)` |
| ZIP / tar | entries | `as(...)` |
| ihex | `region/N` | `as(...)` |

Three free-standing dispatch registries support all this:

- `ext_db` — keyed by extension or compound extension (`tar.gz`),
  resolves to a format name.
- `mime_db` — keyed by mime type (fed by `puremagic` or similar),
  resolves to a format name.
- `format_db` — keyed by format name (`zip`, `elf`, `pof`, …),
  produces a parser Node from a `Readable` parent.

**Auto-detection on `start()`** (augment, not replace):

1. Run extension + magic detection.
2. If a format is detected, instantiate the parser **in-place**
   — its structural children attach directly to `self`, NOT
   under an `as` wrapper. The Node's own `read()` (if any) is
   preserved.
3. If detection is ambiguous or absent, no auto-population.

**Explicit reinterpretation via `as`**:

`child_summon("as", type="zip")` reads `type=` (or `mime-type=`),
resolves to a `format_db` entry, instantiates the parser with
`self` as its `Readable` source. `as` is itself an on-demand
child — never pre-populated, never listed by `ls`.

**Class-defined on-demand spawners** (e.g. ELF symbols): a Node
class overrides `child_spawn` to handle specific names or
sub-namespace prefixes. ELF root has a pre-populated `symbol`
namespace Node whose children are spawned on demand by name —
`file.elf/symbol/main` triggers a symbol-table lookup at the
time of access.

**Collisions** in `ext_db` / `mime_db` (e.g. `.bit` →
multiple candidate formats) are an accepted reality; handlers
raise `NoMatch` to defer.

### D10. Path grammar

```
path        := part ("/" part)*
part        := name options?
name        := <bytes excluding "/", ":", "\\";
                balanced parens; balanced brackets>
options     := "(" (kv ("," kv)*)? ")"
kv          := key "=" value
key         := <bytes excluding "=", ",", "(", ")", whitespace>
value       := bare_value | quoted_value
bare_value  := <bytes excluding ",", "(", ")", whitespace, '"'>
quoted_value:= '"' (<any char> | '\"' | '\\\\')* '"'
```

Rules:

- Names must contain balanced parens (forward depth scan, never
  negative, ends at 0).
- Options are recognised as the trailing `(...)` group whose
  content parses as a non-empty list of `key=value` entries.
- **Bare keys are NOT supported.** Every option is `key=value`.
  This avoids a special case and keeps the grammar uniform.
- `()` (empty parens) at the end of a name is the explicit "no
  options" escape, used when the name itself ends in balanced
  parens.
- `/`, `:`, `\` are forbidden in names.
- **Quoted values are supported from v1.** Inside `"…"`, the
  parser only recognises `\"` (literal quote) and `\\` (literal
  backslash); everything else is a literal byte. This lets
  values carry `,`, `)`, `=`, whitespace, parens.

Common options: `type=…` / `mime-type=…` (selects format for the
`as` child, D3); `offset=…`, `size=…` for slicing; node-specific
tunables (e.g. `serial=…` for an adapter).

The parser tokenises forward, tracks quote state, identifies the
trailing `(...)` group at depth 0, and validates its content.
Anything that doesn't match the grammar is treated as a name
without options.

### D11. Programmatic API is canonical, CLI parses to it

`child_summon(name, **options)` is the structured interface. The
path-string parser is a CLI convenience that produces calls to
`child_summon`. Edge cases the path parser can't handle remain
reachable from code without contortions.

### D12. Forbidden-in-name characters are split at discovery

ZIP/tar/CPIO and similar archives store entries as flat paths
(`a/b/file.bin`). At discovery the container splits on `/` and
synthesises intermediate directory nodes. A shared
"flat-paths-to-tree" helper handles this — multiple formats need
it.

## API sketch

### Node base

```python
class Node:
    def __init__(self, name: str): ...

    # tree
    @property
    def parent(self) -> "Node | None": ...
    @property
    def children(self) -> list["Node"]:
        """Pre-populated children only — on-demand children
        (e.g. `as(...)`, ELF `symbol/<name>`) are not listed."""
        ...
    @property
    def fqdn(self) -> str: ...

    # lifecycle
    async def start(self) -> None: ...   # parse header, populate structural children
    async def stop(self) -> None: ...    # release / finalise (writeback)
    async def start_tree(self) -> None: ...
    async def stop_tree(self) -> None: ...

    # navigation
    def child_lookup(self, name: str) -> "Node | None": ...
    async def child_spawn(self, name: str, **opts) -> "Node": ...
    async def child_summon(self, *parts: str) -> "Node": ...

    # options
    def option_set(self, key: str, value): ...

    # generic metadata for inspection (D6.1)
    @property
    def metadata(self) -> dict[str, Any]: ...
```

`Readable`, `Writable`, `Addressable` from D6 are independent
mixins; a Node opts into whichever apply. Their interfaces are
defined in D6.

### Reinterpretation children — the `as` mechanism

For any Node implementing `Readable`, `child_summon("as", **opts)`
resolves to a thin marker Node whose `start()` looks up
`format_db[opts["type"]]` (or maps `mime-type=` → format name
first), instantiates the format parser with `self.parent` as its
`Readable`, and re-parents the parser as the `as` Node's only
meaningful child. Effectively `parent/as(type=X)/...` is the
indirection point at which a different parser takes over reading
the parent's bytes.

`as` is a reserved child name and is **always on-demand** —
never pre-populated, never visible to `acrobe loadable ls`.
Format discovery happens through documentation or
`acrobe format list` (TBD), not by browsing.

### ELF symbols (on-demand sub-namespace)

The `symbol` child of an ELF root is itself a Node, but its
children are populated on demand:

```python
class ElfSymbols(Node):
    """Pre-populated as `symbol` child of an ELF root.
    Children spawned on demand by name; not listed by `ls`."""

    async def child_spawn(self, name: str) -> "ElfSymbol":
        sym = self._elf.lookup_symbol_by_name(name)
        if sym is None:
            raise NoMatch("symbol", name)
        return ElfSymbol(name, sym)
```

Reverse lookups (address → symbol) live as methods on the ELF
root, returning references to actual `ElfSymbol` Nodes (created
on demand if not previously summoned):

```python
class Elf(Node):
    def symbol_at(self, address: int) -> ElfSymbol | None: ...
    def symbols_in(self, start: int, end: int) -> list[ElfSymbol]: ...
    def section_at(self, address: int) -> ElfSection | None: ...
```

Same pattern serves any format with namespaces too large to
pre-enumerate (e.g. a 100-MiB ZIP with 10k entries — but for ZIP
we do pre-populate, since the central directory is cheap to
walk; this is a per-format choice).

## Boundary cases

### Live region exposed as VFS root

A flash region (Component, today; Node, tomorrow) implements
`Readable` over its address space. Walking
`flash/data/as(type=pof)/partition/1/as(type=rbf)` works: the POF
parser is content with any `Readable` parent, not just files.

This means file containers must be written against `Readable`,
never against `os.PathLike`.

### Output formatters

Stay in their own module. They consume a Node subtree by walking
its `Addressable + Readable` descendants. The `Program`/`Segment`
classes go away; their methods (`paged`, `simplified`, `within`)
either become utilities operating on a Node subtree, or migrate
into the output formatters that need them.

### In-place patch of a leaf

When the chain is fully passthrough, `node.write(0x40, b"\x42")`
is allowed. No finalisation needed if no container in the chain
transforms bytes.

### ELF: segments vs. sections, both as children

ELF describes the same address space twice: program headers
(segments) for runtime loading, sections for linking and
analysis. Both live as separate child namespaces under the ELF
root:

```
file.elf
├── program/0          # ELF program header (segment) — Readable + Addressable
├── program/1
├── section/.text      # Readable + Addressable
├── section/.data
├── section/.bss       # NOBITS — read() synthesises zeros
├── symbol/main        # on-demand
└── symbol/_start
```

Each `program/N` and `section/<name>` is a `Readable +
Addressable` Node. Children read directly from the parent file's
`Readable`, never through the ELF root.

`ElfSection` carries multi-address metadata:

```python
class ElfSection(Node, Readable, Addressable):
    @property
    def load_address(self) -> int:
        return self._lma

    @property
    def addresses(self) -> dict[str, int]:
        return {"vma": self._vma, "lma": self._lma,
                "file_offset": self._file_offset,
                "load": self._lma}

    @property
    def metadata(self) -> dict[str, Any]:
        return {"flags": list(self._flags),
                "section_type": self._section_type,
                **self.addresses}
```

Symbols are on-demand (see "ELF symbols" above).

Loading semantics: `acrobe chip program file.elf` walks
`program/*` (canonical loadable view per ELF spec). Walking
`section/*` filtered by `'alloc'` flag gives the same answer in
well-formed binaries.

**Relocations and DWARF** are out of scope for v1. They don't
fit the byte-data-Node mould (relocations are operations; DWARF
is a graph of DIEs). Add later as either methods on the ELF
root (`elf.relocations()`, `elf.dwarf.address_to_line(addr)`) or
a specialised subtree, when a use case appears.

### Per-format `read()` semantics

Per D2, every format chooses whether its root Node is `Readable`,
and if so, what bytes `read()` returns:

- **ihex**: `Readable`. `read()` returns a single contiguous blob
  from `min_addr` to `max_addr`, gaps filled (e.g. with `0xFF`).
  `base_address = min_addr`. Individual regions remain
  accessible via `region/N` children for users who care about
  the gaps. This makes `file.ihex/as(type=zip)` meaningful — the
  zip parser sees the decoded blob.
- **ELF**: not `Readable` on the root. The parent file Node
  already covers the raw ELF bytes. To reinterpret the whole
  file, walk `file.elf/../as(type=zip)` (i.e., reinterpret the
  parent), or apply `as(type=…)` on a section/segment.
- **ZIP, tar**: not `Readable` on the root. Same reasoning as
  ELF.
- **POF, SOF**: `Readable`, returning raw POF/SOF bytes — same
  as the parent file's `read()`. Slightly redundant but uniform,
  and it lets `file.pof/as(type=zip)` succeed (rare but
  consistent). Implementation may simply delegate `read()` to
  parent.

Children of structural-format containers reference the backing
storage directly (i.e., the file Node, or whichever `Readable`
ancestor provides bytes). This avoids an unnecessary
indirection on every read.

## CLI commands

A new top-level group, `acrobe loadable`, mirrors filesystem
ergonomics over the Node tree:

```
acrobe loadable ls   <path> [-r] [-l] [--depth N]
acrobe loadable info <path>
acrobe loadable cp   <src> [<dst>] [--offset N] [--size N]
acrobe loadable hexdump <path> [--offset N] [--size N]
acrobe loadable tree <path> [--depth N]
```

- **`ls`** lists pre-populated children; `-r` recurses; `-l`
  long format with mixin tags (R/W/A), size, `load_address`.
  On-demand children (`as`, ELF `symbol/<name>`) are NOT listed
  — they're infinite or parameter-dispatched.
- **`info`** dumps everything: class, mixins implemented, size,
  all addresses, metadata dict, child names. Useful as `info
  file.elf/section/.text` to see VMA/LMA/flags at a glance.
- **`cp`** copies bytes from the source Node to the destination
  (defaults to `-` = stdout via Click). `--offset` /
  `--size` slice the source. Works for any `Readable`. Errors
  cleanly on non-`Readable` sources.
- **`hexdump`** is a shortcut for `cp ... | hexdump -C`-style
  inspection, formatted with addresses honouring the source
  Node's `load_address` if `Addressable`.
- **`tree`** is `ls -r` formatted with box-drawing.

**Conversion commands** (e.g. binary → ihex output) are
deliberately left unscoped here. Two candidate shapes — `cp
--format=ihex` versus a separate `acrobe format convert`
command — pinned in `docs/vfs-plan.md`.

Existing `acrobe loadable {dump,hexdump,to-bin,to-hex,to-c-blob,
to-vhdl-blob}` commands stay where they are conceptually
(memory-map and output-formatter operations) but consume Node
subtrees internally.

## Out of scope (v1)

- Quoted values in path options.
- `Writable` for non-passthrough containers (POF rewriting, ZIP
  recompression).
- VFS-driven output (continue using output formatters).
- A caching layer for parsed container state (each Node owns its
  parsed state in instance attrs; freed on `stop()`).

## Glossary

- **Node**: tree element (replaces Component). At most one
  contiguous blob of bytes.
- **Container Node**: discovers child Nodes during `start()`.
- **Leaf Node**: no children; exposes bytes via `Readable`.
- **Structural children**: children that decompose the parent's
  bytes according to a format (POF partitions, ELF sections).
  Auto-populated on `start()` when format is detected.
- **Reinterpretation children**: children that re-parse the
  parent's bytes as a different format. Created on demand via
  `format_db` lookup.
- **Passthrough container**: byte offsets in children map directly
  to bytes in parent's storage.
