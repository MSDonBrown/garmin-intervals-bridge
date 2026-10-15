"""Config from environment without loading secrets into logs."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _timezone() -> tuple[ZoneInfo, str]:
    """BRIDGE_TIMEZONE, else this computer's own zone (TZ, /etc/localtime, Windows' setting), else UTC.

    The zone is the day boundary: which date last night's sleep and today's totals belong to.
    """
    name, source = os.getenv("BRIDGE_TIMEZONE", "").strip(), "BRIDGE_TIMEZONE"
    if not name:
        try:
            from tzlocal import get_localzone_name
            name, source = get_localzone_name() or "", "this computer"
        except Exception:                      # no zone configured at all (a bare container)
            name = ""
    if not name:
        name, source = "UTC", "fallback, nothing set"
    try:
        return ZoneInfo(name), source
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Unknown time zone {name!r} (from {source}): set BRIDGE_TIMEZONE to yours, "
                         "e.g. Europe/London") from exc


HOME_FOLDER = "GarminBridge"


def bridge_home() -> Path:
    """Where `setup` puts a bridge: ~/GarminBridge (on Windows %USERPROFILE%\\GarminBridge)."""
    return Path.home() / HOME_FOLDER


def env_file() -> Path | None:
    """The settings file that applies: BRIDGE_ENV_FILE, else ./.env, else ~/GarminBridge/.env.

    The last one is what `setup` writes, so a bridge set up that way answers in any terminal,
    from any folder. systemd and containers set everything themselves and have none of these.
    """
    named = os.getenv("BRIDGE_ENV_FILE", "").strip()
    if named:
        return Path(named).expanduser()
    for candidate in (Path(".env"), bridge_home() / ".env"):
        if candidate.is_file():
            return candidate
    return None


def _path(name: str, default: str | None, base: Path) -> Path | None:
    """A path setting; a relative one counts from the settings file's folder."""
    value = os.getenv(name, "").strip() or default
    if not value:
        return None
    path = Path(value).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def configured_log_file() -> Path | None:
    """BRIDGE_LOG_FILE as the settings will see it, read before them: so that even a broken
    setting is reported in the log file of a bridge that runs without a window."""
    settings_file = env_file()
    try:
        load_env_file(settings_file)
        base = settings_file.parent.resolve() if settings_file is not None else Path.cwd()
        return _path("BRIDGE_LOG_FILE", None, base)
    except (OSError, ValueError):
        return None


def _flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name, "").strip().lower()
    if value == "":
        return default
    if value in ("0", "false", "no", "off"):
        return False
    if value in ("1", "true", "yes", "on"):
        return True
    raise ValueError(f"{name} must be true or false")


def poll_hours(text: str) -> tuple[tuple[int, int], ...]:
    """'10-18', '6:30-9,17-22', '22-6' (over midnight) -> ranges in minutes after midnight."""
    ranges = []
    for part in [p.strip() for p in (text or "").split(",") if p.strip()]:
        found = re.fullmatch(r"(\d{1,2})(?::([0-5]\d))?[ \t]*-[ \t]*(\d{1,2})(?::([0-5]\d))?", part)
        if not found:
            raise ValueError(f"BRIDGE_POLL_HOURS: '{part}' is no range like 10-18 or 6:30-9")
        start = int(found.group(1)) * 60 + int(found.group(2) or 0)
        end = int(found.group(3)) * 60 + int(found.group(4) or 0)
        if start >= 24 * 60 or end > 24 * 60 or start == end % (24 * 60):    # 24 only as an end: 22-24
            raise ValueError(f"BRIDGE_POLL_HOURS: '{part}' is no range of the day")
        ranges.append((start, end))
    return tuple(ranges)


def within_hours(when: Any, ranges: tuple[tuple[int, int], ...]) -> bool:
    """No ranges: always. Otherwise inside one of them (a range may run over midnight)."""
    if not ranges:
        return True
    minute = when.hour * 60 + when.minute
    return any(start <= minute < end if start < end else (minute >= start or minute < end) for start, end in ranges)


MODES = ("enrich", "upload", "backup")
POLL_MODES = ("fixed", "adaptive")
SWAP_METHODS = ("replace", "upload")


def _swap_method() -> str:
    """BRIDGE_SWAP_METHOD: replace (default) – the original file into the same activity; upload – the
    original as a new activity of your own, Garmin's copy deleted."""
    method = os.getenv("BRIDGE_SWAP_METHOD", "").strip().lower() or "replace"
    if method not in SWAP_METHODS:
        raise ValueError(f"BRIDGE_SWAP_METHOD must be one of {', '.join(SWAP_METHODS)}")
    return method


def _mode() -> str:
    """BRIDGE_MODE: what `run` does without --mode – enrich (default), upload, or backup (Garmin only)."""
    mode = os.getenv("BRIDGE_MODE", "").strip().lower() or "enrich"
    if mode not in MODES:
        raise ValueError(f"BRIDGE_MODE must be one of {', '.join(MODES)}")
    return mode


def _poll_mode() -> str:
    """BRIDGE_POLL_MODE: fixed (default) or adaptive – fast only when your activities usually end."""
    mode = os.getenv("BRIDGE_POLL_MODE", "").strip().lower() or "fixed"
    if mode not in POLL_MODES:
        raise ValueError(f"BRIDGE_POLL_MODE must be one of {', '.join(POLL_MODES)}")
    return mode


def _text(name: str) -> str | None:
    return os.getenv(name, "").strip() or None


def _port(name: str) -> int | None:
    value = os.getenv(name, "").strip()
    if not value:
        return None
    if not value.isdigit() or not 1 <= int(value) <= 65535:
        raise ValueError(f"{name} must be a port number (1-65535)")
    return int(value)


# A Home Assistant add-on hands its form to the container as /data/options.json.
OPTIONS = {"intervals_api_key": "INTERVALS_API_KEY", "timezone": "BRIDGE_TIMEZONE",
           "imready4": "BRIDGE_IMREADY4_ENABLED", "hrvtrend": "BRIDGE_HRVTREND_ENABLED",
           "update_check": "BRIDGE_UPDATE_CHECK", "intervals_notes": "BRIDGE_INTERVALS_NOTES",
           "ui_password": "BRIDGE_UI_PASSWORD", "alert_email": "BRIDGE_ALERT_EMAIL",
           "smtp_password": "BRIDGE_SMTP_PASSWORD", "ntfy_url": "BRIDGE_NTFY_URL", "mode": "BRIDGE_MODE",
           "swap": "BRIDGE_SWAP", "swap_method": "BRIDGE_SWAP_METHOD",
           "poll_hours": "BRIDGE_POLL_HOURS", "poll_seconds": "BRIDGE_POLL_SECONDS",
           "backup_hours": "BRIDGE_BACKUP_HOURS", "poll_quiet_seconds": "BRIDGE_POLL_QUIET_SECONDS",
           "poll_mode": "BRIDGE_POLL_MODE"}


def load_options_file(path: Path | None = None) -> list[str]:
    """Settings from an add-on's options file (BRIDGE_OPTIONS_FILE, else /data/options.json);
    variables already set win, as with a settings file."""
    candidate = path or Path(os.getenv("BRIDGE_OPTIONS_FILE", "").strip() or "/data/options.json")
    if not candidate.is_file():
        return []
    import json
    try:
        options = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    loaded = []
    for option, name in OPTIONS.items():
        value = options.get(option) if isinstance(options, dict) else None
        if value is None or value == "" or name in os.environ:
            continue
        os.environ[name] = str(value).lower() if isinstance(value, bool) else str(value)
        loaded.append(name)
    return loaded


def _positive_int(name: str, default: int, max_value: int) -> int:
    raw = os.getenv(name, "").strip()                    # set but empty (`NAME=`): the default
    value = int(raw) if raw else default
    if not 1 <= value <= max_value:
        raise ValueError(f"{name} must be between 1 and {max_value}")
    return value


def load_env_file(path: Path | None = None) -> list[str]:
    """Read KEY=VALUE lines from `.env` (or BRIDGE_ENV_FILE) into the environment.

    Variables already set win, so a systemd EnvironmentFile or an exported
    variable is never overridden. Values are never logged; the names are returned.
    """
    candidate = path or env_file()
    if candidate is None or not candidate.is_file():
        return []
    loaded: list[str] = []
    for key, value in parse_env_file(candidate).items():
        if key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


def env_line_key(line: str) -> str | None:
    """The variable a KEY=VALUE line sets; None for comments, blanks and anything else."""
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    key = line.split("=", 1)[0].strip().removeprefix("export ").strip()
    return key if re.fullmatch(r"[A-Z][A-Z0-9_]*", key) else None


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        key = env_line_key(line)
        if key is None:
            continue
        value = line.split("=", 1)[1].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    token_dir: Path
    intervals_api_key: str = field(repr=False)
    intervals_athlete_id: str
    timezone: ZoneInfo
    activity_days: int
    wellness_days: int
    wellness_refresh_hours: int
    garmin_request_delay: float
    wellness_profile: str = "recommended"
    stale_hours: int = 24
    timezone_source: str = "BRIDGE_TIMEZONE"
    imready4: bool = False
    hrvtrend: bool = False
    env_file: Path | None = None
    log_file: Path | None = None
    update_check: bool = True
    intervals_notes: bool = True
    ui_port: int | None = None
    ui_host: str = "127.0.0.1"
    alert_email: str | None = None
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_user: str | None = None
    smtp_password: str | None = field(default=None, repr=False)
    smtp_from: str | None = None
    ntfy_url: str | None = field(default=None, repr=False)        # the topic is the secret
    ntfy_token: str | None = field(default=None, repr=False)
    swap: bool = False
    swap_method: str = "replace"
    swap_hours: int = 48
    poll_seconds: int | None = None
    poll_hours: tuple = ()
    poll_quiet_seconds: int | None = None
    poll_mode: str = "fixed"
    mode: str = "enrich"
    backup_hours: int = 12

    @classmethod
    def from_env(cls) -> "Settings":
        settings_file = env_file()
        load_env_file(settings_file)
        if settings_file is None:
            load_options_file()
        base = settings_file.parent.resolve() if settings_file is not None else Path.cwd()
        data_dir = _path("BRIDGE_DATA_DIR", "data", base)
        token_dir = _path("GARMIN_TOKEN_DIR", str(data_dir / "tokens"), base)
        tz, tz_source = _timezone()
        delay = float(os.getenv("BRIDGE_GARMIN_REQUEST_DELAY", "").strip() or "0.5")
        if delay < 0.25:
            raise ValueError("BRIDGE_GARMIN_REQUEST_DELAY must be >= 0.25 seconds")
        profile = os.getenv("BRIDGE_WELLNESS_PROFILE", "recommended").strip().lower()
        if profile not in ("recommended", "all"):
            raise ValueError("BRIDGE_WELLNESS_PROFILE must be 'recommended' or 'all'")
        athlete_id = os.getenv("INTERVALS_ATHLETE_ID", "0").strip()
        if re.fullmatch(r"i?\d+", athlete_id) is None:
            raise ValueError("Invalid INTERVALS_ATHLETE_ID")
        return cls(
            data_dir=data_dir,
            token_dir=token_dir,
            intervals_api_key=os.getenv("INTERVALS_API_KEY", "").strip(),
            intervals_athlete_id=athlete_id,
            timezone=tz,
            activity_days=_positive_int("BRIDGE_ACTIVITY_LOOKBACK_DAYS", 4, 30),
            wellness_days=_positive_int("BRIDGE_WELLNESS_LOOKBACK_DAYS", 3, 30),
            wellness_refresh_hours=_positive_int("BRIDGE_WELLNESS_REFRESH_HOURS", 4, 72),
            garmin_request_delay=delay,
            wellness_profile=profile,
            stale_hours=_positive_int("BRIDGE_STALE_HOURS", 24, 24 * 30),
            timezone_source=tz_source,
            imready4=_flag("BRIDGE_IMREADY4_ENABLED"),
            hrvtrend=_flag("BRIDGE_HRVTREND_ENABLED"),
            env_file=settings_file.resolve() if settings_file is not None else None,
            log_file=_path("BRIDGE_LOG_FILE", None, base),
            update_check=_flag("BRIDGE_UPDATE_CHECK", True),
            intervals_notes=_flag("BRIDGE_INTERVALS_NOTES", True),
            ui_port=_port("BRIDGE_UI_PORT"),
            ui_host=os.getenv("BRIDGE_UI_HOST", "").strip() or (
                "0.0.0.0" if os.getenv("BRIDGE_IN_CONTAINER") else "127.0.0.1"),
            alert_email=_text("BRIDGE_ALERT_EMAIL"),
            smtp_host=_text("BRIDGE_SMTP_HOST"),
            smtp_port=_port("BRIDGE_SMTP_PORT"),
            smtp_user=_text("BRIDGE_SMTP_USER"),
            smtp_password=os.getenv("BRIDGE_SMTP_PASSWORD") or None,
            smtp_from=_text("BRIDGE_SMTP_FROM"),
            ntfy_url=_text("BRIDGE_NTFY_URL"),
            ntfy_token=_text("BRIDGE_NTFY_TOKEN"),
            poll_seconds=_positive_int("BRIDGE_POLL_SECONDS", 60, 24 * 3600) if _text("BRIDGE_POLL_SECONDS") else None,
            poll_hours=poll_hours(os.getenv("BRIDGE_POLL_HOURS", "")),
            poll_quiet_seconds=(_positive_int("BRIDGE_POLL_QUIET_SECONDS", 60, 24 * 3600)
                                if _text("BRIDGE_POLL_QUIET_SECONDS") else None),
            poll_mode=_poll_mode(),
            mode=_mode(),
            backup_hours=_positive_int("BRIDGE_BACKUP_HOURS", 12, 168),
            swap=_flag("BRIDGE_SWAP"),
            swap_method=_swap_method(),
            swap_hours=_positive_int("BRIDGE_SWAP_HOURS", 48, 24 * 30),
        )

    def ensure_data_dir(self) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        except PermissionError:
            pass                                       # reported just below, in words
        if not os.access(self.data_dir, os.W_OK):
            uid = os.getuid() if hasattr(os, "getuid") else None
            raise PermissionError(
                f"The data folder {self.data_dir} is not writable for the bridge"
                + (f" (user id {uid})" if uid is not None else "") + ". In a container: give that folder "
                "read and write access for everyone (Synology: File Station, Properties, Permission) or "
                f"`chown -R {uid if uid is not None else 10001} <folder>` on the host")
        try:
            self.data_dir.chmod(0o700)
        except OSError:
            pass
