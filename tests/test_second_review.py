"""The second review's findings on running the bridge: rate limits, failing full runs, the
look-for-new-activities timing, health, retries outside the scan window."""

from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import requests
from test_review_fixes import _clock, _loop_settings

from garmin_intervals_bridge import cli, health, intervals, polling, sync
from garmin_intervals_bridge.store import Store

VIENNA = ZoneInfo("Europe/Vienna")


def _http(status, headers=None):
    response = requests.Response()
    response.status_code = status
    response.headers.update(headers or {})
    return requests.HTTPError(str(status), response=response)


def test_a_failing_full_run_takes_growing_breaks_not_one_each_minute(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "sync_enrich", lambda *a, **k: calls.append("full") or (_ for _ in ()).throw(
        ValueError("Garmin list broken")))
    monkeypatch.setattr(cli, "watch_once", lambda *a, **k: {"new": 0})
    monkeypatch.setattr(cli, "wellness_check", lambda *a, **k: {})
    now = _clock(monkeypatch)
    start = now[0]
    st = Store(tmp_path)
    cli.run_loop(_loop_settings(tmp_path), st, apply=False, poll_seconds=60, sync_minutes=30, iterations=40)
    assert now[0] - start > 1800 and 2 <= len(calls) <= 5            # over half an hour: not a full run a minute
    st.close()


def test_no_health_probe_while_intervals_asked_for_a_break(tmp_path, monkeypatch):
    probes = []
    monkeypatch.setattr(cli, "watch_once", lambda *a, **k: {"new": 0})
    monkeypatch.setattr(cli, "wellness_check", lambda *a, **k: {})
    now = _clock(monkeypatch, jump=400)
    monkeypatch.setattr(cli, "probe", lambda *a, **k: probes.append(1) or {"findings": []})
    st = Store(tmp_path)
    st.set_meta(cli.PAUSE_KEY, f"{now[0] + 86400:.0f}")
    cli.run_loop(_loop_settings(tmp_path), st, apply=False, poll_seconds=60, sync_minutes=30, iterations=3)
    assert probes == []
    st.close()


def test_retry_after_in_seconds_or_as_a_date():
    assert intervals.rate_limited(_http(429, {"Retry-After": "3600"})) == 3600
    later = intervals.rate_limited(_http(429, {"Retry-After": "Wed, 21 Oct 2099 07:28:00 GMT"}))
    assert later > 3600 * 24 * 365
    assert intervals.rate_limited(_http(429)) == 0 and intervals.rate_limited(_http(503)) is None
    assert intervals.refused(_http(429)) and intervals.refused(_http(422)) and not intervals.refused(_http(502))


def test_a_429_in_one_days_write_pauses_the_whole_run(tmp_path):
    from test_wellness_schedule import TODAY, at, fresh, run
    st, g, i = fresh(tmp_path)
    g.sleep = g.readiness = True
    i.records[TODAY] = {"sleepSecs": 28080}

    def write(day, changes):
        raise _http(429, {"Retry-After": "1800"})
    i.write_wellness = write
    with pytest.raises(requests.HTTPError):
        run(tmp_path, st, g, i, at(8, 0))
    st.close()


def test_upload_mode_counts_the_full_runs_look_and_an_upload_makes_the_hour_fast(tmp_path):
    st = Store(tmp_path)
    s = SimpleNamespace(timezone=VIENNA, poll_seconds=600, poll_hours=((600, 1080),), poll_quiet_seconds=3600,
                        poll_mode="fixed")
    now = datetime(2026, 10, 12, 20, 0, tzinfo=VIENNA).timestamp()           # quiet hours
    st.record_run("sync", {}, started=now - 60)
    assert polling.last_look(st, "upload") == now - 60 and polling.last_look(st, "enrich") is None
    assert not polling.due(s, st, now, 600, "upload")[0]                       # the full run just looked
    assert polling.interval(s, st, now, 600)[0] == 3600
    polling.note_new(st, now - 120)                                          # an upload two minutes ago
    assert polling.interval(s, st, now, 600) == (600, "within the hour after an activity")
    st.close()


def test_check_now_waits_half_a_minute_after_a_look_and_the_loop_wakes_on_time(tmp_path):
    st = Store(tmp_path)
    s = SimpleNamespace(timezone=VIENNA, poll_seconds=90, poll_hours=(), poll_quiet_seconds=None, poll_mode="fixed")
    now = 1_800_000_000.0
    st.record_run("watch", {}, started=now - 10)
    st.set_meta(polling.NOW_KEY, f"{now - 1:.3f}")
    assert not polling.due(s, st, now, 90)[0]                                  # a look 10 s ago
    assert polling.seconds_to_next(s, st, now, 90) == 20
    assert polling.due(s, st, now + 20, 90) == (True, "asked on the web page")
    st.set_meta(polling.NOW_KEY, "")
    assert polling.seconds_to_next(s, st, now, 90) == 80                       # 90 s pace, not the next minute
    st.close()


def test_no_watch_alarm_while_the_poll_hours_are_closed(monkeypatch):
    s = SimpleNamespace(timezone=VIENNA, poll_seconds=None, poll_hours=((600, 1080),), poll_quiet_seconds=None,
                        poll_mode="fixed")
    morning = datetime(2026, 10, 13, 8, 31, tzinfo=VIENNA).timestamp()
    noon = datetime(2026, 10, 13, 11, 0, tzinfo=VIENNA).timestamp()            # the window opened an hour ago
    afternoon = datetime(2026, 10, 13, 14, 0, tzinfo=VIENNA).timestamp()
    assert health._watch_stale_hours(s, morning) is None
    assert health._watch_stale_hours(s, noon) is None
    assert health._watch_stale_hours(s, afternoon) == health.STALE_RUN_HOURS
    slow = SimpleNamespace(timezone=VIENNA, poll_seconds=None, poll_hours=(), poll_quiet_seconds=4 * 3600,
                           poll_mode="fixed")
    assert health._watch_stale_hours(slow, morning) == 8                        # twice the slowest pace


def test_a_failure_older_than_the_scan_window_is_retried_by_its_id(tmp_path, monkeypatch):
    st = Store(tmp_path)
    st.record_failure("4242", "ValueError: no file")
    st.db.execute("UPDATE activity SET next_retry=0 WHERE garmin_id='4242'")
    st.db.commit()
    tried = []
    monkeypatch.setattr(sync, "enrich_activity", lambda gid, *a, **k: tried.append(gid) or {"outcome": "enriched"})
    metrics = {"failed": 0}
    sync._retry_overdue(None, None, st, metrics, {"5555"}, None, apply=True)
    assert tried == ["4242"] and st.activity_status("4242") != "failed" and metrics["enriched"] == 1
    st.close()


def test_the_short_activity_list_falls_back_when_the_first_day_may_hold_more(tmp_path):
    from garmin_intervals_bridge.garmin import GarminSource
    g = GarminSource(tmp_path)
    page = [{"activityId": n, "startTimeLocal": f"2026-10-0{1 if n == 0 else 5} 08:00:00"} for n in range(100)]
    asked = []

    def call(name, *args):
        asked.append(name)
        return page if name == "get_activities" else page + [{"activityId": 100, "startTimeLocal": "2026-10-01 07:00"}]
    g._call = call
    assert len(g.activities(date(2026, 10, 1), date(2026, 10, 5))) == 101
    assert asked == ["get_activities", "get_activities_by_date"]


def test_health_and_garmin_only_backfills_need_no_intervals_key(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BRIDGE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("INTERVALS_API_KEY", raising=False)
    monkeypatch.setenv("BRIDGE_MODE", "backup")
    seen = []
    monkeypatch.setattr(cli, "probe", lambda settings, garmin, intervals, store: seen.append(intervals) or
                        {"findings": [], "exit": 0})
    assert cli.main(["health"]) == 0 and seen == [None]
    monkeypatch.setattr(cli.GarminSource, "login", lambda self, interactive=True: None)
    monkeypatch.setattr(cli, "sync_wellness_files", lambda *a, **k: {"days": 1})
    assert cli.main(["backfill", "--scope", "wellness", "--wellness-files", "--from", "2026-10-01",
                     "--to", "2026-10-02"]) == 0
    assert '"days": 1' in capsys.readouterr().out
