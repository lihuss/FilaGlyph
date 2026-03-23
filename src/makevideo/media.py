from __future__ import annotations

import random
import shutil
import subprocess

from pathlib import Path

from moviepy import AudioFileClip, CompositeAudioClip, VideoFileClip, afx

from .config import PROJECT_ROOT, AUDIO_EXTENSIONS


def probe_media_duration(media_path: Path) -> float:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(media_path),
    ]
    completed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    raw = (completed.stdout or "").strip()
    return max(0.0, float(raw))


def escape_concat_path(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/").replace("'", "'\\''")


def merge_segments_with_ffmpeg(segment_paths: list[Path], tmp_dir: Path, merged_output: Path) -> None:
    concat_file = tmp_dir / "concat_list.txt"
    concat_content = "\n".join([f"file '{escape_concat_path(path)}'" for path in segment_paths]) + "\n"
    concat_file.write_text(concat_content, encoding="utf-8")

    command = [
        "ffmpeg",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_file),
        "-c",
        "copy",
        str(merged_output),
    ]
    subprocess.run(command, check=True, cwd=PROJECT_ROOT)
    concat_file.unlink(missing_ok=True)


def pick_random_music(musics_dir: Path) -> Path | None:
    if not musics_dir.exists() or not musics_dir.is_dir():
        return None

    music_files = [
        path
        for path in musics_dir.iterdir()
        if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS
    ]
    if not music_files:
        return None

    return random.choice(music_files)


def retime_video_to_target_duration(
    video_path: Path,
    target_duration: float,
    fps: int,
) -> float:
    if target_duration <= 0:
        return probe_media_duration(video_path)

    current = probe_media_duration(video_path)
    if current <= 0.02:
        return current

    factor = max(0.02, target_duration / current)
    if abs(factor - 1.0) < 0.005:
        return current

    parent_dir = video_path.parent
    retimed_path = parent_dir / f"{video_path.stem}.retimed.mp4"
    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
        "-an",
        "-vf",
        f"setpts=PTS*{factor:.8f}",
        "-r",
        str(fps),
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        str(retimed_path),
    ]
    subprocess.run(command, check=True, cwd=PROJECT_ROOT)
    shutil.move(str(retimed_path), str(video_path))
    return probe_media_duration(video_path)


def mix_video_with_audio(
    video_path: Path,
    narration_audio_path: Path,
    args,
    bgm_path: Path | None,
    tmp_dir: Path,
) -> None:
    temp_output = tmp_dir / f"{video_path.stem}.dub_tmp.mp4"

    video_clip = None
    narration_clip = None
    narration_track = None
    bgm_clip = None
    final_video = None

    try:
        video_clip = VideoFileClip(str(video_path))

        narration_clip = AudioFileClip(str(narration_audio_path)).with_effects([
            afx.AudioNormalize(),
            afx.MultiplyVolume(max(0.1, args.tts_gain)),
        ])

        if narration_clip.duration > video_clip.duration:
            narration_clip = narration_clip.subclipped(0, video_clip.duration)

        narration_track = CompositeAudioClip([narration_clip.with_start(0)]).with_duration(video_clip.duration)
        tracks = [narration_track]

        if bgm_path is not None and bgm_path.exists() and bgm_path.is_file():
            bgm_clip = AudioFileClip(str(bgm_path))
            if bgm_clip.duration < video_clip.duration:
                bgm_clip = bgm_clip.with_effects([afx.AudioLoop(duration=video_clip.duration)])
            else:
                bgm_clip = bgm_clip.subclipped(0, video_clip.duration)
            bgm_clip = bgm_clip.with_effects([afx.MultiplyVolume(max(0.0, args.bgm_volume))])
            tracks.append(bgm_clip)

        mixed_audio = tracks[0] if len(tracks) == 1 else CompositeAudioClip(tracks).with_duration(video_clip.duration)

        final_video = video_clip.with_audio(mixed_audio)
        final_video.write_videofile(
            str(temp_output),
            fps=args.fps,
            codec="libx264",
            audio_codec="aac",
            logger="bar",
        )

        final_video.close()
        final_video = None
        video_clip.close()
        video_clip = None
        if narration_clip is not None:
            narration_clip.close()
            narration_clip = None
        if narration_track is not None:
            narration_track.close()
            narration_track = None
        if bgm_clip is not None:
            bgm_clip.close()
            bgm_clip = None

        shutil.move(str(temp_output), str(video_path))
    finally:
        if final_video is not None:
            final_video.close()
        if video_clip is not None:
            video_clip.close()
        if narration_clip is not None:
            narration_clip.close()
        if narration_track is not None:
            narration_track.close()
        if bgm_clip is not None:
            bgm_clip.close()
        if temp_output.exists():
            temp_output.unlink(missing_ok=True)
