import pytest
from crobe_async.allocator import Range, Allocator


class TestRange:
    def test_basic(self):
        r = Range(0x100, 0x40)
        assert r.address == 0x100
        assert r.size == 0x40
        assert r.end == 0x140

    def test_zero_size_rejected(self):
        with pytest.raises(AssertionError):
            Range(0x100, 0)

    def test_touches_adjacent(self):
        a = Range(0x100, 0x40)
        b = Range(0x140, 0x20)
        assert a.touches(b)
        assert b.touches(a)

    def test_touches_gap(self):
        a = Range(0x100, 0x40)
        b = Range(0x180, 0x20)
        assert not a.touches(b)

    def test_merge(self):
        a = Range(0x100, 0x40)
        b = Range(0x140, 0x20)
        m = a.merge(b)
        assert m.address == 0x100
        assert m.size == 0x60
        # Also works in reverse order
        m2 = b.merge(a)
        assert m2 == m

    def test_split_exact(self):
        r = Range(0x100, 0x40)
        left, right = r.split(0x40)
        assert left == r
        assert right is None

    def test_split_partial(self):
        r = Range(0x100, 0x40)
        left, right = r.split(0x10)
        assert left == Range(0x100, 0x10)
        assert right == Range(0x110, 0x30)

    def test_split_alloc_exact_fit(self):
        r = Range(0x100, 0x40)
        result = r.split_alloc(0x40, 1)
        assert result is not None
        left, size, right = result
        assert left == 0
        assert size == 0x40
        assert right == 0

    def test_split_alloc_too_big(self):
        r = Range(0x100, 0x40)
        assert r.split_alloc(0x80, 1) is None

    def test_split_alloc_aligned(self):
        r = Range(0x100, 0x100)
        result = r.split_alloc(0x40, 0x40)
        assert result is not None
        left, size, right = result
        # Should be aligned to 0x40
        alloc_addr = r.address + left
        assert alloc_addr % 0x40 == 0
        assert size == 0x40

    def test_hash_and_eq(self):
        a = Range(0x100, 0x40)
        b = Range(0x100, 0x40)
        assert a == b
        assert hash(a) == hash(b)
        s = {a, b}
        assert len(s) == 1

    def test_ordering(self):
        a = Range(0x100, 0x40)
        b = Range(0x200, 0x40)
        assert a < b
        assert sorted([b, a]) == [a, b]

    def test_repr(self):
        r = Range(0x100, 0x40)
        assert "0x100" in repr(r)


class TestAllocator:
    def test_simple_alloc(self):
        a = Allocator(0x20000000, 0x1000)
        r = a.allocate(0x100)
        assert r.size == 0x100
        assert r.address >= 0x20000000
        assert r.end <= 0x20001000

    def test_alloc_and_free(self):
        a = Allocator(0x20000000, 0x1000)
        r = a.allocate(0x100)
        assert r in a
        a.free(r)
        assert r not in a

    def test_free_and_realloc_full(self):
        a = Allocator(0x20000000, 0x100)
        r = a.allocate(0x100)
        a.free(r)
        # Should be able to allocate full size again after free
        r2 = a.allocate(0x100)
        assert r2.size == 0x100

    def test_multiple_allocs(self):
        a = Allocator(0x20000000, 0x1000)
        r1 = a.allocate(0x100)
        r2 = a.allocate(0x100)
        # No overlap
        assert r1.end <= r2.address or r2.end <= r1.address

    def test_alignment(self):
        a = Allocator(0x20000000, 0x1000)
        r = a.allocate(0x40, align=0x100)
        assert r.address % 0x100 == 0

    def test_out_of_space(self):
        a = Allocator(0x20000000, 0x100)
        a.allocate(0x100)
        with pytest.raises(ValueError, match="No space"):
            a.allocate(0x10)

    def test_free_merges_adjacent(self):
        a = Allocator(0x20000000, 0x300)
        r1 = a.allocate(0x100)
        r2 = a.allocate(0x100)
        r3 = a.allocate(0x100)
        # Free middle then neighbors — should merge
        a.free(r2)
        a.free(r1)
        a.free(r3)
        # All free, should be able to allocate full size
        r = a.allocate(0x300)
        assert r.size == 0x300

    def test_best_fit(self):
        a = Allocator(0x20000000, 0x1000)
        # Create fragmentation: alloc 3 blocks, free the smaller ones
        r1 = a.allocate(0x100)
        r2 = a.allocate(0x200)
        r3 = a.allocate(0x100)
        a.free(r1)  # 0x100 hole
        a.free(r3)  # 0x100 hole
        # r2 still allocated (0x200 in the middle)
        # Allocate 0x100 - should fit in one of the 0x100 holes (best fit)
        r4 = a.allocate(0x100)
        assert r4.size == 0x100

    def test_free_unknown_range_raises(self):
        a = Allocator(0x20000000, 0x100)
        r = Range(0x30000000, 0x100)
        with pytest.raises(KeyError):
            a.free(r)

    def test_repr(self):
        a = Allocator(0x20000000, 0x100)
        assert "Allocator" in repr(a)
