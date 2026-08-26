"""Phase-1 smoke target: a synthetic EchoNode exercising the
@wire decorators end-to-end.

This file double-serves as the executable example of the wire IDL
surface and as the integration test for `dump_idl`.

It uses the module-level default registry (via the @wire.* decorators),
so it asserts on what `wire.dump_idl()` emits for these registrations.
The dump only mentions classes with the `_TestEcho` prefix to avoid
fragility if other test modules register types.
"""

from dataclasses import dataclass

from acrobe import wire
from acrobe.engine import Batcher
from acrobe.node import Node


@wire.op("01000000-0000-4000-8000-000000000001")
@dataclass
class _TestEchoSay:
    text: str
    times: int = 1


@wire.op("01000000-0000-4000-8000-000000000002")
@dataclass
class _TestEchoNested:
    payload: _TestEchoSay
    flag: bool = False


@wire.error("01000000-0000-4000-8000-0000000000FE")
@dataclass
class _TestEchoTooLoud(Exception):
    measured_db: float

    def __post_init__(self):
        super().__init__(f"too loud: {self.measured_db} dB")


@wire.node("01000000-0000-4000-8000-0000000000FF",
           uses=[_TestEchoSay, _TestEchoNested, _TestEchoTooLoud])
class _TestEchoNode(Node, Batcher):
    def __init__(self):
        Node.__init__(self, "echo")
        Batcher.__init__(self)

    async def flush_ops(self, batch):
        # Phase 1 doesn't exercise execution; the node exists for
        # registry/IDL purposes only.
        for _, fut in batch:
            if not fut.done():
                fut.set_result(None)


def test_echo_node_registered():
    reg = wire.default_registry()
    entry = reg.lookup_by_class(_TestEchoNode)
    assert entry.kind == "node"
    used_classes = {reg.lookup_by_uuid(u).cls for u in entry.uses}
    assert used_classes == {_TestEchoSay, _TestEchoNested, _TestEchoTooLoud}


def test_echo_op_roundtrip():
    reg = wire.default_registry()
    entry = reg.lookup_by_class(_TestEchoSay)
    inst = _TestEchoSay(text="hello", times=3)
    raw = wire.to_bytes(inst, entry.codec)
    decoded = wire.from_bytes(raw, entry.codec)
    assert decoded == inst


def test_echo_nested_op_roundtrip():
    reg = wire.default_registry()
    entry = reg.lookup_by_class(_TestEchoNested)
    inst = _TestEchoNested(
        payload=_TestEchoSay(text="hi", times=2),
        flag=True)
    decoded = wire.from_bytes(wire.to_bytes(inst, entry.codec), entry.codec)
    assert decoded == inst


def test_echo_error_roundtrip():
    reg = wire.default_registry()
    entry = reg.lookup_by_class(_TestEchoTooLoud)
    inst = _TestEchoTooLoud(measured_db=120.5)
    decoded = wire.from_bytes(wire.to_bytes(inst, entry.codec), entry.codec)
    assert decoded.measured_db == 120.5
    assert isinstance(decoded, Exception)


def test_dump_idl_mentions_echo_node():
    text = wire.dump_idl()
    assert "_TestEchoNode" in text
    assert "_TestEchoSay" in text
    assert "_TestEchoNested" in text
    assert "_TestEchoTooLoud" in text
    # Sanity: the section header for the node is present.
    assert "node _TestEchoNode" in text
