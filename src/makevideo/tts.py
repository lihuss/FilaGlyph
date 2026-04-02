from __future__ import annotations

import subprocess
import threading
import sys

from pathlib import Path

from .config import PROJECT_ROOT
from .media import merge_audio_segments_with_ffmpeg, probe_media_duration
from .subprocess import run_subprocess_with_failfast, truncate_output


def run_narration_child_script(
    narration_file: Path,
    output_audio: Path,
    tts_run_dir: Path,
    args,
    dub_runner_script: Path,
    runtime_log_file: Path,
    stop_event: threading.Event,
    process_registry: list[subprocess.Popen],
    registry_lock: threading.Lock,
) -> tuple[Path, float, str]:
    tts_run_dir.mkdir(parents=True, exist_ok=True)
    output_audio.parent.mkdir(parents=True, exist_ok=True)
    # Removed for RTX 5060 stability
    pass

    command = [
        sys.executable,
        str(dub_runner_script),
        "--tts-script-file",
        str(narration_file),
        "--output-audio",
        str(output_audio),
        "--run-dir",
        str(tts_run_dir),
        "--tts-lang",
        args.tts_lang,
        "--voice",
        args.voice,
        "--tts-backend",
        args.tts_backend,
        "--tts-device",
        args.tts_device,
        "--tts-speed",
        str(args.tts_speed),
        "--runtime-log-file",
        str(runtime_log_file),
    ]

    if args.tts_model_dir:
        command.extend(["--tts-model-dir", args.tts_model_dir])
    if getattr(args, "tts_prompt_text", None):
        command.extend(["--tts-prompt-text", args.tts_prompt_text])
    if getattr(args, "tts_api_base_url", None):
        command.extend(["--tts-api-base-url", args.tts_api_base_url])
    if getattr(args, "tts_api_key", None):
        command.extend(["--tts-api-key", args.tts_api_key])
    if getattr(args, "tts_api_timeout", None) is not None:
        command.extend(["--tts-api-timeout", str(args.tts_api_timeout)])

    try:
        output = run_subprocess_with_failfast(command, PROJECT_ROOT, stop_event, process_registry, registry_lock)
    except subprocess.CalledProcessError as exc:
        command_text = " ".join(command)
        output_text = truncate_output(exc.output or "")
        raise RuntimeError(
            f"Narration child script failed: input={narration_file}\n"
            f"Command: {command_text}\n"
            f"ExitCode: {exc.returncode}\n"
            f"Output:\n{output_text}"
        ) from exc

    if not output_audio.exists() or not output_audio.is_file():
        raise FileNotFoundError(
            f"Narration child script reported success but audio not found: {output_audio}\n"
            f"Output:\n{truncate_output(output)}"
        )

    duration = probe_media_duration(output_audio)
    return output_audio, duration, output


def read_narration_lines(narration_file: Path) -> list[str]:
    if not narration_file.exists() or not narration_file.is_file():
        raise FileNotFoundError(f"TTS script file not found: {narration_file}")
    lines = [line.strip() for line in narration_file.read_text(encoding="utf-8-sig").splitlines()]
    return [line for line in lines if line and not line.startswith("#")]


def run_segmented_narration_child_scripts(
    narration_lines: list[str],
    output_audio: Path,
    tts_run_dir: Path,
    args,
    dub_runner_script: Path,
    runtime_log_file: Path,
    stop_event: threading.Event,
    process_registry: list[subprocess.Popen],
    registry_lock: threading.Lock,
) -> tuple[list[Path], list[float], Path, float, str]:
    if not narration_lines:
        raise ValueError("Narration lines are empty for segmented TTS.")

    scripts_dir = tts_run_dir / "scripts"
    segments_dir = tts_run_dir / "segments"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    segments_dir.mkdir(parents=True, exist_ok=True)
    output_audio.parent.mkdir(parents=True, exist_ok=True)

    audio_paths: list[Path] = []
    durations: list[float] = []
    outputs: list[str] = []

    for idx, line in enumerate(narration_lines, start=1):
        script_path = scripts_dir / f"scene_{idx:03d}.txt"
        script_path.write_text(line + "\n", encoding="utf-8")
        segment_audio = segments_dir / f"scene_{idx:03d}.wav"

        audio_path, duration, output = run_narration_child_script(
            narration_file=script_path,
            output_audio=segment_audio,
            tts_run_dir=tts_run_dir / f"tts_scene_{idx:03d}",
            args=args,
            dub_runner_script=dub_runner_script,
            runtime_log_file=runtime_log_file,
            stop_event=stop_event,
            process_registry=process_registry,
            registry_lock=registry_lock,
        )
        audio_paths.append(audio_path)
        durations.append(duration)
        outputs.append(output)

    merge_audio_segments_with_ffmpeg(audio_paths, tts_run_dir, output_audio)
    total_duration = probe_media_duration(output_audio)
    combined_output = "\n".join(outputs)
    return audio_paths, durations, output_audio, total_duration, combined_output
