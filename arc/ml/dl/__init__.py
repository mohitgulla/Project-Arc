"""Deep-learning challengers to the LightGBM M2 champion (E27.6, D126).

Research only: every challenger trains and scores on the E27.3 harness
(:mod:`arc.ml.validate` folds, purge, embargo and weights; :mod:`arc.ml.calibrate`
isotonic maps; :func:`arc.ml.m2.train.eval_rows` metrics; the
:mod:`arc.ml.m2.replay` decision-value replays and promote legs). Nothing here is
imported by the live scorer, and nothing here is a gate input.

Challengers (:data:`arc.ml.dl.config.CHALLENGERS`):

* ``mlp``: ResNet-MLP on the M2 features (quantile-normalised), 5 seeds averaged;
* ``ft``: one FT-Transformer variant on the same inputs;
* ``temporal``: a 1-D CNN over the last 60 sessions of per-ticker daily vectors
  (:mod:`.sequence`), its embedding concatenated with the candidate features;
* ``surface``: a small CNN over the per (ticker, day) implied-vol surface
  (:mod:`.surface`), concatenated with the candidate features; also tested as an
  M1 feature (surface -> ``move_vs_implied``);
* ``stack``: M2 + the best DL challenger, blended by a logistic stacker fitted
  out of fold.

Modules: :mod:`.config` (pre-registered specs), :mod:`.prep` (quantile normaliser),
:mod:`.sequence` / :mod:`.surface` (raw inputs, pure numpy), :mod:`.nets` and
:mod:`.trainer` (torch), :mod:`.pipeline` (folds, replays, promote rule),
:mod:`.report` (``docs/RESEARCH/ml-dl-challengers.md``).

``torch`` is the optional ``ml-dl`` extra and is imported lazily, inside the
functions that need it (:func:`arc.ml.dl.nets.load_torch`); the pure-numpy modules
and their tests run without it.
"""
