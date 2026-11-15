"""Exception types for the controlled-vocabulary annotation entry point.

All failures raised by :mod:`distagg.lexicon` are subclasses of
:class:`LexiconError`.  Dictionary errors always carry the physical 1-based
dictionary line and, when the problem belongs to one alias, the 0-based
alias index inside that line's ``aliases`` array.  Remark-input errors carry
the physical 1-based input line.  Library errors describe a matching library
that is missing, corrupted, incomplete or already present.
"""

from __future__ import annotations

from typing import Optional


class LexiconError(Exception):
    """Base class for lexicon compile/scan failures."""

    code = "lexicon_error"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message

    def to_dict(self) -> dict:
        return {"error": self.code, "message": self.message}


class LexiconDictionaryError(LexiconError):
    """One physical dictionary line is malformed or semantically invalid."""

    code = "lexicon_dictionary_error"

    def __init__(self, line_number: Optional[int], message: str,
                 alias_index: Optional[int] = None, path: Optional[str] = None):
        location = f"line {line_number}" if line_number is not None else "file"
        if path is not None:
            location = f"{path}({location})"
        if alias_index is not None:
            location = f"{location} alias[{alias_index}]"
        super().__init__(f"{location}: {message}")
        self.line_number = line_number
        self.alias_index = alias_index
        self.path = path

    def to_dict(self) -> dict:
        data = super().to_dict()
        if self.line_number is not None:
            data["line_number"] = self.line_number
        if self.alias_index is not None:
            data["alias_index"] = self.alias_index
        if self.path is not None:
            data["path"] = self.path
        return data


class LexiconLibraryError(LexiconError):
    """The matching library cannot be opened, created or trusted."""

    code = "lexicon_library_error"

    def __init__(self, message: str, path: Optional[str] = None):
        if path is not None:
            message = f"{path}: {message}"
        super().__init__(message)
        self.path = path

    def to_dict(self) -> dict:
        data = super().to_dict()
        if self.path is not None:
            data["path"] = self.path
        return data


class LexiconInputError(LexiconError):
    """One physical remark line cannot be parsed or fails the contract."""

    code = "lexicon_input_error"

    def __init__(self, line_number: int, message: str,
                 path: Optional[str] = None):
        location = f"line {line_number}"
        if path is not None:
            location = f"{path}({location})"
        super().__init__(f"{location}: {message}")
        self.line_number = line_number
        self.path = path

    def to_dict(self) -> dict:
        data = super().to_dict()
        data["line_number"] = self.line_number
        if self.path is not None:
            data["path"] = self.path
        return data
