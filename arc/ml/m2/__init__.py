"""Model M2 (E27.3, D126): calibrated candidate-outcome model on the E7.21 ledger.

Per candidate structure on a day: ``p_win``, ``p_stop``, ``e_r``, ``r_q10/q50/q90``,
``e_days``. One pooled LightGBM family per profile family (``credit`` = the margin
profile's ledger, every kind it lists; ``debit`` = the cash_debit profile's ledger).
Never a gate input (import-linter contract on ``arc.gate``).

Modules: :mod:`.config` (training config + ``--overlay``), :mod:`.dataset` (ledger ×
feature store), :mod:`.train` (walk-forward, grid, calibration, registry),
:mod:`.predict` (load + score), :mod:`.replay` (decision-value sim replays),
:mod:`.report` (model cards and ``docs/RESEARCH/ml-m2.md``).
"""
