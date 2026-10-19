from test_fit import build_fit

from garmin_intervals_bridge.enrich import (
    align_stream,
    gap_report,
    load_field_mappings,
    match_activity,
    plan_scalars,
    plan_streams,
)

GARMIN = {"activityId": 42, "startTimeGMT": "2026-10-07 08:00:00", "duration": 3600.0}


def test_match_by_external_id_wins_regardless_of_source():
    remote = [{"id": "i1", "external_id": "garmin:42", "source": "STRAVA"}]
    assert match_activity(GARMIN, remote)["id"] == "i1"
    # A bare number is Garmin's id only when the official sync wrote it (Strava ids are numbers too).
    assert match_activity(GARMIN, [{"id": "i1", "external_id": "42", "source": "GARMIN_CONNECT"}])["id"] == "i1"
    assert match_activity(GARMIN, [{"id": "i1", "external_id": "42"}]) is None


def test_match_recognises_manual_upload_file_names():
    remote = [{"id": "i9", "source": "UPLOAD", "external_id": "42_ACTIVITY.fit",
               "start_date": "2026-10-07T08:00:00Z"}]
    assert match_activity(GARMIN, remote)["id"] == "i9"
    # A different activity whose ID merely starts with the same digits must not match.
    assert match_activity({**GARMIN, "activityId": 4}, remote) is None


def test_match_requires_garmin_source_start_and_duration():
    ok = {"id": "i2", "source": "GARMIN_CONNECT", "start_date": "2026-10-07T08:01:30Z", "elapsed_time": 3590}
    wrong_source = {**ok, "id": "i3", "source": "STRAVA"}
    too_late = {**ok, "id": "i4", "start_date": "2026-10-07T08:05:00Z"}
    too_long = {**ok, "id": "i5", "elapsed_time": 5400}
    assert match_activity(GARMIN, [wrong_source, too_late, too_long]) is None
    assert match_activity(GARMIN, [wrong_source, ok])["id"] == "i2"


def test_match_fails_closed_without_usable_timestamps():
    assert match_activity({"activityId": 42}, [{"id": "i1", "source": "GARMIN_CONNECT"}]) is None
    naive = {"id": "i1", "source": "GARMIN_CONNECT", "start_date": "2026-10-07T08:00:00"}
    assert match_activity(GARMIN, [naive]) is None


def test_short_activities_use_the_absolute_duration_floor():
    garmin = {**GARMIN, "duration": 300.0}
    remote = [{"id": "i6", "source": "GARMIN_CONNECT", "start_date": "2026-10-07T08:00:00Z", "elapsed_time": 345}]
    assert match_activity(garmin, remote)["id"] == "i6"


def test_gap_report_lists_session_scalars_and_whole_messages_only_in_original():
    original = build_fit(records=3, training_effect=3.2)
    partner = build_fit(records=3, training_effect=None)
    report = gap_report(original, partner)
    assert report["comparable"] is True and report["same_bytes"] is False
    assert report["scalar_candidates"] == {"total_anaerobic_training_effect": 1.1,
                                           "total_training_effect": 3.2}
    assert report["stream_candidates"] == []
    assert report["messages_only_in_original"] == {}
    assert report["unknown_ids_needing_names"] == {}


def test_gap_report_without_partner_is_an_inventory_not_a_gap():
    report = gap_report(build_fit(records=2), None)
    assert report["comparable"] is False and report["partner"] is None
    assert "total_training_effect" in report["session_fields"]
    assert "stream_candidates" not in report


def test_gap_report_counts_message_differences():
    report = gap_report(build_fit(records=5), build_fit(records=2))
    assert report["message_count_differences"]["record"] == {"original": 5, "partner": 2}


# ---- writing the gap back ----

ITEMS = [
    {"type": "ACTIVITY_FIELD", "content": {"code": "AerobicEffect", "fit_session_field": "total_training_effect"}},
    {"type": "ACTIVITY_FIELD", "content": {"code": "Sweatloss", "fit_session_field": "178"}},
    {"type": "ACTIVITY_FIELD", "content": {"code": "RecoveryTime", "fit_session_field": "140.9",
                                           "script": "activity.isNew ? activity.RecoveryTime / 60 : activity.RecoveryTime"}},
    {"type": "ACTIVITY_FIELD", "content": {"code": "VO2MaxGarmin", "fit_session_field": "140.7",
                                           "script": "activity.isNew ? activity.VO2MaxGarmin * 3.5 / 65536 : activity.VO2MaxGarmin"}},
    {"type": "ACTIVITY_FIELD", "content": {"code": "ActiveCalories", "fit_session_field": None,
                                           "script": "total = activity.calories; ..."}},
    {"type": "ACTIVITY_FIELD", "content": {"code": "TrainingEffectSelect", "fit_session_field": "188", "type": "select",
                                           "options": [{"value": 1.0}, {"value": 2.0}, {"value": 3.0}]}},
    {"type": "ACTIVITY_FIELD", "content": {"code": "LTPaceDetected", "fit_session_field": "140.16",
                                           "script": "activity.isNew ? (activity.LTPaceDetected == 0 ? NaN : activity.LTPaceDetected / 36) :  (activity.LTPaceDetected == 0 ? NaN : activity.LTPaceDetected)"}},
    {"type": "ACTIVITY_FIELD", "content": {"code": "LTHRdetected", "fit_session_field": "140.14",
                                           "script": "activity.LTHRdetected == 0 ? NaN : activity.LTHRdetected"}},
    {"type": "ACTIVITY_FIELD", "content": {"code": "Weird", "fit_session_field": "181",
                                           "script": "activity.isNew ? someOtherThing(activity.Weird) : activity.Weird"}},
    {"type": "ACTIVITY_STREAM", "content": {"code": "Stamina", "script": "{\n for (let m of icu.fit.record) {\n let f = m.f_138\n if (f) data.setAt(m.timestamp.value, f.value)\n }\n}"}},
    {"type": "ACTIVITY_STREAM", "content": {"code": "GarminGCT", "fit_record_field": "stance_time"}},
    {"type": "ACTIVITY_STREAM", "content": {"code": "GarminGASpeed", "script": "{\n  for (let m of icu.fit.record) {\n    let f = m.f_140\n    if (f) data.setAt(m.timestamp.value, f.value/1000)\n  }\n}"}},
    {"type": "ACTIVITY_STREAM", "content": {"code": "Odd", "script": "for (let m of icu.fit.record) { let f = m.f_141; if (f) data.setAt(m.timestamp.value, Math.sqrt(f.value)) }"}},
    {"type": "ACTIVITY_STREAM", "content": {"code": "Battery", "script": "for (let m of icu.fit) { if (m._num !== 104) continue; ... }"}},
    {"type": "INPUT_FIELD", "content": {"code": "BodyBatteryMax", "type": "numeric"}},
]


def test_mappings_come_from_the_athletes_own_definitions():
    m = load_field_mappings(ITEMS)
    by_code = {x.code: x for x in m.scalars}
    assert by_code["AerobicEffect"].source == "session:total_training_effect" and by_code["AerobicEffect"].convert == ()
    assert by_code["Sweatloss"].source == "session:178"
    assert by_code["RecoveryTime"].source == "mesg:140.9" and by_code["RecoveryTime"].convert == (("/", 60.0),)
    assert by_code["VO2MaxGarmin"].convert == (("*", 3.5), ("/", 65536.0))
    assert "ActiveCalories" not in by_code            # computed by Intervals, not a FIT field
    assert m.unsupported["Weird"] == "script with unknown semantics"
    assert m.unsupported["Battery"] == "stream script does more than copy one record field"
    streams = {x.code: (x.record_field, x.convert) for x in m.streams}
    assert streams == {"Stamina": (138, ()), "GarminGCT": ("stance_time", ()),
                       "GarminGASpeed": (140, (("/", 1000.0),))}
    assert m.unsupported["Odd"] == "stream script does more than copy one record field"


def test_scalar_plan_converts_and_never_overwrites_without_a_partner_copy():
    messages = {"session": [{"total_training_effect": 3.2, 178: 1240, 188: 2}],
                "140": [{9: 2160, 7: 936228}]}
    m = load_field_mappings(ITEMS)
    plan = plan_scalars(messages, {"AerobicEffect": 2.9, "Sweatloss": None, "RecoveryTime": 0}, m)
    assert plan["writes"] == {"Sweatloss": 1240.0, "TrainingEffectSelect": 2.0,
                              "VO2MaxGarmin": round(936228 * 3.5 / 65536, 4)}
    assert plan["kept_existing"] == {"AerobicEffect": 2.9, "RecoveryTime": 0}
    assert plan["replaced_filtered"] == {}
    assert set(plan["absent_in_original"]) == {"LTPaceDetected", "LTHRdetected"}   # message 140 lacks 14/16 here


def test_scalar_plan_replaces_values_that_cannot_come_from_data():
    original = {"session": [{"total_training_effect": 3.2, 188: 2}], "140": [{9: 2160, 7: 936228}]}
    partner = {"session": [{"total_training_effect": 3.2}]}       # message 140 stripped, 188 stripped
    m = load_field_mappings(ITEMS)
    activity = {"AerobicEffect": 3.2, "RecoveryTime": 0, "VO2MaxGarmin": 0.0, "TrainingEffectSelect": None}
    plan = plan_scalars(original, activity, m, partner)
    assert plan["kept_existing"] == {"AerobicEffect": 3.2}       # source present in partner: real data
    assert plan["replaced_filtered"] == {"RecoveryTime": {"old": 0, "new": 36.0},
                                         "VO2MaxGarmin": {"old": 0.0, "new": round(936228 * 3.5 / 65536, 4)}}
    assert plan["writes"] == {"RecoveryTime": 36.0, "VO2MaxGarmin": round(936228 * 3.5 / 65536, 4),
                              "TrainingEffectSelect": 2.0}


def test_second_run_writes_nothing_when_values_are_already_there():
    original = {"session": [{178: 7}], "140": [{9: 488, 7: 887982}]}
    partner = {"session": [{}]}
    m = load_field_mappings(ITEMS)
    first = plan_scalars(original, {"Sweatloss": None, "RecoveryTime": 0, "VO2MaxGarmin": 0.0}, m, partner)
    assert set(first["writes"]) == {"Sweatloss", "RecoveryTime", "VO2MaxGarmin"}
    after = {"Sweatloss": 7.0, "RecoveryTime": 8.1333, "VO2MaxGarmin": 47.426}   # as Intervals stores them
    second = plan_scalars(original, after, m, partner)
    assert second["writes"] == {} and second["replaced_filtered"] == {}
    assert second["kept_existing"] == after


def test_zero_guard_scripts_mean_no_value_and_keep_their_conversion():
    m = load_field_mappings(ITEMS)
    by = {x.code: x for x in m.scalars}
    assert by["LTPaceDetected"].convert == (("/", 36.0),) and by["LTPaceDetected"].zero_is_null
    assert by["LTHRdetected"].convert == () and by["LTHRdetected"].zero_is_null
    plan = plan_scalars({"140": [{16: 0, 14: 0}]}, {}, m)
    assert "LTPaceDetected" in plan["absent_in_original"] and "LTHRdetected" in plan["absent_in_original"]
    plan = plan_scalars({"140": [{16: 3600, 14: 165}]}, {}, m)
    assert plan["writes"]["LTPaceDetected"] == 100.0 and plan["writes"]["LTHRdetected"] == 165.0


def test_a_field_from_a_named_fit_message_by_its_number():
    # GwenBZH in the forum: running threshold pace, user_profile (message 3) field 37, kept out of the copy
    item = {"type": "ACTIVITY_FIELD", "content": {"code": "LTPaceDetected", "fit_session_field": "3.37", "script":
            "activity.isNew ? (activity.LTPaceDetected == 0 ? NaN : activity.LTPaceDetected / 36) : "
            "(activity.LTPaceDetected == 0 ? NaN : activity.LTPaceDetected)"}}
    m = load_field_mappings([item])
    original = {"user_profile": [{37: 146, "speed_setting": "metric"}]}
    partner = {"user_profile": [{"speed_setting": "metric"}]}                  # Garmin's copy: field 37 gone
    plan = plan_scalars(original, {"LTPaceDetected": 0}, m, partner)
    assert plan["writes"] == {"LTPaceDetected": 4.0556}                         # 146 / 36 m/s, about 4:07/km
    assert plan_scalars({"140": [{37: 146}]}, {}, m)["writes"] == {}           # another message: not this one


def test_select_fields_only_accept_their_options():
    m = load_field_mappings(ITEMS)
    plan = plan_scalars({"session": [{188: 0}]}, {}, m)
    assert plan["writes"] == {} and plan["rejected_for_select"] == {"TrainingEffectSelect": 0.0}


def test_stream_alignment_is_by_timestamp_not_index():
    records = [{"timestamp": 1000, 138: 90}, {"timestamp": 1001, 138: 89},
               {"timestamp": 1003, 138: 87}]              # second 1002 missing on the device
    data, stats = align_stream(records, [0, 1, 2, 3], 138)
    assert data == [90, 89, None, 87]
    assert stats == {"matched": 3, "points": 4, "non_null": 3}


def test_stream_values_follow_the_scripts_transform_and_refresh_rewrites_own_streams():
    m = load_field_mappings(ITEMS)
    records = [{"timestamp": 1000 + i, 140: 3377, 138: 100} for i in range(4)]
    plan = plan_streams({"record": records}, {"stream_types": ["time"]}, [0, 1, 2, 3], m)
    by = {w["type"]: w["data"] for w in plan["writes"]}
    assert by["GarminGASpeed"] == [3.377] * 4 and by["Stamina"] == [100] * 4
    # Already on the activity -> skipped, unless it is one of ours being refreshed
    present = {"stream_types": ["time", "GarminGASpeed", "Stamina"]}
    assert plan_streams({"record": records}, present, [0, 1, 2, 3], m)["writes"] == []
    again = plan_streams({"record": records}, present, [0, 1, 2, 3], m, refresh={"GarminGASpeed"})
    assert [w["type"] for w in again["writes"]] == ["GarminGASpeed"]


def test_stream_plan_skips_existing_absent_and_misaligned():
    m = load_field_mappings(ITEMS)
    records = [{"timestamp": 1000 + i, 138: 90 - i, "stance_time": 250.0} for i in range(10)]
    messages = {"record": records}
    activity = {"stream_types": ["time", "GarminGCT"]}
    plan = plan_streams(messages, activity, list(range(10)), m)
    assert [w["type"] for w in plan["writes"]] == ["Stamina"]
    assert plan["writes"][0]["custom"] is True and plan["writes"][0]["data"][0] == 90
    assert plan["skipped"]["GarminGCT"] == "already on activity"
    assert plan["skipped"]["GarminGASpeed"] == "not in original"
    # Time stream that does not line up with the records at all -> refuse
    bad = plan_streams(messages, {"stream_types": []}, [5000 + i for i in range(10)], m)
    assert "alignment too poor" in bad["skipped"]["Stamina"]
    # Original without stamina -> nothing to write
    none = plan_streams({"record": [{"timestamp": 1000, "stance_time": 1.0}]}, {"stream_types": []}, [0], m)
    assert none["skipped"]["Stamina"] == "not in original"


def test_an_intervals_link_or_id_names_one_activity_and_its_garmin_original():
    import pytest

    from garmin_intervals_bridge.enrich import garmin_id_from, intervals_id_from, resolve_one
    for text in ("i12345678", "12345678", "https://intervals.icu/activities/i12345678",
                 "https://intervals.icu/activities/i12345678/data?x=1", " i12345678 "):
        assert intervals_id_from(text) == "i12345678", text
    with pytest.raises(ValueError, match="activity id"):
        intervals_id_from("https://intervals.icu/wellness")
    assert garmin_id_from("24544097680") == "24544097680"                  # the official sync
    assert garmin_id_from("24544097680_ACTIVITY.fit") == "24544097680"     # Garmin's export uploaded by hand
    assert garmin_id_from("garmin:24544097680") == "24544097680"           # the bridge's own upload
    assert garmin_id_from("18234567.fit") is None and garmin_id_from(None) is None

    class Strava:
        def activity(self, iid):
            return {"id": iid, "source": "STRAVA", "external_id": None}
    with pytest.raises(ValueError, match="did not come from Garmin"):
        resolve_one(Strava(), "i1")


def test_a_stream_script_counts_only_when_it_copies_one_field_and_nothing_else():
    def stream(script, **extra):
        items = [{"type": "ACTIVITY_STREAM", "content": {"code": "S", "script": script, **extra}}]
        m = load_field_mappings(items)
        return (m.streams[0].record_field, m.streams[0].convert) if m.streams else m.unsupported.get("S")
    plain = "{ for (let m of icu.fit.record) { let f = m.f_138; if (f) data.setAt(m.timestamp.value, f.value / 1000) } }"
    assert stream(plain) == (138, (("/", 1000.0),))
    assert stream(plain.replace("if (f) data", "if (f) { data").replace("/ 1000)", "/ 1000) }")) == (138, (("/", 1000.0),))
    unknown = "stream script does more than copy one record field"
    for script in (
            # scaled before it is written: writing f.value raw would be wrong by 1000
            "for (let m of icu.fit.record) { let f = m.f_138; f.value = f.value / 1000; "
            "if (f) data.setAt(m.timestamp.value, f.value) }",
            # a condition decides which values go in
            "for (let m of icu.fit.record) { let f = m.f_138; if (f && f.value > 0 && f.value < 100) "
            "data.setAt(m.timestamp.value, f.value) }",
            # another field read first, its value written
            "for (let m of icu.fit.record) { let g = m.f_137; let f = m.f_138; if (g) data.setAt(m.timestamp.value, "
            "g.value) }"):
        assert stream(script) == unknown, script
    assert "not certain" in stream(plain, fit_record_field="stamina")          # a FIT field and a script
