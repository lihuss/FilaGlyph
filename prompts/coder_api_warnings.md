# Coder API Warnings

This checklist captures common failure patterns observed in runtime scene generation.
Follow these rules to reduce retries and avoid low-quality formula rendering.

## 1) numpy object does not have Manim methods

Wrong:
```python
vec = np.array([1, 0, 0])
vec.get_vector()
```

Right:
```python
arr = Arrow(ORIGIN, RIGHT)
arr.get_vector()
```

Rule:
- Call Manim methods only on Manim mobjects.
- Keep numpy arrays for numeric calculations only.

## 2) Do not use filled highlight boxes on formulas

Wrong:
```python
box = SurroundingRectangle(eq, color=ORANGE, fill_opacity=0.6)
```

Right:
```python
box = SurroundingRectangle(eq, color=YELLOW, stroke_width=2)
box.set_fill(opacity=0)
```

Rule:
- Formula highlight must be `none` or `outline_only`.
- Never use filled backgrounds for formula emphasis.

## 3) Formula boundary check must be done twice

Rule:
- First check after layout/arrange/scale.
- Second check after highlight/indicate adjustments.

Required boundary:
- right <= config.frame_width / 2 - 0.3
- left >= -config.frame_width / 2 + 0.3
- top <= config.frame_height / 2 - 0.3
- bottom >= -config.frame_height / 2 + 0.3

API requirement:
- Use `get_left/get_right/get_top/get_bottom/get_center/width/height`.
- Do not use `get_bounding_box` or `get_bounding_box_point` for MathTex boundary logic.

## 4) Prefer time-splitting over crowded same-screen formulas

Rule:
- Keep formulas on screen <= 3 at any moment.
- If too many formulas are needed, split into stages instead of shrinking to unreadable size.

## 5) Keep fixes local when tool errors occur

Rule:
- Read the specific error location first.
- Modify only the failing scene/action if possible.
- Avoid full rewrite of all scene files unless absolutely necessary.

## 6) Never mutate camera via self.camera.config

Wrong:
```python
self.camera.config.background_color = "#000000"
```

Right:
```python
from manim import config
config.background_color = "#000000"
```

Rule:
- Do not access or mutate `self.camera.config`.
- Use global `manim.config` or scene-local APIs supported by current Manim version.

## 7) Treat formula validator runtime_error as hard stop

Rule:
- If `validate_formula_layout` returns `runtime_error=true`, stop blind retries.
- Typical trigger is NaN/Inf geometry (invalid bbox, broken transform chain).
- First fix scene geometry so all formula coordinates are finite, then re-run validation and render.

## 8) Do not call point_from_proportion on dashed mobjects

Wrong:
```python
line_pc = DashedLine(P, C)
mid = line_pc.point_from_proportion(0.5)
```

Right:
```python
line_pc = DashedLine(P, C)
mid = 0.5 * (line_pc.get_start() + line_pc.get_end())
```

Also acceptable:
```python
guide = Line(P, C)
mid = guide.point_from_proportion(0.5)
```

Rule:
- In this runtime, `DashedLine`/`DashedVMobject` may have zero points on the parent mobject.
- Never use `point_from_proportion` on dashed objects directly.
- Use endpoint interpolation or a helper `Line` for proportion-based coordinates.

## 9) Do not rely on objects created in previous scenes

Symptom:
```text
ValueError: Required object 'particle' not found on scene.
```

Wrong:
```python
def apply_scene2_animation(scene):
	particle = _require(scene, "particle")  # created only in scene1
```

Right:
```python
def apply_scene2_animation(scene):
	particle = Dot(np.array([-2.0, 0.0, 0.0]), color=YELLOW)
	scene.add(particle)
```

Rule:
- scene2+ must not assume runtime objects from earlier scenes exist.
- If an object is not defined in `base_scene.py`, rebuild it locally in the current scene.
- Do not use `scene.xxx = ...` to pass temporary objects across scenes.

## 10) Do not use `alignment_edge` in `next_to` for this runtime

Symptom:
```text
TypeError: Mobject.next_to() got an unexpected keyword argument 'alignment_edge'
```

Wrong:
```python
f2 = MathTex("U = \\frac{Ed}{2}").next_to(f1, DOWN, alignment_edge=LEFT)
```

Right:
```python
f2 = MathTex("U = \\frac{Ed}{2}").next_to(f1, DOWN)
f2.align_to(f1, LEFT)
```

Rule:
- In this Manim runtime, `Mobject.next_to` may not accept `alignment_edge`.
- Use `next_to(...)` first, then `align_to(..., LEFT/RIGHT/UP/DOWN)` when edge alignment is needed.

## 11) Avoid `GrowArrow` in this runtime

Symptom:
```text
TypeError: Mobject.apply_points_function_about_point() got an unexpected keyword argument 'scale_tips'
```

Wrong:
```python
scene.play(GrowArrow(arrow_v0), FadeIn(label_v0))
```

Right:
```python
scene.play(Create(arrow_v0), FadeIn(label_v0))
```

Rule:
- In this Manim runtime, `GrowArrow` may fail due to unsupported `scale_tips` handling.
- Prefer `Create(arrow)` (or `FadeIn(arrow)`) for arrow reveal animations.
