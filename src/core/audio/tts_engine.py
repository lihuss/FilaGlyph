import re
import sys
import threading
import types
import os
from pathlib import Path
from typing import Any
import torch
import torchaudio

_cosyvoice_models: dict[str, object] = {}
_model_lock = threading.Lock()


class TTSEngine:
    def __init__(self, language="zh-cn", speaker_id=None, model_dir="iic/CosyVoice-300M-SFT", device="auto", retries=3):
        self.language = language
        self.speaker_id = speaker_id
        self.model_dir = model_dir
        self.device = device
        self.retries = max(1, retries)

    def synthesize_segments(self, segments, output_dir, speed=1.0):
        output_dir.mkdir(parents=True, exist_ok=True)
        normalized_segments = [self._normalize_text(text) for text in segments]
        return self._synthesize_all_cosyvoice(normalized_segments, output_dir, speed=speed)

    def _normalize_text(self, text):
        processed = (text or "").replace("\ufeff", " ").strip()
        processed = re.sub(r"\s+", " ", processed)
        return processed

    def _synthesize_all_cosyvoice(self, segments, output_dir, speed=1.0):
        cosyvoice_model = self._load_cosyvoice_model(self.model_dir, device=self.device)
        speaker_id = self._resolve_speaker_id(cosyvoice_model)
        generated_paths = [output_dir / f"{i:04d}.wav" for i in range(len(segments))]

        for idx, text in enumerate(segments):
            self._synthesize_cosyvoice_with_retry(cosyvoice_model, text, speaker_id, generated_paths[idx], speed)
        return generated_paths

    def _synthesize_cosyvoice_with_retry(self, cosyvoice_model, text, speaker_id, output_path, speed=1.0):
        if output_path.exists():
            output_path.unlink()

        spoken_text = text.strip() or "嗯"
        last_error = None
        for _ in range(self.retries):
            try:
                with _model_lock:
                    output = cosyvoice_model.inference_sft(spoken_text, speaker_id)

                waveform = self._extract_tts_waveform(output)
                if waveform is None:
                    raise RuntimeError("CosyVoice inference did not return tts_speech")

                if not torch.is_tensor(waveform):
                    waveform = torch.as_tensor(waveform)

                waveform = waveform.detach().cpu().float()
                if waveform.dim() == 1:
                    waveform = waveform.unsqueeze(0)

                sample_rate = 22050
                waveform, sample_rate = self._apply_speed_change(waveform, sample_rate, speed)
                try:
                    torchaudio.save(
                        str(output_path),
                        waveform,
                        sample_rate,
                        encoding="PCM_S",
                        bits_per_sample=16,
                    )
                except Exception:
                    import soundfile as sf

                    audio_np = waveform.squeeze(0).numpy() if waveform.shape[0] == 1 else waveform.transpose(0, 1).numpy()
                    sf.write(str(output_path), audio_np, sample_rate, subtype="PCM_16")
                return
            except Exception as exc:
                last_error = exc

        raise RuntimeError(f"CosyVoice synthesis failed for: {spoken_text}") from last_error

    @staticmethod
    def _extract_tts_waveform(output: Any):
        # Legacy CosyVoice API: dict with "tts_speech".
        if isinstance(output, dict):
            return output.get("tts_speech")

        # Newer API may return a generator yielding dict chunks.
        if hasattr(output, "__iter__") and not torch.is_tensor(output):
            collected: list[torch.Tensor] = []
            try:
                for chunk in output:
                    wave = None
                    if isinstance(chunk, dict) and "tts_speech" in chunk:
                        wave = chunk.get("tts_speech")
                    elif torch.is_tensor(chunk):
                        wave = chunk

                    if wave is None:
                        continue
                    if not torch.is_tensor(wave):
                        wave = torch.as_tensor(wave)
                    if wave.dim() == 1:
                        wave = wave.unsqueeze(0)
                    collected.append(wave)
            except TypeError:
                return None

            if not collected:
                return None
            if len(collected) == 1:
                return collected[0]
            return torch.cat(collected, dim=1)

        return None

    @staticmethod
    def _apply_speed_change(waveform: torch.Tensor, sample_rate: int, speed: float) -> tuple[torch.Tensor, int]:
        if abs(speed - 1.0) < 1e-6:
            return waveform, sample_rate

        try:
            from cosyvoice.utils.file_utils import speed_change
            adjusted, adjusted_rate = speed_change(waveform, sample_rate, str(speed))
            if not torch.is_tensor(adjusted):
                adjusted = torch.as_tensor(adjusted)
            adjusted = adjusted.detach().cpu().float()
            if adjusted.dim() == 1:
                adjusted = adjusted.unsqueeze(0)
            return adjusted, int(adjusted_rate)
        except Exception:
            # If sox effects are unavailable on the host, keep original speed.
            return waveform, sample_rate

    def _resolve_speaker_id(self, cosyvoice_model) -> str:
        list_fn = getattr(cosyvoice_model, "list_avaliable_spks", None)
        if list_fn is None:
            list_fn = getattr(cosyvoice_model, "list_available_spks", None)
        if list_fn is None:
            raise RuntimeError("CosyVoice model does not expose speaker list API")

        available = list(list_fn())
        if not available:
            raise RuntimeError("CosyVoice returned an empty speaker list")

        if self.speaker_id:
            if self.speaker_id in available:
                return self.speaker_id
            available_str = ", ".join(available)
            raise ValueError(f"CosyVoice speaker '{self.speaker_id}' is not available. Available speakers: {available_str}")

        preferred = ["中文女", "中文男", "粤语女", "粤语男"]
        for candidate in preferred:
            if candidate in available:
                return candidate
        return available[0]

    @classmethod
    def _load_cosyvoice_model(cls, model_dir: str, device: str = "auto"):
        resolved_model_ref = cls._resolve_model_ref(model_dir)
        runtime_device = cls._select_runtime_device(device)
        cache_key = f"{resolved_model_ref}::{runtime_device}"

        if cache_key in _cosyvoice_models:
            return _cosyvoice_models[cache_key]

        with _model_lock:
            if cache_key in _cosyvoice_models:
                return _cosyvoice_models[cache_key]

            original_is_available = None
            try:
                if runtime_device == "cpu":
                    # Must be set before CosyVoice internals initialize torch CUDA contexts.
                    os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")
                    original_is_available = torch.cuda.is_available
                    torch.cuda.is_available = lambda: False

                cls._inject_cosyvoice_paths()
                from cosyvoice.cli.cosyvoice import CosyVoice

                model = CosyVoice(resolved_model_ref)
                _cosyvoice_models[cache_key] = model
                return model
            finally:
                if original_is_available is not None:
                    torch.cuda.is_available = original_is_available

    @classmethod
    def _select_runtime_device(cls, requested: str) -> str:
        choice = (requested or "auto").strip().lower()
        if choice == "cpu":
            return "cpu"
        if choice == "cuda":
            return "cuda"

        # auto: prefer CUDA only when the installed torch build supports current GPU arch.
        if not torch.cuda.is_available():
            return "cpu"

        try:
            arch_list = torch.cuda.get_arch_list()
            supported = []
            for arch in arch_list:
                m = re.match(r"sm_(\d+)", arch)
                if m:
                    supported.append(int(m.group(1)))

            if not supported:
                return "cpu"

            major, minor = torch.cuda.get_device_capability(0)
            current = major * 10 + minor
            if current > max(supported):
                return "cpu"
            return "cuda"
        except Exception:
            return "cpu"

    @classmethod
    def _inject_cosyvoice_paths(cls) -> None:
        project_root = cls._project_root()
        cosyvoice_root = project_root / "CosyVoice"
        matcha_root = cosyvoice_root / "third_party" / "Matcha-TTS"

        if not cosyvoice_root.exists() or not cosyvoice_root.is_dir():
            raise FileNotFoundError(f"CosyVoice directory not found: {cosyvoice_root}")

        cosyvoice_root_str = str(cosyvoice_root)
        matcha_root_str = str(matcha_root)
        if cosyvoice_root_str not in sys.path:
            sys.path.insert(0, cosyvoice_root_str)
        if matcha_root.exists() and matcha_root_str not in sys.path:
            sys.path.insert(0, matcha_root_str)

        cls._inject_optional_dependency_stubs()

    @classmethod
    def _inject_optional_dependency_stubs(cls) -> None:
        # Do not silently inject fake tokenizers/normalizers: they can produce invalid tokens
        # and cause gibberish synthesis. Fail fast with actionable installation guidance.
        try:
            __import__("whisper")
        except Exception as exc:
            raise RuntimeError(
                "Missing dependency 'openai-whisper'. Install it with: "
                "pip install openai-whisper"
            ) from exc

    @classmethod
    def _project_root(cls) -> Path:
        return Path(__file__).resolve().parents[3]

    @classmethod
    def _resolve_model_ref(cls, model_dir: str) -> str:
        raw = (model_dir or "").strip()
        if not raw:
            raw = "iic/CosyVoice-300M-SFT"

        candidate = Path(raw)
        if candidate.is_absolute() and candidate.exists():
            return str(candidate)

        local_candidate = (cls._project_root() / candidate).resolve()
        if local_candidate.exists():
            return str(local_candidate)

        # Keep as repo id for modelscope snapshot download.
        return raw
