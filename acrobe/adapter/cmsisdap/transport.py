"""CMSIS-DAP HID transport.

CMSIS-DAP devices come in two flavours: HID-class (v1) and bulk
(v2). The LPC-LINK2 in this codebase exposes only HID, and on
macOS the OS owns HID interfaces — libusb can't claim them. We
therefore use cython-hidapi (the ``hid`` Python module) for
transport, wrapping the synchronous calls in
``asyncio.to_thread`` so the rest of acrobe stays async.

The HID device handle is opened by ``CMSIS-DAP usage page''
(0xFF00 — the convention from the CMSIS-DAP spec): walking
``hid.enumerate(vid, pid)`` and picking the entry whose
``usage_page`` is 0xFF00 gives us the right interface even on
composite adapters that also ship CDC + extra HID interfaces."""

from __future__ import annotations

import asyncio
import logging

import hid


# CMSIS-DAP convention — vendor-defined HID usage page.
DAP_USAGE_PAGE = 0xFF00


class CmsisDapTransport:
    """HID transport for CMSIS-DAP. One device, request/response.

    Holds an :class:`asyncio.Lock` so concurrent coroutines don't
    interleave commands. Each ``request`` writes one HID report
    and reads one report back."""

    def __init__(self, device: hid.device, packet_size: int,
                 logger: logging.Logger):
        self.__device = device
        self.__packet_size = packet_size
        self.__lock = asyncio.Lock()
        self.__logger = logger

    @classmethod
    def from_descriptor(cls, vid: int, pid: int, *,
                        serial: str | None = None,
                        logger: logging.Logger | None = None
                        ) -> "CmsisDapTransport":
        """Open the CMSIS-DAP HID interface on the given device.

        Selects the HID interface whose USB usage_page is
        :data:`DAP_USAGE_PAGE`. ``serial``, when provided, picks a
        specific adapter on systems with multiples plugged in."""
        if logger is None:
            logger = logging.getLogger("cmsisdap.transport")

        match = None
        for entry in hid.enumerate(vid, pid):
            if entry.get("usage_page") != DAP_USAGE_PAGE:
                continue
            if serial is not None and entry.get("serial_number") != serial:
                continue
            match = entry
            break
        if match is None:
            raise RuntimeError(
                f"CMSIS-DAP HID interface not found "
                f"(vid={vid:04x} pid={pid:04x} "
                f"serial={serial!r}, usage_page=0x{DAP_USAGE_PAGE:04x})")

        device = hid.device()
        device.open_path(match["path"])
        # Default packet size for CMSIS-DAP HID v1 is 64 bytes; the
        # device reports its actual size via DAP_Info(0xFF) which
        # the adapter reads at init.
        return cls(device, packet_size=64, logger=logger)

    @property
    def packet_size(self) -> int:
        return self.__packet_size

    @packet_size.setter
    def packet_size(self, value: int) -> None:
        self.__packet_size = value

    async def request(self, payload: bytes) -> bytes:
        """Issue one CMSIS-DAP request and return the response.

        ``payload`` is the command byte plus its arguments — this
        function prepends the HID report ID (0) and zero-pads to the
        packet size. The response strips the leading byte if it
        echoes the report ID and trims trailing zero padding back
        to a sensible length (the protocol's status/data sections
        are not length-prefixed; callers parse by command shape)."""
        if len(payload) > self.__packet_size:
            raise ValueError(
                f"CMSIS-DAP request {len(payload)} bytes exceeds "
                f"packet size {self.__packet_size}")
        # CMSIS-DAP HID uses report ID 0 (unnumbered reports). The
        # cython-hidapi `write()` call expects the first byte to be
        # the report ID, so we prepend a 0 and pad to packet_size.
        report = bytes([0x00]) + payload
        report += bytes(self.__packet_size + 1 - len(report))

        self.__logger.protocol(
            "DAP -> cmd=0x%02x args=%s",
            payload[0], payload[1:16].hex())

        async with self.__lock:
            await asyncio.to_thread(self.__device.write, report)
            resp = await asyncio.to_thread(
                self.__device.read, self.__packet_size, 1000)

        self.__logger.protocol(
            "DAP <- cmd=0x%02x rsp=%s",
            resp[0] if resp else 0xFF, bytes(resp[:16]).hex())
        return bytes(resp)

    async def close(self) -> None:
        await asyncio.to_thread(self.__device.close)
