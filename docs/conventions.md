# Project-wide coding conventions

These rules constrain *every* piece of code in the acrobe tree.
They are deliberately small in number and uniformly applied.
When a convention is broken in a part of the codebase, that part
is wrong, not the convention.

## Operation classes are frozen dataclasses

All protocol-level operations are immutable inputs:

```python
@dataclass(frozen=True, slots=True)
class Shift:
    tdi: BitString
    read_tdo: bool = True
    post_run: int = 0
```

The future returned by `Batcher.post(op)` resolves to the
natural result value (e.g. a `BitString` for a reading shift,
`None` otherwise).

The legacy "op carries result via mutation" pattern was inherited from
crobe and does not map to futures. It was removed when ops became
transportable over the wire layer — mutation across the wire is
meaningless. New code MUST follow the immutable convention; result
fields on op classes do not exist.

## Concrete-subclass discovery via `Db`

`acrobe.db.Db` is the registry/factory pattern used everywhere a
new subclass needs to be picked at runtime from a discoverable
identifier:

* `Tap.db` keys on IDCODE with a custom equality function
  (revision masking).
* `adapter_db` keys on `AdapterInfo.matches(descriptor)` so one
  `AdapterInfo` entry can cover a VID:PID family.
* `Ap.db` keys on IDR (revision/variant masked).
* Format dispatch in `acrobe.vfs` uses `Db` too.

When adding a new chip, adapter, or format, register against the
relevant `Db`. Do not wire an `if/elif` chain in a parent class —
plugin registration must work without source edits.

## Node-tree contract

Anything in the live tree subclasses `acrobe.node.Node`. The base
class defines parenting, async `start()` / `stop()` with
top-down `start_tree` / `stop_tree` walks, idempotent
single-flight startup, pre-populated vs on-demand children,
`child_lookup` / `child_spawn` / `child_summon`, `option_set`,
and the `children_of_class` / `parent_of_class` navigators. See
`docs/node-model.md` for the full reference; subclasses follow
that contract verbatim — do not re-invent lifecycle, do not add
parallel parenting attributes.

## Lifecycle hooks for non-Node resources

Anything holding a background context (sockets, USB handles,
aiohttp sessions, subprocesses) follows the symmetric pattern:

```python
def __init__(self, ...):
    ...
    on_shutdown(self.close)

async def close(self):
    cancel_shutdown(self.close)
    ...
```

The CLI drains the lifecycle automatically on result-callback /
context-close. Library users call `acrobe.lifecycle.shutdown()`
explicitly before exit. Without this, USB contexts leak past
interpreter shutdown and ausb's daemon thread can be torn down
mid-libusb-call, triggering a libusb assertion.

## Descriptive module names

No `util.py` / `common.py` / `helpers.py`. Each module's name
says what's in it. Examples in tree: `freq_capper.py`,
`memory_map.py`, `part_id.py`, `bitstring.py`. If you're tempted
to name something `utils`, the contents probably belong as
methods of an existing class.

## Class methods over standalone functions

The codebase leans heavily on classes even for short helpers.
Group OO-shaped logic as class methods, static methods, or
instance methods of the class they're about — not as free
functions in the module. The few free functions that exist are
genuinely module-level (e.g. `make_hw_root()`, `make_adapter_name()`),
not helpers waiting to be attached.

## Member naming

Python private members are prefixed with double underscores
(`self.__foo`); the name-mangling enforces the private intent.
Public members and protected-but-visible members have no prefix
(`self.foo`). Single-underscore prefix is reserved for the few
places acrobe deliberately echoes Python's conventional
"protected" hint and is not the default — when in doubt, choose
between no prefix (public) or double underscore (private).

## Comments

Default to writing no comments. Only add one when the *why* is
non-obvious: a hidden constraint, a subtle invariant, a
workaround for a specific bug, behaviour that would surprise a
reader. Don't paraphrase the code — well-named identifiers
already describe what it does. Don't reference the current task,
fix, or callers ("used by X", "added for the Y flow") — those
belong in the commit message, not in the source.
