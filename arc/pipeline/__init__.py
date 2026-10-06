"""E5.2 pipeline runner: candidate → structures → gate → proposal.

* :mod:`arc.pipeline.steps`: Research/Quant/Risk/propose routine handlers (D16).
* :mod:`arc.pipeline.runner`: ``arc propose`` (Scalp plus Research chain, run now).
* :mod:`arc.pipeline.market`: deterministic gate inputs (account, portfolio, quotes).
* :mod:`arc.pipeline.env`: injected market data, account and persona LLMs (live or fixtures).

Sizing is :mod:`arc.sizing` (D18). The gate is :mod:`arc.gate`. Nothing here
submits orders.
"""

from arc.pipeline.env import FIXTURE_NOW, PipelineEnv
from arc.pipeline.runner import ProposeReport, run_propose
from arc.pipeline.steps import pipeline_handlers

__all__ = ["FIXTURE_NOW", "PipelineEnv", "ProposeReport", "pipeline_handlers", "run_propose"]
