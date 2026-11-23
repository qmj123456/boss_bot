"""BOSS greeting message generation capability."""

from __future__ import annotations

import json
from typing import Any

from ai.client import AIClient, OpenAICompatibleClient
from boss_hr.config_manager import ConfigManager, get_config


class MessageGenerator:
    """Generate one BOSS greeting message."""

    def __init__(
        self,
        *,
        config: ConfigManager | None = None,
        client: AIClient | None = None,
    ) -> None:
        self.config = config or get_config()
        self.client = client or OpenAICompatibleClient(self.config)

    def generate(
        self,
        *,
        candidate_info: dict[str, Any] | str,
        job_requirement: dict[str, Any] | str,
    ) -> str:
        prompt_name = self.config.get("ai.prompts.message_generation", "generate_message.md")
        system_prompt = self.config.load_prompt(str(prompt_name))
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": self._build_user_prompt(candidate_info, job_requirement)},
        ]
        return _clean_message(self.client.complete_text(messages))

    @staticmethod
    def _build_user_prompt(
        candidate_info: dict[str, Any] | str,
        job_requirement: dict[str, Any] | str,
    ) -> str:
        payload = {
            "candidate_info": candidate_info,
            "job_requirement": job_requirement,
            "output": "只输出一条可直接发送到 BOSS 的招呼语，不要解释。",
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)


def _clean_message(text: str) -> str:
    return " ".join(str(text or "").strip().split())


__all__ = ["MessageGenerator"]
