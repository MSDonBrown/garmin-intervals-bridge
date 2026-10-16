"""After a pause – a computer switched on once a week – the scheduled runs read back to the last one."""
from datetime import timedelta

import pytest
from test_wellness_schedule import TODAY, ListingGarmin, QuietIntervals, at, fresh, settings

from garmin_intervals_bridge.garmin import GarminBlocked
from garmin_intervals_bridge.store import Store
from garmin_intervals_bridge.sync import CATCH_UP_KEY, catch_up_days, caught_up, note_pause, sync_enrich, wellness_check


def ran(st, when):
    """A run of the bridge at `when` (straight into the table: record_run prunes by the real clock)."""
    st.db.execute("INSERT INTO runs (command, started, metrics) VALUES ('watch', ?, '{}')", (when.timestamp(),))
    st.db.commit()


class Listing(ListingGarmin):
    def activities(self, start, end):
        self.window = (start, end)
        return super().activities(start, end)


def test_a_running_bridge_keeps_its_usual_window(tmp_path):
    st, s = Store(tmp_path), settings(tmp_path, days=3)
    note_pause(s, st, TODAY)                                             # a new install: no last run
    ran(st, at(7, 0))
    note_pause(s, st, TODAY)
    assert catch_up_days(st, "wellness", 3, TODAY) == 3 and catch_up_days(st, "activities", 3, TODAY) == 3
    assert not st.get_meta(CATCH_UP_KEY + "wellness")
    st.close()


def test_after_a_week_off_the_window_reaches_back_to_the_last_run(tmp_path):
    st, s = Store(tmp_path), settings(tmp_path, days=3)
    ran(st, at(18, 0, day=TODAY - timedelta(days=7)))                    # last week's evening
    note_pause(s, st, TODAY)
    assert catch_up_days(st, "wellness", 3, TODAY) == 10                 # the window of that run, up to today
    ran(st, at(9, 0))                                                    # this morning's first run ...
    note_pause(s, st, TODAY)
    assert catch_up_days(st, "wellness", 3, TODAY) == 10                 # ... keeps it until it is read
    st.close()


def test_a_long_pause_reads_back_two_weeks_at_most(tmp_path):
    st = Store(tmp_path)
    ran(st, at(9, 0, day=TODAY - timedelta(days=60)))
    note_pause(settings(tmp_path, days=3), st, TODAY)
    assert catch_up_days(st, "wellness", 3, TODAY) == 14
    assert catch_up_days(st, "wellness", 20, TODAY) == 20                # a longer window of one's own stays
    st.close()


def test_wellness_reads_the_missed_days_once_then_is_caught_up(tmp_path):
    st, g, i = fresh(tmp_path, days=1)                                   # nothing final yet
    ran(st, at(18, 0, day=TODAY - timedelta(days=7)))
    wellness_check(settings(tmp_path, 3), g, i, st, apply=True, now=at(10, 0), throttle=False)
    missed = [TODAY - timedelta(days=o) for o in range(9, 1, -1)]        # back to the last run's window
    assert [day for day, endpoints in g.reads if endpoints is None] == missed
    assert all(st.get_meta(f"wellness:final:{day}") for day in missed) and {d for d, _ in i.writes} >= set(missed)
    ran(st, at(10, 0))
    wellness_check(settings(tmp_path, 3), g, i, st, apply=True, now=at(10, 20), throttle=False)
    assert not st.get_meta(CATCH_UP_KEY + "wellness")                    # read: the usual window again
    assert len([1 for _, endpoints in g.reads if endpoints is None]) == len(missed)
    st.close()


def test_the_activity_scan_after_a_pause_lists_back_to_the_last_run(tmp_path):
    st, g, s = Store(tmp_path), Listing(), settings(tmp_path)           # activities: 3 days
    ran(st, at(18, 0, day=TODAY - timedelta(days=5)))
    sync_enrich(s, g, QuietIntervals(), st, apply=True, today=TODAY, scan_interval_minutes=120, catch_up=True)
    assert g.window == (TODAY - timedelta(days=7), TODAY)
    assert not st.get_meta(CATCH_UP_KEY + "activities")                  # gone through
    ran(st, at(11, 0))
    sync_enrich(s, g, QuietIntervals(), st, apply=True, today=TODAY, catch_up=True)
    assert g.window == (TODAY - timedelta(days=2), TODAY)
    sync_enrich(s, g, QuietIntervals(), st, apply=True, today=TODAY, activity_days=2)
    assert g.window == (TODAY - timedelta(days=1), TODAY)                # without catch_up: as asked
    st.close()


def test_a_scan_cut_short_keeps_the_catch_up_for_the_next_run(tmp_path):
    class Blocked(Listing):
        def activities(self, start, end):
            super().activities(start, end)
            raise GarminBlocked("429")

    st, s = Store(tmp_path), settings(tmp_path)
    ran(st, at(18, 0, day=TODAY - timedelta(days=5)))
    with pytest.raises(GarminBlocked):
        sync_enrich(s, Blocked(), QuietIntervals(), st, apply=True, today=TODAY, catch_up=True)
    ran(st, at(11, 0))                                                   # a watch ran in between
    g = Listing()
    sync_enrich(s, g, QuietIntervals(), st, apply=True, today=TODAY, scan_interval_minutes=120, catch_up=True)
    assert g.window == (TODAY - timedelta(days=7), TODAY)
    st.close()


def test_midnight_is_no_pause_and_a_pause_is_noted_once(tmp_path):
    st, s = Store(tmp_path), settings(tmp_path, days=3)
    ran(st, at(23, 50, day=TODAY - timedelta(days=1)))                   # a bridge that runs on, past midnight
    note_pause(s, st, TODAY)
    assert not st.get_meta(CATCH_UP_KEY + "activities") and not st.get_meta(CATCH_UP_KEY + "wellness")
    st.close()
    st = Store(tmp_path / "b")
    ran(st, at(23, 50, day=TODAY - timedelta(days=2)))                   # a whole day without a run
    note_pause(s, st, TODAY)
    assert catch_up_days(st, "activities", 3, TODAY) == 5
    caught_up(st, "activities")                                          # the scan of this run went through ...
    note_pause(s, st, TODAY)                                             # ... and its wellness part asks again
    assert catch_up_days(st, "activities", 3, TODAY) == 3 and catch_up_days(st, "wellness", 3, TODAY) == 5
    st.close()
