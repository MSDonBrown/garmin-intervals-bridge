"""Windows: not from a window opened "as administrator" – and what schtasks says when it fails."""
import logging
import os
import subprocess
import sys

import pytest

from garmin_intervals_bridge import autostart, cli


def test_the_elevation_check_answers_everywhere_and_is_false_off_windows():
    assert isinstance(autostart._token_elevated(), bool)           # on Windows: the real token, no error
    if os.name != "nt":
        assert autostart._token_elevated() is False


@pytest.fixture
def admin_window(monkeypatch, tmp_path):
    monkeypatch.setattr(autostart, "elevated_window", lambda: True)
    monkeypatch.delenv("BRIDGE_ALLOW_ADMIN", raising=False)
    env = tmp_path / ".env"
    env.write_text("INTERVALS_API_KEY=k\n")
    monkeypatch.setenv("BRIDGE_ENV_FILE", str(env))
    return env


def test_an_administrator_window_is_refused_with_the_reason(admin_window, caplog, monkeypatch):
    monkeypatch.setattr(cli.wizard, "run", lambda: pytest.fail("setup must not start"))
    with caplog.at_level(logging.ERROR):
        assert cli.main(["setup"]) == 1
        assert cli.main(["sync"]) == 1
    assert caplog.text.count("This window runs as administrator") == 2 and "BRIDGE_ALLOW_ADMIN=true" in caplog.text


def test_clearing_up_after_one_is_allowed_from_it(admin_window, caplog, monkeypatch):
    monkeypatch.setattr(autostart, "remove", lambda env, **k: {})
    monkeypatch.setattr(autostart, "status", lambda env, **k: {"kind": "windows", "installed": False, "detail": ""})
    assert cli.main(["autostart", "--remove"]) == 0
    cli.main(["autostart", "--status"])
    assert "This window runs as administrator" not in caplog.text


def test_allowed_on_purpose_it_warns_and_goes_on(admin_window, caplog, monkeypatch):
    admin_window.write_text("INTERVALS_API_KEY=k\nBRIDGE_ALLOW_ADMIN=true\n")
    monkeypatch.setattr(cli.wizard, "run", lambda: 0)
    with caplog.at_level(logging.WARNING):
        assert cli.main(["setup"]) == 0
    assert "BRIDGE_ALLOW_ADMIN" in caplog.text and "This window runs as administrator" not in caplog.text


def test_a_failed_tool_says_what_it_said():
    with pytest.raises(subprocess.CalledProcessError) as failed:     # still one for callers that catch that
        autostart._run([sys.executable, "-c", "import sys; sys.stderr.write('ERROR: Access is denied.'); sys.exit(1)"])
    assert "Access is denied" in str(failed.value) and "exit 1" in str(failed.value)
    error = autostart.ToolError(subprocess.CalledProcessError(
        1, ["schtasks", "/Create", "/TN", autostart.NAME, "/XML", "task.xml", "/F"], "", "ERROR: Access is denied."))
    assert "schtasks /Create failed (exit 1): ERROR: Access is denied." in str(error)
    assert f'schtasks /Delete /TN "{autostart.NAME}" /F' in str(error)          # how to clear it up


def test_a_restart_from_the_web_page_that_fails_is_logged(caplog, tmp_path):
    from garmin_intervals_bridge import webui

    def refuse(env):
        raise autostart.ToolError(subprocess.CalledProcessError(1, ["schtasks", "/Create"], "", "ERROR: denied"))
    ui = webui.UI(host="127.0.0.1", port=0, restart=refuse)
    with caplog.at_level(logging.ERROR):
        ui._restart(tmp_path / ".env")
    assert "Restart of the background bridge failed" in caplog.text and "ERROR: denied" in caplog.text
