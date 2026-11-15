"""Execution configuration and the report structures of the flow executor."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

DEFAULT_CAPACITY = 8
DEFAULT_MAX_INFLIGHT = 8
DEFAULT_RESULT_CAPACITY = 64
DEFAULT_MAX_LINE_BYTES = 1024 * 1024
DEFAULT_MAX_RECORD_BYTES = 1024 * 1024
DEFAULT_MAX_NODES = 256
DEFAULT_MAX_EDGES = 1024


class FlowConfig:
    """Knobs for one run.

    ``results_dir``
        Where sinks are written.  It must not exist yet; every execution gets a
        fresh directory and an existing directory is never overwritten.
    ``default_capacity``
        Record capacity of an edge that does not declare one.
    ``max_inflight_per_node``
        Upper bound on records assigned to one node but not yet fully
        processed/delivered.  Producers stop pushing to a node at this bound.
    ``max_line_bytes`` / ``max_record_bytes``
        Hard byte limits.  ``None`` means "use the value declared in the graph
        file, or the documented default".  Both limits apply to the encoded
        payload; nothing is truncated to satisfy them.
    ``max_nodes`` / ``max_edges``
        A finite graph is required; oversized declarations are rejected before
        any file is opened.
    ``result_stream``
        When true, a bounded in-memory channel is created between the sinks and
        :meth:`FlowRun.iter_results` (capacity ``result_capacity``).  When
        false (default) the sinks write only files and never buffer rows for a
        caller, which keeps the run's memory bound as small as possible.
    """

    __slots__ = (
        "results_dir", "default_capacity", "max_inflight_per_node",
        "max_line_bytes", "max_record_bytes", "max_nodes", "max_edges",
        "result_capacity", "result_stream", "source_read_chunk",
    )

    def __init__(
        self,
        results_dir: Optional[str] = None,
        *,
        default_capacity: int = DEFAULT_CAPACITY,
        max_inflight_per_node: int = DEFAULT_MAX_INFLIGHT,
        max_line_bytes: Optional[int] = None,
        max_record_bytes: Optional[int] = None,
        max_nodes: int = DEFAULT_MAX_NODES,
        max_edges: int = DEFAULT_MAX_EDGES,
        result_capacity: int = DEFAULT_RESULT_CAPACITY,
        result_stream: bool = False,
        source_read_chunk: int = 65536,
    ):
        self.results_dir = results_dir
        self.default_capacity = _positive("default_capacity", default_capacity)
        self.max_inflight_per_node = _positive(
            "max_inflight_per_node", max_inflight_per_node
        )
        self.max_line_bytes = _optional_positive("max_line_bytes", max_line_bytes)
        self.max_record_bytes = _optional_positive(
            "max_record_bytes", max_record_bytes
        )
        self.max_nodes = _positive("max_nodes", max_nodes)
        self.max_edges = _positive("max_edges", max_edges)
        self.result_capacity = _positive("result_capacity", result_capacity)
        if not isinstance(result_stream, bool):
            raise ValueError("result_stream must be a boolean")
        self.result_stream = result_stream
        self.source_read_chunk = _positive("source_read_chunk", source_read_chunk)
        if results_dir is not None:
            if not isinstance(results_dir, str) or not results_dir:
                raise ValueError("results_dir must be a non-empty path string")


def _positive(label: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer, got {value!r}")
    return value


def _optional_positive(label: str, value: Any) -> Optional[int]:
    if value is None:
        return None
    return _positive(label, value)


class RunSummary:
    """The outcome of one run, as plain JSON-able data.

    ``status`` is ``"success"``, ``"failed"`` or ``"cancelled"``.  Only a
    success means every sink received every record that should have arrived
    and closed cleanly; ``complete`` per sink is never true otherwise.
    """

    def __init__(self, data: Dict[str, Any]):
        self._data = data

    # Mapping-friendly access for tests and tools ------------------------ #
    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def keys(self):
        return self._data.keys()

    def to_dict(self) -> Dict[str, Any]:
        return self._data

    # Attribute access --------------------------------------------------- #
    @property
    def status(self) -> str:
        return self._data["status"]

    @property
    def ok(self) -> bool:
        return self._data["status"] == "success"

    @property
    def error(self) -> Optional[Dict[str, Any]]:
        return self._data.get("error")

    @property
    def sinks(self) -> Dict[str, Dict[str, Any]]:
        return self._data["sinks"]

    @property
    def sources(self) -> Dict[str, Dict[str, Any]]:
        return self._data["sources"]

    @property
    def edges(self) -> List[Dict[str, Any]]:
        return self._data["edges"]

    @property
    def results_dir(self) -> str:
        return self._data["results_dir"]

    @property
    def success(self) -> bool:
        """Alias of :attr:`ok` used by some callers; never true for failure."""
        return self.ok

    def __repr__(self) -> str:  # pragma: no cover
        return f"RunSummary(status={self.status!r}, {len(self.edges)} edges)"
