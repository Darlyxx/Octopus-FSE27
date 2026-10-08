"""Optional diagnostic events, stored separately from task results."""

import json
import os
import threading
from datetime import datetime
from enum import Enum


_LOG_PATH = ""
_LOCK = threading.Lock()


def set_log_path(path: str):
    """Enable diagnostic logging to path, or disable it with an empty path."""
    global _LOG_PATH
    _LOG_PATH = path or ""


def get_log_path():
    return _LOG_PATH


def _json_default(value):
    if isinstance(value, Enum):
        return value.name
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def write_event(path, event, **payload):
    record = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "event": event,
        "payload": payload,
    }
    line = json.dumps(record, ensure_ascii=False, default=_json_default)
    with _LOCK:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def log_event(event: str, **payload):
    path = _LOG_PATH
    if path:
        write_event(path, event, **payload)
