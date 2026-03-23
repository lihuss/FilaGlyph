from __future__ import annotations

import os
from datetime import datetime
import threading

from pathlib import Path

from .config import LOG_DIR

_LOG_WRITE_LOCK = threading.Lock()


def create_task_log_file() -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = LOG_DIR / f"log_{stamp}.log"
    suffix = 1
    while candidate.exists():
        candidate = LOG_DIR / f"log_{stamp}_{suffix:02d}.log"
        suffix += 1
    candidate.touch()
    return candidate


def append_runtime_log(log_file: Path, message: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    payload = f"[{timestamp}] [pid={os.getpid()}] {message}"
    with _LOG_WRITE_LOCK:
        with log_file.open("a", encoding="utf-8") as handle:
            handle.write(payload.rstrip() + "\n")


def log_status(message: str, log_file: Path | None = None) -> None:
    print(message)
    if log_file is not None:
        append_runtime_log(log_file, message)
