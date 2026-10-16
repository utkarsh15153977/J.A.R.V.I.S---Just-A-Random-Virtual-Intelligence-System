"""Built-in, read-only tools for Jarvis.

These are the first real tools: deterministic, local, read-only and
side-effect free. Each one is classified ``PermissionLevel.SAFE`` and
still runs through ``core.executor.ToolExecutor`` like any other tool.

There is deliberately no shell, browser, network, process or
input-simulation access here. None of these tools read the environment,
the user name, the machine name or any file's contents. The disk tool
reports only aggregate filesystem capacity (through the standard
library); the battery tool reports only the power state. All are
read-only and ``SAFE``.

Each handler returns a small structured mapping that includes a
developer-authored ``text`` entry. The application shows that text; the
other entries exist for future programmatic use and are never spoken or
written to logs.
"""

from __future__ import annotations

import ctypes
import os
import platform
import shutil
import sys
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, Optional

from core.permissions import PermissionLevel
from core.tools import ToolRegistry, ToolSpec, validate_mapping

TOOL_GET_CURRENT_TIME = "get_current_time"
TOOL_GET_CURRENT_DATE = "get_current_date"
TOOL_GET_CURRENT_DATETIME = "get_current_datetime"
TOOL_GET_SYSTEM_STATUS = "get_system_status"
TOOL_GET_DISK_SPACE = "get_disk_space"
TOOL_GET_BATTERY_STATUS = "get_battery_status"

# The complete, closed set of tools this module knows how to register.
BUILTIN_TOOL_NAMES = (
    TOOL_GET_CURRENT_TIME,
    TOOL_GET_CURRENT_DATE,
    TOOL_GET_CURRENT_DATETIME,
    TOOL_GET_SYSTEM_STATUS,
    TOOL_GET_DISK_SPACE,
    TOOL_GET_BATTERY_STATUS,
)

Clock = Callable[[], datetime]
# Returns (total_bytes, used_bytes, free_bytes) or None when unavailable.
DiskReader = Callable[[], Optional[tuple]]
# Returns {"percent": int | None, "plugged": bool} or None when absent.
BatteryReader = Callable[[], Optional[Mapping[str, Any]]]

_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)

_WEEKDAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)


def _default_clock() -> datetime:
    return datetime.now()


def _reject_arguments(raw: Mapping[str, Any]) -> dict:
    """Validation for zero-argument tools: reject every supplied key."""
    return validate_mapping(raw, required={}, optional={})


def _format_time(moment: datetime) -> str:
    hour = moment.hour % 12 or 12
    period = "AM" if moment.hour < 12 else "PM"
    return f"{hour}:{moment.minute:02d} {period}"


def _format_date(moment: datetime) -> str:
    weekday = _WEEKDAYS[moment.weekday()]
    month = _MONTHS[moment.month - 1]
    return f"{weekday}, {month} {moment.day}, {moment.year}"


def make_get_current_time(clock: Clock) -> Callable[[], dict]:
    """Build the ``get_current_time`` handler around an injected clock."""

    def handler() -> dict:
        moment = clock()
        text = _format_time(moment)
        return {"text": f"It's {text}.", "time": text}

    return handler


def make_get_current_date(clock: Clock) -> Callable[[], dict]:
    """Build the ``get_current_date`` handler around an injected clock."""

    def handler() -> dict:
        moment = clock()
        return {
            "text": f"Today is {_format_date(moment)}.",
            "date": moment.date().isoformat(),
        }

    return handler


def make_get_current_datetime(clock: Clock) -> Callable[[], dict]:
    """Build the combined ``get_current_datetime`` handler.

    A spoken command can reasonably ask for the date *and* the time at
    once ("what is the date and time?"). Rather than inventing a
    multi-tool shortcut, this is a single, fixed, read-only tool that
    reuses the same ``_format_date`` / ``_format_time`` helpers -- one
    SAFE read through the clock, exactly like the others, so nothing
    bypasses ``ToolExecutor``.
    """

    def handler() -> dict:
        moment = clock()
        date_text = _format_date(moment)
        time_text = _format_time(moment)
        return {
            "text": f"It's {time_text} on {date_text}.",
            "date": moment.date().isoformat(),
            "time": time_text,
        }

    return handler


def get_system_status() -> dict:
    """Report only the OS name and Python version (no private details)."""
    os_name = platform.system() or "unknown"
    version = ".".join(str(part) for part in sys.version_info[:3])
    return {
        "text": f"System status: {os_name}, Python {version}.",
        "os": os_name,
        "python": version,
    }


def _disk_target() -> str:
    """The fixed, safe target whose filesystem capacity is reported."""
    home = os.path.expanduser("~")
    return home or os.getcwd()


def _read_disk_usage() -> Optional[tuple]:
    """Aggregate capacity of the home filesystem, or None if unavailable."""
    try:
        usage = shutil.disk_usage(_disk_target())
    except OSError:
        return None
    return (usage.total, usage.used, usage.free)


def _format_gb(num_bytes: int) -> str:
    return f"{num_bytes / (1024 ** 3):.1f} GB"


def make_get_disk_space(reader: DiskReader) -> Callable[[], dict]:
    """Build the ``get_disk_space`` handler around an injected reader."""

    def handler() -> dict:
        usage = reader()
        if usage is None:
            return {"text": "Disk information is not available."}
        total, used, free = usage
        return {
            "text": (
                f"Disk space: {_format_gb(free)} free of "
                f"{_format_gb(total)}."
            ),
            "total_bytes": total,
            "used_bytes": used,
            "free_bytes": free,
        }

    return handler


class _SystemPowerStatus(ctypes.Structure):
    """Win32 ``SYSTEM_POWER_STATUS`` (the only Windows API used here)."""

    _fields_ = [
        ("ACLineStatus", ctypes.c_byte),
        ("BatteryFlag", ctypes.c_byte),
        ("BatteryLifePercent", ctypes.c_byte),
        ("SystemStatusFlag", ctypes.c_byte),
        ("BatteryLifeTime", ctypes.c_ulong),
        ("BatteryFullLifeTime", ctypes.c_ulong),
    ]


def _is_windows() -> bool:
    return sys.platform == "win32"


def _read_battery_windows() -> Optional[dict]:
    """Read the power state on Windows, or None when there is no battery.

    Uses only the standard library (``ctypes`` -> ``GetSystemPowerStatus``),
    so no package is installed. Returns ``None`` on any non-Windows
    platform, when there is no system battery, or on any failure.
    """
    if not _is_windows():
        return None
    try:
        status = _SystemPowerStatus()
        if not ctypes.windll.kernel32.GetSystemPowerStatus(
            ctypes.byref(status)
        ):
            return None
    except Exception:
        return None

    # BatteryFlag 128 means "no system battery"; 255 means unknown.
    if status.BatteryFlag == 128:
        return None
    percent: Optional[int] = status.BatteryLifePercent
    if percent > 100:  # 255 == unknown
        percent = None
    return {"percent": percent, "plugged": status.ACLineStatus == 1}


def _battery_state(plugged: bool) -> str:
    return "plugged in" if plugged else "on battery"


def make_get_battery_status(reader: BatteryReader) -> Callable[[], dict]:
    """Build the ``get_battery_status`` handler around an injected reader."""

    def handler() -> dict:
        reading = reader()
        if not reading:
            return {
                "text": "Battery status is not available on this system."
            }
        percent = reading.get("percent")
        plugged = bool(reading.get("plugged"))
        state = _battery_state(plugged)
        if percent is None:
            text = f"Battery level is unknown ({state})."
        else:
            text = f"Battery is at {percent}% ({state})."
        return {"text": text, "percent": percent, "plugged": plugged}

    return handler


def build_builtin_specs(
    clock: Optional[Clock] = None,
    *,
    disk_reader: Optional[DiskReader] = None,
    battery_reader: Optional[BatteryReader] = None,
) -> tuple[ToolSpec, ...]:
    """Return fresh specs for every built-in tool.

    A clock can be injected so tests get deterministic time and date
    output; disk and battery readers can be injected so tests never touch
    real hardware. All tools are SAFE and reject any arguments.
    """
    active_clock = clock or _default_clock
    active_disk = disk_reader or _read_disk_usage
    active_battery = battery_reader or _read_battery_windows
    return (
        ToolSpec(
            name=TOOL_GET_CURRENT_TIME,
            description="report the current local time",
            permission=PermissionLevel.SAFE,
            validate=_reject_arguments,
            handler=make_get_current_time(active_clock),
        ),
        ToolSpec(
            name=TOOL_GET_CURRENT_DATE,
            description="report the current local date",
            permission=PermissionLevel.SAFE,
            validate=_reject_arguments,
            handler=make_get_current_date(active_clock),
        ),
        ToolSpec(
            name=TOOL_GET_CURRENT_DATETIME,
            description="report the current local date and time",
            permission=PermissionLevel.SAFE,
            validate=_reject_arguments,
            handler=make_get_current_datetime(active_clock),
        ),
        ToolSpec(
            name=TOOL_GET_SYSTEM_STATUS,
            description="report the operating system and Python version",
            permission=PermissionLevel.SAFE,
            validate=_reject_arguments,
            handler=get_system_status,
        ),
        ToolSpec(
            name=TOOL_GET_DISK_SPACE,
            description="report free and total disk space",
            permission=PermissionLevel.SAFE,
            validate=_reject_arguments,
            handler=make_get_disk_space(active_disk),
        ),
        ToolSpec(
            name=TOOL_GET_BATTERY_STATUS,
            description="report the battery level and power state",
            permission=PermissionLevel.SAFE,
            validate=_reject_arguments,
            handler=make_get_battery_status(active_battery),
        ),
    )


def register_builtin_tools(
    registry: ToolRegistry,
    *,
    clock: Optional[Clock] = None,
    disk_reader: Optional[DiskReader] = None,
    battery_reader: Optional[BatteryReader] = None,
) -> ToolRegistry:
    """Register every built-in tool into ``registry`` and return it."""
    for spec in build_builtin_specs(
        clock, disk_reader=disk_reader, battery_reader=battery_reader
    ):
        registry.register(spec)
    return registry


def build_builtin_registry(
    clock: Optional[Clock] = None,
    *,
    disk_reader: Optional[DiskReader] = None,
    battery_reader: Optional[BatteryReader] = None,
) -> ToolRegistry:
    """Return a new :class:`ToolRegistry` holding the built-in tools."""
    return register_builtin_tools(
        ToolRegistry(),
        clock=clock,
        disk_reader=disk_reader,
        battery_reader=battery_reader,
    )
