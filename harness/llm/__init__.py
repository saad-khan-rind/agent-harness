"""LLM client implementations and the protocol the loop depends on."""

from harness.llm.openai_compatible import OpenAICompatibleClient
from harness.llm.protocol import LLMClient, LLMError, LLMResponse, ToolCall

__all__ = ["OpenAICompatibleClient", "LLMClient", "LLMError", "LLMResponse", "ToolCall"]
