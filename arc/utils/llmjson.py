"""Pull the JSON object out of an LLM reply.

Models sometimes wrap the object in a code fence or chatter, or append text after it
(a note, a second object, a stray ``}``). Slicing from the first ``{`` to the last
``}`` breaks on the trailing case with ``Extra data``, so decode the object that starts
at the first ``{`` and ignore whatever follows it.
"""

from __future__ import annotations

import json
from typing import Any

_DECODER = json.JSONDecoder()


def first_json_object(text: str) -> Any:
    """Decode the JSON value starting at the first ``{`` in *text*; trailing text is ignored.

    Raises ``ValueError`` (``json.JSONDecodeError`` for a malformed object) if there is
    no ``{`` or the object doesn't decode.
    """
    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object in response")
    obj, _end = _DECODER.raw_decode(text, start)
    return obj
