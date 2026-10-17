"""Google OAuth for optional calendar event creation (write).

This is the write-side counterpart to :mod:`core.google_auth`. It is kept
deliberately separate so the read-only daily briefing and the write-capable
event creation never share a token or a scope: the briefing keeps
``calendar.events.readonly`` + ``tasks.readonly`` and its own token, while
creation requests only ``calendar.events`` and stores its token in a separate
file. A token is therefore never silently upgraded, and a write grant can be
revoked without disturbing the briefing.

Security posture (mirrors :mod:`core.google_auth`)
-------------------------------------------------
* **Narrowest write scope only.** ``CALENDAR_WRITE_SCOPE`` is
  ``calendar.events``; nothing requests full-calendar access.
* **User-started authorization.** A browser is opened only by the explicit
  ``--calendar-write-auth`` command, never on startup and never during a
  creation. Importing this module never imports the Google libraries.
* **Untracked storage.** The secret and token live in the per-user Google
  directory, get best-effort owner-only permissions and are never written to
  configuration, a log, an exception message or a spoken reply.
* **Separate token file.** ``token_calendar_write.json`` is distinct from the
  briefing's ``token.json``.
* **Fail closed with a category.** A missing token, a missing secret and a
  failed refresh raise :class:`GoogleAuthError` carrying a
  :class:`~core.briefing.BriefingStatus`.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
from pathlib import Path
from typing import Iterable

from core.briefing import BriefingError, BriefingStatus

logger = logging.getLogger("jarvis.calendar.google")

CALENDAR_WRITE_SCOPE = "https://www.googleapis.com/auth/calendar.events"

DEFAULT_WRITE_SCOPES: tuple[str, ...] = (CALENDAR_WRITE_SCOPE,)

_DATA_DIR_NAME = "Jarvis"
_GOOGLE_SUBDIR = "google"
_CLIENT_SECRET_FILENAME = "client_secret.json"
_TOKEN_WRITE_FILENAME = "token_calendar_write.json"


class GoogleAuthError(BriefingError):
    """Credentials could not be resolved, refreshed or created."""

    def __init__(self, status: BriefingStatus, message: str) -> None:
        self.status = status
        self.message = message
        super().__init__(message)


def default_google_dir() -> str:
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
    path = Path(explicit) if explicit else Path(default_google_dir()) / default_name
    if _has_control_characters(str(path)):
        raise GoogleAuthError(
            BriefingStatus.UNAVAILABLE,
            "a Google credential path contains control characters",
        )
    return path


def _restrict(path: Path, mode: int) -> None:
    with contextlib.suppress(OSError, NotImplementedError):
        os.chmod(path, mode)


def _require_libraries() -> None:
    if not google_libraries_available():
        raise GoogleAuthError(
            BriefingStatus.UNAVAILABLE,
            "the Google API libraries are not installed",
        )


def load_write_credentials(
    client_secret_path: str = "",
    token_path: str = "",
    *,
    scopes: Iterable[str] = DEFAULT_WRITE_SCOPES,
    allow_interactive: bool = False,
):
    """Return refreshed write credentials for the ``calendar.events`` scope.

    Raises :class:`GoogleAuthError` with ``UNAVAILABLE`` when the libraries
    are missing or no write token exists (and interactive auth was not
    allowed), and with ``ERROR`` when an existing token cannot be refreshed.
    A token value never appears in a message or a log line.
    """
    _require_libraries()
    token_file = _resolve_path(token_path, _TOKEN_WRITE_FILENAME)
    secret_file = _resolve_path(client_secret_path, _CLIENT_SECRET_FILENAME)

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if not token_file.is_file():
        if not allow_interactive:
            raise GoogleAuthError(
                BriefingStatus.UNAVAILABLE,
                "calendar write authorization is required; "
                "run --calendar-write-auth once",
            )
        return run_write_authorization(
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
            "the stored calendar write token could not be read; "
            "run --calendar-write-auth again",
        ) from None

    if getattr(credentials, "expired", False) or not getattr(
        credentials, "valid", False
    ):
        if not getattr(credentials, "refresh_token", None):
            raise GoogleAuthError(
                BriefingStatus.UNAVAILABLE,
                "the stored calendar write token cannot be refreshed; "
                "run --calendar-write-auth again",
            )
        try:
            credentials.refresh(Request())
        except Exception:
            raise GoogleAuthError(
                BriefingStatus.ERROR,
                "the calendar write token could not be refreshed; "
                "run --calendar-write-auth again",
            ) from None
        _save_token(credentials, token_file)

    return credentials


def run_write_authorization(
    client_secret_path: str,
    token_path: str,
    *,
    scopes: Iterable[str] = DEFAULT_WRITE_SCOPES,
):
    """Run the installed-app OAuth flow and persist the write token.

    Interactive by design: this is only reached from the explicit
    ``--calendar-write-auth`` command, never from a normal run or on startup.
    """
    _require_libraries()
    secret_file = _resolve_path(client_secret_path, _CLIENT_SECRET_FILENAME)
    token_file = _resolve_path(token_path, _TOKEN_WRITE_FILENAME)

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
            "the calendar write token could not be stored",
        ) from None


def credentials_path_exists(token_path: str = "") -> bool:
    """True when a write token file exists (used only for status messages)."""
    try:
        return _resolve_path(token_path, _TOKEN_WRITE_FILENAME).is_file()
    except GoogleAuthError:
        return False
