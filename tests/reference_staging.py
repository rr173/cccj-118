"""Independent in-memory history model for the staging store.

Used by the random differential test as ground truth.  It deliberately
shares no code with the real store: every batch is appended to a per-key
history list, and reads at a commit number walk that list.  Snapshots are
modelled simply by querying at the snapshot's commit number.
"""


class HistoryModel:
    def __init__(self):
        self._history = {}  # key -> list of (commit, value|None), commit ascending
        self.commit = 0

    def apply_batch(self, ops):
        """ops: iterable of ("put", key, value) / ("delete", key); returns the commit."""
        collapsed = {}
        for op in ops:
            if op[0] == "put":
                collapsed[op[1]] = op[2]
            elif op[0] == "delete":
                collapsed[op[1]] = None
            else:
                raise ValueError(f"unknown op {op!r}")
        self.commit += 1
        for key, value in collapsed.items():
            self._history.setdefault(key, []).append((self.commit, value))
        return self.commit

    def get(self, key, commit):
        """Value visible at ``commit``: bytes, or None (missing / deleted)."""
        for version_commit, value in reversed(self._history.get(key, [])):
            if version_commit <= commit:
                return value
        return None

    def scan(self, lower, upper, commit):
        """All ``(key, value)`` pairs in ``[lower, upper)`` visible at ``commit``."""
        out = []
        for key in sorted(self._history):
            if lower is not None and key < lower:
                continue
            if upper is not None and key >= upper:
                continue
            value = self.get(key, commit)
            if value is not None:
                out.append((key, value))
        return out
