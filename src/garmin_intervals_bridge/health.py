"""Daily health run: probe both services and look for errors that would otherwise stay silent.

Most failures are handled on purpose and do not end a run: an activity that
fails gets a backoff, a Garmin endpoint that errors is noted in the raw
archive, a field Garmin stops delivering simply stops appearing. This run
turns those into one daily finding list. Exit 2 (alert mail) only for
findings that need a human; everything else is reported with exit 0.

Findings that alert
  - a service failing longer than BRIDGE_STALE_HOURS (probe)
  - no scheduled run recorded for 3 hours (timers dead, unit broken)
  - a Garmin wellness endpoint erroring on each of the last 3 archived days
  - a wellness field Garmin delivered on most of the previous week but on none
    of the last 2 finished days (a changed response shape, a stopped sensor);
    what the athlete logs or starts (weight, food, drink, Health Snapshots)
    is left out, a day without it is a day without logging
  - activities failed twice or more, or uploads pending reconciliation

Informational: what the last day did, a day boundary in UTC, a newer release (notices.py).
Findings that alert also go out by e-mail / push message when set up, at most once a day
(alerts.py).
"""
from __future__ import annotations

import glob
import json
import logging
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from . import alerts, notices
from .autostart import platform
from .config import within_hours
from .mapping import map_wellness

log = logging.getLogger(__name__)

STALE_RUN_HOURS = 3
ENDPOINT_ERROR_DAYS = 3
DROPOUT_BASELINE_DAYS = 7
DROPOUT_MIN_BASELINE = 5
DROPOUT_MISSING_DAYS = 2
# Raw sources that exist only when the athlete logs something (scale, food and drink log) or
# starts it (Health Snapshot): their absence is no dropout.
USER_LOGGED = ("body_composition", "nutrition", "hydration", "health_snapshots")


def _today(settings: Any) -> date:
    tz = getattr(settings, "timezone", None)
    return datetime.now(tz).date() if tz else date.today()


def probe(settings: Any, garmin: Any, intervals: Any, store: Any, *, now: float | None = None) -> dict:
    now = now or time.time()
    stale_after = settings.stale_hours * 3600
    report: dict[str, Any] = {"stale_hours": settings.stale_hours, "services": {}, "findings": [], "exit": 0}
    checks = [("garmin", lambda: garmin.login(interactive=False))]
    if intervals is not None:                          # backup mode has no Intervals to ask
        checks.append(("intervals", lambda: intervals.activities(_today(settings) - timedelta(days=1),
                                                                 _today(settings), fields=["id"], limit=1)))
    for name, call in checks:
        entry: dict[str, Any] = {}
        try:
            call()
            if name == "garmin":
                notices.garmin_ok(store)
            else:
                store.set_meta(f"{name}_last_ok")
            entry.update(ok=True, last_ok_age_hours=0.0)
        except Exception as exc:                       # every failure counts here
            if name == "garmin" and type(exc).__name__ == "GarminLoginNeeded":
                notices.login_needed(settings, store, None, _today(settings), apply=False)
            last_ok = store.meta_updated(f"{name}_last_ok")
            age = (now - last_ok) if last_ok else None
            entry.update(ok=False, error=f"{type(exc).__name__}: {exc}"[:200],
                         last_ok_age_hours=round(age / 3600, 1) if age is not None else None)
            if age is None or age > stale_after:
                entry["stale"] = True
                report["findings"].append({"alert": True, "what": f"{name} unreachable",
                                           "detail": f"no success for {entry['last_ok_age_hours'] or 'ever'} h "
                                                     f"(limit {settings.stale_hours} h): {entry['error']}"})
            else:
                log.warning("%s probe failed (%s); last success %.1f h ago, inside the %d h window",
                            name, type(exc).__name__, age / 3600, settings.stale_hours)
        report["services"][name] = entry
    report["findings"] += silent_error_checks(settings, store, now=now)
    if str(getattr(settings, "timezone_source", "")).startswith("fallback"):
        report["findings"].append({"alert": False, "what": "day boundary in UTC",
                                   "detail": "no BRIDGE_TIMEZONE and no time zone on this computer (a container?): "
                                             "set BRIDGE_TIMEZONE to yours, e.g. Europe/London"})
    update = notices.update_finding(settings, in_container=platform() == "container")
    if update:
        report["findings"].append(update)
    for f in report["findings"]:
        (log.error if f["alert"] else log.warning)("%s: %s", f["what"], f["detail"])
    alerts.health(settings, store, report["findings"], _today(settings).isoformat())
    if any(f["alert"] for f in report["findings"]):
        report["exit"] = 2
    return report


def _watch_stale_hours(settings: Any, now: float) -> float | None:
    """How long without a look counts as "not running": at least STALE_RUN_HOURS, twice the
    slowest planned pace; None while no look is expected (quiet hours without a quiet pace,
    or too soon after they ended)."""
    from .polling import QUIET_DEFAULT
    slowest = max(getattr(settings, "poll_seconds", None) or 300, getattr(settings, "poll_quiet_seconds", None) or 0,
                  QUIET_DEFAULT if getattr(settings, "poll_mode", "fixed") == "adaptive" else 0)
    hours = max(STALE_RUN_HOURS, 2 * slowest / 3600)
    ranges = getattr(settings, "poll_hours", ())
    if ranges and getattr(settings, "poll_quiet_seconds", None) is None:
        tz = getattr(settings, "timezone", None)
        steps = int(hours * 4) + 1                    # every quarter of an hour back
        if not all(within_hours(datetime.fromtimestamp(now - 900 * n, tz), ranges) for n in range(steps)):
            return None
    return hours


FULL_RUN_MINUTES = 30            # `run`'s full runs (and the systemd timer's)


def liveness(settings: Any, store: Any, *, now: float | None = None) -> tuple[bool, str]:
    """Does the bridge still work? For a container's healthcheck (`status --check`), without a
    request: a run recorded within twice the time it takes between two – the full runs come
    every 30 minutes whatever the poll plan (backup mode: every BRIDGE_BACKUP_HOURS). A break
    Intervals asked for, a Garmin sign-in it waits for (a restart would not help) and a bridge
    that has not finished its first run yet all count as working."""
    now = now or time.time()
    if float(store.get_meta("intervals:paused_until") or 0) > now:
        return True, "paused at Intervals.icu's request"
    if store.get_meta(notices.WAITING_KEY) or not any(Path(settings.token_dir).glob("*.json")):
        return True, "waiting for a Garmin sign-in (the web page, `login`)"
    runs = {c: store.last_run(c) for c in ("sync", "watch", "backup")}
    backup_mode = getattr(settings, "mode", "enrich") == "backup"
    last = runs["backup"] if backup_mode else max((t for t in (runs["sync"], runs["watch"]) if t), default=None)
    if last is None:
        return True, "no run finished yet"
    limit = 2 * (getattr(settings, "backup_hours", 12) * 3600 if backup_mode else FULL_RUN_MINUTES * 60) + 300
    age = now - last
    if age > limit:
        return False, f"no run for {age / 60:.0f} minutes (expected within {limit / 60:.0f})"
    return True, f"last run {age / 60:.0f} minutes ago"


def silent_error_checks(settings: Any, store: Any, *, now: float | None = None) -> list[dict]:
    now = now or time.time()
    findings: list[dict] = []

    # 1) Are the timers alive at all? (backup mode: its runs every BRIDGE_BACKUP_HOURS instead)
    every = getattr(settings, "backup_hours", 12)
    last = {c: max((r["started"] for r in store.recent_runs(24 * 14, c)), default=0) for c in ("backup", "sync")}
    backup_mode = last["backup"] > last["sync"]          # whichever ran last is the mode it runs in
    watch_hours = _watch_stale_hours(settings, now)
    expected = ((("backup", "backup", 2 * every + 1),) if backup_mode else
                ((("watch", "watch for new activities", watch_hours),) if watch_hours else ())
                + (("sync", "30-minute sync", STALE_RUN_HOURS),))
    for command, label, hours in expected:
        runs = store.recent_runs(hours, command)
        if not runs and store.recent_runs(24 * 14, command):      # ran before, not any more
            findings.append({"alert": True, "what": f"{label} not running",
                             "detail": f"no {command} run recorded in the last {hours} h"})

    # 2) Activities that keep failing, uploads waiting for a human
    failed = [f for f in store.failed_activities() if (f.get("attempts") or 0) >= 2]
    if failed:
        findings.append({"alert": True, "what": f"{len(failed)} activities failing repeatedly",
                         "detail": "; ".join(f"{f['garmin_id']}: {f['error']}" for f in failed[:5])})
    pending = store.pending_activities()
    if pending:
        findings.append({"alert": True, "what": f"{len(pending)} uploads pending reconciliation",
                         "detail": ", ".join(pending[:10])})

    # 3) Garmin wellness endpoints erroring day after day; fields that silently stopped
    raw_dir = os.path.join(str(settings.data_dir), "raw")
    days = sorted(glob.glob(os.path.join(raw_dir, "2*.json")))[-DROPOUT_BASELINE_DAYS - DROPOUT_MISSING_DAYS - 1:]
    snapshots = []
    for path in days:
        try:
            with open(path, encoding="utf-8") as fh:
                snapshots.append(json.load(fh))
        except (OSError, ValueError):
            continue
    if len(snapshots) >= ENDPOINT_ERROR_DAYS:
        recent = snapshots[-ENDPOINT_ERROR_DAYS:]
        for key in sorted({k for s in recent for k in (s.get("errors") or {})}):
            if all(key in (s.get("errors") or {}) for s in recent):
                findings.append({"alert": True, "what": f"Garmin endpoint '{key}' failing",
                                 "detail": f"error on each of the last {ENDPOINT_ERROR_DAYS} archived days: "
                                           f"{recent[-1]['errors'][key]}"})
    today = datetime.fromtimestamp(now, settings.timezone).date()
    # today's file holds only what has been read so far (the night, before the scale and the evening)
    finished = [s for s in snapshots if s["date"] < today.isoformat()][-DROPOUT_BASELINE_DAYS - DROPOUT_MISSING_DAYS:]
    if len(finished) > DROPOUT_MISSING_DAYS:
        profile = getattr(settings, "wellness_profile", "recommended")

        def fields_of(snapshot: dict) -> set[str]:
            day = date.fromisoformat(snapshot["date"])
            data = {k: v for k, v in (snapshot.get("data") or {}).items() if k not in USER_LOGGED}
            native, custom = map_wellness({**snapshot, "data": data}, day, today, profile)
            return set(native) | set(custom)

        baseline, latest = finished[:-DROPOUT_MISSING_DAYS], finished[-DROPOUT_MISSING_DAYS:]
        counts: dict[str, int] = {}
        for s in baseline:
            for f in fields_of(s):
                counts[f] = counts.get(f, 0) + 1
        present_lately = set().union(*(fields_of(s) for s in latest))
        dropped = sorted(f for f, c in counts.items()
                         if c >= min(DROPOUT_MIN_BASELINE, len(baseline)) and f not in present_lately)
        if dropped:
            findings.append({"alert": True, "what": f"{len(dropped)} wellness fields stopped arriving from Garmin",
                             "detail": f"present on most of the previous {len(baseline)} days, absent on the last "
                                       f"{DROPOUT_MISSING_DAYS}: {', '.join(dropped)}"})

    # 4) Informational: what the last day of runs did (no alert)
    day_runs = store.recent_runs(24)
    requests = sum(r["metrics"].get("garmin_requests") or 0 for r in day_runs)
    if day_runs and backup_mode:
        backups = [r["metrics"] for r in day_runs if r["command"] == "backup"]
        findings.append({"alert": False, "what": "last 24 h",
                         "detail": f"{len(backups)} backups: {sum(m.get('activities') or 0 for m in backups)} "
                                   f"activities, {sum(m.get('wellness_days') or 0 for m in backups)} days of "
                                   f"wellness, {requests} Garmin requests"})
    elif day_runs:
        def enriched_in(metrics: dict) -> int:
            activities = metrics.get("activities")           # a dict in sync runs, a count in backups
            return metrics.get("enriched") or (activities.get("enriched") if isinstance(activities, dict) else 0) or 0
        enriched = sum(enriched_in(r["metrics"]) for r in day_runs)
        writes = sum((r["metrics"].get("wellness", {}) or {}).get("writes", 0)
                     + (r["metrics"].get("writes", 0) if r["command"] == "backfill" else 0) for r in day_runs)
        findings.append({"alert": False, "what": "last 24 h",
                         "detail": f"{len(day_runs)} runs, {enriched} activities enriched, "
                                   f"{writes} wellness days written, {requests} Garmin requests"})
    return findings
