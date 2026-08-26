# cython: language_level=3, boundscheck=False, wraparound=False
"""Cython implementation of :mod:`acrobe.bitstring`.

Drop-in replacement: same class hierarchy (BitStringBase / BitString /
BitStringSlice / MutableBitString), same operator semantics (==,
__bytes__, __int__, __add__, slicing), same wire serialization
(__cbor_encode__ / __cbor_decode__), same indexed bit access.

Loaded opportunistically by ``acrobe.bitstring`` — the pure-Python
classes in that module remain the source of truth and the
unconditional fallback when this extension can't be built/imported.
The Cython gain comes from cdef-class attribute access (no dict
lookup), C-level method dispatch, and statically typed ints in the
hot per-op paths. The actual byte/int math is still CPython
builtins (int.from_bytes, int.to_bytes, bytes slicing) — those are
already at C speed in pure Python.
"""


cdef class BitStringBase:
    """Common operator/dunder behaviour shared by every concrete bit
    string. Subclasses must provide ``__len__`` and ``data``."""

    # Slots populated by acrobe.wire.registry when the class is
    # decorated as @wire.value etc. cdef class types are otherwise
    # immutable, so we have to pre-declare anything the registry
    # might set.
    __wire_uuid__ = None
    __wire_kind__ = None

    def __eq__(self, other):
        if len(self) != len(other):
            return False
        return bytes(self) == bytes(other)

    def __bytes__(self):
        return self.data

    def __bool__(self):
        return len(self) > 0

    def __int__(self):
        # NOTE: ``length`` is intentionally a Python int — ``1 << length``
        # with a cdef-int length compiles to a C shift, which is
        # undefined when length >= 32. The whole BitString point is to
        # carry shift values that can reach ~thousands of bits.
        length = len(self)
        if length == 0:
            return 0
        return int.from_bytes(self.data, 'little') & ((1 << length) - 1)

    def __str__(self):
        n = len(self)
        if n > 1024:
            return "[%d bits]" % n
        if n:
            return bin(int(self))[2:][::-1].ljust(n, '0')
        return "."

    def __repr__(self):
        n = len(self)
        if n > 1024:
            return "BitString([...], %d)" % n
        return "BitString(%s, %d)" % (self.data.hex(), n)

    def __hash__(self):
        return hash((len(self), bytes(self)))

    @classmethod
    def __cbor_encode__(cls, instance):
        """Wire format: [bit_count, bytes_lsb_first]."""
        return [len(instance), bytes(instance)]

    @classmethod
    def __cbor_decode__(cls, data):
        length, raw = data
        return BitString(raw, length)

    def reversed(self):
        n = len(self)
        if n == 0:
            return BitString()
        val = int(self)
        rev = 0
        for i in range(n):
            if val & (1 << i):
                rev |= 1 << (n - 1 - i)
        return BitString(rev, n)


cdef class BitStringSlice(BitStringBase):
    """View into another bitstring's data without owning storage."""

    cdef BitStringBase _bs
    cdef int _begin
    cdef int _end

    def __cinit__(self, BitStringBase bs, int begin, int end):
        self._bs = bs
        self._begin = begin
        self._end = end

    def __len__(self):
        return self._end - self._begin

    def __int__(self):
        cdef int begin_byte
        cdef int end_byte
        cdef int begin_bit
        # length stays Python int — see note in BitStringBase.__int__.
        length = self._end - self._begin
        if length == 0:
            return 0
        begin_byte = self._begin >> 3
        end_byte = (self._end + 7) >> 3
        blob = self._bs.data[begin_byte:end_byte]
        begin_bit = self._begin & 7
        return (int.from_bytes(blob, 'little') >> begin_bit) & ((1 << length) - 1)

    @property
    def data(self):
        cdef int begin_byte
        length = self._end - self._begin
        if length == 0:
            return b''
        # Byte-aligned slice: take a substring of the underlying
        # data without the int round-trip. JtagMpsse._emit_shift's
        # 8-aligned chunking lands here for every full-byte chunk.
        if (self._begin & 7) == 0 and (length & 7) == 0:
            begin_byte = self._begin >> 3
            return self._bs.data[begin_byte:begin_byte + (length >> 3)]
        return int(self).to_bytes(length=(length + 7) >> 3, byteorder='little')

    def __getitem__(self, offset):
        cdef int length = self._end - self._begin
        cdef int b
        cdef int e
        cdef int abs_offset
        if isinstance(offset, slice):
            sb = offset.start
            se = offset.stop
            if sb is None:
                b = 0
            elif sb < 0:
                b = sb + length
            else:
                b = sb
            if se is None:
                e = length
            elif se < 0:
                e = se + length
            else:
                e = se
            if b < 0:
                b = 0
            if e < 0:
                e = 0
            if b > length:
                b = length
            if e > length:
                e = length
            if e <= b:
                return BitString(0, 0)
            return BitStringSlice(self._bs, self._begin + b, self._begin + e)

        if offset < 0:
            offset += length
        abs_offset = self._begin + offset
        data = self._bs.data
        return bool(data[abs_offset >> 3] & (1 << (abs_offset & 7)))

    def __add__(self, other):
        n = BitString(int(self), len(self))
        n.append(other)
        return n


cdef class BitString(BitStringBase):
    """Append-friendly bit string. Storage is a list of immutable
    ``bytes`` chunks plus a partial last byte for sub-byte tails."""

    cdef list _bytes
    cdef int _last_byte
    cdef int _length
    cdef object _data_cache

    def __init__(self, data=None, length=None):
        # ``blen`` / byte_count stay as Python ints — they participate
        # in ``1 << blen`` shifts that overflow a C int once blen >= 32.
        if data is None:
            self._bytes = []
            self._last_byte = 0
            self._length = 0
            self._data_cache = None
            return

        if isinstance(data, int) and length is not None:
            blen = length
            ival = data
            if ival < 0:
                ival += 1 << blen
            ival &= (1 << blen) - 1
            byte_count = (blen + 7) >> 3
            raw = ival.to_bytes(length=byte_count, byteorder='little')
            if blen & 7:
                self._bytes = [raw[:-1]]
                self._last_byte = raw[-1]
            else:
                self._bytes = [raw]
                self._last_byte = 0
            self._length = blen
            self._data_cache = raw if blen else None
            return

        if isinstance(data, BitString) and length is None:
            self._bytes = (<BitString>data)._bytes[:]
            self._last_byte = (<BitString>data)._last_byte
            self._length = (<BitString>data)._length
            self._data_cache = (<BitString>data)._data_cache
            return

        if isinstance(data, (bytes, bytearray)) and length is not None:
            byte_count = (length + 7) >> 3
            if len(data) == byte_count:
                raw = bytes(data) if isinstance(data, bytearray) else data
                if length & 7:
                    self._bytes = [raw[:-1]]
                    self._last_byte = raw[-1]
                else:
                    self._bytes = [raw]
                    self._last_byte = 0
                self._length = length
                self._data_cache = raw
                return

        # Fallback: empty self + general append.
        self._bytes = []
        self._last_byte = 0
        self._length = 0
        self._data_cache = None
        self.append(data, length)

    def append(self, data, length=None):
        # All length-like locals here are Python ints — they all
        # participate in shifts/multiplications that would overflow
        # a C int.
        if isinstance(data, BitStringBase):
            dlen = len(data)
            if self._length & 7:
                # Unaligned tail: convert to int for shift-merge.
                data = int(data)
            else:
                data = data.data
            length = dlen

        if isinstance(data, (bytes, bytearray)):
            if length is None:
                length = len(data) * 8
            else:
                data = data.ljust((length + 7) >> 3, b'\x00')

            if self._length & 7:
                ival = int.from_bytes(data, byteorder='little')
                ival <<= (self._length & 7)
                ival |= self._last_byte
                length += self._length & 7
                self._length &= ~7
                self._last_byte = 0
                data = ival.to_bytes(length=(length + 7) >> 3, byteorder='little')

        elif isinstance(data, int):
            ival = data
            if ival < 0:
                ival += 1 << length
            ival &= (1 << length) - 1

            if self._length & 7:
                ival <<= (self._length & 7)
                ival |= self._last_byte
                length += self._length & 7
                self._length &= ~7
            data = ival.to_bytes(length=(length + 7) >> 3, byteorder='little')

        self._length += length
        if self._length & 7:
            self._last_byte = data[-1]
            self._bytes.append(data[:-1])
        else:
            self._bytes.append(data)
            self._last_byte = 0

        self._data_cache = None

    @property
    def data(self):
        if self._data_cache is None:
            if self._length & 7:
                self._data_cache = b''.join(self._bytes + [bytes([self._last_byte])])
            else:
                self._data_cache = b''.join(self._bytes)
        return self._data_cache

    def __len__(self):
        return self._length

    def _coalesce(self):
        if self._length & 7:
            flat = bytearray().join(self._bytes) + bytearray([self._last_byte])
        else:
            flat = bytearray().join(self._bytes)
        self._bytes = [flat]
        self._last_byte = 0
        self._data_cache = None
        return flat

    def __getitem__(self, offset):
        cdef int b
        cdef int e
        if isinstance(offset, slice):
            sb = offset.start
            se = offset.stop
            if sb is None:
                b = 0
            elif sb < 0:
                b = sb + self._length
            else:
                b = sb
            if se is None:
                e = self._length
            elif se < 0:
                e = se + self._length
            else:
                e = se
            if b < 0:
                b = 0
            if e < 0:
                e = 0
            if b > self._length:
                b = self._length
            if e > self._length:
                e = self._length
            if e <= b:
                return BitString(0, 0)
            return BitStringSlice(self, b, e)

        if offset < 0:
            offset += self._length

        if not (0 <= offset < self._length):
            raise IndexError(offset)

        data = self.data
        return bool((data[offset >> 3] >> (offset & 7)) & 1)

    def __setitem__(self, offset, value):
        cdef int b
        cdef int e
        cdef int slice_len
        cdef int i
        cdef int byte_idx
        cdef int bit_idx
        cdef int chunk_boundary

        if isinstance(offset, slice):
            sb = offset.start
            se = offset.stop
            if sb is None:
                b = 0
            elif sb < 0:
                b = sb + self._length
            else:
                b = sb
            if se is None:
                e = self._length
            elif se < 0:
                e = se + self._length
            else:
                e = se
            if b < 0:
                b = 0
            if e < 0:
                e = 0
            if b > self._length:
                b = self._length
            if e > self._length:
                e = self._length
            slice_len = e - b
            if len(value) != slice_len:
                raise ValueError(
                    "assigned BitString length %d does not match slice length %d"
                    % (len(value), slice_len))
            if slice_len == 0:
                return
            flat = self._coalesce()
            val_int = int(value)
            for i in range(slice_len):
                byte_idx = (b + i) >> 3
                bit_idx = (b + i) & 7
                flat[byte_idx] &= ~(1 << bit_idx)
                if val_int & (1 << i):
                    flat[byte_idx] |= (1 << bit_idx)
            if self._length & 7:
                self._last_byte = flat[-1]
                self._bytes = [flat[:-1]]
            else:
                self._bytes = [flat]
                self._last_byte = 0
            self._data_cache = None
            return

        if offset < 0:
            offset += self._length
        if not (0 <= offset < self._length):
            raise IndexError(offset)

        byte_idx = offset >> 3
        bit_idx = offset & 7
        chunk_boundary = sum(len(x) for x in self._bytes)

        if byte_idx < chunk_boundary:
            if len(self._bytes) > 1 or not isinstance(self._bytes[0], bytearray):
                self._bytes = [bytearray(b'').join(self._bytes)]
            if value:
                self._bytes[0][byte_idx] |= 1 << bit_idx
            else:
                self._bytes[0][byte_idx] &= ~(1 << bit_idx)
        else:
            if value:
                self._last_byte |= 1 << bit_idx
            else:
                self._last_byte &= ~(1 << bit_idx)

        self._data_cache = None

    def __iadd__(self, other):
        if not self._length and isinstance(other, BitString):
            self._length = (<BitString>other)._length
            self._bytes = (<BitString>other)._bytes[:]
            self._last_byte = (<BitString>other)._last_byte
            self._data_cache = None
            return self
        if len(other):
            self.append(other)
        return self

    def __add__(self, other):
        # ``self_len`` participates in ``int(other) << self_len`` which
        # is a Python-int shift only if self_len is a Python int.
        # Otherwise Cython compiles a C shift and overflows above 31.
        self_len = self._length
        other_len = len(other)
        if other_len == 0:
            return BitString(self)
        if self_len == 0:
            if isinstance(other, BitString):
                return BitString(other)
            return BitString(int(other), other_len)
        total = self_len + other_len
        if total <= 256:
            combined = int(self) | (int(other) << self_len)
            return BitString(combined, total)
        n = BitString(self)
        n.append(other)
        return n


cdef class MutableBitString(BitStringBase):
    """Fixed-length bitstring with a mutable bytearray backing.

    Same constructor as :class:`BitString`. Single-bit set is O(1)
    once built, unlike :class:`BitString` which is optimized for
    append-only construction."""

    cdef bytearray _data
    cdef int _length

    def __init__(self, *args, **kwargs):
        cdef int expected
        cdef int excess
        if not args and not kwargs:
            self._data = bytearray()
            self._length = 0
            return
        seed = BitString(*args, **kwargs)
        self._length = len(seed)
        self._data = bytearray(seed.data)
        expected = (self._length + 7) >> 3
        if len(self._data) < expected:
            self._data.extend(b'\x00' * (expected - len(self._data)))
        elif len(self._data) > expected:
            del self._data[expected:]
        excess = self._length & 7
        if excess and self._data:
            self._data[-1] &= (1 << excess) - 1

    @property
    def data(self):
        return bytes(self._data)

    def __len__(self):
        return self._length

    def __getitem__(self, offset):
        cdef int b
        cdef int e
        if isinstance(offset, slice):
            if offset.step not in (None, 1):
                raise ValueError(
                    "MutableBitString supports only step=1 slices; "
                    "use .reversed() for descending order")
            sb = offset.start
            se = offset.stop
            if sb is None:
                b = 0
            elif sb < 0:
                b = sb + self._length
            else:
                b = sb
            if se is None:
                e = self._length
            elif se < 0:
                e = se + self._length
            else:
                e = se
            if b < 0:
                b = 0
            if e < 0:
                e = 0
            if b > self._length:
                b = self._length
            if e > self._length:
                e = self._length
            if e <= b:
                return BitString(0, 0)
            return BitStringSlice(self, b, e)

        if offset < 0:
            offset += self._length
        if not (0 <= offset < self._length):
            raise IndexError(offset)
        return bool((self._data[offset >> 3] >> (offset & 7)) & 1)

    def __setitem__(self, offset, value):
        cdef int b
        cdef int e
        cdef int slice_len
        cdef int n
        cdef int i
        cdef int pos
        if isinstance(offset, slice):
            if offset.step not in (None, 1):
                raise ValueError(
                    "MutableBitString supports only step=1 slices; "
                    "reverse the source with .reversed() instead")
            sb = offset.start
            se = offset.stop
            if sb is None:
                b = 0
            elif sb < 0:
                b = sb + self._length
            else:
                b = sb
            if se is None:
                e = self._length
            elif se < 0:
                e = se + self._length
            else:
                e = se
            if b < 0:
                b = 0
            if e < 0:
                e = 0
            if b > self._length:
                b = self._length
            if e > self._length:
                e = self._length
            slice_len = e - b
            n = slice_len if slice_len < len(value) else len(value)
            for i in range(n):
                pos = b + i
                if value[i]:
                    self._data[pos >> 3] |= 1 << (pos & 7)
                else:
                    self._data[pos >> 3] &= ~(1 << (pos & 7))
            return

        if offset < 0:
            offset += self._length
        if not (0 <= offset < self._length):
            raise IndexError(offset)
        if value:
            self._data[offset >> 3] |= 1 << (offset & 7)
        else:
            self._data[offset >> 3] &= ~(1 << (offset & 7))
