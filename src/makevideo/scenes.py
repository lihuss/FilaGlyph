from __future__ import annotations

import subprocess
import sys

from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from pathlib import Path
import threading

from .config import PROJECT_ROOT
from .logging import log_status
from .subprocess import run_subprocess_with_failfast, terminate_all_processes, truncate_output


def parse_csv_list(raw: str) -> list[str]:
    return [item.strip() for item in (raw or "").split(",") if item.strip()]


def resolve_scene_jobs(args) -> list[tuple[int, Path, str]]:
    scene_jobs: list[tuple[int, Path, str]] = []

    scene_files_raw = parse_csv_list(args.scene_files)
    if not scene_files_raw:
        raise ValueError("--scene-files is required and must contain at least one file path")

    scene_names = parse_csv_list(args.scene_names)
    if not scene_names:
        raise ValueError("--scene-names is required and must contain at least one scene class name")

    if len(scene_names) != len(scene_files_raw):
        raise ValueError(
            "--scene-files count must match --scene-names count. "
            f"Got scene-files={len(scene_files_raw)}, scene-names={len(scene_names)}"
        )

    scene_files: list[Path] = []
    for raw in scene_files_raw:
        scene_path = resolve_path(raw)
        if not scene_path.exists() or not scene_path.is_file():
            raise FileNotFoundError(f"Scene file not found: {scene_path}")
        scene_files.append(scene_path)

    for idx, (scene_path, chosen) in enumerate(zip(scene_files, scene_names), start=1):
        scene_jobs.append((idx, scene_path, chosen))

    return scene_jobs


def resolve_path(raw_path: str) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path.resolve()
    return (PROJECT_ROOT / path).resolve()


def run_scene_child_script(
    scene_file: Path,
    scene_name: str,
    scene_index: int,
    args,
    run_dir: Path,
    scene_runner_script: Path,
    runtime_log_file: Path,
    stop_event: threading.Event,
    process_registry: list[subprocess.Popen],
    registry_lock: threading.Lock,
) -> tuple[Path, str]:
    command = [
        sys.executable,
        str(scene_runner_script),
        "--scene-file",
        str(scene_file),
        "--scene-name",
        scene_name,
        "--scene-index",
        str(scene_index),
        "--run-dir",
        str(run_dir),
        "--quality",
        args.quality,
        "--fps",
        str(args.fps),
        "--resolution",
        args.resolution,
        "--runtime-log-file",
        str(runtime_log_file),
    ]

    try:
        output = run_subprocess_with_failfast(command, PROJECT_ROOT, stop_event, process_registry, registry_lock)
    except subprocess.CalledProcessError as exc:
        command_text = " ".join(command)
        output_text = truncate_output(exc.output or "")
        raise RuntimeError(
            f"Scene child script failed: index={scene_index}, name={scene_name}\n"
            f"Command: {command_text}\n"
            f"ExitCode: {exc.returncode}\n"
            f"Output:\n{output_text}"
        ) from exc

    segment_path = run_dir / f"scene{scene_index}" / f"segment_{scene_index:03d}_{scene_name}.mp4"
    if not segment_path.exists() or not segment_path.is_file():
        raise FileNotFoundError(
            f"Scene child script reported success but segment not found: {segment_path}\n"
            f"Scene output:\n{truncate_output(output)}"
        )

    return segment_path, output


def render_scenes_via_child_scripts(
    scene_jobs: list[tuple[int, Path, str]],
    args,
    run_dir: Path,
    scene_runner_script: Path,
    runtime_log_file: Path,
) -> list[Path]:
    jobs = len(scene_jobs) if args.enable_multithread else 1
    jobs = max(1, min(jobs, len(scene_jobs)))

    indexed_paths: dict[int, Path] = {}
    stop_event = threading.Event()
    process_registry: list[subprocess.Popen] = []
    registry_lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=jobs) as executor:
        futures = {}
        for scene_index, scene_file, scene_name in scene_jobs:
            future = executor.submit(
                run_scene_child_script,
                scene_file,
                scene_name,
                scene_index,
                args,
                run_dir,
                scene_runner_script,
                runtime_log_file,
                stop_event,
                process_registry,
                registry_lock,
            )
            futures[future] = scene_index

        pending = set(futures)
        while pending:
            done, pending = wait(pending, return_when=FIRST_EXCEPTION)

            first_error: tuple[int, Exception] | None = None
            for future in done:
                idx = futures[future]
                try:
                    segment_path, scene_output = future.result()
                    indexed_paths[idx] = segment_path
                    log_status(f"Scene child completed: index={idx}, segment={segment_path}", runtime_log_file)
                    if scene_output.strip():
                        log_status(truncate_output(scene_output, limit=1000), runtime_log_file)
                except Exception as exc:
                    if first_error is None:
                        first_error = (idx, exc)

            if first_error is not None:
                fail_idx, fail_exc = first_error
                stop_event.set()
                terminate_all_processes(process_registry, registry_lock)
                for pending_future in pending:
                    pending_future.cancel()
                raise RuntimeError(f"Parallel scene rendering failed at index {fail_idx}: {fail_exc}") from fail_exc

    return [indexed_paths[idx] for idx in sorted(indexed_paths)]
