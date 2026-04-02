from __future__ import annotations

import base64
import json
import re
import shutil
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from .config import AgentConfig, load_agent_config
from .coder_tools import build_coder_tools
from .llm_factory import create_chat_model
from .prompts import load_prompt


@dataclass
class AgentOutputs:
    solver_answer: str
    architect_code: str
    director_plan: str
    coder_output: str
    run_dir: Path
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

        # Read source bytes before tmp cleanup, because pasted images may be
        # temporarily stored under tmp/ and would otherwise be deleted.
        image_name = image_path.name
        image_bytes = image_path.read_bytes()

        tmp_dir = self._prepare_tmp_workspace()
        run_dir = self._make_run_dir()
        self._current_run_dir = run_dir
        image_copy_path = run_dir / image_name
        image_copy_path.write_bytes(image_bytes)

        # Write metadata early so we can resume if partially failed
        meta = {
            "image": image_copy_path.name,
            "render_options": render_options or {}
        }
        (run_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

        self._check_cancelled()
        emit_progress("Solver 正在解题...")
        solver_answer = self._run_solver(image_copy_path)
        (run_dir / "solver_answer.md").write_text(solver_answer, encoding="utf-8")
        emit_stage("solver", solver_answer)

        self._check_cancelled()
        emit_progress("Architect 正在生成 base_scene...")
        architect_code = self._run_architect(solver_answer, image_copy_path)
        (run_dir / "architect_code.py").write_text(architect_code, encoding="utf-8")
        emit_stage("architect", architect_code)
        self._write_base_scene(tmp_dir, architect_code)

        self._check_cancelled()
        emit_progress("Director 正在规划动画...")
        director_plan = self._run_director(solver_answer, architect_code, image_copy_path)
        (run_dir / "director_plan.md").write_text(director_plan, encoding="utf-8")
        emit_stage("director", director_plan)

        self._check_cancelled()
        emit_progress("Coder 正在编排场景并调用工具...")
        coder_output, coder_failed = self._run_coder(
            director_plan,
            render_options or {},
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
            coder_output=coder_output,
            run_dir=run_dir,
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
        tmp_dir = self._prepare_tmp_workspace()

        meta = {}
        meta_path = run_dir / "meta.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
        render_options = render_options or meta.get("render_options", {}) or {}

        image_name = str(meta.get("image", "") or "").strip()
        image_path = run_dir / image_name if image_name else None
        if image_path is None or not image_path.exists():
            raise ValueError("继续运行失败：找不到原始题目图片。")

        solver_path = run_dir / "solver_answer.md"
        architect_path = run_dir / "architect_code.py"
        director_path = run_dir / "director_plan.md"
        coder_path = run_dir / "coder_output.md"

        solver_answer = self._read_or_default(solver_path)
        architect_code = self._read_or_default(architect_path)
        director_plan = self._read_or_default(director_path)
        coder_output = self._read_or_default(coder_path)
        coder_failed = False

        # Force rerun from a specific stage regardless of existing artifacts.
        stage = (resume_from_stage or "").strip().lower()
        stop_stage = (stop_after_stage or "").strip().lower()
        if stage == "solver":
            solver_answer = ""
            architect_code = ""
            director_plan = ""
            coder_output = ""
        elif stage == "architect":
            architect_code = ""
            director_plan = ""
            coder_output = ""
        elif stage == "director":
            director_plan = ""
            coder_output = ""
        elif stage == "coder":
            coder_output = ""

        if not solver_answer:
            self._check_cancelled()
            emit_progress("Solver 正在解题...")
            solver_answer = self._run_solver(image_path)
            solver_path.write_text(solver_answer, encoding="utf-8")
            emit_stage("solver", solver_answer)
            if stop_stage == "solver":
                emit_progress("运行结束...")
                return AgentOutputs(
                    solver_answer=solver_answer,
                    architect_code=architect_code,
                    director_plan=director_plan,
                    coder_output=coder_output,
                    run_dir=run_dir,
                    coder_failed=coder_failed,
                )

        if not architect_code:
            self._check_cancelled()
            emit_progress("Architect 正在生成 base_scene...")
            architect_code = self._run_architect(solver_answer, image_path)
            architect_path.write_text(architect_code, encoding="utf-8")
            emit_stage("architect", architect_code)
            if stop_stage == "architect":
                emit_progress("运行结束...")
                return AgentOutputs(
                    solver_answer=solver_answer,
                    architect_code=architect_code,
                    director_plan=director_plan,
                    coder_output=coder_output,
                    run_dir=run_dir,
                    coder_failed=coder_failed,
                )

        self._write_base_scene(tmp_dir, architect_code)

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
                    coder_output=coder_output,
                    run_dir=run_dir,
                    coder_failed=coder_failed,
                )

        self._check_cancelled()
        emit_progress("Coder 正在编排场景并调用工具...")
        coder_output, coder_failed = self._run_coder(
            director_plan,
            render_options,
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
            coder_output=coder_output,
            run_dir=run_dir,
            coder_failed=coder_failed,
        )
    def _run_solver(self, image_path: Path) -> str:
        role_cfg = self.config.roles["solver"]
        llm = create_chat_model(role_cfg, self.config.timeout_s * 2)

        system_prompt = load_prompt("solver_system.md")
        messages: List = []
        if system_prompt:
            messages.append(SystemMessage(content=system_prompt))

        user_text = (
            "请读取题目图片并解题。\n"
            "输出包含：题意与条件、步骤推导、最终答案。"
        )
        messages.append(self._build_multimodal_message(user_text, image_path))

        return self._invoke_to_text(llm, messages, "Solver")

    def _run_architect(self, solution: str, image_path: Path) -> str:
        role_cfg = self.config.roles["architect"]
        llm = create_chat_model(role_cfg, self.config.timeout_s)

        system_prompt = load_prompt("architect_system.md")
        messages: List = []
        if system_prompt:
            messages.append(SystemMessage(content=system_prompt))

        user_text = f"瑙ｉ姝ラ锛歕n{solution}"
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
            f"瑙ｉ姝ラ锛歕n---------------------------\n{solution}\n\n----------------------------\n"
            f"Manim 瀵硅薄浠ｇ爜锛歕n--------------------\n\n{architect_code}"
        )
        messages.append(self._build_multimodal_message(user_text, image_path))

        return self._invoke_to_text(llm, messages, "Director")

    def _run_coder(
        self,
        director_plan: str,
        render_options: dict,
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
        tools = build_coder_tools(project_root, internal_opts, shared_state=shared_state)
        llm_with_tools = llm.bind_tools(tools)
        tool_map = {tool.name: tool for tool in tools}

        system_prompt = load_prompt("coder_system.md")
        messages: List = []
        if system_prompt:
            messages.append(SystemMessage(content=system_prompt))

        voice = str(render_options.get("voice", "none")).strip()
        bgm_path = str(render_options.get("bgm_path", "")).strip()

        if voice.lower() in ("none", "disable"):
            tts_hint = "关闭配音"
        else:
            tts_hint = "启用音色复刻"

        bgm_hint = "关闭配乐" if not bgm_path else "使用固定配乐"

        user_prompt = (
            "请根据动画规划书完成代码实现。\n"
            "你必须通过工具读取 tmp/base_scene.py，不要要求外部再传 Manim 对象代码。\n\n"
            f"用户配音偏好：{tts_hint}，{bgm_hint}。\n\n"
            f"动画规划书：\n---------------\n{director_plan}\n--------------------------"
        )
        messages.append(HumanMessage(content=user_prompt))

        final_text = ""
        max_rounds = 16
        # Allow exactly one retry after the first render failure.
        # This means we stop after 2 total failures.
        max_video_failures = 2
        video_failure_count = 0
        coder_failed = False
        repeated_sig = None
        repeated_count = 0
        tool_history: List[str] = []
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
                if tool is None:
                    result = f"Unknown tool: {name}"
                else:
                    try:
                        result = tool.invoke(args)
                    except Exception as exc:
                        result = f"Tool execution failed: {exc}"
                _append_coder_tool_log(
                    f"[round {round_idx + 1}] RESULT {name} -> {str(result)[:2000]}"
                )

                # Track make_manim_video failures
                if name == "make_manim_video":
                    last_make_video_result = str(result)
                    try:
                        parsed = json.loads(result)
                        last_make_video_ok = bool(parsed.get("ok", False))
                        if not last_make_video_ok:
                            video_failure_count += 1
                    except (json.JSONDecodeError, TypeError):
                        last_make_video_ok = False
                        video_failure_count += 1
                elif name == "write_narration_script" and last_make_video_result:
                    post_render_narration_writes += 1

                tool_history.append(f"[round {round_idx + 1}] {name}({args}) -> {str(result)[:260]}")
                messages.append(ToolMessage(content=str(result), tool_call_id=call_id))

            # Exit immediately once video render succeeds to avoid redundant tool loops.
            if last_make_video_ok:
                shared_state.setdefault(
                    "summary",
                    f"视频已成功生成，Coder 自动结束。\n最后一次 make_manim_video 结果：\n{last_make_video_result}",
                )
                break

            # Guardrail: after render has already been attempted, repeated narration rewrites
            # indicate the agent drifted away from fixing the concrete render error.
            if post_render_narration_writes >= 2 and video_failure_count > 0:
                coder_failed = True
                shared_state.setdefault(
                    "summary",
                    "Coder 在渲染失败后陷入旁白反复改写，未继续修复场景错误，已提前终止以避免无效轮次消耗。"
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
                            "不要输出工作总结，只输出错误分析。"
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
                if not shared_state.get("summary"):
                    shared_state["summary"] = self._normalize_content(
                        getattr(response, "content", "make_manim_video 失败，未能生成视频。")
                    )
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
                shared_state.setdefault(
                    "summary",
                    f"Coder 达到最大轮次。最后一次渲染结果：\n{last_make_video_result}",
                )
            else:
                shared_state.setdefault("summary", f"Coder 达到最大轮次。\n{summary}")

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
        director_plan: str,
        render_options: dict,
        run_dir: Path,
        on_progress: Callable[[str], None] | None = None,
        on_stage_result: Callable[[str, str], None] | None = None,
    ) -> AgentOutputs:
        """Re-run only the coder step, reusing prior workflow outputs."""
        emit_progress = on_progress or (lambda _msg: None)
        emit_stage = on_stage_result or (lambda _stage, _content: None)

        self._cancel_requested = False
        emit_progress("Coder 姝ｅ湪閲嶆柊缂栨帓鍦烘櫙骞惰皟鐢ㄥ伐鍏?..")
        coder_output, coder_failed = self._run_coder(
            director_plan,
            render_options,
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

        return AgentOutputs(
            solver_answer=solver_answer,
            architect_code=architect_code,
            director_plan=director_plan,
            coder_output=coder_output,
            run_dir=run_dir,
            coder_failed=coder_failed,
        )

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
    def _prepare_tmp_workspace() -> Path:
        tmp_dir = Path(__file__).resolve().parents[2] / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        for item in tmp_dir.iterdir():
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
        return tmp_dir

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
