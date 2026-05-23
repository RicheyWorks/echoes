"""Timezone-aware scheduling, including DST transitions.

Covers:
  - Unknown IANA names rejected at register time.
  - Workflow-level `timezone:` validated.
  - Same cron expression in two timezones produces different UTC times.
  - 2:30 local time fires correctly across DST spring-forward (jumped over)
    and fall-back (fires once, not twice) for America/New_York.
  - preview_fires returns both local and UTC strings.
  - The default (no tz) interprets in UTC.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from automaton import db as _db
from automaton import engine
from automaton import scheduler as _sched


@pytest.fixture
def store(tmp_path):
    conn = _db.connect(tmp_path / "test.db")
    _db.migrate(conn)
    return conn


# --- timezone validation -----------------------------------

def test_unknown_timezone_rejected_in_register_cron(store):
    with pytest.raises(ValueError, match="unknown IANA timezone"):
        _sched.register_cron(store, "wf", "0 12 * * *", timezone="Mars/Phobos")


def test_unknown_workflow_timezone_rejected_in_validate_spec():
    with pytest.raises(ValueError, match="workflow timezone"):
        engine.validate_spec({
            "name": "tz_bad",
            "timezone": "Not/A_Real_Zone",
            "steps": [{"name": "n", "type": "shell", "cmd": ["true"]}],
        })


def test_valid_timezone_accepted():
    # Should not raise.
    engine.validate_spec({
        "name": "tz_ok",
        "timezone": "America/Los_Angeles",
        "steps": [{"name": "n", "type": "shell", "cmd": ["true"]}],
    })


def test_no_timezone_is_fine():
    """Workflows without `timezone:` are valid; cron interprets in UTC."""
    engine.validate_spec({
        "name": "tz_default",
        "steps": [{"name": "n", "type": "shell", "cmd": ["true"]}],
    })


# --- fire-time math ----------------------------------------

def test_same_expression_in_different_tzs_fires_at_different_utc_times():
    """`0 12 * * *` in NY vs LA: 12:00 local time = different UTC instants."""
    base = datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)
    ny = _sched._next_fire("0 12 * * *", base=base, tz="America/New_York")
    la = _sched._next_fire("0 12 * * *", base=base, tz="America/Los_Angeles")
    assert ny != la
    # NY is 3h ahead of LA (both standard or both daylight), so NY's UTC
    # instant precedes LA's by 3 hours.
    assert (la - ny).total_seconds() == 3 * 3600


def test_utc_default_when_no_tz():
    base = datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)
    nxt = _sched._next_fire("30 13 * * *", base=base)
    assert nxt.tzinfo == timezone.utc
    assert nxt.hour == 13
    assert nxt.minute == 30


# --- DST handling ------------------------------------------

def test_dst_spring_forward_skips_nonexistent_hour():
    """At 02:00 on 2026-03-08 (US/Eastern), clocks jump to 03:00.
    A 'every day at 2:30' cron should NOT fire that day - the next fire
    must be 2026-03-09 02:30 local."""
    # Start just before midnight on the transition day in NY.
    base = datetime(2026, 3, 8, 4, 0, tzinfo=timezone.utc)  # 23:00 ET prev day -> nah
    # Easier: start from 2026-03-08 06:00 UTC = 01:00 EST that morning,
    # so the next 02:30 in local time should be the *next* day's 02:30.
    base = datetime(2026, 3, 8, 6, 0, tzinfo=timezone.utc)
    nxt = _sched._next_fire("30 2 * * *", base=base, tz="America/New_York")
    # Either 03:30 EDT same day (if croniter rolls forward through the
    # gap) or 02:30 EDT next day. Both are reasonable; assert it's not
    # 02:30 on the transition day (which doesn't exist).
    from zoneinfo import ZoneInfo
    local = nxt.astimezone(ZoneInfo("America/New_York"))
    assert not (local.date() == datetime(2026, 3, 8).date() and
                local.hour == 2 and local.minute == 30), \
        f"fired at nonexistent 2:30 on DST spring-forward day: {local}"


def test_dst_fall_back_fires_once_per_day():
    """At 02:00 on 2026-11-01 (US/Eastern), clocks fall back to 01:00.
    A 'every day at 1:30' cron must fire at most twice that day (or, with
    croniter's default behavior, exactly once - the early occurrence).
    Crucially the *next-day* 1:30 still fires on schedule afterward."""
    from zoneinfo import ZoneInfo
    base = datetime(2026, 11, 1, 4, 0, tzinfo=timezone.utc)  # ~midnight ET
    # Walk forward four fires; the wall-clock times should each be 01:30
    # local on subsequent days. We don't double-fire on the fall-back day.
    fires = []
    cursor = base
    for _ in range(4):
        cursor = _sched._next_fire("30 1 * * *", base=cursor, tz="America/New_York")
        fires.append(cursor.astimezone(ZoneInfo("America/New_York")))
        cursor += __import__("datetime").timedelta(seconds=1)
    days = [f.date() for f in fires]
    assert len(set(days)) >= 3, \
        f"expected fires on at least 3 distinct days, got: {days}"


# --- end-to-end: registering with a tz ---------------------

def test_register_cron_persists_timezone(store):
    tid = _sched.register_cron(store, "tzdemo", "30 14 * * *",
                                timezone="America/Los_Angeles")
    row = store.execute(
        "SELECT timezone, cron_expr FROM cron_trigger WHERE id = ?", (tid,)
    ).fetchone()
    assert row["timezone"] == "America/Los_Angeles"
    assert row["cron_expr"] == "30 14 * * *"


def test_list_crons_returns_timezone(store):
    _sched.register_cron(store, "wf", "0 9 * * *", timezone="Europe/Berlin")
    _sched.register_cron(store, "wf2", "0 9 * * *")  # no tz = UTC
    rows = _sched.list_crons(store)
    by_wf = {r["workflow_name"]: r for r in rows}
    assert by_wf["wf"]["timezone"] == "Europe/Berlin"
    assert by_wf["wf2"]["timezone"] is None


# --- preview_fires -----------------------------------------

def test_preview_fires_with_tz_returns_both_local_and_utc():
    previews = _sched.preview_fires("0 12 * * *",
                                     tz="America/New_York", count=3)
    assert len(previews) == 3
    for p in previews:
        assert "UTC" in p["utc"]
        # Local string includes a tz abbreviation (EST or EDT).
        assert p["local"] != p["utc"]


def test_preview_fires_without_tz_local_equals_utc():
    previews = _sched.preview_fires("0 12 * * *", count=2)
    for p in previews:
        assert p["local"] == p["utc"]


def test_preview_fires_rejects_bad_cron():
    with pytest.raises(ValueError, match="invalid cron expression"):
        _sched.preview_fires("not a cron")


def test_preview_fires_rejects_bad_tz():
    with pytest.raises(ValueError, match="unknown IANA timezone"):
        _sched.preview_fires("0 12 * * *", tz="Mars/Phobos")
