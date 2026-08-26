"""IDL introspection — `dump_idl()` walks the registry and prints the
catalog in a human-readable form.

Used as the phase-1 smoke test: register some Transportables, run
`acrobe wire dump-idl`, see them.
"""

import dataclasses

from .registry import Registry, RegistryEntry, default_registry


def dump_idl(registry: Registry | None = None) -> str:
    """Render the IDL of every node in `registry` (or the default)."""
    reg = registry or default_registry()
    chunks = []

    for entry in reg.nodes():
        chunks.append(_render_node(entry, reg))

    orphans = [e for e in reg.all_entries()
               if e.kind in ("op", "error") and not _used_by_any(e, reg)]
    if orphans:
        chunks.append(_render_orphans(orphans))

    return "\n".join(chunks)


def _render_node(entry: RegistryEntry, reg: Registry) -> str:
    lines = [f"node {entry.cls.__name__}  ({entry.type_uuid})"]
    used = [reg.lookup_by_uuid(u) for u in entry.uses]
    ops = [u for u in used if u.kind == "op"]
    errors = [u for u in used if u.kind == "error"]
    if ops:
        lines.append("  ops:")
        for u in ops:
            lines.append(f"    {_render_type(u)}")
    if errors:
        lines.append("  errors:")
        for u in errors:
            lines.append(f"    {_render_type(u)}")
    return "\n".join(lines)


def _render_type(entry: RegistryEntry) -> str:
    head = f"{entry.cls.__name__}  ({entry.type_uuid})"
    if dataclasses.is_dataclass(entry.cls):
        fields = [f"{f.name}: {_format_annotation(f.type)}"
                  for f in dataclasses.fields(entry.cls)]
        if fields:
            return f"{head}({', '.join(fields)})"
        return f"{head}()"
    return f"{head}  [custom codec]"


def _format_annotation(ann) -> str:
    if isinstance(ann, str):
        return ann
    if isinstance(ann, type):
        return ann.__name__
    return repr(ann)


def _used_by_any(entry: RegistryEntry, reg: Registry) -> bool:
    for n in reg.nodes():
        if entry.type_uuid in n.uses:
            return True
    return False


def _render_orphans(orphans: list[RegistryEntry]) -> str:
    lines = ["unreferenced types:"]
    for e in orphans:
        lines.append(f"  {_render_type(e)}  [kind={e.kind}]")
    return "\n".join(lines)
