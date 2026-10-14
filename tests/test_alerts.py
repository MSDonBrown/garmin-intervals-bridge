"""E-mail and push alerts: the channels, the two occasions and their once-only rules."""
from types import SimpleNamespace

import pytest
import requests

from garmin_intervals_bridge import alerts, cli, notices
from garmin_intervals_bridge.store import Store


def settings(**kw):
    base = dict(alert_email=None, smtp_host=None, smtp_port=None, smtp_user=None, smtp_password=None,
                smtp_from=None, ntfy_url=None, ntfy_token=None, intervals_notes=False)
    return SimpleNamespace(**{**base, **kw})


class SMTP:
    sent: list = []

    def __init__(self, host, port, timeout, context=None):
        self.log = [("connect", host, port, context is not None)]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        SMTP.sent.append(self.log)

    def starttls(self, context):
        self.log.append(("starttls",))

    def login(self, user, password):
        if password != "app-password":
            import smtplib
            raise smtplib.SMTPAuthenticationError(535, b"bad")
        self.log.append(("login", user))

    def send_message(self, message):
        self.log.append(("message", message["From"], message["To"], message["Subject"], message.get_content()))


class Ntfy:
    def __init__(self, status=200):
        self.posts, self.status = [], status

    def post(self, url, data, headers, timeout):
        self.posts.append((url, data.decode(), headers))
        response = requests.Response()
        response.status_code = self.status
        response.url = url
        return response


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch):
    SMTP.sent = []
    monkeypatch.setattr(alerts.smtplib, "SMTP", SMTP)
    monkeypatch.setattr(alerts.smtplib, "SMTP_SSL", SMTP)


def test_e_mail_through_the_providers_server_with_starttls():
    s = settings(alert_email="ann@gmx.at", smtp_password="app-password")
    assert alerts.channels(s) == ["e-mail"] and alerts.smtp_server(s) == ("mail.gmx.net", 587)
    alerts.send_email(s, "Subject", "Text")
    (log,) = SMTP.sent
    assert log[0] == ("connect", "mail.gmx.net", 587, False) and log[1] == ("starttls",)
    assert log[2] == ("login", "ann@gmx.at")
    kind, sender, to, subject, body = log[3]
    assert sender == to == "ann@gmx.at" and subject == "Subject" and body.startswith("Text")


def test_e_mail_over_ssl_on_465_and_an_own_server():
    s = settings(alert_email="me@example.org", smtp_host="mail.example.org", smtp_port=465,
                 smtp_user="me", smtp_password="app-password")
    alerts.send_email(s, "S", "T")
    (log,) = SMTP.sent
    assert log[0] == ("connect", "mail.example.org", 465, True) and ("starttls",) not in log
    assert log[1] == ("login", "me")


def test_no_channel_without_what_it_needs():
    assert alerts.channels(settings(alert_email="ann@gmail.com")) == []                  # no password
    assert alerts.channels(settings(alert_email="me@unknown.example", smtp_password="x")) == []   # no server
    assert alerts.test(settings()) == {}


def test_push_carries_the_headline_and_an_optional_token():
    ntfy = Ntfy()
    s = settings(ntfy_url="https://ntfy.sh/garmin-bridge-abc", ntfy_token="tk")
    alerts.send_push(s, "Garmin bridge: test message", "Text", session=ntfy)
    url, text, headers = ntfy.posts[0]
    assert url == "https://ntfy.sh/garmin-bridge-abc" and text == "Text"
    assert headers["Title"] == "Garmin bridge: test message" and headers["Authorization"] == "Bearer tk"
    with pytest.raises(ValueError, match="web address"):
        alerts.send_push(settings(ntfy_url="ntfy.sh/topic"), "S", "T", session=ntfy)


def test_send_reports_per_channel_and_never_raises():
    s = settings(alert_email="ann@gmail.com", smtp_password="wrong", ntfy_url="https://ntfy.sh/t")
    result = alerts.send(s, "S", "T", session=Ntfy(status=500))
    assert result == {"e-mail": "the mailbox refused the user name or password", "push": "HTTP 500"}


def test_garmin_sign_in_alert_once_per_episode(tmp_path, monkeypatch):
    st, sent = Store(tmp_path), []
    monkeypatch.setattr(alerts, "send", lambda s, subject, text, **k: sent.append(subject) or {"push": "sent"})
    s = settings(ntfy_url="https://ntfy.sh/t")
    notices.login_needed(s, st, None, None, apply=False)
    notices.login_needed(s, st, None, None, apply=False)
    assert sent == [alerts.LOGIN_SUBJECT]
    notices.garmin_ok(st)                                                   # signed in again
    notices.login_needed(s, st, None, None, apply=False)
    assert sent == [alerts.LOGIN_SUBJECT] * 2
    st.close()


def test_a_failed_alert_is_tried_again_next_time(tmp_path, monkeypatch):
    st = Store(tmp_path)
    outcomes = iter([{"push": "HTTP 502"}, {"push": "sent"}])
    monkeypatch.setattr(alerts, "send", lambda *a, **k: next(outcomes))
    s = settings(ntfy_url="https://ntfy.sh/t")
    assert alerts.login_needed(s, st) is False and alerts.login_needed(s, st) is True
    st.close()


def test_health_alerts_once_a_day_and_push_without_details(tmp_path, monkeypatch):
    st, ntfy = Store(tmp_path), Ntfy()
    monkeypatch.setattr(alerts.requests, "post", ntfy.post)
    s = settings(alert_email="ann@gmail.com", smtp_password="app-password", ntfy_url="https://ntfy.sh/t")
    findings = [{"alert": True, "what": "garmin unreachable", "detail": "no success for 30 h: secret detail"},
                {"alert": False, "what": "last 24 h", "detail": "3 runs"}]
    assert alerts.health(s, st, findings, "2026-10-13") is True
    assert alerts.health(s, st, findings, "2026-10-13") is False                       # once a day
    assert alerts.health(s, st, [findings[1]], "2026-10-14") is False                  # nothing to act on
    assert alerts.health(s, st, findings, "2026-10-14") is True                        # a reminder the next day
    mail = SMTP.sent[-1][-1]
    assert "secret detail" in mail[4] and "last 24 h" not in mail[4]
    push = ntfy.posts[-1][1]
    assert "garmin unreachable" in push and "secret detail" not in push              # headlines only
    st.close()


def test_notify_test_command(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BRIDGE_DATA_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    assert cli.main(["notify-test"]) == 1 and "No alerts set up" in capsys.readouterr().out
    monkeypatch.setenv("BRIDGE_ALERT_EMAIL", "ann@gmail.com")
    monkeypatch.setenv("BRIDGE_SMTP_PASSWORD", "app-password")
    assert cli.main(["notify-test"]) == 0 and "e-mail: sent" in capsys.readouterr().out
    assert SMTP.sent[-1][-1][3] == alerts.TEST_SUBJECT


def test_the_daily_probe_sends_the_sign_in_alert(tmp_path, monkeypatch):
    from zoneinfo import ZoneInfo

    from garmin_intervals_bridge.garmin import GarminLoginNeeded
    from garmin_intervals_bridge.health import probe
    sent = []
    monkeypatch.setattr(alerts, "send", lambda s, subject, text, **k: sent.append(subject) or {"push": "sent"})

    class Garmin:
        def login(self, interactive=False):
            raise GarminLoginNeeded("login needed")

    class Intervals:
        def activities(self, *a, **k):
            return []
    s = settings(ntfy_url="https://ntfy.sh/t", stale_hours=24, timezone=ZoneInfo("Europe/Vienna"), data_dir=tmp_path,
                 wellness_profile="recommended")
    st = Store(tmp_path)
    probe(s, Garmin(), Intervals(), st)
    assert sent[0] == alerts.LOGIN_SUBJECT
    st.close()


def test_a_push_title_is_one_line_and_padding_is_dropped():
    # thanks @FrancescoCastaldi: a line break in the title made the push fail
    ntfy = Ntfy()
    s = settings(ntfy_url="  https://ntfy.sh/topic \n", ntfy_token="  tk_example  ")
    alerts.send_push(s, "Garmin needs\na new sign-in\r\n", "Body", session=ntfy)
    url, text, headers = ntfy.posts[0]
    assert url == "https://ntfy.sh/topic" and headers["Title"] == "Garmin needs a new sign-in"
    assert headers["Authorization"] == "Bearer tk_example"


def test_a_gmail_app_password_works_with_or_without_its_spaces(monkeypatch):
    # Google shows it as "abcd efgh ijkl mnop"; pasted like that it is refused
    logins = []

    class Server:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, **k):
            pass

        def login(self, user, password):
            logins.append(password)

        def send_message(self, message):
            pass
    s = settings(alert_email="ann@gmail.com", smtp_password="abcd efgh ijkl mnop")
    alerts.send_email(s, "S", "T", smtp=Server)
    assert logins == ["abcdefghijklmnop"]
