"""The manifest: which files currently form the committed store.

The store directory holds:

* ``CURRENT`` — one line naming the live manifest, e.g. ``MANIFEST-000007``.
* ``MANIFEST-<id>`` — a JSON document ``{"payload": ..., "crc32": ...}``.
  The payload records the store format, the highest commit number whose
  batches are included in the listed segments, the next free file id, the
  live WAL file, and the ordered (newest first) list of live segments with
  their key ranges, commit ranges, record counts and byte sizes.

A manifest switch is atomic: the new manifest is written to
``MANIFEST-<id+1>`` (via a ``.tmp`` file, fsync, rename, directory fsync)
and only then is ``CURRENT`` repointed (again via ``.tmp`` + rename +
directory fsync).  A crash anywhere in this window leaves either the old
or the new pair consistent; files left over from an abandoned switch are
recognised as unreferenced on open and removed — never mistaken for
committed state.  A damaged CURRENT or manifest (bad JSON, bad CRC, unknown
or missing fields, references to missing files) refuses the open instead of
rebuilding anything.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import List

from .errors import CorruptionError
from .format import crc32, manifest_name
from .segment import SegmentMeta, _fsync_dir

FORMAT_VERSION = 1
PAYLOAD_KIND = "distagg-staging-manifest"


@dataclass
class Manifest:
    commit: int
    next_file_id: int
    wal: str
    segments: List[SegmentMeta] = field(default_factory=list)  # newest first

    def payload(self) -> dict:
        return {
            "kind": PAYLOAD_KIND,
            "format": FORMAT_VERSION,
            "commit": self.commit,
            "next_file_id": self.next_file_id,
            "wal": self.wal,
            "segments": [meta.to_json() for meta in self.segments],
        }

    @classmethod
    def from_payload(cls, payload: dict, source: str) -> "Manifest":
        if not isinstance(payload, dict):
            raise CorruptionError(f"{source}: manifest payload is not an object")
        expected = {"kind", "format", "commit", "next_file_id", "wal", "segments"}
        unknown = set(payload) - expected
        missing = expected - set(payload)
        if unknown or missing:
            raise CorruptionError(
                f"{source}: manifest fields mismatch (missing {sorted(missing)}, "
                f"unknown {sorted(unknown)})"
            )
        if payload["kind"] != PAYLOAD_KIND:
            raise CorruptionError(f"{source}: not a staging manifest")
        if payload["format"] != FORMAT_VERSION:
            raise CorruptionError(
                f"{source}: unsupported manifest format {payload['format']!r}"
            )
        for field_name in ("commit", "next_file_id"):
            value = payload[field_name]
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise CorruptionError(f"{source}: bad {field_name} {value!r}")
        wal = payload["wal"]
        if not isinstance(wal, str) or "/" in wal or not wal.endswith(".log"):
            raise CorruptionError(f"{source}: bad wal file name {wal!r}")
        segments = payload["segments"]
        if not isinstance(segments, list):
            raise CorruptionError(f"{source}: segments is not a list")
        return cls(
            commit=payload["commit"],
            next_file_id=payload["next_file_id"],
            wal=wal,
            segments=[SegmentMeta.from_json(s, source) for s in segments],
        )


def _canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def encode_manifest(manifest: Manifest) -> bytes:
    payload = manifest.payload()
    document = {"payload": payload, "crc32": crc32(_canonical(payload))}
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")


def decode_manifest(data: bytes, source: str) -> Manifest:
    try:
        document = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise CorruptionError(f"{source}: manifest is not valid JSON: {exc}") from exc
    if not isinstance(document, dict) or set(document) != {"payload", "crc32"}:
        raise CorruptionError(f"{source}: manifest must have exactly 'payload' and 'crc32'")
    checksum = document["crc32"]
    if not isinstance(checksum, int) or isinstance(checksum, bool):
        raise CorruptionError(f"{source}: bad manifest checksum")
    if crc32(_canonical(document["payload"])) != checksum:
        raise CorruptionError(f"{source}: manifest checksum mismatch")
    return Manifest.from_payload(document["payload"], source)


def _write_file_atomic(dir_path: str, tmp_name: str, final_name: str, data: bytes) -> None:
    tmp_path = os.path.join(dir_path, tmp_name)
    with open(tmp_path, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, os.path.join(dir_path, final_name))
    _fsync_dir(dir_path)


def store_manifest_atomic(dir_path: str, manifest: Manifest, manifest_id: int) -> None:
    """Persist ``manifest`` as MANIFEST-<id> and repoint CURRENT atomically."""
    name = manifest_name(manifest_id)
    _write_file_atomic(dir_path, name + ".tmp", name, encode_manifest(manifest))
    _write_file_atomic(dir_path, "CURRENT.tmp", "CURRENT", (name + "\n").encode("utf-8"))
    # The old manifest is now unreferenced; remove it (a crash before this
    # leaves an orphan that the next open cleans up instead).
    for entry in os.listdir(dir_path):
        if entry.startswith("MANIFEST-") and entry != name and not entry.endswith(".tmp"):
            try:
                os.remove(os.path.join(dir_path, entry))
            except OSError:
                pass


def load_manifest(dir_path: str):
    """Read CURRENT and the manifest it names; return ``(Manifest, manifest_id)``."""
    current_path = os.path.join(dir_path, "CURRENT")
    try:
        with open(current_path, "r", encoding="utf-8") as handle:
            content = handle.read()
    except FileNotFoundError:
        raise CorruptionError(
            f"{dir_path}: CURRENT is missing but the directory holds store files; "
            "refusing to guess the committed state"
        )
    name = content.strip()
    if name != manifest_name(_manifest_id_from_name(name)):
        raise CorruptionError(f"{current_path}: does not name a manifest ({name!r})")
    manifest_path = os.path.join(dir_path, name)
    try:
        with open(manifest_path, "rb") as handle:
            data = handle.read()
    except FileNotFoundError:
        raise CorruptionError(
            f"{current_path}: points at {name}, which is missing"
        )
    return decode_manifest(data, manifest_path), _manifest_id_from_name(name)


def _manifest_id_from_name(name: str):
    if name.startswith("MANIFEST-") and name[len("MANIFEST-"):].isdigit():
        return int(name[len("MANIFEST-"):])
    return -1
