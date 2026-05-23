"""Notifications (Apprise-based dispatch).

Apprise is mocked to capture sends without hitting the network. We
verify:
  - quiet-hours window logic (including the midnight-wrapping case)
  - urgent notifications bypass quiet hours
  - dispatch_for_run fires for failed runs when ENV_NOTIFY_ON_FAILURE is set
  - dispatch_for_run fires for timed_out runs when ENV_NOTIFY_ON_TIMEOUT is set
  - dispatch is a no-op when no URL is configured
  - the engine fires notifications on run.failed end-to-end
  - reap_timed_out_runs() marks runs timed_out and fires notify
  - the engine's terminal hook swallows notify exceptions (fire-and-forget)
  - render() handles missing template variables gracefully
"""
from __future__ import annotations

import datetime as dt
import os

import pytest

from automaton import db as _db
from automaton import engine
from automaton import notify


@pytest.fixture
def store(tmp_path):
    conn = _db.connect(tmp_path / "test.db")
    _db.migrate(conn)
    return conn


@pytest.fixture
def captured_apprise(monkeypatch):
    """Replace apprise.Apprise with a fake that records calls.

    Tests assert against ``captured.calls``, which is a list of
    ``{"urls": [...], "title": str, "body": str}`` dicts.
    """
    captured = type("Captured", (), {"calls": []})()
    captured.calls = []

    class _FakeApprise:
        def __init__(self):
            self._urls = []

        def add(self, url):
            self._urls.append(url)
            return True

        def __len__(self):
            return len(self._urls)

        def notify(self, title="", body=""):
            captured.calls.append({"urls": list(self._urls), "title": title, "body": body})
            return True

    import apprise
    monkeypatch.setattr(apprise, "Apprise", _FakeApprise)
    return captured


# --------------------- quiet hours --------------------------

def test_quiet_hours_inside(monkeypatch):
    monkeypatch.setenv(notify.ENV_NOTIFY_QUIET_HOURS, "22:00-07:00")
    assert notify.is_quiet_hours(dt.datetime(2026, 1, 1, 23, 30)) is True
    assert notify.is_quiet_hours(dt.datetime(2026, 1, 1, 3, 0)) is True


def test_quiet_hours_outside(monkeypatch):
    monkeypatch.setenv(notify.ENV_NOTIFY_QUIET_HOURS, "22:00-07:00")
    assert notify.is_quiet_hours(dt.datetime(2026, 1, 1, 9, 0)) is False
    assert notify.is_quiet_hours(dt.datetime(2026, 1, 1, 21, 59)) is False


def test_quiet_hours_no_wrap(monkeypatch):
    """A same-day window like 09:00-17:00 doesn't wrap."""
    monkeypatch.setenv(notify.ENV_NOTIFY_QUIET_HOURS, "09:00-17:00")
    assert notify.is_quiet_hours(dt.datetime(2026, 1, 1, 12, 0)) is True
    assert notify.is_quiet_hours(dt.datetime(2026, 1, 1, 19, 0)) is False


def test_quiet_hours_malformed_is_ignored(monkeypatch):
    monkeypatch.setenv(notify.ENV_NOTIFY_QUIET_HOURS, "definitely not a time range")
    # Logged at warning level; behavior is "no quiet hours".
    assert notify.is_quiet_hours(dt.datetime(2026, 1, 1, 12, 0)) is False


def test_quiet_hours_unset(monkeypatch):
    monkeypatch.delenv(notify.ENV_NOTIFY_QUIET_HOURS, raising=False)
    assert notify.is_quiet_hours(dt.datetime(2026, 1, 1, 3, 0)) is False


# --------------------- send() -------------------------------

def test_send_no_urls_returns_no_op():
    result = notify.send([], "t", "b")
    assert result["sent"] is False
    assert "no notify URLs" in result["reason"]


def test_send_during_quiet_hours_skips(captured_apprise, monkeypatch):
    monkeypatch.setenv(notify.ENV_NOTIFY_QUIET_HOURS, "22:00-07:00")
    result = notify.send(
        ["ntfy://example.com/topic"], "t", "b",
        urgent=False,
        now=dt.datetime(2026, 1, 1, 23, 30),
    )
    assert result["skipped"] is True
    assert captured_apprise.calls == []


def test_send_urgent_bypasses_quiet_hours(captured_apprise, monkeypatch):
    monkeypatch.setenv(notify.ENV_NOTIFY_QUIET_HOURS, "22:00-07:00")
    result = notify.send(
        ["ntfy://example.com/topic"], "t", "b",
        urgent=True,
        now=dt.datetime(2026, 1, 1, 23, 30),
    )
    assert result["sent"] is True
    assert result["skipped"] is False
    assert len(captured_apprise.calls) == 1
    assert captured_apprise.calls[0]["title"] == "t"


def test_send_outside_quiet_hours_goes_through(captured_apprise, monkeypatch):
    monkeypatch.setenv(notify.ENV_NOTIFY_QUIET_HOURS, "22:00-07:00")
    result = notify.send(
        ["ntfy://example.com/topic"], "t", "b",
        urgent=False,
        now=dt.datetime(2026, 1, 1, 12, 0),
    )
    assert result["sent"] is True
    assert len(captured_apprise.calls) == 1


# --------------------- template rendering -------------------

def test_render_substitutes_known_vars():
    out = notify.render("run {run_id} {status}", {"run_id": 42, "status": "failed"})
    assert out == "run 42 failed"


def test_render_tolerates_missing_keys():
    """Missing keys come back as literal {placeholders} - never KeyError."""
    out = notify.render("run {run_id} {nonexistent}", {"run_id": 42})
    assert "{nonexistent}" in out


# --------------------- dispatch_for_run ---------------------

def _setup_failing_workflow(store, name="failing"):
    engine.register_workflow(store, {
        "name": name,
        "steps": [{
            "name": "boom",
            "type": "shell",
            "cmd": ["sh", "-c", "exit 1"],
        }],
    })
    rid = engine.trigger_run(store, name)
    return rid


def test_dispatch_for_run_failure_path(captured_apprise, store, monkeypatch):
    monkeypatch.setenv(notify.ENV_NOTIFY_ON_FAILURE, "ntfy://example.com/topic")
    rid = _setup_failing_workflow(store)
    engine.worker_loop(store, stop_when_idle=True)
    # Engine already dispatched as part of commit_step; clear and re-dispatch
    # explicitly to assert the function shape independently.
    captured_apprise.calls.clear()
    result = notify.dispatch_for_run(store, rid)
    assert result is not None
    assert result["sent"] is True
    call = captured_apprise.calls[0]
    assert "failing" in call["body"]
    assert "boom" in call["body"]  # failed_step name surfaces in the body
    assert "1" in call["title"]  # run id appears in title


def test_dispatch_for_run_no_env_var_is_noop(captured_apprise, store, monkeypatch):
    monkeypatch.delenv(notify.ENV_NOTIFY_ON_FAILURE, raising=False)
    monkeypatch.delenv(notify.ENV_NOTIFY_ON_SUCCESS, raising=False)
    rid = _setup_failing_workflow(store)
    engine.worker_loop(store, stop_when_idle=True)
    result = notify.dispatch_for_run(store, rid)
    assert result is None
    assert captured_apprise.calls == []


def test_success_dispatch_when_configured(captured_apprise, store, monkeypatch):
    monkeypatch.setenv(notify.ENV_NOTIFY_ON_SUCCESS, "ntfy://example.com/topic")
    engine.register_workflow(store, {
        "name": "ok",
        "steps": [{"name": "noop", "type": "shell",
                   "cmd": ["sh", "-c", "exit 0"]}],
    })
    rid = engine.trigger_run(store, "ok")
    engine.worker_loop(store, stop_when_idle=True)
    result = notify.dispatch_for_run(store, rid)
    assert result["sent"] is True
    assert "completed" in captured_apprise.calls[0]["body"]


def test_workflow_marked_urgent_bypasses_quiet_hours(captured_apprise, store,
                                                     monkeypatch):
    monkeypatch.setenv(notify.ENV_NOTIFY_ON_FAILURE, "ntfy://example.com/topic")
    monkeypatch.setenv(notify.ENV_NOTIFY_QUIET_HOURS, "00:00-23:59")
    engine.register_workflow(store, {
        "name": "urgent_demo",
        "urgent": True,
        "steps": [{"name": "boom", "type": "shell",
                   "cmd": ["sh", "-c", "exit 1"]}],
    })
    rid = engine.trigger_run(store, "urgent_demo")
    engine.worker_loop(store, stop_when_idle=True)
    captured_apprise.calls.clear()
    notify.dispatch_for_run(store, rid)
    assert len(captured_apprise.calls) == 1


# --------------------- engine integration end-to-end --------

def test_engine_fires_notification_on_run_failed(captured_apprise, store, monkeypatch):
    """The actual hook in commit_step calls notify; confirm it lands."""
    monkeypatch.setenv(notify.ENV_NOTIFY_ON_FAILURE, "ntfy://example.com/topic")
    _setup_failing_workflow(store)
    engine.worker_loop(store, stop_when_idle=True)
    # commit_step should have dispatched once for run.failed.
    assert len(captured_apprise.calls) == 1
    assert "failed" in captured_apprise.calls[0]["body"]


def test_engine_swallows_notify_exception(store, monkeypatch):
    """A broken notify configuration should never break the run itself."""
    monkeypatch.setenv(notify.ENV_NOTIFY_ON_FAILURE, "ntfy://example.com/topic")

    def boom(*a, **kw):
        raise RuntimeError("oops")

    monkeypatch.setattr(notify, "dispatch_for_run", boom)
    rid = _setup_failing_workflow(store)
    # Should not raise; the run still transitions to failed.
    engine.worker_loop(store, stop_when_idle=True)
    row = store.execute(
        "SELECT status FROM run WHERE id = ?", (rid,)
    ).fetchone()
    assert row["status"] == "failed"


# --------------------- timed_out path ----------------------

def _setup_timeout_workflow(store, timeout_seconds=1):
    """Register a slow workflow with a tight timeout and trigger a run.

    The run is deliberately NOT executed via worker_loop so we can call
    reap_timed_out_runs() directly after back-dating started_at.
    """
    engine.register_workflow(store, {
        "name": "slow",
        "timeout_seconds": timeout_seconds,
        "steps": [{"name": "slow_step", "type": "shell",
                   "cmd": ["sh", "-c", "sleep 99"]}],
    })
    rid = engine.trigger_run(store, "slow")
    # Back-date started_at so the timeout has already elapsed.
    store.execute(
        "UPDATE run SET started_at = datetime('now', '-10 seconds') WHERE id = ?",
        (rid,),
    )
    store.commit()
    return rid


def test_reap_timed_out_runs_marks_run(store):
    rid = _setup_timeout_workflow(store)
    reaped = engine.reap_timed_out_runs(store)
    assert reaped == 1
    row = store.execute("SELECT status FROM run WHERE id = ?", (rid,)).fetchone()
    assert row["status"] == "timed_out"


def test_reap_timed_out_runs_cancels_steps(store):
    """Steps in pending/running state are cancelled when the run is reaped."""
    rid = _setup_timeout_workflow(store)
    # trigger_run already seeded a pending step + queue entry; find it.
    step_id = store.execute(
        "SELECT id FROM step WHERE run_id=?", (rid,)
    ).fetchone()["id"]
    in_queue_before = store.execute(
        "SELECT 1 FROM queue WHERE step_id=?", (step_id,)
    ).fetchone()
    assert in_queue_before is not None, "step should be queued before reap"
    engine.reap_timed_out_runs(store)
    # Step should be cancelled and queue entry removed.
    row = store.execute("SELECT status FROM step WHERE id=?", (step_id,)).fetchone()
    assert row["status"] == "cancelled"
    in_queue_after = store.execute(
        "SELECT 1 FROM queue WHERE step_id=?", (step_id,)
    ).fetchone()
    assert in_queue_after is None


def test_reap_writes_event_log(store):
    rid = _setup_timeout_workflow(store)
    engine.reap_timed_out_runs(store)
    kinds = [
        r["kind"] for r in
        store.execute("SELECT kind FROM event_log WHERE run_id=?", (rid,)).fetchall()
    ]
    assert "run.timed_out" in kinds


def test_reap_skips_already_terminal_runs(store):
    rid = _setup_timeout_workflow(store)
    # Complete the run before reaping.
    store.execute(
        "UPDATE run SET status='completed', finished_at=datetime('now') WHERE id=?",
        (rid,),
    )
    store.commit()
    reaped = engine.reap_timed_out_runs(store)
    assert reaped == 0


def test_reap_skips_runs_without_timeout(store):
    """Workflows without timeout_seconds are never reaped."""
    engine.register_workflow(store, {
        "name": "no_timeout",
        "steps": [{"name": "s", "type": "shell", "cmd": ["sh", "-c", "exit 0"]}],
    })
    rid = engine.trigger_run(store, "no_timeout")
    store.execute(
        "UPDATE run SET started_at=datetime('now','-9999 seconds') WHERE id=?",
        (rid,),
    )
    store.commit()
    reaped = engine.reap_timed_out_runs(store)
    assert reaped == 0


def test_dispatch_for_run_timed_out(captured_apprise, store, monkeypatch):
    """dispatch_for_run fires the timeout channel for timed_out runs."""
    monkeypatch.setenv(notify.ENV_NOTIFY_ON_TIMEOUT, "ntfy://example.com/topic")
    rid = _setup_timeout_workflow(store)
    engine.reap_timed_out_runs(store)
    captured_apprise.calls.clear()
    result = notify.dispatch_for_run(store, rid)
    assert result is not None
    assert result["sent"] is True
    call = captured_apprise.calls[0]
    assert "timed_out" in call["body"]


def test_dispatch_for_run_timed_out_no_env_is_noop(captured_apprise, store, monkeypatch):
    monkeypatch.delenv(notify.ENV_NOTIFY_ON_TIMEOUT, raising=False)
    rid = _setup_timeout_workflow(store)
    engine.reap_timed_out_runs(store)
    result = notify.dispatch_for_run(store, rid)
    assert result is None
    assert captured_apprise.calls == []


def test_timed_out_urgent_bypasses_quiet_hours(captured_apprise, store, monkeypatch):
    monkeypatch.setenv(notify.ENV_NOTIFY_ON_TIMEOUT, "ntfy://example.com/topic")
    monkeypatch.setenv(notify.ENV_NOTIFY_QUIET_HOURS, "00:00-23:59")
    engine.register_workflow(store, {
        "name": "urgent_timeout",
        "timeout_seconds": 1,
        "urgent": True,
        "steps": [{"name": "s", "type": "shell", "cmd": ["sh", "-c", "sleep 99"]}],
    })
    rid = engine.trigger_run(store, "urgent_timeout")
    store.execute(
        "UPDATE run SET started_at=datetime('now','-10 seconds') WHERE id=?", (rid,)
    )
    store.commit()
    engine.reap_timed_out_runs(store)
    captured_apprise.calls.clear()
    notify.dispatch_for_run(store, rid)
    assert len(captured_apprise.calls) == 1


def test_notify_test_includes_timeout_urls(monkeypatch):
    """ENV_NOTIFY_ON_TIMEOUT URLs are collected by the CLI's notify test."""
 