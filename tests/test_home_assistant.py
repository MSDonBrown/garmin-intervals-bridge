"""The Home Assistant app: its files, the form's settings, the web page through ingress."""
import json
import os
import re
import sys
import threading
from pathlib import Path

import pytest
from test_webui import call, csrf, env  # noqa: F401  (the env fixture)

from garmin_intervals_bridge import __version__, config, webui

APP = Path(__file__).resolve().parents[1] / "home-assistant"


def test_the_app_builds_from_the_image_of_this_very_version():
    cfg = (APP / "config.yaml").read_text()
    assert re.search(r'^version: "([^"]+)"$', cfg, re.M).group(1) == __version__
    assert f"FROM futurewebat/garmin-intervals-bridge:{__version__}\n" in (APP / "Dockerfile").read_text()
    assert os.access(APP / "run.sh", os.X_OK)
    assert (APP.parent / "repository.yaml").is_file() and (APP / "icon.png").read_bytes()[:4] == b"\x89PNG"


def test_every_form_field_reaches_a_setting():
    schema = (APP / "config.yaml").read_text().split("\nschema:\n", 1)[1]
    keys = {m.group(1) for m in re.finditer(r"^  (\w+):", schema, re.M)}
    assert keys - {"write_to_intervals"} <= set(config.OPTIONS)          # write_to_intervals: run.sh
    labels = (APP / "translations" / "en.yaml").read_text()
    assert all(f"\n  {k}:\n" in labels for k in keys)                   # every field has a label


def test_the_form_becomes_the_settings(tmp_path, monkeypatch):
    options = tmp_path / "options.json"
    options.write_text(json.dumps({"intervals_api_key": "", "mode": "backup", "swap": True, "poll_hours": "6-22",
                                   "backup_hours": 24, "write_to_intervals": True}))
    for name in ("BRIDGE_MODE", "BRIDGE_SWAP", "BRIDGE_POLL_HOURS", "BRIDGE_BACKUP_HOURS", "INTERVALS_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BRIDGE_OPTIONS_FILE", str(options))
    monkeypatch.setenv("BRIDGE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.chdir(tmp_path)
    s = config.Settings.from_env()
    assert (s.mode, s.swap, s.poll_hours, s.backup_hours) == ("backup", True, ((360, 1320),), 24)
    assert s.intervals_api_key == ""                                    # empty: not set (backup needs none)


class Offline:
    """An Intervals that cannot be reached – at once, without the network."""

    def __init__(self, *a, **k):
        pass

    def __getattr__(self, name):
        def fail(*a, **k):
            raise ConnectionError("offline (test)")
        return fail


def test_the_page_answers_only_home_assistants_ingress(env, monkeypatch):  # noqa: F811
    ui = webui.UI(host="0.0.0.0", port=0, in_run=True, ingress=True, newer_release=lambda: None,
                  intervals_client=Offline)
    server = webui.make_server(ui)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert call(ui, "GET", "/")[0] == 403                          # we are not 172.30.32.2
        monkeypatch.setattr(webui, "INGRESS_IP", "127.0.0.1")
        prefix = "/api/hassio_ingress/abc123"
        import http.client

        def get(path, cookie=None, method="GET", body=None, origin=None):
            conn = http.client.HTTPConnection("127.0.0.1", ui.port, timeout=10)
            headers = {"X-Ingress-Path": prefix}
            if cookie:
                headers["Cookie"] = cookie
            if origin:
                headers["Origin"] = origin
            data = None
            if body is not None:
                from urllib.parse import urlencode
                data = urlencode(body).encode()
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            conn.request(method, path, body=data, headers=headers)
            r = conn.getresponse()
            out = (r.status, {k.lower(): v for k, v in r.getheaders()}, r.read().decode())
            conn.close()
            return out
        status, headers, _ = get("/")                                  # Home Assistant signed the user in
        assert status == 303 and headers["location"] == prefix + "/"
        assert f"Path={prefix};" in headers["set-cookie"]
        cookie = headers["set-cookie"].split(";", 1)[0]
        status, _, text = get("/", cookie)
        assert status == 200 and f'href="{prefix}/garmin"' in text and 'href="/garmin"' not in text
        text = get("/settings", cookie)[2]
        assert f'action="{prefix}/settings"' in text and 'action="/settings"' not in text      # forms too
        token = csrf(get("/actions", cookie)[2])
        # the browser talks to Home Assistant: its Origin is not ours, the token decides
        status, _, text = get("/actions/advice", cookie, "POST", {"csrf": token}, origin="https://ha.example")
        assert status == 200
        assert get("/actions/advice", cookie, "POST", {"csrf": "forged"})[0] == 403
        assert get("/", cookie)[0] == 200 and "Sign out" not in get("/", cookie)[2]
    finally:
        server.shutdown()
        server.server_close()


def test_a_forged_ingress_path_is_not_echoed(env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(webui, "INGRESS_IP", "127.0.0.1")
    ui = webui.UI(host="0.0.0.0", port=0, in_run=True, ingress=True, newer_release=lambda: None)
    server = webui.make_server(ui)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", ui.port, timeout=10)
        conn.request("GET", "/", headers={"X-Ingress-Path": '/x"><script>'})
        r = conn.getresponse()
        assert r.status == 303 and r.getheader("Location") == "/"       # not a valid ingress path: ignored
        conn.close()
    finally:
        server.shutdown()
        server.server_close()


def _run_app(tmp_path, data, share):
    """home-assistant/run.sh with a stand-in bridge that records how it was started."""
    if sys.platform == "win32":
        pytest.skip("run.sh is the Linux container's start script")
    import subprocess
    stub = tmp_path / "bin"
    stub.mkdir(exist_ok=True)
    (stub / "garmin-intervals-bridge").write_text('#!/bin/sh\necho "started: $*"\n')
    (stub / "garmin-intervals-bridge").chmod(0o755)
    environment = {"PATH": f"{stub}:{os.environ['PATH']}", "HA_DATA": str(data), "HA_SHARE": str(share)}
    return subprocess.run(["sh", str(APP / "run.sh")], env=environment, capture_output=True, text=True,
                          check=True).stdout


def test_the_app_takes_a_data_folder_over_even_after_an_empty_first_start(tmp_path):
    data, share = tmp_path / "data", tmp_path / "share"
    (data / "tokens").mkdir(parents=True)
    (data / "bridge.db").write_text("empty, from a start before the folder was there")
    (data / "options.json").write_text('{"write_to_intervals": false}')
    (share / "garmin-bridge" / "tokens").mkdir(parents=True)
    (share / "garmin-bridge" / "bridge.db").write_text("the NAS's")
    (share / "garmin-bridge" / "tokens" / "garmin_tokens.json").write_text("{}")
    (share / "garmin-bridge" / "options.json").write_text('{"write_to_intervals": true}')    # another app's
    out = _run_app(tmp_path, data, share)
    assert "Taking over" in out and "started: run\n" in out                       # dry run: no --apply
    assert (data / "bridge.db").read_text() == "the NAS's" and (data / "tokens" / "garmin_tokens.json").exists()
    assert (data / "options.json").read_text() == '{"write_to_intervals": false}'   # the form stays
    assert (data / "bridge.db.before-takeover").read_text().startswith("empty")      # kept aside, not deleted
    # never again, and never over a session of its own
    (data / "bridge.db").write_text("its own by now")
    (data / "options.json").write_text('{"write_to_intervals": true}')
    out = _run_app(tmp_path, data, share)
    assert "Taking over" not in out and "started: run --apply" in out and (data / "bridge.db").read_text() == \
        "its own by now"


def test_the_app_never_takes_over_when_it_has_a_session(tmp_path):
    data, share = tmp_path / "data", tmp_path / "share"
    (data / "tokens").mkdir(parents=True)
    (data / "tokens" / "garmin_tokens.json").write_text("{}")
    (data / "options.json").write_text("{}")
    (share / "garmin-bridge").mkdir(parents=True)
    (share / "garmin-bridge" / "bridge.db").write_text("the NAS's")
    assert "Taking over" not in _run_app(tmp_path, data, share)


def test_home_assistant_may_frame_the_page_and_nobody_else(env, monkeypatch):  # noqa: F811
    import http.client
    monkeypatch.setattr(webui, "INGRESS_IP", "127.0.0.1")
    ui = webui.UI(host="0.0.0.0", port=0, in_run=True, ingress=True, newer_release=lambda: None)
    server = webui.make_server(ui)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", ui.port, timeout=10)
        conn.request("GET", "/", headers={"X-Ingress-Path": "/api/hassio_ingress/abc"})
        r = conn.getresponse()
        assert r.getheader("X-Frame-Options") == "SAMEORIGIN"
        assert "frame-ancestors 'self'" in r.getheader("Content-Security-Policy")
        conn.close()
    finally:
        server.shutdown()
        server.server_close()
    assert webui.HEADERS["X-Frame-Options"] == "DENY"                   # everywhere else: no frames at all
