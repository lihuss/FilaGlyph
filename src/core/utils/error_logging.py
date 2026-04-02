from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import sys
import traceback


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _default_runtime_log_file() -> Path:
    run_dir_value = os.getenv("FILAGLYPH_RUN_DIR", "").strip()
    if run_dir_value:
        run_dir = Path(run_dir_value)
        return run_dir / "runtime_errors.log"
    return PROJECT_ROOT / "outputs" / "agent_runs" / "runtime_errors.log"


def log_runtime_error(script_name: str, exc: BaseException | None = None, context: str = "") -> Path:
    runtime_log_file = _default_runtime_log_file()
    runtime_log_file.parent.mkdir(parents=True, exist_ok=True)

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

    with runtime_log_file.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines))

    return runtime_log_file
