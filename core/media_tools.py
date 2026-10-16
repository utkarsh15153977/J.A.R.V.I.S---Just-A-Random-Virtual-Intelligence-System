"""Safe local media playback tools for Jarvis.

Three tools make up the media capability:

* ``media_play`` (``CONFIRM``) searches the approved folders for a title
  and, when the match is unambiguous, opens the file. When several files
  plausibly match it does **not** pick one: it lists the candidates and
  waits for an explicit follow-up choice.
* ``media_select`` (``CONFIRM``) opens one candidate from the most recent
  ``media_play`` result by its position. It is the *only* way a file from
  a multi-candidate result is ever opened.
* ``media_cancel`` (``SAFE``) forgets any pending candidates.

Selection state lives in a small :class:`MediaSession` owned by the
composition root. It is an in-memory, per-session object: nothing about a
media selection is ever written to persistent memory. Every handler
re-validates the chosen path (exists, is a file, supported extension and
still inside an approved root) immediately before opening it, so a stale
or forged selection cannot escape the approved folders.
"""
from __future__ import annotations

from core.media import (
    MediaDiscovery,
    MediaError,
    MediaMatch,
    SubprocessMediaLauncher,
)
from core.permissions import PermissionLevel
from core.tools import ToolInputError, ToolRegistry, ToolSpec

TOOL_MEDIA_PLAY = "media_play"
TOOL_MEDIA_SELECT = "media_select"
TOOL_MEDIA_CANCEL = "media_cancel"

MAX_CANDIDATES = 10


class MediaSession:
    """The candidates offered by the most recent media search.

    Owned by the composition root and shared with the media tool handlers
    through a closure. It holds only discovered :class:`MediaMatch`
    objects and never persists anything to disk.
    """

    def __init__(self, max_candidates: int = MAX_CANDIDATES) -> None:
        self._max = max(1, int(max_candidates))
        self._candidates: list[MediaMatch] = []

    @property
    def active(self) -> bool:
        return bool(self._candidates)

    def candidates(self) -> tuple[MediaMatch, ...]:
        return tuple(self._candidates)

    def offer(self, matches: tuple[MediaMatch, ...] | list[MediaMatch]) -> None:
        self._candidates = list(matches)[: self._max]

    def clear(self) -> None:
        self._candidates = []

    def resolve(self, index: int) -> MediaMatch | None:
        if isinstance(index, bool) or not isinstance(index, int):
            return None
        if 0 <= index < len(self._candidates):
            return self._candidates[index]
        return None


def _is_unambiguous(candidates: list[MediaMatch]) -> bool:
    """True when exactly one file may be played without a follow-up.

    A single result is obviously unambiguous. A single *exact* match is
    also accepted even if weaker partial matches exist, because the user
    named the whole title.
    """
    if len(candidates) == 1:
        return True
    exact = [m for m in candidates if m.is_exact]
    return len(exact) == 1


def _validate_play(raw) -> dict:
    query = raw.get("query") if hasattr(raw, "get") else None
    if not isinstance(query, str) or not query.strip():
        raise ToolInputError("argument 'query' must be a non-empty string")
    if len(query) > 200:
        raise ToolInputError("argument 'query' is too long")
    return {"query": query.strip()}


def _validate_select(raw) -> dict:
    index = raw.get("index") if hasattr(raw, "get") else None
    if isinstance(index, bool) or not isinstance(index, int):
        raise ToolInputError("argument 'index' must be an integer")
    if index < 0:
        raise ToolInputError("argument 'index' must not be negative")
    return {"index": index}


def _validate_no_args(raw) -> dict:
    if raw:
        raise ToolInputError("this command takes no arguments")
    return {}


class _LaunchGuard:
    """Re-validate and open one discovered file. Never raises."""

    def __init__(
        self,
        discovery: MediaDiscovery,
        launcher: SubprocessMediaLauncher,
    ) -> None:
        self._discovery = discovery
        self._launcher = launcher

    def launch(self, match: MediaMatch) -> str:
        """Return a developer-authored reply; opens the file on success."""
        path = match.path
        try:
            resolved = path.resolve(strict=False)
        except Exception:
            return "That file could not be read."
        if not self._discovery.is_within_roots(resolved):
            return "That file is outside the approved folders."
        if not self._discovery.is_supported(resolved):
            return "That file is not a supported video."
        try:
            if not resolved.is_file():
                return "That file is no longer available."
        except OSError:
            return "That file could not be read."
        try:
            self._launcher.launch(resolved)
        except MediaError:
            return "I could not open that file."
        return f"Playing {match.display_name}."


class _MediaPlayHandler:
    def __init__(
        self,
        discovery: MediaDiscovery,
        session: MediaSession,
        guard: _LaunchGuard,
    ) -> None:
        self._discovery = discovery
        self._session = session
        self._guard = guard

    def __call__(self, query: str) -> dict:
        candidates = self._discovery.find_candidates(query)
        if not candidates:
            self._session.clear()
            return {
                "text": "I couldn't find a video matching that in the "
                "approved folders."
            }
        if _is_unambiguous(candidates):
            self._session.clear()
            return {"text": self._guard.launch(candidates[0])}
        self._session.offer(candidates)
        lines = [
            f"{position}. {match.display_name}"
            for position, match in enumerate(self._session.candidates(), 1)
        ]
        return {
            "text": "I found several matches. Say 'play the first one' or "
            "'play the second one' to choose:\n" + "\n".join(lines)
        }


class _MediaSelectHandler:
    def __init__(
        self,
        session: MediaSession,
        guard: _LaunchGuard,
    ) -> None:
        self._session = session
        self._guard = guard

    def __call__(self, index: int) -> dict:
        match = self._session.resolve(index)
        if match is None:
            return {
                "text": "There is no current list to choose from. Say "
                "'play <title>' first."
            }
        text = self._guard.launch(match)
        # A selection is single-use: whatever the outcome, forget it.
        self._session.clear()
        return {"text": text}


class _MediaCancelHandler:
    def __init__(self, session: MediaSession) -> None:
        self._session = session

    def __call__(self) -> dict:
        self._session.clear()
        return {"text": "Cancelled."}


def _describe_select(session: MediaSession):
    def describe(args) -> str:
        match = session.resolve(args.get("index"))
        if match is None:
            return "play a media file"
        return f'play "{match.display_name}"'

    return describe


def build_media_specs(
    discovery: MediaDiscovery,
    session: MediaSession,
    launcher: SubprocessMediaLauncher,
) -> tuple[ToolSpec, ToolSpec, ToolSpec]:
    """Build the three media tool specs sharing one discovery/session."""
    guard = _LaunchGuard(discovery, launcher)
    play = ToolSpec(
        name=TOOL_MEDIA_PLAY,
        description="Find and play a local video from the approved folders",
        permission=PermissionLevel.CONFIRM,
        validate=_validate_play,
        handler=_MediaPlayHandler(discovery, session, guard),
        timeout=30.0,
        describe=lambda args: "search the approved folders and play a video",
    )
    select = ToolSpec(
        name=TOOL_MEDIA_SELECT,
        description="Play one of the media search results by position",
        permission=PermissionLevel.CONFIRM,
        validate=_validate_select,
        handler=_MediaSelectHandler(session, guard),
        timeout=30.0,
        describe=_describe_select(session),
    )
    cancel = ToolSpec(
        name=TOOL_MEDIA_CANCEL,
        description="Forget the current media search results",
        permission=PermissionLevel.SAFE,
        validate=_validate_no_args,
        handler=_MediaCancelHandler(session),
    )
    return play, select, cancel


def register_media_tools(
    registry: ToolRegistry,
    discovery: MediaDiscovery,
    session: MediaSession,
    launcher: SubprocessMediaLauncher | None = None,
) -> None:
    """Register the three media tools against ``registry``."""
    specs = build_media_specs(
        discovery,
        session,
        launcher if launcher is not None else SubprocessMediaLauncher(),
    )
    for spec in specs:
        registry.register(spec)
