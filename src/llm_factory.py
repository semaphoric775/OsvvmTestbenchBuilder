"""Return a LangChain chat model based on available API keys.

Priority: OPENAI_API_KEY → ANTHROPIC_API_KEY.
Raises RuntimeError if neither is set.
"""

from __future__ import annotations

import os
from langchain_core.language_models.chat_models import BaseChatModel


def get_llm(temperature: float = 0.2) -> BaseChatModel:
    if os.environ.get("OPENAI_API_KEY"):
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model="gpt-4o-mini", temperature=temperature)

    if os.environ.get("ANTHROPIC_API_KEY"):
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model="claude-haiku-4-5-20251001", temperature=temperature)

    raise RuntimeError(
        "No LLM API key found. Set OPENAI_API_KEY or ANTHROPIC_API_KEY."
    )
