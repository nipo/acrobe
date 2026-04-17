from . import mpsse_cmd


class ActivityLed:
    """LED driven by an FTDI GPIO, pulsed high/low around MPSSE batches.

    Use as `FtdiJtagAdapter._led = ActivityLed(pin=13, active_low=False)`:
    the adapter includes the pin in its OE mask, initializes it to the
    off state, and configures MpsseEngine to prepend a "LED on" SetBits
    before each batch and append a "LED off" SetBits after it. The LED
    effectively blinks at the batch rate while the adapter is active.
    """

    def __init__(self, pin: int, active_low: bool = False):
        if not (0 <= pin <= 15):
            raise ValueError(f"LED pin must be 0-15, got {pin}")
        self.pin = pin
        self.active_low = active_low

    @property
    def is_high_byte(self) -> bool:
        return self.pin >= 8

    @property
    def byte_mask(self) -> int:
        return 1 << (self.pin & 7)

    @property
    def word_mask(self) -> int:
        return 1 << self.pin

    def off_bits(self, gpio_val: int) -> int:
        """Return gpio_val with the LED bit forced to the off state."""
        if self.active_low:
            return gpio_val | self.word_mask
        return gpio_val & ~self.word_mask

    def bracket_bytes(self, gpio_val: int, gpio_oe: int) -> tuple[bytes, bytes]:
        """Compute raw (on_cmd, off_cmd) MPSSE bytes from the full 16-bit state.

        Both commands preserve every non-LED bit in the LED's byte. The
        off command must match what was programmed at setup time to avoid
        perturbing the port when the batch completes.
        """
        if self.is_high_byte:
            val = (gpio_val >> 8) & 0xff
            oe = (gpio_oe >> 8) & 0xff
            cmd_byte = mpsse_cmd.SET_BITS_HIGH
        else:
            val = gpio_val & 0xff
            oe = gpio_oe & 0xff
            cmd_byte = mpsse_cmd.SET_BITS_LOW

        if self.active_low:
            on_val = val & ~self.byte_mask
            off_val = val | self.byte_mask
        else:
            on_val = val | self.byte_mask
            off_val = val & ~self.byte_mask

        return (
            bytes([cmd_byte, on_val, oe]),
            bytes([cmd_byte, off_val, oe]),
        )
