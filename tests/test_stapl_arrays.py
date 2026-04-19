"""Test STAPL array indexing, initialization, and subrange semantics.

These tests use synthetic STAPL programs that don't need JTAG hardware.
They verify that our interpreter and transpiler handle array operations
correctly per JESD71.
"""

import asyncio
import pytest

from acrobe.stapl import load, Interpreter, StaplPlayer, StaplExit


class CollectingPlayer(StaplPlayer):
    """Player that captures EXPORTs and PRINTs for verification."""

    def __init__(self):
        self.exports = {}
        self.prints = []

    async def note(self, text):
        pass

    async def export(self, key, value):
        self.exports[key] = value

    async def state(self, target, path=None):
        pass

    async def wait(self, ws, cy, us, es):
        pass

    async def trst(self, cy, us):
        pass


async def run_stapl(source, action='TEST'):
    """Parse and run a STAPL program, return the player."""
    prog = load(source, check_crc=False)
    player = CollectingPlayer()
    interp = Interpreter(prog)
    try:
        exit_code = await interp.execute(action, player)
    except StaplExit as e:
        exit_code = e.code
    return player, exit_code


# === INTEGER array initialization ===

@pytest.mark.asyncio
async def test_integer_array_init_order():
    """INTEGER array[n] = v0, v1, ..., vn-1 should store v0 at index 0."""
    source = """
    NOTE "test" "test";
    PROCEDURE test;
    INTEGER a[5] = 10, 20, 30, 40, 50;
    EXPORT "a0", a[0];
    EXPORT "a1", a[1];
    EXPORT "a2", a[2];
    EXPORT "a3", a[3];
    EXPORT "a4", a[4];
    ENDPROC;
    ACTION TEST = test;
    CRC 0;
    """
    player, code = await run_stapl(source)
    assert player.exports['a0'] == 10
    assert player.exports['a1'] == 20
    assert player.exports['a2'] == 30
    assert player.exports['a3'] == 40
    assert player.exports['a4'] == 50


@pytest.mark.asyncio
async def test_integer_array_init_two_elements():
    """Verify A61-like [idcode, count] pattern: first value at index 0."""
    source = """
    NOTE "test" "test";
    PROCEDURE test;
    INTEGER a[2] = 56946909, 1;
    EXPORT "a0", a[0];
    EXPORT "a1", a[1];
    ENDPROC;
    ACTION TEST = test;
    CRC 0;
    """
    player, code = await run_stapl(source)
    # a[0] should be the first listed value (56946909 = 0x0364F0DD)
    assert player.exports['a0'] == 56946909
    assert player.exports['a1'] == 1


# === INTEGER array subrange ===

@pytest.mark.asyncio
@pytest.mark.xfail(reason="Integer subrange not yet implemented in interpreter")
async def test_integer_subrange_decreasing():
    """ia[high..low] = ia[high..low] copies in decreasing (preferred) order."""
    source = """
    NOTE "test" "test";
    PROCEDURE test;
    INTEGER src[4] = 100, 200, 300, 400;
    INTEGER dst[4];
    dst[3..0] = src[3..0];
    EXPORT "d0", dst[0];
    EXPORT "d1", dst[1];
    EXPORT "d2", dst[2];
    EXPORT "d3", dst[3];
    ENDPROC;
    ACTION TEST = test;
    CRC 0;
    """
    player, code = await run_stapl(source)
    assert player.exports['d0'] == 100
    assert player.exports['d1'] == 200
    assert player.exports['d2'] == 300
    assert player.exports['d3'] == 400


@pytest.mark.asyncio
@pytest.mark.xfail(reason="Integer subrange not yet implemented in interpreter")
async def test_integer_subrange_increasing_reverses():
    """ia[low..high] = ia[high..low] should reverse the elements.

    Per JESD71 7.3: increasing order "represents a reversal of the
    preferred order of the bits."
    """
    source = """
    NOTE "test" "test";
    PROCEDURE test;
    INTEGER src[4] = 100, 200, 300, 400;
    INTEGER dst[4];
    dst[0..3] = src[3..0];
    EXPORT "d0", dst[0];
    EXPORT "d1", dst[1];
    EXPORT "d2", dst[2];
    EXPORT "d3", dst[3];
    ENDPROC;
    ACTION TEST = test;
    CRC 0;
    """
    player, code = await run_stapl(source)
    # dst[0..3] increasing = reverse of src[3..0] decreasing
    # src[3..0] = [400, 300, 200, 100] (high to low)
    # dst[0..3] = reversed = [100, 200, 300, 400]? Or [400, 300, 200, 100]?
    # Actually per spec: "When increasing order is specified, it represents
    # a reversal of the preferred order"
    # So dst[0..3] = src[3..0] means:
    # dst[0]=src[3], dst[1]=src[2], dst[2]=src[1], dst[3]=src[0]
    assert player.exports['d0'] == 400
    assert player.exports['d1'] == 300
    assert player.exports['d2'] == 200
    assert player.exports['d3'] == 100


# === BOOLEAN array initialization ===

@pytest.mark.asyncio
async def test_boolean_hex_init():
    """$hex literal: rightmost digit LSB = index 0."""
    source = """
    NOTE "test" "test";
    PROCEDURE test;
    BOOLEAN b[8] = $A5;
    EXPORT "b0", b[0];
    EXPORT "b1", b[1];
    EXPORT "b2", b[2];
    EXPORT "b3", b[3];
    EXPORT "b4", b[4];
    EXPORT "b5", b[5];
    EXPORT "b6", b[6];
    EXPORT "b7", b[7];
    ENDPROC;
    ACTION TEST = test;
    CRC 0;
    """
    player, code = await run_stapl(source)
    # $A5 = 0xA5 = 1010_0101 binary
    # bit 0 (LSB) = 1
    # bit 1 = 0
    # bit 2 = 1
    # bit 3 = 0
    # bit 4 = 0 (LSB of upper nibble 0xA = 1010)
    # bit 5 = 1
    # bit 6 = 0
    # bit 7 = 1 (MSB)
    assert player.exports['b0'] == 1
    assert player.exports['b1'] == 0
    assert player.exports['b2'] == 1
    assert player.exports['b3'] == 0
    assert player.exports['b4'] == 0
    assert player.exports['b5'] == 1
    assert player.exports['b6'] == 0
    assert player.exports['b7'] == 1


@pytest.mark.asyncio
async def test_boolean_binary_init():
    """#binary literal: rightmost bit = index 0."""
    source = """
    NOTE "test" "test";
    PROCEDURE test;
    BOOLEAN b[4] = #1010;
    EXPORT "b0", b[0];
    EXPORT "b1", b[1];
    EXPORT "b2", b[2];
    EXPORT "b3", b[3];
    ENDPROC;
    ACTION TEST = test;
    CRC 0;
    """
    player, code = await run_stapl(source)
    # #1010: rightmost '0' = bit 0
    assert player.exports['b0'] == 0
    assert player.exports['b1'] == 1
    assert player.exports['b2'] == 0
    assert player.exports['b3'] == 1


# === BOOLEAN subrange and DRSCAN ordering ===

@pytest.mark.asyncio
async def test_boolean_subrange_decreasing():
    """b[high..low] extracts bits in decreasing (preferred) order."""
    source = """
    NOTE "test" "test";
    PROCEDURE test;
    BOOLEAN src[8] = $A5;
    BOOLEAN dst[4];
    dst[3..0] = src[3..0];
    EXPORT "d0", dst[0];
    EXPORT "d1", dst[1];
    EXPORT "d2", dst[2];
    EXPORT "d3", dst[3];
    ENDPROC;
    ACTION TEST = test;
    CRC 0;
    """
    player, code = await run_stapl(source)
    # src = 0xA5 = bits [10100101]
    # src[3..0] = lower nibble = 0101 = 0x5
    # dst[3..0] = same
    assert player.exports['d0'] == 1  # bit 0 of 0x5
    assert player.exports['d1'] == 0
    assert player.exports['d2'] == 1
    assert player.exports['d3'] == 0


# === INT() and BOOL() conversion ===

@pytest.mark.asyncio
async def test_int_bool_conversion():
    """INT() converts 32-bit Boolean to integer, BOOL() converts back."""
    source = """
    NOTE "test" "test";
    PROCEDURE test;
    BOOLEAN b[32] = $0000000F;
    INTEGER i;
    i = INT(b);
    EXPORT "i", i;
    ENDPROC;
    ACTION TEST = test;
    CRC 0;
    """
    player, code = await run_stapl(source)
    assert player.exports['i'] == 0x0F


@pytest.mark.asyncio
async def test_integer_loop_with_array():
    """Test that FOR loop with array indexing works correctly."""
    source = """
    NOTE "test" "test";
    PROCEDURE test;
    INTEGER a[3] = 10, 20, 30;
    INTEGER sum = 0;
    FOR i = 0 TO 2;
    sum = sum + a[i];
    NEXT i;
    EXPORT "sum", sum;
    ENDPROC;
    ACTION TEST = test;
    CRC 0;
    """
    player, code = await run_stapl(source)
    assert player.exports['sum'] == 60  # 10 + 20 + 30
