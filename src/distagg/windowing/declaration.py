"""Parsing and validation of window declaration documents.

Declaration shape (UTF-8 JSON)::

    {
      "partition_by": ["region", "segment"],
      "order_by": "ts",
      "value": "amount",
      "lag_lead_k": 2,
      "windows": {
        "w1": {"mode": "ROWS", "start": -1, "end": 1, "exclude": "none"},
        "w2": {"mode": "RANGE", "start": 0, "end": 0, "exclude": "group"}
      }
    }

The document is validated strictly: unknown members are rejected so a typo
cannot silently disable part of a job.  Window offsets are mathematical
integers and may exceed the 64-bit range; only the order column itself is
constrained to signed 64-bit.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .errors import WindowDeclarationError

_VALID_MODES = ("ROWS", "RANGE")
_VALID_EXCLUDES = ("none", "current", "group")
_TOP_LEVEL_KEYS = ("partition_by", "order_by", "value", "lag_lead_k", "windows")
_WINDOW_KEYS = ("mode", "start", "end", "exclude")


@dataclass(frozen=True)
class WindowSpec:
    """One named window frame."""

    name: str
    mode: str            # "ROWS" or "RANGE"
    start: int           # inclusive frame offset from the current row
    end: int
    exclude: str         # "none", "current" or "group"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "mode": self.mode,
            "start": self.start,
            "end": self.end,
            "exclude": self.exclude,
        }


@dataclass(frozen=True)
class WindowDeclaration:
    """A fully validated window declaration."""

    partition_by: List[str]
    order_by: str
    value_field: str
    lag_lead_k: int
    windows: Dict[str, WindowSpec] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "partition_by": list(self.partition_by),
            "order_by": self.order_by,
            "value": self.value_field,
            "lag_lead_k": self.lag_lead_k,
            "windows": {name: spec.to_dict() for name, spec in self.windows.items()},
        }


def _reject_unknown(mapping, allowed, where, path):
    extra = sorted(set(mapping) - set(allowed))
    if extra:
        raise WindowDeclarationError(
            f"{where}: unknown member(s) {', '.join(extra)}; allowed: "
            f"{', '.join(allowed)}",
            path,
        )


def _as_plain_int(value, what, path):
    # Reject bools and floats even though bool is an int subclass.
    if isinstance(value, bool) or not isinstance(value, int):
        raise WindowDeclarationError(
            f"{what} must be an integer, got {type(value).__name__}", path
        )
    return value


def _as_name(value, what, path):
    if not isinstance(value, str) or not value:
        raise WindowDeclarationError(f"{what} must be a non-empty string", path)
    return value


def parse_declaration(data, path: Optional[str] = None) -> WindowDeclaration:
    """Validate an already-decoded declaration object."""
    if not isinstance(data, dict):
        raise WindowDeclarationError(
            f"declaration root must be a JSON object, got {type(data).__name__}", path
        )
    _reject_unknown(data, _TOP_LEVEL_KEYS, "declaration root", path)

    for required in ("partition_by", "order_by", "value", "lag_lead_k", "windows"):
        if required not in data:
            raise WindowDeclarationError(f"missing required member {required!r}", path)

    partition_by = data["partition_by"]
    if not isinstance(partition_by, list) or not partition_by:
        raise WindowDeclarationError(
            "partition_by must be a non-empty JSON array of field names", path
        )
    partition_fields = [
        _as_name(name, f"partition_by[{index}]", path)
        for index, name in enumerate(partition_by)
    ]
    if len(set(partition_fields)) != len(partition_fields):
        raise WindowDeclarationError("partition_by contains duplicate field names", path)

    order_by = _as_name(data["order_by"], "order_by", path)
    value_field = _as_name(data["value"], "value", path)

    lag_lead_k = _as_plain_int(data["lag_lead_k"], "lag_lead_k", path)
    if lag_lead_k < 1:
        raise WindowDeclarationError(
            f"lag_lead_k must be a positive integer, got {lag_lead_k}", path
        )

    windows_data = data["windows"]
    if not isinstance(windows_data, dict):
        raise WindowDeclarationError("windows must be a JSON object of named frames", path)
    if not windows_data:
        raise WindowDeclarationError("windows must name at least one frame", path)

    windows: Dict[str, WindowSpec] = {}
    for name, spec_data in windows_data.items():
        _as_name(name, "window name", path)
        if name in windows:  # pragma: no cover - dict keys already unique
            raise WindowDeclarationError(f"duplicate window name {name!r}", path)
        if not isinstance(spec_data, dict):
            raise WindowDeclarationError(
                f"window {name!r} must be a JSON object", path
            )
        _reject_unknown(spec_data, _WINDOW_KEYS, f"window {name!r}", path)
        for required in ("mode", "start", "end", "exclude"):
            if required not in spec_data:
                raise WindowDeclarationError(
                    f"window {name!r}: missing required member {required!r}", path
                )
        mode = spec_data["mode"]
        if mode not in _VALID_MODES:
            raise WindowDeclarationError(
                f"window {name!r}: mode must be one of {', '.join(_VALID_MODES)}, "
                f"got {mode!r}",
                path,
            )
        exclude = spec_data["exclude"]
        if exclude not in _VALID_EXCLUDES:
            raise WindowDeclarationError(
                f"window {name!r}: exclude must be one of "
                f"{', '.join(_VALID_EXCLUDES)}, got {exclude!r}",
                path,
            )
        start = _as_plain_int(spec_data["start"], f"window {name!r} start", path)
        end = _as_plain_int(spec_data["end"], f"window {name!r} end", path)
        if start > end:
            raise WindowDeclarationError(
                f"window {name!r}: start ({start}) must be <= end ({end})", path
            )
        windows[name] = WindowSpec(
            name=name, mode=mode, start=start, end=end, exclude=exclude
        )

    return WindowDeclaration(
        partition_by=partition_fields,
        order_by=order_by,
        value_field=value_field,
        lag_lead_k=lag_lead_k,
        windows=windows,
    )


def load_declaration(path: str) -> WindowDeclaration:
    """Load and validate a declaration file from disk."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        raise WindowDeclarationError(f"declaration file not found: {path}", path)
    except (OSError, json.JSONDecodeError) as exc:
        raise WindowDeclarationError(f"cannot read declaration: {exc}", path)
    return parse_declaration(data, path=path)
