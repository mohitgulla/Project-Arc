"""Daily beta vs SPY (E3.6, D62).

- :mod:`arc.features.beta`  pure math (``beta_vs``, ``floored_beta``)
- :mod:`arc.betas.store`    the ``betas`` table and **the one lookup** every consumer
  uses (gate inputs, portfolio_context, monitor, Tower): :func:`arc.betas.store.betas_used`
- :mod:`arc.betas.refresh`  the ``betas`` routine / ``arc betas refresh`` (network)
- :mod:`arc.betas.cli`      ``arc betas refresh|show``

The gate never imports this package: callers hand it the floored betas on
``MarketSnapshot.underlying_beta`` / ``Portfolio.beta_dollar_delta``.
"""
