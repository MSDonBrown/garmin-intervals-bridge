"""A small web page for the bridge: what it does in plain words, the Garmin sign-in (MFA
included), the settings, the one-off actions, the log. Standard library only.

Three ways to open it:
  `garmin-intervals-bridge ui`   on your own computer: 127.0.0.1 only, the link it opens
                                 carries a random key (other users of the computer and web
                                 pages in your browser cannot use the page without it)
  `run` with BRIDGE_UI_PORT      next to the running bridge, e.g. in a container on a NAS:
                                 needs BRIDGE_UI_PASSWORD (8 characters or more)
  Home Assistant app             through Home Assistant's ingress (BRIDGE_UI_INGRESS=1): only
                                 its proxy (172.30.32.2) is let in, its users are signed in
                                 already, and every link lives under its X-Ingress-Path

Safety: every form carries a per-session token and must come from this page (CSRF); a
page on 127.0.0.1 answers only to its own host name (DNS rebinding); no caching, no
framing, no scripts. The Garmin password is handed to Garmin and dropped; the API key
can be replaced but is never shown. Nothing is written to Intervals without a button
that says so.
"""
from __future__ import annotations

import hmac
import html
import logging
import os
import re
import secrets
import threading
import time
import webbrowser
from collections import deque
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

from . import __version__, alerts, autostart, hrvtrend, imready4, notices, polling
from .charts import setup_charts
from .config import Settings, poll_hours
from .enrich import enrich_activity, resolve_one
from .garmin import GarminBlocked, GarminSource
from .gear import apply_plan as apply_gear_plan
from .gear import build_plan as build_gear_plan
from .gear import load_config as load_gear_config
from .gear import report as gear_report
from .gear import save_config as save_gear_config
from .health import silent_error_checks
from .intervals import IntervalsClient
from .store import InstanceBusy, Store, single_instance
from .sync import restore_intake

log = logging.getLogger(__name__)

DEFAULT_PORT = 8765
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
COOKIE = "gib_session"
SESSION_SECONDS = 12 * 3600
PENDING_SECONDS = 10 * 60
MAX_BODY = 64 * 1024
MAX_CONNECTIONS = 16                       # requests served at once; more wait in the kernel's queue
REQUEST_SECONDS = 20                       # a request (headers and body) that takes longer is dropped
PLACEHOLDER = "change-me-please"           # what the templates ship with; never accepted
INGRESS_IP = "172.30.32.2"                 # Home Assistant's ingress proxy: the only client then
INGRESS_PATH = re.compile(r"/api/hassio_ingress/[A-Za-z0-9_-]{1,200}")
LOG_LINES = 300
HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; "
                               "form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    # same-origin, not no-referrer: with no-referrer a browser sends a form's Origin as "null"
    # (Firefox does, as the Fetch standard says); other sites still learn nothing
    "Referrer-Policy": "same-origin",
    "Cache-Control": "no-store",
}
# Home Assistant shows an ingress page in a frame of its own (same origin): that one frame is allowed
INGRESS_HEADERS = {**HEADERS, "X-Frame-Options": "SAMEORIGIN",
                   "Content-Security-Policy": HEADERS["Content-Security-Policy"].replace(
                       "frame-ancestors 'none'", "frame-ancestors 'self'")}
STYLE = """
body{font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#f6f7f9;color:#1d2330}
header{background:#1d2330;color:#fff;padding:10px 16px}header a{color:#cfd6e4;margin-right:14px;text-decoration:none}
header b{margin-right:22px}main{max-width:780px;margin:0 auto;padding:16px}
section{background:#fff;border:1px solid #dde1e8;border-radius:8px;padding:12px 16px;margin:0 0 14px}
h1{font-size:20px;margin:6px 0 12px}h2{font-size:16px;margin:4px 0 8px}
.ok{color:#17803d}.warn{color:#a35c00}.bad{color:#b42318}.muted{color:#667085}
table{border-collapse:collapse;width:100%}td,th{text-align:left;padding:3px 8px 3px 0;vertical-align:top}
input[type=text],input[type=password],input[type=email]{width:100%;max-width:360px;padding:6px;font:inherit}
button{font:inherit;padding:6px 14px;border-radius:6px;border:1px solid #1d2330;background:#1d2330;color:#fff}
button.light{background:#fff;color:#1d2330}form.inline{display:inline}
pre{white-space:pre-wrap;word-break:break-word;font-size:12px;background:#f0f2f5;padding:8px;border-radius:6px}
label{display:block;margin:6px 0}
"""


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _ago(ts: float | None, now: float | None = None) -> str:
    if not ts:
        return "never"
    minutes = int(((now or time.time()) - ts) // 60)
    if minutes < 1:
        return "just now"
    if minutes < 120:
        return f"{minutes} min ago"
    hours = minutes // 60
    return f"{hours} h ago" if hours < 48 else f"{hours // 24} days ago"


class Session:
    def __init__(self) -> None:
        self.id = secrets.token_urlsafe(32)
        self.csrf = secrets.token_urlsafe(32)
        self.created = time.time()
        self.garmin: Any = None                      # a sign-in waiting for its MFA code
        self.garmin_since = 0.0
        self.gear_plan: dict | None = None           # the plan shown, applied exactly as shown
        self.gear_options: dict = {}
        self.activity: tuple[str, str] | None = None  # (Garmin id, Intervals id) shown, to fill in again


class Reply(Exception):
    """Ends a request with this response (redirects, refusals)."""

    def __init__(self, status: int, body: str = "", headers: dict | None = None):
        super().__init__(status)
        self.status, self.body, self.headers = status, body, headers or {}


class UI:
    def __init__(self, *, host: str, port: int, password: str | None = None, key: str | None = None,
                 in_run: bool = False, ingress: bool = False, settings: Callable[[], Settings] = Settings.from_env,
                 intervals_client: Callable[..., Any] = IntervalsClient,
                 garmin_source: Callable[..., Any] = GarminSource,
                 newer_release: Callable[[], str | None] = notices.newer_release,
                 restart: Callable[[Path], Any] | None = None):
        self.host, self.port, self.password, self.key, self.in_run = host, port, password, key, in_run
        self.ingress = ingress
        self.settings, self.intervals_client, self.garmin_source = settings, intervals_client, garmin_source
        self.newer_release = newer_release
        self.restart = restart or (lambda env: autostart.install(env))
        self.sessions: dict[str, Session] = {}
        self.lock = threading.Lock()
        self.failures = 0
        self.login_gate = threading.Lock()          # one password check at a time: guessing stays slow
        self._update: tuple[float, str | None] = (0.0, None)

    @property
    def local(self) -> bool:
        return self.host in LOCAL_HOSTS

    # ---- sessions ----

    def new_session(self) -> Session:
        with self.lock:
            now = time.time()
            for sid in [s for s, v in self.sessions.items() if now - v.created > SESSION_SECONDS]:
                del self.sessions[sid]
            session = Session()
            self.sessions[session.id] = session
            return session

    def session(self, cookie_header: str | None) -> Session | None:
        if not cookie_header:
            return None
        try:
            cookie = SimpleCookie(cookie_header)
        except Exception:
            return None
        morsel = cookie.get(COOKIE)
        if morsel is None:
            return None
        with self.lock:
            session = self.sessions.get(morsel.value)
        if session is None or time.time() - session.created > SESSION_SECONDS:
            return None
        return session

    def update_available(self) -> str | None:
        """PyPI at most once a day, and only when the update check is on."""
        checked, latest = self._update
        if time.time() - checked > 24 * 3600:
            try:
                latest = self.newer_release() if self.settings().update_check else None
            except Exception:
                latest = None
            self._update = (time.time(), latest)
        return self._update[1]

    # ---- the pages ----

    def page(self, session: Session | None, title: str, body: str) -> str:
        nav = ""
        if session is not None:
            nav = "".join(f'<a href="{href}">{label}</a>' for href, label in (
                ("/", "Status"), ("/garmin", "Garmin"), ("/settings", "Settings"), ("/actions", "Actions"),
                ("/log", "Log")))
            if self.password:
                nav += '<a href="/logout">Sign out</a>'
        return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
                f'<meta name="viewport" content="width=device-width,initial-scale=1">'
                f'<title>{esc(title)} - Garmin Intervals Bridge</title><style>{STYLE}</style></head>'
                f'<body><header><b>Garmin &rarr; Intervals Bridge</b>{nav}</header><main><h1>{esc(title)}</h1>'
                f'{body}</main></body></html>')

    def form(self, session: Session, action: str, inner: str, button: str, light: bool = False,
             inline: bool = False) -> str:
        return (f'<form method="post" action="{esc(action)}"{" class=inline" if inline else ""}>'
                f'<input type="hidden" name="csrf" value="{esc(session.csrf)}">{inner}'
                f'<button{" class=light" if light else ""}>{esc(button)}</button></form>')

    def status(self, session: Session, query: dict, form: dict, note: str = "") -> str:
        settings = self.settings()
        parts: list[str] = [note] if note else []
        latest = self.update_available()
        if latest:
            how = ("Pull the new image and recreate the container." if autostart.platform() == "container" else
                   "Update: <code>python -m pip install --upgrade garmin-intervals-bridge</code>, then restart "
                   "the bridge.")
            parts.append(f'<section><b class="warn">Version {esc(latest)} is out</b> (this is '
                         f'{esc(__version__)}). {how}</section>')
        store = Store(settings.data_dir)
        try:
            now = time.time()
            runs = store.recent_runs(24 * 14)
            last = runs[-1]["started"] if runs else None
            alive = self.in_run or (last is not None and now - last < 35 * 60)     # the full run: every 30 min
            tokens = any(settings.token_dir.glob("*.json")) if settings.token_dir.is_dir() else False
            line = (f'<b class="ok">Running</b> &middot; last check {esc(_ago(last, now))}' if alive else
                    f'<b class="bad">Not running</b> &middot; last check {esc(_ago(last, now))}')
            if alive and not tokens:
                line = '<b class="warn">Waiting for the Garmin sign-in</b> &middot; <a href="/garmin">sign in</a>'

            rows = [("Bridge", line), ("Version", esc(__version__))]
            kind = autostart.platform()
            if not self.in_run and kind != "container" and settings.env_file is not None:
                state = autostart.status(settings.env_file)
                rows.append(("Starts at login", '<span class="ok">yes</span>' if state["installed"] else
                             '<span class="warn">no</span> &middot; set it up with <code>autostart</code>'))
            garmin_ok = store.meta_updated("garmin_last_ok")
            if store.get_meta(notices.LOGIN_NOTE_KEY):
                garmin = '<b class="bad">sign-in needed</b> &middot; <a href="/garmin">sign in again</a>'
            elif tokens:
                garmin = f'<span class="ok">signed in</span> &middot; last confirmed {esc(_ago(garmin_ok, now))}'
            else:
                garmin = '<b class="warn">not signed in</b> &middot; <a href="/garmin">sign in</a>'
            rows.append(("Garmin Connect", garmin))
            syncs = [r for r in runs if r["command"] == "sync"]
            if syncs:
                m = syncs[-1]["metrics"]
                a, w = m.get("activities") or {}, m.get("wellness") or {}
                rows.append(("Last full run", f'{esc(_ago(syncs[-1]["started"], now))}: {a.get("seen", 0)} activities '
                             f'seen, {a.get("enriched", a.get("uploaded", 0))} completed; wellness '
                             f'{w.get("days_checked", 0)} days checked, {w.get("writes", 0)} written'))
            backups = [r for r in runs if r["command"] == "backup"]
            if backups:
                total = {k: sum(r["metrics"].get(k) or 0 for r in backups if now - r["started"] < 86400)
                         for k in ("activities", "wellness_days")}
                rows.append(("Last backup", f'{esc(_ago(backups[-1]["started"], now))}; in the last 24 h '
                             f'{total["activities"]} activities and {total["wellness_days"]} days of wellness '
                             f'&middot; all in {esc(settings.data_dir)}'))
            rows.append(("Mode", esc({"enrich": "complete Garmin's copies in Intervals",
                                      "upload": "upload the originals to Intervals",
                                      "backup": "back up Garmin to this computer (no Intervals)"}[settings.mode]
                                     + (" + swap" if settings.swap and settings.mode == "enrich" else ""))))
            if settings.mode != "backup":
                rows.append(("New activities", self._polls(session, settings, store, now)))
            rows.append(("Day boundary", esc(settings.timezone.key)))
            on = [name for name, flag in (("ImReady4 advice", settings.imready4),
                                          ("HRV trend advice", settings.hrvtrend)) if flag]
            gear = load_gear_config(store)
            if gear:
                on.append("gear" + (" (creates new gear)" if gear.get("create") else ""))
            rows.append(("Extras", esc(", ".join(on) or "none")))
            parts.append("<section><table>" + "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in rows)
                         + "</table></section>")
            findings = silent_error_checks(settings, store, now=now)
            if findings:
                items = "".join(f'<li class="{"bad" if f["alert"] else "muted"}"><b>{esc(f["what"])}</b>: '
                                f'{esc(f["detail"])}</li>' for f in findings)
                parts.append(f"<section><h2>Checks</h2><ul>{items}</ul></section>")
        finally:
            store.close()
        return self.page(session, "Status", "".join(parts))

    def _polls(self, session: Session, settings: Settings, store: Store, now: float) -> str:
        """When the bridge looks for new activities, why, and the button to look now."""
        mode = polling.ACTIVE.get("mode", settings.mode) if self.in_run else settings.mode
        fast = polling.ACTIVE.get("fast") if self.in_run and polling.ACTIVE else polling.fast_seconds(settings, mode)
        plan = esc(polling.schedule_text(settings, store, now, fast))
        paused = float(store.get_meta(polling.PAUSE_KEY) or 0)
        if paused > now:
            state = (f'<b class="warn">paused until {datetime.fromtimestamp(paused, settings.timezone):%H:%M}</b> '
                     '(Intervals.icu asked for a break, HTTP 429)')
        else:
            wanted, why = polling.interval(settings, store, now, fast)
            last = polling.last_look(store, mode)
            if wanted is None:
                state = f"now: {esc(why)}"
            else:
                left = (last or 0) + wanted - now
                state = f"now every {esc(polling.span(wanted))} ({esc(why)}); next look " + (
                    f"in about {esc(polling.span(left))}" if left >= 60 else "within a minute")
        button = self.form(session, "/poll-now", "", "Check now", light=True, inline=True)
        return f'{plan}<br><span class="muted">{state}</span> {button}'

    def poll_now(self, session: Session, query: dict, form: dict) -> str:
        """Look for new activities now, and at the fast pace for the next hour."""
        settings = self.settings()
        store = Store(settings.data_dir)
        try:
            store.set_meta(polling.NOW_KEY, f"{time.time():.3f}")
            fast = polling.fast_seconds(settings, settings.mode)
            keeps = polling.interval(settings, store, time.time(), fast)[0] is not None
        finally:
            store.close()
        polling.POLL_NOW.set()                          # a `run` in this process: at once
        when = "now" if self.in_run else "within a minute"
        then = (" and keeps looking at its fastest pace for the next hour" if keeps else
                " (once: outside BRIDGE_POLL_HOURS it does not keep looking)")
        return self.status(session, query, {}, f'<section><p class="ok">The bridge looks for new activities {when}'
                                                f'{then}. What it found shows in the <a href="/log">log</a>.</p>'
                                                '</section>')

    # -- Garmin --

    def garmin_page(self, session: Session, message: str = "", mfa: bool = False) -> str:
        warning = ""
        if not self.local and not self.ingress:
            warning = ('<p class="warn">This page travels unencrypted (http) unless you reach it through '
                       'HTTPS, e.g. your NAS\'s reverse proxy. Sign in from your home network only.</p>')
        if mfa:
            body = self.form(session, "/garmin/mfa", '<label>The code Garmin just sent you '
                             '<input type="text" name="code" inputmode="numeric" autocomplete="one-time-code" '
                             'required autofocus></label>', "Confirm")
        else:
            body = self.form(session, "/garmin", (
                '<label>Garmin Connect e-mail <input type="email" name="email" autocomplete="username" required>'
                '</label><label>Password <input type="password" name="password" autocomplete="current-password" '
                'required></label>'), "Sign in")
        return self.page(session, "Garmin Connect", f'{message}<section><p>Your password goes to Garmin for this '
                         'sign-in only and is not stored; the bridge keeps the session, which renews itself for '
                         f'about a year.</p>{warning}{body}</section>')

    def garmin_post(self, session: Session, query: dict, form: dict) -> str:
        settings = self.settings()
        garmin = self.garmin_source(settings.token_dir, settings.garmin_request_delay)
        try:
            outcome = garmin.start_login(form.get("email", "").strip(), form.get("password", ""))
        except GarminBlocked:
            return self.garmin_page(session, '<p class="bad">Garmin limits sign-ins right now (429). Wait an hour, '
                                             'then try again.</p>')
        except Exception as exc:
            name = type(exc).__name__
            text = ("Garmin did not accept this e-mail and password." if "Authentication" in name
                    else f"The sign-in failed ({esc(name)}).")
            return self.garmin_page(session, f'<p class="bad">{text}</p>')
        if outcome == "mfa":
            session.garmin, session.garmin_since = garmin, time.time()
            return self.garmin_page(session, "", mfa=True)
        return self._signed_in(session, settings)

    def garmin_mfa(self, session: Session, query: dict, form: dict) -> str:
        settings = self.settings()
        garmin = session.garmin
        if garmin is None or time.time() - session.garmin_since > PENDING_SECONDS:
            session.garmin = None
            return self.garmin_page(session, '<p class="warn">That took too long; please sign in again.</p>')
        try:
            garmin.finish_login(form.get("code", ""))
        except Exception as exc:
            return self.garmin_page(session, f'<p class="bad">Garmin did not accept the code '
                                             f'({esc(type(exc).__name__)}); try again.</p>', mfa=True)
        session.garmin = None
        return self._signed_in(session, settings)

    def _signed_in(self, session: Session, settings: Settings) -> str:
        store = Store(settings.data_dir)
        try:
            notices.garmin_ok(store)
        finally:
            store.close()
        notices.SIGNED_IN.set()
        soon = "goes on with it at once" if self.in_run else "uses it within 15 minutes"
        return self.page(session, "Garmin Connect", f'<section><p class="ok"><b>Signed in.</b> The session is saved; '
                         f'the running bridge {soon}.</p><p><a href="/">Back to the status</a></p></section>')

    # -- settings --

    def settings_page(self, session: Session, message: str = "") -> str:
        settings = self.settings()
        if settings.env_file is None:
            rows = [("Time zone", settings.timezone.key), ("ImReady4 advice", settings.imready4),
                    ("HRV trend advice", settings.hrvtrend), ("Update hint", settings.update_check),
                    ("Calendar note when Garmin needs a sign-in", settings.intervals_notes),
                    ("Swap Garmin's copy for the original", settings.swap),
                    ("New activities", self._schedule(settings)),
                    ("Alerts", ", ".join(alerts.channels(settings)) or "none (BRIDGE_ALERT_EMAIL, BRIDGE_NTFY_URL)")]
            table = "".join(f"<tr><th>{esc(k)}</th><td>{esc(v)}</td></tr>" for k, v in rows)
            where = ("the app's <i>Configuration</i> tab in Home Assistant" if self.ingress else
                     "the container's settings (its environment variables)")
            return self.page(session, "Settings", f'{message}<section><p>These come from {where}; change them '
                             f'there.</p><table>{table}</table></section>{self._test_form(session, settings)}')

        def box(name: str, label: str, value: bool) -> str:
            return (f'<label><input type="checkbox" name="{name}" value="1"{" checked" if value else ""}> '
                    f'{esc(label)}</label>')
        inner = (f'<label>Time zone (Region/City) <input type="text" name="timezone" '
                 f'value="{esc(settings.timezone.key)}" required></label>'
                 + box("imready4", "ImReady4's training advice into TrainingAdvice each morning", settings.imready4)
                 + box("hrvtrend", "Experimental: the HRV trend advice in its own field", settings.hrvtrend)
                 + box("update_check", "Tell me when a new version is out (asks pypi.org once a day)",
                       settings.update_check)
                 + box("intervals_notes", "A note in my Intervals calendar when Garmin needs a new sign-in",
                       settings.intervals_notes)
                 + box("swap", "Swap Garmin's filtered copy of each new activity for the original (deletes the copy "
                               "in Intervals, never one with comments, nothing on Garmin Connect; 'Reprocess file' "
                               "then keeps everything)", settings.swap)
                 + '<label>Replace the Intervals API key (leave empty to keep it) <input type="password" '
                   'name="api_key" autocomplete="off"></label>'
                 + self._poll_fields(settings)
                 + '<h2>When the bridge needs you</h2><p class="muted">Garmin wants a new sign-in, or the bridge '
                   'has been failing for a day: besides the calendar note, an e-mail and/or a push message.</p>'
                 + f'<label>E-mail address (empty = no e-mail) <input type="email" name="alert_email" '
                   f'value="{esc(settings.alert_email)}"></label>'
                 + '<label>Password to send with (for most providers an app password; empty = keep it) '
                   '<input type="password" name="smtp_password" autocomplete="off"></label>'
                 + f'<label>Mail server, only for a provider the bridge does not know (smtp.example.com) '
                   f'<input type="text" name="smtp_host" value="{esc(settings.smtp_host)}"></label>'
                 + f'<label>Its port <input type="text" name="smtp_port" value="{esc(settings.smtp_port or "")}" '
                   f'inputmode="numeric"></label>'
                 + box("push", "Push messages on my phone (free ntfy app: subscribe to the topic below)",
                       bool(settings.ntfy_url))
                 + f'<label>ntfy topic (empty = a new one) <input type="text" name="ntfy_url" '
                   f'value="{esc(settings.ntfy_url)}"></label>')
        hint = ""
        server = alerts.smtp_server(settings)
        if settings.alert_email and server and alerts.HINTS.get(server[0]):
            hint = f'<p class="muted">{esc(alerts.HINTS[server[0]])}</p>'
        return self.page(session, "Settings", f'{message}<section>{self.form(session, "/settings", inner, "Save")}'
                         f'{hint}<p class="muted">Saved in {esc(settings.env_file)}</p></section>'
                         f'{self._test_form(session, settings)}')

    def _schedule(self, settings: Settings) -> str:
        store = Store(settings.data_dir)
        try:
            return polling.schedule_text(settings, store, time.time(), polling.fast_seconds(settings, settings.mode))
        finally:
            store.close()

    def _poll_fields(self, settings: Settings) -> str:
        if settings.mode == "backup":
            return ""
        def minutes(seconds: int | None) -> str:
            return f"{seconds / 60:g}" if seconds else ""
        hours = ", ".join(polling.hours_range(a, b) for a, b in settings.poll_hours)
        return ('<h2>Looking for new activities</h2><p class="muted">Each look is one small request to Intervals.icu '
                f'(Garmin in upload mode). Now: {esc(self._schedule(settings))}.</p>'
                f'<label>Every … minutes (empty = {5 if settings.mode != "upload" else 10}) <input type="text" '
                f'name="poll_minutes" value="{esc(minutes(settings.poll_seconds))}" inputmode="decimal"></label>'
                f'<label>Only between (e.g. 7-23 or 6:30-9, 16-22; empty = all day) <input type="text" '
                f'name="poll_hours" value="{esc(hours)}"></label>'
                f'<label>Outside those hours every … minutes (empty = not at all) <input type="text" '
                f'name="poll_quiet_minutes" value="{esc(minutes(settings.poll_quiet_seconds))}" '
                f'inputmode="decimal"></label>'
                f'<label><input type="checkbox" name="poll_adaptive" value="1"'
                f'{" checked" if settings.poll_mode == "adaptive" else ""}> Beta: learn when my activities usually '
                'end and look at the pace above only then, less often otherwise – fewer requests for Intervals.icu'
                '</label>')

    def _test_form(self, session: Session, settings: Settings) -> str:
        if not alerts.channels(settings):
            return ""
        return ("<section>" + self.form(session, "/settings/test", "",
                                        f"Send a test message ({', '.join(alerts.channels(settings))})",
                                        light=True) + "</section>")

    def alert_test(self, session: Session, query: dict, form: dict) -> str:
        result = alerts.test(self.settings())
        lines = "".join(f'<li class="{"ok" if v == "sent" else "bad"}">{esc(k)}: {esc(v)}</li>'
                        for k, v in result.items()) or "<li>No alerts set up.</li>"
        return self.settings_page(session, f"<section><h2>Test message</h2><ul>{lines}</ul></section>")

    def settings_post(self, session: Session, query: dict, form: dict) -> str:
        from .wizard import write_env, write_private
        settings = self.settings()
        if settings.env_file is None:
            raise Reply(403, "Settings come from the container here")
        zone = form.get("timezone", "").strip()
        try:
            ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError):
            return self.settings_page(session, f'<p class="bad">"{esc(zone)}" is not a time zone name '
                                               '(Region/City, e.g. Europe/London).</p>')
        values = {"BRIDGE_TIMEZONE": zone}
        for key, name in (("imready4", "BRIDGE_IMREADY4_ENABLED"), ("hrvtrend", "BRIDGE_HRVTREND_ENABLED"),
                          ("update_check", "BRIDGE_UPDATE_CHECK"), ("intervals_notes", "BRIDGE_INTERVALS_NOTES"),
                          ("swap", "BRIDGE_SWAP")):
            values[name] = "true" if form.get(key) == "1" else "false"
        problem = self._poll_values(settings, form, values)
        if problem:
            return self.settings_page(session, f'<p class="bad">{esc(problem)}; nothing was changed.</p>')
        key = form.get("api_key", "").strip()
        if key:
            try:
                self.intervals_client(key).athlete()
            except requests.RequestException:
                return self.settings_page(session, '<p class="bad">Intervals did not accept this key; nothing '
                                                   'was changed.</p>')
            values["INTERVALS_API_KEY"] = key
        email = form.get("alert_email", "").strip()
        port = form.get("smtp_port", "").strip()
        if (email and "@" not in email) or (port and not (port.isdigit() and 1 <= int(port) <= 65535)):
            return self.settings_page(session, '<p class="bad">That e-mail address or port does not look right; '
                                               'nothing was changed.</p>')
        values.update({"BRIDGE_ALERT_EMAIL": email, "BRIDGE_SMTP_HOST": form.get("smtp_host", "").strip(),
                       "BRIDGE_SMTP_PORT": port})
        if form.get("smtp_password"):
            values["BRIDGE_SMTP_PASSWORD"] = form["smtp_password"]
        if not email:
            values["BRIDGE_SMTP_PASSWORD"] = ""
        if form.get("push") == "1":
            values["BRIDGE_NTFY_URL"] = (form.get("ntfy_url", "").strip() or settings.ntfy_url
                                         or f"https://ntfy.sh/garmin-bridge-{secrets.token_hex(8)}")
        else:
            values["BRIDGE_NTFY_URL"] = ""
        before = settings.env_file.read_text(encoding="utf-8-sig") if settings.env_file.is_file() else None
        previous = {key: os.environ.get(key) for key in values}
        try:
            write_env(settings.env_file, values)
            os.environ.update(values)
            Settings.from_env()                          # the bridge must still start with them
        except ValueError as exc:
            for key, value in previous.items():          # nothing changed: the file, the process
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            if before is not None:
                write_private(settings.env_file, before)
            return self.settings_page(session, f'<p class="bad">{esc(exc)}; nothing was changed.</p>')
        restart = ""
        if not self.in_run and autostart.platform() != "container" and \
                autostart.status(settings.env_file)["installed"]:
            restart = self.form(session, "/restart", "", "Restart the background bridge now", light=True)
        elif self.in_run:
            restart = "<p>Restart the bridge to use them.</p>"
        return self.settings_page(session, f'<section><p class="ok"><b>Saved.</b> The running bridge uses them '
                                           f'after a restart.</p>{restart}</section>')

    @staticmethod
    def _poll_values(settings: Settings, form: dict, values: dict) -> str | None:
        """The look-for-new-activities fields into `values`; what is wrong with them, if anything."""
        if settings.mode == "backup":
            return None
        seconds = {}
        for field, name, low in (("poll_minutes", "BRIDGE_POLL_SECONDS", 5 if settings.mode == "upload" else 1),
                                 ("poll_quiet_minutes", "BRIDGE_POLL_QUIET_SECONDS", 1)):
            text = form.get(field, "").strip().replace(",", ".")
            try:
                value = round(float(text) * 60) if text else None
            except (ValueError, OverflowError):
                return f'"{text}" is no number of minutes'
            if value is not None and not low * 60 <= value <= 24 * 3600:
                return f"Looking every {text} minutes: between {low} and 1440, please"
            seconds[name] = value
            values[name] = str(value) if value else ""
        try:
            poll_hours(form.get("poll_hours", ""))
        except ValueError as exc:
            return str(exc).replace("BRIDGE_POLL_HOURS: ", "Hours ")
        fast = seconds["BRIDGE_POLL_SECONDS"] or (600 if settings.mode == "upload" else 300)
        if seconds["BRIDGE_POLL_QUIET_SECONDS"] and seconds["BRIDGE_POLL_QUIET_SECONDS"] < fast:
            return "Outside the hours the bridge can only look less often, not more"
        values["BRIDGE_POLL_HOURS"] = form.get("poll_hours", "").strip()
        values["BRIDGE_POLL_MODE"] = "adaptive" if form.get("poll_adaptive") == "1" else "fixed"
        return None

    def restart_post(self, session: Session, query: dict, form: dict) -> str:
        settings = self.settings()
        if settings.env_file is None or autostart.platform() == "container":
            raise Reply(403, "Nothing to restart from here")
        # after this answer has gone out: the bridge that serves this page may be the one restarted
        threading.Timer(1.0, self.restart, args=(settings.env_file,)).start()
        return self.page(session, "Restart", '<section><p class="ok">The background bridge restarts with the '
                         'current settings.</p><p><a href="/">Back to the status</a></p></section>')

    # -- actions --

    def actions_page(self, session: Session, message: str = "") -> str:
        if self.settings().mode == "backup":
            return self.page(session, "Actions", f'{message}<section><p>Backup mode keeps your Garmin data in the '
                             'data folder; the actions here are for Intervals.icu. The past: <code>backup --from '
                             '2020-01-01</code> in a terminal.</p></section>')
        gear_form = self.form(session, "/actions/gear", (
            '<label><input type="checkbox" name="create" value="1" checked> Create Garmin gear that Intervals does '
            'not have yet</label><label><input type="checkbox" name="retired" value="1"> Retired gear too</label>'),
            "Show the gear plan")
        charts_form = self.form(session, "/actions/charts", "", "Show the chart plan")
        advice_form = self.form(session, "/actions/advice", "", "Show today's advice")
        one_form = self.form(session, "/actions/enrich", '<label>Intervals activity: its link or id (i12345678) '
                             '<input type="text" name="activity" required></label>', "Show what it gets")
        return self.page(session, "Actions", f'{message}'
                         '<section><h2>Gear</h2><p>Puts the bike or shoes Garmin recorded on the Intervals activities '
                         f'the bridge has archived. You see the plan first; nothing is written before you confirm.</p>'
                         f'{gear_form}</section>'
                         '<section><h2>Charts</h2><p>Charts for the Garmin values on Intervals\' Fitness page '
                         f'(private; existing charts are only completed).</p>{charts_form}</section>'
                         '<section><h2>Training advice</h2><p>What ImReady4\'s rules and the HRV trend say for today '
                         f'(read only).</p>{advice_form}</section>'
                         '<section><h2>One activity again</h2><p>Puts Garmin\'s values back into one activity – after '
                         'Intervals\' "Reprocess file", which drops them, or after you added a custom field or stream. '
                         f'You see what it gets first.</p>{one_form}</section>')

    def _intervals(self, settings: Settings) -> Any:
        return self.intervals_client(settings.intervals_api_key, settings.intervals_athlete_id)

    def gear_post(self, session: Session, query: dict, form: dict) -> str:
        settings = self.settings()
        create, retired = form.get("create") == "1", form.get("retired") == "1"
        store = Store(settings.data_dir)
        try:
            config = load_gear_config(store)
            plan = build_gear_plan(store, self._intervals(settings), create=create, include_retired=retired,
                                   config=config, policy=(config or {}).get("policy", "intervals"))
        finally:
            store.close()
        session.gear_plan, session.gear_options = plan, {"create": create, "retired": retired}
        out = gear_report(plan)
        rows = "".join(f'<tr><td>{esc(g["garmin"])}</td><td>{esc(g["to"])}</td><td>{esc(g["sport"])}</td>'
                       f'<td>{g["count"]}</td><td>{esc(g["from"])} &ndash; {esc(g["until"])}</td>'
                       f'<td class="muted">{esc(g["reason"])}</td></tr>' for g in out["assign"])
        made = "".join(f'<li>{esc(c.get("name"))} ({esc(c.get("type"))})</li>' for c in out["create"])
        body = (f'<section><p>{out["garmin_activities"]} archived activities with Garmin gear; '
                f'{sum(g["count"] for g in out["assign"])} get gear.</p>'
                + (f"<h2>Created in Intervals</h2><ul>{made}</ul>" if made else "")
                + (f'<table><tr><th>Garmin</th><th>Intervals</th><th>Sport</th><th>Activities</th><th>When</th>'
                   f'<th>Why</th></tr>{rows}</table>' if rows else "<p>Nothing to change.</p>")
                + "</section>")
        if out["assign"] or out["create"]:
            body += "<section>" + self.form(session, "/actions/gear-apply", "", "Apply this plan") + "</section>"
        return self.page(session, "Gear plan", body)

    def gear_apply(self, session: Session, query: dict, form: dict) -> str:
        settings = self.settings()
        plan, options = session.gear_plan, session.gear_options
        if plan is None:
            return self.actions_page(session, '<p class="warn">Show the plan first.</p>')
        session.gear_plan = None
        store = Store(settings.data_dir)
        try:
            with single_instance(settings.data_dir, ("gear",)):
                done = apply_gear_plan(plan, self._intervals(settings))
                before = load_gear_config(store) or {}
                create = options.get("create") or bool(before.get("create"))
                retired = create and (options.get("retired") or bool(before.get("include_retired")))
                save_gear_config(store, plan, done.pop("ids"), create=create, include_retired=retired)
        except InstanceBusy:
            return self.actions_page(session, '<p class="warn">A gear run is busy right now; try again in a minute.'
                                              '</p>')
        finally:
            store.close()
        return self.actions_page(session, f'<section><p class="ok"><b>Done:</b> {done["written"]} activities, '
                                          f'{len(done["created"])} gear created, {done["failed"]} failed.</p>'
                                          '</section>')

    def _one(self, session: Session, apply: bool) -> tuple[dict, str]:
        settings = self.settings()
        gid, iid = session.activity or ("", "")
        store = Store(settings.data_dir)
        try:
            with single_instance(settings.data_dir, ("enrich",)):
                garmin = self.garmin_source(settings.token_dir, settings.garmin_request_delay)
                intervals = self._intervals(settings)
                plan = enrich_activity(gid, garmin, intervals, store, apply=apply, intervals_id=iid,
                                       refresh_own=True, recheck=True)
                intake = restore_intake(gid, iid, intervals, store, apply=apply)
        finally:
            store.close()
        if intake and plan.get("outcome") == "nothing_to_add":
            plan["outcome"] = "enriched" if apply else "planned"
        fields = sorted(set((plan.get("fields") or {}).get("writes") or {}) | set(intake))
        streams = [w["type"] for w in (plan.get("streams") or {}).get("writes") or []]
        what = (f"fields: {', '.join(fields) or 'none'}; streams: {', '.join(streams) or 'none'}")
        return plan, what

    def enrich_post(self, session: Session, query: dict, form: dict) -> str:
        settings = self.settings()
        try:
            session.activity = resolve_one(self._intervals(settings), form.get("activity", ""))
        except (ValueError, requests.RequestException) as exc:
            text = str(exc) if isinstance(exc, ValueError) else "Intervals does not know this activity."
            return self.actions_page(session, f'<section><p class="bad">{esc(text)}</p></section>')
        try:
            plan, what = self._one(session, apply=False)
        except InstanceBusy:
            return self.actions_page(session, '<p class="warn">Another activity is being filled in; try again in '
                                              'a minute.</p>')
        gid, iid = session.activity
        if plan["outcome"] != "planned":
            text = {"nothing_to_add": "It has everything Garmin's original can give it.",
                    "unmatched": "Intervals does not have this activity yet."}.get(plan["outcome"], plan["outcome"])
            return self.actions_page(session, f'<section><p>{esc(iid)}: {esc(text)}</p></section>')
        confirm = self.form(session, "/actions/enrich-apply", "", "Write these values")
        return self.page(session, f"Activity {iid}", f'<section><p>From Garmin\'s original (activity {esc(gid)}) '
                         f'it gets {esc(what)}.</p>{confirm}</section>')

    def enrich_apply(self, session: Session, query: dict, form: dict) -> str:
        if session.activity is None:
            return self.actions_page(session, '<p class="warn">Choose the activity first.</p>')
        try:
            plan, what = self._one(session, apply=True)
        except InstanceBusy:
            return self.actions_page(session, '<p class="warn">Another activity is being filled in; try again in '
                                              'a minute.</p>')
        iid, session.activity = session.activity[1], None
        done = "written" if plan["outcome"] == "enriched" else "nothing to write any more"
        return self.actions_page(session, f'<section><p class="ok"><b>{esc(iid)}:</b> {esc(done)} ({esc(what)}).'
                                          '</p></section>')

    def charts_post(self, session: Session, query: dict, form: dict) -> str:
        settings = self.settings()
        intervals = self._intervals(settings)
        apply = form.get("apply") == "1"
        out = setup_charts(intervals, apply=apply)
        if apply:
            return self.actions_page(session, f'<section><p class="ok"><b>Done:</b> {len(out["created"])} charts '
                                              f'created, {len(out["updated"])} completed. Intervals: Fitness, '
                                              'a tab, custom charts, search "Garmin Bridge".</p></section>')
        names = [c["name"] for c in out["charts"]] + [f'{c["name"]} (completed)' for c in out["updates"]]
        if not names:
            return self.actions_page(session, '<section><p>All charts are there already.</p></section>')
        items = "".join(f"<li>{esc(n)}</li>" for n in names)
        confirm = self.form(session, "/actions/charts", '<input type="hidden" name="apply" value="1">',
                            "Create these charts")
        return self.page(session, "Chart plan", f"<section><ul>{items}</ul>{confirm}</section>")

    def advice_post(self, session: Session, query: dict, form: dict) -> str:
        settings = self.settings()
        intervals = self._intervals(settings)
        today = datetime.now(settings.timezone).date()
        parts = []
        for label, feature in (("ImReady4", imready4), ("HRV trend", hrvtrend)):
            try:
                out = feature.run_days(intervals, today, today, apply=False, today=today)
                day = (out.get("days") or [{}])[0]
                text = day.get("advice") or ("waits for last night's values" if day.get("waiting") else
                                             "no advice")
                why = day.get("zone") or day.get("rule") or day.get("reason") or out.get("field") or ""
                parts.append(f"<tr><th>{esc(label)}</th><td><b>{esc(text)}</b></td>"
                             f"<td class=muted>{esc(why)}</td></tr>")
            except Exception as exc:
                parts.append(f"<tr><th>{esc(label)}</th><td colspan=2 class=muted>"
                             f"{esc(type(exc).__name__)}</td></tr>")
        return self.page(session, f"Training advice {today}", f"<section><table>{''.join(parts)}</table></section>")

    # -- log --

    def log_page(self, session: Session, query: dict, form: dict) -> str:
        settings = self.settings()
        if settings.log_file is None or not settings.log_file.is_file():
            text = ("No log file: set BRIDGE_LOG_FILE (setup does). In a container, its own log shows everything."
                    if settings.log_file is None else "The log file is empty so far.")
            return self.page(session, "Log", f"<section><p>{esc(text)}</p></section>")
        with settings.log_file.open(encoding="utf-8", errors="replace") as fh:
            lines = deque(fh, maxlen=LOG_LINES)
        return self.page(session, "Log", f'<section><p class="muted">The last {len(lines)} lines of '
                         f'{esc(settings.log_file)}, newest at the bottom.</p><pre>{esc("".join(lines))}</pre>'
                         '</section>')

    # -- sign-in to the page itself (password mode) --

    def login_page(self, message: str = "") -> str:
        return self.page(None, "Sign in", f'{message}<section><form method="post" action="/login">'
                         '<label>Password (BRIDGE_UI_PASSWORD) <input type="password" name="password" '
                         'autocomplete="current-password" required autofocus></label><button>Sign in</button>'
                         '</form></section>')


GET_PAGES = {"/": "status", "/garmin": "garmin", "/settings": "settings", "/actions": "actions", "/log": "log_page"}
POST_PAGES = {"/garmin": "garmin_post", "/garmin/mfa": "garmin_mfa", "/settings": "settings_post",
              "/settings/test": "alert_test",
              "/restart": "restart_post", "/actions/gear": "gear_post", "/actions/gear-apply": "gear_apply",
              "/actions/charts": "charts_post", "/actions/advice": "advice_post", "/actions/enrich": "enrich_post",
              "/actions/enrich-apply": "enrich_apply", "/poll-now": "poll_now"}


def handler_for(ui: UI) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "garmin-intervals-bridge"
        sys_version = ""
        timeout = REQUEST_SECONDS                    # a client that sends nothing does not hold a thread

        def log_message(self, format: str, *args: Any) -> None:       # no URLs (the key) in any log
            pass

        def do_GET(self) -> None:
            self._handle("GET")

        def do_POST(self) -> None:
            self._handle("POST")

        prefix = ""                                 # Home Assistant's ingress path, for every link

        @property
        def secure(self) -> str:
            """Behind an HTTPS reverse proxy the session cookie travels over HTTPS only."""
            return "; Secure" if (self.headers.get("X-Forwarded-Proto") or "").lower() == "https" else ""

        def _send(self, status: int, body: str = "", headers: dict | None = None) -> None:
            if self.prefix:                         # our own markup only: user text is escaped (&quot;)
                body = body.replace('href="/', f'href="{self.prefix}/').replace('action="/', f'action="{self.prefix}/')
                if headers and headers.get("Location", "").startswith("/"):
                    headers = {**headers, "Location": self.prefix + headers["Location"]}
            data = body.encode("utf-8")
            self.send_response(status)
            base = INGRESS_HEADERS if ui.ingress else HEADERS
            for name, value in {**base, "Content-Type": "text/html; charset=utf-8", **(headers or {})}.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _handle(self, method: str) -> None:
            try:
                self._send(200, self._route(method))
            except Reply as reply:
                self._send(reply.status, reply.body, reply.headers)
            except Exception as exc:
                log.warning("Web page error on %s: %s", urlsplit(self.path).path, type(exc).__name__)
                self._send(500, ui.page(None, "Something went wrong", f'<section><p class="bad">'
                                        f'{esc(type(exc).__name__)}</p><p><a href="/">Back</a></p></section>'))

        def _route(self, method: str) -> str:
            url = urlsplit(self.path)
            path, query = url.path, {k: v[0] for k, v in parse_qs(url.query).items()}
            if ui.ingress:
                if self.client_address[0] != INGRESS_IP:
                    raise Reply(403, "Only through Home Assistant")
                given = self.headers.get("X-Ingress-Path", "")
                self.prefix = given if INGRESS_PATH.fullmatch(given) else ""
            if ui.local:
                allowed = {f"{h}:{ui.port}" for h in ("127.0.0.1", "localhost", "[::1]")}
                if self.headers.get("Host", "") not in allowed:
                    raise Reply(403, "Wrong host")
            form: dict = {}
            if method == "POST":
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    raise Reply(400, "Bad length") from None
                if not 0 <= length <= MAX_BODY:
                    raise Reply(413, "Too large")
                raw = self.rfile.read(length).decode("utf-8", errors="replace")
                form = {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}
                origin = self.headers.get("Origin")
                # behind ingress the browser talks to Home Assistant, not to us: the token decides;
                # "null" says nothing (a privacy setting) – the session's token and cookie still must fit
                if origin and origin != "null" and not ui.ingress and \
                        urlsplit(origin).netloc != self.headers.get("Host", ""):
                    raise Reply(403, "Foreign form")
            session = ui.session(self.headers.get("Cookie"))
            if session is None:
                return self._without_session(method, path, query, form)
            if path == "/login":
                raise Reply(303, "", {"Location": "/"})
            if path == "/logout":
                with ui.lock:
                    ui.sessions.pop(session.id, None)
                raise Reply(303, "", {"Location": "/login" if ui.password else "/",
                                      "Set-Cookie": f"{COOKIE}=; Max-Age=0; Path=/; HttpOnly; SameSite=Strict"
                                                    + self.secure})
            if method == "POST":
                if not hmac.compare_digest(form.get("csrf", ""), session.csrf):
                    raise Reply(403, "Form expired; reload the page")
                name = POST_PAGES.get(path)
                if name is None:
                    raise Reply(404, "Not found")
                return getattr(ui, name)(session, query, form)
            name = GET_PAGES.get(path)
            if name is None:
                raise Reply(404, "Not found")
            if name == "garmin":
                return ui.garmin_page(session)
            if name == "settings":
                return ui.settings_page(session)
            if name == "actions":
                return ui.actions_page(session)
            return getattr(ui, name)(session, query, form)

        def _without_session(self, method: str, path: str, query: dict, form: dict) -> str:
            cookie = ("{0}={1}; Path=" + (self.prefix or "/") + "; HttpOnly; SameSite=Strict; Max-Age="
                      + str(SESSION_SECONDS) + self.secure)
            if ui.ingress:                          # Home Assistant signed the user in already
                session = ui.new_session()
                raise Reply(303, "", {"Location": path, "Set-Cookie": cookie.format(COOKIE, session.id)})
            if ui.key is not None:
                if hmac.compare_digest(query.get("key", ""), ui.key):
                    session = ui.new_session()
                    raise Reply(303, "", {"Location": path, "Set-Cookie": cookie.format(COOKIE, session.id)})
                raise Reply(403, ui.page(None, "Open the link from the terminal",
                                         "<section><p>This page opens with the link "
                                         "<code>garmin-intervals-bridge ui</code> printed.</p></section>"))
            if path == "/login" and method == "POST":
                if not ui.login_gate.acquire(blocking=False):        # guesses in parallel: one at a time
                    return ui.login_page('<p class="warn">Another sign-in is being checked; try again in a '
                                         'moment.</p>')
                try:
                    if ui.password and hmac.compare_digest(form.get("password", "").encode(),
                                                           ui.password.encode()):
                        with ui.lock:
                            ui.failures = 0
                        session = ui.new_session()
                        raise Reply(303, "", {"Location": "/", "Set-Cookie": cookie.format(COOKIE, session.id)})
                    with ui.lock:
                        ui.failures += 1
                        pause = min(30, ui.failures)
                    time.sleep(pause)                                # slows guessing down
                finally:
                    ui.login_gate.release()
                return ui.login_page('<p class="bad">Wrong password.</p>')
            if path == "/login":
                return ui.login_page()
            raise Reply(303, "", {"Location": "/login"})

    return Handler


def check(host: str, password: str | None, in_run: bool) -> None:
    if password is not None and len(password) < 8:
        raise ValueError("BRIDGE_UI_PASSWORD must have 8 characters or more")
    if password == PLACEHOLDER:
        raise ValueError("BRIDGE_UI_PASSWORD is still the template's placeholder: choose your own")
    if (in_run or host not in LOCAL_HOSTS) and not password:
        raise ValueError("The web page " + ("next to `run`" if in_run else f"on {host}") +
                         " needs BRIDGE_UI_PASSWORD (8 characters or more)")


class _Server(ThreadingHTTPServer):
    """At most MAX_CONNECTIONS requests at once; one more is closed instead of getting a thread."""
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.slots = threading.BoundedSemaphore(MAX_CONNECTIONS)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def make_server(ui: UI) -> ThreadingHTTPServer:
    server = _Server((ui.host, ui.port), handler_for(ui))
    ui.port = server.server_address[1]
    return server


def serve(*, host: str = "127.0.0.1", port: int = DEFAULT_PORT, open_browser: bool = True) -> int:
    """`garmin-intervals-bridge ui`: until Ctrl+C."""
    password = os.getenv("BRIDGE_UI_PASSWORD") or None
    check(host, password, in_run=False)
    ui = UI(host=host, port=port, password=password, key=None if password else secrets.token_urlsafe(24))
    server = make_server(ui)
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    url = f"http://{shown}:{ui.port}/" + (f"?key={ui.key}" if ui.key else "")
    print(f"The bridge's page: {url}\n(Ctrl+C closes it; the bridge itself keeps running in the background.)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def start_in_background(port: int, host: str) -> ThreadingHTTPServer:
    """Next to `run` (a container on a NAS): a password, never a key in a log. In a Home Assistant
    app: its ingress instead, nothing else let in."""
    ingress = os.getenv("BRIDGE_UI_INGRESS", "").strip().lower() in ("1", "true", "yes")
    password = None if ingress else (os.getenv("BRIDGE_UI_PASSWORD") or None)
    if not ingress:
        check(host, password, in_run=True)
    ui = UI(host=host, port=port, password=password, in_run=True, ingress=ingress)
    server = make_server(ui)
    threading.Thread(target=server.serve_forever, name="web-page", daemon=True).start()
    log.info("Web page on port %d (%s)", ui.port,
             "through Home Assistant's ingress only" if ingress else "sign in with BRIDGE_UI_PASSWORD")
    return server

