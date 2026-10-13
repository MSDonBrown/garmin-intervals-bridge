"""What a bridge running unseen in the background has to tell its owner.

  - A newer release on PyPI: one log line and a health finding a day, with the update
    command (BRIDGE_UPDATE_CHECK=false turns the check off; it asks pypi.org, nothing else
    is sent).
  - Garmin's login expired: everything stops until someone signs in again, and nobody
    reads a log file. One NOTE in the Intervals calendar on that day, once per episode,
    only by a bridge that may write (--apply); never deleted by the bridge.
    BRIDGE_INTERVALS_NOTES=false turns it off. And an e-mail or push message, when
    set up (alerts.py).
"""
from __future__ import annotations

import logging
import re
import threading
from datetime import date
from typing import Any

import requests

from . import __version__, alerts

log = logging.getLogger(__name__)

PYPI_URL = "https://pypi.org/pypi/garmin-intervals-bridge/json"
LOGIN_NOTE_KEY = "notice:garmin_login"
WAITING_KEY = "notice:garmin_sign_in_due"    # Garmin wants a new sign-in: set until it answers again
# Set by the web page after a Garmin sign-in: a `run` waiting for one goes on at once.
SIGNED_IN = threading.Event()
LOGIN_NOTE_NAME = "Garmin bridge: Garmin login needed"
LOGIN_NOTE_TEXT = ("The garmin-intervals-bridge can no longer sign in to Garmin Connect: the saved session "
                   "expired or was revoked. Nothing is synced until you sign in again: on the bridge's web page "
                   "('garmin-intervals-bridge ui', then Garmin) or with 'garmin-intervals-bridge login' on the "
                   "computer the bridge runs on. The bridge does not delete this note; remove it whenever you "
                   "like.")


def _version(text: str) -> tuple[int, ...] | None:
    """'0.8.1' -> (0, 8, 1); pre-releases and anything else -> None (never offered)."""
    return tuple(int(p) for p in text.split(".")) if re.fullmatch(r"\d+(\.\d+){1,3}", text or "") else None


def newer_release(current: str = __version__, *, session: Any = None, timeout: float = 10) -> str | None:
    """The newest release on PyPI if it is newer than this one; None otherwise or when unreachable."""
    try:
        response = (session or requests).get(PYPI_URL, timeout=timeout)
        response.raise_for_status()
        latest = str(response.json()["info"]["version"])
    except Exception as exc:
        log.debug("Update check failed: %s", type(exc).__name__)
        return None
    mine, theirs = _version(current), _version(latest)
    return latest if mine and theirs and theirs > mine else None


def update_finding(settings: Any, *, in_container: bool, session: Any = None) -> dict | None:
    if not getattr(settings, "update_check", False):
        return None
    latest = newer_release(session=session)
    if latest is None:
        return None
    how = ("pull the image futurewebat/garmin-intervals-bridge:latest and recreate the container" if in_container
           else "python -m pip install --upgrade garmin-intervals-bridge, then restart it "
                "(log out and in, or: garmin-intervals-bridge autostart)")
    return {"alert": False, "what": f"update available: {latest}",
            "detail": f"this is {__version__}; to update: {how}"}


def login_needed(settings: Any, store: Any, intervals: Any, today: date, *, apply: bool) -> bool:
    """Tell the owner once per episode: an e-mail / push message (when set up), and the note in
    the calendar (a bridge that may write); True when the note was written."""
    if not store.get_meta(WAITING_KEY):
        store.set_meta(WAITING_KEY, str(today or "yes"))
    alerts.login_needed(settings, store)
    if not apply or not getattr(settings, "intervals_notes", False) or intervals is None:
        return False
    if store.get_meta(LOGIN_NOTE_KEY):
        return False                                      # this episode is in the calendar already
    try:
        intervals.create_note(today, LOGIN_NOTE_NAME, LOGIN_NOTE_TEXT)
    except Exception as exc:
        log.warning("Could not put the 'Garmin login needed' note in the Intervals calendar: %s",
                    type(exc).__name__)
        return False
    store.set_meta(LOGIN_NOTE_KEY, today.isoformat())
    log.info("Put a 'Garmin login needed' note in the Intervals calendar on %s", today)
    return True


def garmin_ok(store: Any) -> None:
    """Garmin answered: the episode is over, the next expiry gets its own note."""
    store.set_meta("garmin_last_ok")
    for key in (LOGIN_NOTE_KEY, alerts.LOGIN_KEY, WAITING_KEY):
        if store.get_meta(key):
            store.set_meta(key, "")
