from __future__ import annotations

import shutil

from pathlib import Path


def ensure_run_workspace(run_dir: Path) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)


def cleanup_runtime_artifacts(run_dir: Path) -> None:
    (run_dir / "concat_list.txt").unlink(missing_ok=True)
    (run_dir / "merged_video.mp4").unlink(missing_ok=True)
    (run_dir / "narration_full.wav").unlink(missing_ok=True)
    for temp_video in run_dir.glob("*.dub_tmp.mp4"):
        temp_video.unlink(missing_ok=True)

    for scene_dir in run_dir.glob("scene*"):
        if not scene_dir.is_dir():
            continue

        for generated in scene_dir.glob("segment_*.mp4"):
            generated.unlink(missing_ok=True)
        for generated in scene_dir.glob("narration_scene_*.wav"):
            generated.unlink(missing_ok=True)
        for generated in scene_dir.glob("narration_scene_*.txt"):
            generated.unlink(missing_ok=True)

        scene_tts_tmp = scene_dir / "tts_tmp"
        if scene_tts_tmp.exists():
            shutil.rmtree(scene_tts_tmp, ignore_errors=True)

        scene_media_dir = scene_dir / "videos"
        if scene_media_dir.exists():
            shutil.rmtree(scene_media_dir, ignore_errors=True)

        scene_tex_dir = scene_dir / "Tex"
        if scene_tex_dir.exists():
            shutil.rmtree(scene_tex_dir, ignore_errors=True)

        scene_text_dir = scene_dir / "text"
        if scene_text_dir.exists():
            shutil.rmtree(scene_text_dir, ignore_errors=True)

        scene_texts_dir = scene_dir / "texts"
        if scene_texts_dir.exists():
            shutil.rmtree(scene_texts_dir, ignore_errors=True)


def cleanup_success_inputs(
    scene_jobs: list[tuple[int, Path, str]],
    narration_source: Path | None,
    run_dir: Path,
) -> None:
    run_root = run_dir.resolve()
    candidates: list[Path] = []

    for _, scene_file, _ in scene_jobs:
        scene_file = scene_file.resolve()
        try:
            scene_file.relative_to(run_root)
            if scene_file.suffix.lower() == ".py":
                candidates.append(scene_file)
        except ValueError:
            continue

    for path in candidates:
        path.unlink(missing_ok=True)
