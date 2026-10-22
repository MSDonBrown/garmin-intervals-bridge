"""`garmin-intervals-bridge setup`: from nothing to a bridge running in the background, by
answering questions.

Every answer is checked where it can be: the key against Intervals, the zone against the
time zone database, Garmin by signing in. The settings file is saved after each step (only
its owner may read it; lines setup does not set are kept), so an interrupted setup goes on
where it stopped. Nothing is written to Intervals before the last question, which follows
a trial run. Run it again to change anything: the saved answers are the defaults.
"""
from __future__ import annotations

import getpass
import os
import secrets
import shutil
import sys
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

from . import __version__, alerts, autostart, hrvtrend, imready4
from .backup import backup_once
from .backup import describe as describe_backup
from .charts import setup_charts
from .config import Settings, bridge_home, env_file, env_line_key, parse_env_file
from .garmin import GarminBlocked, GarminLoginNeeded, GarminSource, sign_in_problem
from .gear import RECENT_DAYS
from .gear import apply_plan as apply_gear_plan
from .gear import build_plan as build_gear_plan
from .gear import load_config as load_gear_config
from .gear import save_config as save_gear_config
from .intervals import IntervalsClient
from .store import InstanceBusy, Store, single_instance

HEADER = ("# Garmin -> Intervals.icu Bridge settings, written by `garmin-intervals-bridge setup`.\n"
          "# Run setup again to change them, or edit this file (all settings: README, Configuration).\n")
STEPS = 6
GUIDES = "https://github.com/futureweb/garmin-intervals-bridge/tree/main/docs"


def command_name() -> str:
    """How this person starts the bridge, for the commands setup prints."""
    if autostart.platform() == "windows":
        return "py -m garmin_intervals_bridge"
    started = Path(sys.argv[0]) if sys.argv and sys.argv[0] else None
    if started is not None and started.name == "garmin-intervals-bridge":
        found = shutil.which("garmin-intervals-bridge")
        if found and Path(found).resolve() == started.resolve():
            return "garmin-intervals-bridge"
        path = str(started.resolve())
        home = str(Path.home())
        return "~" + path[len(home):] if path.startswith(home + os.sep) else path
    return "python3 -m garmin_intervals_bridge"


class Prompt:
    """Questions on the terminal; tests hand in scripted answers instead."""

    def __init__(self, ask: Callable[[str], str] = input, secret: Callable[[str], str] = getpass.getpass,
                 say: Callable[[str], Any] = print):
        self._ask, self._secret, self._say = ask, secret, say

    def say(self, text: str = "") -> None:
        self._say(text)

    def ask(self, question: str, default: str = "") -> str:
        answer = self._ask(f"{question} [{default}]: " if default else f"{question}: ").strip()
        return answer or default

    def yes(self, question: str, default: bool = True) -> bool:
        while True:
            answer = self._ask(f"{question} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
            if not answer:
                return default
            if answer in ("y", "yes", "j", "ja"):
                return True
            if answer in ("n", "no", "nein"):
                return False
            self.say("Please answer y or n.")

    def secret(self, question: str) -> str:
        # what a paste brings along is no part of it: in the classic Windows console Ctrl+V reaches a
        # hidden prompt as the control character ^V, a copied line may end in a line break
        return "".join(c for c in self._secret(f"{question}: ") if c.isprintable()).strip()


# ---- the settings file ----

_ENV_LOCK = threading.Lock()                # two saves at once (a double click) take turns


def env_value(value: str) -> str:
    """A value as a settings line keeps it: quoted when reading it back would change it."""
    if any(c in value for c in "\r\n\0"):
        raise ValueError("A setting cannot contain a line break")
    if value != value.strip() or (len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'"):
        quote = "'" if value.startswith('"') and value.endswith('"') else '"'
        return f"{quote}{value}{quote}"
    return value


def write_env(path: Path, values: dict[str, str]) -> None:
    """Set these variables, keep every other line; readable by its owner only."""
    encoded = {key: env_value(value) for key, value in values.items()}
    with _ENV_LOCK:
        lines = path.read_text(encoding="utf-8-sig").splitlines() if path.is_file() else HEADER.splitlines()
        done: set[str] = set()
        for index, line in enumerate(lines):
            key = env_line_key(line)
            if key in encoded:
                lines[index] = f"{key}={encoded[key]}"
                done.add(key)
        lines += [f"{key}={value}" for key, value in encoded.items() if key not in done]
        write_private(path, "\n".join(lines) + "\n")


def write_private(path: Path, text: str) -> None:
    """Replace a file in one step (a fresh temporary file next to it), readable by its owner only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _is_bridge_file(path: Path) -> bool:
    """A ./.env of some other project is not ours to write into."""
    try:
        return any(k == "INTERVALS_API_KEY" or k.startswith(("BRIDGE_", "GARMIN_")) for k in parse_env_file(path))
    except (OSError, UnicodeDecodeError):
        return False


def _settings_file(p: Prompt) -> Path:
    named = os.getenv("BRIDGE_ENV_FILE", "").strip()
    if named:
        return Path(named).expanduser().resolve()
    found = env_file()
    if found is not None and _is_bridge_file(found):
        p.say(f"Settings file: {found.resolve()} (your earlier answers are the defaults)")
        return found.resolve()
    folder = p.ask("Folder for the bridge: settings, log and your Garmin data", str(bridge_home()))
    return (Path(folder).expanduser() / ".env").resolve()


# ---- the steps ----

def _athlete_name(athlete: dict) -> str:
    name = athlete.get("name") or " ".join(x for x in (athlete.get("firstname"), athlete.get("lastname")) if x)
    return str(name or athlete.get("id") or "your account")


def _paste_hint() -> tuple[str, str, str] | None:
    """How a paste reaches the hidden key prompt: (first try, why it may not, the other ways). No
    guessing at the window (Barry_Turner in the forum got the classic console's advice in a window
    where Ctrl+Shift+V pasted): Windows Terminal pastes Ctrl+V itself, the classic console hands
    it over as ^V – which is dropped, so "nothing arrived" names the other ways."""
    if os.name == "nt":
        return ("Ctrl+V", "this window does not pass Ctrl+V on to a hidden prompt",
                "a right-click (on a trackpad a two-finger tap) or Ctrl+Shift+V")
    if sys.platform != "darwin":
        return ("Ctrl+Shift+V", "Ctrl+V does not paste in a terminal", "Ctrl+Shift+V or a right-click → Paste")
    return None                                       # the Mac: Cmd+V, everywhere


def _intervals_key(p: Prompt, saved: str, client: Callable[[str], Any]) -> tuple[str, Any] | None:
    if saved:
        try:
            intervals = client(saved)
            name = _athlete_name(intervals.athlete())
            if p.yes(f"The saved key works ({name}). Keep it?"):
                return saved, intervals
        except requests.HTTPError:
            p.say("Intervals does not accept the saved key any more.")
        except requests.RequestException as exc:
            p.say(f"Could not reach intervals.icu ({type(exc).__name__}); check the internet connection.")
            return None
    paste = _paste_hint()
    p.say("Your Intervals.icu API key: intervals.icu -> Settings -> Developer Settings -> API key.\n"
          "Copy it from there; it is not shown while you paste it, and it stays on this computer."
          + (f"\nPaste it with {paste[0]}." if paste else ""))
    for _ in range(3):
        key = p.secret("API key").strip("\"'").strip()          # quotes copied along are no part of it
        if not key:
            p.say("Nothing arrived" + (f" – {paste[1]}: paste with {paste[2]}, then Enter." if paste else "."))
            continue
        try:
            intervals = client(key)
            name = _athlete_name(intervals.athlete())
        except requests.HTTPError as exc:
            status = getattr(exc.response, "status_code", None)
            if status in (401, 403) and len(key) < 16:
                p.say(f"Intervals refused it – only {len(key)} characters arrived, an API key is longer."
                      + (f" Paste with {paste[2]}, then Enter." if paste else ""))
            else:
                p.say("Intervals refused this key; check that it is complete." if status in (401, 403)
                      else f"Intervals answered with an error ({status}); try again.")
            continue
        except requests.RequestException as exc:
            p.say(f"Could not reach intervals.icu ({type(exc).__name__}); check the internet connection.")
            continue
        p.say(f"Connected to Intervals.icu as {name}.")
        return key, intervals
    return None


def _timezone(p: Prompt, saved: str) -> str:
    try:
        from tzlocal import get_localzone_name
        detected = get_localzone_name() or ""
    except Exception:
        detected = ""
    current = saved or detected or "UTC"
    p.say("The time zone decides which date last night's sleep and today's totals belong to.")
    if p.yes(f"Time zone {current} - right?"):
        return current
    while True:
        name = p.ask("Your time zone, like Europe/London or America/New_York").strip()
        try:
            ZoneInfo(name)
            return name
        except (ZoneInfoNotFoundError, ValueError):
            p.say(f"'{name}' is not a known time zone name (Region/City, as in the list on "
                  "https://en.wikipedia.org/wiki/List_of_tz_database_time_zones).")


def _garmin(p: Prompt, settings: Settings, source: Callable[..., Any]) -> Any | None:
    garmin = source(settings.token_dir, settings.garmin_request_delay)
    try:
        garmin.login(interactive=False)
        p.say("Garmin Connect: signed in with the saved session.")
        return garmin
    except GarminLoginNeeded:
        pass
    except GarminBlocked as exc:
        p.say(f"Garmin is not answering or limits sign-ins right now ({exc}). Wait an hour, then run setup "
              "again; your answers so far are saved.")
        return None
    p.say("Sign in to Garmin Connect once. Your password is used for this sign-in only and not stored; "
          "the session is saved in the bridge's folder and renews itself for about a year.")
    try:
        garmin.login(interactive=True)
    except GarminBlocked as exc:
        p.say(f"Garmin refused the sign-in for now ({exc}). If it says 429: wait an hour, then run setup again.")
        return None
    except Exception as exc:
        p.say(f"{sign_in_problem(exc)} Run setup again then; your answers so far are saved.")
        return None
    p.say("Garmin Connect: signed in.")
    return garmin


def _extras(p: Prompt, saved: dict[str, str], intervals: Any, gear_config: dict | None) -> dict[str, Any]:
    out: dict[str, Any] = {"gear": False}
    if gear_config:
        p.say(f"Gear: set up earlier, stays as it is (`{command_name()} gear` shows it).")
    else:
        p.say("Your gear: Garmin knows which bike or shoes an activity used; Intervals only knows what you\n"
              "pick there. The bridge can fill that in and create your Garmin gear in Intervals.")
        out["gear"] = p.yes("Put your Garmin gear on your Intervals activities?", default=True)
        if out["gear"]:
            out["retired"] = p.yes("Your retired gear too (created as retired)?", default=False)
    field = imready4.find_field(intervals)
    out["imready4"] = False
    if field is not None and imready4.field_problem(field) is None:
        p.say("You have ImReady4's TrainingAdvice field. The bridge can fill it each morning from your\n"
              "HRV and resting heart rate, as ImReady4's script does (only if you do not run that script).")
        out["imready4"] = p.yes("Fill TrainingAdvice each morning?",
                                default=saved.get("BRIDGE_IMREADY4_ENABLED", "").lower() == "true")
    out["hrvtrend"] = p.yes("Experimental: a training advice from your 7-day HRV trend, in its own field?",
                            default=saved.get("BRIDGE_HRVTREND_ENABLED", "").lower() == "true")
    out["charts"] = p.yes("Add charts for the Garmin values to your Intervals Fitness page?", default=True)
    p.say("Garmin sends Intervals a filtered file. The bridge completes it (the default) - or it swaps it for\n"
          "the original right after it arrives: Intervals then holds the complete file, and its 'Reprocess file'\n"
          "keeps everything. The original goes into the same activity: its notes, RPE, comments and edited\n"
          "intervals stay, nothing is deleted, nothing on Garmin Connect is touched.")
    out["swap"] = p.yes("Swap Garmin's copy for the original?",
                        default=saved.get("BRIDGE_SWAP", "").lower() == "true")
    return out


def send_test(channel: str, values: dict[str, str]) -> str:
    """A test message through one channel with these (not yet saved) values: 'sent' or why not."""
    target = SimpleNamespace(alert_email=values.get("BRIDGE_ALERT_EMAIL"), smtp_host=values.get("BRIDGE_SMTP_HOST"),
                             smtp_port=values.get("BRIDGE_SMTP_PORT"), smtp_user=values.get("BRIDGE_SMTP_USER"),
                             smtp_password=values.get("BRIDGE_SMTP_PASSWORD"), smtp_from=None,
                             ntfy_url=values.get("BRIDGE_NTFY_URL"), ntfy_token=values.get("BRIDGE_NTFY_TOKEN"))
    try:
        if channel == "e-mail":
            alerts.send_email(target, alerts.TEST_SUBJECT, alerts.TEST_TEXT)
        else:
            alerts.send_push(target, alerts.TEST_SUBJECT, alerts.TEST_TEXT)
        return "sent"
    except Exception as exc:
        return alerts._why(exc)


def _email(p: Prompt, saved: dict[str, str], notify: Callable[[str, dict], str]) -> dict[str, str]:
    address = saved.get("BRIDGE_ALERT_EMAIL", "")
    for _ in range(3):
        address = p.ask("Your e-mail address", address).strip()       # a second try starts from the first
        if "@" not in address:
            p.say("That is not an e-mail address.")
            continue
        values = {"BRIDGE_ALERT_EMAIL": address}
        server = alerts.provider(address)
        if server is None:
            host = p.ask("Your mailbox's outgoing mail server (SMTP), e.g. smtp.example.com",
                         saved.get("BRIDGE_SMTP_HOST", "")).strip()
            port = p.ask("Its port (587, or 465)", saved.get("BRIDGE_SMTP_PORT", "587")).strip()
            user = p.ask("User name at the mail server", saved.get("BRIDGE_SMTP_USER", address)).strip()
            if not host or not port.isdigit():
                p.say("Without a server and port the bridge cannot send e-mail.")
                continue
            values.update({"BRIDGE_SMTP_HOST": host, "BRIDGE_SMTP_PORT": port, "BRIDGE_SMTP_USER": user})
            server = (host, int(port))
        p.say(f"The bridge sends from your own mailbox to yourself, through {server[0]}.")
        if alerts.HINTS.get(server[0]):
            p.say(alerts.HINTS[server[0]])
        password = saved.get("BRIDGE_SMTP_PASSWORD", "")
        if not (password and address == saved.get("BRIDGE_ALERT_EMAIL") and p.yes("Keep the saved password?")):
            password = p.secret("Password for the bridge (not shown while you type or paste)")
        values["BRIDGE_SMTP_PASSWORD"] = password
        result = notify("e-mail", values)
        if result == "sent":
            p.say(f"Test e-mail sent to {address}: look in your inbox (and the spam folder).")
            return values
        p.say(f"Sending failed: {result}.")
        if not p.yes("Try again?"):
            break
    p.say("No e-mail alerts; setup can add them later.")
    return {"BRIDGE_ALERT_EMAIL": "", "BRIDGE_SMTP_PASSWORD": ""}


def _push(p: Prompt, current: str, notify: Callable[[str, dict], str]) -> dict[str, str]:
    url = current or f"https://ntfy.sh/garmin-bridge-{secrets.token_hex(8)}"
    server, topic = url.rsplit("/", 1)
    p.say("1. Install the free app 'ntfy' (App Store or Google Play); it needs no account.\n"
          f"2. In the app tap +, and subscribe to the topic:  {topic}"
          + ("" if server == "https://ntfy.sh" else f"  (server {server})") + "\n"
          "   Whoever knows the topic name can read its messages: they only say that the bridge needs you.")
    p.ask("Press Enter once you have subscribed")
    result = notify("push", {"BRIDGE_NTFY_URL": url})
    if result == "sent":
        p.say("Test message sent: it should be on your phone now.")
        return {"BRIDGE_NTFY_URL": url}
    p.say(f"Sending failed: {result}.")
    return {"BRIDGE_NTFY_URL": url} if p.yes("Keep the push messages anyway?", default=False) else \
        {"BRIDGE_NTFY_URL": ""}


def _alerts(p: Prompt, saved: dict[str, str], notify: Callable[[str, dict], str], *,
            calendar: bool = True) -> dict[str, str]:
    if calendar:
        p.say("When the bridge needs you - Garmin wants a new sign-in, or it has been failing for a day - it puts\n"
              "a note in your Intervals calendar. It can also tell you by e-mail, or with a push message on your "
              "phone.")
    else:
        p.say("When the bridge needs you - Garmin wants a new sign-in, or it has been failing for a day - it can\n"
              "tell you by e-mail, or with a push message on your phone.")
    values: dict[str, str] = {}
    mail = saved.get("BRIDGE_ALERT_EMAIL", "")
    if p.yes("By e-mail?" + (f" (now: {mail})" if mail else ""), default=bool(mail)):
        values.update(_email(p, saved, notify))
    elif mail:
        values.update({"BRIDGE_ALERT_EMAIL": "", "BRIDGE_SMTP_PASSWORD": ""})
    push = saved.get("BRIDGE_NTFY_URL", "")
    if p.yes("As a push message on your phone (free ntfy app)?", default=bool(push)):
        values.update(_push(p, push, notify))
    elif push:
        values["BRIDGE_NTFY_URL"] = ""
    return values


def _trial(settings: Settings, garmin: Any, intervals: Any, store: Store) -> dict:
    """What a first real run would do, writing nothing: two days of activities, last night."""
    from .sync import sync_enrich, sync_wellness
    activities = sync_enrich(settings, garmin, intervals, store, apply=False, activity_days=2)
    wellness = sync_wellness(settings, garmin, intervals, store, apply=False, wellness_days=1)
    return {"activities": activities, "wellness": wellness}


def _describe_trial(result: dict) -> list[str]:
    a, w = result["activities"], result["wellness"]
    waiting = f", {a['unmatched']} not in Intervals yet (Garmin's sync brings them)" if a.get("unmatched") else ""
    lines = [f"  Activities of the last 2 days in Garmin: {a.get('seen', 0)}; "
             f"{a.get('planned', 0)} would get what Garmin's own sync leaves out{waiting}"]
    if w.get("days_with_changes"):
        lines.append("  Last night's wellness values would be written.")
    elif w.get("days_checked"):
        lines.append("  Wellness: nothing new for last night yet (it comes after your watch has synced).")
    if w.get("new_fields"):
        lines.append(f"  New wellness fields in Intervals: {len(w['new_fields'])} (private, created on first use)")
    if a.get("failed"):
        lines.append(f"  {a['failed']} activities failed this time; the bridge retries them.")
    return lines


def run(prompt: Prompt | None = None, *, intervals_client: Callable[[str], Any] | None = None,
        garmin_source: Callable[..., Any] = GarminSource, install: Callable[..., Any] = autostart.install,
        trial: Callable[..., dict] = _trial, notify: Callable[[str, dict], str] = send_test,
        backup: Callable[..., dict] = backup_once) -> int:
    p = prompt or Prompt()
    if autostart.platform() == "container":
        p.say("In a container the settings are the container's variables (INTERVALS_API_KEY, BRIDGE_TIMEZONE),\n"
              "and it restarts itself. Sign in to Garmin with `garmin-intervals-bridge login` in its terminal.\n"
              "Guide: https://github.com/futureweb/garmin-intervals-bridge/blob/main/docs/SYNOLOGY.md")
        return 1
    if prompt is None and not sys.stdin.isatty():
        raise ValueError("setup asks questions: run it in a terminal window")
    client = intervals_client or IntervalsClient
    p.say(f"Garmin -> Intervals.icu Bridge {__version__}: setup. Ctrl+C stops it at any point.\n")
    try:
        return _run(p, client, garmin_source, install, trial, notify, backup)
    except KeyboardInterrupt:
        p.say("\nSetup stopped. What was answered so far is saved; run setup again to go on.")
        return 1


def _run(p: Prompt, client: Callable[[str], Any], garmin_source: Callable[..., Any],
         install: Callable[..., Any], trial: Callable[..., dict], notify: Callable[[str, dict], str],
         backup: Callable[..., dict]) -> int:
    path = _settings_file(p)
    saved = parse_env_file(path) if path.is_file() else {}

    def save(values: dict[str, str]) -> Settings:
        write_env(path, values)
        os.environ["BRIDGE_ENV_FILE"] = str(path)
        os.environ.update(values)                       # what this setup chose wins over earlier variables
        return Settings.from_env()

    p.say("What should the bridge do?\n"
          "  1  Bring your complete Garmin data into Intervals.icu (you use Intervals)\n"
          "  2  Only back up your Garmin data to this computer - no Intervals needed")
    choice = p.ask("1 or 2", "2" if saved.get("BRIDGE_MODE") == "backup" else "1").strip()
    if choice == "2":
        return _run_backup(p, path, saved, save, garmin_source, install, notify, backup)
    mode = saved.get("BRIDGE_MODE") if saved.get("BRIDGE_MODE") in ("enrich", "upload") else "enrich"

    p.say(f"\n1/{STEPS} Intervals.icu")
    key = _intervals_key(p, saved.get("INTERVALS_API_KEY", ""), client)
    if key is None:
        p.say("No working key; run setup again when you have it.")
        return 1
    api_key, intervals = key

    p.say(f"\n2/{STEPS} Time zone")
    zone = _timezone(p, saved.get("BRIDGE_TIMEZONE", ""))
    settings = save({"INTERVALS_API_KEY": api_key, "BRIDGE_TIMEZONE": zone, "BRIDGE_MODE": mode,
                     "BRIDGE_LOG_FILE": saved.get("BRIDGE_LOG_FILE") or "bridge.log"})
    settings.ensure_data_dir()
    p.say(f"Saved in {path}")

    p.say(f"\n3/{STEPS} Garmin Connect")
    garmin = _garmin(p, settings, garmin_source)
    if garmin is None:
        return 1

    p.say(f"\n4/{STEPS} Extras")
    store = Store(settings.data_dir)
    try:
        extras = _extras(p, saved, intervals, load_gear_config(store))
        settings = save({"BRIDGE_IMREADY4_ENABLED": str(extras["imready4"]).lower(),
                         "BRIDGE_HRVTREND_ENABLED": str(extras["hrvtrend"]).lower(),
                         "BRIDGE_SWAP": str(extras["swap"]).lower()})

        p.say(f"\n5/{STEPS} When the bridge needs you")
        chosen = _alerts(p, saved, notify)
        if chosen:
            settings = save(chosen)

        p.say(f"\n6/{STEPS} Trial run and start")
        running = False
        try:
            with single_instance(settings.data_dir, ("activities", "wellness")):
                p.say("Trial run, nothing is written yet ...")
                for line in _describe_trial(trial(settings, garmin, intervals, store)):
                    p.say(line)
        except InstanceBusy:
            running = True
            p.say("The bridge already runs in the background; it restarts with these settings.")
        if not p.yes("Start now: write to Intervals, and run in the background – from now on whenever you log in "
                     "to this computer (no terminal needed)?"):
            me = command_name()
            p.say(f"Nothing written to Intervals. Start it later with: {me} autostart\n"
                  f"(or in a window that stays open: {me} run --apply)")
            return 0
        notes = _first_writes(p, settings, store, intervals, extras)
    finally:
        store.close()
    steps = install(settings.env_file or path)
    p.say("\nDone. The bridge runs in the background now"
          + (" (restarted)." if running else " and starts by itself whenever you log in to this computer."))
    for line in notes:
        p.say(line)
    me = command_name()
    p.say(f"  Log:       {settings.log_file}\n"
          f"  Settings:  {path}  (to change them: {me} setup)\n"
          f"  Its page:  {me} ui  (status, Garmin sign-in, settings, gear, the log - in your browser)\n"
          f"  Is it on:  {me} autostart --status\n"
          f"  Stop it:   {me} autostart --remove\n"
          f"  The past, gear of older activities, updating: the guide for your system, {GUIDES}")
    if steps.get("note"):
        p.say(f"  Note:      {steps['note']}")
    return 0


def _run_backup(p: Prompt, path: Path, saved: dict[str, str], save: Callable[[dict], Settings],
                garmin_source: Callable[..., Any], install: Callable[..., Any], notify: Callable[[str, dict], str],
                backup: Callable[..., dict]) -> int:
    """Backup mode: no Intervals; the time zone, Garmin, alerts, a first backup, the start."""
    steps = 4
    p.say(f"\n1/{steps} Time zone")
    zone = _timezone(p, saved.get("BRIDGE_TIMEZONE", ""))
    settings = save({"BRIDGE_MODE": "backup", "BRIDGE_TIMEZONE": zone,
                     "BRIDGE_LOG_FILE": saved.get("BRIDGE_LOG_FILE") or "bridge.log"})
    settings.ensure_data_dir()
    p.say(f"Saved in {path}")
    p.say(f"\n2/{steps} Garmin Connect")
    garmin = _garmin(p, settings, garmin_source)
    if garmin is None:
        return 1
    p.say(f"\n3/{steps} When the bridge needs you")
    chosen = _alerts(p, saved, notify, calendar=False)
    if chosen:
        settings = save(chosen)
    p.say(f"\n4/{steps} First backup and start")
    running = False
    store = Store(settings.data_dir)
    try:
        with single_instance(settings.data_dir, ("activities", "wellness")):
            p.say("Backing up the last days ...")
            p.say(f"  {describe_backup(backup(settings, garmin, store))}")
    except InstanceBusy:
        running = True
        p.say("The bridge already runs in the background; it restarts with these settings.")
    finally:
        store.close()
    me = command_name()
    if not p.yes("Keep backing up in the background – from now on whenever you log in to this computer?"):
        p.say(f"Start it later with: {me} autostart  (or in a window that stays open: {me} run)")
        return 0
    steps_done = install(settings.env_file or path, mode="backup")
    p.say("\nDone. The backup runs in the background now" + (" (restarted)." if running else
                                                               " and starts by itself whenever you log in to "
                                                               "this computer."))
    p.say(f"  Your data: {settings.data_dir}  (what is where: {GUIDES}/BACKUP.md)\n"
          f"  The past:  {me} backup --from 2020-01-01  (once; runs next to the background backup, takes a while)\n"
          f"  Log:       {settings.log_file}\n"
          f"  Settings:  {path}  (to change them: {me} setup)\n"
          f"  Is it on:  {me} autostart --status\n"
          f"  Stop it:   {me} autostart --remove")
    if steps_done.get("note"):
        p.say(f"  Note:      {steps_done['note']}")
    return 0


def _first_writes(p: Prompt, settings: Settings, store: Store, intervals: Any, extras: dict) -> list[str]:
    """The writes the answers asked for; a failure is reported, the bridge still starts."""
    notes: list[str] = []
    if extras.get("charts"):
        try:
            out = setup_charts(intervals, apply=True)
            notes.append(f"  Charts:    {len(out['created'])} created, {len(out['updated'])} updated "
                         "(Intervals -> Fitness, chart picker)")
        except Exception as exc:
            notes.append(f"  Charts:    not created ({type(exc).__name__}); later: {command_name()} "
                         "setup-charts --apply")
    if extras.get("hrvtrend"):
        try:
            hrvtrend.ensure_fields(intervals, apply=True)
        except Exception as exc:
            notes.append(f"  HRV trend: {exc}")
    if extras.get("gear"):
        try:
            today = datetime.now(settings.timezone).date()
            with single_instance(settings.data_dir, ("gear",)):
                # the last week, as the scheduled runs do: older activities only after `gear` showed its plan
                plan = build_gear_plan(store, intervals, start=today - timedelta(days=RECENT_DAYS), end=today,
                                       create=True, include_retired=bool(extras.get("retired")))
                done = apply_gear_plan(plan, intervals)
                save_gear_config(store, plan, done.pop("ids"), create=True,
                                 include_retired=bool(extras.get("retired")))
            made = len(done.get("created") or [])
            notes.append(f"  Gear:      on; {done.get('written', 0)} activities of the last week, {made} gear created"
                         if done.get("written") or made else "  Gear:      on; new activities get their Garmin gear")
        except Exception as exc:
            notes.append(f"  Gear:      not set up ({type(exc).__name__}); later: {command_name()} gear "
                         "--create --apply")
    if extras.get("imready4"):
        notes.append("  ImReady4:  TrainingAdvice is filled each morning once the night's values are in")
    return notes
