import json
import hashlib
import os
import re
import sys
import threading
from pathlib import Path
from typing import Any

import numpy as np
import requests
import torch
import torchaudio

# RTX 5060 Blackwell GPU requires disabling experimental SDPA for CosyVoice2 stability
torch.backends.cuda.enable_flash_sdp(False)
torch.backends.cuda.enable_mem_efficient_sdp(False)
torch.backends.cuda.enable_math_sdp(True)

_cosyvoice_models: dict[str, object] = {}
_model_lock = threading.Lock()

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _read_cosyvoice_default_model_dir() -> str:
    candidates = [
        PROJECT_ROOT / "config" / "cosyvoice_config.json",
        PROJECT_ROOT / "cosyvoice_config.json",
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            model_dir = str(data.get("model_dir", "")).strip() if isinstance(data, dict) else ""
            if model_dir:
                return model_dir
        except Exception:
            return "CosyVoice/pretrained_models/CosyVoice2-0.5B"
    return "CosyVoice/pretrained_models/CosyVoice2-0.5B"


DEFAULT_MODEL_DIR = _read_cosyvoice_default_model_dir()
DEFAULT_API_BASE_URL = os.environ.get("COSYVOICE_MODELSCOPE_API_URL", "").strip() or os.environ.get("COSYVOICE_API_URL", "").strip()
DEFAULT_API_TIMEOUT_S = float(os.environ.get("COSYVOICE_API_TIMEOUT_S", "180"))


class TTSEngine:
    """CosyVoice engine supporting only zero-shot voice cloning."""

    def __init__(
        self,
        voice: str = "none",
        language: str = "zh-cn",
        model_dir: str = DEFAULT_MODEL_DIR,
        backend: str = "local",
        device: str = "auto",
        prompt_text: str = "",
        api_base_url: str = DEFAULT_API_BASE_URL,
        api_key: str = "",
        api_timeout_s: float = DEFAULT_API_TIMEOUT_S,
        retries: int = 3,
    ):
        self.language = language
        self.model_dir = model_dir
        self.backend = self._resolve_backend(backend)
        self.device = device
        self.prompt_text = prompt_text.strip()
        self.api_base_url = api_base_url.strip()
        self.api_key = api_key.strip()
        self.api_timeout_s = max(5.0, float(api_timeout_s))
        self.retries = max(1, retries)

        voice_stripped = (voice or "").strip()
        if voice_stripped.lower() in {"none", "disable", ""}:
            raise ValueError("Narration is disabled. Provide a reference audio file path to enable CosyVoice cloning.")
        if voice_stripped.lower() in {"male", "female"}:
            raise ValueError("Default male/female voices have been removed. Please choose a reference audio file for cloning.")

        resolved = self._resolve_voice_path(voice_stripped)
        if not resolved.exists():
            raise FileNotFoundError(f"Voice cloning audio file not found: {resolved}")

        self.mode = "zero_shot"
        self.speaker_id = ""
        self.prompt_wav = str(self._prepare_prompt_wav(resolved))

        if self.backend == "modelscope_api" and not self.api_base_url:
            raise ValueError(
                "TTS backend is modelscope_api, but no API base URL was provided. "
                "Pass --tts-api-base-url or set COSYVOICE_MODELSCOPE_API_URL."
            )

    def synthesize_segments(self, segments: list[str], output_dir: Path, speed: float = 1.0) -> list[Path]:
        output_dir.mkdir(parents=True, exist_ok=True)
        normalized = [self._normalize_text(text) for text in segments]
        return self._synthesize_all(normalized, output_dir, speed=speed)

    def _synthesize_all(self, segments: list[str], output_dir: Path, speed: float = 1.0) -> list[Path]:
        cosyvoice_model = None
        if self.backend == "local":
            cosyvoice_model = self._load_cosyvoice_model(self.model_dir, device=self.device)

        generated_paths = [output_dir / f"{i:04d}.wav" for i in range(len(segments))]

        import gc

        for idx, text in enumerate(segments):
            # One narration line => one TTS pass, no punctuation sub-chunking.
            output_path = generated_paths[idx]
            if self.backend == "modelscope_api":
                self._synthesize_api_zero_shot(text, output_path, speed)
            else:
                self._synthesize_zero_shot(cosyvoice_model, text, output_path, speed)
            gc.collect()

        return generated_paths

    def _synthesize_zero_shot(self, cosyvoice_model, text: str, output_path: Path, speed: float = 1.0):
        spoken_text = text.strip() or "你好。"
        last_error = None

        for _ in range(self.retries):
            try:
                if self.prompt_text:
                    output = cosyvoice_model.inference_zero_shot(
                        spoken_text,
                        self.prompt_text,
                        self.prompt_wav,
                        stream=False,
                        speed=speed,
                    )
                else:
                    output = cosyvoice_model.inference_cross_lingual(
                        spoken_text,
                        self.prompt_wav,
                        stream=False,
                        speed=speed,
                    )

                waveform = self._extract_tts_waveform(output)
                if waveform is None:
                    raise RuntimeError("CosyVoice cloning inference did not return tts_speech")

                self._save_waveform(waveform, output_path, speed=1.0)
                return
            except Exception as exc:
                last_error = exc

        if self.prompt_text:
            raise RuntimeError(
                f"使用提供的语音原文（prompt_text）时声音复刻失败：{spoken_text}。"
                f"请确认参考音频文本与音频内容完全一致。原始错误：{last_error}"
            ) from last_error
        raise RuntimeError(f"免参考文本跨语种复刻失败：{spoken_text}。原始错误：{last_error}") from last_error

    def _synthesize_api_zero_shot(self, text: str, output_path: Path, speed: float = 1.0):
        spoken_text = text.strip() or "你好。"
        with open(self.prompt_wav, "rb") as prompt_file:
            files = {
                "prompt_wav": (
                    Path(self.prompt_wav).name,
                    prompt_file,
                    "audio/wav",
                )
            }
            if self.prompt_text:
                waveform = self._call_cosyvoice_api(
                    "/inference_zero_shot",
                    data={"tts_text": spoken_text, "prompt_text": self.prompt_text},
                    files=files,
                )
            else:
                waveform = self._call_cosyvoice_api(
                    "/inference_cross_lingual",
                    data={"tts_text": spoken_text},
                    files=files,
                )
        self._save_waveform(waveform, output_path, speed=speed)

    def _call_cosyvoice_api(self, endpoint: str, *, data: dict[str, str], files: Any | None = None) -> torch.Tensor:
        url = f"{self.api_base_url.rstrip('/')}{endpoint}"
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        last_error = None
        for _ in range(self.retries):
            try:
                response = requests.post(
                    url,
                    data=data,
                    files=files,
                    headers=headers,
                    timeout=self.api_timeout_s,
                )
                response.raise_for_status()
                pcm_bytes = response.content
                if not pcm_bytes:
                    raise RuntimeError(f"CosyVoice API returned an empty body: {url}")

                audio = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
                if audio.size == 0:
                    raise RuntimeError(f"CosyVoice API returned empty PCM data: {url}")
                return torch.from_numpy(audio).unsqueeze(0)
            except Exception as exc:
                last_error = exc

        raise RuntimeError(f"CosyVoice API request failed: endpoint={url}, error={last_error}") from last_error

    @staticmethod
    def _normalize_text(text: str) -> str:
        processed = (text or "").replace("\ufeff", " ").strip()
        processed = re.sub(r"\s+", " ", processed)
        return processed

    def _save_waveform(self, waveform, output_path: Path, speed: float = 1.0):
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

    @staticmethod
    def _extract_tts_waveform(output: Any):
        if torch.is_tensor(output):
            return output
        if isinstance(output, dict):
            return output.get("tts_speech")
        if hasattr(output, "__iter__") and not torch.is_tensor(output):
            collected: list[torch.Tensor] = []
            try:
                for chunk in output:
                    waveform = None
                    if isinstance(chunk, dict):
                        waveform = chunk.get("tts_speech")
                    elif torch.is_tensor(chunk):
                        waveform = chunk

                    if waveform is not None:
                        stable = waveform.detach().cpu().clone()
                        if stable.dim() == 1:
                            stable = stable.unsqueeze(0)
                        collected.append(stable)
            except Exception:
                return None
            return torch.cat(collected, dim=1) if collected else None
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
            return waveform, sample_rate

    @classmethod
    def _resolve_voice_path(cls, raw: str) -> Path:
        voice_ref = (raw or "").strip()
        if voice_ref.lower().startswith("clone:"):
            filename = voice_ref[len("clone:"):].strip()
            if not filename:
                raise ValueError("Voice clone reference is missing a filename after 'clone:'.")
            return (cls._project_root() / "materials" / "voices" / filename).resolve()

        path = Path(voice_ref)
        if path.is_absolute():
            return path.resolve()
        return (cls._project_root() / path).resolve()

    @classmethod
    def _prepare_prompt_wav(cls, prompt_path: Path) -> Path:
        if prompt_path.suffix.lower() in {".wav", ".wave"}:
            return prompt_path
        return cls._transcode_prompt_audio(prompt_path)

    @classmethod
    def _transcode_prompt_audio(cls, source_path: Path) -> Path:
        cache_dir = cls._project_root() / "tmp" / "cosyvoice_prompt_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        stat = source_path.stat()
        digest = hashlib.sha1(
            f"{source_path.resolve()}::{stat.st_size}::{stat.st_mtime_ns}".encode("utf-8")
        ).hexdigest()[:16]
        target_path = cache_dir / f"{source_path.stem}_{digest}.wav"
        if target_path.exists():
            return target_path

        waveform, sample_rate = cls._load_audio_any_format(source_path)
        waveform = waveform.detach().cpu().float()
        if waveform.dim() == 1:
            waveform = waveform.unsqueeze(0)
        if waveform.shape[0] > 1:
            waveform = waveform.mean(dim=0, keepdim=True)
        if sample_rate != 24000:
            waveform = torchaudio.transforms.Resample(orig_freq=sample_rate, new_freq=24000)(waveform)
            sample_rate = 24000

        torchaudio.save(
            str(target_path),
            waveform,
            sample_rate,
            encoding="PCM_S",
            bits_per_sample=16,
        )
        return target_path

    @staticmethod
    def _load_audio_any_format(source_path: Path) -> tuple[torch.Tensor, int]:
        try:
            return torchaudio.load(str(source_path))
        except Exception:
            import av

            container = av.open(str(source_path))
            audio_stream = container.streams.audio[0]
            chunks: list[torch.Tensor] = []
            for frame in container.decode(audio_stream):
                array = frame.to_ndarray()
                tensor = torch.from_numpy(array)
                if tensor.dim() == 1:
                    tensor = tensor.unsqueeze(0)
                chunks.append(tensor)

            if not chunks:
                raise RuntimeError(f"No audio frames decoded from: {source_path}")

            waveform = torch.cat(chunks, dim=1).float()
            if waveform.abs().max().item() > 1.5:
                waveform = waveform / 32768.0
            return waveform, int(audio_stream.rate)

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
                    os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")
                    original_is_available = torch.cuda.is_available
                    torch.cuda.is_available = lambda: False

                cls._inject_cosyvoice_paths()
                from cosyvoice.cli.cosyvoice import AutoModel

                model = AutoModel(model_dir=resolved_model_ref)
                _cosyvoice_models[cache_key] = model
                return model
            finally:
                if original_is_available is not None:
                    torch.cuda.is_available = original_is_available

    @classmethod
    def _select_runtime_device(cls, requested: str) -> str:
        requested_norm = (requested or "auto").strip().lower()
        if requested_norm in {"cuda", "cpu"}:
            return requested_norm
        return "cuda" if torch.cuda.is_available() else "cpu"

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
            raw = DEFAULT_MODEL_DIR

        candidate = Path(raw)
        if candidate.is_absolute() and candidate.exists():
            return str(candidate)

        local_candidate = (cls._project_root() / candidate).resolve()
        if local_candidate.exists():
            return str(local_candidate)

        return raw

    @staticmethod
    def _resolve_backend(raw: str) -> str:
        backend = (raw or "local").strip().lower()
        if backend in {"local", "modelscope_api"}:
            return backend
        raise ValueError(f"Unsupported TTS backend: {raw}")
