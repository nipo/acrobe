"""Tests for FsWatcher (Slice 5)."""

import asyncio
import os
from pathlib import Path as PyPath

import pytest
from watchdog.observers.polling import PollingObserver

from acrobe.event import Event, FsWatcher, get_bus, reset_for_tests
from acrobe.node import Path


# PollingObserver is used throughout — deterministic without
# relying on FSEvents / inotify event-source latency.
POLL_INTERVAL = 0.05
# Generous enough to let PollingObserver tick + debounce fire +
# bus dispatch complete. Keep small so tests stay fast.
SETTLE = 0.4
DEBOUNCE_MS = 30


@pytest.fixture(autouse=True)
def isolated_bus():
    reset_for_tests()
    yield
    reset_for_tests()


@pytest.fixture
async def watcher_factory():
    """Yields a builder for FsWatchers; tears them all down at
    the end so tests don't leak observer threads."""
    watchers: list[FsWatcher] = []

    def build(base_dir, **kwargs):
        kwargs.setdefault("observer_class",
                          lambda: PollingObserver(timeout=POLL_INTERVAL))
        kwargs.setdefault("debounce_ms", DEBOUNCE_MS)
        w = FsWatcher(base_dir, **kwargs)
        watchers.append(w)
        return w

    yield build

    for w in watchers:
        await w.stop()


def collect_events(action=None):
    """Subscribe and return a list that fills in as events fire."""
    events: list[Event] = []
    get_bus().subscribe(lambda e: events.append(e), action=action)
    return events


async def settle():
    await asyncio.sleep(SETTLE)


class TestBasicEvents:
    @pytest.mark.asyncio
    async def test_create_file_fires_created(self, tmp_path: PyPath,
                                              watcher_factory):
        watcher = watcher_factory(tmp_path)
        events = collect_events(action="created")
        await watcher.start()

        (tmp_path / "fw.elf").write_bytes(b"")
        await settle()

        matched = [e for e in events
                   if e.source == Path.canonicalize_fs(tmp_path / "fw.elf")]
        assert len(matched) >= 1
        assert matched[0].phase is None
        assert matched[0].properties == {"is_dir": False}

    @pytest.mark.asyncio
    async def test_modify_file_fires_changed(self, tmp_path: PyPath,
                                              watcher_factory):
        (tmp_path / "fw.elf").write_bytes(b"initial")
        watcher = watcher_factory(tmp_path)
        events = collect_events(action="changed")
        await watcher.start()

        (tmp_path / "fw.elf").write_bytes(b"new content longer")
        await settle()

        matched = [e for e in events
                   if e.source == Path.canonicalize_fs(tmp_path / "fw.elf")]
        assert len(matched) >= 1
        assert matched[0].phase is None

    @pytest.mark.asyncio
    async def test_delete_file_fires_deleted(self, tmp_path: PyPath,
                                              watcher_factory):
        f = tmp_path / "fw.elf"
        f.write_bytes(b"")
        watcher = watcher_factory(tmp_path)
        events = collect_events(action="deleted")
        await watcher.start()

        f.unlink()
        await settle()

        matched = [e for e in events
                   if e.source == Path.canonicalize_fs(f)]
        assert len(matched) >= 1
        assert matched[0].properties.get("is_dir") is False

    @pytest.mark.asyncio
    async def test_rename_fires_moved(self, tmp_path: PyPath,
                                       watcher_factory):
        src = tmp_path / "old.elf"
        dest = tmp_path / "new.elf"
        src.write_bytes(b"")
        watcher = watcher_factory(tmp_path)
        events = collect_events(action="moved")
        await watcher.start()

        src.rename(dest)
        await settle()

        matched = [e for e in events
                   if e.source == Path.canonicalize_fs(dest)]
        assert len(matched) >= 1
        assert matched[0].properties["from"] == Path.canonicalize_fs(src)

    @pytest.mark.asyncio
    async def test_directory_modified_is_silent(self, tmp_path: PyPath,
                                                  watcher_factory):
        # Creating a file inside a watched dir bumps the parent's
        # mtime — we drop that to avoid noise. Only the file's
        # `created` event should fire.
        watcher = watcher_factory(tmp_path)
        changed_events = collect_events(action="changed")
        await watcher.start()

        (tmp_path / "x").write_bytes(b"")
        await settle()

        # No directory-level `changed` events (only file-level
        # `created`/`changed` for the new file itself, which would
        # NOT match action="changed" because file is new and the
        # watchdog backend fires `created`, not `changed`, on a
        # first write).
        dir_events = [e for e in changed_events
                      if e.properties.get("is_dir")]
        assert dir_events == []


class TestCanonicalization:
    @pytest.mark.asyncio
    async def test_base_dir_canonicalised(self, tmp_path: PyPath,
                                            watcher_factory):
        # Pass a symlinked path as base_dir; watcher stores the
        # canonical form.
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)

        watcher = watcher_factory(link)
        assert watcher.base_dir == Path.canonicalize_fs(real)

    @pytest.mark.asyncio
    async def test_symlinked_dir_emits_under_real_path(
            self, tmp_path: PyPath, watcher_factory):
        # User passes /a/b/d/foo where d -> ../c, foo absent.
        # Events should emit under /a/b/c/foo (canonical), not
        # /a/b/d/foo. Without canonicalisation, subscribers
        # listening on the canonical path would never match.
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)

        watcher = watcher_factory(link)
        events = collect_events(action="created")
        await watcher.start()

        # Touch the file through the SYMLINK path.
        (link / "fw.elf").write_bytes(b"")
        await settle()

        canonical = Path.canonicalize_fs(real / "fw.elf")
        matched = [e for e in events if e.source == canonical]
        assert len(matched) >= 1


class TestCoalescing:
    @pytest.mark.asyncio
    async def test_multiple_writes_collapse_to_one_changed(
            self, tmp_path: PyPath, watcher_factory):
        f = tmp_path / "fw.elf"
        f.write_bytes(b"v0")
        # Slightly larger debounce so the burst gets coalesced.
        watcher = watcher_factory(tmp_path, debounce_ms=150)
        events = collect_events(action="changed")
        await watcher.start()

        # Three quick writes within the debounce window.
        f.write_bytes(b"v1")
        await asyncio.sleep(0.02)
        f.write_bytes(b"v2 longer")
        await asyncio.sleep(0.02)
        f.write_bytes(b"v3 longer still")
        # Wait for debounce + dispatch.
        await asyncio.sleep(0.5)

        matched = [e for e in events
                   if e.source == Path.canonicalize_fs(f)]
        # Coalesce to one (or at most a couple if the poll catches
        # them in distinct cycles outside the debounce window).
        assert 1 <= len(matched) <= 2

    @pytest.mark.asyncio
    async def test_different_actions_dont_coalesce(
            self, tmp_path: PyPath, watcher_factory):
        # Created and deleted on the same path are distinct keys
        # in the debounce map; both fire even if close together.
        watcher = watcher_factory(tmp_path)
        events = collect_events()
        await watcher.start()

        f = tmp_path / "fw.elf"
        f.write_bytes(b"")
        await asyncio.sleep(0.15)
        f.unlink()
        await settle()

        actions = [e.action for e in events
                   if e.source == Path.canonicalize_fs(f)]
        assert "created" in actions
        assert "deleted" in actions


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_stop_is_idempotent(self, tmp_path: PyPath,
                                        watcher_factory):
        watcher = watcher_factory(tmp_path)
        await watcher.start()
        await watcher.stop()
        await watcher.stop()  # should not raise

    @pytest.mark.asyncio
    async def test_stop_without_start(self, tmp_path: PyPath,
                                        watcher_factory):
        watcher = watcher_factory(tmp_path)
        await watcher.stop()  # should not raise

    @pytest.mark.asyncio
    async def test_no_events_after_stop(self, tmp_path: PyPath,
                                          watcher_factory):
        watcher = watcher_factory(tmp_path)
        await watcher.start()
        await watcher.stop()

        events = collect_events()
        (tmp_path / "after.elf").write_bytes(b"")
        await settle()

        # Watcher is stopped; no events for the post-stop touch.
        assert events == []

    @pytest.mark.asyncio
    async def test_start_twice_is_idempotent(self, tmp_path: PyPath,
                                               watcher_factory):
        watcher = watcher_factory(tmp_path)
        await watcher.start()
        await watcher.start()  # should not raise / not duplicate
        events = collect_events(action="created")
        (tmp_path / "f.elf").write_bytes(b"")
        await settle()
        # One observer means events fire once, not duplicated.
        canonical = Path.canonicalize_fs(tmp_path / "f.elf")
        matched = [e for e in events if e.source == canonical]
        assert len(matched) == 1


class TestSubscriberPattern:
    @pytest.mark.asyncio
    async def test_subscribe_to_specific_file(self, tmp_path: PyPath,
                                                watcher_factory):
        # Real use case: auto-reload subscribes by exact path.
        target = tmp_path / "firmware.elf"
        target.write_bytes(b"v0")
        canonical = Path.canonicalize_fs(target)

        watcher = watcher_factory(tmp_path)
        events: list[Event] = []
        get_bus().subscribe(
            lambda e: events.append(e),
            action="changed",
            source=canonical, source_match="exact")
        await watcher.start()

        # Modify the watched file.
        target.write_bytes(b"v1 substantially different content")
        # Touch an unrelated file in the same dir.
        (tmp_path / "other.txt").write_bytes(b"unrelated")
        await settle()

        # Only events for the watched file.
        assert all(e.source == canonical for e in events)
        assert len(events) >= 1
