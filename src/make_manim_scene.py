from __future__ import annotations

import json
from datetime import datetime
import os
import shutil
import subprocess
import sys
import traceback
from argparse import ArgumentParser
from pathlib import Path

from core.utils.error_logging import log_runtime_error


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _append_runtime_log(runtime_log_file: Path | None, message: str) -> None:
    if runtime_log_file is None:
        return
    runtime_log_file.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] [pid={os.getpid()}] [scene-child] {message}"
    with runtime_log_file.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _runtime_log_requested(argv: list[str]) -> bool:
    for index, value in enumerate(argv):
        if value == "--runtime-log-file":
            return index + 1 < len(argv) and bool(argv[index + 1].strip())
        if value.startswith("--runtime-log-file="):
            return bool(value.partition("=")[2].strip())
    return False


def _resolve_path(raw_path: str) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path.resolve()
    return (PROJECT_ROOT / path).resolve()


def _find_rendered_mp4(media_dir: Path, file_stem: str) -> Path:
    candidates = list(media_dir.glob(f"videos/**/{file_stem}.mp4"))
    if not candidates:
        raise FileNotFoundError(f"No rendered MP4 found under {media_dir} for stem: {file_stem}")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _truncate_output(text: str, limit: int = 12000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... [truncated]"


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(description="Render one Manim scene in an isolated runtime workspace (outputs/runs/run_*/sceneX).")
    parser.add_argument("--scene-file", type=str, required=True)
    parser.add_argument("--scene-name", type=str, required=True)
    parser.add_argument("--scene-index", type=int, required=True)
    parser.add_argument("--run-dir", type=str, default="outputs/runs/manual")
    parser.add_argument("--quality", type=str, choices=["l", "m", "h", "p", "k"], default="h")
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--resolution", type=str, default="1920,1080")
    parser.add_argument("--runtime-log-file", type=str, default="")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    runtime_log_file = _resolve_path(args.runtime_log_file) if args.runtime_log_file else None
    _append_runtime_log(runtime_log_file, f"start scene-index={args.scene_index} scene-name={args.scene_name}")

    try:
        scene_file = _resolve_path(args.scene_file)
        run_dir = _resolve_path(args.run_dir)

        if not scene_file.exists() or not scene_file.is_file():
            raise FileNotFoundError(f"Scene file not found: {scene_file}")

        scene_tmp_dir = run_dir / f"scene{args.scene_index}"
        scene_tmp_dir.mkdir(parents=True, exist_ok=True)

        local_scene_file = scene_tmp_dir / scene_file.name
        if local_scene_file.resolve() != scene_file.resolve():
            shutil.copy2(scene_file, local_scene_file)

        output_stem = f"segment_{args.scene_index:03d}_{args.scene_name}"
        command = [
            "-m",
            "manim",
            "render",
            str(local_scene_file),
            args.scene_name,
            "--format",
            "mp4",
            "--quality",
            args.quality,
            "--fps",
            str(args.fps),
            "--resolution",
            args.resolution,
            "--media_dir",
            str(scene_tmp_dir),
            "--output_file",
            output_stem,
        ]
        _append_runtime_log(runtime_log_file, f"render command started: {' '.join(command)}")

        completed = subprocess.run(
            [sys.executable, *command],
            cwd=PROJECT_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        if completed.returncode != 0:
            output_text = _truncate_output(completed.stdout or "")
            raise RuntimeError(
                f"Scene render failed: index={args.scene_index}, name={args.scene_name}\n"
                f"Command: {' '.join(command)}\n"
                f"ExitCode: {completed.returncode}\n"
                f"Output:\n{output_text}"
            )

        rendered = _find_rendered_mp4(scene_tmp_dir, output_stem)
        segment_path = scene_tmp_dir / f"{output_stem}.mp4"
        if rendered.resolve() != segment_path.resolve():
            shutil.copy2(rendered, segment_path)

        result = {
            "status": "ok",
            "scene_index": args.scene_index,
            "scene_name": args.scene_name,
            "segment_path": str(segment_path),
        }
        _append_runtime_log(runtime_log_file, f"scene complete: index={args.scene_index}, segment={segment_path}")
        print(json.dumps(result, ensure_ascii=True))
    except Exception as exc:
        _append_runtime_log(runtime_log_file, f"scene failed: index={args.scene_index}, error={exc}")
        raise


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(traceback.format_exc())
        if not _runtime_log_requested(sys.argv[1:]):
            log_path = log_runtime_error("src/make_manim_scene.py", exc)
            print(f"Error details written to: {log_path}")
        raise
