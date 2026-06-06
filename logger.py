"""
logger.py - Structured logging with rotating file handlers.
"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from typing import Optional


LOG_DIR = os.environ.get("LOG_DIR", "logs")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
os.makedirs(LOG_DIR, exist_ok=True)

_FMT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE = "%Y-%m-%d %H:%M:%S"

_formatter = logging.Formatter(_FMT, datefmt=_DATE)


def _file_handler(filename: str, level: int = logging.DEBUG) -> RotatingFileHandler:
    path = os.path.join(LOG_DIR, filename)
    h = RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    h.setFormatter(_formatter)
    h.setLevel(level)
    return h


def _console_handler() -> logging.StreamHandler:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(_formatter)
    h.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    return h


def get_logger(name: str) -> logging.Logger:
    """Return a logger with file + console handlers, idempotent."""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)
    logger.addHandler(_console_handler())
    logger.addHandler(_file_handler("app.log"))
    logger.propagate = False
    return logger


# ── Specialised loggers ───────────────────────────────────────────────────────

def _special(name: str, filename: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    logger.addHandler(_console_handler())
    logger.addHandler(_file_handler(filename))
    logger.propagate = False
    return logger


payment_logger  = _special("payment",      "payment.log")
ai_logger       = _special("ai_engine",    "ai.log")
voice_logger    = _special("voice_engine", "voice.log")
db_logger       = _special("database",     "database.log")
admin_logger    = _special("admin",        "admin.log")
bot_logger      = _special("bot",          "bot.log")
