"""SDM (Secure Device Manager) command/response protocol.

Transport-agnostic SDM command layer. Subclasses implement do_io()
for the specific physical transport (JTAG, FIFO, etc.).

SDM command word format (32-bit):
    [31:28]  Upper nibble (0 normally, 0xF for SYNC)
    [27:24]  ID tag (incremented per command, echoed in response)
    [23]     0
    [22:12]  Length (argument word count)
    [11]     0
    [10:0]   Command opcode

SDM response header (32-bit):
    [27:24]  ID tag (echoed from command)
    [22:12]  Length (data word count)
    [10:0]   Error code (0 = OK)
"""

import enum


class SdmError(Exception):
    """SDM returned a non-zero error code."""

    def __init__(self, error_code, opcode=None):
        self.error_code = error_code
        self.opcode = opcode
        op = f" for opcode {opcode:#05x}" if opcode is not None else ""
        super().__init__(f"SDM error {error_code:#05x}{op}")


class SdmFramingError(Exception):
    """SDM response framing mismatch."""


class SdmTimeoutError(Exception):
    """No response from SDM."""


class Sdm:
    """ABC for SDM command/response transport.

    Subclasses implement do_io() for the physical transport.
    This class provides the command serialization and response parsing.
    """

    def __init__(self):
        self._id = 0

    async def do_io(self, cmd: list[int]) -> list[int]:
        """Send a command frame and receive a response frame.

        Args:
            cmd: list of 32-bit words (header + arguments)

        Returns:
            list of 32-bit words (header + data)

        Must be implemented by transport subclass.
        """
        raise NotImplementedError

    async def sync(self, nonce=0xDEADBEEF):
        """Perform SDM sync handshake.

        Sends SYNC command (opcode 1) with an arbitrary nonce.
        SDM echoes the nonce back to prove it's alive.

        Returns the echoed nonce.
        """
        cid = self._id & 0xF
        self._id += 1

        header = 0x001 | (1 << 12) | (cid << 24) | (0xF << 28)
        rsp = await self.do_io([header, nonce])

        if not rsp:
            raise SdmTimeoutError("SDM sync: no response")

        rsp_error = rsp[0] & 0x7FF
        if rsp_error:
            raise SdmError(rsp_error, opcode=1)

        if len(rsp) < 2 or rsp[1] != nonce:
            got = rsp[1] if len(rsp) >= 2 else None
            raise SdmFramingError(
                f"SDM sync: nonce mismatch "
                f"(sent {nonce:#010x}, got {got!r})")

        return rsp[1]

    async def command(self, opcode: int, argument: bytes = b'') -> bytes:
        """Send an SDM command with arbitrary arguments.

        Args:
            opcode: SDM command opcode (11 bits)
            argument: command arguments as bytes (padded to word boundary)

        Returns:
            Response data as bytes (excluding header).

        Raises:
            SdmError: if SDM returns non-zero error code
            SdmFramingError: if response length doesn't match header
            SdmTimeoutError: if no response received
        """
        # Pack arguments as 32-bit words
        arg_words = []
        for off in range(0, len(argument), 4):
            chunk = argument[off:off + 4].ljust(4, b'\x00')
            arg_words.append(int.from_bytes(chunk, 'little'))

        cid = self._id & 0xF
        self._id += 1

        header = (opcode & 0x7FF) \
            | ((len(arg_words) & 0x7FF) << 12) \
            | ((cid & 0xF) << 24)

        rsp = await self.do_io([header] + arg_words)

        if not rsp:
            raise SdmTimeoutError(f"No response for opcode {opcode:#05x}")

        # Parse response header
        rsp_header = rsp[0]
        rsp_error = rsp_header & 0x7FF
        rsp_length = (rsp_header >> 12) & 0x7FF
        rsp_id = (rsp_header >> 24) & 0xF

        if rsp_error:
            raise SdmError(rsp_error, opcode=opcode)

        if len(rsp) != rsp_length + 1:
            raise SdmFramingError(
                f"Response length mismatch: header says {rsp_length} "
                f"data words, got {len(rsp) - 1}")

        # Convert data words to bytes
        return b''.join(w.to_bytes(4, 'little') for w in rsp[1:])
