"""Dispatcharr 0.34+ lifecycle: data_dir storage, the leader-elected scheduler,
and the scheduled pipeline's lock. Django-free: the Redis and DB helpers are
monkeypatched, and the service loop gets a fake clock and stop event."""
import json
import os
import stat
import sys
import threading
from datetime import datetime

import pytest

from dispatcharr_sports_filter import plugin


@pytest.fixture
def storage(monkeypatch, tmp_path):
    code = tmp_path / "code"
    code.mkdir()
    monkeypatch.setattr(plugin, "PLUGIN_DIR", str(code))
    monkeypatch.setattr(plugin, "_DATA_DIR", None)
    monkeypatch.setattr(plugin, "CACHE_PATH", str(code / "cache.json"))
    monkeypatch.setattr(plugin, "STREAM_CACHE_PATH", str(code / "stream_cache.json"))
    return code, tmp_path / "_data" / "dispatcharr_sports_filter"


def test_configure_storage_moves_legacy_state_once(storage):
    code, data = storage
    (code / "cache.json").write_text(json.dumps({"NFL": "pure_sports"}))
    (code / "stream_cache.json").write_text(json.dumps({"ESPN 1": "sports"}))
    key = code / "anthropic_api_key"
    key.write_text("sk-test\n")
    os.chmod(key, 0o600)

    plugin._configure_storage({"data_dir": str(data)})

    assert plugin.CACHE_PATH == str(data / "cache.json")
    assert plugin.STREAM_CACHE_PATH == str(data / "stream_cache.json")
    assert plugin._read_group_cache() == {"NFL": "pure_sports"}
    assert plugin._read_stream_cache() == {"ESPN 1": "sports"}
    assert plugin._read_api_key({}, provider="anthropic") == "sk-test"
    assert stat.S_IMODE(os.stat(data / "anthropic_api_key").st_mode) == 0o600
    assert (data / plugin._LEGACY_IMPORT_MARKER).exists()


def test_configure_storage_keeps_data_dir_state_and_never_reimports(storage, monkeypatch):
    code, data = storage
    data.mkdir(parents=True)
    (data / "cache.json").write_text(json.dumps({"NBA": "mixed"}))
    (code / "cache.json").write_text(json.dumps({"NBA": "not_sports"}))

    plugin._configure_storage({"data_dir": str(data)})
    assert plugin._read_group_cache() == {"NBA": "mixed"}, "data_dir state must win"

    # A cache the user deleted from data_dir after the import stays deleted.
    (data / "cache.json").unlink()
    monkeypatch.setattr(plugin, "_DATA_DIR", None)
    plugin._configure_storage({"data_dir": str(data)})
    assert not (data / "cache.json").exists()


def test_configure_storage_without_data_dir_keeps_code_dir(storage):
    code, _ = storage
    plugin._configure_storage({})
    plugin._configure_storage(None)
    assert plugin.CACHE_PATH == str(code / "cache.json")
    assert plugin._api_key_path("openai") == str(code / "openai_api_key")


class _Clock:
    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now


class _StopAfter:
    """A stop event whose wait() advances the fake clock and stops the loop
    after `waits` parks, so the loop runs a bounded number of ticks."""

    def __init__(self, clock, waits):
        self.clock = clock
        self.waits = waits
        self.timeouts = []
        self._set = False

    def is_set(self):
        return self._set

    def set(self):
        self._set = True

    def wait(self, timeout=None):
        from datetime import timedelta
        self.timeouts.append(timeout)
        self.clock.now += timedelta(seconds=timeout)
        self.waits -= 1
        if self.waits <= 0:
            self._set = True
        return self._set


def _run_loop(monkeypatch, settings, start, waits, claim=True):
    monkeypatch.setattr(plugin, "_scheduler_close_db", lambda: None)
    monkeypatch.setattr(plugin, "_claim_slot", lambda slot: claim)
    clock = _Clock(start)
    stop = _StopAfter(clock, waits)
    dispatched = []
    context = {
        "get_settings": lambda: dict(settings),
        "dispatch_scheduled_task": lambda action, params: dispatched.append((action, params, clock.now)) or "task-1",
    }
    plugin._service_loop(stop, context, now=clock)
    return dispatched, stop


def test_service_loop_dispatches_once_per_slot(monkeypatch):
    settings = {"auto_pipeline_enabled": True, "auto_pipeline_schedule": "0300"}
    # 02:50 -> sleeps 600s in two capped parks, fires 03:00, then waits for tomorrow.
    dispatched, stop = _run_loop(monkeypatch, settings, datetime(2026, 11, 25, 2, 50), waits=6)
    assert [d[:2] for d in dispatched] == [("auto_pipeline", {plugin.SCHEDULED_PARAM: True})]
    assert max(stop.timeouts) <= plugin.SERVICE_POLL_S


def test_service_loop_does_not_backfill_a_missed_slot(monkeypatch):
    settings = {"auto_pipeline_enabled": True, "auto_pipeline_schedule": "0300"}
    # A leader that starts at 03:05 must not fire the 03:00 slot it missed.
    dispatched, _ = _run_loop(monkeypatch, settings, datetime(2026, 11, 25, 3, 5), waits=3)
    assert dispatched == []


def test_service_loop_idles_while_disabled(monkeypatch):
    settings = {"auto_pipeline_enabled": False, "auto_pipeline_schedule": "0300"}
    dispatched, _ = _run_loop(monkeypatch, settings, datetime(2026, 11, 25, 2, 59), waits=3)
    assert dispatched == []


def test_service_loop_skips_a_slot_another_leader_claimed(monkeypatch):
    settings = {"auto_pipeline_enabled": True, "auto_pipeline_schedule": "0300"}
    dispatched, _ = _run_loop(monkeypatch, settings, datetime(2026, 11, 25, 2, 59), waits=3, claim=False)
    assert dispatched == []


def test_scheduled_run_takes_the_pipeline_lock(monkeypatch):
    calls = []
    monkeypatch.setattr(plugin, "_try_acquire_scheduler_lock", lambda: True)
    monkeypatch.setattr(plugin, "_release_scheduler_lock", lambda: calls.append("release"))
    monkeypatch.setattr(plugin, "_action_auto_pipeline", lambda s: calls.append(("run", s)) or {"status": "ok"})
    p = plugin.Plugin.__new__(plugin.Plugin)
    result = p.run("auto_pipeline", {plugin.SCHEDULED_PARAM: True}, {"settings": {"dry_run": True}})
    assert result == {"status": "ok"}
    assert calls == [("run", {"dry_run": True}), "release"], "the private param must not reach the pipeline"


def test_scheduled_run_reports_partial_when_lock_is_held(monkeypatch):
    monkeypatch.setattr(plugin, "_try_acquire_scheduler_lock", lambda: False)
    monkeypatch.setattr(plugin, "_action_auto_pipeline", lambda s: pytest.fail("must not run"))
    p = plugin.Plugin.__new__(plugin.Plugin)
    result = p.run("auto_pipeline", {plugin.SCHEDULED_PARAM: True}, {"settings": {}})
    assert result["status"] == "partial"


def test_managed_init_starts_no_thread_and_stops_a_legacy_one(monkeypatch):
    plugin._stop_scheduler("test setup")
    sys.modules.pop(plugin._SCHEDULER_REGISTRY_KEY, None)
    legacy_stop = threading.Event()
    reg = plugin._scheduler_registry()
    reg.thread, reg.stop_event = object(), legacy_stop
    monkeypatch.setattr(plugin, "_managed_services", lambda: True)
    monkeypatch.setattr(plugin, "_start_scheduler", lambda: pytest.fail("legacy thread started"))
    monkeypatch.setattr(plugin, "_build_account_field", lambda: {"id": "m3u_account_id", "type": "string"})
    monkeypatch.setattr(plugin, "_build_profile_field", lambda: {"id": "channel_profile_id", "type": "string"})
    try:
        plugin.Plugin()
        assert legacy_stop.is_set()
        assert reg.thread is None and reg.stop_event is None
    finally:
        sys.modules.pop(plugin._SCHEDULER_REGISTRY_KEY, None)


def test_leader_hook_starts_one_named_service(storage, monkeypatch):
    _, data = storage
    started = []
    context = {"data_dir": str(data),
               "start_service": lambda target, name=None: started.append((target, name))}
    plugin.Plugin.__new__(plugin.Plugin).on_leader_acquired(context)
    assert [name for _, name in started] == ["scheduler"]
    assert plugin.CACHE_PATH == str(data / "cache.json")


def test_manifest_declares_the_capabilities_the_code_uses():
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "..", "plugin.json")) as f:
        manifest = json.load(f)
    assert manifest["manifest_version"] == 2
    # dispatch_scheduled_task -> background_tasks; on_leader_acquired ->
    # persistent_service; classifier's urllib LLM calls -> outbound_network.
    assert set(manifest["capabilities"]) == {"background_tasks", "persistent_service", "outbound_network"}
    assert manifest["version"] == plugin.PLUGIN_VERSION
