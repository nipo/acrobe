"""VFS infrastructure and generic format parsers.

This package contains:

- The file-system root Node (`fs.py`).
- The format dispatch registries (`format_db`, `mime_db`, `ext_db`)
  for the `as(...)` reinterpretation child and auto-detection on
  `start()`.
- Generic format Nodes (ihex, bin, ELF, ZIP, tar, literals).

Vendor-specific format parsers (Altera POF, Xilinx bitstreams, etc.)
live in `acrobe.component.<vendor>` next to the related hardware
code.

See `docs/vfs-design.md` (D3, D9).
"""

from ..db import Db, NoMatch
from ..node import Node, Readable


# format_db: format name → Node class.
# The class must accept (name: str, source: Readable) in its
# constructor and populate self._children in start().
format_db = Db("format")

# ext_db: extension (lowercase, no dot) → format name string.
# Compound extensions (e.g. "tar.gz") are checked before single.
ext_db = Db("file_ext")

# mime_db: mime type → format name string.
mime_db = Db("mime")

# Magic-byte detectors: list of fn(head: bytes) -> str | None
# Each detector inspects the head of a Readable and returns a
# format_db key on match, or None to defer.
_magic_detectors = []


def register_format(format_name, *, exts=(), mimes=()):
    """Decorator: register a format parser class.

    The decorated class must be a Node subclass whose constructor
    accepts (name: str, source: Readable). Its start() should
    populate self._children with structural children.

    Optional `exts` / `mimes` register the format for auto-detection.
    """
    def decorator(cls):
        format_db.register(format_name)(cls)
        for ext in exts:
            ext_db.register(ext.lower())(format_name)
        for mime in mimes:
            mime_db.register(mime.lower())(format_name)
        return cls
    return decorator


def register_magic(fn):
    """Decorator: register a magic-byte detector.

    fn(head: bytes) -> str | None
    Returns a format_db key on match, or None to defer to the next
    detector.
    """
    _magic_detectors.append(fn)
    return fn


def detect_by_extension(name):
    """Resolve a filename to candidate format names via ext_db.

    Tries compound extensions first (file.tar.gz → tar.gz), then
    progressively shorter ones, ending with the simple extension.
    Returns a list of all formats registered for the longest matching
    extension, in registration order. Returns [] if no match.

    Multiple formats can register for the same extension (e.g. legacy
    Altera RBF and Agilex CMF both use ".rbf" but have different
    contents); auto_populate tries each in turn, falling through on
    NoMatch.
    """
    if "." not in name:
        return []
    parts = name.lower().split(".")
    # Try compound extensions, longest first
    for i in range(1, len(parts)):
        ext = ".".join(parts[i:])
        try:
            return list(ext_db.get(ext, allow_default=False))
        except NoMatch:
            continue
    return []


def detect_by_mime(mime):
    """Resolve a mime type to a format name via mime_db.

    Returns the first registered format. Used by `as(mime-type=...)`
    where the user has explicitly chosen a format, so a single
    deterministic answer is wanted.
    """
    try:
        return mime_db.get(mime.lower(), allow_default=False)[0]
    except NoMatch:
        return None


async def detect_by_magic(source):
    """Try registered magic-byte detectors against the head of
    `source`. Returns the first match, or None."""
    if source.size == 0:
        return None
    head = await source.read(0, min(source.size, 4096))
    for fn in _magic_detectors:
        result = fn(head)
        if result is not None:
            return result
    return None


async def auto_detect_candidates(name, source):
    """Return an ordered list of format-name candidates for `source`.

    Extension matches come first (in registration order), followed by
    a magic-byte match if it adds anything new. Used by auto_populate
    to walk candidates until one parser accepts the bytes.
    """
    candidates = list(detect_by_extension(name))
    magic = await detect_by_magic(source)
    if magic is not None and magic not in candidates:
        candidates.append(magic)
    return candidates


async def auto_populate(target, source, name):
    """Run auto-detection on `source` (a Readable). Tries each
    candidate format in turn (extension matches first, then magic);
    a parser may signal "wrong format" by raising NoMatch from
    start(), in which case the next candidate is tried.

    Behaviour:
    - No candidates at all (unknown extension and no magic match): no-op.
    - One or more candidates, all reject: re-raise the last NoMatch.
    - First candidate that succeeds wins.

    Usage from a Readable Node's start():
        await auto_populate(self, self, self.name)
    """
    last_err = None
    for fmt in await auto_detect_candidates(name, source):
        try:
            await populate_format(target, fmt, source)
            return
        except NoMatch as e:
            last_err = e
            continue
    if last_err is not None:
        raise last_err


async def populate_format(target, format_name, source, *, parser_opts=None):
    """Apply a format to `target`: instantiate the parser, run its
    start(), then transplant its children to `target` and merge
    metadata.

    Per design D9: parser children attach directly to `target`,
    NOT under a wrapper. The parser instance itself is kept
    accessible as `target._format_parsers` (a list — successive
    detections append).

    `parser_opts` (dict) — any options to apply to the parser via
    option_set BEFORE start(). Used by AsNode to forward extra
    options like `offset=`, `size=`, `value=` to the format parser.
    """
    cls = format_db.get(format_name, allow_default=False)[0]
    parser = cls(name=format_name, source=source)
    if parser_opts:
        for k, v in parser_opts.items():
            parser.option_set(k, v)
    await parser.start()
    parser._started = True

    for child in list(parser._children):
        parser._children.remove(child)
        child._parent = target
        target._children.append(child)
    target.children_changed()

    target._metadata.update(parser.metadata)

    # The parser instance is kept accessible on `target` for code
    # that wants typed methods (e.g. Elf.symbol_at). Methods that
    # walk children must use parser._target (set here) — parser's
    # own ._children was emptied by the transplant.
    parser._target = target

    if not hasattr(target, "_format_parsers"):
        target._format_parsers = []
    target._format_parsers.append(parser)


class FormatNode(Node):
    """Base class for format parser Nodes.

    Constructed with (name, source: Readable). `source` is the byte
    stream to parse. Subclasses override start() to parse `source`
    and populate self._children.
    """

    def __init__(self, name, source):
        super().__init__(name)
        self._source = source

    @property
    def source(self):
        return self._source


class AsNode(Node):
    """Reserved child of any Readable Node. Reinterprets the parent's
    bytes as a specified format.

    Reached via path syntax `parent/as(type=zip)/...` or
    `parent/as(mime-type=application/zip)/...`. Extra options on the
    `as(...)` invocation (other than `type`/`mime-type`) are forwarded
    to the format parser via its option_set, before start().

    start() looks up the format and populates self with the format's
    children (parser's children are transplanted to self, per D9).
    """

    def __init__(self, name="as"):
        super().__init__(name)
        self._format_name = None
        self._parser_opts = {}

    def option_set(self, key, value):
        if key == "type":
            self._format_name = value
        elif key == "mime-type":
            fmt = detect_by_mime(value)
            if fmt is None:
                raise ValueError(f"Unknown mime type: {value}")
            self._format_name = fmt
        else:
            # Forward to the format parser at start time.
            self._parser_opts[key] = value

    async def start(self):
        if self._format_name is None:
            raise ValueError(
                f"{self.fqdn}: as() requires type= or mime-type= option")
        if not isinstance(self._parent, Readable):
            raise TypeError(
                f"{self.fqdn}: as() parent must be Readable")
        await populate_format(
            self, self._format_name, self._parent,
            parser_opts=self._parser_opts)


from .fs import FsRoot, FileNode  # noqa: F401, E402
from . import stapl  # noqa: F401, E402  registers STAPL parser
from . import zip as _zip  # noqa: F401, E402  registers ZIP parser
from . import tar  # noqa: F401, E402  registers tar parser
from . import elf  # noqa: F401, E402  registers ELF parser
from . import bin as _bin  # noqa: F401, E402  registers Bin parser
from . import ihex  # noqa: F401, E402  registers Ihex parser
from . import uf2 as _uf2  # noqa: F401, E402  registers Uf2 parser
from . import literals  # noqa: F401, E402  registers literals
