from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


def _sanitize_manim_compat(source: str) -> tuple[str, int]:
    """Normalize generated code for the Manim version used by this project.

    Current compatibility fix: ``Mobject.next_to`` in this runtime does not
    accept ``alignment_edge``. Remove this keyword to prevent runtime TypeError.
    Some runtimes also fail on ``GrowArrow`` due to unsupported ``scale_tips``
    handling; rewrite to ``Create`` for broader compatibility.
    """
    pattern = re.compile(r",\s*alignment_edge\s*=\s*[^,\)\n]+")
    sanitized, count_align = pattern.subn("", source)
    sanitized, count_grow = re.subn(r"\bGrowArrow\s*\(", "Create(", sanitized)
    return sanitized, int(count_align + count_grow)


@dataclass
class AnimatorCodegenResult:
    raw_text: str
    files: list[str]
    scene_indices: list[int]
    manifest_path: Path
    summary: str


def _extract_code_block(block_text: str) -> str:
    text = block_text.strip()
    if not text:
        return ""

    # Some model outputs include UTF-8 BOM at the start of a FILE block.
    # Keep generated Python source ASCII-clean for downstream AST parsing.
    text = text.lstrip("\ufeff")

    # Some model outputs wrap each FILE block in markdown fences and may leave
    # dangling fence lines between adjacent FILE sections. Remove standalone
    # fence lines globally instead of relying on a single regex pair.
    lines = text.splitlines()
    lines = [ln for ln in lines if not ln.strip().startswith("```")]

    cleaned = "\n".join(lines).strip().lstrip("\ufeff")
    if not cleaned:
        return ""
    return cleaned + "\n"


def _parse_file_blocks(raw_text: str) -> list[tuple[str, str]]:
    pattern = re.compile(r"^FILE:\s*(.+?)\s*$", flags=re.MULTILINE)
    matches = list(pattern.finditer(raw_text))
    blocks: list[tuple[str, str]] = []
    for idx, match in enumerate(matches):
        path_text = match.group(1).strip().replace("\\", "/")
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(raw_text)
        code_text = raw_text[start:end].strip()
        if not path_text:
            continue
        blocks.append((path_text, _extract_code_block(code_text)))
    return blocks


def _validate_codegen_path(path_text: str) -> str:
    normalized = path_text.strip().replace("\\", "/")
    if not normalized.startswith("animator_codegen/"):
        raise ValueError(f"Animator output path must start with animator_codegen/: {path_text}")
    if normalized.endswith("/"):
        raise ValueError(f"Animator output path cannot be a directory: {path_text}")
    if ".." in normalized.split("/"):
        raise ValueError(f"Animator output path cannot contain '..': {path_text}")
    return normalized


def _build_scene_wrapper(scene_index: int) -> str:
    return (
        "from pathlib import Path\n"
        "import sys\n\n"
        "scene_dir = Path(__file__).resolve().parent\n"
        "scene_parent = scene_dir.parent\n"
        "if str(scene_parent) not in sys.path:\n"
        "    sys.path.insert(0, str(scene_parent))\n\n"
        "from base_scene import *\n"
        f"from animator_codegen.scene{scene_index}_anim import apply_scene{scene_index}_animation\n\n"
        f"class Scene{scene_index}(PhysicsProblemDiagram):\n"
        "    def construct(self):\n"
        "        super().construct()\n"
        f"        apply_scene{scene_index}_animation(self)\n"
    )


def write_animator_codegen(runtime_dir: Path, raw_text: str) -> AnimatorCodegenResult:
    runtime_dir.mkdir(parents=True, exist_ok=True)
    blocks = _parse_file_blocks(raw_text)
    if not blocks:
        raise ValueError("Animator output does not contain any FILE: blocks")

    written_files: list[str] = []
    scene_indices: list[int] = []
    compat_fixes = 0

    for rel_path_raw, code in blocks:
        rel_path = _validate_codegen_path(rel_path_raw)
        out_file = (runtime_dir / rel_path).resolve()
        runtime_root = runtime_dir.resolve()
        if not str(out_file).startswith(str(runtime_root)):
            raise ValueError(f"Animator output path escapes runtime: {rel_path}")
        if rel_path.endswith(".py"):
            code, fix_count = _sanitize_manim_compat(code)
            compat_fixes += fix_count
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text(code, encoding="utf-8")
        written_files.append(str(out_file.relative_to(runtime_dir)).replace("\\", "/"))

        scene_match = re.match(r"animator_codegen/scene(\d+)_anim\.py$", rel_path)
        if scene_match:
            scene_indices.append(int(scene_match.group(1)))

    scene_indices = sorted(set(scene_indices))
    if not scene_indices:
        raise ValueError("Animator output must include animator_codegen/scene{index}_anim.py files")

    # Ensure package import works for wrappers.
    init_file = runtime_dir / "animator_codegen" / "__init__.py"
    if not init_file.exists():
        init_file.write_text("", encoding="utf-8")
        written_files.append("animator_codegen/__init__.py")

    wrappers: dict[str, str] = {}
    for idx in scene_indices:
        wrapper_path = runtime_dir / f"scene{idx}" / f"scene{idx}.py"
        wrapper_path.parent.mkdir(parents=True, exist_ok=True)
        wrapper_code = _build_scene_wrapper(idx)
        wrapper_path.write_text(wrapper_code, encoding="utf-8")
        wrappers[str(idx)] = str(wrapper_path.relative_to(runtime_dir)).replace("\\", "/")
        written_files.append(wrappers[str(idx)])

    manifest = {
        "mode": "animator_direct_codegen_v1",
        "scene_indices": scene_indices,
        "anim_files": [f"animator_codegen/scene{i}_anim.py" for i in scene_indices],
        "wrappers": wrappers,
    }
    manifest_path = runtime_dir / "animator_codegen" / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    written_files.append("animator_codegen/manifest.json")

    summary = (
        "Animator 代码包已落盘。\n"
        f"- scenes: {', '.join(str(i) for i in scene_indices)}\n"
        f"- manifest: {manifest_path.relative_to(runtime_dir).as_posix()}\n"
        f"- files: {len(written_files)}\n"
        f"- compat_fixes: {compat_fixes}"
    )

    return AnimatorCodegenResult(
        raw_text=raw_text,
        files=sorted(set(written_files)),
        scene_indices=scene_indices,
        manifest_path=manifest_path,
        summary=summary,
    )
