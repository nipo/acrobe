"""Tests for acrobe.log — logging infrastructure."""

import io
import logging
import re

import pytest

from acrobe.log import (
    NOTE, TRACE, PROTOCOL, LEVELS,
    CrobeLogger, DomainFilter, Formatter,
    NullProgress, TextProgress,
    set_progress, get_progress,
    setup,
)
from acrobe.node import Node


# --- Fixtures ---

@pytest.fixture(autouse=True)
def _clean_logging():
    """Remove all handlers from root logger after each test."""
    yield
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.WARNING)
    # Reset progress delegate
    set_progress(NullProgress())


def _capture_logger(name, level=PROTOCOL):
    """Create a logger with a StringIO handler, return (logger, stream)."""
    logger = logging.getLogger(name)
    logger.setLevel(level)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s:%(message)s"))
    logger.addHandler(handler)
    logger.propagate = False
    return logger, stream


# --- Custom Levels ---

class TestCustomLevels:
    def test_level_values(self):
        assert NOTE == 25
        assert TRACE == 15
        assert PROTOCOL == 5

    def test_level_names_registered(self):
        assert logging.getLevelName(NOTE) == 'NOTE'
        assert logging.getLevelName(TRACE) == 'TRACE'
        assert logging.getLevelName(PROTOCOL) == 'PROTOCOL'

    def test_levels_strictly_decreasing(self):
        for i in range(len(LEVELS) - 1):
            assert LEVELS[i] > LEVELS[i + 1]

    def test_levels_contains_all_standard_and_custom(self):
        assert logging.CRITICAL in LEVELS
        assert logging.ERROR in LEVELS
        assert logging.WARNING in LEVELS
        assert NOTE in LEVELS
        assert logging.INFO in LEVELS
        assert TRACE in LEVELS
        assert logging.DEBUG in LEVELS
        assert PROTOCOL in LEVELS


# --- CrobeLogger ---

class TestCrobeLogger:
    def test_logger_class_is_crobe_logger(self):
        logger = logging.getLogger("test.crobe_logger_class")
        assert isinstance(logger, CrobeLogger)

    def test_note(self):
        logger, stream = _capture_logger("test.note")
        logger.note("hello %s", "world")
        assert "NOTE:hello world" in stream.getvalue()

    def test_trace(self):
        logger, stream = _capture_logger("test.trace")
        logger.trace("detail %d", 42)
        assert "TRACE:detail 42" in stream.getvalue()

    def test_protocol(self):
        logger, stream = _capture_logger("test.protocol")
        logger.protocol("raw data")
        assert "PROTOCOL:raw data" in stream.getvalue()

    def test_level_filtering(self):
        """Custom levels respect level threshold."""
        logger, stream = _capture_logger("test.filtering", level=NOTE)
        logger.note("visible")
        logger.trace("hidden")
        logger.protocol("hidden")
        output = stream.getvalue()
        assert "visible" in output
        assert "hidden" not in output

    def test_timed_success(self):
        logger, stream = _capture_logger("test.timed")
        with logger.timed("operation"):
            pass
        output = stream.getvalue()
        assert "Starting operation..." in output
        assert "Done operation, took" in output

    def test_timed_error(self):
        logger, stream = _capture_logger("test.timed_err")
        with pytest.raises(ValueError):
            with logger.timed("failing"):
                raise ValueError("boom")
        output = stream.getvalue()
        assert "Starting failing..." in output
        assert "Error in failing, took" in output


# --- DomainFilter ---

class TestDomainFilter:
    def _make_record(self, name):
        record = logging.LogRecord(
            name=name, level=logging.INFO, pathname="", lineno=0,
            msg="test", args=(), exc_info=None)
        return record

    def test_default_allows_all(self):
        f = DomainFilter()
        assert f.filter(self._make_record("any.name"))
        assert f.filter(self._make_record("other"))

    def test_silent_set(self):
        f = DomainFilter(silent=["adapter.ftdi", "spi.flash"])
        assert not f.filter(self._make_record("adapter.ftdi"))
        assert not f.filter(self._make_record("spi.flash"))
        assert f.filter(self._make_record("adapter.jlink"))

    def test_silent_re(self):
        f = DomainFilter(silent_re=r"adapter\.ftdi")
        assert not f.filter(self._make_record("adapter.ftdi"))
        assert not f.filter(self._make_record("adapter.ftdi.mpsse"))
        assert f.filter(self._make_record("adapter.jlink"))

    def test_only_re(self):
        f = DomainFilter(only_re=r"^swd")
        assert f.filter(self._make_record("swd.dp"))
        assert f.filter(self._make_record("swd"))
        assert not f.filter(self._make_record("jtag.chain"))

    def test_silent_overrides_only(self):
        """Silent takes precedence over only_re."""
        f = DomainFilter(silent=["swd.dp"], only_re=r"^swd")
        assert not f.filter(self._make_record("swd.dp"))
        assert f.filter(self._make_record("swd.ap"))

    def test_silent_re_overrides_only(self):
        f = DomainFilter(silent_re=r"\.dp$", only_re=r"^swd")
        assert not f.filter(self._make_record("swd.dp"))
        assert f.filter(self._make_record("swd.ap"))


# --- Formatter ---

class TestFormatter:
    def _format(self, **kwargs):
        fmt = Formatter(**kwargs)
        record = logging.LogRecord(
            name="chain.tap0", level=NOTE, pathname="", lineno=0,
            msg="Found device 0x%08x", args=(0x24001093,), exc_info=None)
        return fmt.format(record)

    def test_plain(self):
        result = self._format(color=False)
        assert result == "chain.tap0: Found device 0x24001093"

    def test_color(self):
        result = self._format(color=True)
        # Should contain ANSI escape for NOTE (green = 32)
        assert "\x1b[32m" in result
        assert "\x1b[m" in result
        assert "chain.tap0:" in result
        assert "Found device 0x24001093" in result

    def test_timestamp(self):
        result = self._format(color=False, timestamp=True)
        # Should start with [   X.XXX]
        assert re.match(r'\[\s+\d+\.\d{3}\] chain\.tap0: ', result)

    def test_exception_included(self):
        fmt = Formatter(color=False)
        try:
            raise RuntimeError("test error")
        except RuntimeError:
            import sys
            record = logging.LogRecord(
                name="test", level=logging.ERROR, pathname="", lineno=0,
                msg="failed", args=(), exc_info=sys.exc_info())
            result = fmt.format(record)
        assert "failed" in result
        assert "RuntimeError: test error" in result


# --- Progress ---

class TestNullProgress:
    def test_null_is_silent(self):
        p = NullProgress()
        handle = p.create("test", "op", 100)
        handle.advance(50)
        handle.close()
        # No error, no output


class TestTextProgress:
    def test_text_logs_trace(self):
        logger, stream = _capture_logger("test.text_progress")
        p = TextProgress()
        handle = p.create("test.text_progress", "erasing", 4, "sectors")
        assert "0/4 sectors" in stream.getvalue()
        handle.advance(1)
        assert "1/4 sectors" in stream.getvalue()
        handle.advance(2)
        assert "3/4 sectors" in stream.getvalue()
        handle.close()
        assert "done" in stream.getvalue()


class TestNodeProgress:
    def test_component_progress_delegates(self):
        logger, stream = _capture_logger("root.child")
        set_progress(TextProgress())

        root = Node("root")
        child = Node("child")
        root.child_add(child)

        with child.progress("flashing", total=10, unit="pages") as p:
            p.advance(5)
            p.advance(5)

        output = stream.getvalue()
        assert "0/10 pages" in output
        assert "5/10 pages" in output
        assert "10/10 pages" in output
        assert "done" in output

    def test_component_progress_null_default(self):
        """Default NullProgress doesn't crash."""
        set_progress(NullProgress())
        c = Node("test")
        with c.progress("op", total=5) as p:
            p.advance(5)


# --- Setup ---

class TestSetup:
    def test_setup_configures_root(self):
        stream = io.StringIO()
        setup(level=NOTE, color=False, stream=stream)

        logger = logging.getLogger("test.setup")
        logger.note("visible note")
        logger.info("hidden info")

        output = stream.getvalue()
        assert "visible note" in output
        assert "hidden info" not in output

    def test_setup_with_filter(self):
        stream = io.StringIO()
        setup(level=PROTOCOL, color=False, stream=stream,
              silent=["noisy"])

        logging.getLogger("noisy").info("suppressed")
        logging.getLogger("quiet").info("visible")

        output = stream.getvalue()
        assert "suppressed" not in output
        assert "visible" in output

    def test_setup_sets_progress(self):
        stream = io.StringIO()
        text_progress = TextProgress()
        setup(level=TRACE, color=False, stream=stream,
              progress=text_progress)

        assert get_progress() is text_progress
