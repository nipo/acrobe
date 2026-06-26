"""Logging infrastructure for acrobe.

Custom log levels, domain filtering, colored formatting,
and progress reporting.

Custom levels (in addition to standard WARNING=30, INFO=20, DEBUG=10):
    NOTE     = 25  Important operational messages (e.g. "Found device X")
    TRACE    = 15  Detailed tracing (register reads, state changes)
    PROTOCOL = 5   Raw protocol-level data (bit patterns, wire traffic)

Verbosity stepping for CLI (-v increases verbosity):
    default  WARNING   only warnings and errors
    -v       NOTE      important operational messages
    -vv      INFO      general information
    -vvv     TRACE     detailed tracing
    -vvvv    DEBUG     debug info
    -vvvvv   PROTOCOL  raw protocol data
"""

import logging
import re
import time
from contextlib import contextmanager
from typing import Protocol, runtime_checkable


# --- Custom Log Levels ---

NOTE = 25
TRACE = 15
PROTOCOL = 5

logging.addLevelName(NOTE, 'NOTE')
logging.addLevelName(TRACE, 'TRACE')
logging.addLevelName(PROTOCOL, 'PROTOCOL')

# Verbosity stepping order (most restrictive first)
LEVELS = [
    logging.CRITICAL,
    logging.ERROR,
    logging.WARNING,
    NOTE,
    logging.INFO,
    TRACE,
    logging.DEBUG,
    PROTOCOL,
]

# ANSI color codes per level name
LEVEL_COLORS = {
    'CRITICAL': '91',   # bright red
    'ERROR': '31',      # red
    'WARNING': '33',    # yellow
    'NOTE': '32',       # green
    'INFO': '97',       # bright white
    'TRACE': '96',      # bright cyan
    'DEBUG': '94',      # bright blue
    'PROTOCOL': '36',   # cyan
}


# --- Custom Logger ---

class CrobeLogger(logging.Logger):
    """Logger with crobe-specific level methods and timed() helper."""

    def note(self, msg, *args, **kwargs):
        if self.isEnabledFor(NOTE):
            self._log(NOTE, msg, args, **kwargs)

    def trace(self, msg, *args, **kwargs):
        if self.isEnabledFor(TRACE):
            self._log(TRACE, msg, args, **kwargs)

    def protocol(self, msg, *args, **kwargs):
        if self.isEnabledFor(PROTOCOL):
            self._log(PROTOCOL, msg, args, **kwargs)

    @contextmanager
    def timed(self, message):
        """Context manager that logs duration of a code block at TRACE level."""
        self.trace("Starting %s...", message)
        t0 = time.monotonic()
        try:
            yield
        except Exception:
            self.trace("Error in %s, took %.3f s", message, time.monotonic() - t0)
            raise
        else:
            self.trace("Done %s, took %.3f s", message, time.monotonic() - t0)


logging.setLoggerClass(CrobeLogger)


# --- Filtering ---

class DomainFilter(logging.Filter):
    """Filter log records by logger name patterns.

    Args:
        silent: logger names to suppress entirely.
        silent_re: regex - matching logger names are suppressed.
        only_re: regex - only matching logger names pass through.
    """

    def __init__(self, *, silent=(), silent_re=None, only_re=None):
        super().__init__()
        self._silent = set(silent)
        self._silent_re = re.compile(silent_re) if silent_re else None
        self._only_re = re.compile(only_re) if only_re else None

    def filter(self, record):
        name = record.name
        if name in self._silent:
            return False
        if self._silent_re and self._silent_re.search(name):
            return False
        if self._only_re:
            return bool(self._only_re.search(name))
        return True


# --- Formatting ---

class Formatter(logging.Formatter):
    """Log formatter with optional ANSI colors and timestamps."""

    def __init__(self, *, color=True, timestamp=False, short_name=False):
        super().__init__()
        self._color = color
        self._timestamp = timestamp
        self._short_name = short_name

    def format(self, record):
        msg = record.getMessage()

        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            msg = f"{msg}\n{record.exc_text}"
        if record.stack_info:
            msg = f"{msg}\n{record.stack_info}"

        name = record.name
        if self._short_name:
            name = name.rsplit(".", 1)[-1]

        if self._timestamp:
            elapsed = record.relativeCreated / 1000.0
            prefix = f"[{elapsed:8.3f}] "
        else:
            prefix = ""

        if self._color:
            color = LEVEL_COLORS.get(record.levelname, '0')
            return f"{prefix}{name}: \x1b[{color}m{msg}\x1b[m"
        else:
            return f"{prefix}{name}: {msg}"


# --- Progress ---

@runtime_checkable
class ProgressHandle(Protocol):
    """Handle for an active progress operation."""
    def advance(self, count: int = 1) -> None: ...
    def close(self) -> None: ...


@runtime_checkable
class ProgressDelegate(Protocol):
    """Factory for progress handles."""
    def create(self, name: str, label: str, total: int, unit: str = "") -> ProgressHandle: ...


class _NullHandle:
    def advance(self, count=1):
        pass

    def close(self):
        pass


class NullProgress:
    """Progress delegate that discards all updates."""
    def create(self, name, label, total, unit=""):
        return _NullHandle()


class _TextHandle:
    def __init__(self, logger, label, total, unit):
        self._logger = logger
        self._label = label
        self._total = total
        self._unit = unit or "items"
        self._current = 0
        self._logger.trace("%s: 0/%d %s", label, total, self._unit)

    def advance(self, count=1):
        self._current += count
        self._logger.trace(
            "%s: %d/%d %s",
            self._label, self._current, self._total, self._unit)

    def close(self):
        self._logger.trace("%s: done", self._label)


class TextProgress:
    """Progress delegate that logs updates as TRACE messages."""
    def create(self, name, label, total, unit=""):
        logger = logging.getLogger(name)
        return _TextHandle(logger, label, total, unit)


class _TqdmHandle:
    def __init__(self, label, total, unit):
        import tqdm
        self._bar = tqdm.tqdm(total=total, desc=label, unit=unit or "it")

    def advance(self, count=1):
        self._bar.update(count)

    def close(self):
        self._bar.close()


class TqdmProgress:
    """Progress delegate using tqdm for visual progress bars."""
    def create(self, name, label, total, unit=""):
        return _TqdmHandle(label, total, unit)


# Module-level progress delegate
_progress_delegate: ProgressDelegate = NullProgress()


def set_progress(delegate: ProgressDelegate):
    """Set the global progress delegate."""
    global _progress_delegate
    _progress_delegate = delegate


def get_progress() -> ProgressDelegate:
    """Get the current global progress delegate."""
    return _progress_delegate


# --- Setup ---

_ACROBE_HANDLER_MARKER = "_acrobe_cli_handler"


def setup(*, level=logging.WARNING, color=True, timestamp=False,
          silent=(), silent_re=None, only_re=None,
          progress=None, stream=None, short_name=False):
    """Configure crobe logging with a StreamHandler.

    Convenience function for CLI setup. Creates and attaches a
    StreamHandler with Formatter and DomainFilter to the root logger.

    Idempotent: when called multiple times (e.g. once per chained
    CLI segment, since each segment re-enters the cli group's
    async body), any previously-installed acrobe handler is
    removed first. Without this, chained commands produce N
    duplicate log lines.

    Args:
        level: logging level (use LEVELS list for verbosity stepping)
        color: enable ANSI color output
        timestamp: show relative timestamps
        silent: logger names to suppress
        silent_re: regex pattern for names to suppress
        only_re: regex pattern - only matching names shown
        progress: ProgressDelegate instance (default: NullProgress)
        stream: output stream (default: stderr)
        short_name: print short name prefixes
    """
    root = logging.getLogger()
    # Drop any handler we installed in a previous setup() call.
    for h in list(root.handlers):
        if getattr(h, _ACROBE_HANDLER_MARKER, False):
            root.removeHandler(h)

    handler = logging.StreamHandler(stream)
    handler.setFormatter(Formatter(color=color, timestamp=timestamp, short_name=short_name))
    handler.addFilter(DomainFilter(
        silent=silent, silent_re=silent_re, only_re=only_re))
    setattr(handler, _ACROBE_HANDLER_MARKER, True)

    root.addHandler(handler)
    root.setLevel(level)

    if progress is not None:
        set_progress(progress)
