"""TimesFM loader behind a config switch: forecast.model in config.yaml, or env FORECAST_MODEL.

- timesfm-3.0 (default): google/timesfm-3.0-pytorch, 330M parameters. The
  weights are under the TimesFM Non-Commercial License v1.0: non-commercial,
  non-production use only. The human OK'd them for the demo (RUNLOG).
- timesfm-2.5: google/timesfm-2.5-200m-pytorch, 231M parameters with its
  quantile head, Apache-2.0. This is what production would use.

Both run on CPU torch, because the Cloud Run job has no GPU. Weights are
pinned to a Hugging Face revision and cached under forecast.weights_dir
(models/ locally, gitignored; the forecast image bakes them into
/app/models). No Hugging Face account or token is needed.

Every forecaster returns float32 quantiles shaped (n, horizon, 3): p10, p50,
p90. torch and timesfm are imported lazily, inside the loaders, so this module
is importable with only the main dependencies installed.
"""
from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

import numpy as np

log = logging.getLogger(__name__)

QUANTILES = (0.1, 0.5, 0.9)


@dataclass(frozen=True)
class ModelSpec:
    name: str
    repo: str  # Hugging Face repo id
    revision: str  # pinned commit, for reproducible forecasts
    licence: str
    params_millions: int
    family: str  # timesfm3 | timesfm2p5

    @property
    def dirname(self) -> str:
        return self.repo.split("/")[-1]


MODELS: dict[str, ModelSpec] = {
    "timesfm-3.0": ModelSpec(
        "timesfm-3.0", "google/timesfm-3.0-pytorch", "43046b85ec22d584a13f8098c2ed39c889e129c2",
        "TimesFM Non-Commercial License v1.0 (non-commercial, non-production use only)", 330, "timesfm3",
    ),
    "timesfm-2.5": ModelSpec(
        "timesfm-2.5", "google/timesfm-2.5-200m-pytorch", "1d952420fba87f3c6dee4f240de0f1a0fbc790e3",
        "Apache-2.0", 231, "timesfm2p5",
    ),
}


class Forecaster(Protocol):
    spec: ModelSpec
    supports_covariates: bool

    def forecast(
        self,
        contexts: Sequence[np.ndarray],
        horizon: int,
        covariates: Sequence[np.ndarray] | None = None,
    ) -> np.ndarray:
        """Quantiles (n, horizon, 3) as float32: p10, p50, p90.

        contexts[i] is 1-D, oldest first; NaN marks a missing interval.
        covariates[i], if given, is (channels, len(contexts[i]) + horizon):
        known-future covariates over the context and the horizon.
        """
        ...


def spec_for(name: str) -> ModelSpec:
    try:
        return MODELS[name]
    except KeyError:
        raise ValueError(f"unknown model {name!r}; known: {', '.join(MODELS)}") from None


def weights_path(spec: ModelSpec, weights_dir: Path) -> Path:
    return Path(weights_dir) / spec.dirname


def weights_present(spec: ModelSpec, weights_dir: Path) -> bool:
    return (weights_path(spec, weights_dir) / "model.safetensors").is_file()


def ensure_weights(spec: ModelSpec, weights_dir: Path, download: bool = True) -> Path:
    """Local directory holding the pinned checkpoint, downloading it if allowed."""
    path = weights_path(spec, weights_dir)
    if weights_present(spec, weights_dir):
        return path
    if not download:
        raise FileNotFoundError(
            f"{spec.name} weights not found in {path}; run `make forecast-weights` "
            "or set forecast.download: true"
        )
    from huggingface_hub import snapshot_download

    log.info("downloading %s@%s to %s", spec.repo, spec.revision[:8], path)
    snapshot_download(spec.repo, revision=spec.revision, local_dir=str(path))
    return path


def available_cpus() -> int:
    """CPUs this process may use: the cgroup quota if set (containers), else the affinity mask."""
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()[:2]
        if quota != "max":
            return max(1, math.floor(int(quota) / int(period)))
    except (OSError, ValueError):
        pass
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except AttributeError:  # macOS
        return max(1, os.cpu_count() or 1)


def configure_torch(threads: int = 0) -> int:
    """Pin torch's intra-op threads (0 = available_cpus()). Returns the count used."""
    import torch

    n = threads or available_cpus()
    torch.set_num_threads(n)
    try:
        torch.set_num_interop_threads(1)
    except RuntimeError:  # already set, or parallel work already ran
        pass
    return n


def load_forecaster(
    name: str,
    weights_dir: Path,
    *,
    download: bool = True,
    device: str = "cpu",
    batch_size: int = 32,
    max_context: int = 2048,
    max_horizon: int = 128,
) -> Forecaster:
    """Build the named forecaster. max_context/max_horizon bound what 2.5 compiles for."""
    spec = spec_for(name)
    path = ensure_weights(spec, weights_dir, download)
    if spec.family == "timesfm3":
        return TimesFM3(spec, path, device=device, batch_size=batch_size)
    return TimesFM25(spec, path, batch_size=batch_size, max_context=max_context, max_horizon=max_horizon)


class TimesFM3:
    """TimesFM 3.0 through timesfm3.torch.TimesFM3Forecaster (deciles 0.1..0.9)."""

    supports_covariates = True

    def __init__(self, spec: ModelSpec, path: Path, *, device: str = "cpu", batch_size: int = 32) -> None:
        from timesfm3.torch import TimesFM3Forecaster

        self.spec = spec
        self._model = TimesFM3Forecaster.from_pretrained(
            str(path), device=device, per_core_batch_size=batch_size, local_files_only=True
        )
        q = [round(x, 2) for x in self._model.config.quantiles]
        self._index = [q.index(x) for x in QUANTILES]

    def forecast(self, contexts, horizon, covariates=None) -> np.ndarray:
        if not contexts:
            return np.zeros((0, horizon, 3), dtype=np.float32)
        outs = self._model.predict_batch(
            contexts=[np.asarray(c, dtype=np.float32) for c in contexts],
            horizon=horizon,
            past_future_covariates=None if covariates is None else [np.asarray(c, np.float32) for c in covariates],
            return_quantiles=True,
            sort_quantiles=True,
        )
        q = np.stack([o.quantiles for o in outs])  # (n, horizon, 9)
        return np.ascontiguousarray(q[..., self._index], dtype=np.float32)


class TimesFM25:
    """TimesFM 2.5 through timesfm.TimesFM_2p5_200M_torch (mean + deciles; 1 = p10, 5 = p50, 9 = p90).

    Covariates on 2.5 need the XReg path (JAX), so this wrapper is univariate.
    """

    supports_covariates = False

    def __init__(self, spec: ModelSpec, path: Path, *, batch_size: int, max_context: int, max_horizon: int) -> None:
        import timesfm

        self.spec = spec
        # torch.compile needs a C++ toolchain, which the slim image lacks; eager is fine on CPU.
        self._model = timesfm.TimesFM_2p5_200M_torch.from_pretrained(str(path), torch_compile=False)
        self._model.compile(timesfm.ForecastConfig(
            max_context=max_context,
            max_horizon=max_horizon,
            normalize_inputs=True,
            use_continuous_quantile_head=True,
            force_flip_invariance=True,
            infer_is_positive=True,
            fix_quantile_crossing=True,
            per_core_batch_size=batch_size,
        ))

    def forecast(self, contexts, horizon, covariates=None) -> np.ndarray:
        if covariates is not None:
            raise NotImplementedError("timesfm-2.5 runs univariate here; set rt_price.covariates: none")
        if not contexts:
            return np.zeros((0, horizon, 3), dtype=np.float32)
        import torch

        with torch.inference_mode():
            # forecast() pads the list it is given in place, so hand it a fresh one.
            _, q = self._model.forecast(horizon=horizon, inputs=[np.asarray(c, np.float64) for c in contexts])
        return np.ascontiguousarray(q[..., [1, 5, 9]], dtype=np.float32)
