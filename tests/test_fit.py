import io
import zipfile

import pytest
from garmin_fit_sdk import Encoder, Profile

from garmin_intervals_bridge.fit import (
    InvalidFIT,
    compare_fit,
    decode_fit,
    extract_original_fit,
    fit_inventory,
    sha256,
    validate_fit,
)

MESG = Profile["mesg_num"]


def build_fit(*, records: int = 2, training_effect: float | None = 3.2) -> bytes:
    """Synthetic but structurally valid FIT with a correct CRC, built with the SDK encoder."""
    enc = Encoder()
    enc.write_mesg({"mesg_num": MESG["FILE_ID"], "type": "activity", "manufacturer": "garmin",
                    "product": 4315, "serial_number": 123456, "time_created": 1000000000})
    for i in range(records):
        enc.write_mesg({"mesg_num": MESG["RECORD"], "timestamp": 1000000000 + i,
                        "heart_rate": 120 + i, "power": 200 + i})
    session = {"mesg_num": MESG["SESSION"], "timestamp": 1000000000 + records,
               "start_time": 1000000000, "sport": "cycling", "total_elapsed_time": float(records)}
    if training_effect is not None:
        session["total_training_effect"] = training_effect
        session["total_anaerobic_training_effect"] = 1.1
    enc.write_mesg(session)
    return enc.close()


def minimal_fit() -> bytes:
    return build_fit(records=0, training_effect=None)


def test_valid_fit_passes_structure_and_crc():
    data = build_fit()
    validate_fit(data)
    assert len(sha256(data)) == 64


def test_fit_extraction_preserves_bytes():
    data = build_fit()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Folder/TEST.FIT", data)
    assert extract_original_fit(buf.getvalue()) == data
    assert extract_original_fit(data) == data


def test_fit_rejects_invalid_header_and_mismatched_size():
    with pytest.raises(InvalidFIT):
        validate_fit(b"not fit")
    with pytest.raises(InvalidFIT, match="mismatch"):
        validate_fit(build_fit() + b"X")


def test_fit_rejects_corrupted_data_via_crc():
    data = bytearray(build_fit())
    data[20] ^= 0xFF  # flip a byte inside the data section; length stays right
    with pytest.raises(InvalidFIT, match="CRC"):
        validate_fit(bytes(data))


def test_fit_rejects_multiple_entries():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("first.fit", minimal_fit())
        z.writestr("second.fit", minimal_fit())
    with pytest.raises(InvalidFIT, match="exactly one"):
        extract_original_fit(buf.getvalue())


def test_decode_names_messages_and_keeps_numeric_timestamps():
    messages, errors = decode_fit(build_fit(records=3))
    assert errors == []
    assert len(messages["record"]) == 3
    assert messages["session"][0]["total_training_effect"] == pytest.approx(3.2)
    assert isinstance(messages["record"][0]["timestamp"], int)


def test_inventory_counts_messages_and_fields(tmp_path):
    path = tmp_path / "a.fit"
    path.write_bytes(build_fit(records=2))
    inv = fit_inventory(path)
    assert inv["messages"]["record"] == 2 and inv["messages"]["session"] == 1
    assert {"heart_rate", "power", "timestamp"} <= set(inv["fields"]["record"])
    assert "total_training_effect" in inv["fields"]["session"]
    assert inv["unknown_messages"] == {} and inv["unknown_fields"] == {}
    assert inv["decode_errors"] == []


def test_compare_reports_what_only_the_original_carries(tmp_path):
    original, partner = tmp_path / "original.fit", tmp_path / "partner.fit"
    original.write_bytes(build_fit(training_effect=3.2))
    partner.write_bytes(build_fit(training_effect=None))
    result = compare_fit(original, partner)
    assert result["same_bytes"] is False
    assert result["only_in_a_fields"] == {
        "session": ["total_anaerobic_training_effect", "total_training_effect"]}
    assert result["only_in_b_fields"] == {}
    assert "record" not in result["message_count_differences"]


def test_chained_fit_files_are_valid_and_decoded_together():
    from garmin_intervals_bridge.fit import decode_fit, fit_segments
    one, two = build_fit(records=3), build_fit(records=2)
    chained = one + two
    assert len(fit_segments(chained)) == 2
    validate_fit(chained)
    messages, _ = decode_fit(chained)
    assert len(messages["record"]) == 5
    with pytest.raises(InvalidFIT):
        validate_fit(one + b"garbage")            # trailing bytes that are not a FIT file


def test_an_empty_or_oversized_download_is_no_fit(monkeypatch):
    from garmin_intervals_bridge import fit
    with pytest.raises(fit.InvalidFIT, match="Empty"):
        fit.extract_original_fit(b"")
    monkeypatch.setattr(fit, "MAX_FIT_BYTES", 10)
    with pytest.raises(fit.InvalidFIT, match="large"):
        fit.extract_original_fit(build_fit(records=3))


def test_the_crc_is_checked_once(monkeypatch):
    from garmin_intervals_bridge import fit
    checks = []
    real = fit.validate_fit
    monkeypatch.setattr(fit, "validate_fit", lambda data: checks.append(1) or real(data))
    data = build_fit(records=3)
    fit.decode_fit(data)
    fit.decode_fit(data, checked=True)                      # just downloaded: checked there
    assert checks == [1]
