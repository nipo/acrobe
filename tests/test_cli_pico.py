"""Unit tests for `acrobe.cli.pico` — VID:PID parsing and reset-
interface lookup against synthetic descriptors.

Live device interaction is out of scope here (covered by manual
runs against a connected pico-sdk-based board).
"""

import asyncclick as click
import pytest

from acrobe.cli.pico import (
    parse_vidpid, find_reset_interface,
    PICO_RESET_CLASS, PICO_RESET_SUBCLASS, PICO_RESET_PROTOCOL,
)


class TestParseVidPid:
    def test_well_formed(self):
        assert parse_vidpid("2e8a:000a") == (0x2E8A, 0x000A)

    def test_uppercase(self):
        assert parse_vidpid("2E8A:000A") == (0x2E8A, 0x000A)

    def test_no_padding(self):
        assert parse_vidpid("1:2") == (1, 2)

    def test_missing_colon(self):
        with pytest.raises(click.BadParameter):
            parse_vidpid("2e8a")

    def test_too_many_colons(self):
        with pytest.raises(click.BadParameter):
            parse_vidpid("2e8a:000a:foo")

    def test_non_hex(self):
        with pytest.raises(click.BadParameter):
            parse_vidpid("zzz:000a")

    def test_out_of_range(self):
        with pytest.raises(click.BadParameter):
            parse_vidpid("10000:0001")


# -- find_reset_interface -------------------------------------------------


class _Setting:
    def __init__(self, cls, sub, proto):
        self.classes = (cls, sub)
        self.protocol = proto


class _Interface:
    def __init__(self, *settings):
        self._settings = settings

    def __iter__(self):
        return iter(self._settings)


class _Config:
    def __init__(self, *interfaces):
        self._interfaces = interfaces

    def __iter__(self):
        return iter(self._interfaces)


class _Descriptor:
    def __init__(self, *configs):
        self._configs = configs

    def __getitem__(self, idx):
        return self._configs[idx]


class _Device:
    def __init__(self, configuration: int, descriptor: _Descriptor):
        self.configuration = configuration
        self.descriptor = descriptor


def test_finds_pico_reset_interface():
    """The classic pico-sdk descriptor: MSC + CDC + reset (vendor-
    class subclass 0 protocol 1) — reset is at index 2."""
    dev = _Device(0, _Descriptor(_Config(
        _Interface(_Setting(0x08, 0x06, 0x50)),       # MSC
        _Interface(_Setting(0x02, 0x02, 0x00)),       # CDC ACM control
        _Interface(_Setting(
            PICO_RESET_CLASS,
            PICO_RESET_SUBCLASS,
            PICO_RESET_PROTOCOL)),                    # pico reset
    )))
    assert find_reset_interface(dev) == 2


def test_returns_none_without_reset_iface():
    """A device with only vendor-class subclass=0 protocol=0
    (e.g. PICOBOOT) doesn't match — the protocol byte
    differentiates."""
    dev = _Device(0, _Descriptor(_Config(
        _Interface(_Setting(0x08, 0x06, 0x50)),
        _Interface(_Setting(0xFF, 0x00, 0x00)),       # PICOBOOT-like
    )))
    assert find_reset_interface(dev) is None


def test_no_interfaces():
    dev = _Device(0, _Descriptor(_Config()))
    assert find_reset_interface(dev) is None


def test_alt_setting_match():
    """Reset can show up as an alt setting on an existing interface."""
    dev = _Device(0, _Descriptor(_Config(
        _Interface(
            _Setting(0xFF, 0x00, 0x00),                # alt 0: PICOBOOT
            _Setting(PICO_RESET_CLASS,
                     PICO_RESET_SUBCLASS,
                     PICO_RESET_PROTOCOL)),            # alt 1: reset
    )))
    assert find_reset_interface(dev) == 0
