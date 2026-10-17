"""Point-in-time feature store (E27.1): one formula per feature, two adapters.

* :mod:`.registry`: the :class:`~.registry.FeatureSpec` per feature and the schema hash.
* :mod:`.compute`: pure formulas (no I/O), :func:`~.compute.compute_panel`.
* :mod:`.adapters.history` / :mod:`.adapters.live`: build the same inputs from the
  backtest store and the live store.
"""
