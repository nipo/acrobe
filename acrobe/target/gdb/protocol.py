"""GDB Remote Serial Protocol responder.

`Responder` is the pure-protocol layer: it consumes packet
payloads, calls into a `Debuggable` (+ optional `Loadable`), and
returns response bytes. It does no I/O. `GdbServer` (server.py)
wraps it in an asyncio TCP session.

The Responder is **CPU-family agnostic**: it pulls
`gdb_feature_name`, `gdb_byteorder`, and the register layout off
the `Core` objects under the Debuggable; routes `vFlashErase/
Write/Done` into the Loadable; and exposes `qRcmd` to the
Debuggable's `monitor` hook.

For continue / step the Responder polls the current Core's
state() in a loop and races that against an optional `transport`
hook that resolves when the client sends a 0x03 interrupt byte.
The Session in `server.py` provides the transport — tests can
pass a stub.
"""

from __future__ import annotations

import asyncio
import binascii
from xml.etree import ElementTree as et

from ..debuggable import CoreState, HaltCause, RegisterType
from . import message


REGTYPE_MAP = {
    RegisterType.GPR:    ("int",         "general"),
    RegisterType.FLOAT:  ("float",       "float"),
    RegisterType.DOUBLE: ("ieee_double", "float"),
    RegisterType.PC:     ("code_ptr",    "general"),
    RegisterType.LR:     ("code_ptr",    "general"),
    RegisterType.SP:     ("data_ptr",    "general"),
    RegisterType.SYSTEM: ("int",         "general"),
}


STOP_REASON = {
    CoreState.HALT:   "S05",  # SIGTRAP
    CoreState.SLEEP:  "S05",
    CoreState.LOCKUP: "S0b",  # SIGSEGV
    CoreState.FAULT:  "S0b",
    CoreState.RUN:    None,   # caller shouldn't ask
    CoreState.UNKNOWN: "T00",
}


HALT_CAUSE_TAG = {
    HaltCause.BREAKPOINT:  "T05hwbreak:;",
    HaltCause.WATCHPOINT:  "T05watch:;",
    HaltCause.DEBUGGER:    "T05hwbreak:;",
    HaltCause.INSTRUCTION: "S04",  # SIGILL
    HaltCause.EXCEPTION:   "T05syscall_entry:;",
    HaltCause.UNKNOWN:     "T00",
}


class Responder:
    """Per-connection GDB RSP dispatcher.

    Each TCP session owns one Responder, bound to a shared
    Debuggable + optional Loadable. `handle_packet(payload)` is
    the entry point; it returns the response payload (without
    framing) or None to send no reply.
    """

    PACKET_SIZE = 4096
    RUN_POLL_INTERVAL = 0.05

    def __init__(self, debuggable, loadable=None, *, transport=None):
        self.debuggable = debuggable
        self.loadable = loadable
        # Thread IDs are 1-based per the GDB protocol; we map each
        # Core to its index + 1 so 0 / -1 (special "any" / "all")
        # don't collide.
        self.cores = list(debuggable.cores)
        if not self.cores:
            raise ValueError("Debuggable has no Core children")
        self.current_core = self.cores[0]
        self.packet_ack = True
        self.no_ack_mode_requested = False
        self.flash_image: list[tuple[int, bytes]] = []
        # `transport` exposes `next_interrupt_byte() -> awaitable` —
        # resolves when the client sends a 0x03 byte while we're
        # waiting for a continue/step to complete. None disables
        # interrupt support (handle_c then waits forever for the
        # core to halt on its own — fine for tests, wrong for live
        # use without breakpoints).
        self.transport = transport
        self.__target_xml = self.__build_target_xml()
        self.__memory_map_xml = self.__build_memory_map_xml()

    # -- Public entry point ----------------------------------------

    async def handle_packet(self, payload: bytes) -> bytes | None:
        """Process one decoded packet, return response bytes (no
        framing) or None to send no reply."""
        if not payload:
            return b""
        # Dispatch on the first byte. Each handler is named after
        # the GDB command letter; multi-letter commands (`q*`, `Q*`,
        # `v*`, `H`) get a sub-dispatch.
        first = chr(payload[0])
        handler_name = {
            "?": "handle_question",
            "!": "handle_extended_mode",
        }.get(first, f"handle_{first}")
        handler = getattr(self, handler_name, None)
        if handler is None:
            return b""
        return await handler(payload)

    async def handle_interrupt(self) -> bytes | None:
        """Ctrl-C (0x03) byte received outside a packet."""
        await self.current_core.halt()
        return await self.__stop_reason()

    # -- Handlers --------------------------------------------------

    async def handle_question(self, payload: bytes) -> bytes:
        return await self.__stop_reason()

    async def handle_extended_mode(self, payload: bytes) -> bytes:
        return message.ok()

    async def handle_q(self, payload: bytes) -> bytes | None:
        return await self.__q_dispatch(payload)

    async def handle_Q(self, payload: bytes) -> bytes | None:
        return await self.__q_dispatch(payload)

    async def handle_v(self, payload: bytes) -> bytes | None:
        return await self.__v_dispatch(payload)

    async def handle_H(self, payload: bytes) -> bytes:
        # H<op><tid> — op is 'c' (continue) or 'g' (other). We
        # honour the thread selection regardless of op.
        try:
            tid = int(payload[2:], 16)
        except ValueError:
            return message.error(1)
        if tid <= 0:
            self.current_core = self.cores[0]
        elif 1 <= tid <= len(self.cores):
            self.current_core = self.cores[tid - 1]
        else:
            return message.error(1)
        return message.ok()

    async def handle_g(self, payload: bytes) -> bytes:
        regs = await self.current_core.reg_read(self.current_core.registers)
        out = bytearray()
        byteorder = self.current_core.gdb_byteorder
        for r in self.current_core.registers:
            value = regs[r]
            out.extend(value.to_bytes(r.width // 8, byteorder=byteorder).hex().encode("ascii"))
        return bytes(out)

    async def handle_G(self, payload: bytes) -> bytes:
        raw = payload[1:]
        to_write = {}
        cursor = 0
        byteorder = self.current_core.gdb_byteorder
        for r in self.current_core.registers:
            hex_len = (r.width // 8) * 2
            chunk = raw[cursor:cursor + hex_len]
            cursor += hex_len
            if not chunk or chunk.lower() == b"x" * len(chunk):
                continue
            try:
                value = int.from_bytes(
                    binascii.a2b_hex(chunk), byteorder=byteorder)
            except (binascii.Error, ValueError):
                return message.error(1)
            to_write[r] = value
        if to_write:
            await self.current_core.reg_write(to_write)
        return message.ok()

    async def handle_p(self, payload: bytes) -> bytes:
        try:
            num = int(payload[1:], 16)
        except ValueError:
            return message.error(1)
        try:
            r = self.current_core.lookup_register(num)
        except (KeyError, AttributeError):
            return message.error(1)
        regs = await self.current_core.reg_read([r])
        value = regs[r]
        byteorder = self.current_core.gdb_byteorder
        return value.to_bytes(
            r.width // 8, byteorder=byteorder).hex().encode("ascii")

    async def handle_P(self, payload: bytes) -> bytes:
        try:
            eq = payload.index(b"=")
            num = int(payload[1:eq], 16)
            value_hex = payload[eq + 1:]
        except ValueError:
            return message.error(1)
        try:
            r = self.current_core.lookup_register(num)
        except (KeyError, AttributeError):
            return message.error(1)
        try:
            value = int.from_bytes(
                binascii.a2b_hex(value_hex),
                byteorder=self.current_core.gdb_byteorder)
        except (binascii.Error, ValueError):
            return message.error(1)
        await self.current_core.reg_write({r: value})
        return message.ok()

    async def handle_m(self, payload: bytes) -> bytes:
        try:
            comma = payload.index(b",")
            addr = int(payload[1:comma], 16)
            size = int(payload[comma + 1:], 16)
        except ValueError:
            return message.error(1)
        if size == 0:
            return b""
        data = await self.debuggable.mem_read(addr, size)
        return data.hex().encode("ascii")

    async def handle_M(self, payload: bytes) -> bytes:
        try:
            comma = payload.index(b",")
            colon = payload.index(b":")
            addr = int(payload[1:comma], 16)
            _size = int(payload[comma + 1:colon], 16)
            data = binascii.a2b_hex(payload[colon + 1:])
        except (ValueError, binascii.Error):
            return message.error(1)
        await self.debuggable.mem_write(addr, data)
        return message.ok()

    async def handle_X(self, payload: bytes) -> bytes:
        # X is binary-encoded memory write — bytes are escaped at
        # the packet layer; the payload after `:` is already raw.
        try:
            comma = payload.index(b",")
            colon = payload.index(b":")
            addr = int(payload[1:comma], 16)
            _size = int(payload[comma + 1:colon], 16)
        except ValueError:
            return message.error(1)
        data = bytes(payload[colon + 1:])
        if data:
            await self.debuggable.mem_write(addr, data)
        return message.ok()

    async def handle_c(self, payload: bytes) -> bytes:
        await self.current_core.resume()
        return await self.__wait_for_halt()

    async def handle_s(self, payload: bytes) -> bytes:
        await self.current_core.step()
        return await self.__wait_for_halt()

    async def __wait_for_halt(self) -> bytes:
        """Block until the core leaves RUN or the client sends an
        interrupt byte. On interrupt, halt the core. Returns the
        stop-reason packet."""
        poll = asyncio.create_task(self.__poll_until_not_running())
        waiters = {poll}
        interrupt = None
        if self.transport is not None:
            interrupt = asyncio.create_task(
                self.transport.next_interrupt_byte())
            waiters.add(interrupt)
        try:
            done, _ = await asyncio.wait(
                waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in waiters:
                if not t.done():
                    t.cancel()
        if interrupt is not None and interrupt in done:
            await self.current_core.halt()
        return await self.__stop_reason()

    async def __poll_until_not_running(self):
        while True:
            try:
                state = await self.current_core.state()
            except NotImplementedError:
                return
            if state != CoreState.RUN:
                return
            await asyncio.sleep(self.RUN_POLL_INTERVAL)

    async def handle_R(self, payload: bytes) -> bytes | None:
        await self.current_core.reset(stop=True)
        return None

    async def handle_Z(self, payload: bytes) -> bytes:
        # Z<type>,<addr>,<kind>
        return await self.__breakpoint(payload, add=True)

    async def handle_z(self, payload: bytes) -> bytes:
        return await self.__breakpoint(payload, add=False)

    async def handle_D(self, payload: bytes) -> bytes:
        await self.debuggable.detach()
        return message.ok()

    async def handle_k(self, payload: bytes) -> bytes | None:
        await self.debuggable.detach()
        return None

    # -- q / Q dispatch --------------------------------------------

    async def __q_dispatch(self, payload: bytes) -> bytes | None:
        text = payload.decode("ascii", errors="replace")
        # Splittable subcommands: "qXxxx:args" or "qXxxx,args" or "qXxxx".
        for sep in (":", ","):
            if sep in text:
                head, args = text.split(sep, 1)
                break
        else:
            head, args = text, ""
        handler = getattr(self, f"_q_{head}", None)
        if handler is None:
            return b""
        return await handler(args)

    async def _q_qSupported(self, args: str) -> bytes:
        features = [
            "PacketSize=%x" % self.PACKET_SIZE,
            "QStartNoAckMode+",
            "qXfer:features:read+",
            "hwbreak+",
        ]
        if self.__memory_map_xml is not None:
            features.append("qXfer:memory-map:read+")
        return ";".join(features).encode("ascii")

    async def _q_QStartNoAckMode(self, args: str) -> bytes:
        # The reply is acked normally; we switch to no-ack *after*
        # sending the OK. The Session handles this.
        self.no_ack_mode_requested = True
        return message.ok()

    async def _q_qXfer(self, args: str) -> bytes:
        # Format: <object>:read:<annex>:<offset>,<length>
        parts = args.split(":")
        if len(parts) < 4 or parts[1] != "read":
            return message.error(0)
        try:
            offset_str, length_str = parts[3].split(",")
            offset = int(offset_str, 16)
            length = int(length_str, 16)
        except ValueError:
            return message.error(0)
        if parts[0] == "features" and parts[2] == "target.xml":
            data = self.__target_xml
        elif parts[0] == "memory-map":
            data = self.__memory_map_xml or b""
        else:
            return b""
        if offset >= len(data):
            return b"l"
        chunk = data[offset:offset + length]
        prefix = b"l" if offset + len(chunk) >= len(data) else b"m"
        return prefix + chunk

    async def _q_qfThreadInfo(self, args: str) -> bytes:
        ids = ",".join(f"{i + 1:x}" for i in range(len(self.cores)))
        return ("m" + ids).encode("ascii")

    async def _q_qsThreadInfo(self, args: str) -> bytes:
        return b"l"

    async def _q_qC(self, args: str) -> bytes:
        tid = self.cores.index(self.current_core) + 1
        return f"QC{tid:x}".encode("ascii")

    async def _q_qAttached(self, args: str) -> bytes:
        return b"1"

    async def _q_qRcmd(self, args: str) -> bytes:
        try:
            cmd = binascii.a2b_hex(args).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return message.error(1)
        parts = cmd.strip().split()
        if not parts:
            return message.hex_encoded("Usage: monitor <command>\n")
        head, *tail = parts
        try:
            result = await self.debuggable.monitor(head, tail)
        except NotImplementedError:
            return message.hex_encoded(f"Unknown monitor command: {head}\n")
        if isinstance(result, str):
            return message.hex_encoded(result)
        return message.ok()

    # -- v dispatch ------------------------------------------------

    async def __v_dispatch(self, payload: bytes) -> bytes | None:
        # vFlashErase:<addr>,<length>
        # vFlashWrite:<addr>:<binary data>
        # vFlashDone
        if payload.startswith(b"vFlashErase:"):
            return await self.__v_flash_erase(payload[len(b"vFlashErase:"):])
        if payload.startswith(b"vFlashWrite:"):
            return await self.__v_flash_write(payload[len(b"vFlashWrite:"):])
        if payload == b"vFlashDone":
            return await self.__v_flash_done()
        if payload.startswith(b"vCont"):
            return await self.__v_cont(payload)
        if payload.startswith(b"vMustReplyEmpty"):
            return b""
        return b""

    async def __v_flash_erase(self, args: bytes) -> bytes:
        if self.loadable is None:
            return message.error(1)
        # GDB asks us to erase before writing; we collect writes until
        # vFlashDone, then call Loadable.write with do_erase=True so
        # the regions handle the erase consistently.
        self.flash_image = []
        return message.ok()

    async def __v_flash_write(self, args: bytes) -> bytes:
        if self.loadable is None:
            return message.error(1)
        try:
            colon = args.index(b":")
        except ValueError:
            return message.error(1)
        addr = int(args[:colon], 16)
        data = bytes(args[colon + 1:])
        self.flash_image.append((addr, data))
        return message.ok()

    async def __v_flash_done(self) -> bytes:
        if self.loadable is None:
            return message.error(1)
        from ...memory_map import MemoryMap
        m = MemoryMap()
        for addr, data in self.flash_image:
            m.append(addr, data)
        self.flash_image = []
        await self.loadable.write(m, do_erase=True)
        return message.ok()

    async def __v_cont(self, payload: bytes) -> bytes | None:
        # vCont? — query supported actions.
        if payload == b"vCont?":
            return b"vCont;c;s"
        # vCont;<action>[:<thread>]...
        spec = payload[len(b"vCont;"):]
        # Pick the first action that applies to the current thread,
        # or to all threads.
        action = None
        for entry in spec.split(b";"):
            kind = entry[0:1]
            if b":" in entry:
                kind = entry.split(b":")[0]
            action = kind
            break
        if action == b"c":
            return await self.handle_c(b"c")
        if action == b"s":
            return await self.handle_s(b"s")
        return b""

    # -- Helpers ---------------------------------------------------

    async def __stop_reason(self) -> bytes:
        try:
            state = await self.current_core.state()
        except NotImplementedError:
            return b"T00"
        if state == CoreState.RUN:
            # Reached when the caller's wait_for_halt exit raced the
            # core's actual S_HALT settle. Never return an empty
            # packet — GDB rejects that as "Invalid remote reply".
            # T05 with no extra info is "stopped, reason unknown".
            return b"T05"
        try:
            cause = await self.current_core.halt_cause()
        except NotImplementedError:
            return b"T00"
        tag = HALT_CAUSE_TAG.get(cause, STOP_REASON.get(state, "T00"))
        return tag.encode("ascii")

    async def __breakpoint(self, payload: bytes, *, add: bool) -> bytes:
        # Z<type>,<addr>,<kind>
        # type 0 = SW breakpoint, 1 = HW breakpoint (both → FPB)
        # type 2 = write WP, 3 = read WP, 4 = access WP (→ DWT);
        # kind on watchpoints is the watched span in bytes.
        rest = payload[1:].decode("ascii", errors="replace")
        try:
            type_str, addr_str, kind_str = rest.split(",")
            bp_type = int(type_str, 16)
            addr = int(addr_str, 16)
            kind = int(kind_str, 16)
        except ValueError:
            return message.error(1)
        try:
            if bp_type in (0, 1):
                if add:
                    await self.current_core.breakpoint_add(addr, kind)
                else:
                    for bp in await self.current_core.breakpoint_list():
                        if bp[1] == addr:
                            await self.current_core.breakpoint_remove(bp)
                            break
            elif bp_type in (2, 3, 4):
                if add:
                    await self.current_core.watchpoint_add(
                        addr, size=kind, kind=bp_type)
                else:
                    await self.current_core.watchpoint_remove(
                        (bp_type, addr, kind))
            else:
                return b""
        except (NotImplementedError, RuntimeError, KeyError, ValueError):
            return message.error(1)
        return message.ok()

    def __build_target_xml(self) -> bytes:
        target = et.Element("target")
        # Group registers by feature name. Cortex-M cores all share
        # one feature; AMP / heterogeneous Debuggables may have more.
        by_feature: dict[str, list] = {}
        for core in self.cores:
            feat = core.gdb_feature_name or "org.gnu.gdb.generic"
            by_feature.setdefault(feat, [])
            for r in core.registers:
                if not getattr(r, "gdb_visible", True):
                    continue
                if r not in by_feature[feat]:
                    by_feature[feat].append(r)
        for feat_name, regs in by_feature.items():
            feature_el = et.SubElement(target, "feature", name=feat_name)
            for r in regs:
                t, g = REGTYPE_MAP.get(r.datatype, ("int", "general"))
                et.SubElement(
                    feature_el, "reg",
                    name=r.name,
                    bitsize=str(r.width),
                    regnum=str(r.number),
                    type=t,
                    group=g,
                )
        return (b'<?xml version="1.0"?>'
                b'<!DOCTYPE target SYSTEM "gdb-target.dtd">'
                + et.tostring(target))

    def __build_memory_map_xml(self) -> bytes | None:
        from ..region import Flash, Ram
        regions = list(self.debuggable.memory_map) if self.debuggable.memory_map else []
        # Also pick up Loadable regions when present — most useful
        # for vFlashErase/Write routing.
        if self.loadable is not None:
            regions = regions + list(self.loadable.regions)
        if not regions:
            return None
        mm = et.Element("memory-map")
        for r in sorted(regions, key=lambda r: r.address):
            if isinstance(r, Flash):
                el = et.SubElement(
                    mm, "memory", type="flash",
                    start="0x%x" % r.address,
                    length="0x%x" % r.size)
                et.SubElement(el, "property",
                              name="blocksize").text = "0x%x" % r.write_page_size
            elif isinstance(r, Ram):
                et.SubElement(
                    mm, "memory", type="ram",
                    start="0x%x" % r.address,
                    length="0x%x" % r.size)
        return (b'<?xml version="1.0"?>'
                b'<!DOCTYPE memory-map PUBLIC '
                b'"+//IDN gnu.org//DTD GDB Memory Map V1.0//EN" '
                b'"http://sourceware.org/gdb/gdb-memory-map.dtd">'
                + et.tostring(mm))
