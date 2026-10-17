"""Google OAuth for the optional daily briefing.

The briefing reads two read-only Google services (Calendar and Tasks). This
module owns the *only* place credentials are handled: it locates the OAuth
client-secret file and the stored token, loads and refreshes the token, and
runs the one-time installed-app authorisation flow. It deliberately does
**not** import the Google libraries at module import time, so importing
``core.google_auth`` (and therefore starting Jarvis without the briefing
enabled) never requires the Google packages to be installed.

Security posture
----------------
* **Read-only scopes only.** The default scopes are calendar-events-read and
  tasks-read; nothing requests write access.
* **User-started authorisation.** A browser is opened only by the explicit
  ``--briefing-auth`` command, never on startup and never during a briefing.
* **Untracked storage.** The secret and token live in a per-user data
  directory (mirroring ``core.memory_store.default_memory_dir``), get
  best-effort owner-only permissions, and are never written to
  configuration, a log, an exception message or a spoken reply.
* **Fail closed with a category.** A missing token, a missing secret and a
  failed refresh raise :class:`GoogleAuthError` carrying a
  :class:`~core.briefing.BriefingStatus`, so a caller can tell "not
  configured" from "it failed" without ever seeing a credential.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
from pathlib import Path
from typing import Iterable

from core.briefing import BriefingError, BriefingStatus

logger = logging.getLogger("jarvis.briefing.google")

CALENDAR_READONLY_SCOPE = "https://www.googleapis.com/auth/calendar.events.readonly"
TASKS_READONLY_SCOPE = "https://www.googleapis.com/auth/tasks.readonly"

DEFAULT_SCOPES: tuple[str, ...] = (CALENDAR_READONLY_SCOPE, TASKS_READONLY_SCOPE)

_DATA_DIR_NAME = "Jarvis"
_GOOGLE_SUBDIR = "google"
_CLIENT_SECRET_FILENAME = "client_secret.json"
_TOKEN_FILENAME = "token.json"


class GoogleAuthError(BriefingError):
    """Credentials could not be resolved, refreshed or created."""

    def __init__(self, status: BriefingStatus, message: str) -> None:
        self.status = status
        self.message = message
        super().__init__(message)


def default_google_dir() -> str:
    """Return the per-user directory that holds briefing credentials.

    Mirrors :func:`core.memory_store.default_memory_dir` (one application
    data directory per user) and adds a ``google`` subdirectory, so the OAuth
    secret and token live together and beside the other Jarvis user data.
    """
    home = Path.home()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(home / "AppData" / "Local")
        root = Path(base) / _DATA_DIR_NAME
    elif sys.platform == "darwin":
        root = home / "Library" / "Application Support" / _DATA_DIR_NAME
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(home / ".local" / "share")
        root = Path(base) / _DATA_DIR_NAME
    return str(root / _GOOGLE_SUBDIR)


def google_libraries_available() -> bool:
    """True when the optional Google client libraries are importable."""
    try:
        import google.auth
        import google.oauth2.credentials  # noqa: F401
        import google_auth_oauthlib.flow  # noqa: F401
        import googleapiclient.discovery  # noqa: F401
    except Exception:  # pragma: no cover - depends on optional install
        return False
    return True


def _has_control_characters(text: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in text)


def _resolve_path(explicit: str, default_name: str) -> Path:
    path = (
        Path(explicit)
        if explicit
        else Path(default_google_dir()) / default_name
    )
    if _has_control_characters(str(path)):
        raise GoogleAuthError(
            BriefingStatus.UNAVAILABLE,
            "a Google credential path contains control characters",
        )
    return path


def _restrict(path: Path, mode: int) -> None:
    """Best-effort owner-only permissions; never fatal if unsupported."""
    with contextlib.suppress(OSError, NotImplementedError):
        os.chmod(path, mode)


def _require_libraries() -> None:
    if not google_libraries_available():
        raise GoogleAuthError(
            BriefingStatus.UNAVAILABLE,
            "the Google API libraries are not installed",
        )


def load_credentials(
    client_secret_path: str = "",
    token_path: str = "",
    *,
    scopes: Iterable[str] = DEFAULT_SCOPES,
    allow_interactive: bool = False,
):
    """Return refreshed Google credentials for the read-only scopes.

    Raises :class:`GoogleAuthError` with ``UNAVAILABLE`` when the libraries
    are missing or no token exists (and interactive auth was not allowed),
    and with ``ERROR`` when an existing token cannot be refreshed. A token
    value never appears in a message or a log line.
    """
    _require_libraries()
    token_file = _resolve_path(token_path, _TOKEN_FILENAME)
    secret_file = _resolve_path(client_secret_path, _CLIENT_SECRET_FILENAME)

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if not token_file.is_file():
        if not allow_interactive:
            raise GoogleAuthError(
                BriefingStatus.UNAVAILABLE,
                "Google authorization is required; run --briefing-auth once",
            )
        return run_authorization(
            client_secret_path=str(secret_file),
            token_path=str(token_file),
            scopes=scopes,
        )

    try:
        credentials = Credentials.from_authorized_user_file(
            str(token_file), list(scopes)
        )
    except Exception:
        raise GoogleAuthError(
            BriefingStatus.ERROR,
            "the stored Google token could not be read; run --briefing-auth again",
        ) from None

    if getattr(credentials, "expired", False) or not getattr(
        credentials, "valid", False
    ):
        if not getattr(credentials, "refresh_token", None):
            raise GoogleAuthError(
                BriefingStatus.UNAVAILABLE,
                "the stored Google token cannot be refreshed; "
                "run --briefing-auth again",
            )
        try:
            credentials.refresh(Request())
        except Exception:
            raise GoogleAuthError(
                BriefingStatus.ERROR,
                "the Google token could not be refreshed; run --briefing-auth again",
            ) from None
        _save_token(credentials, token_file)

    return credentials


def run_authorization(
    client_secret_path: str,
    token_path: str,
    *,
    scopes: Iterable[str] = DEFAULT_SCOPES,
):
    """Run the installed-app OAuth flow and persist the granted token.

    Interactive by design: this is only reached from the explicit
    ``--briefing-auth`` command, never from a normal run or on startup.
    """
    _require_libraries()
    secret_file = _resolve_path(client_secret_path, _CLIENT_SECRET_FILENAME)
    token_file = _resolve_path(token_path, _TOKEN_FILENAME)

    if not secret_file.is_file():
        raise GoogleAuthError(
            BriefingStatus.UNAVAILABLE,
            "the Google client-secret file was not found",
        )

    from google_auth_oauthlib.flow import InstalledAppFlow

    try:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(secret_file), list(scopes)
        )
        credentials = flow.run_local_server(port=0)
    except Exception:
        raise GoogleAuthError(
            BriefingStatus.ERROR, "Google authorization did not complete"
        ) from None

    _save_token(credentials, token_file)
    return credentials


def _save_token(credentials, token_file: Path) -> None:
    """Write the token, creating the parent directory owner-only if needed."""
    try:
        token_file.parent.mkdir(parents=True, exist_ok=True)
        _restrict(token_file.parent, 0o700)
        token_file.write_text(credentials.to_json(), encoding="utf-8")
        _restrict(token_file, 0o600)
    except OSError:
        raise GoogleAuthError(
            BriefingStatus.ERROR,
            "the Google token could not be stored",
        ) from None


def credentials_path_exists(token_path: str = "") -> bool:
    """True when a token file exists (used only for status messages)."""
    try:
        return _resolve_path(token_path, _TOKEN_FILENAME).is_file()
    except GoogleAuthError:
        return False
