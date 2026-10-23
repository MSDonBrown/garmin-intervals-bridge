"""sync-now: the result, the exit codes, the locks, and the timed syncs stepping back."""
from __future__ import annotations

import json
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from garmin_intervals_bridge import ondemand, polling, tuning
from garmin_intervals_bridge.garmin import GarminBlocked, GarminLoginNeeded
from garmin_intervals_bridge.store import Store, single_instance


class FakeGarmin:
    requests = 0
    client = None

    def login(self, interactive=False):
        pass


def settings(tmp_path, **kw):
    base = dict(data_dir=tmp_path, mode="enrich", intervals_api_key="k", intervals_athlete_id="0",
                token_dir=tmp_path / "tokens", garmin_request_delay=0, timezone=ZoneInfo("UTC"))
    return SimpleNamespace(**{**base, **kw})


def args(**kw):
    base = dict(apply=True, scope=None, mode=None, activity=None, garmin_id=None, day=None, wait_new=False,
                force=False)
    return SimpleNamespace(**{**base, **kw})


class Clock:
    def __init__(self):
        self.now = 1_000_000.0
        self.slept = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in tuning.TUNABLES:
        monkeypatch.delenv(name, raising=False)


def run(tmp_path, a, clock=None, **kw):
    clock = clock or Clock()
    code = ondemand.run(a, settings(tmp_path), garmin=FakeGarmin(), intervals=object(), clock=clock.time,
                        sleep=clock.sleep)
    return code, clock


def result(capsys):
    return json.loads(capsys.readouterr().out)


def fake_activities(monkeypatch, outcomes):
    calls = []

    def activities(*a, **k):
        calls.append(1)
        new, again = outcomes[min(len(calls), len(outcomes)) - 1]
        return {"seen": 1}, new, again

    monkeypatch.setattr(ondemand, "_activities", activities)
    monkeypatch.setattr(ondemand, "_wellness", lambda *a, **k: ({"writes": 2}, 2))
    return calls


def test_done_with_what_was_new(tmp_path, monkeypatch, capsys):
    fake_activities(monkeypatch, [(1, False)])
    code, _ = run(tmp_path, args())
    out = result(capsys)
    assert code == 0 and out["status"] == "done" and out["new"] == 3 and out["exit_code"] == 0
    assert out["scope"] == ["activities", "wellness"] and out["apply"] is True


def test_wait_new_looks_again_then_succeeds(tmp_path, monkeypatch, capsys):
    calls = fake_activities(monkeypatch, [(0, True), (0, True), (1, False)])
    code, clock = run(tmp_path, args(scope="activities", wait_new=True))
    out = result(capsys)
    assert code == 0 and len(calls) == 3 and out["attempts"] == 3
    assert clock.slept == [tuning.get("BRIDGE_ONDEMAND_RETRY_SECONDS")] * 2


def test_wait_new_gives_up_as_pending(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BRIDGE_ONDEMAND_RETRIES", "2")
    calls = fake_activities(monkeypatch, [(0, True)])
    code, _ = run(tmp_path, args(scope="activities", wait_new=True))
    out = result(capsys)
    assert code == 2 and out["status"] == "pending" and len(calls) == 3


def test_without_wait_new_nothing_new_is_still_done(tmp_path, monkeypatch, capsys):
    calls = fake_activities(monkeypatch, [(0, True)])
    code, _ = run(tmp_path, args(scope="activities"))
    assert code == 0 and len(calls) == 1 and result(capsys)["status"] == "done"


def test_the_overall_deadline_ends_the_waiting(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BRIDGE_ONDEMAND_MAX_SECONDS", "60")
    monkeypatch.setenv("BRIDGE_ONDEMAND_RETRIES", "50")
    calls = fake_activities(monkeypatch, [(0, True)])
    code, _ = run(tmp_path, args(scope="activities", wait_new=True))
    out = result(capsys)
    assert code == 2 and out["deadline_reached"] is True and len(calls) == 1


def test_a_held_part_is_waited_for_then_busy(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BRIDGE_ONDEMAND_LOCK_WAIT_SECONDS", "10")
    calls = fake_activities(monkeypatch, [(1, False)])
    with single_instance(tmp_path, ("activities",)):
        code, clock = run(tmp_path, args(scope="activities"))
    out = result(capsys)
    assert code == 5 and out["status"] == "busy" and out["part"] == "activities" and calls == []
    assert sum(clock.slept) >= 10


def test_only_the_needed_part_is_locked(tmp_path, monkeypatch, capsys):
    fake_activities(monkeypatch, [(1, False)])
    with single_instance(tmp_path, ("wellness",)):                 # a backfill of wellness runs
        code, _ = run(tmp_path, args(scope="activities"))
    assert code == 0 and result(capsys)["status"] == "done"


def test_apply_makes_the_timed_syncs_step_back(tmp_path, monkeypatch, capsys):
    fake_activities(monkeypatch, [(1, False)])
    clock = Clock()
    run(tmp_path, args(), clock)
    capsys.readouterr()
    store = Store(tmp_path)
    try:
        assert polling.ondemand_done(store) == clock.now
        assert polling.poll_holdoff_until(store) == clock.now + 900
        assert polling.sync_holdoff_until(store) == clock.now + 30 * 60
        due, why = polling.due(settings(tmp_path), store, clock.now + 60, 300)
        assert due is False and "on-demand" in why
        assert polling.due(settings(tmp_path), store, clock.now + 901, 300)[0] is True
    finally:
        store.close()


def test_a_dry_run_delays_nothing(tmp_path, monkeypatch, capsys):
    fake_activities(monkeypatch, [(1, False)])
    run(tmp_path, args(apply=False))
    out = result(capsys)
    assert out["apply"] is False
    store = Store(tmp_path)
    try:
        assert polling.ondemand_done(store) is None and polling.sync_holdoff_until(store) == 0
    finally:
        store.close()


def test_holdoff_zero_means_none(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BRIDGE_ONDEMAND_SYNC_HOLDOFF_MINUTES", "0")
    monkeypatch.setenv("BRIDGE_ONDEMAND_POLL_HOLDOFF_SECONDS", "0")
    fake_activities(monkeypatch, [(1, False)])
    clock = Clock()
    run(tmp_path, args(), clock)
    store = Store(tmp_path)
    try:
        assert polling.sync_holdoff_until(store) == clock.now and polling.poll_holdoff_until(store) == clock.now
    finally:
        store.close()


def test_a_repeated_request_returns_the_last_result(tmp_path, monkeypatch, capsys):
    calls = fake_activities(monkeypatch, [(1, False)])
    clock = Clock()
    run(tmp_path, args(), clock)
    capsys.readouterr()
    clock.now += 20
    run(tmp_path, args(), clock)
    again = result(capsys)
    assert again["coalesced"] is True and len(calls) == 1
    clock.now += 100
    run(tmp_path, args(), clock)
    assert result(capsys).get("coalesced") is None and len(calls) == 2


def test_force_and_named_targets_always_ask(tmp_path, monkeypatch, capsys):
    calls = fake_activities(monkeypatch, [(1, False)])
    clock = Clock()
    run(tmp_path, args(), clock)
    run(tmp_path, args(force=True), clock)
    run(tmp_path, args(day="2026-10-22"), clock)
    assert len(calls) == 2           # the day-only request does wellness alone
    capsys.readouterr()


def test_a_day_alone_is_wellness_only(tmp_path):
    assert ondemand.scopes_for(args(day="2026-10-22")) == ("wellness",)
    assert ondemand.scopes_for(args(activity="i1")) == ("activities",)
    assert ondemand.scopes_for(args()) == ("activities", "wellness")
    assert ondemand.scopes_for(args(scope="wellness")) == ("wellness",)


@pytest.mark.parametrize("exc,status,code", [
    (GarminLoginNeeded("tokens rejected"), "login_needed", 4),
    (GarminBlocked("429"), "paused", 3),
    (RuntimeError("boom"), "error", 1),
])
def test_failures_have_their_own_status(tmp_path, monkeypatch, capsys, exc, status, code):
    def boom(*a, **k):
        raise exc
    monkeypatch.setattr(ondemand, "_activities", boom)
    monkeypatch.setattr(ondemand, "_wellness", boom)
    got, _ = run(tmp_path, args(apply=False))
    out = result(capsys)
    assert got == code and out["status"] == status and out["exit_code"] == code and "message" in out


def test_intervals_429_pauses_everything(tmp_path, monkeypatch, capsys):
    import requests

    class Resp:
        status_code = 429
        headers = {"Retry-After": "120"}
        url = "https://intervals.icu/api"
    err = requests.exceptions.HTTPError("429", response=Resp())

    def boom(*a, **k):
        raise err
    monkeypatch.setattr(ondemand, "_activities", boom)
    code, _ = run(tmp_path, args(scope="activities"))
    out = result(capsys)
    assert code == 3 and out["status"] == "paused" and out["retry_after_seconds"] >= 900
    code, _ = run(tmp_path, args(scope="activities"))             # the pause is remembered
    assert code == 3 and result(capsys)["status"] == "paused"


def test_backup_mode_has_nothing_to_do(tmp_path, capsys):
    code = ondemand.run(args(), settings(tmp_path, mode="backup"))
    out = result(capsys)
    assert code == 1 and out["status"] == "error" and "backup" in out["message"]


def test_the_command_is_wired_into_the_cli(tmp_path, monkeypatch, capsys):
    from garmin_intervals_bridge import cli
    monkeypatch.setenv("BRIDGE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GARMIN_TOKEN_DIR", str(tmp_path / "tokens"))
    monkeypatch.setenv("INTERVALS_API_KEY", "k")
    seen = {}
    monkeypatch.setattr(ondemand, "run", lambda a, s: seen.setdefault("args", a) and 5)
    assert cli.main(["sync-now", "--apply", "--wait-new", "--activity", "i42"]) == 5
    a = seen["args"]
    assert a.apply and a.wait_new and a.activity == "i42" and a.scope is None
    capsys.readouterr()


def test_the_run_loop_steps_back_after_an_on_demand_sync(tmp_path):
    store = Store(tmp_path)
    try:
        store.set_meta(polling.ONDEMAND_KEY, "5000.0")
        assert polling.sync_holdoff_until(store) == 5000.0 + 30 * 60
        assert polling.seconds_to_next(settings(tmp_path), store, 5100.0, 300) is not None
    finally:
        store.close()


# ---- against the real sync code, with the fakes of test_sync_enrich ----

def _enrich_settings(tmp_path):
    return SimpleNamespace(data_dir=tmp_path, timezone=ZoneInfo("Europe/Vienna"), activity_days=3,
                           mode="enrich", intervals_api_key="k", intervals_athlete_id="0",
                           token_dir=tmp_path / "tokens", garmin_request_delay=0)


def test_enrich_end_to_end_writes_and_reports(tmp_path, capsys):
    from test_sync_enrich import GarminFake, IntervalsFake
    g, i, clock = GarminFake(), IntervalsFake(), Clock()
    g.client, g.requests, g.login = None, 0, lambda interactive=False: None
    code = ondemand.run(args(scope="activities"), _enrich_settings(tmp_path), garmin=g, intervals=i,
                        clock=clock.time, sleep=clock.sleep)
    out = result(capsys)
    assert code == 0 and out["new"] == 1 and out["activities"]["enriched"] == 1 and i.updates
    # the same call again, past the gap: nothing new, still done
    clock.now += 500
    code = ondemand.run(args(scope="activities"), _enrich_settings(tmp_path), garmin=g, intervals=i,
                        clock=clock.time, sleep=clock.sleep)
    assert code == 0 and result(capsys)["new"] == 0


def test_enrich_waits_for_the_official_import(tmp_path, capsys):
    from test_sync_enrich import GarminFake, IntervalsFake
    g, i, clock = GarminFake(), IntervalsFake(), Clock()
    g.client, g.requests, g.login = None, 0, lambda interactive=False: None
    i.remote = []                                         # Intervals has not imported it yet
    code = ondemand.run(args(scope="activities", wait_new=True), _enrich_settings(tmp_path), garmin=g,
                        intervals=i, clock=clock.time, sleep=clock.sleep)
    out = result(capsys)
    assert code == 2 and out["status"] == "pending" and out["attempts"] == tuning.get("BRIDGE_ONDEMAND_RETRIES") + 1
    assert out["activities"]["unmatched"] == 1


def test_wellness_ignores_the_refresh_window(tmp_path, monkeypatch):
    from garmin_intervals_bridge import sync
    seen = []
    monkeypatch.setattr(sync, "sync_wellness", lambda *a, **k: seen.append(k) or {"writes": 1})
    a = args(day="2026-10-22")
    metrics, new = ondemand._wellness(a, None, None, None, None)
    assert new == 1 and seen[0]["force"] is True and [d.isoformat() for d in seen[0]["days"]] == ["2026-10-22"]
    ondemand._wellness(args(), None, None, None, None)
    assert seen[1]["force"] is True and seen[1]["wellness_days"] == 2
