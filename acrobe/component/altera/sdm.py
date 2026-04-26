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
from ...node import Node

class SdmErrorCode(enum.IntEnum):
    OK = 0	
    INVALID_COMMAND = 1
    UNKNOWN_COMMAND = 3
    INVALID_COMMAND_PARAMETERS = 4
    COMMAND_INVALID_ON_SOURCE = 6
    CLIENT_ID_NO_MATCH = 8
    INVALID_ADDRESS = 9
    AUTHENTICATION_FAIL = 0xa
    TIMEOUT = 0xb
    HW_NOT_READY = 0xc
    HW_ERROR = 0xd
    QSPI_HW_ERROR = 0x80
    QSPI_ALREADY_OPEN = 0x81
    EFUSE_SYSTEM_FAILURE = 0x82
    COMMAND_SPECIFIC_ERROR_3 = 0x83
    COMMAND_SPECIFIC_ERROR_4 = 0x84
    COMMAND_SPECIFIC_ERROR_5 = 0x85
    COMMAND_SPECIFIC_ERROR_6 = 0x86
    COMMAND_SPECIFIC_ERROR_7 = 0x87
    COMMAND_SPECIFIC_ERROR_8 = 0x88
    COMMAND_SPECIFIC_ERROR_9 = 0x89
    COMMAND_SPECIFIC_ERROR_A = 0x8A
    COMMAND_SPECIFIC_ERROR_B = 0x8B
    COMMAND_SPECIFIC_ERROR_C = 0x8C
    COMMAND_SPECIFIC_ERROR_D = 0x8D
    COMMAND_SPECIFIC_ERROR_E = 0x8E
    QSPI_OWNED_BY_SDM_IN_USER_MODE = 0x8F
    NOT_CONFIGURED = 0x100
    ALT_SDM_MBOX_RESP_DEVICE_BUSY = 0x1FF
    ALT_SDM_MBOX_RESP_NO_VALID_RESP_AVAILABLE = 0x2FF
    ALT_SDM_MBOX_RESP_ERROR = 0x3FF

class SdmError(Exception):
    """SDM returned a non-zero error code."""

    def __init__(self, error_code, opcode=None):
        try:
            error_code = SdmErrorCode(error_code)
        except:
            pass
        self.error_code = error_code
        self.opcode = opcode
        op = f" for opcode {opcode:#05x}" if opcode is not None else ""
        super().__init__(f"SDM error {error_code}{op}")


class SdmFramingError(Exception):
    """SDM response framing mismatch."""


class SdmTimeoutError(Exception):
    """No response from SDM."""


class Sdm(Node):
    """Base for SDM command/response transport.

    Subclasses implement do_io() for the physical transport.
    This class provides the command serialization and response parsing.
    """

    def __init__(self):
        super().__init__("sdm")
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

    async def nop(self) -> None:
        raise NotImplementedError

    async def sync(self, nonce=None):
        """Perform SDM sync handshake.

        Sends SYNC command (opcode 1) with an arbitrary nonce.
        SDM echoes the nonce back to prove it's alive.

        Returns the echoed nonce.
        """
        raise NotImplementedError

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
