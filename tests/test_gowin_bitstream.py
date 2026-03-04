import gzip
import pytest

from crobe_async.loadable.gowin import load_gowin_bitstream
from crobe_async.loadable import Program


def _make_fs(*, header_lines=None, bits="1010", usercode="0xDEADBEEF"):
    """Build a synthetic .fs file content as a string."""
    lines = []
    if header_lines is None:
        lines.append(f"//Device: GW5A-60\n")
        if usercode is not None:
            lines.append(f"//UserCode: {usercode}\n")
        lines.append("//Checksum: 0x1234\n")
    else:
        lines.extend(header_lines)
    lines.append(bits + "\n")
    return "".join(lines)


class TestGowinBitstream:
    def test_basic_parse(self, tmp_path):
        content = _make_fs(bits="10110100")
        path = tmp_path / "test.fs"
        path.write_text(content)

        prog = load_gowin_bitstream(str(path))
        assert len(prog) == 1
        assert prog.info["Device"] == "GW5A-60"
        assert prog.info["UserCode"] == "0xDEADBEEF"
        assert prog.info["Checksum"] == "0x1234"
        assert prog[0].address == 0

    def test_offset(self, tmp_path):
        content = _make_fs(bits="10101010")
        path = tmp_path / "test.fs"
        path.write_text(content)

        prog = load_gowin_bitstream(str(path), offset=0x1000)
        assert prog[0].address == 0x1000

    def test_binary_conversion(self, tmp_path):
        # 8 bits "10110100" = 0xB4 as MSB-first integer
        # BitString(0xB4, 8) in little-endian bytes = [0xB4]
        # reversed = [0xB4]
        content = _make_fs(bits="10110100")
        path = tmp_path / "test.fs"
        path.write_text(content)

        prog = load_gowin_bitstream(str(path))
        data = bytes(prog[0].data)
        assert len(data) == 1

    def test_multiline_bits(self, tmp_path):
        # Two lines of 8 bits each = 16 bits total
        lines = [
            "//Device: test\n",
            "10110100\n",
            "11001010\n",
        ]
        path = tmp_path / "test.fs"
        path.write_text("".join(lines))

        prog = load_gowin_bitstream(str(path))
        data = bytes(prog[0].data)
        assert len(data) == 2

    def test_gzip(self, tmp_path):
        content = _make_fs(bits="10101010")
        path = tmp_path / "test.fs.gz"
        with gzip.open(str(path), 'wt', encoding='utf-8') as f:
            f.write(content)

        prog = load_gowin_bitstream(str(path))
        assert len(prog) == 1
        assert prog.info["Device"] == "GW5A-60"

    def test_missing_usercode(self, tmp_path):
        content = _make_fs(usercode=None)
        path = tmp_path / "test.fs"
        path.write_text(content)

        prog = load_gowin_bitstream(str(path))
        assert "UserCode" not in prog.info

    def test_ext_db_fs(self):
        handlers = Program.ext_db.get("fs")
        assert load_gowin_bitstream in handlers

    def test_ext_db_fs_gz(self):
        handlers = Program.ext_db.get("fs.gz")
        assert load_gowin_bitstream in handlers

    def test_format_db_fs(self):
        handlers = Program.format_db.get("fs")
        assert load_gowin_bitstream in handlers

    def test_source_recorded(self, tmp_path):
        content = _make_fs(bits="10101010")
        path = tmp_path / "test.fs"
        path.write_text(content)

        prog = load_gowin_bitstream(str(path))
        assert str(path) in prog.sources

    def test_from_file(self, tmp_path):
        content = _make_fs(bits="10101010")
        path = tmp_path / "test.fs"
        path.write_text(content)

        prog = Program.from_file(str(path))
        assert len(prog) == 1

    def test_empty_bitstream(self, tmp_path):
        path = tmp_path / "test.fs"
        path.write_text("//Device: test\n")

        with pytest.raises(ValueError, match="No bitstream data"):
            load_gowin_bitstream(str(path))

    def test_comment_without_colon_skipped(self, tmp_path):
        """Comment lines without colon are skipped."""
        lines = [
            "//Device: test\n",
            "//This is just a comment\n",
            "10101010\n",
        ]
        path = tmp_path / "test.fs"
        path.write_text("".join(lines))

        prog = load_gowin_bitstream(str(path))
        assert prog.info["Device"] == "test"
        assert len(prog) == 1
