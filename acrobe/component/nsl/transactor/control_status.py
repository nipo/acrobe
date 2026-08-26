"""NSL control/status transactor batch codec.

Encodes register read/write operations against an NSL
``control_status`` peripheral as a stream of command bytes and decodes
the response back into op results. Pure codec: no transport, no
Batcher. The caller owns the transport (typically a routed datagram
endpoint that targets the control_status peripheral) and wires the
codec to it.

Wire protocol (matches RTL ``nsl_bnoc.control_status``):

* WRITE: ``0x00 | reg``, followed by 4 little-endian data bytes.
  One status byte in response.
* READ:  ``0x80 | reg``. Five bytes in response: one status, then
  four little-endian data bytes.

The status byte is currently informational; the codec resolves the
future with the natural value (``int`` for reads, ``None`` for
writes) regardless of its content.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RegRead:
    """Read a 32-bit register. Future resolves to ``int``."""

    reg: int


@dataclass(frozen=True, slots=True)
class RegWrite:
    """Write a 32-bit register. Future resolves to ``None``."""

    reg: int
    value: int


class ControlStatusTransactor:
    """Batch codec for the NSL control_status command stream."""

    CMD_WRITE = 0x00
    CMD_READ  = 0x80

    @dataclass(frozen=True, slots=True)
    class __Gather:
        op_idx: int
        rsp_offset: int           # offset of the 4 data bytes (status precedes)
        is_read: bool

    def encode(self, batch) -> tuple[bytes, int, list]:
        cmd = bytearray()
        rsp_size = 0
        gather: list = []

        for op_idx, (op, _future) in enumerate(batch):
            if isinstance(op, RegRead):
                cmd.append(self.CMD_READ | (op.reg & 0x7f))
                gather.append(self.__Gather(op_idx, rsp_size + 1, True))
                rsp_size += 5
                continue

            if isinstance(op, RegWrite):
                cmd.append(self.CMD_WRITE | (op.reg & 0x7f))
                cmd.extend(int(op.value & 0xffffffff).to_bytes(4, "little"))
                gather.append(self.__Gather(op_idx, rsp_size, False))
                rsp_size += 1
                continue

            raise TypeError(
                f"ControlStatusTransactor cannot encode {type(op).__name__}")

        return bytes(cmd), rsp_size, gather

    def decode(self, batch, response: bytes, gather) -> None:
        results: dict[int, object] = {}
        for entry in gather:
            if entry.is_read:
                value = int.from_bytes(
                    response[entry.rsp_offset:entry.rsp_offset + 4], "little")
                results[entry.op_idx] = value
            else:
                results[entry.op_idx] = None

        for op_idx, (_op, future) in enumerate(batch):
            if future is None or future.done():
                continue
            future.set_result(results.get(op_idx))
