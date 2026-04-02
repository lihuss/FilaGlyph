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

    tmp_dir = project_root / "tmp"
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

    def _scene_path(scene_index: int) -> Path:
        if scene_index < 1:
            raise ValueError("scene_index must be >= 1")
        scene_dir = tmp_dir / f"scene{scene_index}"
        return scene_dir / f"scene{scene_index}.py"

    def _scene_files() -> List[Path]:
        files = sorted(
            tmp_dir.glob("scene*/scene*.py"),
            key=lambda p: int(re.findall(r"\d+", p.stem)[0]),
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

    def _safe_rel_path(path: str) -> Path:
        clean = path.replace("\\", "/").strip().lstrip("/")
        candidate = (project_root / clean).resolve()
        project = project_root.resolve()
        if not str(candidate).startswith(str(project)):
            raise ValueError("Path must stay inside project root")
        return candidate

    @tool
    def write_scene_code(scene_index: int, code: str) -> str:
        """Write Manim scene code to tmp/scene{index}/scene{index}.py."""
        path = _scene_path(scene_index)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(code, encoding="utf-8")
        return json.dumps({"ok": True, "path": str(path), "bytes": len(code.encode("utf-8"))}, ensure_ascii=False)

    @tool
    def list_scene_files() -> str:
        """List all existing scene files under tmp/scene*/scene*.py."""
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
        """Read a scene code file from tmp/scene{index}/scene{index}.py."""
        path = _scene_path(scene_index)
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(f"Scene file not found: {path}")
        return path.read_text(encoding="utf-8")

    @tool
    def write_narration_script(content: str, mode: str = "overwrite") -> str:
        """Write narration text to tmp/narration.txt. mode: overwrite|append."""
        if mode not in {"overwrite", "append"}:
            raise ValueError("mode must be 'overwrite' or 'append'")
        path = tmp_dir / "narration.txt"
        if mode == "append" and path.exists():
            existing = path.read_text(encoding="utf-8")
            next_content = existing.rstrip() + "\n" + content.strip() + "\n"
        else:
            next_content = content.strip() + "\n"
        path.write_text(next_content, encoding="utf-8")
        return json.dumps({"ok": True, "path": str(path), "chars": len(next_content)}, ensure_ascii=False)

    @tool
    def validate_python_syntax(path: str) -> str:
        """Validate Python syntax for a file path."""
        target = _safe_rel_path(path)
        if not target.exists():
            raise FileNotFoundError(f"File not found: {target}")
        source = target.read_text(encoding="utf-8")
        ast.parse(source, filename=str(target))
        return json.dumps({"ok": True, "path": str(target)}, ensure_ascii=False)

    @tool
    def validate_scene_syntax() -> str:
        """Validate syntax for all tmp/scene*/scene*.py files."""
        scene_files = _scene_files()
        if not scene_files:
            raise FileNotFoundError("No scene files found in tmp/")
        results = []
        for path in scene_files:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            results.append(str(path))
        return json.dumps({"ok": True, "count": len(results), "files": results}, ensure_ascii=False)

    @tool
    def make_manim_video(
        output: str,
        quality: str = "h",
        fps: int = 30,
        resolution: str = "1920,1080",
        tts_script_file: str = "tmp/narration.txt",
    ) -> str:
        """Run makevideo.py using generated scenes and narration."""
        scene_files = _scene_files()
        if not scene_files:
            raise FileNotFoundError("No scene files found in tmp/")

        scene_files_arg = ",".join([str(p.relative_to(project_root)).replace("\\", "/") for p in scene_files])
        scene_names_arg = ",".join([_infer_scene_class_name(p) for p in scene_files])

        output_rel = output.replace("\\", "/")
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
        ]
        if agent_run_dir is not None:
            runtime_log_file = (agent_run_dir / "makevideo.log").as_posix()
            command.extend(["--runtime-log-file", runtime_log_file])
        command.extend([
            "--tts-script-file", tts_script_file.replace("\\", "/"),
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
            "command": " ".join(command),
            "runtime_log_file": runtime_log_file,
            "stdout_file": str(stdout_file),
            "stderr_file": str(stderr_file),
        }

        if ok:
            # Success path: keep response compact; full logs remain in debug files.
            payload["stdout_preview"] = stdout_text[-1200:]
            payload["stderr_preview"] = stderr_text[-1200:]
        else:
            # Failure path: expose only focused error content to save tokens.
            stdout_error = _extract_error_output(stdout_text)
            stderr_error = _extract_error_output(stderr_text)
            payload["stdout_error"] = stdout_error
            payload["stderr_error"] = stderr_error
            payload["error_summary"] = stderr_error or stdout_error or "makevideo failed; inspect debug logs"
            payload["next_action"] = "Use runtime_log_file or stdout_file/stderr_file with read_text_file for full context"

        return json.dumps(payload, ensure_ascii=False)

    @tool
    def read_text_file(path: str) -> str:
        """Read a text file under project root."""
        target = _safe_rel_path(path)
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
        write_narration_script,
        validate_python_syntax,
        validate_scene_syntax,
        make_manim_video,
        read_text_file,
        report_summary,
    ]
