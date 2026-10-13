from pathlib import Path

import garmin_intervals_bridge


def test_the_type_hints_count_for_type_checkers():
    # PEP 561 (thanks @FrancescoCastaldi): the marker ships with the package
    assert (Path(garmin_intervals_bridge.__file__).resolve().parent / "py.typed").is_file()
    assert 'garmin_intervals_bridge = ["py.typed"]' in (Path(__file__).parents[1] / "pyproject.toml").read_text()
