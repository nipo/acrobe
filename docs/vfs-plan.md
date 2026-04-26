# VFS implementation plan

Companion to `docs/vfs-design.md`. Track progress here; update as
steps land.

## Ground rules

- **No backward-compat code.** The repo is private and has no
  external users. When an API changes, every call site is migrated
  in the same commit (or PR). No deprecation warnings, no shims, no
  re-exports of old names. If something needs to be removed, delete
  it; don't leave a stub.
- **Master branch.** Land incrementally. Each step ends with the
  test suite green.
- **No half-implementations.** When a code branch is left
  unimplemented, raise (assert / `NotImplementedError`). Never
  return a placeholder.
- **Async-first.** New IO interfaces are async from inception.
- **At most one contiguous blob per Node** (design D2).
  `Readable` is optional on a Node; multi-region data becomes
  children, never multiple ranges within a Node.
- **Reinterpretation goes through the reserved `as` child**
  (design D3): `parent/as(type=zip)/...`. `as` is reserved.
- **Two child kinds** (design D9): pre-populated (in
  `node.children`, listed by `ls`) vs on-demand (spawned by
  name only — `as`, ELF symbols). On-demand children are never
  pre-enumerated.
- **Code layout** (design D1): generic VFS infra and generic
  format parsers go to `acrobe/vfs/`; vendor-specific format
  parsers live alongside vendor hardware components in
  `acrobe/component/<vendor>.py`. The old `acrobe/loadable/`
  package is deleted.

## Stepwise plan

Each step is meant to land as a discrete commit (or small
commit-group) on master. Earlier steps unblock later ones.

### Step 1 — Move `Component` → `Node` at `acrobe/node.py`

**Scope.** Pure rename + relocation. No behavioural change.

**Touches.**
- New file: `acrobe/node.py` with the renamed class.
- `acrobe/component/__init__.py` — no longer holds the base.
  Hardware-flavored subclasses keep living here; the package's
  `__init__.py` imports them and the registries they decorate.
- All `from ..component import Component` →
  `from ..node import Node`.
- All subclasses across `acrobe/adapter/`, `acrobe/protocol/`,
  `acrobe/component/`, `acrobe/target/`.
- All `isinstance(x, Component)` → `isinstance(x, Node)`.

**Validation.** `pytest` green; `acrobe --help` works; chip
program smoke test if hardware reachable.

### Step 2 — Define `Readable`, `Writable`, `Addressable` mixins + `Node.metadata`

**Scope.** Add the three mixins per design D6 alongside `Node` in
`acrobe/node.py` (no separate file — they're tiny). Add the
`metadata` property on `Node` (D6.1) returning `{}` by default.

Pure interface — no concrete implementations yet.

**Locked decisions.**
- Sync `size`, `load_address`, `addresses`, `metadata`. Async
  `read`, `write`.
- POSIX-pread read semantics (returns up to `size`, may return
  fewer at EOF).
- `Addressable.load_address` is the single canonical address;
  `Addressable.addresses` returns named aliases (default
  `{"load": load_address}`).

**Validation.** Lint clean. Mixin docstrings carry the
contract.

### Step 3 — `child_summon` options grammar upgrade

**Scope.** Replace today's `_parse_options` (bare-options list)
with the new `key=value` grammar from design D10:

- Every option is `key=value`. **Bare keys are not supported**
  (parse error).
- Quoted values via `"…"` with `\"` and `\\` escapes — supported
  from v1.
- Names must contain balanced parens; trailing `(...)` group at
  depth 0 with content matching the kv grammar is the options
  block.
- `()` (empty parens) is the explicit "no options" escape.

**Behavioural changes.**
- `option_set(opt: str)` becomes `option_set(key: str, value)`.
- All existing `option_set` overrides updated to the new
  signature. Today's bare-option call sites are migrated to
  `key=value` form (no dual-form support).

**Implementation.** A small forward tokeniser tracking quote
state and paren depth; identifies the trailing options block;
parses kv pairs. ~40-60 lines of Python.

**Validation.** Parser unit tests covering:

- Balanced-parens names: `crazy(I like it)`,
  `crazy(I like it)()` (the latter is the name with explicit
  null-options).
- Last-group-as-options: `bin(a=1)(b=2)` → name `bin(a=1)`,
  options `b=2`.
- Quoted values: `node(label="hello, world")`,
  `node(regex="(a=b)")`, `node(path="some dir/file.bin")`.
- Quoted-value escapes: `node(s="he said \"hi\"")`.
- Bare keys are rejected: `node(verbose)` is a parse error.
- Adversarial filenames (per design D2 boundary case)
  via programmatic API only.

### Step 4 — File-system root Node

**Scope.** A Node whose children are files in a directory.
`child_spawn(name)` returns a leaf Node holding an async file
handle and implementing `Readable` over its bytes.

**Touches.**
- New: `acrobe/vfs/__init__.py`, `acrobe/vfs/fs.py`.

**Validation.** A test that opens a fixture binary and reads a
slice asynchronously.

### Step 5 — Format detection: augment on `start()` + reinterpretation via `as`

**Scope.** Implement design D9 (augment, not replace) and the
reserved `as` child (D3).

**Mechanism.**
- `format_db` — free-standing registry, maps format names (`zip`,
  `elf`, `pof`, …) to factory callables that take a `Readable`
  parent and return a parser Node.
- `mime_db` — free-standing registry keyed by mime type, fed by
  a magic library (pick `puremagic` for now — no native deps).
  Resolves a mime type to a `format_db` name.
- `ext_db` — keyed by file extension, also resolves to a
  `format_db` name.
- On a `Readable` Node's `start()`: run extension + magic
  detection; if a format is identified, instantiate the parser
  in-place — its children are added directly to `self`, NOT
  under an `as` wrapper. The Node's own `read()` (if any) is
  preserved.
- On `child_summon("as", **opts)`: read `type=` (or
  `mime-type=`), resolve to a `format_db` entry, instantiate the
  parser with `self` as its `Readable` source, attach as the
  unique meaningful child of an `as` marker Node.

**`as` is reserved.** A container that would naturally have a
child literally named `as` must rename it.

**Touches.**
- `acrobe/vfs/__init__.py` — `format_db`, `mime_db`, `ext_db`.
- `acrobe/node.py` — `child_summon`'s `as` fall-through.

**Validation.** With one toy format (a zip stub):

- `archive.zip/entry.bin` works (auto-populated from extension).
- `unknown.bin/as(type=zip)/entry.bin` works (explicit `as`).
- `archive.zip/as(mime-type=application/zip)/entry.bin` works
  (mime-type form, redundant on top of auto-detection but
  legal).

### Step 6 — Altera POF as a Node tree (in `acrobe/component/altera.py`)

**Scope.** Migrate Altera POF handling out of
`acrobe/loadable/altera.py` and into
`acrobe/component/altera.py`, alongside the existing Altera
hardware Nodes.

**New shape.**
- `Pof` (Node + Readable, raw POF bytes) — `start()` parses
  sections, pre-populates `partition/N` per BOOT_INFO entry.
  Tool / flash / design strings are typed attributes
  (`pof.tool`, `pof.flash`, `pof.design`) and also surfaced via
  `metadata`.
- `PofPartition` (Node + Readable + Addressable) — `read()`
  returns the partition bytes; `load_address` is the
  partition's flash address. Children reference the backing
  `Readable` (the POF's parent file Node) directly, not through
  Pof.
- `Rbf` (Node + Readable) — applies bitswap; `read()` returns
  the JTAG-bit-order bitstream. Reached via
  `partition/N/as(type=rbf)`. Auto-detection on a `.rbf` file
  also creates an `Rbf` directly.

**File layout decision.** If
`acrobe/component/altera.py` becomes too long with hardware +
formats, split into a package:
`acrobe/component/altera/__init__.py` (re-exports),
`acrobe/component/altera/hardware.py`,
`acrobe/component/altera/formats.py`. Public import path stays
`acrobe.component.altera`.

**Removed.**
- `acrobe/loadable/altera.py`.
- `load_altera_pof` (returns Program). Path-walking replaces it.

**Validation.** Existing fixture POFs produce the same RBF bytes
when walked. Tests:
- `pof.pof/partition/0/as(type=rbf)` returns the expected blob.
- `flash.bin/as(type=pof)/partition/0/as(type=rbf)` works (POF
  inside a raw flash dump).

### Step 7 — Altera RBF, SOF as Nodes (in `acrobe/component/altera.py`)

**Scope.** Same treatment for raw RBF (leaf with bitswap
detection) and SOF (container with metadata attrs and a
`config_data` Readable child). Lives alongside `Pof` per Step 6.

**Other vendors.** Apply the same pattern in
`acrobe/component/xilinx.py`, `acrobe/component/gowin.py`,
`acrobe/component/lattice.py` for their respective format
parsers (currently in `acrobe/loadable/xilinx.py`, `gowin.py`,
`lattice.py`). Treat as a single sub-step of Step 7 — each
vendor's parser migrates next to its hardware code.

**Removed.** `acrobe/loadable/{altera,xilinx,gowin,lattice}.py`.

### Step 8 — STAPL as a Node container

**Scope.** Wrap the STAPL parser (`acrobe/stapl/`) so a STAPL
file appears as a tree: `var/<NAME>` for each variable; array
initialisers exposed as `Readable` leaves. STAPL root is
`Readable` (raw STAPL text — useful for diagnostics).

**Touches.** New `acrobe/vfs/stapl.py`. Existing parser stays
in `acrobe/stapl/`.

**Validation.** `prog.jam/var/J2/as(type=rbf)` returns the
expected blob.

### Step 9 — ZIP / tar containers

**Scope.** Two containers exercising the "flat-paths-to-tree"
helper. ZIP uses `zipfile`; tar uses `tarfile`. Both decode
bytes on demand. **Roots are NOT `Readable`** (per design "Per-
format read() semantics"); the parent file Node already covers
the raw bytes. Entries reference the parent's `Readable`
directly for backing storage.

**Touches.**
- `acrobe/vfs/zip.py`, `acrobe/vfs/tar.py`.
- Shared helper for synthesising intermediate directory nodes
  from flat entry paths.

**Validation.** Cross-format walk:
`archive.zip/inner.pof/partition/0/as(type=rbf)`.

### Step 10 — ELF as Node container (in `acrobe/vfs/elf.py`)

**Scope.** ELF root: pure structural Node (not `Readable`; the
parent file Node already covers raw ELF bytes).

**Pre-populated children.**
- `program/N` for program headers (Readable + Addressable;
  `load_address = LMA`).
- `section/<name>` for sections (Readable + Addressable;
  `addresses = {vma, lma, file_offset, load}`).
- `symbol` — namespace Node only. *Not* enumerated.

**On-demand children.**
- `symbol/<name>` — spawned by `ElfSymbols.child_spawn` via
  symbol-table lookup.
- `as(type=…)` (universal).

**Reverse lookups.** Methods on the ELF root:
- `symbol_at(addr) -> ElfSymbol | None`
- `symbols_in(start, end) -> list[ElfSymbol]`
- `section_at(addr) -> ElfSection | None`

These return references to actual Nodes (creating an
`ElfSymbol` if not previously summoned, then caching).

**.bss handling.** A NOBITS section's `read()` synthesises zeros.

**Touches.** `acrobe/vfs/elf.py` (generic — ELF is not vendor-
specific). Existing `acrobe/loadable/elf.py` deleted.

**Out of scope (deferred).** Relocations and DWARF — neither fits
the byte-data-Node mould; add when a use case appears.

### Step 11 — ihex / bin / literals as Nodes (in `acrobe/vfs/`)

**Scope.** Migrate generic loaders.

- `Ihex` (`acrobe/vfs/ihex.py`): Node + Readable + Addressable.
  `read()` returns a single contiguous blob
  (`min_addr`..`max_addr`, fill `0xFF`). `region/N` children
  for individual contiguous regions.
- `Bin` (`acrobe/vfs/bin.py`): Node + Readable + Addressable.
  The whole file is one blob; `load_address = 0` by default,
  overridable via `offset=…` option.
- Literals (`acrobe/vfs/literals.py`): synthesised Nodes via
  `format_db`. Path form `literal(value=DEADBEEF)`,
  `random(size=0x400)`, etc. Option-driven, since
  the value/size is a parameter, not a structural step.

**Removed.** `acrobe/loadable/ihex.py`, `acrobe/loadable/bin.py`,
`acrobe/loadable/literals.py`.

### Step 12 — Soft dissolution: VFS-backed Program (full removal deferred)

**Status as implemented**: soft dissolution. Program/Segment
remain as in-memory aggregator classes; `Program.from_file` is
rewired to walk the VFS (parsing happens entirely in
`acrobe.vfs.*`). Existing callers (Target.write/read/verify, FPGA
load() methods) continue to work unchanged. A new
`acrobe/program_view.py` provides `from_node` which builds a
Program from a started VFS subtree by collecting every
Readable+Addressable descendant.

Hard dissolution (full removal of Program/Segment, conversion of
target.write to take a Node subtree directly) is deferred — it
touches every FPGA load() method and the CLI; see open
decisions for the migration sketch.

### Step 12 (full) — Hard dissolution (deferred)

**Scope.** Remove `acrobe/loadable/model.py`'s `Program` and
`Segment` classes. Methods migrate as follows:

- `Program.from_file(...)` → `Node` walk via `vfs.summon(path)`.
- `Program.from_files(...)` → walks for each path; the result is
  a list of Node subtrees (or a synthetic `Group` Node — see
  decision below).
- `Program.simplified()` / `paged()` / `within()` → utilities in
  a new `acrobe/program_view.py` (or similar) that operate on
  *iterables of Addressable+Readable Nodes* and produce
  flattened, contiguous, page-aligned views as needed.
- `Program.save_bin()` / `save_hex()` → output formatters in
  `acrobe/format/` (or wherever feels natural). They consume
  Node subtrees and produce files.

**Decision needed.** Multi-source CLI invocations
(`program a.bin b.bin`) — do we synthesise a `Group` Node holding
both, or pass a list of Nodes through? *Recommendation:* a
`Group` Node (just a Node with N children attached) — it
preserves the "subtree of addressable children" walking
contract everywhere.

**Validation.** All loadable CLI commands continue to work.
Outputs of `to-bin`, `to-hex`, `to-c-blob`, `to-vhdl-blob` are
byte-identical to before.

### Step 13 — CLI integration

**Scope.** Wire VFS path resolution into the CLI and add the new
`acrobe resource` command group.

**Existing commands updated.**
- `ProgramParamType.convert` → walks via VFS, returns a Node.
- `acrobe/cli/loadable.py` commands consume Node subtrees
  internally (using `program_view` utilities).
- `acrobe/root.py` — single path parser used by both hardware
  and file paths (they share the Node tree, so today's
  hardware `child_summon` works for both).

**New command group: `acrobe resource`** (`acrobe/cli/resource.py`).

- `acrobe resource ls <path> [-r] [-l] [--depth N]` — list
  pre-populated children. `-l` shows mixin tags (R/W/A), size,
  `load_address`. On-demand children (`as`, ELF symbols) NOT
  listed.
- `acrobe resource info <path>` — class, mixins, size,
  addresses, metadata dict, child count + names.
- `acrobe resource cp <src> [<dst>] [--offset N] [--size N]`
  — `<dst>` defaults to `-` (stdout via Click). Errors cleanly
  if `<src>` is non-`Readable`.
- `acrobe resource hexdump <path> [--offset N] [--size N]` —
  formatted hex with addresses honouring `load_address` if
  `Addressable`.
- `acrobe resource tree <path> [--depth N]` — recursive `ls`
  with box-drawing.

**Conversion commands deliberately deferred** — pinned in open
decisions below.

**Removed.** The `:format` and `:+offset` suffix syntax in
`Program.from_file`. Replaced by `(offset=…)` option and
`as(type=…)` child step.

### Step 14 — Live region as VFS root

**Scope.** Make a flash region (e.g. SPI flash) implement
`Readable` (and `Writable + Addressable` if it's a real chip)
so it can host file containers as children:
`flash/data/as(type=pof)/partition/1/as(type=rbf)`.

**Touches.** `acrobe/component/spi_flash.py` (or wherever flash
regions live).

**Validation.** Manual: read a flash chip whose contents are a
known POF, walk into a partition, verify bytes.

### Step 15 — `Writable` propagation

**Scope.** Define and implement `Writable` propagation through
passthrough containers.

- Slice container Node — propagates `Writable` from parent.
- SPI flash region — implements `Writable`.
- Container Nodes (POF, ZIP) — do not implement `Writable`;
  attempting `write()` on a non-passthrough chain raises.

**Out of scope (v1).** POF/ZIP rewriting; the `stop()`
finalisation hook is defined but not exercised.

## Order rationale

Steps 1–3 are pure refactors with no new behaviour; they unblock
everything else.

Steps 4–5 build the file-side machinery and the dispatch core.
Step 6 validates the model against the motivating Altera case.

Steps 7–10 broaden coverage to formats that demonstrate
composition. Steps 11–12 close the Program/Segment dissolution.

Step 13 is the user-visible payoff; 14–15 close the loop with
hardware composition and write support.

## Validation gates

Every step ends with:

1. `pytest` green.
2. `acrobe loadable dump <fixture>` round-trips for at least one
   fixture in each format already supported.
3. `acrobe chip program -r <path> <file>` smoke test (manual,
   when hardware is reachable).

## Open decisions captured for later

- Step 6: file split for `acrobe/component/altera.py` once it
  grows — single file vs package with `hardware.py` /
  `formats.py`. Decide when the file becomes uncomfortably
  large.
- Step 12: multi-source group Node vs list-of-Nodes for
  `program a.bin b.bin`-style invocations. *Leaning Group
  Node* (uniform "subtree of addressable children" contract).
- Step 13: shape of conversion commands.
  - Option A: `acrobe resource cp <src> <dst> --format=ihex` —
    extra flag on `cp`.
  - Option B: `acrobe format convert <src> <dst> --to=ihex` —
    separate command group dedicated to format conversion.
  - Option A is more Unix-flavoured (one verb, polymorphic
    output); B keeps `cp` strict (raw bytes only) and groups
    related operations. Pin when implementing — likely after
    using `cp` for a while informs which feels right.

Resolved (pinned in this revision):

- ~~`load_address` vs `base_address`~~ → `load_address`.
- ~~Symbols as Readable children of `symbol/`~~ → on-demand
  spawn via `child_spawn`; not pre-enumerated.
- ~~`as` listed by `ls`?~~ → no, on-demand only.
- ~~Generic vs vendor format home~~ → vendor → component
  package; generic → `acrobe/vfs/`.
- ~~Bare keys in option grammar~~ → not supported (parse
  error); always `key=value`.
- ~~Quoted values~~ → supported from v1.
