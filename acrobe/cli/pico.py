"""`acrobe pico` — host-side helpers for Raspberry Pi pico-sdk
devices.

The pico-sdk's stdio-USB template exposes a "reset" interface
(USB class 0xFF, subclass 0, protocol 1) that handles two
class-specific control requests:

    bRequest 0x01  RESET_REQUEST_BOOTSEL  — reboot into BOOTSEL
    bRequest 0x02  RESET_REQUEST_FLASH    — reboot into application

This convention is documented by pico-sdk and trivially reusable
by third-party firmware on custom VID/PID. The `acrobe pico`
group therefore takes an explicit ``VID:PID`` argument rather
than matching Raspberry Pi's VID implicitly.

These commands only work when the firmware *cooperates* — i.e.
was built with pico-sdk's ``pico_stdio_usb`` (or hand-rolled
equivalent). They aren't a substitute for the BOOTSEL button on
an unresponsive chip.
"""

import asyncclick as click
import ausb
from ausb.exception import TransferError
from ausb.handle import RequestTypeType, RequestTypeRecipient

from . import base


RESET_REQUEST_BOOTSEL = 0x01
RESET_REQUEST_FLASH = 0x02

# pico-sdk's reset interface descriptor convention.
PICO_RESET_CLASS = 0xFF
PICO_RESET_SUBCLASS = 0x00
PICO_RESET_PROTOCOL = 0x01


def parse_vidpid(arg: str) -> tuple[int, int]:
    """Parse ``"VID:PID"`` hex pair (no ``0x`` prefix, e.g.
    ``"2e8a:000a"``)."""
    parts = arg.split(":")
    if len(parts) != 2:
        raise click.BadParameter(
            f"expected VID:PID, got {arg!r}")
    try:
        vid = int(parts[0], 16)
        pid = int(parts[1], 16)
    except ValueError:
        raise click.BadParameter(
            f"VID:PID must be hex, got {arg!r}")
    if not (0 <= vid <= 0xFFFF) or not (0 <= pid <= 0xFFFF):
        raise click.BadParameter(
            f"VID and PID must each be 16-bit, got {arg!r}")
    return vid, pid


def find_reset_interface(device) -> int | None:
    """Return the index of the device's pico-sdk reset interface,
    or ``None`` if the device exposes no such interface.

    Match key: ``bInterfaceClass == 0xFF``, ``bInterfaceSubClass
    == 0``, ``bInterfaceProtocol == 1``. The subclass + protocol
    combination is what distinguishes the reset interface from
    other pico-sdk vendor interfaces (e.g. PICOBOOT itself uses
    protocol 0 on RP2040 in BOOTSEL mode).
    """
    cfg = device.descriptor[device.configuration]
    for i, interface in enumerate(cfg):
        for setting in interface:
            if (setting.classes == (PICO_RESET_CLASS,
                                    PICO_RESET_SUBCLASS)
                    and setting.protocol == PICO_RESET_PROTOCOL):
                return i
    return None


async def _send_reset(vid: int, pid: int, *,
                      serial: str | None,
                      request: int,
                      label: str) -> None:
    """Open the matching device, locate its reset interface, and
    send the class-specific control request. The device USB-
    disconnects in response; the consequent ``LIBUSB_ERROR_IO``
    on the control transfer's status phase is expected and
    swallowed."""
    ctx = ausb.Context(enable_hotplug=False)
    try:
        device, dev_serial = _open_match(ctx, vid, pid, serial)
        try:
            iface = find_reset_interface(device)
            if iface is None:
                raise click.ClickException(
                    f"device {vid:04x}:{pid:04x} doesn't expose a "
                    f"pico-sdk reset interface (class 0xff "
                    f"subclass 0 protocol 1)")
            try:
                device.handle.detachKernelDriver(iface)
            except Exception:
                pass
            device.handle.claimInterface(iface)
            try:
                try:
                    await device.control(
                        RequestTypeType.Class,
                        RequestTypeRecipient.Interface,
                        request, 0, iface, b"")
                except TransferError:
                    # Device disappears before the status phase
                    # completes — expected when the reboot kicks in.
                    pass
                identity = f"{vid:04x}:{pid:04x}"
                if dev_serial:
                    identity = f"{identity} ({dev_serial})"
                click.echo(f"{label}: {identity}")
            finally:
                try:
                    device.handle.releaseInterface(iface)
                except Exception:
                    pass
        finally:
            try:
                device.handle.close()
            except Exception:
                pass
    finally:
        ctx.close()


def _open_match(ctx, vid: int, pid: int, serial: str | None):
    """Find the (single) device matching ``vid:pid`` (and an
    optional ``serial`` substring), open it, and return
    ``(device, serial_string_or_None)``.

    Raises :class:`click.ClickException` for zero matches or
    ambiguity that ``--serial`` doesn't resolve."""
    descriptors = [
        d for d in ctx.device_filter()
        if d.vendor_id == vid and d.product_id == pid]
    if not descriptors:
        raise click.ClickException(
            f"no device {vid:04x}:{pid:04x} found")

    matches = []
    for desc in descriptors:
        try:
            dev = desc.open()
        except Exception as e:
            click.echo(
                f"warning: couldn't open {vid:04x}:{pid:04x} "
                f"on bus {desc.bus}, address {desc.address}: {e}",
                err=True)
            continue
        try:
            dev_serial = dev.serial
        except Exception:
            dev_serial = None
        if serial is not None:
            if not dev_serial or serial.lower() not in dev_serial.lower():
                dev.handle.close()
                continue
        matches.append((dev, dev_serial))

    if not matches:
        if serial is not None:
            raise click.ClickException(
                f"no device {vid:04x}:{pid:04x} with serial "
                f"matching {serial!r}")
        raise click.ClickException(
            f"could not open any device {vid:04x}:{pid:04x}")

    if len(matches) > 1:
        # Close all but report the ambiguity.
        for dev, _ in matches:
            dev.handle.close()
        serials = ", ".join(s or "<no-serial>" for _, s in matches)
        raise click.ClickException(
            f"{len(matches)} devices match {vid:04x}:{pid:04x} "
            f"(serials: {serials}); narrow with --serial")
    return matches[0]


@base.cli.group(help="Raspberry Pi pico-sdk device helpers")
async def pico():
    pass


@pico.command(help="Reset device into BOOTSEL mode via the "
                   "pico-sdk reset interface")
@click.argument("vidpid")
@click.option("--serial", "serial", default=None,
              help="Substring match against the device's USB "
                   "serial — required when multiple devices "
                   "share the same VID:PID")
async def bootsel(vidpid, serial):
    vid, pid = parse_vidpid(vidpid)
    await _send_reset(
        vid, pid, serial=serial,
        request=RESET_REQUEST_BOOTSEL,
        label="reset to BOOTSEL")


@pico.command(help="Reset device into application mode via the "
                   "pico-sdk reset interface")
@click.argument("vidpid")
@click.option("--serial", "serial", default=None,
              help="Substring match against the device's USB "
                   "serial — required when multiple devices "
                   "share the same VID:PID")
async def app(vidpid, serial):
    vid, pid = parse_vidpid(vidpid)
    await _send_reset(
        vid, pid, serial=serial,
        request=RESET_REQUEST_FLASH,
        label="reset to application")
