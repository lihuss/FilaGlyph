from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sys
import traceback


PROJECT_ROOT = Path(__file__).resolve().parents[3]
LOG_DIR = PROJECT_ROOT / "logs"
RUNTIME_LOG_FILE = LOG_DIR / "runtime_errors.log"


def log_runtime_error(script_name: str, exc: BaseException | None = None, context: str = "") -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if exc is None:
        trace = traceback.format_exc()
        if trace.strip() in {"", "NoneType: None"}:
            trace = "No traceback available."
    else:
        trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))

    lines = [
        f"[{timestamp}] script={script_name}",
        f"cwd={Path.cwd()}",
        f"python={sys.executable}",
    ]
    if context:
        lines.append(f"context={context}")

    lines.extend([
        "traceback:",
        trace.rstrip(),
        "-" * 100,
        "",
    ])

    with RUNTIME_LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines))

    return RUNTIME_LOG_FILE
