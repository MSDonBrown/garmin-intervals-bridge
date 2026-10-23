"""Every scalar timer and threshold of the bridge, as a setting.

Each one is an environment variable (or a line in the settings file, `.env`), with the value
the bridge always used as its default. An empty or missing variable means the default; a value
outside its range, or no number, stops the bridge at start with the variable's name – the same
way the other settings behave. The registry below is the single list: the code asks `get(name)`
when it needs a value (not at import, so a settings file read at start counts), and the tests
keep `.env.example` and the README table in step with it.

Lists of times (the morning slots, the retry ladders) are not here: they stay in the code.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Tunable:
    name: str
    default: int | float
    low: int | float
    high: int | float
    unit: str
    text: str
    group: str

    @property
    def is_float(self) -> bool:
        return isinstance(self.default, float)


def _all() -> list[Tunable]:
    out: list[Tunable] = []

    def add(group: str, name: str, default: int | float, low: int | float, high: int | float, unit: str,
            text: str) -> None:
        out.append(Tunable(name, default, low, high, unit, text, group))

    g = "On-demand sync (`sync-now`)"
    add(g, "BRIDGE_ONDEMAND_SYNC_HOLDOFF_MINUTES", 30, 0, 1440, "minutes",
        "After an on-demand sync the next full run of `run` waits at least this long (0 = no hold-off)")
    add(g, "BRIDGE_ONDEMAND_POLL_HOLDOFF_SECONDS", 900, 0, 86400, "seconds",
        "After an on-demand sync the regular polls for new activities pause this long (\"Check now\" still works)")
    add(g, "BRIDGE_ONDEMAND_MIN_GAP_SECONDS", 60, 0, 3600, "seconds",
        "A `sync-now` without a named activity or day, this soon after the last one, returns its result")
    add(g, "BRIDGE_ONDEMAND_LOCK_WAIT_SECONDS", 600, 0, 7200, "seconds",
        "How long `sync-now` waits for a running sync (a cycle of `run`, a timer, a backfill) to let go of its part")
    add(g, "BRIDGE_ONDEMAND_MAX_SECONDS", 900, 60, 7200, "seconds",
        "Longest a whole `sync-now` may take, waiting and retrying included (then it reports and stops)")
    add(g, "BRIDGE_ONDEMAND_RETRIES", 5, 0, 50, "tries",
        "`sync-now --wait-new`: how often to look again when nothing new is there yet (the watch may still be syncing)")
    add(g, "BRIDGE_ONDEMAND_RETRY_SECONDS", 120, 10, 3600, "seconds",
        "`sync-now --wait-new`: pause between those looks")
    add(g, "BRIDGE_ONDEMAND_ACTIVITY_DAYS", 2, 1, 30, "days",
        "`sync-now` looks this many days back for activities (today included)")
    add(g, "BRIDGE_ONDEMAND_WELLNESS_DAYS", 2, 1, 30, "days",
        "`sync-now` reads wellness for this many days (today included), ignoring the refresh window")

    g = "The run loop (`run`)"
    add(g, "BRIDGE_SYNC_MINUTES", 30, 5, 1440, "minutes",
        "Minutes between the full runs (`--sync-minutes` wins; backup mode uses `BRIDGE_BACKUP_HOURS`)")
    add(g, "BRIDGE_ACTIVITY_SCAN_MINUTES", 120, 0, 1440, "minutes",
        "Enrich mode: the full run skips its Garmin activity scan while the last one is younger (0 = never skip)")
    add(g, "BRIDGE_TICK_SECONDS", 60, 5, 3600, "seconds",
        "How often the loop asks whether a poll or a full run is due")
    add(g, "BRIDGE_PART_BUSY_RETRY_SECONDS", 300, 30, 3600, "seconds",
        "A full run that left a part out because a backfill works on it comes back this soon")
    add(g, "BRIDGE_BUSY_WAIT_SECONDS", 300, 0, 86400, "seconds",
        "A backfill waits this long for a running sync to finish with its part")
    add(g, "BRIDGE_RATE_LIMIT_PAUSE_SECONDS", 900, 60, 86400, "seconds",
        "Least pause after Intervals.icu answers 429 (longer if it asks for longer)")
    add(g, "BRIDGE_BLOCK_PAUSE_SECONDS", 900, 60, 86400, "seconds",
        "Pause after Garmin blocks, rate-limits or wants a sign-in")
    add(g, "BRIDGE_FAILURE_BACKOFF_SECONDS", 300, 30, 3600, "seconds",
        "A failed full run is tried again after this, doubling with each failure up to the full-run interval")
    add(g, "BRIDGE_HEALTH_HOURS", 24, 1, 168, "hours",
        "Hours between the health probes written to the log")
    add(g, "BRIDGE_HEALTH_FIRST_SECONDS", 300, 0, 86400, "seconds",
        "The first health probe comes this long after the start")

    g = "Polling (`watch`, adaptive mode)"
    add(g, "BRIDGE_HOT_MINUTES", 60, 0, 1440, "minutes",
        "After a new activity: poll at the fast pace this long, a second one may follow")
    add(g, "BRIDGE_ASK_GAP_SECONDS", 30, 0, 3600, "seconds",
        "\"Check now\" right after a look waits this long (no button hammering)")
    add(g, "BRIDGE_POLL_SLACK_SECONDS", 15, 0, 300, "seconds",
        "A look this much earlier than its time still counts as due")
    add(g, "BRIDGE_POLL_QUIET_DEFAULT_SECONDS", 1800, 60, 86400, "seconds",
        "Adaptive mode, unusual hours, without `BRIDGE_POLL_QUIET_SECONDS`: the pace")
    add(g, "BRIDGE_ADAPTIVE_LEARN_DAYS", 84, 7, 365, "days",
        "Adaptive mode learns your usual activity hours from this many days")
    add(g, "BRIDGE_ADAPTIVE_HALF_LIFE_DAYS", 28, 1, 365, "days",
        "... the weight of an activity halves with this many days of age")
    add(g, "BRIDGE_ADAPTIVE_MIN_ACTIVITIES", 15, 1, 1000, "activities",
        "... and it needs this many before it trusts what it learned (until then: the fast pace)")
    add(g, "BRIDGE_ADAPTIVE_FAST_SHARE", 0.8, 0.05, 1.0, "share",
        "... the hours that hold this share of your activity ends get the fast pace")
    add(g, "BRIDGE_ADAPTIVE_MIDDLE_SHARE", 0.97, 0.05, 1.0, "share",
        "... up to this share the middle pace (must be above the fast share); the rest the quiet pace")

    g = "Activities and wellness"
    add(g, "BRIDGE_ARCHIVE_SETTLE_DAYS", 2, 0, 30, "days",
        "Garmin attaches weather and gear to a fresh activity a little later: asked again within this many days")
    add(g, "BRIDGE_INTAKE_SETTLE_HOURS", 12, 0, 168, "hours",
        "Drinks and gear are logged after the ride: Garmin is asked for them again once, this long after the start")
    add(g, "BRIDGE_CATCH_UP_DAYS", 14, 1, 90, "days",
        "After a pause the scheduled runs look back to the last run, at most this far")
    add(g, "BRIDGE_RETRIES_BY_ID", 3, 0, 50, "activities",
        "Per full run: failed activities that left the scan window and are retried by id")
    add(g, "BRIDGE_FINAL_TRIES", 3, 1, 10, "tries",
        "A day with passing endpoint failures counts as final after this many reads (wellness and backup)")
    add(g, "BRIDGE_WELLNESS_CHECK_MINUTES", 10, 1, 1440, "minutes",
        "The wellness pre-check of Intervals (costs no Garmin request) at most this often")
    add(g, "BRIDGE_WELLNESS_SIGNAL_DAYS", 2, 0, 30, "days",
        "Official sleep seen this recently: wait for it instead of asking Garmin")
    add(g, "BRIDGE_GARMIN_NIGHT_DAYS", 7, 1, 60, "days",
        "No night from Garmin for this long: the advice does not wait for one")
    add(g, "BRIDGE_FINAL_ANYWAY_HOUR", 20, 0, 23, "hour",
        "A past day is read at this hour the day after, synced or not")
    add(g, "BRIDGE_READINESS_RETRIES", 2, 0, 10, "tries",
        "Training readiness: tries when Garmin has not computed it yet")
    add(g, "BRIDGE_READINESS_RETRY_MINUTES", 30, 1, 1440, "minutes",
        "Training readiness: minutes between those tries")

    g = "Health checks"
    add(g, "BRIDGE_HEALTH_STALE_RUN_HOURS", 3, 1, 168, "hours",
        "No look or full run for this long (at least) counts as \"not running\"")
    add(g, "BRIDGE_HEALTH_ENDPOINT_ERROR_DAYS", 3, 1, 30, "days",
        "A Garmin endpoint with an error on each of the last this many days is reported")
    add(g, "BRIDGE_HEALTH_DROPOUT_BASELINE_DAYS", 7, 3, 60, "days",
        "Dropout check: the days a field must have been there before")
    add(g, "BRIDGE_HEALTH_DROPOUT_MIN_BASELINE", 5, 1, 60, "days",
        "Dropout check: in at least this many of those")
    add(g, "BRIDGE_HEALTH_DROPOUT_MISSING_DAYS", 2, 1, 14, "days",
        "Dropout check: missing on each of the latest this many days is reported")

    g = "Backup mode"
    add(g, "BRIDGE_BACKUP_FINAL_HOUR", 12, 0, 23, "hour",
        "A day counts as final at this hour of the next day (the watch has synced)")
    add(g, "BRIDGE_BACKUP_LATE_READ_DAYS", 3, 1, 14, "days",
        "A day is read once more this many days after it, for a watch that synced late")
    add(g, "BRIDGE_BACKUP_RECENT_ACTIVITY_DAYS", 3, 1, 30, "days",
        "Each backup run looks for new activities this many days back")
    add(g, "BRIDGE_BACKUP_RECENT_WELLNESS_DAYS", 7, 1, 30, "days",
        "Each backup run considers the wellness of this many past days")
    add(g, "BRIDGE_BACKUP_ACCOUNT_DAYS", 7, 1, 90, "days",
        "The account data (profile, devices, zones ...) is archived this often")
    return out


TUNABLES: dict[str, Tunable] = {t.name: t for t in _all()}


def default(name: str) -> int | float:
    return TUNABLES[name].default


def _parse(spec: Tunable, raw: str) -> int | float:
    kind = "a number" if spec.is_float else "a whole number"
    try:
        value: int | float = float(raw) if spec.is_float else int(raw)
    except ValueError:
        raise ValueError(f"{spec.name} must be {kind} ({spec.unit}), between {spec.low} and {spec.high}") from None
    if not spec.low <= value <= spec.high or value != value:
        raise ValueError(f"{spec.name} must be {kind} ({spec.unit}), between {spec.low} and {spec.high}")
    return value


def get(name: str) -> int | float:
    """The value of a setting now: the environment's, else the default. Raises ValueError when invalid."""
    spec = TUNABLES[name]
    raw = os.environ.get(name, "").strip()
    return _parse(spec, raw) if raw else spec.default


def validate() -> None:
    """Every setting at once, so a typo stops the start and not some run hours later."""
    for name in TUNABLES:
        get(name)
    if get("BRIDGE_ADAPTIVE_FAST_SHARE") >= get("BRIDGE_ADAPTIVE_MIDDLE_SHARE"):
        raise ValueError("BRIDGE_ADAPTIVE_MIDDLE_SHARE must be above BRIDGE_ADAPTIVE_FAST_SHARE")
    if get("BRIDGE_HEALTH_DROPOUT_MIN_BASELINE") > get("BRIDGE_HEALTH_DROPOUT_BASELINE_DAYS"):
        raise ValueError("BRIDGE_HEALTH_DROPOUT_MIN_BASELINE must not exceed BRIDGE_HEALTH_DROPOUT_BASELINE_DAYS")


def groups() -> dict[str, list[Tunable]]:
    out: dict[str, list[Tunable]] = {}
    for spec in TUNABLES.values():
        out.setdefault(spec.group, []).append(spec)
    return out


def env_block() -> str:
    """The `.env.example` section: every setting, with its default."""
    lines = ["# Timers and thresholds, each with the value the bridge always used (see tuning.py and the README,",
             "# Timers and thresholds). Change one here; an empty value means the default."]
    for group, specs in groups().items():
        lines.append(f"# --- {group}")
        for spec in specs:
            lines.append(f"# {spec.text} [{spec.unit}, {spec.low}-{spec.high}]")
            lines.append(f"{spec.name}={spec.default}")
    return "\n".join(lines) + "\n"
