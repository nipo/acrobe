import pytest
from acrobe.stapl.lexer import tokenize
from acrobe.stapl.parser import parse
from acrobe.stapl.interpreter import Interpreter, StaplPlayer, StaplExit


class MockPlayer(StaplPlayer):
    """Record all player calls for verification."""

    def __init__(self):
        self.calls = []
        self.notes = []
        self.exports = {}

    async def ir_scan(self, length, tdi, pre, post, capture):
        self.calls.append(('ir_scan', length, tdi, pre, post, capture))
        if capture:
            return b'\x00' * ((length + 7) // 8)
        return None

    async def dr_scan(self, length, tdi, pre, post, capture):
        self.calls.append(('dr_scan', length, tdi, pre, post, capture))
        if capture:
            return b'\x00' * ((length + 7) // 8)
        return None

    async def state(self, target, path=None):
        self.calls.append(('state', target, path))

    async def wait(self, wait_state, cycles, usecs, end_state):
        self.calls.append(('wait', wait_state, cycles, usecs, end_state))

    async def trst(self, cycles, usecs):
        self.calls.append(('trst', cycles, usecs))

    async def frequency(self, hertz):
        self.calls.append(('frequency', hertz))

    async def note(self, text):
        self.notes.append(text)

    async def export(self, key, value):
        self.exports[key] = value


def _build_program(source):
    stmts = tokenize(source, check_crc=False)
    return parse(stmts)


async def test_simple_exit():
    source = '''
    NOTE "DEVICE" "test";
    ACTION TEST = DO_TEST;
    PROCEDURE DO_TEST;
        EXIT 0;
    ENDPROC;
    CRC 0000;
    '''
    prog = _build_program(source)
    interp = Interpreter(prog)
    player = MockPlayer()
    code = await interp.execute("TEST", player)
    assert code == 0


async def test_exit_nonzero():
    source = '''
    NOTE "DEVICE" "test";
    ACTION TEST = DO_TEST;
    PROCEDURE DO_TEST;
        EXIT 5;
    ENDPROC;
    CRC 0000;
    '''
    prog = _build_program(source)
    interp = Interpreter(prog)
    player = MockPlayer()
    code = await interp.execute("TEST", player)
    assert code == 5


async def test_integer_arithmetic():
    source = '''
    NOTE "DEVICE" "test";
    ACTION TEST = DO_TEST;
    PROCEDURE DO_TEST;
        INTEGER x = 10;
        INTEGER y = 3;
        INTEGER result;
        result = x + y;
        EXPORT "SUM", result;
        result = x * y;
        EXPORT "PRODUCT", result;
        EXIT 0;
    ENDPROC;
    CRC 0000;
    '''
    prog = _build_program(source)
    interp = Interpreter(prog)
    player = MockPlayer()
    await interp.execute("TEST", player)
    assert player.exports["SUM"] == 13
    assert player.exports["PRODUCT"] == 30


async def test_for_loop():
    source = '''
    NOTE "DEVICE" "test";
    ACTION TEST = DO_TEST;
    PROCEDURE DO_TEST;
        INTEGER i;
        INTEGER sum = 0;
        FOR i = 1 TO 5;
            sum = sum + i;
        NEXT i;
        EXPORT "SUM", sum;
        EXIT 0;
    ENDPROC;
    CRC 0000;
    '''
    prog = _build_program(source)
    interp = Interpreter(prog)
    player = MockPlayer()
    await interp.execute("TEST", player)
    assert player.exports["SUM"] == 15  # 1+2+3+4+5


async def test_for_loop_step():
    source = '''
    NOTE "DEVICE" "test";
    ACTION TEST = DO_TEST;
    PROCEDURE DO_TEST;
        INTEGER i;
        INTEGER sum = 0;
        FOR i = 0 TO 10 STEP 3;
            sum = sum + 1;
        NEXT i;
        EXPORT "COUNT", sum;
        EXIT 0;
    ENDPROC;
    CRC 0000;
    '''
    prog = _build_program(source)
    interp = Interpreter(prog)
    player = MockPlayer()
    await interp.execute("TEST", player)
    assert player.exports["COUNT"] == 4  # i=0,3,6,9


async def test_goto():
    source = '''
    NOTE "DEVICE" "test";
    ACTION TEST = DO_TEST;
    PROCEDURE DO_TEST;
        INTEGER x = 0;
        GOTO skip;
        x = 99;
        skip: EXPORT "X", x;
        EXIT 0;
    ENDPROC;
    CRC 0000;
    '''
    prog = _build_program(source)
    interp = Interpreter(prog)
    player = MockPlayer()
    await interp.execute("TEST", player)
    assert player.exports["X"] == 0


async def test_if_then():
    source = '''
    NOTE "DEVICE" "test";
    ACTION TEST = DO_TEST;
    PROCEDURE DO_TEST;
        INTEGER x = 5;
        INTEGER result = 0;
        IF x > 3 THEN result = 1;
        IF x < 3 THEN result = 2;
        EXPORT "RESULT", result;
        EXIT 0;
    ENDPROC;
    CRC 0000;
    '''
    prog = _build_program(source)
    interp = Interpreter(prog)
    player = MockPlayer()
    await interp.execute("TEST", player)
    assert player.exports["RESULT"] == 1


async def test_call_procedure():
    source = '''
    NOTE "DEVICE" "test";
    ACTION TEST = DO_TEST;
    PROCEDURE DO_TEST USES helper;
        INTEGER x = 0;
        CALL helper;
        EXPORT "X", x;
        EXIT 0;
    ENDPROC;
    PROCEDURE helper;
        x = 42;
    ENDPROC;
    CRC 0000;
    '''
    prog = _build_program(source)
    interp = Interpreter(prog)
    player = MockPlayer()
    await interp.execute("TEST", player)
    assert player.exports["X"] == 42


async def test_push_pop():
    source = '''
    NOTE "DEVICE" "test";
    ACTION TEST = DO_TEST;
    PROCEDURE DO_TEST;
        INTEGER x;
        PUSH 42;
        POP x;
        EXPORT "X", x;
        EXIT 0;
    ENDPROC;
    CRC 0000;
    '''
    prog = _build_program(source)
    interp = Interpreter(prog)
    player = MockPlayer()
    await interp.execute("TEST", player)
    assert player.exports["X"] == 42


async def test_boolean_operations():
    source = '''
    NOTE "DEVICE" "test";
    ACTION TEST = DO_TEST;
    PROCEDURE DO_TEST;
        BOOLEAN data[8] = $AB;
        EXPORT "BIT0", data[0];
        EXPORT "BIT1", data[1];
        EXPORT "INT_VAL", INT(data[7..0]);
        EXIT 0;
    ENDPROC;
    CRC 0000;
    '''
    prog = _build_program(source)
    interp = Interpreter(prog)
    player = MockPlayer()
    await interp.execute("TEST", player)
    assert player.exports["BIT0"] == 1   # 0xAB = 10101011, bit 0 = 1
    assert player.exports["BIT1"] == 1   # bit 1 = 1
    assert player.exports["INT_VAL"] == 0xAB


async def test_drscan():
    source = '''
    NOTE "DEVICE" "test";
    ACTION TEST = DO_TEST;
    PROCEDURE DO_TEST;
        BOOLEAN tdi[8] = $FF;
        DRSCAN 8, tdi[7..0];
        EXIT 0;
    ENDPROC;
    CRC 0000;
    '''
    prog = _build_program(source)
    interp = Interpreter(prog)
    player = MockPlayer()
    await interp.execute("TEST", player)
    assert len(player.calls) == 1
    assert player.calls[0][0] == 'dr_scan'
    assert player.calls[0][1] == 8  # length


async def test_state():
    source = '''
    NOTE "DEVICE" "test";
    ACTION TEST = DO_TEST;
    PROCEDURE DO_TEST;
        STATE RESET;
        STATE IDLE;
        EXIT 0;
    ENDPROC;
    CRC 0000;
    '''
    prog = _build_program(source)
    interp = Interpreter(prog)
    player = MockPlayer()
    await interp.execute("TEST", player)
    assert player.calls[0] == ('state', 'RESET', None)
    assert player.calls[1] == ('state', 'IDLE', None)


async def test_wait():
    source = '''
    NOTE "DEVICE" "test";
    ACTION TEST = DO_TEST;
    PROCEDURE DO_TEST;
        WAIT 100 USEC;
        EXIT 0;
    ENDPROC;
    CRC 0000;
    '''
    prog = _build_program(source)
    interp = Interpreter(prog)
    player = MockPlayer()
    await interp.execute("TEST", player)
    assert player.calls[0] == ('wait', None, None, 100, None)


async def test_print():
    source = '''
    NOTE "DEVICE" "test";
    ACTION TEST = DO_TEST;
    PROCEDURE DO_TEST;
        INTEGER x = 42;
        PRINT "Value is ", x;
        EXIT 0;
    ENDPROC;
    CRC 0000;
    '''
    prog = _build_program(source)
    interp = Interpreter(prog)
    player = MockPlayer()
    await interp.execute("TEST", player)
    assert player.notes[0] == "Value is 42"


async def test_data_block():
    source = '''
    NOTE "DEVICE" "test";
    ACTION TEST = DO_TEST;
    PROCEDURE DO_TEST USES mydata;
        EXPORT "COUNT", num;
        EXIT 0;
    ENDPROC;
    DATA mydata;
        INTEGER num = 99;
    ENDDATA;
    CRC 0000;
    '''
    prog = _build_program(source)
    interp = Interpreter(prog)
    player = MockPlayer()
    await interp.execute("TEST", player)
    assert player.exports["COUNT"] == 99


async def test_predr_postdr():
    source = '''
    NOTE "DEVICE" "test";
    ACTION TEST = DO_TEST;
    PROCEDURE DO_TEST;
        BOOLEAN tdi[8] = $FF;
        PREDR 4;
        POSTDR 4;
        DRSCAN 8, tdi[7..0];
        EXIT 0;
    ENDPROC;
    CRC 0000;
    '''
    prog = _build_program(source)
    interp = Interpreter(prog)
    player = MockPlayer()
    await interp.execute("TEST", player)
    # Check that pre/post were passed to dr_scan
    call = player.calls[0]
    assert call[0] == 'dr_scan'
    pre = call[3]  # pre tuple
    post = call[4]  # post tuple
    assert pre[0] == 4
    assert post[0] == 4


async def test_optional_procedure():
    source = '''
    NOTE "DEVICE" "test";
    ACTION TEST =
        DO_REQUIRED,
        DO_OPTIONAL OPTIONAL;
    PROCEDURE DO_REQUIRED;
        EXPORT "REQ", 1;
    ENDPROC;
    PROCEDURE DO_OPTIONAL;
        EXPORT "OPT", 1;
    ENDPROC;
    CRC 0000;
    '''
    prog = _build_program(source)

    # Without include: optional not called
    interp = Interpreter(prog)
    player = MockPlayer()
    await interp.execute("TEST", player)
    assert "REQ" in player.exports
    assert "OPT" not in player.exports

    # With include: optional called
    interp = Interpreter(prog)
    player = MockPlayer()
    await interp.execute("TEST", player, include={"DO_OPTIONAL"})
    assert "REQ" in player.exports
    assert "OPT" in player.exports
