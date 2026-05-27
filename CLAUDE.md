= Presentation

This project is acrobe, a debug probe toolset that started as a port
from synchronous Python code to asyncio-based code. The original
synchronous sibling project is crobe, checked out in @../crobe .
When in doubt about *what* a feature is meant to do (as opposed to
*how* the async version implements it), crobe is the reference.

= Philosophy (carried over from crobe)

Crobe is a toolset of serial interfaces (UART, SPI, I2C, JTAG, SWD,
etc.) that drives various circuits through gateway adapters
(usually USB, sometimes network-attached). Acrobe inherits four
hard rules from it:

* **Composability**: every protocol is modelled as a class and every
  action we can perform on a protocol is an object as well.
* **Pluggability**: there are registries at every compatibility
  layer (protocols, chips, …) that can be dynamically extended.
  Custom components plug in through the `acrobe_plugin` namespace.
* **Visibility**: the component tree mirrors the hardware
  hierarchy and information flow. Each layer logs the operations
  it performs on the layer beneath it; the user can focus on a
  single component by name or regex (see @docs/logging.md).
* **Dynamic discovery**: use the protocol's self-discovery
  features wherever they exist. Avoid asking the user to describe
  hardware acrobe could probe instead.

= Documentation

Architectural and "how do I add X" docs live under `docs/`. Start
with @docs/acrobe-structure.md — it explains the four-layer
shape (adapter / component tree / target framework / user
operation layer) and links the rest:

* @docs/acrobe-structure.md — overall shape, layer boundaries,
  unified Node tree, where things live.
* @docs/node-model.md — Node lifecycle, parenting, start/stop,
  child lookup/spawn/summon, options, tree navigation.
* @docs/adding-an-adapter.md — bringing up a new physical
  adapter (USB / network / FPGA-backed).
* @docs/adding-a-target.md — bringing up a new chip target,
  including the Puppet framework for on-target stubs.
* @docs/conventions.md — project-wide coding conventions
  (frozen-dataclass ops, Db-based subclass discovery, lifecycle
  hooks, naming, comments). **Read before writing code.**
* @docs/logging.md — log levels, CLI controls, programmatic use.
* @docs/events.md — process-global pub/sub bus: publishing,
  subscribing, canonical actions, FsWatcher, USB hotplug.
* @docs/testing.md — pytest setup, mocking the wire, known
  isolation gotcha.
* @docs/vfs-design.md — VFS, the `as(type=...)` syntax, format
  dispatch.
* @docs/vfs-plan.md — VFS evolution notes.
* @docs/jtag-refresh.md — JTAG re-discovery semantics across
  detach / TLR.

Plan files under `docs/plans/` are living design documents —
read the matching one first when picking up a deferred area:

* @docs/plans/target.md — target framework.
* @docs/plans/itap.md — iTap probe support.
