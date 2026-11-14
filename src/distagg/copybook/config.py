"""Loading of the REDEFINES branch-selection configuration.

The selection config is a JSON object mapping each redefines group (the full
path of the redefined item) to the field that chooses the branch and the
value-to-branch map::

    {
      "selectors": {
        "ORDER-RECORD.ORDER-BODY": {
          "using": "ORDER-RECORD.ORDER-KIND",
          "map": {
            "S": "ORDER-RECORD.ORDER-BODY",
            "E": "ORDER-RECORD.EXTENDED-BODY"
          }
        }
      }
    }

The top-level ``"selectors"`` wrapper may be omitted.  Map keys are strings:
text selector values compare byte-for-byte after cp037 decoding (spaces
count), integer selector values compare as their canonical decimal string.
"""

from __future__ import annotations

import json

from .errors import SelectorConfigError


def load_selector_config(path: str) -> dict:
    """Read a selector configuration JSON file into the plain mapping."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except OSError as exc:
        raise SelectorConfigError(f"cannot read selector config {path!r}: {exc}")
    except json.JSONDecodeError as exc:
        raise SelectorConfigError(
            f"selector config {path!r} is not valid JSON: {exc}")
    if isinstance(raw, dict) and set(raw.keys()) == {"selectors"}:
        raw = raw["selectors"]
    if not isinstance(raw, dict):
        raise SelectorConfigError(
            f"selector config {path!r} must be a JSON object (optionally "
            f"wrapped in a \"selectors\" member)")
    return raw
