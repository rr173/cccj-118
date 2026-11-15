"""Exception types for the local asynchronous flow executor.

Every failure is an :class:`FlowError` subclass with a stable ``code``.  The
two large families are:

* :class:`GraphError` — the graph definition or one of its operations is
  invalid.  These are raised by :func:`distagg.flow.graph.load_graph` /
  :func:`distagg.flow.graph.validate_graph` *before* any business input is
  opened or any output file is created.
* :class:`FlowFailure` — a runtime failure.  It keeps the node where the
  problem was first seen, the *source identity* of the record being processed
  (source name, physical 1-based line, expansion path) when one exists, the
  exception type and the tool's own human-readable reason.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


class FlowError(Exception):
    """Base class for every error raised by :mod:`distagg.flow`."""

    code = "flow_error"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message

    def to_dict(self) -> dict:
        return {"error": self.code, "message": self.message}


class GraphError(FlowError):
    """The graph definition is malformed or semantically invalid."""

    code = "graph_error"

    def __init__(self, problems: List[str], path: Optional[str] = None):
        if isinstance(problems, str):  # convenience for single-problem use
            problems = [problems]
        self.problems = list(problems)
        self.path = path
        prefix = f"{path}: " if path is not None else ""
        super().__init__(prefix + "; ".join(self.problems))

    def to_dict(self) -> dict:
        data = super().to_dict()
        data["problems"] = list(self.problems)
        if self.path is not None:
            data["path"] = self.path
        return data


class SourceRecordError(FlowError):
    """A physical source line cannot be read or decoded under the strict contract."""

    code = "source_record_error"

    def __init__(self, source: str, line_number: int, message: str,
                 path: Optional[str] = None):
        full = f"{source}:{line_number}: {message}"
        super().__init__(full)
        self.source = source
        self.line_number = line_number
        self.path = path

    def to_dict(self) -> dict:
        data = super().to_dict()
        data.update(source=self.source, line_number=self.line_number)
        if self.path is not None:
            data["path"] = self.path
        return data


class FlowFailure(FlowError):
    """A runtime failure attributed to one node and (usually) one record.

    ``identity`` is the source identity of the record that was being
    processed when the failure was detected, or ``None`` for failures that
    are not tied to a record (source decode errors already carry their own
    fields).  The first failure observed by the supervisor wins, so this
    object always describes the original error, never a follow-up broken
    pipe/cancellation.
    """

    code = "flow_failure"

    def __init__(
        self,
        node: str,
        reason: str,
        *,
        identity: Optional[Dict[str, Any]] = None,
        cause: Optional[BaseException] = None,
        kind: Optional[str] = None,
    ):
        self.node = node
        self.reason = reason
        self.identity = identity
        self.cause = cause
        self.kind = kind or (type(cause).__name__ if cause is not None else None)
        location = f"node {node!r}"
        if identity is not None:
            location += (
                f" source={identity.get('source')!r} line={identity.get('line')}"
                f" expansion_path={identity.get('expansion_path')}"
            )
        super().__init__(f"{location}: {reason}")

    def to_dict(self) -> dict:
        data = super().to_dict()
        data.update(node=self.node, reason=self.reason,
                    exception_kind=self.kind)
        if self.identity is not None:
            data["identity"] = dict(self.identity)
        return data


class FlowCancelled(FlowError):
    """The run was cancelled by the caller (``run.cancel()`` or iterator close)."""

    code = "flow_cancelled"

    def to_dict(self) -> dict:
        data = super().to_dict()
        data["status"] = "cancelled"
        return data
