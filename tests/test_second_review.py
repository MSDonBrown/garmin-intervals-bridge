"""The second review's findings on running the bridge: rate limits, failing full runs, the
look-for-new-activities timing, health, retries outside the scan window."""

import sys
import time
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import requests
from test_review_fixes import _clock, _loop_settings
from test_webui import call, csrf, env, page  # noqa: F401  (fixtures)

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
    listing = [{"activityId": n, "startTimeLocal": f"2026-10-0{1 if n == 0 else 5} 08:00:00"} for n in range(100)]
    asked = []

    def call(name, *args):
        asked.append(name)
        return listing if name == "get_activities" else listing + [{"activityId": 100, "startTimeLocal": "2026-10-01 07:00"}]
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


# ---- the web page and the settings file ----

def _web():
    from test_webui import FakeGarmin, FakeIntervals, serve
    return FakeGarmin, FakeIntervals, serve


def test_a_negative_or_broken_length_is_refused_before_anything_is_read(page):  # noqa: F811
    import http.client
    for length in ("-1", "lots"):
        conn = http.client.HTTPConnection("127.0.0.1", page.port, timeout=10)
        conn.putrequest("POST", "/login")
        conn.putheader("Host", f"127.0.0.1:{page.port}")
        conn.putheader("Content-Length", length)
        conn.endheaders()
        assert conn.getresponse().status in (400, 413)
        conn.close()


def test_more_connections_than_allowed_are_closed_not_served(env, monkeypatch):  # noqa: F811
    import socket

    from garmin_intervals_bridge import webui
    _, _, serve = _web()
    monkeypatch.setattr(webui, "MAX_CONNECTIONS", 2)
    monkeypatch.setattr(webui, "REQUEST_SECONDS", 3)
    ui = webui.UI(host="127.0.0.1", port=0, key="the-key", newer_release=lambda: None)
    server = serve(ui)
    idle = [socket.create_connection(("127.0.0.1", ui.port)) for _ in range(2)]   # sending nothing
    try:
        time.sleep(0.3)
        extra = socket.create_connection(("127.0.0.1", ui.port))
        extra.settimeout(2)
        extra.sendall(b"GET / HTTP/1.0\r\n\r\n")
        assert extra.recv(100) == b""                                       # closed at once
        extra.close()
        time.sleep(3.5)                                                    # the idle ones time out ...
        assert call(ui, "GET", "/")[0] in (303, 403)                        # ... and the page answers again
    finally:
        for s in idle:
            s.close()
        server.shutdown()
        server.server_close()


def test_password_guesses_take_turns_and_https_keeps_the_cookie_on_https(env, monkeypatch):  # noqa: F811
    from garmin_intervals_bridge import webui
    FakeGarmin, FakeIntervals, serve = _web()
    monkeypatch.setattr(webui.time, "sleep", lambda s: None)
    ui = webui.UI(host="127.0.0.1", port=0, password="long enough", in_run=True, intervals_client=FakeIntervals,
                  garmin_source=FakeGarmin, newer_release=lambda: None)
    server = serve(ui)
    try:
        ui.login_gate.acquire()                                            # a guess is being checked
        assert "Another sign-in is being checked" in call(ui, "POST", "/login", {"password": "x"})[2]
        ui.login_gate.release()
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", ui.port, timeout=10)
        conn.request("POST", "/login", body="password=long+enough", headers={
            "Host": f"127.0.0.1:{ui.port}", "X-Forwarded-Proto": "https",
            "Content-Type": "application/x-www-form-urlencoded"})
        cookie = conn.getresponse().getheader("Set-Cookie")
        assert cookie.endswith("; Secure")
        conn.close()
        status, headers, _ = call(ui, "POST", "/login", {"password": "long enough"})
        assert "Secure" not in headers["set-cookie"]                       # plain http at home: as before
    finally:
        server.shutdown()
        server.server_close()


def test_settings_lines_keep_their_value_and_take_no_second_line(tmp_path):
    from garmin_intervals_bridge.config import parse_env_file
    from garmin_intervals_bridge.wizard import write_env
    path = tmp_path / ".env"
    with pytest.raises(ValueError, match="line break"):
        write_env(path, {"BRIDGE_SMTP_HOST": "smtp.example\nBRIDGE_DATA_DIR=/elsewhere"})
    values = {"BRIDGE_SMTP_PASSWORD": '"quoted"', "BRIDGE_SMTP_FROM": " spaced ", "BRIDGE_ALERT_EMAIL": "a@b.c"}
    write_env(path, values)
    assert {k: parse_env_file(path)[k] for k in values} == values


def test_two_saves_at_once_lose_nothing(tmp_path):
    import threading

    from garmin_intervals_bridge.config import parse_env_file
    from garmin_intervals_bridge.wizard import write_env
    path = tmp_path / ".env"
    write_env(path, {"INTERVALS_API_KEY": "k"})
    errors = []

    def save(n):
        try:
            for i in range(40):
                write_env(path, {f"BRIDGE_TEST_{n}": str(i)})
        except Exception as exc:
            errors.append(exc)
    threads = [threading.Thread(target=save, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    values = parse_env_file(path)
    assert errors == [] and values["INTERVALS_API_KEY"] == "k"
    assert all(values[f"BRIDGE_TEST_{n}"] == "39" for n in range(4))
    assert not list(tmp_path.glob("*.tmp"))


def test_a_setting_the_bridge_could_not_start_with_is_not_kept(page, env):  # noqa: F811
    from test_webui import signed_in
    cookie = signed_in(page)
    before = env.read_text()
    token = csrf(call(page, "GET", "/settings", cookie=cookie)[2])
    for wrong, says in (({"alert_email": "a@b.c", "smtp_port": "65536"}, "does not look right"),
                        ({"poll_hours": "24-0"}, "no range of the day")):
        text = call(page, "POST", "/settings", {"csrf": token, "timezone": "Europe/Vienna", **wrong},
                    cookie=cookie)[2]
        assert says in text and "nothing was changed" in text
    assert env.read_text() == before
    assert call(page, "GET", "/", cookie=cookie)[0] == 200                  # the page still works


def test_poll_hours_are_read_strictly():
    from garmin_intervals_bridge.config import poll_hours
    assert poll_hours("22-24") == ((1320, 1440),)
    for wrong in ("6:99-9", "24-0", "7\n-9", "0-24:30", "10-10"):
        with pytest.raises(ValueError, match="BRIDGE_POLL_HOURS"):
            poll_hours(wrong)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_a_rotated_log_stays_private(tmp_path, monkeypatch):
    import logging
    import os
    from logging.handlers import RotatingFileHandler
    old = os.umask(0o022)
    try:
        path = tmp_path / "logs" / "bridge.log"
        cli._log_to_file(path, logging.INFO)
        handler = next(h for h in logging.getLogger().handlers if isinstance(h, RotatingFileHandler)
                       and h.baseFilename == str(path))
        logging.getLogger("bridge").info("a line")
        handler.doRollover()
        logging.getLogger("bridge").info("another line")
        assert oct(path.stat().st_mode & 0o777) == "0o600"
        assert oct(path.parent.stat().st_mode & 0o777) == "0o700"
    finally:
        os.umask(old)


# ---- enriching: what is the athlete's, which activity, which second ----

def test_a_value_typed_in_stays_a_placeholder_or_the_bridges_own_is_replaced():
    from garmin_intervals_bridge.enrich import FieldMappings, ScalarMapping, plan_scalars
    mappings = FieldMappings(scalars=[ScalarMapping("RecoveryTime", "mesg:140.9", (("/", 60.0),))])
    original = {"140": [{9: 2160}]}                     # 36 h, as the device wrote it
    partner = {"140": [{}]}                             # Garmin's copy lacks it
    typed = plan_scalars(original, {"RecoveryTime": 24}, mappings, partner)
    assert typed["writes"] == {} and typed["kept_existing"] == {"RecoveryTime": 24}
    placeholder = plan_scalars(original, {"RecoveryTime": 0}, mappings, partner)
    assert placeholder["writes"] == {"RecoveryTime": 36.0}
    ours = plan_scalars(original, {"RecoveryTime": 0.6}, mappings, partner, own={"RecoveryTime": 0.6})
    assert ours["writes"] == {"RecoveryTime": 36.0}    # the bridge's own (older units): put right


def test_the_exact_activity_wins_and_two_lookalikes_are_no_match():
    from garmin_intervals_bridge.enrich import match_activity
    garmin = {"activityId": 42, "startTimeGMT": "2026-10-07 08:00:00", "duration": 3600}
    near = {"id": "i1", "source": "GARMIN_CONNECT", "start_date": "2026-10-07T08:00:30Z"}       # no duration
    exact = {"id": "i2", "source": "GARMIN_CONNECT", "external_id": "42", "start_date": "2026-10-07T09:00:00Z"}
    assert match_activity(garmin, [near, exact])["id"] == "i2"
    assert match_activity(garmin, [near]) is None                         # a duration is needed too
    watch = {"id": "i3", "source": "GARMIN_CONNECT", "start_date": "2026-10-07T08:00:10Z", "moving_time": 3590}
    edge = dict(watch, id="i4", start_date="2026-10-07T08:00:20Z")
    assert match_activity(garmin, [watch]) == watch and match_activity(garmin, [watch, edge]) is None
    other = dict(watch, id="i5", external_id="43")                        # names another recording
    assert match_activity(garmin, [other]) is None


def test_a_stream_is_not_written_when_its_second_cannot_be_checked():
    from garmin_intervals_bridge.enrich import _starts_differ
    ours = {"record": [{"timestamp": 1000}, {"timestamp": 1001}]}
    assert not _starts_differ(ours, {"record": [{"timestamp": 1000}]})
    assert _starts_differ(ours, {"record": [{"timestamp": 1005}]})
    assert not _starts_differ(ours, {})                                   # nothing to compare with


def test_a_long_running_bridge_sees_fields_added_since(monkeypatch):
    from garmin_intervals_bridge.intervals import IntervalsClient
    answers = [[{"type": "ACTIVITY_FIELD", "content": {"code": "A"}}],
               [{"type": "ACTIVITY_FIELD", "content": {"code": "A"}}, {"type": "ACTIVITY_STREAM",
                                                                       "content": {"code": "New"}}]]
    client = IntervalsClient("k", "0")
    monkeypatch.setattr(client, "_request", lambda *a, **k: answers.pop(0))
    assert len(client.custom_items()) == 1 and len(client.custom_items()) == 1     # one request
    client.forget_custom_items()                                           # a full run of `run`
    assert len(client.custom_items()) == 2
