"""The AI's prediction for the rest of a year (src/forecast/predict.py, predictions.py) and the replay across the cutoff
(src/feeds/predicted.py, the virtual forecasts in src/forecast/provider.py).

FakeModel is a test double, not TimesFM: its "forecast" continues the last context value by +1 per step, so which
context each chained chunk saw can be read back from its output. Prices in the synthetic series are SYNTHETIC too,
and every interval AFTER the cutoff is poisoned (1e6) to prove nothing past it is ever fed to the model.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from src.feeds.memory import InMemoryPriceFeed
from src.feeds.predicted import PredictedFeed
from src.forecast import predict, predictions, store
from src.forecast.settings import load_settings

UTC = timezone.utc
CUTOFF = pd.Timestamp("2026-12-10T06:00Z")  # midnight CST: three 7-day chunks to the year's end
YEAR_END = pd.Timestamp("2027-01-01T06:00Z")
POISON = 1e6
LOCS = ("LZ_HOUSTON", "LZ_NORTH", "LZ_CPS", "LZ_AEN")


class FakeModel:
    """p50[j] = last context value + j + 1; p10/p90 = p50 -/+ 5 (unsorted on purpose: the job sorts)."""

    def __init__(self):
        self.calls = []

    def forecast(self, contexts, horizon, covariates=None):
        assert covariates is None  # univariate
        self.calls.append(([np.array(c, copy=True) for c in contexts], horizon))
        out = np.empty((len(contexts), horizon, 3), dtype=np.float32)
        for k, c in enumerate(contexts):
            p50 = float(c[-1]) + np.arange(1, horizon + 1, dtype=np.float32)
            out[k, :, 0], out[k, :, 1], out[k, :, 2] = p50 + 5, p50, p50 - 5
        return out


def synthetic_fetch(requested):
    """SYNTHETIC real prices: 20 + (interval index % 96) / 4, with every interval from the cutoff on poisoned;
    LZ_NORTH even has 2 extra days of (poisoned) data past the cutoff, as a city published further would."""
    def fetch(location, lo, hi):
        requested.append((location, pd.Timestamp(lo), pd.Timestamp(hi)))
        end = CUTOFF + pd.Timedelta(days=2) if location == "LZ_NORTH" else CUTOFF
        ts = pd.date_range(CUTOFF - pd.Timedelta(days=30), end, freq="15min", tz="UTC", inclusive="left")
        values = 20.0 + (np.arange(len(ts)) % 96) / 4.0
        values[ts >= CUTOFF] = POISON
        s = pd.Series(values, index=ts, name=location)
        return s[(s.index >= pd.Timestamp(lo)) & (s.index < pd.Timestamp(hi))]
    return fetch


def plan(context_steps=256, chunk_days=7, limit_days=None):
    return predict.make_plan(2026, load_settings(), predict.PredictSettings(context_steps, chunk_days), locations=LOCS,
                             out="parquet", cutoff=CUTOFF.to_pydatetime(), limit_days=limit_days)


def test_chained_chunks_feed_p50_back_and_never_look_past_the_cutoff():
    p = plan()
    assert (p.steps, p.chunks, p.chunk_steps) == (2112, 4, 672)  # 22 days: 7 + 7 + 7 + 1
    requested, model = [], FakeModel()
    result = predict.run(p, model=model, fetch=synthetic_fetch(requested),
                         issued_at=datetime(2026, 12, 10, 12, tzinfo=UTC))
    # no look-ahead: nothing at or after the cutoff was asked for, or fed
    assert requested and all(hi <= CUTOFF for _, _, hi in requested)
    fed = result["contexts_fed"]
    assert len(fed) == len(model.calls) == 4 and [h for _, h in model.calls] == [672, 672, 672, 96]
    assert all(len(ctx) == len(LOCS) for ctx, _ in model.calls)  # the cities go through the model together
    for c in fed:
        for loc, ctx in c.items():
            assert len(ctx) == 256 and np.isfinite(ctx).all() and ctx.max() < POISON, loc
    last_real = 20.0 + ((30 * 96 - 1) % 96) / 4.0  # the interval that ENDED at the cutoff
    assert all(ctx[-1] == last_real for ctx in fed[0].values())
    paths = result["paths"]
    for loc in LOCS:
        path = paths[loc]
        # chaining: chunk c's context ends with chunk c-1's p50
        for c in range(1, 4):
            prev = path.p50[path.chunk == c - 1]
            assert np.array_equal(fed[c][loc][-min(256, len(prev)):], prev[-256:].astype(np.float32))
        assert path.p50[0] == last_real + 1 and path.p50[-1] == last_real + 2112  # one continuous +1 path
        # the path: from the cutoff to Dec 31 24:00 Central, every 15 minutes, p10 <= p50 <= p90, cents
        ts = pd.to_datetime(path.ts, utc=True)
        assert ts[0] == CUTOFF and ts[-1] + pd.Timedelta(minutes=15) == YEAR_END and path.end == YEAR_END
        assert (np.diff(path.ts) == 15 * 60 * 10**9).all() and len(path) == 2112
        assert (path.p10 <= path.p50).all() and (path.p50 <= path.p90).all()
        assert (path.p90 - path.p10 == 10).all() and list(np.unique(path.chunk)) == [0, 1, 2, 3]
        assert path.cutoff == CUTOFF and path.model == load_settings().model
        assert path.issued_at == pd.Timestamp("2026-12-10T12:00Z")


class ShrinkingBand(FakeModel):
    """The real model's failure mode: fed its own smooth p50, its band collapses (here +-7 +-(j%3), then +-1)."""

    def forecast(self, contexts, horizon, covariates=None):
        out = super().forecast(contexts, horizon, covariates)
        width = (7.0 + np.arange(horizon) % 3) if len(self.calls) == 1 else np.ones(horizon)
        out[:, :, 0] = out[:, :, 1] - width
        out[:, :, 2] = out[:, :, 1] + width
        return out


def test_later_chunks_keep_at_least_the_first_weeks_band():
    result = predict.run(plan(), model=ShrinkingBand(), fetch=synthetic_fetch([]))
    for path in result["paths"].values():
        first = path.chunk == 0
        width0 = (path.p90 - path.p10)[first]
        assert set(np.unique(width0)) == {14.0, 16.0, 18.0}
        for c in (1, 2, 3):
            mine = path.chunk == c
            n = int(mine.sum())
            assert np.array_equal((path.p90 - path.p10)[mine], width0[:n]), c  # floored at the same offsets
            assert np.array_equal(path.p50[mine] - path.p10[mine], (path.p50 - path.p10)[first][:n])
        assert path.p50[-1] == path.p50[0] + 2111  # the p50 path is untouched


def test_the_plan_starts_where_real_prices_end(monkeypatch):
    """From the committed snapshot (2026 published on Sep 20): the cutoff and the year's end."""
    from src.data import years

    monkeypatch.setenv("SERIES_BACKEND", "snapshot")
    years.clear_cache()
    p = predict.make_plan(2026, load_settings(), predict.predict_settings(), out="parquet")
    assert p.cutoff == pd.Timestamp("2026-09-20T05:00Z") and p.end == YEAR_END
    assert (p.steps, p.chunks, p.context_steps) == (9892, 15, 4096) and p.locations == LOCS  # 103 days + 1 h
    with pytest.raises(SystemExit, match="real for the whole year"):
        predict.make_plan(2025, load_settings(), predict.predict_settings(), out="parquet")
    short = predict.make_plan(2026, load_settings(), predict.predict_settings(), out="parquet", limit_days=2)
    assert short.end == pd.Timestamp("2026-09-22T05:00Z") and short.chunks == 1


def test_real_contexts_from_the_snapshot_end_at_the_cutoff(monkeypatch):
    monkeypatch.setenv("SERIES_BACKEND", "snapshot")
    p = predict.make_plan(2026, load_settings(), predict.PredictSettings(512, 7), out="parquet",
                          cutoff=datetime(2026, 9, 20, 5, tzinfo=UTC), limit_days=1)
    ctx = predict.load_contexts(p)
    from src.data import series

    for loc in LOCS:
        real = series.rt_prices(loc, datetime(2026, 9, 10, tzinfo=UTC), datetime(2026, 9, 21, tzinfo=UTC))
        assert len(ctx[loc]) == 512 and ctx[loc][-1] == np.float32(real.iloc[-1])  # the last real interval
        assert np.array_equal(ctx[loc], real.iloc[-512:].to_numpy(np.float32))


def make_path(loc, cutoff=CUTOFF, n=2112, base=50.0, issued="2026-12-10T12:00Z"):
    ts = pd.Timestamp(cutoff).value + np.arange(n, dtype=np.int64) * 15 * 60 * 10**9
    p50 = np.round(base + np.arange(n) * 0.25, 2)
    return predictions.PredictionPath(loc, "timesfm-3.0", pd.Timestamp(cutoff), pd.Timestamp(issued), ts,
                                      p50 - 3, p50, p50 + 3, (np.arange(n) // 672).astype(np.int16))


def test_parquet_paths_round_trip_and_the_same_cutoff_replaces_its_path(tmp_path):
    a = make_path("LZ_HOUSTON")
    assert predictions.write([a], out="parquet", cache_dir=tmp_path) == 2112
    got = predictions.load("LZ_HOUSTON", model="timesfm-3.0", source="parquet", cache_dir=tmp_path, refresh=True)
    for field in ("ts", "p10", "p50", "p90", "chunk"):
        assert np.array_equal(getattr(got, field), getattr(a, field)), field
    assert (got.cutoff, got.issued_at) == (a.cutoff, a.issued_at)
    predictions.write([make_path("LZ_HOUSTON", base=70.0)], out="parquet", cache_dir=tmp_path)  # same cutoff
    again = predictions.load("LZ_HOUSTON", model="timesfm-3.0", source="parquet", cache_dir=tmp_path, refresh=True)
    assert again.p50[0] == 70.0 and len(list((tmp_path / "timesfm-3.0" / "LZ_HOUSTON").glob("*.parquet"))) == 1
    later = CUTOFF + pd.Timedelta(days=7)
    predictions.write([make_path("LZ_HOUSTON", cutoff=later, n=1440, base=90.0)], out="parquet", cache_dir=tmp_path)
    kw = dict(model="timesfm-3.0", source="parquet", cache_dir=tmp_path, refresh=True)
    assert predictions.load("LZ_HOUSTON", max_cutoff=later, **kw).p50[0] == 90.0  # the latest usable cutoff
    assert predictions.load("LZ_HOUSTON", max_cutoff=later - pd.Timedelta(minutes=15), **kw).p50[0] == 70.0
    assert predictions.load("LZ_HOUSTON", max_cutoff=CUTOFF - pd.Timedelta(days=1), **kw) is None
    assert predictions.ready(["LZ_HOUSTON"], later, **kw) and not predictions.ready(["LZ_HOUSTON", "LZ_AEN"], later, **kw)


def test_virtual_forecasts_are_hourly_slices_of_the_path():
    path = make_path("LZ_NORTH", n=200)
    v = predictions.virtual_forecasts(path, start=CUTOFF, horizon_steps=96, every_minutes=60)
    assert list(v.columns) == store.COLUMNS and len(v) == 50  # 200 steps = 50 hours
    assert (pd.DatetimeIndex(v["issued_at"]) == pd.date_range(CUTOFF, periods=50, freq="h", tz="UTC")).all()
    assert (v["start_ts"] == v["issued_at"]).all() and (v["step_minutes"] == 15).all()
    for h in (0, 10, 49):
        row = v.iloc[h]
        take = path.p50[4 * h:4 * h + 96]
        assert np.array_equal(row["p50"][:len(take)], take.astype(np.float32))
        assert np.isnan(row["p50"][len(take):]).all()  # past the path's end: NaN (the strategy skips them)
        assert np.array_equal(row["p10"][:len(take)], (path.p10[4 * h:4 * h + 96]).astype(np.float32))
    later = predictions.virtual_forecasts(path, start=CUTOFF + pd.Timedelta(minutes=20), horizon_steps=96)
    assert later["issued_at"].iloc[0] == CUTOFF + pd.Timedelta(hours=1)  # whole hours only


def real_rows(loc, start, n, price=30.0):
    from src.core.models import PriceTick

    return [PriceTick((start + pd.Timedelta(minutes=15 * i)).to_pydatetime(), loc, price + i, 1.0, None)
            for i in range(n)]


def test_the_replay_feed_is_real_before_the_cutoff_and_the_p50_after():
    real = InMemoryPriceFeed.from_intervals(real_rows("LZ_HOUSTON", CUTOFF - pd.Timedelta(hours=2), 16))  # 2 h past
    path = make_path("LZ_HOUSTON", n=8)
    feed = PredictedFeed(real, CUTOFF.to_pydatetime(), {"LZ_HOUSTON": path})
    ticks = list(feed.ticks((CUTOFF - pd.Timedelta(hours=1)).to_pydatetime(),
                            (CUTOFF + pd.Timedelta(hours=1)).to_pydatetime(), "LZ_HOUSTON"))
    assert len(ticks) == 24 and all(b.ts - a.ts == timedelta(minutes=5) for a, b in zip(ticks, ticks[1:]))
    before, after = ticks[:12], ticks[12:]
    assert all(t.ts < CUTOFF and t.demand_mw == 1.0 for t in before)  # real, with its context
    assert [t.price_usd_mwh for t in before[::3]] == [30.0 + 4 + i for i in range(4)]
    assert all(t.ts >= CUTOFF and t.demand_mw is None and t.fuel_mix is None for t in after)
    # 15-min held 3 ticks; the real rows past the cutoff (priced 38-45 here, 1 h of them) are never used
    assert [t.price_usd_mwh for t in after] == [float(path.p50[i // 3]) for i in range(12)]
    assert not {t.price_usd_mwh for t in after} & {30.0 + i for i in range(8, 16)}
    # a city without a prediction ends at the cutoff; a window inside the prediction starts mid-interval
    assert list(feed.ticks(CUTOFF.to_pydatetime(), (CUTOFF + pd.Timedelta(hours=1)).to_pydatetime(), "LZ_NORTH")) == []
    mid = list(feed.ticks((CUTOFF + pd.Timedelta(minutes=5)).to_pydatetime(),
                          (CUTOFF + pd.Timedelta(minutes=20)).to_pydatetime(), "LZ_HOUSTON"))
    assert [t.price_usd_mwh for t in mid] == [path.p50[0], path.p50[0], path.p50[1]]


def test_forecast_book_swaps_real_forecasts_after_the_cutoff_for_virtual_ones(tmp_path, monkeypatch):
    """ForecastBook.load: real forecasts before real_until_utc, virtual ones (slices of the prediction) after it,
    including where a stale real forecast had been stored."""
    from src.data import years
    from src.forecast import provider

    monkeypatch.delenv("PRICE_SOURCE", raising=False)
    years.clear_cache()
    cutoff = pd.Timestamp("2026-09-20T05:00Z")  # the committed snapshot's real_until_utc
    rows = []
    for h in range(-6, 3):  # 6 real forecasts before the cutoff, and 3 stale ones after it
        t = cutoff + pd.Timedelta(hours=h)
        rows.append(store.ForecastRow(t, "LZ_AEN", "rt_price", "timesfm-3.0", t, 15, np.full(96, 1.0, np.float32),
                                      np.full(96, 2.0, np.float32), np.full(96, 3.0, np.float32)))
    store.ParquetSink(tmp_path / "fc").write(rows)
    predictions.write([make_path("LZ_AEN", cutoff=cutoff, n=96 * 3)], out="parquet", cache_dir=tmp_path / "pred")
    monkeypatch.setattr(predictions, "default_cache_dir", lambda: tmp_path / "pred")
    predictions.clear_cache()
    book = provider.ForecastBook.load("LZ_AEN", model="timesfm-3.0", source="parquet", cache_dir=tmp_path / "fc",
                                      virtual=True)
    raw_book = provider.ForecastBook.load("LZ_AEN", model="timesfm-3.0", source="parquet", cache_dir=tmp_path / "fc")
    assert len(raw_book) == 9  # without virtual=True: exactly what is stored
    issued = pd.to_datetime(book.issued, utc=True)
    assert (issued[:6] < cutoff).all() and (book.p50[:6] == 2.0).all()  # the real ones
    assert issued[6] == cutoff and len(book) == 6 + 72  # then one virtual forecast per hour of the 3-day path
    assert np.array_equal(book.p50[6][:96], make_path("LZ_AEN", cutoff=cutoff, n=96 * 3).p50[:96].astype(np.float32))
    assert provider.virtual_cutoff(cutoff + pd.Timedelta(hours=5)) == cutoff
    assert provider.virtual_cutoff(cutoff - pd.Timedelta(minutes=1)) is None
    got = provider.latest_forecast("LZ_AEN", cutoff + pd.Timedelta(hours=5, minutes=40), model="timesfm-3.0",
                                   source="parquet", cache_dir=tmp_path / "fc")
    assert got["issued_utc"] == "2026-09-20T10:00:00Z" and got["points"][0]["ts_utc"] == "2026-09-20T10:00:00Z"
    assert got["points"][0]["p50"] == round(float(make_path("LZ_AEN", cutoff=cutoff).p50[20]), 2)
    before = provider.latest_forecast("LZ_AEN", cutoff - pd.Timedelta(minutes=30), model="timesfm-3.0",
                                      source="parquet", cache_dir=tmp_path / "fc")
    assert before["issued_utc"] == "2026-09-20T04:00:00Z" and before["points"][0]["p50"] == 2.0  # still real
