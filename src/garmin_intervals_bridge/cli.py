"""Command line interface. NO REMOTE WRITES unless --apply is present."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from contextlib import ExitStack
from datetime import date, datetime, timedelta
from pathlib import Path

from . import __version__, alerts, autostart, hrvtrend, imready4, notices, polling, webui, wizard
from .backup import backup_once, backup_range
from .backup import describe as describe_backup
from .charts import setup_charts
from .config import Settings, configured_log_file
from .enrich import enrich_activity, exact_match, gap_report, intervals_id_from, match_activity, resolve_one
from .fit import compare_fit, extract_original_fit, sha256
from .garmin import DAY_ENDPOINTS, GarminBlocked, GarminLoginNeeded, GarminSource, parse_endpoints
from .gear import SensorCache as GearSensors
from .gear import apply_plan as apply_gear_plan
from .gear import build_plan as build_gear_plan
from .gear import load_config as load_gear_config
from .gear import refresh_totals as refresh_gear_totals
from .gear import report as gear_report
from .gear import save_config as save_gear_config
from .gear import sync_recent as sync_gear_recent
from .health import liveness, probe
from .intervals import IntervalsClient, rate_limited
from .store import InstanceBusy, Store, single_instance
from .sync import (
    backfill_intake,
    restore_intake,
    sync_activities,
    sync_enrich,
    sync_wellness,
    sync_wellness_files,
    watch_once,
    wellness_check,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="garmin-intervals-bridge", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--env-file", type=Path, metavar="PATH",
                        help="Settings file (default: ./.env, else ~/GarminBridge/.env as `setup` writes it)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup", help="Set the bridge up step by step: Intervals key, time zone, Garmin login, "
                                 "extras, a trial run, start at logon (interactive; run it again to change "
                                 "anything)")
    start = sub.add_parser("autostart", help="Start the bridge (run --apply) in the background whenever you log "
                                             "in: Windows Task Scheduler, macOS LaunchAgent, Linux systemd user "
                                             "service; again = restart with the current settings")
    what = start.add_mutually_exclusive_group()
    what.add_argument("--remove", action="store_true", help="Stop it and remove the autostart")
    what.add_argument("--status", action="store_true", help="Show whether it is set up")
    what.add_argument("--dry-run", action="store_true", help="Show the files and commands, change nothing")
    start.add_argument("--mode", choices=["enrich", "upload", "backup"], help="As for `run` (default BRIDGE_MODE)")
    page = sub.add_parser("ui", help="Open the bridge's web page in your browser: status, Garmin sign-in (MFA), "
                                     "settings, gear and chart actions, the log. Until Ctrl+C")
    page.add_argument("--port", type=int, default=webui.DEFAULT_PORT)
    page.add_argument("--host", default="127.0.0.1",
                      help="127.0.0.1 (default) = this computer only; anything else needs BRIDGE_UI_PASSWORD")
    page.add_argument("--no-browser", action="store_true", help="Only print the link")
    sub.add_parser("notify-test", help="Send a test e-mail / push message to the alert channels set up "
                                       "(BRIDGE_ALERT_EMAIL, BRIDGE_NTFY_URL)")
    sub.add_parser("login", help="Sign into Garmin once and save refreshable tokens (interactive)")
    probe = sub.add_parser("probe", help="Download and archive one untouched ORIGINAL FIT; no Intervals writes")
    probe.add_argument("--activity-id", help="Garmin Connect activity ID (default: latest)")
    cmp = sub.add_parser("compare-fit", help="Compare two local FITs by message/field inventories")
    cmp.add_argument("file_a", type=Path)
    cmp.add_argument("file_b", type=Path)
    gap = sub.add_parser("gap", help="Compare one Garmin original with the copy Intervals received; no writes "
                                     "(works next to a running bridge)")
    gap.add_argument("--activity-id", required=True, help="Garmin Connect activity ID")
    gap.add_argument("--intervals-id", help="Intervals activity ID (default: match automatically)")
    enrich = sub.add_parser("enrich", help="One activity again: fill its custom fields/streams from the Garmin "
                                           "original, compared with what Intervals holds now – e.g. after "
                                           "Intervals' 'Reprocess file' (dry run unless --apply; works next to "
                                           "a running bridge)")
    enrich.add_argument("activity", nargs="?", help="The Intervals activity: its id (i12345678) or its link")
    swap = sub.add_parser("swap", help="Replace Garmin's filtered copy of one activity by its original: upload "
                                       "the original, carry name, workout, RPE ... over, then delete the copy "
                                       "(dry run unless --apply; BRIDGE_SWAP=true does it for every new one)")
    swap.add_argument("activity", help="The Intervals activity: its id (i12345678) or its link")
    swap.add_argument("--apply", action="store_true", help="Actually upload and delete")
    enrich.add_argument("--activity-id", help="Garmin Connect activity ID")
    enrich.add_argument("--intervals-id", help="Same as ACTIVITY")
    enrich.add_argument("--apply", action="store_true", help="Actually PUT fields and streams")
    enrich.add_argument("--refresh-own-streams", action="store_true",
                        help="(always on now) rewrite the streams this bridge wrote before")
    setup = sub.add_parser("setup-fields", help="Preview/create missing private numeric custom fields")
    setup.add_argument("--apply", action="store_true", help="Actually create missing custom fields")
    sync = sub.add_parser("sync", help="Synchronize (local archive only / dry-run by default)")
    sync.add_argument("--apply", action="store_true", help="Permit Intervals writes")
    sync.add_argument("--scope", choices=["all", "activities", "wellness"], default="all")
    sync.add_argument("--mode", choices=["enrich", "upload"], default="enrich",
                      help="enrich = official import stays on, add what Garmin stripped (default); "
                           "upload = post originals as new activities (only with the official import off)")
    sync.add_argument("--allow-activity-upload", action="store_true",
                      help="Required in addition to --apply; first disable official Garmin activity import")
    sync.add_argument("--force-wellness", action="store_true", help="Ignore cached wellness refresh window")
    sync.add_argument("--activity-days", type=int, help="Override activity lookback (1-30 days)")
    sync.add_argument("--wellness-days", type=int, help="Override wellness lookback (1-30 days)")
    sync.add_argument("--activity-interval", type=int, default=0, metavar="MINUTES",
                      help="Enrich mode, for scheduled runs: skip the Garmin activity scan while the last one "
                           "is younger than this (the watch enriches new activities in between)")
    watch = sub.add_parser("watch", help="One cheap poll of Intervals; enrich only what appeared "
                                         "since the last poll, and read last night's wellness once the "
                                         "watch has synced (dry run unless --apply)")
    watch.add_argument("--apply", action="store_true")
    run = sub.add_parser("run", help="Keep running: poll Intervals every 5 minutes, full sync every 30 minutes, "
                                     "health probe once a day. For Windows and anything without systemd "
                                     "(dry run unless --apply)")
    run.add_argument("--apply", action="store_true")
    run.add_argument("--mode", choices=["enrich", "upload", "backup"],
                     help="enrich = official import stays on, the bridge adds what Garmin stripped (default); "
                          "upload = official import off, the bridge uploads every original as your own file; "
                          "backup = Garmin only, your data into the data folder, no Intervals (default: BRIDGE_MODE)")
    run.add_argument("--poll-seconds", type=int,
                     help="Seconds between polls: Intervals in enrich mode (default 300), Garmin in upload mode "
                          "(default 600, min 300); also BRIDGE_POLL_SECONDS. BRIDGE_POLL_HOURS=10-18 polls only "
                          "then (the full runs go on)")
    run.add_argument("--sync-minutes", type=int,
                     help="Minutes between full runs (default 30; backup mode: BRIDGE_BACKUP_HOURS, default 12 h)")
    run.add_argument("--iterations", type=int, help=argparse.SUPPRESS)      # tests
    backup = sub.add_parser("backup", help="Back up your Garmin data into the data folder – no Intervals needed: "
                                           "without --from what backup mode does every BRIDGE_BACKUP_HOURS (12), "
                                           "with --from the past")
    backup.add_argument("--from", dest="start", metavar="DATE", help="The past from DATE (YYYY-MM-DD)")
    backup.add_argument("--to", dest="end", metavar="DATE", help="… up to DATE (default: yesterday)")
    backup.add_argument("--scope", choices=["all", "activities", "wellness", "account"], default="all")
    backup.add_argument("--endpoints", default="all",
                        help="Wellness endpoints with --from: 'all' (default, about 30 requests per day), "
                             "'standard' (15), 'essential' (12) or a list")
    backup.add_argument("--pause", type=float, default=2.0, help="Seconds between days/activities (default 2)")
    backup.add_argument("--force", action="store_true", help="With --from: read days already backed up again")
    backfill = sub.add_parser("backfill", help="Enrich a date range from the past, paced (dry run unless --apply)")
    backfill.add_argument("--scope", choices=["activities", "wellness"], required=True)
    backfill.add_argument("--mode", choices=["enrich", "upload"], default="enrich",
                          help="Activities: enrich the officially imported ones (default) or upload the originals "
                               "of those Intervals does not have (official import off)")
    backfill.add_argument("--from", dest="start", required=True, help="YYYY-MM-DD")
    backfill.add_argument("--to", dest="end", help="YYYY-MM-DD (default: today)")
    backfill.add_argument("--apply", action="store_true")
    backfill.add_argument("--pause", type=float, default=2.0, help="Seconds between days/activities (default 2)")
    backfill.add_argument("--force-wellness", action="store_true", help="Re-fetch days already fetched")
    backfill.add_argument("--endpoints", default="standard",
                          help="Wellness: 'standard' (the per-day measurements plus endurance/hill score and "
                               "race predictions, default), 'essential' (the measurements only), 'all', or a "
                               "comma-separated list of endpoint keys (e.g. hill_score,race_predictions) to add to "
                               "days fetched before (with --force-wellness)")
    backfill.add_argument("--wellness-files", action="store_true",
                          help="Wellness: mirror the device's original wellness files of each day (monitoring, sleep, "
                               "HRV, Health Snapshots ...) into wellness-files/; one Garmin request per day, no "
                               "Intervals")
    backfill.add_argument("--intake", action="store_true",
                          help="Activities: fluid (ml) and food (kcal) logged in Garmin into the activity fields "
                               "GarminFluidIntake / GarminCaloriesConsumed, from the archived summaries; no Garmin "
                               "request")
    backfill.add_argument("--archive-only", action="store_true",
                          help="Activities: download the original FIT files that are not on disk yet and "
                               "leave Intervals alone (a local mirror of every recording)")
    backfill.add_argument("--rewrite", help="Wellness: comma-separated bridge fields (Garmin...) whose existing "
                                            "values may be replaced, after a mapping correction")
    backfill.add_argument("--from-archive", action="store_true",
                          help="Wellness: map the locally archived snapshots instead of asking Garmin "
                               "(completes the past after the mapping gained fields; no Garmin requests)")
    charts = sub.add_parser("setup-charts", help="Create private Intervals fitness charts for the synced "
                                                 "Garmin values (dry run unless --apply)")
    charts.add_argument("--apply", action="store_true")
    sub.add_parser("health", help="Probe Garmin and Intervals once; exit 2 when one has been failing "
                                  "longer than BRIDGE_STALE_HOURS (for a daily timer with an alert)")
    pending = sub.add_parser("reset-pending", help="After manual duplicate check, clear a pending upload lock")
    pending.add_argument("--activity-id", required=True)
    pending.add_argument("--i-checked-intervals", action="store_true", required=True,
                         help="Confirm you checked Intervals for duplicates and verified it is absent")
    gear = sub.add_parser("gear", help="Put Garmin's gear (bikes, shoes ...) on the Intervals activities, from the "
                                       "local archive: no Garmin requests, nothing removed (dry run unless --apply)")
    gear.add_argument("--apply", action="store_true", help="Write the plan; later scheduled runs keep the last "
                                                           "week in line with the same choices")
    gear.add_argument("--prefer", choices=["intervals", "evidence", "garmin"], default="intervals",
                      help="When both sides have different gear: keep Intervals' (default), take Garmin's only "
                           "with proof (gear not bought yet / retired that day, or the bike's own sensors missing "
                           "from the recording), or always take Garmin's")
    gear.add_argument("--map", action="append", default=[], metavar="GARMIN=INTERVALS",
                      help="Pair gear whose names differ, e.g. --map 'Zoom Fly Orange=Nike Zoom Fly - Red' "
                           "(repeatable)")
    gear.add_argument("--create", action="store_true", help="Create Garmin gear Intervals does not have")
    gear.add_argument("--include-retired", action="store_true", help="With --create: retired gear too")
    gear.add_argument("--no-create", action="store_true",
                      help="Stop the scheduled runs from creating new Garmin gear (they do once --create was used "
                           "with --apply)")
    gear.add_argument("--from", dest="start", help="YYYY-MM-DD (default: everything archived)")
    gear.add_argument("--to", dest="end", help="YYYY-MM-DD (default: today)")
    gear.add_argument("--details", action="store_true", help="List every activity, not only the counts")
    gear.add_argument("--recalc", action="store_true",
                      help="Also recount the totals of every paired gear and its components with Intervals' own "
                           "count, when they drifted (e.g. after reassignments by hand)")
    advice = sub.add_parser("imready4", help="ImReady4-compatible training advice from Intervals' HRV and resting "
                                             "HR into the TrainingAdvice field: a day (default today), a range, a "
                                             "comparison with values already there (dry run unless --apply)")
    advice.add_argument("--date", help="YYYY-MM-DD (default: today)")
    advice.add_argument("--from", dest="start", help="YYYY-MM-DD: a range instead of one day")
    advice.add_argument("--to", dest="end", help="YYYY-MM-DD (default: today)")
    advice.add_argument("--compare", action="store_true",
                        help="Compare with the values TrainingAdvice already holds (e.g. from running ImReady4)")
    advice.add_argument("--apply", action="store_true", help="Write days whose TrainingAdvice is empty")
    advice.add_argument("--overwrite", action="store_true",
                        help="Also replace values TrainingAdvice already holds (with --apply)")
    advice.add_argument("--details", action="store_true", help="With --compare: list every day too")
    trend = sub.add_parser("hrvtrend", help="Experimental: training advice from the 7-day HRV trend into its own "
                                            "field HRVTrendAdvice (+ HRVTrendCV), next to ImReady4's; a day, a "
                                            "range, a comparison with TrainingAdvice (dry run unless --apply)")
    trend.add_argument("--date", help="YYYY-MM-DD (default: today)")
    trend.add_argument("--from", dest="start", help="YYYY-MM-DD: a range instead of one day")
    trend.add_argument("--to", dest="end", help="YYYY-MM-DD (default: today)")
    trend.add_argument("--compare", action="store_true", help="Compare with ImReady4's TrainingAdvice")
    trend.add_argument("--apply", action="store_true",
                       help="Create the two fields if missing and write days whose HRVTrendAdvice is empty")
    trend.add_argument("--overwrite", action="store_true", help="Also replace existing HRVTrendAdvice values")
    trend.add_argument("--details", action="store_true", help="With --compare: list every day too")
    status = sub.add_parser("status", help="Show pending uploads requiring manual reconciliation")
    status.add_argument("--check", action="store_true",
                        help="Exit 1 when the bridge has stopped working (no run for twice the expected time) – "
                             "a container's healthcheck; no request to anyone")
    account = sub.add_parser("snapshot-account", help="Archive what is not a time series: profile, settings, "
                                                      "devices, zones, gear, personal records, badges, workouts, "
                                                      "training plans, goals, the calendar, FTP and running "
                                                      "tolerance of the recent weeks")
    account.add_argument("--history-from", metavar="DATE",
                         help="Also the calendar and Garmin's FTP / running tolerance series since DATE (YYYY-MM-DD)")
    return parser


ACTIVITY_SCAN_MINUTES = 120     # run loop, enrich mode: the poll catches new activities in between
TICK_SECONDS = 60               # run loop: how often it asks whether a poll is due


def _gear_recent(settings: Settings, store: Store, intervals: IntervalsClient, *, apply: bool) -> dict:
    """Gear of the last week, once `gear --apply` has set the choices; never fails the run."""
    try:
        return sync_gear_recent(settings, store, intervals, apply=apply)
    except Exception as exc:
        if rate_limited(exc) is not None:                # Intervals asked for a break: the run pauses
            raise
        logging.getLogger("bridge").warning("Gear not synced this time: %s", type(exc).__name__)
        return {"error": type(exc).__name__}


def run_loop(settings: Settings, store: Store, *, apply: bool, poll_seconds: int, sync_minutes: int,
             iterations: int | None = None, mode: str = "enrich") -> int:
    """What the three systemd timers do, in one long-running process.

    A fresh Garmin session per full run (like the timer), the cheap poll in between –
    every `poll_seconds`, or as quiet hours and the adaptive mode say (polling.py) – and
    a health probe once a day written to the log. A Garmin block pauses everything for
    15 minutes; any other error is logged and the loop goes on.

    Upload mode (official import off): the poll asks Garmin for new activities and
    uploads their originals; the full run does the same plus wellness.
    """
    log = logging.getLogger("bridge")
    intervals = IntervalsClient(settings.intervals_api_key, settings.intervals_athlete_id)
    garmin = GarminSource(settings.token_dir, settings.garmin_request_delay)
    next_sync, next_health, count, failures = 0.0, time.time() + 300, 0, 0
    tick = min(TICK_SECONDS, poll_seconds)
    # only a web page in this process can wake the loop ("Check now"); one elsewhere waits for the tick
    waker = polling.POLL_NOW if getattr(settings, "ui_port", None) else None
    polling.ACTIVE.update(fast=poll_seconds, mode=mode)
    log.info("garmin-intervals-bridge %s running (%s mode): polls %s; full run every %d min, %s", __version__,
             mode, polling.schedule_text(settings, store, time.time(), poll_seconds), sync_minutes,
             "writing to Intervals" if apply else "DRY RUN (add --apply to write)")
    source = getattr(settings, "timezone_source", "BRIDGE_TIMEZONE")
    (log.warning if source.startswith("fallback") else log.info)(
        "Day boundary: %s (%s)%s", settings.timezone.key, source,
        "; set BRIDGE_TIMEZONE to yours" if source.startswith("fallback") else "")
    while iterations is None or count < iterations:
        count += 1
        now = time.time()
        try:
            if intervals_paused(store):
                pass                                    # Intervals asked for a break (429): nothing at all
            elif now >= next_sync:
                garmin = GarminSource(settings.token_dir, settings.garmin_request_delay)
                if hasattr(intervals, "forget_custom_items"):
                    intervals.forget_custom_items()      # fields added since: seen by this full run
                results: dict = {"apply": apply, "scope": "all", "mode": mode}
                if mode == "upload":
                    results["activities"] = sync_activities(settings, garmin, intervals, store, apply=apply,
                                                            allow_upload=True)
                    gear_result = _gear_recent(settings, store, intervals, apply=apply)
                    if gear_result:
                        results["gear"] = gear_result
                else:
                    results["activities"] = sync_enrich(settings, garmin, intervals, store, apply=apply,
                                                        scan_interval_minutes=ACTIVITY_SCAN_MINUTES)
                    if not results["activities"].get("scan_skipped"):
                        gear_result = _gear_recent(settings, store, intervals, apply=apply)
                        if gear_result:
                            results["gear"] = gear_result
                results["wellness"] = sync_wellness(settings, garmin, intervals, store, apply=apply)
                results["garmin_requests"] = garmin.requests
                store.record_run("sync", results, started=now)
                if mode == "upload" and (results["activities"].get("uploaded") or results["activities"].get(
                        "would_upload")):
                    polling.note_new(store, now)
                if garmin.client is not None:
                    notices.garmin_ok(store)
                a, w = results["activities"], results["wellness"]
                labels = {("upload", True): ("uploaded", "uploaded"),
                          ("upload", False): ("would_upload", "to upload"),
                          ("enrich", True): ("enriched", "enriched"),
                          ("enrich", False): ("planned", "planned")}
                key, label = labels[(mode, apply)]
                done = a.get(key, 0)
                log.info("Full run: %d activities seen, %d %s; wellness %d days checked, %d written; "
                         "%d Garmin requests", a["seen"], done, label, w["days_checked"], w["writes"], garmin.requests)
                next_sync, failures = now + sync_minutes * 60, 0
            elif not polling.due(settings, store, now, poll_seconds, mode)[0]:
                pass                                    # quiet hours, or not yet: the full runs go on
            elif mode == "upload":
                garmin = GarminSource(settings.token_dir, settings.garmin_request_delay)
                result = sync_activities(settings, garmin, intervals, store, apply=apply, allow_upload=True,
                                         activity_days=2)
                if result.get("uploaded"):
                    result["gear"] = _gear_recent(settings, store, intervals, apply=apply)
                result["wellness"] = wellness_check(settings, garmin, intervals, store, apply=apply)
                store.record_run("watch", {"apply": apply, "mode": mode, "garmin_requests": garmin.requests, **result},
                                 started=now)
                if result.get("uploaded") or result.get("would_upload"):
                    polling.note_new(store, now)
                    log.info("Garmin poll: %d uploaded, %d would upload", result["uploaded"], result["would_upload"])
            else:
                # the poll shares the full run's Garmin session: count only its own requests
                before = garmin.requests
                result = watch_once(settings, garmin, intervals, store, apply=apply)
                wellness = wellness_check(settings, garmin, intervals, store, apply=apply)
                if wellness.get("intervals_checks"):
                    result["wellness"] = wellness
                store.record_run("watch", {"apply": apply, "garmin_requests": garmin.requests - before, **result},
                                 started=now)
                if result["new"]:
                    log.info("Poll: %d new, %d enriched, %d planned", result["new"], result["enriched"],
                             result["planned"])
            if now >= next_health and not intervals_paused(store):
                report = probe(settings, garmin, intervals, store)
                for finding in report.get("findings", []):
                    (log.warning if finding.get("alert") else log.info)("Health: %s - %s", finding.get("what"),
                                                                        finding.get("detail"))
                next_health = now + 86400
        except KeyboardInterrupt:
            log.info("Stopped")
            return 0
        except GarminBlocked as exc:
            on_page = isinstance(exc, GarminLoginNeeded) and getattr(settings, "ui_port", None)
            log.warning("%s%s; pausing for 15 minutes", exc, ", or sign in on the web page" if on_page else "")
            if isinstance(exc, GarminLoginNeeded):
                notices.login_needed(settings, store, intervals, datetime.now(settings.timezone).date(), apply=apply)
            next_sync = max(next_sync, time.time() + 900)
            if iterations is None and wait_after_block(exc):
                next_sync = 0                               # signed in on the web page: go on now
            continue
        except Exception as exc:
            if pause_on_rate_limit(store, exc):
                next_sync = max(next_sync, time.time() + RATE_LIMIT_PAUSE_SECONDS)
            elif exc.__class__.__module__.startswith("requests"):
                log.error("HTTP/API request failed: %s", type(exc).__name__)
            else:
                log.error("%s: %s", type(exc).__name__, exc)
            if now >= next_sync:             # the full run failed: again after a growing break, not each tick
                failures += 1
                next_sync = max(next_sync, time.time() + min(sync_minutes * 60, 300 * 2 ** (failures - 1)))
        if iterations is None or count < iterations:
            # wake when the next look is due (within a tick), for a full run at the latest
            wait = polling.seconds_to_next(settings, store, time.time(), poll_seconds, mode)
            pause = max(1.0, min(tick, wait if wait is not None else tick, max(0.0, next_sync - time.time()) or tick))
            if waker is not None:
                waker.wait(pause)
                waker.clear()
            else:
                time.sleep(pause)
    return 0


def run_backup_loop(settings: Settings, store: Store, *, every_minutes: int, iterations: int | None = None) -> int:
    """Backup mode: Garmin only, into the data folder; a health check once a day, no Intervals."""
    log = logging.getLogger("bridge")
    log.info("garmin-intervals-bridge %s running (backup mode): your Garmin data into %s every %d min",
             __version__, settings.data_dir, every_minutes)
    log.info("Day boundary: %s (%s)", settings.timezone.key, getattr(settings, "timezone_source", "BRIDGE_TIMEZONE"))
    next_health, count = time.time() + 300, 0
    while iterations is None or count < iterations:
        count += 1
        garmin = GarminSource(settings.token_dir, settings.garmin_request_delay)
        try:
            out = backup_once(settings, garmin, store)
            out["garmin_requests"] = garmin.requests
            store.record_run("backup", out)
            if garmin.client is not None:
                notices.garmin_ok(store)
            if out["activities"] or out["wellness_days"] or out["account"]:
                log.info("Backup: %s", describe_backup(out))
            if time.time() >= next_health:
                for finding in probe(settings, garmin, None, store).get("findings", []):
                    (log.warning if finding.get("alert") else log.info)("Health: %s - %s", finding.get("what"),
                                                                        finding.get("detail"))
                next_health = time.time() + 86400
        except KeyboardInterrupt:
            log.info("Stopped")
            return 0
        except GarminBlocked as exc:
            on_page = isinstance(exc, GarminLoginNeeded) and getattr(settings, "ui_port", None)
            log.warning("%s%s; pausing for 15 minutes", exc, ", or sign in on the web page" if on_page else "")
            if isinstance(exc, GarminLoginNeeded):
                notices.login_needed(settings, store, None, datetime.now(settings.timezone).date(), apply=False)
            if iterations is None:
                wait_after_block(exc)
            continue
        except Exception as exc:
            log.error("%s: %s", type(exc).__name__, exc)
        if iterations is None or count < iterations:
            time.sleep(every_minutes * 60)
    return 0


PAUSE_KEY = polling.PAUSE_KEY
RATE_LIMIT_PAUSE_SECONDS = 900


def intervals_paused(store: Store) -> float:
    """Seconds left of a pause Intervals asked for (HTTP 429); 0 when there is none."""
    return max(0.0, float(store.get_meta(PAUSE_KEY) or 0) - time.time())


def pause_on_rate_limit(store: Store, exc: BaseException) -> float | None:
    """Intervals answered 429: no polls, no full runs for its Retry-After, at least 15 minutes."""
    asked = rate_limited(exc)
    if asked is None:
        return None
    pause = max(asked, RATE_LIMIT_PAUSE_SECONDS)
    store.set_meta(PAUSE_KEY, f"{time.time() + pause:.0f}")
    logging.getLogger("bridge").warning("Intervals.icu asks to slow down (HTTP 429): pausing for %d minutes",
                                        round(pause / 60))
    return pause


def wait_after_block(exc: GarminBlocked, seconds: float = 900) -> bool:
    """15 minutes of quiet after a block; True when a sign-in on the web page ended it early.
    A rate limit (429) always waits the full time."""
    if not isinstance(exc, GarminLoginNeeded):
        time.sleep(seconds)
        return False
    notices.SIGNED_IN.clear()                   # only a sign-in from now on counts
    return notices.SIGNED_IN.wait(seconds)


def _log_to_file(path: Path, level: int) -> None:
    """Also log to a file (the autostarted bridge has no window): 5 MB, three old ones kept."""
    from logging.handlers import RotatingFileHandler

    class PrivateLog(RotatingFileHandler):
        """Every log file – the first and each one after a rotation – readable by its owner only."""

        def _open(self):
            fd = os.open(self.baseFilename, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            return os.fdopen(fd, "a", encoding=self.encoding, errors=self.errors)
    root = logging.getLogger()
    if any(isinstance(h, RotatingFileHandler) and Path(h.baseFilename) == path for h in root.handlers):
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handler = PrivateLog(path, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    try:
        path.chmod(0o600)                       # activities and health data: the owner's only
    except OSError:
        pass
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    handler.setLevel(level)
    root.addHandler(handler)


def _autostart(args: argparse.Namespace, settings: Settings) -> int:
    if settings.env_file is None:
        raise ValueError(f"No settings file: run `{wizard.command_name()} setup` first (or pass --env-file)")
    if args.status:
        state = autostart.status(settings.env_file)
        print(f"Autostart ({state['kind']}): {'set up' if state['installed'] else 'not set up'}")
        if state["detail"] and state["installed"]:
            print(state["detail"])
        if settings.log_file:
            print(f"Log: {settings.log_file}")
        return 0 if state["installed"] else 1
    if args.dry_run:
        steps = autostart.plan(settings.env_file, mode=args.mode or settings.mode)
        print(json.dumps({k: v for k, v in steps.items() if k != "encoding"}, indent=2))
        return 0
    if args.remove:
        autostart.remove(settings.env_file)
        print("Autostart removed; the bridge is stopped. Your settings and data stay where they are.")
        return 0
    steps = autostart.install(settings.env_file, mode=args.mode or settings.mode)
    print("The bridge now runs in the background and starts whenever you log in.")
    if settings.log_file:
        print(f"Log: {settings.log_file}")
    else:
        print("Tip: BRIDGE_LOG_FILE=bridge.log in the settings file gives it a log file.")
    if steps.get("note"):
        print(f"Note: {steps['note']}")
    print(f"Stop it: {wizard.command_name()} autostart --remove")
    return 0


def main(argv: list[str] | None = None) -> int:
    # pythonw.exe (the Windows autostart) has no console: no stdout or stderr to write to
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
    args = build_parser().parse_args(argv)
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s %(message)s")
    log = logging.getLogger("bridge")
    try:
        if args.cmd == "compare-fit":
            print(json.dumps(compare_fit(args.file_a, args.file_b), indent=2))
            return 0
        if args.cmd == "enrich":
            args.intervals_id = args.intervals_id or args.activity
            if not (args.activity_id or args.intervals_id):
                raise ValueError("enrich needs the Intervals activity: enrich i12345678 (or its link)")
        if args.env_file:
            os.environ["BRIDGE_ENV_FILE"] = str(args.env_file)
        if args.cmd == "setup":
            return wizard.run()
        log_file = configured_log_file()
        if log_file:
            _log_to_file(log_file, level)
        settings = Settings.from_env()
        if args.cmd == "autostart":
            return _autostart(args, settings)
        settings.ensure_data_dir()
        if args.cmd == "ui":
            return webui.serve(host=args.host, port=args.port, open_browser=not args.no_browser)
        if args.cmd == "notify-test":
            result = alerts.test(settings)
            if not result:
                print("No alerts set up: BRIDGE_ALERT_EMAIL with BRIDGE_SMTP_PASSWORD, and/or BRIDGE_NTFY_URL "
                      "(setup asks for them).")
                return 1
            for channel, outcome in result.items():
                print(f"{channel}: {outcome}")
            return 0 if all(v == "sent" for v in result.values()) else 1
        if args.cmd == "login":
            garmin = GarminSource(settings.token_dir, settings.garmin_request_delay)
            garmin.login(interactive=True)
            store = Store(settings.data_dir)
            try:
                notices.garmin_ok(store)
            finally:
                store.close()
            print("Garmin authenticated; session tokens saved locally.")
            return 0
        scope = getattr(args, "scope", None)
        lock_scopes = (("backup",) if args.cmd == "backup"         # the past, next to a running bridge
                       else ("activities", "wellness") if scope in (None, "all")
                       and args.cmd in ("sync", "run", "watch")
                       else ("wellness",) if scope == "wellness"
                       else ("health",) if args.cmd == "health"
                       # gear touches only the gear field: next to a running `run`, only not twice at once
                       else ("gear",) if args.cmd == "gear"
                       # one activity on request: next to a running `run`, writes are idempotent
                       else ("enrich",) if args.cmd in ("enrich", "swap")
                       else ("imready4",) if args.cmd == "imready4"
                       else ("hrvtrend",) if args.cmd == "hrvtrend"
                       # read-only diagnosis must work next to a running `run`: no shared state written
                       else () if args.cmd in ("status", "snapshot-account", "gap")
                       else ("activities",))
        locks = ExitStack()
        held: list[str] = []
        for lock_scope in lock_scopes:
            try:
                locks.enter_context(single_instance(settings.data_dir, (lock_scope,)))
                held.append(lock_scope)
            except InstanceBusy as exc:
                if args.cmd not in ("watch", "sync"):
                    locks.close()
                    raise
                # A backfill holds this scope for hours: the scheduled runs do what is free and
                # come back later (no failure, no OnFailure alert).
                log.info("%s; %s skipped this time", exc, lock_scope)
        if lock_scopes and not held:
            locks.close()
            return 0
        if args.cmd == "sync" and scope == "all" and held != ["activities", "wellness"]:
            args.scope = held[0]
        with locks:
            store = Store(settings.data_dir)
            try:
                if args.cmd == "status" and args.check:
                    ok, why = liveness(settings, store)
                    print(("healthy: " if ok else "unhealthy: ") + why)
                    return 0 if ok else 1
                if args.cmd == "status":
                    print(json.dumps({"pending_garmin_activity_ids": store.pending_activities(),
                                      "failed_activities": store.failed_activities()}, indent=2))
                    return 0
                if args.cmd == "reset-pending":
                    if not args.activity_id.isdecimal() or store.activity_status(args.activity_id) != "pending":
                        raise ValueError("Activity must have numeric ID and pending status")
                    store.record_activity(args.activity_id, "downloaded")
                    print(f"Pending lock cleared locally for {args.activity_id}. Next sync will recheck remote.")
                    return 0
                if args.cmd == "health":
                    intervals = (IntervalsClient(settings.intervals_api_key, settings.intervals_athlete_id)
                                 if settings.intervals_api_key and settings.mode != "backup" else None)
                    garmin = GarminSource(settings.token_dir, settings.garmin_request_delay)
                    report = probe(settings, garmin, intervals, store)
                    print(json.dumps(report, indent=2))
                    return report["exit"]
                if args.cmd == "setup-charts":
                    intervals = IntervalsClient(settings.intervals_api_key, settings.intervals_athlete_id)
                    print(json.dumps(setup_charts(intervals, apply=args.apply), indent=2))
                    return 0
                if args.cmd == "setup-fields":
                    intervals = IntervalsClient(settings.intervals_api_key, settings.intervals_athlete_id)
                    fields = intervals.provision_fields(apply=args.apply)
                    print(json.dumps({"apply": args.apply, "missing_codes": fields}, indent=2))
                    return 0
                if args.cmd in ("imready4", "hrvtrend"):
                    intervals = IntervalsClient(settings.intervals_api_key, settings.intervals_athlete_id)
                    today = datetime.now(settings.timezone).date()
                    if args.date and args.start:
                        raise ValueError("--date or --from, not both")
                    start = date.fromisoformat(args.start or args.date or today.isoformat())
                    end = date.fromisoformat(args.end) if args.end else (today if args.start else start)
                    if start > end:
                        raise ValueError("--from must not be after --to")
                    feature = imready4 if args.cmd == "imready4" else hrvtrend
                    out = feature.run_days(intervals, start, end, apply=args.apply, overwrite=args.overwrite,
                                            compare=args.compare, details=args.details, today=today)
                    print(json.dumps(out, indent=2, ensure_ascii=False))
                    return 0
                if args.cmd == "gear":
                    intervals = IntervalsClient(settings.intervals_api_key, settings.intervals_athlete_id)
                    start = date.fromisoformat(args.start) if args.start else None
                    end = date.fromisoformat(args.end) if args.end else None
                    if args.prefer == "evidence":
                        log.info("Reading the paired sensors of the archived rides (once; cached in gear-sensors.json)")
                    plan = build_gear_plan(store, intervals, start=start, end=end, policy=args.prefer,
                                           explicit=args.map, create=args.create,
                                           include_retired=args.include_retired, config=load_gear_config(store),
                                           sensors=GearSensors(store) if args.prefer == "evidence" else None)
                    out = gear_report(plan, details=args.details)
                    out["apply"] = args.apply
                    if args.apply:
                        done = apply_gear_plan(plan, intervals)
                        before = load_gear_config(store) or {}
                        # --create is remembered: the scheduled runs create new Garmin gear too (until --no-create)
                        create = (args.create or bool(before.get("create"))) and not args.no_create
                        retired = create and (args.include_retired or bool(before.get("include_retired")))
                        save_gear_config(store, plan, done.pop("ids"), create=create, include_retired=retired)
                        out["result"] = done
                        out["scheduled_runs_create_new_gear"] = create
                    if args.recalc:
                        corrected = {x["gear"] for x in (out.get("result") or {}).get("totals_corrected", [])}
                        out["totals"] = [x for x in refresh_gear_totals(
                            intervals, sorted({t for t in plan["pairs"].values() if not t.startswith("new:")}),
                            apply=args.apply) if x["gear"] not in corrected]
                    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
                    return 0
                garmin = GarminSource(settings.token_dir, settings.garmin_request_delay)
                if args.cmd not in ("watch", "sync", "run") and not (
                        args.cmd == "backfill" and (args.from_archive or args.intake)):
                    garmin.login(interactive=False)      # fail early for one-off commands
                # (the scheduled ones log in on their first Garmin request, so a run with nothing to do costs none)
                if args.cmd == "snapshot-account":
                    raw = garmin.account_snapshot(
                        history_start=date.fromisoformat(args.history_from) if args.history_from else None,
                        today=datetime.now(settings.timezone).date())
                    path = store.save_account_snapshot(raw)
                    print(json.dumps({"file": str(path), "sections": sorted(raw["data"]), "errors": raw["errors"],
                                      "garmin_requests": garmin.requests}, indent=2))
                    return 0
                if args.cmd == "gap":
                    intervals = IntervalsClient(settings.intervals_api_key, settings.intervals_athlete_id)
                    gid = args.activity_id
                    path = store.fit_path(gid)
                    if not path.is_file():
                        store.atomic_save(path, garmin.original_fit(gid))
                    original = path.read_bytes()
                    if args.intervals_id:
                        remote_id = args.intervals_id
                        found = intervals.activity(remote_id)
                        matched = {"id": remote_id, "how": "given"}
                    else:
                        activity = garmin.activity(gid)
                        day = datetime.fromisoformat(activity["startTimeGMT"]).date()
                        candidates = intervals.activities(day - timedelta(days=1), day + timedelta(days=1))
                        found = match_activity(activity, candidates)
                        if found is None:
                            print(json.dumps({"activity_id": gid, "matched": None,
                                              "candidates_seen": len(candidates),
                                              "report": gap_report(original, None)}, indent=2, default=str))
                            return 0
                        remote_id = str(found["id"])
                        matched = {"id": remote_id, "how": "matched", "source": found.get("source"),
                                   "external_id": found.get("external_id"), "start_date": found.get("start_date")}
                    partner = extract_original_fit(intervals.activity_file(remote_id))
                    kept = exact_match([found], gid) is not None      # only the copy of exactly this recording
                    if kept:                                          # serves the enrichment later
                        store.atomic_save(store.partner_path(gid), partner)
                    print(json.dumps({"activity_id": gid, "matched": matched,
                                      "partner_file": str(store.partner_path(gid)) if kept else None,
                                      "report": gap_report(original, partner)}, indent=2, default=str))
                    return 0
                if args.cmd == "enrich":
                    intervals = IntervalsClient(settings.intervals_api_key, settings.intervals_athlete_id)
                    gid, iid = args.activity_id, intervals_id_from(args.intervals_id) if args.intervals_id else None
                    if gid is None:
                        gid, iid = resolve_one(intervals, args.intervals_id)
                    plan = enrich_activity(gid, garmin, intervals, store, apply=args.apply, intervals_id=iid,
                                           refresh_own=True, recheck=True)
                    if plan.get("intervals_id"):
                        plan["intake"] = restore_intake(gid, plan["intervals_id"], intervals, store, apply=args.apply)
                    print(json.dumps(plan, indent=2, default=str))
                    return 0
                if args.cmd == "swap":
                    from .swap import swap_activity
                    intervals = IntervalsClient(settings.intervals_api_key, settings.intervals_athlete_id)
                    gid, iid = resolve_one(intervals, args.activity)
                    out = swap_activity(gid, iid, garmin, intervals, store, apply=args.apply, window_hours=None)
                    print(json.dumps(out, indent=2, default=str))
                    return 0 if out["outcome"] in ("planned", "swapped", "same") else 1
                if args.cmd == "probe":
                    if args.activity_id:
                        gid = args.activity_id
                    else:
                        today = datetime.now(settings.timezone).date()
                        recent = garmin.activities(today - timedelta(days=7), today)
                        if not recent:
                            raise ValueError("No recent Garmin activities; provide --activity-id")
                        gid = str(sorted(recent, key=lambda a: a.get("startTimeGMT", ""))[-1]["activityId"])
                    path = store.fit_path(gid)
                    fit = garmin.original_fit(gid)
                    store.atomic_save(path, fit)
                    print(json.dumps({"activity_id": gid, "file": str(path), "bytes": len(fit),
                                      "sha256": sha256(fit)}, indent=2))
                    return 0
                if args.cmd == "run":
                    mode = args.mode or settings.mode
                    # a Garmin poll (upload mode) costs three requests, an Intervals poll one small one
                    # Intervals asked for no minute polling: every 5 minutes by default
                    poll = args.poll_seconds or settings.poll_seconds or (600 if mode == "upload" else 300)
                    every = args.sync_minutes or (settings.backup_hours * 60 if mode == "backup" else 30)
                    if poll < (300 if mode == "upload" else 30) or every < 5:
                        raise ValueError("--poll-seconds too small for this mode, or --sync-minutes < 5")
                    if settings.ui_port:
                        try:
                            webui.start_in_background(settings.ui_port, settings.ui_host)
                        except (ValueError, OSError) as exc:       # the bridge runs on without its page
                            log.error("No web page: %s", exc)
                    if mode == "backup":
                        return run_backup_loop(settings, store, every_minutes=every, iterations=args.iterations)
                    return run_loop(settings, store, apply=args.apply, poll_seconds=poll,
                                    sync_minutes=every, iterations=args.iterations, mode=mode)
                if args.cmd == "backup":
                    if args.start:
                        start = date.fromisoformat(args.start)
                        end = date.fromisoformat(args.end) if args.end else datetime.now(settings.timezone).date()
                        if start > end:
                            raise ValueError("--from must not be after --to")
                        scopes = {"activities", "wellness", "account"} if args.scope == "all" else {args.scope}
                        log.info("Backup %s..%s (%s), %.1fs pause; days already backed up are skipped", start, end,
                                 ", ".join(sorted(scopes)), args.pause)
                        out = backup_range(settings, garmin, store, start=start, end=end, scopes=scopes,
                                           endpoints=parse_endpoints(args.endpoints), pause=args.pause,
                                           force=args.force)
                    else:
                        out = backup_once(settings, garmin, store)
                    out["garmin_requests"] = garmin.requests
                    store.record_run("backup", out)
                    log.info("Backup: %s", describe_backup(out))
                    print(json.dumps(out, indent=2))
                    return 0
                if args.cmd in ("watch", "sync") and intervals_paused(store):
                    print(json.dumps({"apply": getattr(args, "apply", False),
                                      "skipped": "Intervals asked for a pause (HTTP 429)"}))
                    return 0
                started = time.time()
                if args.cmd == "watch":
                    # the timer starts it every minute; when to look is decided here
                    due, why = polling.due(settings, store, started, polling.fast_seconds(settings))
                    if not due:
                        print(json.dumps({"apply": args.apply, "skipped": why}))
                        return 0
                if args.cmd == "watch":
                    intervals = IntervalsClient(settings.intervals_api_key, settings.intervals_athlete_id)
                    result: dict = {}
                    if "activities" in held:
                        result = watch_once(settings, garmin, intervals, store, apply=args.apply)
                    wellness = (wellness_check(settings, garmin, intervals, store, apply=args.apply)
                                if "wellness" in held else {})
                    record = {"apply": args.apply, "garmin_requests": garmin.requests, **result}
                    if wellness.get("intervals_checks") or wellness.get("days_from_archive"):
                        record["wellness"] = wellness
                    store.record_run("watch", record, started=started)
                    print(json.dumps({"apply": args.apply, "watch": result, "wellness": wellness}, indent=2))
                    return 0
                if args.cmd == "backfill":
                    garmin_only = args.archive_only or (args.scope == "wellness" and args.wellness_files)
                    intervals = (None if garmin_only else
                                 IntervalsClient(settings.intervals_api_key, settings.intervals_athlete_id))
                    start = date.fromisoformat(args.start)
                    end = date.fromisoformat(args.end) if args.end else datetime.now(settings.timezone).date()
                    if start > end:
                        raise ValueError("--from must not be after --to")
                    if args.scope == "wellness" and args.wellness_files:
                        # only finished days: today's files are fetched with its final read, complete
                        today = datetime.now(settings.timezone).date()
                        days = [start + timedelta(days=n) for n in range((end - start).days + 1)
                                if start + timedelta(days=n) < today]
                        log.info("Backfill wellness files: %d days, one Garmin request each, %.1fs pause",
                                 len(days), args.pause)
                        out = sync_wellness_files(settings, garmin, store, days=days, pause_seconds=args.pause)
                    elif args.scope == "wellness":
                        days = [start + timedelta(days=n) for n in range((end - start).days + 1)]
                        if args.from_archive:
                            log.info("Backfill wellness from the archive: %d days, no Garmin requests", len(days))
                        else:
                            log.info("Backfill wellness: %d days, %d Garmin requests each, %.1fs pause", len(days),
                                     len(parse_endpoints(args.endpoints) or DAY_ENDPOINTS), args.pause)
                        endpoints = parse_endpoints(args.endpoints) or tuple(DAY_ENDPOINTS)
                        rewrite = {c.strip() for c in (args.rewrite or "").split(",") if c.strip()}
                        if any(not c.startswith("Garmin") for c in rewrite):
                            raise ValueError("--rewrite accepts the bridge's own Garmin... fields only")
                        out = sync_wellness(settings, garmin, intervals, store, apply=args.apply,
                                            force=args.force_wellness, days=days,
                                            pause_seconds=0 if args.from_archive else args.pause,
                                            endpoints=endpoints, from_archive=args.from_archive,
                                            rewrite=rewrite or None)
                    elif args.intake:
                        log.info("Backfill intake %s..%s from the archived summaries, no Garmin requests", start, end)
                        out = backfill_intake(intervals, store, start=start, end=end, apply=args.apply)
                    elif args.mode == "upload" and not args.archive_only:
                        log.info("Backfill activities %s..%s, UPLOAD mode: originals of activities Intervals "
                                 "does not have, %.1fs pause", start, end, args.pause)
                        out = sync_activities(settings, garmin, intervals, store, apply=args.apply,
                                              allow_upload=True, date_range=(start, end), pause_seconds=args.pause)
                    else:
                        log.info("Backfill activities %s..%s, one original download per new activity, %.1fs pause%s",
                                 start, end, args.pause, " (archive only)" if args.archive_only else "")
                        out = sync_enrich(settings, garmin, intervals, store, apply=args.apply,
                                          date_range=(start, end), pause_seconds=args.pause,
                                          archive_only=args.archive_only)
                    store.record_run("backfill", {"apply": args.apply, "scope": args.scope, "from": str(start),
                                                  "to": str(end), "garmin_requests": garmin.requests, **out})
                    print(json.dumps({"apply": args.apply, "scope": args.scope, "from": str(start), "to": str(end),
                                      "result": out}, indent=2))
                    return 0
                if args.cmd == "sync":
                    for name in ("activity_days", "wellness_days"):
                        v = getattr(args, name)
                        if v is not None and not 1 <= v <= 30:
                            raise ValueError(f"--{name.replace('_','-')} must be 1..30")
                    intervals = IntervalsClient(settings.intervals_api_key, settings.intervals_athlete_id)
                    results: dict = {"apply": args.apply, "scope": args.scope}
                    results["mode"] = args.mode
                    if args.scope in ("all", "activities") and args.mode == "enrich":
                        results["activities"] = sync_enrich(settings, garmin, intervals, store,
                            apply=args.apply, activity_days=args.activity_days,
                            scan_interval_minutes=max(args.activity_interval, 0))
                        if not results["activities"].get("scan_skipped"):
                            gear_result = _gear_recent(settings, store, intervals, apply=args.apply)
                            if gear_result:
                                results["gear"] = gear_result
                    elif args.scope in ("all", "activities"):
                        if args.apply and not args.allow_activity_upload:
                            log.warning("Activity uploads disabled; add --allow-activity-upload ONLY AFTER "
                                        "disabling Garmin auto-activity sync")
                        results["activities"] = sync_activities(settings, garmin, intervals, store,
                            apply=args.apply, allow_upload=args.allow_activity_upload,
                            activity_days=args.activity_days)
                        # gear as in enrich mode, for the uploaded originals
                        gear_result = _gear_recent(settings, store, intervals, apply=args.apply)
                        if gear_result:
                            results["gear"] = gear_result
                    if args.scope in ("all", "wellness"):
                        results["wellness"] = sync_wellness(settings, garmin, intervals, store,
                            apply=args.apply, wellness_days=args.wellness_days,
                            force=args.force_wellness)
                    results["garmin_requests"] = garmin.requests
                    store.record_run("sync", results)
                    print(json.dumps(results, indent=2))
                    return 0
            except GarminLoginNeeded:
                if getattr(args, "apply", False) and settings.intervals_api_key:
                    intervals = IntervalsClient(settings.intervals_api_key, settings.intervals_athlete_id)
                    notices.login_needed(settings, store, intervals, datetime.now(settings.timezone).date(),
                                         apply=True)
                raise
            except Exception as exc:
                pause_on_rate_limit(store, exc)                 # the timers then skip their runs too
                raise
            finally:
                store.close()
    except Exception as exc:
        # Do not echo possibly credential-bearing exception messages for requests failures.
        if exc.__class__.__module__.startswith("requests"):
            log.error("HTTP/API request failed: %s", type(exc).__name__)
        else:
            log.error("%s: %s", type(exc).__name__, str(exc))
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
