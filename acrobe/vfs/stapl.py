"""STAPL (.jam, .stp) format as a VFS Node.

Wraps the existing STAPL parser in `acrobe.stapl` so a STAPL file
appears in the tree as:

    file.jam/var/<NAME>     # for each BOOLEAN array variable with init
                            # (Readable + bit_count metadata)

Use `file.jam/var/J2/as(type=altera_rbf)/bitstream` to interpret a
variable initializer as RBF, etc.

Notes/actions/procedures/data_blocks are exposed via metadata
(introspection-only). Their typed access is via the parsed STAPL
program object held on `_program`.
"""

from ..node import Node, Readable
from ..db import NoMatch
from . import FormatNode, register_format


class StaplBooleanArray(Node, Readable):
    """A BOOLEAN array initializer from a STAPL DATA block.

    The underlying ACA-decoded byte array is fetched lazily from the
    BooleanLiteral on first read; subsequent reads slice the cached
    bytes.
    """

    def __init__(self, name, literal):
        super().__init__(name)
        self._literal = literal

    @property
    def size(self) -> int:
        return len(self._literal.data)

    async def read(self, offset, size):
        data = self._literal.data
        if offset < 0 or offset > len(data):
            raise ValueError(f"offset {offset} out of range")
        avail = len(data) - offset
        n = min(size, avail)
        if n <= 0:
            return b""
        return data[offset:offset + n]

    @property
    def metadata(self) -> dict:
        return {
            "bit_count": self._literal.bit_count,
            "byte_size": len(self._literal.data),
            **self._metadata,
        }


@register_format("stapl_jam",
                 exts=["jam", "stp", "stapl"],
                 mimes=["application/x-stapl"])
class Stapl(FormatNode):
    """STAPL file container."""

    def __init__(self, name, source):
        super().__init__(name, source)
        self._program = None

    async def start(self):
        from ..stapl import load
        from ..stapl.parser import BooleanDecl, BooleanLiteral

        text_bytes = await self._source.read(0, self._source.size)
        text = text_bytes.decode("ascii", errors="replace")
        try:
            program = load(text, check_crc=False)
        except Exception as exc:
            raise NoMatch("stapl_jam", f"parse failed: {exc}") from exc
        self._program = program

        # Surface notes / actions / procedures as metadata.
        notes = {}
        for note in program.notes:
            notes[note.key] = note.value
        self._metadata.update({
            "notes": notes,
            "actions": list(program.actions.keys()),
            "procedures": list(program.procedures.keys()),
            "data_blocks": list(program.data_blocks.keys()),
        })

        # Build var/<NAME> namespace from BOOLEAN array initializers
        # in DATA blocks.
        var_container = Node("var")
        for db in program.data_blocks.values():
            for stmt in db.statements:
                if isinstance(stmt, BooleanDecl) and \
                        isinstance(stmt.init, BooleanLiteral):
                    leaf = StaplBooleanArray(stmt.name, stmt.init)
                    var_container._child_attach(leaf)
        self._child_attach(var_container)


# STAPL files start with NOTE statements but also have lots of
# preamble/whitespace; magic detection is unreliable. Stick to
# extensions for auto-detection.
