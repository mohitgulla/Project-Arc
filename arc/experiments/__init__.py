"""Forward A/B experiments (PLAN D44, cards E10.1–E10.7).

E10.1 is the data backbone: the spec contract (:mod:`.models`), the defaults
(:mod:`.config`), overlay loading (:mod:`.overlay`), the append-only registry
(:mod:`.store`) and the ``arc experiment`` CLI (:mod:`.cli`). No trading
behaviour lives here yet.
"""
