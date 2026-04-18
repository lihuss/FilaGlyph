from __future__ import annotations

import importlib.util
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

import numpy as np
from manim import Circle, Line, MathTex, Mobject, Scene, Tex, YELLOW, config


class SmartLayoutEngine:
    """Discrete grid + integral-image placement engine for Manim mobjects."""

    def __init__(
        self,
        frame_width: float = 14.222,
        frame_height: float = 8.0,
        cells_per_unit: int = 20,
    ) -> None:
        self.frame_width = float(frame_width)
        self.frame_height = float(frame_height)
        self.cells_per_unit = max(4, int(cells_per_unit))

        self.left = -self.frame_width / 2.0
        self.right = self.frame_width / 2.0
        self.top = self.frame_height / 2.0
        self.bottom = -self.frame_height / 2.0

        self.cols = max(8, int(round(self.frame_width * self.cells_per_unit)))
        self.rows = max(8, int(round(self.frame_height * self.cells_per_unit)))

    def _clip_bbox(self, left: float, right: float, top: float, bottom: float) -> tuple[float, float, float, float]:
        return (
            max(self.left, min(self.right, left)),
            max(self.left, min(self.right, right)),
            max(self.bottom, min(self.top, top)),
            max(self.bottom, min(self.top, bottom)),
        )

    def _bbox(self, mob: Mobject) -> tuple[float, float, float, float]:
        return (
            float(mob.get_left()[0]),
            float(mob.get_right()[0]),
            float(mob.get_top()[1]),
            float(mob.get_bottom()[1]),
        )

    def xy_to_rc(self, x: float, y: float) -> tuple[int, int]:
        col = int((float(x) - self.left) * self.cells_per_unit)
        row = int((self.top - float(y)) * self.cells_per_unit)
        row = max(0, min(self.rows - 1, row))
        col = max(0, min(self.cols - 1, col))
        return row, col

    def rc_to_xy(self, row: float, col: float) -> tuple[float, float]:
        x = self.left + (float(col) + 0.5) / self.cells_per_unit
        y = self.top - (float(row) + 0.5) / self.cells_per_unit
        return x, y

    def _mark_bbox(self, grid: np.ndarray, bbox: tuple[float, float, float, float]) -> None:
        left, right, top, bottom = self._clip_bbox(*bbox)
        if right <= left or top <= bottom:
            return
        r0, c0 = self.xy_to_rc(left, top)
        r1, c1 = self.xy_to_rc(right, bottom)
        if r1 < r0:
            r0, r1 = r1, r0
        if c1 < c0:
            c0, c1 = c1, c0
        r1 = min(self.rows - 1, r1)
        c1 = min(self.cols - 1, c1)
        grid[r0 : r1 + 1, c0 : c1 + 1] = 1

    def _dilate(self, grid: np.ndarray, padding_cells: int) -> np.ndarray:
        pad = int(max(0, padding_cells))
        if pad <= 0:
            return grid
        padded = np.pad(grid.astype(bool), ((pad, pad), (pad, pad)), mode="constant")
        out = np.zeros_like(grid, dtype=bool)
        # Disk-like dilation kernel using integer offsets.
        for dr in range(-pad, pad + 1):
            for dc in range(-pad, pad + 1):
                if dr * dr + dc * dc > pad * pad:
                    continue
                out |= padded[pad + dr : pad + dr + self.rows, pad + dc : pad + dc + self.cols]
        return out.astype(np.uint8)

    def build_occupancy_grid(self, obstacles: list[Mobject], buffer_padding: float) -> np.ndarray:
        grid = np.zeros((self.rows, self.cols), dtype=np.uint8)
        for mob in obstacles:
            try:
                self._mark_bbox(grid, self._bbox(mob))
            except Exception:
                continue
        pad_cells = int(round(max(0.0, float(buffer_padding)) * self.cells_per_unit))
        return self._dilate(grid, pad_cells)

    @staticmethod
    def integral_image(grid: np.ndarray) -> np.ndarray:
        sat = np.cumsum(np.cumsum(grid.astype(np.int32), axis=0), axis=1)
        return np.pad(sat, ((1, 0), (1, 0)), mode="constant")

    @staticmethod
    def area_sum(integral: np.ndarray, r: np.ndarray, c: np.ndarray, h: int, w: int) -> np.ndarray:
        return integral[r + h, c + w] - integral[r, c + w] - integral[r + h, c] + integral[r, c]

    def get_safe_position(
        self,
        target: Mobject,
        obstacles: list[Mobject],
        preferred_pos: np.ndarray | None,
        buffer_padding: float,
    ) -> np.ndarray:
        occ = self.build_occupancy_grid(obstacles, buffer_padding=buffer_padding)
        ii = self.integral_image(occ)

        w_cells = max(1, int(math.ceil(float(target.width) * self.cells_per_unit)))
        h_cells = max(1, int(math.ceil(float(target.height) * self.cells_per_unit)))
        max_r = self.rows - h_cells
        max_c = self.cols - w_cells
        if max_r < 0 or max_c < 0:
            return np.array([0.0, 0.0, 0.0], dtype=float)

        rr = np.arange(0, max_r + 1, dtype=np.int32)[:, None]
        cc = np.arange(0, max_c + 1, dtype=np.int32)[None, :]
        sums = self.area_sum(ii, rr, cc, h_cells, w_cells)
        valid = np.argwhere(sums == 0)
        if valid.size == 0:
            raise RuntimeError("No free grid region for target mobject")

        centers_r = valid[:, 0].astype(float) + h_cells / 2.0
        centers_c = valid[:, 1].astype(float) + w_cells / 2.0
        x = self.left + (centers_c + 0.5) / self.cells_per_unit
        y = self.top - (centers_r + 0.5) / self.cells_per_unit

        if preferred_pos is None:
            pref_x, pref_y = 0.0, 0.0
        else:
            pref = np.asarray(preferred_pos, dtype=float)
            pref_x = float(pref[0]) if pref.size > 0 else 0.0
            pref_y = float(pref[1]) if pref.size > 1 else 0.0

        dist2 = (x - pref_x) ** 2 + (y - pref_y) ** 2
        idx = int(np.argmin(dist2))
        return np.array([x[idx], y[idx], 0.0], dtype=float)


def get_safe_position(
    target: Mobject,
    obstacles: list[Mobject],
    preferred_pos: np.ndarray,
    buffer_padding: float,
    frame_width: float = 14.222,
    frame_height: float = 8.0,
    cells_per_unit: int = 20,
) -> np.ndarray:
    engine = SmartLayoutEngine(
        frame_width=frame_width,
        frame_height=frame_height,
        cells_per_unit=cells_per_unit,
    )
    return engine.get_safe_position(target, obstacles, preferred_pos, buffer_padding)


def _purge_runtime_modules(runtime_root: Path) -> None:
    runtime_root = runtime_root.resolve()
    for name, mod in list(sys.modules.items()):
        if mod is None:
            continue
        mod_file = getattr(mod, "__file__", None)
        if not mod_file:
            continue
        try:
            mod_path = Path(mod_file).resolve()
        except Exception:
            continue
        try:
            mod_path.relative_to(runtime_root)
        except ValueError:
            continue
        sys.modules.pop(name, None)


def _walk_mobjects(root: Mobject) -> list[Mobject]:
    items = [root]
    for sub in getattr(root, "submobjects", []):
        items.extend(_walk_mobjects(sub))
    return items


def _build_scene_without_render(scene_cls: type[Scene]) -> Scene:
    original_play = Scene.play
    original_wait = Scene.wait

    def _patched_play(self: Scene, *animations: Any, **kwargs: Any) -> None:
        if not hasattr(self, "_fg_seen_formula_mobjects"):
            setattr(self, "_fg_seen_formula_mobjects", [])
        seen_formula: list[Mobject] = getattr(self, "_fg_seen_formula_mobjects")
        for anim in animations:
            anim_name = str(getattr(anim, "__class__", type(anim)).__name__).lower()
            is_remove_like = (
                "fadeout" in anim_name
                or "uncreate" in anim_name
                or "remove" in anim_name
                or "fadeoutto" in anim_name
            )
            candidates: list[Any] = []
            if hasattr(anim, "mobject"):
                candidates.append(getattr(anim, "mobject"))
            if hasattr(anim, "mobjects"):
                maybe = getattr(anim, "mobjects")
                if isinstance(maybe, (list, tuple)):
                    candidates.extend(list(maybe))
            for item in candidates:
                if isinstance(item, Mobject):
                    formula_key = str(getattr(item, "_fg_formula_registry_key", "") or "")
                    if formula_key and all(id(item) != id(prev) for prev in seen_formula):
                        seen_formula.append(item)
                    if is_remove_like:
                        self.remove(item)
                    else:
                        self.add(item)

    def _patched_wait(self: Scene, *args: Any, **kwargs: Any) -> None:
        return None

    Scene.play = _patched_play
    Scene.wait = _patched_wait
    try:
        scene = scene_cls()
        scene.construct()
        return scene
    finally:
        Scene.play = original_play
        Scene.wait = original_wait


def validate_scene_formula_layout(
    scene_file: Path,
    scene_class_name: str,
    safe_margin: float = 0.3,
    cells_per_unit: int = 20,
    overflow_tolerance: float = 0.06,
) -> dict:
    runtime_root = scene_file.parent.parent
    _purge_runtime_modules(runtime_root)
    importlib.invalidate_caches()

    # Keep Manim transient outputs under this run's runtime folder instead of project-root media/.
    validation_media_dir = (runtime_root / "_manim_media").resolve()
    validation_media_dir.mkdir(parents=True, exist_ok=True)
    try:
        config.media_dir = str(validation_media_dir)
    except Exception:
        pass
    try:
        config.tex_dir = str((validation_media_dir / "Tex").resolve())
    except Exception:
        pass

    runtime_root_str = str(runtime_root.resolve())
    inserted_runtime_path = False
    if runtime_root_str not in sys.path:
        sys.path.insert(0, runtime_root_str)
        inserted_runtime_path = True

    try:
        spec = importlib.util.spec_from_file_location(f"_scene_guard_grid_{scene_class_name}", scene_file)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Unable to load scene module: {scene_file}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        if inserted_runtime_path:
            try:
                sys.path.remove(runtime_root_str)
            except ValueError:
                pass

    scene_cls = getattr(module, scene_class_name, None)
    if scene_cls is None:
        raise RuntimeError(f"Scene class not found: {scene_class_name}")

    scene = _build_scene_without_render(scene_cls)
    items: list[Mobject] = []
    for root in scene.mobjects:
        items.extend(_walk_mobjects(root))

    seen: set[int] = set()
    deduped: list[Mobject] = []
    for item in items:
        ident = id(item)
        if ident in seen:
            continue
        seen.add(ident)
        deduped.append(item)

    scene_idx_match = re.search(r"scene(\d+)$", scene_file.parent.name)
    scene_key = scene_idx_match.group(1) if scene_idx_match else ""
    registry_path = runtime_root / "formula_layout_registry.json"

    registered_events: list[dict] = []
    if registry_path.exists():
        try:
            registry_obj = json.loads(registry_path.read_text(encoding="utf-8"))
            if isinstance(registry_obj, dict):
                scenes = registry_obj.get("scenes", {})
                if isinstance(scenes, dict):
                    scene_entry = scenes.get(scene_key, {}) if scene_key else {}
                    if isinstance(scene_entry, dict):
                        maybe_events = scene_entry.get("events", [])
                        if isinstance(maybe_events, list):
                            registered_events = [ev for ev in maybe_events if isinstance(ev, dict)]
        except Exception:
            registered_events = []

    formula_keys = {
        f"scene{scene_key}:{str(item.get('name', '')).strip()}"
        for item in registered_events
        if str(item.get("name", "")).strip() and scene_key
    }

    formulas: list[Mobject] = []
    if formula_keys:
        merged: list[Mobject] = []
        seen_ids: set[int] = set()
        scene_seen_formulas = list(getattr(scene, "_fg_seen_formula_mobjects", []) or [])
        for mob in scene_seen_formulas + deduped:
            if not isinstance(mob, Mobject):
                continue
            key = str(getattr(mob, "_fg_formula_registry_key", "") or "")
            if key not in formula_keys:
                continue
            ident = id(mob)
            if ident in seen_ids:
                continue
            seen_ids.add(ident)
            merged.append(mob)
        formulas = merged
        if len(formulas) < len(formula_keys):
            raise RuntimeError(
                "Formula registry is incomplete in scene runtime state: "
                f"expected={len(formula_keys)} found={len(formulas)} scene={scene_file}"
            )

    formula_id_set = {id(m) for m in formulas}
    obstacles = [m for m in deduped if id(m) not in formula_id_set]

    frame_w = float(config.frame_width)
    frame_h = float(config.frame_height)
    safe_left = -frame_w / 2 + float(safe_margin)
    safe_right = frame_w / 2 - float(safe_margin)
    safe_top = frame_h / 2 - float(safe_margin)
    safe_bottom = -frame_h / 2 + float(safe_margin)

    engine = SmartLayoutEngine(frame_width=frame_w, frame_height=frame_h, cells_per_unit=cells_per_unit)
    occ = engine.build_occupancy_grid(obstacles, buffer_padding=0.0)
    occ_ii = engine.integral_image(occ)

    formula_grid = np.zeros_like(occ, dtype=np.uint8)
    violations: list[dict] = []
    eps = max(1e-6, float(overflow_tolerance))

    for idx, mob in enumerate(formulas):
        left = float(mob.get_left()[0])
        right = float(mob.get_right()[0])
        top = float(mob.get_top()[1])
        bottom = float(mob.get_bottom()[1])

        if left < safe_left - eps or right > safe_right + eps or top > safe_top + eps or bottom < safe_bottom - eps:
            violations.append(
                {
                    "type": "frame_overflow",
                    "formula_index": idx,
                    "box": [left, right, top, bottom],
                    "safe_box": [safe_left, safe_right, safe_top, safe_bottom],
                }
            )

        grid_rect = np.zeros_like(occ, dtype=np.uint8)
        engine._mark_bbox(grid_rect, (left, right, top, bottom))
        r_idx, c_idx = np.where(grid_rect > 0)
        if r_idx.size > 0:
            r0 = int(r_idx.min())
            r1 = int(r_idx.max())
            c0 = int(c_idx.min())
            c1 = int(c_idx.max())
            area_obs = SmartLayoutEngine.area_sum(occ_ii, np.array([[r0]]), np.array([[c0]]), r1 - r0 + 1, c1 - c0 + 1)[0, 0]
            if int(area_obs) > 0:
                violations.append(
                    {
                        "type": "formula_obstacle_overlap",
                        "formula_index": idx,
                        "grid_box": [r0, r1, c0, c1],
                    }
                )

            overlap_formula = int(np.sum((formula_grid > 0) & (grid_rect > 0)))
            if overlap_formula > 0:
                violations.append(
                    {
                        "type": "formula_overlap",
                        "formula_index": idx,
                        "overlap_cells": overlap_formula,
                    }
                )
            formula_grid = np.maximum(formula_grid, grid_rect)

    return {
        "ok": len(violations) == 0,
        "scene_file": str(scene_file),
        "scene_class": scene_class_name,
        "formula_count": len(formulas),
        "violations": violations,
    }


class SmartLayoutDemo(Scene):
    def construct(self) -> None:
        circle = Circle(radius=1.6).shift(np.array([-1.2, 0.8, 0.0]))
        line = Line(np.array([-5.0, -1.2, 0.0]), np.array([4.8, -0.2, 0.0]))
        formula = MathTex(r"E = mc^2", color=YELLOW)
        self.add(circle, line)

        pos = get_safe_position(
            target=formula,
            obstacles=[circle, line],
            preferred_pos=np.array([3.2, 2.2, 0.0]),
            buffer_padding=0.25,
            frame_width=float(config.frame_width),
            frame_height=float(config.frame_height),
            cells_per_unit=20,
        )
        formula.move_to(pos)
        self.add(formula)
