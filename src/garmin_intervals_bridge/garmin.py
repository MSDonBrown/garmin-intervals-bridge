"""Garmin Connect private API adapter. No private API assumptions leak to core."""
from __future__ import annotations

import getpass
import logging
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .fit import extract_original_fit

log = logging.getLogger(__name__)


class GarminBlocked(RuntimeError):
    """Garmin refused the session or rate-limited us.

    This is a property of the *run*, not of one activity: the caller must stop
    the whole run and keep local state untouched instead of marking the
    current activity as failed and hammering the next one.
    """


class GarminLoginNeeded(GarminBlocked):
    """Tokens are missing or rejected: a person has to run `login`. Ends the run like a block."""


# How many day endpoints may fail in a row before the day is treated as a Garmin outage.
MAX_CONSECUTIVE_ENDPOINT_ERRORS = 3

_TRANSIENT_NAMES = re.compile(r"Timeout|RemoteDisconnected|ProtocolError|MaxRetry|ConnectionReset")
_TRANSIENT_TEXT = re.compile(r"timed out|Connection (aborted|reset|refused)|Max retries exceeded")
_BLOCK_TEXT = re.compile(r"(?<!\d)(401|429)(?!\d)")        # a status code, not digits inside an id
# "API Error 404", "429 Client Error", "status 503": a status code in the message, not a port
# number or an id (the library's connection errors mention "port=443").
_STATUS_TEXT = re.compile(r"(?:API Error|[Ss]tatus(?: code)?:?|HTTP)\s*([45]\d\d)(?!\d)"
                          r"|(?<![\w=:])([45]\d\d) (?:Client|Server) Error")


def _chain(exc: BaseException):
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def _is_blocked(exc: BaseException) -> bool:
    """Rate-limited or session rejected: the whole run must stop."""
    return any("TooManyRequests" in type(e).__name__ or "Authentication" in type(e).__name__
               or _BLOCK_TEXT.search(str(e)) for e in _chain(exc))


def _status_code(exc: BaseException) -> int | None:
    """The HTTP status behind an exception, from the response the library keeps or from its text."""
    for e in _chain(exc):
        for candidate in (getattr(e, "status_code", None), getattr(getattr(e, "response", None), "status_code", None)):
            if isinstance(candidate, int):
                return candidate
    m = _STATUS_TEXT.search(str(exc))
    return int(m.group(1) or m.group(2)) if m else None


def _is_transient(exc: BaseException) -> bool:
    """Garmin unreachable or failing (5xx, timeouts, connection errors), nothing wrong with our
    session. A 4xx is an answer, not an outage: an endpoint that did not exist for an old date
    says 404 and the day goes on. The library wraps every HTTP failure in one class, so the
    status code decides where there is one."""
    code = _status_code(exc)
    if code is not None:
        return code >= 500
    return any((not type(e).__name__.startswith("GarminConnect") and _TRANSIENT_NAMES.search(type(e).__name__))
               or _TRANSIENT_TEXT.search(str(e)) for e in _chain(exc))


# Source snapshot: all available fields are retained in a *local* JSON archive.
# Only confirmed scalar metrics are mapped into Intervals wellness.
DAY_ENDPOINTS = {
    "stats": "get_stats",
    "sleep": "get_sleep_data",
    "hrv": "get_hrv_data",
    "body_battery": "get_body_battery",
    # get_morning_training_readiness fetches the same URL as get_training_readiness and picks
    # the morning entry; the mapping does that itself, so one request serves both.
    "training_readiness": "get_training_readiness",
    "training_status": "get_training_status",
    "endurance_score": "get_endurance_score",
    "hill_score": "get_hill_score",
    "max_metrics": "get_max_metrics",
    "respiration": "get_respiration_data",
    "spo2": "get_spo2_data",
    "stress": "get_stress_data",
    "intensity_minutes": "get_intensity_minutes_data",
    "hydration": "get_hydration_data",
    "body_composition": "get_body_composition",
    "weigh_ins": "get_daily_weigh_ins",
    "fitness_age": "get_fitnessage_data",
    "race_predictions": "get_race_predictions",
    "lifestyle": "get_lifestyle_logging_data",
    "blood_pressure": "get_blood_pressure",
    "lactate_threshold": "get_lactate_threshold",
    "nutrition": "get_nutrition_daily_food_log",   # logged food: calories, carbs, fat, protein
    # Intraday series and events: nothing of these is mapped to Intervals today; they are kept in
    # the local archive so that the past can be worked on later without asking Garmin again.
    "heart_rates": "get_heart_rates",              # all-day heart rate, two-minute values
    "steps_intraday": "get_steps_data",            # steps per 15 minutes
    "floors": "get_floors",                        # floors climbed/descended per 15 minutes
    "body_battery_events": "get_body_battery_events",
    "all_day_events": "get_all_day_events",
    "training_load_balance": "get_training_four_week_load_balance",
    "rhr": "get_rhr_day",                           # resting HR of the day with its 7-day course
    "daily_training_status": "get_daily_training_status",   # productive / maintaining ... per day
}

# Not a time series: a snapshot of the account, taken on demand (`snapshot-account`).
ACCOUNT_ENDPOINTS = {
    "user_profile": "get_user_profile",
    "user_settings": "get_userprofile_settings",
    "devices": "get_devices",
    "heart_rate_zones": "get_heart_rate_zones",
    "power_zones": "get_power_zones",
    "personal_records": "get_personal_record",
    "earned_badges": "get_earned_badges",
    "workouts": "get_workouts",
    "training_plans": "get_training_plans",
    "cycling_ftp": "get_cycling_ftp",
    "activity_types": "get_activity_types",
}
# Garmin series kept with the account snapshot: (key, method, positional args after the range, keywords)
ACCOUNT_RANGES = (
    ("ftp_history_cycling", "get_functional_threshold_power_range", (), {"sport": "CYCLING", "aggregation": "daily"}),
    ("ftp_history_running", "get_functional_threshold_power_range", (), {"sport": "RUNNING", "aggregation": "daily"}),
    ("running_tolerance", "get_running_tolerance", ("daily",), {}),
)
RANGE_CHUNK_DAYS = 180
# What Garmin knows about an activity beyond the recording. Sets only exist for some types.
EXTRA_KEYS = ("weather", "gear", "exercise_sets", "typed_splits", "split_summaries")

# Activity types whose Garmin summary carries exercise sets (names, reps, weights).
SET_ACTIVITY_TYPES = ("strength_training", "hiit", "indoor_cardio", "cardio_training", "yoga", "pilates")


def parse_endpoints(text: str | None) -> tuple[str, ...] | None:
    """`--endpoints`: "standard", "essential", "all" (None: every endpoint) or a comma-separated list of keys."""
    if text is None or text == "all":
        return None
    if text == "essential":
        return ESSENTIAL_ENDPOINTS
    if text == "standard":
        return STANDARD_ENDPOINTS
    keys = tuple(k.strip() for k in text.split(",") if k.strip())
    unknown = [k for k in keys if k not in DAY_ENDPOINTS]
    if not keys or unknown:
        raise ValueError(f"Unknown endpoints {unknown}; known: {', '.join(DAY_ENDPOINTS)}")
    return keys

# For backfilling the past: the endpoints that carry per-day measurements. The
# rest (training status, scores, predictions, fitness age, lactate, blood pressure,
# lifestyle, readiness) change slowly or are not meaningful retroactively, and each
# one costs a Garmin request per day.
ESSENTIAL_ENDPOINTS = ("stats", "sleep", "hrv", "respiration", "spo2", "hydration",
                       "body_composition", "nutrition", "intensity_minutes", "stress",
                       "body_battery", "training_readiness")
# What a backfill reads by default: the measurements, plus the scores and predictions people
# chart (endurance and hill score, race predictions) – three more requests per day.
STANDARD_ENDPOINTS = ESSENTIAL_ENDPOINTS + ("endurance_score", "hill_score", "race_predictions")


class GarminSource:
    def __init__(self, token_dir: Path, delay: float = 0.5):
        self.token_dir = token_dir
        self.delay = delay
        self.client: Any | None = None
        self.requests = 0        # Garmin requests this process made; recorded per run, summed by `health`
        self._pending: Any | None = None     # a web sign-in waiting for its MFA code

    def login(self, interactive: bool = True) -> None:
        from garminconnect import Garmin, GarminConnectAuthenticationError
        self.token_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.token_dir.chmod(0o700)
        try:
            # No library retries: the bridge retries on its own schedule (timers, per-activity
            # backoff), and four attempts per endpoint would multiply every outage.
            api = Garmin(retry_attempts=0)
            api.login(str(self.token_dir))
            self.requests += 2   # the token login loads profile and settings
            self.client = api
            return
        except FileNotFoundError as exc:
            problem: BaseException = exc                       # no tokens yet
        except GarminConnectAuthenticationError as exc:
            # The library reports any failure of the first profile request as an authentication
            # error, rate limits and outages included. Those are not a reason to log in again.
            if _is_transient(exc) or any("TooManyRequests" in type(e).__name__ for e in _chain(exc)):
                raise GarminBlocked(f"Garmin unreachable or rate-limiting during login: "
                                    f"{type(exc.__cause__ or exc).__name__}") from exc
            problem = exc
        except Exception as exc:
            if _is_blocked(exc) or _is_transient(exc):
                raise GarminBlocked(f"Garmin unreachable during login: {type(exc).__name__}") from exc
            raise
        if not interactive or not sys.stdin.isatty():
            raise GarminLoginNeeded("Garmin login needed: run 'garmin-intervals-bridge login' "
                                    "in an interactive terminal") from problem
        email = input("Garmin Connect email: ").strip()
        password = getpass.getpass("Garmin Connect password: ")
        api = Garmin(email=email, password=password, retry_attempts=1,
                     prompt_mfa=lambda: input("Garmin MFA code: ").strip())
        try:
            api.login(str(self.token_dir))
            self.client = api
        finally:
            password = None

    # ---- sign-in from a web page: the password now, the MFA code in a second request ----

    def start_login(self, email: str, password: str) -> str:
        """'done' when signed in, 'mfa' when Garmin wants a code (then `finish_login`).

        Credentials only, never the saved session: this is the way to replace an expired one.
        The password goes to Garmin and is dropped; only the session tokens are saved.
        """
        from garminconnect import Garmin
        api = Garmin(email=email, password=password, retry_attempts=1, return_on_mfa=True)
        try:
            status, _ = api.login()
        except Exception as exc:
            api.password = None
            if _is_blocked(exc) or _is_transient(exc):
                raise GarminBlocked(f"Garmin refused the sign-in for now: {type(exc).__name__}") from exc
            raise
        api.password = None
        if status == "needs_mfa":
            self._pending = api
            return "mfa"
        self._save_session(api)
        return "done"

    def finish_login(self, code: str) -> None:
        api = self._pending
        if api is None:
            raise ValueError("No sign-in waiting for a code; start again")
        api.resume_login(None, code.strip())          # a wrong code keeps it waiting for another try
        self._pending = None
        self._save_session(api)

    def _save_session(self, api: Any) -> None:
        self.token_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        api.client.dump(str(self.token_dir))
        self.client = None
        self.login(interactive=False)                 # load it back the way every run does

    def _client(self) -> Any:
        if self.client is None:
            # Lazy: a watch run that finds nothing new never touches Garmin at all.
            # Loading the token store costs two profile requests, so it is deferred
            # to the first real call instead of being paid on every poll.
            self.login(interactive=False)
        return self.client

    def _call(self, method: str, *args, **kwargs) -> Any:
        """One Garmin request. A block (429, rejected session) ends the whole run instead of
        being booked against whatever activity or day happened to be in progress."""
        fn = getattr(self._client(), method)
        self.requests += 1
        try:
            return fn(*args, **kwargs)
        except GarminBlocked:
            raise
        except Exception as exc:
            if _is_blocked(exc):
                raise GarminBlocked(f"Garmin API blocked request: {type(exc).__name__}") from exc
            raise
        finally:
            time.sleep(self.delay)

    def activities(self, start: date, end: date) -> list[dict]:
        if (end - start).days <= 14:
            # One request: the newest 100 activities cover a short window. The by-date
            # endpoint pages 20 at a time and always needs a second, empty page.
            result = self._call("get_activities", 0, 100)
            lo, hi = start.isoformat(), end.isoformat()
            if isinstance(result, list):
                days = [str(x.get("startTimeLocal") or x.get("startTimeGMT") or "")[:10]
                        for x in result if isinstance(x, dict)]
                if len(result) == 100 and days and min(days) >= lo:      # the first day may hold more
                    result = self._call("get_activities_by_date", lo, hi)   # busier than 100 in 14 days
                else:
                    result = [x for x in result if isinstance(x, dict)
                              and lo <= str(x.get("startTimeLocal") or x.get("startTimeGMT") or "")[:10] <= hi]
        else:
            result = self._call("get_activities_by_date", start.isoformat(), end.isoformat())
        if not isinstance(result, list):
            raise ValueError("Unexpected Garmin activity response")
        return [x for x in result if isinstance(x, dict) and x.get("activityId") is not None]

    def activity(self, activity_id: int | str) -> dict:
        result = self._call("get_activity", str(activity_id))
        if not isinstance(result, dict) or result.get("activityId") is None:
            raise ValueError("Unexpected Garmin activity response")
        # get_activity() nests the start time differently from the list endpoint.
        if "startTimeGMT" not in result and isinstance(result.get("summaryDTO"), dict):
            result["startTimeGMT"] = result["summaryDTO"].get("startTimeGMT")
            result.setdefault("duration", result["summaryDTO"].get("duration"))
        return result

    def activity_extras(self, activity: dict, keys: tuple[str, ...] | None = None) -> dict:
        """What Garmin knows about an activity beyond the recording: weather, gear, exercise
        sets, and its own splits (run/walk/stand, climbs, intervals; computed on Garmin's side).
        `keys` limits the reads to what an older archive entry still lacks."""
        gid = str(activity.get("activityId"))
        wanted = keys or EXTRA_KEYS
        out: dict = {key: None for key in wanted}
        out["errors"] = {}
        # the list endpoint says activityType, the detail endpoint activityTypeDTO
        kind = activity.get("activityType") or activity.get("activityTypeDTO") or {}
        type_key = str(kind.get("typeKey") or "") if isinstance(kind, dict) else ""
        methods = {"weather": "get_activity_weather", "gear": "get_activity_gear",
                   "exercise_sets": "get_activity_exercise_sets", "typed_splits": "get_activity_typed_splits",
                   "split_summaries": "get_activity_split_summaries"}
        calls = [(key, methods[key]) for key in wanted
                 if key != "exercise_sets" or any(t in type_key for t in SET_ACTIVITY_TYPES)]
        for key, method in calls:
            try:
                out[key] = self._call(method, gid)
            except GarminBlocked:
                raise
            except Exception as exc:
                out["errors"][key] = type(exc).__name__
        return out

    def wellness_files(self, day: date) -> bytes | None:
        """The device's original wellness files of one day (monitoring, sleep, HRV, skin
        temperature, Health Snapshots ...) as Garmin's ZIP; None when Garmin has none."""
        try:
            return self._call("download_health_snapshot", day.isoformat())
        except GarminBlocked:
            raise
        except Exception as exc:
            if _status_code(exc) == 404:
                return None
            raise

    def _account_extras(self, raw: dict, today: date, history_start: date | None) -> None:
        """Goals, gear defaults, plan details, the calendar and Garmin's own series (FTP, running
        tolerance): the recent weeks on every snapshot, everything since `history_start` once."""
        def keep(key: str, call) -> Any:
            try:
                return call()
            except GarminBlocked:
                raise
            except Exception as exc:
                raw["errors"][key] = type(exc).__name__
                return None

        data = raw["data"]
        data["goals"] = {status: keep(f"goals:{status}", lambda st=status: self._call("get_goals", st))
                         for status in ("active", "future", "past")}
        number = getattr(self._client(), "profile_id", None) or (data.get("user_profile") or {}).get("id")
        if number:
            data["gear_defaults"] = keep("gear_defaults", lambda: self._call("get_gear_defaults", str(number)))
        plans = (data.get("training_plans") or {}).get("trainingPlanList") if isinstance(
            data.get("training_plans"), dict) else None
        for plan in plans if isinstance(plans, list) else []:
            plan_id = str((plan or {}).get("trainingPlanId") or "")
            if plan_id.isdecimal():
                method = ("get_adaptive_training_plan_by_id" if plan.get("trainingPlanCategory") == "FBT_ADAPTIVE"
                          else "get_training_plan_by_id")
                data.setdefault("training_plan_details", {})[plan_id] = keep(
                    f"training_plan:{plan_id}", lambda m=method, i=plan_id: self._call(m, i))
        start = history_start or today - timedelta(days=62)
        month = date(start.year, start.month, 1)
        last = date(today.year + (today.month == 12), today.month % 12 + 1, 1)      # through next month
        while month <= last:
            label = f"{month.year}-{month.month:02d}"
            data.setdefault("scheduled_workouts", {})[label] = keep(
                f"scheduled_workouts:{label}", lambda y=month.year, mo=month.month: self._call(
                    "get_scheduled_workouts", y, mo))
            month = date(month.year + (month.month == 12), month.month % 12 + 1, 1)
        for key, method, args, kwargs in ACCOUNT_RANGES:
            chunks = []
            lo = start
            while lo <= today:
                hi = min(lo + timedelta(days=RANGE_CHUNK_DAYS - 1), today)
                chunks.append({"start": lo.isoformat(), "end": hi.isoformat(), "data": keep(
                    f"{key}:{lo}", lambda a=lo, b=hi: self._call(method, a.isoformat(), b.isoformat(), *args,
                                                                  **kwargs))})
                lo = hi + timedelta(days=1)
            data[key] = chunks

    def account_snapshot(self, history_start: date | None = None, today: date | None = None) -> dict:
        """One read of everything about the account that is not a time series, plus the
        recent weeks of Garmin's account-level series; `history_start` reaches further back."""
        from datetime import datetime, timezone
        raw: dict = {"taken": datetime.now(timezone.utc).isoformat(timespec="seconds"), "data": {}, "errors": {}}
        if history_start:
            raw["history_start"] = history_start.isoformat()
        for key, method in ACCOUNT_ENDPOINTS.items():
            try:
                raw["data"][key] = self._call(method)
            except GarminBlocked:
                raise
            except Exception as exc:
                raw["errors"][key] = type(exc).__name__
        devices = raw["data"].get("devices")
        for dev in devices if isinstance(devices, list) else []:
            dev_id = str((dev or {}).get("deviceId") or "")
            if dev_id.isdecimal():
                try:
                    raw["data"].setdefault("device_settings", {})[dev_id] = self._call("get_device_settings", dev_id)
                except GarminBlocked:
                    raise
                except Exception as exc:
                    raw["errors"][f"device_settings:{dev_id}"] = type(exc).__name__
        client = self._client()
        number = getattr(client, "profile_id", None) or (raw["data"].get("user_profile") or {}).get("id")
        if number:
            try:
                raw["data"]["gear"] = self._call("get_gear", str(number))
                for g in raw["data"]["gear"] if isinstance(raw["data"]["gear"], list) else []:
                    uuid = str((g or {}).get("uuid") or "")
                    if uuid:
                        raw["data"].setdefault("gear_stats", {})[uuid] = self._call("get_gear_stats", uuid)
            except GarminBlocked:
                raise
            except Exception as exc:
                raw["errors"]["gear"] = type(exc).__name__
        self._account_extras(raw, today or datetime.now().date(), history_start)
        return raw

    def original_fit(self, activity_id: int | str) -> bytes:
        api = self._client()
        raw = self._call("download_activity", str(activity_id), dl_fmt=api.ActivityDownloadFormat.ORIGINAL)
        return extract_original_fit(raw)

    def snapshot(self, day: date, endpoints: tuple[str, ...] | None = None) -> dict:
        """Fetch the day endpoints (all, or a named subset), preserving raw responses locally.

        Individual unavailable endpoints are recorded as errors; 429/auth failures
        abort the day to avoid hammering Garmin or masking an expired session, and so
        does a run of consecutive failures, which means Garmin itself is down.
        """
        raw: dict = {"date": day.isoformat(), "data": {}, "errors": {}}
        consecutive = 0
        for key, method in DAY_ENDPOINTS.items():
            if endpoints is not None and key not in endpoints:
                continue
            if not hasattr(self._client(), method):
                raw["errors"][key] = "MethodNotAvailable"
                continue
            try:
                if key == "race_predictions":
                    result = self._call(method, startdate=day.isoformat(), enddate=day.isoformat(), _type="daily")
                elif key == "blood_pressure":
                    result = self._call(method, day.isoformat(), day.isoformat())
                elif key == "lactate_threshold":
                    result = self._call(method, latest=False, start_date=day.isoformat(),
                                        end_date=day.isoformat(), aggregation="daily")
                else:
                    result = self._call(method, day.isoformat())
                raw["data"][key] = result
                consecutive = 0
            except GarminBlocked:
                raise
            except Exception as exc:
                name = type(exc).__name__
                code = _status_code(exc)
                raw["errors"][key] = f"{name} {code}" if code else name
                log.warning("Garmin endpoint %s unavailable: %s%s", key, name, f" ({code})" if code else "")
                if not _is_transient(exc):
                    continue                  # a 4xx answer for this endpoint and date; not an outage
                consecutive += 1
                if consecutive >= MAX_CONSECUTIVE_ENDPOINT_ERRORS:
                    raise GarminBlocked(f"{consecutive} Garmin endpoints failed in a row on {day} "
                                        f"({name}); Garmin looks unavailable") from exc
        return raw
