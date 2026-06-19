"""Reload-idempotency + teardown tests for the background scheduler.

Regression coverage for the leak fixed alongside this file: the loader re-execs
the plugin module on every discovery (force_reload), which resets module
globals. The scheduler thread/stop-event therefore live in a reload-stable
sys.modules registry so a re-imported incarnation reuses the running thread
instead of spawning (and orphaning, with its DB connection) a new one.

No Django needed: the scheduler loop's Django imports are lazy and caught, so a
started thread simply parks on its stop event. conftest.py puts the package on
the path.
"""
import sys
import threading
import time

from dispatcharr_sports_filter import plugin


def _live_scheduler_threads():
    return [t for t in threading.enumerate() if t.name == "sports-filter-scheduler"]


def _reset():
    """Stop any running scheduler and clear the reload-stable registry so each
    test starts clean."""
    plugin._stop_scheduler("test teardown")
    sys.modules.pop(plugin._SCHEDULER_REGISTRY_KEY, None)


def test_reload_does_not_respawn_scheduler():
    """A simulated loader force_reload (module re-exec) must reuse the existing
    scheduler thread via the reload-stable registry, not spawn a second one.

    On the pre-fix module-global implementation the re-imported incarnation saw
    a fresh `_SCHEDULER_THREAD = None`, passed the guard, and started a new
    thread — the leak. This asserts the live count stays 1 (it was 2 there)."""
    _reset()
    try:
        plugin._start_scheduler()
        time.sleep(0.3)
        t1 = plugin._scheduler_registry().thread
        assert t1 is not None and t1.is_alive()

        # Drop the plugin's package modules (NOT the synthetic registry key,
        # which the loader's unload-by-path leaves in place) and re-import a
        # fresh incarnation, then start again.
        for mod in [m for m in list(sys.modules)
                    if m.startswith("dispatcharr_sports_filter") and "scheduler_state" not in m]:
            del sys.modules[mod]
        from dispatcharr_sports_filter import plugin as plugin2

        plugin2._start_scheduler()
        assert plugin2._scheduler_registry().thread is t1, \
            "reload spawned a new thread instead of reusing the registry one"
        assert len(_live_scheduler_threads()) == 1, "scheduler thread leaked across reload"
    finally:
        _reset()


def test_stop_reclaims_thread():
    """Plugin.stop() (loader disable/delete) must signal the thread to exit so it
    stops polling Postgres and releases its DB connection, and clear the
    registry."""
    _reset()
    try:
        plugin._start_scheduler()
        time.sleep(0.3)
        assert len(_live_scheduler_threads()) == 1

        plugin._stop_scheduler("test")
        time.sleep(0.3)
        assert len(_live_scheduler_threads()) == 0, "stop() did not reclaim the thread"
        reg = plugin._scheduler_registry()
        assert reg.thread is None and reg.stop_event is None, "stop() did not clear the registry"
    finally:
        _reset()
