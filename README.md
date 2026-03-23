# FilaGlyph

FilaGlyph is the Manim teaching-video pipeline project.

## Scope
- Render multiple Manim scenes
- Generate one-pass narration audio (optional)
- Align scene timeline to narration duration
- Final muxing to MP4

## Entry
- `make_manim_video.py`

## Scene Worker Scripts
- `src/make_manim_scene.py`
- `src/make_manim_dub.py`

## Install

```powershell
pip install "setuptools<81.0.0"
pip install openai-whisper==20231117 --no-build-isolation
pip install -r requirements.txt
```

## Quick Start
```powershell
venv\Scripts\python.exe make_manim_video.py --tts-script-file tmp\narration.txt --scene-files tmp\scene1.py,tmp\scene2.py --scene-names Scene1,Scene2 --output outputs\lesson.mp4 --no-bgm
```

## Notes
- This subproject is intentionally separated from text-only video generation.
- CosyVoice is resolved from `FilaGlyph/CosyVoice` first, then `../CosyVoice`.
