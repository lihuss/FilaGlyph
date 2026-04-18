from __future__ import annotations

import base64
import json
import re
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from .config import AgentConfig, load_agent_config
from .animator_codegen import write_animator_codegen
from .boundary_policy import build_boundary_policy_prompt
from .coder_tools import build_coder_tools
from .llm_factory import create_chat_model
from .prompts import load_prompt


@dataclass
class AgentOutputs:
    solver_answer: str
    architect_code: str
    director_plan: str
    animator_plan: str
    coder_output: str
    run_dir: Path
    runtime_dir: Path
    coder_failed: bool = False


class AgentWorkflow:
    def __init__(self, config: AgentConfig | None = None) -> None:
        self.config = config or load_agent_config()
        self._cancel_requested = False
        self._current_run_dir: Path | None = None

    @property
    def current_run_dir(self) -> Path | None:
        return self._current_run_dir

    def cancel(self) -> None:
        self._cancel_requested = True

    def _check_cancelled(self) -> None:
        if self._cancel_requested:
            raise RuntimeError("运行已取消")

    def run(
        self,
        image_path: Path | None,
        on_progress: Callable[[str], None] | None = None,
        on_stage_result: Callable[[str, str], None] | None = None,
        render_options: dict | None = None,
    ) -> AgentOutputs:
        emit_progress = on_progress or (lambda _msg: None)
        emit_stage = on_stage_result or (lambda _stage, _content: None)
        if not image_path or not image_path.exists():
            raise ValueError("请先上传题目图片。")
        solver_prompt_text = str((render_options or {}).get("solver_prompt_text", "") or "").strip()

        # Read source bytes before tmp cleanup, because pasted images may be
        # temporarily stored under tmp/ and would otherwise be deleted.
        image_name = image_path.name
        image_bytes = image_path.read_bytes()

        run_dir = self._make_run_dir()
        runtime_dir = self._prepare_runtime_workspace(run_dir)
        self._current_run_dir = run_dir
        image_copy_path = run_dir / image_name
        image_copy_path.write_bytes(image_bytes)

        # Write metadata early so we can resume if partially failed
        meta = {
            "image": image_copy_path.name,
            "render_options": render_options or {},
            "runtime_dir": str(runtime_dir),
        }
        (run_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

        self._check_cancelled()
        emit_progress("Solver 正在解题...")
        solver_raw = self._run_solver(image_copy_path, solver_prompt_text)
        solver_answer = self._extract_solver_solution(solver_raw)
        (run_dir / "solver_output.md").write_text(solver_raw, encoding="utf-8")
        (run_dir / "solver_answer.md").write_text(solver_answer, encoding="utf-8")
        emit_stage("solver", solver_answer.strip() or solver_raw)

        self._check_cancelled()
        emit_progress("DeepSeek 正在整理/数值化物理量...")
        quantizer_raw = self._run_quantizer(
            solver_solution=solver_answer,
        )
        solver_quantities = self._parse_quantizer_output(quantizer_raw, self._default_solver_quantities())
        (run_dir / "quantizer_output.md").write_text(quantizer_raw, encoding="utf-8")
        (run_dir / "deepseek_quantities.json").write_text(
            json.dumps(solver_quantities, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (run_dir / "solver_quantities.json").write_text(
            json.dumps(solver_quantities, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        emit_stage("deepseek", json.dumps(solver_quantities, ensure_ascii=False, indent=2))

        self._check_cancelled()
        emit_progress("Architect 正在生成 base_scene...")
        architect_code = self._run_architect(image_copy_path)
        (run_dir / "architect_code.py").write_text(architect_code, encoding="utf-8")
        emit_stage("architect", architect_code)
        self._write_base_scene(runtime_dir, architect_code)

        self._check_cancelled()
        emit_progress("Director 正在规划动画...")
        director_plan = self._run_director(solver_answer, architect_code, image_copy_path)
        (run_dir / "director_plan.md").write_text(director_plan, encoding="utf-8")
        emit_stage("director", director_plan)

        self._check_cancelled()
        emit_progress("Animator 正在生成动画代码包...")
        animator_plan = self._run_animator(
            quantities=solver_quantities,
            solver_solution=solver_answer,
            architect_code=architect_code,
            director_plan=director_plan,
            image_path=image_copy_path,
            runtime_dir=runtime_dir,
        )
        (run_dir / "animator_codegen.md").write_text(animator_plan, encoding="utf-8")
        emit_stage("animator", animator_plan)

        self._check_cancelled()
        emit_progress("Coder 正在集成动画代码并调用工具...")
        coder_output, coder_failed = self._run_coder(
            director_plan,
            render_options or {},
            runtime_dir=runtime_dir,
            emit_progress=emit_progress,
        )
        (run_dir / "coder_output.md").write_text(coder_output, encoding="utf-8")
        emit_stage("coder", coder_output)

        self._check_cancelled()
        emit_progress("运行结束...")

        return AgentOutputs(
            solver_answer=solver_answer,
            architect_code=architect_code,
            director_plan=director_plan,
            animator_plan=animator_plan,
            coder_output=coder_output,
            run_dir=run_dir,
            runtime_dir=runtime_dir,
            coder_failed=coder_failed,
        )


    def continue_run(
        self,
        run_dir: Path,
        on_progress: Callable[[str], None] | None = None,
        on_stage_result: Callable[[str, str], None] | None = None,
        render_options: dict | None = None,
        resume_from_stage: str | None = None,
        stop_after_stage: str | None = None,
    ) -> AgentOutputs:
        emit_progress = on_progress or (lambda _msg: None)
        emit_stage = on_stage_result or (lambda _stage, _content: None)
        if not run_dir.exists():
            raise ValueError("继续运行失败：找不到任务目录。")

        self._cancel_requested = False
        self._current_run_dir = run_dir
        meta = {}
        meta_path = run_dir / "meta.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
        render_options = render_options or meta.get("render_options", {}) or {}
        solver_prompt_text = str((render_options or {}).get("solver_prompt_text", "") or "").strip()

        runtime_value = str(meta.get("runtime_dir", "") or "").strip()
        runtime_dir = Path(runtime_value) if runtime_value else (run_dir / "runtime")
        runtime_dir.mkdir(parents=True, exist_ok=True)
        if not runtime_value:
            meta["runtime_dir"] = str(runtime_dir)
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

        image_name = str(meta.get("image", "") or "").strip()
        image_path = run_dir / image_name if image_name else None
        if image_path is None or not image_path.exists():
            raise ValueError("继续运行失败：找不到原始题目图片。")

        solver_path = run_dir / "solver_answer.md"
        solver_output_path = run_dir / "solver_output.md"
        quantizer_output_path = run_dir / "quantizer_output.md"
        deepseek_quantities_path = run_dir / "deepseek_quantities.json"
        quantities_path = run_dir / "solver_quantities.json"
        architect_path = run_dir / "architect_code.py"
        director_path = run_dir / "director_plan.md"
        animator_path = run_dir / "animator_codegen.md"
        coder_path = run_dir / "coder_output.md"

        def _safe_unlink(path: Path) -> None:
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass

        solver_answer = self._read_or_default(solver_path)
        solver_raw = self._read_or_default(solver_output_path)
        quantizer_raw = self._read_or_default(quantizer_output_path)
        solver_quantities = self._read_solver_quantities(deepseek_quantities_path)
        if not solver_quantities.get("items"):
            solver_quantities = self._read_solver_quantities(quantities_path)
        if solver_answer:
            solver_answer = self._extract_solver_solution(solver_answer)
            solver_path.write_text(solver_answer, encoding="utf-8")
        if not solver_raw:
            solver_raw = solver_answer
        if (not quantities_path.exists()) or solver_quantities.get("items"):
            quantities_path.write_text(
                json.dumps(solver_quantities, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        if solver_quantities.get("items"):
            deepseek_quantities_path.write_text(
                json.dumps(solver_quantities, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        architect_code = self._read_or_default(architect_path)
        director_plan = self._read_or_default(director_path)
        animator_plan = self._read_or_default(animator_path)
        if not animator_plan:
            animator_plan = self._read_or_default(run_dir / "animator_plan.md")
        coder_output = self._read_or_default(coder_path)
        coder_failed = False

        # Force rerun from a specific stage regardless of existing artifacts.
        stage = (resume_from_stage or "").strip().lower()
        stop_stage = (stop_after_stage or "").strip().lower()
        if stage == "solver":
            solver_answer = ""
            solver_raw = ""
            quantizer_raw = ""
            solver_quantities = self._default_solver_quantities()
            architect_code = ""
            director_plan = ""
            animator_plan = ""
            coder_output = ""
            _safe_unlink(quantizer_output_path)
            _safe_unlink(deepseek_quantities_path)
            _safe_unlink(architect_path)
            _safe_unlink(director_path)
            _safe_unlink(animator_path)
            _safe_unlink(run_dir / "animator_plan.md")
            _safe_unlink(coder_path)
        elif stage == "deepseek":
            quantizer_raw = ""
            solver_quantities = self._default_solver_quantities()
            animator_plan = ""
            coder_output = ""
            _safe_unlink(quantizer_output_path)
            _safe_unlink(deepseek_quantities_path)
            _safe_unlink(animator_path)
            _safe_unlink(run_dir / "animator_plan.md")
            _safe_unlink(coder_path)
        elif stage == "architect":
            architect_code = ""
            director_plan = ""
            animator_plan = ""
            coder_output = ""
            _safe_unlink(director_path)
            _safe_unlink(animator_path)
            _safe_unlink(run_dir / "animator_plan.md")
            _safe_unlink(coder_path)
        elif stage == "director":
            director_plan = ""
            animator_plan = ""
            coder_output = ""
            _safe_unlink(animator_path)
            _safe_unlink(run_dir / "animator_plan.md")
            _safe_unlink(coder_path)
        elif stage == "animator":
            animator_plan = ""
            coder_output = ""
            _safe_unlink(coder_path)
        elif stage == "coder":
            coder_output = ""

        if not solver_answer:
            self._check_cancelled()
            emit_progress("Solver 正在解题...")
            solver_raw = self._run_solver(image_path, solver_prompt_text)
            solver_answer = self._extract_solver_solution(solver_raw)
            quantizer_raw = ""
            solver_quantities = self._default_solver_quantities()
            solver_output_path.write_text(solver_raw, encoding="utf-8")
            solver_path.write_text(solver_answer, encoding="utf-8")
            emit_stage("solver", solver_answer.strip() or solver_raw)
            if stop_stage == "solver":
                emit_progress("运行结束...")
                return AgentOutputs(
                    solver_answer=solver_answer,
                    architect_code=architect_code,
                    director_plan=director_plan,
                    animator_plan=animator_plan,
                    coder_output=coder_output,
                    run_dir=run_dir,
                    runtime_dir=runtime_dir,
                    coder_failed=coder_failed,
                )

        if (not quantizer_raw) or (not solver_quantities.get("items")):
            self._check_cancelled()
            emit_progress("DeepSeek 正在整理/数值化物理量...")
            quantizer_raw = self._run_quantizer(
                solver_solution=solver_answer,
            )
            solver_quantities = self._parse_quantizer_output(quantizer_raw, self._default_solver_quantities())
            quantizer_output_path.write_text(quantizer_raw, encoding="utf-8")
            deepseek_quantities_path.write_text(
                json.dumps(solver_quantities, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            quantities_path.write_text(
                json.dumps(solver_quantities, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            emit_stage("deepseek", json.dumps(solver_quantities, ensure_ascii=False, indent=2))
            if stop_stage == "deepseek":
                emit_progress("运行结束...")
                return AgentOutputs(
                    solver_answer=solver_answer,
                    architect_code=architect_code,
                    director_plan=director_plan,
                    animator_plan=animator_plan,
                    coder_output=coder_output,
                    run_dir=run_dir,
                    runtime_dir=runtime_dir,
                    coder_failed=coder_failed,
                )

        if not architect_code:
            self._check_cancelled()
            emit_progress("Architect 正在生成 base_scene...")
            architect_code = self._run_architect(image_path)
            architect_path.write_text(architect_code, encoding="utf-8")
            emit_stage("architect", architect_code)
            self._write_base_scene(runtime_dir, architect_code)
            if stop_stage == "architect":
                emit_progress("运行结束...")
                return AgentOutputs(
                    solver_answer=solver_answer,
                    architect_code=architect_code,
                    director_plan=director_plan,
                    animator_plan=animator_plan,
                    coder_output=coder_output,
                    run_dir=run_dir,
                    runtime_dir=runtime_dir,
                    coder_failed=coder_failed,
                )
        else:
            self._write_base_scene(runtime_dir, architect_code)

        if not director_plan:
            self._check_cancelled()
            emit_progress("Director 正在规划动画...")
            director_plan = self._run_director(solver_answer, architect_code, image_path)
            director_path.write_text(director_plan, encoding="utf-8")
            emit_stage("director", director_plan)
            if stop_stage == "director":
                emit_progress("运行结束...")
                return AgentOutputs(
                    solver_answer=solver_answer,
                    architect_code=architect_code,
                    director_plan=director_plan,
                    animator_plan=animator_plan,
                    coder_output=coder_output,
                    run_dir=run_dir,
                    runtime_dir=runtime_dir,
                    coder_failed=coder_failed,
                )

        if not animator_plan:
            self._check_cancelled()
            emit_progress("Animator 正在生成动画代码包...")
            animator_plan = self._run_animator(
                quantities=solver_quantities,
                solver_solution=solver_answer,
                architect_code=architect_code,
                director_plan=director_plan,
                image_path=image_path,
                runtime_dir=runtime_dir,
            )
            animator_path.write_text(animator_plan, encoding="utf-8")
            emit_stage("animator", animator_plan)
            if stop_stage == "animator":
                emit_progress("运行结束...")
                return AgentOutputs(
                    solver_answer=solver_answer,
                    architect_code=architect_code,
                    director_plan=director_plan,
                    animator_plan=animator_plan,
                    coder_output=coder_output,
                    run_dir=run_dir,
                    runtime_dir=runtime_dir,
                    coder_failed=coder_failed,
                )

        self._check_cancelled()
        emit_progress("Coder 正在集成动画代码并调用工具...")
        coder_output, coder_failed = self._run_coder(
            director_plan,
            render_options,
            runtime_dir=runtime_dir,
            emit_progress=emit_progress,
        )
        coder_path.write_text(coder_output, encoding="utf-8")
        emit_stage("coder", coder_output)

        self._check_cancelled()
        emit_progress("运行结束...")

        return AgentOutputs(
            solver_answer=solver_answer,
            architect_code=architect_code,
            director_plan=director_plan,
            animator_plan=animator_plan,
            coder_output=coder_output,
            run_dir=run_dir,
            runtime_dir=runtime_dir,
            coder_failed=coder_failed,
        )
    def _run_solver(self, image_path: Path, solver_prompt_text: str = "") -> str:
        role_cfg = self.config.roles["solver"]
        llm = create_chat_model(role_cfg, self.config.timeout_s * 2)

        system_prompt = load_prompt("solver_system.md")
        messages: List = []
        if system_prompt:
            messages.append(SystemMessage(content=system_prompt))

        user_text = (
            "请读取题目图片并解题。\n"
            "输出必须包含解题过程(SOLUTION)。\n"
            "请严格使用以下标签格式输出：\n"
            "[SOLUTION]...[/SOLUTION]"
        )
        if solver_prompt_text:
            user_text += (
                "\n\n"
                "用户补充要求（来自工作台输入框）：\n"
                f"{solver_prompt_text}"
            )
        messages.append(self._build_multimodal_message(user_text, image_path))
        return self._invoke_to_text(llm, messages, "Solver")

    def _run_quantizer(self, solver_solution: str) -> str:
        role_cfg = self.config.roles.get("quantizer") or self.config.roles["coder"]
        if not (str(role_cfg.model or "").strip() and str(role_cfg.api_key or "").strip()):
            role_cfg = self.config.roles["coder"]
        llm = create_chat_model(role_cfg, self.config.timeout_s)

        system_prompt = load_prompt("quantizer_system.md")
        messages: List = []
        if system_prompt:
            messages.append(SystemMessage(content=system_prompt))

        user_text = (
            "请基于 solver 解题过程，产出规范化物理量 JSON。\n"
            "如果物理量已经是具体数值，主要做字段整理与单位统一；\n"
            "如果物理量主要是字母表达，请完成数值化（给出 value），无法唯一数值化时 value=null 并在 note 说明。\n\n"
            f"[SOLUTION]\n{solver_solution}\n[/SOLUTION]"
        )
        messages.append(HumanMessage(content=user_text))
        return self._invoke_to_text(llm, messages, "DeepSeek Quantizer")

    @classmethod
    def _parse_quantizer_output(cls, raw_text: str, fallback: dict | None = None) -> dict:
        default_quantities = cls._normalize_solver_quantities(fallback or cls._default_solver_quantities())
        text = str(raw_text or "").strip()
        if not text:
            return default_quantities

        tagged = re.search(r"\[QUANTITIES_JSON\](.*?)\[/QUANTITIES_JSON\]", text, flags=re.DOTALL | re.IGNORECASE)
        if tagged:
            candidate = tagged.group(1).strip()
            try:
                return cls._normalize_solver_quantities(json.loads(candidate))
            except Exception:
                return default_quantities

        fenced_json = re.search(r"```json\s*(\{[\s\S]*?\})\s*```", text, flags=re.IGNORECASE)
        if fenced_json:
            try:
                return cls._normalize_solver_quantities(json.loads(fenced_json.group(1).strip()))
            except Exception:
                return default_quantities

        try:
            if text.startswith("{") and text.endswith("}"):
                return cls._normalize_solver_quantities(json.loads(text))
        except Exception:
            return default_quantities
        return default_quantities

    @staticmethod
    def _default_solver_quantities() -> dict:
        return {
            "schema_version": "1.0",
            "items": [],
        }

    @classmethod
    def _normalize_solver_quantities(cls, payload: object) -> dict:
        if not isinstance(payload, dict):
            return cls._default_solver_quantities()
        normalized = dict(payload)
        normalized.setdefault("schema_version", "1.0")
        if not isinstance(normalized.get("items"), list):
            normalized["items"] = []
        return normalized

    @classmethod
    def _extract_solver_solution(cls, raw_text: str) -> str:
        text = str(raw_text or "").strip()
        if not text:
            return ""

        solution_match = re.search(r"\[SOLUTION\](.*?)\[/SOLUTION\]", text, flags=re.DOTALL | re.IGNORECASE)
        solution = solution_match.group(1).strip() if solution_match else text
        return solution or text

    @classmethod
    def _read_solver_quantities(cls, path: Path) -> dict:
        if not path.exists():
            return cls._default_solver_quantities()
        try:
            return cls._normalize_solver_quantities(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            return cls._default_solver_quantities()

    def _run_architect(self, image_path: Path) -> str:
        role_cfg = self.config.roles["architect"]
        llm = create_chat_model(role_cfg, self.config.timeout_s)

        system_prompt = load_prompt("architect_system.md")
        messages: List = []
        if system_prompt:
            messages.append(SystemMessage(content=system_prompt))

        user_text = f"图片如上"
        messages.append(self._build_multimodal_message(user_text, image_path))

        return self._invoke_to_text(llm, messages, "Architect")

    def _run_director(
        self,
        solution: str,
        architect_code: str,
        image_path: Path,
    ) -> str:
        role_cfg = self.config.roles["director"]
        llm = create_chat_model(role_cfg, self.config.timeout_s)

        system_prompt = load_prompt("director_system.md")
        messages: List = []
        if system_prompt:
            messages.append(SystemMessage(content=system_prompt))

        user_text = (
            f"题目解答---------------------------\n{solution}\n\n----------------------------\n"
            f"Manim 动画规划书--------------------\n\n{architect_code}"
        )
        messages.append(self._build_multimodal_message(user_text, image_path))

        return self._invoke_to_text(llm, messages, "Director")

    def _run_coder(
        self,
        director_plan: str,
        render_options: dict,
        runtime_dir: Path,
        emit_progress: Callable[[str], None] | None = None,
    ) -> tuple[str, bool]:
        """Run the coder agent.  Returns (output_text, coder_failed)."""
        role_cfg = self.config.roles["coder"]
        llm = create_chat_model(role_cfg, self.config.timeout_s)
        project_root = Path(__file__).resolve().parents[2]

        shared_state: dict = {}
        internal_opts = dict(render_options or {})
        if self._current_run_dir is not None:
            internal_opts["agent_run_dir"] = str(self._current_run_dir)
        internal_opts["workflow_runtime_dir"] = str(runtime_dir)
        tools = build_coder_tools(project_root, internal_opts, shared_state=shared_state)
        llm_with_tools = llm.bind_tools(tools)
        tool_map = {tool.name: tool for tool in tools}

        system_prompt = load_prompt("coder_system.md")
        messages: List = []
        if system_prompt:
            messages.append(SystemMessage(content=system_prompt))
        try:
            warnings_text = load_prompt("coder_api_warnings.md")
        except Exception as exc:
            warnings_text = f"警示清单读取失败：{exc}"
        boundary_policy_text = build_boundary_policy_prompt()

        user_prompt = (
            "请基于 runtime 内 Animator 代码包完成集成、校验与渲染，并将 Director 规划书仅作为语义参考。\n"
            "第一步必须调用 write_narration_script 写入 runtime/narration.txt，未写入旁白前禁止场景写入、公式布局写入、语法/布局校验与渲染。\n"
            "你必须先读取 runtime/animator_codegen/manifest.json，再处理 scene 包装层与动画模块。\n"
            "你必须通过工具读取 runtime 目录中的 base_scene.py，不要要求外部再传 Manim 对象代码。\n"
            "路径参数必须使用 runtime 相对路径（例如 base_scene.py, scene1/scene1.py），禁止项目根路径或绝对路径。\n"
            "只要涉及公式入场/出场与位置安排，必须调用 insert_formula_layout_plan(scene_index, spec_json) 由工具直接写入 animator 模块；禁止手写公式堆叠位置逻辑。\n"
            "调用 make_manim_video 前必须先执行 validate_scene_syntax，语法通过后才允许渲染。\n"
            "公式强调只允许 outline_only，不允许填充高亮框；公式在高亮后必须再次边界检查。\n\n"
            f"统一边界策略（必须遵守）：\n---------------\n{boundary_policy_text}\n--------------------------\n\n"
            f"Coder 警示清单（必须遵守）：\n---------------\n{warnings_text}\n--------------------------\n\n"
            f"Director 规划书（仅参考）：\n---------------\n{director_plan}\n--------------------------"
        )
        messages.append(HumanMessage(content=user_prompt))

        final_text = ""
        max_rounds = 26
        # Allow exactly one retry after the first render failure.
        # This means we stop after 2 total failures.
        max_video_failures = 2
        video_failure_count = 0
        coder_failed = False
        repeated_sig = None
        repeated_count = 0
        locked_scene_index: int | None = None
        locked_error_signature = ""
        locked_error_repeat = 0
        tool_history: List[str] = []
        formula_layout_validated = False
        formula_layout_ok = False
        validator_runtime_error = False
        validator_runtime_error_count = 0
        last_make_video_result = ""
        last_make_video_ok = False
        post_render_narration_writes = 0
        coder_tool_log: Path | None = None
        if self._current_run_dir is not None:
            coder_tool_log = self._current_run_dir / "coder_tools.log"

        def _append_coder_tool_log(line: str) -> None:
            if coder_tool_log is None:
                return
            coder_tool_log.parent.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with coder_tool_log.open("a", encoding="utf-8") as handle:
                handle.write(f"[{stamp}] {line.rstrip()}\n")

        def _emit_progress_tool(name: str, round_idx: int) -> None:
            if emit_progress is None:
                return
            emit_progress(f"Coder 正在调用工具: {name}（第 {round_idx + 1} 轮）")

        def _runtime_narration_ready() -> bool:
            narration_path = runtime_dir / "narration.txt"
            return narration_path.exists() and narration_path.is_file()

        def _is_scene_wrapper_path(path_value: object) -> bool:
            rel = str(path_value or "").replace("\\", "/").strip().lower()
            return bool(re.match(r"^scene\d+/scene\d+\.py$", rel))

        def _requires_narration_first(tool_name: str, tool_args: dict) -> bool:
            if tool_name in {
                "write_scene_code",
                "insert_formula_layout_plan",
                "validate_scene_syntax",
                "validate_formula_layout",
                "make_manim_video",
            }:
                return True
            if tool_name == "write_runtime_file" and _is_scene_wrapper_path(tool_args.get("path")):
                return True
            return False

        for round_idx in range(max_rounds):
            self._check_cancelled()
            response = llm_with_tools.invoke(messages)
            messages.append(response)
            tool_calls = getattr(response, "tool_calls", None) or []
            if not tool_calls:
                final_text = self._normalize_content(getattr(response, "content", ""))
                break

            call_sig = "|".join(
                [f"{call.get('name','')}:{json.dumps(call.get('args', {}), ensure_ascii=False, sort_keys=True)}" for call in tool_calls]
            )
            if call_sig == repeated_sig:
                repeated_count += 1
            else:
                repeated_sig = call_sig
                repeated_count = 1

            for call in tool_calls:
                name = call.get("name", "")
                args = call.get("args", {})
                call_id = call.get("id", "")
                tool = tool_map.get(name)
                _emit_progress_tool(name, round_idx)
                _append_coder_tool_log(
                    f"[round {round_idx + 1}] CALL {name} args={json.dumps(args, ensure_ascii=False)}"
                )

                if _requires_narration_first(name, args) and not _runtime_narration_ready():
                    result = (
                        "Tool execution blocked: runtime/narration.txt is missing. "
                        "Call write_narration_script(content, mode='overwrite') first."
                    )
                    _append_coder_tool_log(
                        f"[round {round_idx + 1}] RESULT {name} -> {result}"
                    )
                    tool_history.append(f"[round {round_idx + 1}] {name}({args}) -> {result[:260]}")
                    messages.append(ToolMessage(content=str(result), tool_call_id=call_id))
                    continue

                if locked_scene_index is not None:
                    if name == "write_narration_script":
                        result = (
                            f"Tool execution blocked: scene{locked_scene_index} previously failed. "
                            "Fix failed scene before narration changes."
                        )
                        _append_coder_tool_log(
                            f"[round {round_idx + 1}] RESULT {name} -> {result}"
                        )
                        tool_history.append(f"[round {round_idx + 1}] {name}({args}) -> {result[:260]}")
                        messages.append(ToolMessage(content=str(result), tool_call_id=call_id))
                        continue
                    if name == "write_scene_code":
                        try:
                            scene_index = int(args.get("scene_index", 0))
                        except Exception:
                            scene_index = 0
                        if scene_index != locked_scene_index:
                            result = (
                                f"Tool execution blocked: must patch failed scene index={locked_scene_index} first; "
                                f"write_scene_code({scene_index}) is not allowed now."
                            )
                            _append_coder_tool_log(
                                f"[round {round_idx + 1}] RESULT {name} -> {result}"
                            )
                            tool_history.append(f"[round {round_idx + 1}] {name}({args}) -> {result[:260]}")
                            messages.append(ToolMessage(content=str(result), tool_call_id=call_id))
                            continue
                if name == "make_manim_video":
                    if not formula_layout_validated or not formula_layout_ok:
                        # Auto-run validator once to provide concrete diagnostics instead of
                        # repeatedly returning a generic block message.
                        validator_tool = tool_map.get("validate_formula_layout")
                        if validator_tool is not None:
                            try:
                                validator_result = validator_tool.invoke({})
                            except Exception as exc:
                                validator_result = f"Tool execution failed: {exc}"
                            _append_coder_tool_log(
                                f"[round {round_idx + 1}] AUTO_CALL validate_formula_layout args={{}}"
                            )
                            _append_coder_tool_log(
                                f"[round {round_idx + 1}] AUTO_RESULT validate_formula_layout -> {str(validator_result)}"
                            )
                            try:
                                parsed_validator = json.loads(str(validator_result))
                                formula_layout_validated = True
                                formula_layout_ok = bool(parsed_validator.get("ok", False))
                                if bool(parsed_validator.get("runtime_error", False)):
                                    runtime_errors = parsed_validator.get("runtime_errors", [])
                                    detail = ""
                                    if isinstance(runtime_errors, list) and runtime_errors:
                                        detail = str(runtime_errors[0].get("error", "")).strip()
                                    if not detail:
                                        detail = "公式布局校验器运行时异常。"
                                    shared_state["summary"] = (
                                        "validate_formula_layout 出现运行时异常，已要求 Coder 优先修复错误 scene 后继续重试。"
                                        f"\n错误详情：{detail}"
                                    )
                                    validator_runtime_error = True
                                    validator_runtime_error_count += 1
                                    messages.append(
                                        HumanMessage(
                                            content=(
                                                "validate_formula_layout 出现 runtime_error。"
                                                "请先修复触发异常的 scene 代码，再次调用 validate_formula_layout。"
                                                "不要停止，不要写总结，继续修复。\n"
                                                f"错误详情：{detail}"
                                            )
                                        )
                                    )
                            except (json.JSONDecodeError, TypeError):
                                formula_layout_validated = True
                                formula_layout_ok = False

                            if validator_runtime_error:
                                result = str(validator_result)
                                _append_coder_tool_log(
                                    f"[round {round_idx + 1}] RESULT {name} -> {result}"
                                )
                                tool_history.append(f"[round {round_idx + 1}] {name}({args}) -> {result[:260]}")
                                messages.append(ToolMessage(content=str(result), tool_call_id=call_id))
                                continue

                            if not formula_layout_ok:
                                result = (
                                    "Tool execution blocked: validate_formula_layout did not pass. "
                                    f"Validator result: {str(validator_result)}"
                                )
                                _append_coder_tool_log(
                                    f"[round {round_idx + 1}] RESULT {name} -> {result}"
                                )
                                tool_history.append(f"[round {round_idx + 1}] {name}({args}) -> {result[:260]}")
                                messages.append(ToolMessage(content=str(result), tool_call_id=call_id))
                                continue
                        else:
                            result = (
                                "Tool execution blocked: validate_formula_layout tool unavailable before make_manim_video."
                            )
                            _append_coder_tool_log(
                                f"[round {round_idx + 1}] RESULT {name} -> {result}"
                            )
                            tool_history.append(f"[round {round_idx + 1}] {name}({args}) -> {result[:260]}")
                            messages.append(ToolMessage(content=str(result), tool_call_id=call_id))
                            continue

                if tool is None:
                    result = f"Unknown tool: {name}"
                else:
                    try:
                        result = tool.invoke(args)
                    except Exception as exc:
                        result = f"Tool execution failed: {exc}"
                _append_coder_tool_log(
                    f"[round {round_idx + 1}] RESULT {name} -> {str(result)}"
                )

                # Track make_manim_video failures
                if name == "make_manim_video":
                    last_make_video_result = str(result)
                    try:
                        parsed = json.loads(result)
                        last_make_video_ok = bool(parsed.get("ok", False))
                        if not last_make_video_ok:
                            video_failure_count += 1
                            failed_scene = str(parsed.get("error_scene_index", "")).strip()
                            error_signature = str(parsed.get("error_summary", "")).strip()
                            if failed_scene.isdigit():
                                locked_scene_index = int(failed_scene)
                            if error_signature:
                                if error_signature == locked_error_signature:
                                    locked_error_repeat += 1
                                else:
                                    locked_error_signature = error_signature
                                    locked_error_repeat = 1
                        else:
                            locked_scene_index = None
                            locked_error_signature = ""
                            locked_error_repeat = 0
                    except (json.JSONDecodeError, TypeError):
                        last_make_video_ok = False
                        video_failure_count += 1
                elif name == "validate_formula_layout":
                    try:
                        parsed = json.loads(str(result))
                        formula_layout_validated = True
                        formula_layout_ok = bool(parsed.get("ok", False))
                        if bool(parsed.get("runtime_error", False)):
                            runtime_errors = parsed.get("runtime_errors", [])
                            detail = ""
                            if isinstance(runtime_errors, list) and runtime_errors:
                                detail = str(runtime_errors[0].get("error", "")).strip()
                            if not detail:
                                detail = "公式布局校验器运行时异常。"
                            shared_state["summary"] = (
                                "validate_formula_layout 出现运行时异常，已要求 Coder 优先修复错误 scene 后继续重试。"
                                f"\n错误详情：{detail}"
                            )
                            validator_runtime_error = True
                            validator_runtime_error_count += 1
                            messages.append(
                                HumanMessage(
                                    content=(
                                        "validate_formula_layout 出现 runtime_error。"
                                        "请先修复触发异常的 scene 代码，再次调用 validate_formula_layout。"
                                        "不要停止，不要写总结，继续修复。\n"
                                        f"错误详情：{detail}"
                                    )
                                )
                            )
                    except (json.JSONDecodeError, TypeError):
                        formula_layout_validated = True
                        formula_layout_ok = False

                    # Avoid a false max-round failure when validation passes on the last round:
                    # if no render has been attempted yet, auto-trigger one render immediately.
                    if (
                        formula_layout_ok
                        and not validator_runtime_error
                        and not last_make_video_result
                    ):
                        make_tool = tool_map.get("make_manim_video")
                        if make_tool is not None:
                            _emit_progress_tool("make_manim_video", round_idx)
                            try:
                                auto_render_args = {"output": "lesson.mp4"}
                                _append_coder_tool_log(
                                    f"[round {round_idx + 1}] AUTO_CALL make_manim_video args={json.dumps(auto_render_args, ensure_ascii=False)}"
                                )
                                auto_render_result = make_tool.invoke(auto_render_args)
                            except Exception as exc:
                                auto_render_result = f"Tool execution failed: {exc}"
                            _append_coder_tool_log(
                                f"[round {round_idx + 1}] AUTO_RESULT make_manim_video -> {str(auto_render_result)}"
                            )
                            last_make_video_result = str(auto_render_result)
                            try:
                                parsed_render = json.loads(last_make_video_result)
                                last_make_video_ok = bool(parsed_render.get("ok", False))
                                if not last_make_video_ok:
                                    video_failure_count += 1
                                    failed_scene = str(parsed_render.get("error_scene_index", "")).strip()
                                    error_signature = str(parsed_render.get("error_summary", "")).strip()
                                    if failed_scene.isdigit():
                                        locked_scene_index = int(failed_scene)
                                    if error_signature:
                                        if error_signature == locked_error_signature:
                                            locked_error_repeat += 1
                                        else:
                                            locked_error_signature = error_signature
                                            locked_error_repeat = 1
                                else:
                                    locked_scene_index = None
                                    locked_error_signature = ""
                                    locked_error_repeat = 0
                            except (json.JSONDecodeError, TypeError):
                                last_make_video_ok = False
                                video_failure_count += 1
                            tool_history.append(
                                f"[round {round_idx + 1}] auto_make_manim_video({{}}) -> {str(auto_render_result)[:260]}"
                            )
                            # Keep model context consistent with internal auto-render execution,
                            # so next round can fix concrete render errors instead of guessing.
                            messages.append(
                                HumanMessage(
                                    content=(
                                        "系统自动调用 make_manim_video 的结果如下，请基于该结果继续修复：\n"
                                        f"{str(auto_render_result)}"
                                    )
                                )
                            )
                elif name == "write_narration_script" and last_make_video_result:
                    post_render_narration_writes += 1

                tool_history.append(f"[round {round_idx + 1}] {name}({args}) -> {str(result)[:260]}")
                messages.append(ToolMessage(content=str(result), tool_call_id=call_id))

                if validator_runtime_error and validator_runtime_error_count >= 3:
                    coder_failed = True
                    shared_state["summary"] = (
                        "Coder 在 validate_formula_layout runtime_error 上连续重试 3 次仍失败，"
                        "已停止自动重试以避免无效循环。"
                    )
                    break

            if validator_runtime_error and validator_runtime_error_count >= 3:
                break

            if locked_scene_index is not None and locked_error_repeat >= 2:
                coder_failed = True
                shared_state["summary"] = (
                    f"Coder 在 scene{locked_scene_index} 遇到相同渲染错误重复失败（{locked_error_repeat} 次）。"
                    "已自动熔断以避免无效重试，请定向修复该 scene 后再重试。"
                )
                break

            if locked_scene_index is not None and not last_make_video_ok:
                messages.append(
                    HumanMessage(
                        content=(
                            f"上一轮渲染失败点在 scene{locked_scene_index}。"
                            "下一轮只允许定向修复该 scene 并重试渲染；"
                            "不要改 narration，不要重写其他 scene。"
                        )
                    )
                )

            # Exit immediately once video render succeeds to avoid redundant tool loops.
            if last_make_video_ok:
                shared_state["summary"] = (
                    "视频已成功生成，Coder 自动结束。\n"
                    f"最后一次 make_manim_video 结果：\n{last_make_video_result}"
                )
                break

            # Guardrail: after render has already been attempted, repeated narration rewrites
            # indicate the agent drifted away from fixing the concrete render error.
            if post_render_narration_writes >= 2 and video_failure_count > 0:
                coder_failed = True
                shared_state["summary"] = (
                    "Coder 在渲染失败后陷入旁白反复改写，未继续修复场景错误，"
                    "已提前终止以避免无效轮次消耗。"
                )
                break

            # Check if we've hit the video failure limit
            if video_failure_count >= max_video_failures:
                coder_failed = True
                messages.append(
                    HumanMessage(
                        content=(
                            f"make_manim_video 已连续失败 {video_failure_count} 次，不要再重试了。\n"
                            "请仔细阅读上面的错误日志，分析失败的根本原因。\n"
                            "然后调用 report_summary 工具，将错误原因和你的分析输出给用户。\n"
                            "只需输出错误分析。"
                        )
                    )
                )
                self._check_cancelled()
                response = llm_with_tools.invoke(messages)
                messages.append(response)
                error_tool_calls = getattr(response, "tool_calls", None) or []
                for call in error_tool_calls:
                    name = call.get("name", "")
                    args = call.get("args", {})
                    call_id = call.get("id", "")
                    tool = tool_map.get(name)
                    if tool is not None:
                        try:
                            tool.invoke(args)
                        except Exception:
                            pass
                summary_candidate = self._normalize_content(
                    getattr(response, "content", "make_manim_video 失败，未能生成视频。")
                )
                existing_summary = str(shared_state.get("summary") or "").strip()
                if (not existing_summary) or ("make_manim_video" not in existing_summary and "失败" not in existing_summary):
                    shared_state["summary"] = summary_candidate
                break

            if repeated_count >= 3:
                messages.append(
                    HumanMessage(
                        content=(
                            "你已经连续多轮重复相同工具调用。"
                            "请立即调用 report_summary 工具输出最终总结。"
                        )
                    )
                )
        else:
            # Reached max_rounds without breaking
            coder_failed = True
            summary = "\n".join(tool_history[-8:]) if tool_history else "无工具调用记录。"
            if last_make_video_result:
                shared_state["summary"] = f"Coder 达到最大轮次。最后一次渲染结果：\n{last_make_video_result}"
            else:
                shared_state["summary"] = f"Coder 达到最大轮次。\n{summary}"

        # Global consistency guard: successful render must dominate final status.
        if last_make_video_ok:
            coder_failed = False
            if not str(shared_state.get("summary") or "").strip():
                shared_state["summary"] = (
                    "视频已成功生成，Coder 自动结束。\n"
                    f"最后一次 make_manim_video 结果：\n{last_make_video_result}"
                )

        # Prefer the summary set via the report_summary tool.
        summary_text = str(shared_state.get("summary") or "").strip()
        final_text = summary_text or str(final_text or "").strip()
        if not final_text:
            fallback_lines = []
            if coder_failed:
                fallback_lines.append("Coder 执行失败，但未返回有效总结。")
            else:
                fallback_lines.append("Coder 未返回可显示内容。")
            if last_make_video_result:
                fallback_lines.append("最后一次 make_manim_video 结果：")
                fallback_lines.append(last_make_video_result)
            if tool_history:
                fallback_lines.append("最近工具调用：")
                fallback_lines.extend(tool_history[-6:])
            final_text = "\n".join(fallback_lines).strip()
        return final_text, coder_failed

    def rerun_coder(
        self,
        animator_plan: str,
        director_plan: str,
        render_options: dict,
        run_dir: Path,
        on_progress: Callable[[str], None] | None = None,
        on_stage_result: Callable[[str, str], None] | None = None,
    ) -> AgentOutputs:
        """Re-run only the coder step, reusing prior workflow outputs."""
        emit_progress = on_progress or (lambda _msg: None)
        emit_stage = on_stage_result or (lambda _stage, _content: None)

        # Reuse the same run context so _run_coder can append coder_tools.log
        # under run_dir, consistent with normal/continue workflow paths.
        self._current_run_dir = run_dir

        self._cancel_requested = False
        runtime_dir = run_dir / "runtime"
        meta_path = run_dir / "meta.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                runtime_value = str(meta.get("runtime_dir", "") or "").strip()
                if runtime_value:
                    runtime_dir = Path(runtime_value)
            except Exception:
                pass
        runtime_dir.mkdir(parents=True, exist_ok=True)

        emit_progress("Coder 正在重新集成动画代码并调用工具...")
        coder_output, coder_failed = self._run_coder(
            director_plan,
            render_options,
            runtime_dir=runtime_dir,
            emit_progress=emit_progress,
        )
        if not str(coder_output or "").strip():
            coder_output = "Coder 执行结束，但未返回有效输出。请查看控制台日志与 make_manim_video 错误信息。"
        emit_stage("coder", coder_output)

        # Overwrite coder output in run_dir
        (run_dir / "coder_output.md").write_text(coder_output, encoding="utf-8")

        # Read back earlier outputs for the return value
        solver_answer = self._read_or_default(run_dir / "solver_answer.md")
        architect_code = self._read_or_default(run_dir / "architect_code.py")
        loaded_animator_plan = self._read_or_default(run_dir / "animator_codegen.md")
        if not loaded_animator_plan:
            loaded_animator_plan = self._read_or_default(run_dir / "animator_plan.md")
        if not loaded_animator_plan:
            loaded_animator_plan = animator_plan

        return AgentOutputs(
            solver_answer=solver_answer,
            architect_code=architect_code,
            director_plan=director_plan,
            animator_plan=loaded_animator_plan,
            coder_output=coder_output,
            run_dir=run_dir,
            runtime_dir=runtime_dir,
            coder_failed=coder_failed,
        )

    def _run_animator(
        self,
        quantities: dict,
        solver_solution: str,
        architect_code: str,
        director_plan: str,
        image_path: Path,
        runtime_dir: Path,
    ) -> str:
        role_cfg = self.config.roles["animator"]
        llm = create_chat_model(role_cfg, self.config.timeout_s)

        system_prompt = load_prompt("animator_system.md")
        messages: List = []
        if system_prompt:
            messages.append(SystemMessage(content=system_prompt))

        base_scene_path = runtime_dir / "base_scene.py"
        base_scene_code = ""
        if base_scene_path.exists():
            base_scene_code = base_scene_path.read_text(encoding="utf-8")
        quantities_json = json.dumps(self._normalize_solver_quantities(quantities), ensure_ascii=False, indent=2)
        try:
            animator_warnings_text = load_prompt("coder_api_warnings.md")
        except Exception as exc:
            animator_warnings_text = f"警示清单读取失败：{exc}"

        user_text = (
            "请基于以下输入输出 Animator 动画代码包。\n"
            "必须使用 FILE: <runtime-relative-path> + python 代码块 的格式输出多个文件。\n"
            "至少输出：animator_codegen/scene1_anim.py ... sceneN_anim.py。\n"
            "每个文件必须定义 apply_sceneX_animation(scene) 函数。\n"
            "仅生成 sceneX_anim.py 动画文件；sceneX.py 包装层由工作流处理。\n"
            "base_scene 主体几何保持在 base_scene.py。\n\n"
            "语义优先级（必须遵守）：\n"
            "1) 题目图片 + base_scene 静态几何真值\n"
            "2) Solver 解题过程与物理结论\n"
            "3) Director 分镜叙事\n"
            "4) Solver quantities JSON（仅数值参考，不是唯一语义来源）\n"
            "禁止为了命中目标而无依据新增轨迹段；若主轨迹不闭合，应回溯参数与约束，不得用额外直线/曲线补段。\n\n"
            f"Animator 警示清单（必须遵守）：\n---------------\n{animator_warnings_text}\n--------------------------\n\n"
            f"Solver 解题文本----------------------\n{solver_solution}\n\n----------------------------\n"
            f"Solver 物理量数值(JSON)------------\n{quantities_json}\n\n----------------------------\n"
            f"Architect 静态代码--------------------\n{architect_code}\n\n----------------------------\n"
            f"Director 规划书----------------------\n{director_plan}\n\n----------------------------\n"
            f"base_scene.py-----------------------\n{base_scene_code}\n----------------------------"
        )
        messages.append(self._build_multimodal_message(user_text, image_path))
        raw_text = self._invoke_to_text(llm, messages, "Animator")
        codegen = write_animator_codegen(runtime_dir, raw_text)
        raw_output_path = runtime_dir / "animator_codegen" / "raw_animator_output.md"
        raw_output_path.parent.mkdir(parents=True, exist_ok=True)
        raw_output_path.write_text(raw_text, encoding="utf-8")
        return codegen.summary

    @staticmethod
    def _read_or_default(path: Path, default: str = "") -> str:
        if path.exists():
            return path.read_text(encoding="utf-8")
        return default

    @staticmethod
    def _normalize_content(content: object) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text")
                    if text:
                        parts.append(str(text))
                else:
                    parts.append(str(item))
            return "\n".join(parts).strip()
        return str(content)

    @staticmethod
    def _classify_error(exc: Exception) -> str:
        msg = str(exc).lower()
        if (
            "invalid api key" in msg
            or "api key not valid" in msg
            or "unauthorized" in msg
            or "401" in msg
            or "permission denied" in msg
        ):
            return "API Key 无效或权限不足。"
        if ("model" in msg and "not found" in msg) or "404" in msg or "unknown model" in msg:
            return "模型名称错误，或该模型当前不可用。"
        if "quota" in msg or "insufficient" in msg or "billing" in msg or "429" in msg or "rate limit" in msg:
            return "配额不足或触发速率限制。"
        if "timeout" in msg or "timed out" in msg:
            return "请求超时，请检查网络或稍后重试。"
        if (
            "connection" in msg
            or "dns" in msg
            or "proxy" in msg
            or "ssl" in msg
            or "name or service not known" in msg
            or "failed to establish" in msg
        ):
            return "网络或代理连接失败。"
        return "调用失败，请检查模型名、API Key 和网络。"

    def _invoke_to_text(self, llm, messages: List, stage: str) -> str:
        try:
            response = llm.invoke(messages)
            content = response.content if hasattr(response, "content") else response
            return self._normalize_content(content)
        except Exception as exc:
            reason = self._classify_error(exc)
            raise RuntimeError(f"{stage} 调用失败：{reason} 原始错误：{exc}") from exc

    def _build_multimodal_message(self, text: str, image_path: Path | None) -> HumanMessage:
        if not image_path:
            return HumanMessage(content=text)

        data_url = self._image_to_data_url(image_path)
        content = [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]
        return HumanMessage(content=content)

    @staticmethod
    def _image_to_data_url(image_path: Path) -> str:
        ext = image_path.suffix.lower().lstrip(".") or "png"
        mime = f"image/{ext}"
        encoded = base64.b64encode(image_path.read_bytes()).decode("utf-8")
        return f"data:{mime};base64,{encoded}"

    @staticmethod
    def _agent_runs_root() -> Path:
        return Path(__file__).resolve().parents[2] / "outputs" / "agent_runs"

    @staticmethod
    def _make_run_dir() -> Path:
        root = AgentWorkflow._agent_runs_root()
        base = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_dir = root / base
        suffix = 1
        while run_dir.exists():
            run_dir = root / f"{base}_{suffix:02d}"
            suffix += 1
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir

    @staticmethod
    def _prepare_runtime_workspace(run_dir: Path) -> Path:
        runtime_dir = run_dir / "runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        return runtime_dir

    @staticmethod
    def _write_base_scene(tmp_dir: Path, architect_code: str) -> Path:
        base_scene_path = tmp_dir / "base_scene.py"
        base_scene_path.write_text(AgentWorkflow._extract_python_code(architect_code), encoding="utf-8")
        return base_scene_path

    @staticmethod
    def _extract_python_code(text: str) -> str:
        raw = text.strip()
        block = re.search(r"```(?:python|py)?\s*(.*?)```", raw, flags=re.IGNORECASE | re.DOTALL)
        if block:
            return block.group(1).strip() + "\n"
        return raw + ("\n" if not raw.endswith("\n") else "")
