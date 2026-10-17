"""ImReady4-compatible advice: the scoring, the write rules, the morning automation, the command."""
import json
import math
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from garmin_intervals_bridge import imready4
from garmin_intervals_bridge.store import Store

TZ = ZoneInfo("Europe/Vienna")
DAY = date(2026, 10, 12)
FIELD_ITEM = {"id": 351492, "type": "INPUT_FIELD", "content": {"code": "TrainingAdvice", "type": "select", "options": [
    {"text": "Rest", "value": 1.0}, {"text": "LIT", "value": 2.0}, {"text": "Planned", "value": 3.0},
    {"text": "HIT", "value": 4.0}]}}


def history(day=DAY, today_hrv=50.0, today_rhr=50.0, days=30, **today):
    """`days` records ending at `day`: HRV alternating 45/55 ms, resting HR 48/52; the day as given."""
    out = []
    for n in range(days - 1, 0, -1):
        out.append({"id": (day - timedelta(days=n)).isoformat(), "hrv": 45.0 if n % 2 else 55.0,
                    "restingHR": 48 if n % 2 else 52})
    out.append({"id": day.isoformat(), "hrv": today_hrv, "restingHR": today_rhr, **today})
    return out


# ---- the scoring ----

@pytest.mark.parametrize("z_hrv,z_rhr,code,value", [
    (1.01, 0.0, 1, 4),        # HRV above, resting HR normal: HIT
    (1.0, 0.0, 4, 3),         # exactly +1 is not above: as planned
    (2.0, 1.0, 1, 4),         # resting HR +1 still counts for HIT ...
    (2.0, -1.0, 4, 3),        # ... -1 does not
    (-0.5, -2.0, 2, 2),       # very low resting HR, HRV a little below: LIT
    (0.0, -2.0, 3, 2),        # very low resting HR, HRV not below: LIT! acute fatigue
    (0.0, 1.69, 4, 3),        # as planned up to resting HR < 1.7
    (0.0, 1.7, 2, 2),         # resting HR from +1.7: LIT
    (-1.5, -2.5, 5, 1),       # HRV well below, resting HR very low: Rest
    (-1.5, 0.0, 3, 2),        # HRV well below, resting HR normal: LIT, recovery not complete
    (-1.5, 1.7, 3, 2),        # ... up to +1.7
    (-1.5, 1.8, 6, 1),        # HRV well below, resting HR well above: REST! illness or stress
    (-1.0, 5.0, 2, 2),        # HRV at -1 still counts as normal HRV
])
def test_zones_and_their_borders_as_imready4_scores_them(z_hrv, z_rhr, code, value):
    got, _ = imready4.classify(z_hrv, z_rhr)
    assert got == code and imready4.CODE_TO_VALUE[got] == value


def test_reference_is_30_rows_with_population_sd_and_gaps_left_out():
    values = [10.0, None] + [4.0, 6.0] * 14 + [1000.0]          # the 31st row is outside the window
    ref = imready4.reference(values)
    window = [10.0] + [4.0, 6.0] * 14
    mean = sum(window) / len(window)
    sd = math.sqrt(sum((v - mean) ** 2 for v in window) / len(window))     # population, not sample
    assert ref.valid == 29 and ref.mean == pytest.approx(mean) and ref.z == pytest.approx((10.0 - mean) / sd)
    assert imready4.reference([5.0] * 20).z is None                         # no variability: no z-score


def test_each_advice_from_a_synthetic_history():
    planned = imready4.advise(history(), DAY)
    assert (planned.value, planned.advice, planned.code) == (3, "Planned", 4)
    hit = imready4.advise(history(today_hrv=80.0), DAY)
    assert hit.value == 4 and hit.z_hrv > 1 and -1 < hit.z_resting_hr <= 1
    acute = imready4.advise(history(today_hrv=60.0, today_rhr=42), DAY)
    assert (acute.value, acute.code) == (2, 3) and acute.zone.startswith("LIT!") and acute.z_resting_hr <= -2
    rest = imready4.advise(history(today_hrv=30.0, today_rhr=42), DAY)
    assert (rest.value, rest.code) == (1, 5)
    illness = imready4.advise(history(today_hrv=30.0, today_rhr=60), DAY)
    assert (illness.value, illness.code) == (1, 6)
    recovering = imready4.advise(history(today_hrv=30.0, today_rhr=50), DAY)
    assert (recovering.value, recovering.code) == (2, 3) and "recovery" in recovering.zone


def test_no_advice_without_the_days_values_enough_history_or_variability():
    late = imready4.advise(history(today_rhr=None), DAY, today=DAY)
    assert late.value is None and late.waiting and late.reason == "no restingHR for the day yet"
    zeros = imready4.advise(history(today_hrv=0.0), DAY, today=DAY)
    assert zeros.value is None and zeros.waiting                       # a zero is missing, never a measurement
    past = imready4.advise(history(today_hrv=None), DAY, today=DAY + timedelta(days=3))
    assert past.value is None and not past.waiting and past.reason == "no hrv for the day"
    short = imready4.advise(history(days=10), DAY)
    assert short.value is None and not short.waiting and short.reason.startswith("not enough history: 10")
    flat = [{"id": (DAY - timedelta(days=n)).isoformat(), "hrv": 50.0, "restingHR": 50 + n % 2} for n in range(30)]
    assert imready4.advise(flat, DAY).reason == "no variability in the last 30 days"
    gone = imready4.advise(history(day=DAY - timedelta(days=1)), DAY, today=DAY)
    assert gone.waiting and gone.reason == "no wellness record for the day yet"   # never yesterday's score instead


# ---- the field and the write rules ----

class Intervals:
    def __init__(self, records, item=FIELD_ITEM):
        self.records = {r["id"]: dict(r) for r in records}
        self.item = item
        self.writes, self.fail = [], 0

    def custom_items(self):
        return [self.item] if self.item else []

    def wellness_range(self, oldest, newest, cols=None):
        if self.fail:
            self.fail -= 1
            raise ConnectionError("down")
        return [dict(r) for d, r in sorted(self.records.items()) if oldest.isoformat() <= d <= newest.isoformat()]

    def wellness(self, day):
        return dict(self.records.get(day.isoformat(), {"id": day.isoformat()}))

    def write_wellness(self, day, changes):
        self.writes.append((day, changes))
        self.records.setdefault(day.isoformat(), {"id": day.isoformat()}).update(changes)


def test_the_field_is_found_by_code_and_must_offer_the_four_values():
    assert imready4.field_problem(imready4.find_field(Intervals([]))) is None
    assert "has no 'TrainingAdvice'" in imready4.field_problem(imready4.find_field(Intervals([], item=None)))
    other = json.loads(json.dumps(FIELD_ITEM))
    other["content"]["options"][2]["text"] = "Easy"
    assert "does not offer" in imready4.field_problem(other)


def test_write_rules_empty_only_never_locked_and_read_back():
    advice = imready4.advise(history(), DAY)
    iv = Intervals(history())
    assert imready4.apply_day(iv, DAY, advice, {"id": "x"}, apply=False)["action"] == "would write"
    assert iv.writes == []                                                       # a dry run writes nothing
    assert imready4.apply_day(iv, DAY, advice, {"TrainingAdvice": 1}, apply=True)["action"] == "keep"
    assert imready4.apply_day(iv, DAY, advice, {"TrainingAdvice": 3}, apply=True)["action"] == "same"
    assert imready4.apply_day(iv, DAY, advice, {"locked": True}, apply=True)["action"] == "locked"
    assert iv.writes == []
    assert imready4.apply_day(iv, DAY, advice, {"TrainingAdvice": ""}, apply=True)["action"] == "written"
    assert iv.writes == [(DAY, {"TrainingAdvice": 3})]                          # only the field, nothing else
    assert imready4.apply_day(iv, DAY, advice, {"TrainingAdvice": 1}, apply=True, overwrite=True)["action"] \
        == "written"


def test_a_value_that_does_not_read_back_is_an_error():
    iv = Intervals(history())
    iv.wellness = lambda day: {"id": day.isoformat(), "TrainingAdvice": 2}       # someone else's value won
    with pytest.raises(RuntimeError, match="reads back as 2"):
        imready4.apply_day(iv, DAY, imready4.advise(history(), DAY), {}, apply=True)


# ---- the morning automation ----

def at(hour, minute=0, day=DAY):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)


ON = SimpleNamespace(timezone=TZ, imready4=True)


def test_written_once_after_the_night_and_never_again(tmp_path):
    st, iv = Store(tmp_path), Intervals(history())
    assert imready4.due(ON, st, DAY, at(9).timestamp(), apply=True)
    assert imready4.morning(ON, iv, st, DAY, at(9), apply=True)["action"] == "written"
    assert iv.writes == [(DAY, {"TrainingAdvice": 3})]
    assert not imready4.due(ON, st, DAY, at(9, 10).timestamp(), apply=True)       # repeated runs: nothing more
    assert json.loads(st.get_meta(f"imready4:{DAY}"))["value"] == 3
    st.close()


def test_off_by_default_and_a_dry_run_reports_once(tmp_path):
    st, iv = Store(tmp_path), Intervals(history())
    assert not imready4.due(SimpleNamespace(timezone=TZ), st, DAY, at(9).timestamp(), apply=True)
    assert imready4.morning(ON, iv, st, DAY, at(9), apply=False)["action"] == "would write"
    assert iv.writes == [] and not imready4.due(ON, st, DAY, at(9, 10).timestamp(), apply=False)
    assert imready4.due(ON, st, DAY, at(9, 10).timestamp(), apply=True)          # --apply later the same day
    st.close()


def test_a_late_resting_hr_is_waited_for_and_the_advice_follows(tmp_path):
    st, iv = Store(tmp_path), Intervals(history(today_rhr=None))
    assert imready4.morning(ON, iv, st, DAY, at(7), apply=True)["action"] == "wait"
    assert not imready4.due(ON, st, DAY, at(7, 5).timestamp(), apply=True)       # backs off ...
    assert imready4.due(ON, st, DAY, at(7, 11).timestamp(), apply=True)          # ... and comes back
    iv.records[DAY.isoformat()]["restingHR"] = 50                                 # the watch synced again
    assert imready4.morning(ON, iv, st, DAY, at(7, 11), apply=True)["action"] == "written"
    st.close()


def test_no_complete_night_by_the_evening_means_no_advice_and_nothing_written(tmp_path):
    st, iv = Store(tmp_path), Intervals(history(today_hrv=None))
    assert imready4.morning(ON, iv, st, DAY, at(22, 5), apply=True)["action"] == "gave up"
    assert iv.writes == [] and not imready4.due(ON, st, DAY, at(23).timestamp(), apply=True)
    st.close()


def test_an_intervals_error_is_retried_and_a_manual_value_is_kept(tmp_path):
    st, iv = Store(tmp_path), Intervals(history(TrainingAdvice=1))
    iv.fail = 1
    assert imready4.morning(ON, iv, st, DAY, at(9), apply=True) == {"action": "wait", "reason": "ConnectionError"}
    result = imready4.morning(ON, iv, st, DAY, at(9, 11), apply=True)
    assert result["action"] == "keep" and result["intervals_value"] == 1 and result["value"] == 3
    assert iv.writes == []                                                       # your own value stays
    st.close()


def test_locked_day_and_missing_field_are_left_alone(tmp_path):
    st = Store(tmp_path)
    locked = Intervals(history(locked=True))
    assert imready4.morning(ON, locked, st, DAY, at(9), apply=True)["action"] == "locked"
    other = DAY + timedelta(days=1)
    nofield = Intervals(history(day=other), item=None)
    assert imready4.morning(ON, nofield, st, other, at(9, day=other), apply=True)["action"] == "no field"
    assert locked.writes == nofield.writes == []
    st.close()


def test_the_day_is_the_local_one_across_the_clock_change(tmp_path):
    change = date(2026, 3, 29)                                     # 02:00 -> 03:00 in Vienna
    st, iv = Store(tmp_path), Intervals(history(day=change))
    now = datetime(2026, 3, 29, 3, 30, tzinfo=TZ)
    assert now.date() == change and imready4.morning(ON, iv, st, now.date(), now, apply=True)["action"] == "written"
    assert iv.writes == [(change, {"TrainingAdvice": 3})]
    st.close()


def test_the_morning_wellness_run_writes_the_advice_after_the_night(tmp_path):
    from test_wellness_schedule import Intervals as WellnessIntervals
    from test_wellness_schedule import at as morning_at
    from test_wellness_schedule import fresh

    from garmin_intervals_bridge.sync import wellness_check

    class Both(WellnessIntervals):
        def custom_items(self):
            return [FIELD_ITEM]

        def wellness_range(self, oldest, newest, cols=None):
            return [{**r, "id": d.isoformat()} for d, r in sorted(self.records.items())
                    if oldest <= d <= newest]

    st, g, _ = fresh(tmp_path)
    i = Both()
    today = date(2026, 10, 10)
    st.set_meta(f"wellness:morning:{today - timedelta(days=1)}", "ok")    # Garmin delivers this person's nights
    for r in history(day=today, today_rhr=None):
        i.records[date.fromisoformat(r["id"])] = {k: v for k, v in r.items() if k != "id"}
    s = SimpleNamespace(data_dir=tmp_path, timezone=TZ, wellness_days=2, wellness_profile="all", activity_days=3,
                        imready4=True)
    wellness_check(s, g, i, st, apply=True, now=morning_at(7), throttle=False)
    assert not [w for w in i.writes if "TrainingAdvice" in w[1]]            # no night yet: nothing
    g.sleep = g.readiness = True
    i.records[today].update(sleepSecs=28080, restingHR=50)                   # the official sync delivered it
    m = wellness_check(s, g, i, st, apply=True, now=morning_at(9, 50), throttle=False)
    assert m["morning"] == 1 and m["advice"]["action"] == "written"
    assert [w for w in i.writes if "TrainingAdvice" in w[1]] == [(today, {"TrainingAdvice": 3})]
    wellness_check(s, g, i, st, apply=True, now=morning_at(10, 30), throttle=False)
    assert len([w for w in i.writes if "TrainingAdvice" in w[1]]) == 1      # once a day
    st.close()


def test_without_a_garmin_watch_at_night_the_advice_takes_the_values_from_intervals(tmp_path):
    # MedTechCD in the forum: a Coros watch, Kubios HRV into Intervals, Garmin only on the bike
    from test_wellness_schedule import Intervals as WellnessIntervals
    from test_wellness_schedule import at as morning_at
    from test_wellness_schedule import fresh

    from garmin_intervals_bridge.sync import wellness_check

    class Both(WellnessIntervals):
        def custom_items(self):
            return [FIELD_ITEM]

        def wellness_range(self, oldest, newest, cols=None):
            return [{**r, "id": d.isoformat()} for d, r in sorted(self.records.items())
                    if oldest <= d <= newest]

    st, g, _ = fresh(tmp_path)                                               # Garmin had no night all week
    i = Both()
    today = date(2026, 10, 10)
    for r in history(day=today, today_hrv=None, today_rhr=None):
        i.records[date.fromisoformat(r["id"])] = {k: v for k, v in r.items() if k != "id"}
    s = SimpleNamespace(data_dir=tmp_path, timezone=TZ, wellness_days=2, wellness_profile="all", activity_days=3,
                        imready4=True)
    wellness_check(s, g, i, st, apply=True, now=morning_at(7), throttle=False)
    assert not [w for w in i.writes if "TrainingAdvice" in w[1]]            # the values are not there yet
    i.records[today].update(hrv=50.0, restingHR=50)                          # Kubios, through another tool
    m = wellness_check(s, g, i, st, apply=True, now=morning_at(8, 30), throttle=False)
    assert m.get("morning", 0) == 0 and m["advice"]["action"] == "written"   # no Garmin night needed
    assert [w for w in i.writes if "TrainingAdvice" in w[1]] == [(today, {"TrainingAdvice": 3})]
    st.close()


# ---- the command ----

def test_command_previews_compares_and_backfills_only_empty_days(tmp_path, monkeypatch, capsys):
    from garmin_intervals_bridge import cli
    records = history(days=40)
    for r in records[-5:-2]:
        r["TrainingAdvice"] = 3                                     # what running the script once left there
    records[-2]["TrainingAdvice"] = 1                               # one that disagrees
    iv = Intervals(records)
    monkeypatch.setenv("BRIDGE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GARMIN_TOKEN_DIR", str(tmp_path / "tokens"))
    monkeypatch.setenv("INTERVALS_API_KEY", "not-a-real-key")
    monkeypatch.setenv("BRIDGE_TIMEZONE", "Europe/Vienna")
    monkeypatch.setattr(cli, "IntervalsClient", lambda *a, **k: iv)
    monkeypatch.setattr(cli, "GarminSource", lambda *a, **k: pytest.fail("no Garmin for the advice"))
    assert cli.main(["imready4", "--date", DAY.isoformat()]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["apply"] is False and out["days"][0]["advice"] == "Planned" and out["days"][0]["action"] \
        == "would write"
    first = (DAY - timedelta(days=4)).isoformat()
    assert cli.main(["imready4", "--from", first, "--to", DAY.isoformat(), "--compare"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["comparison"]["days_compared"] == 4 and out["comparison"]["agree"] >= 1
    assert [m["intervals"] for m in out["comparison"]["mismatches"]] == [1]
    assert cli.main(["imready4", "--from", first, "--to", DAY.isoformat(), "--apply"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [d for d, _ in iv.writes] == [DAY]                       # the only empty day; the others are kept
    assert out["actions"] == {"keep": 1, "same": 3, "written": 1}
