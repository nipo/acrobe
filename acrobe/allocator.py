"""Memory range allocator with best-fit strategy.

Used by Puppet to carve out RAM for stacks, trampolines, and data buffers.
"""


class Range:
    """Contiguous address range."""

    __slots__ = ('address', 'size')

    def __init__(self, address, size):
        assert size > 0
        self.address = address
        self.size = size

    @property
    def end(self):
        return self.address + self.size

    def touches(self, other):
        """True if self and other are adjacent (no gap, no overlap)."""
        if self.address > other.address:
            return other.end == self.address
        return self.end == other.address

    def merge(self, other):
        """Merge two adjacent ranges into one."""
        lo, hi = (self, other) if self.address <= other.address else (other, self)
        assert lo.end == hi.address
        return Range(lo.address, lo.size + hi.size)

    def split(self, size):
        """Split into (left_of_size, remainder). remainder is None if exact fit."""
        assert size <= self.size
        if size == self.size:
            return self, None
        return Range(self.address, size), Range(self.address + size, self.size - size)

    def split_alloc(self, size, align):
        """Try to carve out an aligned allocation of `size` bytes.

        Returns (left_waste, alloc_size, right_waste) or None if impossible.
        Prefers right-aligned placement (minimizes fragmentation for
        stack-like allocations).
        """
        if self.size < size:
            return None

        # Try right-aligned
        address = (self.end - size) & ~(align - 1)
        if address >= self.address:
            return address - self.address, size, self.end - size - address

        # Try left-aligned
        if self.address % align:
            address = (self.address | (align - 1)) + 1
        else:
            address = self.address
        if address + size <= self.end:
            return address - self.address, size, self.end - size - address

        return None

    def __hash__(self):
        return hash(self.address) ^ hash(self.size)

    def __eq__(self, other):
        return self.address == other.address and self.size == other.size

    def __lt__(self, other):
        return self.address < other.address

    def __repr__(self):
        return f"Range({self.address:#x}, {self.size:#x})"


class Allocator:
    """Best-fit memory allocator with free-list merging."""

    def __init__(self, address, size):
        self._address = address
        self._size = size
        self._free = {Range(address, size)}
        self._used = set()

    def allocate(self, size, align=1):
        """Allocate a range of at least `size` bytes with given alignment.

        Uses best-fit: picks the free block that leaves the smallest waste.
        """
        size = size or 4
        assert size > 0

        best = None
        best_waste = None

        for block in self._free:
            result = block.split_alloc(size, align)
            if result is None:
                continue
            left, _, right = result
            waste = left + right
            if best is None or waste < best_waste:
                best = block, result
                best_waste = waste
                if waste == 0:
                    break

        if best is None:
            raise ValueError(f"No space for {size:#x} bytes (align={align})")

        block, (left, alloc_size, right) = best

        self._free.remove(block)
        remaining = block

        if left:
            crumb, remaining = remaining.split(left)
            self._merge_free(crumb)

        if right:
            remaining, crumb = remaining.split(alloc_size)
            self._merge_free(crumb)

        self._used.add(remaining)
        assert remaining.address % align == 0
        return remaining

    def free(self, r):
        """Return a range to the free pool."""
        self._used.remove(r)
        self._merge_free(r)

    def _merge_free(self, r):
        """Add range to free set, merging with adjacent free blocks."""
        for _ in range(2):
            for block in self._free:
                if block.touches(r):
                    self._free.remove(block)
                    r = r.merge(block)
                    break
        self._free.add(r)

    def __contains__(self, r):
        return r in self._used

    def __repr__(self):
        free_strs = ", ".join(repr(b) for b in sorted(self._free))
        return f"<Allocator free=[{free_strs}]>"
