"""TimesFM itself, on real snapshot contexts. Skipped unless the weights are present.

`make forecast-weights` (or any job run) fetches them into models/. These
tests run each model once on CPU (~5 s). Set FORECAST_SKIP_MODEL_TESTS=1 to
skip them even when the weights are there. Registry and loader checks at the
bottom need no weights.
"""
import importlib.util
import os
from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

from src.forecast import inputs
from src.forecast.model import MODELS, available_cpus, ensure_weights, spec_for, weights_present
from src.forecast.settings import load_settings

SETTINGS = load_settings(env={})


def needs(name: str):
    reason = None
    if os.environ.get("FORECAST_SKIP_MODEL_TESTS"):
        reason = "FORECAST_SKIP_MODEL_TESTS is set"
    elif importlib.util.find_spec("timesfm") is None or importlib.util.find_spec("torch") is None:
        reason = "the forecast extra is not installed (uv sync --extra forecast)"
    elif not weights_present(MODELS[name], SETTINGS.weights_dir):
        reason = f"{name} weights not in {SETTINGS.weights_dir} (make forecast-weights)"
    return pytest.mark.skipif(reason is not None, reason=reason or "")


@pytest.fixture(scope="module")
def real_contexts():
    from src.data import series

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("SERIES_BACKEND", "snapshot")
        t = pd.Timestamp("2025-05-16T21:00Z")
        raw = series.rt_prices("LZ_HOUSTON", t - timedelta(days=12), t + timedelta(days=2))
        grid = inputs.Grid.from_series(raw, 15)
        da = inputs.da_on_grid(series.da_prices("LZ_HOUSTON", t - timedelta(days=12), t + timedelta(days=2)), grid)
    times = [t, t + pd.Timedelta(hours=1)]
    return (grid, [grid.context(x, 512) for x in times],
            [inputs.da_covariate(da, grid, x, 512, 96) for x in times])


def _check(q, n, horizon):
    assert q.shape == (n, horizon, 3) and q.dtype == np.float32
    assert np.isfinite(q).all()
    assert (q[..., 0] <= q[..., 1] + 1e-4).all() and (q[..., 1] <= q[..., 2] + 1e-4).all()


@needs("timesfm-3.0")
def test_timesfm3_on_real_prices(real_contexts):
    from src.forecast.model import configure_torch, load_forecaster

    configure_torch(4)
    _, contexts, covariates = real_contexts
    model = load_forecaster("timesfm-3.0", SETTINGS.weights_dir, download=False, batch_size=8)
    uni = model.forecast(contexts, 96)
    _check(uni, 2, 96)
    with_da = model.forecast(contexts, 96, covariates)
    _check(with_da, 2, 96)
    assert not np.allclose(uni, with_da)  # the covariate reaches the model
    assert 0.0 < float(np.median(uni[..., 1])) < 1000.0  # $/MWh scale, not normalised units


@needs("timesfm-2.5")
def test_timesfm25_fallback_on_real_prices(real_contexts):
    from src.forecast.model import configure_torch, load_forecaster

    configure_torch(4)
    _, contexts, _ = real_contexts
    model = load_forecaster("timesfm-2.5", SETTINGS.weights_dir, download=False, batch_size=2,
                            max_context=512, max_horizon=96)
    _check(model.forecast(contexts, 96), 2, 96)
    with pytest.raises(NotImplementedError):
        model.forecast(contexts, 96, [np.zeros((1, 608), np.float32)] * 2)


# --- no weights needed ------------------------------------------------------------------------


def test_registry_pins_revisions_and_licences():
    assert set(MODELS) == {"timesfm-3.0", "timesfm-2.5"}
    assert "non-commercial" in MODELS["timesfm-3.0"].licence.lower()
    assert MODELS["timesfm-2.5"].licence == "Apache-2.0"
    assert all(len(m.revision) == 40 for m in MODELS.values())
    with pytest.raises(ValueError):
        spec_for("timesfm-9")


def test_missing_weights_without_download_is_a_clear_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="make forecast-weights"):
        ensure_weights(MODELS["timesfm-2.5"], tmp_path, download=False)


def test_available_cpus_is_positive():
    assert available_cpus() >= 1
