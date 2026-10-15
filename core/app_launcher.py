"""Allowlisted, confirmed launching of a small set of local applications.

This is the first tool that performs a real side effect, so it is built
to be as narrow as possible:

* Only a tiny, **trusted, developer-authored catalog** of Windows
  applications is known (Notepad and Calculator). Executable targets are
  resolved from an absolute path under the real system directory only.
  A path, command or executable target is **never** taken from speech,
  from arbitrary user configuration or from tool arguments.
* The process argument vector is built **exclusively** from the catalog
  entry: ``[executable, *target.arguments]``. There is no shell, no
  arbitrary launch arguments, no browser automation and no way to run an
  unrestricted process. ``subprocess.Popen`` is called with a list and
  ``shell=False``.
* Launching is **disabled by default** and, even when enabled, only the
  identifiers explicitly selected in configuration can be launched.
* Every launch passes through ``core.executor.ToolExecutor`` at
  ``PermissionLevel.CONFIRM``, so it needs a fresh, single-use Batch 9
  confirmation ticket bound to this exact tool and validated identifier.
  The approval prompt names the exact application using trusted catalog
  display names.
* Launching is **fire-and-forget**: the child process is not waited for,
  tracked or terminated.

Nothing here reads speech. The module exposes a small, inert data model
plus an injectable :class:`ProcessLauncher`, so tests use a fake launcher
and never start a real application.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Optional, Protocol

from core.permissions import PermissionLevel
from core.router import IntentRule
from core.tools import ToolInputError, ToolRegistry, ToolSpec, validate_mapping

TOOL_LAUNCH_APPLICATION = "launch_application"

# The only identifiers that may ever appear in configuration: a short,
# lowercase ASCII token. This is intentionally restrictive so that no
# path, command line or shell metacharacter can pass as an identifier.
_IDENTIFIER_PATTERN = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")

# Trusted catalog: identifier -> (display name, executable file name).
# The executable name is a bare file name that is joined to the real
# system directory; no value here is ever attacker- or user-supplied.
_SYSTEM32_TARGETS: Mapping[str, tuple[str, str]] = {
    "notepad": ("Notepad", "notepad.exe"),
    "calculator": ("Calculator", "calc.exe"),
}


class AppLaunchConfigError(ValueError):
    """Raised when a configured application identifier is invalid.

    An unknown or malformed identifier is a configuration error and must
    never result in a launch. ``app.py`` turns this into the standard
    configuration-error exit path.
    """


@dataclass(frozen=True)
class AppTarget:
    """One trusted, resolvable application from the catalog.

    ``executable`` must be an absolute path (the catalog always builds
    it from the system directory), and ``arguments`` are developer-authored
    and fixed — never taken from speech or user input.
    """

    identifier: str
    display_name: str
    executable: str
    arguments: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.identifier, str) or not (
            _IDENTIFIER_PATTERN.match(self.identifier)
        ):
            raise ValueError("identifier must be a short lowercase token")
        if not isinstance(self.display_name, str) or not (
            self.display_name.strip()
        ):
            raise ValueError("display_name must be a non-empty string")
        if not isinstance(self.executable, str) or not os.path.isabs(
            self.executable
        ):
            raise ValueError("executable must be an absolute path")
        if not isinstance(self.arguments, tuple) or not all(
            isinstance(item, str) for item in self.arguments
        ):
            raise TypeError("arguments must be a tuple of strings")


def default_catalog() -> dict[str, AppTarget]:
    """Return the trusted Windows catalog, resolved under the system dir.

    The catalog is empty on non-Windows platforms, so no identifier is
    ever selectable there. On Windows, each executable is an absolute
    path under ``%SystemRoot%\\System32`` built from a fixed file name —
    never from configuration or input.
    """
    if sys.platform != "win32":
        return {}
    root = os.environ.get("SYSTEMROOT") or r"C:\Windows"
    system32 = os.path.join(root, "System32")
    catalog: dict[str, AppTarget] = {}
    for identifier, (display_name, exe_name) in _SYSTEM32_TARGETS.items():
        executable = os.path.abspath(os.path.join(system32, exe_name))
        catalog[identifier] = AppTarget(
            identifier=identifier,
            display_name=display_name,
            executable=executable,
        )
    return catalog


class AppAllowlist:
    """The closed set of applications that may currently be launched.

    Built only from the trusted catalog; membership and resolution are
    pure lookups. An unidentified or absent target resolves to ``None``.
    """

    def __init__(self, targets: Mapping[str, AppTarget]) -> None:
        self._targets = dict(targets)

    def resolve(self, identifier: object) -> Optional[AppTarget]:
        if not isinstance(identifier, str):
            return None
        return self._targets.get(identifier)

    def identifiers(self) -> tuple[str, ...]:
        return tuple(sorted(self._targets))

    def display_names(self) -> tuple[str, ...]:
        return tuple(
            self._targets[name].display_name for name in self.identifiers()
        )

    def __contains__(self, identifier: object) -> bool:
        return self.resolve(identifier) is not None

    def __len__(self) -> int:
        return len(self._targets)


def build_allowlist(
    allowed: object,
    *,
    catalog: Optional[Mapping[str, AppTarget]] = None,
) -> AppAllowlist:
    """Resolve selected identifiers against the trusted catalog.

    Raises :class:`AppLaunchConfigError` for a malformed identifier or
    one that is not in the catalog. Only catalog entries are ever
    returned, so no user-supplied path can become a launch target.
    """
    source = catalog if catalog is not None else default_catalog()
    selected: dict[str, AppTarget] = {}
    for identifier in allowed:
        if not isinstance(identifier, str) or not _IDENTIFIER_PATTERN.match(
            identifier
        ):
            raise AppLaunchConfigError(
                f"invalid application identifier: {identifier!r}"
            )
        target = source.get(identifier)
        if target is None:
            raise AppLaunchConfigError(
                f"unknown application identifier: {identifier!r}"
            )
        selected[identifier] = target
    return AppAllowlist(selected)


class ProcessLauncher(Protocol):
    """Starts an allowlisted application. Injectable for tests."""

    def launch(self, target: AppTarget) -> None:
        ...


class SubprocessLauncher:
    """Fire-and-forget launch using a trusted argv and ``shell=False``.

    The list form plus ``shell=False`` means no shell parsing, no command
    injection and no metacharacter expansion. Standard streams are sent
    to ``DEVNULL``; the child is never waited for, tracked or terminated.
    """

    def launch(self, target: AppTarget) -> None:
        argv = [target.executable, *target.arguments]
        subprocess.Popen(
            argv,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )


def _make_validate_application(
    allowlist: AppAllowlist,
) -> Callable[[Mapping[str, Any]], dict]:
    def validate(raw: Mapping[str, Any]) -> dict:
        validated = validate_mapping(
            raw, required={"app": str}, optional={}
        )
        identifier = validated["app"]
        if not _IDENTIFIER_PATTERN.match(identifier):
            raise ToolInputError("invalid application identifier")
        if allowlist.resolve(identifier) is None:
            raise ToolInputError("unknown application")
        return validated

    return validate


def _make_describe_application(
    allowlist: AppAllowlist,
) -> Callable[[Mapping[str, Any]], str]:
    def describe(args: Mapping[str, Any]) -> str:
        target = allowlist.resolve(args.get("app"))
        if target is None:
            return "launch an allowlisted application"
        return f"launch {target.display_name}"

    return describe


def _make_launch_handler(
    allowlist: AppAllowlist, launcher: ProcessLauncher
) -> Callable[[str], dict]:
    def handler(app: str) -> dict:
        target = allowlist.resolve(app)
        if target is None:
            # Unreachable through the guarded executor (the validator
            # already checked membership), but fail closed if a handler
            # is ever reached another way.
            raise ValueError("application is not allowlisted")
        launcher.launch(target)
        return {"text": f"Opening {target.display_name}.", "app": app}

    return handler


def build_launch_spec(
    *,
    allowlist: AppAllowlist,
    launcher: ProcessLauncher,
) -> ToolSpec:
    """Build the ``launch_application`` spec at ``CONFIRM``.

    The tool takes exactly one validated, allowlisted ``app`` identifier;
    the executable and argv come only from the trusted catalog.
    """
    return ToolSpec(
        name=TOOL_LAUNCH_APPLICATION,
        description="launch an allowlisted application",
        permission=PermissionLevel.CONFIRM,
        validate=_make_validate_application(allowlist),
        handler=_make_launch_handler(allowlist, launcher),
        describe=_make_describe_application(allowlist),
    )


def register_launch_tool(
    registry: ToolRegistry,
    *,
    allowlist: AppAllowlist,
    launcher: ProcessLauncher,
) -> ToolRegistry:
    """Register the ``launch_application`` tool into ``registry``."""
    spec = build_launch_spec(allowlist=allowlist, launcher=launcher)
    registry.register(spec)
    return registry


# Fixed, developer-authored spoken phrases. The arguments are constant
# and never derived from the spoken text, so a transcript can select only
# one of these known identifiers — nothing else.
APP_LAUNCH_INTENTS: tuple[IntentRule, ...] = (
    IntentRule(
        tool_name=TOOL_LAUNCH_APPLICATION,
        phrases=frozenset(
            {
                "open notepad",
                "launch notepad",
                "start notepad",
                "open the notepad",
            }
        ),
        arguments={"app": "notepad"},
    ),
    IntentRule(
        tool_name=TOOL_LAUNCH_APPLICATION,
        phrases=frozenset(
            {
                "open calculator",
                "launch calculator",
                "start calculator",
                "open the calculator",
            }
        ),
        arguments={"app": "calculator"},
    ),
)
