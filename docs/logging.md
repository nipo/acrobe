# Logging

Every `Node` implicitly has a logger reachable as `self.logger`.
Its name is the node's path in the live tree
(`proby-9.jtag.chain.0.dap.ahb-ap@0`), so the global logging
handler can filter by level *and* by origin — the user focuses
on a single component without grep-ing the output.

## Levels

The standard `logging` levels are augmented with one extra
acrobe-specific level:

| Level    | Value | Used for                                          |
|----------|-------|---------------------------------------------------|
| ERROR    | 40    | Genuine failures.                                 |
| WARNING  | 30    | Recoverable issues, expected-but-rare quirks.     |
| NOTE     | 25    | Important user-visible facts (chip detected, MAC, package). |
| INFO     | 20    | Routine progress.                                 |
| DEBUG    | 10    | Implementation-detail tracing.                    |
| TRACE    | 7     | Very fine-grained tracing.                        |
| PROTOCOL | 5     | Raw protocol bytes / bit patterns / wire traffic. |

`PROTOCOL` is used by `Batcher` subclasses to log every batch
they flush (`USB >> N bytes, expect M back` style). It is
expensive to format — `Batcher` and the FTDI MPSSE engine both
guard `logger.protocol(...)` calls with
`logger.isEnabledFor(PROTOCOL)` so the list-comprehension /
formatting overhead is skipped when the level is off. New code
that logs in tight inner loops should do the same.

## CLI controls

The root group exposes:

* `-v` / `-q` — bump verbosity up or down one level. Stacks
  (`-vvv` → TRACE, `-vvvvv` → PROTOCOL). `-q` hides progress
  bars too.
* `-t` — prefix each line with a timestamp.
* `-b` — disable ANSI colour.
* `--silent <name>` — suppress one logger by exact name. May be
  passed multiple times.
* `--silent-re <regex>` — suppress logger names matching the
  regex.
* `--only-re <regex>` — show *only* logger names matching the
  regex.

Logger names are node tree paths joined by `.`, which makes
`--only-re 'dap\.ahb-ap'` enough to isolate one CoreSight AP's
chatter while a long flash program runs.

## Programmatic use

Library users invoke `acrobe.log.setup(level=...,
silent=(...), silent_re=..., only_re=..., progress=...)` once,
typically right after constructing `HwRoot`. Without `setup`,
Python's default `WARNING`-or-higher console handler is in
effect — fine for ad-hoc scripts, not enough to see the routine
chip-detected / region-erased messages acrobe emits at `NOTE`.

## Adding new log calls

When you add a new log call:

* Pick the level from the table above. Most additions belong at
  `DEBUG` or `INFO`; `NOTE` is reserved for facts the user
  genuinely wants to see by default (chip identification,
  package, factory UID, …).
* Use `self.logger` from a `Node`. Never `logging.getLogger(__name__)`
  — that loses the per-tree-path filtering and ends up unfiltered
  in `--only-re` flows.
* Avoid logging structured Python objects directly (`%s` on a
  list of 50k ops). For hot-path summaries, format scalars
  (`"N=%d total=%d"`); keep object dumps behind `isEnabledFor`
  guards.
