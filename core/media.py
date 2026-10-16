"""Local media discovery and safe launching helpers for Jarvis.

This module contains **no** authority to run anything on its own. It
offers two narrow, side-effect-visible pieces:

* :class:`MediaDiscovery` walks a fixed set of *approved* root folders
  and returns video files whose names match a spoken query. Traversal is
  bounded, skips symbolic links by default, and keeps every returned
  path inside a root (subject to the Windows reparse-point caveat in
  ``docs/safety.md``).
* :class:`SubprocessMediaLauncher` opens a single, already-validated file
  with the operating system's default handler. It never uses a shell and
  never interprets the file name or title as a command.

The selection/session logic that decides *which* file may be launched
lives in ``core.media_tools`` and is deliberately separate so that this
module can be tested in isolation.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

VIDEO_EXTENSIONS: tuple[str, ...] = (
    ".mp4",
    ".mkv",
    ".avi",
    ".mov",
    ".wmv",
    ".m4v",
    ".webm",
    ".mpeg",
)

_MAX_SCAN_FILES: int = 50000

_PUNCT = re.compile(r"[_\.\-]+")
# A trailing "from drive E" / "on disk D" / "in folder X" tail is
# informational: the approved roots already bound where Jarvis may look,
# so it is stripped before matching a title.
_LOCATION_TAIL = re.compile(
    r"\s+(?:from|on|in)\s+(?:the\s+)?"
    r"(?:drive|disk|folder|directory|partition)\s+[A-Za-z0-9]+\.?$",
    re.IGNORECASE,
)


class MediaError(ValueError):
    """Raised when a media file cannot be launched safely."""


@dataclass(frozen=True)
class MediaMatch:
    """One discovered candidate and how well it matched the query."""

    path: Path
    display_name: str
    match_score: float
    is_exact: bool


def _strip_media_location(query: str) -> str:
    """Remove a trailing drive/folder hint from a spoken title."""
    text = (query or "").strip()
    previous = None
    while text and text != previous:
        previous = text
        text = _LOCATION_TAIL.sub("", text).strip()
    return text


def _normalize_for_match(name: str) -> str:
    """Lower-cased title with separators collapsed, extension removed."""
    s = (name or "").strip().lower()
    ext = Path(s).suffix
    base = s[: -len(ext)] if ext else s
    base = _PUNCT.sub(" ", base)
    return " ".join(base.split())


class MediaDiscovery:
    """Find video files under a fixed set of approved root folders."""

    def __init__(
        self,
        roots: Sequence[Path] | Sequence[str],
        *,
        extensions: Sequence[str] = VIDEO_EXTENSIONS,
        max_scan: int = _MAX_SCAN_FILES,
    ) -> None:
        self._roots: tuple[Path, ...] = tuple(
            Path(r).resolve(strict=False) for r in roots
        )
        self._exts = tuple(
            (e if e.startswith(".") else "." + e).lower() for e in extensions
        )
        self._max_scan = max(1, int(max_scan))

    @property
    def roots(self) -> tuple[Path, ...]:
        return self._roots

    def is_supported(self, path: Path) -> bool:
        """True when ``path`` has a launchable video extension."""
        try:
            return path.suffix.lower() in self._exts
        except Exception:
            return False

    def is_within_roots(self, path: Path) -> bool:
        """True when ``path`` resolves inside one of the approved roots.

        The comparison is done on fully resolved paths, so a symlink or
        Windows reparse point that resolves outside every root is
        rejected. It is a defence-in-depth check performed again
        immediately before a file is opened.
        """
        try:
            resolved = path.resolve(strict=False)
        except Exception:
            return False
        for root in self._roots:
            if resolved == root:
                continue
            try:
                if resolved.is_relative_to(root):
                    return True
            except Exception:
                continue
        return False

    def find_candidates(
        self,
        query: str,
        *,
        follow_symlinks: bool = False,
    ) -> list[MediaMatch]:
        """Return matching files, best (exact) first, bounded by ``max_scan``."""
        qnorm = _normalize_for_match(_strip_media_location(query))
        if not qnorm:
            return []
        candidates: list[MediaMatch] = []
        scanned = 0
        for root in self._roots:
            if not root.exists() or not root.is_dir():
                continue
            for dirpath, dirnames, filenames in os.walk(
                root, followlinks=follow_symlinks
            ):
                if not follow_symlinks:
                    # Never descend through a symlinked/reparse-point
                    # directory: it could resolve outside the root.
                    dirnames[:] = [
                        name
                        for name in dirnames
                        if not (Path(dirpath) / name).is_symlink()
                    ]
                for name in filenames:
                    scanned += 1
                    if scanned > self._max_scan:
                        return self._rank(candidates)
                    p = Path(dirpath) / name
                    try:
                        if p.suffix.lower() not in self._exts:
                            continue
                        if not follow_symlinks and p.is_symlink():
                            continue
                        if not p.is_file():
                            continue
                    except OSError:
                        continue
                    mnorm = _normalize_for_match(name)
                    if not mnorm:
                        continue
                    if mnorm == qnorm:
                        score = 1.0
                        is_exact = True
                    elif mnorm.startswith(qnorm):
                        score = 0.8 + min(
                            len(qnorm) / (len(mnorm) + 1.0), 0.15
                        )
                        is_exact = False
                    elif qnorm in mnorm:
                        score = 0.6 + min(
                            len(qnorm) / (len(mnorm) + 1.0), 0.2
                        )
                        is_exact = False
                    else:
                        continue
                    try:
                        resolved = p.resolve(strict=False)
                    except OSError:
                        continue
                    if not self.is_within_roots(resolved):
                        continue
                    candidates.append(
                        MediaMatch(
                            path=resolved,
                            display_name=name,
                            match_score=score,
                            is_exact=is_exact,
                        )
                    )
        return self._rank(candidates)

    @staticmethod
    def _rank(candidates: list[MediaMatch]) -> list[MediaMatch]:
        return sorted(
            candidates,
            key=lambda m: (1 if m.is_exact else 0, m.match_score, m.display_name),
            reverse=True,
        )


class SubprocessMediaLauncher:
    """Open one already-validated file with the OS default handler.

    ``opener`` is injectable for tests; when it is ``None`` on Windows the
    file is opened with ``os.startfile``. No shell is ever used and the
    file name is never interpreted as a command.
    """

    def __init__(self, opener: Optional[Callable[[str], None]] = None) -> None:
        self._opener = opener

    def launch(self, path: Path) -> None:
        pstr = str(path)
        try:
            if self._opener is not None:
                self._opener(pstr)
                return
            if os.name == "nt":
                os.startfile(pstr)  # type: ignore[attr-defined]
                return
            raise MediaError("media launch not supported on this platform")
        except MediaError:
            raise
        except OSError as exc:
            raise MediaError("could not open the media file") from exc
