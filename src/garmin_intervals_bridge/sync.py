"""Fail-closed Garmin -> Intervals sync orchestration."""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

from . import hrvtrend, imready4
from .config import Settings
from .enrich import enrich_activity, load_field_mappings
from .fit import sha256, validate_fit, wellness_bundle_index
from .garmin import ESSENTIAL_ENDPOINTS, EXTRA_KEYS, GarminBlocked
from .intake import intake_values, write_intake
from .intervals import rate_limited, refused
from .mapping import PROVISIONAL_CODES, map_wellness, merge_wellness, snapshot_corrections
from .store import InstanceBusy, Store
from .times import external_id_names, parse_utc

log = logging.getLogger(__name__)


def _error_name(exc: BaseException) -> str:
    """The error's type and, when it has one, the HTTP status: a log line that says what happened."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status is None:
        found = re.search(r"\b(?:API Error|HTTP) (\d{3})\b", str(exc))
        status = found.group(1) if found else None
    return f"{type(exc).__name__} (HTTP {status})" if status else type(exc).__name__


_time = parse_utc


def activity_match(garmin: dict, remote: list[dict], tolerance_seconds: int = 180) -> dict | None:
    """Conservative safeguard: a remote activity starting near Garmin's UTC start
    blocks automatic upload even if metadata are missing or sport differs.
    """
    gid = str(garmin.get("activityId", ""))
    gt = _time(garmin.get("startTimeGMT"), garmin_gmt=True)
    if gt is None:
        # No reliable timestamp means we cannot prove absence of duplicates.
        raise ValueError("Garmin activity lacks startTimeGMT; refusing automatic upload")
    for item in remote:
        if not isinstance(item, dict):
            continue
        external = str(item.get("external_id") or "")
        if external_id_names(external, gid):
            return item
        it = _time(item.get("start_date"), garmin_gmt=True)
        if not it and not external:
            # Unknown start data => absence of duplicates cannot be established.
            raise ValueError("Intervals returned an activity without usable UTC start/external ID")
        if it and abs((it - gt).total_seconds()) <= tolerance_seconds:
            return item
    return None


def _garmin_day(garmin: dict) -> date:
    ts = _time(garmin.get("startTimeGMT"), garmin_gmt=True)
    if ts is None:
        raise ValueError("Activity missing startTimeGMT")
    return ts.date()


ARCHIVE_SETTLE_DAYS = 2     # Garmin attaches weather and gear to a fresh activity a little after the upload


def _archived_extras(store: Store, gid: str) -> dict | None:
    path = store.activity_extras_path(gid)
    if not path.is_file():
        return None
    try:
        obj = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def archive_activity(gid: str, activity: dict, garmin: Any, store: Store, *, now: datetime | None = None) -> str:
    """Complete the local mirror of one activity: the recording itself, Garmin's own
    summary of it (names what the FIT only numbers) and what is not in the file at
    all (weather, gear, exercise sets, Garmin's own splits).

    Returns `on_disk`, `archived` or `pending`: extras that came back incomplete
    for a fresh activity are not kept, the next pass asks again. Older activities
    keep whatever Garmin still has. An archive entry from an earlier version gets
    only the extras it lacks.
    """
    path, meta = store.fit_path(gid), store.activity_json_path(gid)
    old = _archived_extras(store, gid)
    missing = tuple(k for k in EXTRA_KEYS if old is None or k not in old)
    if path.is_file() and meta.is_file() and not missing:
        return "on_disk"
    if not path.is_file():
        store.atomic_save(path, garmin.original_fit(gid))
    if not meta.is_file():
        store.save_activity_json(gid, garmin.activity(gid))
    if missing:
        found = garmin.activity_extras(activity, keys=None if old is None else missing)
        started = _time(activity.get("startTimeGMT"), garmin_gmt=True)
        current = now or datetime.now(timezone.utc)
        fresh = started is not None and current - started < timedelta(days=ARCHIVE_SETTLE_DAYS)
        if found.get("errors") and fresh:
            log.info("Extras of %s not complete yet (%s); asking again next run",
                     gid, ", ".join(sorted(found["errors"])))
            return "pending"
        if old is not None:
            found = {**old, **found, "errors": {**(old.get("errors") or {}), **found.get("errors", {})}}
        store.save_activity_extras(gid, found)
    return "archived"


INTAKE_SETTLE_HOURS = 12      # drinks and gear are logged after the ride: Garmin is asked again once, later


def complete_intake(gid: str, activity: dict, intervals_id: str | None, garmin: Any, intervals: Any, store: Store,
                    metrics: dict, *, apply: bool, now: datetime | None = None) -> None:
    """Fluid and food logged for an activity, into its Intervals fields, once the athlete had time to log.

    The summary archived with the activity is reused when it was fetched late enough;
    otherwise it is asked for once more (one request), and the archive keeps the newer one.
    The same goes for its gear, which many pick in the app after the run (one more request):
    the archive gets the newer answer, and the gear sync takes it from there.
    Dry runs ask nothing: the first real run does it.
    """
    key = f"intake:{gid}"
    if not apply or not intervals_id or store.get_meta(key) is not None:
        return
    started = _time(activity.get("startTimeGMT"), garmin_gmt=True)
    current = now or datetime.now(timezone.utc)
    settled = started is not None and current - started >= timedelta(hours=INTAKE_SETTLE_HOURS)
    if not settled:
        return
    path = store.activity_json_path(gid)
    summary = None
    if path.is_file() and path.stat().st_mtime >= started.timestamp() + INTAKE_SETTLE_HOURS * 3600:
        try:
            summary = json.loads(path.read_text())
        except (OSError, ValueError):
            summary = None
    if summary is None:
        summary = garmin.activity(gid)
        store.save_activity_json(gid, summary)
    extras = store.activity_extras_path(gid)
    if extras.is_file() and extras.stat().st_mtime < started.timestamp() + INTAKE_SETTLE_HOURS * 3600:
        _refresh_gear(gid, activity, garmin, store)
    values = intake_values(summary)
    written = write_intake(intervals_id, values, intervals, apply=apply)
    if written:
        metrics["intake_written"] = metrics.get("intake_written", 0) + 1
        log.info("Intake of %s -> %s: %s", gid, intervals_id, written)
    store.set_meta(key, json.dumps(values, sort_keys=True))


def restore_intake(gid: str, intervals_id: str, intervals: Any, store: Store, *, apply: bool) -> dict:
    """The logged intake again, from the archived summary (no Garmin request): Intervals'
    "Reprocess file" empties fields the file does not fill. Only empty fields are written."""
    path = store.activity_json_path(gid)
    try:
        summary = json.loads(path.read_text()) if path.is_file() else None
    except (OSError, ValueError):
        summary = None
    return write_intake(intervals_id, intake_values(summary), intervals, apply=apply) if summary else {}


def _refresh_gear(gid: str, activity: dict, garmin: Any, store: Store) -> None:
    """The archived extras with Garmin's current gear of the activity; an error keeps the old answer."""
    found = garmin.activity_extras(activity, keys=("gear",))
    if "gear" in found.get("errors", {}):
        return
    try:
        archived = json.loads(store.activity_extras_path(gid).read_text())
    except (OSError, ValueError):
        return
    if archived.get("gear") != found["gear"]:
        log.info("Gear of %s changed after the activity (picked later in the app)", gid)
    errors = {k: v for k, v in (archived.get("errors") or {}).items() if k != "gear"}
    store.save_activity_extras(gid, {**archived, "gear": found["gear"], "errors": errors})


def _complete_intake(gid: str, activity: dict, intervals_id: str | None, garmin: Any, intervals: Any, store: Store,
                     metrics: dict, *, apply: bool) -> None:
    """Like _complete_archive: a failure here is logged and tried next run, never held against the activity."""
    try:
        complete_intake(gid, activity, intervals_id, garmin, intervals, store, metrics, apply=apply)
    except GarminBlocked:
        raise
    except Exception as exc:
        if rate_limited(exc) is not None:          # Intervals asked for a break: the whole run pauses
            raise
        log.warning("Intake of %s not written: %s; next run tries again", gid, _error_name(exc))


def backfill_intake(intervals: Any, store: Store, *, start: date, end: date, apply: bool) -> dict:
    """Intake fields for past activities from the archived Garmin summaries: no Garmin request.

    The Intervals activity is the one the bridge enriched or uploaded, else the one whose
    external id names the Garmin activity (a bare number only from the official sync).
    """
    found = []
    for path in sorted((store.base / "raw" / "activities").glob("*.json")):
        gid = path.stem
        if path.name.endswith(".extras.json") or not gid.isdecimal():
            continue
        try:
            summary = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        day = str((summary.get("summaryDTO") or {}).get("startTimeLocal") or "")[:10]
        if start.isoformat() <= day <= end.isoformat() and (values := intake_values(summary)):
            found.append((day, gid, values))
    metrics = {"with_intake": len(found), "written": 0, "already": 0, "unmatched": 0}
    months: dict[str, list[dict]] = {}
    for day, gid, values in found:
        intervals_id = store.activity_intervals_id(gid)
        if not intervals_id:
            month = day[:7]
            if month not in months:
                first = date.fromisoformat(f"{month}-01")
                after = date(first.year + (first.month == 12), first.month % 12 + 1, 1)
                months[month] = intervals.activities(first - timedelta(days=1), after,
                                                     fields=["id", "external_id", "source"])
            match = next((a for a in months[month]
                          if external_id_names(str(a.get("external_id") or ""), gid)
                          and (str(a.get("source") or "").upper() == "GARMIN_CONNECT"
                               or not str(a.get("external_id") or "").isdecimal())), None)
            intervals_id = str(match["id"]) if match else None
        if not intervals_id:
            metrics["unmatched"] += 1
            continue
        written = write_intake(intervals_id, values, intervals, apply=apply)
        metrics["written" if written else "already"] += 1
        if apply:
            store.set_meta(f"intake:{gid}", json.dumps(values, sort_keys=True))
    return metrics


def files_missing(store: Store, day: date, now: datetime | None = None) -> bool:
    """No original files of this day on disk – or Garmin said "none" before a late device sync
    could have brought them (within two days of the day): worth asking once more."""
    index = store.load_wellness_index(day)
    if index is None:
        return True
    if index.get("files"):
        return False
    try:
        fetched = datetime.fromisoformat(str(index.get("fetched")))
    except ValueError:
        return False
    late = datetime.combine(day + timedelta(days=2), datetime.min.time(), tzinfo=timezone.utc)
    return fetched < late <= (now or datetime.now(timezone.utc))


def mirror_wellness_files(day: date, garmin: Any, store: Store) -> str:
    """Keep the device's original wellness files of a finished day: `on_disk`, `saved`, or
    `none` when Garmin has no files for it (recorded too; asked once more if that came early)."""
    if not files_missing(store, day):
        return "on_disk"
    data = garmin.wellness_files(day)
    fetched = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if not data:
        store.save_wellness_files(day, None, {"files": 0, "kinds": {}, "invalid": [], "health_snapshots": [],
                                              "fetched": fetched})
        return "none"
    index = {**wellness_bundle_index(data), "fetched": fetched}
    store.save_wellness_files(day, data, index)
    if index["invalid"]:
        log.warning("Wellness files of %s: %d of %d failed the FIT check; kept as delivered",
                    day, len(index["invalid"]), index["files"])
    return "saved"


def sync_wellness_files(settings: Settings, garmin: Any, store: Store, *, days: list[date],
                        pause_seconds: float = 0.0) -> dict:
    """Backfill: mirror the original wellness files of every given day not on disk yet."""
    metrics = {"days": len(days), "saved": 0, "on_disk": 0, "none": 0, "failed": 0, "health_snapshots": 0}
    for day in days:
        try:
            state = mirror_wellness_files(day, garmin, store)
        except GarminBlocked:
            raise
        except Exception as exc:
            metrics["failed"] += 1
            log.warning("Wellness files of %s not mirrored: %s", day, _error_name(exc))
            continue
        metrics[state] += 1
        if state == "saved":
            metrics["health_snapshots"] += len((store.load_wellness_index(day) or {}).get("health_snapshots") or [])
            log.info("Mirrored wellness files of %s", day)
        if state != "on_disk" and pause_seconds:
            time.sleep(pause_seconds)
    return metrics


def _complete_archive(gid: str, activity: dict, garmin: Any, store: Store, metrics: dict) -> str:
    """The regular runs keep the mirror complete for every activity they handle, so
    `--archive-only` is only ever needed for the past. A failure here is logged and
    tried again next run, never held against the activity: the enrichment or upload
    it belongs to has already succeeded.
    """
    try:
        state = archive_activity(gid, activity, garmin, store)
    except GarminBlocked:
        raise
    except Exception as exc:
        metrics["archive_failed"] += 1
        log.warning("Archiving the summary of %s failed with %s; next run tries again", gid, _error_name(exc))
        return "failed"
    if state == "archived":
        metrics["archived"] += 1
        log.info("Archived summary and extras of %s", gid)
    elif state == "pending":
        metrics["archive_pending"] += 1
    return state


def _sync_one_activity(activity: dict, gid: str, garmin: Any, intervals: Any, store: Store,
                       remote: list[dict], metrics: dict, *, apply: bool, allow_upload: bool) -> None:
    path = store.fit_path(gid)
    if not path.is_file():
        fit = garmin.original_fit(gid)
        store.atomic_save(path, fit)
        metrics["downloaded"] += 1
    else:
        try:
            validate_fit(path.read_bytes())
        except ValueError:
            fit = garmin.original_fit(gid)
            store.atomic_save(path, fit)
            metrics["downloaded"] += 1
    digest = sha256(path.read_bytes())
    status = store.activity_status(gid)
    if status in ("uploaded", "pending"):
        metrics["pending"] += int(status == "pending")
        log.info("FIT %s already tracked as %s; not reposting", gid, status)
        return
    # Always re-evaluate remote existence; old manual imports need preservation.
    duplicate = activity_match(activity, remote)
    if duplicate is not None:
        store.record_activity(gid, "remote_exists", digest, str(duplicate.get("id") or ""))
        metrics["remote_exists"] += 1
        log.info("FIT %s already present remotely; not replacing", gid)
        return
    store.record_activity(gid, "downloaded", digest)
    if not apply or not allow_upload:
        metrics["would_upload"] += 1
        log.info("DRY/SAFE FIT %s: new original ready; upload NOT performed", gid)
        return
    # Query Intervals again immediately before upload for race protection.
    nearby = intervals.nearby_activities(_garmin_day(activity))
    duplicate = activity_match(activity, nearby)
    if duplicate is not None:
        store.record_activity(gid, "remote_exists", digest, str(duplicate.get("id") or ""))
        metrics["remote_exists"] += 1
        return
    # Pending is durable: after a timeout/crash, NEVER automatically retry
    # a potentially successful upload. Manual reconciliation required.
    store.record_activity(gid, "pending", digest)
    result = intervals.upload_fit(gid, path)
    if not result.get("created"):
        # 200 means *no* new activity. Keep the pending lock; diagnose
        # what Intervals considered a duplicate before trying again.
        log.warning("Intervals did not create Garmin activity %s; manual reconciliation needed", gid)
        metrics["pending"] += 1
        return
    entries = result.get("items", [])
    imported_id = str(entries[0].get("id", "")) if entries and isinstance(entries[0], dict) else ""
    store.record_activity(gid, "uploaded", digest, imported_id)
    metrics["uploaded"] += 1
    log.info("Uploaded original FIT for Garmin ID %s", gid)


CATCH_UP_DAYS = 14              # after a pause the scheduled runs look back to the last one, at most this far
CATCH_UP_KEY = "catchup:"       # + scope: the first day a pause left to read, until a run has read it


def note_pause(settings: Settings, store: Store, today: date) -> None:
    """A computer switched on once a week, a server down for days, a Garmin sign-in that waited:
    the bridge's last run is older than its usual window. Remember, for activities and wellness,
    the first day the scheduled runs have to read again – the window as it was at the last run,
    at most CATCH_UP_DAYS back. A new install has no last run: the usual window.

    A pause is a whole day without a run: the night of a bridge that runs on (last run yesterday)
    only moves the window. Each pause is noted once – the same run asks again after its
    activities are caught up, before it is recorded itself."""
    last = max(store.last_run("sync") or 0, store.last_run("watch") or 0)
    if not last:
        return
    last_day = datetime.fromtimestamp(last, settings.timezone).date()
    if (today - last_day).days < 2 or store.get_meta(CATCH_UP_KEY + "from") == f"{last:.0f}":
        return
    store.set_meta(CATCH_UP_KEY + "from", f"{last:.0f}")
    for scope, base in (("activities", settings.activity_days), ("wellness", settings.wellness_days)):
        start = max(last_day - timedelta(days=base - 1), today - timedelta(days=CATCH_UP_DAYS - 1))
        if start >= today - timedelta(days=base - 1):
            continue                                  # no pause: the usual window covers it
        saved = store.get_meta(CATCH_UP_KEY + scope)
        if saved and date.fromisoformat(saved) <= start:
            continue
        store.set_meta(CATCH_UP_KEY + scope, start.isoformat())
        log.info("Back after a pause (last run %s): %s from %s on", last_day, scope, start)


def catch_up_days(store: Store, scope: str, base: int, today: date) -> int:
    """The scheduled run's lookback: `base`, or back to the first day a pause left to read."""
    saved = store.get_meta(CATCH_UP_KEY + scope)
    if not saved:
        return base
    start = max(date.fromisoformat(saved), today - timedelta(days=max(base, CATCH_UP_DAYS) - 1))
    return max(base, (today - start).days + 1)


def caught_up(store: Store, scope: str) -> None:
    if store.get_meta(CATCH_UP_KEY + scope):
        store.set_meta(CATCH_UP_KEY + scope, "")
        log.info("Caught up: %s", scope)


def sync_activities(settings: Settings, garmin: Any, intervals: Any, store: Store,
                    *, apply: bool, allow_upload: bool, activity_days: int | None = None,
                    today: date | None = None, date_range: tuple[date, date] | None = None,
                    pause_seconds: float = 0.0, catch_up: bool = False) -> dict:
    """Upload mode: every Garmin activity Intervals does not have is uploaded as the athlete's
    own file. `date_range` replaces the lookback for a backfill; `pause_seconds` paces it.
    `catch_up` (the scheduled full runs): after a pause, back to the bridge's last run."""
    if date_range:
        start, current = date_range
    else:
        current = today or datetime.now(settings.timezone).date()
        lookback = activity_days or settings.activity_days
        if catch_up:
            note_pause(settings, store, current)
            lookback = catch_up_days(store, "activities", lookback, current)
        start = current - timedelta(days=lookback - 1)
    activities = garmin.activities(start, current)
    metrics = {"seen": 0, "downloaded": 0, "remote_exists": 0, "uploaded": 0, "pending": 0,
               "would_upload": 0, "skipped": 0, "deferred": 0, "failed": 0,
               "archived": 0, "archive_pending": 0, "archive_failed": 0}
    # Never rely solely on the hash of partner-filtered and original FIT files.
    remote = intervals.activities(start - timedelta(days=1), current + timedelta(days=1))
    for activity in sorted(activities, key=lambda a: str(a.get("startTimeGMT") or "")):
        gid = str(activity["activityId"])
        if not gid.isdecimal():
            log.warning("Ignoring invalid Garmin activity ID")
            continue
        metrics["seen"] += 1
        if activity.get("manualActivity") is True:
            # Manual entries carry no device recording; Garmin's original export
            # has nothing to return for them and would fail on every run.
            if store.activity_status(gid) != "skipped":
                store.record_activity(gid, "skipped")
            metrics["skipped"] += 1
            continue
        if store.is_deferred(gid):
            metrics["deferred"] += 1
            continue
        # One broken activity must never stop the others. Errors are recorded
        # per activity with backoff; only a Garmin-wide block aborts the run.
        try:
            before = metrics["downloaded"] + metrics["uploaded"] + metrics["would_upload"]
            _sync_one_activity(activity, gid, garmin, intervals, store, remote, metrics,
                               apply=apply, allow_upload=allow_upload)
            asked = _complete_archive(gid, activity, garmin, store, metrics) != "on_disk"
            _complete_intake(gid, activity, store.activity_intervals_id(gid), garmin, intervals, store, metrics,
                             apply=apply)
            moved = metrics["downloaded"] + metrics["uploaded"] + metrics["would_upload"] > before
            if pause_seconds and (asked or moved):
                time.sleep(pause_seconds)
        except GarminBlocked:
            raise
        except Exception as exc:
            if store.activity_status(gid) == "pending" and refused(exc):
                store.record_activity(gid, "downloaded")    # Intervals refused it: nothing went in
            if rate_limited(exc) is not None:          # Intervals asked for a break: the whole run pauses
                raise
            if store.activity_status(gid) == "pending":
                # The upload request itself failed after the pending lock was taken:
                # its outcome is unknown, so it stays pending and is never retried blindly.
                metrics["pending"] += 1
                log.error("Upload of %s ended with %s; outcome unknown, left pending for manual check",
                          gid, _error_name(exc))
                continue
            attempts = store.record_failure(gid, f"{type(exc).__name__}: {exc}")
            metrics["failed"] += 1
            log.warning("Activity %s failed with %s (attempt %d); retrying later with backoff",
                        gid, _error_name(exc), attempts)
    if catch_up and not date_range and not metrics["failed"]:
        caught_up(store, "activities")                 # a failed one keeps the window: retried in it
    return metrics

def sync_enrich(settings: Settings, garmin: Any, intervals: Any, store: Store, *, apply: bool,
                activity_days: int | None = None, today: date | None = None,
                date_range: tuple[date, date] | None = None, pause_seconds: float = 0.0,
                archive_only: bool = False, scan_interval_minutes: int = 0, swap: bool = False,
                catch_up: bool = False) -> dict:
    """Scheduled enrich mode: for every recent Garmin activity, add to the officially
    synced Intervals activity what the partner copy lacks. Never uploads, never deletes.

    `date_range` replaces the lookback for a backfill; `pause_seconds` spaces the
    activities out so a long backfill stays polite towards Garmin. `archive_only`
    downloads the originals that are not on disk yet and leaves Intervals alone: a
    local mirror of every recording, for whatever is extracted from it later.

    `scan_interval_minutes` is for the scheduled runs: the watch already enriches every
    activity the moment Intervals imports it, so the scan of Garmin's list is a safety
    net (late imports, retries, new field definitions) and is skipped while the last one
    is younger than this, unless a failed activity is due for its retry.
    """
    if catch_up and not date_range and not archive_only:
        note_pause(settings, store, today or datetime.now(settings.timezone).date())
    if (scan_interval_minutes and not date_range and not archive_only
            and not store.get_meta(CATCH_UP_KEY + "activities")):     # a pause to catch up: the scan now
        last = store.meta_updated("activity_scan")
        retry_due = any(not f["next_retry"] or f["next_retry"] <= time.time() for f in store.failed_activities())
        if last and time.time() - last < scan_interval_minutes * 60 and not retry_due:
            return {"seen": 0, "skipped": 0, "deferred": 0, "failed": 0, "unmatched": 0,
                    "already_enriched": 0, "nothing_to_add": 0, "planned": 0, "enriched": 0,
                    "fields_written": 0, "streams_written": 0, "archived": 0, "on_disk": 0,
                    "archive_pending": 0, "archive_failed": 0, "scan_skipped": 1}
    if date_range:
        start, current = date_range
    else:
        current = today or datetime.now(settings.timezone).date()
        lookback = activity_days or settings.activity_days
        if catch_up and not archive_only:
            lookback = catch_up_days(store, "activities", lookback, current)
        start = current - timedelta(days=lookback - 1)
    activities = garmin.activities(start, current)
    metrics = {"seen": 0, "skipped": 0, "deferred": 0, "failed": 0, "unmatched": 0,
               "already_enriched": 0, "nothing_to_add": 0, "planned": 0, "enriched": 0,
               "fields_written": 0, "streams_written": 0, "archived": 0, "on_disk": 0,
               "archive_pending": 0, "archive_failed": 0}
    if not archive_only:
        remote = intervals.activities(start - timedelta(days=1), current + timedelta(days=1),
                                      fields=["id", "external_id", "source", "start_date", "moving_time",
                                              "elapsed_time"])
        mappings = load_field_mappings(intervals.custom_items())
    for activity in sorted(activities, key=lambda a: str(a.get("startTimeGMT") or "")):
        gid = str(activity["activityId"])
        if not gid.isdecimal():
            continue
        metrics["seen"] += 1
        if activity.get("manualActivity") is True:
            metrics["skipped"] += 1
            continue
        if store.is_deferred(gid):
            metrics["deferred"] += 1
            continue
        try:
            if archive_only:
                state = archive_activity(gid, activity, garmin, store)
                if state == "on_disk":
                    metrics["on_disk"] += 1
                    continue
                metrics["archived" if state == "archived" else "archive_pending"] += 1
                if state == "archived":
                    log.info("Archived original of %s (%s)", gid, str(activity.get("startTimeLocal") or "")[:10])
                if pause_seconds:
                    time.sleep(pause_seconds)
                store.clear_failure(gid)
                continue
            result = None
            copy = next((c for c in remote if str(c.get("source") or "").upper() == "GARMIN_CONNECT"
                         and str(c.get("external_id") or "") == gid), None)
            # swap mode, and a backfill with --swap: the original into Garmin's copy (in place, so the past too)
            if copy is not None and (swap or (getattr(settings, "swap", False) and not date_range)):
                swapped = _try_swap(settings, gid, str(copy["id"]), garmin, intervals, store, metrics, apply=apply,
                                    method="replace" if swap else None)
                if swapped:
                    result = {"outcome": "swapped", "intervals_id": swapped}
            if result is None:
                result = enrich_activity(gid, garmin, intervals, store, apply=apply,
                                         remote_candidates=remote, mappings=mappings)
            asked = _complete_archive(gid, activity, garmin, store, metrics) != "on_disk"
            _complete_intake(gid, activity, result.get("intervals_id"), garmin, intervals, store, metrics,
                             apply=apply)
            if pause_seconds and (asked or result["outcome"] not in ("already_enriched",)):
                time.sleep(pause_seconds)
        except GarminBlocked:
            raise
        except Exception as exc:
            if rate_limited(exc) is not None:          # Intervals asked for a break: the whole run pauses
                raise
            attempts = store.record_failure(gid, f"{type(exc).__name__}: {exc}")
            metrics["failed"] += 1
            log.warning("Enrich %s failed with %s (attempt %d); retrying later", gid, _error_name(exc), attempts)
            continue
        store.clear_failure(gid)
        outcome = result["outcome"]
        if outcome == "swapped":
            continue                                   # counted by _try_swap
        metrics[outcome] += 1
        if outcome == "unmatched":
            log.info("Activity %s has no Intervals counterpart yet; will look again next run", gid)
        elif outcome in ("planned", "enriched"):
            fields = sorted(result["fields"]["writes"])
            streams = [w["type"] for w in result["streams"]["writes"]]
            if outcome == "enriched":
                metrics["fields_written"] += len(fields)
                metrics["streams_written"] += len(streams)
            log.info("%s %s -> %s: fields %s, streams %s",
                     "ENRICHED" if outcome == "enriched" else "DRY-RUN would enrich",
                     gid, result["intervals_id"], fields, streams)
    if not date_range and not archive_only:
        _retry_overdue(garmin, intervals, store, metrics, {str(a.get("activityId")) for a in activities},
                       mappings, apply=apply)
        store.set_meta("activity_scan")
        if catch_up:
            caught_up(store, "activities")             # gone through; a failed one is retried by its id
    return metrics


RETRIES_BY_ID = 3               # per full run: failed activities that left the scan window


def _retry_overdue(garmin: Any, intervals: Any, store: Store, metrics: dict, listed: set[str], mappings: Any, *,
                   apply: bool) -> None:
    """Failures whose retry is due but whose activity is older than the scan window: by their id
    (else they would stay failed for good, and keep the scan from skipping)."""
    now = time.time()
    overdue = [f["garmin_id"] for f in store.failed_activities()
               if f["garmin_id"] not in listed and (not f["next_retry"] or f["next_retry"] <= now)]
    for gid in overdue[:RETRIES_BY_ID]:
        try:
            result = enrich_activity(gid, garmin, intervals, store, apply=apply, mappings=mappings)
        except GarminBlocked:
            raise
        except Exception as exc:
            if rate_limited(exc) is not None:          # Intervals asked for a break: the whole run pauses
                raise
            attempts = store.record_failure(gid, f"{type(exc).__name__}: {exc}")
            metrics["failed"] += 1
            log.warning("Retry of %s (outside the scan window) failed with %s (attempt %d)", gid, _error_name(exc),
                        attempts)
            continue
        store.clear_failure(gid)
        metrics[result["outcome"]] = metrics.get(result["outcome"], 0) + 1
        log.info("Retried %s (outside the scan window): %s", gid, result["outcome"])


def wellness_due(day: date, current: date, fetched: float | None, midnight: float,
                 refresh_seconds: float, now: float | None = None) -> bool:
    """When a day's Garmin data is read again.

    Today: every refresh period (its values keep changing). Yesterday: once after local
    midnight, when its totals became final, then every second refresh period, because a
    watch that syncs in the morning still carries yesterday's evening. Older days: once
    after midnight and not again; the archive keeps their snapshot.
    """
    if fetched is None:
        return True
    now = now if now is not None else time.time()
    age = (current - day).days
    if age <= 0:
        return now - fetched >= refresh_seconds
    if fetched < midnight:
        return True
    return age == 1 and now - fetched >= 2 * refresh_seconds


def _write_wellness_day(day: date, raw: dict, current: date, settings: Settings, intervals: Any,
                        metrics: dict, *, apply: bool, rewrite: set[str] | None = None,
                        night: dict | None = None) -> bool:
    """Map one snapshot and add what Intervals lacks. False when the day is locked there."""
    native, custom = map_wellness(raw, day, current, getattr(settings, "wellness_profile", "recommended"))
    if not native and not custom:
        log.info("No supported Garmin scalar wellness data for %s", day)
        return True
    existing = intervals.wellness(day)
    if existing.get("locked") is True:
        metrics["days_locked"] += 1
        log.info("Skipping locked wellness day %s", day)
        return False
    changes = merge_wellness(existing, native, custom, rewrite)
    fixes = snapshot_corrections(existing, {**native, **(night or {})},
                                 (raw.get("data") or {}).get("health_snapshots") or [])
    if fixes:
        log.info("Wellness %s: %s put back to the night's values (a Health Snapshot had replaced them)",
                 day, ", ".join(sorted(fixes)))
        changes.update(fixes)
        metrics["snapshot_fixes"] = metrics.get("snapshot_fixes", 0) + 1
    if changes:
        metrics["days_with_changes"] += 1
        log.info("Wellness %s new fields: %s", day, ", ".join(sorted(changes.keys())))
        missing_custom = set(custom).intersection(changes)
        if missing_custom:
            # Create only needed, nonexisting fields. The private custom-field
            # list is queried before writes; no global athlete config reset.
            created = intervals.provision_fields(apply=apply, needed=missing_custom)
            metrics["new_fields"].extend(created)
        if apply:
            intervals.write_wellness(day, changes)
            metrics["writes"] += 1
    return True


def sync_wellness(settings: Settings, garmin: Any, intervals: Any, store: Store,
                  *, apply: bool, force: bool = False, wellness_days: int | None = None,
                  today: date | None = None, days: list[date] | None = None,
                  pause_seconds: float = 0.0, endpoints: tuple[str, ...] | None = None,
                  from_archive: bool = False, rewrite: set[str] | None = None,
                  now: datetime | None = None) -> dict:
    """Daily wellness.

    The scheduled run (no `days`, no `force`) goes through `wellness_check`: the
    night's values as soon as the watch has synced, the finished day once the device
    has synced after it ended. Explicit runs read what they are told: `days` (a
    backfill) replaces the lookback, `force` reads the whole lookback now;
    `pause_seconds` is slept between days because each day costs up to 30 Garmin
    requests. `from_archive` maps the locally archived snapshots instead of asking
    Garmin: after the mapping gained fields, the past is completed without a single
    Garmin request.
    """
    if days is None and not force and endpoints is None and not from_archive and rewrite is None:
        return wellness_check(settings, garmin, intervals, store, apply=apply, now=now,
                              lookback=wellness_days, throttle=False)
    current = today or datetime.now(settings.timezone).date()
    lookback = wellness_days or settings.wellness_days
    metrics = {"days_checked": 0, "days_skipped_recent": 0, "days_from_archive": 0, "days_locked": 0,
               "days_with_changes": 0, "writes": 0, "failed": 0, "new_fields": []}
    day_list = days if days is not None else [current - timedelta(days=o) for o in reversed(range(lookback))]
    midnight = datetime.combine(current, datetime.min.time(), tzinfo=settings.timezone).timestamp()
    refresh = settings.wellness_refresh_hours * 3600
    for index, day in enumerate(day_list):
        if pause_seconds and index:
            time.sleep(pause_seconds)
        fetched, written = store.wellness_state(day)
        raw = None
        if from_archive:
            raw = store.load_snapshot(day)
            if raw is None:
                log.info("No archived snapshot for %s; nothing to map", day)
                continue
            metrics["days_from_archive"] += 1
        elif not force and not wellness_due(day, current, fetched, midnight, refresh):
            if written or not apply:
                metrics["days_skipped_recent"] += 1
                continue
            # Fetched by a dry run and never written: map the archived snapshot, do not ask again.
            raw = store.load_snapshot(day)
            if raw is not None:
                metrics["days_from_archive"] += 1
        if raw is None:
            # The first read of a day (and the one after midnight) takes every endpoint; the
            # refreshes within a day only the measurements, which is what changes during a day.
            chosen = (endpoints if endpoints is not None
                      else None if fetched is None or fetched < midnight else ESSENTIAL_ENDPOINTS)
            raw = garmin.snapshot(day, **({"endpoints": chosen} if chosen else {}))
            # a full read too: an endpoint that failed this time keeps what the archive had
            raw = _merge_partial(store.load_snapshot(day), raw)
            store.save_snapshot(day, raw)
            metrics["days_checked"] += 1
            if not raw.get("data"):
                log.warning("No Garmin wellness payload for %s; not marking complete", day)
                continue
            store.mark_wellness(day)                     # the throttle counts the read, not the write
        try:
            unlocked = _write_wellness_day(day, _with_files(store, day, raw), current, settings, intervals, metrics,
                                           apply=apply, rewrite=rewrite)
        except Exception as exc:                         # one day's Intervals trouble must not end the run
            if rate_limited(exc) is not None:          # Intervals asked for a break: the whole run pauses
                raise
            metrics["failed"] += 1
            log.warning("Wellness %s not written: %s", day, _error_name(exc))
            continue
        if apply and unlocked:
            store.mark_wellness(day, written=True)
    metrics["new_fields"] = sorted(set(metrics["new_fields"]))
    return metrics


def _with_files(store: Store, day: date, raw: dict) -> dict:
    """The day's Health Snapshots (from the index of its original files) next to Garmin's JSON."""
    snapshots = (store.load_wellness_index(day) or {}).get("health_snapshots") or []
    if not snapshots:
        return raw
    return {**raw, "data": {**(raw.get("data") or {}), "health_snapshots": snapshots}}


def _merge_partial(previous: dict | None, raw: dict) -> dict:
    """A partial read must not shrink the archived day: keep what was not re-read."""
    if not previous:
        return raw
    errors = {k: v for k, v in (previous.get("errors") or {}).items() if k not in raw["data"]}
    return {**previous, "data": {**(previous.get("data") or {}), **raw["data"]},
            "errors": {**errors, **raw.get("errors", {})}}


def transient_errors(raw: dict) -> list[str]:
    """Endpoints that failed for a passing reason (5xx, timeout, connection) – worth another read;
    a 4xx or a method this Garmin library lacks is no reason to wait."""
    out = []
    for key, error in (raw.get("errors") or {}).items():
        code = re.search(r"\b([1-5]\d\d)$", str(error))
        if str(error) != "MethodNotAvailable" and (code is None or code.group(1).startswith("5")):
            out.append(key)
    return out


FINAL_TRIES = 3                 # a day with passing endpoint failures is final after this many reads


# ---- when Garmin is asked for a day's wellness ----
#
# What can be written for today comes from four endpoints and only exists once the watch
# has synced after waking up; everything else about a day is final once the day is over
# and the device has synced after it. So instead of re-reading on a clock, the bridge
# waits for evidence, which it gets from Intervals for free: the official integration
# puts last night's sleep into today's record within minutes of the watch syncing.

MORNING_ENDPOINTS = ("sleep", "hrv", "training_readiness", "body_composition",
                     # Garmin's status metrics as of this morning (replaced at the final read)
                     "endurance_score", "hill_score", "max_metrics", "fitness_age", "race_predictions")
CHECK_MINUTES = 10              # Intervals pre-check cadence; costs no Garmin request
SIGNAL_DAYS = 2                 # official sleep seen this recently: wait for it instead of asking Garmin
FALLBACK_SLOTS = ((6, 30), (7, 30), (8, 30), (10, 0), (12, 0), (16, 0), (20, 0))
LATE_SLOTS = ((12, 0), (16, 0), (20, 0))     # with a signal: in case it stays silent all day
MORNING_BACKOFF_MINUTES = (30, 60, 120, 240)
READINESS_RETRIES, READINESS_RETRY_MINUTES = 2, 30
VERIFY_BACKOFF_MINUTES = (60, 120, 240)
FINAL_ANYWAY_HOUR = 20          # a past day is read at 20:00 the day after, synced or not


def _positive(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def _has_sleep(raw: dict | None) -> bool:
    sleep = ((raw or {}).get("data", {}).get("sleep") or {}).get("dailySleepDTO") or {}
    return _positive(sleep.get("sleepTimeSeconds"))


def _has_morning_readiness(raw: dict | None) -> bool:
    entries = (raw or {}).get("data", {}).get("training_readiness")
    return isinstance(entries, list) and any(
        isinstance(x, dict) and x.get("inputContext") in ("AFTER_WAKEUP_RESET", "MORNING_REPORT") for x in entries)


def _last_device_sync(raw: dict | None) -> datetime | None:
    """Garmin reports the device's last sync only in the current day's summary."""
    stats = (raw or {}).get("data", {}).get("stats") or {}
    return _time(stats.get("lastSyncTimestampGMT"), garmin_gmt=True)


def _slot_due(store: Store, day: date, now: datetime, slots: tuple) -> bool:
    """True once per slot that has passed; a run that comes late uses up all passed slots at once."""
    passed = [i for i, (h, m) in enumerate(slots)
              if now >= datetime.combine(day, datetime.min.time(), tzinfo=now.tzinfo) + timedelta(hours=h, minutes=m)]
    key = f"wellness:slot:{day}:{len(slots)}"
    used = int(store.get_meta(key) or -1)
    if not passed or passed[-1] <= used:
        return False
    store.set_meta(key, str(passed[-1]))
    return True


def _backoff(store: Store, key: str, steps: tuple, now_ts: float) -> None:
    tries = int(store.get_meta(f"{key}:tries") or 0)
    store.set_meta(f"{key}:tries", str(tries + 1))
    store.set_meta(key, str(now_ts + steps[min(tries, len(steps) - 1)] * 60))


def _wellness_metrics() -> dict:
    return {"days_checked": 0, "days_skipped_recent": 0, "days_from_archive": 0, "days_locked": 0,
            "days_with_changes": 0, "writes": 0, "failed": 0, "new_fields": [],
            "intervals_checks": 0, "sync_checks": 0, "morning": 0, "final": 0, "files": 0}


def wellness_check(settings: Settings, garmin: Any, intervals: Any, store: Store, *, apply: bool,
                   now: datetime | None = None, lookback: int | None = None, throttle: bool = True) -> dict:
    """The scheduled wellness run; cheap enough to be called with every poll.

    Today: nothing is asked of Garmin until there is a night to read. The evidence is
    Intervals' own record for today: once the official integration has put the sleep
    there, the watch has synced, and the four endpoints that make today's values are
    read exactly once (Training Readiness gets two more tries if it lags behind).
    Without that signal (official wellness sync off, or Intervals no longer passing
    Garmin data through its API) Garmin itself is checked at a few fixed times.

    Past days: read in full once the device has synced after the day ended, so the
    day's totals are complete; that is proven by today's sleep, or by Garmin's last
    sync time once Intervals shows anything new for today. A day that never gets a
    sync is read anyway at 20:00 the day after.

    Every check of Intervals costs one small request and none of Garmin; when nothing
    is pending, not even that.
    """
    tz = settings.timezone
    now = now or datetime.now(tz)
    now_ts = now.timestamp()
    current = now.date()
    midnight = datetime.combine(current, datetime.min.time(), tzinfo=tz)
    metrics = _wellness_metrics()
    base = lookback or settings.wellness_days
    note_pause(settings, store, current)
    days = catch_up_days(store, "wellness", base, current)       # after a pause: back to the last run
    past = [current - timedelta(days=o) for o in reversed(range(1, max(days, 1)))]

    if store.get_meta("wellness:since") is None:
        # First run of this schedule: days already read after they ended count as final, and the
        # official integration is assumed to deliver sleep until two mornings prove otherwise.
        for day in past:
            fetched, _ = store.wellness_state(day)
            day_end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=tz).timestamp()
            if fetched and fetched >= day_end:
                store.set_meta(f"wellness:final:{day}", "migrated")
        store.set_meta("wellness:official_sleep", (current - timedelta(days=1)).isoformat())
        store.set_meta("wellness:since", now.isoformat(timespec="seconds"))

    morning = store.get_meta(f"wellness:morning:{current}")
    night_key, snapshot_key = f"wellness:night:{current}", f"wellness:snapshot:{current}"
    watching = bool(morning) and store.get_meta(night_key) is not None    # a snapshot may replace the night
    pending_past = [d for d in past if store.get_meta(f"wellness:final:{d}") is None]
    if days > base and not any(d < current - timedelta(days=base - 1) for d in pending_past):
        caught_up(store, "wellness")                  # the days a pause left are read
    readiness_key = f"wellness:readiness:{current}"
    readiness_due = (morning == "no-readiness"
                     and int(store.get_meta(f"{readiness_key}:tries") or 0) < READINESS_RETRIES
                     and now_ts >= float(store.get_meta(readiness_key) or 0))
    files_due = [d for d in past if store.get_meta(f"wellness:final:{d}") and files_missing(store, d)
                 and now_ts >= float(store.get_meta(f"wellness:files:{d}") or 0)]
    unwritten = [d for d in past + [current]
                 if apply and (state := store.wellness_state(d))[0] and not state[1]
                 and (d == current and morning or store.get_meta(f"wellness:final:{d}"))]
    advice_due = bool(morning) and (imready4.due(settings, store, current, now_ts, apply=apply)
                                    or hrvtrend.due(settings, store, current, now_ts, apply=apply))
    if (morning and not watching and not pending_past and not readiness_due and not unwritten and not files_due
            and not advice_due):
        return metrics
    last = float(store.get_meta("wellness:check") or 0)        # the time of the last check, as the caller has it
    if throttle and not unwritten and not files_due and last and 0 <= now_ts - last < CHECK_MINUTES * 60 - 5:
        return metrics
    store.set_meta("wellness:check", f"{now_ts:.0f}")

    def read(day: date, endpoints: tuple[str, ...] | None) -> dict:
        fresh = garmin.snapshot(day, **({"endpoints": endpoints} if endpoints else {}))
        raw = _merge_partial(store.load_snapshot(day), fresh)       # a failed endpoint keeps the archive's
        store.save_snapshot(day, raw)
        store.mark_wellness(day)
        metrics["days_checked"] += 1
        return {**raw, "transient": transient_errors(fresh)}

    def write(day: date, raw: dict, rewrite: set[str] | frozenset[str] | None = None) -> None:
        if rewrite and apply:
            store.set_meta(f"wellness:rewrite:{day}", "1")     # kept until written: a retry rewrites too
        try:
            unlocked = _write_wellness_day(day, _with_files(store, day, raw), current, settings, intervals, metrics,
                                           apply=apply, rewrite=set(rewrite) if rewrite else None)
            if apply:
                store.mark_wellness(day, written=True)   # a locked day too: asked again after its next fetch
                if rewrite and unlocked:
                    store.set_meta(f"wellness:rewrite:{day}", "")
        except Exception as exc:                         # one day's Intervals trouble must not end the run
            if rate_limited(exc) is not None:          # Intervals asked for a break: the whole run pauses
                raise
            metrics["failed"] += 1
            log.warning("Wellness %s not written: %s; written from the archive next time", day, _error_name(exc))

    def mirror(day: date) -> bool:
        """The device's original files of a finished day; True when they were just saved."""
        try:
            saved = mirror_wellness_files(day, garmin, store) == "saved"
        except GarminBlocked:
            raise
        except Exception as exc:
            _backoff(store, f"wellness:files:{day}", VERIFY_BACKOFF_MINUTES, now_ts)
            log.warning("Wellness files of %s not mirrored: %s; trying again later", day, _error_name(exc))
            return False
        metrics["files"] += int(saved)
        return saved

    for day in unwritten:                  # read, never written (a dry run, a failed write): no new request
        if (raw := store.load_snapshot(day)) is not None:
            metrics["days_from_archive"] += 1
            write(day, raw, rewrite=PROVISIONAL_CODES if store.get_meta(f"wellness:rewrite:{day}") else None)

    record = intervals.wellness(current)
    metrics["intervals_checks"] += 1
    official_sleep = _positive(record.get("sleepSecs"))
    touched = official_sleep or any(_positive(record.get(k)) for k in ("steps", "restingHR", "hrv", "weight"))
    if official_sleep and not morning:
        store.set_meta("wellness:official_sleep", current.isoformat())
    seen = store.get_meta("wellness:official_sleep")
    signal = bool(seen) and (current - date.fromisoformat(seen)).days <= SIGNAL_DAYS
    synced = official_sleep          # the official integration had the night: the device synced after midnight

    morning_key = f"wellness:next:{current}"
    if not morning and now_ts >= float(store.get_meta(morning_key) or 0) and (
            official_sleep or _slot_due(store, current, now, LATE_SLOTS if signal else FALLBACK_SLOTS)):
        raw = read(current, MORNING_ENDPOINTS)
        if _has_sleep(raw):
            morning = "ok" if _has_morning_readiness(raw) else "no-readiness"
            store.set_meta(f"wellness:morning:{current}", morning)
            data = raw.get("data") or {}
            night = {"hrv": ((data.get("hrv") or {}).get("hrvSummary") or {}).get("lastNightAvg"),
                     "restingHR": (data.get("sleep") or {}).get("restingHeartRate")}
            store.set_meta(night_key, json.dumps({k: v for k, v in night.items() if _positive(v)}))
            store.set_meta(readiness_key, str(now_ts + READINESS_RETRY_MINUTES * 60))
            metrics["morning"] = 1
            synced = True
            log.info("Morning values of %s read (%s)", current,
                     "with readiness" if morning == "ok" else "readiness not there yet")
            write(current, raw)
        else:
            _backoff(store, morning_key, MORNING_BACKOFF_MINUTES, now_ts)
            log.info("Garmin has no sleep for %s yet; asking again later", current)
    elif readiness_due:
        raw = read(current, ("training_readiness",))
        if _has_morning_readiness(raw):
            store.set_meta(f"wellness:morning:{current}", "ok")
            write(current, raw)
        else:
            _backoff(store, readiness_key, (READINESS_RETRY_MINUTES,), now_ts)

    if morning:
        synced = True                    # Garmin has today's sleep: the device synced after midnight
    night = json.loads(store.get_meta(night_key) or "{}")
    shown = record.get("hrv")
    if (morning and _positive(night.get("hrv")) and _positive(shown) and abs(shown - night["hrv"]) >= 0.5
            and store.get_meta(snapshot_key) != str(shown)):
        # Intervals' HRV moved away from the night's: the official integration has just written a Health
        # Snapshot over it (it does so for resting HR too). Today's files say which values were the snapshot's;
        # they are only read here, the final read keeps the complete day.
        metrics["snapshot_checks"] = metrics.get("snapshot_checks", 0) + 1
        try:
            files = garmin.wellness_files(current)
            snapshots = wellness_bundle_index(files)["health_snapshots"] if files else []
            if snapshots and (raw := store.load_snapshot(current)) is not None:
                raw = {**raw, "data": {**(raw.get("data") or {}), "health_snapshots": snapshots}}
                _write_wellness_day(current, raw, current, settings, intervals, metrics, apply=apply, night=night)
            store.set_meta(snapshot_key, str(shown))     # done: only now not asked again
        except GarminBlocked:
            raise
        except Exception as exc:
            if rate_limited(exc) is not None:          # Intervals asked for a break: the whole run pauses
                raise
            metrics["failed"] += 1
            log.warning("Health Snapshot check of %s not done: %s; again at the next check", current,
                        _error_name(exc))
    verify_key = f"wellness:verify:{current}"
    if pending_past and not synced and touched and now_ts >= float(store.get_meta(verify_key) or 0):
        # Something new arrived in Intervals for today; ask Garmin whether that was a device sync.
        raw = garmin.snapshot(current, endpoints=("stats",))
        store.save_snapshot(current, _merge_partial(store.load_snapshot(current), raw))
        metrics["sync_checks"] += 1
        last_sync = _last_device_sync(raw)
        if last_sync is not None and last_sync >= midnight:
            synced = True
        else:
            _backoff(store, verify_key, VERIFY_BACKOFF_MINUTES, now_ts)
    for day in pending_past:
        anyway = now >= datetime.combine(day + timedelta(days=1), datetime.min.time(),
                                         tzinfo=tz) + timedelta(hours=FINAL_ANYWAY_HOUR)
        retry_key = f"wellness:retry:{day}"
        if not (synced or anyway) or now_ts < float(store.get_meta(retry_key) or 0):
            continue
        raw = read(day, None)
        if not raw.get("data"):
            _backoff(store, retry_key, VERIFY_BACKOFF_MINUTES, now_ts)
            log.warning("No Garmin wellness payload for %s; trying again later", day)
            continue
        if raw["transient"] and int(store.get_meta(f"{retry_key}:tries") or 0) < FINAL_TRIES - 1:
            _backoff(store, retry_key, VERIFY_BACKOFF_MINUTES, now_ts)
            log.info("Final read of %s: %s failed this time; reading again later", day,
                     ", ".join(raw["transient"]))
            continue
        store.set_meta(f"wellness:final:{day}", "synced" if synced else "anyway")
        metrics["final"] += 1
        log.info("Final read of %s (%s)", day, "device synced after the day" if synced else "no sync by 20:00")
        mirror(day)                                     # its Health Snapshots belong to the same write
        write(day, raw, rewrite=PROVISIONAL_CODES)      # the evening's values replace this morning's

    for day in files_due:                               # finished earlier, files still missing
        if mirror(day) and (raw := store.load_snapshot(day)) is not None:
            write(day, raw)

    # the advice once the night (and any Health Snapshot correction) is in Intervals: Intervals requests only
    for key, feature in (("advice", imready4), ("trend_advice", hrvtrend)):
        if morning and feature.due(settings, store, current, now_ts, apply=apply):
            try:
                metrics[key] = feature.morning(settings, intervals, store, current, now, apply=apply)
            except Exception as exc:                     # never at the cost of the wellness run
                if rate_limited(exc) is not None:          # Intervals asked for a break: the whole run pauses
                    raise
                log.warning("%s for %s failed: %s", key, current, _error_name(exc))

    if store.get_meta("wellness:pruned") != current.strftime("%Y-%m"):
        store.delete_meta("wellness:*:20*", now_ts - 40 * 86400)     # per-day bookkeeping, once a month
        store.delete_meta("imready4:*20*", now_ts - 40 * 86400)
        store.delete_meta("hrvtrend:*20*", now_ts - 40 * 86400)
        store.set_meta("wellness:pruned", current.strftime("%Y-%m"))
    metrics["new_fields"] = sorted(set(metrics["new_fields"]))
    return metrics


def _try_swap(settings: Any, gid: str, iid: str, garmin: Any, intervals: Any, store: Store, metrics: dict, *,
              apply: bool, method: str | None = None) -> str | None:
    """Swap mode: the original for Garmin's copy – into the same activity (replace) or as a new
    one of your own (upload). The Intervals id the activity has now when the swap took it (or
    planned it); None when the copy stays and is completed as usual."""
    from .swap import replace_activity, swap_activity
    method = method or getattr(settings, "swap_method", "replace")
    try:
        if method == "replace":
            result = replace_activity(gid, iid, garmin, intervals, store, apply=apply)
        else:
            result = swap_activity(gid, iid, garmin, intervals, store, apply=apply,
                                   window_hours=settings.swap_hours)
    except GarminBlocked:
        raise
    except InstanceBusy:                     # a swap by hand right now: the next full run looks again
        log.info("Swap of %s waits: another swap is running", gid)
        return iid
    except Exception as exc:
        if rate_limited(exc) is not None:          # Intervals asked for a break: the whole run pauses
            raise
        # nothing deleted; an upload already sent is recorded (pending/uploaded) and never sent twice
        log.warning("Swap of %s not done (%s); completing Garmin's copy instead", gid, _error_name(exc))
        return None
    outcome = result["outcome"]
    metrics[f"swap_{outcome}"] = metrics.get(f"swap_{outcome}", 0) + 1
    if outcome in ("kept", "same"):
        if outcome == "kept":
            log.info("Swap: %s stays Garmin's copy: %s", iid, result["reason"])
        return None
    if outcome == "planned":
        if method == "replace":
            log.info("DRY-RUN would put the original of %s into Garmin's copy %s", gid, iid)
        else:
            log.info("DRY-RUN would swap %s: Garmin's copy %s for the original (carrying %s)", gid, iid,
                     result["carry"])
        return iid
    return result.get("new_id") or iid


def watch_once(settings: Settings, garmin: Any, intervals: Any, store: Store, *, apply: bool,
               today: date | None = None) -> dict:
    """One cheap poll of Intervals; Garmin is contacted only for activities seen for the first time.

    Intervals offers no push. A poll (every 5 minutes by default) costs one ~450-byte request;
    the official import then triggers exactly one original download per new
    Garmin activity, typically within minutes of the activity appearing. An
    activity seen before as someone else's copy (Strava's, typically, when it
    arrived first) that Intervals has since turned into Garmin's own is taken up
    then too, instead of waiting for the next full scan.
    """
    current = today or datetime.now(settings.timezone).date()
    note_pause(settings, store, current)               # back after a pause: the full run reads it all
    if getattr(settings, "swap", False) and apply:
        from .swap import retry_deletes
        retry_deletes(intervals, store)                # copies whose delete failed last time
    recent = intervals.activities(current - timedelta(days=2), current + timedelta(days=1),
                                  fields=["id", "external_id", "source", "start_date"], limit=50)
    metrics = {"polled": len(recent), "new": 0, "ignored": 0, "failed": 0,
               "enriched": 0, "planned": 0, "nothing_to_add": 0, "already_enriched": 0, "unmatched": 0}
    for item in recent:
        iid = str(item.get("id") or "")
        if not iid:
            continue
        external = str(item.get("external_id") or "")
        source = str(item.get("source") or "")
        seen_as = store.intervals_seen_source(iid)
        if seen_as is not None:
            if seen_as.upper() == "GARMIN_CONNECT" or source.upper() != "GARMIN_CONNECT":
                continue
            log.info("Watch: %s is Garmin's own copy now (was %s); enriching it", iid, seen_as or "unknown")
            metrics["switched"] = metrics.get("switched", 0) + 1
        store.mark_intervals_seen(iid, external, source)
        metrics["new"] += 1
        if source.upper() != "GARMIN_CONNECT" or not external.isdecimal():
            metrics["ignored"] += 1          # manual uploads, Strava, ... : not ours to touch
            continue
        if store.is_deferred(external):
            continue
        if getattr(settings, "swap", False):
            done = _try_swap(settings, external, iid, garmin, intervals, store, metrics, apply=apply)
            if done:
                continue
        try:
            result = enrich_activity(external, garmin, intervals, store, apply=apply, intervals_id=iid)
        except GarminBlocked:
            raise
        except Exception as exc:
            if rate_limited(exc) is not None:          # Intervals asked for a break: the whole run pauses
                raise
            store.record_failure(external, f"{type(exc).__name__}: {exc}")
            metrics["failed"] += 1
            log.warning("Watch: enrich of %s failed with %s; the scheduled run will retry",
                        external, _error_name(exc))
            continue
        store.clear_failure(external)
        metrics[result["outcome"]] += 1
        if result["outcome"] in ("planned", "enriched"):
            log.info("Watch: %s %s -> %s fields %s streams %s", result["outcome"].upper(), external, iid,
                     sorted(result["fields"]["writes"]), [w["type"] for w in result["streams"]["writes"]])
    return metrics
