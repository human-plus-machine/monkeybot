"""Optional LLM providers (lazy-import heavy SDKs inside ``stream()``)."""

from monkeybot.providers.claude import ClaudeProvider
from monkeybot.providers.gemini import GeminiProvider
from monkeybot.providers.huggingface import HuggingFaceProvider
from monkeybot.providers.nvidia import NvidiaProvider
from monkeybot.providers.ollama import OllamaProvider
from monkeybot.providers.openai import OpenAIProvider
from monkeybot.providers.openrouter import OpenRouterProvider
from monkeybot.providers.vertex_claude import VertexClaudeProvider

__all__ = [
    "ClaudeProvider",
    "GeminiProvider",
    "HuggingFaceProvider",
    "NvidiaProvider",
    "OllamaProvider",
    "OpenAIProvider",
    "OpenRouterProvider",
    "VertexClaudeProvider",
]
