"""FsWatcher — bridge watchdog.Observer to the event bus.

Standalone (not a Node), single owner per directory tree. Emits
canonical FS events on the global bus with absolute, symlink-
resolved paths so subscribers using the same canonical form
match reliably.

Path canonicalisation is mandatory because watchdog itself never
resolves symlinks — events fire under the real directory, never
under symlink aliases. The watcher canonicalises its `base_dir`
at construction and every emitted `source` is canonical.

Per-`(path, action)` debounce coalesces editor atomic-rename
bursts and multi-event saves into one bus emit per logical
change. Default 100 ms; tunable per instance.
"""

import asyncio
import logging

from watchdog.events import FileSystemEventHandler

from ..node import Path
from .bus import get_bus
from .event import Event


_LOG = logging.getLogger("acrobe.event.fs")


class FsWatcher:
    """Watch a directory and forward file events to the bus.

    `start()` spins up a background watchdog Observer thread.
    `stop()` joins it cleanly. Subscribers don't need to know
    `FsWatcher` exists — they subscribe to the bus by path.
    """

    def __init__(self, base_dir, *,
                 recursive: bool = True,
                 debounce_ms: int = 100,
                 observer_class=None):
        """Construct a watcher rooted at `base_dir`.

        `recursive` — watch subdirectories too.
        `debounce_ms` — coalesce repeated events on the same
            `(path, action)` arriving within this window.
        `observer_class` — for tests: pass `PollingObserver` for
            deterministic behaviour without OS-level event-source
            latency. Default `None` uses watchdog's platform pick
            (FSEvents on macOS, inotify on Linux, …).
        """
        self.base_dir: str = Path.canonicalize_fs(base_dir)
        self.recursive = recursive
        self.debounce_ms = debounce_ms
        self.__observer_class = observer_class
        self.__observer = None
        self.__loop: asyncio.AbstractEventLoop | None = None
        # Latest pending properties per (canonical_path, action)
        # key — coalesced, most recent wins.
        self.__pending: dict[tuple[str, str], dict] = {}
        self.__timers: dict[tuple[str, str], asyncio.TimerHandle] = {}

    async def start(self) -> None:
        if self.__observer is not None:
            return
        self.__loop = asyncio.get_running_loop()
        if self.__observer_class is None:
            from watchdog.observers import Observer
            observer_class = Observer
        else:
            observer_class = self.__observer_class
        handler = _Handler(self)
        self.__observer = observer_class()
        self.__observer.schedule(handler, self.base_dir,
                                 recursive=self.recursive)
        self.__observer.start()

    async def stop(self) -> None:
        if self.__observer is not None:
            self.__observer.stop()
            # join() blocks; offload to a thread so we don't
            # stall the event loop while watchdog winds down.
            await asyncio.to_thread(self.__observer.join, 5.0)
            self.__observer = None
        for handle in self.__timers.values():
            handle.cancel()
        self.__timers.clear()
        self.__pending.clear()

    def enter_event(self, src_path: str, action: str,
                    properties: dict) -> None:
        """Hand-off point from the observer thread to the event
        loop. Safe to call from any thread; uses
        call_soon_threadsafe to cross the boundary.

        Public so the handler class can call it, but not part of
        the user-facing surface — callers normally don't invoke
        this directly."""
        if self.__loop is None:
            return
        try:
            self.__loop.call_soon_threadsafe(
                self.__schedule_emit, src_path, action, properties)
        except RuntimeError:
            # Loop already closed; drop silently.
            pass

    def __schedule_emit(self, src_path: str, action: str,
                        properties: dict) -> None:
        """Coalesce by (path, action) with a debounce timer."""
        key = (src_path, action)
        self.__pending[key] = properties
        prior = self.__timers.get(key)
        if prior is not None:
            prior.cancel()
        self.__timers[key] = self.__loop.call_later(
            self.debounce_ms / 1000.0, self.__fire, key)

    def __fire(self, key: tuple[str, str]) -> None:
        self.__timers.pop(key, None)
        properties = self.__pending.pop(key, None)
        if properties is None:
            return
        src_path, action = key
        asyncio.create_task(get_bus().emit(Event(
            source=src_path, action=action, phase=None,
            properties=properties)))


class _Handler(FileSystemEventHandler):
    """Bridges watchdog file events into FsWatcher.enter_event.

    All paths are canonicalised to absolute symlink-resolved form
    before handoff. Directory `modified` events are dropped (every
    file create bumps the parent dir's mtime — noise)."""

    def __init__(self, watcher: FsWatcher):
        super().__init__()
        self.watcher = watcher

    @staticmethod
    def __canonical(path) -> str:
        return Path.canonicalize_fs(path)

    def on_created(self, event):
        self.watcher.enter_event(
            self.__canonical(event.src_path),
            "created",
            {"is_dir": event.is_directory})

    def on_deleted(self, event):
        self.watcher.enter_event(
            self.__canonical(event.src_path),
            "deleted",
            {"is_dir": event.is_directory})

    def on_modified(self, event):
        if event.is_directory:
            return
        self.watcher.enter_event(
            self.__canonical(event.src_path),
            "changed",
            {})

    def on_moved(self, event):
        self.watcher.enter_event(
            self.__canonical(event.dest_path),
            "moved",
            {"from": self.__canonical(event.src_path),
             "is_dir": event.is_directory})
