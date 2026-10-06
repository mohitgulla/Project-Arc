"""IV history done right (E4.12, D55).

- :mod:`arc.iv.store`  the ``iv_daily`` table: upsert, the per-ticker series the
  regime/scanner read (``alpaca_cm30`` then ``alpaca_backfill``; Option Strategist rows
  are never mixed in), and the fresh external percentile.
- :mod:`arc.iv.record`  the forward ``iv.record`` routine (15:50 ET): 30-DTE constant-
  maturity ATM IV from the Alpaca chain with a Cboe ``iv30`` cross-check.
- :mod:`arc.iv.backfill`  ``arc iv backfill``: the same 30-DTE IV rebuilt from Alpaca
  option daily bars (Black-Scholes inversion of the ATM call + put closes).
- :mod:`arc.iv.optionstrategist`  ``arc iv import-optionstrategist``: the McMillan weekly
  file, ad hoc, internal use only (owner decision, D55).
- :mod:`arc.iv.validate`  ``arc iv validate``: our series vs the Option Strategist file.

IV is context only: nothing here is a gate input.
"""
