"""XML reading for OOXML parts.

The standard library's Expat-backed ``ElementTree`` is used for actual
parsing, with two deliberate constraints layered on top:

* a document type declaration is refused before parsing, so no DTD, entity
  or external-subset machinery can be involved at all (OOXML never needs it);
* a parse failure, a non-XML part and a non-finite/undecodable document are
  reported as :class:`WorkbookPackageError` naming the part.

Namespaces are folded to local names so callers can look for ``sheet`` and
``r:id`` regardless of which (valid) OOXML namespace spelling a producer
used.  Element tree order is preserved.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from .errors import WorkbookPackageError


class Element:
    """Thin, iterable wrapper over an ElementTree element with local names."""

    __slots__ = ("tag", "attrs", "children", "text", "_raw")

    def __init__(self, raw: ET.Element):
        self._raw = raw
        self.tag = _local(raw.tag)
        self.attrs = {_attr_local(key): value for key, value in raw.attrib.items()}
        self.children = []
        self.text = raw.text or ""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Element {self.tag} {self.attrs!r}>"

    def child(self, tag: str):
        for item in self.children:
            if item.tag == tag:
                return item
        return None

    def children_named(self, tag: str):
        return [item for item in self.children if item.tag == tag]

    def iter(self):
        stack = [self]
        while stack:
            node = stack.pop()
            yield node
            stack.extend(reversed(node.children))


def _local(tag: str) -> str:
    if isinstance(tag, str) and tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag


def _attr_local(name: str) -> str:
    # Keep namespaced attributes distinguishable: "{uri}id" -> "r:id" is not
    # recoverable from the URI alone, so the caller matches on the local part
    # for "id" and on the literal "xml:space" for whitespace.
    if name.startswith("{"):
        return name.split("}", 1)[1]
    if name.startswith("xml:"):
        return name
    return name


def _has_doctype(data: bytes) -> bool:
    """True if a DTD/entity declaration appears before the root element.

    Standard-library ElementTree refuses *external* entities but happily
    expands internal ones; OOXML never needs any, so any document type
    declaration in the prolog is treated as an input fault.  The prolog ends
    at the first '<' that is not part of a declaration/comment/processing
    instruction.
    """
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    position = 0
    length = len(data)
    while position < length:
        start = data.find(b"<", position)
        if start < 0:
            return False
        head = data[start:start + 9]
        if head.startswith(b"<?") or head.startswith(b"<!--"):
            terminator = b"?>" if head.startswith(b"<?") else b"-->"
            end = data.find(terminator, start)
            if end < 0:
                return False
            position = end + len(terminator)
            continue
        if head.startswith(b"<!DOCTYPE") or head.startswith(b"<!ENTITY"):
            return True
        if head.startswith(b"<!"):
            # Other declarations are not part of OOXML either; skip to '>'.
            end = data.find(b">", start)
            if end < 0:
                return False
            position = end + 1
            continue
        return False
    return False


def parse_xml(data: bytes, *, part: str) -> Element:
    if not isinstance(data, (bytes, bytearray)):
        raise WorkbookPackageError("XML part is not bytes", part)
    if not data.strip():
        raise WorkbookPackageError("XML part is empty", part)
    if _has_doctype(data):
        raise WorkbookPackageError(
            "document type declarations are not accepted in OOXML parts", part
        )
    try:
        raw = ET.fromstring(bytes(data))
    except ET.ParseError as exc:
        raise WorkbookPackageError(f"malformed XML: {exc}", part) from exc
    except (ValueError, LookupError) as exc:
        raise WorkbookPackageError(
            f"XML cannot be decoded: {exc}", part
        ) from exc
    return _wrap(raw)


def _wrap(raw: ET.Element) -> Element:
    """Convert the ElementTree into wrapped Elements iteratively."""
    root = Element(raw)
    stack = [(raw, root)]
    while stack:
        source, target = stack.pop()
        for child_raw in list(source):
            child = Element(child_raw)
            target.children.append(child)
            stack.append((child_raw, child))
    return root
