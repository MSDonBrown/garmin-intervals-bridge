"""When the bridge looks for new activities: its pace, quiet hours, the hour after an activity,
"Check now", and the adaptive mode that learns the usual hours from the archive."""
import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from garmin_intervals_bridge import cli, config, polling
from garmin_intervals_bridge.cli import main
from garmin_intervals_bridge.store import Store

VIENNA = ZoneInfo("Europe/Vienna")
MONDAY_EVENING = datetime(2026, 10, 12, 18, 30, tzinfo=VIENNA).timestamp()
NIGHT = datetime(2026, 10, 13, 3, 0, tzinfo=VIENNA).timestamp()                 # Tuesday, 3:00
DAY_HOURS = ((10 * 60, 23 * 60),)


def _settings(**changes):
    values = dict(timezone=VIENNA, poll_seconds=60, poll_hours=(), poll_quiet_seconds=None, poll_mode="fixed")
    values.update(changes)
    return SimpleNamespace(**values)


def _looked(st, when):
    st.db.execute("INSERT INTO runs (command, started, metrics) VALUES ('watch', ?, '{}')", (when,))
    st.db.commit()


def _arrived(st, when, iid="i1"):
    st.db.execute("INSERT INTO intervals_seen VALUES (?, '123', 'GARMIN_CONNECT', ?)", (iid, when))
    st.db.commit()


def _archive(st, ends):
    """Synthetic archived activities, an hour long, ending at the given local times; both shapes
    the archive holds (Garmin's list entry and its activity summary)."""
    for n, end in enumerate(ends):
        start = (end - timedelta(hours=1)).astimezone(timezone.utc)
        item = ({"startTimeGMT": start.strftime("%Y-%m-%d %H:%M:%S"), "duration": 3600.0} if n % 2 else
                {"summaryDTO": {"startTimeGMT": start.strftime("%Y-%m-%dT%H:%M:%S.0"), "elapsedDuration": 3600.0}})
        st.save_activity_json(str(1000 + n), item)


def _weekly(weeks, *moments):
    """(weekday, hour, minute) moments in each of the `weeks` weeks before MONDAY_EVENING's week."""
    monday = datetime(2026, 10, 12, tzinfo=VIENNA)
    return [monday - timedelta(weeks=w) + timedelta(days=d, hours=h, minutes=m)
            for w in range(1, weeks + 1) for d, h, m in moments]


def test_without_hours_it_looks_at_its_pace(tmp_path):
    st = Store(tmp_path)
    assert polling.interval(_settings(), st, MONDAY_EVENING, 60) == (60, "the usual pace")
    assert polling.fast_seconds(_settings(poll_seconds=None)) == 300                     # Intervals asked: not every minute
    assert polling.fast_seconds(_settings(poll_seconds=None), "upload") == 600
    st.close()


def test_quiet_hours_stay_silent_unless_a_quiet_pace_is_set(tmp_path):
    st = Store(tmp_path)
    silent = _settings(poll_hours=DAY_HOURS)
    assert polling.interval(silent, st, NIGHT, 60) == (None, "outside BRIDGE_POLL_HOURS")
    assert polling.interval(silent, st, MONDAY_EVENING, 60)[0] == 60
    slow = _settings(poll_hours=DAY_HOURS, poll_quiet_seconds=1800)
    assert polling.interval(slow, st, NIGHT, 60) == (1800, "quiet hours")
    st.close()


def test_the_hour_after_an_activity_is_fast_but_silent_quiet_hours_stay_silent(tmp_path):
    st = Store(tmp_path)
    _arrived(st, NIGHT - 600)                                               # ten minutes ago
    slow = _settings(poll_hours=DAY_HOURS, poll_quiet_seconds=1800)
    assert polling.interval(slow, st, NIGHT, 60) == (60, "within the hour after an activity")
    assert polling.interval(_settings(poll_hours=DAY_HOURS), st, NIGHT, 60)[0] is None
    assert polling.interval(slow, st, NIGHT + 3600, 60)[0] == 1800          # an hour later: quiet again
    st.close()


def test_never_faster_than_the_configured_pace(tmp_path):
    st = Store(tmp_path)
    upside_down = _settings(poll_seconds=300, poll_hours=DAY_HOURS, poll_quiet_seconds=60)
    assert polling.interval(upside_down, st, NIGHT, 300)[0] == 300
    assert polling.middle_seconds(60, 1800) == 300 and polling.middle_seconds(300, 1800) == 720
    assert polling.middle_seconds(300, 300) == 300
    st.close()


def test_due_once_the_interval_has_passed_or_at_once_when_asked(tmp_path):
    st = Store(tmp_path)
    assert polling.due(_settings(), st, MONDAY_EVENING, 60)[0]                    # never looked yet
    _looked(st, MONDAY_EVENING - 30)
    due, why = polling.due(_settings(), st, MONDAY_EVENING, 60)
    assert not due and "every 1 min, the last 30 s ago" in why
    assert polling.due(_settings(), st, MONDAY_EVENING + 20, 60)[0]                # a timer tick a bit early counts
    slow = _settings(poll_hours=DAY_HOURS, poll_quiet_seconds=1800)
    _looked(st, NIGHT - 600)
    assert not polling.due(slow, st, NIGHT, 60)[0]
    st.set_meta(polling.NOW_KEY, f"{NIGHT - 5:.0f}")                           # "Check now" on the web page
    assert polling.due(slow, st, NIGHT, 60) == (True, "asked on the web page")
    _looked(st, NIGHT)
    assert polling.interval(slow, st, NIGHT + 60, 60)[0] == 60                 # ... and fast for the hour after
    assert polling.due(_settings(poll_hours=DAY_HOURS), st, NIGHT + 60, 60)[0] is False
    st.close()


def test_activity_end_from_both_archive_shapes():
    utc_end = datetime(2026, 10, 12, 17, 0, tzinfo=timezone.utc)
    assert polling.activity_end({"startTimeGMT": "2026-10-12 16:00:00", "duration": 3600.0}) == utc_end
    assert polling.activity_end({"summaryDTO": {"startTimeGMT": "2026-10-12T16:00:00.0", "elapsedDuration": 3600.0,
                                                "duration": 3000.0}}) == utc_end
    assert polling.activity_end({"startTimeGMT": "yesterday"}) is None
    assert polling.activity_end({}) is None


def test_adaptive_learns_the_usual_hours_and_keeps_to_the_pace(tmp_path, caplog):
    st = Store(tmp_path)
    # Monday and Wednesday evenings, Saturday mornings, for eleven weeks; one odd night long ago
    _archive(st, _weekly(11, (0, 18, 40), (2, 18, 20), (5, 9, 50))
             + [datetime(2026, 5, 1, 3, 0, tzinfo=VIENNA)])
    adaptive = _settings(poll_mode="adaptive", poll_quiet_seconds=1800)
    with caplog.at_level("INFO"):
        plan = polling.learned(adaptive, st, MONDAY_EVENING)
    assert plan["activities"] == 33 and len(plan["tiers"]) == 168             # the odd night is too old
    tiers = plan["tiers"]
    assert tiers[0 * 24 + 18] == tiers[2 * 24 + 18] == tiers[5 * 24 + 9] == "2"
    assert tiers[1 * 24 + 3] == "0"
    assert tiers[3 * 24 + 18] == "1"                                           # Thursday at six: not unusual
    assert "Adaptive polling, learned from 33 activities: fast Mon 17-20, Wed 17-20, Sat 8-11" in caplog.text
    assert polling.interval(adaptive, st, MONDAY_EVENING, 60) == (60, "a usual time for an activity")
    assert polling.interval(adaptive, st, NIGHT, 60) == (1800, "an unusual time")
    hours = [MONDAY_EVENING + 3600 * n for n in range(168)]
    paces = {polling.interval(adaptive, st, when, 60)[0] for when in hours}
    assert paces == {60, 300, 1800}                                            # never faster than BRIDGE_POLL_SECONDS
    text = polling.schedule_text(adaptive, st, MONDAY_EVENING, 60)
    assert text.startswith("adaptive: every 1 min Mon 17-20, Wed 17-20, Sat 8-11;") and "otherwise every 30 min" in text
    st.close()


def test_adaptive_works_it_out_once_a_day(tmp_path, monkeypatch):
    st = Store(tmp_path)
    _archive(st, _weekly(6, (0, 18, 40), (2, 18, 20), (5, 9, 50)))
    adaptive = _settings(poll_mode="adaptive")
    first = polling.learned(adaptive, st, MONDAY_EVENING)
    monkeypatch.setattr(polling, "archived_ends", lambda *a: pytest.fail("read the archive twice a day"))
    assert polling.learned(adaptive, st, MONDAY_EVENING + 3600) == first
    assert json.loads(st.get_meta(polling.PLAN_KEY))["day"] == "2026-10-12"
    st.close()


def test_adaptive_keeps_the_pace_while_learning_and_the_hours_as_a_limit(tmp_path):
    st = Store(tmp_path)
    _archive(st, _weekly(1, (0, 18, 40), (2, 18, 20), (5, 9, 50)))
    adaptive = _settings(poll_mode="adaptive", poll_hours=DAY_HOURS)
    assert polling.interval(adaptive, st, MONDAY_EVENING, 60) == (60, "still learning (3 of 15 activities)")
    assert polling.interval(adaptive, st, NIGHT, 60)[0] is None                 # BRIDGE_POLL_HOURS still holds
    assert "still learning (3 of 15 activities): every 1 min; only 10-23" in polling.schedule_text(
        adaptive, st, MONDAY_EVENING, 60)
    st.close()


def test_the_plan_in_words(tmp_path):
    st = Store(tmp_path)
    assert polling.schedule_text(_settings(poll_seconds=None), st, MONDAY_EVENING, 300) == "every 5 min"
    slow = _settings(poll_hours=((390, 23 * 60),), poll_quiet_seconds=1800)
    assert polling.schedule_text(slow, st, MONDAY_EVENING, 60) == (
        "every 1 min; 6:30-23, otherwise every 30 min; every 1 min for an hour after a new activity")
    tiers = ["0"] * 168
    for day in range(5):
        for hour in (17, 18, 19):
            tiers[day * 24 + hour] = "2"
    for hour in (8, 9, 10, 11):
        tiers[5 * 24 + hour] = "2"
    assert polling.hours_text("".join(tiers)) == "Mon-Fri 17-20, Sat 8-12"
    st.close()


def test_poll_settings(monkeypatch):
    monkeypatch.setenv("INTERVALS_API_KEY", "not-a-real-key")
    monkeypatch.setenv("BRIDGE_POLL_QUIET_SECONDS", "1800")
    monkeypatch.setenv("BRIDGE_POLL_MODE", "Adaptive")
    settings = config.Settings.from_env()
    assert settings.poll_quiet_seconds == 1800 and settings.poll_mode == "adaptive"
    monkeypatch.setenv("BRIDGE_POLL_MODE", "sometimes")
    with pytest.raises(ValueError, match="BRIDGE_POLL_MODE"):
        config.Settings.from_env()


def test_a_watch_that_is_not_due_ends_without_a_request(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BRIDGE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("INTERVALS_API_KEY", "not-a-real-key")
    looks = []
    monkeypatch.setattr(cli, "watch_once", lambda *a, **k: looks.append("watch") or {"new": 0})
    monkeypatch.setattr(cli, "wellness_check", lambda *a, **k: {})
    st = Store(tmp_path)
    st.record_run("watch", {}, started=time.time() - 60)                    # looked a minute ago
    assert main(["watch"]) == 0
    assert "every 5 min, the last" in json.loads(capsys.readouterr().out)["skipped"] and looks == []
    st.set_meta(polling.NOW_KEY, f"{st.last_run('watch') + 1:.0f}")         # "Check now"
    assert main(["watch"]) == 0 and looks == ["watch"]
    st.close()


def test_check_now_wakes_a_waiting_run_loop(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "sync_enrich", lambda *a, **k: calls.append("sync") or {
        "seen": 0, "enriched": 0, "planned": 0, "scan_skipped": 1})
    monkeypatch.setattr(cli, "sync_wellness", lambda *a, **k: {"days_checked": 0, "writes": 0})
    monkeypatch.setattr(cli, "watch_once", lambda *a, **k: calls.append("watch") or {"new": 0})
    monkeypatch.setattr(cli, "wellness_check", lambda *a, **k: {})
    monkeypatch.setattr(cli.time, "sleep", lambda s: pytest.fail("a loop with its web page waits on the button"))
    settings = SimpleNamespace(data_dir=tmp_path, token_dir=tmp_path / "tokens", intervals_api_key="k",
                               intervals_athlete_id="0", garmin_request_delay=0.5, timezone=VIENNA, activity_days=3,
                               wellness_days=3, wellness_refresh_hours=4, wellness_profile="all", stale_hours=24,
                               ui_port=8765)
    st = Store(tmp_path)
    polling.POLL_NOW.set()                                   # pressed while the full run was busy
    cli.run_loop(settings, st, apply=False, poll_seconds=300, sync_minutes=30, iterations=2)
    assert calls == ["sync", "watch"] and not polling.POLL_NOW.is_set()
    st.close()
