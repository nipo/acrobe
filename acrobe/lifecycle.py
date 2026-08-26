"""Process-wide cleanup registry.

Modules holding background resources (HTTP sessions, USB contexts,
file descriptors that won't get GC'd cleanly, ...) register an
async cleanup callback. `shutdown()` drains them in LIFO order so
a library user's program-level teardown is one call regardless of
which subsystems were exercised.

For things that already have a Node.stop() — most of acrobe — the
node tree's `stop_tree()` cascades naturally; the lifecycle is
the catch-all when stop_tree wasn't called (the CLI's case before
this module existed) or for resources that don't live in the tree.

Resources that have their own deterministic cleanup path
(stop_tree, close, __aexit__) should `register` on creation and
`cancel` when their normal cleanup runs — `shutdown()` then becomes
a true no-op for code that already cleaned up properly.

CLI integration: `acrobe.cli.base.cli`'s result_callback runs
`shutdown()` after every command, so subcommands don't have to
remember.
"""

import logging


_log = logging.getLogger(__name__)


class Lifecycle:
    """An ordered list of async cleanup callables.

    The default global instance lives below; tests can construct
    their own to avoid touching real resources.
    """

    def __init__(self):
        self._cleanups: list = []

    def register(self, callback):
        """Add `callback` (an async callable) to the cleanup queue.

        Returns `callback` unchanged so it can be used as a
        decorator. Duplicate registrations are allowed — they'll all
        run, but the caller is responsible for them being idempotent.
        """
        self._cleanups.append(callback)
        return callback

    def cancel(self, callback):
        """Remove a previously-registered callback. No-op if absent.

        Use this when a resource cleans up through its normal
        path (stop, __aexit__, ...): cancel the registration so
        `shutdown()` doesn't double-clean.
        """
        try:
            self._cleanups.remove(callback)
        except ValueError:
            pass

    def pending(self) -> int:
        """Number of cleanups currently registered. For tests."""
        return len(self._cleanups)

    async def shutdown(self):
        """Drain every registered callback in LIFO order.

        Exceptions are caught and logged so one stuck cleanup
        doesn't block the rest. After shutdown the registry is
        empty — calling again is a no-op until new resources
        register.
        """
        cleanups = self._cleanups[:]
        self._cleanups.clear()
        for callback in reversed(cleanups):
            try:
                await callback()
            except Exception:
                _log.exception("cleanup callback failed: %r", callback)


_default = Lifecycle()


def on_shutdown(callback):
    """Register `callback` with the process-wide Lifecycle. Returns
    the callback (decorator-friendly)."""
    return _default.register(callback)


def cancel_shutdown(callback):
    """Cancel a previously-registered cleanup."""
    _default.cancel(callback)


async def shutdown():
    """Run all registered cleanups."""
    await _default.shutdown()


def lifecycle() -> Lifecycle:
    """Return the process-wide Lifecycle instance. For introspection
    or for tests that want to inspect what's pending."""
    return _default
