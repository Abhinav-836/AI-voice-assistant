import logging
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


class _FallbackHandler(logging.StreamHandler):
    """Prints only while nobody else is handling logs.

    The LiveKit Agents CLI installs its own root handler (plain text in dev,
    JSON in production). Our records propagate to it anyway, so printing them
    here as well showed every line two or three times. Plain scripts such as
    frontend/serve.py have no root handler, so this one prints there.
    """

    def emit(self, record: logging.LogRecord) -> None:
        if logging.getLogger().handlers:
            return
        super().emit(record)


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = _FallbackHandler(sys.stdout)
        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        level_name = os.getenv("LOG_LEVEL", "INFO").upper()
        level = getattr(logging, level_name, logging.INFO)
        logger.setLevel(level)
    return logger