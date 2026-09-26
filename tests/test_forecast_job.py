"""The rolling-origin job on real snapshot prices, with a FAKE model (no torch needed).

FakeModel returns SYNTHETIC quantiles (persistence of the last context value)
and records every input, so the tests can prove what the job fed the model.
"""
import numpy as np
import pandas as pd
import pytest

from src.data import series
from src.forecast import job, store
from src.forecast.model import spec_for
from src.forecast.provider import ForecastBook
from src.forecast.settings import load_settings


class FakeModel:
    spec = spec_for("timesfm-3.0")
    supports_covariates = True

    def __init__(self):
        self.calls = []

    def forecast(self, contexts, horizon, covariates=None):
        self.calls.append((list(contexts), horizon, covariates))
        q = np.zeros((len(contexts), horizon, 3), dtype=np.float32)
        for k, c in enumerate(contexts):  # SYNTHETIC output: persistence +/- 1
            last = np.asarray(c)[np.isfinite(c)][-1]
            q[k] = np.array([last - 1, last, last + 1], dtype=np.float32)
        return q


@pytest.fixture(autouse=True)
def snapshot_backend(monkeypatch):
    monkeypatch.setenv("SERIES_BACKEND", "snapshot")
    monkeypatch.delenv("CLOUD_RUN_TASK_INDEX", raising=False)
    monkeypatch.delenv("CLOUD_RUN_TASK_COUNT", raising=False)


def make(tmp_path, *argv):
    args = job.build_parser().parse_args(["--out", "parquet", "--cache-dir", str(tmp_path), *argv])
    return job.make_plan(args, job.apply_overrides(load_settings(), args))


def test_one_day_writes_every_issue_time_with_no_look_ahead(tmp_path):
    plan = make(tmp_path, "--start", "2025-05-16", "--limit-days", "1")
    fake = FakeModel()
    result = job.run(plan, model=fake)
    n_loc = len(load_settings().locations)
    assert result["forecasts"] == 24 * n_loc + 24  # hourly RT for every city + hourly demand

    houston = ForecastBook.load("LZ_HOUSTON", model="timesfm-3.0", source="parquet", cache_dir=tmp_path)
    assert len(houston) == 24 and houston.horizon == 96 and houston.step_minutes == 15
    assert (houston.start == houston.issued).all()  # hourly issues sit on the 15-min grid
    demand = ForecastBook.load("ERCOT", "demand", model="timesfm-3.0", source="parquet", cache_dir=tmp_path)
    assert len(demand) == 24 and demand.horizon == 48 and demand.step_minutes == 60

    # Persistence output = the last context value: it must be the price of the interval ending at T.
    real = series.rt_prices("LZ_HOUSTON", pd.Timestamp("2025-05-15T00:00Z"), pd.Timestamp("2025-05-18T00:00Z"))
    for i in range(len(houston)):
        issued = pd.Timestamp(int(houston.issued[i]), tz="UTC")
        last_ended = real[real.index + pd.Timedelta(minutes=15) <= issued].iloc[-1]
        assert houston.p50[i][0] == pytest.approx(last_ended, abs=1e-3)

    rt_calls = [c for c in fake.calls if c[1] == 96]
    for contexts, _, covariates in rt_calls:
        assert all(len(c) == 512 for c in contexts)  # config rt_price.context_steps
        assert covariates is not None and all(cov.shape == (1, 512 + 96) for cov in covariates)  # DA covariate


def test_a_model_without_covariates_runs_univariate(tmp_path):
    """FORECAST_MODEL=timesfm-2.5 alone must work, although the config asks for the DA covariate."""
    plan = make(tmp_path, "--start", "2025-05-16", "--limit-days", "1", "--series", "rt_price")
    fake = FakeModel()
    fake.supports_covariates = False
    assert job.run(plan, model=fake)["forecasts"] == 24 * len(load_settings().locations)
    assert fake.calls and all(cov is None for _, _, cov in fake.calls)


def test_resume_skips_what_is_stored(tmp_path):
    plan = make(tmp_path, "--start", "2025-05-16", "--limit-days", "1", "--series", "rt_price")
    job.run(plan, model=FakeModel())
    again = FakeModel()
    result = job.run(plan, model=again)
    stored = 24 * len(load_settings().locations)
    assert result["forecasts"] == 0 and result["skipped_existing"] == stored and again.calls == []


def test_demand_skips_issue_times_with_too_little_history(tmp_path):
    plan = make(tmp_path, "--start", "2025-01-01", "--limit-days", "4", "--series", "demand")
    result = job.run(plan, model=FakeModel())
    # load history starts 2025-01-01 00:00 CST; min_context_steps = 72 hours
    assert result["skipped_short"] == 72
    assert result["forecasts"] == 4 * 24 - 72


def test_full_year_plan_counts():
    args = job.build_parser().parse_args(["--year", "2025", "--out", "parquet"])
    plan = job.make_plan(args, job.apply_overrides(load_settings(), args))
    assert plan.start == pd.Timestamp("2025-01-01T06:00Z") and plan.end == pd.Timestamp("2026-01-01T06:00Z")
    d = job.dry_run(plan)
    locations = load_settings().locations
    assert d["forecasts"] == {**{f"rt_price/{loc}": 8760 for loc in locations}, "demand/ERCOT": 8760}
    # the budget the job was sized for (docs/notes/forecast.md) was 2 cities; each city adds the same RT share
    two = job.dry_run(job.make_plan(*(lambda a: (a, job.apply_overrides(load_settings(), a)))(
        job.build_parser().parse_args(["--year", "2025", "--out", "parquet", "--locations", *locations[:2]]))))
    demand = job.dry_run(job.make_plan(*(lambda a: (a, job.apply_overrides(load_settings(), a)))(
        job.build_parser().parse_args(["--year", "2025", "--out", "parquet", "--series", "demand"]))))["tflop"]
    assert 400 < two["tflop"] < 600
    per_city = (two["tflop"] - demand) / 2
    assert d["tflop"] == pytest.approx(demand + per_city * len(locations), rel=1e-3)


def test_cloud_run_tasks_split_the_days_contiguously(monkeypatch):
    spans = []
    for index in range(3):
        monkeypatch.setenv("CLOUD_RUN_TASK_INDEX", str(index))
        monkeypatch.setenv("CLOUD_RUN_TASK_COUNT", "3")
        args = job.build_parser().parse_args(["--year", "2025", "--out", "parquet"])
        plan = job.make_plan(args, load_settings())
        spans.append((plan.start, plan.end))
    assert spans[0][0] == pd.Timestamp("2025-01-01T06:00Z") and spans[-1][1] == pd.Timestamp("2026-01-01T06:00Z")
    assert all(a[1] == b[0] for a, b in zip(spans, spans[1:]))


def test_every_minutes_override_must_fit_both_grids(tmp_path):
    with pytest.raises(SystemExit):
        make(tmp_path, "--every-minutes", "45")  # not a multiple of demand's 60 minutes
    plan = make(tmp_path, "--every-minutes", "1740")
    assert plan.settings.rt_price.every_minutes == plan.settings.demand.every_minutes == 1740


def test_chunks_fill_batches(tmp_path):
    import math

    assert job.chunk_days(make(tmp_path)) == job.CHUNK_DAYS  # hourly: 4 days = 4 x 24 x cities RT forecasts
    sparse = make(tmp_path, "--every-minutes", "1740")  # sparse sampling: longer chunks, still ~4 full batches
    per_day = len(sparse.locations) * 1440 / 1740
    assert job.chunk_days(sparse) == math.ceil(4 * sparse.settings.batch_size / per_day) > job.CHUNK_DAYS


def test_parquet_rows_are_upserts(tmp_path):
    sink = store.ParquetSink(tmp_path)
    t = pd.Timestamp("2025-05-16T20:00Z")
    row = store.ForecastRow(t, "LZ_HOUSTON", "rt_price", "m", t, 15, *(np.ones(96, np.float32),) * 3)
    sink.write([row])
    sink.write([row])
    assert len(store.read_parquet(tmp_path, "m", "rt_price", "LZ_HOUSTON")) == 1
