"""On-demand sync: `sync-now`, for a person or an agent that knows something new has happened.

    docker exec garmin-intervals-bridge garmin-intervals-bridge sync-now --apply [--wait-new]
    ... sync-now --apply --activity i12345678        # one activity, as soon as Intervals has it
    ... sync-now --apply --scope wellness --day 2026-10-22

It goes to Garmin now – whatever the hours, the activity-scan skip and the wellness refresh
window say – enriches what Intervals holds (activities, gear, wellness), prints ONE JSON result
on stdout and exits with a code the caller can act on:

    0  done            2  pending: nothing new there yet (--wait-new ran out of tries or time)
    1  error           3  paused: Garmin or Intervals.icu asked for a break (retry_after_seconds)
    4  login_needed    5  busy: a running sync did not let go of its part in time

It waits for a running sync (a cycle of `run`, a timer, a backfill) to let go of the part it
needs – the same per-part locks, so the two never overlap – and, once it has written something,
it tells the timed ones to step back: `run` pushes its next full run, and the polls pause, for
BRIDGE_ONDEMAND_SYNC_HOLDOFF_MINUTES / BRIDGE_ONDEMAND_POLL_HOLDOFF_SECONDS (polling.py). Only
a run with --apply does that; a dry run changes nothing and delays nothing.

Nothing listens anywhere: the trigger is a command in the container, `docker exec`.
"""
from __future__ import annotations

import json
import logging
import time
from contextlib import ExitStack
from datetime import date, datetime
from typing import Any, Callable

from . import polling, tuning
from .garmin import GarminBlocked, GarminLoginNeeded, GarminSource
from .intervals import IntervalsClient, rate_limited, request_error
from .store import InstanceBusy, Store, single_instance

log = logging.getLogger("bridge")

LAST_KEY = "ondemand:last"          # the last result, to answer a repeated request without asking Garmin again

EXIT_CODES = {"done": 0, "error": 1, "pending": 2, "paused": 3, "login_needed": 4, "busy": 5}


class Stop(Exception):
    """Ends the sync with a status of its own (not an error)."""

    def __init__(self, status: str, **detail: Any) -> None:
        super().__init__(status)
        self.status, self.detail = status, detail


def scopes_for(args: Any) -> tuple[str, ...]:
    """The parts of the bridge this request works on."""
    if getattr(args, "activity", None) or getattr(args, "garmin_id", None):
        return ("activities",)
    scope = args.scope or ("wellness" if getattr(args, "day", None) else "all")
    return ("activities", "wellness") if scope == "all" else (scope,)


def explicit_target(args: Any) -> bool:
    return bool(getattr(args, "activity", None) or getattr(args, "garmin_id", None) or getattr(args, "day", None))


def _take(data_dir: Any, scopes: tuple[str, ...], stack: ExitStack, deadline: float, clock: Callable[[], float],
          sleep: Callable[[float], None]) -> None:
    """Hold each part; wait (up to the deadline) for a running sync to let go of it."""
    said = False
    for part in scopes:
        while True:
            try:
                stack.enter_context(single_instance(data_dir, (part,)))
                break
            except InstanceBusy:
                if clock() >= deadline:
                    raise Stop("busy", part=part, message=f"a running sync still holds '{part}'") from None
                if not said:
                    log.info("The bridge is working on %s right now; waiting for it to finish", part)
                    said = True
                sleep(2)


def _count(metrics: dict, *keys: str) -> int:
    return sum(int(metrics.get(k) or 0) for k in keys)


def _one_activity(args: Any, settings: Any, garmin: Any, intervals: Any, store: Store) -> dict:
    """One activity on request, as `enrich` does it; 'unmatched' while Intervals has not got it yet."""
    from .enrich import enrich_activity, intervals_id_from, resolve_one
    from .sync import restore_intake
    gid, iid = getattr(args, "garmin_id", None), None
    if gid is None:
        try:
            gid, iid = resolve_one(intervals, args.activity)
        except Exception as exc:          # the official import may lag: Intervals answers 404 until then
            if getattr(getattr(exc, "response", None), "status_code", None) == 404:
                return {"outcome": "unmatched", "intervals_id": intervals_id_from(args.activity)}
            raise
    plan = enrich_activity(str(gid), garmin, intervals, store, apply=args.apply, intervals_id=iid,
                           refresh_own=True, recheck=True)
    if plan.get("intervals_id") and args.apply:
        plan["intake"] = restore_intake(str(gid), plan["intervals_id"], intervals, store, apply=args.apply)
    return {"outcome": plan.get("outcome"), "intervals_id": plan.get("intervals_id"), "garmin_id": str(gid)}


def _activities(args: Any, settings: Any, garmin: Any, intervals: Any, store: Store,
                mode: str) -> tuple[dict, int, bool]:
    """(metrics, how many new things, whether to look again: nothing new and something may still come)."""
    from .cli import _gear_recent
    from .sync import sync_activities, sync_enrich
    if getattr(args, "activity", None) or getattr(args, "garmin_id", None):
        one = _one_activity(args, settings, garmin, intervals, store)
        new = int(one["outcome"] in ("enriched", "planned"))
        return one, new, one["outcome"] == "unmatched"
    days = tuning.get("BRIDGE_ONDEMAND_ACTIVITY_DAYS")
    if mode == "upload":
        metrics = sync_activities(settings, garmin, intervals, store, apply=args.apply, allow_upload=True,
                                  activity_days=days)
        new = _count(metrics, "uploaded", "would_upload")
        waiting = not new and not metrics.get("seen")
    else:
        metrics = sync_enrich(settings, garmin, intervals, store, apply=args.apply, activity_days=days,
                              scan_interval_minutes=0)
        new = _count(metrics, "enriched", "planned", "swapped")
        waiting = not new and (metrics.get("unmatched", 0) > 0 or not metrics.get("seen"))
        if not new and (metrics.get("fields_written") or metrics.get("streams_written")):
            new = 1
    gear = _gear_recent(settings, store, intervals, apply=args.apply)
    if gear:
        metrics = {**metrics, "gear": gear}
    return metrics, new, waiting


def _wellness(args: Any, settings: Any, garmin: Any, intervals: Any, store: Store) -> tuple[dict, int]:
    from .sync import sync_wellness
    if getattr(args, "day", None):
        metrics = sync_wellness(settings, garmin, intervals, store, apply=args.apply, force=True,
                                days=[date.fromisoformat(args.day)])
    else:
        metrics = sync_wellness(settings, garmin, intervals, store, apply=args.apply, force=True,
                                wellness_days=tuning.get("BRIDGE_ONDEMAND_WELLNESS_DAYS"))
    return metrics, _count(metrics, "writes")


def _coalesced(store: Store, args: Any, mode: str, now: float) -> dict | None:
    """The last result, when this request would only repeat it: the same, within the minimum gap."""
    gap = tuning.get("BRIDGE_ONDEMAND_MIN_GAP_SECONDS")
    raw = store.get_meta(LAST_KEY)
    if not raw or not gap or explicit_target(args) or getattr(args, "force", False) or not args.apply:
        return None
    try:
        last = json.loads(raw)
    except ValueError:
        return None
    if (last.get("scope") != list(scopes_for(args)) or last.get("mode") != mode
            or not 0 <= now - float(last.get("finished", 0)) < gap):
        return None
    return {**last["result"], "coalesced": True,
            "message": f"a sync-now finished {round(now - float(last['finished']))} s ago; its result is returned "
                       f"(BRIDGE_ONDEMAND_MIN_GAP_SECONDS={gap}, or --force)"}


def _sync(args: Any, settings: Any, store: Store, garmin: Any, intervals: Any, mode: str, deadline: float,
          clock: Callable[[], float], sleep: Callable[[float], None]) -> dict:
    from .cli import intervals_paused
    scopes = scopes_for(args)
    result: dict = {"scope": list(scopes), "mode": mode}
    retries = tuning.get("BRIDGE_ONDEMAND_RETRIES") if getattr(args, "wait_new", False) else 0
    pause = tuning.get("BRIDGE_ONDEMAND_RETRY_SECONDS")
    todo = set(scopes)
    attempt, total_new = 0, 0
    while True:
        attempt += 1
        waiting = False
        if intervals_paused(store):
            raise Stop("paused", retry_after_seconds=round(intervals_paused(store)),
                       message="Intervals.icu asked for a break (HTTP 429)")
        if "activities" in todo:
            result["activities"], new, again = _activities(args, settings, garmin, intervals, store, mode)
            total_new += new
            waiting = waiting or again
            if not again:
                todo.discard("activities")
        if "wellness" in todo:
            result["wellness"], new = _wellness(args, settings, garmin, intervals, store)
            total_new += new
            # wellness comes after the watch has synced: worth waiting for only when it is all that was asked
            waiting = waiting or (not new and scopes == ("wellness",))
            todo.discard("wellness")
        result["attempts"] = attempt
        result["new"] = total_new
        if not (waiting and not total_new and attempt <= retries):
            break
        if clock() + pause >= deadline:
            result["deadline_reached"] = True
            break
        log.info("Nothing new yet (try %d of %d); looking again in %d s", attempt, retries + 1, pause)
        sleep(pause)
        todo = {s for s in scopes if s == "activities" or scopes == ("wellness",)}
    pending = bool(getattr(args, "wait_new", False)) and waiting and not total_new
    result["status"] = "pending" if pending else "done"
    return result


def run(args: Any, settings: Any, *, garmin: Any = None, intervals: Any = None,
        clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep) -> int:
    """Do the sync, print the JSON result, return the exit code."""
    from .cli import pause_on_rate_limit
    started = clock()
    deadline = started + tuning.get("BRIDGE_ONDEMAND_MAX_SECONDS")
    result: dict = {"apply": bool(args.apply), "status": "error"}
    store = Store(settings.data_dir)
    try:
        if getattr(settings, "mode", "enrich") == "backup":
            raise ValueError("sync-now has nothing to do in backup mode (use `backup`)")
        if not settings.intervals_api_key:
            raise ValueError("INTERVALS_API_KEY is not set")
        mode = getattr(args, "mode", None) or getattr(settings, "mode", "enrich") or "enrich"
        scopes = scopes_for(args)
        with ExitStack() as locks:
            wait_until = min(deadline, started + tuning.get("BRIDGE_ONDEMAND_LOCK_WAIT_SECONDS"))
            _take(settings.data_dir, scopes, locks, wait_until, clock, sleep)
            again = _coalesced(store, args, mode, clock())
            if again is not None:
                result = again
            else:
                garmin = garmin or GarminSource(settings.token_dir, settings.garmin_request_delay)
                if hasattr(garmin, "login"):
                    garmin.login(interactive=False)        # fail early and clearly: login_needed
                intervals = intervals or IntervalsClient(settings.intervals_api_key, settings.intervals_athlete_id)
                before = getattr(garmin, "requests", 0)
                result = {**result, **_sync(args, settings, store, garmin, intervals, mode, deadline, clock, sleep)}
                result["garmin_requests"] = getattr(garmin, "requests", 0) - before
                result["elapsed_seconds"] = round(clock() - started, 1)
                finished = clock()
                store.record_run("ondemand", {k: v for k, v in result.items() if k != "message"}, started=started)
                if args.apply:
                    store.set_meta(polling.ONDEMAND_KEY, f"{finished:.3f}")     # the timed syncs step back
                    if not explicit_target(args):
                        store.set_meta(LAST_KEY, json.dumps({"finished": finished, "scope": list(scopes), "mode": mode,
                                                             "result": result}, default=str))
                if getattr(garmin, "client", None) is not None:
                    from . import notices
                    notices.garmin_ok(store)
    except Stop as stop:
        result = {**result, "status": stop.status, **stop.detail}
    except GarminLoginNeeded as exc:
        result = {**result, "status": "login_needed", "message": str(exc) or "sign in to Garmin again"}
        if args.apply and settings.intervals_api_key:
            from . import notices
            try:
                notices.login_needed(settings, store, intervals or IntervalsClient(
                    settings.intervals_api_key, settings.intervals_athlete_id),
                    datetime.now(settings.timezone).date(), apply=True)
            except Exception as note_exc:      # the notice is a courtesy; the result still goes out
                log.warning("Login notice not sent: %s", type(note_exc).__name__)
    except GarminBlocked as exc:
        result = {**result, "status": "paused", "message": f"Garmin blocked or rate-limited the bridge: {exc}",
                  "retry_after_seconds": tuning.get("BRIDGE_BLOCK_PAUSE_SECONDS")}
    except Exception as exc:
        paused = pause_on_rate_limit(store, exc)
        if paused is not None:
            result = {**result, "status": "paused", "retry_after_seconds": round(paused),
                      "message": "Intervals.icu asked for a break (HTTP 429)"}
        else:
            http = exc.__class__.__module__.startswith("requests")
            text = request_error(exc) if http else f"{type(exc).__name__}: {exc}"
            log.error("sync-now failed: %s", text)
            result = {**result, "status": "error", "message": text}
        if rate_limited(exc) is not None and paused is None:
            result["status"] = "paused"
    finally:
        store.close()
    result["exit_code"] = EXIT_CODES.get(result["status"], 1)
    print(json.dumps(result, indent=2, default=str))
    return result["exit_code"]
