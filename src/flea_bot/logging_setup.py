"""Structured logging via loguru.

Three sinks: coloured console, rotating human-readable file, and an optional
newline-delimited JSON file for post-run analysis.

Every bot action should go through :func:`log_action` so that dry-run and live
runs produce directly comparable logs — the only difference between them is the
``dry_run`` field.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from loguru import logger

from flea_bot.config import Config

_CONSOLE_FORMAT = (
    "<green>{time:HH:mm:ss.SSS}</green> "
    "<level>{level: <8}</level> "
    "<cyan>{extra[component]: <12}</cyan> "
    "<level>{message}</level>"
)

_FILE_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
    "{extra[component]: <12} | {name}:{function}:{line} | {message}"
)

_configured = False


def setup_logging(config: Config, *, force: bool = False) -> None:
    """Install the log sinks. Idempotent unless ``force`` is set."""
    global _configured
    if _configured and not force:
        return

    logger.remove()
    logger.configure(extra={"component": "-"})

    logger.add(
        sys.stderr,
        level=config.logging.level,
        format=_CONSOLE_FORMAT,
        colorize=True,
        backtrace=False,
        diagnose=False,
    )

    log_file: Path = config.log_dir / config.logging.file
    logger.add(
        log_file,
        level=config.logging.level,
        format=_FILE_FORMAT,
        rotation=config.logging.rotation,
        retention=config.logging.retention,
        encoding="utf-8",
        enqueue=True,
    )

    if config.logging.json_sink:
        logger.add(
            config.log_dir / (Path(config.logging.file).stem + ".jsonl"),
            level=config.logging.level,
            serialize=True,
            rotation=config.logging.rotation,
            retention=config.logging.retention,
            encoding="utf-8",
            enqueue=True,
        )

    _configured = True
    logger.bind(component="logging").debug(
        "Logging initialised: level={} file={}", config.logging.level, log_file
    )


def get_logger(component: str) -> Any:
    """Return a logger tagged with a component name (``vision``, ``input``...)."""
    return logger.bind(component=component)


def log_action(
    component: str,
    action: str,
    *,
    dry_run: bool,
    **fields: Any,
) -> None:
    """Log a single bot action in a uniform, machine-parseable shape.

    In dry-run the message is prefixed ``[DRY]`` and no input is dispatched by
    the caller; the structured payload is identical either way.
    """
    prefix = "[DRY] would " if dry_run else ""
    detail = " ".join(f"{k}={v!r}" for k, v in fields.items())
    bound = logger.bind(component=component, action=action, dry_run=dry_run, **fields)
    bound.info("{}{}{}", prefix, action, f" {detail}" if detail else "")
