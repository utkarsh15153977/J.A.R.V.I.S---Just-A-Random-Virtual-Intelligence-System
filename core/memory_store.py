"""Local, file-backed persistence for Jarvis memories.

Memory is stored as a single JSON file under a per-user application data
directory (overridable via ``MemoryConfig.data_dir``). The store is small
and deliberate:

* **One fixed filename.** The path is always ``<data_dir>/memories.json``;
  the store never derives a filename from user input, so no memory content
  can influence where data is written.
* **Atomic writes.** Data is written to a sibling temporary file and moved
  into place with :func:`os.replace`, so a crash mid-write cannot leave a
  half-written file.
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
    """A single-file JSON implementation of :class:`core.memory.MemoryStore`."""

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
        except OSError as exc:
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
        """Atomically write ``records`` as the complete memory set."""
        payload = {
            "version": _FORMAT_VERSION,
            "memories": [record.to_dict() for record in records],
        }
        directory = self.path.parent
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise MemoryStoreError(
                f"could not create the memory directory: {exc}"
            ) from exc
        _restrict(directory, 0o700)
        temporary = self.path.with_name(self.path.name + ".tmp")
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            _restrict(temporary, 0o600)
            os.replace(temporary, self.path)
        except OSError as exc:
            _safe_unlink(temporary)
            raise MemoryStoreError(
                f"could not write the memory file: {exc}"
            ) from exc
        logger.info("Saved %d memory record(s).", len(payload["memories"]))


def _restrict(path: Path, mode: int) -> None:
    """Best-effort owner-only permissions; silently skipped on Windows."""
    try:
        os.chmod(path, mode)
    except OSError:
        logger.debug("Could not set permissions on %s.", path)


def _safe_unlink(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()
