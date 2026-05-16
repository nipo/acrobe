# Testing

acrobe uses `pytest` with `asyncio_mode = auto` (configured in
`pyproject.toml`). Async tests still use `@pytest.mark.asyncio`
in this codebase as the existing convention.

All tests live flat under `tests/test_<topic>.py`. There is no
nested test tree; tests for a layer's concrete subclasses go in
the same file as tests for that layer's framework.

## Running tests

```
pytest tests/                       # full suite
pytest tests/test_nrf52.py          # one topic
pytest -k name_substring            # filter
pytest -x tests/test_jtag.py        # stop on first failure
```

## Known test-isolation issue

`tests/test_jtag.py` calls `Tap.db._registry.clear()` in two
tests' cleanup. This wipes globally-registered `Tap` subclasses;
only matters when running a filtered subset that puts
`test_jtag.py` before `test_fpga` / `test_gowin` / similar files
that rely on those globals. Pre-existing; harmless in default
alphabetical order.

If you hit unexplained "no subclass found for IDCODE X" failures
under a filtered run, this is usually why — run the full suite
or include `test_jtag.py` *after* the affected files.

## Mocking the wire

The hard parts to test are the JTAG / DAP transactions. The
canonical pattern (see `tests/test_nrf52.py`) is to build a
synthetic component tree rather than going through real
discovery:

* `MockAp(MemAp)` bypasses `MemAp.__init__` via direct
  `Node.__init__`, implements `read32` / `write32` / `mem_read`
  / `mem_write` returning resolved futures, and tracks every
  transaction for assertions.
* Build a synthetic component tree: a `Node`-based DP, attach
  the `MockAp`, attach a `RomTable` with an `Scs` child —
  enough to make the chip's probe function succeed without
  hardware.
* For Loadable + region tests: instantiate them stand-alone
  (don't go through discovery), drive `Loadable.write` on a
  small `MemoryMap`, assert the `MockAp`'s `writes` log
  matches the expected NVMC / MSC sequence.

Do not try to integrate with the real Mem-AP machinery — it
pulls in the entire DAP + Batcher stack and isn't worth the
complexity for unit testing. The synthetic-tree pattern is
small, deterministic, and runs in milliseconds.

## What's worth testing

* **Probe decline path.** A probe that mismatches must raise
  `NoMatch` cleanly. Test both the recognised-part path and
  the unknown-part path.
* **Region erase + write sequencing.** For a Flash region with
  page-aligned erase, the order and addresses of MSC / NVMC
  pokes are stable enough to assert on directly.
* **Full Loadable.write through to mock bus.** This covers the
  cross-layer integration (plan_update → erase → paged write)
  without touching real hardware.

What's *not* worth unit testing:

* The Batcher itself — already covered by `tests/test_engine.py`.
* USB transports — hardware-in-the-loop only.
* GDB protocol framing — covered by `tests/test_gdb.py`.
