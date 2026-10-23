"""The timers and thresholds as settings: defaults unchanged, bad values stop the start, docs in step."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from garmin_intervals_bridge import tuning

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG_DEFAULTS = {            # the values the bridge always used
    "BRIDGE_SYNC_MINUTES": 30, "BRIDGE_ACTIVITY_SCAN_MINUTES": 120, "BRIDGE_TICK_SECONDS": 60,
    "BRIDGE_PART_BUSY_RETRY_SECONDS": 300, "BRIDGE_BUSY_WAIT_SECONDS": 300,
    "BRIDGE_RATE_LIMIT_PAUSE_SECONDS": 900, "BRIDGE_BLOCK_PAUSE_SECONDS": 900,
    "BRIDGE_FAILURE_BACKOFF_SECONDS": 300, "BRIDGE_HEALTH_FIRST_SECONDS": 300, "BRIDGE_HEALTH_HOURS": 24,
    "BRIDGE_HOT_MINUTES": 60, "BRIDGE_ASK_GAP_SECONDS": 30, "BRIDGE_POLL_SLACK_SECONDS": 15,
    "BRIDGE_POLL_QUIET_DEFAULT_SECONDS": 1800, "BRIDGE_ADAPTIVE_LEARN_DAYS": 84,
    "BRIDGE_ADAPTIVE_HALF_LIFE_DAYS": 28, "BRIDGE_ADAPTIVE_MIN_ACTIVITIES": 15,
    "BRIDGE_ADAPTIVE_FAST_SHARE": 0.8, "BRIDGE_ADAPTIVE_MIDDLE_SHARE": 0.97,
    "BRIDGE_ARCHIVE_SETTLE_DAYS": 2, "BRIDGE_INTAKE_SETTLE_HOURS": 12, "BRIDGE_CATCH_UP_DAYS": 14,
    "BRIDGE_RETRIES_BY_ID": 3, "BRIDGE_FINAL_TRIES": 3, "BRIDGE_WELLNESS_CHECK_MINUTES": 10,
    "BRIDGE_WELLNESS_SIGNAL_DAYS": 2, "BRIDGE_GARMIN_NIGHT_DAYS": 7, "BRIDGE_FINAL_ANYWAY_HOUR": 20,
    "BRIDGE_READINESS_RETRIES": 2, "BRIDGE_READINESS_RETRY_MINUTES": 30,
    "BRIDGE_HEALTH_STALE_RUN_HOURS": 3, "BRIDGE_HEALTH_ENDPOINT_ERROR_DAYS": 3,
    "BRIDGE_HEALTH_DROPOUT_BASELINE_DAYS": 7, "BRIDGE_HEALTH_DROPOUT_MIN_BASELINE": 5,
    "BRIDGE_HEALTH_DROPOUT_MISSING_DAYS": 2, "BRIDGE_BACKUP_FINAL_HOUR": 12,
    "BRIDGE_BACKUP_LATE_READ_DAYS": 3, "BRIDGE_BACKUP_RECENT_ACTIVITY_DAYS": 3,
    "BRIDGE_BACKUP_RECENT_WELLNESS_DAYS": 7, "BRIDGE_BACKUP_ACCOUNT_DAYS": 7,
}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in tuning.TUNABLES:
        monkeypatch.delenv(name, raising=False)


def test_defaults_are_the_values_the_bridge_always_used():
    for name, value in CHANGELOG_DEFAULTS.items():
        assert tuning.get(name) == value, name
        assert type(tuning.get(name)) is type(value), name


def test_every_default_lies_in_its_own_range():
    for spec in tuning.TUNABLES.values():
        assert spec.low <= spec.default <= spec.high, spec.name
    tuning.validate()


def test_an_empty_value_means_the_default(monkeypatch):
    monkeypatch.setenv("BRIDGE_TICK_SECONDS", "  ")
    assert tuning.get("BRIDGE_TICK_SECONDS") == 60


def test_a_value_is_read_when_asked_not_at_import(monkeypatch):
    monkeypatch.setenv("BRIDGE_HOT_MINUTES", "5")
    assert tuning.get("BRIDGE_HOT_MINUTES") == 5


@pytest.mark.parametrize("name,value", [("BRIDGE_TICK_SECONDS", "soon"), ("BRIDGE_TICK_SECONDS", "1"),
                                        ("BRIDGE_TICK_SECONDS", "99999"), ("BRIDGE_TICK_SECONDS", "6.5"),
                                        ("BRIDGE_ADAPTIVE_FAST_SHARE", "nan"), ("BRIDGE_ADAPTIVE_FAST_SHARE", "2")])
def test_a_bad_value_names_the_variable(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        tuning.validate()


def test_the_shares_must_be_in_order(monkeypatch):
    monkeypatch.setenv("BRIDGE_ADAPTIVE_FAST_SHARE", "0.9")
    monkeypatch.setenv("BRIDGE_ADAPTIVE_MIDDLE_SHARE", "0.5")
    with pytest.raises(ValueError, match="MIDDLE_SHARE"):
        tuning.validate()


def test_settings_refuse_a_bad_timer_at_start(monkeypatch, tmp_path):
    from garmin_intervals_bridge.config import Settings
    monkeypatch.setenv("BRIDGE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("INTERVALS_API_KEY", "x")
    monkeypatch.setenv("BRIDGE_HOT_MINUTES", "forever")
    with pytest.raises(ValueError, match="BRIDGE_HOT_MINUTES"):
        Settings.from_env()


def test_env_example_lists_every_setting_with_its_default():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    for spec in tuning.TUNABLES.values():
        assert re.search(rf"^{spec.name}={re.escape(str(spec.default))}$", text, re.M), spec.name


def test_readme_documents_every_setting():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    for name in tuning.TUNABLES:
        assert f"`{name}`" in text, name


def test_no_module_keeps_a_second_copy_of_a_default():
    # a setting changed in tuning.py must change the behaviour: modules ask tuning, never hold a literal
    src = ROOT / "src" / "garmin_intervals_bridge"
    for fn in ("polling.py", "health.py", "backup.py", "sync.py", "cli.py"):
        for line in (src / fn).read_text(encoding="utf-8").splitlines():
            m = re.match(r"^([A-Z][A-Z_]+) = (\d+(?:\.\d+)?)\s*(#.*)?$", line)
            assert not (m and m.group(1) in {"HOT_MINUTES", "ASK_GAP", "SLACK", "STALE_RUN_HOURS", "CATCH_UP_DAYS",
                                             "FINAL_TRIES", "TICK_SECONDS", "LEARN_DAYS"}), (fn, line)
