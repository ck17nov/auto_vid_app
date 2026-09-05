"""Structured, secret-safe logging.  Format: [TAG] message  key=value"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

_SECRET_KEYS = re.compile(
    r"(api[_-]?key|token|secret|password|client[_-]?secret|refresh[_-]?token|authorization)",
    re.I,
)
_LONG_KEYISH = re.compile(r"\b(?:AIza|gsk_|sk-|ya29\.)[A-Za-z0-9_\-\.]{8,}")

_HANDLER_READY = False
_JSONL_PATH: Path | None = None


def redact(value: Any) -> Any:
    """Remove anything that looks like a credential from log output."""
    if isinstance(value, dict):
        return {
            k: ("***REDACTED***" if _SECRET_KEYS.search(str(k)) else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return _LONG_KEYISH.sub("***REDACTED***", value)
    return value


class _Fmt(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        tag = getattr(record, "tag", record.name.split(".")[-1].upper())
        ts = time.strftime("%H:%M:%S", time.localtime(record.created))
        extra = getattr(record, "kv", None)
        kv = ""
        if extra:
            kv = "  " + " ".join(f"{k}={v}" for k, v in redact(extra).items())
        lvl = "" if record.levelno < logging.WARNING else f"{record.levelname} "
        return f"{ts} {lvl}[{tag}] {redact(record.getMessage())}{kv}"


def setup_logging(level: str = "INFO", jsonl: Path | None = None) -> None:
    global _HANDLER_READY, _JSONL_PATH
    _JSONL_PATH = jsonl
    if _HANDLER_READY:
        return
    # Force UTF-8 on the log stream.
    #
    # Real YouTube titles, LLM output and stock-photo credits are full of
    # non-ASCII - curly quotes, emoji, accented names. On a cp1252 Windows
    # console, logging one raises UnicodeEncodeError INSIDE the handler, and
    # Python prints a "--- Logging error ---" traceback to stderr while
    # dropping the line. It is not fatal, but it is noise that hides real
    # errors, and a log that cannot print its own content is not much of a log.
    # errors="replace" is deliberate: a mangled character beats a lost line.
    stream = sys.stdout
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError, OSError):
        pass
    h = logging.StreamHandler(stream)
    h.setFormatter(_Fmt())
    root = logging.getLogger("autotube")
    root.handlers.clear()
    root.addHandler(h)
    root.setLevel(os.environ.get("AUTOTUBE_LOG_LEVEL", level).upper())
    root.propagate = False
    _HANDLER_READY = True


def get_logger(tag: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(f"autotube.{tag.lower()}")


def log_event(tag: str, message: str, level: int = logging.INFO, **kv: Any) -> None:
    """Emit `[TAG] message key=value` and optionally mirror to a JSONL file."""
    logger = get_logger(tag)
    logger.log(level, message, extra={"tag": tag.upper(), "kv": kv or None})
    if _JSONL_PATH is not None:
        try:
            _JSONL_PATH.parent.mkdir(parents=True, exist_ok=True)
            with _JSONL_PATH.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "ts": time.time(), "tag": tag.upper(),
                    "level": logging.getLevelName(level),
                    "msg": str(redact(message)), "data": redact(kv),
                }) + "\n")
        except OSError:
            pass
