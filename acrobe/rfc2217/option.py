"""Com-Port-Control telnet option (code 44) — framing + role dispatch.

The subnegotiation parser is identical on client and server sides; only
the direction in which subcommands are interpreted differs. Role classes
implement handle_subcmd(code, payload) and receive already-decoded
subcommand code and payload bytes.
"""

from ..protocol.telnet import TelnetOption, TelnetPipe
from . import codes


class ComPortRole:
    """Common interface for client-side and server-side roles."""

    async def on_subcmd(self, telnet: TelnetPipe, code: int, payload: bytes):
        """Process a received subcommand (code + payload bytes after it)."""
        raise NotImplementedError

    async def on_ready(self, telnet: TelnetPipe):
        """Called after the peer accepted the option (DO/WILL exchanged)."""
        pass


class ComPortOption(TelnetOption):
    """Telnet option 44 handler.

    Wraps a ComPortRole. Handles WILL/DO/WONT/DONT negotiation and
    decodes SB into (code, payload) for the role.
    """

    code = codes.OPTION_COM_PORT_CONTROL
    name = "comport"

    def __init__(self, role: ComPortRole, initiator: bool):
        """initiator=True means we actively advertise the option at start
        (typically the client — and equally valid on the server which
        also advertises WILL for the RFC 2217 server role)."""
        self.role = role
        self.initiator = initiator
        self._peer_agreed = False

    async def start(self, telnet: TelnetPipe):
        if self.initiator:
            # Per RFC 2217: client sends WILL, server replies DO.
            # A server that supports it will likewise WILL-advertise.
            await telnet.send_iac(0xFB, self.code)  # IAC WILL <code>

    async def peer_do(self, telnet: TelnetPipe):
        self._peer_agreed = True
        if not self.initiator:
            # We hadn't advertised yet: acknowledge with WILL
            await telnet.send_iac(0xFB, self.code)
        await self.role.on_ready(telnet)

    async def peer_dont(self, telnet: TelnetPipe):
        self._peer_agreed = False

    async def peer_will(self, telnet: TelnetPipe):
        # Peer advertises they can speak the option: reply DO if we want it.
        self._peer_agreed = True
        await telnet.send_iac(0xFD, self.code)  # IAC DO <code>
        await self.role.on_ready(telnet)

    async def peer_wont(self, telnet: TelnetPipe):
        self._peer_agreed = False
        await telnet.send_iac(0xFE, self.code)  # IAC DONT <code>

    async def peer_sb(self, telnet: TelnetPipe, payload: bytes):
        if not payload:
            return
        subcmd = payload[0]
        await self.role.on_subcmd(telnet, subcmd, payload[1:])

    # Convenience: emit a subcommand SB
    async def send(self, telnet: TelnetPipe, subcmd: int, payload: bytes = b""):
        await telnet.send_sb(self.code, bytes([subcmd]) + payload)
