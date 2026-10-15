"""Pull the JSON object out of an LLM reply.

Models sometimes wrap the object in a code fence or chatter, or append text after it
(a note, a second object, a stray ``}``). Slicing from the first ``{`` to the last
``}`` breaks on the trailing case with ``Extra data``, so decode the object that starts
at the first ``{`` and ignore whatever follows it.

Models also occasionally emit a JavaScript-style trailing comma (``"quote": "...",}``),
which strict JSON rejects with ``Expecting property name enclosed in double quotes``.
When the strict decode fails, retry once with the commas that directly precede a
closing ``}``/``]`` removed (outside string literals only). Nothing else is repaired.
"""

from __future__ import annotations

import json
from typing import Any

_DECODER = json.JSONDecoder()
_WS = " \t\r\n"


def strip_trailing_commas(text: str) -> str:
    """Drop each ``,`` whose next non-whitespace char is ``}`` or ``]``, outside strings."""
    out: list[str] = []
    in_str = False
    escaped = False
    n = len(text)
    for i, ch in enumerate(text):
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == ",":
            j = i + 1
            while j < n and text[j] in _WS:
                j += 1
            if j < n and text[j] in "}]":
                continue
        out.append(ch)
    return "".join(out)


def first_json_object(text: str) -> Any:
    """Decode the JSON value starting at the first ``{`` in *text*; trailing text is ignored.

    Raises ``ValueError`` (``json.JSONDecodeError`` for a malformed object) if there is
    no ``{`` or the object doesn't decode, even with trailing commas removed. The error
    raised is the strict decoder's, so its position refers to the original reply.
    """
    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object in response")
    try:
        obj, _end = _DECODER.raw_decode(text, start)
    except json.JSONDecodeError as strict_exc:
        try:
            obj, _end = _DECODER.raw_decode(strip_trailing_commas(text[start:]))
        except json.JSONDecodeError:
            raise strict_exc from None
    return obj
