"""RP2040 PICOBOOT USB bootloader as an acrobe adapter.

Triggers VID/PID registration in `adapter_db` so the device shows
up in `acrobe info adapters` and can be resolved via `-r
rp2040-bootsel-<serial>/picoboot`.

The underlying USB framing lives in
`acrobe.component.raspberry.picoboot_transport`; the puppet
(`PicobootPuppet`) and Target wiring live in
`acrobe.component.raspberry.picoboot` — this module is just the
discovery glue.
"""

from . import adapter  # noqa: F401
