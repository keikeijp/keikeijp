"""Claude の tool_runner でエージェントループを回す。"""

from __future__ import annotations

import os
from typing import Callable, Optional

import anthropic

from .prompt import SYSTEM_PROMPT
from .session import Session
from .tools import build_tools

DEFAULT_MODEL = os.environ.get("DTM_AGENT_MODEL", "claude-opus-5")


def run_agent(prompt: str, session: Session, *, client: Optional[anthropic.Anthropic] = None,
              model: str = DEFAULT_MODEL, max_iterations: int = 40,
              on_message: Optional[Callable[[anthropic.types.beta.BetaMessage], None]] = None,
              fallbacks: bool = True) -> str:
    """プロンプトを実行し、最終的なテキスト応答を返す。"""
    client = client or anthropic.Anthropic()
    extra: dict = {}
    if fallbacks and os.environ.get("DTM_AGENT_FALLBACKS", "1") != "0":
        # 安全分類器による refusal 時にサーバ側で代替モデルへ自動フォールバックする
        extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}

    runner = client.beta.messages.tool_runner(
        model=model,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        tools=build_tools(session),
        messages=[{"role": "user", "content": prompt}],
        max_iterations=max_iterations,
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        **extra,
    )
    final_text: list[str] = []
    for message in runner:
        if on_message:
            on_message(message)
        if message.stop_reason == "refusal":
            return "リクエストは安全上の理由で処理できませんでした。"
        final_text = [b.text for b in message.content if b.type == "text"]
    return "\n".join(final_text)
