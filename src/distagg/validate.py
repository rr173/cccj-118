"""Strict validation and canonicalisation of a submitted transaction.

The submitted JSON object is *never* trusted:

* ``seq`` must be a JSON integer (not bool, not 1.0-from-string), >= 1.
* ``edits`` must be a list (may be empty).
* every edit must be an object with exactly the keys ``relation``, ``row`` and
  ``weight`` (extra keys are invalid).
* relation is ``"E"`` or ``"T"``, case sensitive.
* weight is a *non-zero* JSON integer (bool is rejected explicitly).
* E row: exactly ``publisher``, ``creative``, ``cost``; publisher/creative
  non-empty strings, cost int or null (bool rejected).
* T row: exactly ``creative``, ``tag``; both non-empty strings.

Strings are compared verbatim: no strip, no case folding, no unicode
normalisation.  Therefore a string with a leading space is a *different*
string, but it is still a valid non-empty string.

After validation the edits are folded by identical key into a net delta.  This
is the "meaning" of a transaction.  The fingerprint used for idempotent
re-submission is computed from this *net* form, so edit order, splitting a row
across several edits, and JSON object field order do not change it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .errors import InvalidTransaction

REL_E = "E"
REL_T = "T"

E_KEYS = frozenset(("publisher", "creative", "cost"))
T_KEYS = frozenset(("creative", "tag"))
EDIT_KEYS = frozenset(("relation", "row", "weight"))

# Key tuple layout for E: (publisher, creative, cost-or-None)
EKey = Tuple[str, str, Optional[int]]
# Key tuple layout for T: (creative, tag)
TKey = Tuple[str, str]

# E order in the canonical fingerprint: publisher, creative, cost.
#   cost None sorts before any integer; integers sort numerically.
# T order: creative, tag.
E_SORT_FIELDS = ("publisher", "creative", "cost")
T_SORT_FIELDS = ("creative", "tag")


@dataclass(frozen=True)
class NetEdit:
    relation: str
    key: tuple
    delta: int

    def sort_key(self):
        # relation first (E before T), then the defining fields, with None
        # (null cost) first among costs.  Both types are consistently ordered,
        # which is all the fingerprint needs.
        if self.relation == REL_E:
            publisher, creative, cost = self.key
            return (0, publisher, creative, 0 if cost is None else 1, 0 if cost is None else cost)
        creative, tag = self.key
        return (1, creative, tag)


def _is_int(value) -> bool:
    # bool is a subclass of int in Python; JSON true/false must not pass.
    return isinstance(value, int) and not isinstance(value, bool)


def _require_object(value, what: str) -> dict:
    if not isinstance(value, dict):
        raise InvalidTransaction(f"{what} must be a JSON object")
    return value


def _require_exact_keys(obj: dict, required: frozenset, what: str) -> None:
    keys = set(obj.keys())
    if keys != set(required):
        missing = sorted(set(required) - keys)
        extra = sorted(keys - set(required))
        parts = []
        if missing:
            parts.append(f"missing {missing}")
        if extra:
            parts.append(f"unexpected {extra}")
        raise InvalidTransaction(f"{what} has wrong fields ({', '.join(parts)})")


def _require_nonempty_string(value, what: str) -> str:
    if not isinstance(value, str):
        raise InvalidTransaction(f"{what} must be a string")
    if value == "":
        raise InvalidTransaction(f"{what} must not be the empty string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise InvalidTransaction(f"{what} is not valid Unicode text: {exc}") from exc
    return value


def _require_cost(value):
    if value is None:
        return None
    if not _is_int(value):
        raise InvalidTransaction("row.cost must be an integer or null")
    return value


def validate_transaction(obj) -> Tuple[int, List[NetEdit]]:
    """Validate a decoded JSON object and return ``(seq, net_edits)``.

    ``net_edits`` contains one entry per distinct key with non-zero net delta,
    deterministically sorted.  Zero-net keys are dropped (they produce no
    change, and are not part of the transaction's effective meaning).
    """
    obj = _require_object(obj, "transaction")
    _require_exact_keys(obj, frozenset(("seq", "edits")), "transaction")
    seq = obj["seq"]
    if not _is_int(seq):
        raise InvalidTransaction("seq must be an integer")
    if seq < 1:
        raise InvalidTransaction("seq must be >= 1")
    edits = obj["edits"]
    if not isinstance(edits, list):
        raise InvalidTransaction("edits must be a list")

    e_delta: Dict[EKey, int] = {}
    t_delta: Dict[TKey, int] = {}

    for index, edit in enumerate(edits):
        what = f"edits[{index}]"
        edit = _require_object(edit, what)
        _require_exact_keys(edit, EDIT_KEYS, what)
        relation = edit["relation"]
        if relation not in (REL_E, REL_T):
            raise InvalidTransaction(f"{what}.relation must be 'E' or 'T'")
        weight = edit["weight"]
        if not _is_int(weight):
            raise InvalidTransaction(f"{what}.weight must be an integer")
        if weight == 0:
            raise InvalidTransaction(f"{what}.weight must be non-zero")
        row = _require_object(edit["row"], f"{what}.row")
        if relation == REL_E:
            _require_exact_keys(row, E_KEYS, f"{what}.row")
            publisher = _require_nonempty_string(row["publisher"], f"{what}.row.publisher")
            creative = _require_nonempty_string(row["creative"], f"{what}.row.creative")
            cost = _require_cost(row["cost"])
            key: tuple = (publisher, creative, cost)
            e_delta[key] = e_delta.get(key, 0) + weight
        else:
            _require_exact_keys(row, T_KEYS, f"{what}.row")
            creative = _require_nonempty_string(row["creative"], f"{what}.row.creative")
            tag = _require_nonempty_string(row["tag"], f"{what}.row.tag")
            key = (creative, tag)
            t_delta[key] = t_delta.get(key, 0) + weight

    net: List[NetEdit] = []
    for key, delta in e_delta.items():
        if delta != 0:
            net.append(NetEdit(REL_E, key, delta))
    for key, delta in t_delta.items():
        if delta != 0:
            net.append(NetEdit(REL_T, key, delta))
    net.sort(key=NetEdit.sort_key)
    return seq, net


def fingerprint(net_edits: List[NetEdit]) -> str:
    """Stable hash of the *effective* meaning of a transaction.

    Two transactions have the same fingerprint iff they produce the same
    multiset of net (relation, full row) -> signed weight changes.  Order of
    edits, order of JSON object fields, and splitting one row over several
    edits do not affect it.  Zero-net rows have already been dropped.
    """
    payload = [
        [ne.relation, list(ne.key), ne.delta] for ne in net_edits
    ]
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def net_edits_json(net_edits: List[NetEdit]) -> str:
    payload = [[ne.relation, list(ne.key), ne.delta] for ne in net_edits]
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
