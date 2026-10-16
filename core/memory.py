"""Persistent, user-approved memory for Jarvis.

This module holds the memory *domain*: validated records, the service that
performs explicit save / list / retrieve / delete / clear operations, and
the declarations that expose those operations as ordinary registered
tools. It performs no I/O itself — persistence lives behind the injected
:class:`MemoryStore` (see :mod:`core.memory_store` for the JSON file
implementation) — so the whole domain is deterministic and unit testable.

Design rules
------------
* Memory is **user-started**: nothing is ever saved automatically. A value
  is stored only when the user explicitly saves it, and only through the
  guarded executor (handlers here are never called directly by the router).
* Memory **never grants authority**. It cannot change a permission, satisfy
  a confirmation, or authorise an action; it only stores and returns text
  the user chose to remember.
* Stored content is **data, never instructions**. It is bounded in length,
  returned as plain text, and never interpreted as a command, path or
  option.
* Listing and retrieval are read-only and side-effect free. Deletion and
  clearing are destructive and are declared at ``CONFIRM`` so the executor
  requires a fresh, bound approval every time.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional, Protocol

from core.permissions import PermissionLevel
from core.tools import ToolInputError, ToolRegistry, ToolSpec, validate_mapping

TOOL_MEMORY_SAVE = "memory_save"
TOOL_MEMORY_LIST = "memory_list"
TOOL_MEMORY_GET = "memory_get"
TOOL_MEMORY_DELETE = "memory_delete"
TOOL_MEMORY_CLEAR = "memory_clear"

# Operation name -> registered tool name. The router uses this mapping so
# it never has to hard-code tool ids and stays in step with the registry.
MEMORY_OPERATIONS: Mapping[str, str] = {
    "save": TOOL_MEMORY_SAVE,
    "list": TOOL_MEMORY_LIST,
    "get": TOOL_MEMORY_GET,
    "delete": TOOL_MEMORY_DELETE,
    "clear": TOOL_MEMORY_CLEAR,
}

DEFAULT_CATEGORY = "general"

# Absolute upper bounds. Neither ``MemoryConfig`` nor ``MemoryService``
# can raise a per-store limit above these; they exist so a corrupt or
# hostile store cannot produce unbounded records.
MAX_VALUE_CHARS = 500
MAX_CATEGORY_CHARS = 32
MAX_RECORDS = 200

# A single summary line is trimmed to this length before it is returned, so
# a large memory collection can never produce an unbounded spoken reply.
MAX_LIST_CHARS = 600

_IDENTIFIER_RE = re.compile(r"[0-9a-f]{8}")
_CATEGORY_RE = re.compile(r"[a-z0-9 _-]+")
_WORD_EDGE_CHARS = "'\u2019\".,:;!?"


class MemoryValidationError(ValueError):
    """Raised when memory input is missing, malformed or out of bounds."""


class MemoryStoreError(RuntimeError):
    """Raised when stored memories cannot be read or written safely.

    The name deliberately avoids the builtin ``MemoryError``.
    """


class MemoryStore(Protocol):
    """Persistence seam for memories (a JSON file, or a test fake)."""

    def load(self) -> tuple["MemoryRecord", ...]:
        ...

    def save(self, records: Sequence["MemoryRecord"]) -> None:
        ...


def _clean(text: str) -> str:
    """Collapse whitespace and drop control characters."""
    return "".join(
        character
        for character in " ".join(str(text).split())
        if character.isprintable()
    )


def validate_identifier(identifier: Any) -> str:
    """Return a valid memory id or raise :class:`MemoryValidationError`."""
    if not isinstance(identifier, str) or not _IDENTIFIER_RE.fullmatch(
        identifier
    ):
        raise MemoryValidationError("memory identifier is invalid")
    return identifier


def validate_value(value: Any, *, max_chars: int = MAX_VALUE_CHARS) -> str:
    """Return a cleaned memory value within bounds, or raise."""
    if not isinstance(value, str):
        raise MemoryValidationError("memory value must be text")
    cleaned = _clean(value)
    if not cleaned:
        raise MemoryValidationError("memory value must not be empty")
    if len(cleaned) > max_chars:
        raise MemoryValidationError("memory value is too long")
    return cleaned


def validate_category(
    category: Any, *, max_chars: int = MAX_CATEGORY_CHARS
) -> str:
    """Return a normalised category, defaulting an empty one to 'general'."""
    if not isinstance(category, str):
        raise MemoryValidationError("memory category must be text")
    cleaned = _clean(category).casefold()
    if not cleaned:
        return DEFAULT_CATEGORY
    if len(cleaned) > max_chars or not _CATEGORY_RE.fullmatch(cleaned):
        raise MemoryValidationError("memory category is invalid")
    return cleaned


def normalize_value(value: str) -> str:
    """Comparison key for duplicate detection and value selectors."""
    return _clean(value).casefold()


@dataclass(frozen=True)
class MemoryRecord:
    """One explicitly saved memory.

    ``identifier`` is a short, random, lowercase hex string the user can
    read back to target a specific memory. ``created_at`` / ``updated_at``
    are ISO-8601 UTC strings so a stored file is independent of the local
    clock format.
    """

    identifier: str
    value: str
    category: str = DEFAULT_CATEGORY
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        validate_identifier(self.identifier)
        if not isinstance(self.value, str) or not self.value.strip():
            raise MemoryValidationError("memory value must not be empty")
        if len(self.value) > MAX_VALUE_CHARS:
            raise MemoryValidationError("memory value is too long")
        if not self.value.isprintable():
            # Values from the service are always cleaned (whitespace
            # collapsed, control characters dropped); a stored value
            # with a newline or other non-printable content can only
            # come from a hand-edited or damaged file, so it is refused
            # rather than accepted and later echoed.
            raise MemoryValidationError("memory value must be printable text")
        if not isinstance(self.category, str) or not self.category.strip():
            raise MemoryValidationError("memory category must not be empty")
        if len(self.category) > MAX_CATEGORY_CHARS:
            raise MemoryValidationError("memory category is too long")
        if not _CATEGORY_RE.fullmatch(self.category):
            # Categories are normalised to the same character set the
            # service accepts, so a stored value cannot smuggle in
            # punctuation or control characters.
            raise MemoryValidationError("memory category is invalid")
        if not isinstance(self.created_at, str) or not isinstance(
            self.updated_at, str
        ):
            raise MemoryValidationError("memory timestamps must be text")

    def to_dict(self) -> dict:
        return {
            "id": self.identifier,
            "value": self.value,
            "category": self.category,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "MemoryRecord":
        """Rebuild a record from stored data, raising ``MemoryStoreError``.

        Every failure (wrong shape, missing key, wrong type, out-of-bounds
        value) is reported as a store error so a damaged file is surfaced
        rather than silently repaired.
        """
        if not isinstance(data, Mapping):
            raise MemoryStoreError("a stored memory is not an object")
        try:
            identifier = data["id"]
            value = data["value"]
        except (KeyError, TypeError) as exc:
            raise MemoryStoreError("a stored memory is missing fields") from exc
        category = data.get("category", DEFAULT_CATEGORY)
        created_at = data.get("created_at", "")
        updated_at = data.get("updated_at", "")
        for field_value in (identifier, value, category, created_at, updated_at):
            if not isinstance(field_value, str):
                raise MemoryStoreError(
                    "a stored memory has a field of the wrong type"
                )
        try:
            return cls(
                identifier=identifier,
                value=value,
                category=category,
                created_at=created_at,
                updated_at=updated_at,
            )
        except MemoryValidationError as exc:
            raise MemoryStoreError("a stored memory is invalid") from exc


def _default_identifier() -> str:
    return secrets.token_hex(4)


def _bounded_limit(name: str, value: Any, ceiling: int) -> int:
    """Return a valid per-service limit, or raise ``ValueError``.

    A limit must be a real integer in ``1..ceiling``; a wrong type
    (including ``bool``) or an out-of-range value is refused rather than
    silently coerced or clamped, so a misconfiguration can never raise
    the store above the module's absolute caps. It fails before any data
    is read or written.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer between 1 and {ceiling}")
    if not 1 <= value <= ceiling:
        raise ValueError(f"{name} must be between 1 and {ceiling}")
    return value


def _iso(moment: datetime) -> str:
    return moment.replace(microsecond=0).isoformat()


class MemoryService:
    """Explicit save / list / retrieve / delete / clear over a store.

    Every operation loads the current records, works in memory, and saves
    only when something actually changes. Nothing is written on
    construction or on reads, so enabling memory never creates a file; the
    file appears only after the first successful save.
    """

    def __init__(
        self,
        store: MemoryStore,
        *,
        max_records: int = MAX_RECORDS,
        max_value_chars: int = MAX_VALUE_CHARS,
        max_category_chars: int = MAX_CATEGORY_CHARS,
        id_factory: Optional[Callable[[], str]] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._store = store
        self._max_records = _bounded_limit(
            "max_records", max_records, MAX_RECORDS
        )
        self._max_value_chars = _bounded_limit(
            "max_value_chars", max_value_chars, MAX_VALUE_CHARS
        )
        self._max_category_chars = _bounded_limit(
            "max_category_chars", max_category_chars, MAX_CATEGORY_CHARS
        )
        self._new_id = id_factory if id_factory is not None else (
            _default_identifier
        )
        self._clock = clock if clock is not None else (
            lambda: datetime.now(timezone.utc)
        )

    def list(self) -> tuple[MemoryRecord, ...]:
        """Return every saved memory, oldest first. Read-only."""
        records = tuple(self._store.load())
        return tuple(
            sorted(records, key=lambda record: (record.created_at, record.identifier))
        )

    def find(self, selector: str) -> tuple[MemoryRecord, ...]:
        """Return memories matching ``selector`` (an id or an exact value).

        A selector that is a valid id matches by id; anything else matches a
        record whose value equals it (case- and whitespace-insensitively).
        There is no fuzzy or partial matching.
        """
        if not isinstance(selector, str):
            return ()
        text = selector.strip()
        if not text:
            return ()
        records = self.list()
        if _IDENTIFIER_RE.fullmatch(text):
            return tuple(r for r in records if r.identifier == text)
        target = normalize_value(text)
        return tuple(r for r in records if normalize_value(r.value) == target)

    def save(
        self, value: str, category: str = DEFAULT_CATEGORY
    ) -> tuple[MemoryRecord, bool]:
        """Save a value; return ``(record, created)``.

        Saving an exact duplicate is a no-op that returns the existing
        record and ``False``. Reaching the record limit raises instead of
        silently dropping data.
        """
        cleaned = validate_value(value, max_chars=self._max_value_chars)
        category_name = validate_category(
            category, max_chars=self._max_category_chars
        )
        records = list(self.list())
        target = normalize_value(cleaned)
        for existing in records:
            if normalize_value(existing.value) == target:
                return existing, False
        if len(records) >= self._max_records:
            raise MemoryValidationError("the memory limit has been reached")
        taken = {record.identifier for record in records}
        identifier = self._allocate_identifier(taken)
        timestamp = _iso(self._clock())
        record = MemoryRecord(
            identifier=identifier,
            value=cleaned,
            category=category_name,
            created_at=timestamp,
            updated_at=timestamp,
        )
        records.append(record)
        self._store.save(records)
        return record, True

    def delete(self, selector: str) -> tuple[MemoryRecord, ...]:
        """Delete the single memory matching ``selector``.

        Returns the matches. Nothing is deleted unless exactly one record
        matches, so an ambiguous or empty selector is always a safe no-op.
        """
        matches = self.find(selector)
        if len(matches) != 1:
            return matches
        target = matches[0].identifier
        remaining = [
            record for record in self.list() if record.identifier != target
        ]
        self._store.save(remaining)
        return matches

    def clear(self) -> int:
        """Delete every saved memory; return how many were removed."""
        count = len(self.list())
        if count:
            self._store.save([])
        return count

    def _allocate_identifier(self, taken: set) -> str:
        for _ in range(50):
            candidate = self._new_id()
            if (
                isinstance(candidate, str)
                and _IDENTIFIER_RE.fullmatch(candidate)
                and candidate not in taken
            ):
                return candidate
        raise MemoryStoreError("could not allocate a memory identifier")


# ---------------------------------------------------------------------------
# Tool integration
# ---------------------------------------------------------------------------
_STORE_ERROR_TEXT = (
    "I couldn't access the saved memories; the memory file may be damaged."
)


def _reject_arguments(raw: Mapping[str, Any]) -> dict:
    return validate_mapping(raw, required={}, optional={})


def _validate_selector(raw: Mapping[str, Any]) -> dict:
    values = validate_mapping(raw, required={"selector": str}, optional={})
    selector = _clean(values["selector"])
    if not selector:
        raise ToolInputError("memory selector must not be empty")
    if len(selector) > MAX_VALUE_CHARS:
        raise ToolInputError("memory selector is too long")
    return {"selector": selector}


def _make_save_validator(
    max_value_chars: int, max_category_chars: int
) -> Callable[[Mapping[str, Any]], dict]:
    def validate(raw: Mapping[str, Any]) -> dict:
        values = validate_mapping(
            raw, required={"value": str}, optional={"category": str}
        )
        try:
            value = validate_value(
                values["value"], max_chars=max_value_chars
            )
            category = validate_category(
                values.get("category", DEFAULT_CATEGORY),
                max_chars=max_category_chars,
            )
        except MemoryValidationError as exc:
            raise ToolInputError(str(exc)) from exc
        return {"value": value, "category": category}

    return validate


def _make_save_handler(service: MemoryService) -> Callable[..., dict]:
    def handler(value: str, category: str = DEFAULT_CATEGORY) -> dict:
        try:
            record, created = service.save(value, category)
        except MemoryStoreError:
            return {"text": _STORE_ERROR_TEXT}
        except MemoryValidationError as exc:
            return {"text": f"I couldn't save that memory: {exc}"}
        if created:
            text = (
                "Saved. I'll remember that "
                f"(memory {record.identifier})."
            )
        else:
            text = (
                "I already had that saved "
                f"(memory {record.identifier})."
            )
        return {"text": text, "id": record.identifier, "saved": created}

    return handler


def _make_list_handler(service: MemoryService) -> Callable[..., dict]:
    def handler() -> dict:
        try:
            records = service.list()
        except MemoryStoreError:
            return {"text": _STORE_ERROR_TEXT}
        if not records:
            return {"text": "I don't have any saved memories yet.", "count": 0}
        text = "Saved memories: " + "; ".join(
            f"{record.identifier}: {record.value}" for record in records
        )
        if len(text) > MAX_LIST_CHARS:
            text = text[:MAX_LIST_CHARS].rstrip() + "..."
        return {"text": text, "count": len(records)}

    return handler


def _make_get_handler(service: MemoryService) -> Callable[..., dict]:
    def handler(selector: str) -> dict:
        try:
            matches = service.find(selector)
        except MemoryStoreError:
            return {"text": _STORE_ERROR_TEXT}
        if len(matches) == 1:
            record = matches[0]
            return {
                "text": f"Memory {record.identifier}: {record.value}.",
                "id": record.identifier,
            }
        if not matches:
            return {"text": "I don't have a memory matching that."}
        return {
            "text": (
                "More than one saved memory matches that; please use the "
                "memory id."
            )
        }

    return handler


def _make_delete_handler(service: MemoryService) -> Callable[..., dict]:
    def handler(selector: str) -> dict:
        try:
            matches = service.delete(selector)
        except MemoryStoreError:
            return {"text": _STORE_ERROR_TEXT}
        if len(matches) == 1:
            return {
                "text": f"Deleted memory {matches[0].identifier}.",
                "deleted": True,
            }
        if not matches:
            return {
                "text": (
                    "I don't have a memory matching that, "
                    "so nothing was deleted."
                )
            }
        return {
            "text": (
                "More than one saved memory matches that; nothing was "
                "deleted. Please use the memory id."
            )
        }

    return handler


def _make_clear_handler(service: MemoryService) -> Callable[..., dict]:
    def handler() -> dict:
        try:
            removed = service.clear()
        except MemoryStoreError:
            return {"text": _STORE_ERROR_TEXT}
        if removed:
            return {
                "text": f"Cleared {removed} saved memories.",
                "removed": removed,
            }
        return {"text": "There were no saved memories to clear."}

    return handler


def build_memory_specs(service: MemoryService) -> tuple[ToolSpec, ...]:
    """Return fresh :class:`ToolSpec` objects bound to ``service``.

    Save / list / retrieve are read- or append-only and are ``SAFE``.
    Delete and clear are destructive and are declared ``CONFIRM`` so the
    executor always demands a fresh, bound approval.
    """
    return (
        ToolSpec(
            name=TOOL_MEMORY_SAVE,
            description="save a memory you were explicitly asked to keep",
            permission=PermissionLevel.SAFE,
            validate=_make_save_validator(
                MAX_VALUE_CHARS, MAX_CATEGORY_CHARS
            ),
            handler=_make_save_handler(service),
            describe=lambda args: "save one memory",
        ),
        ToolSpec(
            name=TOOL_MEMORY_LIST,
            description="list the memories that were explicitly saved",
            permission=PermissionLevel.SAFE,
            validate=_reject_arguments,
            handler=_make_list_handler(service),
        ),
        ToolSpec(
            name=TOOL_MEMORY_GET,
            description="retrieve one saved memory",
            permission=PermissionLevel.SAFE,
            validate=_validate_selector,
            handler=_make_get_handler(service),
            describe=lambda args: "retrieve one saved memory",
        ),
        ToolSpec(
            name=TOOL_MEMORY_DELETE,
            description="delete one saved memory",
            permission=PermissionLevel.CONFIRM,
            validate=_validate_selector,
            handler=_make_delete_handler(service),
            describe=lambda args: "delete one saved memory",
        ),
        ToolSpec(
            name=TOOL_MEMORY_CLEAR,
            description="delete every saved memory",
            permission=PermissionLevel.CONFIRM,
            validate=_reject_arguments,
            handler=_make_clear_handler(service),
            describe=lambda args: "delete all saved memories",
        ),
    )


def register_memory_tools(
    registry: ToolRegistry, service: MemoryService
) -> None:
    """Register every memory tool on an existing registry."""
    for spec in build_memory_specs(service):
        registry.register(spec)
