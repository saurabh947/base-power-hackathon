"""Replay years and their presets (src/data/years.py, src/sim/control.py; docs/API.md Settings.year, Year, Preset).

Real prices come from the committed snapshot here (PRICE_SOURCE unset): 2025 is real all year, 2026 is the
year-to-date file ERCOT published on 2026-09-20, whose last RT interval ends 2026-09-20 05:00Z for every fleet city.
The synthetic YearInfo objects below are test doubles for the "not loaded" and "complete" cases.
"""
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from src.config import load_config, load_raw_config
from src.data import years
from src.sim import control as ctl

UTC = timezone.utc
END_2026 = datetime(2026, 9, 20, 5, tzinfo=UTC)  # the committed 2026 snapshot's real_until_utc


@pytest.fixture(autouse=True)
def snapshot_prices(monkeypatch):
    monkeypatch.delenv("PRICE_SOURCE", raising=False)
    years.clear_cache()
    yield
    years.clear_cache()


@pytest.fixture(scope="module")
def cfg():
    return load_config()


@pytest.fixture(scope="module")
def raw():
    return load_raw_config()


def info(year, first=None, last_end=None):
    a, b = years.year_window(year)
    return years.YearInfo(year, a, b, first, last_end)


def test_configured_years_are_2025_and_2026(raw):
    assert years.configured_years(raw) == [2025, 2026] and years.default_year(raw) == 2025


def test_real_until_from_the_snapshot():
    y25, y26 = years.info(2025), years.info(2026)
    assert y25.available and y25.real_until_utc is None  # real all year
    assert y25.first_utc == datetime(2025, 1, 1, 6, tzinfo=UTC)
    assert y26.available and y26.first_utc == datetime(2026, 1, 1, 6, tzinfo=UTC)
    assert y26.real_until_utc == END_2026 == years.real_until(2026)
    assert not years.info(2024 + 10).available  # no file: nothing real


def test_real_until_is_where_every_city_has_prices(tmp_path):
    """The END of the last interval ALL fleet cities have: a city that stops early sets it."""
    rows = []
    start = pd.Timestamp("2026-01-01T06:00Z")
    for loc, n in (("LZ_HOUSTON", 100), ("LZ_NORTH", 100), ("LZ_CPS", 97), ("LZ_AEN", 100)):
        ts = pd.date_range(start, periods=n, freq="15min", tz="UTC")
        rows.append(pd.DataFrame({"ts": ts, "location": loc}))
    pd.concat(rows).to_parquet(tmp_path / "ercot_rt_2026_hubs.parquet")
    y = years.info(2026, snapshot_dir=tmp_path)
    assert y.real_until_utc == (start + pd.Timedelta(minutes=15 * 97)).to_pydatetime()


def test_the_evening_before_real_prices_end():
    assert years.evening_before(END_2026) == datetime(2026, 9, 19, 23, tzinfo=UTC)  # 6 pm CDT, 6 h before
    ten_am = datetime(2026, 9, 20, 15, tzinfo=UTC)  # 10 am CDT: still the evening before
    assert years.evening_before(ten_am) == datetime(2026, 9, 19, 23, tzinfo=UTC)
    late = datetime(2026, 9, 21, 1, 30, tzinfo=UTC)  # 8:30 pm CDT: 6 pm is only 2.5 h before, so the day before
    assert years.evening_before(late) == datetime(2026, 9, 19, 23, tzinfo=UTC)
    winter = datetime(2026, 12, 2, 6, tzinfo=UTC)  # midnight CST
    assert years.evening_before(winter) == datetime(2026, 12, 2, 0, tzinfo=UTC)  # 6 pm CST Dec 1


def test_descriptions():
    assert years.describe(info(2025, years.year_window(2025)[0], years.year_window(2025)[1]), predicted=False) == \
        "Real ERCOT prices for the whole year."
    y = info(2026, years.year_window(2026)[0], datetime(2026, 9, 21, 5, tzinfo=UTC))
    assert years.describe(y, predicted=True) == "Real prices through Sep 20, then the AI's prediction."  # docs/API.md
    assert years.describe(y, predicted=False).startswith("Real prices through Sep 20. The AI's prediction")


def test_public_years_follow_docs_api(raw):
    got = years.public_years(raw, predicted={2026: True})
    assert got == [
        {"year": 2025, "label": "2025", "description": "Real ERCOT prices for the whole year.", "real_until_utc": None},
        {"year": 2026, "label": "2026", "description": "Real prices through Sep 19, then the AI's prediction.",
         "real_until_utc": "2026-09-20T05:00:00Z"}]


def test_presets_carry_their_year_and_the_computed_start(raw):
    listed = ctl.presets(raw)
    assert [p["year"] for p in listed].count(2025) == 6 and [p["year"] for p in listed].count(2026) == 3
    assert all(list(p) == list(ctl.PRESET_KEYS) for p in listed)
    end = next(p for p in listed if p["id"] == "real_prices_end_2026")
    assert end["start_utc"] == "2026-09-19T23:00:00Z" and end["date_local"] == "2026-09-19"
    # the switch comes 72 ticks (36 s at 2 ticks/s) after the start
    assert (END_2026 - datetime.fromisoformat(end["start_utc"].replace("Z", "+00:00"))) / timedelta(minutes=5) == 72


def test_presets_of_a_year_without_prices_and_a_complete_year(raw):
    a26, b26 = years.year_window(2026)
    complete = {2025: info(2025, *years.year_window(2025)), 2026: info(2026, a26, b26)}
    ids = [p["id"] for p in ctl.presets(raw, infos=complete)]
    assert "real_prices_end_2026" not in ids and "spike_2026" in ids  # real all year: nothing ends
    missing = {2025: complete[2025], 2026: info(2026)}  # 2026 not ingested yet
    assert {p["year"] for p in ctl.presets(raw, infos=missing)} == {2025}


def test_resolve_year_and_preset(cfg, raw):
    s = ctl.resolve_settings({"year": 2026}, cfg, raw)  # a year alone replays its default preset
    assert (s["year"], s["preset"], s["start_utc"]) == (2026, "real_prices_end_2026", "2026-09-19T23:00:00Z")
    s = ctl.resolve_settings({"preset": "spike_2026"}, cfg, raw)  # a preset sets its year
    assert (s["year"], s["start_utc"]) == (2026, "2026-04-24T21:00:00Z")
    s = ctl.resolve_settings({"year": 2025, "preset": "jan1_2025"}, cfg, raw)
    assert (s["year"], s["start_utc"]) == (2025, "2025-01-01T06:00:00Z")
    s = ctl.resolve_settings({"year": 2025}, cfg, raw)
    assert (s["year"], s["preset"]) == (2025, "ai_best_evening")
    s = ctl.resolve_settings({}, cfg, raw)
    assert list(s)[:2] == ["year", "preset"] and (s["year"], s["preset"]) == (2025, "ai_best_evening")
    internal = ctl.resolve_settings({"start_utc": "2026-02-01T06:00:00Z"}, cfg, raw, allow_internal=True)
    assert (internal["year"], internal["preset"]) == (2026, "custom")


@pytest.mark.parametrize("partial, code, words", [
    ({"year": 2024}, "invalid_year", "2025 or 2026"),
    ({"year": "2026"}, "invalid_year", "whole number"),
    ({"year": True}, "invalid_year", "whole number"),
    ({"year": 2026.5}, "invalid_year", "whole number"),
    ({"year": 2026, "preset": "ai_best_evening"}, "invalid_preset", "is in 2025, not 2026"),
    ({"year": 2025, "preset": "spike_2026"}, "invalid_preset", "is in 2026, not 2025"),
    ({"preset": "spike_2024"}, "invalid_preset", "no replay called"),
])
def test_bad_years_and_presets_are_400s(cfg, raw, partial, code, words):
    with pytest.raises(ctl.ControlError) as err:
        ctl.resolve_settings(partial, cfg, raw)
    assert err.value.status == 400 and err.value.code == code and words in err.value.message


def test_a_year_that_is_not_loaded_is_refused(cfg, raw):
    only_2025 = {2025: info(2025, *years.year_window(2025)), 2026: info(2026)}
    with pytest.raises(ctl.ControlError) as err:
        ctl.resolve_settings({"year": 2026}, cfg, raw, infos=only_2025)
    assert err.value.code == "invalid_year" and "aren't loaded yet" in err.value.message


def test_real_until_ahead_of_a_start():
    assert years.real_until_ahead(datetime(2025, 5, 1, tzinfo=UTC)) == END_2026  # a 2025 run could run into it
    assert years.real_until_ahead(datetime(2026, 11, 1, tzinfo=UTC)) == END_2026  # already past: predicted from tick 0
    assert years.real_until_ahead(datetime(2027, 1, 1, 7, tzinfo=UTC)) is None
