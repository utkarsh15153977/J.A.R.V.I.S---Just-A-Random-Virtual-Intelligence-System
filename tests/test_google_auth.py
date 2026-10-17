"""Tests for the Google OAuth helper used by the daily briefing.

The optional Google libraries must never be needed: these tests simulate
their presence and absence deterministically (tiny fakes installed in
``sys.modules``, or a ``None`` entry that makes the lazy ``import`` fail), so
the suite passes whether or not the real packages are installed, and asserts
the categorised, credential-free failure behaviour. No network call is ever
made.
"""

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from core.briefing import BriefingStatus
from core.google_auth import (
    DEFAULT_SCOPES,
    GoogleAuthError,
    credentials_path_exists,
    default_google_dir,
    google_libraries_available,
    load_credentials,
    run_authorization,
)


class FakeCredentials:
    """A controllable stand-in for ``google.oauth2.credentials.Credentials``."""

    def __init__(self, *, valid=True, expired=False, refresh_token="r"):
        self.valid = valid
        self.expired = expired
        self.refresh_token = refresh_token
        self.refreshed = False
        self.saved = False

    @classmethod
    def from_authorized_user_file(cls, path, scopes):
        return installed_from_file

    def refresh(self, request):
        self.refreshed = True
        if refresh_error is not None:
            raise refresh_error

    def to_json(self):
        self.saved = True
        return "{}"


class FakeRequest:
    def __init__(self):
        pass


# Module-level state the fakes read (tests set these).
installed_from_file = None
refresh_error = None


def _install_fake_google():
    """Install a minimal fake ``google`` package tree in ``sys.modules``."""
    modules = {}

    def module(name):
        mod = types.ModuleType(name)
        modules[name] = mod
        return mod

    google = module("google")
    auth = module("google.auth")
    transport = module("google.auth.transport")
    requests = module("google.auth.transport.requests")
    requests.Request = FakeRequest
    oauth2 = module("google.oauth2")
    credentials = module("google.oauth2.credentials")
    credentials.Credentials = FakeCredentials
    flow = module("google_auth_oauthlib.flow")
    flow.InstalledAppFlow = FakeFlow
    module("google_auth_oauthlib")
    module("googleapiclient")
    module("googleapiclient.discovery")

    google.auth = auth
    google.oauth2 = oauth2
    auth.transport = transport
    transport.requests = requests
    oauth2.credentials = credentials

    return modules


class FakeFlow:
    @classmethod
    def from_client_secrets_file(cls, path, scopes):
        return cls()

    def run_local_server(self, port=0):
        return FakeCredentials()


class GoogleAuthTestCase(unittest.TestCase):
    def setUp(self):
        global installed_from_file, refresh_error
        installed_from_file = FakeCredentials()
        refresh_error = None
        self._modules = _install_fake_google()
        self._patch = mock.patch.dict(sys.modules, self._modules)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self._libs = mock.patch(
            "core.google_auth.google_libraries_available", return_value=True
        )
        self._libs.start()
        self.addCleanup(self._libs.stop)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def _write_token(self):
        token = self.dir / "token.json"
        token.write_text("{}", encoding="utf-8")
        return token


def _hidden_google_modules():
    """Make every optional Google import fail, deterministically.

    A ``None`` entry in ``sys.modules`` makes ``import`` raise, so the
    availability check can be exercised without depending on whether the
    libraries happen to be installed on the host.
    """
    return dict.fromkeys(
        (
            "google",
            "google.auth",
            "google.oauth2",
            "google.oauth2.credentials",
            "google_auth_oauthlib",
            "google_auth_oauthlib.flow",
            "googleapiclient",
            "googleapiclient.discovery",
        )
    )


class TestLibrariesMissing(unittest.TestCase):
    def test_missing_libraries_report_unavailable(self):
        with mock.patch(
            "core.google_auth.google_libraries_available", return_value=False
        ):
            with self.assertRaises(GoogleAuthError) as ctx:
                load_credentials()
        self.assertIs(ctx.exception.status, BriefingStatus.UNAVAILABLE)
        self.assertIn("not installed", ctx.exception.message)


class TestLibrariesAvailability(unittest.TestCase):
    """The availability check itself, independent of the host environment."""

    def test_returns_false_when_libraries_missing(self):
        with mock.patch.dict(sys.modules, _hidden_google_modules()):
            self.assertFalse(google_libraries_available())

    def test_returns_true_when_libraries_present(self):
        # The fake package tree satisfies the lazy imports, so the real check
        # reports availability without any real dependency.
        with mock.patch.dict(sys.modules, _install_fake_google()):
            self.assertTrue(google_libraries_available())


class TestLoadCredentials(GoogleAuthTestCase):
    def _token(self):
        return str(self.dir / "token.json")

    def test_missing_token_without_interactive_is_unavailable(self):
        with self.assertRaises(GoogleAuthError) as ctx:
            load_credentials(token_path=self._token())
        self.assertIs(ctx.exception.status, BriefingStatus.UNAVAILABLE)
        self.assertIn("--briefing-auth", ctx.exception.message)

    def test_existing_valid_token_is_returned(self):
        self._write_token()
        credentials = load_credentials(token_path=self._token())
        self.assertIs(credentials, installed_from_file)
        self.assertFalse(credentials.refreshed)

    def test_expired_token_is_refreshed_and_saved(self):
        self._write_token()
        installed_from_file.expired = True
        installed_from_file.valid = False
        credentials = load_credentials(token_path=self._token())
        self.assertTrue(credentials.refreshed)
        self.assertTrue(credentials.saved)

    def test_expired_without_refresh_token_is_unavailable(self):
        self._write_token()
        installed_from_file.refresh_token = None
        installed_from_file.expired = True
        installed_from_file.valid = False
        with self.assertRaises(GoogleAuthError) as ctx:
            load_credentials(token_path=self._token())
        self.assertIs(ctx.exception.status, BriefingStatus.UNAVAILABLE)

    def test_refresh_failure_is_error(self):
        global refresh_error
        self._write_token()
        installed_from_file.expired = True
        installed_from_file.valid = False
        refresh_error = RuntimeError("boom")
        self.addCleanup(lambda: _clear_refresh_error())
        with self.assertRaises(GoogleAuthError) as ctx:
            load_credentials(token_path=self._token())
        self.assertIs(ctx.exception.status, BriefingStatus.ERROR)

    def test_unreadable_token_is_error(self):
        self._write_token()
        with mock.patch.object(
            FakeCredentials,
            "from_authorized_user_file",
            side_effect=ValueError("corrupt"),
        ):
            with self.assertRaises(GoogleAuthError) as ctx:
                load_credentials(token_path=self._token())
        self.assertIs(ctx.exception.status, BriefingStatus.ERROR)

    def test_control_characters_in_path_are_rejected(self):
        with self.assertRaises(GoogleAuthError) as ctx:
            load_credentials(token_path="bad\ntoken.json")
        self.assertIs(ctx.exception.status, BriefingStatus.UNAVAILABLE)

    def test_missing_secret_on_interactive_auth_is_unavailable(self):
        with self.assertRaises(GoogleAuthError) as ctx:
            load_credentials(
                client_secret_path=str(self.dir / "missing.json"),
                token_path=self._token(),
                allow_interactive=True,
            )
        self.assertIs(ctx.exception.status, BriefingStatus.UNAVAILABLE)
        self.assertIn("client-secret", ctx.exception.message)


class TestRunAuthorization(GoogleAuthTestCase):
    def test_missing_client_secret_is_unavailable(self):
        with self.assertRaises(GoogleAuthError) as ctx:
            run_authorization(
                client_secret_path=str(self.dir / "missing.json"),
                token_path=str(self.dir / "token.json"),
            )
        self.assertIs(ctx.exception.status, BriefingStatus.UNAVAILABLE)

    def test_successful_authorization_stores_token(self):
        secret = self.dir / "client_secret.json"
        secret.write_text("{}", encoding="utf-8")
        token = self.dir / "token.json"
        run_authorization(
            client_secret_path=str(secret), token_path=str(token)
        )
        self.assertTrue(token.is_file())

    def test_default_scopes_are_read_only(self):
        for scope in DEFAULT_SCOPES:
            self.assertIn("readonly", scope)
        self.assertNotIn("https://www.googleapis.com/auth/calendar", DEFAULT_SCOPES)


class TestHelpers(GoogleAuthTestCase):
    def test_default_google_dir_is_a_google_subdirectory(self):
        path = Path(default_google_dir())
        self.assertEqual(path.name, "google")

    def test_credentials_path_exists_true_and_false(self):
        token = self._write_token()
        self.assertTrue(credentials_path_exists(str(token)))
        self.assertFalse(
            credentials_path_exists(str(self.dir / "absent.json"))
        )


def _clear_refresh_error():
    global refresh_error
    refresh_error = None


if __name__ == "__main__":
    unittest.main()
