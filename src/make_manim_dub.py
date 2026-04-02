from __future__ import annotations

import json
from datetime import datetime
import os
import shutil
import traceback
import sys
from argparse import ArgumentParser
from pathlib import Path
import io

# Force UTF-8 for Windows console output to avoid GBK encoding errors
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
if sys.stderr.encoding != 'utf-8':
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

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


def _load_cosyvoice_config() -> dict:
    candidates = [
        _resolve_path("config/cosyvoice_config.json"),
        _resolve_path("cosyvoice_config.json"),
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def _read_narration_text(script_file: Path) -> list[str]:
    if not script_file.exists() or not script_file.is_file():
        raise FileNotFoundError(f"TTS script file not found: {script_file}")

    lines = [line.strip() for line in script_file.read_text(encoding="utf-8-sig").splitlines()]
    lines = [line for line in lines if line and not line.startswith("#")]
    if not lines:
        raise ValueError(f"TTS script file is empty: {script_file}")

    return lines


def _resolve_model_dir(model_override: str) -> str:
    """Resolve CosyVoice model directory, preferring local pretrained models."""
    cfg = _load_cosyvoice_config()
    configured = str(cfg.get("model_dir", "")).strip()
    default_ref = configured or "CosyVoice/pretrained_models/CosyVoice2-0.5B"
    default_model = _resolve_path(default_ref)
    model_fallback = str(default_model) if default_model.exists() else "iic/CosyVoice2-0.5B"
    return model_override.strip() if model_override else model_fallback


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(description="Generate one full narration audio file using CosyVoice.")
    parser.add_argument("--tts-script-file", type=str, required=True)
    parser.add_argument("--output-audio", type=str, required=True)
    parser.add_argument("--run-dir", type=str, default="outputs/runs/manual")

    parser.add_argument("--tts-lang", type=str, default="zh-cn")
    parser.add_argument(
        "--voice", type=str, default="none",
        help="Voice clone audio path, `clone:<filename>`, or 'none' to disable narration.",
    )
    parser.add_argument("--tts-model-dir", type=str, default="", help="Override CosyVoice model dir or repo id.")
    parser.add_argument("--tts-backend", type=str, choices=["local", "modelscope_api"], default="local")
    parser.add_argument("--tts-device", type=str, choices=["auto", "cuda", "cpu"], default="auto")
    parser.add_argument("--tts-prompt-text", type=str, default="")
    parser.add_argument("--tts-api-base-url", type=str, default="")
    parser.add_argument("--tts-api-key", type=str, default="")
    parser.add_argument("--tts-api-timeout", type=float, default=180.0)
    parser.add_argument("--tts-speed", type=float, default=1.1)
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

        narration_texts = _read_narration_text(script_file)
        model_dir = _resolve_model_dir(args.tts_model_dir)

        voice_val = args.voice.strip()
        _append_runtime_log(runtime_log_file, f"voice={voice_val}, model_dir={model_dir}")

        engine = TTSEngine(
            voice=voice_val,
            language=args.tts_lang,
            model_dir=model_dir,
            backend=args.tts_backend,
            device=args.tts_device,
            prompt_text=args.tts_prompt_text,
            api_base_url=args.tts_api_base_url,
            api_key=args.tts_api_key,
            api_timeout_s=args.tts_api_timeout,
        )

        _append_runtime_log(
            runtime_log_file,
            f"TTS mode={engine.mode}, speaker_id={engine.speaker_id}, prompt_wav={engine.prompt_wav}",
        )

        generated_paths = engine.synthesize_segments(narration_texts, run_dir, speed=args.tts_speed)

        if not generated_paths:
            raise RuntimeError("No audio segments were generated.")

        # Concatenate segments into one final audio file
        output_audio.parent.mkdir(parents=True, exist_ok=True)
        if len(generated_paths) == 1:
            shutil.copy2(generated_paths[0], output_audio)
        else:
            _append_runtime_log(runtime_log_file, f"concatenating {len(generated_paths)} segments...")
            import torch
            import torchaudio
            waveforms = []
            sample_rate = 22050
            for p in generated_paths:
                w, sr = torchaudio.load(str(p))
                waveforms.append(w)
                sample_rate = sr

            final_waveform = torch.cat(waveforms, dim=1)
            torchaudio.save(str(output_audio), final_waveform, sample_rate)

        result = {
            "status": "ok",
            "audio_path": str(output_audio),
            "mode": engine.mode,
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
