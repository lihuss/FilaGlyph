from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import List

from langchain_core.tools import tool

from .smart_layout_engine import validate_scene_formula_layout


def build_coder_tools(project_root: Path, render_options: dict | None = None, shared_state: dict | None = None):
    """Build the tool list for the coder agent.

    Args:
        shared_state: mutable dict shared with the caller.  Keys set by tools:
            - ``summary``: text written by the ``report_summary`` tool.
    """
    opts = render_options or {}
    _shared = shared_state if shared_state is not None else {}
    workflow_voice = str(opts.get("voice", "none")).strip()
    workflow_prompt_text = str(opts.get("prompt_text", "")).strip()
    workflow_bgm = str(opts.get("bgm_path", "")).strip()
    workflow_enable_multi = bool(opts.get("enable_multithread", False))
    workflow_tts_device = str(opts.get("tts_device", "auto")).strip()
    workflow_tts_backend = str(opts.get("tts_backend", "local")).strip() or "local"
    workflow_tts_api_base_url = str(opts.get("tts_api_base_url", "")).strip()
    workflow_tts_api_key = str(opts.get("tts_api_key", "")).strip()
    workflow_tts_api_timeout = float(opts.get("tts_api_timeout", 180.0))
    agent_run_dir_value = str(opts.get("agent_run_dir", "")).strip()
    agent_run_dir = Path(agent_run_dir_value) if agent_run_dir_value else None
    runtime_dir_value = str(opts.get("workflow_runtime_dir", "")).strip()
    if not runtime_dir_value:
        raise ValueError("Missing workflow_runtime_dir in render options")
    runtime_dir = Path(runtime_dir_value)
    runtime_dir.mkdir(parents=True, exist_ok=True)

    tmp_dir = runtime_dir
    tmp_dir.mkdir(parents=True, exist_ok=True)
    makevideo_debug_dir = tmp_dir / "makevideo_debug"
    makevideo_debug_dir.mkdir(parents=True, exist_ok=True)

    def _extract_error_output(text: str) -> str:
        if not text:
            return ""
        markers = [
            "Traceback (most recent call last):",
            "+--------------------- Traceback",
            "RuntimeError:",
            "ValueError:",
            "TypeError:",
        ]
        index = min((text.find(marker) for marker in markers if marker in text), default=-1)
        if index >= 0:
            return text[index:]
        return text

    def _primary_exception_segment(text: str) -> str:
        if not text:
            return ""
        marker = "During handling of the above exception"
        if marker in text:
            head = text.split(marker, 1)[0].strip()
            if head:
                return head
        return text

    def _extract_scene_index(text: str) -> str:
        match = re.search(r"index\s*=\s*(\d+)", text)
        return match.group(1) if match else ""

    def _extract_error_location(text: str) -> str:
        file_line_matches = re.findall(r'File "([^"]+)", line (\d+)', text)
        if not file_line_matches:
            return ""

        scene_level = []
        preferred = []
        fallback = []
        for path_str, line_str in file_line_matches:
            normalized = path_str.replace("\\", "/").lower()
            candidate = f"{path_str}:{line_str}"
            if re.search(r"/(runtime|tmp)/scene\d+/scene\d+\.py$", normalized) or re.search(r"/scene\d+\.py$", normalized):
                scene_level.append(candidate)
            if "/site-packages/" not in normalized and "python" not in normalized:
                # Avoid framework wrapper files that hide the actual scene error.
                if not (
                    normalized.endswith("/src/make_manim_scene.py")
                    or normalized.endswith("/makevideo.py")
                    or "/src/makevideo/" in normalized
                ):
                    preferred.append(candidate)
            fallback.append(candidate)

        if scene_level:
            return scene_level[-1]
        if preferred:
            return preferred[-1]
        return fallback[-1]

    def _extract_error_type_and_message(text: str) -> tuple[str, str]:
        error_lines = re.findall(r"^([A-Za-z_][\w\.]*Error):\s*(.+)$", text, flags=re.MULTILINE)
        if not error_lines:
            return "RuntimeError", "makevideo failed; inspect runtime_log_file"

        wrapper_errors = {"RuntimeError", "CalledProcessError"}
        noisy_secondary = {"UnicodeEncodeError"}

        # Prefer concrete root causes over wrapper/secondary errors.
        for err_type, err_message in reversed(error_lines):
            if err_type not in wrapper_errors and err_type not in noisy_secondary:
                return err_type, err_message.strip()

        for err_type, err_message in reversed(error_lines):
            if err_type not in wrapper_errors:
                return err_type, err_message.strip()

        err_type, err_message = error_lines[-1]
        return err_type, err_message.strip()

    def _build_concise_error(stdout_text: str, stderr_text: str) -> dict:
        merged = "\n".join([stderr_text or "", stdout_text or ""]).strip()
        focused = _extract_error_output(merged)
        primary = _primary_exception_segment(focused)
        err_type, err_message = _extract_error_type_and_message(primary)
        location = _extract_error_location(primary)
        scene_index = _extract_scene_index(focused)

        # Manim rich traceback often embeds scene location as "sceneX.py:LINE"
        # without a standard Python traceback frame. Prefer that concrete location.
        rich_scene_match = re.search(r"scene(\d+)\.py:(\d+)", focused)
        if rich_scene_match:
            rich_scene_idx = int(rich_scene_match.group(1))
            rich_line = rich_scene_match.group(2)
            try:
                rich_scene_path = _scene_path(rich_scene_idx)
                location = f"{rich_scene_path}:{rich_line}"
            except Exception:
                location = f"scene{rich_scene_idx}.py:{rich_line}"
            scene_index = str(rich_scene_idx)

        summary_parts = [f"{err_type}: {err_message}"]
        if location:
            summary_parts.append(f"at {location}")
        if scene_index:
            summary_parts.append(f"scene index={scene_index}")

        return {
            "error_type": err_type,
            "error_message": err_message,
            "error_location": location,
            "error_scene_index": scene_index,
            "error_summary": " | ".join(summary_parts),
        }

    def _scene_path(scene_index: int) -> Path:
        if scene_index < 1:
            raise ValueError("scene_index must be >= 1")
        scene_dir = tmp_dir / f"scene{scene_index}"
        return scene_dir / f"scene{scene_index}.py"

    def _scene_files() -> List[Path]:
        files = sorted(
            [
                p
                for p in tmp_dir.glob("scene*/scene*.py")
                if re.fullmatch(r"scene\d+\.py", p.name)
            ],
            key=lambda p: int(re.search(r"(\d+)", p.stem).group(1)),
        )
        return files

    def _scene_index_from_path(path: Path) -> int:
        match = re.search(r"scene(\d+)\.py$", str(path).replace("\\", "/"))
        if not match:
            raise ValueError(f"Invalid scene file name: {path}")
        return int(match.group(1))

    def _infer_scene_class_name(path: Path) -> str:
        source = path.read_text(encoding="utf-8")
        module = ast.parse(source, filename=str(path))
        candidate_names: list[str] = []
        fallback_scene_like: list[str] = []
        fallback_with_construct: list[str] = []
        for node in module.body:
            if not isinstance(node, ast.ClassDef):
                continue
            if re.match(r"^Scene\d*$", node.name):
                fallback_scene_like.append(node.name)
            if any(isinstance(m, ast.FunctionDef) and m.name == "construct" for m in node.body):
                fallback_with_construct.append(node.name)
            for base in node.bases:
                if isinstance(base, ast.Name) and base.id == "Scene":
                    return node.name
                if isinstance(base, ast.Attribute) and base.attr == "Scene":
                    return node.name
                if isinstance(base, ast.Name) and base.id.endswith("Scene"):
                    candidate_names.append(node.name)
        if candidate_names:
            return candidate_names[0]
        if fallback_scene_like:
            return fallback_scene_like[0]
        if fallback_with_construct:
            return fallback_with_construct[0]
        raise ValueError(f"Could not infer Manim scene class name from: {path}")

    def _safe_runtime_rel_path(path: str) -> Path:
        clean = path.replace("\\", "/").strip()
        if not clean:
            raise ValueError("Path cannot be empty")
        if clean.startswith("/"):
            clean = clean.lstrip("/")
        candidate = (tmp_dir / clean).resolve()
        runtime_root = tmp_dir.resolve()
        if not str(candidate).startswith(str(runtime_root)):
            raise ValueError("Path must stay inside current runtime workspace")
        return candidate

    def _sanitize_narration_text(raw: str) -> tuple[str, int]:
        text = (raw or "").replace("\r\n", "\n")
        lines = text.split("\n")
        cleaned: list[str] = []
        removed = 0

        # Strip scene heading labels that should never be spoken by TTS.
        scene_label_only = re.compile(
            r"^\s*(?:scene|scence|场景|第\s*\d+\s*幕|幕)\s*[-_#：: ]*\d*\s*[-_：: ]*\s*$",
            flags=re.IGNORECASE,
        )
        scene_label_prefix = re.compile(
            r"^\s*(?:scene|scence|场景)\s*\d+\s*[-_：: ]+\s*(.+?)\s*$",
            flags=re.IGNORECASE,
        )

        for line in lines:
            stripped = line.strip()
            if not stripped:
                cleaned.append("")
                continue

            if scene_label_only.match(stripped):
                removed += 1
                continue

            prefix_match = scene_label_prefix.match(stripped)
            if prefix_match:
                content = prefix_match.group(1).strip()
                if content:
                    cleaned.append(content)
                removed += 1
                continue

            cleaned.append(stripped)

        output = "\n".join(cleaned)
        output = re.sub(r"\n{3,}", "\n\n", output).strip()
        if output:
            output += "\n"
        return output, removed

    def _validate_all_scene_layout(scene_files: List[Path]) -> tuple[list[dict], list[dict], list[dict]]:
        results = []
        all_violations = []
        runtime_errors = []
        for path in scene_files:
            scene_class = _infer_scene_class_name(path)
            try:
                result = validate_scene_formula_layout(path, scene_class)
            except Exception as exc:
                error_msg = f"{type(exc).__name__}: {exc}"
                result = {
                    "ok": False,
                    "scene_file": str(path),
                    "scene_class": scene_class,
                    "formula_count": 0,
                    "violations": [],
                    "runtime_error": True,
                    "error": error_msg,
                }
                runtime_errors.append(
                    {
                        "scene_file": str(path),
                        "scene_class": scene_class,
                        "error": error_msg,
                    }
                )
            results.append(result)
            if not result.get("ok", True):
                all_violations.extend(result.get("violations", []))
        return results, all_violations, runtime_errors

    def _coerce_float(value: object, fallback: float) -> float:
        try:
            return float(value)
        except Exception:
            return fallback

    def _normalize_latex_text(latex: str) -> str:
        text = str(latex or "").strip()
        if not text:
            return text
        # Collapse duplicated command slashes from JSON-escaped fragments:
        # \\frac -> \frac, \\theta -> \theta, etc.
        text = re.sub(r"\\{2,}([A-Za-z]+)", r"\\\1", text)
        # Normalize common invalid superscript forms like 45^\circ -> 45^{\circ}
        text = re.sub(r"\^\\([A-Za-z]+)", r"^{\\\1}", text)
        text = re.sub(r"\^([A-Za-z]+)", r"^{\1}", text)
        return text

    def _normalize_formula_events(spec: dict) -> list[dict]:
        raw_events = spec.get("events", [])
        if not isinstance(raw_events, list) or not raw_events:
            raise ValueError("events must be a non-empty list")

        normalized: list[dict] = []
        default_gap = _coerce_float(spec.get("default_gap", 1.1), 1.1)
        default_hold = _coerce_float(spec.get("default_hold", 1.6), 1.6)

        for idx, item in enumerate(raw_events):
            if isinstance(item, str):
                latex = _normalize_latex_text(item.strip())
                item_obj = {}
            elif isinstance(item, dict):
                latex = _normalize_latex_text(str(item.get("latex", item.get("tex", ""))).strip())
                item_obj = item
            else:
                raise ValueError(f"events[{idx}] must be object or string")

            if not latex:
                raise ValueError(f"events[{idx}] missing latex")

            fallback_start = idx * default_gap
            start = _coerce_float(item_obj.get("at", item_obj.get("start", fallback_start)), fallback_start)
            end_raw = item_obj.get("off", item_obj.get("end"))
            hold = _coerce_float(item_obj.get("hold", default_hold), default_hold)
            if end_raw is None:
                end = start + max(0.4, hold)
            else:
                end = _coerce_float(end_raw, start + max(0.4, hold))
            if end <= start:
                end = start + max(0.4, hold)

            normalized.append(
                {
                    "name": f"f{idx + 1}",
                    "latex": latex,
                    "start": round(float(start), 4),
                    "end": round(float(end), 4),
                    "style": str(item_obj.get("style", "math")).strip().lower(),
                    "color": str(item_obj.get("color", "WHITE")).strip(),
                    "z_index": int(item_obj.get("z_index", 30 + idx)),
                }
            )

        normalized.sort(key=lambda it: (it["start"], it["end"], it["name"]))
        return normalized

    def _build_formula_layout_plan(events: list[dict], frame_width: float, frame_height: float) -> tuple[list[dict], dict]:
        anchors = [
            [4.2, 2.5, 0.0],
            [4.1, 0.8, 0.0],
            [4.0, -0.9, 0.0],
            [3.8, -2.4, 0.0],
            [2.9, 2.6, 0.0],
            [2.8, -2.5, 0.0],
        ]
        for idx, ev in enumerate(events):
            ev["preferred_pos"] = anchors[idx % len(anchors)]
            ev["scale"] = float(ev.get("scale", 1.0) or 1.0)

        frame = {
            "left": round(float(-frame_width / 2), 4),
            "right": round(float(frame_width / 2), 4),
            "top": round(float(frame_height / 2), 4),
            "bottom": round(float(-frame_height / 2), 4),
            "width": round(float(frame_width), 4),
            "height": round(float(frame_height), 4),
            "safe_margin": 0.3,
            "safe_left": round(float(-frame_width / 2 + 0.3), 4),
            "safe_right": round(float(frame_width / 2 - 0.3), 4),
            "safe_top": round(float(frame_height / 2 - 0.3), 4),
            "safe_bottom": round(float(-frame_height / 2 + 0.3), 4),
            "cells_per_unit": 20,
            "buffer_padding": 0.22,
            "min_formula_scale": 0.16,
            "formula_panel_left": round(float(-frame_width / 2 + frame_width * 0.58), 4),
            "main_ratio_target": 2.0 / 3.0,
            "main_ratio_min": 0.5,
            "left_anchor_margin": 0.25,
        }
        return events, frame

    def _render_formula_layout_snippet(scene_index: int, events: list[dict], frame: dict) -> str:
        event_json = json.dumps(events, ensure_ascii=False, indent=2)
        frame_json = json.dumps(frame, ensure_ascii=False, indent=2)
        return (
            "# AUTO_FORMULA_LAYOUT_PLAN\n"
            f"_FORMULA_EVENTS = {event_json}\n"
            f"_FORMULA_FRAME = {frame_json}\n\n"
            "def _bbox(mob):\n"
            "    return (\n"
            "        float(mob.get_left()[0]),\n"
            "        float(mob.get_right()[0]),\n"
            "        float(mob.get_top()[1]),\n"
            "        float(mob.get_bottom()[1]),\n"
            "    )\n"
            "\n"
            "def _xy_to_rc(x, y, rows, cols, cells_per_unit):\n"
            "    left = float(_FORMULA_FRAME['left'])\n"
            "    top = float(_FORMULA_FRAME['top'])\n"
            "    c = int((float(x) - left) * cells_per_unit)\n"
            "    r = int((top - float(y)) * cells_per_unit)\n"
            "    r = max(0, min(rows - 1, r))\n"
            "    c = max(0, min(cols - 1, c))\n"
            "    return r, c\n"
            "\n"
            "def _mark_bbox(grid, bbox, cells_per_unit):\n"
            "    left, right, top, bottom = bbox\n"
            "    r0, c0 = _xy_to_rc(left, top, grid.shape[0], grid.shape[1], cells_per_unit)\n"
            "    r1, c1 = _xy_to_rc(right, bottom, grid.shape[0], grid.shape[1], cells_per_unit)\n"
            "    if r1 < r0:\n"
            "        r0, r1 = r1, r0\n"
            "    if c1 < c0:\n"
            "        c0, c1 = c1, c0\n"
            "    grid[r0:r1+1, c0:c1+1] = 1\n"
            "\n"
            "def _dilate(grid, pad_cells):\n"
            "    pad_cells = int(max(0, pad_cells))\n"
            "    if pad_cells <= 0:\n"
            "        return grid\n"
            "    padded = np.pad(grid.astype(bool), ((pad_cells, pad_cells), (pad_cells, pad_cells)), mode='constant')\n"
            "    out = np.zeros_like(grid, dtype=bool)\n"
            "    for dr in range(-pad_cells, pad_cells + 1):\n"
            "        for dc in range(-pad_cells, pad_cells + 1):\n"
            "            if dr * dr + dc * dc > pad_cells * pad_cells:\n"
            "                continue\n"
            "            out |= padded[pad_cells + dr:pad_cells + dr + grid.shape[0], pad_cells + dc:pad_cells + dc + grid.shape[1]]\n"
            "    return out.astype(np.uint8)\n"
            "\n"
            "def _integral(grid):\n"
            "    sat = np.cumsum(np.cumsum(grid.astype(np.int32), axis=0), axis=1)\n"
            "    return np.pad(sat, ((1, 0), (1, 0)), mode='constant')\n"
            "\n"
            "def _find_safe_pos(target, obstacles, preferred, buffer_padding):\n"
            "    cells_per_unit = int(_FORMULA_FRAME.get('cells_per_unit', 20))\n"
            "    width = float(_FORMULA_FRAME['width'])\n"
            "    height = float(_FORMULA_FRAME['height'])\n"
            "    rows = max(8, int(round(height * cells_per_unit)))\n"
            "    cols = max(8, int(round(width * cells_per_unit)))\n"
            "    occ = np.zeros((rows, cols), dtype=np.uint8)\n"
            "    for mob in obstacles:\n"
            "        try:\n"
            "            _mark_bbox(occ, _bbox(mob), cells_per_unit)\n"
            "        except Exception:\n"
            "            continue\n"
            "    occ = _dilate(occ, int(round(float(buffer_padding) * cells_per_unit)))\n"
            "    ii = _integral(occ)\n"
            "\n"
            "    tw = max(1, int(np.ceil(float(target.width) * cells_per_unit)))\n"
            "    th = max(1, int(np.ceil(float(target.height) * cells_per_unit)))\n"
            "    max_r = rows - th\n"
            "    max_c = cols - tw\n"
            "    if max_r < 0 or max_c < 0:\n"
            "        return None\n"
            "\n"
            "    rr = np.arange(0, max_r + 1, dtype=np.int32)[:, None]\n"
            "    cc = np.arange(0, max_c + 1, dtype=np.int32)[None, :]\n"
            "    sums = ii[rr + th, cc + tw] - ii[rr, cc + tw] - ii[rr + th, cc] + ii[rr, cc]\n"
            "    valid = np.argwhere(sums == 0)\n"
            "    if valid.size == 0:\n"
            "        return None\n"
            "\n"
            "    pref = np.asarray(preferred, dtype=float) if preferred is not None else np.array([0.0, 0.0, 0.0], dtype=float)\n"
            "    left = float(_FORMULA_FRAME['left'])\n"
            "    top = float(_FORMULA_FRAME['top'])\n"
            "    centers_r = valid[:, 0].astype(float) + th / 2.0\n"
            "    centers_c = valid[:, 1].astype(float) + tw / 2.0\n"
            "    x = left + (centers_c + 0.5) / cells_per_unit\n"
            "    y = top - (centers_r + 0.5) / cells_per_unit\n"
            "    half_w = float(target.width) / 2.0\n"
            "    half_h = float(target.height) / 2.0\n"
            "    safe_margin = float(_FORMULA_FRAME.get('safe_margin', 0.3))\n"
            "    safe_left = float(_FORMULA_FRAME.get('safe_left', float(_FORMULA_FRAME['left']) + safe_margin))\n"
            "    safe_right = float(_FORMULA_FRAME.get('safe_right', float(_FORMULA_FRAME['right']) - safe_margin))\n"
            "    safe_top = float(_FORMULA_FRAME.get('safe_top', float(_FORMULA_FRAME['top']) - safe_margin))\n"
            "    safe_bottom = float(_FORMULA_FRAME.get('safe_bottom', float(_FORMULA_FRAME['bottom']) + safe_margin))\n"
            "    bounds_mask = (x - half_w >= safe_left) & (x + half_w <= safe_right) & (y + half_h <= safe_top) & (y - half_h >= safe_bottom)\n"
            "    if np.any(bounds_mask):\n"
            "        x = x[bounds_mask]\n"
            "        y = y[bounds_mask]\n"
            "    else:\n"
            "        return None\n"
            "    panel_left = float(_FORMULA_FRAME.get('formula_panel_left', left + width * 0.58))\n"
            "    panel_mask = x >= panel_left\n"
            "    if np.any(panel_mask):\n"
            "        x_sel = x[panel_mask]\n"
            "        y_sel = y[panel_mask]\n"
            "    else:\n"
            "        x_sel = x\n"
            "        y_sel = y\n"
            "    dist2 = (x_sel - float(pref[0])) ** 2 + (y_sel - float(pref[1])) ** 2\n"
            "    idx = int(np.argmin(dist2))\n"
            "    return np.array([x_sel[idx], y_sel[idx], 0.0], dtype=float)\n"
            "\n"
            "def _candidate_points(preferred):\n"
            "    pref = np.asarray(preferred, dtype=float) if preferred is not None else np.array([0.0, 0.0, 0.0], dtype=float)\n"
            "    px = float(pref[0]) if pref.size > 0 else 0.0\n"
            "    py = float(pref[1]) if pref.size > 1 else 0.0\n"
            "    candidates = [\n"
            "        np.array([px, py, 0.0], dtype=float),\n"
            "        np.array([-px, py, 0.0], dtype=float),\n"
            "        np.array([px, -py, 0.0], dtype=float),\n"
            "        np.array([-px, -py, 0.0], dtype=float),\n"
            "        np.array([0.0, py, 0.0], dtype=float),\n"
            "        np.array([0.0, -py, 0.0], dtype=float),\n"
            "        np.array([0.0, 0.0, 0.0], dtype=float),\n"
            "    ]\n"
            "    return candidates\n"
            "\n"
            "def _scene_bbox(mobjects):\n"
            "    xs = []\n"
            "    ys = []\n"
            "    for mob in mobjects:\n"
            "        try:\n"
            "            xs.extend([float(mob.get_left()[0]), float(mob.get_right()[0])])\n"
            "            ys.extend([float(mob.get_bottom()[1]), float(mob.get_top()[1])])\n"
            "        except Exception:\n"
            "            continue\n"
            "    if not xs or not ys:\n"
            "        return None\n"
            "    return min(xs), max(xs), min(ys), max(ys)\n"
            "\n"
            "def _norm_formula_text(text):\n"
            "    return ''.join(str(text or '').split())\n"
            "\n"
            "def _mob_formula_text(mob):\n"
            "    if hasattr(mob, 'tex_string'):\n"
            "        return str(getattr(mob, 'tex_string') or '')\n"
            "    if hasattr(mob, 'tex_strings'):\n"
            "        maybe = getattr(mob, 'tex_strings')\n"
            "        if isinstance(maybe, (list, tuple)):\n"
            "            return ''.join(str(x) for x in maybe)\n"
            "    return ''\n"
            "\n"
            "def _clear_preexisting_event_formulas(scene):\n"
            "    event_texts = []\n"
            "    for item in _FORMULA_EVENTS:\n"
            "        if isinstance(item, dict):\n"
            "            t = _norm_formula_text(item.get('latex', ''))\n"
            "            if t:\n"
            "                event_texts.append(t)\n"
            "    if not event_texts:\n"
            "        return\n"
            "\n"
            "    to_remove = []\n"
            "    for mob in list(scene.mobjects):\n"
            "        if bool(getattr(mob, '_fg_formula_registry_key', '')):\n"
            "            continue\n"
            "        if not isinstance(mob, (Tex, MathTex)):\n"
            "            continue\n"
            "        mob_text = _norm_formula_text(_mob_formula_text(mob))\n"
            "        if not mob_text:\n"
            "            continue\n"
            "        if any((ev == mob_text) or (len(mob_text) >= 8 and (ev in mob_text or mob_text in ev)) for ev in event_texts):\n"
            "            to_remove.append(mob)\n"
            "\n"
            "    if to_remove:\n"
            "        scene.remove(*to_remove)\n"
            "\n"
            "def _prepare_scene_formula_space(scene):\n"
            "    non_formula = [m for m in list(scene.mobjects) if not bool(getattr(m, '_fg_formula_registry_key', ''))]\n"
            "    main_for_bbox = [m for m in non_formula if not isinstance(m, (Tex, MathTex))]\n"
            "    box = _scene_bbox(main_for_bbox if main_for_bbox else non_formula)\n"
            "    if box is None:\n"
            "        return\n"
            "    left, right, bottom, top = box\n"
            "    width = max(1e-6, right - left)\n"
            "    height = max(1e-6, top - bottom)\n"
            "\n"
            "    frame_w = float(_FORMULA_FRAME.get('width', 14.222))\n"
            "    frame_h = float(_FORMULA_FRAME.get('height', 8.0))\n"
            "    frame_left = float(_FORMULA_FRAME.get('left', -frame_w / 2.0))\n"
            "    frame_right = float(_FORMULA_FRAME.get('right', frame_w / 2.0))\n"
            "    formula_panel_left = float(_FORMULA_FRAME.get('formula_panel_left', frame_left + frame_w * 0.58))\n"
            "\n"
            "    desired_main_ratio = float(_FORMULA_FRAME.get('main_ratio_target', 2.0 / 3.0))\n"
            "    min_main_ratio = float(_FORMULA_FRAME.get('main_ratio_min', 0.5))\n"
            "    left_margin = float(_FORMULA_FRAME.get('left_anchor_margin', 0.25))\n"
            "    reserve_right = max(frame_right - formula_panel_left + 0.15, frame_w * max(0.0, min(0.49, 1.0 - desired_main_ratio)))\n"
            "\n"
            "    def _shift_all(dx, dy):\n"
            "        vec = np.array([float(dx), float(dy), 0.0], dtype=float)\n"
            "        for mob in non_formula:\n"
            "            try:\n"
            "                mob.shift(vec)\n"
            "            except Exception:\n"
            "                continue\n"
            "\n"
            "    # Step 1: left-anchor main diagram first (no scaling).\n"
            "    dx_left = (frame_left + left_margin) - left\n"
            "    if abs(dx_left) > 1e-6:\n"
            "        _shift_all(dx_left, 0.0)\n"
            "\n"
            "    box_after_anchor = _scene_bbox(non_formula)\n"
            "    if box_after_anchor is None:\n"
            "        return\n"
            "    l2, r2, b2, t2 = box_after_anchor\n"
            "\n"
            "    # If right-side room is enough, do not shrink.\n"
            "    if (frame_right - r2) >= reserve_right:\n"
            "        cy = (b2 + t2) / 2.0\n"
            "        if abs(cy) > 1e-6:\n"
            "            _shift_all(0.0, -cy)\n"
            "        return\n"
            "\n"
            "    # Step 2: shrink only when needed; keep main diagram around 2/3 width,\n"
            "    # and never shrink below 1/2 width unless geometry is impossible.\n"
            "    target_w = min(width, frame_w * desired_main_ratio)\n"
            "    panel_target_w = max(1e-6, formula_panel_left - (frame_left + left_margin) - 0.15)\n"
            "    target_w = min(target_w, panel_target_w)\n"
            "    min_w = frame_w * min_main_ratio\n"
            "    if target_w < min_w:\n"
            "        target_w = min_w\n"
            "    target_h = frame_h * 0.86\n"
            "\n"
            "    s = min(1.0, target_w / max(1e-6, width), target_h / max(1e-6, height))\n"
            "    if (width * s) < min_w:\n"
            "        s_floor = min(1.0, min_w / max(1e-6, width))\n"
            "        s = max(s, s_floor)\n"
            "\n"
            "    if s < 0.999:\n"
            "        for mob in non_formula:\n"
            "            try:\n"
            "                mob.scale(float(s), about_point=np.array([0.0, 0.0, 0.0]))\n"
            "            except Exception:\n"
            "                continue\n"
            "\n"
            "    box_final = _scene_bbox(non_formula)\n"
            "    if box_final is None:\n"
            "        return\n"
            "    lf, rf, bf, tf = box_final\n"
            "    dx_final = (frame_left + left_margin) - lf\n"
            "    cy_final = (bf + tf) / 2.0\n"
            "    if abs(dx_final) > 1e-6 or abs(cy_final) > 1e-6:\n"
            "        _shift_all(dx_final, -cy_final)\n"
            "\n"
            "def _place_formula_with_fallback(mob, obstacles, preferred, base_padding):\n"
            "    current_scale = float(getattr(mob, '_fg_scale', 1.0) or 1.0)\n"
            "    min_scale = float(_FORMULA_FRAME.get('min_formula_scale', 0.22))\n"
            "    scale_ladder = [\n"
            "        current_scale,\n"
            "        max(min_scale, current_scale * 0.92),\n"
            "        max(min_scale, current_scale * 0.84),\n"
            "        max(min_scale, current_scale * 0.76),\n"
            "        max(min_scale, current_scale * 0.68),\n"
            "        max(min_scale, current_scale * 0.60),\n"
            "        min_scale,\n"
            "    ]\n"
            "    dedup = []\n"
            "    for s in scale_ladder:\n"
            "        s = float(round(s, 4))\n"
            "        if s not in dedup:\n"
            "            dedup.append(s)\n"
            "    padding_ladder = [float(base_padding), max(0.12, float(base_padding) * 0.75), 0.08, 0.03, 0.0]\n"
            "\n"
            "    for s in dedup:\n"
            "        if s < current_scale - 1e-6 and current_scale > 1e-6:\n"
            "            mob.scale(float(s / current_scale))\n"
            "            current_scale = s\n"
            "            setattr(mob, '_fg_scale', current_scale)\n"
            "        for pad in padding_ladder:\n"
            "            for candidate in _candidate_points(preferred):\n"
            "                pos = _find_safe_pos(mob, obstacles, candidate, float(pad))\n"
            "                if pos is not None:\n"
            "                    mob.move_to(pos)\n"
            "                    return True\n"
            "    return False\n"
            "\n"
            "def _place_active_formulas(scene, active_names, formula_mobs):\n"
            "    if not active_names:\n"
            "        return\n"
            "    meta = {item['name']: item for item in _FORMULA_EVENTS}\n"
            "    placed = []\n"
            "    ordered = sorted(active_names, key=lambda n: (meta[n]['start'], meta[n]['end'], n))\n"
            "    for name in ordered:\n"
            "        mob = formula_mobs[name]\n"
            "        pref = np.asarray(meta[name].get('preferred_pos', [0.0, 0.0, 0.0]), dtype=float)\n"
            "        obstacles = []\n"
            "        for item in scene.mobjects:\n"
            "            if item is mob:\n"
            "                continue\n"
            "            if item in formula_mobs.values() and item not in placed:\n"
            "                continue\n"
            "            obstacles.append(item)\n"
            "        placed_ok = _place_formula_with_fallback(\n"
            "            mob,\n"
            "            obstacles,\n"
            "            pref,\n"
            "            float(_FORMULA_FRAME.get('buffer_padding', 0.22)),\n"
            "        )\n"
            "        if not placed_ok:\n"
            "            raise RuntimeError(f'No non-overlap slot for formula {name}')\n"
            "        placed.append(mob)\n"
            "\n"
            "def play_formula_timeline(scene):\n"
            "    formula_mobs = {}\n"
            "    active_names = []\n"
            "    _clear_preexisting_event_formulas(scene)\n"
            "    _prepare_scene_formula_space(scene)\n"
            "    timeline = sorted(set([item['start'] for item in _FORMULA_EVENTS] + [item['end'] for item in _FORMULA_EVENTS]))\n"
            "    for idx, t in enumerate(timeline):\n"
            "        remove_now = [item for item in _FORMULA_EVENTS if abs(item['end'] - t) < 1e-6 and item['name'] in active_names]\n"
            "        if remove_now:\n"
            "            scene.play(*[FadeOut(formula_mobs[item['name']]) for item in remove_now], run_time=0.25)\n"
            "            for item in remove_now:\n"
            "                active_names.remove(item['name'])\n"
            "\n"
            "        add_now = [item for item in _FORMULA_EVENTS if abs(item['start'] - t) < 1e-6]\n"
            "        if add_now:\n"
            "            for item in add_now:\n"
            "                if item.get('style', 'math') == 'tex':\n"
            "                    mob = Tex(item['latex'])\n"
            "                else:\n"
            "                    mob = MathTex(item['latex'])\n"
            f"                setattr(mob, '_fg_formula_registry_key', 'scene{scene_index}:' + str(item.get('name', '')))\n"
            "                mob.set_z_index(int(item.get('z_index', 30)))\n"
            "                if item.get('color'):\n"
            "                    try:\n"
            "                        mob.set_color(item['color'])\n"
            "                    except Exception:\n"
            "                        pass\n"
            "                base_scale = max(0.2, float(item.get('scale', 1.0)))\n"
            "                if abs(base_scale - 1.0) > 1e-6:\n"
            "                    mob.scale(base_scale)\n"
            "                setattr(mob, '_fg_scale', base_scale)\n"
            "                formula_mobs[item['name']] = mob\n"
            "                active_names.append(item['name'])\n"
            "            _place_active_formulas(scene, active_names, formula_mobs)\n"
            "            scene.play(*[FadeIn(formula_mobs[item['name']], shift=UP * 0.12) for item in add_now], run_time=0.35)\n"
            "\n"
            "        next_t = timeline[idx + 1] if idx + 1 < len(timeline) else None\n"
            "        if next_t is not None and next_t > t:\n"
            "            scene.wait(float(next_t - t))\n"
            "\n"
            "    if active_names:\n"
            "        scene.play(*[FadeOut(formula_mobs[name]) for name in list(active_names)], run_time=0.25)\n"
        )

    def _formula_registry_path() -> Path:
        return tmp_dir / "formula_layout_registry.json"

    def _load_formula_registry() -> dict:
        path = _formula_registry_path()
        if not path.exists():
            return {"version": 1, "scenes": {}}
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {"version": 1, "scenes": {}}
        if not isinstance(obj, dict):
            return {"version": 1, "scenes": {}}
        scenes = obj.get("scenes", {})
        if not isinstance(scenes, dict):
            scenes = {}
        return {"version": 1, "scenes": scenes}

    def _save_formula_registry(obj: dict) -> None:
        path = _formula_registry_path()
        path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")

    def _update_formula_registry(scene_index: int, events: list[dict], frame: dict) -> None:
        registry = _load_formula_registry()
        scenes = registry.setdefault("scenes", {})
        scenes[str(scene_index)] = {
            "scene_index": int(scene_index),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "events": events,
            "frame": frame,
        }
        _save_formula_registry(registry)

    def _resolve_animator_scene_path(scene_index: int) -> Path:
        """Resolve the animator module actually used by runtime scene wrapper.

        Scene wrappers may import a patched module (e.g. scene3_anim_clean.py)
        instead of the default scene3_anim.py. Formula insertion must target the
        imported module to keep registry/events aligned with runtime execution.
        """
        default_path = tmp_dir / "animator_codegen" / f"scene{scene_index}_anim.py"
        wrapper_path = _scene_path(scene_index)
        if not wrapper_path.exists() or not wrapper_path.is_file():
            return default_path

        try:
            wrapper_source = wrapper_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return default_path

        import_pat = re.compile(
            rf"from\s+animator_codegen\.([A-Za-z_][\w]*)\s+import\s+apply_scene{scene_index}_animation"
        )
        match = import_pat.search(wrapper_source)
        if not match:
            return default_path

        module_name = match.group(1).strip()
        if not module_name:
            return default_path

        resolved = tmp_dir / "animator_codegen" / f"{module_name}.py"
        return resolved if resolved.exists() else default_path

    def _insert_formula_timeline_into_animator(scene_index: int, snippet: str) -> tuple[str, Path]:
        animator_path = _resolve_animator_scene_path(scene_index)
        if not animator_path.exists() or not animator_path.is_file():
            raise FileNotFoundError(f"Animator scene file not found: {animator_path}")

        source = animator_path.read_text(encoding="utf-8")
        # Some generated animator files include UTF-8 BOM; strip it so ast.parse
        # and regex matching stay stable across files.
        if source.startswith("\ufeff"):
            source = source.lstrip("\ufeff")
        source = re.sub(
            rf"\n?# AUTO_FORMULA_TIMELINE_START scene{scene_index}\n.*?# AUTO_FORMULA_TIMELINE_END scene{scene_index}\n?",
            "\n",
            source,
            flags=re.DOTALL,
        )
        source = re.sub(
            rf"\n?\s*# AUTO_FORMULA_TIMELINE_CALL_START scene{scene_index}\n.*?# AUTO_FORMULA_TIMELINE_CALL_END scene{scene_index}\n?",
            "\n",
            source,
            flags=re.DOTALL,
        )

        lines = source.splitlines()
        func_pat = re.compile(
            rf"^def\s+apply_scene{scene_index}_animation\s*\(\s*scene(?:\s*:\s*[^)]*)?\s*\)\s*(?:->\s*[^:]+)?\s*:"
        )
        func_start = -1
        for idx, line in enumerate(lines):
            if func_pat.match(line.strip()):
                func_start = idx
                break
        if func_start < 0:
            raise RuntimeError(f"apply_scene{scene_index}_animation(scene) not found in {animator_path}")

        def_indent = len(lines[func_start]) - len(lines[func_start].lstrip())
        func_end = len(lines)
        for idx in range(func_start + 1, len(lines)):
            raw = lines[idx]
            stripped = raw.strip()
            if not stripped:
                continue
            indent = len(raw) - len(raw.lstrip())
            if indent <= def_indent and not stripped.startswith("#"):
                func_end = idx
                break

        # If legacy manual formulas are still hard-coded at the function tail
        # (f1/f2/f3 MathTex blocks), remove that tail so timeline injection
        # truly replaces old placement behavior instead of appending after it.
        formula_tail_start = -1
        formula_head_pat = re.compile(r"^f1\s*=\s*(?:MathTex|Tex)\(")
        body_indent = def_indent + 4
        for idx in range(func_start + 1, func_end):
            raw = lines[idx]
            stripped = raw.strip()
            if not stripped:
                continue
            indent = len(raw) - len(raw.lstrip())
            if indent != body_indent:
                continue
            if formula_head_pat.match(stripped):
                formula_tail_start = idx
                break

        if formula_tail_start >= 0:
            del lines[formula_tail_start:func_end]
            func_end = formula_tail_start

        call_indent = " " * (def_indent + 4)
        call_block = [
            f"{call_indent}# AUTO_FORMULA_TIMELINE_CALL_START scene{scene_index}",
            f"{call_indent}play_formula_timeline(scene)",
            f"{call_indent}# AUTO_FORMULA_TIMELINE_CALL_END scene{scene_index}",
        ]
        lines[func_end:func_end] = call_block
        updated = "\n".join(lines).rstrip() + "\n\n"
        updated += f"# AUTO_FORMULA_TIMELINE_START scene{scene_index}\n{snippet.rstrip()}\n"
        updated += f"# AUTO_FORMULA_TIMELINE_END scene{scene_index}\n"

        ast.parse(updated, filename=str(animator_path))
        animator_path.write_text(updated, encoding="utf-8")
        return updated, animator_path

    @tool
    def write_scene_code(scene_index: int, code: str) -> str:
        """Write Manim scene code to runtime workspace scene{index}/scene{index}.py."""
        path = _scene_path(scene_index)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(code, encoding="utf-8")
        return json.dumps({"ok": True, "path": str(path), "bytes": len(code.encode("utf-8"))}, ensure_ascii=False)

    @tool
    def write_runtime_file(path: str, content: str) -> str:
        """Write a text file in current runtime workspace using runtime-relative path."""
        target = _safe_runtime_rel_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return json.dumps({"ok": True, "path": str(target), "bytes": len(content.encode("utf-8"))}, ensure_ascii=False)

    @tool
    def list_scene_files() -> str:
        """List all existing scene files under runtime workspace scene*/scene*.py."""
        scene_files = _scene_files()
        items = [
            {
                "scene_index": _scene_index_from_path(path),
                "path": str(path.relative_to(project_root)).replace("\\", "/"),
            }
            for path in scene_files
        ]
        return json.dumps({"ok": True, "count": len(items), "items": items}, ensure_ascii=False)

    @tool
    def read_scene_code(scene_index: int) -> str:
        """Read a scene code file from runtime workspace scene{index}/scene{index}.py."""
        path = _scene_path(scene_index)
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(f"Scene file not found: {path}")
        return path.read_text(encoding="utf-8")

    @tool
    def write_narration_script(content: str, mode: str = "overwrite") -> str:
        """Write narration text to runtime workspace narration.txt. mode: overwrite|append."""
        if mode not in {"overwrite", "append"}:
            raise ValueError("mode must be 'overwrite' or 'append'")
        path = tmp_dir / "narration.txt"
        normalized_content, removed_labels = _sanitize_narration_text(content)
        if not normalized_content.strip():
            raise ValueError("Narration content is empty after removing scene labels")
        if mode == "append" and path.exists():
            existing = path.read_text(encoding="utf-8")
            next_content = existing.rstrip() + "\n" + normalized_content.strip() + "\n"
        else:
            next_content = normalized_content.strip() + "\n"
        path.write_text(next_content, encoding="utf-8")
        return json.dumps(
            {
                "ok": True,
                "path": str(path),
                "chars": len(next_content),
                "removed_scene_labels": removed_labels,
            },
            ensure_ascii=False,
        )

    @tool
    def validate_python_syntax(path: str) -> str:
        """Validate Python syntax for a runtime-relative file path."""
        target = _safe_runtime_rel_path(path)
        if not target.exists():
            raise FileNotFoundError(f"File not found: {target}")
        source = target.read_text(encoding="utf-8")
        ast.parse(source, filename=str(target))
        return json.dumps({"ok": True, "path": str(target)}, ensure_ascii=False)

    @tool
    def validate_scene_syntax() -> str:
        """Validate syntax for all runtime workspace scene*/scene*.py files."""
        scene_files = _scene_files()
        if not scene_files:
            raise FileNotFoundError("No scene files found in runtime workspace")
        results = []
        for path in scene_files:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            results.append(str(path))
        return json.dumps(
            {
                "ok": True,
                "count": len(results),
                "files": results,
            },
            ensure_ascii=False,
        )

    @tool
    def validate_formula_layout() -> str:
        """Validate formula boundary and overlap risks for all runtime scene files."""
        scene_files = _scene_files()
        if not scene_files:
            raise FileNotFoundError("No scene files found in runtime workspace")

        results, all_violations, runtime_errors = _validate_all_scene_layout(scene_files)

        return json.dumps(
            {
                "ok": len(all_violations) == 0 and len(runtime_errors) == 0,
                "runtime_error": len(runtime_errors) > 0,
                "runtime_errors": runtime_errors,
                "auto_fixed_scenes": [],
                "scene_count": len(results),
                "results": results,
                "violations": all_violations,
            },
            ensure_ascii=False,
        )

    @tool
    def build_formula_layout_plan(spec_json: str) -> str:
        """Build deterministic formula placement+timing plan for preview/debug.

        Input schema (JSON string):
        {
          "scene_index": 2,
          "frame_width": 14.222,
          "frame_height": 8.0,
          "events": [
            {"latex": "qU = \\frac{1}{2}mv_0^2", "at": 0.0, "off": 1.8},
            {"latex": "U = \\frac{Ed}{2}", "at": 0.8, "hold": 1.5},
            {"latex": "\\frac{qE}{m}=\\frac{v_0^2}{d}", "at": 1.8, "off": 3.6}
          ]
        }

        This tool does not write files. Use insert_formula_layout_plan for direct integration.
        """
        try:
            spec_obj = json.loads(spec_json)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid spec_json: {exc}")

        if not isinstance(spec_obj, dict):
            raise ValueError("spec_json must decode to an object")

        frame_w = _coerce_float(spec_obj.get("frame_width", 14.2222), 14.2222)
        frame_h = _coerce_float(spec_obj.get("frame_height", 8.0), 8.0)
        events = _normalize_formula_events(spec_obj)
        planned_events, frame = _build_formula_layout_plan(events, frame_w, frame_h)
        scene_index = int(spec_obj.get("scene_index", 0) or 0)
        snippet = _render_formula_layout_snippet(scene_index, planned_events, frame)

        payload = {
            "ok": True,
            "scene_index": int(spec_obj.get("scene_index", 0) or 0),
            "event_count": len(planned_events),
            "frame": frame,
            "events": planned_events,
            "snippet": snippet,
            "usage": "Preview only. Call insert_formula_layout_plan(scene_index, spec_json) to write scene code and registry.",
        }
        return json.dumps(payload, ensure_ascii=False)

    @tool
    def insert_formula_layout_plan(scene_index: int, spec_json: str) -> str:
        """Generate formula timeline and write it into runtime animator scene module directly.

        This is the single integration path. It updates:
        1) runtime/animator_codegen/sceneX_anim.py
        2) runtime/formula_layout_registry.json
        """
        try:
            spec_obj = json.loads(spec_json)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid spec_json: {exc}")
        if not isinstance(spec_obj, dict):
            raise ValueError("spec_json must decode to an object")

        if scene_index <= 0:
            scene_index = int(spec_obj.get("scene_index", 0) or 0)
        if scene_index <= 0:
            raise ValueError("scene_index must be >= 1")

        frame_w = _coerce_float(spec_obj.get("frame_width", 14.2222), 14.2222)
        frame_h = _coerce_float(spec_obj.get("frame_height", 8.0), 8.0)
        events = _normalize_formula_events(spec_obj)
        planned_events, frame = _build_formula_layout_plan(events, frame_w, frame_h)
        snippet = _render_formula_layout_snippet(scene_index, planned_events, frame)

        _, animator_path = _insert_formula_timeline_into_animator(scene_index, snippet)
        _update_formula_registry(scene_index, planned_events, frame)

        return json.dumps(
            {
                "ok": True,
                "scene_index": scene_index,
                "event_count": len(planned_events),
                "animator_file": str(animator_path),
                "registry_file": str(_formula_registry_path()),
                "message": "formula timeline inserted and registry updated",
            },
            ensure_ascii=False,
        )

    @tool
    def make_manim_video(
        output: str,
        quality: str = "h",
        fps: int = 30,
        resolution: str = "1920,1080",
        tts_script_file: str = "",
    ) -> str:
        """Run makevideo.py using generated scenes and narration from current runtime workspace."""
        scene_files = _scene_files()
        if not scene_files:
            raise FileNotFoundError("No scene files found in runtime workspace")

        scene_files_arg = ",".join(
            [str(p.resolve().relative_to(project_root.resolve())).replace("\\", "/") for p in scene_files]
        )
        scene_names_arg = ",".join([_infer_scene_class_name(p) for p in scene_files])

        # Preserve historical behavior: final videos default under outputs/.
        requested_output = Path((output or "").strip())
        output_name = requested_output.name or "lesson.mp4"
        if not Path(output_name).suffix:
            output_name = f"{output_name}.mp4"

        if requested_output.is_absolute():
            output_target = Path("outputs") / output_name
        else:
            output_parent = requested_output.parent
            if str(output_parent) in ("", "."):
                output_target = Path("outputs") / output_name
            else:
                output_target = requested_output

        output_rel = str(output_target).replace("\\", "/")
        default_tts_runtime_path = (tmp_dir / "narration.txt").resolve()
        tts_script_value = (
            tts_script_file.replace("\\", "/").strip()
            or str(default_tts_runtime_path.relative_to(project_root.resolve())).replace("\\", "/")
        )
        if tts_script_file.strip():
            runtime_tts = _safe_runtime_rel_path(tts_script_file)
            if not runtime_tts.exists():
                raise FileNotFoundError(f"tts_script_file not found in runtime workspace: {runtime_tts}")
            tts_script_value = str(runtime_tts.resolve().relative_to(project_root.resolve())).replace("\\", "/")

        removed_scene_labels_tts = 0
        auto_created_tts_script = False
        tts_script_abs = (project_root / tts_script_value).resolve()
        if not tts_script_abs.exists():
            raise FileNotFoundError(
                "Narration script not found. Please provide runtime narration.txt before rendering. "
                f"Expected: {tts_script_value}"
            )

        if tts_script_abs.exists() and tts_script_abs.is_file():
            raw_tts = tts_script_abs.read_text(encoding="utf-8")
            sanitized_tts, removed_scene_labels_tts = _sanitize_narration_text(raw_tts)
            if removed_scene_labels_tts > 0:
                if not sanitized_tts.strip():
                    raise ValueError("TTS script becomes empty after removing scene labels")
                tts_script_abs.write_text(sanitized_tts, encoding="utf-8")

        command: List[str] = [
            sys.executable,
            "makevideo.py",
            "--scene-files",
            scene_files_arg,
            "--scene-names",
            scene_names_arg,
            "--output",
            output_rel,
            "--quality",
            quality,
            "--fps",
            str(fps),
            "--resolution",
            resolution,
            "--run-dir",
            str(tmp_dir.resolve().relative_to(project_root.resolve())).replace("\\", "/"),
        ]
        if agent_run_dir is not None:
            runtime_log_file = (agent_run_dir / "makevideo.log").as_posix()
            command.extend(["--runtime-log-file", runtime_log_file])
        command.extend([
            "--tts-script-file", tts_script_value,
            "--voice", workflow_voice,
            "--tts-backend", workflow_tts_backend,
            "--tts-device", workflow_tts_device
        ])
        if workflow_prompt_text:
            command.extend(["--tts-prompt-text", workflow_prompt_text])
        if workflow_tts_api_base_url:
            command.extend(["--tts-api-base-url", workflow_tts_api_base_url])
        if workflow_tts_api_key:
            command.extend(["--tts-api-key", workflow_tts_api_key])
        command.extend(["--tts-api-timeout", str(workflow_tts_api_timeout)])
        if workflow_enable_multi:
            command.append("--enable-multithread")
        if not workflow_bgm:
            command.append("--no-bgm")
        else:
            command.extend(["--bgm-path", workflow_bgm.replace("\\", "/")])

        env = dict(os.environ)
        if agent_run_dir is not None:
            env["FILAGLYPH_RUN_DIR"] = str(agent_run_dir)

        proc = subprocess.run(
            command,
            cwd=str(project_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        stdout_file = makevideo_debug_dir / f"makevideo_{timestamp}_stdout.log"
        stderr_file = makevideo_debug_dir / f"makevideo_{timestamp}_stderr.log"
        stdout_file.write_text(proc.stdout or "", encoding="utf-8", errors="replace")
        stderr_file.write_text(proc.stderr or "", encoding="utf-8", errors="replace")

        runtime_log_file = ""
        runtime_match = re.search(r"Runtime log initialized:\s*(.+)", (proc.stdout or "") + "\n" + (proc.stderr or ""))
        if runtime_match:
            runtime_log_file = runtime_match.group(1).strip()

        stdout_text = proc.stdout or ""
        stderr_text = proc.stderr or ""
        ok = proc.returncode == 0

        payload = {
            "ok": ok,
            "returncode": proc.returncode,
            "runtime_log_file": runtime_log_file,
            "output_path": output_rel,
            "removed_scene_labels_in_tts": removed_scene_labels_tts,
            "auto_created_tts_script": auto_created_tts_script,
            "tts_script_file": tts_script_value,
            "stdout_file": str(stdout_file),
            "stderr_file": str(stderr_file),
        }

        if ok:
            # Success path: keep response compact; full logs remain in debug files.
            payload["stdout_preview"] = stdout_text[-1200:]
            payload["stderr_preview"] = stderr_text[-1200:]
        else:
            # Failure path: return only concise root cause to the coder agent.
            # Full traceback is kept in runtime_log_file/stdout_file/stderr_file.
            concise = _build_concise_error(stdout_text, stderr_text)
            payload.update(concise)
            payload["next_action"] = (
                "Fix the reported scene/code location first. "
                "Only open runtime_log_file or stderr_file when this summary is insufficient."
            )

        return json.dumps(payload, ensure_ascii=False)

    @tool
    def read_text_file(path: str) -> str:
        """Read a text file from current runtime workspace using runtime-relative path."""
        target = _safe_runtime_rel_path(path)
        if not target.exists():
            raise FileNotFoundError(f"File not found: {target}")
        return target.read_text(encoding="utf-8")

    @tool
    def report_summary(message: str) -> str:
        """Report a summary or error message to the user.

        Call this tool to display your final summary or error analysis in the
        user interface.  This is the **only** way for your output to appear in
        the UI — do NOT rely on plain text responses for important messages.
        """
        normalized = (message or "").strip()
        if not normalized:
            return json.dumps(
                {"ok": False, "error": "message is empty; provide a concrete summary"},
                ensure_ascii=False,
            )
        _shared["summary"] = normalized
        return json.dumps({"ok": True, "chars": len(normalized)}, ensure_ascii=False)

    return [
        list_scene_files,
        read_scene_code,
        write_scene_code,
        write_runtime_file,
        write_narration_script,
        validate_python_syntax,
        validate_scene_syntax,
        validate_formula_layout,
        build_formula_layout_plan,
        insert_formula_layout_plan,
        make_manim_video,
        read_text_file,
        report_summary,
    ]
