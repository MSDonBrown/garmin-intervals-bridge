"""Keep FIT files as bytes; never reconstruct device streams from JSON.

Decoding uses Garmin's official FIT SDK. Two properties of that decoder matter
for this project and are relied upon here: messages whose number is not in the
public profile are kept under their numeric key, and fields a profile message
does not know are kept under their numeric field ID. The data Garmin strips
from partner downloads (stamina, recovery time, sweat loss, training status)
lives exactly in those unknown messages and fields.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import io
import struct
import zipfile
from pathlib import Path
from typing import Any

from garmin_fit_sdk import Decoder, Stream


class InvalidFIT(ValueError):
    pass


# Size guard for anything we are asked to decode. Device activity files are a
# few hundred kB; the guard only blocks absurd inputs such as ZIP bombs.
MAX_FIT_BYTES = 32 * 1024 * 1024


def fit_segments(data: bytes) -> list[bytes]:
    """The FIT files inside a download: usually one, sometimes several back to back.

    Garmin originals can be "chained": a second complete FIT file follows the first
    (the device appended another file to the same recording). Each segment carries its
    own header, data size and CRC; the download is valid when every segment is.
    """
    segments: list[bytes] = []
    pos = 0
    while pos < len(data):
        if len(data) - pos < 14:
            if pos:
                raise InvalidFIT(f"FIT length mismatch: expected {pos}, got {len(data)}")   # trailing bytes
            raise InvalidFIT("File is shorter than FIT header/trailer")
        size = data[pos]
        if size not in (12, 14):
            raise InvalidFIT(f"Unexpected FIT header size {size}")
        if data[pos + 8:pos + 12] != b".FIT":
            raise InvalidFIT("FIT magic header missing")
        declared = struct.unpack_from("<I", data, pos + 4)[0]
        if declared > MAX_FIT_BYTES:
            raise InvalidFIT(f"FIT segment declares {declared} bytes, more than {MAX_FIT_BYTES // 2**20} MB")
        end = pos + size + declared + 2  # FIT CRC after data
        if end > len(data):
            raise InvalidFIT(f"FIT length mismatch: expected {end}, got {len(data)}")
        segments.append(data[pos:end])
        pos = end
    return segments


def validate_fit(data: bytes) -> None:
    """Structural check followed by the SDK's CRC check of header and data, per segment."""
    segments = fit_segments(data)
    if not segments:
        raise InvalidFIT("Empty download: no FIT file in it")
    for segment in segments:
        decoder = Decoder(Stream.from_byte_array(bytearray(segment)))
        if not decoder.is_fit():
            raise InvalidFIT("Not a FIT file according to the SDK")
        if not decoder.check_integrity():
            raise InvalidFIT("FIT CRC check failed")


def extract_original_fit(download: bytes) -> bytes:
    """The ORIGINAL Garmin Connect export is normally a ZIP containing a FIT."""
    if zipfile.is_zipfile(io.BytesIO(download)):
        with zipfile.ZipFile(io.BytesIO(download)) as z:
            entries = [i for i in z.infolist() if not i.is_dir() and i.filename.lower().endswith(".fit")]
            if len(entries) != 1:
                raise InvalidFIT(f"Expected exactly one FIT in archive, found {len(entries)}")
            # No extraction to disk: prevents zip-slip, oversized ZIP bombs, etc.
            entry = entries[0]
            if entry.file_size > MAX_FIT_BYTES:
                raise InvalidFIT(f"Unexpectedly large FIT (>{MAX_FIT_BYTES // 2**20} MB)")
            with z.open(entry) as f:
                data = f.read(MAX_FIT_BYTES + 1)
    else:
        data = download
    if len(data) > MAX_FIT_BYTES:
        raise InvalidFIT(f"Unexpectedly large FIT (>{MAX_FIT_BYTES // 2**20} MB)")
    validate_fit(data)
    return data


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def decode_fit(data: bytes, *, checked: bool = False) -> tuple[dict[str, list[dict]], list]:
    """Decode to {message_name: [messages]} plus the SDK's non-fatal error list.

    Timestamps stay numeric (FIT epoch seconds) so results are JSON-safe.
    Heart-rate merging is off: the record stream must stay what the device wrote.
    The CRC is checked once – here, or (`checked`) already when the file was downloaded;
    in pure Python it costs about a third of the decoding.
    """
    normalised: dict[str, list[dict]] = {}
    errors: list = []
    if not checked:
        validate_fit(data)
    for segment in fit_segments(data):           # a chained file: every segment's messages, in order
        decoder = Decoder(Stream.from_byte_array(bytearray(segment)))   # a fresh stream: the check consumed its own
        messages, segment_errors = decoder.read(convert_datetimes_to_dates=False, merge_heart_rates=False,
                                                enable_crc_check=False)
        errors.extend(segment_errors)
        for key, items in messages.items():
            # Profile messages arrive as "<name>_mesgs"; unknown ones as their number.
            name = key[:-6] if key.endswith("_mesgs") else key
            normalised.setdefault(name, []).extend(items)
    return normalised, errors


def _sample(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return list(value[:5])
    if isinstance(value, bytes):
        return value[:16].hex()
    return value


def fit_inventory(path: Path) -> dict:
    """Message/field inventory, separating profile-known from Garmin-internal data.

    `unknown_fields` and `unknown_messages` carry a sample value each, which is
    what you need to put a name to a numeric ID when comparing a device
    original with the file a partner received.
    """
    content = path.read_bytes()
    messages, errors = decode_fit(content)
    counts: dict[str, int] = {}
    fields: dict[str, list[str]] = {}
    unknown_messages: dict[str, int] = {}
    unknown_fields: dict[str, dict[str, Any]] = {}
    developer_fields: dict[str, list[str]] = {}
    for name, items in sorted(messages.items()):
        counts[name] = len(items)
        if name.isdecimal():
            unknown_messages[name] = len(items)
        seen: set[str] = set()
        dev: set[str] = set()
        samples: dict[str, Any] = {}
        for message in items:
            for key, value in message.items():
                if key == "developer_fields" and isinstance(value, dict):
                    dev.update(str(k) for k in value)
                    continue
                label = str(key)
                seen.add(label)
                if isinstance(key, int) and value is not None and label not in samples:
                    samples[label] = _sample(value)
        fields[name] = sorted(seen)
        if samples:
            unknown_fields[name] = dict(sorted(samples.items(), key=lambda kv: int(kv[0])))
        if dev:
            developer_fields[name] = sorted(dev)
    return {
        "bytes": len(content),
        "sha256": sha256(content),
        "messages": counts,
        "fields": fields,
        "unknown_messages": unknown_messages,
        "unknown_fields": unknown_fields,
        "developer_fields": developer_fields,
        "decode_errors": [str(e) for e in errors],
    }


def compare_fit(first: Path, second: Path) -> dict:
    """What does file A carry that file B lacks, and vice versa?

    Used to prove that a Garmin Connect original holds data the partner API
    copy does not. Unknown numeric IDs are compared like named fields.
    """
    a, b = fit_inventory(first), fit_inventory(second)
    kinds = sorted(set(a["messages"]) | set(b["messages"]))

    def only(x: dict, y: dict) -> dict[str, list[str]]:
        return {k: sorted(set(x["fields"].get(k, [])) - set(y["fields"].get(k, [])))
                for k in kinds if set(x["fields"].get(k, [])) - set(y["fields"].get(k, []))}

    return {
        "file_a": {"name": str(first), "bytes": a["bytes"], "sha256": a["sha256"]},
        "file_b": {"name": str(second), "bytes": b["bytes"], "sha256": b["sha256"]},
        "same_bytes": a["sha256"] == b["sha256"],
        "message_count_differences": {
            k: {"a": a["messages"].get(k, 0), "b": b["messages"].get(k, 0)}
            for k in kinds if a["messages"].get(k) != b["messages"].get(k)
        },
        "only_in_a_fields": only(a, b),
        "only_in_b_fields": only(b, a),
        "only_in_a_developer_fields": {
            k: sorted(set(a["developer_fields"].get(k, [])) - set(b["developer_fields"].get(k, [])))
            for k in kinds if set(a["developer_fields"].get(k, [])) - set(b["developer_fields"].get(k, []))
        },
        "unknown_field_samples_a": a["unknown_fields"],
        "unknown_field_samples_b": b["unknown_fields"],
    }


# ---- a day's original wellness files ----

MAX_WELLNESS_MEMBERS = 5000                 # a busy day is ~120 files
MAX_WELLNESS_BYTES = 128 * 1024 * 1024      # uncompressed, all members together
FIT_EPOCH = 631065600                       # 1989-12-31T00:00:00Z in Unix seconds
SNAPSHOT_FIELDS = {                         # session field -> name in the index
    "avg_heart_rate": "hr_avg", "min_heart_rate": "hr_min", "max_heart_rate": "hr_max",
    "rmssd_hrv": "hrv_rmssd", "sdrr_hrv": "hrv_sdrr", "enhanced_avg_respiration_rate": "respiration_avg",
    "avg_spo2": "spo2_avg", "avg_stress": "stress_avg", "total_elapsed_time": "duration_s",
}


def _is_health_snapshot(session: dict) -> bool:
    name = str(session.get("sport_profile_name") or "")
    return "snapshot" in name.lower() or session.get("sport") in (60, "60")


def wellness_bundle_index(data: bytes) -> dict:
    """Check a day's wellness download (a ZIP of the device's original FIT files) and describe it.

    Every member is CRC-checked; damaged ones are listed, not dropped, because the ZIP is
    kept exactly as Garmin delivered it. Health Snapshots (two-minute recordings stored
    as an activity file) are summarised so later mappings need not decode the files again.
    Nothing personal from the files (the user profile message carries the name) goes into
    the index.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValueError("Garmin wellness download is not a ZIP") from exc
    members = [i for i in archive.infolist() if not i.is_dir()]
    if len(members) > MAX_WELLNESS_MEMBERS or sum(i.file_size for i in members) > MAX_WELLNESS_BYTES:
        raise ValueError("Garmin wellness download is implausibly large")
    kinds: dict[str, int] = {}
    invalid: list[str] = []
    snapshots: list[dict] = []
    for info in members:
        name = Path(info.filename).name
        # "<file id>_<KIND>.fit", where the kind itself may contain underscores (SLEEP_DATA, HRV_STATUS)
        kind = name.split("_", 1)[1].rsplit(".", 1)[0].upper() if "_" in name else "OTHER"
        kinds[kind] = kinds.get(kind, 0) + 1
        content = archive.read(info)
        try:
            validate_fit(content)
        except ValueError:
            invalid.append(name)
            continue
        if kind != "ACTIVITY":
            continue
        messages, _ = decode_fit(content)
        for session in messages.get("session", []):
            if not _is_health_snapshot(session):
                continue
            start = session.get("start_time")
            entry: dict = {"file": name, "start_utc": (
                _dt.datetime.fromtimestamp(start + FIT_EPOCH, _dt.timezone.utc).isoformat(timespec="seconds")
                if isinstance(start, (int, float)) else None)}
            for field, key in SNAPSHOT_FIELDS.items():
                value = session.get(field)
                if isinstance(value, (int, float)) and value == value:          # not NaN
                    entry[key] = round(float(value), 2)
            snapshots.append(entry)
    snapshots.sort(key=lambda s: s.get("start_utc") or "")
    return {"files": len(members), "bytes": len(data), "sha256": sha256(data), "kinds": dict(sorted(kinds.items())),
            "invalid": invalid, "health_snapshots": snapshots}
