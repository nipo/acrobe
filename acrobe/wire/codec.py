"""CBOR codec for Transportable classes.

Two registration paths:

1. **Custom codec.** A class providing `__cbor_encode__` / `__cbor_decode__`
   classmethods drives its own encoding. Used when introspection isn't
   appropriate (computed fields, externally-imposed shape, BitString-
   style domain types).

2. **Dataclass introspection.** A `@dataclass` whose every field has a
   supported type annotation gets a generated codec that emits a CBOR
   array of the field values in declaration order.

Supported field types (introspection path):

* primitives: `int`, `str`, `bytes`, `bool`, `float`, `NoneType`
* `Optional[X]` (i.e. `X | None`)
* `list[X]`
* `dict[K, V]` with primitive K
* `tuple[X, ...]` (homogeneous) and `tuple[A, B, C]` (heterogeneous fixed)
* any other registered Transportable class

Untyped, `Any`, or unregistered class fields are rejected at
registration time. Decisions are made once at class-decoration time
and cached in a `FieldSchema`; encode/decode uses the cached form.
"""

import dataclasses
import types
import typing
from typing import Any

import cbor2


_PRIMITIVES = (int, str, bytes, bool, float, type(None))


class CodecError(Exception):
    """Raised when a class can't be given a codec, or when encode/decode
    encounters data that doesn't match the schema. Always indicates a
    structural bug — not a wire-protocol-level error."""


class _Codec:
    """A codec attached to a registered class. Subclasses pick a strategy."""

    cls: type

    def encode(self, instance) -> Any:
        """Return a cbor2-encodable Python object representing `instance`."""
        raise NotImplementedError

    def decode(self, data: Any):
        """Reverse of encode: rebuild an instance from cbor2-decoded data."""
        raise NotImplementedError


class _CustomCodec(_Codec):
    """Codec backed by user-provided classmethods."""

    def __init__(self, cls: type):
        self.cls = cls

    def encode(self, instance) -> Any:
        return self.cls.__cbor_encode__(instance)

    def decode(self, data: Any):
        return self.cls.__cbor_decode__(data)


class _DataclassCodec(_Codec):
    """Codec generated from dataclass field annotations."""

    def __init__(self, cls: type, fields: list["_FieldSchema"]):
        self.cls = cls
        self.fields = fields

    def encode(self, instance) -> Any:
        return [f.encode(getattr(instance, f.name)) for f in self.fields]

    def decode(self, data: Any):
        if not isinstance(data, list) or len(data) != len(self.fields):
            raise CodecError(
                f"{self.cls.__name__}: expected {len(self.fields)}-element "
                f"array, got {data!r}")
        kwargs = {f.name: f.decode(v) for f, v in zip(self.fields, data)}
        return self.cls(**kwargs)


class _FieldSchema:
    """Per-field codec inside a _DataclassCodec.

    Holds the field name and a thunk that knows how to encode and
    decode a single value of that field's declared type.
    """

    def __init__(self, name: str, encode_fn, decode_fn):
        self.name = name
        self._encode = encode_fn
        self._decode = decode_fn

    def encode(self, value):
        return self._encode(value)

    def decode(self, value):
        return self._decode(value)


def _resolve_field_codec(annotation: Any, registry, owner: str, fname: str):
    """Build (encode_fn, decode_fn) for a single field annotation.

    `registry` is the active Registry; lookups resolve nested
    Transportable classes recursively. `owner` and `fname` are used
    for error messages only.
    """
    if annotation is Any or annotation is None:
        raise CodecError(
            f"{owner}.{fname}: type annotation {annotation!r} is not "
            f"codec-supported. Use a concrete type.")

    if annotation in _PRIMITIVES:
        return _identity, _identity

    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)

    if origin in (typing.Union, types.UnionType):
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) != 1 or len(args) != 2:
            raise CodecError(
                f"{owner}.{fname}: only Optional[X] unions are "
                f"supported, got {annotation!r}")
        inner_enc, inner_dec = _resolve_field_codec(
            non_none[0], registry, owner, fname)

        def enc(v, _e=inner_enc):
            return None if v is None else _e(v)

        def dec(v, _d=inner_dec):
            return None if v is None else _d(v)

        return enc, dec

    if origin is list:
        if len(args) != 1:
            raise CodecError(
                f"{owner}.{fname}: list must be parameterized, got {annotation!r}")
        inner_enc, inner_dec = _resolve_field_codec(
            args[0], registry, owner, fname)
        return (lambda v, _e=inner_enc: [_e(x) for x in v],
                lambda v, _d=inner_dec: [_d(x) for x in v])

    if origin is dict:
        if len(args) != 2 or args[0] not in _PRIMITIVES:
            raise CodecError(
                f"{owner}.{fname}: dict requires (primitive, V) parameters, "
                f"got {annotation!r}")
        v_enc, v_dec = _resolve_field_codec(args[1], registry, owner, fname)
        return (lambda v, _e=v_enc: {k: _e(val) for k, val in v.items()},
                lambda v, _d=v_dec: {k: _d(val) for k, val in v.items()})

    if origin is tuple:
        if not args:
            raise CodecError(
                f"{owner}.{fname}: tuple must be parameterized, got {annotation!r}")
        if len(args) == 2 and args[1] is Ellipsis:
            inner_enc, inner_dec = _resolve_field_codec(
                args[0], registry, owner, fname)
            return (lambda v, _e=inner_enc: [_e(x) for x in v],
                    lambda v, _d=inner_dec: tuple(_d(x) for x in v))
        elem_codecs = [_resolve_field_codec(a, registry, owner, fname)
                       for a in args]
        return (
            lambda v, _ec=elem_codecs:
                [e(x) for (e, _), x in zip(_ec, v)],
            lambda v, _ec=elem_codecs:
                tuple(d(x) for (_, d), x in zip(_ec, v)))

    if isinstance(annotation, type):
        entry = registry.try_lookup_by_class(annotation)
        if entry is not None:
            return (lambda v, _r=entry: _r.codec.encode(v),
                    lambda v, _r=entry: _r.codec.decode(v))

    raise CodecError(
        f"{owner}.{fname}: type {annotation!r} is not codec-supported. "
        f"Either use a primitive, a registered Transportable, or one of "
        f"the supported generics (Optional/list/dict/tuple).")


def _identity(value):
    return value


def build_codec(cls: type, registry) -> _Codec:
    """Decide the codec strategy for `cls` and build it.

    Custom-codec path wins when present (caller's deliberate choice).
    Otherwise the class must be a dataclass with codec-supported fields.
    """
    has_custom = (callable(getattr(cls, "__cbor_encode__", None))
                  and callable(getattr(cls, "__cbor_decode__", None)))
    if has_custom:
        return _CustomCodec(cls)

    if not dataclasses.is_dataclass(cls):
        raise CodecError(
            f"{cls.__name__}: not a dataclass and lacks __cbor_encode__ / "
            f"__cbor_decode__ classmethods. Apply @dataclass or implement "
            f"the custom codec hooks.")

    hints = typing.get_type_hints(cls)
    schema = []
    for f in dataclasses.fields(cls):
        if f.name not in hints:
            raise CodecError(
                f"{cls.__name__}.{f.name}: missing type annotation.")
        encode_fn, decode_fn = _resolve_field_codec(
            hints[f.name], registry, cls.__name__, f.name)
        schema.append(_FieldSchema(f.name, encode_fn, decode_fn))
    return _DataclassCodec(cls, schema)


def to_bytes(instance, codec: _Codec) -> bytes:
    """Top-level encode: wrap a codec output in CBOR."""
    return cbor2.dumps(codec.encode(instance))


def from_bytes(data: bytes, codec: _Codec):
    """Top-level decode: parse CBOR then route through the codec."""
    return codec.decode(cbor2.loads(data))
