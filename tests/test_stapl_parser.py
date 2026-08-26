import pytest
from acrobe.stapl.lexer import tokenize
from acrobe.stapl.parser import (
    parse, parse_expr,
    IntLiteral, VarRef, BinOp, UnaryOp, ArrayIndex, ArraySubrange,
    ArrayWhole, FuncCall, BooleanLiteral,
    NoteStmt, ActionDef, IntegerDecl, BooleanDecl,
    AssignStmt, GotoStmt, CallStmt, ExitStmt, IfStmt, ForStmt, NextStmt,
    DrScanStmt, IrScanStmt, StateStmt, WaitStmt, PrintStmt, ExportStmt,
    PreDrStmt, PostDrStmt, PreIrStmt, PostIrStmt, PushStmt, PopStmt,
    DrStopStmt, IrStopStmt, FrequencyStmt,
)


# --- Expression parser tests ---

def test_expr_integer_literal():
    e = parse_expr("42")
    assert isinstance(e, IntLiteral)
    assert e.value == 42


def test_expr_variable():
    e = parse_expr("my_var")
    assert isinstance(e, VarRef)
    assert e.name == "my_var"


def test_expr_binary_op():
    e = parse_expr("a + b")
    assert isinstance(e, BinOp)
    assert e.op == '+'
    assert isinstance(e.left, VarRef)
    assert isinstance(e.right, VarRef)


def test_expr_precedence():
    e = parse_expr("a + b * c")
    assert isinstance(e, BinOp)
    assert e.op == '+'
    assert isinstance(e.right, BinOp)
    assert e.right.op == '*'


def test_expr_parens():
    e = parse_expr("(a + b) * c")
    assert isinstance(e, BinOp)
    assert e.op == '*'
    assert isinstance(e.left, BinOp)
    assert e.left.op == '+'


def test_expr_unary_minus():
    e = parse_expr("-x")
    assert isinstance(e, UnaryOp)
    assert e.op == '-'


def test_expr_unary_not():
    e = parse_expr("!x")
    assert isinstance(e, UnaryOp)
    assert e.op == '!'


def test_expr_bitwise_not():
    e = parse_expr("~x")
    assert isinstance(e, UnaryOp)
    assert e.op == '~'


def test_expr_comparison():
    e = parse_expr("a == b")
    assert isinstance(e, BinOp)
    assert e.op == '=='


def test_expr_array_index():
    e = parse_expr("arr[5]")
    assert isinstance(e, ArrayIndex)
    assert e.name == "arr"
    assert isinstance(e.index, IntLiteral)
    assert e.index.value == 5


def test_expr_array_subrange():
    e = parse_expr("data[31..0]")
    assert isinstance(e, ArraySubrange)
    assert e.name == "data"


def test_expr_array_whole():
    e = parse_expr("data[]")
    assert isinstance(e, ArrayWhole)
    assert e.name == "data"


def test_expr_function_abs():
    e = parse_expr("ABS(x)")
    assert isinstance(e, FuncCall)
    assert e.name == "ABS"


def test_expr_function_int():
    e = parse_expr("INT(boolvar)")
    assert isinstance(e, FuncCall)
    assert e.name == "INT"


def test_expr_boolean_binary():
    e = parse_expr("#101")
    assert isinstance(e, BooleanLiteral)
    assert e.bit_count == 3


def test_expr_boolean_hex():
    e = parse_expr("$FF")
    assert isinstance(e, BooleanLiteral)
    assert e.bit_count == 8


def test_expr_complex():
    e = parse_expr("(a + 1) * (b - 2) / c")
    assert isinstance(e, BinOp)


def test_expr_shift():
    e = parse_expr("x << 3")
    assert isinstance(e, BinOp)
    assert e.op == '<<'


def test_expr_logical():
    e = parse_expr("a && b || c")
    # || has lower precedence than &&
    assert isinstance(e, BinOp)
    assert e.op == '||'
    assert isinstance(e.left, BinOp)
    assert e.left.op == '&&'


# --- Full parser tests ---

def _parse_source(source):
    """Helper: tokenize and parse STAPL source."""
    stmts = tokenize(source, check_crc=False)
    return parse(stmts)


def test_parse_notes():
    prog = _parse_source('NOTE "DEVICE" "EPM7128";\nNOTE "DATE" "2024/01/01";\nCRC 0000;\n')
    assert len(prog.notes) == 2
    assert prog.notes[0].key == "DEVICE"
    assert prog.notes[0].value == "EPM7128"


def test_parse_action():
    source = '''
    NOTE "DEVICE" "test";
    ACTION PROGRAM "Program device" =
        DO_ENTER,
        DO_PROGRAM,
        DO_VERIFY RECOMMENDED,
        DO_EXIT;
    CRC 0000;
    '''
    prog = _parse_source(source)
    assert "PROGRAM" in prog.actions
    action = prog.actions["PROGRAM"]
    assert action.description == "Program device"
    assert len(action.procedures) == 4
    assert action.procedures[2] == ("DO_VERIFY", "RECOMMENDED")


def test_parse_procedure():
    source = '''
    NOTE "DEVICE" "test";
    ACTION TEST = DO_TEST;
    PROCEDURE DO_TEST;
        INTEGER x = 0;
        x = x + 1;
    ENDPROC;
    CRC 0000;
    '''
    prog = _parse_source(source)
    assert "DO_TEST" in prog.procedures
    proc = prog.procedures["DO_TEST"]
    assert len(proc.statements) == 2
    assert isinstance(proc.statements[0], IntegerDecl)
    assert isinstance(proc.statements[1], AssignStmt)


def test_parse_procedure_with_labels():
    source = '''
    NOTE "DEVICE" "test";
    ACTION TEST = DO_TEST;
    PROCEDURE DO_TEST;
        INTEGER x = 0;
        loop: x = x + 1;
        IF x < 10 THEN GOTO loop;
    ENDPROC;
    CRC 0000;
    '''
    prog = _parse_source(source)
    proc = prog.procedures["DO_TEST"]
    assert "LOOP" in proc.labels
    assert proc.labels["LOOP"] == 1  # points to "x = x + 1" statement


def test_parse_data_block():
    source = '''
    NOTE "DEVICE" "test";
    ACTION TEST = DO_TEST;
    PROCEDURE DO_TEST USES my_data;
        ENDPROC;
    DATA my_data;
        INTEGER count = 10;
        BOOLEAN flags[8] = $FF;
    ENDDATA;
    CRC 0000;
    '''
    prog = _parse_source(source)
    assert "MY_DATA" in prog.data_blocks
    data = prog.data_blocks["MY_DATA"]
    assert len(data.statements) == 2
    assert isinstance(data.statements[0], IntegerDecl)
    assert isinstance(data.statements[1], BooleanDecl)


def test_parse_integer_decl_scalar():
    source = '''
    NOTE "DEVICE" "test";
    ACTION T = P;
    PROCEDURE P;
        INTEGER x;
        INTEGER y = 42;
        INTEGER z = -1;
    ENDPROC;
    CRC 0000;
    '''
    prog = _parse_source(source)
    stmts = prog.procedures["P"].statements
    assert stmts[0].name == "x" and stmts[0].init is None
    assert stmts[1].name == "y"
    assert isinstance(stmts[1].init[0], IntLiteral)
    assert stmts[1].init[0].value == 42


def test_parse_integer_decl_array():
    source = '''
    NOTE "DEVICE" "test";
    ACTION T = P;
    PROCEDURE P;
        INTEGER arr[3] = 10, 20, 30;
    ENDPROC;
    CRC 0000;
    '''
    prog = _parse_source(source)
    decl = prog.procedures["P"].statements[0]
    assert isinstance(decl, IntegerDecl)
    assert len(decl.init) == 3


def test_parse_boolean_decl():
    source = '''
    NOTE "DEVICE" "test";
    ACTION T = P;
    PROCEDURE P;
        BOOLEAN flag;
        BOOLEAN data[8] = $AB;
        BOOLEAN bits[4] = #1010;
    ENDPROC;
    CRC 0000;
    '''
    prog = _parse_source(source)
    stmts = prog.procedures["P"].statements
    assert stmts[0].name == "flag" and stmts[0].init is None
    assert isinstance(stmts[1].init, BooleanLiteral)
    assert isinstance(stmts[2].init, BooleanLiteral)


def test_parse_for_loop():
    source = '''
    NOTE "DEVICE" "test";
    ACTION T = P;
    PROCEDURE P;
        INTEGER i;
        FOR i = 0 TO 10 STEP 2;
        NEXT i;
    ENDPROC;
    CRC 0000;
    '''
    prog = _parse_source(source)
    stmts = prog.procedures["P"].statements
    assert isinstance(stmts[1], ForStmt)
    assert stmts[1].var == "i"
    assert isinstance(stmts[1].step, IntLiteral)
    assert stmts[1].step.value == 2
    assert isinstance(stmts[2], NextStmt)


def test_parse_drscan():
    source = '''
    NOTE "DEVICE" "test";
    ACTION T = P;
    PROCEDURE P;
        BOOLEAN tdi[10] = $3FF;
        BOOLEAN tdo[10];
        DRSCAN 10, tdi[9..0], CAPTURE tdo[9..0];
    ENDPROC;
    CRC 0000;
    '''
    prog = _parse_source(source)
    scan = prog.procedures["P"].statements[2]
    assert isinstance(scan, DrScanStmt)
    assert scan.capture is not None


def test_parse_wait():
    source = '''
    NOTE "DEVICE" "test";
    ACTION T = P;
    PROCEDURE P;
        WAIT 10 CYCLES, 1000 USEC;
    ENDPROC;
    CRC 0000;
    '''
    prog = _parse_source(source)
    wait = prog.procedures["P"].statements[0]
    assert isinstance(wait, WaitStmt)
    assert wait.cycles is not None
    assert wait.usecs is not None


def test_parse_state():
    source = '''
    NOTE "DEVICE" "test";
    ACTION T = P;
    PROCEDURE P;
        STATE RESET;
        STATE IREXIT2 IRSHIFT IREXIT1 IRUPDATE IDLE;
    ENDPROC;
    CRC 0000;
    '''
    prog = _parse_source(source)
    stmts = prog.procedures["P"].statements
    assert isinstance(stmts[0], StateStmt)
    assert stmts[0].path == ["RESET"]
    assert len(stmts[1].path) == 5


def test_parse_if_goto():
    source = '''
    NOTE "DEVICE" "test";
    ACTION T = P;
    PROCEDURE P;
        INTEGER x = 0;
        IF x == 0 THEN GOTO done;
        done: EXIT 0;
    ENDPROC;
    CRC 0000;
    '''
    prog = _parse_source(source)
    stmts = prog.procedures["P"].statements
    assert isinstance(stmts[1], IfStmt)
    assert isinstance(stmts[1].then_stmt, GotoStmt)


def test_parse_push_pop():
    source = '''
    NOTE "DEVICE" "test";
    ACTION T = P;
    PROCEDURE P;
        INTEGER x;
        PUSH 42;
        POP x;
    ENDPROC;
    CRC 0000;
    '''
    prog = _parse_source(source)
    stmts = prog.procedures["P"].statements
    assert isinstance(stmts[1], PushStmt)
    assert isinstance(stmts[2], PopStmt)


def test_parse_export():
    source = '''
    NOTE "DEVICE" "test";
    ACTION T = P;
    PROCEDURE P;
        EXPORT "PERCENT_DONE", 50;
    ENDPROC;
    CRC 0000;
    '''
    prog = _parse_source(source)
    stmt = prog.procedures["P"].statements[0]
    assert isinstance(stmt, ExportStmt)
    assert stmt.key == "PERCENT_DONE"


def test_parse_print():
    source = '''
    NOTE "DEVICE" "test";
    ACTION T = P;
    PROCEDURE P;
        INTEGER x = 5;
        PRINT "Value is ", x;
    ENDPROC;
    CRC 0000;
    '''
    prog = _parse_source(source)
    stmt = prog.procedures["P"].statements[1]
    assert isinstance(stmt, PrintStmt)
    assert len(stmt.parts) == 2
    assert stmt.parts[0] == "Value is "


def test_parse_pre_post():
    source = '''
    NOTE "DEVICE" "test";
    ACTION T = P;
    PROCEDURE P;
        PREDR 10;
        POSTDR 5, #11111;
        PREIR 8;
        POSTIR 12;
    ENDPROC;
    CRC 0000;
    '''
    prog = _parse_source(source)
    stmts = prog.procedures["P"].statements
    assert isinstance(stmts[0], PreDrStmt)
    assert isinstance(stmts[1], PostDrStmt)
    assert stmts[1].data is not None
    assert isinstance(stmts[2], PreIrStmt)
    assert isinstance(stmts[3], PostIrStmt)
