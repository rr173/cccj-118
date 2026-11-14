"""Exception types for the offline window processor.

All failures raised by :mod:`distagg.windowing` are subclasses of
:class:`WindowError`.  Input errors always carry the physical 1-based line
number where the problem was detected so a data-prep team can locate a bad
record without guessing.
"""

from __future__ import annotations


class WindowError(Exception):
    """Base class for window-processing failures."""

    code = "window_error"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message

    def to_dict(self) -> dict:
        return {"error": self.code, "message": self.message}


class WindowDeclarationError(WindowError):
    """The window declaration document is malformed or semantically invalid."""

    code = "window_declaration_error"

    def __init__(self, message: str, path: str = None):
        if path is not None:
            message = f"{path}: {message}"
        super().__init__(message)
        self.path = path

    def to_dict(self) -> dict:
        data = super().to_dict()
        if self.path is not None:
            data["path"] = self.path
        return data


class WindowInputError(WindowError):
    """A physical input line cannot be parsed or fails the type contract."""

    code = "window_input_error"

    def __init__(self, line_number: int, message: str, path: str = None):
        location = f"line {line_number}"
        if path is not None:
            location = f"{path}({location})"
        full = f"{location}: {message}"
        super().__init__(full)
        self.line_number = line_number
        self.path = path

    def to_dict(self) -> dict:
        data = super().to_dict()
        data["line_number"] = self.line_number
        if self.path is not None:
            data["path"] = self.path
        return data
