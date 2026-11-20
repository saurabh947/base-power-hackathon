"""The backend without a database: the image serves data/frozen (src/data/freeze.py, exported from Cloud SQL
fleet-sim-db on 2026-11-19) through FORECAST_CACHE_DIR, PREDICTION_CACHE_DIR and BACKTEST_RESULTS_DIR, prices come
from data/snapshot, and full-year re-runs are switched off (BACKTEST_RERUNS=off)."""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from src.api import main
from src.config import REPO_ROOT

UTC = timezone.utc
FROZEN = REPO_ROOT / "data" / "frozen"
CITIES = ("LZ_HOUSTON", "LZ_NORTH", "LZ_CPS", "LZ_AEN")


def test_the_data_folders_can_be_set_from_the_environment(monkeypatch):
    from src.forecast import predictions
    from src.forecast.settings import load_settings

    assert load_settings(env={"FORECAST_CACHE_DIR": "data/frozen/forecasts"}).cache_dir == FROZEN / "forecasts"
    assert load_settings(env={}).cache_dir == REPO_ROOT / "data" / "cache" / "forecasts"
    monkeypatch.setenv("PREDICTION_CACHE_DIR", "/srv/predictions")
    predictions.default_cache_dir.cache_clear()
    try:
        assert str(predictions.default_cache_dir()) == "/srv/predictions"
    finally:
        monkeypatch.delenv("PREDICTION_CACHE_DIR")
        predictions.default_cache_dir.cache_clear()
    assert predictions.default_cache_dir() == REPO_ROOT / "data" / "cache" / "predictions"


def test_only_the_two_most_recent_snapshot_feeds_are_kept(monkeypatch):
    """Each year-set's feed holds every tick in memory, so a long-lived backend keeps only two."""
    from src.feeds import parquet
    from src.sim import coordinator

    built = []
    monkeypatch.setattr(parquet, "snapshot_feed", lambda years, locations: built.append(years) or object())
    monkeypatch.setenv("PRICE_SOURCE", "snapshot")
    monkeypatch.setattr(coordinator, "_FEEDS", {})
    a = coordinator.price_feed((2025,), CITIES)
    coordinator.price_feed((2024, 2025), CITIES)
    assert coordinator.price_feed((2025,), CITIES) is a  # still kept, and now the most recent
    coordinator.price_feed((2026,), CITIES)  # evicts (2024, 2025), the least recently used
    assert [k[1] for k in coordinator._FEEDS] == [(2025,), (2026,)]
    coordinator.price_feed((2024, 2025), CITIES)
    assert built == [(2025,), (2024, 2025), (2026,), (2024, 2025)]
    assert [k[1] for k in coordinator._FEEDS] == [(2026,), (2024, 2025)]


def test_full_year_reruns_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("BACKTEST_RERUNS", "off")
    main.configure(url="redis://127.0.0.1:1/0", prefix="test:frozen:")  # nothing listens on port 1
    main.state.limits.reset()
    try:
        with TestClient(main.app) as client:
            r = client.post("/backtest", json={"strategies": ["bands"], "year": 2025})
    finally:
        main.state.backend = None
    assert r.status_code == 403
    assert r.json() == {"error": "backtest_disabled",
                        "message": "Full-year re-runs are switched off on this server: its results are fixed."}


@pytest.fixture
def frozen(monkeypatch):
    """The backend image's data settings, with no database."""
    from src.data import years
    from src.forecast import predictions, provider

    if not (FROZEN / "results" / "pitch_2025.json").exists():
        pytest.skip("data/frozen is not present")
    for name in ("DATABASE_URL", "DB_HOST", "FORECAST_SOURCE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PRICE_SOURCE", "snapshot")
    monkeypatch.setenv("FORECAST_CACHE_DIR", str(FROZEN / "forecasts"))
    monkeypatch.setenv("PREDICTION_CACHE_DIR", str(FROZEN / "predictions"))
    monkeypatch.setenv("BACKTEST_RESULTS_DIR", str(FROZEN / "results"))

    def clear():
        predictions.default_cache_dir.cache_clear()
        predictions.clear_cache()
        provider.clear_cache()
        years.clear_cache()

    clear()
    yield
    clear()


def test_the_frozen_data_serves_the_published_results(frozen):
    from src.backtest import service

    for year, ai, rule in ((2025, 190.95, 123.67), (2026, 156.63, 98.7)):  # the Results screen's figures, in cents
        by_strategy = {s["strategy"]: s for s in service.pitch_results(year)["strategies"]}
        assert (by_strategy["forecast"]["per_battery_usd_year"], by_strategy["bands"]["per_battery_usd_year"]) == (ai, rule)
    gains = {y["year"]: y for y in service.ai_gains()["years"]}
    assert set(gains) == {2024, 2025, 2026}
    assert gains[2025]["uplift"]["pct"] == pytest.approx(0.544, abs=5e-4)


def test_the_frozen_data_has_forecasts_and_the_2026_prediction(frozen):
    from src.data import years
    from src.forecast import predictions, provider

    if not (FROZEN / "forecasts").is_dir() or not (FROZEN / "predictions").is_dir():
        pytest.skip("the AI files aren't in git: copy them from the backup bucket (data/frozen/README.md)")

    until = years.real_until(2026)
    assert until == datetime(2026, 9, 20, 5, tzinfo=UTC)
    for city in CITIES:
        path = predictions.load(city, max_cutoff=until)
        assert path is not None and path.cutoff == until and len(path) == 9892
        real = provider.latest_forecast(city, datetime(2025, 4, 7, 11, tzinfo=UTC))
        assert real is not None and real["issued_utc"].startswith("2025-04-07")
        virtual = provider.latest_forecast(city, datetime(2026, 10, 15, 18, tzinfo=UTC))  # after real prices end
        assert virtual is not None and virtual["issued_utc"].startswith("2026-10-15")
    assert provider.latest_forecast("ERCOT", datetime(2025, 4, 7, 11, tzinfo=UTC), "demand") is not None
    public = {y["year"]: y for y in years.public_years()}
    assert public[2026]["description"] == "Real prices through Sep 19, then the AI's prediction."
