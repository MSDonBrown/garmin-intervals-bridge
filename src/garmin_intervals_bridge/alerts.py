"""Telling the owner when the bridge needs them: by e-mail, as a push message, or both.

  E-mail   through the owner's own mailbox (SMTP): BRIDGE_ALERT_EMAIL, and
           BRIDGE_SMTP_PASSWORD – for most providers an app password. The server is
           known for the common providers (Gmail, GMX, web.de, iCloud ...); any other
           one is BRIDGE_SMTP_HOST / _PORT / _USER. Nothing to send from otherwise:
           mail straight from a home connection is refused or lands in spam.
  Push     ntfy (free app, no account): BRIDGE_NTFY_URL, e.g. https://ntfy.sh/<topic>;
           the topic name is the secret. Push messages carry the headline only, no
           details (they pass through the ntfy server).

When: Garmin wants a new sign-in (once per episode), and the daily health check found
something that needs a person (at most once a day). Never for routine events.
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Any

import requests

log = logging.getLogger(__name__)

# domain -> (server, port); the user name is the address itself
PROVIDERS = {
    "gmail.com": ("smtp.gmail.com", 587), "googlemail.com": ("smtp.gmail.com", 587),
    "gmx.at": ("mail.gmx.net", 587), "gmx.de": ("mail.gmx.net", 587), "gmx.net": ("mail.gmx.net", 587),
    "gmx.ch": ("mail.gmx.net", 587), "gmx.com": ("mail.gmx.com", 587),
    "web.de": ("smtp.web.de", 587),
    "icloud.com": ("smtp.mail.me.com", 587), "me.com": ("smtp.mail.me.com", 587), "mac.com": ("smtp.mail.me.com", 587),
    "yahoo.com": ("smtp.mail.yahoo.com", 465), "yahoo.de": ("smtp.mail.yahoo.com", 465),
    "posteo.de": ("posteo.de", 587), "posteo.net": ("posteo.de", 587),
    "mailbox.org": ("smtp.mailbox.org", 587),
    "t-online.de": ("securesmtp.t-online.de", 587),
    "outlook.com": ("smtp-mail.outlook.com", 587), "hotmail.com": ("smtp-mail.outlook.com", 587),
    "live.com": ("smtp-mail.outlook.com", 587),
}
# What the owner has to do at the provider before the bridge may send.
HINTS = {
    "smtp.gmail.com": "Gmail needs an app password (Google account -> Security -> 2-Step Verification -> "
                      "App passwords); 2-step verification has to be on.",
    "mail.gmx.net": "GMX: allow 'POP3/IMAP access' in the mailbox settings, then use your normal password.",
    "mail.gmx.com": "GMX: allow 'POP3/IMAP access' in the mailbox settings, then use your normal password.",
    "smtp.web.de": "web.de: allow 'POP3/IMAP access' in the mailbox settings, then use your normal password.",
    "smtp.mail.me.com": "iCloud needs an app-specific password (account.apple.com -> Sign-In and Security).",
    "smtp.mail.yahoo.com": "Yahoo needs an app password (Account security -> Generate app password).",
    "smtp-mail.outlook.com": "Outlook/Hotmail: Microsoft no longer accepts passwords from programs for most "
                             "private accounts; if it fails, use push messages instead.",
}

LOGIN_SUBJECT = "Garmin bridge: Garmin sign-in needed"
LOGIN_TEXT = ("The garmin-intervals-bridge can no longer sign in to Garmin Connect: the saved session expired "
              "or was revoked. Nothing is synced until you sign in again - on its web page "
              "('garmin-intervals-bridge ui', then Garmin) or with 'garmin-intervals-bridge login'.")
HEALTH_SUBJECT = "Garmin bridge: needs attention"
TEST_SUBJECT = "Garmin bridge: test message"
TEST_TEXT = "A test message from the garmin-intervals-bridge: its alerts reach you here."


def provider(address: str) -> tuple[str, int] | None:
    domain = address.rsplit("@", 1)[-1].strip().lower() if "@" in address else ""
    return PROVIDERS.get(domain)


def smtp_server(settings: Any) -> tuple[str, int] | None:
    """BRIDGE_SMTP_HOST/_PORT, else the provider of the alert address."""
    host = getattr(settings, "smtp_host", None)
    if host:
        return host, int(getattr(settings, "smtp_port", None) or 587)
    return provider(getattr(settings, "alert_email", None) or "")


def channels(settings: Any) -> list[str]:
    out = []
    if getattr(settings, "alert_email", None) and getattr(settings, "smtp_password", None) and smtp_server(settings):
        out.append("e-mail")
    if getattr(settings, "ntfy_url", None):
        out.append("push")
    return out


def send_email(settings: Any, subject: str, text: str, *, smtp: Any = None) -> None:
    host, port = smtp_server(settings) or (None, 0)
    if not host:
        raise ValueError("No mail server for this address: set BRIDGE_SMTP_HOST")
    to = settings.alert_email
    user = getattr(settings, "smtp_user", None) or to
    message = EmailMessage()
    message["From"] = getattr(settings, "smtp_from", None) or user
    message["To"] = to
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=(user.rsplit("@", 1)[-1] if "@" in user else None))
    message.set_content(text + "\n\n-- \nSent by your garmin-intervals-bridge "
                        "(https://github.com/futureweb/garmin-intervals-bridge).\n")
    context = ssl.create_default_context()
    if smtp is None:
        smtp = smtplib.SMTP_SSL if port == 465 else smtplib.SMTP
    kwargs = {"context": context} if port == 465 else {}
    with smtp(host, port, timeout=20, **kwargs) as server:
        if port != 465:
            server.starttls(context=context)
        server.login(user, settings.smtp_password)
        server.send_message(message)


def send_push(settings: Any, subject: str, text: str, *, session: Any = None) -> None:
    url = str(settings.ntfy_url or "").strip()
    if not url.startswith(("https://", "http://")):
        raise ValueError("BRIDGE_NTFY_URL must be a web address, e.g. https://ntfy.sh/<topic>")
    # an HTTP header is one line: a line break in the subject would make the push fail
    title = " ".join(subject.encode("ascii", "replace").decode().split())
    headers = {"Title": title, "Tags": "warning"}
    token = str(getattr(settings, "ntfy_token", None) or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = (session or requests).post(url, data=text.encode("utf-8"), headers=headers, timeout=15)
    response.raise_for_status()


def send(settings: Any, subject: str, text: str, *, push_text: str | None = None, smtp: Any = None,
         session: Any = None) -> dict[str, str]:
    """To every channel set up; what happened per channel ('sent' or the error's name). Never raises."""
    result: dict[str, str] = {}
    for channel in channels(settings):
        try:
            if channel == "e-mail":
                send_email(settings, subject, text, smtp=smtp)
            else:
                send_push(settings, subject, push_text if push_text is not None else text, session=session)
            result[channel] = "sent"
        except Exception as exc:
            result[channel] = _why(exc)
            log.warning("Alert by %s not sent: %s", channel, result[channel])
    return result


def _why(exc: Exception) -> str:
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "the mailbox refused the user name or password"
    if isinstance(exc, requests.HTTPError):
        return f"HTTP {getattr(exc.response, 'status_code', '?')}"
    return type(exc).__name__


# ---- the occasions ----

LOGIN_KEY = "alert:garmin_login"
HEALTH_KEY = "alert:health"


def login_needed(settings: Any, store: Any) -> bool:
    """Once per episode; `notices.garmin_ok` ends the episode."""
    if not channels(settings) or store.get_meta(LOGIN_KEY):
        return False
    result = send(settings, LOGIN_SUBJECT, LOGIN_TEXT)
    if "sent" in result.values():
        store.set_meta(LOGIN_KEY, "sent")
        return True
    return False


def health(settings: Any, store: Any, findings: list[dict], today: str) -> bool:
    """The day's findings that need a person, at most once a day."""
    alerts = [f for f in findings if f.get("alert")]
    if not alerts or not channels(settings) or store.get_meta(HEALTH_KEY) == today:
        return False
    lines = "\n".join(f"- {f['what']}: {f['detail']}" for f in alerts)
    headlines = "\n".join(f"- {f['what']}" for f in alerts)
    result = send(settings, HEALTH_SUBJECT,
                  f"The garmin-intervals-bridge needs attention:\n\n{lines}\n\nIts web page or log tells more.",
                  push_text=f"{headlines}\nIts web page or log tells more.")
    if "sent" in result.values():
        store.set_meta(HEALTH_KEY, today)
        return True
    return False


def test(settings: Any, **kwargs: Any) -> dict[str, str]:
    if not channels(settings):
        return {}
    return send(settings, TEST_SUBJECT, TEST_TEXT, **kwargs)
