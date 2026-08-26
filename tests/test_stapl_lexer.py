import pytest
from acrobe.stapl.lexer import tokenize, StaplSyntaxError, StaplCrcError


def test_simple_statements():
    source = "NOTE \"DEVICE\" \"test\";\nNOTE \"DATE\" \"2024/01/01\";\nCRC 0000;\n"
    stmts = tokenize(source)
    assert len(stmts) == 3
    assert stmts[0].text == 'NOTE "DEVICE" "test"'
    assert stmts[1].text == 'NOTE "DATE" "2024/01/01"'
    assert stmts[2].text == 'CRC 0000'


def test_comments_stripped():
    source = "INTEGER x = 5; ' this is a comment\nINTEGER y = 10;\nCRC 0000;\n"
    stmts = tokenize(source)
    assert len(stmts) == 3
    assert stmts[0].text == 'INTEGER x = 5'
    assert stmts[1].text == 'INTEGER y = 10'


def test_multiline_statement():
    source = "ACTION PROGRAM =\n  DO_ENTER,\n  DO_PROGRAM;\nCRC 0000;\n"
    stmts = tokenize(source)
    assert len(stmts) == 2
    assert stmts[0].text == 'ACTION PROGRAM = DO_ENTER, DO_PROGRAM'
    assert stmts[0].line == 1


def test_whitespace_normalization():
    source = "INTEGER   x   =   5;\nCRC 0000;\n"
    stmts = tokenize(source)
    assert stmts[0].text == 'INTEGER x = 5'


def test_string_literals_preserved():
    source = 'NOTE "DEVICE" "My   Device";\nCRC 0000;\n'
    stmts = tokenize(source)
    assert stmts[0].text == 'NOTE "DEVICE" "My   Device"'


def test_line_tracking():
    source = "INTEGER x;\n\n\nINTEGER y;\nCRC 0000;\n"
    stmts = tokenize(source)
    assert stmts[0].line == 1
    assert stmts[1].line == 4


def test_unterminated_string():
    with pytest.raises(StaplSyntaxError, match="Unterminated string"):
        tokenize('NOTE "DEVICE;\nCRC 0000;\n')


def test_unterminated_statement():
    with pytest.raises(StaplSyntaxError, match="Unterminated statement"):
        tokenize('INTEGER x = 5')


def test_missing_crc():
    with pytest.raises(StaplSyntaxError, match="Missing mandatory CRC"):
        tokenize('INTEGER x = 5;\n')


def test_crc_zero_skips_check():
    source = "INTEGER x = 5;\nCRC 0000;\n"
    stmts = tokenize(source)
    assert len(stmts) == 2


def test_crc_verification():
    # Build a source, compute its CRC, then verify
    from acrobe.stapl.lexer import _compute_crc
    body = 'NOTE "DEVICE" "test";\n'
    crc_val = _compute_crc(body)
    source = f'{body}CRC {crc_val:04X};\n'
    stmts = tokenize(source)
    assert len(stmts) == 2


def test_crc_mismatch():
    source = 'NOTE "DEVICE" "test";\nCRC DEAD;\n'
    with pytest.raises(StaplCrcError, match="CRC mismatch"):
        tokenize(source)


def test_skip_crc_check():
    source = 'NOTE "DEVICE" "test";\nCRC DEAD;\n'
    stmts = tokenize(source, check_crc=False)
    assert len(stmts) == 2


def test_label_in_statement():
    source = "PROCEDURE test;\nstart: INTEGER x = 0;\nENDPROC;\nCRC 0000;\n"
    stmts = tokenize(source)
    assert stmts[1].text == 'start: INTEGER x = 0'


def test_empty_statements_skipped():
    source = ";;INTEGER x;\n;;CRC 0000;\n"
    stmts = tokenize(source)
    assert len(stmts) == 2
    assert stmts[0].text == 'INTEGER x'


def test_cr_ignored():
    source = "INTEGER x;\r\nCRC 0000;\r\n"
    stmts = tokenize(source)
    assert len(stmts) == 2
    assert stmts[0].text == 'INTEGER x'


def test_comment_at_start_of_line():
    source = "' Full line comment\nINTEGER x;\nCRC 0000;\n"
    stmts = tokenize(source)
    assert len(stmts) == 2
    assert stmts[0].text == 'INTEGER x'
