"""Roundtrip and registration tests for the wire codec/registry.

Each test builds an isolated `Registry` and registers types directly
against it (rather than the module-level default), so tests don't
share state.
"""

from dataclasses import dataclass, field

import pytest

from acrobe.wire.codec import (
    CodecError,
    build_codec,
    from_bytes,
    to_bytes,
)
from acrobe.wire.registry import Registry, RegistryError


def _register_op(reg: Registry, cls, uuid: str):
    """Standalone helper mirroring @wire.op against an explicit registry."""
    return reg.register(cls, "op", uuid)


def _register_error(reg: Registry, cls, uuid: str):
    return reg.register(cls, "error", uuid)


# Roundtrip — primitive fields

def test_roundtrip_primitive_fields():
    reg = Registry()

    @dataclass
    class Echo:
        value: int
        label: str = "x"

    entry = _register_op(reg, Echo, "11111111-1111-1111-1111-111111111111")
    instance = Echo(value=42, label="hello")
    raw = to_bytes(instance, entry.codec)
    decoded = from_bytes(raw, entry.codec)
    assert decoded == instance


def test_roundtrip_optional_field():
    reg = Registry()

    @dataclass
    class Echo:
        value: int | None = None

    entry = _register_op(reg, Echo, "22222222-2222-2222-2222-222222222222")
    for v in (None, 0, 7):
        inst = Echo(value=v)
        assert from_bytes(to_bytes(inst, entry.codec), entry.codec) == inst


def test_roundtrip_list_and_dict():
    reg = Registry()

    @dataclass
    class Bag:
        ints: list[int] = field(default_factory=list)
        named: dict[str, int] = field(default_factory=dict)

    entry = _register_op(reg, Bag, "33333333-3333-3333-3333-333333333333")
    inst = Bag(ints=[1, 2, 3], named={"a": 10, "b": 20})
    assert from_bytes(to_bytes(inst, entry.codec), entry.codec) == inst


def test_roundtrip_homogeneous_tuple():
    reg = Registry()

    @dataclass
    class T:
        items: tuple[int, ...] = ()

    entry = _register_op(reg, T, "44444444-4444-4444-4444-444444444444")
    inst = T(items=(1, 2, 3))
    decoded = from_bytes(to_bytes(inst, entry.codec), entry.codec)
    assert decoded == inst
    assert isinstance(decoded.items, tuple)


def test_roundtrip_heterogeneous_tuple():
    reg = Registry()

    @dataclass
    class Point:
        xy: tuple[int, str]

    entry = _register_op(reg, Point, "55555555-5555-5555-5555-555555555555")
    inst = Point(xy=(7, "hello"))
    decoded = from_bytes(to_bytes(inst, entry.codec), entry.codec)
    assert decoded == inst
    assert isinstance(decoded.xy, tuple)


# Roundtrip — nested registered types

def test_roundtrip_nested_registered_type():
    reg = Registry()

    @dataclass
    class Inner:
        value: int

    @dataclass
    class Outer:
        inner: Inner
        tag: str = "tag"

    inner_entry = _register_op(reg, Inner, "66666666-6666-6666-6666-666666666666")
    outer_entry = _register_op(reg, Outer, "77777777-7777-7777-7777-777777777777")
    inst = Outer(inner=Inner(value=99), tag="outer")
    assert from_bytes(to_bytes(inst, outer_entry.codec), outer_entry.codec) == inst
    # The inner codec is independently usable too.
    assert from_bytes(
        to_bytes(inst.inner, inner_entry.codec),
        inner_entry.codec) == inst.inner


# Custom codec hook

def test_custom_codec_hook():
    reg = Registry()

    class FancyId:
        def __init__(self, value: int):
            self.value = value

        def __eq__(self, other):
            return isinstance(other, FancyId) and self.value == other.value

        @classmethod
        def __cbor_encode__(cls, instance):
            return instance.value

        @classmethod
        def __cbor_decode__(cls, data):
            return cls(int(data))

    entry = _register_op(reg, FancyId, "88888888-8888-8888-8888-888888888888")
    inst = FancyId(0xdeadbeef)
    assert from_bytes(to_bytes(inst, entry.codec), entry.codec) == inst


# Registration validation

def test_duplicate_uuid_raises():
    reg = Registry()

    @dataclass
    class A:
        v: int = 0

    @dataclass
    class B:
        v: int = 0

    _register_op(reg, A, "99999999-9999-9999-9999-999999999999")
    with pytest.raises(RegistryError, match="already registered"):
        _register_op(reg, B, "99999999-9999-9999-9999-999999999999")


def test_duplicate_class_raises():
    reg = Registry()

    @dataclass
    class A:
        v: int = 0

    _register_op(reg, A, "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    with pytest.raises(RegistryError, match="already registered"):
        _register_op(reg, A, "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


def test_invalid_uuid_raises():
    reg = Registry()

    @dataclass
    class A:
        v: int = 0

    with pytest.raises(RegistryError, match="invalid UUID"):
        _register_op(reg, A, "not-a-uuid")


def test_non_dataclass_without_custom_codec_raises():
    reg = Registry()

    class Plain:
        pass

    with pytest.raises(CodecError, match="not a dataclass"):
        _register_op(reg, Plain, "cccccccc-cccc-cccc-cccc-cccccccccccc")


def test_unsupported_field_type_raises():
    reg = Registry()

    @dataclass
    class A:
        v: object  # `object` is not a primitive nor registered

    with pytest.raises(CodecError, match="not codec-supported"):
        _register_op(reg, A, "dddddddd-dddd-dddd-dddd-dddddddddddd")


def test_node_uses_unregistered_raises():
    from acrobe.engine import Batcher
    from acrobe.node import Node

    reg = Registry()

    class Stranger:
        pass

    class Sample(Node, Batcher):
        def __init__(self):
            Node.__init__(self, "sample")
            Batcher.__init__(self)

    with pytest.raises(RegistryError, match="not a registered Transportable"):
        reg.register(
            Sample, "node", "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
            uses=[Stranger])


def test_node_uses_node_raises():
    from acrobe.engine import Batcher
    from acrobe.node import Node

    reg = Registry()

    class A(Node, Batcher):
        def __init__(self):
            Node.__init__(self, "a")
            Batcher.__init__(self)

    class B(Node, Batcher):
        def __init__(self):
            Node.__init__(self, "b")
            Batcher.__init__(self)

    reg.register(A, "node", "f1111111-1111-1111-1111-111111111111")
    with pytest.raises(RegistryError, match="is a node"):
        reg.register(
            B, "node", "f2222222-2222-2222-2222-222222222222", uses=[A])
