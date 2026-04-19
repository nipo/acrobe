"""Run .jam fixture files through our interpreter, check against Altera reference.

These .jam files were tested against Altera's jam_player-2.6.2 reference
implementation to establish ground truth for array ordering and boolean
literal semantics.

Each fixture is tested both via interpreter and via transpiled output.
"""

import importlib
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from acrobe.stapl import load, Interpreter, StaplPlayer, StaplExit
from acrobe.stapl.transpile import transpile as do_transpile, TranspileConfig


FIXTURES = Path(__file__).parent / 'stapl_fixtures'


class CollectingPlayer(StaplPlayer):
    """Player that captures EXPORTs and PRINTs for verification."""

    def __init__(self):
        self.exports = {}
        self.prints = []

    async def note(self, text):
        self.prints.append(text)

    async def export(self, key, value):
        self.exports[key] = value

    async def state(self, target, path=None):
        pass

    async def wait(self, ws, cy, us, es):
        pass

    async def trst(self, cy, us):
        pass


async def run_jam(filename, action='TEST'):
    source = (FIXTURES / filename).read_text()
    prog = load(source, check_crc=False)
    player = CollectingPlayer()
    interp = Interpreter(prog)
    try:
        exit_code = await interp.execute(action, player)
    except StaplExit as e:
        exit_code = e.code
    return player, exit_code


async def run_jam_transpiled(filename, action='TEST'):
    """Transpile a .jam file and run the result, collecting exports."""
    source = (FIXTURES / filename).read_text()
    prog = load(source, check_crc=False)
    config = TranspileConfig()
    python_source, data_files = do_transpile(prog, config, filename)

    with tempfile.TemporaryDirectory() as tmpdir:
        out = Path(tmpdir)
        (out / 'program.py').write_text(python_source)
        if data_files:
            data_dir = out / 'data'
            data_dir.mkdir()
            for name, data in data_files.items():
                (data_dir / name).write_bytes(data)

        # Import the transpiled module
        mod_name = f'_transpiled_{filename.replace(".", "_")}'
        spec = importlib.util.spec_from_file_location(mod_name, out / 'program.py')
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)

        try:
            tp = mod.TranspiledProgram(None)  # no JTAG interface needed
            exports = {}
            tp._export = lambda k, v: _store(exports, k, v)
            action_method = getattr(tp, f'action_{action.lower()}')
            exit_code = await action_method()
        finally:
            del sys.modules[mod_name]

    return exports, exit_code


async def _store(exports, key, value):
    exports[key] = value


# === Transpiled vs interpreter consistency ===

class TestTranspiledConsistency:
    """Verify transpiled output produces identical exports as interpreter."""

    @pytest.fixture(params=[
        'test_array_order.jam',
        'test_bool_order.jam',
        'test_bool_subrange.jam',
    ])
    def fixture_name(self, request):
        return request.param

    async def test_exports_match(self, fixture_name):
        interp_player, interp_code = await run_jam(fixture_name)
        transpiled_exports, transpiled_code = await run_jam_transpiled(fixture_name)
        assert interp_code == transpiled_code
        assert transpiled_exports == interp_player.exports


# Expected output verified against Altera jam_player-2.6.2

class TestArrayOrder:
    """test_array_order.jam — INTEGER array init uses Altera reversed order."""

    @pytest.fixture()
    async def result(self):
        return await run_jam('test_array_order.jam')

    async def test_exit_success(self, result):
        _, code = result
        assert code == 0

    async def test_integer_array_reversed(self, result):
        """Last value in init list → index 0 (Altera convention)."""
        player, _ = result
        assert player.exports['a0'] == 50
        assert player.exports['a4'] == 10

    async def test_two_element_array(self, result):
        """A61-like [idcode, count] pattern: last value → index 0."""
        player, _ = result
        assert player.exports['b0'] == 1
        assert player.exports['b1'] == 56946909

    async def test_print_output(self, result):
        """Verify PRINT output matches Altera reference."""
        player, _ = result
        prints = player.prints
        assert 'a[0] = 50' in prints
        assert 'a[1] = 40' in prints
        assert 'a[2] = 30' in prints
        assert 'a[3] = 20' in prints
        assert 'a[4] = 10' in prints
        assert 'b[0] = 1' in prints
        assert 'b[1] = 56946909' in prints


class TestBoolOrder:
    """test_bool_order.jam — BOOLEAN literal bit ordering."""

    @pytest.fixture()
    async def result(self):
        return await run_jam('test_bool_order.jam')

    async def test_exit_success(self, result):
        _, code = result
        assert code == 0

    async def test_hex_literal(self, result):
        """$A5 hex literal: rightmost nibble is lowest indices."""
        player, _ = result
        # $A5 = 1010_0101, bit 0 = LSB
        prints = player.prints
        assert ' bh[0]=1' in prints
        assert ' bh[1]=0' in prints
        assert ' bh[2]=1' in prints
        assert ' bh[3]=0' in prints
        assert ' bh[4]=0' in prints
        assert ' bh[5]=1' in prints
        assert ' bh[6]=0' in prints
        assert ' bh[7]=1' in prints

    async def test_binary_literal(self, result):
        """#1010 binary literal: rightmost bit = index 0."""
        player, _ = result
        prints = player.prints
        assert ' bb[0]=0' in prints
        assert ' bb[1]=1' in prints
        assert ' bb[2]=0' in prints
        assert ' bb[3]=1' in prints

    async def test_16bit_hex_int_conversion(self, result):
        """$CAFE as 16-bit boolean → INT() = 0xCAFE."""
        player, _ = result
        assert player.exports['b16_int'] == 0xCAFE

    async def test_subrange_copy(self, result):
        """dst[3..0] = src[7..4] copies upper nibble to lower."""
        player, _ = result
        # src = $A5 = 1010_0101, upper nibble bits [7..4] = 1010
        # dst[3..0] = 1010 → dst[0]=0, dst[1]=1, dst[2]=0, dst[3]=1
        assert player.exports['dst_0'] == 0
        assert player.exports['dst_1'] == 1
        assert player.exports['dst_2'] == 0
        assert player.exports['dst_3'] == 1

    async def test_reversed_subrange(self, result):
        """dst2[0..3] = src[7..4]: reversed target write."""
        player, _ = result
        # src[7..4] = 1010 ascending, written reversed into dst2[0..3]
        # dst2[0]=src[7]=1, dst2[1]=src[6]=0, dst2[2]=src[5]=1, dst2[3]=src[4]=0
        assert player.exports['dst2_0'] == 1
        assert player.exports['dst2_1'] == 0
        assert player.exports['dst2_2'] == 1
        assert player.exports['dst2_3'] == 0


class TestBoolSubrange:
    """test_bool_subrange.jam — BOOLEAN subrange read/write operations.

    All expected values verified against Altera jam_player-2.6.2.
    """

    @pytest.fixture()
    async def result(self):
        return await run_jam('test_bool_subrange.jam')

    async def test_exit_success(self, result):
        _, code = result
        assert code == 0

    async def test_read_upper_nibble(self, result):
        """src[7..4]: upper nibble of $A5 = 1010."""
        player, _ = result
        assert player.exports['r1_0'] == 0
        assert player.exports['r1_1'] == 1
        assert player.exports['r1_2'] == 0
        assert player.exports['r1_3'] == 1

    async def test_read_lower_nibble(self, result):
        """src[3..0]: lower nibble of $A5 = 0101."""
        player, _ = result
        assert player.exports['r2_0'] == 1
        assert player.exports['r2_1'] == 0
        assert player.exports['r2_2'] == 1
        assert player.exports['r2_3'] == 0

    async def test_write_lower_to_upper(self, result):
        """d3 = $A5 then d3[7..4] = src[3..0]: swap nibbles → $55."""
        player, _ = result
        # src[3..0] = 0101 placed at bits [7..4]
        # lower nibble unchanged from src: 0101
        # result: 0101_0101 = $55
        assert (player.exports['d3_0'], player.exports['d3_1'],
                player.exports['d3_2'], player.exports['d3_3']) == (1, 0, 1, 0)
        assert (player.exports['d3_4'], player.exports['d3_5'],
                player.exports['d3_6'], player.exports['d3_7']) == (1, 0, 1, 0)

    async def test_write_upper_to_lower(self, result):
        """d4 = $A5 then d4[3..0] = src[7..4]: swap nibbles → $AA."""
        player, _ = result
        # src[7..4] = 1010 placed at bits [3..0]
        # upper nibble unchanged from src: 1010
        # result: 1010_1010 = $AA
        assert (player.exports['d4_0'], player.exports['d4_1'],
                player.exports['d4_2'], player.exports['d4_3']) == (0, 1, 0, 1)
        assert (player.exports['d4_4'], player.exports['d4_5'],
                player.exports['d4_6'], player.exports['d4_7']) == (0, 1, 0, 1)

    async def test_single_bit_subrange(self, result):
        """src[5..5]: single-bit extraction. $A5 bit 5 = 1."""
        player, _ = result
        assert player.exports['r5_0'] == 1

    async def test_full_copy(self, result):
        """r6[7..0] = src[7..0]: full array copy preserves $A5."""
        player, _ = result
        assert (player.exports['r6_0'], player.exports['r6_1'],
                player.exports['r6_2'], player.exports['r6_3']) == (1, 0, 1, 0)
        assert (player.exports['r6_4'], player.exports['r6_5'],
                player.exports['r6_6'], player.exports['r6_7']) == (0, 1, 0, 1)

    async def test_wide_partial_subrange(self, result):
        """w=$BEEF, extract w[11..4] = $EE = 0111_0111."""
        player, _ = result
        assert (player.exports['r7_0'], player.exports['r7_1'],
                player.exports['r7_2'], player.exports['r7_3']) == (0, 1, 1, 1)
        assert (player.exports['r7_4'], player.exports['r7_5'],
                player.exports['r7_6'], player.exports['r7_7']) == (0, 1, 1, 1)

    async def test_overlapping_self_copy(self, result):
        """ov=$A5 then ov[5..2] = ov[3..0]: overlapping regions."""
        player, _ = result
        # $A5=1010_0101, ov[3..0]=0101 copied to ov[5..2]
        # result: 1001_0101 = $95
        assert (player.exports['ov_0'], player.exports['ov_1'],
                player.exports['ov_2'], player.exports['ov_3']) == (1, 0, 1, 0)
        assert (player.exports['ov_4'], player.exports['ov_5'],
                player.exports['ov_6'], player.exports['ov_7']) == (1, 0, 0, 1)

    async def test_non_overlapping_self_copy(self, result):
        """ns=$CF then ns[7..4] = ns[3..0]: non-overlapping → $FF."""
        player, _ = result
        for i in range(8):
            assert player.exports[f'ns_{i}'] == 1

    async def test_clear_middle_bits(self, result):
        """d10=$FF then d10[5..2] = zero: clear middle → $C3."""
        player, _ = result
        # $FF with bits [5..2] zeroed = 1100_0011
        assert (player.exports['d10_0'], player.exports['d10_1']) == (1, 1)
        assert (player.exports['d10_2'], player.exports['d10_3'],
                player.exports['d10_4'], player.exports['d10_5']) == (0, 0, 0, 0)
        assert (player.exports['d10_6'], player.exports['d10_7']) == (1, 1)

    async def test_chain_copy(self, result):
        """src2=$E1 → mid → d11[7..4]: two-hop copy."""
        player, _ = result
        # $E1 bits [7..4] = 1110, mid = 0111 (bit 0 first)
        assert (player.exports['mid_0'], player.exports['mid_1'],
                player.exports['mid_2'], player.exports['mid_3']) == (0, 1, 1, 1)
        # d11[7..4] = mid = 0111
        assert (player.exports['d11_4'], player.exports['d11_5'],
                player.exports['d11_6'], player.exports['d11_7']) == (0, 1, 1, 1)

    async def test_wide_32bit_subrange(self, result):
        """big=$DEADBEEF, extract big[23..8] spot checks."""
        player, _ = result
        # $DEADBEEF bytes LSB first: EF BE AD DE
        # bits [23..8] spans bytes 1-2 = BE AD = $ADBE
        assert player.exports['r12_0'] == 0   # bit 8 of $DEADBEEF = 0
        assert player.exports['r12_7'] == 1   # bit 15 = 1
        assert player.exports['r12_8'] == 1   # bit 16 = 1
        assert player.exports['r12_15'] == 1  # bit 23 = 1
