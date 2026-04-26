"""Memory region model.

Base classes for memory regions in the component tree.
All read/write/erase operations are async.

Region types:
  Ram      — volatile read/write memory
  Flash    — erase-before-write, has write pages and erase pages
  Eeprom   — byte-writable non-volatile, no explicit erase needed
"""

from ..node import Node


class Region(Node):
    """Contiguous memory region.

    Subclasses must implement read() and write().
    """

    def __init__(self, name, address, size):
        super().__init__(name)
        self.address = address
        self.size = size

    @property
    def end(self):
        return self.address + self.size

    def contains(self, addr):
        return self.address <= addr < self.end

    async def read(self, offset, size):
        raise NotImplementedError

    async def write(self, offset, data):
        raise NotImplementedError

    def __lt__(self, other):
        return self.address < other.address

    def __repr__(self):
        return (f"<{self.__class__.__name__} '{self._name}' "
                f"0x{self.address:08x}-0x{self.end:08x}>")


class Ram(Region):
    """Volatile read/write memory."""

    async def erase(self, offset, size):
        pass


class Flash(Region):
    """Flash memory with erase-before-write semantics.

    Attributes:
        erased_value: byte value after erase (typically 0xff)
        write_page_size: max bytes per write command
        erase_page_sizes: available erase granularities, ascending
    """

    erased_value = 0xff
    write_page_size: int
    erase_page_sizes: list[int]

    def __init__(self, name, address, size, write_page_size, erase_page_sizes):
        super().__init__(name, address, size)
        self.write_page_size = write_page_size
        self.erase_page_sizes = sorted(erase_page_sizes)
        self._blank = False

    @property
    def is_blank(self):
        return self._blank

    @is_blank.setter
    def is_blank(self, value):
        self._blank = value

    async def erase(self, offset, size):
        raise NotImplementedError

    def __repr__(self):
        return (f"<{self.__class__.__name__} '{self._name}' "
                f"0x{self.address:08x}-0x{self.end:08x} "
                f"wp={self.write_page_size} ep={self.erase_page_sizes}>")


class Eeprom(Region):
    """Byte-addressable non-volatile memory. No explicit erase needed.

    Attributes:
        write_page_size: max bytes per write command
    """

    def __init__(self, name, address, size, write_page_size):
        super().__init__(name, address, size)
        self.write_page_size = write_page_size

    @property
    def is_blank(self):
        return False

    async def erase(self, offset, size):
        pass
