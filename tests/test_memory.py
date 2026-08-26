import pytest
from acrobe.target.region import Region, Ram, Flash, Eeprom


class TestRegion:
    def test_basic(self):
        r = Region("test", 0x08000000, 0x10000)
        assert r.address == 0x08000000
        assert r.size == 0x10000
        assert r.end == 0x08010000
        assert r.name == "test"

    def test_contains(self):
        r = Region("test", 0x1000, 0x100)
        assert r.contains(0x1000)
        assert r.contains(0x10ff)
        assert not r.contains(0x0fff)
        assert not r.contains(0x1100)

    def test_ordering(self):
        a = Region("lo", 0x1000, 0x100)
        b = Region("hi", 0x2000, 0x100)
        assert a < b
        assert sorted([b, a]) == [a, b]

    def test_repr(self):
        r = Region("flash", 0x08000000, 0x10000)
        s = repr(r)
        assert "Region" in s
        assert "flash" in s

    @pytest.mark.asyncio
    async def test_read_not_implemented(self):
        r = Region("test", 0, 0x100)
        with pytest.raises(NotImplementedError):
            await r.read(0, 0x10)

    @pytest.mark.asyncio
    async def test_write_not_implemented(self):
        r = Region("test", 0, 0x100)
        with pytest.raises(NotImplementedError):
            await r.write(0, b"\x00" * 16)


class TestRam:
    def test_is_region(self):
        r = Ram("sram", 0x20000000, 0x10000)
        assert isinstance(r, Region)

    @pytest.mark.asyncio
    async def test_erase_is_noop(self):
        r = Ram("sram", 0x20000000, 0x10000)
        await r.erase(0, 0x100)  # should not raise


class TestFlash:
    def test_basic(self):
        f = Flash("main", 0x08000000, 0x40000,
                  write_page_size=256,
                  erase_page_sizes=[4096, 32768, 65536])
        assert f.write_page_size == 256
        assert f.erase_page_sizes == [4096, 32768, 65536]
        assert f.erased_value == 0xff

    def test_is_blank_tracking(self):
        f = Flash("main", 0x08000000, 0x40000,
                  write_page_size=256,
                  erase_page_sizes=[4096])
        assert not f.is_blank
        f.is_blank = True
        assert f.is_blank
        f.is_blank = False
        assert not f.is_blank

    def test_erase_page_sizes_sorted(self):
        f = Flash("main", 0, 0x40000,
                  write_page_size=256,
                  erase_page_sizes=[65536, 4096, 32768])
        assert f.erase_page_sizes == [4096, 32768, 65536]

    @pytest.mark.asyncio
    async def test_erase_not_implemented(self):
        f = Flash("main", 0, 0x40000,
                  write_page_size=256,
                  erase_page_sizes=[4096])
        with pytest.raises(NotImplementedError):
            await f.erase(0, 4096)

    def test_repr(self):
        f = Flash("main", 0x08000000, 0x40000,
                  write_page_size=256,
                  erase_page_sizes=[4096])
        s = repr(f)
        assert "Flash" in s
        assert "main" in s


class TestEeprom:
    def test_basic(self):
        e = Eeprom("config", 0x0000, 0x100, write_page_size=16)
        assert e.write_page_size == 16
        assert isinstance(e, Region)

    def test_is_blank_always_false(self):
        e = Eeprom("config", 0, 0x100, write_page_size=16)
        assert not e.is_blank

    @pytest.mark.asyncio
    async def test_erase_is_noop(self):
        e = Eeprom("config", 0, 0x100, write_page_size=16)
        await e.erase(0, 0x100)  # should not raise
