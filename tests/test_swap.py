"""Swap mode: Garmin's filtered copy for the original – upload first, delete last, carefully."""
from datetime import date, datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import requests
from test_fit import build_fit
from test_sync_enrich import GarminFake, IntervalsFake

from garmin_intervals_bridge.store import Store
from garmin_intervals_bridge.swap import replace_activity, retry_deletes, swap_activity
from garmin_intervals_bridge.sync import watch_once

NOW = datetime(2026, 10, 8, 9, 0, tzinfo=timezone.utc)


def http_error(status):
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(str(status), response=response)


class SwapIntervals(IntervalsFake):
    """Garmin's official copy i42 (filtered), with the athlete's RPE and a paired workout on it."""

    def __init__(self):
        super().__init__()
        self.activity_obj.update({"source": "GARMIN_CONNECT", "external_id": "42", "name": "Morning Ride",
                                  "description": "", "created": "2026-10-08T08:05:00Z", "icu_rpe": 6,
                                  "paired_event_id": 77, "type": "Ride", "icu_intervals_edited": False,
                                  "GarminCaloriesConsumed": 1200, "Computed": 5, "LTPace": "NaN",
                                  "start_date_local": "2026-10-08T10:00:00"})
        self.comments, self.calls, self.created, self.delete_status = [], [], {}, None
        self.upload_result, self.update_error, self.ignored, self.gone = None, None, set(), set()

    def activities(self, start, end, fields=None, limit=None):
        """The list: a deleted activity is not in it (by its id it still answers)."""
        ids = ([] if "delete i42" in self.calls else ["i42"]) + [k for k in self.created if k not in self.gone]
        return [{"id": k} for k in ids]

    def activity(self, iid):
        if iid in self.created:
            return dict(self.created[iid])
        if iid == "i42" and "delete i42" in self.calls:
            raise http_error(404)
        return dict(self.activity_obj)

    def custom_items(self):
        return super().custom_items() + [
            {"type": "ACTIVITY_FIELD", "content": {"code": "GarminCaloriesConsumed", "type": "numeric"}},
            {"type": "ACTIVITY_FIELD", "content": {"code": "Computed", "script": "activity.x"}}]

    def activity_messages(self, iid):
        return list(self.comments)

    def upload_fit(self, gid, path, **extra):
        self.calls.append(("upload", gid, extra))
        if self.upload_result is not None:
            return self.upload_result
        self.created["i99"] = {"id": "i99", "source": "UPLOAD", "external_id": f"garmin:{gid}", "type": "Ride",
                               "name": extra.get("name"), "paired_event_id": None}
        return {"created": True, "items": [{"id": "i99"}]}

    def update_activity(self, iid, fields):
        self.calls.append(("update", iid, fields))
        if self.update_error:
            raise http_error(self.update_error)
        if iid in self.created:
            self.created[iid].update({k: v for k, v in fields.items() if k not in self.ignored})
        return {"id": iid}

    def delete_activity(self, iid):
        if self.delete_status:
            raise http_error(self.delete_status)
        self.calls.append(f"delete {iid}")

    def replace_file(self, iid, path, keep_intervals=True, keep_custom_fields=True):
        self.calls.append(("replace", iid, path.name, keep_intervals, keep_custom_fields))
        return None if getattr(self, "unchanged", False) else {"id": iid}


def test_swap_uploads_the_original_carries_the_athletes_values_then_deletes_the_copy(tmp_path):
    st, g, i = Store(tmp_path), GarminFake(), SwapIntervals()
    out = swap_activity("42", "i42", g, i, st, apply=True, now=NOW)
    assert out["outcome"] == "swapped" and out["new_id"] == "i99"
    upload, *rest = i.calls
    assert upload == ("upload", "42", {"name": "Morning Ride", "description": "", "paired_event_id": 77})
    carried = {"icu_rpe": 6, "GarminCaloriesConsumed": 1200}       # typed values, not the computed one
    assert ("update", "i99", carried) in rest
    assert rest.index("delete i42") > rest.index(("update", "i99", carried))           # delete last
    assert ("update", "i99", {"paired_event_id": 77}) in rest                            # paired again
    assert st.activity_status("42") == "uploaded" and st.intervals_seen_source("i99") == "UPLOAD"
    st.close()


def test_a_dry_run_only_plans(tmp_path):
    st, i = Store(tmp_path), SwapIntervals()
    out = swap_activity("42", "i42", GarminFake(), i, st, apply=False, now=NOW)
    assert out["outcome"] == "planned" and "icu_rpe" in out["carry"] and i.calls == []
    st.close()


def test_guards_keep_the_copy(tmp_path):
    st, g = Store(tmp_path), GarminFake()
    i = SwapIntervals()
    i.comments = [{"content": "nice ride"}]
    assert swap_activity("42", "i42", g, i, st, apply=True, now=NOW)["reason"] == "it has comments"
    i = SwapIntervals()
    late = datetime(2026, 10, 11, 9, 0, tzinfo=timezone.utc)
    assert "more than 48 h" in swap_activity("42", "i42", g, i, st, apply=True, now=late)["reason"]
    assert swap_activity("42", "i42", g, i, st, apply=True, window_hours=None, now=late)["outcome"] == "swapped"
    st.close()
    st, i = Store(tmp_path / "b"), SwapIntervals()
    i.activity_obj["icu_intervals_edited"] = True
    assert swap_activity("42", "i42", g, i, st, apply=True, now=NOW)["reason"] == "its intervals were edited by hand"
    i.activity_obj.update(icu_intervals_edited=False, source="STRAVA")
    assert "not Garmin's import" in swap_activity("42", "i42", g, i, st, apply=True, now=NOW)["reason"]
    assert i.calls == []
    st.close()


def test_nothing_to_swap_when_garmin_sent_the_original(tmp_path):
    st, i = Store(tmp_path), SwapIntervals()
    i.activity_file = lambda iid: build_fit(records=3, training_effect=3.2)      # the very same file
    assert swap_activity("42", "i42", GarminFake(), i, st, apply=True, now=NOW)["outcome"] == "same"
    assert i.calls == []
    st.close()


def test_never_a_loss(tmp_path):
    # Intervals already had the file: nothing deleted
    st, i = Store(tmp_path), SwapIntervals()
    i.upload_result = {"created": False, "items": []}
    assert swap_activity("42", "i42", GarminFake(), i, st, apply=True, now=NOW)["outcome"] == "kept"
    assert not any(str(c).startswith("delete") for c in i.calls)
    st.close()
    # the original became two activities (multisport): nothing deleted, a person looks
    st, i = Store(tmp_path / "b"), SwapIntervals()
    i.upload_result = {"created": True, "items": [{"id": "i98"}, {"id": "i99"}]}
    assert swap_activity("42", "i42", GarminFake(), i, st, apply=True, now=NOW)["outcome"] == "review"
    assert not any(str(c).startswith("delete") for c in i.calls)
    st.close()


def test_a_failed_delete_is_tried_again(tmp_path):
    st, i = Store(tmp_path), SwapIntervals()
    i.delete_status = 502
    assert swap_activity("42", "i42", GarminFake(), i, st, apply=True, now=NOW)["outcome"] == "delete_failed"
    assert st.meta_items("swap:delete:*") == [("swap:delete:i42", "i99")]
    i.delete_status = None
    assert retry_deletes(i, st) == 1 and "delete i42" in i.calls
    assert retry_deletes(i, st) == 0                                   # done once
    st.close()


def test_watch_swaps_new_imports_when_swap_mode_is_on_and_completes_kept_ones(tmp_path):
    class Watched(SwapIntervals):
        def activities(self, start, end, fields=None, limit=None):
            return [{"id": "i42", "source": "GARMIN_CONNECT", "external_id": "42", "start_date": "2026-10-08T08:00:00Z"}]

    settings = SimpleNamespace(data_dir=tmp_path, timezone=ZoneInfo("Europe/Vienna"), swap=True, swap_hours=10_000,
                               swap_method="upload")
    st, i = Store(tmp_path), Watched()
    m = watch_once(settings, GarminFake(), i, st, apply=True, today=date(2026, 10, 8))
    assert m["swap_swapped"] == 1 and m["enriched"] == 0 and i.updates == []          # no enrich of the copy
    st.close()
    st, i = Store(tmp_path / "b"), Watched()
    i.comments = [{"content": "!"}]
    settings.data_dir = tmp_path / "b"
    m = watch_once(settings, GarminFake(), i, st, apply=True, today=date(2026, 10, 8))
    assert m["swap_kept"] == 1 and m["enriched"] == 1                                    # completed instead
    st.close()


def test_enrich_after_a_swap_rewrites_no_streams_into_the_original(tmp_path, monkeypatch):
    from garmin_intervals_bridge import enrich
    st, g, i = Store(tmp_path), GarminFake(), SwapIntervals()
    st.record_enrichment("42", "i42", "old-sha", ["AerobicEffect"], ["Stamina"])      # the copy was enriched
    swap_activity("42", "i42", g, i, st, apply=True, now=NOW)
    seen = []
    real = enrich.plan_streams
    monkeypatch.setattr(enrich, "plan_streams", lambda *a, **k: seen.append(k.get("refresh")) or real(*a, **k))
    i.activity_obj = {**i.created["i99"], "stream_types": ["time"]}
    i.activity_file = lambda iid: build_fit(records=3, training_effect=3.2)          # Intervals holds the original
    enrich.enrich_activity("42", g, i, st, apply=False, intervals_id="i99", refresh_own=True, recheck=True)
    assert seen == [None]                          # the copy's streams are not "own streams" of the original
    st.close()


def _uploads(i):
    return [c for c in i.calls if c[0] == "upload"]


def test_the_copy_stays_until_the_athletes_values_are_on_the_original(tmp_path):
    st, i = Store(tmp_path), SwapIntervals()
    i.update_error = 502                                            # carrying fails
    out = swap_activity("42", "i42", GarminFake(), i, st, apply=True, now=NOW)
    assert out["outcome"] == "review" and "icu_rpe" in out["reason"] and "delete i42" not in i.calls
    i.update_error = None
    i.ignored = {"icu_rpe"}                                         # accepted, but not there afterwards
    out = swap_activity("42", "i42", GarminFake(), i, st, apply=True, now=NOW)
    assert out["outcome"] == "review" and out["reason"] == "icu_rpe not carried over; both stay"
    i.ignored = set()
    out = swap_activity("42", "i42", GarminFake(), i, st, apply=True, now=NOW)        # the next full run
    assert out["outcome"] == "swapped" and "delete i42" in i.calls
    assert len(_uploads(i)) == 1                                    # taken up again, never uploaded twice
    st.close()


def test_comments_that_arrive_during_the_swap_keep_both(tmp_path):
    st, i = Store(tmp_path), SwapIntervals()
    real = i.update_activity

    def update(iid, fields):
        i.comments = [{"content": "great ride!"}]                  # the coach, just now
        return real(iid, fields)
    i.update_activity = update
    out = swap_activity("42", "i42", GarminFake(), i, st, apply=True, now=NOW)
    assert out["outcome"] == "review" and "comments" in out["reason"] and "delete i42" not in i.calls
    st.close()


def test_a_delete_is_tried_again_only_while_both_are_listed_and_without_comments(tmp_path):
    for case in ("original gone", "comments", "fine"):
        st, i = Store(tmp_path / case), SwapIntervals()
        i.delete_status = 502
        swap_activity("42", "i42", GarminFake(), i, st, apply=True, now=NOW)
        i.delete_status = None
        if case == "original gone":
            i.gone.add("i99")                    # the athlete deleted the new one; by its id it still answers
        if case == "comments":
            i.comments = [{"content": "!"}]
        assert retry_deletes(i, st) == (1 if case == "fine" else 0)
        assert ("delete i42" in i.calls) is (case == "fine")
        assert st.meta_items("swap:delete:*") == [("swap:delete:i42", "")]      # not tried again
        st.close()


def test_an_upload_with_an_unknown_outcome_is_never_sent_again(tmp_path):
    st, i = Store(tmp_path), SwapIntervals()

    def lost(gid, path, **extra):
        i.calls.append(("upload", gid, extra))
        raise requests.ReadTimeout("no answer")
    i.upload_fit = lost
    try:
        swap_activity("42", "i42", GarminFake(), i, st, apply=True, now=NOW)
    except requests.ReadTimeout:
        pass
    assert st.activity_status("42") == "pending"
    out = swap_activity("42", "i42", GarminFake(), i, st, apply=True, now=NOW)
    assert out["outcome"] == "kept" and "reset-pending --activity-id 42" in out["reason"]
    assert len(_uploads(i)) == 1 and not any(str(c).startswith("delete") for c in i.calls)
    st.close()


def test_a_typed_zero_goes_over_but_not_what_the_filtered_file_gave(tmp_path):
    st, i = Store(tmp_path), SwapIntervals()
    i.activity_obj.update(GarminCaloriesConsumed=0, AerobicEffect=0.0, trainer=False)
    out = swap_activity("42", "i42", GarminFake(), i, st, apply=False, now=NOW)
    assert "GarminCaloriesConsumed" in out["carry"]                 # typed in: 0 is a value
    assert "AerobicEffect" not in out["carry"]                      # from the FIT: the original's own wins
    assert "trainer" not in out["carry"]
    st.close()


def test_edited_intervals_stop_automatic_swaps_after_bridge_streams_too(tmp_path):
    st, i = Store(tmp_path), SwapIntervals()
    st.record_enrichment("42", "i42", "old-sha", [], ["Stamina"])  # the bridge wrote streams (an "edit")
    i.activity_obj["icu_intervals_edited"] = True
    out = swap_activity("42", "i42", GarminFake(), i, st, apply=True, now=NOW)
    assert out["outcome"] == "kept" and "swap it by hand" in out["reason"]
    out = swap_activity("42", "i42", GarminFake(), i, st, apply=True, window_hours=None, now=NOW)
    assert out["outcome"] == "swapped" and "warning" in out
    assert st.enrichment("42") is None and st.activity_intervals_id("42") == "i99"   # not the deleted copy
    st.close()


def test_a_workout_not_paired_again_is_tried_again(tmp_path):
    st, i = Store(tmp_path), SwapIntervals()
    real = i.update_activity

    def update(iid, fields):
        if "paired_event_id" in fields and not i.calls.count("pairing failed"):
            i.calls.append("pairing failed")
            raise http_error(502)
        return real(iid, fields)
    i.update_activity = update
    assert swap_activity("42", "i42", GarminFake(), i, st, apply=True, now=NOW)["outcome"] == "swapped"
    assert st.meta_items("swap:pair:*") == [("swap:pair:i99", "77")]
    retry_deletes(i, st)
    assert i.created["i99"]["paired_event_id"] == 77 and st.meta_items("swap:pair:*") == [("swap:pair:i99", "")]
    st.close()


def test_comments_that_cannot_be_read_count_as_comments(monkeypatch):
    from garmin_intervals_bridge.intervals import IntervalsClient
    client = IntervalsClient("k", "0")
    monkeypatch.setattr(client, "_api", lambda *a, **k: {"unexpected": True})
    import pytest
    with pytest.raises(ValueError):
        client.activity_messages("i42")


# ---- replace (the default): the original into the same activity, POST /activity/{id}/file ----

def test_replace_puts_the_original_into_the_same_activity_and_deletes_nothing(tmp_path):
    st, i = Store(tmp_path), SwapIntervals()
    i.comments = [{"content": "nice ride"}]                         # no reason to wait any more:
    i.activity_obj["icu_intervals_edited"] = True                     # the activity, its intervals stay
    st.record_enrichment("42", "i42", "old-sha", ["AerobicEffect"], ["Stamina"])
    assert replace_activity("42", "i42", GarminFake(), i, st, apply=False)["outcome"] == "planned"
    assert i.calls == []                                              # a dry run plans only
    out = replace_activity("42", "i42", GarminFake(), i, st, apply=True)
    assert out["outcome"] == "replaced" and out["intervals_id"] == "i42"
    assert i.calls == [("replace", "i42", "42.fit", True, True)]      # intervals and typed values kept
    assert not any(str(c).startswith("delete") for c in i.calls) and _uploads(i) == []
    assert st.partner_path("42").read_bytes() == st.fit_path("42").read_bytes()   # nothing left to add
    assert st.enrichment("42") is None
    i.activity = lambda iid: (_ for _ in ()).throw(AssertionError("no request"))
    again = replace_activity("42", "i42", GarminFake(), i, st, apply=True)
    assert again["outcome"] == "same" and len(i.calls) == 1           # the next full run: nothing to do, no GET
    st.close()


def test_replace_does_not_trust_the_file_of_an_earlier_activity(tmp_path, monkeypatch):
    monkeypatch.setattr("garmin_intervals_bridge.swap.extract_original_fit", lambda data: data)
    st, i = Store(tmp_path), SwapIntervals()
    st.atomic_save(st.fit_path("42"), b"original")
    st.atomic_save(st.partner_path("42"), b"original")               # what an earlier, deleted activity held
    st.record_enrichment("42", "i41", "sha", [], [])
    i.activity_file = lambda iid: b"filtered"
    assert replace_activity("42", "i42", GarminFake(), i, st, apply=False)["outcome"] == "planned"
    assert st.partner_path("42").read_bytes() == b"filtered"
    st.close()


def test_replace_leaves_other_activities_and_says_when_intervals_had_the_file(tmp_path):
    st, i = Store(tmp_path), SwapIntervals()
    i.unchanged = True                                                # Intervals: file unchanged
    assert replace_activity("42", "i42", GarminFake(), i, st, apply=True)["outcome"] == "same"
    st.close()
    st, i = Store(tmp_path / "b"), SwapIntervals()
    i.activity_obj["source"] = "STRAVA"
    assert "not Garmin's import" in replace_activity("42", "i42", GarminFake(), i, st, apply=True)["reason"]
    assert i.calls == []
    st.close()


def test_the_watch_replaces_by_default_and_completes_nothing_more(tmp_path):
    class Watched(SwapIntervals):
        def activities(self, start, end, fields=None, limit=None):
            return [{"id": "i42", "source": "GARMIN_CONNECT", "external_id": "42", "start_date": "2026-10-08T08:00:00Z"}]

    settings = SimpleNamespace(data_dir=tmp_path, timezone=ZoneInfo("Europe/Vienna"), swap=True, swap_hours=48)
    st, i = Store(tmp_path), Watched()
    m = watch_once(settings, GarminFake(), i, st, apply=True, today=date(2026, 10, 8))
    assert m["swap_replaced"] == 1 and m["enriched"] == 0 and _uploads(i) == []
    st.close()


def test_a_backfill_with_swap_replaces_the_past_in_place(tmp_path, monkeypatch):
    from garmin_intervals_bridge import sync
    done = []
    monkeypatch.setattr("garmin_intervals_bridge.swap.replace_activity",
                        lambda gid, iid, *a, **k: done.append((gid, iid)) or {"outcome": "replaced", "intervals_id": iid})
    garmin = GarminFake()
    garmin.acts = [{"activityId": 42, "startTimeGMT": "2026-01-08 08:00:00", "duration": 3600}]

    class Remote(SwapIntervals):
        def activities(self, start, end, fields=None, limit=None):
            return [{"id": "i42", "source": "GARMIN_CONNECT", "external_id": "42", "start_date": "2026-01-08T08:00:00Z"}]
    settings = SimpleNamespace(data_dir=tmp_path, timezone=ZoneInfo("Europe/Vienna"), swap=False, swap_hours=48,
                               swap_method="upload", activity_days=4)
    st = Store(tmp_path)
    m = sync.sync_enrich(settings, garmin, Remote(), st, apply=True,
                         date_range=(date(2026, 1, 1), date(2026, 1, 31)), swap=True)
    assert done == [("42", "i42")] and m["swap_replaced"] == 1          # in place, whatever the swap setting
    st.close()


def test_the_swap_method_setting(monkeypatch):
    import pytest

    from garmin_intervals_bridge import config
    monkeypatch.setenv("INTERVALS_API_KEY", "not-a-real-key")
    assert config.Settings.from_env().swap_method == "replace"
    monkeypatch.setenv("BRIDGE_SWAP_METHOD", "Upload")
    assert config.Settings.from_env().swap_method == "upload"
    monkeypatch.setenv("BRIDGE_SWAP_METHOD", "delete")
    with pytest.raises(ValueError, match="BRIDGE_SWAP_METHOD"):
        config.Settings.from_env()
