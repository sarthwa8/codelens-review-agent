from app.config import Settings
from app.llm.adapter import LLMError, LLMEvent, LLMProvider, LLMRequest, TextDelta, Usage


def get_provider(settings: Settings) -> LLMProvider:
    if settings.llm_provider == "anthropic":
        from app.llm.anthropic_provider import AnthropicProvider

        return AnthropicProvider(settings.anthropic_api_key, settings.anthropic_model)
    if settings.llm_provider == "openai":
        from app.llm.openai_provider import OpenAIProvider

        return OpenAIProvider(settings.openai_api_key, settings.openai_model)
    if settings.llm_provider == "groq":
        from app.llm.groq_provider import GroqProvider

        return GroqProvider(
            settings.groq_api_key,
            settings.groq_model,
            base_url=settings.groq_base_url,
            reasoning_effort=settings.groq_reasoning_effort,
        )
    if settings.llm_provider == "ollama":
        from app.llm.ollama_provider import OllamaProvider

        return OllamaProvider(settings.ollama_base_url, settings.ollama_model)
    from app.llm.fake_provider import FakeProvider

    return FakeProvider(settings.llm_model, settings.fake_llm_delay_ms)


__all__ = [
    "LLMError",
    "LLMEvent",
    "LLMProvider",
    "LLMRequest",
    "TextDelta",
    "Usage",
    "get_provider",
]
