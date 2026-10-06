"""Decision journal (E7.4, D22/D23): every pipeline decision, why, and what came of it.

Append-only SQLite tables (migration 009): ``decisions`` (one row per
selected / rejected / no-trade / sized / gated / approved / expired choice, each
with a stable :class:`~arc.journal.reasons.ReasonCode`), ``market_context``
(quotes frozen at proposal time), ``outcomes`` (deterministic attribution) and
``decision_reviews`` (decision quality judged separately from outcome).

An LLM reviewer may only append reviews that cite existing decision ids.
"""

from arc.journal.reasons import Choice, JournalPersona, ReasonCode, Stage

__all__ = ["Choice", "JournalPersona", "ReasonCode", "Stage"]
