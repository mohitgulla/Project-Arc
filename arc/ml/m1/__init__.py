"""Model M1 (E27.2, D126): per-ticker move & volatility distribution forecast.

Calibrated quantiles of the underlying's log return and realised vol at horizons of
5 / 10 / 21 / 42 sessions, structure-agnostic. Modules:

* :mod:`~arc.ml.m1.targets`: the labels (pure, forward-looking by design).
* :mod:`~arc.ml.m1.baselines`: HAR per horizon, the historical-quantile model, the
  implied-vol lognormal and the pooled HAR-IV regression (pure).
* :mod:`~arc.ml.m1.conformal`: per-level conformal calibration (CQR), sorting.
* :mod:`~arc.ml.m1.metrics`: pinball / CRPS / coverage / Diebold-Mariano / QLIKE / IC.
* :mod:`~arc.ml.m1.dataset`: builds the dev dataset from the E27.1 feature store (I/O).
* :mod:`~arc.ml.m1.train`: walk-forward training, the pre-registered keep rule and the
  artefacts under ``data/ml/models/m1/<version>/``.
* :mod:`~arc.ml.m1.predict`: pure features → quantiles, the published ``m1_*`` features,
  the Monte Carlo path distribution and the ``m1_direction`` stance proxy.

Never a gate input (D126): nothing under ``arc/gate`` may import this package.
"""
