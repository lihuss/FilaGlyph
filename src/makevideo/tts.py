from __future__ import annotations

import subprocess
import threading
import sys

from pathlib import Path

from .config import PROJECT_ROOT
from .media import probe_media_duration
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
    output_audio.unlink(missing_ok=True)

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
        "--tts-device",
        args.tts_device,
        "--tts-speed",
        str(args.tts_speed),
        "--runtime-log-file",
        str(runtime_log_file),
    ]

    if args.tts_speaker_id:
        command.extend(["--tts-speaker-id", args.tts_speaker_id])
    if args.tts_model_dir:
        command.extend(["--tts-model-dir", args.tts_model_dir])

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
