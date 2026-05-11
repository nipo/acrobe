= Presentation

This project is acrobe, a debug probe toolset that started as a port
from syncrhonous python code to asyncio-based code.  Original
synchronous sibling project is crobe, checked out in @../crobe .

= Prior Art

Crobe, at first, is a toolset of serial interfaces (UART, SPI, I2C,
JTAG, SWD, etc.).  It supports debugging model of various circuits,
can inspect various interfaces.

Root of crobe system is a set of adapters, which are gateways to
hardware protocols.  Usually, they are USB-based devices, but they may
also be network-based.

Crobe insists on some aspects:

* composability: every protocol is modeled as a class and all actions
  we can perform on a protocol are objects as well.

* pluggability: there are registries at every compatibility layer
  (protocols, chips, etc) that can be dynamically plugged. This allows
  to hook custom components from plugins.

* visibility: there is a component model that mimmics the hardware
  hierarchy and information flow. All layers log the operation they
  perform on lower layers.

* dynamic discovery: crobe tries hard to use every protocol's self
  discovery features. Avoid relying on the user describing the
  hardware as much as possible.

= Design

== Component Tree

There is a python object instance tree of `acrobe.node.Node` (or
descendants) that mimmic the DUT's hardware hierarchy.  Node defines
rules for object life cycle, discovery, etc.

== Operation Batching

The new version heavily relies on asyncio. All operations on each
layer are modeled as Futures. All operations are batched.

This system is abstracted out in `acrobe.engine.Batcher`.  When upper
layer calls lower layer to perform an action, lower layer returns a
future and posts an actual action to batch later. As soon as upper
layer awaits on a result, control eventually transfers to lower layer
dispatch that will actually trigger actions on the hardware.

== Logging

Every Node implicitly has a logger in `self.logger`. It is able to
report messages with a logging ID matching the node instance path in
the tree. A global logging handler is able to filter messages by level
and origin, in a way user can focus on node of interest.

= Layout

* `acrobe/node.py` — `Node` base, child lifecycle, path resolution.
* `acrobe/engine.py` — `Batcher` async batching engine.
* `acrobe/db.py` — `Db`, the registry/factory pattern used everywhere
  (adapter handlers, TAP IDCODE → subclass, format dispatch).
* `acrobe/bitstring.py` — `BitString` (LSB-first), the domain
  bit-vector type. Used pervasively in JTAG paths.
* `acrobe/lifecycle.py` — process-wide cleanup registry
  (`on_shutdown(cb)`, `shutdown()`).
* `acrobe/configuration.py` — YAML config from
  `~/.config/acrobe.conf` (override via `$ACROBE_CONFIG`).
* `acrobe/adapter/` — physical adapters (FTDI, Proby, AJI client,
  XVC client). `model.py::HwRoot` is the root of the live tree.
* `acrobe/protocol/` — protocol layers (JTAG, SPI, I2C, SWD).
* `acrobe/component/` — chip/board specific code (Altera, Gowin,
  Xilinx, ARM cores). Auto-loaded via the `acrobe_plugin`
  namespace package.
* `acrobe/target/` — programming abstraction (Target, Field,
  Region, Flash). Field discovers Targets from a started subtree.
* `acrobe/vfs/` — VFS + format dispatch. `as(type=...)` syntax,
  format DBs.
* `acrobe/wire/` — client/server transport. See `PLAN_wire.md`.
* `acrobe/cli/` — asyncclick subcommands. `console.py::main` is
  the CLI entry; loads plugins, then dispatches.

= Conventions

* **Operation classes are frozen dataclasses.** Inputs only — no
  result fields. The future returned by `Batcher.post(op)` resolves
  to the natural result value (e.g. a `BitString` for a reading
  shift, `None` otherwise). The legacy "op carries result via
  mutation" pattern was removed; new code MUST follow the
  immutable convention.

* **Concrete-subclass discovery via Db.** `Tap.db` keys on IDCODE
  with a custom equality function (revision masking). Adapter
  matchers similarly use `Db(eq_func=AdapterInfo.matches)`. When
  adding a new chip / adapter, register against the relevant Db.

* **Lifecycle hooks for non-Node resources.** Anything holding a
  background context (sockets, USB handles, aiohttp sessions)
  registers `on_shutdown(self.close)` at construction and
  `cancel_shutdown(self.close)` at the start of close. CLI's
  `cli.result_callback` drains the lifecycle automatically; library
  users call `acrobe.shutdown()` explicitly.

* **Names are descriptive.** No `util.py` / `common.py`. Each
  module's name says what's in it.

* **Class methods over standalone functions** for OO-shaped logic;
  the codebase leans heavily on classes even for short helpers.

= Plan files

* `PLAN.md` — Layer 4 (target framework).

These are living documents — when picking up a deferred area,
read the matching PLAN first.

= Tests

* `pytest` with `asyncio_mode = auto` (configured in
  `pyproject.toml`). Async tests still use `@pytest.mark.asyncio`
  in this codebase; existing convention.
* All tests are flat under `tests/test_<topic>.py`.
* **Known test-isolation issue**: `tests/test_jtag.py` calls
  `Tap.db._registry.clear()` in two tests' cleanup. Wipes
  globally-registered Tap subclasses; only matters when running a
  filtered subset that puts test_jtag.py before test_fpga /
  test_gowin / similar. Pre-existing; harmless in default
  alphabetical order.

= CLI shape

`acrobe.cli.console::main` calls `plugin.load_plugins()` then
runs `base.cli()`. Subcommands attach to `base.cli` as click
groups (`@base.cli.group`). The root group's options
(`-v`/`-q`/etc.) configure logging in its async body.

`make_hw_root()` (in `adapter/model.py`) is the canonical
factory — it wires USB, TTY, AJI, XVC, and Wire enumerators in
one call. CLI subcommands that take `-r <path>` resolve that path
through `hw_root.child_summon(*parts)`.
