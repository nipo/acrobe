from abc import ABC, abstractmethod


class BitStringBase(ABC):
    __slots__ = ()

    @abstractmethod
    def __len__(self):
        ...

    @property
    @abstractmethod
    def data(self):
        ...

    def __eq__(self, other):
        if len(self) != len(other):
            return False
        return bytes(self) == bytes(other)

    def __bytes__(self):
        return self.data

    def __bool__(self):
        return len(self) > 0

    def __int__(self):
        return int.from_bytes(self.data, byteorder='little') & ((1 << len(self)) - 1)

    def __str__(self):
        if len(self) > 1024:
            return "[%d bits]" % len(self)
        if len(self):
            return bin(int(self))[2:][::-1].ljust(len(self), '0')
        return "."

    def __repr__(self):
        if len(self) > 1024:
            return "BitString([...], %d)" % len(self)
        return "BitString(%s, %d)" % (self.data.hex(), len(self))

    def __hash__(self):
        return hash((len(self), bytes(self)))

    @classmethod
    def __cbor_encode__(cls, instance):
        """Wire format: [bit_count, bytes_lsb_first].

        Acrobe's BitString is LSB-first, which doesn't match the
        standard CBOR bit-string convention. We use an app-specific
        tag (registered in wire/values.py) to keep the semantics
        explicit and avoid confusion with any future standard
        bit-string encoding.
        """
        return [len(instance), bytes(instance)]

    @classmethod
    def __cbor_decode__(cls, data):
        """Reverse of __cbor_encode__: rebuild a BitString from the
        (bit_count, bytes) pair."""
        from . import bitstring as _bs
        length, raw = data
        return _bs.BitString(raw, length)

    def reversed(self):
        """Return a new BitString with the bits in reverse order."""
        n = len(self)
        if n == 0:
            return BitString()
        val = int(self)
        rev = 0
        for i in range(n):
            if val & (1 << i):
                rev |= 1 << (n - 1 - i)
        return BitString(rev, n)


class BitStringSlice(BitStringBase):
    __slots__ = ('_bs', '_begin', '_end')

    def __init__(self, bs, begin, end):
        self._bs = bs
        self._begin = begin
        self._end = end

    def __len__(self):
        return self._end - self._begin

    def __int__(self):
        length = self._end - self._begin
        if length == 0:
            return 0

        # Extract only the bytes that span [begin, end)
        begin_byte = self._begin >> 3
        end_byte = (self._end + 7) >> 3
        blob = self._bs.data[begin_byte:end_byte]

        # Shift right to drop bits below begin, then mask to length
        begin_bit = self._begin & 7
        return (int.from_bytes(blob, byteorder='little') >> begin_bit) & ((1 << length) - 1)

    @property
    def data(self):
        length = self._end - self._begin
        if length == 0:
            return b''
        # Byte-aligned slice: just take a substring of the underlying
        # data without the int round-trip. Hot for JtagMpsse._emit_shift,
        # which slices 8-aligned chunks out of a chain-wide BitString.
        if (self._begin & 7) == 0 and (length & 7) == 0:
            begin_byte = self._begin >> 3
            return self._bs.data[begin_byte:begin_byte + (length >> 3)]
        return int(self).to_bytes(length=(length + 7) >> 3, byteorder='little')

    def __getitem__(self, offset):
        length = len(self)

        if isinstance(offset, slice):
            b, e = offset.start, offset.stop
            if b is None:
                b = 0
            elif b < 0:
                b += length
            if e is None:
                e = length
            elif e < 0:
                e += length

            b = max(0, min(b, length))
            e = max(0, min(e, length))

            if e <= b:
                return BitString(0, 0)

            return BitStringSlice(self._bs, self._begin + b, self._begin + e)

        if offset < 0:
            offset += length

        abs_offset = self._begin + offset
        data = self._bs.data
        return bool(data[abs_offset // 8] & (1 << (abs_offset & 7)))

    def __add__(self, other):
        n = BitString(int(self), len(self))
        n.append(other)
        return n


class BitString(BitStringBase):
    __slots__ = ('_bytes', '_last_byte', '_length', '_data_cache')

    def __init__(self, data=None, length=None):
        # Fast paths for the cases that dominate the hot transfer
        # loop. The general path stays available via append() for
        # everything else (mixed bytes/int, signed-int sentinels in a
        # follow-up append, etc.).
        if data is None:
            self._bytes = []
            self._last_byte = 0
            self._length = 0
            self._data_cache = None
            return
        if isinstance(data, int) and length is not None:
            if data < 0:
                data += 1 << length
            data &= (1 << length) - 1
            byte_count = (length + 7) >> 3
            raw = data.to_bytes(length=byte_count, byteorder='little')
            if length & 7:
                self._bytes = [raw[:-1]]
                self._last_byte = raw[-1]
            else:
                self._bytes = [raw]
                self._last_byte = 0
            self._length = length
            self._data_cache = raw if length else None
            return
        if isinstance(data, BitString) and length is None:
            # Cheap clone: chunk lists are immutable bytes objects so
            # the shallow copy is safe even if the source mutates
            # later (mutation always allocates a fresh bytes anyway).
            self._bytes = data._bytes[:]
            self._last_byte = data._last_byte
            self._length = data._length
            self._data_cache = data._data_cache
            return
        # Fallback: empty self + general append.
        self._bytes = []
        self._last_byte = 0
        self._length = 0
        self._data_cache = None
        self.append(data, length)

    def append(self, data, length=None):
        if isinstance(data, BitStringBase):
            length = len(data)
            if self._length & 7:
                # Unaligned: convert to int for bit-shifting merge
                data = int(data)
            else:
                data = data.data

        if isinstance(data, (bytes, bytearray)):
            if length is None:
                length = len(data) * 8
            else:
                data = data.ljust((length + 7) // 8, b'\x00')

            if self._length & 7:
                # Merge partial last byte with incoming data via int shift
                data = int.from_bytes(data, byteorder='little')
                data <<= (self._length & 7)
                data |= self._last_byte
                length += self._length & 7
                self._length &= ~7
                self._last_byte = 0
                data = data.to_bytes(length=(length + 7) // 8, byteorder='little')

        elif isinstance(data, int):
            if data < 0:
                data += 1 << length

            data &= (1 << length) - 1

            if self._length & 7:
                # Shift new int data up past the partial byte bits, merge
                data <<= (self._length & 7)
                data |= self._last_byte
                length += self._length & 7
                self._length &= ~7
            data = data.to_bytes(length=(length + 7) // 8, byteorder='little')

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
            assert 0 <= self._length <= len(self._data_cache) * 8

        return self._data_cache

    def __len__(self):
        return self._length

    def _coalesce(self):
        """Flatten internal chunk list into a single mutable bytearray."""
        if self._length & 7:
            flat = bytearray().join(self._bytes) + bytearray([self._last_byte])
        else:
            flat = bytearray().join(self._bytes)
        self._bytes = [flat]
        self._last_byte = 0
        self._data_cache = None
        return flat

    def __getitem__(self, offset):
        if isinstance(offset, slice):
            b, e = offset.start, offset.stop
            if b is None:
                b = 0
            elif b < 0:
                b += self._length
            if e is None:
                e = self._length
            elif e < 0:
                e += self._length

            b = max(0, min(b, self._length))
            e = max(0, min(e, self._length))

            if e <= b:
                return BitString(0, 0)

            return BitStringSlice(self, b, e)

        if offset < 0:
            offset += self._length

        if not (0 <= offset < self._length):
            raise IndexError(offset)

        data = self.data
        return bool((data[offset // 8] >> (offset & 7)) & 1)

    def __setitem__(self, offset, value):
        if isinstance(offset, slice):
            b, e = offset.start, offset.stop
            if b is None:
                b = 0
            elif b < 0:
                b += self._length
            if e is None:
                e = self._length
            elif e < 0:
                e += self._length

            b = max(0, min(b, self._length))
            e = max(0, min(e, self._length))

            slice_len = e - b
            if len(value) != slice_len:
                raise ValueError(
                    "assigned BitString length %d does not match slice length %d"
                    % (len(value), slice_len)
                )

            if slice_len == 0:
                return

            flat = self._coalesce()
            val_int = int(value)

            # Walk each bit position in the target range.
            # We clear the old bit and set the new one from val_int.
            for i in range(slice_len):
                byte_idx = (b + i) // 8
                bit_idx = (b + i) & 7
                # Clear
                flat[byte_idx] &= ~(1 << bit_idx)
                # Set from value
                if val_int & (1 << i):
                    flat[byte_idx] |= (1 << bit_idx)

            # Re-split: if length is not byte-aligned, last byte goes to _last_byte
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

        byte_idx = offset // 8
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
            self._length = other._length
            self._bytes = other._bytes[:]
            self._last_byte = other._last_byte
            self._data_cache = None
            return self
        if len(other):
            self.append(other)
        return self

    def __add__(self, other):
        n = BitString(self)
        if len(other):
            n.append(other)
        return n


class MutableBitString(BitStringBase):
    """
    Fixed-length bitstring with mutable bytearray backing.

    Construction matches BitString's prototype (same args). Once built,
    individual bits can be modified in place via __setitem__ — single-bit
    set is O(1) without copying or reallocating, unlike BitString (which
    is optimized for append-only construction and degrades sharply on
    per-bit mutation).
    """

    __slots__ = ('_data', '_length')

    def __init__(self, *args, **kwargs):
        if not args and not kwargs:
            self._data = bytearray()
            self._length = 0
            return
        # Reuse BitString's argument parsing for the initial value.
        seed = BitString(*args, **kwargs)
        self._length = len(seed)
        self._data = bytearray(seed.data)
        # Normalize storage: exactly ceil(length / 8) bytes, with junk in
        # bits past the end of the last byte cleared. Keeps __int__,
        # __eq__ and friends from leaking the unused tail.
        expected = (self._length + 7) // 8
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
        if isinstance(offset, slice):
            if offset.step not in (None, 1):
                raise ValueError(
                    "MutableBitString supports only step=1 slices; "
                    "use .reversed() for descending order")
            b, e = offset.start, offset.stop
            if b is None:
                b = 0
            elif b < 0:
                b += self._length
            if e is None:
                e = self._length
            elif e < 0:
                e += self._length
            b = max(0, min(b, self._length))
            e = max(0, min(e, self._length))
            if e <= b:
                return BitString(0, 0)
            return BitStringSlice(self, b, e)

        if offset < 0:
            offset += self._length
        if not (0 <= offset < self._length):
            raise IndexError(offset)
        return bool((self._data[offset >> 3] >> (offset & 7)) & 1)

    def __setitem__(self, offset, value):
        if isinstance(offset, slice):
            if offset.step not in (None, 1):
                raise ValueError(
                    "MutableBitString supports only step=1 slices; "
                    "reverse the source with .reversed() instead")
            b, e = offset.start, offset.stop
            if b is None:
                b = 0
            elif b < 0:
                b += self._length
            if e is None:
                e = self._length
            elif e < 0:
                e += self._length
            b = max(0, min(b, self._length))
            e = max(0, min(e, self._length))
            slice_len = e - b
            n = min(slice_len, len(value))
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
