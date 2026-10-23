"""The experimental HRV trend advice: its rules, its own fields, the morning run, the command."""
import json
import math
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from garmin_intervals_bridge import hrvtrend
from garmin_intervals_bridge.store import Store

TZ = ZoneInfo("Europe/Vienna")
DAY = date(2026, 10, 12)
NEUTRAL = math.sqrt(45 * 55)          # the baseline's geometric mean: ln of it is the baseline mean


def history(day=DAY, days=80, hr=True, week=None, **today):
    """Baseline HRV alternating 45/55 ms, night HR 48/52; the last week neutral unless `week` says otherwise."""
    out = []
    for n in range(days - 1, -1, -1):
        d = day - timedelta(days=n)
        record = {"id": d.isoformat()}
        if n >= 7:
            record["hrv"] = 45.0 if n % 2 else 55.0
            if hr:
                record["avgSleepingHR"] = 48 if n % 2 else 52
        else:
            record["hrv"] = (week or {}).get("hrv", NEUTRAL)
            if hr:
                record["avgSleepingHR"] = (week or {}).get("hr", 50)
        out.append(record)
    out[-1].update(today)
    return out


def test_a_normal_week_trains_as_planned_and_hit_is_never_given():
    t = hrvtrend.advise(history(), DAY)
    assert (t.value, t.advice) == (3, "Planned") and t.rule.startswith("within the normal range")
    assert t.cv_percent == 0.0 and t.hrv_band_ms[0] < NEUTRAL < t.hrv_band_ms[1]
    rested = hrvtrend.advise(history(week={"hrv": 70.0, "hr": 44}), DAY)
    assert rested.value == 3 and "saturation" in rested.rule                      # a note, not a brake


def test_a_week_below_the_normal_range_means_lit_and_with_a_raised_night_hr_rest():
    assert hrvtrend.advise(history(week={"hrv": 38.0}), DAY).value == 2
    rest = hrvtrend.advise(history(week={"hrv": 38.0, "hr": 53}), DAY)          # above its band, not +2 SD
    assert rest.value == 1 and rest.rule.startswith("trend: HRV below its normal range, night heart rate above")


def test_acute_rules_one_far_drop_two_bad_nights_or_hrv_down_with_hr_way_up():
    single = hrvtrend.advise(history(hrv=38.0), DAY)                              # today alone, far below
    assert single.value == 2 and single.rule == "acute: HRV well below normal" and single.z_today < -2
    records = history(hrv=43.0)
    records[-2]["hrv"] = 43.0                                                    # yesterday too
    two = hrvtrend.advise(records, DAY)
    assert two.value == 2 and two.rule.endswith("two nights in a row")
    one = hrvtrend.advise(history(hrv=43.0), DAY)
    assert one.value == 3                                                        # one moderately low night: no
    sick = hrvtrend.advise(history(hrv=40.0, avgSleepingHR=60), DAY)
    assert sick.value == 1 and sick.rule.startswith("acute: HRV down and night heart rate well up")


def test_without_night_heart_rate_it_judges_hrv_alone():
    t = hrvtrend.advise(history(hr=False, week={"hrv": 38.0}), DAY)
    assert t.value == 2 and t.night_hr_week is None                              # LIT, never the HR-based Rest


def test_no_advice_without_the_night_enough_nights_or_history():
    late = hrvtrend.advise(history(hrv=None), DAY, today=DAY)
    assert late.value is None and late.waiting and late.reason == "no hrv for the day yet"
    past = hrvtrend.advise(history(hrv=None), DAY, today=DAY + timedelta(days=2))
    assert past.value is None and not past.waiting
    sparse = history()
    for r in sparse[-6:-2]:
        r["hrv"] = 0                                                             # zeros are gaps, not values
    assert hrvtrend.advise(sparse, DAY).reason == "only 3 of the last 7 nights measured, 5 needed"
    assert hrvtrend.advise(history(days=30), DAY).reason.startswith("not enough history: 23 nights")


class Intervals:
    def __init__(self, records, items=()):
        self.records = {r["id"]: dict(r) for r in records}
        self.items = list(items)
        self.writes, self.created = [], []

    def custom_items(self):
        return list(self.items)

    def create_custom_item(self, item):
        self.created.append(item["content"]["code"])
        self.items.append(item)

    def wellness_range(self, oldest, newest, cols=None):
        return [dict(r) for d, r in sorted(self.records.items()) if oldest.isoformat() <= d <= newest.isoformat()]

    def wellness(self, day):
        return dict(self.records.get(day.isoformat(), {"id": day.isoformat()}))

    def write_wellness(self, day, changes):
        self.writes.append((day, changes))
        self.records.setdefault(day.isoformat(), {"id": day.isoformat()}).update(changes)


def test_its_own_two_fields_are_created_only_on_apply_and_never_over_another_kind():
    iv = Intervals(history())
    assert hrvtrend.ensure_fields(iv, apply=False) == ["HRVTrendAdvice", "HRVTrendCV"] and iv.created == []
    hrvtrend.ensure_fields(iv, apply=True)
    assert iv.created == ["HRVTrendAdvice", "HRVTrendCV"]
    assert iv.items[0]["content"]["options"][2] == {"value": 3, "text": "Planned", "color": "#00AB00"}
    assert hrvtrend.ensure_fields(iv, apply=True) == [] and len(iv.created) == 2      # once
    clash = Intervals([], items=[{"type": "INPUT_FIELD", "content": {"code": "HRVTrendAdvice", "type": "numeric"}}])
    with pytest.raises(ValueError, match="not a select"):
        hrvtrend.ensure_fields(clash, apply=True)


def test_written_with_its_cv_next_to_imready4_never_over_a_value():
    iv = Intervals(history(TrainingAdvice=4))
    t = hrvtrend.advise(list(iv.records.values()), DAY)
    out = hrvtrend.apply_day(iv, DAY, t, iv.records[DAY.isoformat()], apply=True)
    assert out["action"] == "written" and out["imready4"] == 4
    assert iv.writes == [(DAY, {"HRVTrendAdvice": 3, "HRVTrendCV": 0.0})]         # TrainingAdvice untouched
    assert hrvtrend.apply_day(iv, DAY, hrvtrend.advise(history(week={"hrv": 38.0}), DAY),
                              iv.wellness(DAY), apply=True)["action"] == "keep"


def at(hour, minute=0, day=DAY):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)


def test_the_morning_writes_once_when_switched_on(tmp_path):
    st, iv = Store(tmp_path), Intervals(history())
    off = SimpleNamespace(timezone=TZ)
    assert not hrvtrend.due(off, st, DAY, at(9).timestamp(), apply=True)          # its own switch
    on = SimpleNamespace(timezone=TZ, hrvtrend=True)
    result = hrvtrend.morning(on, iv, st, DAY, at(9), apply=True)
    assert result["action"] == "written" and result["value"] == 3 and iv.created == ["HRVTrendAdvice", "HRVTrendCV"]
    assert not hrvtrend.due(on, st, DAY, at(9, 30).timestamp(), apply=True) and len(iv.writes) == 1
    assert json.loads(st.get_meta(f"hrvtrend:{DAY}"))["advice"] == "Planned"
    st.close()


def test_command_compares_with_imready4_and_fills_its_field(tmp_path, monkeypatch, capsys):
    from garmin_intervals_bridge import cli
    records = history(days=90)
    for r in records[-5:]:
        r["TrainingAdvice"] = 3
    records[-1]["TrainingAdvice"] = 4
    iv = Intervals(records)
    monkeypatch.setenv("BRIDGE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GARMIN_TOKEN_DIR", str(tmp_path / "tokens"))
    monkeypatch.setenv("INTERVALS_API_KEY", "not-a-real-key")
    monkeypatch.setenv("BRIDGE_TIMEZONE", "Europe/Vienna")
    monkeypatch.setattr(cli, "IntervalsClient", lambda *a, **k: iv)
    first = (DAY - timedelta(days=4)).isoformat()
    assert cli.main(["hrvtrend", "--from", first, "--to", DAY.isoformat(), "--compare"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["fields"] == {"would create": ["HRVTrendAdvice", "HRVTrendCV"]} and iv.writes == []
    assert out["comparison"]["days_with_both"] == 5 and out["comparison"]["pairs"] == {"Planned/Planned": 4,
                                                                                       "HIT/Planned": 1}
    assert cli.main(["hrvtrend", "--from", first, "--to", DAY.isoformat(), "--apply"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["actions"] == {"written": 5} and len(iv.writes) == 5
    assert all("TrainingAdvice" not in changes for _, changes in iv.writes)


FIELD_ITEM = {"id": 351492, "type": "INPUT_FIELD", "content": {"code": "TrainingAdvice", "type": "select", "options": [
    {"text": "Rest", "value": 1.0}, {"text": "LIT", "value": 2.0}, {"text": "Planned", "value": 3.0},
    {"text": "HIT", "value": 4.0}]}}


def test_the_morning_wellness_run_writes_the_trend_advice(tmp_path):
    from test_wellness_schedule import Intervals as WellnessIntervals
    from test_wellness_schedule import at as morning_at
    from test_wellness_schedule import fresh

    from garmin_intervals_bridge.sync import wellness_check

    class Both(WellnessIntervals):
        items = [FIELD_ITEM]

        def custom_items(self):
            return list(self.items)

        def create_custom_item(self, item):
            self.items = self.items + [item]

        def wellness_range(self, oldest, newest, cols=None):
            return [{**r, "id": d.isoformat()} for d, r in sorted(self.records.items()) if oldest <= d <= newest]

    st, g, _ = fresh(tmp_path)
    i = Both()
    today = date(2026, 10, 10)
    for r in history(day=today):
        i.records[date.fromisoformat(r["id"])] = {**{k: v for k, v in r.items() if k != "id"},
                                                  "restingHR": r["avgSleepingHR"]}
    s = SimpleNamespace(data_dir=tmp_path, timezone=TZ, wellness_days=2, wellness_profile="all", activity_days=3,
                        hrvtrend=True)
    g.sleep = g.readiness = True
    i.records[today]["sleepSecs"] = 28080
    m = wellness_check(s, g, i, st, apply=True, now=morning_at(9, 50), throttle=False)
    assert m["trend_advice"]["action"] == "written"
    fields = {k for _, changes in i.writes for k in changes}
    assert {"HRVTrendAdvice", "HRVTrendCV"} <= fields and "TrainingAdvice" not in fields
    st.close()
