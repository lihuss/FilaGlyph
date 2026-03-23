from __future__ import annotations

import subprocess
import threading

from pathlib import Path


def truncate_output(text: str, limit: int = 12000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... [truncated]"


def terminate_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def run_subprocess_with_failfast(
    command: list[str],
    cwd: Path,
    stop_event: threading.Event,
    process_registry: list[subprocess.Popen],
    registry_lock: threading.Lock,
) -> str:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    with registry_lock:
        process_registry.append(process)

    try:
        while True:
            if stop_event.is_set():
                terminate_process(process)
                raise RuntimeError("Cancelled due to another worker failure.")

            try:
                stdout, _ = process.communicate(timeout=0.2)
                if process.returncode != 0:
                    stop_event.set()
                    raise subprocess.CalledProcessError(process.returncode, command, output=stdout)
                return stdout
            except subprocess.TimeoutExpired:
                continue
    finally:
        with registry_lock:
            if process in process_registry:
                process_registry.remove(process)


def terminate_all_processes(process_registry: list[subprocess.Popen], registry_lock: threading.Lock) -> None:
    with registry_lock:
        running = list(process_registry)

    for process in running:
        try:
            terminate_process(process)
        except Exception:
            pass
