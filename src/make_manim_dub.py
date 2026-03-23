from __future__ import annotations

import json
from datetime import datetime
import os
import shutil
import traceback
from argparse import ArgumentParser
from pathlib import Path

from core.audio.tts_engine import TTSEngine
from core.utils.error_logging import log_runtime_error


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _append_runtime_log(runtime_log_file: Path | None, message: str) -> None:
    if runtime_log_file is None:
        return
    runtime_log_file.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] [pid={os.getpid()}] [dub-child] {message}"
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


def _read_narration_text(script_file: Path) -> str:
    if not script_file.exists() or not script_file.is_file():
        raise FileNotFoundError(f"TTS script file not found: {script_file}")

    lines = [line.strip() for line in script_file.read_text(encoding="utf-8-sig").splitlines()]
    lines = [line for line in lines if line and not line.startswith("#")]
    if not lines:
        raise ValueError(f"TTS script file is empty: {script_file}")

    return "\n".join(lines).strip()


def _resolve_tts_runtime(model_override: str, speaker_override: str, voice_key: str) -> tuple[str, str]:
    default_model = _resolve_path("CosyVoice/pretrained_models/CosyVoice-300M-SFT")
    model_fallback = str(default_model) if default_model.exists() else "iic/CosyVoice-300M-SFT"
    speaker_fallback = "中文男" if voice_key.strip().lower() == "male" else "中文女"

    model_dir = model_override.strip() if model_override else model_fallback
    speaker_id = speaker_override.strip() if speaker_override else speaker_fallback
    return model_dir, speaker_id


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(description="Generate one full narration audio file using CosyVoice.")
    parser.add_argument("--tts-script-file", type=str, required=True)
    parser.add_argument("--output-audio", type=str, required=True)
    parser.add_argument("--run-dir", type=str, default="outputs/runs/manual")

    parser.add_argument("--tts-lang", type=str, default="zh-cn")
    parser.add_argument("--voice", type=str, choices=["female", "male"], default="male")
    parser.add_argument("--tts-speaker-id", type=str, default="")
    parser.add_argument("--tts-model-dir", type=str, default="")
    parser.add_argument("--tts-device", type=str, choices=["auto", "cuda", "cpu"], default="auto")
    parser.add_argument("--tts-speed", type=float, default=1.1)
    parser.add_argument("--keep-tts-audio", action="store_true")
    parser.add_argument("--runtime-log-file", type=str, default="")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    runtime_log_file = _resolve_path(args.runtime_log_file) if args.runtime_log_file else None
    _append_runtime_log(runtime_log_file, "start narration synthesis")

    try:
        script_file = _resolve_path(args.tts_script_file)
        output_audio = _resolve_path(args.output_audio)
        run_dir = _resolve_path(args.run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)

        narration_text = _read_narration_text(script_file)
        model_dir, speaker_id = _resolve_tts_runtime(args.tts_model_dir, args.tts_speaker_id, args.voice)

        engine = TTSEngine(
            language=args.tts_lang,
            speaker_id=speaker_id,
            model_dir=model_dir,
            device=args.tts_device,
        )

        generated_paths = engine.synthesize_segments([narration_text], run_dir, speed=args.tts_speed)
        if len(generated_paths) != 1:
            raise RuntimeError(f"Expected 1 TTS file, got {len(generated_paths)}")

        output_audio.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(generated_paths[0], output_audio)

        if not args.keep_tts_audio:
            for path in generated_paths:
                path.unlink(missing_ok=True)

        result = {
            "status": "ok",
            "audio_path": str(output_audio),
            "speaker_id": speaker_id,
            "model_dir": model_dir,
        }
        _append_runtime_log(runtime_log_file, f"narration complete: output={output_audio}")
        print(json.dumps(result, ensure_ascii=True))
    except Exception as exc:
        _append_runtime_log(runtime_log_file, f"narration failed: error={exc}")
        raise


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(traceback.format_exc())
        if not _runtime_log_requested(sys.argv[1:]):
            log_path = log_runtime_error("src/make_manim_dub.py", exc)
            print(f"Error details written to: {log_path}")
        raise
