"""Exception types used by distagg.

All failures raised by the transaction engine are subclasses of :class:`DistaggError`.
The ``code`` attribute is a stable machine-readable string that is also stored in
the ``error_code`` column of the receipt table.
"""


class DistaggError(Exception):
    code = "error"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message

    def to_dict(self) -> dict:
        return {"error": self.code, "message": self.message}


class InvalidTransaction(DistaggError):
    """The submitted transaction object is malformed (fields, types, seq...)."""

    code = "invalid_transaction"


class NegativeBaseWeight(DistaggError):
    """Applying the net edit would drive a base relation row below zero."""

    code = "negative_weight"

    def __init__(self, relation: str, key: dict, current: int, delta: int):
        self.relation = relation
        self.key = key
        self.current = current
        self.delta = delta
        super().__init__(
            f"{relation} base weight for {key!r} would become {current + delta} "
            f"(current {current}, net delta {delta})"
        )

    def to_dict(self) -> dict:
        d = super().to_dict()
        d.update(
            relation=self.relation,
            key=self.key,
            current_weight=self.current,
            net_delta=self.delta,
        )
        return d


class SequenceGap(DistaggError):
    """seq is neither the next expected value nor a re-submission of a known seq."""

    code = "sequence_gap"

    def __init__(self, seq: int, expected: int):
        self.seq = seq
        self.expected = expected
        super().__init__(f"seq {seq} is a gap; next expected seq is {expected}")


class SeqConflict(DistaggError):
    """A known seq was re-used with a different meaning."""

    code = "seq_conflict"

    def __init__(self, seq: int, stored_fingerprint: str, new_fingerprint: str):
        self.seq = seq
        self.stored_fingerprint = stored_fingerprint
        self.new_fingerprint = new_fingerprint
        super().__init__(
            f"seq {seq} was already committed with a different effective meaning "
            f"(stored {stored_fingerprint}, new {new_fingerprint})"
        )

    def to_dict(self) -> dict:
        d = super().to_dict()
        d.update(
            seq=self.seq,
            stored_fingerprint=self.stored_fingerprint,
            new_fingerprint=self.new_fingerprint,
        )
        return d


class DatabaseError(DistaggError):
    code = "database_error"
