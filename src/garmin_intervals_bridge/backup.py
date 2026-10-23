"""Backup mode (BRIDGE_MODE=backup, `run --mode backup`, `backup`): your Garmin Connect data
on your own disk – no Intervals.icu needed, nothing written anywhere but the data folder.

What it keeps, in the data folder:
  fits/<id>.fit                  every activity's original recording, as the device made it
  raw/activities/<id>.json       Garmin's summary of it; <id>.extras.json weather, gear,
                                 exercise sets, Garmin's own splits
  raw/<day>.json                 each day's wellness: the full answers of every Garmin endpoint
                                 (sleep, HRV, stress, Body Battery, readiness, scores …)
  wellness-files/<day>.zip       the watch's original wellness files (monitoring, sleep, HRV,
                                 Health Snapshots), as Garmin hands them out; <day>.json lists them
  raw/account/<date>.json        weekly: profile, devices, zones, records, badges, workouts,
                                 plans, calendar

When, kindly to Garmin: a run every BRIDGE_BACKUP_HOURS (12) – new activities (one list
request, a download per new one); a day's wellness once it is final – at the first run after
noon of the next day – and once more three days later for a watch that synced late (about
60 requests per day); the account once a week.
`backup --from DATE` fills in the past, resumably.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from typing import Any

from . import tuning
from .garmin import GarminBlocked

_t = tuning.get

log = logging.getLogger(__name__)

# a day counts as final at noon of the next day (the watch synced)
FINAL_HOUR = tuning.default("BRIDGE_BACKUP_FINAL_HOUR")
# read once more this many days after the day: late syncs
LATE_READ_DAYS = tuning.default("BRIDGE_BACKUP_LATE_READ_DAYS")
RECENT_ACTIVITY_DAYS = tuning.default("BRIDGE_BACKUP_RECENT_ACTIVITY_DAYS")
RECENT_WELLNESS_DAYS = tuning.default("BRIDGE_BACKUP_RECENT_WELLNESS_DAYS")
ACCOUNT_EVERY_DAYS = tuning.default("BRIDGE_BACKUP_ACCOUNT_DAYS")
READS_KEY = "backup:reads:"   # + day: "<final reads>@<time of the last one>"
# a read with passing endpoint failures counts after this many tries
FINAL_TRIES = tuning.default("BRIDGE_FINAL_TRIES")
ACCOUNT_KEY = "backup:account"


def _due(day: date, now: datetime, reads: int, last_read: float | None = None) -> bool:
    """The first final read at noon of the next day, the second LATE_READ_DAYS later – unless the
    first one already came that late (a fresh setup reads the last days once, and that is final)."""
    first = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=now.tzinfo).replace(
        hour=_t("BRIDGE_BACKUP_FINAL_HOUR"))
    second = first + timedelta(days=_t("BRIDGE_BACKUP_LATE_READ_DAYS") - 1)
    if reads == 0:
        return now >= first
    return reads == 1 and now >= second and (last_read is None or last_read < second.timestamp())


def _activities(garmin: Any, store: Any, start: date, end: date, metrics: dict, pause: float = 0.0) -> None:
    from .sync import archive_activity
    for activity in sorted(garmin.activities(start, end), key=lambda a: str(a.get("startTimeGMT") or "")):
        gid = str(activity.get("activityId") or "")
        if not gid.isdecimal() or activity.get("manualActivity") is True:
            continue
        try:
            state = archive_activity(gid, activity, garmin, store)
        except GarminBlocked:
            raise
        except Exception as exc:
            metrics["failed"] += 1
            log.warning("Backup of activity %s failed (%s); next run tries again", gid, type(exc).__name__)
            continue
        if state != "on_disk":
            metrics["activities"] += state == "archived"
            log.info("Backed up activity %s (%s)", gid, str(activity.get("startTimeLocal") or "")[:16])
            if pause:
                time.sleep(pause)


def _wellness_day(day: date, garmin: Any, store: Any, metrics: dict, endpoints: tuple[str, ...] | None,
                  files: bool) -> list[str]:
    """Read a day into the archive (an endpoint that fails keeps what the archive had); the
    endpoints that failed for a passing reason."""
    from .sync import _merge_partial, transient_errors
    fresh = garmin.snapshot(day, **({"endpoints": endpoints} if endpoints else {}))
    store.save_snapshot(day, _merge_partial(store.load_snapshot(day), fresh))
    store.mark_wellness(day)
    metrics["wellness_days"] += 1
    if files:
        _files(day, garmin, store, metrics)
    return transient_errors(fresh)


def _files(day: date, garmin: Any, store: Any, metrics: dict) -> None:
    """The device's original files of the day, when missing (or "none" came too early)."""
    from .sync import files_missing, mirror_wellness_files
    if not files_missing(store, day):
        return
    try:
        metrics["file_days"] += mirror_wellness_files(day, garmin, store) == "saved"
    except GarminBlocked:
        raise
    except Exception as exc:
        metrics["failed"] += 1
        log.warning("Wellness files of %s not backed up (%s); next run tries again", day, type(exc).__name__)


def _account(garmin: Any, store: Any, today: date, metrics: dict, *, history_start: date | None = None,
             force: bool = False) -> None:
    taken = store.meta_updated(ACCOUNT_KEY)
    if not force and taken is not None and time.time() - taken < _t("BRIDGE_BACKUP_ACCOUNT_DAYS") * 86400:
        return
    store.save_account_snapshot(garmin.account_snapshot(history_start=history_start, today=today))
    store.set_meta(ACCOUNT_KEY)
    metrics["account"] = 1
    log.info("Backed up the account (profile, devices, zones, records, workouts …)")


def new_metrics() -> dict:
    return {"activities": 0, "wellness_days": 0, "file_days": 0, "account": 0, "failed": 0}


def backup_once(settings: Any, garmin: Any, store: Any, *, now: datetime | None = None) -> dict:
    """What every run of backup mode does: recent activities, final wellness days, the account weekly."""
    now = now or datetime.now(settings.timezone)
    today = now.date()
    metrics = new_metrics()
    _activities(garmin, store, today - timedelta(days=_t("BRIDGE_BACKUP_RECENT_ACTIVITY_DAYS")), today, metrics)
    for offset in range(_t("BRIDGE_BACKUP_RECENT_WELLNESS_DAYS"), 0, -1):
        day = today - timedelta(days=offset)
        key = READS_KEY + day.isoformat()
        count, _, when = (store.get_meta(key) or "0").partition("@")
        reads, last_read = int(count or 0), (float(when) if when else None)
        if not _due(day, now, reads, last_read):
            if reads:                                   # read already: only files that are still missing
                _files(day, garmin, store, metrics)
            continue
        try:
            passing = _wellness_day(day, garmin, store, metrics, None, files=True)
        except GarminBlocked:
            raise
        except Exception as exc:
            metrics["failed"] += 1
            log.warning("Backup of wellness %s failed (%s); next run tries again", day, type(exc).__name__)
            continue
        tries_key = f"{key}:tries"
        tries = int(store.get_meta(tries_key) or 0) + 1
        if passing and tries < _t("BRIDGE_FINAL_TRIES"):             # what failed this time comes with the next run
            store.set_meta(tries_key, str(tries))
            metrics["failed"] += 1
            log.info("Backup of wellness %s: %s failed this time; next run reads again", day, ", ".join(passing))
            continue
        store.set_meta(tries_key, "0")
        store.set_meta(key, f"{reads + 1}@{now.timestamp():.0f}")
    _account(garmin, store, today, metrics)
    return metrics


def backup_range(settings: Any, garmin: Any, store: Any, *, start: date, end: date, scopes: set[str],
                 endpoints: tuple[str, ...] | None = None, pause: float = 2.0, force: bool = False) -> dict:
    """The past: activities, wellness (days already on disk are skipped unless `force`), the account."""
    metrics = new_metrics()
    today = datetime.now(settings.timezone).date()
    if "activities" in scopes:
        _activities(garmin, store, start, min(end, today), metrics, pause=pause)
    if "wellness" in scopes:
        day, last = start, min(end, today - timedelta(days=1))      # wellness: only finished days
        while day <= last:
            if force or store.load_snapshot(day) is None or endpoints:
                try:
                    _wellness_day(day, garmin, store, metrics, endpoints, files=True)
                    store.set_meta(READS_KEY + day.isoformat(), f"2@{time.time():.0f}")   # the past is final
                except GarminBlocked:
                    raise
                except Exception as exc:
                    metrics["failed"] += 1
                    log.warning("Backup of wellness %s failed (%s)", day, type(exc).__name__)
                if pause:
                    time.sleep(pause)
                if metrics["wellness_days"] and metrics["wellness_days"] % 30 == 0:
                    log.info("Backup: %d days of wellness so far (at %s)", metrics["wellness_days"], day)
            day += timedelta(days=1)
    if "account" in scopes:
        _account(garmin, store, today, metrics, history_start=start, force=True)
    return metrics


def describe(metrics: dict) -> str:
    return (f"{metrics['activities']} activities, {metrics['wellness_days']} days of wellness "
            f"({metrics['file_days']} with the watch's files)"
            + (", the account" if metrics.get("account") else "")
            + (f"; {metrics['failed']} failed, tried again later" if metrics.get("failed") else ""))
