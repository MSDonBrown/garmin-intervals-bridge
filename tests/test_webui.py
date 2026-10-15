"""The web page: access (key, password, host, CSRF), Garmin sign-in with MFA, settings, actions, log."""
import http.client
import json
import re
import threading
import time
from urllib.parse import urlencode

import pytest
import requests

from garmin_intervals_bridge import config, notices, webui
from garmin_intervals_bridge.store import Store


class FakeIntervals:
    def __init__(self, key, athlete_id="0"):
        self.key = key

    def athlete(self):
        if self.key not in ("k", "new-key"):
            response = requests.Response()
            response.status_code = 401
            raise requests.HTTPError("401", response=response)
        return {"name": "Ann"}


GARMIN_PASSWORD = "Garmin-Secret-7f3k"        # distinctive: a random CSRF token never contains it


class FakeGarmin:
    def __init__(self, token_dir, delay):
        self.token_dir = token_dir

    def start_login(self, email, password):
        assert password == GARMIN_PASSWORD and email == "ann@example.com"
        return "mfa"

    def finish_login(self, code):
        if code != "123456":
            raise RuntimeError("wrong code")
        self.token_dir.mkdir(parents=True, exist_ok=True)
        (self.token_dir / "garmin_tokens.json").write_text("{}")


@pytest.fixture
def env(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("INTERVALS_API_KEY=k\nBRIDGE_TIMEZONE=Europe/Vienna\nBRIDGE_LOG_FILE=bridge.log\n")
    monkeypatch.setenv("BRIDGE_ENV_FILE", str(path))
    for name in ("INTERVALS_API_KEY", "BRIDGE_TIMEZONE", "BRIDGE_DATA_DIR", "GARMIN_TOKEN_DIR", "BRIDGE_LOG_FILE",
                 "BRIDGE_IMREADY4_ENABLED", "BRIDGE_HRVTREND_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(webui.autostart, "platform", lambda: "linux")
    monkeypatch.setattr(webui.autostart, "status", lambda env: {"installed": False, "kind": "linux", "detail": ""})
    config.Settings.from_env().ensure_data_dir()
    return path


def serve(ui):
    server = webui.make_server(ui)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def page(env):
    ui = webui.UI(host="127.0.0.1", port=0, key="the-key", intervals_client=FakeIntervals, garmin_source=FakeGarmin,
                  newer_release=lambda: None)
    server = serve(ui)
    yield ui
    server.shutdown()
    server.server_close()


def call(ui, method, path, body=None, cookie=None, host=None, origin=None):
    conn = http.client.HTTPConnection("127.0.0.1", ui.port, timeout=10)
    headers = {"Host": host or f"127.0.0.1:{ui.port}"}
    if cookie:
        headers["Cookie"] = cookie
    if origin:
        headers["Origin"] = origin
    data = None
    if body is not None:
        data = urlencode(body).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    conn.request(method, path, body=data, headers=headers)
    response = conn.getresponse()
    text = response.read().decode()
    conn.close()
    return response.status, {k.lower(): v for k, v in response.getheaders()}, text


def signed_in(ui):
    status, headers, _ = call(ui, "GET", "/?key=the-key")
    assert status == 303 and headers["location"] == "/"
    cookie = headers["set-cookie"].split(";", 1)[0]
    assert "HttpOnly" in headers["set-cookie"] and "SameSite=Strict" in headers["set-cookie"]
    return cookie


def csrf(text):
    return re.search(r'name="csrf" value="([^"]+)"', text).group(1)


def test_the_page_opens_only_with_the_key_from_the_terminal(page):
    assert call(page, "GET", "/")[0] == 403
    assert call(page, "GET", "/?key=wrong")[0] == 403
    cookie = signed_in(page)
    status, headers, text = call(page, "GET", "/", cookie=cookie)
    assert status == 200 and "Status" in text and "Not running" in text
    assert "default-src 'none'" in headers["content-security-policy"] and headers["cache-control"] == "no-store"
    assert headers["x-frame-options"] == "DENY"


def test_a_foreign_host_name_is_refused_dns_rebinding(page):
    cookie = signed_in(page)
    assert call(page, "GET", "/", cookie=cookie, host=f"evil.example:{page.port}")[0] == 403
    assert call(page, "GET", "/", cookie=cookie, host=f"localhost:{page.port}")[0] == 200


def test_forms_need_their_token_and_this_origin(page, env):
    cookie = signed_in(page)
    form = {"timezone": "Europe/Dublin"}
    assert call(page, "POST", "/settings", form, cookie=cookie)[0] == 403                     # no token
    token = csrf(call(page, "GET", "/settings", cookie=cookie)[2])
    assert call(page, "POST", "/settings", {**form, "csrf": token}, cookie=cookie,
                origin="https://evil.example")[0] == 403
    assert "Europe/Vienna" in env.read_text()                                               # nothing changed


def test_garmin_sign_in_with_mfa_ends_the_login_episode(page, env):
    settings = config.Settings.from_env()
    store = Store(settings.data_dir)
    store.set_meta(notices.LOGIN_NOTE_KEY, "2026-10-12")                  # the calendar note is out
    store.close()
    cookie = signed_in(page)
    text = call(page, "GET", "/", cookie=cookie)[2]
    assert "sign-in needed" in text
    token = csrf(call(page, "GET", "/garmin", cookie=cookie)[2])
    status, _, text = call(page, "POST", "/garmin", {"csrf": token, "email": "ann@example.com", "password": GARMIN_PASSWORD},
                           cookie=cookie)
    assert status == 200 and 'name="code"' in text and GARMIN_PASSWORD not in text            # never echoed
    text = call(page, "POST", "/garmin/mfa", {"csrf": token, "code": "000000"}, cookie=cookie)[2]
    assert "did not accept the code" in text and 'name="code"' in text                   # try again
    text = call(page, "POST", "/garmin/mfa", {"csrf": token, "code": "123456"}, cookie=cookie)[2]
    assert "Signed in" in text
    store = Store(settings.data_dir)
    assert not store.get_meta(notices.LOGIN_NOTE_KEY) and store.meta_updated("garmin_last_ok")
    store.close()


def test_a_slow_garmin_sign_in_shows_a_page_that_waits_for_it(page, env, monkeypatch):
    gate = threading.Event()

    class SlowGarmin(FakeGarmin):
        def start_login(self, email, password):
            gate.wait(10)
            return super().start_login(email, password)

    page.garmin_source = SlowGarmin
    monkeypatch.setattr(webui, "GARMIN_WAIT", 0.05)
    cookie = signed_in(page)
    token = csrf(call(page, "GET", "/garmin", cookie=cookie)[2])
    status, headers, _ = call(page, "POST", "/garmin", {"csrf": token, "email": "ann@example.com",
                                                        "password": GARMIN_PASSWORD}, cookie=cookie)
    assert status == 303 and headers["location"] == "/garmin"            # a GET to reload, never the form again
    text = call(page, "GET", "/garmin", cookie=cookie)[2]
    assert "Signing in to Garmin Connect" in text and 'http-equiv=refresh' in text and GARMIN_PASSWORD not in text
    assert call(page, "POST", "/garmin", {"csrf": token, "email": "x@example.com", "password": "x"},
                cookie=cookie)[0] == 303                                 # one sign-in at a time
    gate.set()
    for _ in range(100):
        text = call(page, "GET", "/garmin", cookie=cookie)[2]
        if "Signing in" not in text:
            break
        time.sleep(0.05)
    assert "accepted the e-mail and password" in text and 'name="code"' in text and "http-equiv" not in text
    assert 'name="code"' in call(page, "GET", "/garmin", cookie=cookie)[2]   # a reload keeps asking for the code


def test_two_requests_at_once_start_one_sign_in_and_take_its_answer_once(page):
    session = webui.Session()
    job = page._garmin_start(session, "password", lambda: (None, "ok"))
    assert job.done.wait(5)
    page._garmin_take(session, job)                                     # the first reload answers
    with pytest.raises(webui.Reply) as second:
        page._garmin_take(session, job)                                 # the other one shows the page anew
    assert second.value.status == 303 and session.garmin_job is None
    gate = threading.Event()
    page._garmin_start(session, "password", lambda: gate.wait(5))
    with pytest.raises(webui.Reply):
        page._garmin_start(session, "password", lambda: None)          # a second click while Garmin answers
    gate.set()


def test_settings_are_saved_and_the_key_is_checked_but_never_shown(page, env):
    cookie = signed_in(page)
    text = call(page, "GET", "/settings", cookie=cookie)[2]
    assert "Europe/Vienna" in text and "INTERVALS_API_KEY" not in text and 'value="k"' not in text
    token = csrf(text)
    text = call(page, "POST", "/settings", {"csrf": token, "timezone": "Atlantis"}, cookie=cookie)[2]
    assert "is not a time zone name" in text
    text = call(page, "POST", "/settings", {"csrf": token, "timezone": "Europe/Dublin", "api_key": "bad"},
                cookie=cookie)[2]
    assert "did not accept this key" in text and "Europe/Vienna" in env.read_text()
    text = call(page, "POST", "/settings", {"csrf": token, "timezone": "Europe/Dublin", "imready4": "1",
                                            "api_key": "new-key"}, cookie=cookie)[2]
    assert "Saved" in text and "new-key" not in text
    values = config.parse_env_file(env)
    assert values["BRIDGE_TIMEZONE"] == "Europe/Dublin" and values["INTERVALS_API_KEY"] == "new-key"
    assert values["BRIDGE_IMREADY4_ENABLED"] == "true" and values["BRIDGE_HRVTREND_ENABLED"] == "false"
    assert values["BRIDGE_LOG_FILE"] == "bridge.log"                                     # other lines kept


def test_settings_from_a_container_are_shown_not_edited(page, env, monkeypatch, tmp_path):
    monkeypatch.delenv("BRIDGE_ENV_FILE")
    monkeypatch.chdir(tmp_path / "data")
    monkeypatch.setenv("INTERVALS_API_KEY", "k")
    cookie = signed_in(page)
    text = call(page, "GET", "/settings", cookie=cookie)[2]
    assert "container" in text and "<form" not in text


def test_the_log_page_shows_the_end_of_the_log_escaped(page, env):
    (env.parent / "bridge.log").write_text("".join(f"line {n}\n" for n in range(500)) + "<script>x</script>\n")
    cookie = signed_in(page)
    text = call(page, "GET", "/log", cookie=cookie)[2]
    assert "line 499" in text and "line 100\n" not in text and "&lt;script&gt;" in text and "<script>" not in text


def test_gear_applies_exactly_the_plan_that_was_shown(page, env, monkeypatch):
    shown = {"policy": "intervals", "activities": 1, "without_gear_info": 0, "pairs": {}, "unpaired": {},
             "create": [], "assign": [{"garmin": "Bike", "target": "b1", "intervals": None, "type": "Ride",
                                       "day": "2026-10-11", "km": 40.0, "reason": "gap", "intervals_id": "i1"}],
             "kept": [], "intervals_only": {}, "skipped": {}, "_names": {"b1": "Bike"}}
    applied = []
    monkeypatch.setattr(webui, "build_gear_plan", lambda *a, **k: shown)
    monkeypatch.setattr(webui, "apply_gear_plan",
                        lambda plan, iv: applied.append(plan) or {"written": 1, "created": [], "failed": 0, "ids": {}})
    cookie = signed_in(page)
    token = csrf(call(page, "GET", "/actions", cookie=cookie)[2])
    assert "Show the plan first" in call(page, "POST", "/actions/gear-apply", {"csrf": token}, cookie=cookie)[2]
    text = call(page, "POST", "/actions/gear", {"csrf": token, "create": "1"}, cookie=cookie)[2]
    assert "Apply this plan" in text and "Bike" in text and not applied
    text = call(page, "POST", "/actions/gear-apply", {"csrf": token}, cookie=cookie)[2]
    assert applied == [shown] and "1 activities" in text
    settings = config.Settings.from_env()
    store = Store(settings.data_dir)
    assert webui.load_gear_config(store)["create"] is True
    store.close()


def test_password_mode_for_a_page_on_the_network(env, monkeypatch):
    with pytest.raises(ValueError, match="BRIDGE_UI_PASSWORD"):
        webui.check("0.0.0.0", None, in_run=False)
    with pytest.raises(ValueError, match="8 characters"):
        webui.check("0.0.0.0", "short", in_run=False)
    with pytest.raises(ValueError, match="placeholder"):
        webui.check("0.0.0.0", webui.PLACEHOLDER, in_run=True)                # what the templates ship
    with pytest.raises(ValueError, match="next to `run`"):
        webui.check("127.0.0.1", None, in_run=True)                      # never a key in a log file
    monkeypatch.setattr(webui.time, "sleep", lambda s: None)
    ui = webui.UI(host="127.0.0.1", port=0, password="long enough", in_run=True, intervals_client=FakeIntervals,
                  garmin_source=FakeGarmin, newer_release=lambda: None)
    server = serve(ui)
    try:
        status, headers, _ = call(ui, "GET", "/")
        assert status == 303 and headers["location"] == "/login"
        assert "Wrong password" in call(ui, "POST", "/login", {"password": "nope"})[2]
        status, headers, _ = call(ui, "POST", "/login", {"password": "long enough"})
        assert status == 303
        cookie = headers["set-cookie"].split(";", 1)[0]
        assert "Waiting for the Garmin sign-in" in call(ui, "GET", "/", cookie=cookie)[2]    # runs, no session yet
        assert call(ui, "GET", "/logout", cookie=cookie)[0] == 303
        assert call(ui, "GET", "/", cookie=cookie)[0] == 303                        # signed out
    finally:
        server.shutdown()
        server.server_close()


def test_home_assistant_options_file(tmp_path, monkeypatch):
    options = tmp_path / "options.json"
    options.write_text(json.dumps({"intervals_api_key": "from-ha", "timezone": "Europe/Berlin", "imready4": True,
                                   "ui_password": ""}))
    monkeypatch.delenv("INTERVALS_API_KEY", raising=False)
    monkeypatch.delenv("BRIDGE_TIMEZONE", raising=False)
    monkeypatch.setenv("BRIDGE_IMREADY4_ENABLED", "false")                      # set already: wins
    assert config.load_options_file(options) == ["INTERVALS_API_KEY", "BRIDGE_TIMEZONE"]
    assert config._flag("BRIDGE_IMREADY4_ENABLED") is False


def test_ui_port_setting(monkeypatch):
    monkeypatch.setenv("BRIDGE_UI_PORT", "8765")
    assert config._port("BRIDGE_UI_PORT") == 8765
    monkeypatch.setenv("BRIDGE_UI_PORT", "http")
    with pytest.raises(ValueError, match="port number"):
        config._port("BRIDGE_UI_PORT")


def test_a_web_sign_in_ends_the_wait_but_a_rate_limit_never_does(monkeypatch):
    from garmin_intervals_bridge import cli
    from garmin_intervals_bridge.garmin import GarminBlocked, GarminLoginNeeded
    notices.SIGNED_IN.set()                                   # an old sign-in must not count
    timer = threading.Timer(0.2, notices.SIGNED_IN.set)
    timer.start()
    assert cli.wait_after_block(GarminLoginNeeded("login needed"), seconds=5) is True
    slept = []
    monkeypatch.setattr(cli.time, "sleep", slept.append)
    notices.SIGNED_IN.set()
    assert cli.wait_after_block(GarminBlocked("429"), seconds=900) is False and slept == [900]
    notices.SIGNED_IN.clear()


def test_alerts_are_set_on_the_settings_page_and_tested(page, env, monkeypatch):
    tested = []
    monkeypatch.setattr(webui.alerts, "test", lambda s: tested.append(s.alert_email) or {"e-mail": "sent",
                                                                                       "push": "HTTP 502"})
    cookie = signed_in(page)
    token = csrf(call(page, "GET", "/settings", cookie=cookie)[2])
    form = {"csrf": token, "timezone": "Europe/Vienna", "alert_email": "ann@gmail.com",
            "smtp_password": "abcd efgh ijkl mnop", "push": "1"}
    text = call(page, "POST", "/settings", form, cookie=cookie)[2]
    assert "Saved" in text and "abcd efgh" not in text and "app password" in text          # Gmail's hint
    values = config.parse_env_file(env)
    assert values["BRIDGE_ALERT_EMAIL"] == "ann@gmail.com" and values["BRIDGE_SMTP_PASSWORD"] == "abcd efgh ijkl mnop"
    topic = values["BRIDGE_NTFY_URL"]
    assert topic.startswith("https://ntfy.sh/garmin-bridge-") and topic in text           # to subscribe to
    text = call(page, "POST", "/settings/test", {"csrf": token}, cookie=cookie)[2]
    assert tested == ["ann@gmail.com"] and "e-mail: sent" in text and "push: HTTP 502" in text
    # saved again without a password: the password stays; push off clears the topic
    call(page, "POST", "/settings", {**form, "smtp_password": "", "push": ""}, cookie=cookie)
    values = config.parse_env_file(env)
    assert values["BRIDGE_SMTP_PASSWORD"] == "abcd efgh ijkl mnop" and values["BRIDGE_NTFY_URL"] == ""


def test_one_activity_again_shows_what_it_gets_then_writes(page, env, monkeypatch):
    calls = []
    monkeypatch.setattr(webui, "resolve_one", lambda iv, ref: ("42", "i42") if "i42" in ref else
                        (_ for _ in ()).throw(ValueError("Intervals activity i9 did not come from Garmin")))

    def enrich(gid, garmin, iv, store, *, apply, intervals_id, refresh_own, recheck):
        calls.append((gid, intervals_id, apply, refresh_own, recheck))
        return {"outcome": "enriched" if apply else "planned",
                "fields": {"writes": {"AerobicEffect": 3.2}}, "streams": {"writes": [{"type": "Stamina"}]}}
    monkeypatch.setattr(webui, "enrich_activity", enrich)
    cookie = signed_in(page)
    token = csrf(call(page, "GET", "/actions", cookie=cookie)[2])
    assert "did not come from Garmin" in call(page, "POST", "/actions/enrich", {"csrf": token, "activity": "i9"},
                                              cookie=cookie)[2]
    text = call(page, "POST", "/actions/enrich", {"csrf": token, "activity": "https://intervals.icu/activities/i42"},
                cookie=cookie)[2]
    assert "AerobicEffect" in text and "Stamina" in text and "Write these values" in text
    assert calls == [("42", "i42", False, True, True)]
    text = call(page, "POST", "/actions/enrich-apply", {"csrf": token}, cookie=cookie)[2]
    assert calls[-1] == ("42", "i42", True, True, True) and "written" in text
    assert "Choose the activity first" in call(page, "POST", "/actions/enrich-apply", {"csrf": token},
                                               cookie=cookie)[2]


def test_check_now_on_the_status_page(page, env):
    from garmin_intervals_bridge import polling
    cookie = signed_in(page)
    text = call(page, "GET", "/", cookie=cookie)[2]
    assert "New activities" in text and "every 5 min" in text and "Check now" in text
    polling.POLL_NOW.clear()
    text = call(page, "POST", "/poll-now", {"csrf": csrf(text)}, cookie=cookie)[2]
    assert "looks for new activities within a minute" in text and polling.POLL_NOW.is_set()
    polling.POLL_NOW.clear()
    st = Store(config.Settings.from_env().data_dir)
    assert polling.due(config.Settings.from_env(), st, float(st.get_meta(polling.NOW_KEY)), 300) == (
        True, "asked on the web page")
    st.close()


def test_when_to_look_is_set_on_the_settings_page(page, env):
    cookie = signed_in(page)
    text = call(page, "GET", "/settings", cookie=cookie)[2]
    assert "Looking for new activities" in text and "Beta: learn when my activities usually end" in text
    form = {"csrf": csrf(text), "timezone": "Europe/Vienna"}
    for wrong, says in (({"poll_hours": "7-"}, "is no range"), ({"poll_minutes": "soon"}, "is no number"),
                        ({"poll_minutes": "2", "poll_quiet_minutes": "1"}, "only look less often"),
                        ({"poll_minutes": "0.2"}, "between 1 and 1440")):
        text = call(page, "POST", "/settings", {**form, **wrong}, cookie=cookie)[2]
        assert says in text and "nothing was changed" in text
    assert "BRIDGE_POLL" not in env.read_text()
    text = call(page, "POST", "/settings", {**form, "poll_minutes": "1", "poll_hours": "7-23",
                                            "poll_quiet_minutes": "30", "poll_adaptive": "1"}, cookie=cookie)[2]
    assert "Saved" in text
    values = config.parse_env_file(env)
    assert (values["BRIDGE_POLL_SECONDS"], values["BRIDGE_POLL_HOURS"], values["BRIDGE_POLL_QUIET_SECONDS"],
            values["BRIDGE_POLL_MODE"]) == ("60", "7-23", "1800", "adaptive")
    text = call(page, "GET", "/settings", cookie=cookie)[2]
    assert 'name="poll_minutes" value="1"' in text and 'name="poll_quiet_minutes" value="30"' in text
    assert 'name="poll_hours" value="7-23"' in text and 'name="poll_adaptive" value="1" checked' in text


def test_how_to_swap_is_set_on_the_settings_page(page, env):
    cookie = signed_in(page)
    text = call(page, "GET", "/settings", cookie=cookie)[2]
    assert 'name="swap_method"' in text and '<option value="replace" selected>' in text
    call(page, "POST", "/settings", {"csrf": csrf(text), "timezone": "Europe/Vienna", "swap": "1",
                                     "swap_method": "upload"}, cookie=cookie)
    values = config.parse_env_file(env)
    assert values["BRIDGE_SWAP"] == "true" and values["BRIDGE_SWAP_METHOD"] == "upload"
