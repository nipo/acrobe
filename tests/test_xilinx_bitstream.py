import struct
import gzip
import tempfile
import os
import pytest

from acrobe.loadable.xilinx import load_xilinx_bitstream
from acrobe.loadable import Program


HEADER = bytes([
    0x00, 0x09, 0x0f, 0xf0, 0x0f, 0xf0,
    0x0f, 0xf0, 0x0f, 0xf0, 0x00, 0x00, 0x01,
])


def _make_section(letter, data):
    """Build a lettered section (a-d): 1-byte letter + 2-byte length + data."""
    return letter + struct.pack(">H", len(data)) + data


def _make_payload_section(data):
    """Build section e: 1-byte letter + 4-byte length + data."""
    return b'e' + struct.pack(">L", len(data)) + data


def _build_bitstream(*, project="test_project;UserID=0x12345678",
                     device="6slx9tqg144", date="2024/01/15",
                     time="10:30:00", payload=b'\xaa\x99\x55\x66'):
    parts = HEADER
    parts += _make_section(b'a', project.encode() + b'\x00')
    parts += _make_section(b'b', device.encode() + b'\x00')
    parts += _make_section(b'c', date.encode() + b'\x00')
    parts += _make_section(b'd', time.encode() + b'\x00')
    parts += _make_payload_section(payload)
    return parts


class TestXilinxBitstream:
    def test_basic_parse(self, tmp_path):
        data = _build_bitstream()
        path = tmp_path / "test.bit"
        path.write_bytes(data)

        prog = load_xilinx_bitstream(str(path))
        assert len(prog) == 1
        assert prog[0].data == bytearray(b'\xaa\x99\x55\x66')
        assert prog.info["device"] == "6slx9tqg144"
        assert prog.info["project"] == "test_project"
        assert prog.info["userid"] == 0x12345678
        assert "2024/01/15" in prog.info["build_date"]
        assert "10:30:00" in prog.info["build_date"]

    def test_offset(self, tmp_path):
        data = _build_bitstream()
        path = tmp_path / "test.bit"
        path.write_bytes(data)

        prog = load_xilinx_bitstream(str(path), offset=0x1000)
        assert prog[0].address == 0x1000

    def test_gzip(self, tmp_path):
        data = _build_bitstream()
        path = tmp_path / "test.bit.gz"
        with gzip.open(str(path), 'wb') as f:
            f.write(data)

        prog = load_xilinx_bitstream(str(path))
        assert len(prog) == 1
        assert prog.info["userid"] == 0x12345678

    def test_no_userid(self, tmp_path):
        data = _build_bitstream(project="my_design")
        path = tmp_path / "test.bit"
        path.write_bytes(data)

        prog = load_xilinx_bitstream(str(path))
        assert "userid" not in prog.info
        assert prog.info["project"] == "my_design"

    def test_bad_header(self, tmp_path):
        path = tmp_path / "test.bit"
        path.write_bytes(b'\x00' * 20)

        with pytest.raises(ValueError, match="Bad header"):
            load_xilinx_bitstream(str(path))

    def test_truncated_payload(self, tmp_path):
        data = _build_bitstream()
        # Truncate in the middle of section e payload
        path = tmp_path / "test.bit"
        path.write_bytes(data[:-2])

        with pytest.raises(ValueError, match="Short payload"):
            load_xilinx_bitstream(str(path))

    def test_no_payload(self, tmp_path):
        """Bitstream with only lettered sections but no 'e' section."""
        parts = HEADER
        parts += _make_section(b'a', b'test\x00')
        parts += _make_section(b'b', b'device\x00')
        path = tmp_path / "test.bit"
        path.write_bytes(parts)

        with pytest.raises(ValueError, match="Not bitstream data"):
            load_xilinx_bitstream(str(path))

    def test_ext_db_registered(self):
        handlers = Program.ext_db.get("bit")
        assert load_xilinx_bitstream in handlers

    def test_format_db_registered(self):
        for fmt in ("bit", "xilinx"):
            handlers = Program.format_db.get(fmt)
            assert load_xilinx_bitstream in handlers

    def test_large_payload(self, tmp_path):
        payload = bytes(range(256)) * 100  # 25600 bytes
        data = _build_bitstream(payload=payload)
        path = tmp_path / "test.bit"
        path.write_bytes(data)

        prog = load_xilinx_bitstream(str(path))
        assert len(prog[0].data) == 25600
        assert bytes(prog[0].data) == payload

    def test_source_recorded(self, tmp_path):
        data = _build_bitstream()
        path = tmp_path / "test.bit"
        path.write_bytes(data)

        prog = load_xilinx_bitstream(str(path))
        assert str(path) in prog.sources
