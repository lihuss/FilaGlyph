from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_PATH = PROJECT_ROOT / "src"
AGENT_RUNS_DIR = PROJECT_ROOT / "outputs" / "agent_runs"
SCENE_BUFFER_SECONDS = 0.5
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"}
