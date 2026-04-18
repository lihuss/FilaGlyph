from __future__ import annotations

from argparse import ArgumentParser
import shutil
import subprocess
import sys
import threading
import traceback
from pathlib import Path
import io

# Force UTF-8 for Windows console output to avoid GBK encoding errors
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
if sys.stderr.encoding != 'utf-8':
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

SCRIPT_ROOT = Path(__file__).resolve().parent
SYS_SRC_PATH = SCRIPT_ROOT / "src"
if str(SYS_SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SYS_SRC_PATH))

from makevideo.cleanup import (
    cleanup_runtime_artifacts,
    cleanup_success_inputs,
    ensure_run_workspace,
)
from makevideo.config import PROJECT_ROOT, SCENE_BUFFER_SECONDS, SRC_PATH
from makevideo.logging import create_task_log_file, log_status
from makevideo.media import (
    merge_segments_with_ffmpeg,
    mix_video_with_audio,
    pick_random_music,
    retime_video_to_target_duration,
)
from makevideo.scenes import (
    resolve_scene_jobs,
    resolve_path as resolve_project_path,
    render_scenes_via_child_scripts,
)
from makevideo.subprocess import truncate_output
from makevideo.tts import acquire_tts_slot
from makevideo.tts import run_narration_child_script
from makevideo.tts import read_narration_lines, run_segmented_narration_child_scripts

from core.utils.error_logging import log_runtime_error

CURRENT_TASK_LOG_FILE: Path | None = None


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(
        description=(
            "Render multiple Manim scenes, optionally synthesize narration from one script file, "
            "retime video to narration duration + 0.5s, then mux final output."
        )
    )
    parser.add_argument(
        "--scene-files",
        type=str,
        required=True,
        help="Comma-separated project-relative scene .py files (one file per scene job)",
    )
    parser.add_argument(
        "--scene-names",
        type=str,
        required=True,
        help="Comma-separated scene class names aligned 1:1 with --scene-files",
    )
    parser.add_argument("--output", type=str, required=True, help="Project-relative final MP4 output path")
    parser.add_argument("--quality", type=str, choices=["l", "m", "h", "p", "k"], default="h")
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--resolution", type=str, default="1920,1080")
    parser.add_argument(
        "--run-dir",
        type=str,
        required=True,
        help="Project-relative or absolute runtime workspace for this render task.",
    )

    parser.add_argument(
        "--enable-multithread",
        action="store_true",
        help="If set, scene rendering runs in parallel by scene count; TTS remains serial.",
    )
    parser.add_argument(
        "--tts-script-file",
        type=str,
        default="",
        help="Single narration file used for one-pass TTS synthesis.",
    )
    parser.add_argument("--tts-lang", type=str, default="zh-cn")
    parser.add_argument(
        "--voice",
        type=str,
        default="none",
        help="Voice clone audio path or 'none' to disable narration.",
    )
    parser.add_argument("--tts-prompt-text", type=str, default="", help="Optional transcript of custom voice audio.")
    parser.add_argument("--tts-model-dir", type=str, default="", help="Override CosyVoice model dir or repo id.")
    parser.add_argument("--tts-backend", type=str, choices=["local", "modelscope_api"], default="local")
    parser.add_argument("--tts-device", type=str, choices=["auto", "cuda", "cpu"], default="auto")
    parser.add_argument("--tts-api-base-url", type=str, default="", help="CosyVoice HTTP API base URL, for example https://... or http://host:50000")
    parser.add_argument("--tts-api-key", type=str, default="", help="Optional bearer token for CosyVoice HTTP API.")
    parser.add_argument("--tts-api-timeout", type=float, default=180.0)
    parser.add_argument("--tts-gain", type=float, default=1.35)
    parser.add_argument("--tts-speed", type=float, default=1.1, help="Base speed for TTS generation (default: 1.1)")
    parser.add_argument("--musics-dir", type=str, default="materials/musics", help="Folder with background music.")
    parser.add_argument("--no-bgm", action="store_true", help="Disable background music completely.")
    parser.add_argument("--bgm-path", type=str, default="")
    parser.add_argument("--bgm-volume", type=float, default=0.12)
    parser.add_argument(
        "--runtime-log-file",
        type=str,
        default="",
        help="Optional absolute or project-relative runtime log file path.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    global CURRENT_TASK_LOG_FILE
    if args.runtime_log_file:
        runtime_log_file = resolve_project_path(args.runtime_log_file)
        runtime_log_file.parent.mkdir(parents=True, exist_ok=True)
        runtime_log_file.touch(exist_ok=True)
    else:
        runtime_log_file = create_task_log_file(PROJECT_ROOT / "outputs" / "agent_runs")
    CURRENT_TASK_LOG_FILE = runtime_log_file
    log_status(f"Runtime log initialized: {runtime_log_file}", runtime_log_file)
    log_status(f"Command line: {' '.join(sys.argv)}", runtime_log_file)

    scene_runner_script = (SRC_PATH / "make_manim_scene.py").resolve()
    dub_runner_script = (SRC_PATH / "make_manim_dub.py").resolve()

    if not scene_runner_script.exists() or not scene_runner_script.is_file():
        raise FileNotFoundError(f"Scene child script not found: {scene_runner_script}")
    if not dub_runner_script.exists() or not dub_runner_script.is_file():
        raise FileNotFoundError(f"Dubbing child script not found: {dub_runner_script}")

    output_path = resolve_project_path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    scene_jobs = resolve_scene_jobs(args)
    if not scene_jobs:
        raise ValueError("No scene names selected for rendering")

    run_dir = resolve_project_path(args.run_dir)

    ensure_run_workspace(run_dir)
    cleanup_runtime_artifacts(run_dir)
    log_status(f"Runtime workspace: {run_dir}", runtime_log_file)

    success = False
    narration_source: Path | None = None
    combined_narration_audio: Path | None = None
    merged_target_duration: float | None = None
    narration_segment_durations: list[float] = []
    segmented_tts_used = False

    try:
        tts_disabled = args.voice.strip().lower() in ("none", "disable")
        if not tts_disabled:
            if not args.tts_script_file:
                raise ValueError("TTS is enabled but --tts-script-file is missing")

            narration_source = resolve_project_path(args.tts_script_file)
            if not narration_source.exists() or not narration_source.is_file():
                raise FileNotFoundError(f"TTS script file not found: {narration_source}")

            narration_lines = read_narration_lines(narration_source)
            combined_narration_audio = run_dir / "narration_full.wav"
            stop_event = threading.Event()
            process_registry: list[subprocess.Popen] = []
            registry_lock = threading.Lock()

            with acquire_tts_slot(runtime_log_file):
                if len(narration_lines) == len(scene_jobs):
                    segmented_tts_used = True
                    log_status(
                        f"Synthesizing segmented narration: lines={len(narration_lines)}, scenes={len(scene_jobs)}",
                        runtime_log_file,
                    )
                    _, narration_segment_durations, _, narration_duration, output = run_segmented_narration_child_scripts(
                        narration_lines=narration_lines,
                        output_audio=combined_narration_audio,
                        tts_run_dir=run_dir / "tts_tmp_segmented",
                        args=args,
                        dub_runner_script=dub_runner_script,
                        runtime_log_file=runtime_log_file,
                        stop_event=stop_event,
                        process_registry=process_registry,
                        registry_lock=registry_lock,
                    )
                    if output.strip():
                        log_status(truncate_output(output, limit=500), runtime_log_file)
                    log_status(
                        "Segment durations(s): " + ", ".join([f"{d:.3f}" for d in narration_segment_durations]),
                        runtime_log_file,
                    )
                else:
                    log_status(
                        (
                            "Fallback to full narration synthesis: "
                            f"narration-lines={len(narration_lines)} does not match scene-count={len(scene_jobs)}"
                        ),
                        runtime_log_file,
                    )
                    _, narration_duration, output = run_narration_child_script(
                        narration_file=narration_source,
                        output_audio=combined_narration_audio,
                        tts_run_dir=run_dir / "tts_tmp_full",
                        args=args,
                        dub_runner_script=dub_runner_script,
                        runtime_log_file=runtime_log_file,
                        stop_event=stop_event,
                        process_registry=process_registry,
                        registry_lock=registry_lock,
                    )
                    if output.strip():
                        log_status(truncate_output(output, limit=500), runtime_log_file)
                    merged_target_duration = narration_duration + SCENE_BUFFER_SECONDS
                    log_status(
                        f"Single narration duration={narration_duration:.3f}s, merged target={merged_target_duration:.3f}s",
                        runtime_log_file,
                    )

        workers = len(scene_jobs) if args.enable_multithread else 1
        log_status(f"Rendering scenes via child scripts (workers={workers})", runtime_log_file)
        for scene_index, scene_file_path, scene_name in scene_jobs:
            log_status(
                f"  job#{scene_index}: file={scene_file_path} scene={scene_name}",
                runtime_log_file,
            )

        segment_paths = render_scenes_via_child_scripts(
            scene_jobs=scene_jobs,
            args=args,
            run_dir=run_dir,
            scene_runner_script=scene_runner_script,
            runtime_log_file=runtime_log_file,
        )

        if not tts_disabled:
            if segmented_tts_used:
                if len(narration_segment_durations) != len(segment_paths):
                    raise RuntimeError(
                        "Segmented TTS duration count mismatch: "
                        f"durations={len(narration_segment_durations)} scene-segments={len(segment_paths)}"
                    )
                per_scene_buffer = SCENE_BUFFER_SECONDS / max(1, len(segment_paths))
                for idx, (segment_path, narration_duration) in enumerate(
                    zip(segment_paths, narration_segment_durations), start=1
                ):
                    target_duration = narration_duration + per_scene_buffer
                    aligned_duration = retime_video_to_target_duration(
                        video_path=segment_path,
                        target_duration=target_duration,
                        fps=args.fps,
                    )
                    log_status(
                        (
                            f"Aligned scene#{idx}: narration={narration_duration:.3f}s "
                            f"target={target_duration:.3f}s video={aligned_duration:.3f}s"
                        ),
                        runtime_log_file,
                    )

        merged_video = run_dir / "merged_video.mp4"
        merge_segments_with_ffmpeg(segment_paths, run_dir, merged_video)

        if not tts_disabled and not segmented_tts_used:
            if merged_target_duration is None:
                raise RuntimeError("Internal error: merged target duration missing")
            final_merged_duration = retime_video_to_target_duration(
                video_path=merged_video,
                target_duration=merged_target_duration,
                fps=args.fps,
            )
            log_status(
                f"Aligned merged video duration={final_merged_duration:.3f}s target={merged_target_duration:.3f}s",
                runtime_log_file,
            )

        if not tts_disabled:
            if combined_narration_audio is None:
                raise RuntimeError("Internal error: combined narration audio missing")

            bgm_path: Path | None = None
            if args.no_bgm:
                log_status("Background music disabled by --no-bgm", runtime_log_file)
            elif args.bgm_path:
                bgm_path = resolve_project_path(args.bgm_path)
                if not bgm_path.exists() or not bgm_path.is_file():
                    raise FileNotFoundError(f"Background music file not found: {bgm_path}")
                log_status(f"Using background music: {bgm_path.name}", runtime_log_file)
            else:
                bgm_path = pick_random_music(resolve_project_path(args.musics_dir))
                if bgm_path is not None:
                    log_status(f"Using background music: {bgm_path.name}", runtime_log_file)
                else:
                    log_status(f"No background music found in: {args.musics_dir}", runtime_log_file)

            mix_video_with_audio(
                video_path=merged_video,
                narration_audio_path=combined_narration_audio,
                args=args,
                bgm_path=bgm_path,
                tmp_dir=run_dir,
            )

        shutil.copy2(merged_video, output_path)
        success = True
        log_status(f"Final MP4 generated: {output_path}", runtime_log_file)
    except Exception:
        log_status("Render failed. Detailed error:", runtime_log_file)
        log_status(traceback.format_exc(), runtime_log_file)
        raise
    finally:
        if success:
            cleanup_runtime_artifacts(run_dir)
            cleanup_success_inputs(scene_jobs=scene_jobs, narration_source=narration_source, run_dir=run_dir)
            log_status(f"Cleanup complete: intermediates and task input drafts removed from {run_dir}", runtime_log_file)
        else:
            log_status(f"Render failed; run artifacts retained in: {run_dir}", runtime_log_file)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        if CURRENT_TASK_LOG_FILE is None:
            log_path = log_runtime_error("make_manim_video.py", exc)
            print(f"Error details written to: {log_path}")
        else:
            print(f"Error details already written to: {CURRENT_TASK_LOG_FILE}")
        raise
