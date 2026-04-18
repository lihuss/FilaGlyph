from __future__ import annotations

SAFE_MARGIN = 0.3
MAX_FORMULA_WIDTH_RATIO = 0.38
MAX_FORMULA_HEIGHT_RATIO = 0.45
MAX_FORMULAS_ON_SCREEN = 3
ALLOWED_HIGHLIGHT_POLICY = ("none", "outline_only")


def build_boundary_policy_prompt() -> str:
    """Return canonical boundary policy text for coder prompt injection."""
    return (
        "Boundary Policy v2\n"
        f"- safe_margin: {SAFE_MARGIN}\n"
        f"- max_formula_width_ratio: {MAX_FORMULA_WIDTH_RATIO}\n"
        f"- max_formula_height_ratio: {MAX_FORMULA_HEIGHT_RATIO}\n"
        f"- max_formulas_on_screen: {MAX_FORMULAS_ON_SCREEN}\n"
        f"- allowed_highlight_policy: {', '.join(ALLOWED_HIGHLIGHT_POLICY)}\n"
        "- formula boundary APIs: use get_left/get_right/get_top/get_bottom/get_center/width/height\n"
        "- forbidden APIs for boundary checks: get_bounding_box, get_bounding_box_point\n"
        "- fix strategy after render error: patch only failed scene first; do not rewrite unrelated scenes or narration"
    )
