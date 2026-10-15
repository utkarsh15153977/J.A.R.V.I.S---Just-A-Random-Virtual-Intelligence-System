"""Shared logging setup for every Jarvis entry point.

Console output is kept short and human readable; the log file keeps
full timestamps for debugging.
"""

import logging
from pathlib import Path

_CONSOLE_FORMAT = "%(levelname)-7s %(name)s: %(message)s"
_FILE_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def setup_logging(
    log_dir: str = "logs",
    level: str = "INFO",
    name: str = "jarvis",
) -> logging.Logger:
    """Configure console + file logging for the "jarvis" logger tree.

    Idempotent: calling it more than once returns the same logger
    without adding duplicate handlers.
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)

    if logger.handlers:
        return logger

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(_CONSOLE_FORMAT))
    logger.addHandler(console)

    try:
        directory = Path(log_dir)
        directory.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(
            directory / "jarvis.log", encoding="utf-8"
        )
        file_handler.setFormatter(logging.Formatter(_FILE_FORMAT))
        logger.addHandler(file_handler)
    except OSError:
        logger.warning("Could not open log file in '%s'; file logging disabled.", log_dir)

    return logger
