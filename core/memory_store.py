"""Local, file-backed persistence for Jarvis memories.

Memory is stored as a single JSON file under a per-user application data
directory (overridable via ``MemoryConfig.data_dir``). The store is small
and deliberate:

* **One fixed filename.** The path is always ``<data_dir>/memories.json``;
  the store never derives a filename from user input, so no memory content
  can influence where data is written.
* **Atomic writes.** Data is written to a *uniquely named* sibling
  temporary file, flushed to disk with ``os.fsync`` and moved into place
  with :func:`os.replace`, so a crash or shutdown mid-write can leave
  either the previous file or the new one — never a half-written mix — and
  a failed write cleans up its temporary file and leaves existing data
  untouched.
* **Validated path.** Construction only inspects the configured path (no
  read, no creation) and refuses an unusable one — control characters, a
  parent that is not a directory, or a ``memories.json`` that is a
  directory — with :class:`~core.memory.MemoryStoreError`.
* **Owner-only permissions** where the platform supports them (0o600 file
  / 0o700 directory). Windows does not honour POSIX modes; that limitation
  is documented rather than pretended away, and no encryption is claimed.
* **Fail-safe reads.** A missing file is simply "no memories yet" and never
  creates a file. A corrupt or malformed file raises
  :class:`~core.memory.MemoryStoreError` and is never silently overwritten.

The module performs only filesystem I/O for the memory feature; it is
imported (and constructed) only when memory is enabled, and it never reads
or writes anything on import.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Optional

from core.memory import MemoryRecord, MemoryStoreError

logger = logging.getLogger("jarvis.memory")

_MEMORY_FILENAME = "memories.json"
_DATA_DIR_NAME = "Jarvis"
_FORMAT_VERSION = 1


def default_memory_dir() -> str:
    """Return the per-user application data directory for the platform.

    Mirrors the speaker-enrolment location so Jarvis keeps its user data in
    one place; duplicated here so ``core`` never imports ``voice``.
    """
    home = Path.home()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(
            home / "AppData" / "Local"
        )
        return str(Path(base) / _DATA_DIR_NAME)
    if sys.platform == "darwin":
        return str(home / "Library" / "Application Support" / _DATA_DIR_NAME)
    base = os.environ.get("XDG_DATA_HOME") or str(home / ".local" / "share")
    return str(Path(base) / _DATA_DIR_NAME)


class JsonMemoryStore:
    """A single-file JSON implementation of :class:`core.memory.MemoryStore`.

    Construction validates the *configured path only* (no content is
    read, no file or directory is created): an unusable path — control
    characters, a parent that already exists as something other than a
    directory, or a ``memories.json`` that is itself a directory — is
    refused immediately with :class:`~core.memory.MemoryStoreError`
    instead of surfacing later as an obscure filesystem error. Everything
    else happens on demand: :meth:`load` reads, :meth:`save` writes.
    """

    def __init__(
        self,
        base_dir: Optional[os.PathLike] = None,
        *,
        path: Optional[os.PathLike] = None,
    ) -> None:
        if path is not None:
            self.path = Path(path)
        else:
            base = base_dir if base_dir is not None else default_memory_dir()
            self.path = Path(base) / _MEMORY_FILENAME
        self._validate_path()

    def _validate_path(self) -> None:
        """Refuse a path the store can never use safely (fail fast)."""
        if _has_control_characters(str(self.path)):
            raise MemoryStoreError(
                "the configured memory path contains control characters"
            )
        try:
            if self.path.parent.exists() and not self.path.parent.is_dir():
                raise MemoryStoreError(
                    "the configured memory directory is not a directory"
                )
            if self.path.exists() and not self.path.is_file():
                raise MemoryStoreError(
                    "the configured memory file is not a regular file"
                )
        except OSError as exc:
            raise MemoryStoreError(
                f"could not inspect the memory path: {exc}"
            ) from exc

    def exists(self) -> bool:
        return self.path.is_file()

    def load(self) -> tuple[MemoryRecord, ...]:
        """Return every stored memory, or ``()`` when none exist.

        A missing file is normal and creates nothing. A file that exists
        but cannot be parsed or validated raises
        :class:`~core.memory.MemoryStoreError`.
        """
        if not self.exists():
            return ()
        try:
            raw = self.path.read_text(encoding="utf-8")
        except (OSError, ValueError) as exc:
            raise MemoryStoreError(
                f"could not read the memory file: {exc}"
            ) from exc
        try:
            data = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise MemoryStoreError(
                "the memory file is not valid JSON"
            ) from exc
        if not isinstance(data, dict):
            raise MemoryStoreError("the memory file has the wrong shape")
        if data.get("version") != _FORMAT_VERSION:
            raise MemoryStoreError("the memory file has an unknown version")
        entries = data.get("memories")
        if not isinstance(entries, list):
            raise MemoryStoreError("the memory file has no memories list")
        return tuple(MemoryRecord.from_dict(entry) for entry in entries)

    def save(self, records) -> None:
        """Atomically write ``records`` as the complete memory set.

        The payload goes to a uniquely named sibling temporary file that
        is flushed to disk and then moved into place with
        :func:`os.replace`. Any failure (including a filesystem path that
        only fails at write time) removes the temporary file and raises
        :class:`~core.memory.MemoryStoreError`, leaving the previous file
        exactly as it was: a failed save can never corrupt or truncate
        existing memories.
        """
        payload = {
            "version": _FORMAT_VERSION,
            "memories": [record.to_dict() for record in records],
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        directory = self.path.parent
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise MemoryStoreError(
                f"could not create the memory directory: {exc}"
            ) from exc
        _restrict(directory, 0o700)
        handle, temporary = _create_temporary(directory)
        try:
            _write_and_sync(handle, text)
        except (OSError, ValueError) as exc:
            _safe_unlink(temporary)
            raise MemoryStoreError(
                f"could not write the memory file: {exc}"
            ) from exc
        _restrict(temporary, 0o600)
        try:
            os.replace(temporary, self.path)
        except OSError as exc:
            _safe_unlink(temporary)
            raise MemoryStoreError(
                f"could not write the memory file: {exc}"
            ) from exc
        logger.info("Saved %d memory record(s).", len(payload["memories"]))


def _has_control_characters(text: str) -> bool:
    """True when ``text`` contains a control character (including NUL)."""
    return any(
        ord(character) < 32 or ord(character) == 127 for character in text
    )


def _create_temporary(directory: Path) -> tuple[int, Path]:
    """Create a private, uniquely named temporary file in ``directory``.

    An unpredictable name avoids a fixed ``memories.json.tmp`` that a
    concurrent run could clobber; ``mkstemp`` also creates the file
    exclusively, so it can never be redirected through a pre-made name.
    """
    try:
        handle, name = tempfile.mkstemp(
            prefix=_MEMORY_FILENAME + ".", suffix=".tmp", dir=directory
        )
    except (OSError, ValueError) as exc:
        raise MemoryStoreError(
            f"could not create a temporary memory file: {exc}"
        ) from exc
    return handle, Path(name)


def _write_and_sync(handle: int, text: str) -> None:
    """Write ``text`` through ``handle``, flush it to disk, then close it.

    The descriptor is closed on every path: if :func:`os.fdopen` itself
    fails it is closed explicitly, and once the stream exists the
    ``with`` block owns it.
    """
    try:
        stream = os.fdopen(handle, "w", encoding="utf-8")
    except (OSError, ValueError):
        with contextlib.suppress(OSError):
            os.close(handle)
        raise
    with stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())


def _restrict(path: Path, mode: int) -> None:
    """Best-effort owner-only permissions; silently skipped on Windows."""
    try:
        os.chmod(path, mode)
    except OSError:
        logger.debug("Could not set permissions on %s.", path)


def _safe_unlink(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()
