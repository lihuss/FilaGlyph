from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict


ROLE_NAMES = ("solver", "architect", "director", "coder")


@dataclass
class RoleConfig:
    name: str
    provider: str
    api_key: str
    model: str
    base_url: str | None = None


@dataclass
class AgentConfig:
    roles: Dict[str, RoleConfig]
    timeout_s: int


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "agents_credentials.json"


def infer_provider(model: str, base_url: str | None, provider_hint: str | None = None) -> str:
    if provider_hint:
        hint = provider_hint.strip().lower()
        if hint == "gemini":
            return "google"
        return hint

    model_l = (model or "").strip().lower()
    base_l = (base_url or "").strip().lower()

    if "gemini" in model_l or "googleapis.com" in base_l:
        return "google"
    if "qwen" in model_l or "dashscope" in base_l:
        return "qwen"
    if "deepseek" in model_l or "deepseek.com" in base_l:
        return "deepseek"
    return "openai"


def _coerce_role(name: str, raw: Dict[str, Any]) -> RoleConfig:
    return RoleConfig(
        name=name,
        provider=str(raw.get("provider", "openai")),
        api_key=str(raw.get("api_key", "")),
        model=str(raw.get("model", "")),
        base_url=raw.get("base_url"),
    )


def _load_schema(raw: Dict[str, Any]) -> AgentConfig:
    roles_raw = raw.get("roles")
    if not isinstance(roles_raw, dict):
        raise ValueError("Invalid config: missing 'roles' object")
    roles = {name: _coerce_role(name, roles_raw.get(name, {})) for name in ROLE_NAMES}
    timeout_s = int(raw.get("timeouts", {}).get("default_s", 300))
    return AgentConfig(roles=roles, timeout_s=timeout_s)


def load_agent_config(path: Path | None = None) -> AgentConfig:
    config_path = path or DEFAULT_CONFIG_PATH
    if not config_path.exists():
        raise FileNotFoundError(f"Agent config not found: {config_path}")

    raw = json.loads(config_path.read_text(encoding="utf-8"))
    return _load_schema(raw)


def load_agent_config_json(path: Path | None = None) -> Dict[str, Any]:
    config_path = path or DEFAULT_CONFIG_PATH
    if not config_path.exists():
        raise FileNotFoundError(f"Agent config not found: {config_path}")
    return json.loads(config_path.read_text(encoding="utf-8"))


def save_agent_config_json(raw: Dict[str, Any], path: Path | None = None) -> Path:
    config_path = path or DEFAULT_CONFIG_PATH
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    return config_path
