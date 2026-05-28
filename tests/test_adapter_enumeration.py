"""HwRoot enumeration model: enumerators populate unopened adapter
nodes, child_hints replaces supported_interfaces, singleton access."""

import pytest

import acrobe.adapter  # noqa: F401 — fires adapter registrations
from acrobe.adapter.ftdi.generic import GenericFtdiAdapter
from acrobe.adapter.model import (
    Adapter, Enumerator, HwRoot, get_hw_root, reset_hw_root_for_tests,
)


class _FakeAdapter(Adapter):
    """Unopened adapter whose only knowledge is its declared hints."""

    def __init__(self, name, hints):
        super().__init__(name)
        self.__hints = hints
        self.opened = False

    def child_hints(self):
        return list(self.__hints)


class _FakeListingEnumerator(Enumerator):
    def __init__(self, devices):
        self.__devices = devices

    async def populate(self, hw_root):
        for name, hints in self.__devices:
            if not hw_root.has_child(name):
                hw_root.child_add(_FakeAdapter(name, hints))


async def test_start_attaches_unopened_adapters():
    root = HwRoot()
    root.add_enumerator(_FakeListingEnumerator(
        [("probe-a", ["jtag"]), ("probe-b", ["swd", "jtag"])]))
    await root.ensure_started()

    names = sorted(c.name for c in root.children)
    assert names == ["probe-a", "probe-b"]
    # Adapters are started (so hints are ready) but never opened.
    for adapter in root.children:
        assert adapter.started
        assert not adapter.opened
    assert root.child_lookup("probe-b").child_hints() == ["swd", "jtag"]


async def test_populate_is_idempotent_on_restart():
    enumerator = _FakeListingEnumerator([("probe-a", ["jtag"])])
    root = HwRoot()
    root.add_enumerator(enumerator)
    await root.ensure_started()
    # A second populate (rescan) must not duplicate the child.
    await enumerator.populate(root)
    assert [c.name for c in root.children] == ["probe-a"]


async def test_prefix_named_adapters_both_attach():
    # `jlink-ob-123` must not be skipped just because `jlink-ob-1234`
    # is already present (substring vs exact-name dedup).
    root = HwRoot()
    root.add_enumerator(_FakeListingEnumerator(
        [("jlink-ob-1234", ["jtag"]), ("jlink-ob-123", ["jtag"])]))
    await root.ensure_started()
    assert sorted(c.name for c in root.children) == \
        ["jlink-ob-123", "jlink-ob-1234"]


async def test_generic_ftdi_child_hints_lists_boards():
    adapter = GenericFtdiAdapter("ftdi-test")
    assert "icepizero" in adapter.child_hints()


async def test_adapter_ident_default_empty():
    assert GenericFtdiAdapter("ftdi-test").ident == ""


def test_get_hw_root_singleton():
    reset_hw_root_for_tests()
    try:
        first = get_hw_root()
        assert get_hw_root() is first
        reset_hw_root_for_tests()
        assert get_hw_root() is not first
    finally:
        reset_hw_root_for_tests()
