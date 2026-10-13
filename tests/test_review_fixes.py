"""Behaviour fixed after the 0.2.0 code review."""
import fcntl
import time
from datetime import date
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from test_sync import GarminFake, IntervalsFake, settings

from garmin_intervals_bridge import sync as sync_module
from garmin_intervals_bridge.cli import main
from garmin_intervals_bridge.enrich import FieldMappings, ScalarMapping, mapping_signature, match_activity, origin_check
from garmin_intervals_bridge.garmin import DAY_ENDPOINTS, GarminBlocked, GarminLoginNeeded, GarminSource
from garmin_intervals_bridge.mapping import map_wellness
from garmin_intervals_bridge.store import InstanceBusy, Store, single_instance
from garmin_intervals_bridge.sync import sync_enrich, sync_wellness, wellness_due

# ---- Garmin login failures are classified, not blamed on an activity ----

def _fake_garmin(error):
    class FakeGarmin:
        def __init__(self, *a, **kw):
            pass

        def login(self, tokenstore):
            raise error
    return FakeGarmin


def test_rate_limit_or_outage_at_login_is_a_block_not_a_login_request(tmp_path, monkeypatch):
    import garminconnect
    cause = garminconnect.GarminConnectTooManyRequestsError("429 Client Error")
    err = garminconnect.GarminConnectAuthenticationError("Failed to retrieve social profile")
    err.__cause__ = cause
    monkeypatch.setattr(garminconnect, "Garmin", _fake_garmin(err))
    with pytest.raises(GarminBlocked) as info:
        GarminSource(tmp_path, 0).login(interactive=False)
    assert not isinstance(info.value, GarminLoginNeeded)

    outage = garminconnect.GarminConnectAuthenticationError("Failed to retrieve social profile")
    outage.__cause__ = TimeoutError("Read timed out")
    monkeypatch.setattr(garminconnect, "Garmin", _fake_garmin(outage))
    with pytest.raises(GarminBlocked):
        GarminSource(tmp_path, 0).login(interactive=False)


def test_missing_or_rejected_tokens_need_a_person(tmp_path, monkeypatch):
    import garminconnect
    for error in (FileNotFoundError("no tokens"),
                  garminconnect.GarminConnectAuthenticationError("Failed to retrieve social profile")):
        monkeypatch.setattr(garminconnect, "Garmin", _fake_garmin(error))
        with pytest.raises(GarminLoginNeeded):
            GarminSource(tmp_path, 0).login(interactive=False)


def test_an_id_containing_429_is_not_a_block():
    from garmin_intervals_bridge.garmin import _is_blocked
    assert _is_blocked(RuntimeError("API Error 429 Too Many Requests"))
    assert not _is_blocked(RuntimeError("Activity 2442901 not found (404)"))


# ---- a Garmin outage ends the day instead of trying every endpoint ----

class _Client:
    """Every day endpoint exists; the named ones raise."""
    ActivityDownloadFormat = SimpleNamespace(ORIGINAL="ORIGINAL")

    def __init__(self, failing):
        self.calls = []
        for method in DAY_ENDPOINTS.values():
            setattr(self, method, self._make(method, method in failing))

    def _make(self, method, fails):
        def call(*a, **kw):
            self.calls.append(method)
            if fails:
                raise RuntimeError("API Error 503 Service Unavailable")
            return {"ok": True}
        return call


def test_three_consecutive_endpoint_failures_abort_the_day(tmp_path):
    src = GarminSource(tmp_path, 0)
    src.client = _Client(failing=set(DAY_ENDPOINTS.values()))
    with pytest.raises(GarminBlocked):
        src.snapshot(date(2026, 10, 8))
    assert len(src.client.calls) == 3                     # not 22

    src.client = _Client(failing={"get_hrv_data"})
    raw = src.snapshot(date(2026, 10, 8))
    assert raw["errors"] == {"hrv": "RuntimeError 503"} and len(raw["data"]) == len(DAY_ENDPOINTS) - 1


# ---- a held lock is not a failure of the watch ----

def test_watch_skips_the_poll_when_the_lock_is_held(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GARMIN_TOKEN_DIR", str(tmp_path / "tokens"))
    monkeypatch.setenv("INTERVALS_API_KEY", "not-a-real-key")
    from garmin_intervals_bridge import cli
    checked, polled = [], []
    monkeypatch.setattr(cli, "wellness_check", lambda *a, **k: checked.append(1) or {})
    monkeypatch.setattr(cli, "watch_once", lambda *a, **k: polled.append(1) or {})
    with single_instance(tmp_path, ("activities",)):
        assert main(["watch"]) == 0                        # poll skipped, no error exit, no alert
        assert polled == [] and checked == [1]             # the free wellness scope still does its check
        assert main(["status"]) == 0                       # reads only, needs no lock
        assert main(["sync", "--scope", "activities"]) == 0           # nothing free: a quiet no-op
        assert main(["backfill", "--scope", "activities", "--from", "2026-10-01"]) == 1   # a person's command fails loudly
        with pytest.raises(InstanceBusy):
            with single_instance(tmp_path, ("activities",)):
                pass


# ---- a later success ends the failure bookkeeping ----

def test_successful_enrich_clears_an_earlier_failure(tmp_path, monkeypatch):
    st = Store(tmp_path)
    st.record_failure("42", "ValueError: once")
    st.db.execute("UPDATE activity SET next_retry = 0")
    st.db.commit()                                                      # backoff over
    monkeypatch.setattr(sync_module, "enrich_activity", lambda *a, **kw: {"outcome": "nothing_to_add"})
    monkeypatch.setattr(sync_module, "load_field_mappings", lambda items: None)
    g, i = GarminFake(), IntervalsFake()
    i.custom_items = lambda: []
    out = sync_enrich(settings(tmp_path), g, i, st, apply=False, today=date(2026, 10, 8))
    assert out["nothing_to_add"] == 1
    assert st.failed_activities() == [] and st.activity_status("42") == "downloaded"
    st.close()


# ---- wellness: when a day is read again, and dry runs count ----

def test_wellness_due_schedule():
    tz = ZoneInfo("Europe/Vienna")
    today = date(2026, 10, 8)
    midnight = 1_000_000.0
    now = midnight + 10 * 3600                            # 10:00
    h = 3600
    assert wellness_due(today, today, None, midnight, 4 * h, now)
    assert not wellness_due(today, today, now - 3 * h, midnight, 4 * h, now)
    assert wellness_due(today, today, now - 5 * h, midnight, 4 * h, now)
    yesterday = date(2026, 10, 7)
    assert wellness_due(yesterday, today, midnight - h, midnight, 4 * h, now)       # before midnight
    assert not wellness_due(yesterday, today, now - 7 * h, midnight, 4 * h, now)    # 03:00, within 2 periods
    assert wellness_due(yesterday, today, now - 9 * h, midnight, 4 * h, now)        # 01:00, two periods ago
    older = date(2026, 10, 6)
    assert wellness_due(older, today, midnight - h, midnight, 4 * h, now)
    assert not wellness_due(older, today, midnight + h, midnight, 4 * h, now)       # once after midnight only
    assert tz is not None


class CountingGarmin(GarminFake):
    def __init__(self):
        super().__init__()
        self.snapshots = []

    def snapshot(self, day, endpoints=None):
        self.snapshots.append((day, endpoints))
        return super().snapshot(day)


def test_dry_run_throttles_garmin_and_apply_maps_the_archive(tmp_path):
    st = Store(tmp_path)
    g, i = CountingGarmin(), IntervalsFake()
    i.current_wellness = {"locked": False}
    s = settings(tmp_path)
    day = date(2026, 10, 3)                                   # a backfill day
    dry = sync_wellness(s, g, i, st, apply=False, days=[day], today=date(2026, 10, 8))
    assert dry["days_checked"] == 1 and not i.wellness_writes and len(g.snapshots) == 1
    dry2 = sync_wellness(s, g, i, st, apply=False, days=[day], today=date(2026, 10, 8))
    assert dry2["days_skipped_recent"] == 1 and len(g.snapshots) == 1        # a second dry run asks nothing
    real = sync_wellness(s, g, i, st, apply=True, days=[day], today=date(2026, 10, 8))
    assert real["days_from_archive"] == 1 and real["writes"] == 1 and len(g.snapshots) == 1
    fetched, written = st.wellness_state(day)
    assert fetched and written
    again = sync_wellness(s, g, i, st, apply=True, days=[day], today=date(2026, 10, 8))
    assert again["days_skipped_recent"] == 1 and len(g.snapshots) == 1
    st.close()


def test_refresh_within_a_day_reads_only_the_essential_endpoints_and_keeps_the_archive(tmp_path):
    from garmin_intervals_bridge.garmin import ESSENTIAL_ENDPOINTS
    st = Store(tmp_path)
    g, i = CountingGarmin(), IntervalsFake()
    i.current_wellness = {"locked": False}
    s = settings(tmp_path)
    day = date(2026, 10, 8)
    sync_wellness(s, g, i, st, apply=True, wellness_days=1, today=day, force=True)
    assert g.snapshots[0][1] is None                                          # first read: everything
    st.db.execute("UPDATE wellness SET fetched = ?", (time.time() - 9 * 3600,))
    st.db.commit()
    sync_wellness(s, g, i, st, apply=True, wellness_days=1, today=day, force=True)
    assert g.snapshots[1][1] == ESSENTIAL_ENDPOINTS
    archived = st.load_snapshot(day)
    assert set(archived["data"]) >= set(GarminFake().snapshot(day)["data"])   # nothing lost by the partial read
    st.close()


def test_archive_replay_maps_without_garmin(tmp_path):
    st = Store(tmp_path)
    g, i = CountingGarmin(), IntervalsFake()
    i.current_wellness = {"locked": False}
    s = settings(tmp_path)
    day = date(2026, 10, 7)
    st.save_snapshot(day, GarminFake().snapshot(day))
    out = sync_wellness(s, g, i, st, apply=True, days=[day, date(2026, 10, 6)], today=date(2026, 10, 8),
                        from_archive=True)
    assert out["days_from_archive"] == 1 and out["writes"] == 1 and g.snapshots == []
    st.close()


def test_one_days_intervals_error_does_not_end_the_run(tmp_path):
    st = Store(tmp_path)
    g, i = CountingGarmin(), IntervalsFake()
    i.current_wellness = {"locked": False}
    calls = []

    def wellness(day):
        calls.append(day)
        if day == date(2026, 10, 7):
            raise ValueError("custom field of another type")
        return {"locked": False}
    i.wellness = wellness
    out = sync_wellness(settings(tmp_path), g, i, st, apply=True, wellness_days=2, today=date(2026, 10, 8),
                        force=True)
    assert out["failed"] == 1 and out["writes"] == 1 and len(calls) == 2
    st.close()


# ---- matching ----

def test_bare_numeric_external_id_counts_only_for_garmin_connect():
    garmin = {"activityId": 24544097680, "startTimeGMT": "2026-10-07 08:00:00", "duration": 3600}
    strava = {"id": "i1", "source": "STRAVA", "external_id": "24544097680", "start_date": "2026-10-07T08:00:00Z"}
    assert match_activity(garmin, [strava]) is None
    official = dict(strava, source="GARMIN_CONNECT")
    assert match_activity(garmin, [official])["id"] == "i1"
    bridge_upload = dict(strava, source="UPLOAD", external_id="garmin:24544097680")
    assert match_activity(garmin, [bridge_upload])["id"] == "i1"


def test_duration_matches_moving_time_when_the_ride_was_paused():
    garmin = {"activityId": 1, "startTimeGMT": "2026-10-07 08:00:00", "duration": 3600}
    paused = {"id": "i2", "source": "GARMIN_CONNECT", "external_id": "x", "start_date": "2026-10-07T08:00:00Z",
              "moving_time": 3590, "elapsed_time": 5400}
    assert match_activity(garmin, [paused])["id"] == "i2"
    other = dict(paused, moving_time=1800, elapsed_time=1900)
    assert match_activity(garmin, [other]) is None


def test_flock_is_what_single_instance_uses(tmp_path):
    with single_instance(tmp_path, ("wellness",)):
        lock = (tmp_path / ".bridge-wellness.lock").open("a+")
        with pytest.raises(BlockingIOError):
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock.close()


# ---- streams are only written when the alignment is proven on a shared stream ----

def test_origin_check_catches_a_shifted_origin():
    t0 = 1_000_000
    records = [{"timestamp": t0 + i, "heart_rate": 100 + (i % 7)} for i in range(120)]
    time_stream = list(range(120))
    same = [{"type": "time", "data": time_stream}, {"type": "heartrate", "data": [100 + (i % 7) for i in range(120)]}]
    assert origin_check(records, time_stream, same)["ok"] is True
    shifted = [{"type": "heartrate", "data": [100 + ((i + 3) % 7) for i in range(120)]}]
    assert origin_check(records, time_stream, shifted)["ok"] is False
    assert origin_check(records, time_stream, [{"type": "time", "data": time_stream}])["ok"] is None
    cadence_only = [{"type": "cadence", "data": [80 for _ in range(120)]}]
    with_cadence = [dict(r, cadence=80) for r in records]
    assert origin_check(with_cadence, time_stream, cadence_only)["stream"] == "cadence"


def test_mapping_signature_follows_the_definitions():
    one = FieldMappings(scalars=[ScalarMapping("X", "session:1")])
    same = FieldMappings(scalars=[ScalarMapping("X", "session:1")])
    more = FieldMappings(scalars=[ScalarMapping("X", "session:1"), ScalarMapping("Y", "session:2")])
    assert mapping_signature(one) == mapping_signature(same) != mapping_signature(more)


# ---- fewer Garmin requests, and today's resting HR waits ----

def test_short_activity_window_costs_one_request(tmp_path):
    class Client:
        def __init__(self):
            self.calls = []

        def get_activities(self, start, limit):
            self.calls.append(("list", start, limit))
            return [{"activityId": 1, "startTimeLocal": "2026-10-07 08:00:00"},
                    {"activityId": 2, "startTimeLocal": "2026-09-01 08:00:00"}]

        def get_activities_by_date(self, a, b):
            self.calls.append(("by_date", a, b))
            return []
    src = GarminSource(tmp_path, 0)
    src.client = Client()
    out = src.activities(date(2026, 10, 5), date(2026, 10, 8))
    assert [a["activityId"] for a in out] == [1] and src.client.calls == [("list", 0, 100)]
    src.activities(date(2026, 1, 1), date(2026, 10, 8))
    assert src.client.calls[-1][0] == "by_date"


def test_todays_resting_hr_waits_for_tomorrow():
    from test_mapping import sample
    stats = sample()["data"]["stats"]
    if "restingHeartRate" not in stats:
        pytest.skip("sample has no resting HR")
    today = date(2026, 10, 8)
    nat_today, _ = map_wellness(sample(), today, today)
    nat_past, _ = map_wellness(sample(), date(2026, 10, 7), today)
    assert "restingHR" not in nat_today and nat_past["restingHR"] == stats["restingHeartRate"]


def test_garmin_requests_are_counted(tmp_path):
    src = GarminSource(tmp_path, 0)
    src.client = _Client(failing=set())
    src.snapshot(date(2026, 10, 8))
    assert src.requests == len(DAY_ENDPOINTS)


# ---- .env file, long-running `run` ----

def test_env_file_fills_missing_variables_only(tmp_path, monkeypatch):
    import os

    from garmin_intervals_bridge.config import load_env_file
    env = tmp_path / ".env"
    env.write_text('INTERVALS_API_KEY="from-file"\nBRIDGE_TIMEZONE=UTC\n# comment\nlower=ignored\n', encoding="utf-8")
    monkeypatch.setenv("BRIDGE_TIMEZONE", "Europe/Vienna")
    monkeypatch.setitem(os.environ, "INTERVALS_API_KEY", "placeholder")
    del os.environ["INTERVALS_API_KEY"]
    assert load_env_file(env) == ["INTERVALS_API_KEY"]
    assert os.environ["INTERVALS_API_KEY"] == "from-file"          # quotes stripped
    assert os.environ["BRIDGE_TIMEZONE"] == "Europe/Vienna"         # an exported variable wins
    assert "lower" not in os.environ


def _clock(monkeypatch, calls=None, jump=0):
    """A clock the loop's waits move on (at least `jump` seconds each), so the polls fall due.
    The daily health probe comes due with it: a quiet fake, unless the test sets its own after."""
    from garmin_intervals_bridge import cli
    now = [1_800_000_000.0]
    monkeypatch.setattr(cli, "probe", lambda *a, **k: {"findings": []})

    def sleep(seconds):
        if calls is not None:
            calls.append(("sleep", seconds))
        now[0] += max(seconds, jump)
    monkeypatch.setattr(cli.time, "time", lambda: now[0])
    monkeypatch.setattr(cli.time, "sleep", sleep)
    return now


def _loop_settings(tmp_path):
    return SimpleNamespace(data_dir=tmp_path, token_dir=tmp_path / "tokens", intervals_api_key="k",
                           intervals_athlete_id="0", garmin_request_delay=0.5, timezone=ZoneInfo("Europe/Vienna"),
                           activity_days=3, wellness_days=3, wellness_refresh_hours=4, wellness_profile="all",
                           stale_hours=24)


def test_run_loop_says_which_version_and_day_boundary_it_runs_with(tmp_path, monkeypatch, caplog):
    from garmin_intervals_bridge import __version__, cli
    monkeypatch.setattr(cli, "sync_enrich", lambda *a, **k: {"seen": 0, "enriched": 0, "planned": 0, "scan_skipped": 1})
    monkeypatch.setattr(cli, "sync_wellness", lambda *a, **k: {"days_checked": 0, "writes": 0})
    settings = _loop_settings(tmp_path)
    settings.timezone, settings.timezone_source = ZoneInfo("UTC"), "fallback, nothing set"
    st = Store(tmp_path)
    with caplog.at_level("INFO"):
        cli.run_loop(settings, st, apply=False, poll_seconds=60, sync_minutes=30, iterations=1)
    assert f"garmin-intervals-bridge {__version__} running" in caplog.text        # a container log shows the version
    assert "Day boundary: UTC (fallback, nothing set); set BRIDGE_TIMEZONE" in caplog.text
    st.close()


def test_run_loop_does_a_full_run_then_polls(tmp_path, monkeypatch):
    from garmin_intervals_bridge import cli
    calls = []
    monkeypatch.setattr(cli, "sync_enrich", lambda *a, **k: calls.append("sync") or {"seen": 1, "enriched": 0, "planned": 0})
    monkeypatch.setattr(cli, "sync_wellness", lambda *a, **k: calls.append("wellness") or {"days_checked": 1, "writes": 0})
    monkeypatch.setattr(cli, "watch_once", lambda *a, **k: calls.append("watch") or {"new": 0, "enriched": 0, "planned": 0})
    monkeypatch.setattr(cli, "wellness_check", lambda *a, **k: calls.append("check") or {"intervals_checks": 1})
    monkeypatch.setattr(cli, "probe", lambda *a, **k: calls.append("probe") or {"findings": []})
    _clock(monkeypatch, calls)
    st = Store(tmp_path)
    assert cli.run_loop(_loop_settings(tmp_path), st, apply=False, poll_seconds=60, sync_minutes=30, iterations=3) == 0
    assert calls == ["sync", "wellness", ("sleep", 1), "watch", "check", ("sleep", 60), "watch", "check"]
    assert [r["command"] for r in st.recent_runs(1)] == ["sync", "watch", "watch"]
    st.close()


def test_run_loop_survives_a_block_and_other_errors(tmp_path, monkeypatch):
    from garmin_intervals_bridge import cli
    calls = []
    outcomes = iter([GarminBlocked("429"), ValueError("boom"), {"seen": 0, "enriched": 0, "planned": 0}])

    def enrich(*a, **k):
        calls.append("enrich")
        o = next(outcomes)
        if isinstance(o, Exception):
            raise o
        return o
    monkeypatch.setattr(cli, "sync_enrich", enrich)
    monkeypatch.setattr(cli, "sync_wellness", lambda *a, **k: {"days_checked": 0, "writes": 0})
    monkeypatch.setattr(cli, "watch_once", lambda *a, **k: {"new": 0, "enriched": 0, "planned": 0})
    monkeypatch.setattr(cli, "wellness_check", lambda *a, **k: {})
    _clock(monkeypatch, jump=901)
    st = Store(tmp_path)
    # 1: block (full runs pause 15 min), 2: a poll, 3: the error (a break), 4: a full run – it never dies
    assert cli.run_loop(_loop_settings(tmp_path), st, apply=False, poll_seconds=60, sync_minutes=30, iterations=4) == 0
    assert calls == ["enrich", "enrich", "enrich"]       # the block, the error, then a full run that worked
    assert [r["command"] for r in st.recent_runs(1)].count("watch") >= 1
    st.close()


def test_sync_scope_all_does_the_free_scope_while_a_backfill_holds_the_other(tmp_path, monkeypatch):
    from garmin_intervals_bridge import cli
    monkeypatch.setenv("BRIDGE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GARMIN_TOKEN_DIR", str(tmp_path / "tokens"))
    monkeypatch.setenv("INTERVALS_API_KEY", "not-a-real-key")
    ran = []
    monkeypatch.setattr(cli, "sync_enrich", lambda *a, **k: ran.append("activities") or {"seen": 0})
    monkeypatch.setattr(cli, "sync_wellness", lambda *a, **k: ran.append("wellness") or {"writes": 0})
    monkeypatch.setattr(cli.GarminSource, "login", lambda self, interactive=True: None)
    with single_instance(tmp_path, ("wellness",)):                      # a wellness backfill is running
        assert main(["sync", "--scope", "all"]) == 0
    assert ran == ["activities"]


def test_a_404_answer_is_not_an_outage(tmp_path):
    from garmin_intervals_bridge.garmin import _is_transient

    class NotFound(Exception):
        status_code = 404
    assert not _is_transient(NotFound("API Error 404"))
    assert _is_transient(RuntimeError("API Error 503 Service Unavailable"))
    assert _is_transient(TimeoutError("Read timed out"))
    src = GarminSource(tmp_path, 0)
    client = _Client(failing=set())
    for method in ("get_training_readiness", "get_endurance_score", "get_hill_score", "get_max_metrics"):
        def gone(*a, **k):
            raise NotFound("API Error 404 Not Found")
        setattr(client, method, gone)
    src.client = client
    raw = src.snapshot(date(2021, 6, 1))                    # four 4xx in a row: the day goes on
    assert len(raw["errors"]) == 4 and len(raw["data"]) == len(DAY_ENDPOINTS) - 4


def test_archive_only_downloads_originals_and_leaves_intervals_alone(tmp_path):
    from test_sync import FlakyGarmin
    st = Store(tmp_path)
    g = FlakyGarmin()
    i = IntervalsFake()
    i.custom_items = lambda: (_ for _ in ()).throw(AssertionError("Intervals must not be asked"))
    out = sync_enrich(settings(tmp_path), g, i, st, apply=False, date_range=(date(2026, 10, 1), date(2026, 10, 8)),
                      archive_only=True)
    assert out["archived"] == 3 and g.requested == ["41", "42", "43"]
    assert st.activity_json_path("42").is_file() and st.fit_path("42").is_file()
    assert st.activity_extras_path("42").is_file()
    again = sync_enrich(settings(tmp_path), g, i, st, apply=False, date_range=(date(2026, 10, 1), date(2026, 10, 8)),
                        archive_only=True)
    assert again["on_disk"] == 3 and again["archived"] == 0 and len(g.requested) == 3
    st.close()


def test_endpoint_list_parsing():
    from garmin_intervals_bridge.garmin import ESSENTIAL_ENDPOINTS, parse_endpoints
    assert parse_endpoints("all") is None and parse_endpoints(None) is None
    assert parse_endpoints("essential") == ESSENTIAL_ENDPOINTS
    standard = parse_endpoints("standard")                # the backfill's default: the scores people chart too
    assert standard[:len(ESSENTIAL_ENDPOINTS)] == ESSENTIAL_ENDPOINTS
    assert {"endurance_score", "hill_score", "race_predictions"} <= set(standard)
    assert parse_endpoints("heart_rates, floors") == ("heart_rates", "floors")
    with pytest.raises(ValueError):
        parse_endpoints("heart_rates,nope")


def test_account_snapshot_collects_every_section(tmp_path):
    from garmin_intervals_bridge.garmin import ACCOUNT_ENDPOINTS

    class Account:
        profile_id = 4711

        def __getattr__(self, name):
            if name == "get_devices":
                return lambda: [{"deviceId": 99}]
            if name == "get_gear":
                return lambda n: [{"uuid": "g1"}]
            if name == "get_training_plans":
                raise RuntimeError("API Error 404")
            return lambda *a, **k: {"ok": name, "args": a}
    src = GarminSource(tmp_path, 0)
    src.client = Account()
    raw = src.account_snapshot()
    assert set(ACCOUNT_ENDPOINTS) - {"training_plans"} <= set(raw["data"])
    assert raw["data"]["device_settings"]["99"]["ok"] == "get_device_settings"
    assert raw["data"]["gear_stats"]["g1"]["ok"] == "get_gear_stats"
    assert raw["errors"] == {"training_plans": "RuntimeError"}


def test_status_code_comes_from_the_message_not_from_a_port_number():
    from garmin_intervals_bridge.garmin import _is_transient, _status_code
    reset = RuntimeError("Connection error: HTTPSConnectionPool(host='connectapi.garmin.com', port=443): "
                         "Read timed out. (read timeout=15)")
    assert _status_code(reset) is None and _is_transient(reset)
    assert _status_code(RuntimeError("API Error 404 Not Found")) == 404
    assert _status_code(RuntimeError("429 Client Error: Too Many Requests")) == 429
    assert _status_code(RuntimeError("Activity 2442901 not found")) is None


def test_run_loop_upload_mode_polls_garmin_and_uploads(tmp_path, monkeypatch):
    from garmin_intervals_bridge import cli
    calls = []
    monkeypatch.setattr(cli, "sync_activities", lambda *a, **k: calls.append(("upload", k.get("allow_upload"), k.get("activity_days")))
                        or {"seen": 1, "uploaded": 1, "would_upload": 0})
    monkeypatch.setattr(cli, "sync_wellness", lambda *a, **k: calls.append("wellness") or {"days_checked": 0, "writes": 0})
    monkeypatch.setattr(cli, "watch_once", lambda *a, **k: calls.append("watch") or {"new": 0})
    monkeypatch.setattr(cli, "wellness_check", lambda *a, **k: calls.append("check") or {})
    _clock(monkeypatch, jump=600)
    st = Store(tmp_path)
    assert cli.run_loop(_loop_settings(tmp_path), st, apply=True, poll_seconds=600, sync_minutes=30, iterations=3,
                        mode="upload") == 0
    assert calls == [("upload", True, None), "wellness", ("upload", True, 2), "check", ("upload", True, 2), "check"]
    assert "watch" not in calls
    st.close()


# ---- diagnosis next to a running bridge ----

def test_version_flag_prints_the_package_version(capsys):
    from garmin_intervals_bridge import __version__
    with pytest.raises(SystemExit) as stop:
        main(["--version"])
    assert stop.value.code == 0 and capsys.readouterr().out.strip() == f"garmin-intervals-bridge {__version__}"


def test_gap_runs_while_a_long_running_bridge_holds_every_lock(tmp_path, monkeypatch, capsys):
    from garmin_intervals_bridge import cli
    monkeypatch.setenv("BRIDGE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GARMIN_TOKEN_DIR", str(tmp_path / "tokens"))
    monkeypatch.setenv("INTERVALS_API_KEY", "not-a-real-key")

    class FakeGarmin:
        def __init__(self, *a, **k):
            pass

        def login(self, interactive=True):
            pass

        def original_fit(self, gid):
            return b"original"

    class FakeIntervals:
        def __init__(self, *a, **k):
            pass

        def activity_file(self, iid):
            return b"partner"

        def activity(self, iid):
            return {"id": iid, "source": "GARMIN_CONNECT", "external_id": "42"}
    monkeypatch.setattr(cli, "GarminSource", FakeGarmin)
    monkeypatch.setattr(cli, "IntervalsClient", FakeIntervals)
    monkeypatch.setattr(cli, "extract_original_fit", lambda data: data)
    monkeypatch.setattr(cli, "gap_report", lambda original, partner: {"original": len(original)})
    with single_instance(tmp_path, ("activities", "wellness")):            # e.g. `run` in a container
        assert main(["gap", "--activity-id", "42", "--intervals-id", "i1"]) == 0
    out = capsys.readouterr().out
    assert '"matched"' in out and "42.partner.fit" in out                  # exactly its copy: kept


def test_login_works_next_to_a_running_bridge(tmp_path, monkeypatch):
    # a container: `run` holds its locks, the terminal signs in to Garmin meanwhile
    from garmin_intervals_bridge import cli
    from garmin_intervals_bridge.store import single_instance
    monkeypatch.setenv("BRIDGE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GARMIN_TOKEN_DIR", str(tmp_path / "tokens"))
    monkeypatch.setenv("INTERVALS_API_KEY", "not-a-real-key")
    logins = []

    class Garmin:
        def __init__(self, *a, **k):
            pass

        def login(self, interactive=False):
            logins.append(interactive)

    monkeypatch.setattr(cli, "GarminSource", Garmin)
    with single_instance(tmp_path, ("activities", "wellness")):
        assert cli.main(["login"]) == 0
    assert logins == [True]


def test_enrich_one_activity_runs_next_to_a_running_bridge(tmp_path, monkeypatch, capsys):
    from garmin_intervals_bridge import cli
    monkeypatch.setenv("BRIDGE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("INTERVALS_API_KEY", "not-a-real-key")
    monkeypatch.setattr(cli.GarminSource, "login", lambda self, interactive=True: None)
    monkeypatch.setattr(cli, "resolve_one", lambda iv, ref: ("42", "i42"))
    seen = {}
    monkeypatch.setattr(cli, "enrich_activity", lambda gid, g, iv, st, **k: seen.update(gid=gid, **k)
                        or {"outcome": "planned"})
    with single_instance(tmp_path, ("activities", "wellness")):                # `run` holds these
        assert main(["enrich", "https://intervals.icu/activities/i42"]) == 0
        assert main(["enrich", "--intervals-id", "i42"]) == 0
    assert seen["gid"] == "42" and seen["intervals_id"] == "i42" and seen["recheck"] and seen["refresh_own"]
    assert main(["enrich"]) == 1                                                # names no activity


def test_upload_mode_puts_the_gear_on_the_uploaded_originals_too(tmp_path, monkeypatch):
    from garmin_intervals_bridge import cli
    calls = []
    monkeypatch.setattr(cli, "sync_activities", lambda *a, **k: calls.append("upload") or
                        {"seen": 1, "uploaded": 1, "would_upload": 0})
    monkeypatch.setattr(cli, "sync_wellness", lambda *a, **k: {"days_checked": 0, "writes": 0})
    monkeypatch.setattr(cli, "wellness_check", lambda *a, **k: {})
    monkeypatch.setattr(cli, "_gear_recent", lambda *a, **k: calls.append("gear") or {"checked": 1})
    _clock(monkeypatch, jump=600)
    st = Store(tmp_path)
    cli.run_loop(_loop_settings(tmp_path), st, apply=True, poll_seconds=600, sync_minutes=30, iterations=2,
                 mode="upload")
    assert calls == ["upload", "gear", "upload", "gear"]               # full run, then a poll that uploaded
    st.close()


def test_outside_the_poll_hours_only_the_full_runs_go_on(tmp_path, monkeypatch):
    from garmin_intervals_bridge import cli, polling
    calls = []
    monkeypatch.setattr(cli, "sync_enrich", lambda *a, **k: calls.append("sync") or {"seen": 0, "enriched": 0,
                                                                                    "planned": 0})
    monkeypatch.setattr(cli, "sync_wellness", lambda *a, **k: {"days_checked": 0, "writes": 0})
    monkeypatch.setattr(cli, "watch_once", lambda *a, **k: calls.append("watch") or {"new": 0})
    monkeypatch.setattr(cli, "wellness_check", lambda *a, **k: calls.append("check") or {})
    monkeypatch.setattr(polling, "within_hours", lambda when, ranges: not ranges)      # a window that is closed now
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    settings = _loop_settings(tmp_path)
    settings.poll_hours = ((600, 1080),)
    st = Store(tmp_path)
    cli.run_loop(settings, st, apply=False, poll_seconds=60, sync_minutes=30, iterations=3)
    assert calls == ["sync"]                                    # the full run, no polls
    st.close()


def test_watch_waits_outside_the_poll_hours(tmp_path, monkeypatch, capsys):
    from garmin_intervals_bridge import cli, polling
    monkeypatch.setenv("BRIDGE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("INTERVALS_API_KEY", "not-a-real-key")
    monkeypatch.setenv("BRIDGE_POLL_HOURS", "10-18")
    monkeypatch.setattr(polling, "within_hours", lambda when, ranges: False)
    monkeypatch.setattr(cli, "watch_once", lambda *a, **k: pytest.fail("no poll outside the window"))
    assert main(["watch", "--apply"]) == 0 and "outside BRIDGE_POLL_HOURS" in capsys.readouterr().out


def test_poll_seconds_from_the_settings(tmp_path, monkeypatch):
    from garmin_intervals_bridge import cli
    monkeypatch.setenv("BRIDGE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("INTERVALS_API_KEY", "not-a-real-key")
    monkeypatch.setenv("BRIDGE_POLL_SECONDS", "300")
    seen = {}
    monkeypatch.setattr(cli, "run_loop", lambda settings, store, **k: seen.update(k) or 0)
    assert main(["run"]) == 0 and seen["poll_seconds"] == 300
    assert main(["run", "--poll-seconds", "120"]) == 0 and seen["poll_seconds"] == 120     # the option wins
    monkeypatch.delenv("BRIDGE_POLL_SECONDS")
    assert main(["run", "--mode", "upload"]) == 0 and seen["poll_seconds"] == 600


def test_a_poll_counts_only_its_own_garmin_requests(tmp_path, monkeypatch):
    """The polls share the full run's Garmin session; summing its total per poll made 50 requests
    look like 2000 in the daily summary (reported in the forum)."""
    from garmin_intervals_bridge import cli

    class Counting:
        def __init__(self, *a, **k):
            self.requests, self.client = 0, None

    def full_run(settings, garmin, *a, **k):
        garmin.requests += 50                                # the full run's downloads and reads
        return {"seen": 1, "enriched": 1, "planned": 0, "scan_skipped": 1}

    def poll(settings, garmin, *a, **k):
        garmin.requests += 1 if poll.calls == 0 else 0      # one request in the first poll, none after
        poll.calls += 1
        return {"new": 0, "enriched": 0, "planned": 0}
    poll.calls = 0
    monkeypatch.setattr(cli, "GarminSource", Counting)
    monkeypatch.setattr(cli, "sync_enrich", full_run)
    monkeypatch.setattr(cli, "sync_wellness", lambda *a, **k: {"days_checked": 0, "writes": 0})
    monkeypatch.setattr(cli, "watch_once", poll)
    monkeypatch.setattr(cli, "wellness_check", lambda *a, **k: {})
    _clock(monkeypatch)
    st = Store(tmp_path)
    cli.run_loop(_loop_settings(tmp_path), st, apply=True, poll_seconds=60, sync_minutes=30, iterations=4)
    assert [r["metrics"]["garmin_requests"] for r in st.recent_runs(1)] == [50, 1, 0, 0]
    st.close()


def test_a_429_from_intervals_pauses_the_bridge(tmp_path, monkeypatch, capsys):
    from requests import HTTPError, Response
    from requests.exceptions import RetryError

    from garmin_intervals_bridge import cli
    from garmin_intervals_bridge.intervals import rate_limited

    limited = Response()
    limited.status_code, limited.headers["Retry-After"] = 429, "3600"
    assert rate_limited(HTTPError("429", response=limited)) == 3600
    assert rate_limited(RetryError("Max retries exceeded (too many 429 error responses)")) == 0
    other = Response()
    other.status_code = 500
    assert rate_limited(HTTPError("500", response=other)) is None and rate_limited(ValueError()) is None

    calls = []

    def poll(*a, **k):
        calls.append("poll")
        raise HTTPError("429", response=limited)
    monkeypatch.setattr(cli, "sync_enrich", lambda *a, **k: calls.append("sync") or {"seen": 0, "enriched": 0,
                                                                                    "planned": 0})
    monkeypatch.setattr(cli, "sync_wellness", lambda *a, **k: {"days_checked": 0, "writes": 0})
    monkeypatch.setattr(cli, "watch_once", poll)
    monkeypatch.setattr(cli, "wellness_check", lambda *a, **k: {})
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    st = Store(tmp_path)
    cli.run_loop(_loop_settings(tmp_path), st, apply=True, poll_seconds=300, sync_minutes=30, iterations=4)
    assert calls == ["sync", "poll"]                      # after the 429: nothing more for its hour
    assert 3500 < cli.intervals_paused(st) <= 3601                # Retry-After, rounded to the second
    st.close()
    # the timers skip their runs while the pause lasts
    monkeypatch.setenv("BRIDGE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("INTERVALS_API_KEY", "not-a-real-key")
    monkeypatch.setattr(cli, "watch_once", lambda *a, **k: pytest.fail("no poll during the pause"))
    assert main(["watch", "--apply"]) == 0 and "HTTP 429" in capsys.readouterr().out
