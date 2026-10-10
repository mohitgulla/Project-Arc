"""Compare JSON goldens without flaking on platform float noise.

Goldens are captured on macOS; the Linux CI runner's numpy/BLAS can sum in a
different order and change the last ULP of a derived float (e.g. a stationary
probability ``...017`` vs ``...014``). That is not a behaviour change, so a raw
text compare of a float-bearing golden fails at random. ``assert_json_close``
keeps everything else exact: keys and their order, value types, strings, ints,
bools, nulls and list lengths. Only floats get a 1e-12 relative tolerance.
"""

from __future__ import annotations

import math

REL_TOL = 1e-12


def assert_json_close(got: object, want: object, path: str = "$", rel_tol: float = REL_TOL) -> None:
    """Recursive equality; floats to ``rel_tol`` relative, everything else exact."""
    if isinstance(want, float) or isinstance(got, float):
        # Same type on both sides: an int turning into a float (279 -> 279.0) is a change.
        assert type(got) is float and type(want) is float, (path, got, want)
        assert math.isclose(got, want, rel_tol=rel_tol, abs_tol=1e-300), (path, got, want)
    elif isinstance(want, dict):
        assert isinstance(got, dict) and list(got) == list(want), (path, got, want)
        for k in want:
            assert_json_close(got[k], want[k], f"{path}.{k}", rel_tol)
    elif isinstance(want, list):
        assert isinstance(got, list) and len(got) == len(want), (path, got, want)
        for i, (g, w) in enumerate(zip(got, want, strict=True)):
            assert_json_close(g, w, f"{path}[{i}]", rel_tol)
    else:
        assert type(got) is type(want) and got == want, (path, got, want)
