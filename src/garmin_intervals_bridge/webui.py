"""A small web page for the bridge: what it does in plain words, the Garmin sign-in (MFA
included), the settings, the one-off actions, the log. Standard library only.

Three ways to open it:
  `garmin-intervals-bridge ui`   on your own computer: 127.0.0.1 only, the link it opens
                                 carries a random key (other users of the computer and web
                                 pages in your browser cannot use the page without it)
  `run` with BRIDGE_UI_PORT      next to the running bridge, e.g. in a container on a NAS:
                                 needs BRIDGE_UI_PASSWORD (8 characters or more)

Safety: every form carries a per-session token and must come from this page (CSRF); a
page on 127.0.0.1 answers only to its own host name (DNS rebinding); no caching, no
framing, no scripts. The Garmin password is handed to Garmin and dropped; the API key
can be replaced but is never shown. Nothing is written to Intervals without a button
that says so.
"""
from __future__ import annotations

import errno
import hmac
import html
import logging
import os
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from datetime import date, datetime
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
from .garmin import GarminBlocked, GarminSource, sign_in_problem
from .gear import apply_plan as apply_gear_plan
from .gear import build_plan as build_gear_plan
from .gear import load_config as load_gear_config
from .gear import report as gear_report
from .gear import save_config as save_gear_config
from .health import silent_error_checks
from .intervals import IntervalsClient
from .store import InstanceBusy, Store, single_instance
from .sync import restore_intake
from .wizard import command_name

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
STYLE = """
:root{--bg:#f4f6f9;--fg:#1d2330;--card:#fff;--line:#e1e5eb;--field:#c9cfd8;--code:#f0f2f5;--accent:#1f6feb;
--accent-fg:#fff;--ok:#17803d;--warn:#a35c00;--bad:#b42318;--muted:#667085;--head:#1d2330;color-scheme:light dark}
@media (prefers-color-scheme:dark){:root{--bg:#111418;--fg:#e4e7ec;--card:#1b1f25;--line:#2c323b;--field:#3d4550;
--code:#242931;--accent:#4c8dff;--accent-fg:#0b0d10;--ok:#4cc27a;--warn:#e5a54b;--bad:#f07a6f;--muted:#98a2b3;
--head:#0b0d10}}
*{box-sizing:border-box}
body{font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:var(--bg);color:var(--fg);
overflow-wrap:anywhere}
header{background:var(--head);color:#fff;padding:0 16px;display:flex;flex-wrap:wrap;column-gap:18px;align-items:center}
header b{padding:12px 6px 12px 0}
header a{color:#b9c2d3;text-decoration:none;padding:12px 0 10px;border-bottom:2px solid transparent}
header a:hover{color:#fff}header a[aria-current]{color:#fff;border-color:var(--accent)}
main{max-width:760px;margin:0 auto;padding:20px 16px 40px}
a{color:var(--accent)}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 20px;margin:0 0 16px}
section>:first-child,form>:first-child{margin-top:0}section>:last-child{margin-bottom:0}
h1{font-size:22px;margin:0 0 16px}h2{font-size:16px;margin:0 0 8px}
p{margin:0 0 10px}ul{margin:0;padding-left:20px}li{margin:4px 0}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}.muted{color:var(--muted)}
main>p.ok,main>p.warn,main>p.bad{background:var(--card);border:1px solid var(--line);border-left:4px solid;
border-radius:10px;padding:12px 16px;margin:0 0 16px;background:color-mix(in srgb,currentColor 7%,var(--card))}
p+form,form+p{margin-top:16px}
table{border-collapse:collapse;width:100%}
td,th{text-align:left;padding:9px 12px 9px 0;vertical-align:top;border-top:1px solid var(--line)}
tr:first-child>td,tr:first-child>th{border-top:0;padding-top:0}tr:last-child>td,tr:last-child>th{padding-bottom:0}
th{font-weight:600;white-space:nowrap;width:1%;padding-right:24px}
section>table th{color:var(--muted);font-weight:500}
label{display:block;margin:0 0 14px;font-weight:500}
label>input:not([type=checkbox]),label>select{display:block;width:100%;max-width:440px;margin-top:5px;font:inherit;font-weight:400;
padding:8px 10px;border:1px solid var(--field);border-radius:6px;background:var(--card);color:var(--fg)}
label>input:focus,label>select:focus{outline:2px solid var(--accent);outline-offset:1px;border-color:var(--accent)}
label:has(>input[type=checkbox]){display:flex;gap:10px;align-items:flex-start;font-weight:400;margin-bottom:10px}
input[type=checkbox]{flex:none;width:16px;height:16px;margin:3px 0 0;accent-color:var(--accent)}
form h2{margin:24px -20px 12px;padding:16px 20px 0;border-top:1px solid var(--line)}form h2+p{margin-top:-4px}
button{font:inherit;font-weight:600;padding:8px 18px;border-radius:6px;border:1px solid var(--accent);
background:var(--accent);color:var(--accent-fg);cursor:pointer}
button:hover{filter:brightness(1.08)}button:active{transform:translateY(1px)}
button.light{background:transparent;color:var(--accent);padding:5px 12px}
form.inline{display:inline-block;margin-left:6px}
section>form>button,label+button{margin-top:4px}
code{font-size:13px;background:var(--code);padding:1px 5px;border-radius:4px}
pre{white-space:pre-wrap;word-break:break-word;font-size:12px;background:var(--code);padding:10px 12px;
border-radius:6px;margin:0}
.busy{display:flex;gap:14px;align-items:center}.busy p{margin:0}
.spin{flex:none;width:24px;height:24px;border:3px solid var(--line);border-top-color:var(--accent);border-radius:50%;
animation:spin 1s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}
"""
GARMIN_WAIT = 2.0                          # a quick Garmin answer is shown at once; a slow one gets a waiting page
GARMIN_REFRESH = 2                         # seconds between the waiting page's reloads (a meta refresh: no script)


def settings_source() -> str:
    """Where settings come from when there is no settings file to edit here."""
    if autostart.platform() == "container":
        return "the container's settings (its environment variables)"
    if os.getenv("INVOCATION_ID"):                  # a systemd service: its EnvironmentFile
        return "the service's environment file (<code>/etc/garmin-intervals-bridge/env</code> in the shipped units)"
    return "the environment the bridge runs with"


def installed_version() -> str | None:
    """The version pip has installed now – newer than this process's after an update while it ran."""
    try:
        from importlib.metadata import version
        return version("garmin-intervals-bridge")
    except Exception:
        return None


def _newer(a: str, b: str) -> bool:
    """Release a after b; anything else (an editable install's stale number, say: older) is not."""
    mine, theirs = notices._version(a), notices._version(b)
    return mine is not None and theirs is not None and mine > theirs


def _home_short(path: str) -> str:
    home = str(Path.home())
    return "~" + path[len(home):] if path.startswith(home + os.sep) else path


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
        self.garmin_job: GarminJob | None = None     # a sign-in step talking to Garmin right now
        self.gear_plan: dict | None = None           # the plan shown, applied exactly as shown
        self.gear_options: dict = {}
        self.activity: tuple[str, str] | None = None  # (Garmin id, Intervals id) shown, to fill in again


class GarminJob:
    """One sign-in step (the password, or the MFA code) running beside the page: Garmin can take
    half a minute, and the page shows that it is waiting instead of hanging."""

    def __init__(self, step: str, work: Callable[[], Any]):
        self.step, self.started = step, time.time()
        self.outcome: Any = None
        self.error: Exception | None = None
        self.done = threading.Event()
        threading.Thread(target=self._run, args=(work,), name="garmin-sign-in", daemon=True).start()

    def _run(self, work: Callable[[], Any]) -> None:
        try:
            self.outcome = work()
        except Exception as exc:
            self.error = exc
        finally:
            self.done.set()


class Reply(Exception):
    """Ends a request with this response (redirects, refusals)."""

    def __init__(self, status: int, body: str = "", headers: dict | None = None):
        super().__init__(status)
        self.status, self.body, self.headers = status, body, headers or {}


class UI:
    def __init__(self, *, host: str, port: int, password: str | None = None, key: str | None = None,
                 in_run: bool = False, settings: Callable[[], Settings] = Settings.from_env,
                 intervals_client: Callable[..., Any] = IntervalsClient,
                 garmin_source: Callable[..., Any] = GarminSource,
                 newer_release: Callable[[], str | None] = notices.newer_release,
                 restart: Callable[[Path], Any] | None = None):
        self.host, self.port, self.password, self.key, self.in_run = host, port, password, key, in_run
        self.settings, self.intervals_client, self.garmin_source = settings, intervals_client, garmin_source
        self.newer_release = newer_release
        self.restart = restart or (lambda env: autostart.install(env))
        self.sessions: dict[str, Session] = {}
        self.lock = threading.Lock()
        self.failures = 0
        self.login_gate = threading.Lock()          # one password check at a time: guessing stays slow
        self._update: tuple[float, str | None] = (0.0, None)
        self.backfill: dict | None = None            # the past, started from the page: its process and what it does

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

    def _how_to_update(self) -> str:
        if autostart.platform() == "container":
            return "Pull the new image and recreate the container."
        cmd = command_name()
        pip = "py -m pip" if autostart.platform() == "windows" else f"{_home_short(sys.executable)} -m pip"
        page = ("" if self.in_run else f" – and this page: Ctrl+C in the window where it runs, then "
                                       f"<code>{esc(cmd)} ui</code>")
        return (f"Update: <code>{esc(pip)} install --upgrade garmin-intervals-bridge</code>, then "
                f"<code>{esc(cmd)} autostart</code> (restarts the background bridge){page}.")

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

    def page(self, session: Session | None, title: str, body: str, refresh: int = 0) -> str:
        nav = ""
        if session is not None:
            here = {"Status": "Status", "Garmin Connect": "Garmin", "Settings": "Settings", "Actions": "Actions",
                    "Log": "Log"}.get(title)
            nav = "".join(f'<a href="{href}"{" aria-current=page" if label == here else ""}>{label}</a>'
                          for href, label in (
                ("/", "Status"), ("/garmin", "Garmin"), ("/settings", "Settings"), ("/actions", "Actions"),
                ("/log", "Log")))
            if self.password:
                nav += '<a href="/logout">Sign out</a>'
        return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
                f'<meta name="viewport" content="width=device-width,initial-scale=1">'
                f'{f"<meta http-equiv=refresh content={refresh}>" if refresh else ""}'
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
        installed = installed_version()
        if installed and _newer(installed, __version__):
            # updated, but this page is the process started before: it still runs the old code
            how = ("restart the bridge, which serves it" if self.in_run else
                   f"press Ctrl+C in the window where it runs (or close that window), then "
                   f"<code>{esc(command_name())} ui</code> again")
            parts.append(f'<section><b class="warn">This page still runs {esc(__version__)}</b>, but '
                         f'{esc(installed)} is installed: {how}.</section>')
        elif latest:
            parts.append(f'<section><b class="warn">Version {esc(latest)} is out</b> (this is '
                         f'{esc(__version__)}). {self._how_to_update()}</section>')
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

            ran = str((runs[-1]["metrics"] or {}).get("version") or "") if runs else ""
            version = esc(__version__) + (f" &middot; the bridge's last run: {esc(ran)}"
                                          if ran and ran != __version__ else "")
            rows = [("Bridge", line), ("Version", version)]
            kind = autostart.platform()
            if not self.in_run and kind != "container" and settings.env_file is not None:
                state = autostart.status(settings.env_file)
                rows.append(("Starts when you log in to this computer",
                             '<span class="ok">yes</span>' if state["installed"] else
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
                                     + (f" + swap ({settings.swap_method})"
                                        if settings.swap and settings.mode == "enrich" else ""))))
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
        if not self.local:
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
            if not message:
                message = self._garmin_now()
        return self.page(session, "Garmin Connect", f'{message}<section><p>Your password goes to Garmin for this '
                         'sign-in only and is not stored; the bridge keeps the session, which renews itself for '
                         f'about a year.</p>{warning}{body}</section>')

    def _garmin_now(self) -> str:
        """Whether the bridge is signed in already: the form is then only to replace that session."""
        settings = self.settings()
        if not (settings.token_dir.is_dir() and any(settings.token_dir.glob("*.json"))):
            return ""
        store = Store(settings.data_dir)
        try:
            if store.get_meta(notices.LOGIN_NOTE_KEY):
                return '<p class="bad"><b>Garmin wants a new sign-in.</b> The saved session no longer works.</p>'
            ok = store.meta_updated("garmin_last_ok")
        finally:
            store.close()
        return (f'<p class="ok"><b>Signed in</b> &middot; last confirmed {esc(_ago(ok))}. Sign in again only to '
                'replace this session.</p>')

    def garmin_get(self, session: Session, query: dict, form: dict) -> str:
        job = session.garmin_job
        if job is not None:
            return self._garmin_result(session, job)
        if session.garmin is not None and time.time() - session.garmin_since <= PENDING_SECONDS:
            return self.garmin_page(session, mfa=True)                  # still waiting for the code
        return self.garmin_page(session)

    def garmin_post(self, session: Session, query: dict, form: dict) -> str:
        if session.garmin_job is not None and not session.garmin_job.done.is_set():
            raise Reply(303, "", {"Location": "/garmin"})              # one sign-in at a time: show that one
        settings = self.settings()
        garmin = self.garmin_source(settings.token_dir, settings.garmin_request_delay)
        email, password = form.get("email", "").strip(), form.get("password", "")
        job = self._garmin_start(session, "password", lambda: (garmin, garmin.start_login(email, password)))
        session.garmin = None
        return self._garmin_result(session, job, wait=True)

    def garmin_mfa(self, session: Session, query: dict, form: dict) -> str:
        if session.garmin_job is not None and not session.garmin_job.done.is_set():
            raise Reply(303, "", {"Location": "/garmin"})
        garmin = session.garmin
        if garmin is None or time.time() - session.garmin_since > PENDING_SECONDS:
            session.garmin = None
            return self.garmin_page(session, '<p class="warn">That took too long; please sign in again.</p>')
        code = form.get("code", "")
        job = self._garmin_start(session, "code", lambda: (garmin, garmin.finish_login(code)))
        return self._garmin_result(session, job, wait=True)

    def _garmin_start(self, session: Session, step: str, work: Callable[[], Any]) -> GarminJob:
        with self.lock:                      # two clicks at once: still one sign-in, the other shows it
            if session.garmin_job is not None and not session.garmin_job.done.is_set():
                raise Reply(303, "", {"Location": "/garmin"})
            job = session.garmin_job = GarminJob(step, work)
        return job

    def _garmin_take(self, session: Session, job: GarminJob) -> None:
        """This request answers for the job; with two reloads at once the other one shows the page anew."""
        with self.lock:
            if session.garmin_job is not job:
                raise Reply(303, "", {"Location": "/garmin"})
            session.garmin_job = None

    def _garmin_result(self, session: Session, job: GarminJob, wait: bool = False) -> str:
        """The finished sign-in step's answer, or a page that says Garmin is still being asked."""
        if wait:
            job.done.wait(GARMIN_WAIT)
        if not job.done.is_set():
            if wait:                                    # a GET from here on: reloading it never sends the form again
                raise Reply(303, "", {"Location": "/garmin"})
            seconds = time.time() - job.started
            if seconds > PENDING_SECONDS:
                self._garmin_take(session, job)
                return self.garmin_page(session, '<p class="bad">Garmin has not answered for ten minutes; try again '
                                                 'later.</p>')
            what = "Signing in to Garmin Connect" if job.step == "password" else "Checking the code with Garmin"
            return self.page(session, "Garmin Connect", f'<section class="busy"><div class="spin"></div><div><p><b>'
                             f'{what}&hellip;</b> {int(seconds)} s so far</p><p class="muted">Garmin often takes '
                             'up to half a minute. This page updates by itself.</p></div></section>',
                             refresh=GARMIN_REFRESH)
        self._garmin_take(session, job)
        exc = job.error
        if job.step == "code":
            if exc is not None:
                return self.garmin_page(session, f'<p class="bad">Garmin did not accept the code '
                                                 f'({esc(type(exc).__name__)}); try again.</p>', mfa=True)
            session.garmin = None
            return self._signed_in(session, self.settings())
        if isinstance(exc, GarminBlocked):
            return self.garmin_page(session, '<p class="bad">Garmin limits sign-ins right now (429). Wait an hour, '
                                             'then try again.</p>')
        if exc is not None:
            return self.garmin_page(session, f'<p class="bad">{esc(sign_in_problem(exc))}</p>')
        garmin, outcome = job.outcome
        if outcome == "mfa":
            session.garmin, session.garmin_since = garmin, time.time()
            return self.garmin_page(session, '<p class="ok">Garmin accepted the e-mail and password and sent you a '
                                             'code (by e-mail or in its app).</p>', mfa=True)
        return self._signed_in(session, self.settings())

    def _signed_in(self, session: Session, settings: Settings) -> str:
        store = Store(settings.data_dir)
        try:
            notices.garmin_ok(store)
        finally:
            store.close()
        notices.SIGNED_IN.set()
        soon = "goes on with it at once" if self.in_run else "uses it within 15 minutes"
        return self.page(session, "Garmin Connect", f'<p class="ok"><b>Signed in.</b> The session is saved; the '
                         f'running bridge {soon}.</p><p><a href="/">Back to the status</a></p>')

    # -- settings --

    def settings_page(self, session: Session, message: str = "") -> str:
        settings = self.settings()
        if settings.env_file is None:
            rows = [("Time zone", settings.timezone.key), ("ImReady4 advice", settings.imready4),
                    ("HRV trend advice", settings.hrvtrend), ("Update hint", settings.update_check),
                    ("Calendar note when Garmin needs a sign-in", settings.intervals_notes),
                    ("Swap Garmin's copy for the original", f"{settings.swap} ({settings.swap_method})"
                     if settings.swap else settings.swap),
                    ("New activities", self._schedule(settings)),
                    ("Alerts", ", ".join(alerts.channels(settings)) or "none (BRIDGE_ALERT_EMAIL, BRIDGE_NTFY_URL)")]
            table = "".join(f"<tr><th>{esc(k)}</th><td>{esc(v)}</td></tr>" for k, v in rows)
            where = settings_source()
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
                 + box("swap", "Swap Garmin's filtered copy of each new activity for the original ('Reprocess file' "
                               "then keeps everything; nothing on Garmin Connect is touched)", settings.swap)
                 + '<label>How <select name="swap_method">'
                 + "".join(f'<option value="{v}"{" selected" if settings.swap_method == v else ""}>{esc(t)}</option>'
                           for v, t in (("replace", "into the same activity – id, notes, edited intervals stay, "
                                                    "nothing deleted (recommended)"),
                                        ("upload", "as a new activity of your own – Garmin's copy deleted, never "
                                                   "one with comments or edited intervals")))
                 + '</select></label>'
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
        values["BRIDGE_SWAP_METHOD"] = "upload" if form.get("swap_method") == "upload" else "replace"
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
        threading.Timer(1.0, self._restart, args=(settings.env_file,)).start()
        return self.page(session, "Restart", '<section><p class="ok">The background bridge restarts with the '
                         'current settings.</p><p><a href="/">Back to the status</a></p></section>')

    def _restart(self, env_file: Path) -> None:
        try:
            self.restart(env_file)
        except Exception as exc:                  # after the answer went out: the log is where it shows
            log.error("Restart of the background bridge failed: %s: %s", type(exc).__name__, exc)

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
        past = self._backfill_section(session)
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
                         f'You see what it gets first.</p>{one_form}</section>{past}')

    # -- the past: a backfill in the background --

    def _backfill_running(self) -> dict | None:
        job = self.backfill
        if job is not None and job.get("exit") is None:
            job["exit"] = job["proc"].poll()
        return job

    def _backfill_section(self, session: Session) -> str:
        job = self._backfill_running()
        what = ("Fills in what Garmin's own sync left out on earlier days – Garmin's values on the activities (or, "
                "with swap, the original file into each of them) or the wellness days. It runs in the background, "
                "paced for Garmin, next to the running bridge; the <a href=\"/log\">Log</a> shows how far it got. "
                "You see what it will do first.")
        if job is not None and job["exit"] is None:
            return (f'<section><h2>The past</h2><p class="warn"><b>Running:</b> {esc(job["label"])}, started '
                    f'{esc(_ago(job["started"]))}. The <a href="/log">Log</a> shows how far it got.</p></section>')
        done = ""
        if job is not None:
            ok = job["exit"] == 0
            ended = "finished" if ok else f"exit code {job['exit']}"
            done = (f'<p class="{"ok" if ok else "bad"}"><b>{"Done" if ok else "Stopped"}:</b> {esc(job["label"])} '
                    f'({ended}; details in the <a href="/log">Log</a>).</p>')
        today = date.today().isoformat()
        form = self.form(session, "/actions/backfill", (
            '<label>What <select name="scope"><option value="activities">Activities</option>'
            '<option value="wellness">Wellness</option></select></label>'
            f'<label>From <input type="date" name="start" required max="{today}"></label>'
            f'<label>To <input type="date" name="end" value="{today}" max="{today}"></label>'
            '<label><input type="checkbox" name="swap" value="1"> Activities: swap – the original file into Garmin\'s '
            'copy (same activity, nothing deleted), so Intervals computes everything itself</label>'),
            "Show what it will do")
        return f'<section><h2>The past</h2>{done}<p>{what}</p>{form}</section>'

    def _backfill_request(self, form: dict) -> tuple[dict | None, str]:
        """The request from the form, checked; (None, why) when it does not make sense."""
        scope = form.get("scope", "")
        if scope not in ("activities", "wellness"):
            return None, "Choose activities or wellness."
        try:
            start = date.fromisoformat(form.get("start", ""))
            end = date.fromisoformat(form.get("end") or date.today().isoformat())
        except ValueError:
            return None, "Dates as YYYY-MM-DD, please."
        if start > end:
            return None, "From must not be after To."
        if end > date.today():
            return None, "To must not be in the future."
        if start < date(2006, 1, 1):
            return None, "Garmin Connect has nothing before 2006."
        swap = scope == "activities" and form.get("swap") == "1" and self.settings().mode != "upload"
        return {"scope": scope, "start": start, "end": end, "swap": swap}, ""

    def backfill_post(self, session: Session, query: dict, form: dict) -> str:
        request, why = self._backfill_request(form)
        if request is None:
            return self.actions_page(session, f'<p class="warn">{esc(why)}</p>')
        days = (request["end"] - request["start"]).days + 1
        if request["scope"] == "wellness":
            plan = (f"Wellness for {days} days ({request['start']} to {request['end']}): about 15 Garmin requests "
                    f"per day, two seconds apart – roughly {max(1, round(days * 10 / 60))} minutes. Days the bridge "
                    "has read before are skipped; only empty fields are filled.")
        else:
            plan = (f"Activities from {request['start']} to {request['end']}: one download of the original per "
                    "activity that is not archived yet, two seconds apart. "
                    + ("Each original goes into Garmin's copy in Intervals (swap, in place): same activity, nothing "
                       "deleted; edited intervals stay." if request["swap"] else
                       "Garmin's values go into the empty fields of Garmin's copies; nothing else is changed."))
        hidden = "".join(f'<input type="hidden" name="{k}" value="{esc(v)}">' for k, v in
                         (("scope", request["scope"]), ("start", request["start"]), ("end", request["end"]),
                          ("swap", "1" if request["swap"] else "")))
        start = self.form(session, "/actions/backfill-start", hidden, "Start it – writes to Intervals")
        return self.page(session, "The past", f'<section><p>{esc(plan)}</p><p class="muted">It runs in the '
                         f'background, next to the running bridge; you can close this page.</p>{start}</section>')

    def backfill_start(self, session: Session, query: dict, form: dict) -> str:
        request, why = self._backfill_request(form)
        if request is None:
            return self.actions_page(session, f'<p class="warn">{esc(why)}</p>')
        with self.lock:                                # one from the page at a time
            job = self._backfill_running()
            if job is not None and job["exit"] is None:
                return self.actions_page(session, '<p class="warn">A backfill from this page is still running.</p>')
            cmd = [sys.executable, "-m", "garmin_intervals_bridge", "backfill", "--scope", request["scope"],
                   "--from", request["start"].isoformat(), "--to", request["end"].isoformat(), "--apply"]
            if request["scope"] == "activities" and self.settings().mode == "upload":
                cmd += ["--mode", "upload"]
            if request["swap"]:
                cmd.append("--swap")
            label = (f"{request['scope']} {request['start']} to {request['end']}"
                     + (" with swap" if request["swap"] else ""))
            try:
                proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                        **({} if os.name == "nt" else {"start_new_session": True}))
            except OSError as exc:
                log.error("The backfill did not start: %s", exc)
                return self.actions_page(session, f'<p class="bad">The backfill did not start: {esc(exc)}</p>')
            self.backfill = {"proc": proc, "label": label, "started": time.time(), "exit": None}
        log.info("Backfill started from the web page: %s", label)
        return self.actions_page(session, f'<p class="ok"><b>Started:</b> {esc(label)}.</p>')

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
            text = ("The log file is empty so far." if settings.log_file is not None else
                    "No log file: under systemd the journal has everything (journalctl -u "
                    "'garmin-intervals-bridge*')." if os.getenv("INVOCATION_ID") else
                    "No log file: set BRIDGE_LOG_FILE (setup does). In a container, its own log shows everything.")
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


GET_PAGES = {"/": "status", "/garmin": "garmin_get", "/settings": "settings", "/actions": "actions", "/log": "log_page"}
POST_PAGES = {"/garmin": "garmin_post", "/garmin/mfa": "garmin_mfa", "/settings": "settings_post",
              "/settings/test": "alert_test",
              "/restart": "restart_post", "/actions/gear": "gear_post", "/actions/gear-apply": "gear_apply",
              "/actions/charts": "charts_post", "/actions/advice": "advice_post", "/actions/enrich": "enrich_post",
              "/actions/enrich-apply": "enrich_apply", "/poll-now": "poll_now",
              "/actions/backfill": "backfill_post", "/actions/backfill-start": "backfill_start"}


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

        @property
        def secure(self) -> str:
            """Behind an HTTPS reverse proxy the session cookie travels over HTTPS only."""
            return "; Secure" if (self.headers.get("X-Forwarded-Proto") or "").lower() == "https" else ""

        def _send(self, status: int, body: str = "", headers: dict | None = None) -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            for name, value in {**HEADERS, "Content-Type": "text/html; charset=utf-8", **(headers or {})}.items():
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
                # "null" says nothing (a privacy setting) – the session's token and cookie still must fit
                if origin and origin != "null" and \
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
            if name == "settings":
                return ui.settings_page(session)
            if name == "actions":
                return ui.actions_page(session)
            return getattr(ui, name)(session, query, form)

        def _without_session(self, method: str, path: str, query: dict, form: dict) -> str:
            cookie = ("{0}={1}; Path=" + "/" + "; HttpOnly; SameSite=Strict; Max-Age="
                      + str(SESSION_SECONDS) + self.secure)
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
    # Windows' SO_REUSEADDR lets a second process bind a port in use – two pages, either one answering
    allow_reuse_address = os.name != "nt"

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
    try:
        server = make_server(ui)
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE and getattr(exc, "winerror", None) != 10048:
            raise
        print(f"Port {port} is in use – most likely by a page of the bridge that is still open, perhaps one "
              f"started before an update. Press Ctrl+C in its window (or close that window) and start this "
              f"again; or one on another port: {command_name()} ui --port {port + 1}", file=sys.stderr)
        return 1
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
    """Next to `run` (a container on a NAS): a password, never a key in a log."""
    password = os.getenv("BRIDGE_UI_PASSWORD") or None
    check(host, password, in_run=True)
    ui = UI(host=host, port=port, password=password, in_run=True)
    server = make_server(ui)
    threading.Thread(target=server.serve_forever, name="web-page", daemon=True).start()
    log.info("Web page on port %d (sign in with BRIDGE_UI_PASSWORD)", ui.port)
    return server

