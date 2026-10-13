"""Enrich an officially synced Intervals activity with what Garmin stripped.

The official Garmin -> Intervals sync keeps running. For each Garmin activity
the bridge fetches the device original, finds the Intervals activity the
official sync created, and compares both files. What only the original has
is the gap. Everything here is read-only; writing the gap back is a
separate, explicitly enabled step.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from .fit import decode_fit, sha256
from .times import external_id_names, parse_utc

log = logging.getLogger(__name__)

# Matching tolerances for *writing into* an activity. Tighter than the
# upload blocker in sync.py: a wrong match would put one ride's data on
# another ride.
START_TOLERANCE_SECONDS = 120
DURATION_TOLERANCE = 0.05          # relative
DURATION_TOLERANCE_SECONDS = 60    # absolute floor for short activities


def match_activity(garmin: dict, remote: list[dict]) -> dict | None:
    """Find the Intervals activity created by the official sync for this Garmin activity.

    Accepts a remote activity when its external_id names the Garmin ID, or
    when it comes from Garmin Connect and both start time and duration agree.
    Returns None when nothing matches or when the inputs do not allow a safe
    decision; never guesses.
    """
    gid = str(garmin.get("activityId", ""))
    start = parse_utc(garmin.get("startTimeGMT"), garmin_gmt=True)
    duration = garmin.get("duration")
    if not gid.isdecimal() or start is None:
        return None
    for item in remote:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        source = str(item.get("source") or "").upper()
        external = str(item.get("external_id") or "")
        # A bare number is only Garmin's id when the official sync wrote it; `garmin:<id>` and
        # `<id>_ACTIVITY.fit` are specific enough on their own.
        if external_id_names(external, gid) and (source == "GARMIN_CONNECT" or not external.isdecimal()):
            return item
        if source != "GARMIN_CONNECT":
            continue
        remote_start = parse_utc(item.get("start_date"))
        if remote_start is None:
            continue
        if abs((remote_start - start).total_seconds()) > START_TOLERANCE_SECONDS:
            continue
        # Garmin's `duration` is timer time: compare with moving and elapsed time, either may agree.
        remote_durations = [item.get(k) for k in ("moving_time", "elapsed_time")
                            if isinstance(item.get(k), (int, float))]
        if isinstance(duration, (int, float)) and remote_durations:
            allowed = max(DURATION_TOLERANCE_SECONDS, DURATION_TOLERANCE * float(duration))
            if all(abs(float(r) - float(duration)) > allowed for r in remote_durations):
                continue
        return item
    return None


def _field_sets(messages: dict[str, list[dict]]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for name, items in messages.items():
        keys: set[str] = set()
        for message in items:
            keys.update(str(k) for k in message if k != "developer_fields")
        out[name] = keys
    return out


def _developer_sets(messages: dict[str, list[dict]]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for name, items in messages.items():
        keys: set[str] = set()
        for message in items:
            dev = message.get("developer_fields")
            if isinstance(dev, dict):
                keys.update(str(k) for k in dev)
        if keys:
            out[name] = keys
    return out


def _first_values(items: list[dict], fields: set[str]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for message in items:
        for key, value in message.items():
            label = str(key)
            if label in fields and label not in values and value is not None:
                values[label] = value if not isinstance(value, (list, tuple)) else list(value[:5])
    return values


def _non_null_counts(items: list[dict], fields: set[str]) -> dict[str, int]:
    counts = {f: 0 for f in fields}
    for message in items:
        for key, value in message.items():
            label = str(key)
            if label in counts and value is not None:
                counts[label] += 1
    return counts


def gap_report(original: bytes, partner: bytes | None) -> dict:
    """What does the device original carry that the partner copy lacks?

    With no partner file the report lists the original's inventory and marks
    the comparison as unavailable rather than pretending everything is a gap.
    """
    orig_messages, orig_errors = decode_fit(original)
    orig_fields = _field_sets(orig_messages)
    orig_dev = _developer_sets(orig_messages)
    report: dict[str, Any] = {
        "original": {"bytes": len(original), "sha256": sha256(original),
                     "messages": {k: len(v) for k, v in sorted(orig_messages.items())},
                     "decode_errors": [str(e) for e in orig_errors]},
        "partner": None,
        "comparable": partner is not None,
    }
    if partner is None:
        report["record_fields"] = sorted(orig_fields.get("record", set()))
        report["session_fields"] = sorted(orig_fields.get("session", set()))
        return report

    part_messages, part_errors = decode_fit(partner)
    part_fields = _field_sets(part_messages)
    part_dev = _developer_sets(part_messages)
    report["partner"] = {"bytes": len(partner), "sha256": sha256(partner),
                         "messages": {k: len(v) for k, v in sorted(part_messages.items())},
                         "decode_errors": [str(e) for e in part_errors]}
    report["same_bytes"] = report["original"]["sha256"] == report["partner"]["sha256"]

    # Whole message types that only the original has (workout steps, user
    # profile, sensor settings, Garmin-internal numeric messages ...).
    report["messages_only_in_original"] = {
        name: len(orig_messages[name]) for name in sorted(orig_messages) if name not in part_messages}
    report["message_count_differences"] = {
        name: {"original": len(orig_messages.get(name, [])), "partner": len(part_messages.get(name, []))}
        for name in sorted(set(orig_messages) | set(part_messages))
        if name in part_messages and len(orig_messages.get(name, [])) != len(part_messages[name])}

    # Field-level gaps inside shared message types. Record gaps are stream
    # candidates, session gaps are scalar candidates; both keep sample values
    # so unknown numeric IDs can be named against a known device.
    field_gaps: dict[str, dict[str, Any]] = {}
    for name in sorted(orig_fields):
        if name not in part_fields:
            continue
        missing = orig_fields[name] - part_fields[name]
        if not missing:
            continue
        field_gaps[name] = {
            "fields": sorted(missing, key=lambda f: (f.isdecimal(), int(f) if f.isdecimal() else 0, f)),
            "samples": _first_values(orig_messages[name], missing),
        }
        if name == "record":
            field_gaps[name]["non_null_counts"] = _non_null_counts(orig_messages[name], missing)
    report["field_gaps"] = field_gaps
    report["stream_candidates"] = field_gaps.get("record", {}).get("fields", [])
    report["scalar_candidates"] = {
        f: field_gaps["session"]["samples"].get(f) for f in field_gaps.get("session", {}).get("fields", [])}
    report["developer_fields_only_in_original"] = {
        name: sorted(orig_dev[name] - part_dev.get(name, set()))
        for name in sorted(orig_dev) if orig_dev[name] - part_dev.get(name, set())}
    report["unknown_ids_needing_names"] = {
        name: [f for f in gap["fields"] if f.isdecimal()]
        for name, gap in field_gaps.items() if any(f.isdecimal() for f in gap["fields"])}
    return report


# ---------------------------------------------------------------------------
# Writing the gap back: field mappings come from the athlete's own custom items
# ---------------------------------------------------------------------------
#
# Intervals fills custom activity fields and custom streams from the FIT file
# itself, and every definition says where its value comes from:
#   ACTIVITY_FIELD  content.fit_session_field = "total_training_effect" | "178" | "140.9"
#   ACTIVITY_STREAM content.fit_record_field  = "step_length"
#   ACTIVITY_STREAM content.script            = "... for (let m of icu.fit.record) { let f = m.f_138 ..."
# plus an optional one-off conversion on first import:
#   content.script = "activity.isNew ? activity.RecoveryTime / 60 : activity.RecoveryTime"
# The bridge reuses those definitions verbatim, so it writes exactly the
# fields the athlete has configured, with the same source and the same units.
# Nothing Garmin-specific is hard-coded here.



@dataclass(frozen=True)
class ScalarMapping:
    code: str
    source: str                      # "session:<name>" | "session:<num>" | "mesg:<num>.<field>"
    convert: tuple = ()              # sequence of ("*"|"/", factor) applied to the raw value
    select_values: tuple | None = None   # for "select" fields: the only values the field accepts
    zero_is_null: bool = False           # script treats a raw 0 as "no value" (`x == 0 ? NaN : x`)
    note: str = ""


@dataclass(frozen=True)
class StreamMapping:
    code: str
    record_field: str | int          # profile name or numeric field id in `record`
    convert: tuple = ()              # ("*"|"/", factor) steps the stream script applies to the raw value


@dataclass
class FieldMappings:
    scalars: list[ScalarMapping] = field(default_factory=list)
    streams: list[StreamMapping] = field(default_factory=list)
    unsupported: dict[str, str] = field(default_factory=dict)   # code -> why


def mapping_signature(mappings: FieldMappings) -> str:
    """What the athlete's field definitions currently ask for; a changed signature makes an
    already enriched activity worth a second look (new fields get added, nothing rewritten)."""
    def rows(items):
        return sorted(json.dumps(asdict(m), sort_keys=True, default=str) for m in items)
    payload = {"scalars": rows(mappings.scalars), "streams": rows(mappings.streams)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


_CONVERSION = re.compile(
    r"^\s*activity\.isNew\s*\?\s*activity\.(?P<code>\w+)(?P<ops>(\s*[*/]\s*[0-9.]+)*)\s*:\s*activity\.(?P=code)\s*$")
_RECORD_SCRIPT = re.compile(r"icu\.fit\.record[\s\S]*?m\.f_(\d+)")
# `data.setAt(<ts>, f.value / 1000)`: the value expression the script writes, if it is
# the field value with optional multiplications/divisions by constants.
_RECORD_VALUE = re.compile(r"setAt\(\s*[^,]+,\s*f\.value(?P<ops>(\s*[*/]\s*[0-9.]+)*)\s*\)")


def _parse_conversion(script: str | None, code: str) -> tuple[tuple, bool] | None:
    """Interpret a field script: (conversion steps, zero_is_null), or None if not understood.

    Understood forms, as the athlete's own definitions use them:
      activity.isNew ? activity.X / 60 : activity.X
      activity.X == 0 ? NaN : activity.X
      activity.isNew ? (activity.X == 0 ? NaN : activity.X / 36) : (activity.X == 0 ? NaN : activity.X)
    """
    if not script or not script.strip():
        return (), False
    text = script.strip()
    guard = re.compile(r"\(?\s*activity\." + re.escape(code) + r"\s*==\s*0\s*\?\s*NaN\s*:\s*")
    zero_is_null = guard.search(text) is not None
    if zero_is_null:
        text = guard.sub("", text).replace(")", "").strip()
    if re.fullmatch(r"activity\." + re.escape(code), text):
        return (), zero_is_null
    match = _CONVERSION.match(text)
    if match is None or match.group("code") != code:
        return None
    steps = tuple((op, float(factor)) for op, factor in re.findall(r"([*/])\s*([0-9.]+)", match.group("ops")))
    return steps, zero_is_null


def _number(value: Any) -> float | None:
    """Select options carry their value as a number or as a numeric string."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def load_field_mappings(custom_items: list[dict]) -> FieldMappings:
    """Derive what to write, and from where, out of the athlete's custom item definitions."""
    out = FieldMappings()
    for item in custom_items:
        if not isinstance(item, dict) or not isinstance(item.get("content"), dict):
            continue
        content = item["content"]
        code = content.get("code")
        if not isinstance(code, str) or not code:
            continue
        kind = item.get("type")
        if kind == "ACTIVITY_FIELD":
            src = content.get("fit_session_field")
            if not src:
                continue  # computed from other fields; Intervals evaluates that itself
            parsed = _parse_conversion(content.get("script"), code)
            if parsed is None:
                out.unsupported[code] = "script with unknown semantics"
                continue
            convert, zero_is_null = parsed
            src = str(src).strip()
            if re.fullmatch(r"\d+\.\d+", src):
                source = f"mesg:{src}"
            elif src.isdecimal():
                source = f"session:{src}"
            else:
                source = f"session:{src}"
            select_values = None
            if content.get("type") == "select" and isinstance(content.get("options"), list):
                select_values = tuple(v for o in content["options"]
                                      if isinstance(o, dict) and (v := _number(o.get("value"))) is not None)
            out.scalars.append(ScalarMapping(code, source, convert, select_values, zero_is_null))
        elif kind == "ACTIVITY_STREAM":
            src = content.get("fit_record_field")
            if src:
                src = str(src).strip()
                out.streams.append(StreamMapping(code, int(src) if src.isdecimal() else src))
                continue
            script = content.get("script") or ""
            m = _RECORD_SCRIPT.search(script)
            if m and "_num" not in script:
                value = _RECORD_VALUE.search(script)
                if value is None:
                    out.unsupported[code] = "stream script writes something other than the field value"
                    continue
                steps = tuple((op, float(factor)) for op, factor in
                              re.findall(r"([*/])\s*([0-9.]+)", value.group("ops")))
                out.streams.append(StreamMapping(code, int(m.group(1)), steps))
            elif script.strip():
                out.unsupported[code] = "stream script not limited to a single record field"
    return out


def _lookup(messages: dict[str, list[dict]], source: str) -> Any:
    """Resolve a mapping source against decoded FIT messages; last non-null wins."""
    kind, _, spec = source.partition(":")
    if kind == "mesg":
        mesg, _, fld = spec.partition(".")
        items, key = messages.get(mesg, []), int(fld)
    else:
        items = messages.get("session", [])
        key = int(spec) if spec.isdecimal() else spec
    value = None
    for message in items:
        v = message.get(key)
        if v is not None:
            value = v
    return value


def _convert(value: Any, steps: tuple) -> Any:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    result = float(value)
    for op, factor in steps:
        result = result * factor if op == "*" else result / factor
    return round(result, 4)


def _same_value(existing: Any, value: Any) -> bool:
    if not isinstance(existing, (int, float)) or isinstance(existing, bool):
        return False
    return abs(float(existing) - float(value)) <= 1e-3 * max(1.0, abs(float(value)))


def plan_scalars(messages: dict[str, list[dict]], activity: dict, mappings: FieldMappings,
                 partner_messages: dict[str, list[dict]] | None = None) -> dict:
    """Fields to PUT on the activity: mapped, present in the original, and either empty in
    Intervals or demonstrably filled from a filtered file.

    The second case matters: when Intervals evaluates a field whose FIT source
    Garmin stripped, it may store 0 rather than null. A value that exists in
    Intervals while its source is absent from the partner copy cannot have come
    from real data, so it is replaced. Without a partner copy, existing values
    are left alone.
    """
    writes: dict[str, Any] = {}
    kept: dict[str, Any] = {}
    replaced: dict[str, dict] = {}
    rejected: dict[str, Any] = {}
    absent: list[str] = []
    for m in mappings.scalars:
        raw = _lookup(messages, m.source)
        if raw is None or (m.zero_is_null and raw == 0):
            absent.append(m.code)
            continue
        value = _convert(raw, m.convert)
        if value is None:
            continue
        if m.select_values is not None and value not in m.select_values:
            rejected[m.code] = value             # not one of the field's options
            continue
        existing = activity.get(m.code)
        if _same_value(existing, value):
            kept[m.code] = existing          # already there: a second run must write nothing
            continue
        if existing is not None:
            source_in_partner = partner_messages is not None and _lookup(partner_messages, m.source) is not None
            if partner_messages is None or source_in_partner:
                kept[m.code] = existing          # may be real data: never overwrite
                continue
            replaced[m.code] = {"old": existing, "new": value}
        writes[m.code] = value
    return {"writes": writes, "kept_existing": kept, "replaced_filtered": replaced,
            "rejected_for_select": rejected, "absent_in_original": absent}


def align_stream(records: list[dict], time_stream: list, record_field: str | int,
                 convert: tuple = ()) -> tuple[list, dict]:
    """Values of one record field aligned to the Intervals `time` stream (seconds from start).

    Alignment is by timestamp, not by index, so a record Garmin dropped or
    duplicated cannot shift the whole series.
    """
    stamped = [(r.get("timestamp"), r.get(record_field)) for r in records
               if isinstance(r.get("timestamp"), (int, float))]
    if not stamped or not time_stream:
        return [], {"matched": 0, "points": len(time_stream), "non_null": 0}
    t0 = stamped[0][0]
    by_offset = {int(ts - t0): (_convert(val, convert) if convert else val) for ts, val in stamped}
    data = [by_offset.get(int(t)) if isinstance(t, (int, float)) else None for t in time_stream]
    matched = sum(1 for t in time_stream if isinstance(t, (int, float)) and int(t) in by_offset)
    non_null = sum(1 for v in data if v is not None)
    return data, {"matched": matched, "points": len(time_stream), "non_null": non_null}


# A stream both sides carry, to prove the alignment before anything is written:
# (field in the original's record messages, Intervals stream type, tolerated difference).
_ORIGIN_PAIRS = (("heart_rate", "heartrate", 2), ("cadence", "cadence", 3))


def origin_check(records: list[dict], time_stream: list, remote_streams: list[dict]) -> dict:
    """Guard against writing a stream shifted in time.

    The bridge aligns by timestamp from the original's first record; Intervals built its
    `time` stream from the file it parsed. If the two origins differed by a constant, every
    point would still "match" and the stream would land shifted. So a stream both sides have
    (heart rate, else cadence) is aligned the same way and compared point by point. `ok` is
    None when nothing can be compared; the caller decides whether to proceed.
    """
    for fit_field, stream_type, tolerance in _ORIGIN_PAIRS:
        remote = next((s.get("data") for s in remote_streams
                       if isinstance(s, dict) and s.get("type") == stream_type), None)
        if not remote or not any(r.get(fit_field) is not None for r in records):
            continue
        data, _ = align_stream(records, time_stream, fit_field)
        pairs = [(a, b) for a, b in zip(data, remote)
                 if isinstance(a, (int, float)) and isinstance(b, (int, float))]
        if len(pairs) < 30:
            continue
        mismatched = sum(1 for a, b in pairs if abs(a - b) > tolerance)
        return {"ok": mismatched / len(pairs) <= 0.02, "stream": stream_type,
                "compared": len(pairs), "mismatched": mismatched}
    return {"ok": None, "detail": "no shared stream to compare"}


def plan_streams(messages: dict[str, list[dict]], activity: dict, time_stream: list,
                 mappings: FieldMappings, *, min_alignment: float = 0.95,
                 refresh: set[str] | None = None) -> dict:
    """Custom streams to PUT: mapped, carried by the original, not yet on the activity.

    `refresh` names streams the bridge wrote itself earlier; those are planned
    again even though they exist, so a corrected mapping propagates.
    """
    existing = set(activity.get("stream_types") or []) - set(refresh or ())
    records = messages.get("record", [])
    writes: list[dict] = []
    skipped: dict[str, str] = {}
    for m in mappings.streams:
        if m.code in existing:
            skipped[m.code] = "already on activity"
            continue
        if not any(r.get(m.record_field) is not None for r in records):
            skipped[m.code] = "not in original"
            continue
        data, stats = align_stream(records, time_stream, m.record_field, m.convert)
        if stats["points"] == 0 or stats["matched"] / stats["points"] < min_alignment:
            skipped[m.code] = f"alignment too poor ({stats['matched']}/{stats['points']} points)"
            continue
        writes.append({"type": m.code, "custom": True, "data": data, "_stats": stats})
    return {"writes": writes, "skipped": skipped}


# ---------------------------------------------------------------------------
# One activity, end to end
# ---------------------------------------------------------------------------

GARMIN_EXTERNAL = re.compile(r"(?:garmin:)?(\d{1,20})(?:_.*)?")


def intervals_id_from(text: str) -> str:
    """'i12345', '12345' or an activity link (https://intervals.icu/activities/i12345) -> 'i12345'."""
    found = re.search(r"/activities/(i?\d{1,20})(?![\d])", text) or re.fullmatch(r"\s*(i?\d{1,20})\s*", text)
    if not found:
        raise ValueError("Give an Intervals activity id like i12345678, or the activity's link")
    value = found.group(1)
    return value if value.startswith("i") else f"i{value}"


def garmin_id_from(external_id: object) -> str | None:
    """The Garmin activity id an Intervals activity carries: the official sync stores it bare,
    a manual upload of Garmin's export as `<id>_ACTIVITY.fit`, the bridge's own upload as `garmin:<id>`."""
    if not isinstance(external_id, str):
        return None
    found = GARMIN_EXTERNAL.fullmatch(external_id.strip())
    return found.group(1) if found else None


def resolve_one(intervals: Any, reference: str) -> tuple[str, str]:
    """(Garmin id, Intervals id) of the Intervals activity a person points at."""
    intervals_id = intervals_id_from(reference)
    activity = intervals.activity(intervals_id)
    garmin_id = garmin_id_from(activity.get("external_id"))
    if garmin_id is None:
        raise ValueError(f"Intervals activity {intervals_id} did not come from Garmin "
                         f"(source {activity.get('source') or 'unknown'}); there is no Garmin original for it")
    return garmin_id, intervals_id


def enrich_activity(gid: str, garmin: Any, intervals: Any, store: Any, *, apply: bool,
                    remote_candidates: list[dict] | None = None, intervals_id: str | None = None,
                    mappings: FieldMappings | None = None, refresh_own: bool = False,
                    recheck: bool = False) -> dict:
    """Download (or reuse) the original, find the Intervals activity, plan, optionally write.

    Returns the plan plus an `outcome`:
      unmatched         no Intervals activity yet (the official import may lag; try again later)
      already_enriched  this original was handled before and nothing changed
      nothing_to_add    the partner copy lacks nothing the athlete's fields could take
      planned           writes identified, dry run
      enriched          writes performed

    `recheck` (one activity on request) compares with what Intervals holds now even when
    nothing changed on the bridge's side – after Intervals' "Reprocess file", say, which
    drops everything the bridge added.
    """
    from .fit import decode_fit, extract_original_fit, sha256  # local import keeps fit optional for tests

    path = store.fit_path(gid)
    downloaded = not path.is_file()                 # checked on download (CRC): not again below
    if downloaded:
        store.atomic_save(path, garmin.original_fit(gid))
    original = path.read_bytes()
    digest = sha256(original)

    if intervals_id is None:
        if remote_candidates is None:
            activity = garmin.activity(gid)
            day = parse_utc(activity.get("startTimeGMT"), garmin_gmt=True)
            if day is None:
                raise ValueError("Garmin activity lacks startTimeGMT")
            from datetime import timedelta
            remote_candidates = intervals.activities(day.date() - timedelta(days=1), day.date() + timedelta(days=1))
            found = match_activity(activity, remote_candidates)
        else:
            found = next((c for c in remote_candidates if external_id_names(c.get("external_id"), gid)), None)
        if found is None:
            return {"activity_id": gid, "outcome": "unmatched", "candidates_seen": len(remote_candidates)}
        intervals_id = str(found["id"])

    previous = store.enrichment(gid)
    if mappings is None:
        mappings = load_field_mappings(intervals.custom_items())
    signature = mapping_signature(mappings)
    if (previous and previous["sha256"] == digest and previous["intervals_id"] == intervals_id
            and previous.get("mapping") == signature and not refresh_own and not recheck):
        return {"activity_id": gid, "intervals_id": intervals_id, "outcome": "already_enriched",
                "previous_enrichment": previous}

    remote = intervals.activity(intervals_id)
    partner_file = store.partner_path(gid)
    if previous and previous["intervals_id"] != intervals_id and partner_file.is_file():
        partner_file.unlink()           # the Intervals activity was replaced: its copy is stale
    partner_downloaded = not partner_file.is_file()
    if partner_downloaded:
        store.atomic_save(partner_file, extract_original_fit(intervals.activity_file(intervals_id)))
    messages, _ = decode_fit(original, checked=downloaded)
    partner_messages, _ = decode_fit(partner_file.read_bytes(), checked=partner_downloaded)
    remote_streams = intervals.streams(intervals_id, ["time", "heartrate", "cadence"])
    time_stream = next((st.get("data") for st in remote_streams
                        if isinstance(st, dict) and st.get("type") == "time"), []) or []
    # only into the activity they went to: after a swap Intervals computes them from the original
    refresh = (set(previous["streams"]) if (refresh_own and previous and previous["intervals_id"] == intervals_id)
               else None)
    scalars = plan_scalars(messages, remote, mappings, partner_messages)
    streams = plan_streams(messages, remote, time_stream, mappings, refresh=refresh)
    check = origin_check(messages.get("record", []), time_stream, remote_streams)
    if streams["writes"] and check["ok"] is False:
        # Never write a shifted stream: the alignment could not be reproduced on a stream
        # Intervals already has. Scalars are unaffected.
        log.warning("Activity %s: stream alignment check failed (%s), streams not written", gid, check)
        reason = f"alignment check failed on {check['stream']}"
        streams["skipped"].update({w["type"]: reason for w in streams["writes"]})
        streams["writes"] = []
    streams["origin_check"] = check

    plan: dict = {"activity_id": gid, "intervals_id": intervals_id, "apply": apply,
                  "mappings": {"scalars": len(mappings.scalars), "streams": len(mappings.streams),
                               "unsupported": mappings.unsupported},
                  "fields": scalars,
                  "streams": {"writes": [{k: v for k, v in w.items() if k != "data"} for w in streams["writes"]],
                              "skipped": streams["skipped"], "origin_check": check},
                  "previous_enrichment": previous}
    if not scalars["writes"] and not streams["writes"]:
        plan["outcome"] = "nothing_to_add"
        if apply:
            store.record_enrichment(gid, intervals_id, digest, previous["fields"] if previous else [],
                                    previous["streams"] if previous else [], mapping=signature)
        return plan
    if not apply:
        plan["outcome"] = "planned"
        return plan
    result: dict = {}
    if scalars["writes"]:
        result["fields"] = intervals.update_activity(intervals_id, scalars["writes"]) is not None
    if streams["writes"]:
        body = [{k: v for k, v in w.items() if k != "_stats"} for w in streams["writes"]]
        result["streams"] = intervals.put_streams(intervals_id, body)
    fields_written = sorted(set(scalars["writes"]) | set(previous["fields"] if previous else []))
    streams_written = sorted({w["type"] for w in streams["writes"]} | set(previous["streams"] if previous else []))
    store.record_enrichment(gid, intervals_id, digest, fields_written, streams_written, mapping=signature)
    plan["result"] = result
    plan["outcome"] = "enriched"
    return plan
