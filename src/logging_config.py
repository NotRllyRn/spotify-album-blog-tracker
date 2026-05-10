"""
Application logging setup.
"""

import logging
import os
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Optional, Union


LOG_FILE_NAME = "album-tracker.log"
_ALBUM_TRACKER_HANDLER_ATTR = "_album_tracker_handler"
_LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


def _resolve_log_level(level: Optional[Union[str, int]]) -> int:
    if isinstance(level, int):
        return level

    level_name = (level or os.getenv("LOG_LEVEL") or "INFO").upper()
    resolved_level = logging.getLevelName(level_name)
    if isinstance(resolved_level, int):
        return resolved_level

    return logging.INFO


def configure_logging(project_root: Path, level: Optional[Union[str, int]] = None) -> Path:
    """Configure console and rotating file logging for the whole application."""
    log_level = _resolve_log_level(level)
    logs_dir = Path(project_root) / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / LOG_FILE_NAME

    formatter = logging.Formatter(_LOG_FORMAT)
    root_logger = logging.getLogger()

    for handler in list(root_logger.handlers):
        if getattr(handler, _ALBUM_TRACKER_HANDLER_ATTR, False):
            root_logger.removeHandler(handler)
            handler.close()

    file_handler = TimedRotatingFileHandler(
        log_file,
        when="midnight",
        backupCount=14,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(log_level)
    setattr(file_handler, _ALBUM_TRACKER_HANDLER_ATTR, True)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(log_level)
    setattr(console_handler, _ALBUM_TRACKER_HANDLER_ATTR, True)

    root_logger.setLevel(log_level)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    return log_file
