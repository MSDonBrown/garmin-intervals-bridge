"""When to look for new activities.

  - BRIDGE_POLL_SECONDS (5 minutes by default) is the pace; inside BRIDGE_POLL_HOURS only.
  - Outside those hours the bridge does not look at all – or, with BRIDGE_POLL_QUIET_SECONDS,
    every so often.
  - For an hour after a new activity, or after "Check now" on the web page, it looks at the
    fast pace again – a second session, the cool-down run – quiet hours or not (silent quiet
    hours stay silent).
  - BRIDGE_POLL_MODE=adaptive learns from the archive when your activities usually end: fast
    then, a middle pace around those hours, the quiet pace (30 minutes by default) otherwise.

The full runs go on regardless, so nothing is missed; it may only come in later. The same
decision serves `run` and the systemd timer (which starts `watch` every minute; a `watch`
that is not due ends at once, without a request).
"""
from __future__ import annotations

import json
import logging
import math
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from . import tuning
from .config import within_hours

_t = tuning.get

log = logging.getLogger(__name__)

HOT_MINUTES = tuning.default("BRIDGE_HOT_MINUTES")   # after a new activity: fast, a second one may follow
NOW_KEY = "poll:now"            # "Check now" on the web page
NEW_KEY = "poll:new"            # upload mode: when a poll or full run last found a new activity
TRIED_KEY = "poll:tried"        # when the last look began – one that failed counts for the pace too
# "Check now" right after a look waits this long (no button hammering)
ASK_GAP = tuning.default("BRIDGE_ASK_GAP_SECONDS")
ONDEMAND_KEY = "ondemand:done"   # when the last on-demand sync (`sync-now`) ended: the timed ones step back
PAUSE_KEY = "intervals:paused_until"    # Intervals answered 429: nothing until then
PLAN_KEY = "poll:plan"          # adaptive: the learned week, worked out once a day
# adaptive, unusual hours, without BRIDGE_POLL_QUIET_SECONDS
QUIET_DEFAULT = tuning.default("BRIDGE_POLL_QUIET_DEFAULT_SECONDS")
SLACK = tuning.default("BRIDGE_POLL_SLACK_SECONDS")   # a timer tick a few seconds early still counts
LEARN_DAYS = tuning.default("BRIDGE_ADAPTIVE_LEARN_DAYS")   # twelve weeks of activities ...
HALF_LIFE_DAYS = tuning.default("BRIDGE_ADAPTIVE_HALF_LIFE_DAYS")   # ... the recent ones counting most
MIN_ACTIVITIES = tuning.default("BRIDGE_ADAPTIVE_MIN_ACTIVITIES")   # before that: the fast pace throughout
FAST_SHARE = tuning.default("BRIDGE_ADAPTIVE_FAST_SHARE")
MIDDLE_SHARE = tuning.default("BRIDGE_ADAPTIVE_MIDDLE_SHARE")
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
# What a `run` in this process actually uses (its --mode and --poll-seconds), for its web page.
ACTIVE: dict = {}


def fast_seconds(settings: Any, mode: str = "enrich") -> int:
    return getattr(settings, "poll_seconds", None) or (600 if mode == "upload" else 300)


def quiet_seconds(settings: Any, fast: int) -> int:
    return max(fast, getattr(settings, "poll_quiet_seconds", None) or _t("BRIDGE_POLL_QUIET_DEFAULT_SECONDS"))


def middle_seconds(fast: int, quiet: int) -> int:
    """Halfway between, on a log scale, in whole minutes: 1 and 30 minutes -> 5."""
    return min(quiet, max(fast, round(math.sqrt(fast * quiet) / 60) * 60))


def span(seconds: float) -> str:
    minutes = round(seconds / 60)
    return f"{round(seconds)} s" if seconds < 60 else "1 min" if minutes == 1 else (
        f"{minutes} min" if minutes < 120 else f"{minutes / 60:.0f} h")


# -- learning --

def activity_end(item: dict) -> datetime | None:
    """When an archived Garmin activity (summary or list entry) ended, in UTC."""
    summary = item.get("summaryDTO") if isinstance(item.get("summaryDTO"), dict) else {}
    start = summary.get("startTimeGMT") or item.get("startTimeGMT")
    seconds = (summary.get("elapsedDuration") or item.get("elapsedDuration") or summary.get("duration")
               or item.get("duration") or 0)
    try:
        began = datetime.fromisoformat(str(start).replace(" ", "T")).replace(tzinfo=timezone.utc)
        return began + timedelta(seconds=float(seconds))
    except (TypeError, ValueError, OverflowError):
        return None


def archived_ends(store: Any, tz: Any, now: float) -> list[tuple[datetime, float]]:
    """(local end, age in days) of the archived activities that ended in the last twelve weeks."""
    folder = store.activity_json_path("0").parent
    ends = []
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return []
    for entry in entries:
        if not entry.name.endswith(".json") or entry.name.endswith(".extras.json"):
            continue
        try:
            with open(entry.path, encoding="utf-8") as fh:
                item = json.load(fh)
        except (OSError, ValueError):
            continue
        end = activity_end(item) if isinstance(item, dict) else None
        if end is None:
            continue
        age = (now - end.timestamp()) / 86400
        if 0 <= age <= _t("BRIDGE_ADAPTIVE_LEARN_DAYS"):
            ends.append((end.astimezone(tz), age))
    return ends


def learn(ends: list[tuple[datetime, float]]) -> str:
    """168 hours of the week (Monday 0:00 first): '2' fast, '1' middle, '0' quiet.

    Each activity counts in the hour it ended, half in the next one (Garmin's import may
    come then) and a quarter in the one before; recent weeks count more. The same hour on
    the other days gets a little too – a run at six on Mondays makes six on a Thursday no
    unusual time. The hours holding 80 % of it all are fast, those up to 97 % middle.
    """
    week = [0.0] * 168
    for end, age in ends:
        weight = 0.5 ** (age / _t("BRIDGE_ADAPTIVE_HALF_LIFE_DAYS"))
        slot = end.weekday() * 24 + end.hour
        week[slot] += weight
        week[(slot + 1) % 168] += 0.5 * weight
        week[(slot - 1) % 168] += 0.25 * weight
    daily = [sum(week[day * 24 + hour] for day in range(7)) / 7 for hour in range(24)]
    score = [week[slot] + 0.3 * daily[slot % 24] for slot in range(168)]
    total, running, tiers = sum(score), 0.0, ["0"] * 168
    for slot in sorted(range(168), key=lambda s: -score[s]):
        if score[slot] <= 0:
            break
        fast_share, middle_share = _t("BRIDGE_ADAPTIVE_FAST_SHARE"), _t("BRIDGE_ADAPTIVE_MIDDLE_SHARE")
        tiers[slot] = "2" if running < fast_share * total else "1" if running < middle_share * total else "0"
        running += score[slot]
    return "".join(tiers)


def learned(settings: Any, store: Any, now: float) -> dict:
    """The learned week {"day", "zone", "activities", "tiers"}; worked out once a day from the
    archive, no requests. "tiers" stays empty while fewer than MIN_ACTIVITIES are known."""
    tz = settings.timezone
    today = datetime.fromtimestamp(now, tz).date().isoformat()
    try:
        cached = json.loads(store.get_meta(PLAN_KEY) or "{}")
    except ValueError:
        cached = {}
    if cached.get("day") == today and cached.get("zone") == tz.key:
        return cached
    ends = archived_ends(store, tz, now)
    plan = {"day": today, "zone": tz.key, "activities": len(ends),
            "tiers": learn(ends) if len(ends) >= _t("BRIDGE_ADAPTIVE_MIN_ACTIVITIES") else ""}
    store.set_meta(PLAN_KEY, json.dumps(plan))
    if plan["tiers"]:
        log.info("Adaptive polling, learned from %d activities: fast %s", len(ends), hours_text(plan["tiers"]))
    else:
        log.info("Adaptive polling: still learning (%d of %d activities in the archive); the fast pace until then",
                 len(ends), _t("BRIDGE_ADAPTIVE_MIN_ACTIVITIES"))
    return plan


def hours_text(tiers: str, level: str = "2") -> str:
    """'Mon-Fri 17-20, Sat 8-12' for the hours of the week at `level`."""
    days = []
    for day in range(7):
        ranges: list[list[int]] = []
        for hour in range(24):
            if tiers[day * 24 + hour] != level:
                continue
            if ranges and ranges[-1][1] == hour:
                ranges[-1][1] = hour + 1
            else:
                ranges.append([hour, hour + 1])
        days.append(" ".join(f"{a}-{b}" for a, b in ranges))
    parts, day = [], 0
    while day < 7:
        last = day
        while last + 1 < 7 and days[last + 1] == days[day]:
            last += 1
        if days[day]:
            parts.append(f"{DAYS[day]}{'-' + DAYS[last] if last > day else ''} {days[day]}")
        day = last + 1
    return ", ".join(parts) or "none"


# -- the decision --

def hot_since(store: Any) -> float:
    """When an activity last came in, or someone asked on the web page; 0 if never."""
    marks = [store.get_meta(key) for key in (NOW_KEY, NEW_KEY)]
    return max([store.last_new_activity() or 0.0] + [float(m) for m in marks if m])


def note_new(store: Any, now: float) -> None:
    """Upload mode found a new activity (the watch's own record covers enrich mode)."""
    store.set_meta(NEW_KEY, f"{now:.0f}")


def note_look(store: Any, now: float) -> None:
    """A look begins: during an outage the next one waits for the pace, not for the next minute."""
    store.set_meta(TRIED_KEY, f"{now:.3f}")


def last_look(store: Any, mode: str = "enrich") -> float | None:
    """The last look for new activities, failed ones too; in upload mode a full run reads Garmin's
    list as well."""
    tried = store.get_meta(TRIED_KEY)
    looks = ([store.last_run("watch"), float(tried) if tried else None]
             + ([store.last_run("sync")] if mode == "upload" else []))
    return max((t for t in looks if t is not None), default=None)


def ondemand_done(store: Any) -> float | None:
    """When the last on-demand sync ended; None if there was none."""
    value = store.get_meta(ONDEMAND_KEY)
    try:
        return float(value) if value else None
    except ValueError:
        return None


def poll_holdoff_until(store: Any) -> float:
    """The polls for new activities pause until then, after an on-demand sync (0: no hold-off)."""
    done = ondemand_done(store)
    return done + _t("BRIDGE_ONDEMAND_POLL_HOLDOFF_SECONDS") if done else 0.0


def sync_holdoff_until(store: Any) -> float:
    """The full runs wait until then, after an on-demand sync (0: no hold-off)."""
    done = ondemand_done(store)
    return done + _t("BRIDGE_ONDEMAND_SYNC_HOLDOFF_MINUTES") * 60 if done else 0.0


def interval(settings: Any, store: Any, now: float, fast: int) -> tuple[int | None, str]:
    """Seconds wanted between two looks at `now`, and why; None: no look at all now."""
    local = datetime.fromtimestamp(now, settings.timezone)
    quiet = getattr(settings, "poll_quiet_seconds", None)
    inside = within_hours(local, getattr(settings, "poll_hours", ()))
    if not inside and quiet is None:
        return None, "outside BRIDGE_POLL_HOURS"
    if now - hot_since(store) < _t("BRIDGE_HOT_MINUTES") * 60:
        return fast, "within the hour after an activity"
    if not inside:
        return quiet_seconds(settings, fast), "quiet hours"
    if getattr(settings, "poll_mode", "fixed") == "adaptive":
        plan = learned(settings, store, now)
        tiers = plan.get("tiers") or ""
        minimum = _t("BRIDGE_ADAPTIVE_MIN_ACTIVITIES")
        if not tiers:
            return fast, f"still learning ({plan.get('activities', 0)} of {minimum} activities)"
        tier = tiers[local.weekday() * 24 + local.hour]
        if tier == "2":
            return fast, "a usual time for an activity"
        quiet_pace = quiet_seconds(settings, fast)
        if tier == "1":
            return middle_seconds(fast, quiet_pace), "near a usual time"
        return quiet_pace, "an unusual time"
    return fast, "the usual pace"


def due(settings: Any, store: Any, now: float, fast: int, mode: str = "enrich") -> tuple[bool, str]:
    """Look for new activities now? And why (not)."""
    last = last_look(store, mode)
    asked = store.get_meta(NOW_KEY)
    if asked and (last is None or (float(asked) > last and now - last >= _t("BRIDGE_ASK_GAP_SECONDS"))):
        return True, "asked on the web page"
    held = poll_holdoff_until(store)
    if now < held:
        return False, f"an on-demand sync just ran: the polls resume in {span(held - now)}"
    wanted, why = interval(settings, store, now, fast)
    if wanted is None:
        return False, why
    if last is None or now - last >= wanted - _t("BRIDGE_POLL_SLACK_SECONDS"):
        return True, why
    return False, f"{why}: every {span(wanted)}, the last {span(now - last)} ago"


def seconds_to_next(settings: Any, store: Any, now: float, fast: int, mode: str = "enrich") -> float | None:
    """How long until the next look is due (0: now); None while none is planned."""
    last = last_look(store, mode)
    asked = store.get_meta(NOW_KEY)
    if asked and last is not None and float(asked) > last:
        return max(0.0, last + _t("BRIDGE_ASK_GAP_SECONDS") - now)
    wanted, _ = interval(settings, store, now, fast)
    if wanted is None:
        return None
    return max(0.0 if last is None else max(0.0, last + wanted - now), poll_holdoff_until(store) - now)


def schedule_text(settings: Any, store: Any, now: float, fast: int) -> str:
    """The plan in one line, for the log and the web page."""
    quiet = getattr(settings, "poll_quiet_seconds", None)
    hours = ", ".join(hours_range(a, b) for a, b in getattr(settings, "poll_hours", ()))
    if getattr(settings, "poll_mode", "fixed") == "adaptive":
        plan = learned(settings, store, now)
        tiers = plan.get("tiers") or ""
        quiet_pace = quiet_seconds(settings, fast)
        minimum = _t("BRIDGE_ADAPTIVE_MIN_ACTIVITIES")
        if tiers:
            text = (f"adaptive: every {span(fast)} {hours_text(tiers)}; every "
                    f"{span(middle_seconds(fast, quiet_pace))} around those hours; otherwise every {span(quiet_pace)}")
        else:
            text = (f"adaptive, still learning ({plan.get('activities', 0)} of {minimum} activities): "
                    f"every {span(fast)}")
    else:
        text = f"every {span(fast)}"
    if hours:
        text += f"; {hours}, otherwise every {span(max(fast, quiet))}" if quiet else f"; only {hours}"
    if (hours and quiet) or getattr(settings, "poll_mode", "fixed") == "adaptive":
        text += f"; every {span(fast)} for an hour after a new activity"
    return text


def hours_range(start: int, end: int) -> str:
    def clock(minute: int) -> str:
        return f"{minute // 60}" + (f":{minute % 60:02d}" if minute % 60 else "")
    return f"{clock(start)}-{clock(end)}"
